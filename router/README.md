# LanMiHome router backend

`router/lanmihome.py` is the unified LAN-only backend for the ImmortalWrt router. It keeps the fan/lamp HTTP contract and adds router-native BLE for Xiaomi MiBeacon sensors and CUKTECH AD1204 chargers. `router/recovery_supervisor.py` continuously watches the configured morning recovery window so a router that boots before NTP has corrected its clock can still start fan recovery after time becomes valid.

## Runtime layout

```text
/usr/bin/python3                              CPython 3.11 from ImmortalWrt
/lotusemmc/miot/python                       existing python-miio dependencies
/lotusemmc/lanmihome/lanmihome.py           unified HTTP backend
/lotusemmc/lanmihome/recovery_supervisor.py  recovery/NTP supervisor
/lotusemmc/lanmihome/lanmihome_cuktech      CUKTECH BLE core
/lotusemmc/lanmihome/config.json             local secrets; never commit/share
/etc/init.d/lanmihome                        procd service (backend + supervisor)
```

The BLE runtime owns one persistent BlueZ scanner. It feeds the configured Xiaomi FE95 sensor and also resolves CUKTECH devices to `BLEDevice` objects before GATT connection, avoiding Bleak's implicit per-connection discovery. Each charger keeps its own authenticated GATT session. HTTP charger operations are queued onto the owning charger worker so command replies and Notify traffic still have a single receive-queue consumer.

## Install/update

Run on the router as root:

```sh
wget -O /tmp/lanmihome-install.sh \
  https://raw.githubusercontent.com/MOPELotus/LanMiHome/master/router/install.sh
sh /tmp/lanmihome-install.sh
```

The installer preserves `/lotusemmc/lanmihome/config.json`, backs up the previous backend/supervisor/init script, disables any previous init script that launched the same backend path, installs the current code, and registers `/etc/init.d/lanmihome` with procd. It never force-installs a kernel module; `hci0` must already come from the matching custom firmware.

Keep all Xiaomi tokens, the sensor bind key and CUKTECH tokens only in the router-local `config.json`. `config.example.json` contains placeholders only.

## Configuration

Both the historical top-level `fan`/`lamp` objects and `devices.fan` / `devices.lamp` are accepted, so an existing config does not need to be rewritten. Router-native BLE sections are:

```json
{
  "sensor": {
    "enabled": true,
    "address": "AA:BB:CC:DD:EE:FF",
    "bind_key": "16-byte-hex-bind-key",
    "stale_seconds": 180
  },
  "chargers": [
    {
      "enabled": true,
      "name": "desk",
      "address": "AA:BB:CC:DD:EE:01",
      "token": "12-byte-hex-token"
    }
  ]
}
```

A configured-but-unplugged charger remains offline and is retried independently; it does not prevent the HTTP service, sensor scanner, fan, lamp, or other chargers from working.

### Morning fan recovery

```json
{
  "recovery": {
    "enabled": true,
    "start": "05:00",
    "end": "06:30",
    "retry_seconds": 5,
    "force_timeout_seconds": 120,
    "supervisor_seconds": 10,
    "clock_min_year": 2024
  }
}
```

`lanmihome.py` performs the actual recovery: it repeatedly tries the fan, reads its power state, toggles only when necessary, and reads again to verify that the fan is on. Normal recovery keeps retrying until the configured window ends. `force_timeout_seconds` applies only to a manually forced recovery request.

The separate recovery supervisor checks the clock and backend every `supervisor_seconds`. It ignores obviously uninitialized clocks whose year is below `clock_min_year`, then starts recovery when the clock later enters the configured window. This closes the boot-before-NTP race. If the backend itself restarts during the window, the supervisor sees that recovery is no longer active and starts it again. After a `confirmed-on` result, it does not retrigger recovery for that window. Set `enabled` to `false` to disable automatic recovery entirely.

The existing configuration does not need the two new supervisor keys; the defaults are 10 seconds and year 2024.

## HTTP API

Existing endpoints remain:

```text
GET   /api/v1/health
GET   /api/v1/capabilities
GET   /api/v1/state
GET   /api/v1/fan
PATCH /api/v1/fan
POST  /api/v1/fan/action
GET   /api/v1/lamp
PATCH /api/v1/lamp
POST  /api/v1/lamp/action
GET   /api/v1/sensor
POST  /api/v1/sensor/report
GET   /api/v1/system/recovery
POST  /api/v1/system/recovery/start
POST  /api/v1/raw/{fan|lamp}/{get|set|action}
```

Router-native charger endpoints:

```text
GET   /api/v1/chargers
GET   /api/v1/charger/{name}
PATCH /api/v1/charger/{name}
POST  /api/v1/charger/{name}/action
POST  /api/v1/charger/{name}/raw/get
POST  /api/v1/charger/{name}/raw/set
```

`PATCH /api/v1/charger/{name}` accepts `scene_mode`, `screen_timeout`, `language`, `usb_a_low_current`, `idle_screen_off`, and `orientation_lock`.

Charger actions use one of these request forms:

```json
{"name":"goto","page":2}
{"name":"set-timer","port":"c1","minutes":30}
{"name":"set-port","port":"c3","enabled":false}
{"name":"set-protocol","port":"c1","protocol":"pps","enabled":false}
```

PIID 14 / `goto` is a write-only command and an ACK does not prove a visible screen-page change; it is intentionally not presented as confirmed device state.

The sensor scanner only publishes decoded measurements for the configured FE95/MiBeacon sensor. `POST /api/v1/sensor/report` remains as a legacy/external compatibility endpoint; the Android client no longer scans BLE, stores a bind key, decrypts MiBeacon frames, or uses this endpoint.

## Diagnostics

```sh
/etc/init.d/lanmihome status
logread -e lanmihome -e recovery-supervisor -e cuktech
wget -qO- http://127.0.0.1:8765/api/v1/health
tail -n 30 /etc/init.d/lanmihome
```

The HTTP API never returns device tokens or BLE bind keys.
