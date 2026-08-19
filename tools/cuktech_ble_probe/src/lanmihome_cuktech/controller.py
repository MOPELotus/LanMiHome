from __future__ import annotations

from dataclasses import dataclass

from .miot import MiotCommandClient, MiotResult
from .protocol import (
    PIID_RANGES,
    PORT_BITS,
    READABLE_SETTINGS_PIIDS,
    SIID_CHARGER,
    SUPPORTED_PROTOCOL_SWITCHES,
    TIMER_PORTS,
    WRITEABLE_SETTINGS_PIIDS,
    PortReading,
    PropertyPush,
    parse_property_push,
    set_protocol_switch,
)
from .session import CuktechSession, DeviceInfo
from .state import ChargerState


@dataclass(slots=True)
class DeviceEvent:
    kind: str
    port: PortReading | None = None
    property: PropertyPush | None = None


class CuktechController:
    """Complete per-device AD1204 controller built on one isolated BLE session."""

    def __init__(self, name: str, address: str, token: bytes):
        self.name = name
        self.address = address
        self.session = CuktechSession(name, address, token)
        self.miot = MiotCommandClient(self.session)
        self.state = ChargerState()

    @property
    def connected(self) -> bool:
        return self.session.connected

    @property
    def authenticated(self) -> bool:
        return self.session.authenticated

    async def connect(self) -> DeviceInfo:
        await self.session.connect()
        self.state.connected = True
        info = await self.session.read_device_info()
        self.state.model = info.model
        self.state.firmware = info.firmware
        self.state.miot_version = info.protocol_version
        return info

    async def authenticate(self) -> None:
        await self.session.authenticate()
        self.state.authenticated = True

    async def disconnect(self) -> None:
        await self.session.disconnect()
        self.state.connected = False
        self.state.authenticated = False

    async def initialize_state(self) -> list[PortReading]:
        await self.refresh_settings()
        return await self.refresh_ports()

    async def refresh_settings(self) -> dict[int, int]:
        values: dict[int, int] = {}
        for piid in READABLE_SETTINGS_PIIDS:
            result = await self.miot.get(SIID_CHARGER, piid)
            self.process_deferred(result)
            if result.value is not None:
                self.state.apply_setting(piid, result.value)
                values[piid] = result.value
        return values

    async def refresh_ports(self, piids: tuple[int, ...] = (1, 2, 3, 4)) -> list[PortReading]:
        readings: list[PortReading] = []
        for piid in piids:
            reading = await self.poll_port(piid)
            if reading is not None:
                readings.append(reading)
        return readings

    async def poll_port(self, piid: int) -> PortReading | None:
        reading, _ = await self.poll_port_result(piid)
        return reading

    async def poll_port_result(self, piid: int) -> tuple[PortReading | None, MiotResult]:
        if piid not in range(1, 5):
            raise ValueError("port PIID must be 1..4")
        result = await self.miot.get(SIID_CHARGER, piid)
        self.process_deferred(result)
        reading = None
        if result.value is not None:
            reading = self.state.decode_port_value(piid, result.value, result.raw)
        return reading, result

    async def get_property(self, piid: int) -> MiotResult:
        result = await self.miot.get(SIID_CHARGER, piid)
        self.process_deferred(result)
        if result.value is not None:
            if piid in range(1, 5):
                self.state.decode_port_value(piid, result.value, result.raw)
            else:
                self.state.apply_setting(piid, result.value)
        return result

    async def set_property(self, piid: int, value: int, *, raw: bool = False) -> MiotResult:
        if not raw:
            if piid not in WRITEABLE_SETTINGS_PIIDS:
                raise ValueError(f"PIID {piid} is not a known writable AD1204 property")
            limits = PIID_RANGES.get(piid)
            if limits and not (limits[0] <= value <= limits[1]):
                raise ValueError(f"PIID {piid} value must be in {limits[0]}..{limits[1]}")
        result = await self.miot.set(SIID_CHARGER, piid, value)
        self.process_deferred(result)
        if result.ok:
            self.state.apply_setting(piid, value)
        return result

    async def set_mode(self, value: int) -> MiotResult:
        return await self.set_property(5, value)

    async def set_screen_timeout(self, value: int) -> MiotResult:
        return await self.set_property(6, value)

    async def set_language(self, value: int) -> MiotResult:
        return await self.set_property(13, value)

    async def set_usb_a_low_current(self, enabled: bool) -> MiotResult:
        return await self.set_property(15, int(enabled))

    async def set_idle_screen_off(self, enabled: bool) -> MiotResult:
        return await self.set_property(19, int(enabled))

    async def set_orientation_lock(self, enabled: bool) -> MiotResult:
        return await self.set_property(20, int(enabled))

    async def goto_page(self, page: int) -> MiotResult:
        return await self.set_property(14, page)

    async def set_timer(self, port: str, minutes: int) -> MiotResult:
        try:
            piid = TIMER_PORTS[port.lower()]
        except KeyError as exc:
            raise ValueError("port must be c1/c2/c3/a") from exc
        return await self.set_property(piid, minutes)

    async def set_port(self, port: str, enabled: bool) -> MiotResult:
        port = port.lower()
        current = await self.get_property(16)
        if current.value is None:
            raise RuntimeError("unable to read current port-control bitmask")
        if port == "all":
            value = 0x0F if enabled else 0x00
        else:
            try:
                bit = PORT_BITS[port]
            except KeyError as exc:
                raise ValueError("port must be c1/c2/c3/a/all") from exc
            value = current.value | (1 << bit) if enabled else current.value & ~(1 << bit)
        if value == current.value:
            return MiotResult(SIID_CHARGER, 16, value=value, acknowledged=True)
        return await self.set_property(16, value)

    async def set_protocol(self, port: str, protocol: str, enabled: bool) -> MiotResult:
        port = port.lower()
        protocol = protocol.lower()
        if port not in SUPPORTED_PROTOCOL_SWITCHES:
            raise ValueError("port must be c1/c2/c3/a")
        current = await self.get_property(21)
        if current.value is None:
            raise RuntimeError("unable to read protocol-extend bitmask (PIID 21)")
        value = set_protocol_switch(current.value, port, protocol, enabled)
        if value == current.value:
            return MiotResult(SIID_CHARGER, 21, value=value, acknowledged=True)
        return await self.set_property(21, value)

    async def next_event(self, timeout: float = 5.0) -> DeviceEvent | None:
        plaintext = await self.session.next_plaintext(timeout)
        if plaintext is None:
            return None
        return self.process_plaintext(plaintext)

    def process_plaintext(self, plaintext: bytes) -> DeviceEvent | None:
        prop = parse_property_push(plaintext)
        if prop is None or prop.siid != SIID_CHARGER:
            return None
        if prop.piid in range(1, 5):
            reading = self.state.decode_port_push(plaintext)
            return DeviceEvent("port", port=reading, property=prop) if reading else None
        if prop.value is not None:
            self.state.apply_setting(prop.piid, prop.value)
        return DeviceEvent("property", property=prop)

    def process_deferred(self, result: MiotResult) -> list[DeviceEvent]:
        events: list[DeviceEvent] = []
        for plaintext in result.deferred_plaintexts:
            event = self.process_plaintext(plaintext)
            if event is not None:
                events.append(event)
        return events
