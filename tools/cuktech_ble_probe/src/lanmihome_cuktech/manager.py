from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .active_poll import PortPollResult, poll_port
from .protocol import PortReading, parse_port_push
from .session import AuthenticationError, CuktechSession

_LOG = logging.getLogger("lanmihome.cuktech")

ReadingCallback = Callable[[str, PortReading], Awaitable[None] | None]
StateCallback = Callable[[str, str], Awaitable[None] | None]


@dataclass(slots=True)
class ChargerConfig:
    name: str
    address: str
    token: bytes


class ChargerManager:
    """Run one independent reconnecting task per charger.

    Steady-state GATT sessions run concurrently, but connection/service discovery/
    authentication is serialized.  Port telemetry is primarily change-triggered on
    AD1204, so each session also performs an initial GET of C1/C2/C3/A and later
    actively refreshes ports that have not produced a Notify for a while.
    """

    STALE_AFTER = 15.0
    POLL_RETRY_AFTER = 15.0
    LOOP_TIMEOUT = 2.0

    def __init__(
        self,
        chargers: list[ChargerConfig],
        on_reading: ReadingCallback | None = None,
        on_state: StateCallback | None = None,
        raw: bool = False,
    ):
        self.chargers = chargers
        self.on_reading = on_reading
        self.on_state = on_state
        self.raw = raw
        self._stop = asyncio.Event()
        self._setup_lock = asyncio.Lock()
        self.sessions: dict[str, CuktechSession] = {}

    async def run(self) -> None:
        if not self.chargers:
            raise ValueError("no chargers configured")
        async with asyncio.TaskGroup() as tg:
            for cfg in self.chargers:
                tg.create_task(self._worker(cfg), name=f"cuktech:{cfg.name}")

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.gather(
            *(session.disconnect() for session in self.sessions.values()),
            return_exceptions=True,
        )

    async def _worker(self, cfg: ChargerConfig) -> None:
        delay = 2.0
        while not self._stop.is_set():
            session = CuktechSession(cfg.name, cfg.address, cfg.token)
            self.sessions[cfg.name] = session
            started = time.monotonic()
            last_seen = {piid: started for piid in range(1, 5)}
            last_poll = {piid: 0.0 for piid in range(1, 5)}
            try:
                # WinRT/Bleak can race when two address-based BleakClient.connect()
                # calls both trigger discovery/service setup. Serialize only this
                # phase; once authenticated, all sessions stream concurrently.
                async with self._setup_lock:
                    if self._stop.is_set():
                        break
                    await self._emit_state(cfg.name, "connecting")
                    await session.connect()
                    info = await session.read_device_info()
                    await self._emit_state(
                        cfg.name,
                        f"connected model={info.model or '?'} fw={info.firmware or '?'}",
                    )
                    await session.authenticate()
                    await self._emit_state(cfg.name, "authenticated")

                delay = 2.0

                # AD1204 does not continuously repeat stable port readings. Query
                # all ports once so a process started halfway through a stable A/C
                # load still obtains a complete initial snapshot.
                for piid in range(1, 5):
                    if self._stop.is_set() or not session.connected:
                        break
                    result = await poll_port(session, piid, timeout=5.0)
                    last_poll[piid] = time.monotonic()
                    await self._handle_poll_result(cfg.name, result, last_seen)

                while not self._stop.is_set() and session.connected:
                    plaintext = await session.next_plaintext(timeout=self.LOOP_TIMEOUT)
                    if plaintext is not None:
                        await self._handle_plaintext(cfg.name, plaintext, last_seen)

                    # Refresh only one stale port per loop. This gives event-driven
                    # Notify traffic priority and avoids a burst of four GETs while
                    # still guaranteeing stable ports are periodically re-sampled.
                    now = time.monotonic()
                    candidates = [
                        piid
                        for piid in range(1, 5)
                        if now - last_seen[piid] >= self.STALE_AFTER
                        and now - last_poll[piid] >= self.POLL_RETRY_AFTER
                    ]
                    if candidates:
                        piid = max(candidates, key=lambda p: now - last_seen[p])
                        result = await poll_port(session, piid, timeout=5.0)
                        last_poll[piid] = time.monotonic()
                        await self._handle_poll_result(cfg.name, result, last_seen)

                if not self._stop.is_set():
                    raise ConnectionError("BLE link closed")
            except AuthenticationError as exc:
                await self._emit_state(cfg.name, f"auth-error: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_state(cfg.name, f"offline: {type(exc).__name__}: {exc}")
            finally:
                await session.disconnect()

            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def _handle_plaintext(
        self,
        name: str,
        plaintext: bytes,
        last_seen: dict[int, float],
    ) -> None:
        if self.raw:
            _LOG.info("[%s] RX %s", name, plaintext.hex())
        reading = parse_port_push(plaintext)
        if reading is None:
            return
        last_seen[reading.piid] = time.monotonic()
        await self._emit_reading(name, reading)

    async def _handle_poll_result(
        self,
        name: str,
        result: PortPollResult,
        last_seen: dict[int, float],
    ) -> None:
        # A live Notify can interleave with an active GET. active_poll ACKs it and
        # hands the decrypted plaintext back here so the event is never swallowed.
        for plaintext in result.deferred_plaintexts:
            await self._handle_plaintext(name, plaintext, last_seen)

        if result.response_plaintext is not None and self.raw:
            label = result.reading.name if result.reading else "?"
            _LOG.info("[%s] GET %s %s", name, label, result.response_plaintext.hex())

        if result.reading is not None:
            last_seen[result.reading.piid] = time.monotonic()
            await self._emit_reading(name, result.reading)

    async def _emit_reading(self, name: str, reading: PortReading) -> None:
        if self.on_reading is None:
            return
        result = self.on_reading(name, reading)
        if asyncio.iscoroutine(result):
            await result

    async def _emit_state(self, name: str, state: str) -> None:
        if self.on_state is None:
            _LOG.info("[%s] %s", name, state)
            return
        result = self.on_state(name, state)
        if asyncio.iscoroutine(result):
            await result
