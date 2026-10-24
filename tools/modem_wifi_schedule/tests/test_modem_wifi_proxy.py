from contextlib import nullcontext
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).parents[1]))
import modem_wifi_proxy as proxy


class AccessTests(unittest.TestCase):
    def allowed(self, **changes):
        arguments = dict(peer="10.0.0.123", host="lotusnet.lan:18081", origin="", referer="", fetch_site="same-origin", method="GET", path="/index.asp", networks=["10.0.0.0/24"], hosts=["lotusnet.lan", "10.0.0.1"], port=18081)
        arguments.update(changes)
        return proxy.allowed_request(**arguments)

    def test_main_lan_is_allowed_but_guest_and_wan_are_denied(self):
        self.assertTrue(self.allowed())
        for peer in ("192.168.31.10", "10.184.53.3", "203.0.113.1"):
            self.assertFalse(self.allowed(peer=peer))

    def test_foreign_hosts_and_wrong_ports_are_denied(self):
        for host in ("attacker.example:18081", "lotusnet.lan:80", "user@lotusnet.lan:18081", "lotusnet.lan:18081/path"):
            self.assertFalse(self.allowed(host=host))

    def test_post_requires_matching_origin_or_referer(self):
        self.assertFalse(self.allowed(method="POST"))
        self.assertTrue(self.allowed(method="POST", origin="http://lotusnet.lan:18081"))
        self.assertTrue(self.allowed(method="POST", referer="http://lotusnet.lan:18081/index.asp"))
        self.assertFalse(self.allowed(method="POST", origin="http://attacker.example"))

    def test_cross_site_get_and_absolute_targets_are_denied(self):
        self.assertFalse(self.allowed(fetch_site="cross-site"))
        for path in ("http://attacker.example/", "//attacker.example/", "/\r\nInjected"):
            self.assertFalse(self.allowed(path=path))


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.factory = MagicMock(return_value=self.client)
        self.backend = proxy.Backend({}, self.factory, nullcontext)

    def test_entry_logs_in_once_and_redirects_without_credentials(self):
        self.assertEqual(self.backend.forward("GET", "/", None, {}), (302, [("Location", "/CtcApp/preindex.asp")], b""))
        self.backend.forward("GET", "/CU.HTML", None, {})
        self.client.login.assert_called_once()
        self.client.request_raw.assert_not_called()

    def test_static_bytes_pass_through_without_browser_cookies(self):
        expected = (200, [("Content-Type", "image/png")], b"\x89PNG\xff\x00")
        self.client.request_raw.return_value = expected
        self.assertEqual(self.backend.forward("GET", "/images/logo.png", None, {"Cookie": "untrusted", "Host": "untrusted", "Accept": "image/*"}), expected)
        self.client.request_raw.assert_called_once_with("GET", "/images/logo.png", None, {"Accept": "image/*"})

    def test_expired_get_reauthenticates_once(self):
        self.client.request_raw.side_effect = [(403, [], b"Waiting..."), (200, [], b"device info")]
        self.assertEqual(self.backend.forward("GET", "/index.asp", None, {})[2], b"device info")
        self.assertEqual(self.client.login.call_count, 2)

    def test_expired_post_is_never_replayed(self):
        self.client.request_raw.return_value = (403, [], b"Waiting...")
        with self.assertRaises(proxy.SessionExpired):
            self.backend.forward("POST", "/set.cgi", b"example=value", {})
        self.client.request_raw.assert_called_once()
        self.assertIsNone(self.backend.client)

    def test_login_failures_back_off(self):
        self.client.login.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.backend.forward("GET", "/", None, {})
        with self.assertRaises(proxy.SessionExpired):
            self.backend.forward("GET", "/", None, {})
        self.client.login.assert_called_once()

    def test_normal_cgi_return_page_is_not_mistaken_for_logout(self):
        self.assertFalse(proxy.logged_out(200, b"var pageName='/html/amp/wlanbasic/wlan.asp';"))
        self.assertTrue(proxy.logged_out(200, b"var pageName='/';"))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.ProxyHandler)
        self.server.proxy_settings = {"allowed_networks": ["127.0.0.0/8"], "allowed_hosts": ["localhost"]}
        self.server.backend = MagicMock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        fields = {"Host": "localhost:" + str(self.server.server_port)}
        fields.update(headers or {})
        connection.request(method, path, body, fields)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_binary_body_and_no_backend_cookie_leak(self):
        self.server.backend.forward.return_value = (200, [("Content-Type", "image/png"), ("Set-Cookie", "private-session=example")], b"\x89PNG\x00\xff")
        status, headers, body = self.request("GET", "/image.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"\x89PNG\x00\xff")
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_foreign_host_never_reaches_backend(self):
        self.assertEqual(self.request("GET", "/", {"Host": "attacker.example"})[0], 403)
        self.server.backend.forward.assert_not_called()

    def test_cross_origin_post_never_reaches_backend(self):
        self.assertEqual(self.request("POST", "/set.cgi", {"Origin": "http://attacker.example"}, b"value=1")[0], 403)
        self.server.backend.forward.assert_not_called()

    def test_unavailable_backend_has_sanitized_error(self):
        self.server.backend.forward.side_effect = RuntimeError("example-private-credential")
        status, headers, body = self.request("GET", "/index.asp")
        self.assertEqual(status, 503)
        self.assertNotIn(b"example-private-credential", body)


if __name__ == "__main__":
    unittest.main()
