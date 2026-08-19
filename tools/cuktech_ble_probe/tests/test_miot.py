import unittest

from lanmihome_cuktech.miot import MiotCommandClient


class MiotCodecTests(unittest.TestCase):
    def test_build_get(self):
        data = MiotCommandClient._build_tlv(1, 2, 4)
        self.assertEqual(data.hex(), "0c2001000201020400011000")

    def test_build_uint8_set(self):
        data = MiotCommandClient._build_tlv(3, 2, 16, 0x0F)
        self.assertEqual(data[4], 0x00)
        self.assertEqual(data[-1], 0x0F)
        self.assertEqual(int.from_bytes(data[9:11], "little") >> 12, 1)

    def test_build_uint32_set(self):
        data = MiotCommandClient._build_tlv(3, 2, 21, 0x12345678)
        self.assertEqual(data[-4:], bytes.fromhex("78563412"))
        self.assertEqual(int.from_bytes(data[9:11], "little") >> 12, 5)

    def test_extract_get_uint32(self):
        pt = bytes.fromhex("112004000301020400000004500170156e")
        value = MiotCommandClient._extract_get_value(pt)
        self.assertEqual(value.to_bytes(4, "little"), bytes.fromhex("0170156e"))


if __name__ == "__main__":
    unittest.main()
