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


async def _acquire_gate() -> str:
    result = await asyncio.to_thread(
        _post_json,
        "/api/v1/system/bluetooth/gatt/acquire",
        {"owner": "w96d", "ttl_seconds": 25},
        35.0,
    )
    token = str(result.get("token") or "")
    if not token:
        raise RuntimeError("BLE coordinator did not return a lease token")
    return token


async def _release_gate(token: str) -> None:
    await asyncio.to_thread(
        _post_json,
        "/api/v1/system/bluetooth/gatt/release",
        {"token": token},
        8.0,
    )


async def _coordinated_connect(self) -> None:
    if not self.enabled or not self.policy.scheduled() or self.pause_store.get():
        return

    token = await _acquire_gate()
    try:
        # If no fixed address is configured, discovery is allowed only inside
        # the connect window while the main advertisement scanner is paused.
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
        try:
            await _release_gate(token)
        except Exception as exc:
            # The main service has a lease watchdog, so a failed release cannot
            # leave advertisement scanning disabled indefinitely.
            LOG.warning(
                "failed to release BLE connect lease: %s: %s",
                type(exc).__name__,
                exc,
            )


def _snapshot_get(self) -> None:
    path = self.path.split("?", 1)[0].rstrip("/") or "/"
    if path == "/api/v1/w96d":
        self._json(core.HTTPStatus.OK, self.manager.snapshot())
        return
    _ORIG_GET(self)


core.W96DManager._connect = _coordinated_connect
core.ApiHandler.do_GET = _snapshot_get


if __name__ == "__main__":
    raise SystemExit(core.main())
