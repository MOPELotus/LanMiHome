#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import struct
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner

LOG = logging.getLogger("lanmihome.w96d")

UUID_BASE = "0000{:04x}-0000-1000-8000-00805f9b34fb"

def uuid16(value: int) -> str:
    return UUID_BASE.format(value)

SERVICE_MAIN = uuid16(0xFFF0)
SERVICE_POWER = uuid16(0xFFD0)
CHAR_POWER = uuid16(0xFFF1)
CHAR_SPEED = uuid16(0xFFF3)
CHAR_NATURAL = uuid16(0xFFF4)
CHAR_SHUTDOWN_DELAY = uuid16(0xFFF5)
CHAR_GEAR_DOWN = uuid16(0xFFF6)
CHAR_SPEED_CALIB = uuid16(0xFFF7)
CHAR_TURBO_TIME = uuid16(0xFFF8)
CHAR_LIGHT = uuid16(0xFFFA)
CHAR_TURBO_REMAINING = uuid16(0xFFFB)
CHAR_TURBO = uuid16(0xFFFC)
CHAR_BATTERY = uuid16(0xFFD1)
CHAR_POWER_STATUS = uuid16(0xFFD2)
CHAR_MOTOR = uuid16(0xFFD3)


def _parse_hhmm(value: str) -> int:
    h, m = value.strip().split(":", 1)
    hour, minute = int(h), int(m)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time: {value}")
    return hour * 60 + minute


def _inside_half_open(now: int, start: int, end: int) -> bool:
    if start < end:
        return start <= now < end
    if start > end:
        return now >= start or now < end
    return True


class OwnershipPolicy:
    def __init__(self, site: str, day_start: str = "06:00", night_start: str = "23:00"):
        site = site.strip().lower()
        if site not in {"home", "school"}:
            raise ValueError("ownership.site must be home or school")
        self.site = site
        # Retain and validate the legacy fields so existing config files remain
        # compatible, but the Xiaomi 10S Night Owner is parked now. Both HOME
        # and SCHOOL therefore keep the router eligible for BLE ownership 24h.
        self.day_start = _parse_hhmm(day_start)
        self.night_start = _parse_hhmm(night_start)

    def scheduled(self, when: datetime | None = None) -> bool:
        return True


@dataclass
class W96DState:
    available: bool = False
    connected: bool = False
    name: str = "W96D"
    address: str | None = None
    owner: str = "router"
    scheduled: bool = False
    paused: bool = False
    power: bool | None = None
    speed: int | None = None
    natural: bool | None = None
    turbo: bool | None = None
    turbo_remaining_seconds: int | None = None
    indicator: bool | None = None
    battery_voltage_mv: int | None = None
    battery_current_ma: int | None = None
    battery_capacity_mwh: int | None = None
    vbus_voltage_mv: int | None = None
    vbus_current_ma: int | None = None
    charge_status: int | None = None
    motor_current_ma: int | None = None
    motor_voltage_mv: int | None = None
    motor_blocked: bool | None = None
    error: str | None = None
    updated_at: str | None = None


class PauseStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._paused = self._load()

    def _load(self) -> bool:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            return bool(raw.get("paused", False))
        except (FileNotFoundError, ValueError, OSError):
            return False

    def get(self) -> bool:
        with self._lock:
            return self._paused

    def set(self, paused: bool) -> None:
        with self._lock:
            self._paused = bool(paused)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps({"paused": self._paused}), "utf-8")
            tmp.replace(self.path)


class W96DManager:
    def __init__(self, config: dict[str, Any]):
        device = config.get("device", {})
        ownership = config.get("ownership", {})
        self.enabled = bool(device.get("enabled", True))
        self.name_prefix = str(device.get("name_prefix", "W96D"))
        self.address = str(device.get("address", "")).strip() or None
        self.poll_seconds = max(0.5, float(config.get("poll_seconds", 2.0)))
        self.connect_timeout = max(2.0, float(config.get("connect_timeout_seconds", 10.0)))
        self.policy = OwnershipPolicy(
            str(ownership.get("site", "school")),
            str(ownership.get("day_start", "06:00")),
            str(ownership.get("night_start", "23:00")),
        )
        state_path = Path(str(config.get("state_file", "/lotusemmc/lanmihome/w96d-owner.json")))
        self.pause_store = PauseStore(state_path)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="w96d-ble-loop", daemon=True)
        self._stop_evt: asyncio.Event | None = None
        self._client: BleakClient | None = None
        self._op_lock: asyncio.Lock | None = None
        self._state_lock = threading.Lock()
        self._state = W96DState(name=self.name_prefix, address=self.address, owner="router")

    def start(self) -> None:
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._stop_evt = asyncio.Event()
        self._op_lock = asyncio.Lock()
        self._loop.create_task(self._runner())
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _runner(self) -> None:
        while self._stop_evt is not None and not self._stop_evt.is_set():
            try:
                scheduled = self.enabled and self.policy.scheduled()
                paused = self.pause_store.get()
                self._update_state(scheduled=scheduled, paused=paused)
                if scheduled and not paused:
                    if not self._connected():
                        await self._connect()
                    if self._connected():
                        await self._refresh_locked()
                else:
                    await self._disconnect()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                LOG.warning("BLE loop: %s: %s", type(exc).__name__, exc)
                self._update_state(error=f"{type(exc).__name__}: {exc}", available=False)
                await self._disconnect()
            await asyncio.sleep(self.poll_seconds)
        await self._disconnect()

    def _connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def _discover_address(self) -> str:
        if self.address:
            return self.address
        devices = await BleakScanner.discover(timeout=min(self.connect_timeout, 6.0))
        for device in devices:
            name = getattr(device, "name", None) or ""
            if self.name_prefix.lower() in name.lower():
                self.address = device.address
                self._update_state(address=self.address, name=name or self.name_prefix)
                return device.address
        raise RuntimeError(f"no BLE device matching {self.name_prefix!r}")

    async def _connect(self) -> None:
        if not self.enabled or not self.policy.scheduled() or self.pause_store.get():
            return
        address = await self._discover_address()
        client = BleakClient(address, timeout=self.connect_timeout)
        await client.connect()
        self._client = client
        self._update_state(connected=True, available=True, address=address, error=None)
        LOG.info("connected W96D at %s", address)

    async def _disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
        self._update_state(connected=False, available=False)

    async def _read(self, uuid: str) -> bytes:
        if not self._client or not self._client.is_connected:
            raise RuntimeError("W96D is not connected")
        return bytes(await self._client.read_gatt_char(uuid))

    async def _write(self, uuid: str, data: bytes, response: bool | None = True) -> None:
        if not self._client or not self._client.is_connected:
            raise RuntimeError("W96D is not connected")
        if response is None:
            await self._client.write_gatt_char(uuid, data)
        else:
            await self._client.write_gatt_char(uuid, data, response=response)

    async def _refresh_locked(self) -> dict[str, Any]:
        assert self._op_lock is not None
        async with self._op_lock:
            return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> dict[str, Any]:
        power = await self._read(CHAR_POWER)
        speed = await self._read(CHAR_SPEED)
        natural = await self._read(CHAR_NATURAL)
        light = await self._read(CHAR_LIGHT)
        turbo_remaining = await self._read(CHAR_TURBO_REMAINING)
        battery = await self._read(CHAR_BATTERY)
        power_status = await self._read(CHAR_POWER_STATUS)
        motor = await self._read(CHAR_MOTOR)

        updates: dict[str, Any] = {
            "available": True,
            "connected": True,
            "power": bool(power[0]) if power else None,
            "speed": int(speed[0]) if speed else None,
            "natural": bool(natural[0]) if natural else None,
            "indicator": bool(light[0]) if light else None,
            "error": None,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if len(turbo_remaining) >= 2:
            remain = int.from_bytes(turbo_remaining[:2], "big")
            updates.update(turbo=remain > 0, turbo_remaining_seconds=remain)
        if len(battery) >= 8:
            voltage, current, capacity = struct.unpack(">HhI", battery[:8])
            updates.update(battery_voltage_mv=voltage, battery_current_ma=current, battery_capacity_mwh=capacity)
        if len(power_status) >= 8:
            vbus_voltage = int.from_bytes(power_status[0:4], "big")
            vbus_current = int.from_bytes(power_status[4:6], "big", signed=True)
            if vbus_current == 0x7FFF:
                vbus_current = 0
            updates.update(vbus_voltage_mv=vbus_voltage, vbus_current_ma=vbus_current, charge_status=power_status[7])
        if len(motor) >= 3:
            current = int.from_bytes(motor[0:2], "big")
            voltage = int.from_bytes(motor[-2:], "big")
            updates.update(motor_current_ma=current, motor_voltage_mv=voltage, motor_blocked=bool(motor[2]))
        self._update_state(**updates)
        return self.snapshot()

    async def _ensure_owned(self) -> None:
        if not self.enabled:
            raise RuntimeError("W96D sidecar is disabled")
        if not self.policy.scheduled():
            raise RuntimeError("router is outside its W96D ownership window")
        if self.pause_store.get():
            raise RuntimeError("W96D ownership is released for outdoor mode")
        if not self._connected():
            await self._connect()

    async def _patch(self, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {"power", "speed", "natural", "turbo", "indicator"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        assert self._op_lock is not None
        async with self._op_lock:
            await self._ensure_owned()
            if "indicator" in body:
                await self._write(CHAR_LIGHT, bytes([1 if _as_bool(body["indicator"], "indicator") else 0]))
            if "speed" in body:
                speed = _as_int(body["speed"], "speed", 0, 100)
                await self._write(CHAR_TURBO, b"\x00")
                await self._write(CHAR_POWER, b"\x01")
                await self._write(CHAR_NATURAL, b"\x00")
                await self._write(CHAR_SPEED, bytes([speed]), response=None)
                await asyncio.sleep(0.06)
            if "natural" in body:
                value = _as_bool(body["natural"], "natural")
                if value:
                    await self._write(CHAR_TURBO, b"\x00")
                    await asyncio.sleep(0.1)
                await self._write(CHAR_NATURAL, bytes([1 if value else 0]))
            if "turbo" in body:
                value = _as_bool(body["turbo"], "turbo")
                if value:
                    await self._write(CHAR_NATURAL, b"\x00")
                    await asyncio.sleep(0.1)
                await self._write(CHAR_TURBO, bytes([1 if value else 0]))
            if "power" in body:
                value = _as_bool(body["power"], "power")
                await self._write(CHAR_TURBO, b"\x00")
                await self._write(CHAR_NATURAL, b"\x00")
                await self._write(CHAR_POWER, bytes([1 if value else 0]))
            await asyncio.sleep(0.08)
            return await self._refresh_unlocked()

    async def _set_paused(self, paused: bool) -> dict[str, Any]:
        self.pause_store.set(paused)
        if paused:
            await self._disconnect()
        self._update_state(paused=paused, scheduled=self.enabled and self.policy.scheduled())
        return self.snapshot()

    def call_patch(self, body: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        return self._call(self._patch(body), timeout)

    def call_refresh(self, timeout: float = 15.0) -> dict[str, Any]:
        async def op() -> dict[str, Any]:
            await self._ensure_owned()
            return await self._refresh_locked()
        return self._call(op(), timeout)

    def set_paused(self, paused: bool, timeout: float = 8.0) -> dict[str, Any]:
        return self._call(self._set_paused(paused), timeout)

    def _call(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            state = asdict(self._state)
        state["scheduled"] = self.enabled and self.policy.scheduled()
        state["paused"] = self.pause_store.get()
        state["available"] = bool(state["connected"] and state["scheduled"] and not state["paused"])
        return state

    def _update_state(self, **changes: Any) -> None:
        with self._state_lock:
            for key, value in changes.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        async def shutdown():
            if self._stop_evt is not None:
                self._stop_evt.set()
            await self._disconnect()
        asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def _as_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ValueError(f"{field} must be boolean")


def _as_int(value: Any, field: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be integer")
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be integer") from None
    if not lo <= out <= hi:
        raise ValueError(f"{field} must be in {lo}..{hi}")
    return out


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "LanMiHome-W96D/1"

    @property
    def manager(self) -> W96DManager:
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path in {"/", "/api/v1/health"}:
                self._json(HTTPStatus.OK, {"ok": True, "service": "lanmihome-w96d", "version": 1, "w96d": self.manager.snapshot()})
            elif path == "/api/v1/w96d":
                state = self.manager.snapshot()
                if state.get("available"):
                    try:
                        state = self.manager.call_refresh()
                    except Exception as exc:
                        state["error"] = f"{type(exc).__name__}: {exc}"
                self._json(HTTPStatus.OK, state)
            elif path == "/api/v1/w96d/ownership":
                state = self.manager.snapshot()
                self._json(HTTPStatus.OK, {"owner": "router", "scheduled": state["scheduled"], "paused": state["paused"], "connected": state["connected"]})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            self._error(exc)

    def do_PATCH(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/v1/w96d":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            self._json(HTTPStatus.OK, self.manager.call_patch(self._body()))
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/api/v1/w96d/ownership":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            action = str(self._body().get("state", "")).strip().lower()
            if action == "release":
                state = self.manager.set_paused(True)
            elif action == "resume":
                state = self.manager.set_paused(False)
            else:
                raise ValueError("state must be release or resume")
            self._json(HTTPStatus.OK, {"ok": True, "owner": "router", "scheduled": state["scheduled"], "paused": state["paused"], "connected": state["connected"]})
        except Exception as exc:
            self._error(exc)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("invalid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _error(self, exc: Exception) -> None:
        status = HTTPStatus.BAD_REQUEST if isinstance(exc, ValueError) else HTTPStatus.SERVICE_UNAVAILABLE
        self._json(status, {"error": f"{type(exc).__name__}: {exc}"})

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("http %s - %s", self.address_string(), fmt % args)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, manager: W96DManager):
        super().__init__(addr, handler)
        self.manager = manager


def load_config(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be an object")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="LanMiHome W96D BLE sidecar")
    parser.add_argument("--config", default="/lotusemmc/lanmihome/w96d.json")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(Path(args.config))
    manager = W96DManager(cfg)
    if args.check_config:
        print(json.dumps({"ok": True, "site": manager.policy.site, "enabled": manager.enabled, "listen": cfg.get("listen", "0.0.0.0"), "port": int(cfg.get("port", 8766))}))
        return 0

    manager.start()
    server = Server((str(cfg.get("listen", "0.0.0.0")), int(cfg.get("port", 8766))), ApiHandler, manager)

    def handle_signal(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        manager.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
