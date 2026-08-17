from __future__ import annotations

from dataclasses import dataclass

UUID_FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"
CHAR_DEVICE_INFO = "0000001c-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_CTRL = "00000010-0000-1000-8000-00805f9b34fb"
CHAR_AUTH_DATA = "00000019-0000-1000-8000-00805f9b34fb"
CHAR_CMD_SEND = "0000001a-0000-1000-8000-00805f9b34fb"
CHAR_CMD_RECV = "0000001b-0000-1000-8000-00805f9b34fb"
CHAR_FW_VERSION = "00000004-0000-1000-8000-00805f9b34fb"

PORT_NAMES = {1: "C1", 2: "C2", 3: "C3", 4: "A"}


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


def mac_to_miot_bytes(address: str) -> bytes:
    parts = address.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"invalid BLE MAC address: {address!r}")
    return bytes(int(part, 16) for part in reversed(parts))


def parse_port_push(plaintext: bytes) -> PortReading | None:
    """Decode an AD1204 real-time port push.

    Push frames use MiOT result opcode 0x04.  The final four plaintext bytes
    are status, protocol-code, current and voltage.
    """
    if len(plaintext) < 12 or plaintext[4] != 0x04:
        return None
    piid = plaintext[7]
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
    )


def parse_port_value(
    piid: int,
    value: int,
    plaintext: bytes | None = None,
) -> PortReading | None:
    """Decode a PIID 1..4 active-GET value.

    AD1204 returns each port property as a little-endian UINT32 whose bytes are
    [status, protocol-code, current_raw, voltage_raw].  This is the same payload
    carried at the end of change-triggered push frames, so active polling and
    Notify updates share one PortReading representation.
    """
    if piid not in PORT_NAMES or value < 0 or value > 0xFFFFFFFF:
        return None
    raw = value.to_bytes(4, "little")
    status_raw, code_raw, current_raw, voltage_raw = raw
    return _make_port_reading(
        piid,
        status_raw,
        code_raw,
        current_raw,
        voltage_raw,
        plaintext.hex() if plaintext else "",
    )


def _make_port_reading(
    piid: int,
    status_raw: int,
    code_raw: int,
    current_raw: int,
    voltage_raw: int,
    plaintext_hex: str,
) -> PortReading:
    voltage = voltage_raw / 10.0
    current = current_raw / 10.0
    power = round(voltage * current, 2)
    active = bool(status_raw) or voltage > 0 or current > 0

    return PortReading(
        piid=piid,
        name=PORT_NAMES[piid],
        voltage=voltage,
        current=current,
        power=power,
        active=active,
        status_raw=status_raw,
        code_raw=code_raw,
        protocol_hint=_protocol_hint(piid, voltage, code_raw, active),
        plaintext_hex=plaintext_hex,
    )


def _protocol_hint(piid: int, voltage: float, code: int, active: bool) -> str:
    if not active:
        return "idle"

    if piid in (1, 2):
        if code == 0x08:
            return "PPS"
        if code == 0x70:
            return "PD" if _near_fixed_pd(voltage, 0.15) else "QC/PD"
        if code in {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0A, 0x0B, 0x30}:
            return "PD" if _near_fixed_pd(voltage, 0.20) else "PPS"
        return f"code-0x{code:02X}"

    if piid == 3:
        if voltage >= 15.0:
            return "PD"
        if code == 0x70 or voltage >= 8.5:
            return "QC"
        if voltage <= 5.5:
            return "5V"
        return f"code-0x{code:02X}"

    if code == 0x70 or voltage > 5.5:
        return "QC"
    return "5V" if voltage > 0 else f"code-0x{code:02X}"


def _near_fixed_pd(voltage: float, tolerance: float) -> bool:
    return min(abs(voltage - ref) for ref in (5.0, 9.0, 12.0, 15.0, 20.0)) <= tolerance
