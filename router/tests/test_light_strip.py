from http import HTTPStatus
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from light_strip import LightStripAdapter, LightStripUnavailable


class FakeTransport:
    def __init__(self):
        self.reported = {"power": False, "brightness": 50}
        self.commands = []
        self.reads = 0
        self.fail_read = False
        self.fail_write = False
        self.confirm_write = True

    def read_state(self):
        self.reads += 1
        if self.fail_read:
            raise TimeoutError("test device unreachable")
        return dict(self.reported)

    def set_state(self, changes):
        if self.fail_write:
            raise OSError("test connection lost")
        self.commands.append(dict(changes))
        if self.confirm_write:
            self.reported.update(changes)


class LightStripTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.config = {"enabled": True, "address": "AA:BB:CC:DD:EE:FF", "model": "SP651E"}
        self.adapter = LightStripAdapter(self.config, self.transport)

    def test_absent_config_is_safe_and_unknown_not_off(self):
        state = LightStripAdapter({}).state()
        self.assertEqual(state["name"], "灯带")
        self.assertEqual(state["status"], "not_configured")
        self.assertFalse(state["available"])
        self.assertIsNone(state["power"])
        self.assertIsNone(state["brightness"])

    def test_disabled_device_never_accesses_transport(self):
        adapter = LightStripAdapter(dict(self.config, enabled=False), self.transport)
        self.assertEqual(adapter.state()["status"], "disabled")
        with self.assertRaises(LightStripUnavailable) as error:
            adapter.patch({"power": True})
        self.assertEqual(error.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(self.transport.reads, 0)
        self.assertEqual(self.transport.commands, [])

    def test_missing_address_never_accesses_transport(self):
        adapter = LightStripAdapter({"enabled": True}, self.transport)
        self.assertEqual(adapter.state()["status"], "not_configured")
        self.assertEqual(self.transport.reads, 0)

    def test_config_does_not_enable_an_unverified_protocol(self):
        adapter = LightStripAdapter(self.config)
        self.assertEqual(adapter.state()["status"], "protocol_pending")
        self.assertFalse(adapter.state()["control_ready"])
        with self.assertRaises(LightStripUnavailable) as error:
            adapter.patch({"power": True})
        self.assertEqual(error.exception.status, HTTPStatus.NOT_IMPLEMENTED)

    def test_config_is_not_mutated(self):
        original = dict(self.config)
        self.adapter.state()
        self.assertEqual(self.config, original)

    def test_address_is_normalized(self):
        adapter = LightStripAdapter({"address": "aa-bb-cc-dd-ee-ff"})
        self.assertEqual(adapter.address, "AA:BB:CC:DD:EE:FF")

    def test_bad_config_rejected(self):
        for config in [None, [], {"enabled": "false"}, {"model": "SP631E"}, {"address": "unknown"}]:
            with self.subTest(config=config), self.assertRaises(ValueError):
                LightStripAdapter(config)

    def test_only_single_color_controls_are_exposed(self):
        self.assertEqual(self.adapter.capabilities()["controls"], ["power", "brightness"])

    def test_reported_state_is_used(self):
        state = self.adapter.state()
        self.assertEqual(state["status"], "ready")
        self.assertFalse(state["power"])
        self.assertEqual(state["brightness"], 50)

    def test_power_write_is_confirmed(self):
        state = self.adapter.patch({"power": True})
        self.assertTrue(state["power"])
        self.assertEqual(self.transport.commands, [{"power": True}])

    def test_brightness_does_not_implicitly_power_on(self):
        state = self.adapter.patch({"brightness": 25})
        self.assertFalse(state["power"])
        self.assertEqual(self.transport.commands, [{"brightness": 25}])

    def test_brightness_boundaries(self):
        for value in [1, 100]:
            self.assertEqual(self.adapter.patch({"brightness": value})["brightness"], value)

    def test_invalid_commands_rejected_before_any_write(self):
        for command in [{}, {"power": 1}, {"power": "on"}, {"brightness": True}, {"brightness": 0}, {"brightness": 101}, {"brightness": 50.5}, {"brightness": "50"}, {"rgb": [255, 0, 0]}, {"color_temperature": 4000}, {"power": True, "brightness": 999}]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.adapter.patch(command)
        self.assertEqual(self.transport.commands, [])

    def test_unconfirmed_command_is_not_reported_successful(self):
        self.transport.confirm_write = False
        with self.assertRaisesRegex(LightStripUnavailable, "unconfirmed"):
            self.adapter.patch({"power": True})

    def test_failed_write_is_unavailable(self):
        self.transport.fail_write = True
        with self.assertRaises(LightStripUnavailable) as error:
            self.adapter.patch({"power": True})
        self.assertEqual(error.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

    def test_read_failure_does_not_expose_stale_state(self):
        self.adapter.patch({"power": True})
        self.transport.fail_read = True
        state = self.adapter.state()
        self.assertEqual(state["status"], "offline")
        self.assertFalse(state["available"])
        self.assertIsNone(state["power"])

    def test_missing_reported_fields_are_not_online(self):
        self.transport.reported = {"power": True}
        self.assertFalse(self.adapter.state()["available"])


if __name__ == "__main__":
    unittest.main()
