#!/usr/bin/env python3
"""Ownership policy for W96D BLE sessions.

No discovery race is performed. The selected environment owns the device.
"""
from dataclasses import dataclass
from datetime import datetime, time


@dataclass
class W96DOwner:
    mode: str
    owner: str


class W96DOwnership:
    def __init__(self, mode: str = "home"):
        self.mode = mode

    def owner(self, now: datetime | None = None) -> str:
        if self.mode == "home":
            return "router"
        if self.mode == "outdoor":
            return "phone"
        now = (now or datetime.now()).time()
        if now >= time(23, 0) or now < time(6, 0):
            return "night_node"
        return "router"
