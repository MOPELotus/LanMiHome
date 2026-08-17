from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import struct
import time
from collections import deque
from dataclasses import dataclass

from bleak import BleakClient
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.hmac import HMAC as CryptoHMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .protocol import (
    CHAR_AUTH_CTRL,
    CHAR_AUTH_DATA,
    CHAR_CMD_RECV,
    CHAR_CMD_SEND,
    CHAR_DEVICE_INFO,
    CHAR_FW_VERSION,
    mac_to_miot_bytes,
)

_LOG = logging.getLogger("lanmihome.cuktech")


@dataclass(slots=True)
class DeviceInfo:
    model: str = ""
    firmware: str = ""
    protocol_version: str = ""


class AuthenticationError(RuntimeError):
    pass


class CuktechSession:
    """One isolated MiOT BLE session for one CUKTECH AD1204 charger."""

    def __init__(self, name: str, address: str, token: bytes):
        if len(token) != 12:
            raise ValueError(f"{name}: token must be exactly 12 bytes")
        self.name = name
        self.address = address
        self.token = token
        self.mac_bytes = mac_to_miot_bytes(address)

        self.client: BleakClient | None = None
        self.authenticated = False
        self.info = DeviceInfo()

        self._queues: dict[str, asyncio.Queue[bytes]] = {}
        self._send_it = 0
        self._dev_it_hi = 0
        self._last_dev_it_lo = 0
        self._session_keys: dict[str, bytes] | None = None
        self._init_frames: deque[bytes] = deque()

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    def _handler(self, channel: str):
        queue = self._queues.setdefault(channel, asyncio.Queue(maxsize=1024))

        def callback(_sender, data):
            payload = bytes(data)
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

        return callback

    async def _wait(self, channel: str, timeout: float = 5.0) -> bytes | None:
        queue = self._queues.get(channel)
        if queue is None:
            return None
        try:
            return await asyncio.wait_for(queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def connect(self) -> None:
        _LOG.info("[%s] connecting to %s", self.name, self.address)
        self.client = BleakClient(self.address)
        await self.client.connect()

        for char, channel in (
            (CHAR_CMD_RECV, "cmd_recv"),
            (CHAR_CMD_SEND, "cmd_send"),
            (CHAR_DEVICE_INFO, "dev_info"),
            (CHAR_AUTH_DATA, "auth_data"),
        ):
            await self.client.start_notify(char, self._handler(channel))

        _LOG.info(
            "[%s] connected (MTU=%s)",
            self.name,
            getattr(self.client, "mtu_size", "?"),
        )

    async def disconnect(self) -> None:
        self.authenticated = False
        if not self.client:
            return
        for char, channel in (
            (CHAR_AUTH_CTRL, "auth_ctrl"),
            (CHAR_AUTH_DATA, "auth_data"),
            (CHAR_CMD_SEND, "cmd_send"),
            (CHAR_CMD_RECV, "cmd_recv"),
            (CHAR_DEVICE_INFO, "dev_info"),
        ):
            if channel not in self._queues or not self.client.is_connected:
                continue
            try:
                await self.client.stop_notify(char)
            except Exception:
                pass
        if self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None

    async def read_device_info(self) -> DeviceInfo:
        self._require_client()

        await self.client.write_gatt_char(CHAR_DEVICE_INFO, b"\x00", response=False)
        data = await self._wait("dev_info", 3.0)
        if data and len(data) >= 3:
            self.info.protocol_version = f"{data[1]}.{data[2]}"

        await self.client.write_gatt_char(CHAR_DEVICE_INFO, b"\x03", response=False)
        data = await self._wait("dev_info", 3.0)
        if data and len(data) > 2:
            size = data[1]
            chip = data[2 : 2 + size].decode("ascii", errors="replace")
            self.info.model = f"njcuk.fitting.ad1204_{chip}"

        try:
            raw = await self.client.read_gatt_char(CHAR_FW_VERSION)
            self.info.firmware = bytes(raw).rstrip(b"\x00").decode("ascii", errors="replace")
        except Exception:
            self.info.firmware = ""

        return self.info

    async def authenticate(self) -> None:
        self._require_client()
        self.authenticated = False
        self._drain_queue("auth_data")

        # Phase A: MiOT BLE protocol initialization.
        await self.client.write_gatt_char(CHAR_AUTH_CTRL, b"\xA4", response=False)
        init_resp = await self._wait("auth_data", 3.0)
        if not init_resp:
            raise AuthenticationError("no protocol-init response")

        ack = bytearray(init_resp)
        if len(ack) >= 3:
            ack[2] = (ack[2] + 1) & 0xFF
        await self.client.write_gatt_char(CHAR_AUTH_DATA, bytes(ack), response=False)

        key_data = await self._wait("auth_data", 5.0)
        if key_data and len(key_data) < 20 and len(key_data) >= 3 and key_data[2] == 0x04:
            deadline = asyncio.get_running_loop().time() + 12.0
            while asyncio.get_running_loop().time() < deadline:
                remaining = deadline - asyncio.get_running_loop().time()
                extra = await self._wait("auth_data", min(3.0, remaining))
                if extra and len(extra) >= 20 and extra[2] == 0x04:
                    key_data = extra
                    break
        if not key_data:
            raise AuthenticationError("no key-exchange response")

        placeholder = b"\x00\x00\x05\x01" + (b"\xF2" * max(0, len(key_data) - 4))
        await self.client.write_gatt_char(CHAR_AUTH_DATA, placeholder, response=False)
        await asyncio.sleep(0.6)
        self._drain_queue("auth_data")

        try:
            await self.client.start_notify(CHAR_AUTH_CTRL, self._handler("auth_ctrl"))
        except Exception:
            if "auth_ctrl" not in self._queues:
                raise

        await asyncio.sleep(0.05)
        await self.client.write_gatt_char(CHAR_AUTH_CTRL, b"\x24\x00\x00\x00", response=False)

        rand_key = secrets.token_bytes(16)
        await self.client.write_gatt_char(
            CHAR_AUTH_DATA,
            b"\x00\x00\x00\x0B\x01\x00",
            response=False,
        )

        if not await self._wait_for_exact("auth_data", b"\x00\x00\x01\x01", retries=5):
            raise AuthenticationError("device did not become ready for app random key")

        await self.client.write_gatt_char(
            CHAR_AUTH_DATA,
            b"\x01\x00" + rand_key,
            response=False,
        )
        if await self._wait("auth_data", 3.0) != b"\x00\x00\x01\x00":
            raise AuthenticationError("device did not acknowledge app random key")

        dev_random = await self._recv_auth_payload()
        if not dev_random or len(dev_random) < 16:
            raise AuthenticationError("invalid device random key")
        dev_random = dev_random[:16]

        dev_hmac = await self._recv_auth_payload()
        if not dev_hmac or len(dev_hmac) < 32:
            raise AuthenticationError("invalid device HMAC")
        dev_hmac = dev_hmac[:32]

        salt = rand_key + dev_random
        salt_inv = dev_random + rand_key
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=salt,
            info=b"mible-login-info",
            backend=default_backend(),
        ).derive(self.token)

        self._session_keys = {
            "dev_key": derived[0:16],
            "app_key": derived[16:32],
            "dev_iv": derived[32:36],
            "app_iv": derived[36:40],
        }

        verify = CryptoHMAC(self._session_keys["dev_key"], hashes.SHA256())
        verify.update(salt_inv)
        if not hmac.compare_digest(verify.finalize(), dev_hmac):
            raise AuthenticationError("device HMAC verification failed")

        ours = CryptoHMAC(self._session_keys["app_key"], hashes.SHA256())
        ours.update(salt)
        our_hmac = ours.finalize()

        await self.client.write_gatt_char(
            CHAR_AUTH_DATA,
            b"\x00\x00\x00\x0A\x01\x00",
            response=False,
        )
        if await self._wait("auth_data", 3.0) != b"\x00\x00\x01\x01":
            raise AuthenticationError("device did not become ready for app HMAC")

        await self.client.write_gatt_char(
            CHAR_AUTH_DATA,
            b"\x01\x00" + our_hmac,
            response=False,
        )
        await self._wait("auth_data", 3.0)

        # AD1204 sends a second challenge/response round before auth_ctrl success.
        response_data = None
        deadline = asyncio.get_running_loop().time() + 8.0
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            data = await self._wait("auth_data", min(remaining, 3.0))
            if not data:
                continue
            if len(data) >= 3 and data[2] == 0x0D:
                await self.client.write_gatt_char(
                    CHAR_AUTH_DATA, b"\x00\x00\x03\x00", response=False
                )
            elif len(data) >= 3 and data[2] == 0x0C:
                response_data = data
                await self.client.write_gatt_char(
                    CHAR_AUTH_DATA, b"\x00\x00\x03\x00", response=False
                )
                await self.client.write_gatt_char(
                    CHAR_AUTH_DATA, b"\x00\x00\x00\x0A\x01\x00", response=False
                )
            elif data in (b"\x00\x00\x01\x01", b"\x00\x00\x01\x00"):
                break

        if response_data:
            second = b"\x01\x00\x0C" + response_data[3:]
            await self.client.write_gatt_char(CHAR_AUTH_DATA, second, response=False)
            await self._wait("auth_data", 3.0)

        result = await self._wait("auth_ctrl", 5.0)
        if not result or result[0] not in (0x21, 0x11):
            code = result[0] if result else None
            raise AuthenticationError(f"login failed (auth_ctrl={code!r})")

        self.authenticated = True
        self._send_it = 0
        self._dev_it_hi = 0
        self._last_dev_it_lo = 0
        await asyncio.sleep(0.5)
        await self._capture_initial_pushes()
        _LOG.info("[%s] authenticated", self.name)

    async def next_plaintext(self, timeout: float = 5.0) -> bytes | None:
        if self._init_frames:
            frame = self._init_frames.popleft()
            return self.decrypt(frame[4:])

        data = await self._wait("cmd_recv", timeout)
        if not data or len(data) < 4:
            return None

        if data[2] == 0x02:
            await self.client.write_gatt_char(
                CHAR_CMD_RECV, b"\x00\x00\x03\x00", response=False
            )
            return self.decrypt(data[4:])

        if data[2] == 0x00 and len(data) >= 6:
            count = min(data[4] | (data[5] << 8), 100)
            await self.client.write_gatt_char(
                CHAR_CMD_RECV, b"\x00\x00\x01\x01", response=False
            )
            for _ in range(count):
                await self._wait("cmd_recv", 3.0)
            await self.client.write_gatt_char(
                CHAR_CMD_RECV, b"\x00\x00\x01\x00", response=False
            )
        return None

    def decrypt(self, data: bytes) -> bytes | None:
        if not self._session_keys or len(data) < 6:
            return None

        it_lo = struct.unpack("<H", data[:2])[0]
        if it_lo < self._last_dev_it_lo and (self._last_dev_it_lo - it_lo) > 32768:
            self._dev_it_hi += 1
        self._last_dev_it_lo = it_lo

        it_bytes = struct.pack("<I", (self._dev_it_hi << 16) | it_lo)
        nonce = self._session_keys["dev_iv"] + (b"\x00" * 4) + it_bytes
        try:
            return AESCCM(self._session_keys["dev_key"], tag_length=4).decrypt(
                nonce, data[2:], None
            )
        except Exception as exc:
            _LOG.warning("[%s] decrypt failed: %s", self.name, exc)
            return None

    async def _recv_auth_payload(self) -> bytes | None:
        data = await self._wait("auth_data", 3.0)
        if not data or len(data) < 4:
            return None

        if data[2] == 0x02:
            payload = data[4:]
            await self.client.write_gatt_char(
                CHAR_AUTH_DATA, b"\x00\x00\x03\x00", response=False
            )
            return payload

        if data[2] == 0x00 and len(data) >= 6:
            count = min(data[4] | (data[5] << 8), 100)
            await self.client.write_gatt_char(
                CHAR_AUTH_DATA, b"\x00\x00\x01\x01", response=False
            )
            chunks: list[bytes] = []
            for _ in range(count):
                frame = await self._wait("auth_data", 3.0)
                if not frame:
                    break
                chunks.append(frame[2:])
            await self.client.write_gatt_char(
                CHAR_AUTH_DATA, b"\x00\x00\x01\x00", response=False
            )
            return b"".join(chunks)

        return None

    async def _wait_for_exact(self, channel: str, expected: bytes, retries: int) -> bool:
        for _ in range(retries):
            data = await self._wait(channel, 3.0)
            if data == expected:
                return True
        return False

    def _drain_queue(self, channel: str) -> None:
        queue = self._queues.get(channel)
        if not queue:
            return
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _capture_initial_pushes(self) -> None:
        started = time.monotonic()
        while time.monotonic() - started < 6.0 and len(self._init_frames) < 60:
            data = await self._wait("cmd_recv", 0.8)
            if not data:
                break
            if len(data) >= 4 and data[2] == 0x02:
                self._init_frames.append(data)
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, b"\x00\x00\x03\x00", response=False
                )
            elif len(data) >= 6 and data[2] == 0x00:
                count = min(data[4] | (data[5] << 8), 100)
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, b"\x00\x00\x01\x01", response=False
                )
                for _ in range(count):
                    await self._wait("cmd_recv", 3.0)
                await self.client.write_gatt_char(
                    CHAR_CMD_RECV, b"\x00\x00\x01\x00", response=False
                )

    def _require_client(self) -> None:
        if not self.client or not self.client.is_connected:
            raise ConnectionError(f"{self.name}: BLE client is not connected")
