#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger("lanmihome.recovery-supervisor")


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def parse_hhmm(value: object, default: str):
    text = str(value or default)
    return text, datetime.strptime(text, "%H:%M").time()


def inside_window(now: datetime, start, end) -> bool:
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def window_key(now: datetime, start, end) -> str:
    day = now.date()
    if start > end and now.time() <= end:
        day -= timedelta(days=1)
    return day.isoformat()


def http_json(base: str, method: str, path: str, payload: dict | None = None, timeout: float = 4.0) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    value = json.loads(raw.decode("utf-8")) if raw else {}
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="LanMiHome recovery window supervisor")
    parser.add_argument("--config", default="/lotusemmc/lanmihome/config.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = Path(args.config)
    completed_key: str | None = None
    triggered_key: str | None = None
    last_wait_reason: str | None = None

    while True:
        sleep_seconds = 10
        try:
            config = load_config(config_path)
            recovery = config.get("recovery") if isinstance(config.get("recovery"), dict) else {}
            enabled = recovery.get("enabled", True) is not False
            sleep_seconds = max(2, int(recovery.get("supervisor_seconds", 10)))
            min_year = max(2000, int(recovery.get("clock_min_year", 2024)))
            start_text, start = parse_hhmm(recovery.get("start"), "05:00")
            end_text, end = parse_hhmm(recovery.get("end"), "06:30")
            now = datetime.now()

            if not enabled:
                reason = "disabled"
            elif now.year < min_year:
                reason = f"clock-not-ready:{now.year}"
            elif not inside_window(now, start, end):
                reason = "outside-window"
            else:
                reason = "inside-window"

            if reason != last_wait_reason:
                LOG.info("state=%s window=%s-%s now=%s", reason, start_text, end_text, now.isoformat(timespec="seconds"))
                last_wait_reason = reason

            if reason != "inside-window":
                time.sleep(sleep_seconds)
                continue

            key = window_key(now, start, end)
            if completed_key == key:
                time.sleep(sleep_seconds)
                continue

            listen = str(config.get("listen") or config.get("bind") or "127.0.0.1")
            if listen in ("0.0.0.0", "::", ""):
                listen = "127.0.0.1"
            port = int(config.get("port", 8765))
            base = f"http://{listen}:{port}"

            state = http_json(base, "GET", "/api/v1/system/recovery")
            if triggered_key == key and state.get("success") is True and state.get("reason") == "confirmed-on":
                completed_key = key
                LOG.info("recovery confirmed for window=%s attempts=%s", key, state.get("attempts"))
                time.sleep(sleep_seconds)
                continue

            if state.get("active") is True:
                # The backend may have entered recovery itself at startup before
                # the supervisor's first poll. Treat an active worker as belonging
                # to this window, but never trust a stale confirmed-on state from a
                # previous day after either process has restarted.
                triggered_key = key
            else:
                state = http_json(base, "POST", "/api/v1/system/recovery/start", {"force": False})
                triggered_key = key
                LOG.info("triggered recovery window=%s active=%s reason=%s", key, state.get("active"), state.get("reason"))
        except (ValueError, OSError, HTTPError, URLError, json.JSONDecodeError) as exc:
            LOG.warning("supervisor retry: %s: %s", type(exc).__name__, exc)
        except Exception:
            LOG.exception("unexpected supervisor failure")

        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
