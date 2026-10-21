from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import logging
import time

from bleak import BleakClient

WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
VERIFIED_FIRMWARE = "V3020.07"
LOG = logging.getLogger("lanmihome.sp651e")


def frame(command: int, payload: bytes) -> bytes:
    return bytes([0x53, command, 0, 1, 0, len(payload)]) + payload


def decode_status(data: bytes) -> dict:
    if len(data) < 37 or data[:5] != bytes([0x53, 2, 0, 1, 0]) or len(data) != data[5] + 6:
        raise ValueError("invalid SP651E status frame")
    firmware = data[11:19].decode("ascii")
    if firmware != VERIFIED_FIRMWARE or data[19] != 1:
        raise ValueError("SP651E firmware or light type is not verified")
    if data[29] not in (0, 1) or data[36] == 0:
        raise ValueError("invalid SP651E power or brightness state")
    return {
        "power": bool(data[29]),
        "brightness": round(data[36] * 100 / 255),
        "raw_brightness": data[36],
        "firmware": firmware,
        "mode": data[32],
        "effect": data[33],
    }


def control_frames(changes: dict, current: dict) -> list[bytes]:
    commands = []
    if "brightness" in changes:
        if current["mode"] != 2 or current["effect"] != 1:
            commands.append(frame(0x53, bytes([2, 1])))
        commands.append(frame(0x51, bytes([1, round(changes["brightness"] * 255 / 100)])))
    if "power" in changes:
        commands.append(frame(0x50, bytes([int(changes["power"])])))
    return commands


class SP651ETransport:
    def __init__(self, runtime, address: str, acquire, release):
        self.runtime = runtime
        self.address = address
        self.acquire = acquire
        self.release = release
        self.firmware = None
        self.mode = None
        self._cached = None
        self._updated = 0.0
        self._client = None
        self._queue = None
        self._session_lock = None
        self._worker = None

    def start(self) -> None:
        if self.runtime.loop is not None:
            self.runtime.loop.call_soon_threadsafe(self._start_worker)

    def _start_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._poll(), name="sp651e-poll")

    async def _poll(self) -> None:
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._cached = None
                LOG.warning("poll unavailable: %s: %s", type(error).__name__, error)
            await asyncio.sleep(8)

    async def _poll_once(self) -> None:
        self._save(await self._operate(None))

    def _save(self, state: dict) -> None:
        self.firmware = state["firmware"]
        self.mode = state["mode"]
        self._cached = {key: state[key] for key in ("power", "brightness")}
        self._updated = time.monotonic()

    def read_state(self) -> dict:
        if self._cached is not None and time.monotonic() - self._updated < 20:
            return dict(self._cached)
        raise ConnectionError("SP651E is waiting for a fresh device report")

    def set_state(self, changes: dict) -> None:
        self._run(changes)

    def close(self) -> None:
        loop = self.runtime.loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                future.result(timeout=5)
            except FutureTimeout:
                future.cancel()
        self._cached = None

    async def _shutdown(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await self._disconnect()

    def _run(self, changes: dict | None) -> dict:
        loop = self.runtime.loop
        if loop is None or not loop.is_running():
            raise ConnectionError("shared BLE runtime is unavailable")
        self._cached = None
        future = asyncio.run_coroutine_threadsafe(self._operate(changes), loop)
        try:
            state = future.result(timeout=14)
        except FutureTimeout as error:
            future.cancel()
            raise TimeoutError("SP651E operation timed out") from error
        self._save(state)
        return dict(self._cached)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        self._queue = None
        if client is not None:
            await asyncio.wait_for(client.disconnect(), timeout=2)

    async def _connect(self):
        if self._client is not None and bool(self._client.is_connected):
            return self._client
        if self._client is not None:
            await self._disconnect()
        device = await asyncio.wait_for(self.runtime._resolve_device(self.address), timeout=2)
        if device is None or getattr(device, "name", None) != "SP651E":
            raise ConnectionError("SP651E is not in the shared scanner cache")
        try:
            lease = await asyncio.wait_for(self.acquire(), timeout=9)
        except asyncio.TimeoutError as error:
            raise ConnectionError("shared BLE setup gate is busy") from error
        client = None
        connected = False
        try:
            client = BleakClient(device, timeout=12)
            await client.connect(dangerous_use_bleak_cache=True)
            queue = asyncio.Queue()
            self._queue = queue

            def received(sender, data):
                packet = bytes(data)
                queue.put_nowait(packet)
                if self._client is client and packet[:2] == bytes([0x53, 2]):
                    try:
                        self._save(decode_status(packet))
                    except ValueError:
                        pass

            await client.start_notify(WRITE_UUID, received)
            self._client = client
            connected = True
            return client
        finally:
            try:
                if not connected and client is not None:
                    await asyncio.wait_for(client.disconnect(), timeout=2)
            finally:
                await self.release(lease["token"])

    async def _query(self, client) -> dict:
        queue = self._queue
        while not queue.empty():
            queue.get_nowait()
        await client.write_gatt_char(WRITE_UUID, frame(2, bytes([1])), response=True)

        async def receive():
            while True:
                packet = await queue.get()
                if packet[:2] == bytes([0x53, 2]):
                    return decode_status(packet)

        return await asyncio.wait_for(receive(), timeout=3)

    async def _operate(self, changes: dict | None) -> dict:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            try:
                client = await self._connect()
                current = await self._query(client)
                if changes:
                    for command in control_frames(changes, current):
                        await client.write_gatt_char(WRITE_UUID, command, response=True)
                        await asyncio.sleep(0.15)
                    reported = await self._query(client)
                    if "power" not in changes and reported["power"] != current["power"]:
                        await client.write_gatt_char(WRITE_UUID, frame(0x50, bytes([int(current["power"])])), response=True)
                        reported = await self._query(client)
                    expected = dict(changes, power=changes.get("power", current["power"]))
                    if any(reported[key] != value for key, value in expected.items()):
                        raise ConnectionError("SP651E command was not confirmed")
                    if "brightness" in changes and reported["mode"] != 2:
                        raise ConnectionError("SP651E constant-light mode was not confirmed")
                    return reported
                return current
            except BaseException:
                self._cached = None
                await self._disconnect()
                raise
