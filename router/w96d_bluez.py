#!/usr/bin/env python3
"""BlueZ/Bleak transport for W96D.

This layer owns the BLE connection only. Ownership selection is handled by
w96d_ownership and runtime layers.
"""
from __future__ import annotations

from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

from lanmihome_w96d import CHARS


@dataclass
class W96DBleState:
    address: str | None = None
    connected: bool = False


class W96DBluezTransport:
    def __init__(self, address: str | None = None):
        self.state = W96DBleState(address=address)
        self.client: BleakClient | None = None

    async def discover(self):
        devices = await BleakScanner.discover()
        for device in devices:
            if "W96D" in (device.name or ""):
                self.state.address = device.address
                return device.address
        return None

    async def connect(self):
        if not self.state.address:
            await self.discover()
        if not self.state.address:
            raise RuntimeError("W96D not found")
        self.client = BleakClient(self.state.address)
        await self.client.connect()
        self.state.connected = True

    async def disconnect(self):
        if self.client:
            await self.client.disconnect()
        self.state.connected = False

    async def write(self, characteristic: str, value: int):
        if not self.client:
            raise RuntimeError("W96D is disconnected")
        await self.client.write_gatt_char(
            CHARS[characteristic],
            bytes([value & 0xff]),
        )

    async def read(self, characteristic: str):
        if not self.client:
            raise RuntimeError("W96D is disconnected")
        return await self.client.read_gatt_char(CHARS[characteristic])
