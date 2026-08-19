# CUKTECH BLE Core

LanMiHome's platform-neutral, multi-device BLE implementation for the CUKTECH 10 GaN Charger Ultra (`njcuk.fitting.ad1204`). It is derived from the MIT-licensed reverse-engineering work in `kairui1108/cuktech-ble-ha`, but keeps LanMiHome's own multi-device architecture instead of the upstream single-charger server architecture.

The same Python core targets Windows/WinRT during development and BlueZ/USB HCI on the ImmortalWrt router later.

## Device-side feature coverage

The BLE/device capability layer is now intended to cover the upstream feature set rather than only the original telemetry probe:

- FE95 GATT discovery and device information;
- MiOT BLE login/authentication;
- isolated per-device session keys, counters, queues and reconnect workers;
- multiple AD1204 chargers connected concurrently;
- C1/C2/C3/A voltage, current and power from Notify and active GET;
- stable-load refresh: active ports are re-sampled when Notify becomes stale;
- C3+A shared-output detection (`status_raw == 0x11`), represented as one `C3+A` aggregate measurement so total power is never double-counted;
- exact hardware charging protocol state from PIID 17/18, with upstream/Mi Home-aligned fallback detection when hardware protocol state is unavailable;
- 5V / QC / AFC / FCP / SCP / PD / PPS / UFCS protocol names;
- PDO/protocol state decoding from PIID 17/18;
- protocol-switch state and control from PIID 21:
  - C1/C2: PD, PPS, UFCS;
  - C3/A: UFCS, SCP;
- scene mode (PIID 5);
- screen timeout (PIID 6);
- countdown configuration and per-port timers (PIID 8-12);
- language (PIID 13);
- screen-page jump (PIID 14, write-only);
- USB-A low-current mode (PIID 15);
- per-port/all-port output control (PIID 16);
- idle screen-off (PIID 19);
- screen orientation lock (PIID 20);
- generic raw GET/SET for protocol research;
- GATT service enumeration, auth test and full PIID probe;
- command-channel serialization, preservation of interleaved Notify traffic, keepalive and per-device reconnect.

This directory intentionally does **not** duplicate upstream's Home Assistant integration, MQTT/Bemfa bridge, Web UI, Docker packaging or SQLite history server. Those are application/integration layers; LanMiHome has its own router backend and Android client. The complete charger-facing BLE capability is exposed here for that backend to consume.

## Windows quick start

```powershell
cd tools\cuktech_ble_probe
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

Copy-Item config.example.toml chargers.toml
notepad chargers.toml
```

Keep `chargers.toml` local. Do not commit or share device tokens.

### Discovery / diagnostics

```powershell
python -m lanmihome_cuktech scan --seconds 10
python -m lanmihome_cuktech info --config chargers.toml
python -m lanmihome_cuktech services --config chargers.toml --device desk
python -m lanmihome_cuktech auth --config chargers.toml --device desk
python -m lanmihome_cuktech status --config chargers.toml --device desk
python -m lanmihome_cuktech probe --config chargers.toml --device desk
python -m lanmihome_cuktech get --config chargers.toml --device desk 18
```

### Multi-device monitoring

```powershell
python -m lanmihome_cuktech monitor --config chargers.toml
python -m lanmihome_cuktech monitor --config chargers.toml --device desk --raw
```

`--raw` logs decrypted `RX` frames and active `GET` responses. `-v` enables LanMiHome CUKTECH debug logs without turning Bleak/WinRT into a wall of debug output; use `--bleak-debug` only when backend diagnostics are actually needed.

AD1204 telemetry is primarily change-triggered. The monitor therefore does one complete port snapshot after authentication, then combines Notify updates with active refresh. Active ports are checked much more frequently than long-idle ports.

### Controls

All write commands target exactly one configured charger, so use `--device` when the config contains multiple chargers.

```powershell
python -m lanmihome_cuktech set-mode     --config chargers.toml --device desk ai
python -m lanmihome_cuktech set-screen   --config chargers.toml --device desk 10
python -m lanmihome_cuktech set-language --config chargers.toml --device desk cn
python -m lanmihome_cuktech set-usba     --config chargers.toml --device desk on
python -m lanmihome_cuktech set-idle     --config chargers.toml --device desk on
python -m lanmihome_cuktech set-orient   --config chargers.toml --device desk on
python -m lanmihome_cuktech goto         --config chargers.toml --device desk 2
python -m lanmihome_cuktech set-timer    --config chargers.toml --device desk c1 30
python -m lanmihome_cuktech set-port     --config chargers.toml --device desk c3 off
python -m lanmihome_cuktech set-protocol --config chargers.toml --device desk c1 pps off
```

Raw protocol research remains available:

```powershell
python -m lanmihome_cuktech get --config chargers.toml --device desk 21
python -m lanmihome_cuktech set --config chargers.toml --device desk 21 0x030C0C0F
```

## C3 + A shared-output semantics

The real charger confirms the upstream `status_raw == 0x11` interpretation. When C3 and A are simultaneously loaded, the charger reports the combined electrical measurement on PIID 3 and PIID 4 becomes idle/suppressed. LanMiHome therefore models it as:

```text
C3 + A  5.1 V / 3.2 A / 16.3 W  shared
```

The aggregate is counted once in charger total power. The protocol does not provide a reliable per-device current split in this shared mode, so the core does not invent one.

## Architecture

```text
Bleak backend
  Windows: WinRT / Intel adapter
  Linux:   BlueZ / USB HCI
        |
        +-- CuktechSession                 # FE95 + authentication/crypto
        +-- MiotCommandClient              # serialized encrypted GET/SET
        +-- CuktechController              # complete AD1204 device API/state
        +-- ChargerManager
              +-- charger A worker
              +-- charger B worker
              +-- ...
```

No BLE credential or cryptographic state is global. Every charger owns its own GATT client, auth state, session keys, counters and notification queues.

## Attribution

Protocol behavior is derived from `kairui1108/cuktech-ble-ha` under the MIT license. See `THIRD_PARTY_NOTICES.md`.
