from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from .protocol import CHAR_CMD_RECV, CHAR_CMD_SEND, PortReading, parse_port_push, parse_port_value
from .session import CuktechSession


@dataclass(slots=True)
class PortPollResult:
    reading: PortReading | None = None
    response_plaintext: bytes | None = None
    deferred_plaintexts: list[bytes] = field(default_factory=list)


def _command_lock(session: CuktechSession) -> asyncio.Lock:
    lock = getattr(session, "_lanmihome_command_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(session, "_lanmihome_command_lock", lock)
    return lock


def _next_seq(session: CuktechSession) -> int:
    seq = getattr(session, "_lanmihome_miot_seq", 1)
    setattr(session, "_lanmihome_miot_seq", (seq + 1) & 0xFF)
    return seq


def _build_get(seq: int, siid: int, piid: int) -> bytes:
    type_len = (1 << 12) | 1
    value = b"\x00"
    total_len = 11 + len(value)
    return bytes([
        total_len & 0xFF, 0x20, seq & 0xFF, 0x00,
        0x02, 0x01, siid & 0xFF,
        piid & 0xFF, (piid >> 8) & 0xFF,
        type_len & 0xFF, (type_len >> 8) & 0xFF,
    ]) + value


def _encrypt(session: CuktechSession, plaintext: bytes) -> bytes:
    keys = session._session_keys
    if not keys:
        raise RuntimeError("MiOT session keys are not available")
    it = session._send_it
    it_bytes = struct.pack("<I", it)
    nonce = keys["app_iv"] + (b"\x00" * 4) + it_bytes
    ciphertext = AESCCM(keys["app_key"], tag_length=4).encrypt(nonce, plaintext, None)
    session._send_it += 1
    return it_bytes[:2] + ciphertext


async def _send_encrypted(session: CuktechSession, plaintext: bytes) -> bool:
    client = session.client
    if not client or not client.is_connected:
        return False
    session._drain_queue("cmd_send")
    encrypted = _encrypt(session, plaintext)
    await client.write_gatt_char(CHAR_CMD_SEND, b"\x00\x00\x00\x00\x01\x00", response=False)
    if await session._wait("cmd_send", 3.0) != b"\x00\x00\x01\x01":
        return False
    await client.write_gatt_char(CHAR_CMD_SEND, b"\x01\x00" + encrypted, response=False)
    return await session._wait("cmd_send", 3.0) == b"\x00\x00\x01\x00"


async def _decode_inline(session: CuktechSession, data: bytes) -> bytes | None:
    if len(data) < 4 or data[2] != 0x02:
        return None
    await session.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x03\x00", response=False)
    return session.decrypt(data[4:])


async def _consume_multiframe(session: CuktechSession, data: bytes) -> None:
    if len(data) < 6:
        return
    count = min(data[4] | (data[5] << 8), 100)
    await session.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x01\x01", response=False)
    for _ in range(count):
        await session._wait("cmd_recv", 3.0)
    await session.client.write_gatt_char(CHAR_CMD_RECV, b"\x00\x00\x01\x00", response=False)


def _extract_get_value(plaintext: bytes) -> int | None:
    if len(plaintext) < 14:
        return None
    value_len = plaintext[11]
    if value_len >= 4 and len(plaintext) >= 17:
        return int.from_bytes(plaintext[13:17], "little")
    return plaintext[13] if len(plaintext) > 13 else None


async def poll_port(session: CuktechSession, piid: int, timeout: float = 8.0) -> PortPollResult:
    """Actively GET one AD1204 port while preserving interleaved Notify pushes."""
    if piid not in range(1, 5):
        raise ValueError("port PIID must be 1..4")
    if not session.authenticated:
        raise RuntimeError("charger is not authenticated")

    result = PortPollResult()
    async with _command_lock(session):
        seq = _next_seq(session)
        if not await _send_encrypted(session, _build_get(seq, 2, piid)):
            return result

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return result
            data = await session._wait("cmd_recv", min(remaining, 3.0))
            if not data:
                continue

            if len(data) >= 4 and data[2] == 0x02:
                plaintext = await _decode_inline(session, data)
                if not plaintext or len(plaintext) < 8:
                    continue
                opcode = plaintext[4]
                resp_siid = plaintext[6] if len(plaintext) > 6 else -1
                resp_piid = plaintext[7] if len(plaintext) > 7 else -1
                if opcode == 0x03 and resp_siid == 2 and resp_piid == piid:
                    value = _extract_get_value(plaintext)
                    result.response_plaintext = plaintext
                    if value is not None:
                        result.reading = parse_port_value(piid, value, plaintext)
                    return result
                if parse_port_push(plaintext) is not None:
                    result.deferred_plaintexts.append(plaintext)
                continue

            if len(data) >= 6 and data[2] == 0x00:
                await _consume_multiframe(session, data)


async def poll_all_ports(session: CuktechSession) -> list[PortPollResult]:
    results: list[PortPollResult] = []
    for piid in range(1, 5):
        results.append(await poll_port(session, piid))
    return results
