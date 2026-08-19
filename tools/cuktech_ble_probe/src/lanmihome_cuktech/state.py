from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import monotonic

from .protocol import (
    PORT_KEYS,
    PortReading,
    decode_pdo_caps,
    decode_protocol_extend,
    extract_hw_protocols,
    parse_port_push,
    parse_port_value,
)


@dataclass(slots=True)
class ChargerState:
    connected: bool = False
    authenticated: bool = False
    model: str = ""
    firmware: str = ""
    miot_version: str = ""
    settings: dict[int, int] = field(default_factory=dict)
    ports: dict[int, PortReading] = field(default_factory=dict)
    pdo_caps: dict[str, dict[str, dict[str, int | str | None]]] = field(default_factory=dict)
    hw_protocols: dict[int, int] = field(default_factory=dict)
    last_port_update: dict[int, float] = field(default_factory=dict)
    last_setting_update: dict[int, float] = field(default_factory=dict)
    c3a_shared: bool = False

    @property
    def protocol_extend(self) -> int:
        return self.settings.get(21, 0)

    @property
    def protocol_switches(self) -> dict[str, dict[str, bool]]:
        return decode_protocol_extend(self.protocol_extend)

    def apply_setting(self, piid: int, value: int) -> None:
        self.settings[piid] = value
        self.last_setting_update[piid] = monotonic()
        if piid == 17:
            self.pdo_caps["c1c2"] = decode_pdo_caps(value, "c1", "c2")
            for port, proto in extract_hw_protocols(17, value).items():
                if proto > 0:
                    self.hw_protocols[port] = proto
        elif piid == 18:
            self.pdo_caps["c3a"] = decode_pdo_caps(value, "c3", "a")
            for port, proto in extract_hw_protocols(18, value).items():
                if proto > 0:
                    self.hw_protocols[port] = proto

    def decode_port_push(self, plaintext: bytes) -> PortReading | None:
        if len(plaintext) < 9:
            return None
        piid = plaintext[7] | (plaintext[8] << 8)
        if piid not in range(1, 5):
            return None
        reading = parse_port_push(
            plaintext,
            pdo_data=self._pdo_for_port(piid),
            protocol_switches=self.protocol_switches,
            hw_protocol=self.hw_protocols.get(piid),
        )
        if reading:
            self.apply_port(reading)
        return reading

    def decode_port_value(self, piid: int, value: int, plaintext: bytes | None = None) -> PortReading | None:
        reading = parse_port_value(
            piid,
            value,
            plaintext,
            pdo_data=self._pdo_for_port(piid),
            protocol_switches=self.protocol_switches,
            hw_protocol=self.hw_protocols.get(piid),
        )
        if reading:
            self.apply_port(reading)
        return reading

    def apply_port(self, reading: PortReading) -> None:
        self.ports[reading.piid] = reading
        self.last_port_update[reading.piid] = monotonic()

        # PIID 17/18 hardware protocol bytes are intentionally sticky across
        # transient zero values, because the charger can publish the protocol
        # property slightly before/after the electrical port frame. A *true*
        # idle port frame is the authoritative session boundary, though: once
        # status, voltage and current are all zero, carrying the previous
        # session's hardware protocol into the next plug-in can mislabel the
        # first few frames before PIID 17/18 catches up.
        if not reading.active:
            self.hw_protocols.pop(reading.piid, None)

        if reading.piid == 3:
            self.c3a_shared = reading.shared
        elif reading.piid == 4 and reading.shared:
            self.c3a_shared = True

    def port_enabled(self, piid: int) -> bool:
        mask = self.settings.get(16, 0x0F)
        return bool(mask & (1 << (piid - 1)))

    def total_power(self) -> float:
        total = 0.0
        for piid, reading in self.ports.items():
            # In the shared C3+A mode PIID 3 carries the aggregate measurement.
            # Never add A again even if an older A reading remains cached.
            if self.c3a_shared and piid == 4:
                continue
            total += reading.power
        return round(total, 2)

    def snapshot(self) -> dict:
        ports: dict[str, dict] = {}
        for piid in range(1, 5):
            reading = self.ports.get(piid)
            if reading is None:
                data = {
                    "piid": piid,
                    "name": "C3+A" if self.c3a_shared and piid == 3 else PORT_KEYS[piid].upper(),
                    "voltage": 0.0,
                    "current": 0.0,
                    "power": 0.0,
                    "active": False,
                    "status_raw": 0,
                    "code_raw": 0,
                    "protocol_hint": "idle",
                    "plaintext_hex": "",
                    "protocol_number": None,
                    "protocol_source": "no-load",
                    "shared": self.c3a_shared and piid == 3,
                    "shared_group": "c3+a" if self.c3a_shared and piid == 3 else None,
                }
            else:
                data = asdict(reading)
            data["enabled"] = self.port_enabled(piid)
            if self.c3a_shared and piid == 4:
                data["measurement_suppressed_by"] = "c3+a"
            ports[PORT_KEYS[piid]] = data

        return {
            "connected": self.connected,
            "authenticated": self.authenticated,
            "device_model": self.model,
            "firmware_version": self.firmware,
            "miot_version": self.miot_version,
            "ports": ports,
            "total_power": self.total_power(),
            "c3a_shared": self.c3a_shared,
            "settings": {str(k): v for k, v in sorted(self.settings.items())},
            "protocol_switches": self.protocol_switches,
            "hw_protocols": {PORT_KEYS[k]: v for k, v in sorted(self.hw_protocols.items())},
            "pdo_caps": self.pdo_caps,
        }

    def _pdo_for_port(self, piid: int) -> dict | None:
        if piid in (1, 2):
            return self.pdo_caps.get("c1c2", {}).get(PORT_KEYS[piid])
        return self.pdo_caps.get("c3a", {}).get(PORT_KEYS[piid])
