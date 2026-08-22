#!/bin/sh
set -eu

REPO="MOPELotus/LanMiHome"
BRANCH="master"
BASE="/lotusemmc/lanmihome"
TMP="/tmp/lanmihome-w96d-install.$$"

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 1; }
[ -e /sys/class/bluetooth/hci0 ] || { echo "hci0 is missing" >&2; exit 1; }

missing=""
for pkg in python3 python3-bleak bluez-daemon; do
    opkg list-installed | grep -q "^$pkg - " || missing="$missing $pkg"
done
if [ -n "$missing" ]; then
    opkg update
    opkg install $missing
fi

mkdir -p "$TMP" "$BASE"
wget -q -T 30 -O "$TMP/repo.tar.gz" "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
tar -xzf "$TMP/repo.tar.gz" -C "$TMP"
SRC="$TMP/LanMiHome-$BRANCH"

cp "$SRC/router/w96d_sidecar.py" "$BASE/w96d_sidecar.py"
chmod 0755 "$BASE/w96d_sidecar.py"
if [ ! -f "$BASE/w96d.json" ]; then
    cp "$SRC/router/w96d.config.example.json" "$BASE/w96d.json"
fi
chmod 0600 "$BASE/w96d.json"
cp "$SRC/router/lanmihome-w96d.init" /etc/init.d/lanmihome-w96d
chmod 0755 /etc/init.d/lanmihome-w96d

/usr/bin/python3 "$BASE/w96d_sidecar.py" --config "$BASE/w96d.json" --check-config
/etc/init.d/lanmihome-w96d enable
/etc/init.d/lanmihome-w96d restart
sleep 1

echo "W96D sidecar installed."
echo "  config : $BASE/w96d.json"
echo "  status : wget -qO- http://127.0.0.1:8766/api/v1/w96d"
echo "  logs   : logread -e lanmihome.w96d -e w96d"
