#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import signal
import struct
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bleak import BleakScanner
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from miio.integrations.genericmiot.genericmiot import GenericMiot

from lanmihome_cuktech import ChargerConfig, ChargerManager
from lanmihome_cuktech.miot import MiotResult
from lanmihome_cuktech.protocol import LANGUAGES, SCENE_MODES, SCREEN_TIMEOUTS

LOG = logging.getLogger("lanmihome")
FE95_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
SENSOR_PRODUCT_ID = 0x55B5

FAN_READ = {
    "power": (2, 1),
    "fan_level": (2, 2),
    "mode": (2, 3),
    "swing": (2, 4),
    "swing_angle": (2, 5),
    "off_delay_minutes": (3, 1),
    "indicator": (4, 1),
    "alarm": (5, 1),
    "child_lock": (7, 1),
    "speed": (8, 1),
}
FAN_WRITE = FAN_READ.copy()
FAN_ACTIONS = {
    "turn-left": (2, 2),
    "turn-right": (2, 3),
    "toggle-mode": (8, 1),
    "loop-gear": (8, 2),
}

LAMP_READ = {
    "power": (2, 1),
    "brightness": (2, 2),
    "color_temperature": (2, 3),
    "default_power_on_state": (2, 12),
    "on_gradient_seconds": (2, 13),
    "off_gradient_seconds": (2, 14),
    "mode": (2, 15),
    "delay_enabled": (4, 1),
    "delay_minutes": (4, 2),
    "delay_remain_minutes": (4, 3),
    "focus_enabled": (5, 1),
    "focus_minutes": (5, 2),
    "rest_minutes": (5, 3),
    "recycle_number": (5, 4),
}
LAMP_WRITE = {k: v for k, v in LAMP_READ.items() if k != "delay_remain_minutes"}
LAMP_ACTIONS = {
    "toggle": (2, 1, None),
    "brightness-up": (2, 2, 16),
    "brightness-down": (2, 3, 16),
    "color-temperature-up": (2, 4, 17),
    "color-temperature-down": (2, 5, 17),
    "bright-circle": (6, 1, None),
    "ct-circle": (6, 5, None),
}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


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


def _float(value, field: str, lo: float | None = None, hi: float | None = None) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be number") from None
    if lo is not None and out < lo:
        raise ValueError(f"{field} must be >= {lo}")
    if hi is not None and out > hi:
        raise ValueError(f"{field} must be <= {hi}")
    return out


def _normalize_mac(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("-", ":").upper()


def _hex_bytes(value: str | None, length: int, label: str) -> bytes | None:
    if not value:
        return None
    text = value.strip().replace(" ", "").replace(":", "")
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValueError(f"{label} must be hexadecimal") from None
    if len(raw) != length:
        raise ValueError(f"{label} must be exactly {length} bytes")
    return raw


def _device_config(config: dict, name: str) -> dict:
    candidate = config.get(name)
    if isinstance(candidate, dict):
        return candidate
    devices = config.get("devices")
    if isinstance(devices, dict) and isinstance(devices.get(name), dict):
        return devices[name]
    return {}


def _device_enabled(cfg: dict) -> bool:
    if not cfg:
        return False
    if cfg.get("enabled") is False:
        return False
    return bool(cfg.get("ip") or cfg.get("host") or cfg.get("address")) and bool(cfg.get("token"))


def _miot_items(response) -> list[dict]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        result = response.get("result")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return [response]
    return []


def _miot_ensure_ok(response, label: str) -> None:
    items = _miot_items(response)
    bad = [item for item in items if item.get("code", 0) not in (0, None)]
    if bad:
        raise RuntimeError(f"{label} failed: {bad[0].get('code')}")


def _miot_get_one(device: GenericMiot, siid: int, piid: int):
    response = device.get_property_by(siid, piid)
    items = _miot_items(response)
    if not items:
        raise RuntimeError(f"MIoT {siid}/{piid} returned no result")
    item = items[0]
    code = item.get("code", 0)
    if code not in (0, None):
        raise RuntimeError(f"MIoT {siid}/{piid} failed: {code}")
    if "value" not in item:
        raise RuntimeError(f"MIoT {siid}/{piid} returned no value")
    return item["value"]


def _miot_get_many(device: GenericMiot, mapping: dict[str, tuple[int, int]]) -> dict:
    out: dict[str, object] = {}
    entries = list(mapping.items())
    for offset in range(0, len(entries), 10):
        chunk = entries[offset : offset + 10]
        requests = [
            {"did": key, "siid": siid, "piid": piid}
            for key, (siid, piid) in chunk
        ]
        response = device.send("get_properties", requests)
        items = _miot_items(response)
        by_did = {str(item.get("did")): item for item in items}
        for key, _ids in chunk:
            item = by_did.get(key)
            if item is None:
                continue
            if item.get("code", 0) in (0, None) and "value" in item:
                out[key] = item["value"]
    return out


def _miot_set(device: GenericMiot, siid: int, piid: int, value, label: str) -> None:
    response = device.set_property_by(siid, piid, value)
    _miot_ensure_ok(response, label)


def _miot_action(device: GenericMiot, siid: int, aiid: int, params: list | None, label: str) -> None:
    response = device.call_action_by(siid, aiid, params or [])
    _miot_ensure_ok(response, label)


class MiotAdapter:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.lock = threading.RLock()
        self.enabled = _device_enabled(cfg)
        self.ip = str(cfg.get("ip") or cfg.get("host") or cfg.get("address") or "")
        self.model = str(cfg.get("model") or "")
        self.display_name = str(cfg.get("name") or name)
        self.timeout = int(cfg.get("timeout", 3))

    def new_device(self) -> GenericMiot:
        if not self.enabled:
            raise ConnectionError(f"{self.name} is not configured")
        return GenericMiot(
            self.ip,
            str(self.cfg["token"]),
            model=self.model or None,
            timeout=self.timeout,
            lazy_discover=True,
        )


class FanAdapter(MiotAdapter):
    def state(self) -> dict:
        base = {"available": False, "name": self.display_name, "ip": self.ip or None}
        if not self.enabled:
            return {**base, "error": "not configured"}
        try:
            with self.lock:
                values = _miot_get_many(self.new_device(), FAN_READ)
            values["power"] = bool(values["power"]) if "power" in values else None
            for key in ("swing", "indicator", "alarm", "child_lock"):
                if key in values:
                    values[key] = bool(values[key])
            return {**base, "available": True, **values}
        except Exception as exc:
            return {**base, "error": f"{type(exc).__name__}: {exc}"}

    def patch(self, body: dict) -> dict:
        unknown = set(body) - set(FAN_WRITE)
        if unknown:
            raise ValueError(f"unknown fan field(s): {', '.join(sorted(unknown))}")
        with self.lock:
            device = self.new_device()
            if "power" in body:
                target = _bool(body["power"], "power")
                current = bool(_miot_get_one(device, 2, 1))
                if current != target:
                    _miot_action(device, 2, 1, [], "fan toggle")
                    time.sleep(0.35)
                    if bool(_miot_get_one(device, 2, 1)) != target:
                        raise RuntimeError("fan power verification failed")
            validators = {
                "speed": lambda v: _int(v, "speed", 1, 100),
                "fan_level": lambda v: _int(v, "fan_level", 1, 4),
                "mode": self._mode,
                "swing": lambda v: _bool(v, "swing"),
                "swing_angle": self._angle,
                "off_delay_minutes": lambda v: _int(v, "off_delay_minutes", 0, 480),
                "indicator": lambda v: _bool(v, "indicator"),
                "alarm": lambda v: _bool(v, "alarm"),
                "child_lock": lambda v: _bool(v, "child_lock"),
            }
            for key, value in body.items():
                if key == "power":
                    continue
                siid, piid = FAN_WRITE[key]
                parsed = validators[key](value)
                _miot_set(device, siid, piid, parsed, f"fan {key}")
        return self.state()

    @staticmethod
    def _mode(value):
        if isinstance(value, str):
            aliases = {"straight": 0, "natural": 1}
            if value.lower() in aliases:
                return aliases[value.lower()]
        return _int(value, "mode", 0, 1)

    @staticmethod
    def _angle(value):
        angle = _int(value, "swing_angle")
        if angle not in (30, 60, 90, 120, 140):
            raise ValueError("swing_angle must be one of 30/60/90/120/140")
        return angle

    def action(self, body: dict) -> dict:
        name = str(body.get("name") or "")
        if name not in FAN_ACTIONS:
            raise ValueError(f"unknown fan action: {name}")
        with self.lock:
            device = self.new_device()
            siid, aiid = FAN_ACTIONS[name]
            _miot_action(device, siid, aiid, [], f"fan action {name}")
        return {"ok": True, "action": name}

    def ensure_power(self, target: bool = True) -> bool:
        with self.lock:
            device = self.new_device()
            current = bool(_miot_get_one(device, 2, 1))
            if current != target:
                _miot_action(device, 2, 1, [], "fan recovery toggle")
                time.sleep(0.4)
            return bool(_miot_get_one(device, 2, 1)) == target


class LampAdapter(MiotAdapter):
    def state(self) -> dict:
        base = {"available": False, "name": self.display_name, "ip": self.ip or None}
        if not self.enabled:
            return {**base, "error": "not configured"}
        try:
            with self.lock:
                values = _miot_get_many(self.new_device(), LAMP_READ)
            for key in ("power", "delay_enabled", "focus_enabled"):
                if key in values:
                    values[key] = bool(values[key])
            return {**base, "available": True, **values}
        except Exception as exc:
            return {**base, "error": f"{type(exc).__name__}: {exc}"}

    def patch(self, body: dict) -> dict:
        allowed = set(LAMP_WRITE) | {"on_then_delayoff_minutes"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"unknown lamp field(s): {', '.join(sorted(unknown))}")
        validators = {
            "power": lambda v: _bool(v, "power"),
            "brightness": lambda v: _int(v, "brightness", 1, 100),
            "color_temperature": lambda v: _int(v, "color_temperature", 2700, 5100),
            "default_power_on_state": lambda v: _int(v, "default_power_on_state", 0, 2),
            "on_gradient_seconds": lambda v: _float(v, "on_gradient_seconds", 0, 5),
            "off_gradient_seconds": lambda v: _float(v, "off_gradient_seconds", 0, 5),
            "mode": lambda v: _int(v, "mode", 0, 6),
            "delay_enabled": lambda v: _bool(v, "delay_enabled"),
            "delay_minutes": lambda v: _int(v, "delay_minutes", 1, 120),
            "focus_enabled": lambda v: _bool(v, "focus_enabled"),
            "focus_minutes": lambda v: _int(v, "focus_minutes", 1, 60),
            "rest_minutes": lambda v: _int(v, "rest_minutes", 1, 60),
            "recycle_number": lambda v: _int(v, "recycle_number", 1, 10),
        }
        with self.lock:
            device = self.new_device()
            for key, value in body.items():
                if key == "on_then_delayoff_minutes":
                    parsed = _int(value, key, 0, 120)
                    _miot_set(device, 6, 1, parsed, f"lamp {key}")
                    continue
                siid, piid = LAMP_WRITE[key]
                parsed = validators[key](value)
                _miot_set(device, siid, piid, parsed, f"lamp {key}")
        return self.state()

    def action(self, body: dict) -> dict:
        name = str(body.get("name") or "")
        if name not in LAMP_ACTIONS:
            raise ValueError(f"unknown lamp action: {name}")
        siid, aiid, input_piid = LAMP_ACTIONS[name]
        params: list = []
        if input_piid is not None:
            value = _int(body.get("value"), "value", 1, 1000)
            params = [{"piid": input_piid, "value": value}]
        with self.lock:
            _miot_action(self.new_device(), siid, aiid, params, f"lamp action {name}")
        return {"ok": True, "action": name}


class SensorStore:
    def __init__(self, cfg: dict):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = bool(self.cfg.get("enabled", bool(self.cfg.get("address") or self.cfg.get("mac"))))
        self.address = _normalize_mac(self.cfg.get("address") or self.cfg.get("mac"))
        self.stale_seconds = int(self.cfg.get("stale_seconds", 180))
        self._bind_key_error: str | None = None
        try:
            self.bind_key = _hex_bytes(self.cfg.get("bind_key") or self.cfg.get("bindkey"), 16, "sensor bind_key")
        except ValueError as exc:
            self.bind_key = None
            self._bind_key_error = str(exc)
        self.lock = threading.RLock()
        self.data: dict = {
            "temperature": None,
            "humidity": None,
            "battery": None,
            "rssi": None,
            "mac": self.address or None,
            "frame_counter": None,
            "raw": None,
            "encrypted": None,
            "authenticated": False,
            "received_epoch": None,
            "received_at": None,
            "reports": 0,
            "source": None,
        }
        self._last_identity: tuple[int | None, str | None] | None = None

    def process_advertisement(self, device, adv) -> None:
        if not self.enabled:
            return
        address = _normalize_mac(getattr(device, "address", ""))
        if self.address and address != self.address:
            return
        service_data = getattr(adv, "service_data", {}) or {}
        payload = None
        for uuid, value in service_data.items():
            if str(uuid).lower() == FE95_UUID:
                payload = bytes(value)
                break
        if payload is None:
            return
        decoded = decode_mibeacon_sensor(payload, self.bind_key, address)
        if decoded is None:
            return
        if decoded["product_id"] != SENSOR_PRODUCT_ID:
            return
        if self.address and decoded.get("mac") not in (self.address, "UNKNOWN"):
            return
        now = time.time()
        identity = (decoded.get("frame_counter"), decoded.get("raw"))
        has_measurement = any(
            decoded.get(key) is not None
            for key in ("temperature", "humidity", "battery")
        )
        with self.lock:
            if identity == self._last_identity:
                self.data["rssi"] = getattr(adv, "rssi", self.data.get("rssi"))
                return

            self._last_identity = identity

            # MiBeacon also emits identity/metadata advertisements carrying no
            # sensor measurement. They prove the device is nearby, but must not
            # make an old temperature/humidity reading look fresh.
            if not has_measurement:
                self.data["rssi"] = getattr(adv, "rssi", self.data.get("rssi"))
                return

            for key in ("temperature", "humidity", "battery"):
                if decoded.get(key) is not None:
                    self.data[key] = decoded[key]
            self.data.update({
                "rssi": getattr(adv, "rssi", None),
                "mac": address or decoded.get("mac"),
                "frame_counter": decoded.get("frame_counter"),
                "raw": decoded.get("raw"),
                "encrypted": decoded.get("encrypted"),
                "authenticated": decoded.get("authenticated", False),
                "received_epoch": now,
                "received_at": _iso(now),
                "reports": int(self.data.get("reports") or 0) + 1,
                "source": "router-bluez",
            })

    def external_report(self, body: dict) -> dict:
        address = _normalize_mac(str(body.get("mac") or ""))
        if self.address and address and address != self.address:
            raise ValueError("sensor report MAC does not match configured sensor")
        product_id = body.get("product_id")
        if product_id is not None and int(product_id) != SENSOR_PRODUCT_ID:
            raise ValueError("unexpected sensor product_id")
        seen_at_ms = body.get("seen_at_ms")
        now = float(seen_at_ms) / 1000.0 if seen_at_ms else time.time()
        with self.lock:
            for key in ("temperature", "humidity", "battery", "rssi", "frame_counter", "raw"):
                if key in body and body[key] is not None:
                    self.data[key] = body[key]
            self.data.update({
                "mac": address or self.data.get("mac"),
                "authenticated": True,
                "received_epoch": now,
                "received_at": _iso(now),
                "reports": int(self.data.get("reports") or 0) + 1,
                "source": str(body.get("source") or "external-report"),
            })
        return self.snapshot()

    def snapshot(self) -> dict:
        with self.lock:
            data = copy.deepcopy(self.data)

        # BLE payloads may contain IEEE-754 float32 values such as
        # 32.20000076293945. Sensor resolution is only meaningful to 0.1.
        for key in ("temperature", "humidity"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                data[key] = round(float(value), 1)

        received = data.pop("received_epoch", None)
        age = max(0, int(time.time() - received)) if received else None
        has_measurement = any(data.get(k) is not None for k in ("temperature", "humidity", "battery"))
        data["age_seconds"] = age
        data["available"] = bool(received and age is not None and age <= self.stale_seconds and has_measurement)
        data["enabled"] = self.enabled
        if self._bind_key_error:
            data["configuration_error"] = self._bind_key_error
        return data


def decode_mibeacon_sensor(data: bytes, bind_key: bytes | None, advertised_address: str | None) -> dict | None:
    if len(data) < 5:
        return None
    pid = _u16le(data, 2)
    if pid != SENSOR_PRODUCT_ID:
        return None
    frame_counter = data[4]
    encrypted = bool(data[0] & 0x08)
    compact_encrypted = encrypted and len(data) == 19
    has_capability = bool(data[0] & 0x20)
    payload_start = 5 if compact_encrypted else (12 if has_capability else 11)
    raw_hex = data.hex()

    mac_nonce: bytes | None
    if compact_encrypted:
        normal = _mac_bytes(advertised_address)
        mac_nonce = normal[::-1] if normal else None
        mac = _normalize_mac(advertised_address)
    else:
        if len(data) < 11:
            return None
        mac_nonce = data[5:11]
        mac = ":".join(f"{b:02X}" for b in mac_nonce[::-1])

    authenticated = True
    if encrypted:
        if bind_key is None or mac_nonce is None:
            return {
                "product_id": pid,
                "frame_counter": frame_counter,
                "mac": mac or "UNKNOWN",
                "encrypted": True,
                "authenticated": False,
                "temperature": None,
                "humidity": None,
                "battery": None,
                "raw": raw_hex,
            }
        cipher_end = len(data) - 7
        if cipher_end < payload_start:
            return None
        ciphertext = data[payload_start:cipher_end]
        payload_counter = data[-7:-4]
        tag = data[-4:]
        nonce = mac_nonce + data[2:5] + payload_counter
        try:
            plain = AESCCM(bind_key, tag_length=4).decrypt(nonce, ciphertext + tag, b"\x11")
        except Exception:
            return {
                "product_id": pid,
                "frame_counter": frame_counter,
                "mac": mac or "UNKNOWN",
                "encrypted": True,
                "authenticated": False,
                "temperature": None,
                "humidity": None,
                "battery": None,
                "raw": raw_hex,
            }
    else:
        if payload_start > len(data):
            return None
        plain = data[payload_start:]

    temperature = None
    humidity = None
    battery = None
    offset = 0
    while offset + 3 <= len(plain):
        object_type = _u16le(plain, offset)
        length = plain[offset + 2]
        start = offset + 3
        end = start + length
        if end > len(plain):
            break
        if object_type == 0x1004 and length >= 2:
            temperature = _s16le(plain, start) / 10.0
        elif object_type == 0x1006 and length >= 2:
            humidity = _u16le(plain, start) / 10.0
        elif object_type == 0x100A and length >= 1:
            battery = plain[start]
        elif object_type == 0x100D and length >= 4:
            temperature = _s16le(plain, start) / 10.0
            humidity = _u16le(plain, start + 2) / 10.0
        elif object_type in (0x4801, 0x4C01) and length >= 4:
            value = float(struct.unpack_from("<f", plain, start)[0])
            if -80.0 <= value <= 120.0:
                temperature = value
        elif object_type in (0x4802, 0x4C02) and length >= 1:
            value = plain[start]
            if 0 <= value <= 100:
                humidity = float(value)
        elif object_type in (0x4808, 0x4C08) and length >= 4:
            value = float(struct.unpack_from("<f", plain, start)[0])
            if 0.0 <= value <= 100.0:
                humidity = value
        elif object_type == 0x4803 and length >= 1:
            battery = plain[start]
        offset = end

    return {
        "product_id": pid,
        "frame_counter": frame_counter,
        "mac": mac or "UNKNOWN",
        "encrypted": encrypted,
        "authenticated": authenticated,
        "temperature": temperature,
        "humidity": humidity,
        "battery": battery,
        "raw": raw_hex,
        "plain": plain.hex(),
    }


def _u16le(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _s16le(data: bytes, offset: int) -> int:
    value = _u16le(data, offset)
    return value - 0x10000 if value & 0x8000 else value


def _mac_bytes(address: str | None) -> bytes | None:
    if not address:
        return None
    parts = address.replace("-", ":").split(":")
    if len(parts) != 6:
        return None
    try:
        return bytes(int(part, 16) for part in parts)
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


class BleRuntime:
    def __init__(self, sensor: SensorStore, charger_cfg: list[ChargerConfig]):
        self.sensor = sensor
        self.charger_cfg = charger_cfg
        self.loop: asyncio.AbstractEventLoop | None = None
        self.manager: ChargerManager | None = None
        self.scanner: BleakScanner | None = None
        self.thread: threading.Thread | None = None
        self.started = threading.Event()
        self.lock = threading.RLock()
        self.devices: dict[str, object] = {}
        self.device_events: dict[str, asyncio.Event] = {}
        self.chargers: dict[str, dict] = {
            cfg.name: {
                "name": cfg.name,
                "address": _normalize_mac(cfg.address),
                "status": "starting",
                "connected": False,
                "authenticated": False,
            }
            for cfg in charger_cfg
        }
        self.last_error: str | None = None
        self._main_task: asyncio.Task | None = None

    @property
    def needed(self) -> bool:
        return self.sensor.enabled or bool(self.charger_cfg)

    def start(self) -> None:
        if not self.needed:
            return
        self.thread = threading.Thread(target=self._thread_main, name="lanmihome-ble", daemon=True)
        self.thread.start()
        self.started.wait(10)

    def stop(self) -> None:
        loop = self.loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._request_stop)
        if self.thread:
            self.thread.join(timeout=8)

    def _request_stop(self) -> None:
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()

    def _thread_main(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._main_task = self.loop.create_task(self._main())
        self.started.set()
        try:
            self.loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.exception("BLE runtime stopped unexpectedly")
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()

    async def _main(self) -> None:
        self.scanner = BleakScanner(detection_callback=self._on_detect)
        await self.scanner.start()
        LOG.info("BlueZ scanner started")
        manager_task = None
        try:
            if self.charger_cfg:
                self.manager = ChargerManager(
                    self.charger_cfg,
                    on_reading=self._on_charger_reading,
                    on_state=self._on_charger_state,
                    on_settings=self._on_charger_settings,
                    device_resolver=self._resolve_device,
                )
                manager_task = asyncio.create_task(self.manager.run(), name="cuktech-manager")
            while True:
                await asyncio.sleep(3600)
        finally:
            if self.manager:
                await self.manager.stop()
            if manager_task:
                manager_task.cancel()
                await asyncio.gather(manager_task, return_exceptions=True)
            if self.scanner:
                try:
                    await self.scanner.stop()
                except Exception:
                    pass

    def _on_detect(self, device, advertisement_data) -> None:
        address = _normalize_mac(getattr(device, "address", ""))
        if address:
            self.devices[address] = device
            event = self.device_events.get(address)
            if event:
                event.set()
        try:
            self.sensor.process_advertisement(device, advertisement_data)
        except Exception:
            LOG.exception("sensor advertisement parser failed")

    async def _resolve_device(self, address: str):
        key = _normalize_mac(address)
        device = self.devices.get(key)
        if device is not None:
            return device
        event = self.device_events.setdefault(key, asyncio.Event())
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            return None
        return self.devices.get(key)

    async def _on_charger_reading(self, name, _reading) -> None:
        self._capture_charger(name)

    async def _on_charger_settings(self, name, _state) -> None:
        self._capture_charger(name)

    async def _on_charger_state(self, name: str, status: str) -> None:
        with self.lock:
            current = self.chargers.setdefault(name, {"name": name})
            current["status"] = status
            current["updated_at"] = _iso(time.time())
        self._capture_charger(name)
        LOG.info("charger %s: %s", name, status)

    def _capture_charger(self, name: str) -> None:
        manager = self.manager
        if not manager:
            return
        controller = manager.controllers.get(name)
        if not controller:
            return
        snapshot = controller.state.snapshot()
        snapshot.update({
            "name": name,
            "address": _normalize_mac(controller.address),
        })
        with self.lock:
            old = self.chargers.get(name, {})
            snapshot["status"] = old.get("status", "unknown")
            snapshot["updated_at"] = _iso(time.time())
            self.chargers[name] = snapshot

    def snapshots(self) -> list[dict]:
        with self.lock:
            return [copy.deepcopy(self.chargers[name]) for name in sorted(self.chargers)]

    def snapshot(self, name: str) -> dict:
        with self.lock:
            if name not in self.chargers:
                raise KeyError(f"unknown charger: {name}")
            return copy.deepcopy(self.chargers[name])

    def execute(self, name: str, callback, timeout: float = 25.0):
        if not self.loop or not self.loop.is_running() or not self.manager:
            raise ConnectionError("charger runtime is not ready")
        future = asyncio.run_coroutine_threadsafe(
            self.manager.execute(name, callback, timeout=timeout),
            self.loop,
        )
        return future.result(timeout=timeout + 3.0)

    def patch(self, name: str, body: dict) -> dict:
        allowed = {
            "scene_mode", "screen_timeout", "language", "usb_a_low_current",
            "idle_screen_off", "orientation_lock",
        }
        unknown = set(body) - allowed
        if unknown:
            raise ValueError(f"unknown charger field(s): {', '.join(sorted(unknown))}")

        async def operation(controller):
            results = {}
            for key, value in body.items():
                if key == "scene_mode":
                    parsed = _enum_or_int(value, SCENE_MODES, "scene_mode", 1, 4)
                    result = await controller.set_mode(parsed)
                elif key == "screen_timeout":
                    parsed = _enum_or_int(value, SCREEN_TIMEOUTS, "screen_timeout", 0, 5)
                    result = await controller.set_screen_timeout(parsed)
                elif key == "language":
                    parsed = _enum_or_int(value, LANGUAGES, "language", 0, 1)
                    result = await controller.set_language(parsed)
                elif key == "usb_a_low_current":
                    result = await controller.set_usb_a_low_current(_bool(value, key))
                elif key == "idle_screen_off":
                    result = await controller.set_idle_screen_off(_bool(value, key))
                elif key == "orientation_lock":
                    result = await controller.set_orientation_lock(_bool(value, key))
                else:
                    raise ValueError(key)
                results[key] = _miot_result(result)
            return results

        results = self.execute(name, operation)
        return {"ok": True, "results": results, "charger": self.snapshot(name)}

    def action(self, name: str, body: dict) -> dict:
        action = str(body.get("name") or "").lower()

        async def operation(controller):
            if action == "goto":
                result = await controller.goto_page(_int(body.get("page"), "page", 1, 5))
            elif action == "set-timer":
                result = await controller.set_timer(
                    str(body.get("port") or ""),
                    _int(body.get("minutes"), "minutes", 0, 1440),
                )
            elif action == "set-port":
                result = await controller.set_port(
                    str(body.get("port") or ""),
                    _bool(body.get("enabled"), "enabled"),
                )
            elif action == "set-protocol":
                result = await controller.set_protocol(
                    str(body.get("port") or ""),
                    str(body.get("protocol") or ""),
                    _bool(body.get("enabled"), "enabled"),
                )
            else:
                raise ValueError(f"unknown charger action: {action}")
            return _miot_result(result)

        result = self.execute(name, operation)
        return {"ok": True, "action": action, "result": result, "charger": self.snapshot(name)}

    def raw(self, name: str, body: dict, write: bool) -> dict:
        piid = _int(body.get("piid"), "piid", 1, 65535)

        async def operation(controller):
            if write:
                value = _int(body.get("value"), "value", 0, 0xFFFFFFFF)
                result = await controller.set_property(piid, value, raw=True)
            else:
                result = await controller.get_property(piid)
            return _miot_result(result)

        return {"ok": True, "result": self.execute(name, operation)}


def _enum_or_int(value, mapping: dict[str, int], field: str, lo: int, hi: int) -> int:
    if isinstance(value, str):
        key = value.lower()
        if key in mapping:
            return mapping[key]
    return _int(value, field, lo, hi)


def _miot_result(result: MiotResult) -> dict:
    return {
        "ok": bool(result.ok),
        "value": result.value,
        "acknowledged": bool(result.acknowledged),
        "raw": result.raw.hex() if result.raw else None,
    }


class RecoveryController:
    def __init__(self, fan: FanAdapter, cfg: dict):
        self.fan = fan
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.start_text = str(self.cfg.get("start", "05:00"))
        self.end_text = str(self.cfg.get("end", "06:30"))
        self.retry_seconds = max(1, int(self.cfg.get("retry_seconds", 5)))
        self.force_timeout_seconds = max(10, int(self.cfg.get("force_timeout_seconds", 120)))
        self.enabled = self.cfg.get("enabled", True) is not False
        self.lock = threading.RLock()
        self.active = False
        self.success = False
        self.attempts = 0
        self.reason: str | None = None
        self.last_error: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "active": self.active,
                "success": self.success,
                "attempts": self.attempts,
                "reason": self.reason,
                "last_error": self.last_error,
                "window": {"start": self.start_text, "end": self.end_text},
            }

    def maybe_start(self) -> None:
        if self.enabled and self._inside_window():
            self.start(force=False)

    def start(self, force: bool = False) -> dict:
        with self.lock:
            if self.active:
                return self.snapshot()
            if not self.fan.enabled:
                self.reason = "fan-not-configured"
                return self.snapshot()
            if not force and not self._inside_window():
                self.reason = "outside-window"
                return self.snapshot()
            self.active = True
            self.success = False
            self.attempts = 0
            self.reason = "forced" if force else "power-return-window"
            self.last_error = None
        threading.Thread(target=self._run, args=(force,), name="lanmihome-recovery", daemon=True).start()
        return self.snapshot()

    def _run(self, force: bool) -> None:
        deadline = time.monotonic() + self.force_timeout_seconds if force else None
        while True:
            if not force and not self._inside_window():
                break
            if force and deadline is not None and time.monotonic() >= deadline:
                break
            with self.lock:
                self.attempts += 1
            try:
                if self.fan.ensure_power(True):
                    with self.lock:
                        self.success = True
                        self.active = False
                        self.reason = "confirmed-on"
                        self.last_error = None
                    return
            except Exception as exc:
                with self.lock:
                    self.last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(self.retry_seconds)
        with self.lock:
            self.active = False
            if not self.success:
                self.reason = "timeout" if force else "window-ended"

    def _inside_window(self) -> bool:
        now = datetime.now().time()
        try:
            start = datetime.strptime(self.start_text, "%H:%M").time()
            end = datetime.strptime(self.end_text, "%H:%M").time()
        except ValueError:
            return False
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end


class LanMiHomeApp:
    def __init__(self, config: dict):
        self.config = config
        self.fan = FanAdapter("fan", _device_config(config, "fan"))
        self.lamp = LampAdapter("lamp", _device_config(config, "lamp"))
        self.sensor = SensorStore(config.get("sensor", {}))
        charger_cfg = self._charger_configs(config.get("chargers", []))
        self.ble = BleRuntime(self.sensor, charger_cfg)
        self.recovery = RecoveryController(self.fan, config.get("recovery", {}))
        self.started_at = time.time()

    @staticmethod
    def _charger_configs(raw) -> list[ChargerConfig]:
        if isinstance(raw, dict):
            raw = [dict(value, name=name) for name, value in raw.items() if isinstance(value, dict)]
        if not isinstance(raw, list):
            return []
        configs: list[ChargerConfig] = []
        names: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            name = str(item.get("name") or "").strip()
            address = _normalize_mac(item.get("address") or item.get("mac"))
            token_text = item.get("token")
            if not name or not address or not token_text:
                continue
            if name in names:
                LOG.error("duplicate charger name %s; ignoring duplicate", name)
                continue
            try:
                token = _hex_bytes(str(token_text), 12, f"charger {name} token")
            except ValueError as exc:
                LOG.error("charger %s disabled: %s", name, exc)
                continue
            configs.append(ChargerConfig(name=name, address=address, token=token or b""))
            names.add(name)
        return configs

    def start(self) -> None:
        self.ble.start()
        self.recovery.maybe_start()

    def stop(self) -> None:
        self.ble.stop()

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "lanmihome",
            "version": 2,
            "uptime_seconds": int(time.time() - self.started_at),
            "bluetooth": {
                "enabled": self.ble.needed,
                "thread_alive": bool(self.ble.thread and self.ble.thread.is_alive()),
                "error": self.ble.last_error,
            },
        }

    def capabilities(self) -> dict:
        return {
            "api": "v1",
            "fan": self.fan.enabled,
            "lamp": self.lamp.enabled,
            "sensor": self.sensor.enabled,
            "chargers": [item["name"] for item in self.ble.snapshots()],
            "features": [
                "fan", "lamp", "sensor", "charger", "recovery", "raw-miot",
                "bluez-shared-scan", "cuktech-multi-device",
            ],
        }

    def state(self) -> dict:
        return {
            "fan": self.fan.state(),
            "lamp": self.lamp.state(),
            "sensor": self.sensor.snapshot(),
            "chargers": self.ble.snapshots(),
            "recovery": self.recovery.snapshot(),
        }

    def raw_miot(self, device_name: str, operation: str, body: dict) -> dict:
        adapter = self.fan if device_name == "fan" else self.lamp if device_name == "lamp" else None
        if adapter is None:
            raise ValueError("raw MIoT device must be fan or lamp")
        with adapter.lock:
            device = adapter.new_device()
            siid = _int(body.get("siid"), "siid", 1, 65535)
            if operation == "get":
                piid = _int(body.get("piid"), "piid", 1, 65535)
                value = _miot_get_one(device, siid, piid)
                return {"ok": True, "value": value, "siid": siid, "piid": piid}
            if operation == "set":
                piid = _int(body.get("piid"), "piid", 1, 65535)
                if "value" not in body:
                    raise ValueError("value is required")
                _miot_set(device, siid, piid, body["value"], "raw MIoT set")
                return {"ok": True, "siid": siid, "piid": piid}
            if operation == "action":
                aiid = _int(body.get("aiid"), "aiid", 1, 65535)
                params = body.get("params", [])
                if not isinstance(params, list):
                    raise ValueError("params must be an array")
                _miot_action(device, siid, aiid, params, "raw MIoT action")
                return {"ok": True, "siid": siid, "aiid": aiid}
        raise ValueError("unknown raw operation")


class LanMiHomeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app: LanMiHomeApp):
        super().__init__(address, handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    server_version = "LanMiHome/2"

    @property
    def app(self) -> LanMiHomeApp:
        return self.server.app  # type: ignore[attr-defined]

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._common_headers(0)
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("http %s - %s", self.address_string(), fmt % args)

    def _dispatch(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path.rstrip("/") or "/"
            body = self._read_json() if method in ("POST", "PATCH") else {}
            result = self._route(method, path, body)
            self._json(HTTPStatus.OK, result)
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, KeyError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (ConnectionError, TimeoutError, OSError) as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            LOG.exception("request failed: %s %s", method, self.path)
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})

    def _route(self, method: str, path: str, body: dict):
        if method == "GET":
            if path == "/api/v1/health":
                return self.app.health()
            if path == "/api/v1/capabilities":
                return self.app.capabilities()
            if path == "/api/v1/state":
                return self.app.state()
            if path == "/api/v1/fan":
                return self.app.fan.state()
            if path == "/api/v1/lamp":
                return self.app.lamp.state()
            if path == "/api/v1/sensor":
                return self.app.sensor.snapshot()
            if path == "/api/v1/chargers":
                return {"chargers": self.app.ble.snapshots()}
            if path == "/api/v1/system/recovery":
                return self.app.recovery.snapshot()
            parts = self._parts(path)
            if len(parts) == 4 and parts[:3] == ["api", "v1", "charger"]:
                return self.app.ble.snapshot(parts[3])

        if method == "PATCH":
            if path == "/api/v1/fan":
                return self.app.fan.patch(body)
            if path == "/api/v1/lamp":
                return self.app.lamp.patch(body)
            parts = self._parts(path)
            if len(parts) == 4 and parts[:3] == ["api", "v1", "charger"]:
                return self.app.ble.patch(parts[3], body)

        if method == "POST":
            if path == "/api/v1/fan/action":
                return self.app.fan.action(body)
            if path == "/api/v1/lamp/action":
                return self.app.lamp.action(body)
            if path == "/api/v1/sensor/report":
                return self.app.sensor.external_report(body)
            if path == "/api/v1/system/recovery/start":
                return self.app.recovery.start(_bool(body.get("force", False), "force"))
            parts = self._parts(path)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "raw"] and parts[3] in ("fan", "lamp"):
                return self.app.raw_miot(parts[3], parts[4], body)
            if len(parts) == 5 and parts[:3] == ["api", "v1", "charger"] and parts[4] == "action":
                return self.app.ble.action(parts[3], body)
            if len(parts) == 6 and parts[:3] == ["api", "v1", "charger"] and parts[4] == "raw":
                if parts[5] == "get":
                    return self.app.ble.raw(parts[3], body, False)
                if parts[5] == "set":
                    return self.app.ble.raw(parts[3], body, True)

        raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    @staticmethod
    def _parts(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/") if part]

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1024 * 1024:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid JSON body") from None
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(status)
        self._common_headers(len(data))
        self.end_headers()
        self.wfile.write(data)

    def _common_headers(self, length: int) -> None:
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Accept")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("config root must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="LanMiHome unified router backend")
    parser.add_argument("--config", default="/lotusemmc/lanmihome/config.json")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = Path(args.config)
    config = load_config(config_path)
    app = LanMiHomeApp(config)
    if args.check_config:
        print(json.dumps({
            "ok": True,
            "fan": app.fan.enabled,
            "lamp": app.lamp.enabled,
            "sensor": app.sensor.enabled,
            "chargers": [item["name"] for item in app.ble.snapshots()],
        }, ensure_ascii=False))
        return 0

    listen = str(config.get("listen") or config.get("bind") or "0.0.0.0")
    port = int(config.get("port", 8765))
    server = LanMiHomeHTTPServer((listen, port), Handler, app)

    def shutdown(_signum=None, _frame=None):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    app.start()
    LOG.info("LanMiHome listening on %s:%s", listen, port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
