from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import time
import urllib.parse


CHINA_TIME = timezone(timedelta(hours=8))
WLAN_PAGE = "/html/amp/wlanbasic/wlan.asp"
RADIO_LIST = "/html/amp/common/wlan_list.asp"
DEVICE_PAGE = "/html/ssmp/deviceinfo/cudeviceinfo.asp"
RADIO_PREFIX = "InternetGatewayDevice.LANDevice.1.WiFi.Radio."
EXPECTED_MODEL = "HN8346X6-C"
EXPECTED_FIRMWARE = "V5R022C00S030"


def decode_js(value):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), value)


def parse_radios(source):
    pattern = r'new\s+stRadio\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([01])"\s*\)'
    radios = {}
    for domain, band, enabled in re.findall(pattern, source):
        domain, band = decode_js(domain), decode_js(band)
        if not re.fullmatch(re.escape(RADIO_PREFIX) + r"[12]", domain):
            raise ValueError("Unexpected radio object")
        value = {"domain": domain, "enabled": enabled == "1"}
        if band in radios and radios[band] != value:
            raise ValueError("Conflicting radio reports")
        radios[band] = value
    if set(radios) != {"2.4GHz", "5GHz"}:
        raise ValueError("Expected both Wi-Fi bands")
    if len({radio["domain"] for radio in radios.values()}) != 2:
        raise ValueError("Radio objects must be distinct")
    return radios


class TokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "onttoken":
            self.tokens.append(fields.get("value", ""))


def page_token(source):
    parser = TokenParser()
    parser.feed(source)
    if len(parser.tokens) != 1 or not re.fullmatch(r"[A-Za-z0-9]{8,256}", parser.tokens[0]):
        raise ValueError("Missing or invalid authenticated page token")
    return parser.tokens[0]


def schedule_slot(now, on_at="06:10", off_at="23:15"):
    def minutes(value):
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("Schedule times must be HH:MM")
        hours, minute = map(int, value.split(":"))
        return hours * 60 + minute

    on_minute, off_minute = minutes(on_at), minutes(off_at)
    if on_minute >= off_minute:
        raise ValueError("Expected daytime on period")
    if now.tzinfo is None:
        raise ValueError("A timezone-aware clock is required")
    local = now.astimezone(CHINA_TIME)
    minute = local.hour * 60 + local.minute
    enabled = on_minute <= minute < off_minute
    anchor = local.date()
    if minute < on_minute:
        anchor -= timedelta(days=1)
    boundary = on_at if enabled else off_at
    return enabled, f"{anchor.isoformat()}T{boundary}+08:00"


class ModemClient:
    def __init__(self, config):
        self.config = config
        self.host = config.get("host", "fe80::1")
        if not ipaddress.IPv6Address(self.host).is_link_local:
            raise ValueError("Only directly connected IPv6 link-local modems are supported")
        self.interface = config["interface"]
        self.cookies = {"Cookie": "body:Language:chinese:id=-1", "MenuJumpIndex": "0"}

    def _connect(self, endpoint, timeout=8, source_address=None):
        stream = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        stream.settimeout(timeout)
        try:
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode() + b"\0")
            stream.connect((self.host, 80, 0, socket.if_nametoindex(self.interface)))
            return stream
        except BaseException:
            stream.close()
            raise

    def request_raw(self, method, path, body=None, extra_headers=None):
        origin = f"http://[{self.host}]"
        connection = http.client.HTTPConnection(self.host, 80, timeout=8)
        connection._create_connection = self._connect
        headers = {
            "Host": f"[{self.host}]",
            "Referer": origin + "/CU.HTML",
            "Origin": origin,
            "User-Agent": "Mozilla/5.0",
            "Connection": "close",
            "Cookie": "; ".join(f"{name}={value}" for name, value in self.cookies.items()),
        }
        if extra_headers:
            if any(name.lower() in {"host", "cookie", "connection", "content-length"} for name in extra_headers):
                raise ValueError("Transport-owned headers cannot be overridden")
            headers.update(extra_headers)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(1_000_001)
            if len(payload) > 1_000_000:
                raise RuntimeError(f"Modem HTTP request failed: {response.status}")
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    cookie_name, cookie_value = value.split(";", 1)[0].split("=", 1)
                    if re.fullmatch(r"[A-Za-z0-9_]+", cookie_name.strip()):
                        self.cookies[cookie_name.strip()] = cookie_value.strip()
            return response.status, response.getheaders(), payload
        finally:
            connection.close()

    def request(self, method, path, data=None):
        body = None if data is None else urllib.parse.urlencode(data)
        headers = {} if data is None else {"Content-Type": "application/x-www-form-urlencoded"}
        status, response_headers, payload = self.request_raw(method, path, body, headers)
        if status != 200:
            raise RuntimeError(f"Modem HTTP request failed: {status}")
        return payload.decode("utf-8-sig", errors="replace")

    def login(self):
        self.request("GET", "/CU.HTML")
        token = self.request("POST", "/asp/GetRandCount.asp", {}).strip()
        if not re.fullmatch(r"[A-Za-z0-9]{8,256}", token):
            raise RuntimeError("Invalid login token; refusing credential submission")
        response = self.request("POST", "/login.cgi", {
            "UserName": self.config["username"],
            "PassWord": base64.b64encode(self.config["password"].encode()).decode(),
            "Language": "chinese",
            "x.X_HW_Token": token,
        })
        if not re.search(r"pageName\s*=\s*['\"]CtcApp/preindex\.asp['\"]", response):
            raise RuntimeError("Modem login was not confirmed")
        device = decode_js(self.request("GET", DEVICE_PAGE))
        if EXPECTED_MODEL not in device or EXPECTED_FIRMWARE not in device:
            raise RuntimeError("Unverified modem model or firmware; refusing control")

    def radios(self):
        return parse_radios(self.request("GET", RADIO_LIST))

    def set_enabled(self, enabled, force=False):
        original = self.radios()
        for band in ("2.4GHz", "5GHz"):
            if not force and self.radios()[band]["enabled"] == enabled:
                continue
            source = self.request("GET", WLAN_PAGE + "??" + ("2G" if band == "2.4GHz" else "5G"))
            if not re.search(r"var\s+HiLinkRoll\s*=\s*['\"]1['\"]", source):
                raise RuntimeError("Unverified Wi-Fi control mode")
            domain = original[band]["domain"]
            query = urllib.parse.urlencode({
                "x": "InternetGatewayDevice.X_HW_DEBUG.AMP.SetWifiCoverEnable",
                "y": domain,
                "RequestFile": WLAN_PAGE.lstrip("/"),
            }, safe="/.")
            self.request("POST", "/set.cgi?" + query, {
                "x.Enable": int(enabled),
                "x.RadioInst": int(domain.rsplit(".", 1)[1]),
                "y.Enable": int(enabled),
                "x.X_HW_Token": page_token(source),
            })
            for attempt in range(5):
                time.sleep(2)
                if self.radios()[band]["enabled"] == enabled:
                    break
            else:
                raise RuntimeError(f"Wi-Fi state readback failed: {band}")
        actual = self.radios()
        if any(radio["enabled"] != enabled for radio in actual.values()):
            raise RuntimeError("Both Wi-Fi bands were not confirmed")
        return actual


def needs_reconcile(previous, slot, boot_id, epoch):
    if previous.get("slot") != slot or previous.get("boot_id") != boot_id:
        return True
    if previous.get("confirmed") is True:
        return False
    return not 0 <= epoch - previous.get("attempted_at", 0) < 300


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(state, output)
    temporary.replace(path)


def run(args):
    import fcntl

    config_path = Path(args.config)
    if config_path.stat().st_mode & 0o077:
        raise ValueError("Private configuration must be accessible only to its owner")
    config = json.loads(config_path.read_text())
    state_path = Path(config.get("state_path", "/tmp/modem-wifi-schedule-state.json"))
    with open("/var/lock/modem-wifi-schedule.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = datetime.now(CHINA_TIME)
        if now.year < 2024:
            raise ValueError("Router clock is not synchronized")
        enabled, slot = schedule_slot(now, config.get("on_at", "06:10"), config.get("off_at", "23:15"))
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if args.command == "reconcile":
            try:
                previous = json.loads(state_path.read_text())
            except (OSError, ValueError):
                previous = {}
            if not needs_reconcile(previous, slot, boot_id, now.timestamp()):
                return
        client = ModemClient(config)
        state = {"slot": slot, "boot_id": boot_id, "attempted_at": now.timestamp(), "confirmed": False}
        try:
            client.login()
            if args.command == "status":
                print(json.dumps({"radios": client.radios(), "on_at": config.get("on_at", "06:10"), "off_at": config.get("off_at", "23:15")}))
                return
            if args.command == "set":
                enabled = args.value == "on"
            actual = client.set_enabled(enabled, args.force)
            if args.command == "reconcile":
                state["confirmed"] = True
                save_state(state_path, state)
            print(json.dumps({"time": now.isoformat(), "enabled": enabled, "confirmed": True, "radios": actual}))
        except Exception:
            if args.command == "reconcile":
                save_state(state_path, state)
            raise


def main():
    parser = argparse.ArgumentParser(description="Schedule the verified modem's two Wi-Fi radios without changing router Wi-Fi")
    parser.add_argument("--config", required=True)
    parser.add_argument("command", choices=("status", "reconcile", "set"))
    parser.add_argument("value", nargs="?", choices=("on", "off"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command == "set" and args.value is None:
        parser.error("set requires on or off")
    try:
        run(args)
    except Exception as error:
        print(f"modem-wifi-schedule failed ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
