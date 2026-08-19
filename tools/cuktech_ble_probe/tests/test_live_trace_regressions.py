import unittest

from lanmihome_cuktech.protocol import parse_property_push
from lanmihome_cuktech.state import ChargerState


class LiveTraceRegressionTests(unittest.TestCase):
    def test_real_desk_piid17_marks_c1_as_pd(self):
        state = ChargerState()
        # status run: PIID17 = 0x07240000, then C1 = 12.1 V / 0.4 A.
        state.apply_setting(17, 0x07240000)
        reading = state.decode_port_push(bytes.fromhex("0f20150004010201000450010b0479"))
        self.assertIsNotNone(reading)
        self.assertEqual(reading.protocol_hint, "PD")
        self.assertEqual(reading.protocol_source, "hardware")

    def test_real_piid18_sequence_maps_c3_and_a_protocol_bytes(self):
        state = ChargerState()
        # Real transition values from the 2026-08-19 trace.
        for frame, expected in (
            ("0f201500040102120004500a020000", {4: 2}),
            ("0f201f00040102120004500a020a02", {3: 2, 4: 2}),
            ("0f202000040102120004500a020a01", {3: 1, 4: 2}),
        ):
            prop = parse_property_push(bytes.fromhex(frame))
            self.assertIsNotNone(prop)
            self.assertEqual(prop.piid, 18)
            state.apply_setting(prop.piid, prop.value)
            for port, proto in expected.items():
                self.assertEqual(state.hw_protocols[port], proto)

    def test_true_idle_clears_stale_hardware_protocol(self):
        state = ChargerState()
        state.apply_setting(18, 0x0000020A)  # A reports protocol 2 (5V).
        self.assertEqual(state.hw_protocols[4], 2)

        transition = state.decode_port_push(bytes.fromhex("0f2014000401020400045001600000"))
        self.assertIsNotNone(transition)
        self.assertEqual(transition.protocol_source, "hardware")

        idle = state.decode_port_push(bytes.fromhex("0f2015000401020400045000600000"))
        self.assertIsNotNone(idle)
        self.assertFalse(idle.active)
        self.assertEqual(idle.protocol_hint, "idle")
        self.assertNotIn(4, state.hw_protocols)

        # If a fresh port frame arrives before PIID18 catches up, it must not reuse
        # the previous session's protocol code; fallback detection is safer.
        new_session = state.decode_port_push(bytes.fromhex("0f2016000401020400045001600033"))
        self.assertIsNotNone(new_session)
        self.assertEqual(new_session.protocol_source, "heuristic")
        self.assertEqual(new_session.protocol_hint, "5V")


if __name__ == "__main__":
    unittest.main()
