#!/bin/sh
set -eu

REPO="MOPELotus/LanMiHome"
BRANCH="master"
BASE="/lotusemmc/lanmihome"
PYTHONPATH_VALUE="/lotusemmc/miot/python:$BASE"
TMP="/tmp/lanmihome-install.$$"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BASE/backup-$STAMP"

cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

if [ "$(id -u)" != "0" ]; then
    echo "LanMiHome installer must run as root" >&2
    exit 1
fi

if ! mount | grep -q ' on /lotusemmc '; then
    echo "/lotusemmc is not mounted; refusing to install" >&2
    exit 1
fi

# Kernel Bluetooth is custom-firmware-specific. Never force-install an official
# kmod here: only verify that the rebuilt firmware actually exposes hci0.
if [ ! -e /sys/class/bluetooth/hci0 ]; then
    echo "hci0 is missing. Keep the custom kmod-bluetooth/RTL firmware build; do not force an official kmod." >&2
    exit 1
fi

missing=""
for pkg in python3 python3-bleak python3-cryptography bluez-daemon; do
    if ! opkg list-installed | grep -q "^$pkg - "; then
        missing="$missing $pkg"
    fi
done
if [ -n "$missing" ]; then
    echo "Installing userspace packages:$missing"
    opkg update
    # These are userspace packages; unlike kernel modules they do not depend on
    # the custom kernel ABI hash.
    opkg install $missing
fi

mkdir -p "$TMP" "$BASE" "$BACKUP"
ARCHIVE="$TMP/lanmihome.tar.gz"
echo "Downloading $REPO@$BRANCH ..."
wget -q -T 30 -O "$ARCHIVE" "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
tar -xzf "$ARCHIVE" -C "$TMP"
SRC="$TMP/LanMiHome-$BRANCH"

for required in \
    "$SRC/router/lanmihome.py" \
    "$SRC/router/lanmihome.init" \
    "$SRC/router/config.example.json" \
    "$SRC/tools/cuktech_ble_probe/src/lanmihome_cuktech/__init__.py"; do
    if [ ! -f "$required" ]; then
        echo "archive is missing required file: $required" >&2
        exit 1
    fi
done

# Stop and disable every previous init script that launches this exact backend
# path. This catches old service names without guessing what the user called it.
for svc in /etc/init.d/*; do
    [ -f "$svc" ] || continue
    if grep -q '/lotusemmc/lanmihome/lanmihome.py' "$svc" 2>/dev/null; then
        name="$(basename "$svc")"
        echo "Stopping previous service: $name"
        "$svc" stop 2>/dev/null || true
        "$svc" disable 2>/dev/null || true
        cp -p "$svc" "$BACKUP/init.d-$name" 2>/dev/null || true
    fi
done

# Back up the old backend and local configuration without printing any secret.
for file in lanmihome.py config.json config.example.json; do
    [ -f "$BASE/$file" ] && cp -p "$BASE/$file" "$BACKUP/$file"
done

cp "$SRC/router/lanmihome.py" "$BASE/lanmihome.py"
cp "$SRC/router/config.example.json" "$BASE/config.example.json"
rm -rf "$BASE/lanmihome_cuktech"
cp -a "$SRC/tools/cuktech_ble_probe/src/lanmihome_cuktech" "$BASE/lanmihome_cuktech"
chmod 0755 "$BASE/lanmihome.py"

if [ ! -f "$BASE/config.json" ]; then
    cp "$BASE/config.example.json" "$BASE/config.json"
    echo "No existing config.json was found; created one from the example. Fill secrets locally before use."
fi
chmod 0600 "$BASE/config.json"

cp "$SRC/router/lanmihome.init" /etc/init.d/lanmihome
chmod 0755 /etc/init.d/lanmihome

# Parse/import everything before replacing the live service. This does not print
# Xiaomi tokens, BLE bind keys or CUKTECH tokens.
PYTHONPATH="$PYTHONPATH_VALUE" /usr/bin/python3 "$BASE/lanmihome.py" \
    --config "$BASE/config.json" --check-config

/etc/init.d/lanmihome enable
/etc/init.d/lanmihome restart
sleep 2

echo
echo "LanMiHome router backend installed."
echo "  service: /etc/init.d/lanmihome"
echo "  backend: $BASE/lanmihome.py"
echo "  config : $BASE/config.json (preserved, mode 0600)"
echo "  backup : $BACKUP"
echo "  logs   : logread -e lanmihome -e cuktech"
echo "  health : wget -qO- http://127.0.0.1:8765/api/v1/health"
