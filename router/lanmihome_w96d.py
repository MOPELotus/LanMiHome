#!/usr/bin/env python3
"""W96D BLE fan provider for LanMiHome.

The provider intentionally does not own routing policy. A caller selects the
BLE owner (router/night-node/phone) and exposes this same API surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bleak import BleakClient, BleakScanner

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
CHARS = {
    "power": "0000fff1-0000-1000-8000-00805f9b34fb",
    "speed": "0000fff3-0000-1000-8000-00805f9b34fb",
    "natural": "0000fff4-0000-1000-8000-00805f9b34fb",
    "timer": "0000fff5-0000-1000-8000-00805f9b34fb",
    "turbo": "0000fffc-0000-1000-8000-00805f9b34fb",
    "light": "0000fffa-0000-1000-8000-00805f9b34fb",
    "battery": "0000ffd1-0000-1000-8000-00805f9b34fb",
    "power_status": "0000ffd2-0000-1000-8000-00805f9b34fb",
    "motor": "0000ffd3-0000-1000-8000-00805f9b34fb",
}


@dataclass
class W96DStatus:
    power: bool = False
    speed: int = 0
    natural: bool = False
    turbo: bool = False
    battery: dict[str, Any] | None = None
    power_status: dict[str, Any] | None = None
    motor: dict[str, Any] | None = None


class W96DController:
    def __init__(self, address: str | None = None):
        self.address = address
        self.client: BleakClient | None = None
        self.status = W96DStatus()

    async def connect(self):
        if not self.address:
            devices = await BleakScanner.discover()
            target = next((d for d in devices if "W96D" in (d.name or "")), None)
            if not target:
                raise RuntimeError("W96D not found")
            self.address = target.address
        self.client = BleakClient(self.address)
        await self.client.connect()

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def write(self, name: str, value: int):
        if not self.client:
            raise RuntimeError("not connected")
        await self.client.write_gatt_char(CHARS[name], bytes([value & 0xff]))

    async def set_power(self, enabled: bool):
        await self.write("power", 1 if enabled else 0)
        self.status.power = enabled

    async def set_speed(self, speed: int):
        speed = max(0, min(100, int(speed)))
        await self.write("speed", speed)
        self.status.speed = speed

    async def set_natural(self, enabled: bool):
        await self.write("natural", 1 if enabled else 0)
        self.status.natural = enabled

    async def set_turbo(self, enabled: bool):
        await self.write("turbo", 1 if enabled else 0)
        self.status.turbo = enabled

    async def set_light(self, enabled: bool):
        await self.write("light", 1 if enabled else 0)

    def json(self):
        return {
            "device": "W96D",
            "owner": "router",
            **self.status.__dict__,
        }
