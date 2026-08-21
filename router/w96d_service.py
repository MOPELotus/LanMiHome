#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import struct
import threading
import time
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from bleak import BleakClient, BleakScanner

LOG = logging.getLogger("lanmihome-w96d")

MAIN_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
FFD0_SERVICE = "0000ffd0-0000-1000-8000-00805f9b34fb"
POWER = "0000fff1-0000-1000-8000-00805f9b34fb"
TIMER = "0000fff2-0000-1000-8000-00805f9b34fb"
FAN_SPEED = "0000fff3-0000-1000-8000-00805f9b34fb"
NATURE_WIND = "0000fff4-0000-1000-8000-00805f9b34fb"
SHUTDOWN_DELAY = "0000fff5-0000-1000-8000-00805f9b34fb"
GEAR_DOWN_MODE = "0000fff6-0000-1000-8000-00805f9b34fb"
SPEED_CALIB = "0000fff7-0000-1000-8000-00805f9b34fb"
TURBO_TIME = "0000fff8-0000-1000-8000-00805f9b34fb"
LIGHT_CONTROL = "0000fffa-0000-1000-8000-00805f9b34fb"
TURBO_REMAINING = "0000fffb-0000-1000-8000-00805f9b34fb"
TURBO_MODE = "0000fffc-0000-1000-8000-00805f9b34fb"
BATTERY_INFO = "0000ffd1-0000-1000-8000-00805f9b34fb"
POWER_STATUS = "0000ffd2-0000-1000-8000-00805f9b34fb"
MOTOR_INFO = "0000ffd3-0000-1000-8000-00805f9b34fb"


def _bool(value, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ValueError(f"{field} must be boolean")


def _int(value, field: str, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be integer")
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be integer") from None
    if lo is not None and out < lo:
        raise ValueError(f"{field} must be >= {lo}")
    if hi is not None and out > hi:
        raise ValueError(f"{field} must be <= {hi}")
    return out


def _normalize_mac(value: str | None) -> str:
    return (value or "").replace("-", ":").upper().strip()


class W96DDriver:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = cfg.get("enabled", True) is not False
        self.address = _normalize_mac(str(cfg.get("address") or ""))
        self.name_prefix = str(cfg.get("name_prefix") or "W96D")
        self.scan_timeout = float(cfg.get("scan_timeout", 8.0))
        self.connect_timeout = float(cfg.get("connect_timeout", 12.0))
        self.keep_awake = cfg.get("keep_awake", True) is not False
        self.client: BleakClient | None = None
        self.device_name: str | None = None
        self.lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_seen = 0.0
        self._maintain_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._maintain_task is None or self._maintain_task.done():
            self._maintain_task = asyncio.create_task(self._maintain_loop())

    async def close(self) -> None:
        task = self._maintain_task
        self._maintain_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.disconnect()

    async def _maintain_loop(self) -> None:
        while True:
            try:
                await self.ensure_connected()
                if self.keep_awake:
                    await self._write(SHUTDOWN_DELAY, b"\x00\x00")
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOG.debug("maintain: %s", self.last_error)
            await asyncio.sleep(15)

    async def _scan(self):
        devices = await BleakScanner.discover(timeout=self.scan_timeout)
        wanted = self.address
        for device in devices:
            address = _normalize_mac(getattr(device, "address", ""))
            name = str(getattr(device, "name", "") or "")
            if wanted and address == wanted:
                return device
            if not wanted and name.upper().startswith(self.name_prefix.upper()):
                return device
        if wanted:
            raise ConnectionError(f"W96D {wanted} not found")
        raise ConnectionError(f"no BLE device with name prefix {self.name_prefix!r}")

    async def ensure_connected(self) -> None:
        if not self.enabled:
            raise ConnectionError("W96D is disabled")
        if self.client is not None and self.client.is_connected:
            return
        async with self.lock:
            if self.client is not None and self.client.is_connected:
                return
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
                self.client = None
            device = await self._scan()
            self.address = _normalize_mac(getattr(device, "address", self.address))
            self.device_name = str(getattr(device, "name", "") or self.name_prefix)
            client = BleakClient(device, timeout=self.connect_timeout)
            await client.connect()
            self.client = client
            self.last_seen = time.time()
            self.last_error = None
            LOG.info("connected %s %s", self.device_name, self.address)

    async def disconnect(self) -> None:
        async with self.lock:
            client, self.client = self.client, None
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _read(self, uuid: str) -> bytes:
        await self.ensure_connected()
        assert self.client is not None
        try:
            data = bytes(await self.client.read_gatt_char(uuid))
            self.last_seen = time.time()
            return data
        except Exception:
            await self.disconnect()
            raise

    async def _read_optional(self, uuid: str) -> bytes | None:
        try:
            return await self._read(uuid)
        except Exception:
            return None

    async def _write(self, uuid: str, data: bytes, response: bool = True) -> None:
        await self.ensure_connected()
        assert self.client is not None
        try:
            await self.client.write_gatt_char(uuid, data, response=response)
            self.last_seen = time.time()
        except Exception:
            await self.disconnect()
            raise

    async def state(self) -> dict:
        base = {
            "available": False,
            "connected": bool(self.client and self.client.is_connected),
            "name": self.device_name or self.name_prefix,
            "address": self.address or None,
            "source": "router-bluez",
            "error": self.last_error,
        }
        if not self.enabled:
            return {**base, "error": "disabled"}
        try:
            await self.ensure_connected()
            power_raw, speed_raw, nature_raw, light_raw, shutdown_raw, speeds_raw = await asyncio.gather(
                self._read_optional(POWER),
                self._read_optional(FAN_SPEED),
                self._read_optional(NATURE_WIND),
                self._read_optional(LIGHT_CONTROL),
                self._read_optional(SHUTDOWN_DELAY),
                self._read_optional(SPEED_CALIB),
            )
            turbo_raw, turbo_rem_raw, battery_raw, pwr_raw, motor_raw = await asyncio.gather(
                self._read_optional(TURBO_MODE),
                self._read_optional(TURBO_REMAINING),
                self._read_optional(BATTERY_INFO),
                self._read_optional(POWER_STATUS),
                self._read_optional(MOTOR_INFO),
            )
            out = {
                **base,
                "available": True,
                "connected": True,
                "error": None,
                "power": (power_raw[0] != 0) if power_raw else None,
                "gear": int(power_raw[0]) if power_raw else None,
                "speed": int(speed_raw[0]) if speed_raw else None,
                "natural_wind": (nature_raw[0] != 0) if nature_raw else None,
                "light": (light_raw[0] != 0) if light_raw else None,
                "shutdown_delay_seconds": struct.unpack(">H", shutdown_raw[:2])[0] if shutdown_raw and len(shutdown_raw) >= 2 else None,
                "gear_speeds": list(speeds_raw[:4]) if speeds_raw and len(speeds_raw) >= 4 else None,
            }
            turbo_remaining = struct.unpack(">H", turbo_rem_raw[:2])[0] if turbo_rem_raw and len(turbo_rem_raw) >= 2 else 0
            out["turbo_remaining_seconds"] = turbo_remaining
            out["turbo"] = (turbo_raw[0] != 0) if turbo_raw else turbo_remaining > 0
            if battery_raw and len(battery_raw) >= 8:
                voltage, current, capacity = struct.unpack(">HhI", battery_raw[:8])
                out.update(
                    battery_voltage=voltage / 1000.0,
                    battery_current_ma=current,
                    battery_capacity_mwh=capacity,
                )
            if pwr_raw and len(pwr_raw) >= 8:
                vbus_voltage = struct.unpack(">I", pwr_raw[:4])[0]
                vbus_current = struct.unpack(">h", pwr_raw[4:6])[0]
                if vbus_current == 0x7FFF:
                    vbus_current = 0
                out.update(
                    vbus_voltage=vbus_voltage / 1000.0,
                    vbus_current_ma=vbus_current,
                    charge_status=int(pwr_raw[7]),
                )
            if motor_raw and len(motor_raw) >= 3:
                current = struct.unpack(">H", motor_raw[:2])[0]
                voltage = struct.unpack(">H", motor_raw[-2:])[0] if len(motor_raw) >= 2 else 0
                out.update(
                    motor_current_ma=current,
                    motor_voltage=voltage / 1000.0,
                    motor_power_w=(current / 1000.0) * (voltage / 1000.0),
                    motor_block=int(motor_raw[2]),
                )
            self.last_error = None
            return out
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {**base, "error": self.last_error}

    async def patch(self, body: dict) -> dict:
        allowed = {
            "power", "speed", "natural_wind", "turbo", "light",
            "shutdown_delay_seconds", "gear_down_mode", "gear_speeds", "turbo_time_seconds",
        }
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"unknown W96D field(s): {', '.join(sorted(unknown))}")

        if "power" in body:
            target = _bool(body["power"], "power")
            await self._write(POWER, b"\x01" if target else b"\x00")

        if "speed" in body:
            speed = _int(body["speed"], "speed", 0, 100)
            await self._write(TURBO_MODE, b"\x00")
            await self._write(NATURE_WIND, b"\x00")
            current = await self._read_optional(POWER)
            if not current or current[0] == 0:
                await self._write(POWER, b"\x01")
            await self._write(FAN_SPEED, bytes([speed]), response=False)

        if "natural_wind" in body:
            target = _bool(body["natural_wind"], "natural_wind")
            if target:
                await self._write(TURBO_MODE, b"\x00")
            await self._write(NATURE_WIND, b"\x01" if target else b"\x00")

        if "turbo" in body:
            target = _bool(body["turbo"], "turbo")
            if target:
                await self._write(NATURE_WIND, b"\x00")
            await self._write(TURBO_MODE, b"\x01" if target else b"\x00")

        if "light" in body:
            target = _bool(body["light"], "light")
            await self._write(LIGHT_CONTROL, b"\x01" if target else b"\x00")

        if "shutdown_delay_seconds" in body:
            delay = _int(body["shutdown_delay_seconds"], "shutdown_delay_seconds", 0, 65535)
            await self._write(SHUTDOWN_DELAY, struct.pack(">H", delay))

        if "gear_down_mode" in body:
            mode = _int(body["gear_down_mode"], "gear_down_mode", 0, 1)
            await self._write(GEAR_DOWN_MODE, bytes([mode]))

        if "gear_speeds" in body:
            speeds = body["gear_speeds"]
            if not isinstance(speeds, list) or len(speeds) != 4:
                raise ValueError("gear_speeds must contain exactly four values")
            parsed = bytes(_int(v, "gear_speeds", 0, 100) for v in speeds)
            await self._write(SPEED_CALIB, parsed)

        if "turbo_time_seconds" in body:
            seconds = _int(body["turbo_time_seconds"], "turbo_time_seconds", 0, 600)
            await self._write(TURBO_TIME, struct.pack(">H", seconds))

        return await self.state()


class BleLoop:
    def __init__(self, driver: W96DDriver):
        self.driver = driver
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="lanmihome-w96d-ble", daemon=True)
        self.thread.start()
        self.call(self.driver.start(), timeout=5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 30):
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def close(self):
        try:
            self.call(self.driver.close(), timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    server_version = "LanMiHome-W96D/1"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.client_address[0], fmt % args)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("JSON body must be an object")
        return obj

    def _reply(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path in ("/", "/api/v1/health"):
                state = self.app.call(self.app.driver.state(), timeout=20)
                self._reply(HTTPStatus.OK, {
                    "ok": True,
                    "service": "lanmihome-w96d",
                    "available": bool(state.get("available")),
                    "source": "router-bluez",
                })
            elif path in ("/api/v1/w96d", "/api/v1/state"):
                self._reply(HTTPStatus.OK, self.app.call(self.app.driver.state(), timeout=25))
            else:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception as exc:
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{type(exc).__name__}: {exc}"})

    def do_PATCH(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path != "/api/v1/w96d":
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._reply(HTTPStatus.OK, self.app.call(self.app.driver.patch(self._body()), timeout=30))
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path != "/api/v1/w96d/action":
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            body = self._body()
            name = str(body.get("name") or "")
            if name == "disconnect":
                self.app.call(self.app.driver.disconnect(), timeout=10)
                self._reply(HTTPStatus.OK, {"ok": True, "action": name})
            elif name == "reconnect":
                self.app.call(self.app.driver.disconnect(), timeout=10)
                self.app.call(self.app.driver.ensure_connected(), timeout=25)
                if self.app.driver.keep_awake:
                    self.app.call(self.app.driver._write(SHUTDOWN_DELAY, b"\x00\x00"), timeout=10)
                self._reply(HTTPStatus.OK, {"ok": True, "action": name})
            else:
                raise ValueError(f"unknown W96D action: {name}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{type(exc).__name__}: {exc}"})


def load_config(path: Path) -> tuple[dict, str, int]:
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = raw.get("w96d") if isinstance(raw.get("w96d"), dict) else {}
    bind = str(cfg.get("bind") or "0.0.0.0")
    port = int(cfg.get("port") or 8766)
    return cfg, bind, port


def main() -> int:
    parser = argparse.ArgumentParser(description="LanMiHome W96D BLE sidecar")
    parser.add_argument("--config", default="/lotusemmc/lanmihome/config.json")
    parser.add_argument("--check-config", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg, bind, port = load_config(Path(args.config))
    if args.check_config:
        print(json.dumps({"ok": True, "bind": bind, "port": port, "enabled": cfg.get("enabled", True) is not False}))
        return 0

    driver = W96DDriver(cfg)
    app = BleLoop(driver)
    server = ThreadingHTTPServer((bind, port), Handler)
    server.app = app
    stop = threading.Event()

    def shutdown(_sig=None, _frame=None):
        if stop.is_set():
            return
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("W96D sidecar listening on %s:%d", bind, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
