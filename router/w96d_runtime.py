#!/usr/bin/env python3
"""Runtime wrapper for W96D BLE devices.

Keeps BLE ownership outside the device driver. The service layer chooses
router/night_node/phone and supplies the active transport.
"""
from __future__ import annotations

import threading
from copy import deepcopy


class W96DRuntime:
    def __init__(self, config: dict, controller=None):
        self.config = config if isinstance(config, dict) else {}
        self.controller = controller
        self.lock = threading.RLock()
        self.owner = str(self.config.get("owner", "router"))
        self.mode = str(self.config.get("mode", "home"))
        self.enabled = self.config.get("enabled", False) is True
        self.state_cache = {
            "device": "W96D",
            "owner": self.owner,
            "mode": self.mode,
            "available": False,
            "power": False,
            "speed": 0,
            "natural": False,
            "turbo": False,
            "light": False,
            "battery": None,
            "vbus": None,
            "motor": None,
        }

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state_cache)

    def set_owner(self, owner: str):
        with self.lock:
            self.owner = owner
            self.state_cache["owner"] = owner

    def patch(self, body: dict):
        allowed = {"power", "speed", "natural", "turbo", "light"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError("unknown W96D field(s): " + ",".join(sorted(unknown)))

        with self.lock:
            for key, value in body.items():
                if key == "speed":
                    value = max(0, min(100, int(value)))
                self.state_cache[key] = value

        # Actual BLE transport is injected by the owner backend.
        if self.controller:
            return self.controller.patch(body)
        return {"ok": True, "state": self.snapshot()}
