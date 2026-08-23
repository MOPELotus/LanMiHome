# LanMiHome

LAN-only smart-home controller with an ImmortalWrt router backend and Android client.

The router backend integrates local MIoT control for the fan/lamp, BlueZ MiBeacon scanning for the Xiaomi temperature/humidity sensor, multi-device CUKTECH AD1204 GATT sessions, W96D BLE control, and a recovery supervisor that can bring the MIoT fan back online during the configured morning power-return window even when system time becomes valid only after NTP sync.

The active Android build is now a single client APK. It uses the configured router backend directly and no longer auto-switches to the Xiaomi 10S / Wi-Fi-gateway Night Node path. The previous rooted 10S Night Node, MIoT night fan control, morning handoff, and W96D Night Owner source remain in the repository/history but are intentionally disabled and hidden.

W96D uses deterministic ownership: HOME and SCHOOL are router-owned 24h; OUTDOOR explicitly releases the router and uses the phone's native BluetoothGatt until the user exits outdoor mode.

Router-side Xiaomi/BLE/CUKTECH secrets remain in router-local configuration. Android backup is disabled.

See [`router/README.md`](router/README.md) for router deployment/recovery details and [`docs/W96D.md`](docs/W96D.md) for the W96D BLE channel.
