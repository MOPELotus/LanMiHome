# LanMiHome

LAN-only smart-home controller with an ImmortalWrt router backend and Android client.

The router backend integrates local MIoT control for the fan/lamp, BlueZ MiBeacon scanning for the Xiaomi temperature/humidity sensor, multi-device CUKTECH AD1204 GATT sessions, and a recovery supervisor that can bring the fan back online during the configured morning power-return window even when system time becomes valid only after NTP sync.

The Android app is normally a pure LAN client. On a rooted spare Android phone it can additionally run **Night Node** mode: use an already-running (or root-managed) 2.4 GHz WPA2 hotspot, discover fan/lamp devices on the hotspot subnet via miIO UDP 54321, control them directly with locally stored Xiaomi tokens, and expose a small LanMiHome-compatible HTTP server. The normal client can automatically try the current Wi-Fi gateway as a fallback backend when the primary router is offline.

Night Node secrets are stored only in the spare phone's app-private storage and Android backup is disabled. Router-side Xiaomi/BLE/CUKTECH secrets remain in the router-local configuration.

See [`router/README.md`](router/README.md) for router deployment, recovery behavior and API details.
