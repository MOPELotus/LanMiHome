from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

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

    The steady-state GATT sessions run concurrently, but connection/service discovery/
    authentication is serialized.  Bleak may perform an implicit discovery when a
    client is constructed from an address, and overlapping WinRT discovery/service
    setup proved racy with multiple AD1204 chargers.  Keeping only the setup phase
    behind a lock preserves multi-device operation while avoiding that race.
    """

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
            try:
                # WinRT/Bleak can race when two address-based BleakClient.connect()
                # calls both trigger discovery/service setup.  Serialize only this
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

                while not self._stop.is_set() and session.connected:
                    plaintext = await session.next_plaintext(timeout=5.0)
                    if plaintext is None:
                        continue
                    if self.raw:
                        _LOG.info("[%s] RX %s", cfg.name, plaintext.hex())
                    reading = parse_port_push(plaintext)
                    if reading is not None:
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
                await session.disconnect()

            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

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
