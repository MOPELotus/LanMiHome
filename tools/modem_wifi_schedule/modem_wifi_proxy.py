from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import threading
import time
import urllib.parse

from modem_wifi_schedule import ModemClient


LOGIN_PATHS = {"/", "/cu.html", "/login.asp", "/login.cgi"}
SAFE_RESPONSE_HEADERS = {"content-type", "content-disposition", "location"}
FORWARD_HEADERS = {"accept", "content-type", "range", "x-requested-with"}


class SessionExpired(RuntimeError):
    pass


def allowed_request(peer, host, origin, referer, fetch_site, method, path, networks, hosts, port):
    try:
        address = ipaddress.ip_address(peer)
        if not any(address in ipaddress.ip_network(network) for network in networks):
            return False
        target = urllib.parse.urlsplit("http://" + host)
        if target.username or target.password or target.path or target.query or target.fragment:
            return False
        if target.hostname not in hosts or target.port != port:
            return False
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            return False
        if fetch_site == "cross-site":
            return False
        expected = "http://" + host.lower()
        if origin and origin.lower() != expected:
            return False
        if method != "GET":
            previous = urllib.parse.urlsplit(referer)
            referer_origin = previous.scheme + "://" + previous.netloc.lower()
            if not origin and referer_origin != expected:
                return False
        return True
    except ValueError:
        return False


def logged_out(status, body):
    if status in (401, 403):
        return True
    text = body[:32768].decode("utf-8-sig", errors="replace")
    return bool(
        ("superUserInfo" in text and "txt_Password" in text)
        or re.search(r"pageName\s*=\s*['\"](?:/|/?login\.asp|/?CU\.HTML)['\"]", text, re.IGNORECASE)
    )


@contextmanager
def radio_gate():
    import fcntl

    with open("/var/lock/modem-wifi-schedule.lock", "w") as lock:
        until = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError("Scheduled modem operation is busy")
                time.sleep(0.1)
        yield


class Backend:
    def __init__(self, config, client_factory=ModemClient, gate=radio_gate):
        self.config = config
        self.client_factory = client_factory
        self.gate = gate
        self.client = None
        self.next_login_at = 0.0
        self.lock = threading.Lock()

    def _login(self):
        if time.monotonic() < self.next_login_at:
            raise SessionExpired("Automatic login is backing off")
        try:
            client = self.client_factory(self.config)
            client.login()
        except Exception:
            self.client = None
            self.next_login_at = time.monotonic() + 30
            raise
        self.client = client
        self.next_login_at = 0.0

    def forward(self, method, path, body, headers):
        with self.lock, self.gate():
            if self.client is None:
                self._login()
            if urllib.parse.urlsplit(path).path.lower() in LOGIN_PATHS:
                return 302, [("Location", "/CtcApp/preindex.asp")], b""
            forwarded = {name: value for name, value in headers.items() if name.lower() in FORWARD_HEADERS}
            result = self.client.request_raw(method, path, body, forwarded)
            if not logged_out(result[0], result[2]):
                return result
            self.client = None
            if method != "GET":
                raise SessionExpired("Reload the page before retrying; the submitted operation was not replayed")
            self._login()
            result = self.client.request_raw(method, path, None, forwarded)
            if logged_out(result[0], result[2]):
                self.client = None
                raise SessionExpired("Automatic login did not restore the session")
            return result


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def respond(self, status, headers, body):
        self.send_response(status)
        for name, value in headers:
            if name.lower() in SAFE_RESPONSE_HEADERS:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def handle_proxy(self):
        settings = self.server.proxy_settings
        if not allowed_request(
            self.client_address[0], self.headers.get("Host", ""),
            self.headers.get("Origin", ""), self.headers.get("Referer", ""),
            self.headers.get("Sec-Fetch-Site", ""), self.command, self.path,
            settings["allowed_networks"], settings["allowed_hosts"], self.server.server_port,
        ):
            self.respond(403, [("Content-Type", "text/plain; charset=utf-8")], b"Main LAN access only; untrusted hosts and cross-origin requests are denied.\n")
            return
        if self.headers.get("Transfer-Encoding"):
            self.respond(400, [], b"Transfer encoding is not supported.\n")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > 2_000_000:
            self.respond(413, [], b"Invalid or oversized request body.\n")
            return
        self.connection.settimeout(15)
        try:
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                raise ValueError("Incomplete request body")
            result = self.server.backend.forward(self.command, self.path, body, self.headers)
        except SessionExpired:
            self.respond(409, [("Content-Type", "text/plain; charset=utf-8")], b"Modem session expired. Reload this page, then retry your operation. No POST was replayed.\n")
            return
        except Exception as error:
            print(f"modem-wifi-proxy request failed ({type(error).__name__})", flush=True)
            self.respond(503, [("Content-Type", "text/plain; charset=utf-8")], b"Modem is unavailable or busy. Retry shortly.\n")
            return
        self.respond(*result)

    def do_GET(self):
        self.handle_proxy()

    def do_POST(self):
        self.handle_proxy()


def main():
    parser = argparse.ArgumentParser(description="Main-LAN-only automatically authenticated modem Web proxy")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    path = Path(args.config)
    if path.stat().st_mode & 0o077:
        raise ValueError("Proxy credentials must be accessible only to their owner")
    config = json.loads(path.read_text())
    settings = config["web_proxy"]
    bind = ipaddress.IPv4Address(settings["bind"])
    if bind.is_unspecified or not bind.is_private:
        raise ValueError("An explicit private LAN bind address is required")
    if not settings.get("allowed_hosts") or not settings.get("allowed_networks"):
        raise ValueError("Explicit main-LAN and hostname allowlists are required")
    server = ThreadingHTTPServer((str(bind), int(settings["port"])), ProxyHandler)
    server.daemon_threads = True
    server.proxy_settings = settings
    server.backend = Backend(config)
    print("modem-wifi-proxy started with LAN and origin access controls", flush=True)
    server.serve_forever(poll_interval=1)


if __name__ == "__main__":
    main()
