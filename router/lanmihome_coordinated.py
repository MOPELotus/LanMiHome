#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

import lanmihome as core
from lanmihome_cuktech.session import CuktechSession

LOG = logging.getLogger("lanmihome.btcoord")
_ACTIVE_BLE_RUNTIME = None

_ORIG_BLE_INIT = core.BleRuntime.__init__
_ORIG_BLE_THREAD_MAIN = core.BleRuntime._thread_main
_ORIG_ROUTE = core.Handler._route
_ORIG_CUKTECH_CONNECT = CuktechSession.connect


def _patched_ble_init(self, *args, **kwargs):
    _ORIG_BLE_INIT(self, *args, **kwargs)
    self._gatt_gate = None
    self._gatt_external_token = None
    self._gatt_external_owner = None
    self._gatt_external_restart_scan = False
    self._gatt_external_watchdog = None


def _patched_ble_thread_main(self):
    global _ACTIVE_BLE_RUNTIME
    _ACTIVE_BLE_RUNTIME = self
    try:
        return _ORIG_BLE_THREAD_MAIN(self)
    finally:
        if _ACTIVE_BLE_RUNTIME is self:
            _ACTIVE_BLE_RUNTIME = None


def _gate(runtime):
    if runtime._gatt_gate is None:
        runtime._gatt_gate = asyncio.Lock()
    return runtime._gatt_gate


def _scanner_running(runtime) -> bool:
    scanner = runtime.scanner
    if scanner is None:
        return False
    state = getattr(scanner, "is_scanning", None)
    return bool(state) if state is not None else True


async def _pause_scan(runtime, owner: str) -> bool:
    scanner = runtime.scanner
    restart = _scanner_running(runtime)
    if restart and scanner is not None:
        await scanner.stop()
        LOG.info("BLE advertisement scan paused for GATT connect: %s", owner)
    return restart


async def _resume_scan(runtime, restart: bool, owner: str) -> None:
    scanner = runtime.scanner
    if restart and scanner is not None:
        await scanner.start()
        LOG.info("BLE advertisement scan resumed after GATT connect: %s", owner)


@asynccontextmanager
async def _internal_connect_window(runtime, owner: str):
    lock = _gate(runtime)
    async with lock:
        restart = await _pause_scan(runtime, owner)
        try:
            yield
        finally:
            await _resume_scan(runtime, restart, owner)


async def _patched_cuktech_connect(self):
    runtime = _ACTIVE_BLE_RUNTIME
    if runtime is None or runtime.loop is None or not runtime.loop.is_running():
        return await _ORIG_CUKTECH_CONNECT(self)

    # Router deployments have fixed charger MAC addresses. Avoid making an
    # active shared scanner a prerequisite for resolving the BLEDevice.
    resolver = self.device_resolver
    self.device_resolver = None
    try:
        async with _internal_connect_window(runtime, f"cuktech:{self.name}"):
            return await _ORIG_CUKTECH_CONNECT(self)
    finally:
        self.device_resolver = resolver


async def _release_external(runtime, token: str, *, expired: bool = False):
    if not token or token != runtime._gatt_external_token:
        raise ValueError("invalid or expired GATT lease token")

    owner = runtime._gatt_external_owner or "external"
    restart = bool(runtime._gatt_external_restart_scan)
    watchdog = runtime._gatt_external_watchdog
    current = asyncio.current_task()

    runtime._gatt_external_token = None
    runtime._gatt_external_owner = None
    runtime._gatt_external_restart_scan = False
    runtime._gatt_external_watchdog = None

    if watchdog is not None and watchdog is not current:
        watchdog.cancel()

    try:
        await _resume_scan(runtime, restart, owner)
    finally:
        lock = _gate(runtime)
        if lock.locked():
            lock.release()

    LOG.info(
        "released external GATT connect lease%s: %s",
        " (expired)" if expired else "",
        owner,
    )
    return {"ok": True, "owner": owner, "expired": expired}


async def _external_watchdog(runtime, token: str, ttl: float):
    try:
        await asyncio.sleep(ttl)
        if runtime._gatt_external_token == token:
            await _release_external(runtime, token, expired=True)
    except asyncio.CancelledError:
        pass
    except Exception:
        LOG.exception("external GATT lease watchdog failed")


async def _acquire_external(runtime, owner: str, ttl: float):
    lock = _gate(runtime)
    await lock.acquire()
    restart = False
    try:
        restart = await _pause_scan(runtime, owner)
        token = secrets.token_hex(16)
        runtime._gatt_external_token = token
        runtime._gatt_external_owner = owner
        runtime._gatt_external_restart_scan = restart
        runtime._gatt_external_watchdog = asyncio.create_task(
            _external_watchdog(runtime, token, ttl),
            name=f"gatt-lease:{owner}",
        )
        LOG.info("acquired external GATT connect lease: %s", owner)
        return {"ok": True, "token": token, "owner": owner, "ttl_seconds": ttl}
    except Exception:
        if restart:
            try:
                await _resume_scan(runtime, True, owner)
            except Exception:
                LOG.exception("failed to resume scanner after lease acquisition error")
        if lock.locked():
            lock.release()
        raise


def _run_on_ble_loop(runtime, coro, timeout: float):
    loop = runtime.loop
    if loop is None or not loop.is_running():
        raise ConnectionError("BLE runtime is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _acquire_external_sync(self, owner: str, ttl: float = 25.0):
    owner = str(owner or "external").strip()[:64] or "external"
    ttl = max(5.0, min(float(ttl), 45.0))
    return _run_on_ble_loop(self, _acquire_external(self, owner, ttl), timeout=50.0)


def _release_external_sync(self, token: str):
    return _run_on_ble_loop(self, _release_external(self, str(token)), timeout=8.0)


def _gatt_status(self):
    return {
        "busy": bool(self._gatt_gate and self._gatt_gate.locked()),
        "external_owner": self._gatt_external_owner,
        "external_lease": self._gatt_external_token is not None,
        "scanner_running": _scanner_running(self),
    }


def _localhost_only(handler) -> None:
    host = str(handler.client_address[0])
    if host not in {"127.0.0.1", "::1"}:
        raise core.ApiError(core.HTTPStatus.FORBIDDEN, "localhost only")


def _patched_route(self, method: str, path: str, body: dict):
    if path == "/api/v1/system/bluetooth/gatt":
        _localhost_only(self)
        if method != "GET":
            raise core.ApiError(core.HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        return self.app.ble.gatt_status()

    if path == "/api/v1/system/bluetooth/gatt/acquire":
        _localhost_only(self)
        if method != "POST":
            raise core.ApiError(core.HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        return self.app.ble.gatt_acquire_external(
            str(body.get("owner") or "external"),
            float(body.get("ttl_seconds", 25.0)),
        )

    if path == "/api/v1/system/bluetooth/gatt/release":
        _localhost_only(self)
        if method != "POST":
            raise core.ApiError(core.HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        token = str(body.get("token") or "")
        if not token:
            raise ValueError("token is required")
        return self.app.ble.gatt_release_external(token)

    return _ORIG_ROUTE(self, method, path, body)


core.BleRuntime.__init__ = _patched_ble_init
core.BleRuntime._thread_main = _patched_ble_thread_main
core.BleRuntime.gatt_acquire_external = _acquire_external_sync
core.BleRuntime.gatt_release_external = _release_external_sync
core.BleRuntime.gatt_status = _gatt_status
core.Handler._route = _patched_route
CuktechSession.connect = _patched_cuktech_connect


if __name__ == "__main__":
    raise SystemExit(core.main())
