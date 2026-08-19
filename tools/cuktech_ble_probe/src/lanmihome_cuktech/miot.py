from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from .protocol import CHAR_CMD_RECV, CHAR_CMD_SEND
from .session import CuktechSession


@dataclass(slots=True)
class MiotResult:
    siid: int
    piid: int
    value: int | None = None
    raw: bytes | None = None
    acknowledged: bool = False
    deferred_plaintexts: list[bytes] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.acknowledged or self.raw is not None


class MiotCommandClient:
    """Serialized encrypted MiOT property command channel for one session."""

    def __init__(self, session: CuktechSession):
        self.session = session
        self._lock = asyncio.Lock()
        self._seq = 1

    async def get(self, siid: int, piid: int, timeout: float = 8.0) -> MiotResult:
        async with self._lock:
            result = MiotResult(siid=siid, piid=piid)
            seq = self._next_seq()
            if not await self._send_encrypted(self._build_tlv(seq, siid, piid)):
                return result
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return result
                data = await self.session._wait("cmd_recv", min(remaining, 3.0))
                if not data:
                    continue
                plaintext = await self._decode_transport(data)
                if plaintext is None:
                    continue
                opcode, got_siid, got_piid = self._header(plaintext)
                if opcode == 0x03 and got_siid == siid and got_piid == piid:
                    result.raw = plaintext
                    result.value = self._extract_get_value(plaintext)
                    result.acknowledged = True
                    return result
                if opcode == 0x04:
                    result.deferred_plaintexts.append(plaintext)

    async def set(self, siid: int, piid: int, value: int, timeout: float = 8.0) -> MiotResult:
        if value < 0 or value > 0xFFFFFFFF:
            raise ValueError("MiOT property value must fit UINT32")
        async with self._lock:
            result = MiotResult(siid=siid, piid=piid, value=value)
            seq = self._next_seq()
            if not await self._send_encrypted(self._build_tlv(seq, siid, piid, value)):
                return result
            deadline = asyncio.get_running_loop().time() + timeout
            got_ack = False
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    result.acknowledged = got_ack
                    return result
                data = await self.session._wait("cmd_recv", min(remaining, 3.0))
                if not data:
                    if got_ack:
                        result.acknowledged = True
                        return result
                    continue
                plaintext = await self._decode_transport(data)
                if plaintext is None:
                    continue
                opcode, got_siid, got_piid = self._header(plaintext)
                if got_siid == siid and got_piid == piid:
                    if opcode == 0x01:
                        got_ack = True
                        # Upstream devices sometimes stop at ACK without a separate result.
                        deadline = min(deadline, asyncio.get_running_loop().time() + 1.0)
                        continue
                    if opcode == 0x04:
                        result.raw = plaintext
                        result.acknowledged = True
                        parsed = self._extract_push_value(plaintext)
                        if parsed is not None:
                            result.value = parsed
                        return result
                if opcode == 0x04:
                    result.deferred_plaintexts.append(plaintext)

    async def get_many(self, properties: list[tuple[int, int]]) -> dict[tuple[int, int], MiotResult]:
        # The AD1204 command channel is request/response serialized. Do not gather
        # concurrent commands on one BLE session even though different chargers
        # can operate concurrently.
        results: dict[tuple[int, int], MiotResult] = {}
        for siid, piid in properties:
            results[(siid, piid)] = await self.get(siid, piid)
        return results

    async def keepalive(self) -> None:
        client = self.session.client
        if client and client.is_connected:
            await client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x00\x00", response=False)

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        if self._seq == 0:
            self._seq = 1
        return seq

    @staticmethod
    def _build_tlv(seq: int, siid: int, piid: int, value: int | None = None) -> bytes:
        if value is None:
            opcode = 0x02
            type_id = 1
            value_bytes = b"\x00"
        elif value <= 0xFF:
            opcode = 0x00
            type_id = 1
            value_bytes = bytes([value])
        else:
            opcode = 0x00
            type_id = 5
            value_bytes = value.to_bytes(4, "little")
        type_len = (type_id << 12) | len(value_bytes)
        total_len = 11 + len(value_bytes)
        return bytes([
            total_len & 0xFF,
            0x20,
            seq & 0xFF,
            0x00,
            opcode,
            0x01,
            siid & 0xFF,
            piid & 0xFF,
            (piid >> 8) & 0xFF,
            type_len & 0xFF,
            (type_len >> 8) & 0xFF,
        ]) + value_bytes

    def _encrypt(self, plaintext: bytes) -> bytes:
        keys = self.session._session_keys
        if not keys:
            raise RuntimeError("MiOT session keys are not available")
        it = self.session._send_it
        it_bytes = struct.pack("<I", it)
        nonce = keys["app_iv"] + (b"\x00" * 4) + it_bytes
        ciphertext = AESCCM(keys["app_key"], tag_length=4).encrypt(nonce, plaintext, None)
        self.session._send_it += 1
        return it_bytes[:2] + ciphertext

    async def _send_encrypted(self, plaintext: bytes) -> bool:
        client = self.session.client
        if not client or not client.is_connected or not self.session.authenticated:
            return False
        self.session._drain_queue("cmd_send")
        encrypted = self._encrypt(plaintext)
        await client.write_gatt_char(CHAR_CMD_SEND, b"\x00\x00\x00\x00\x01\x00", response=False)
        if await self.session._wait("cmd_send", 3.0) != b"\x00\x00\x01\x01":
            return False
        await client.write_gatt_char(CHAR_CMD_SEND, b"\x01\x00" + encrypted, response=False)
        return await self.session._wait("cmd_send", 3.0) == b"\x00\x00\x01\x00"

    async def _decode_transport(self, data: bytes) -> bytes | None:
        if len(data) >= 4 and data[2] == 0x02:
            await self.session.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x03\x00", response=False)
            return self.session.decrypt(data[4:])
        if len(data) >= 6 and data[2] == 0x00:
            await self._consume_multiframe(data)
        return None

    async def _consume_multiframe(self, data: bytes) -> None:
        count = min(data[4] | (data[5] << 8), 100)
        await self.session.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x01\x01", response=False)
        for _ in range(count):
            await self.session._wait("cmd_recv", 3.0)
        await self.session.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x01\x00", response=False)

    @staticmethod
    def _header(plaintext: bytes) -> tuple[int, int, int]:
        if len(plaintext) < 9:
            return -1, -1, -1
        return plaintext[4], plaintext[6], plaintext[7] | (plaintext[8] << 8)

    @staticmethod
    def _extract_get_value(plaintext: bytes) -> int | None:
        # GET result: ... piid(2) status(2) value_len(1) type(1) value(N)
        if len(plaintext) < 14:
            return None
        value_len = plaintext[11]
        start = 13
        if value_len <= 0 or len(plaintext) < start + value_len:
            return None
        return int.from_bytes(plaintext[start : start + value_len], "little")

    @staticmethod
    def _extract_push_value(plaintext: bytes) -> int | None:
        # Push/result opcode 0x04: ... piid(2) type_len(2) value(N)
        if len(plaintext) < 11:
            return None
        type_len = int.from_bytes(plaintext[9:11], "little")
        value_len = type_len & 0x0FFF
        start = 11
        if value_len <= 0 or len(plaintext) < start + value_len:
            return None
        return int.from_bytes(plaintext[start : start + value_len], "little")
