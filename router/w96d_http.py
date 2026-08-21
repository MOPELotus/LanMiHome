"""W96D HTTP adapter.

Kept separate from Xiaomi MIoT handlers. Existing fan/lamp routes remain unchanged.
"""


_ALLOWED_FIELDS = {
    "power",
    "speed",
    "natural",
    "turbo",
    "light",
}


def w96d_state(runtime):
    return {
        "ok": True,
        "device": "W96D",
        "state": runtime.state(),
    }


def w96d_patch(runtime, body):
    if not isinstance(body, dict):
        raise ValueError("W96D body must be object")
    unknown = set(body) - _ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            "unknown W96D fields: " + ",".join(sorted(unknown))
        )
    runtime.control(body)
    return w96d_state(runtime)
