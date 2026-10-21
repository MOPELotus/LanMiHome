import http.client
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'router'))
sys.path.insert(0, str(ROOT / 'tools/cuktech_ble_probe/src'))

import lanmihome
from test_light_strip import FakeTransport


class LightStripHTTPTests(unittest.TestCase):
    def setUp(self):
        self.app = lanmihome.LanMiHomeApp({})
        self.server = lanmihome.LanMiHomeHTTPServer(('127.0.0.1', 0), lanmihome.Handler, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        try:
            connection.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_state_without_configuration(self):
        status, state = self.request('GET', '/api/v1/light-strip')
        self.assertEqual(status, 200)
        self.assertEqual(state['status'], 'not_configured')
        self.assertIsNone(state['power'])

    def test_disabled_command_returns_503(self):
        status, body = self.request('PATCH', '/api/v1/light-strip', {"power": True})
        self.assertEqual(status, 503)
        self.assertEqual(body['error'], 'not_configured')

    def test_unverified_protocol_returns_501(self):
        self.app.light_strip = lanmihome.LightStripAdapter({"enabled": True, "address": "AA:BB:CC:DD:EE:FF"})
        status, body = self.request('PATCH', '/api/v1/light-strip', {"power": True})
        self.assertEqual(status, 501)
        self.assertEqual(body['error'], 'protocol_pending')

    def test_invalid_command_returns_400(self):
        status, _ = self.request('PATCH', '/api/v1/light-strip', {"brightness": 0})
        self.assertEqual(status, 400)

    def test_confirmed_transport_roundtrip(self):
        self.app.light_strip = lanmihome.LightStripAdapter({"enabled": True, "address": "AA:BB:CC:DD:EE:FF"}, FakeTransport())
        status, state = self.request('PATCH', '/api/v1/light-strip', {"power": True, "brightness": 35})
        self.assertEqual(status, 200)
        self.assertTrue(state['power'])
        self.assertEqual(state['brightness'], 35)

    def test_old_device_routes_remain_available(self):
        for path in ['health', 'fan', 'lamp', 'sensor', 'chargers', 'system/recovery']:
            with self.subTest(path=path):
                status, _ = self.request('GET', '/api/v1/' + path)
                self.assertEqual(status, 200)

    def test_capabilities_distinguish_planned_controls_and_transport(self):
        status, body = self.request('GET', '/api/v1/capabilities')
        self.assertEqual(status, 200)
        self.assertFalse(body['light_strip']['protocol_ready'])
        self.assertEqual(body['light_strip']['controls'], ['power', 'brightness'])

    def test_aggregate_state_contains_light_strip(self):
        status, body = self.request('GET', '/api/v1/state')
        self.assertEqual(status, 200)
        self.assertEqual(body['light_strip']['status'], 'not_configured')


if __name__ == '__main__':
    unittest.main()
