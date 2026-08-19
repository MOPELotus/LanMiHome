from __future__ import annotations

from dataclasses import dataclass, field

from .miot import MiotCommandClient
from .protocol import PortReading
from .session import CuktechSession
from .state import ChargerState


@dataclass(slots=True)
class PortPollResult:
    reading: PortReading | None = None
    response_plaintext: bytes | None = None
    deferred_plaintexts: list[bytes] = field(default_factory=list)


def _state_for(session: CuktechSession) -> ChargerState:
    state = getattr(session, "_lanmihome_state", None)
    if state is None:
        state = ChargerState()
        setattr(session, "_lanmihome_state", state)
    return state


def _miot_for(session: CuktechSession) -> MiotCommandClient:
    client = getattr(session, "_lanmihome_miot", None)
    if client is None:
        client = MiotCommandClient(session)
        setattr(session, "_lanmihome_miot", client)
    return client


async def poll_port(session: CuktechSession, piid: int, timeout: float = 8.0) -> PortPollResult:
    if piid not in range(1, 5):
        raise ValueError("port PIID must be 1..4")
    result = await _miot_for(session).get(2, piid, timeout=timeout)
    state = _state_for(session)
    for plaintext in result.deferred_plaintexts:
        event = state.decode_port_push(plaintext)
        if event is None:
            from .protocol import parse_property_push
            prop = parse_property_push(plaintext)
            if prop and prop.value is not None:
                state.apply_setting(prop.piid, prop.value)
    reading = None
    if result.value is not None:
        reading = state.decode_port_value(piid, result.value, result.raw)
    return PortPollResult(
        reading=reading,
        response_plaintext=result.raw,
        deferred_plaintexts=list(result.deferred_plaintexts),
    )


async def poll_all_ports(session: CuktechSession) -> list[PortPollResult]:
    return [await poll_port(session, piid) for piid in range(1, 5)]
