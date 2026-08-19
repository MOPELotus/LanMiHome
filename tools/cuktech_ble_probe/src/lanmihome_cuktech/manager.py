from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .controller import CuktechController, DeviceEvent
from .protocol import PortReading
from .session import AuthenticationError
from .state import ChargerState

_LOG = logging.getLogger("lanmihome.cuktech")

ReadingCallback = Callable[[str, PortReading], Awaitable[None] | None]
StateCallback = Callable[[str, str], Awaitable[None] | None]
SettingsCallback = Callable[[str, ChargerState], Awaitable[None] | None]


@dataclass(slots=True)
class ChargerConfig:
    name: str
    address: str
    token: bytes


class ChargerManager:
    """Multi-device AD1204 manager.

    Each charger owns a fully isolated MiOT session, crypto state and command
    channel. Connection/service discovery/authentication remains serialized as a
    WinRT reliability workaround; authenticated steady-state sessions run in
    parallel. Device telemetry is event-driven with active GET refresh for stable
    ports, full setting/protocol state, keepalive and per-device reconnect.
    """

    ACTIVE_STALE_AFTER = 15.0
    IDLE_STALE_AFTER = 90.0
    POLL_RETRY_AFTER = 10.0
    SETTINGS_REFRESH_AFTER = 60.0
    LOOP_TIMEOUT = 2.0
    KEEPALIVE_AFTER = 10.0

    def __init__(
        self,
        chargers: list[ChargerConfig],
        on_reading: ReadingCallback | None = None,
        on_state: StateCallback | None = None,
        on_settings: SettingsCallback | None = None,
        raw: bool = False,
    ):
        self.chargers = chargers
        self.on_reading = on_reading
        self.on_state = on_state
        self.on_settings = on_settings
        self.raw = raw
        self._stop = asyncio.Event()
        self._setup_lock = asyncio.Lock()
        self.controllers: dict[str, CuktechController] = {}
        self.sessions = {}

    async def run(self) -> None:
        if not self.chargers:
            raise ValueError("no chargers configured")
        async with asyncio.TaskGroup() as tg:
            for cfg in self.chargers:
                tg.create_task(self._worker(cfg), name=f"cuktech:{cfg.name}")

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.gather(
            *(controller.disconnect() for controller in self.controllers.values()),
            return_exceptions=True,
        )

    async def _worker(self, cfg: ChargerConfig) -> None:
        delay = 2.0
        while not self._stop.is_set():
            controller = CuktechController(cfg.name, cfg.address, cfg.token)
            self.controllers[cfg.name] = controller
            self.sessions[cfg.name] = controller.session
            started = time.monotonic()
            last_seen = {piid: started for piid in range(1, 5)}
            last_poll = {piid: 0.0 for piid in range(1, 5)}
            last_settings_refresh = 0.0
            last_keepalive = time.monotonic()
            try:
                async with self._setup_lock:
                    if self._stop.is_set():
                        break
                    await self._emit_state(cfg.name, "connecting")
                    info = await controller.connect()
                    await self._emit_state(
                        cfg.name,
                        f"connected model={info.model or '?'} fw={info.firmware or '?'}",
                    )
                    await controller.authenticate()
                    await self._emit_state(cfg.name, "authenticated")

                delay = 2.0

                # Authentication captures/ACKs the charger's initial Notify burst.
                # Consume those frames in counter order, but do not emit them yet:
                # PIID 17/18 hardware protocol state has not been loaded, so exposing
                # these old frames produces brief but incorrect "guess" labels. The
                # fresh settings + port GET snapshot below is authoritative.
                for event in await controller.drain_initial_events():
                    if self.raw and event.property is not None:
                        _LOG.info("[%s] RX(init) %s", cfg.name, event.property.plaintext.hex())

                await controller.refresh_settings()
                await self._emit_pending_events(cfg.name, controller, last_seen)
                last_settings_refresh = time.monotonic()
                await self._emit_settings(cfg.name, controller.state)

                for piid in range(1, 5):
                    if self._stop.is_set() or not controller.connected:
                        break
                    reading, result = await controller.poll_port_result(piid)
                    last_poll[piid] = time.monotonic()
                    self._log_get(cfg.name, reading, result.raw)
                    await self._emit_pending_events(cfg.name, controller, last_seen)
                    if reading is not None:
                        last_seen[piid] = time.monotonic()
                        await self._emit_reading(cfg.name, reading)

                while not self._stop.is_set() and controller.connected:
                    plaintext = await controller.session.next_plaintext(timeout=self.LOOP_TIMEOUT)
                    now = time.monotonic()
                    if plaintext is not None:
                        if self.raw:
                            _LOG.info("[%s] RX %s", cfg.name, plaintext.hex())
                        event = controller.process_plaintext(plaintext)
                        if event is not None:
                            await self._emit_device_event(cfg.name, controller, event, last_seen)
                            # PIID 17/18 contain exact hardware protocol codes. Re-read
                            # the affected pair so labels change immediately.
                            if event.property and event.property.piid in (17, 18):
                                affected = (1, 2) if event.property.piid == 17 else (3, 4)
                                for piid in affected:
                                    reading, result = await controller.poll_port_result(piid)
                                    last_poll[piid] = time.monotonic()
                                    self._log_get(cfg.name, reading, result.raw)
                                    await self._emit_pending_events(cfg.name, controller, last_seen)
                                    if reading is not None:
                                        last_seen[piid] = time.monotonic()
                                        await self._emit_reading(cfg.name, reading)
                    else:
                        # Match upstream behavior: refresh slow-changing settings
                        # during an idle window so command traffic does not fight a
                        # busy stream of real-time port Notify frames.
                        if now - last_settings_refresh >= self.SETTINGS_REFRESH_AFTER:
                            await controller.refresh_settings()
                            await self._emit_pending_events(cfg.name, controller, last_seen)
                            last_settings_refresh = time.monotonic()
                            await self._emit_settings(cfg.name, controller.state)

                    if now - last_keepalive >= self.KEEPALIVE_AFTER:
                        await controller.miot.keepalive()
                        last_keepalive = now

                    candidate = self._stale_candidate(controller.state, last_seen, last_poll, now)
                    if candidate is not None:
                        reading, result = await controller.poll_port_result(candidate)
                        last_poll[candidate] = time.monotonic()
                        self._log_get(cfg.name, reading, result.raw)
                        await self._emit_pending_events(cfg.name, controller, last_seen)
                        if reading is not None:
                            last_seen[candidate] = time.monotonic()
                            await self._emit_reading(cfg.name, reading)

                if not self._stop.is_set():
                    raise ConnectionError("BLE link closed")
            except AuthenticationError as exc:
                await self._emit_state(cfg.name, f"auth-error: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._emit_state(cfg.name, f"offline: {type(exc).__name__}: {exc}")
            finally:
                await controller.disconnect()

            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def _emit_device_event(
        self,
        name: str,
        controller: CuktechController,
        event: DeviceEvent,
        last_seen: dict[int, float],
    ) -> None:
        if event.port is not None:
            last_seen[event.port.piid] = time.monotonic()
            await self._emit_reading(name, event.port)
        elif event.property is not None:
            await self._emit_settings(name, controller.state)

    async def _emit_pending_events(
        self,
        name: str,
        controller: CuktechController,
        last_seen: dict[int, float],
    ) -> None:
        for event in controller.drain_pending_events():
            # A Notify can arrive while a GET/SET temporarily owns cmd_recv. It is
            # preserved by MiotCommandClient and emitted here; raw mode must show it
            # too, otherwise the trace has unexplained state changes between GETs.
            if self.raw and event.property is not None:
                _LOG.info("[%s] RX(deferred) %s", name, event.property.plaintext.hex())
            await self._emit_device_event(name, controller, event, last_seen)

    def _stale_candidate(
        self,
        state: ChargerState,
        last_seen: dict[int, float],
        last_poll: dict[int, float],
        now: float,
    ) -> int | None:
        candidates: list[tuple[float, int]] = []
        for piid in range(1, 5):
            reading = state.ports.get(piid)
            active = bool(reading and reading.active)
            stale_after = self.ACTIVE_STALE_AFTER if active else self.IDLE_STALE_AFTER
            age = now - last_seen[piid]
            if age >= stale_after and now - last_poll[piid] >= self.POLL_RETRY_AFTER:
                candidates.append((age, piid))
        if not candidates:
            return None
        return max(candidates)[1]

    def _log_get(self, name: str, reading: PortReading | None, raw: bytes | None) -> None:
        if not self.raw or raw is None:
            return
        label = reading.name if reading is not None else "?"
        _LOG.info("[%s] GET %s %s", name, label, raw.hex())

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

    async def _emit_settings(self, name: str, state: ChargerState) -> None:
        if self.on_settings is None:
            return
        result = self.on_settings(name, state)
        if asyncio.iscoroutine(result):
            await result
