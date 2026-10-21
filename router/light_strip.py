from __future__ import annotations

from http import HTTPStatus
import re
import threading
from typing import Protocol


class LightStripUnavailable(RuntimeError):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


class LightStripTransport(Protocol):
    def read_state(self) -> dict: ...

    def set_state(self, changes: dict) -> None: ...


class LightStripAdapter:
    def __init__(self, config: dict, transport: LightStripTransport | None = None):
        if not isinstance(config, dict):
            raise ValueError("light_strip must be an object")
        self.enabled = config.get("enabled", False)
        if not isinstance(self.enabled, bool):
            raise ValueError("light_strip.enabled must be boolean")
        self.model = str(config.get("model") or "SP651E").upper()
        if self.model != "SP651E":
            raise ValueError("light_strip.model must be SP651E")
        self.address = str(config.get("address") or "").strip().replace("-", ":").upper()
        if self.address and not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", self.address):
            raise ValueError("light_strip.address must be a Bluetooth MAC address")
        self._transport = transport
        self.protocol = str(config.get("protocol") or "pending")
        self._lock = threading.RLock()

    def attach_transport(self, transport: LightStripTransport) -> None:
        with self._lock:
            self._transport = transport

    def capabilities(self) -> dict:
        return {
            "model": self.model,
            "enabled": self.enabled,
            "protocol_ready": self._transport is not None,
            "protocol": self.protocol,
            "controls": ["power", "brightness"],
            "brightness_min": 1,
            "brightness_max": 100,
        }

    def _pending_status(self) -> str | None:
        if not self.enabled:
            return "disabled" if self.address else "not_configured"
        if not self.address:
            return "not_configured"
        if self._transport is None:
            return "protocol_pending"
        return None

    def _base_state(self) -> dict:
        return {
            "name": "灯带",
            "model": self.model,
            "address": self.address or None,
            "enabled": self.enabled,
            "available": False,
            "control_ready": False,
            "power": None,
            "brightness": None,
            "error": None,
            "capabilities": self.capabilities(),
        }

    def state(self) -> dict:
        with self._lock:
            state = self._base_state()
            pending = self._pending_status()
            if pending:
                return dict(state, status=pending, setup_required=True)
            try:
                reported = self._transport.read_state()
                values = self._validate_patch(reported)
                if set(values) != {"power", "brightness"}:
                    raise ValueError("device state must contain power and brightness")
                return dict(state, **values, available=True, control_ready=True, status="ready", setup_required=False,
                            firmware=getattr(self._transport, "firmware", None), mode=getattr(self._transport, "mode", None))
            except Exception:
                return dict(state, status="offline", setup_required=False, error="light-strip-state-unavailable")

    @staticmethod
    def _validate_patch(body: dict) -> dict:
        if not isinstance(body, dict) or not body:
            raise ValueError("light-strip command must be a non-empty object")
        unknown = set(body) - {"power", "brightness"}
        if unknown:
            raise ValueError("unknown light-strip fields: " + ", ".join(sorted(unknown)))
        if "power" in body and not isinstance(body["power"], bool):
            raise ValueError("power must be boolean")
        if "brightness" in body:
            value = body["brightness"]
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError("brightness must be an integer from 1 to 100")
        return dict(body)

    def patch(self, body: dict) -> dict:
        changes = self._validate_patch(body)
        with self._lock:
            pending = self._pending_status()
            if pending:
                status = HTTPStatus.NOT_IMPLEMENTED if pending == "protocol_pending" else HTTPStatus.SERVICE_UNAVAILABLE
                raise LightStripUnavailable(status, pending)
            try:
                self._transport.set_state(changes)
                reported = self.state()
            except Exception as error:
                raise LightStripUnavailable(HTTPStatus.SERVICE_UNAVAILABLE, "light-strip-command-failed") from error
            if not reported["available"] or any(reported[key] != value for key, value in changes.items()):
                raise LightStripUnavailable(HTTPStatus.SERVICE_UNAVAILABLE, "light-strip-command-unconfirmed")
            return reported
