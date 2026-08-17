from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tomllib
from pathlib import Path

from bleak import BleakScanner

from .manager import ChargerConfig, ChargerManager
from .protocol import PortReading
from .session import CuktechSession


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cuktech-probe",
        description="LanMiHome CUKTECH AD1204 BLE probe",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan nearby BLE devices")
    scan.add_argument("--seconds", type=float, default=10.0)
    scan.add_argument("--all", action="store_true", help="show unnamed devices too")

    info = sub.add_parser("info", help="connect and read basic device info")
    info.add_argument("--config", type=Path, default=Path("chargers.toml"))
    info.add_argument("--device", help="configured charger name; defaults to all")

    monitor = sub.add_parser("monitor", help="keep one or more chargers connected")
    monitor.add_argument("--config", type=Path, default=Path("chargers.toml"))
    monitor.add_argument("--device", action="append", dest="devices")
    monitor.add_argument("--raw", action="store_true")

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
        configs.append(
            ChargerConfig(
                name=name,
                address=str(item["address"]).strip(),
                token=token,
            )
        )

    missing = wanted - {cfg.name for cfg in configs}
    if missing:
        raise ValueError(f"unknown configured device(s): {', '.join(sorted(missing))}")
    return configs


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


async def cmd_info(configs: list[ChargerConfig]) -> None:
    for cfg in configs:
        session = CuktechSession(cfg.name, cfg.address, cfg.token)
        try:
            await session.connect()
            info = await session.read_device_info()
            print(
                f"{cfg.name}: address={cfg.address} "
                f"model={info.model or '?'} firmware={info.firmware or '?'} "
                f"miot={info.protocol_version or '?'}"
            )
        finally:
            await session.disconnect()


async def cmd_monitor(configs: list[ChargerConfig], raw: bool) -> None:
    last: dict[tuple[str, int], tuple[float, float, float, str]] = {}

    def on_state(name: str, state: str) -> None:
        print(f"[{name}] {state}", flush=True)

    def on_reading(name: str, reading: PortReading) -> None:
        key = (name, reading.piid)
        value = (
            reading.voltage,
            reading.current,
            reading.power,
            reading.protocol_hint,
        )
        if last.get(key) == value:
            return
        last[key] = value
        print(
            f"[{name}] {reading.name:>2} "
            f"{reading.voltage:5.1f} V  "
            f"{reading.current:4.1f} A  "
            f"{reading.power:6.1f} W  "
            f"{reading.protocol_hint:<10} "
            f"status=0x{reading.status_raw:02X} code=0x{reading.code_raw:02X}",
            flush=True,
        )

    manager = ChargerManager(configs, on_reading=on_reading, on_state=on_state, raw=raw)
    try:
        await manager.run()
    finally:
        await manager.stop()


async def async_main(args: argparse.Namespace) -> None:
    if args.command == "scan":
        await cmd_scan(args.seconds, args.all)
        return

    selected = None
    if args.command == "info" and args.device:
        selected = [args.device]
    elif args.command == "monitor":
        selected = args.devices

    configs = load_config(args.config, selected)
    if not configs:
        raise ValueError("no charger entries selected from config")

    if args.command == "info":
        await cmd_info(configs)
    elif args.command == "monitor":
        await cmd_monitor(configs, args.raw)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.verbose:
            raise
        raise SystemExit(2) from exc
