#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
from urllib import error as urlerror
from urllib import request as urlrequest

import w96d_sidecar as core

LOG = logging.getLogger("lanmihome.w96d")
COORDINATOR = "http://127.0.0.1:8765"
_ORIG_GET = core.ApiHandler.do_GET


def _post_json(path: str, payload: dict, timeout: float = 12.0) -> dict:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urlrequest.Request(
        COORDINATOR + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"BLE coordinator HTTP {exc.code}: {detail}") from None
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"BLE coordinator unavailable: {exc}") from None

    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise RuntimeError("BLE coordinator returned invalid JSON") from None
    if not isinstance(value, dict):
        raise RuntimeError("BLE coordinator returned invalid response")
    return value


async def _acquire_gate(
    owner: str,
    *,
    pause_scan: bool,
    ttl: float,
    timeout: float = 55.0,
) -> str:
    result = await asyncio.to_thread(
        _post_json,
        "/api/v1/system/bluetooth/gatt/acquire",
        {
            "owner": owner,
            "ttl_seconds": ttl,
            "pause_scan": pause_scan,
        },
        timeout,
    )
    token = str(result.get("token") or "")
    if not token:
        raise RuntimeError("BLE coordinator did not return a gate token")
    return token


async def _release_gate(token: str) -> None:
    await asyncio.to_thread(
        _post_json,
        "/api/v1/system/bluetooth/gatt/release",
        {"token": token},
        8.0,
    )


async def _safe_release(token: str, owner: str) -> None:
    try:
        await _release_gate(token)
    except Exception as exc:
        # The main service has a lease watchdog, so a failed release cannot
        # leave advertisement scanning or the global GATT gate stuck forever.
        LOG.warning(
            "failed to release BLE gate for %s: %s: %s",
            owner,
            type(exc).__name__,
            exc,
        )


async def _coordinated_connect(self) -> None:
    if not self.enabled or not self.policy.scheduled() or self.pause_store.get():
        return

    token = await _acquire_gate(
        "w96d-connect",
        pause_scan=True,
        ttl=45.0,
    )
    try:
        # Fixed-address deployments normally skip discovery entirely. If the
        # address is empty, discovery is still safe here because the main
        # advertisement receiver has already been paused by the coordinator.
        address = await self._discover_address()
        client = core.BleakClient(address, timeout=self.connect_timeout)
        await client.connect()
        self._client = client
        self._update_state(
            connected=True,
            available=True,
            address=address,
            error=None,
        )
        LOG.info("connected W96D at %s", address)
    finally:
        await _safe_release(token, "w96d-connect")


async def _coordinated_refresh_locked(self):
    assert self._op_lock is not None
    async with self._op_lock:
        # Normal I/O keeps the temperature advertisement scanner running, but
        # owns the same global gate so a charger cannot begin a new GATT setup
        # in the middle of this eight-characteristic snapshot.
        token = await _acquire_gate(
            "w96d-io",
            pause_scan=False,
            ttl=20.0,
        )
        try:
            return await self._refresh_unlocked()
        finally:
            await _safe_release(token, "w96d-io")


async def _coordinated_patch(self, body):
    allowed = {"power", "speed", "natural", "turbo", "indicator"}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")

    assert self._op_lock is not None
    async with self._op_lock:
        # If disconnected, _ensure_owned() performs the connection using the
        # exclusive connect gate. Once connected, acquire the normal-I/O gate;
        # if another device setup won the race, we simply wait with W96D idle.
        await self._ensure_owned()
        token = await _acquire_gate(
            "w96d-command",
            pause_scan=False,
            ttl=20.0,
        )
        try:
            if "indicator" in body:
                await self._write(
                    core.CHAR_LIGHT,
                    bytes([1 if core._as_bool(body["indicator"], "indicator") else 0]),
                )
            if "speed" in body:
                speed = core._as_int(body["speed"], "speed", 0, 100)
                await self._write(core.CHAR_TURBO, b"\x00")
                await self._write(core.CHAR_POWER, b"\x01")
                await self._write(core.CHAR_NATURAL, b"\x00")
                await self._write(core.CHAR_SPEED, bytes([speed]), response=None)
                await asyncio.sleep(0.06)
            if "natural" in body:
                value = core._as_bool(body["natural"], "natural")
                if value:
                    await self._write(core.CHAR_TURBO, b"\x00")
                    await asyncio.sleep(0.1)
                await self._write(core.CHAR_NATURAL, bytes([1 if value else 0]))
            if "turbo" in body:
                value = core._as_bool(body["turbo"], "turbo")
                if value:
                    await self._write(core.CHAR_NATURAL, b"\x00")
                    await asyncio.sleep(0.1)
                await self._write(core.CHAR_TURBO, bytes([1 if value else 0]))
            if "power" in body:
                value = core._as_bool(body["power"], "power")
                await self._write(core.CHAR_TURBO, b"\x00")
                await self._write(core.CHAR_NATURAL, b"\x00")
                await self._write(core.CHAR_POWER, bytes([1 if value else 0]))
            await asyncio.sleep(0.08)
            return await self._refresh_unlocked()
        finally:
            await _safe_release(token, "w96d-command")


def _snapshot_get(self) -> None:
    path = self.path.split("?", 1)[0].rstrip("/") or "/"
    if path == "/api/v1/w96d":
        self._json(core.HTTPStatus.OK, self.manager.snapshot())
        return
    _ORIG_GET(self)


core.W96DManager._connect = _coordinated_connect
core.W96DManager._refresh_locked = _coordinated_refresh_locked
core.W96DManager._patch = _coordinated_patch
core.ApiHandler.do_GET = _snapshot_get


if __name__ == "__main__":
    raise SystemExit(core.main())
