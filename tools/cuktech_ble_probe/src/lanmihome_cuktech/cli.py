from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tomllib
from pathlib import Path

from bleak import BleakScanner

from .controller import CuktechController
from .manager import ChargerConfig, ChargerManager
from .protocol import (
    LANGUAGE_NAMES,
    LANGUAGES,
    PIID_NAMES,
    PORT_KEYS,
    SCENE_MODES,
    SCENE_NAMES,
    SCREEN_TIMEOUT_NAMES,
    SCREEN_TIMEOUTS,
    SUPPORTED_PROTOCOL_SWITCHES,
    PortReading,
)


def _add_config_args(parser: argparse.ArgumentParser, *, multi: bool = False) -> None:
    parser.add_argument("--config", type=Path, default=Path("chargers.toml"))
    if multi:
        parser.add_argument("--device", action="append", dest="devices")
    else:
        parser.add_argument("--device", help="configured charger name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cuktech-probe",
        description="LanMiHome complete multi-device CUKTECH AD1204 BLE controller",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug LanMiHome CUKTECH logs")
    parser.add_argument("--bleak-debug", action="store_true", help="also enable verbose Bleak backend logs")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan nearby BLE devices")
    scan.add_argument("--seconds", type=float, default=10.0)
    scan.add_argument("--all", action="store_true", help="show unnamed devices too")

    for name, help_text in (
        ("info", "connect and read basic device info"),
        ("services", "enumerate GATT services/characteristics"),
        ("auth", "test MiOT BLE authentication"),
        ("status", "read full charger state, settings and protocols"),
        ("probe", "read all readable SIID=2 properties"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_config_args(p)

    monitor = sub.add_parser("monitor", help="keep one or more chargers connected")
    _add_config_args(monitor, multi=True)
    monitor.add_argument("--raw", action="store_true", help="log decrypted RX/GET frames")

    getp = sub.add_parser("get", help="read one raw MiOT property")
    _add_config_args(getp)
    getp.add_argument("piid", type=int)

    setp = sub.add_parser("set", help="write one raw MiOT property")
    _add_config_args(setp)
    setp.add_argument("piid", type=int)
    setp.add_argument("value", type=lambda x: int(x, 0))

    mode = sub.add_parser("set-mode", help="set scene mode")
    _add_config_args(mode)
    mode.add_argument("mode", choices=sorted(SCENE_MODES))

    screen = sub.add_parser("set-screen", help="set screen timeout")
    _add_config_args(screen)
    screen.add_argument("timeout", choices=sorted(SCREEN_TIMEOUTS))

    language = sub.add_parser("set-language", help="set display language")
    _add_config_args(language)
    language.add_argument("language", choices=sorted(LANGUAGES))

    for name, help_text in (
        ("set-usba", "toggle USB-A low-current mode"),
        ("set-idle", "toggle idle screen-off"),
        ("set-orient", "toggle screen orientation lock"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_config_args(p)
        p.add_argument("action", choices=("on", "off"))

    goto = sub.add_parser("goto", help="switch charger screen page")
    _add_config_args(goto)
    goto.add_argument("page", type=int, choices=range(1, 6), metavar="1..5")

    timer = sub.add_parser("set-timer", help="set per-port countdown minutes")
    _add_config_args(timer)
    timer.add_argument("port", choices=("c1", "c2", "c3", "a"))
    timer.add_argument("minutes", type=int)

    port = sub.add_parser("set-port", help="toggle output port")
    _add_config_args(port)
    port.add_argument("port", choices=("c1", "c2", "c3", "a", "all"))
    port.add_argument("action", choices=("on", "off"))

    protocol = sub.add_parser("set-protocol", help="toggle PD/PPS/UFCS/SCP protocol switch")
    _add_config_args(protocol)
    protocol.add_argument("port", choices=("c1", "c2", "c3", "a"))
    protocol.add_argument("protocol", choices=("pd", "pps", "ufcs", "scp"))
    protocol.add_argument("action", choices=("on", "off"))

    return parser


def load_config(path: Path, selected: list[str] | None = None) -> list[ChargerConfig]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("charger", [])
    configs: list[ChargerConfig] = []
    wanted = set(selected or [])
    for item in entries:
        name = str(item["name"]).strip()
        if wanted and name not in wanted:
            continue
        token = bytes.fromhex(str(item["token"]).strip())
        if len(token) != 12:
            raise ValueError(f"{name}: token must decode to 12 bytes")
        configs.append(ChargerConfig(name, str(item["address"]).strip(), token))
    missing = wanted - {cfg.name for cfg in configs}
    if missing:
        raise ValueError(f"unknown configured device(s): {', '.join(sorted(missing))}")
    return configs


def _one(configs: list[ChargerConfig], device: str | None) -> ChargerConfig:
    if device:
        selected = [cfg for cfg in configs if cfg.name == device]
        if not selected:
            raise ValueError(f"unknown configured device: {device}")
        return selected[0]
    if len(configs) != 1:
        names = ", ".join(cfg.name for cfg in configs)
        raise ValueError(f"command targets one charger; pass --device NAME (configured: {names})")
    return configs[0]


async def cmd_scan(seconds: float, show_all: bool) -> None:
    print(f"Scanning BLE for {seconds:g}s ...")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for device, adv in devices.values():
        name = adv.local_name or device.name or ""
        if not show_all and not name:
            continue
        target = "njcuk" in name.lower() or "cuktech" in name.lower()
        rows.append((adv.rssi if adv.rssi is not None else -999, device.address, name, target))
    for rssi, address, name, target in sorted(rows, reverse=True):
        marker = "  <CUKTECH?>" if target else ""
        print(f"{address:24} RSSI={rssi:4}  {name or '(unnamed)'}{marker}")


async def _connect(cfg: ChargerConfig, *, auth: bool = True) -> CuktechController:
    controller = CuktechController(cfg.name, cfg.address, cfg.token)
    await controller.connect()
    if auth:
        await controller.authenticate()
    return controller


async def cmd_info(configs: list[ChargerConfig]) -> None:
    for cfg in configs:
        ctrl = await _connect(cfg, auth=False)
        try:
            info = ctrl.session.info
            print(f"{cfg.name}: address={cfg.address} model={info.model or '?'} firmware={info.firmware or '?'} miot={info.protocol_version or '?'}")
        finally:
            await ctrl.disconnect()


async def cmd_services(cfg: ChargerConfig) -> None:
    ctrl = await _connect(cfg, auth=False)
    try:
        print(f"[{cfg.name}] MTU={getattr(ctrl.session.client, 'mtu_size', '?')}")
        for service in ctrl.session.client.services:
            print(f"Service {service.uuid} [{service.description}]")
            for char in service.characteristics:
                print(f"  Char {char.uuid} handle=0x{char.handle:04X} [{', '.join(char.properties)}]")
    finally:
        await ctrl.disconnect()


async def cmd_auth(cfg: ChargerConfig) -> None:
    ctrl = await _connect(cfg, auth=True)
    try:
        print(f"[{cfg.name}] authenticated")
    finally:
        await ctrl.disconnect()


def _setting_display(piid: int, value: int) -> str:
    if piid == 5:
        return SCENE_NAMES.get(value, str(value))
    if piid == 6:
        return SCREEN_TIMEOUT_NAMES.get(value, str(value))
    if piid == 13:
        return LANGUAGE_NAMES.get(value, str(value))
    if piid in (15, 19, 20):
        return "on" if value else "off"
    if piid == 16:
        return f"0b{value & 0xF:04b}"
    if piid in (17, 18, 21):
        return f"0x{value & 0xFFFFFFFF:08X}"
    return str(value)


async def cmd_status(configs: list[ChargerConfig]) -> None:
    for cfg in configs:
        ctrl = await _connect(cfg)
        try:
            await ctrl.initialize_state()
            state = ctrl.state
            print(f"[{cfg.name}] {state.model or '?'} fw={state.firmware or '?'} MiOT={state.miot_version or '?'}")
            for piid in range(1, 5):
                r = state.ports.get(piid)
                if not r:
                    continue
                suffix = " shared" if r.shared else ""
                print(f"  {r.name:>4} {r.voltage:5.1f} V {r.current:4.1f} A {r.power:6.1f} W  {r.protocol_hint:<6} [{r.protocol_source}]{suffix}")
            print(f"  total {state.total_power():.1f} W")
            print("  settings:")
            for piid, value in sorted(state.settings.items()):
                print(f"    {piid:2d} {PIID_NAMES.get(piid, 'unknown')}: {_setting_display(piid, value)}")
            print("  protocol switches:")
            for port, switches in state.protocol_switches.items():
                flags = " ".join(f"{name}={'on' if enabled else 'off'}" for name, enabled in switches.items())
                print(f"    {port}: {flags}")
        finally:
            await ctrl.disconnect()


async def cmd_probe(cfg: ChargerConfig) -> None:
    ctrl = await _connect(cfg)
    try:
        for piid in range(1, 22):
            name = PIID_NAMES.get(piid, f"PIID {piid}")
            if piid == 14:
                print(f"{piid:2d} {name}: write-only")
                continue
            result = await ctrl.get_property(piid)
            if result.value is None:
                print(f"{piid:2d} {name}: no response")
            else:
                raw = result.raw.hex() if result.raw else ""
                print(f"{piid:2d} {name}: {_setting_display(piid, result.value)}  raw={raw}")
            await asyncio.sleep(0.05)
    finally:
        await ctrl.disconnect()


async def cmd_monitor(configs: list[ChargerConfig], raw: bool) -> None:
    last: dict[tuple[str, int], tuple] = {}

    def on_state(name: str, state: str) -> None:
        print(f"[{name}] {state}", flush=True)

    def on_reading(name: str, reading: PortReading) -> None:
        key = (name, reading.piid)
        value = (
            reading.name,
            reading.voltage,
            reading.current,
            reading.power,
            reading.protocol_hint,
            reading.status_raw,
            reading.shared,
        )
        if last.get(key) == value:
            return
        last[key] = value
        source = "HW" if reading.protocol_source == "hardware" else "guess"
        shared = " shared" if reading.shared else ""
        print(
            f"[{name}] {reading.name:>4} {reading.voltage:5.1f} V  {reading.current:4.1f} A  "
            f"{reading.power:6.1f} W  {reading.protocol_hint:<6} {source:<5} "
            f"status=0x{reading.status_raw:02X} code=0x{reading.code_raw:02X}{shared}",
            flush=True,
        )

    manager = ChargerManager(configs, on_reading=on_reading, on_state=on_state, raw=raw)
    try:
        await manager.run()
    finally:
        await manager.stop()


async def _run_write(cfg: ChargerConfig, action) -> None:
    ctrl = await _connect(cfg)
    try:
        result = await action(ctrl)
        if not result.ok:
            raise RuntimeError("charger did not acknowledge command")
        print(f"[{cfg.name}] ok value={result.value}")
    finally:
        await ctrl.disconnect()


async def async_main(args: argparse.Namespace) -> None:
    if args.command == "scan":
        await cmd_scan(args.seconds, args.all)
        return

    multi_selected = getattr(args, "devices", None)
    single_selected = getattr(args, "device", None)
    selected = multi_selected if multi_selected is not None else ([single_selected] if single_selected else None)
    configs = load_config(args.config, selected)
    if not configs:
        raise ValueError("no charger entries selected from config")

    if args.command == "info":
        await cmd_info(configs)
        return
    if args.command == "monitor":
        await cmd_monitor(configs, args.raw)
        return
    if args.command == "status":
        await cmd_status(configs)
        return

    cfg = _one(configs, single_selected)
    if args.command == "services":
        await cmd_services(cfg)
    elif args.command == "auth":
        await cmd_auth(cfg)
    elif args.command == "probe":
        await cmd_probe(cfg)
    elif args.command == "get":
        ctrl = await _connect(cfg)
        try:
            result = await ctrl.get_property(args.piid)
            print(json.dumps({"piid": args.piid, "value": result.value, "raw": result.raw.hex() if result.raw else None}, ensure_ascii=False))
        finally:
            await ctrl.disconnect()
    elif args.command == "set":
        await _run_write(cfg, lambda c: c.set_property(args.piid, args.value, raw=True))
    elif args.command == "set-mode":
        await _run_write(cfg, lambda c: c.set_mode(SCENE_MODES[args.mode]))
    elif args.command == "set-screen":
        await _run_write(cfg, lambda c: c.set_screen_timeout(SCREEN_TIMEOUTS[args.timeout]))
    elif args.command == "set-language":
        await _run_write(cfg, lambda c: c.set_language(LANGUAGES[args.language]))
    elif args.command == "set-usba":
        await _run_write(cfg, lambda c: c.set_usb_a_low_current(args.action == "on"))
    elif args.command == "set-idle":
        await _run_write(cfg, lambda c: c.set_idle_screen_off(args.action == "on"))
    elif args.command == "set-orient":
        await _run_write(cfg, lambda c: c.set_orientation_lock(args.action == "on"))
    elif args.command == "goto":
        await _run_write(cfg, lambda c: c.goto_page(args.page))
    elif args.command == "set-timer":
        if not 0 <= args.minutes <= 1440:
            raise ValueError("minutes must be 0..1440")
        await _run_write(cfg, lambda c: c.set_timer(args.port, args.minutes))
    elif args.command == "set-port":
        await _run_write(cfg, lambda c: c.set_port(args.port, args.action == "on"))
    elif args.command == "set-protocol":
        supported = SUPPORTED_PROTOCOL_SWITCHES[args.port]
        if args.protocol not in supported:
            raise ValueError(f"{args.port} supports protocol switches: {', '.join(supported)}")
        await _run_write(cfg, lambda c: c.set_protocol(args.port, args.protocol, args.action == "on"))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("lanmihome.cuktech").setLevel(logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger("bleak").setLevel(logging.DEBUG if args.bleak_debug else logging.WARNING)
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.verbose:
            raise
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
