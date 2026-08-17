# CUKTECH BLE Probe

A small, platform-neutral BLE core extracted for LanMiHome development. The first target is Windows + Intel BE200; the same Bleak-facing code is intended to move to BlueZ + CM390 on the router later.

Current scope:

- scan nearby BLE devices;
- read AD1204 basic info;
- MiOT BLE login/authentication;
- keep multiple chargers connected concurrently;
- decrypt real-time C1/C2/C3/A push frames;
- actively GET all four ports after authentication so stable loads are visible immediately;
- periodically re-sample stale ports because AD1204 normally reports stable power only when the value changes;
- preserve live Notify frames that arrive while an active GET is in flight;
- print voltage/current/power plus raw status/protocol bytes;
- reconnect each charger independently;
- optional plaintext-frame logging with `--raw`.

This intentionally does **not** touch the Android app or router backend yet.

## Windows quick start

Requires Python 3.11+ and a working Windows Bluetooth adapter (BE200 is fine).

```powershell
cd tools\cuktech_ble_probe
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

python -m lanmihome_cuktech scan --seconds 10
```

Copy the config template and fill the credentials locally:

```powershell
Copy-Item config.example.toml chargers.toml
notepad chargers.toml
```

Do not commit or share `chargers.toml`.

Basic-info probe:

```powershell
python -m lanmihome_cuktech info --config chargers.toml
```

Keep both configured chargers connected and monitor port telemetry:

```powershell
python -m lanmihome_cuktech monitor --config chargers.toml
```

Only one configured charger:

```powershell
python -m lanmihome_cuktech monitor --config chargers.toml --device desk
```

Include decrypted MiOT plaintext frames for protocol debugging. Normal Notify frames are logged as `RX`; active property reads are logged as `GET C1/C2/C3/A`:

```powershell
python -m lanmihome_cuktech -v monitor --config chargers.toml --raw
```

## Port refresh model

AD1204 port telemetry is primarily change-triggered. A stable USB-A load can therefore remain electrically active without emitting repeated Notify frames. The probe handles that by combining events with active reads:

```text
authentication complete
        |
        +-- GET C1
        +-- GET C2
        +-- GET C3
        +-- GET A
        |
        +-- normal Notify updates immediately
        |
        +-- if a port has no fresh reading for ~15 s,
            actively GET one stale port at a time
```

The CLI suppresses duplicate displayed values, so active refreshes do not spam the terminal when voltage/current remain unchanged. `--raw` still shows the corresponding GET response for protocol diagnostics.

## Architecture

```text
Bleak backend
  Windows: WinRT / Intel BE200
  Linux:   BlueZ / USB HCI (planned CM390)
        |
        +-- CuktechSession(name, address, token)  # one isolated MiOT session
        |     +-- FE95 GATT
        |     +-- MiOT BLE login
        |     +-- per-device session keys/counters/notify queues
        |
        +-- ChargerManager
              +-- charger A reconnect task
              +-- charger B reconnect task
              +-- ...
```

No BLE credential is stored globally. Each charger has its own token, GATT client, session keys, counters and notification queues, so multi-device support is part of the core rather than bolted onto a single-device server.

## Notes

The protocol implementation is derived from the reverse-engineering work in `kairui1108/cuktech-ble-ha` (MIT). See `THIRD_PARTY_NOTICES.md`.

The first probe deliberately treats protocol detection as a hint. Voltage/current/power and raw bytes are the important data for initial validation; exact PD/PPS/UFCS/SCP classification can be brought over after the BLE session is proven stable on both Windows and BlueZ.
