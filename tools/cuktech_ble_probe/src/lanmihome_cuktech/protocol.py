from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

UUID_FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"
CHAR_DEVICE_INFO = "0000001c-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CTRL = "00000010-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_DATA = "00000019-0000-1000-8000-00805f9b34fb"
CHAR_CMD_SEND = "0000001a-0000-1000-8000-00805f9b34fb"
CHAR_CMD_RECV = "0000001b-0000-1000-8000-00805f9b34fb"
CHAR_FW_VERSION = "00000004-0000-1000-8000-00805f9b34fb"

PRODUCT_ID = 0x660E
SIID_CHARGER = 2
PORT_NAMES = {1: "C1", 2: "C2", 3: "C3", 4: "A"}
PORT_KEYS = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}
TIMER_PORTS = {"c1": 9, "c2": 10, "c3": 11, "a": 12}

PIID_NAMES = {
    1: "C1 port data",
    2: "C2 port data",
    3: "C3 port data",
    4: "A port data",
    5: "scene mode",
    6: "screen timeout",
    7: "protocol control (legacy/raw)",
    8: "countdown config",
    9: "C1 countdown minutes",
    10: "C2 countdown minutes",
    11: "C3 countdown minutes",
    12: "A countdown minutes",
    13: "language",
    14: "screen page (write-only)",
    15: "USB-A low-current mode",
    16: "port control bitmask",
    17: "C1/C2 protocol/PDO state",
    18: "C3/A protocol/PDO state",
    19: "idle screen-off",
    20: "screen orientation lock",
    21: "protocol extend control",
}

READABLE_SETTINGS_PIIDS = (5, 6, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21)
WRITEABLE_SETTINGS_PIIDS = {5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 19, 20, 21}
PIID_RANGES = {
    5: (1, 4),
    6: (0, 5),
    8: (0, 1440),
    9: (0, 1440),
    10: (0, 1440),
    11: (0, 1440),
    12: (0, 1440),
    13: (0, 1),
    14: (1, 5),
    15: (0, 1),
    16: (0, 15),
    19: (0, 1),
    20: (0, 1),
    21: (0, 0xFFFFFFFF),
}

SCENE_MODES = {"ai": 1, "digital": 2, "apple": 2, "single": 3, "balance": 4}
SCENE_NAMES = {1: "AI", 2: "digital", 3: "single", 4: "balance"}
SCREEN_TIMEOUTS = {"1": 5, "5": 1, "10": 2, "30": 3, "off": 4, "always": 4}
SCREEN_TIMEOUT_NAMES = {1: "5 min", 2: "10 min", 3: "30 min", 4: "always on", 5: "1 min"}
LANGUAGES = {"en": 0, "cn": 1, "zh": 1}
LANGUAGE_NAMES = {0: "English", 1: "Chinese"}

PROTOCOL_SWITCH_BITS = {
    "c1": {"pd": 0, "pps": 1, "ufcs": 2},
    "c2": {"pd": 8, "pps": 9, "ufcs": 10},
    "c3": {"ufcs": 16, "scp": 17},
    "a": {"ufcs": 24, "scp": 25},
}
SUPPORTED_PROTOCOL_SWITCHES = {
    port: tuple(bits) for port, bits in PROTOCOL_SWITCH_BITS.items()
}

MIJIA_PROTOCOLS = {
    0: "idle",
    1: "5V",
    2: "5V",
    3: "QC",
    4: "AFC",
    5: "FCP",
    6: "SCP",
    7: "PD",
    8: "PPS",
    9: "PPS",
    10: "UFCS",
}

PDO_KIND_BY_HIGH_BYTE = {0x07: "PD Fixed", 0x08: "PD PPS"}
PD_FIXED_VOLTAGES = (5.0, 9.0, 12.0, 15.0, 20.0)
PPS_VOLTAGE_RANGE = (3.0, 21.0)


@dataclass(slots=True)
class PortReading:
    piid: int
    name: str
    voltage: float
    current: float
    power: float
    active: bool
    status_raw: int
    code_raw: int
    protocol_hint: str
    plaintext_hex: str
    protocol_number: int | None = None
    protocol_source: str = "heuristic"
    shared: bool = False
    shared_group: str | None = None

    @property
    def protocol(self) -> str:
        return self.protocol_hint


@dataclass(slots=True)
class PropertyPush:
    siid: int
    piid: int
    value: int | None
    raw_value: bytes
    plaintext: bytes


def mac_to_miot_bytes(address: str) -> bytes:
    parts = address.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid BLE MAC address: {address!r}")
    return bytes(int(part, 16) for part in reversed(parts))


def protocol_name(number: int | None) -> str:
    if number is None:
        return "unknown"
    return MIJIA_PROTOCOLS.get(number, f"protocol-{number}")


def decode_protocol_extend(value: int) -> dict[str, dict[str, bool]]:
    value &= 0xFFFFFFFF
    return {
        port: {name: bool(value & (1 << bit)) for name, bit in bits.items()}
        for port, bits in PROTOCOL_SWITCH_BITS.items()
    }


def set_protocol_switch(value: int, port: str, protocol: str, enabled: bool) -> int:
    port = port.lower()
    protocol = protocol.lower()
    try:
        bit = PROTOCOL_SWITCH_BITS[port][protocol]
    except KeyError as exc:
        supported = ", ".join(SUPPORTED_PROTOCOL_SWITCHES.get(port, ()))
        if not supported:
            raise ValueError(f"unknown port: {port}") from exc
        raise ValueError(f"{port} does not expose {protocol}; supported: {supported}") from exc
    value &= 0xFFFFFFFF
    if enabled:
        value |= 1 << bit
    else:
        value &= ~(1 << bit)
    # C1/C2 bit3 in each byte is reserved and is normally fixed to 1.
    value |= (1 << 3) | (1 << 11)
    return value


def decode_pdo_caps(value: int, high_port: str, low_port: str) -> dict[str, dict[str, int | str | None]]:
    value &= 0xFFFFFFFF
    low_half = value & 0xFFFF
    high_half = (value >> 16) & 0xFFFF

    def cap(half: int) -> int | None:
        byte = half & 0xFF
        return byte or None

    def kind(half: int) -> str | None:
        if (half & 0xFF) == 0:
            return None
        return PDO_KIND_BY_HIGH_BYTE.get((half >> 8) & 0xFF)

    return {
        low_port: {"cap": cap(low_half), "kind": kind(low_half)},
        high_port: {"cap": cap(high_half), "kind": kind(high_half)},
    }


def extract_hw_protocols(piid: int, value: int) -> dict[int, int]:
    """Decode the protocol numbers carried by PIID 17/18.

    Upstream/Mi Home mapping is byte[0] for the first named port and byte[2]
    for the second when looking at the device's big-endian display order. With
    the UINT32 represented as an integer this corresponds to bits 31..24 and
    15..8 respectively.
    """
    value &= 0xFFFFFFFF
    first = (value >> 24) & 0xFF
    second = (value >> 8) & 0xFF
    if piid == 17:
        return {1: first, 2: second}
    if piid == 18:
        return {3: first, 4: second}
    raise ValueError("hardware protocol mapping exists only for PIID 17/18")


def parse_property_push(plaintext: bytes) -> PropertyPush | None:
    """Parse an unsolicited MiOT property/result frame (opcode 0x04)."""
    if len(plaintext) < 11 or plaintext[4] != 0x04:
        return None
    siid = plaintext[6]
    piid = plaintext[7] | (plaintext[8] << 8)
    type_len = int.from_bytes(plaintext[9:11], "little")
    length = type_len & 0x0FFF
    if length <= 0 or len(plaintext) < 11 + length:
        return PropertyPush(siid, piid, None, b"", plaintext)
    raw = plaintext[11 : 11 + length]
    value = int.from_bytes(raw, "little")
    return PropertyPush(siid, piid, value, raw, plaintext)


def parse_port_push(
    plaintext: bytes,
    *,
    pdo_data: Mapping[str, object] | None = None,
    protocol_switches: Mapping[str, Mapping[str, bool]] | None = None,
    hw_protocol: int | None = None,
) -> PortReading | None:
    if len(plaintext) < 15 or plaintext[4] != 0x04:
        return None
    piid = plaintext[7] | (plaintext[8] << 8)
    if piid not in PORT_NAMES:
        return None
    status_raw, code_raw, current_raw, voltage_raw = plaintext[-4:]
    return _make_port_reading(
        piid,
        status_raw,
        code_raw,
        current_raw,
        voltage_raw,
        plaintext.hex(),
        pdo_data=pdo_data,
        protocol_switches=protocol_switches,
        hw_protocol=hw_protocol,
    )


def parse_port_value(
    piid: int,
    value: int,
    plaintext: bytes | None = None,
    *,
    pdo_data: Mapping[str, object] | None = None,
    protocol_switches: Mapping[str, Mapping[str, bool]] | None = None,
    hw_protocol: int | None = None,
) -> PortReading | None:
    if piid not in PORT_NAMES or value < 0 or value > 0xFFFFFFFF:
        return None
    status_raw, code_raw, current_raw, voltage_raw = value.to_bytes(4, "little")
    return _make_port_reading(
        piid,
        status_raw,
        code_raw,
        current_raw,
        voltage_raw,
        plaintext.hex() if plaintext else "",
        pdo_data=pdo_data,
        protocol_switches=protocol_switches,
        hw_protocol=hw_protocol,
    )


def _make_port_reading(
    piid: int,
    status_raw: int,
    code_raw: int,
    current_raw: int,
    voltage_raw: int,
    plaintext_hex: str,
    *,
    pdo_data: Mapping[str, object] | None,
    protocol_switches: Mapping[str, Mapping[str, bool]] | None,
    hw_protocol: int | None,
) -> PortReading:
    voltage = voltage_raw / 10.0
    current = current_raw / 10.0
    power = round(voltage * current, 2)
    active = bool(status_raw) or voltage > 0 or current > 0
    shared = piid in (3, 4) and status_raw == 0x11
    shared_group = "c3+a" if shared else None

    if not active:
        proto_num = 0
        proto = "idle"
        source = "no-load"
    elif hw_protocol and hw_protocol > 0:
        proto_num = hw_protocol
        proto = protocol_name(proto_num)
        source = "hardware"
    else:
        proto_num = estimate_protocol_number(
            piid,
            voltage,
            code_raw,
            pdo_data=pdo_data,
            protocol_switches=protocol_switches,
        )
        proto = protocol_name(proto_num) if proto_num else f"code-0x{code_raw:02X}"
        source = "heuristic"

    name = "C3+A" if shared else PORT_NAMES[piid]
    return PortReading(
        piid=piid,
        name=name,
        voltage=voltage,
        current=current,
        power=power,
        active=active,
        status_raw=status_raw,
        code_raw=code_raw,
        protocol_hint=proto,
        plaintext_hex=plaintext_hex,
        protocol_number=proto_num if proto_num else None,
        protocol_source=source,
        shared=shared,
        shared_group=shared_group,
    )


def estimate_protocol_number(
    piid: int,
    voltage: float,
    code: int,
    *,
    pdo_data: Mapping[str, object] | None = None,
    protocol_switches: Mapping[str, Mapping[str, bool]] | None = None,
) -> int:
    """Port the upstream Mi Home-aligned fallback detector.

    Hardware protocol numbers from PIID 17/18 should always take precedence.
    This function is used only when those values are not available.
    """
    if piid in (1, 2):
        port_key = PORT_KEYS[piid]
        switches = (protocol_switches or {}).get(port_key, {})
        if switches and not switches.get("pd", True) and voltage > 0:
            return 1
        if code == 0x08:
            return 8
        if code == 0x70:
            return 7 if _voltage_match(voltage, 0.05) else 3
        if code in {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0A, 0x0B, 0x30}:
            pdo_kind = (pdo_data or {}).get("kind")
            pps_enabled = switches.get("pps", True)
            if pdo_kind == "PD PPS":
                if not pps_enabled:
                    return 7
                return 7 if _voltage_match(voltage, 0.20) else 8
            if pdo_kind == "PD Fixed":
                if pps_enabled and voltage < 12.0:
                    return _estimate_pd_subtype(voltage)
                return 7
            if not pps_enabled and voltage > 0:
                return 7
            return _estimate_pd_subtype(voltage)
        if _voltage_match(voltage, 0.15):
            return 7
        if PPS_VOLTAGE_RANGE[0] <= voltage <= PPS_VOLTAGE_RANGE[1]:
            return 8
        return 0

    if piid == 3:
        if code == 0x70:
            kind = (pdo_data or {}).get("kind")
            if kind in ("PD Fixed", "PD PPS"):
                return 7
            return 3
        if voltage >= 15.0:
            return 7
        if voltage >= 8.5:
            return 3
        if voltage <= 5.5:
            return 1
        return 3 if voltage > 6.0 else 1

    if piid == 4:
        if code == 0x70 or voltage > 5.5:
            return 3
        if voltage > 0:
            return 1
    return 0


def _estimate_pd_subtype(voltage: float) -> int:
    distance = min(abs(voltage - ref) for ref in PD_FIXED_VOLTAGES)
    if voltage < 12.0:
        return 7 if distance <= 0.05 else 8
    if distance <= 0.30:
        return 7
    return 8 if PPS_VOLTAGE_RANGE[0] <= voltage <= PPS_VOLTAGE_RANGE[1] else 7


def _voltage_match(voltage: float, tolerance: float) -> bool:
    return min(abs(voltage - ref) for ref in PD_FIXED_VOLTAGES) <= tolerance
