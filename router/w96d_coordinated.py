#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from datetime import datetime
from urllib import error as urlerror
from urllib import request as urlrequest

import w96d_sidecar as core

LOG = logging.getLogger("lanmihome.w96d")
COORDINATOR = "http://127.0.0.1:8765"
_ORIG_GET = core.ApiHandler.do_GET


def _post_json(path: str, payload: dict, timeout: float = 12.0) -> dict:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urlrequest.Request(
        COORDINATOR + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"BLE coordinator HTTP {exc.code}: {detail}") from None
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"BLE coordinator unavailable: {exc}") from None

    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("BLE coordinator returned invalid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeError("BLE coordinator returned invalid response")
    return value


async def _acquire_gate(
    owner: str,
    *,
    pause_scan: bool,
    ttl: float,
    timeout: float = 55.0,
) -> str:
    result = await asyncio.to_thread(
        _post_json,
        "/api/v1/system/bluetooth/gatt/acquire",
        {
            "owner": owner,
            "ttl_seconds": ttl,
            "pause_scan": pause_scan,
        },
        timeout,
    )
    token = str(result.get("token") or "")
    if not token:
        raise RuntimeError("BLE coordinator did not return a gate token")
    return token


async def _release_gate(token: str) -> None:
    await asyncio.to_thread(
        _post_json,
        "/api/v1/system/bluetooth/gatt/release",
        {"token": token},
        8.0,
    )


async def _safe_release(token: str, owner: str) -> None:
    try:
        await _release_gate(token)
    except Exception as exc:
        LOG.warning(
            "failed to release BLE gate for %s: %s: %s",
            owner,
            type(exc).__name__,
            exc,
        )


def _crc8_table_value(value: int) -> int:
    crc = value & 0xFF
    for _ in range(8):
        crc = ((crc << 1) ^ 0x3B) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _crc8_step(crc: int, value: int) -> int:
    return _crc8_table_value((crc ^ value) & 0xFF)


def _dfu_pack(payload: bytes) -> bytes:
    frame = bytearray(5 + len(payload))
    frame[0] = 0x55
    frame[1] = 0
    frame[2] = len(payload) & 0xFF
    frame[3] = (len(payload) >> 8) & 0xFF
    frame[4 : 4 + len(payload)] = payload
    crc = 137
    for value in frame[: 4 + len(payload)]:
        crc = _crc8_step(crc, value)
    frame[-1] = crc
    return bytes(frame)


async def _dfu_request(self, payload: bytes) -> bytes:
    client = self._client
    if client is None or not client.is_connected:
        raise RuntimeError("W96D disconnected before device-info request")

    loop = asyncio.get_running_loop()
    result: asyncio.Future[bytes] = loop.create_future()
    stage = 0
    key = 0
    mask = 0
    length = 0
    rx_payload = bytearray()
    crc = 137

    def on_notify(_sender, data: bytearray) -> None:
        nonlocal stage, key, mask, length, rx_payload, crc
        for raw in bytes(data):
            if stage == 0:
                if raw == 0x55:
                    crc = _crc8_step(137, 0x55)
                    stage = 1
                continue
            if stage == 1:
                key = raw
                mask = _crc8_table_value(key)
                crc = _crc8_step(crc, key)
                stage = 2
                continue
            decoded = raw ^ mask
            if stage == 2:
                length = decoded
                crc = _crc8_step(crc, decoded)
                stage = 3
                continue
            if stage == 3:
                length |= decoded << 8
                crc = _crc8_step(crc, decoded)
                if not 1 <= length <= 300:
                    stage = 0
                    continue
                rx_payload = bytearray()
                stage = 4
                continue
            if len(rx_payload) < length:
                rx_payload.append(decoded)
                crc = _crc8_step(crc, decoded)
                continue
            if decoded == crc and not result.done():
                result.set_result(bytes(rx_payload))
            stage = 0

    await client.start_notify(core.CHAR_DFU_NOTIFY, on_notify)
    try:
        frame = _dfu_pack(payload)
        for offset in range(0, len(frame), 197):
            await client.write_gatt_char(
                core.CHAR_DFU_WRITE,
                frame[offset : offset + 197],
                response=False,
            )
        return await asyncio.wait_for(result, timeout=5.0)
    finally:
        try:
            await client.stop_notify(core.CHAR_DFU_NOTIFY)
        except Exception:
            pass


async def _load_device_info(self) -> None:
    serial = None
    firmware = None
    try:
        response = await _dfu_request(self, b"\x8a")
        if len(response) >= 2 and (response[0] & 0x7F) == 4:
            raw = response[1]
            firmware = f"{raw // 10}.{raw % 10}"
    except Exception as exc:
        LOG.info("W96D firmware read unavailable: %s: %s", type(exc).__name__, exc)

    try:
        response = await _dfu_request(self, b"\x8f")
        if len(response) >= 5 and (response[0] & 0x7F) == 10:
            serial = str(int.from_bytes(response[1:5], "little", signed=False))
    except Exception as exc:
        LOG.info("W96D serial read unavailable: %s: %s", type(exc).__name__, exc)

    updates: dict[str, object] = {}
    if serial:
        updates["serial_number"] = serial
    if firmware:
        updates["firmware_version"] = firmware
    if updates:
        self._update_state(**updates)


async def _coordinated_connect(self) -> None:
    if not self.enabled or not self.policy.scheduled() or self.pause_store.get():
        return

    token = await _acquire_gate(
        "w96d-connect",
        pause_scan=True,
        ttl=45.0,
    )
    try:
        address = await self._discover_address()
        client = core.BleakClient(address, timeout=self.connect_timeout)
        await client.connect()
        self._client = client
        self._coord_refresh_cycle = 0
        self._coord_failed_refreshes = 0
        self._coord_read_warn_at = {}
        self._coord_force_telemetry = False
        self._coord_force_settings = False
        self._update_state(
            connected=True,
            available=True,
            address=address,
            error=None,
        )
        LOG.info("connected W96D at %s", address)
        await _load_device_info(self)
    finally:
        await _safe_release(token, "w96d-connect")


def _warn_read(self, label: str, exc: Exception) -> None:
    now = time.monotonic()
    seen = getattr(self, "_coord_read_warn_at", None)
    if not isinstance(seen, dict):
        seen = {}
        self._coord_read_warn_at = seen
    last = float(seen.get(label, 0.0))
    if now - last >= 30.0:
        LOG.warning(
            "W96D GATT read %s failed: %s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        seen[label] = now


async def _read_optional(self, label: str, uuid: str) -> bytes | None:
    if not self._connected():
        raise RuntimeError(f"W96D disconnected before reading {label}")
    try:
        data = await self._read(uuid)
        await asyncio.sleep(0.02)
        return data
    except Exception as exc:
        if not self._connected():
            raise RuntimeError(f"W96D disconnected while reading {label}") from exc
        _warn_read(self, label, exc)
        await asyncio.sleep(0.02)
        return None


async def _low_pressure_refresh_unlocked(self):
    cycle = int(getattr(self, "_coord_refresh_cycle", 0)) + 1
    self._coord_refresh_cycle = cycle
    snapshot = self.snapshot()
    initial = snapshot.get("updated_at") is None
    force_telemetry = bool(getattr(self, "_coord_force_telemetry", False))
    force_settings = bool(getattr(self, "_coord_force_settings", False))

    updates: dict[str, object] = {
        "connected": True,
        "available": True,
    }
    successful = 0

    power = await _read_optional(self, "FFF1/power", core.CHAR_POWER)
    if power:
        gear = int(power[0])
        updates["power"] = gear != 0
        updates["gear"] = gear
        successful += 1

    speed = await _read_optional(self, "FFF3/speed", core.CHAR_SPEED)
    if speed:
        updates["speed"] = int(speed[0])
        successful += 1

    natural = await _read_optional(self, "FFF4/natural", core.CHAR_NATURAL)
    if natural:
        updates["natural"] = bool(natural[0])
        successful += 1

    # UI-level state: roughly every 6 seconds.
    if initial or cycle % 3 == 1:
        light = await _read_optional(self, "FFFA/light", core.CHAR_LIGHT)
        if light:
            updates["indicator"] = bool(light[0])
            successful += 1

        turbo_remaining = await _read_optional(
            self,
            "FFFB/turbo_remaining",
            core.CHAR_TURBO_REMAINING,
        )
        if turbo_remaining is not None and len(turbo_remaining) >= 2:
            remain = int.from_bytes(turbo_remaining[:2], "big")
            updates["turbo"] = remain > 0
            updates["turbo_remaining_seconds"] = remain
            successful += 1

        timer = await _read_optional(self, "FFF2/timer", core.CHAR_TIMER)
        if timer is not None and len(timer) >= 2:
            updates["timer_remaining_seconds"] = int.from_bytes(timer[:2], "big")
            successful += 1

    # Persistent settings: roughly every 30 seconds, or immediately after a change.
    if initial or force_settings or cycle % 15 == 1:
        self._coord_force_settings = False

        sleep_delay = await _read_optional(
            self,
            "FFF5/sleep_delay",
            core.CHAR_SHUTDOWN_DELAY,
        )
        if sleep_delay is not None and len(sleep_delay) >= 2:
            updates["sleep_delay_seconds"] = int.from_bytes(sleep_delay[:2], "big")
            successful += 1

        gear_down = await _read_optional(self, "FFF6/gear_down", core.CHAR_GEAR_DOWN)
        if gear_down:
            updates["gear_down_mode"] = int(gear_down[0])
            successful += 1

        gear_speeds = await _read_optional(
            self,
            "FFF7/gear_speeds",
            core.CHAR_SPEED_CALIB,
        )
        if gear_speeds is not None and len(gear_speeds) >= 4:
            updates["gear_speeds"] = [int(v) for v in gear_speeds[:4]]
            successful += 1

        turbo_time = await _read_optional(self, "FFF8/turbo_time", core.CHAR_TURBO_TIME)
        if turbo_time is not None and len(turbo_time) >= 2:
            updates["turbo_time_seconds"] = int.from_bytes(turbo_time[:2], "big")
            successful += 1

    # Differential captures verified these telemetry layouts:
    # FFD1 [0:2] battery mV, [2:4] signed battery mA, [4:8] capacity mWh.
    # FFD2 [0:4] VBUS mV and [7] charge/discharge state. [4:6] is NOT current.
    # FFD3 [0:2] motor mA, [4:6] motor mV. [2:4] remains unknown.
    if initial or force_telemetry or cycle % 5 == 1:
        self._coord_force_telemetry = False

        battery = await _read_optional(self, "FFD1/battery", core.CHAR_BATTERY)
        if battery is not None and len(battery) >= 8:
            voltage, current, capacity = struct.unpack(">HhI", battery[:8])
            updates.update(
                battery_voltage_mv=voltage,
                battery_current_ma=current,
                battery_capacity_mwh=capacity,
            )
            successful += 1

        power_status = await _read_optional(
            self,
            "FFD2/vbus",
            core.CHAR_POWER_STATUS,
        )
        if power_status is not None and len(power_status) >= 4:
            updates["vbus_voltage_mv"] = int.from_bytes(power_status[0:4], "big")
            if len(power_status) >= 8:
                updates["charge_status"] = int(power_status[7])
            successful += 1

        motor = await _read_optional(self, "FFD3/motor", core.CHAR_MOTOR)
        if motor is not None and len(motor) >= 6:
            updates.update(
                motor_current_ma=int.from_bytes(motor[0:2], "big"),
                motor_voltage_mv=int.from_bytes(motor[4:6], "big"),
            )
            successful += 1

    if successful:
        self._coord_failed_refreshes = 0
        updates["error"] = None
        updates["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._update_state(**updates)
        return self.snapshot()

    failures = int(getattr(self, "_coord_failed_refreshes", 0)) + 1
    self._coord_failed_refreshes = failures
    if not self._connected():
        raise RuntimeError("W96D disconnected during refresh")

    self._update_state(
        connected=True,
        available=True,
        error=f"temporary GATT read failure ({failures}/3)",
    )
    if failures >= 3:
        raise RuntimeError("W96D GATT refresh failed three consecutive times")
    return self.snapshot()


async def _coordinated_refresh_locked(self):
    assert self._op_lock is not None
    async with self._op_lock:
        token = await _acquire_gate(
            "w96d-io",
            pause_scan=False,
            ttl=20.0,
        )
        try:
            return await self._refresh_unlocked()
        finally:
            await _safe_release(token, "w96d-io")


async def _coordinated_patch(self, body):
    allowed = {
        "power",
        "speed",
        "natural",
        "turbo",
        "indicator",
        "timer_seconds",
        "sleep_delay_seconds",
        "gear_down_mode",
        "turbo_time_seconds",
        "battery_profile",
    }
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")

    assert self._op_lock is not None
    async with self._op_lock:
        await self._ensure_owned()
        token = await _acquire_gate(
            "w96d-command",
            pause_scan=False,
            ttl=20.0,
        )
        optimistic: dict[str, object] = {}
        try:
            if "indicator" in body:
                value = core._as_bool(body["indicator"], "indicator")
                await self._write(core.CHAR_LIGHT, bytes([1 if value else 0]))
                optimistic["indicator"] = value

            if "speed" in body:
                speed = core._as_int(body["speed"], "speed", 0, 100)
                await self._write(core.CHAR_TURBO, b"\x00")
                await self._write(core.CHAR_POWER, b"\x01")
                await self._write(core.CHAR_NATURAL, b"\x00")
                await self._write(core.CHAR_SPEED, bytes([speed]), response=None)
                await asyncio.sleep(0.06)
                optimistic.update(
                    power=True,
                    gear=1,
                    speed=speed,
                    natural=False,
                    turbo=False,
                )

            if "natural" in body:
                value = core._as_bool(body["natural"], "natural")
                if value:
                    await self._write(core.CHAR_TURBO, b"\x00")
                    await asyncio.sleep(0.1)
                    optimistic["turbo"] = False
                await self._write(core.CHAR_NATURAL, bytes([1 if value else 0]))
                optimistic["natural"] = value

            if "turbo" in body:
                value = core._as_bool(body["turbo"], "turbo")
                if value:
                    await self._write(core.CHAR_NATURAL, b"\x00")
                    await asyncio.sleep(0.1)
                    optimistic["natural"] = False
                await self._write(core.CHAR_TURBO, bytes([1 if value else 0]))
                optimistic["turbo"] = value

            if "power" in body:
                value = core._as_bool(body["power"], "power")
                await self._write(core.CHAR_TURBO, b"\x00")
                await self._write(core.CHAR_NATURAL, b"\x00")
                await self._write(core.CHAR_POWER, bytes([1 if value else 0]))
                optimistic.update(
                    power=value,
                    gear=1 if value else 0,
                    turbo=False,
                    natural=False,
                )

            if "timer_seconds" in body:
                seconds = core._as_int(body["timer_seconds"], "timer_seconds", 0, 28_800)
                await self._write(core.CHAR_TIMER, seconds.to_bytes(2, "big"))
                optimistic["timer_remaining_seconds"] = seconds

            if "sleep_delay_seconds" in body:
                seconds = core._as_int(
                    body["sleep_delay_seconds"],
                    "sleep_delay_seconds",
                    0,
                    65_535,
                )
                if 1 <= seconds <= 9:
                    raise ValueError("sleep_delay_seconds must be 0 or 10..65535")
                await self._write(core.CHAR_SHUTDOWN_DELAY, seconds.to_bytes(2, "big"))
                optimistic["sleep_delay_seconds"] = seconds
                self._coord_force_settings = True

            if "gear_down_mode" in body:
                mode = core._as_int(body["gear_down_mode"], "gear_down_mode", 0, 1)
                await self._write(core.CHAR_GEAR_DOWN, bytes([mode]))
                optimistic["gear_down_mode"] = mode
                self._coord_force_settings = True

            if "turbo_time_seconds" in body:
                seconds = core._as_int(
                    body["turbo_time_seconds"],
                    "turbo_time_seconds",
                    0,
                    600,
                )
                await self._write(core.CHAR_TURBO_TIME, seconds.to_bytes(2, "big"))
                optimistic["turbo_time_seconds"] = seconds
                self._coord_force_settings = True

            if "battery_profile" in body:
                profile = core._as_int(body["battery_profile"], "battery_profile", 4800, 5000)
                if profile not in {4800, 5000}:
                    raise ValueError("battery_profile must be 4800 or 5000")
                capacity = 17_200 if profile == 4800 else 18_000
                await self._write(
                    core.CHAR_BATTERY,
                    f"BAT_CAP={capacity},".encode("ascii"),
                )
                optimistic["battery_capacity_mwh"] = capacity
                self._coord_force_telemetry = True

            if optimistic:
                optimistic["error"] = None
                self._update_state(**optimistic)

            self._coord_force_telemetry = True
            await asyncio.sleep(0.08)
            return await self._refresh_unlocked()
        finally:
            await _safe_release(token, "w96d-command")


def _snapshot_get(self) -> None:
    path = self.path.split("?", 1)[0].rstrip("/") or "/"
    if path == "/api/v1/w96d":
        self._json(core.HTTPStatus.OK, self.manager.snapshot())
        return
    _ORIG_GET(self)


core.W96DManager._connect = _coordinated_connect
core.W96DManager._refresh_unlocked = _low_pressure_refresh_unlocked
core.W96DManager._refresh_locked = _coordinated_refresh_locked
core.W96DManager._patch = _coordinated_patch
core.ApiHandler.do_GET = _snapshot_get


if __name__ == "__main__":
    raise SystemExit(core.main())
