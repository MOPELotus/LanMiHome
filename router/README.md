# LanMiHome router backend

`router/lanmihome.py` is the unified LAN-only backend for the ImmortalWrt router. It keeps the fan/lamp HTTP contract and adds router-native BLE for Xiaomi MiBeacon sensors, CUKTECH AD1204 chargers and W96D BLE fans.

## W96D BLE ownership

W96D does not use Xiaomi MIoT. It is controlled through its BLE GATT service. The W96D provider exposes the same logical operations regardless of which BLE central is selected.

Ownership is explicit:

```text
HOME
  router -> BLE -> W96D

SCHOOL
  23:00-06:00
    night_node -> BLE -> W96D

  06:00-23:00
    router -> BLE -> W96D

OUTDOOR
  phone -> BLE -> W96D
```

No automatic BLE competition or connection racing is performed.

## Runtime layout

```text
/usr/bin/python3                              CPython 3.11 from ImmortalWrt
/lotusemmc/miot/python                       existing python-miio dependencies
/lotusemmc/lanmihome/lanmihome.py           unified HTTP backend
/lotusemmc/lanmihome/lanmihome_w96d.py      W96D BLE provider
/etc/init.d/lanmihome                        procd service
```

The BLE runtime owns persistent Bluetooth sessions and device-specific providers decide only protocol handling. Environment scheduling decides ownership.
