"""W96D HTTP adapter.

Kept separate from Xiaomi MIoT handlers. Existing fan/lamp routes remain unchanged.
"""


def w96d_state(runtime):
    return runtime.state()


def w96d_patch(runtime, body):
    return runtime.control(body)
