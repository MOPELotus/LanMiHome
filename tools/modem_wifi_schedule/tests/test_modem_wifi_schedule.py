from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("modem_wifi_schedule", Path(__file__).parents[1] / "modem_wifi_schedule.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def radio_state(first=True, second=True):
    return {
        "2.4GHz": {"domain": module.RADIO_PREFIX + "1", "enabled": first},
        "5GHz": {"domain": module.RADIO_PREFIX + "2", "enabled": second},
    }


PAGE = "var HiLinkRoll = '1'; <input name='onttoken' id='hwonttoken' value='ExampleToken123'>"


class ScheduleTests(unittest.TestCase):
    def test_daily_boundaries(self):
        cases = [("06:09", False), ("06:10", True), ("23:14", True), ("23:15", False), ("00:00", False)]
        for clock, expected in cases:
            with self.subTest(clock=clock):
                now = datetime.fromisoformat("2026-10-24T" + clock + ":00+08:00")
                self.assertEqual(module.schedule_slot(now)[0], expected)

    def test_overnight_uses_previous_off_boundary(self):
        now = datetime.fromisoformat("2026-10-25T02:00:00+08:00")
        self.assertEqual(module.schedule_slot(now), (False, "2026-10-24T23:15+08:00"))

    def test_utc_clock_converts_to_china_time(self):
        now = datetime(2026, 10, 24, 15, 15, tzinfo=timezone.utc)
        self.assertFalse(module.schedule_slot(now)[0])

    def test_invalid_schedule_is_rejected(self):
        now = datetime.now(timezone.utc)
        for on_at, off_at in [("6:10", "23:15"), ("06:60", "23:15"), ("23:15", "06:10"), ("06:10", "06:10")]:
            with self.assertRaises(ValueError):
                module.schedule_slot(now, on_at, off_at)

    def test_new_boot_or_boundary_is_not_skipped(self):
        previous = {"slot": "old", "boot_id": "boot", "confirmed": True}
        self.assertTrue(module.needs_reconcile(previous, "new", "boot", 1000))
        self.assertTrue(module.needs_reconcile(previous, "old", "new-boot", 1000))
        self.assertFalse(module.needs_reconcile(previous, "old", "boot", 1000))

    def test_failure_retries_after_five_minutes(self):
        previous = {"slot": "slot", "boot_id": "boot", "confirmed": False, "attempted_at": 1000}
        self.assertFalse(module.needs_reconcile(previous, "slot", "boot", 1299))
        self.assertTrue(module.needs_reconcile(previous, "slot", "boot", 1300))
        self.assertTrue(module.needs_reconcile(previous, "slot", "boot", 900))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = module.ModemClient({"interface": "example-wan", "username": "example-user", "password": "example-password"})

    def test_radio_parser_accepts_escaped_and_repeated_entries(self):
        source = r'new stRadio("InternetGatewayDevice\x2eLANDevice\x2e1\x2eWiFi\x2eRadio\x2e1","2\x2e4GHz","1"),new stRadio("InternetGatewayDevice.LANDevice.1.WiFi.Radio.2","5GHz","0")'
        self.assertEqual(module.parse_radios(source + source), radio_state(True, False))

    def test_incomplete_or_conflicting_radio_reports_fail_closed(self):
        for source in ["", 'new stRadio("unexpected","2.4GHz","1")', 'new stRadio("InternetGatewayDevice.LANDevice.1.WiFi.Radio.1","2.4GHz","1")']:
            with self.assertRaises(ValueError):
                module.parse_radios(source)

    def test_token_uses_name_not_mismatched_dom_id(self):
        self.assertEqual(module.page_token(PAGE), "ExampleToken123")
        for source in ["", PAGE + PAGE, "<input name='onttoken' value='bad token'>"]:
            with self.assertRaises(ValueError):
                module.page_token(source)

    def test_login_visits_administrator_entry_before_token(self):
        self.client.request = MagicMock(side_effect=["", "ExampleToken123", "var pageName = 'CtcApp/preindex.asp';", module.EXPECTED_MODEL + module.EXPECTED_FIRMWARE])
        self.client.login()
        calls = self.client.request.call_args_list
        self.assertEqual(calls[0].args, ("GET", "/CU.HTML"))
        self.assertEqual(calls[1].args, ("POST", "/asp/GetRandCount.asp", {}))
        self.assertEqual(calls[2].args[2]["UserName"], "example-user")

    def test_invalid_token_never_submits_credentials(self):
        self.client.request = MagicMock(side_effect=["", "<html>Waiting...</html>"])
        with self.assertRaises(RuntimeError):
            self.client.login()
        self.assertEqual(self.client.request.call_count, 2)

    def test_wrong_firmware_is_rejected(self):
        self.client.request = MagicMock(side_effect=["", "ExampleToken123", "var pageName='CtcApp/preindex.asp';", "other-device"])
        with self.assertRaises(RuntimeError):
            self.client.login()

    def test_http_strips_bom_and_preserves_vendor_cookie(self):
        response = MagicMock(status=200)
        response.read.return_value = b"\xef\xbb\xbfExampleToken123"
        response.getheaders.return_value = [("Set-Cookie", "Cookie=body:Language:chinese:id=7;path=/")]
        with patch.object(module.http.client, "HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response
            self.assertEqual(self.client.request("POST", "/asp/GetRandCount.asp", {}), "ExampleToken123")
        self.assertEqual(self.client.cookies["Cookie"], "body:Language:chinese:id=7")

    def test_nonlocal_management_target_is_rejected(self):
        with self.assertRaises(ValueError):
            module.ModemClient({"host": "2001:db8::1", "interface": "example-wan"})

    @patch.object(module.time, "sleep")
    def test_two_radio_writes_only_change_enable_fields(self, sleep):
        self.client.radios = MagicMock(side_effect=[radio_state(), radio_state(False, True), radio_state(False, False), radio_state(False, False)])
        self.client.request = MagicMock(side_effect=[PAGE, "ok", PAGE, "ok"])
        self.assertEqual(self.client.set_enabled(False, force=True), radio_state(False, False))
        writes = [call.args for call in self.client.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(writes), 2)
        for index, call in enumerate(writes, 1):
            self.assertTrue(call[1].startswith("/set.cgi?"))
            self.assertIn("SetWifiCoverEnable", call[1])
            self.assertEqual(call[2], {"x.Enable": 0, "x.RadioInst": index, "y.Enable": 0, "x.X_HW_Token": "ExampleToken123"})

    def test_already_matching_state_does_not_write(self):
        self.client.radios = MagicMock(return_value=radio_state())
        self.client.request = MagicMock()
        self.client.set_enabled(True)
        self.client.request.assert_not_called()

    @patch.object(module.time, "sleep")
    def test_missing_readback_is_not_success(self, sleep):
        self.client.radios = MagicMock(return_value=radio_state())
        self.client.request = MagicMock(side_effect=[PAGE, "ok"])
        with self.assertRaises(RuntimeError):
            self.client.set_enabled(False, force=True)


if __name__ == "__main__":
    unittest.main()
