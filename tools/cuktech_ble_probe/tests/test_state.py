import unittest

from lanmihome_cuktech.protocol import parse_property_push
from lanmihome_cuktech.state import ChargerState


class StateTests(unittest.TestCase):
    def test_real_c3a_shared_trace_uses_hardware_protocol_and_no_double_count(self):
        state = ChargerState()

        # Real PIID 18 push captured from the charger's C3+A transition.
        prop = parse_property_push(bytes.fromhex("0f201400040102120004500a020a02"))
        self.assertIsNotNone(prop)
        self.assertEqual(prop.piid, 18)
        state.apply_setting(prop.piid, prop.value)
        self.assertEqual(state.hw_protocols[3], 2)
        self.assertEqual(state.hw_protocols[4], 2)

        # A had a previous standalone reading; shared C3+A must suppress it from total.
        a = state.decode_port_push(bytes.fromhex("0f2012000401020400045001601133"))
        self.assertIsNotNone(a)
        self.assertGreater(a.power, 0)

        shared = state.decode_port_push(bytes.fromhex("0f200b000401020300045011302033"))
        self.assertIsNotNone(shared)
        self.assertTrue(shared.shared)
        self.assertEqual(shared.name, "C3+A")
        self.assertEqual(shared.protocol_hint, "5V")
        self.assertEqual(shared.protocol_source, "hardware")
        self.assertAlmostEqual(state.total_power(), shared.power)

        snapshot = state.snapshot()
        self.assertTrue(snapshot["c3a_shared"])
        self.assertEqual(snapshot["ports"]["a"]["measurement_suppressed_by"], "c3+a")

    def test_c3_leaving_shared_mode_restores_independent_total(self):
        state = ChargerState()
        state.decode_port_push(bytes.fromhex("0f200b000401020300045011302033"))
        self.assertTrue(state.c3a_shared)
        state.decode_port_push(bytes.fromhex("0f200b000401020300045000300000"))
        self.assertFalse(state.c3a_shared)


if __name__ == "__main__":
    unittest.main()
