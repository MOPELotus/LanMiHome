#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import secrets

import lanmihome as core
from lanmihome_cuktech.controller import CuktechController

LOG = logging.getLogger("lanmihome.btcoord")
_ACTIVE_BLE_RUNTIME = None

_ORIG_BLE_INIT = core.BleRuntime.__init__
_ORIG_BLE_THREAD_MAIN = core.BleRuntime._thread_main
_ORIG_ROUTE = core.Handler._route
_ORIG_CUKTECH_CONNECT = CuktechController.connect
_ORIG_CUKTECH_AUTHENTICATE = CuktechController.authenticate
_ORIG_CUKTECH_DISCONNECT = CuktechController.disconnect
_ORIG_CUKTECH_POLL_PORT_RESULT = CuktechController.poll_port_result


def _patched_ble_init(self, *args, **kwargs):
    _ORIG_BLE_INIT(self, *args, **kwargs)
    self._gatt_gate = None
    self._gatt_internal_owner = None
    self._gatt_external_token = None
    self._gatt_external_owner = None
    self._gatt_external_restart_scan = False
    self._gatt_external_watchdog = None
    self._gatt_scan_paused = False


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
    if getattr(runtime, "_gatt_scan_paused", False):
        return False
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
        runtime._gatt_scan_paused = True
        LOG.info("BLE advertisement scan paused for GATT setup: %s", owner)
    return restart


async def _resume_scan(runtime, restart: bool, owner: str) -> None:
    scanner = runtime.scanner
    if restart and scanner is not None:
        try:
            await scanner.start()
            LOG.info("BLE advertisement scan resumed after GATT setup: %s", owner)
        finally:
            runtime._gatt_scan_paused = False


async def _release_cuktech_setup(controller) -> None:
    if not getattr(controller, "_btcoord_setup_held", False):
        return
    runtime = getattr(controller, "_btcoord_runtime", None)
    restart = bool(getattr(controller, "_btcoord_restart_scan", False))
    owner = f"cuktech:{controller.name}"
    controller._btcoord_setup_held = False
    controller._btcoord_runtime = None
    controller._btcoord_restart_scan = False
    controller._btcoord_initial_ports = 0
    if runtime is None:
        return
    runtime._gatt_internal_owner = None
    try:
        await _resume_scan(runtime, restart, owner)
    finally:
        lock = _gate(runtime)
        if lock.locked():
            lock.release()
    LOG.info("released internal GATT setup gate: %s", owner)


async def _patched_cuktech_connect(self):
    runtime = _ACTIVE_BLE_RUNTIME
    if runtime is None or runtime.loop is None or not runtime.loop.is_running():
        return await _ORIG_CUKTECH_CONNECT(self)

    owner = f"cuktech:{self.name}"
    lock = _gate(runtime)
    await lock.acquire()
    restart = False
    try:
        runtime._gatt_internal_owner = owner
        restart = await _pause_scan(runtime, owner)
        self._btcoord_runtime = runtime
        self._btcoord_restart_scan = restart
        self._btcoord_setup_held = True
        self._btcoord_initial_ports = 4

        # Router deployments have fixed charger MAC addresses. Do not require
        # the always-on advertisement receiver to resolve a BLEDevice first.
        resolver = self.session.device_resolver
        self.session.device_resolver = None
        try:
            result = await _ORIG_CUKTECH_CONNECT(self)
        finally:
            self.session.device_resolver = resolver
        return result
    except Exception:
        if getattr(self, "_btcoord_setup_held", False):
            await _release_cuktech_setup(self)
        else:
            runtime._gatt_internal_owner = None
            if restart:
                try:
                    await _resume_scan(runtime, True, owner)
                except Exception:
                    LOG.exception("failed to resume scanner after charger connect error")
            if lock.locked():
                lock.release()
        raise


async def _patched_cuktech_authenticate(self) -> None:
    try:
        await _ORIG_CUKTECH_AUTHENTICATE(self)
    except Exception:
        await _release_cuktech_setup(self)
        raise
    # Keep the setup gate through the first settings snapshot and four initial
    # port GETs. Other GATT connections must not start while a charger is still
    # completing its post-auth initialization.


async def _patched_cuktech_poll_port_result(self, piid: int):
    try:
        result = await _ORIG_CUKTECH_POLL_PORT_RESULT(self, piid)
    except Exception:
        # The manager will disconnect after the exception; disconnect is the
        # final safety net that releases a setup gate still held here.
        raise
    if getattr(self, "_btcoord_setup_held", False) and self.authenticated:
        remaining = max(0, int(getattr(self, "_btcoord_initial_ports", 0)) - 1)
        self._btcoord_initial_ports = remaining
        if remaining == 0:
            await _release_cuktech_setup(self)
    return result


async def _patched_cuktech_disconnect(self) -> None:
    try:
        await _ORIG_CUKTECH_DISCONNECT(self)
    finally:
        await _release_cuktech_setup(self)


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
        "released external GATT gate%s: %s",
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
        LOG.exception("external GATT gate watchdog failed")


async def _acquire_external(runtime, owner: str, ttl: float, pause_scan: bool):
    lock = _gate(runtime)
    await lock.acquire()
    restart = False
    try:
        if pause_scan:
            restart = await _pause_scan(runtime, owner)
        token = secrets.token_hex(16)
        runtime._gatt_external_token = token
        runtime._gatt_external_owner = owner
        runtime._gatt_external_restart_scan = restart
        runtime._gatt_external_watchdog = asyncio.create_task(
            _external_watchdog(runtime, token, ttl),
            name=f"gatt-gate:{owner}",
        )
        LOG.info(
            "acquired external GATT gate: %s (pause_scan=%s)",
            owner,
            pause_scan,
        )
        return {
            "ok": True,
            "token": token,
            "owner": owner,
            "ttl_seconds": ttl,
            "pause_scan": pause_scan,
        }
    except Exception:
        if restart:
            try:
                await _resume_scan(runtime, True, owner)
            except Exception:
                LOG.exception("failed to resume scanner after gate acquisition error")
        if lock.locked():
            lock.release()
        raise


def _run_on_ble_loop(runtime, coro, timeout: float):
    loop = runtime.loop
    if loop is None or not loop.is_running():
        raise ConnectionError("BLE runtime is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _acquire_external_sync(
    self,
    owner: str,
    ttl: float = 25.0,
    pause_scan: bool = True,
):
    owner = str(owner or "external").strip()[:64] or "external"
    ttl = max(5.0, min(float(ttl), 45.0))
    return _run_on_ble_loop(
        self,
        _acquire_external(self, owner, ttl, bool(pause_scan)),
        timeout=55.0,
    )


def _release_external_sync(self, token: str):
    return _run_on_ble_loop(self, _release_external(self, str(token)), timeout=8.0)


def _gatt_status(self):
    return {
        "busy": bool(self._gatt_gate and self._gatt_gate.locked()),
        "internal_owner": self._gatt_internal_owner,
        "external_owner": self._gatt_external_owner,
        "external_lease": self._gatt_external_token is not None,
        "scanner_running": _scanner_running(self),
        "scanner_paused_for_setup": bool(self._gatt_scan_paused),
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
        pause_scan = core._bool(body.get("pause_scan", True), "pause_scan")
        return self.app.ble.gatt_acquire_external(
            str(body.get("owner") or "external"),
            float(body.get("ttl_seconds", 25.0)),
            pause_scan,
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
CuktechController.connect = _patched_cuktech_connect
CuktechController.authenticate = _patched_cuktech_authenticate
CuktechController.poll_port_result = _patched_cuktech_poll_port_result
CuktechController.disconnect = _patched_cuktech_disconnect


if __name__ == "__main__":
    raise SystemExit(core.main())
