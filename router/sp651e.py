from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import time

from bleak import BleakClient

WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
VERIFIED_FIRMWARE = "V3020.07"


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

    def read_state(self) -> dict:
        if self._cached is not None and time.monotonic() - self._updated < 8:
            return dict(self._cached)
        return self._run(None)

    def set_state(self, changes: dict) -> None:
        self._run(changes)

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
        self.firmware = state["firmware"]
        self.mode = state["mode"]
        self._cached = {key: state[key] for key in ("power", "brightness")}
        self._updated = time.monotonic()
        return dict(self._cached)

    async def _operate(self, changes: dict | None) -> dict:
        device = await asyncio.wait_for(self.runtime._resolve_device(self.address), timeout=2)
        if device is None or getattr(device, "name", None) != "SP651E":
            raise ConnectionError("SP651E is not in the shared scanner cache")
        lease = await asyncio.wait_for(self.acquire(), timeout=4)
        client = None
        try:
            client = BleakClient(device, timeout=6)
            await client.connect(dangerous_use_bleak_cache=True)
            queue = asyncio.Queue()
            await client.start_notify(WRITE_UUID, lambda sender, data: queue.put_nowait(bytes(data)))

            async def query():
                while not queue.empty():
                    queue.get_nowait()
                await client.write_gatt_char(WRITE_UUID, frame(2, bytes([1])), response=True)

                async def receive():
                    while True:
                        packet = await queue.get()
                        if packet[:2] == bytes([0x53, 2]):
                            return decode_status(packet)

                return await asyncio.wait_for(receive(), timeout=3)

            current = await query()
            if changes:
                for command in control_frames(changes, current):
                    await client.write_gatt_char(WRITE_UUID, command, response=True)
                    await asyncio.sleep(0.15)
                reported = await query()
                if "power" not in changes and reported["power"] != current["power"]:
                    await client.write_gatt_char(WRITE_UUID, frame(0x50, bytes([int(current["power"])])), response=True)
                    reported = await query()
                expected = dict(changes, power=changes.get("power", current["power"]))
                if any(reported[key] != value for key, value in expected.items()):
                    raise ConnectionError("SP651E command was not confirmed")
                if "brightness" in changes and reported["mode"] != 2:
                    raise ConnectionError("SP651E constant-light mode was not confirmed")
                return reported
            return current
        finally:
            try:
                if client is not None:
                    await asyncio.wait_for(client.disconnect(), timeout=2)
            finally:
                await self.release(lease["token"])
