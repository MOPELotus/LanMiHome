#!/usr/bin/env python3
"""Small integration helper for attaching W96D to LanMiHome HTTP models.

The existing MIoT API remains untouched. This module only normalizes W96D
commands and responses so router/night-node/phone providers can share the
same schema.
"""
from __future__ import annotations

from typing import Any


CONTROL_FIELDS = {
    "power",
    "speed",
    "natural",
    "turbo",
    "light",
}


def normalize_command(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in CONTROL_FIELDS:
        if key in payload:
            result[key] = payload[key]
    if "speed" in result:
        result["speed"] = max(0, min(100, int(result["speed"])))
    return result


def state_response(controller, owner: str) -> dict[str, Any]:
    state = controller.json()
    state["owner"] = owner
    return state
