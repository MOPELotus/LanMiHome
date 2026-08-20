from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .controller import CuktechController, DeviceEvent
from .protocol import PortReading
from .session import AuthenticationError, DeviceResolver
from .state import ChargerState

_LOG = logging.getLogger("lanmihome.cuktech")

ReadingCallback = Callable[[str, PortReading], Awaitable[None] | None]
StateCallback = Callable[[str, str], Awaitable[None] | None]
SettingsCallback = Callable[[str, ChargerState], Awaitable[None] | None]
_T = TypeVar("_T")
ControllerOperation = Callable[[CuktechController], Awaitable[_T]]


@dataclass(slots=True)
class ChargerConfig:
    name: str
    address: str
    token: bytes


@dataclass(slots=True)
class _QueuedOperation:
    callback: ControllerOperation
    future: asyncio.Future


class ChargerManager:
    """Multi-device AD1204 manager.

    Each charger owns a fully isolated MiOT session, crypto state and command
    channel. Connection/service discovery/authentication remains serialized as a
    WinRT reliability workaround; authenticated steady-state sessions run in
    parallel. Device telemetry is event-driven with active GET refresh for stable
    ports, full setting/protocol state, keepalive and per-device reconnect.

    ``execute`` queues controller operations onto the owning worker. This keeps
    the command response channel single-consumer even when a router HTTP thread
    requests a setting change while live Notify monitoring is active.
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
        device_resolver: DeviceResolver | None = None,
    ):
        self.chargers = chargers
        self.on_reading = on_reading
        self.on_state = on_state
        self.on_settings = on_settings
        self.raw = raw
        self.device_resolver = device_resolver
        self._stop = asyncio.Event()
        self._setup_lock = asyncio.Lock()
        self.controllers: dict[str, CuktechController] = {}
        self.sessions = {}
        self._operations: dict[str, asyncio.Queue[_QueuedOperation]] = {
            cfg.name: asyncio.Queue() for cfg in chargers
        }

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
        for queue in self._operations.values():
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not item.future.done():
                    item.future.set_exception(ConnectionError("charger manager stopped"))

    async def execute(
        self,
        name: str,
        callback: Callable[[CuktechController], Awaitable[_T]],
        *,
        timeout: float = 20.0,
    ) -> _T:
        """Run one controller operation inside that charger's worker.

        The worker is the only consumer of the MiOT command/Notify receive queue,
        so HTTP controls cannot steal or lose command replies while monitoring.
        """
        if self._stop.is_set():
            raise ConnectionError("charger manager is stopping")
        try:
            queue = self._operations[name]
        except KeyError as exc:
            raise KeyError(f"unknown charger: {name}") from exc
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_T] = loop.create_future()
        await queue.put(_QueuedOperation(callback=callback, future=future))
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise TimeoutError(f"{name}: charger operation timed out after {timeout:.1f}s") from None

    async def _worker(self, cfg: ChargerConfig) -> None:
        delay = 2.0
        while not self._stop.is_set():
            controller = CuktechController(
                cfg.name,
                cfg.address,
                cfg.token,
                device_resolver=self.device_resolver,
            )
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
                    # Execute at most one external control between receive windows.
                    # Controller methods themselves serialize encrypted GET/SET.
                    if await self._run_one_operation(cfg.name, controller, last_seen):
                        last_settings_refresh = time.monotonic()
                        last_keepalive = time.monotonic()
                        continue

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

    async def _run_one_operation(
        self,
        name: str,
        controller: CuktechController,
        last_seen: dict[int, float],
    ) -> bool:
        queue = self._operations[name]
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        if item.future.cancelled():
            return True
        if not controller.connected or not controller.authenticated:
            if not item.future.done():
                item.future.set_exception(ConnectionError(f"{name}: charger is not authenticated"))
            return True
        try:
            result = await item.callback(controller)
            await self._emit_pending_events(name, controller, last_seen)
            await self._emit_settings(name, controller.state)
            if not item.future.done():
                item.future.set_result(result)
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc)
        return True

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
