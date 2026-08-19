import unittest

from lanmihome_cuktech.protocol import (
    decode_protocol_extend,
    extract_hw_protocols,
    parse_port_push,
    parse_port_value,
    set_protocol_switch,
)


class ProtocolTests(unittest.TestCase):
    def test_parse_normal_usb_a_qc(self):
        pt = bytes.fromhex("0f200500040102040004500170146e")
        r = parse_port_push(pt)
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "A")
        self.assertEqual(r.status_raw, 0x01)
        self.assertAlmostEqual(r.voltage, 11.0)
        self.assertAlmostEqual(r.current, 2.0)
        self.assertEqual(r.protocol_hint, "QC")
        self.assertFalse(r.shared)

    def test_parse_c3_a_shared(self):
        pt = bytes.fromhex("0f200c000401020300045011302033")
        r = parse_port_push(pt)
        self.assertIsNotNone(r)
        self.assertEqual(r.name, "C3+A")
        self.assertEqual(r.status_raw, 0x11)
        self.assertTrue(r.shared)
        self.assertEqual(r.shared_group, "c3+a")
        self.assertAlmostEqual(r.voltage, 5.1)
        self.assertAlmostEqual(r.current, 3.2)
        self.assertAlmostEqual(r.power, 16.32)

    def test_active_get_value_layout(self):
        # [status=1, code=0x70, current=0x15, voltage=0x6e]
        r = parse_port_value(4, int.from_bytes(bytes.fromhex("0170156e"), "little"))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.voltage, 11.0)
        self.assertAlmostEqual(r.current, 2.1)
        self.assertEqual(r.protocol_hint, "QC")

    def test_hardware_protocol_overrides_heuristic(self):
        pt = bytes.fromhex("0f20050004010201000450010b0479")
        r = parse_port_push(pt, hw_protocol=10)
        self.assertEqual(r.protocol_hint, "UFCS")
        self.assertEqual(r.protocol_number, 10)
        self.assertEqual(r.protocol_source, "hardware")

    def test_extract_hw_protocols(self):
        value = 0x07000800
        self.assertEqual(extract_hw_protocols(17, value), {1: 7, 2: 8})
        self.assertEqual(extract_hw_protocols(18, value), {3: 7, 4: 8})

    def test_protocol_extend_roundtrip_switches(self):
        value = 0
        value = set_protocol_switch(value, "c1", "pd", True)
        value = set_protocol_switch(value, "c1", "pps", True)
        value = set_protocol_switch(value, "c3", "scp", True)
        switches = decode_protocol_extend(value)
        self.assertTrue(switches["c1"]["pd"])
        self.assertTrue(switches["c1"]["pps"])
        self.assertTrue(switches["c3"]["scp"])
        self.assertFalse(switches["a"]["ufcs"])
        # Reserved bits for C1/C2 are retained/fixed on write.
        self.assertTrue(value & (1 << 3))
        self.assertTrue(value & (1 << 11))


if __name__ == "__main__":
    unittest.main()
