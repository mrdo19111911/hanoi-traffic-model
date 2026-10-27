import sys
import unittest
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "buildings"))

from housenumber_interp import clean_side, lead_address, lis_keep, number_of_object, number_side, parse_number, street_key  # noqa: E402


class StreetKeyTests(unittest.TestCase):
    def test_prefix_and_accents_dropped(self):
        self.assertEqual(street_key("Phố Huế"), ("hue", ()))
        self.assertEqual(street_key("Đường Thiên Nga 6"), ("thien nga 6", ()))

    def test_alley_paths(self):
        self.assertEqual(street_key("Ngõ 49 Thúy Lĩnh"), ("thuy linh", (49,)))
        self.assertEqual(street_key("Ngách 421/4 Hoàng Quốc Việt"), ("hoang quoc viet", (421, 4)))
        self.assertEqual(street_key("Ngách 124 Ngõ 20 Phú Minh"), ("phu minh", (20, 124)))
        self.assertIsNone(street_key("Ngõ 10"))


class AddressTextTests(unittest.TestCase):
    def test_number_leading_the_street(self):
        self.assertEqual(lead_address("73 ngõ 77 bùi xương trạch,"), (("bui xuong trach", (77,)), 73, ""))
        self.assertEqual(lead_address("73/1 ngõ 77 Bùi Xương Trạch"), (("bui xuong trach", (77, 73)), 1, ""))
        self.assertEqual(lead_address("Số 12, Ngõ 85, Sài Đồng, Long Biên, Hà Nội"), (("sai dong", (85,)), 12, ""))
        self.assertEqual(lead_address("số 3, ngách 4, ngõ 20, Phú Minh"), (("phu minh", (20, 4)), 3, ""))
        self.assertIsNone(lead_address("125 - 135 Nguyễn Trãi"))
        self.assertIsNone(lead_address("Cafe 24h"))

    def test_object_rules(self):
        base = dict(hn=None, street=None, full=None, housename=None, place=None, name=None)
        self.assertEqual(number_of_object({**base, "hn": "5", "street": "Ngõ 49 Thúy Lĩnh"}),
                         (("thuy linh", (49,)), 5, "", "housenumber+street"))
        self.assertEqual(number_of_object({**base, "street": "73/1 ngõ 77 bùi xương trạch"})[3], "number_in_street")
        self.assertEqual(number_of_object({**base, "name": "Cafe 24h"}), (None, "no_number"))


class NumberTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_number("7A"), ((), 7, "A"))
        self.assertEqual(parse_number("6/1"), ((6,), 1, ""))
        self.assertEqual(parse_number("125-135"), "range")
        self.assertEqual(parse_number("TT5-N7"), "other")

    def test_lis(self):
        self.assertEqual([int(i) for i in lis_keep([1, 5, 3, 7, 9, 8, 11])], [0, 2, 3, 5, 6])

    def test_conflicts_dropped(self):
        anc = [(0, 1, 1, "", "osm"), (10, 1, 3, "", "osm"), (20, 1, 4, "", "osm"), (30, 1, 99, "", "osm"), (40, 1, 9, "", "osm")]
        c = Counter()
        s, n = clean_side(anc, 1, 1, c)
        self.assertEqual(list(n), [1, 3, 9])
        self.assertEqual((c["dropped_parity"], c["dropped_order"]), (1, 1))

    def test_interpolation_parity_monotone_and_suffix(self):
        c = Counter()
        hs = np.array([5.0, 10.0, 15.0, 20.0, 21.0, 22.0])
        got = number_side(hs, np.array([0.0, 30.0]), np.array([1, 7]), 1, 1, 0.3, c)
        nums = [g[0] for g in got]
        self.assertTrue(all(n % 2 == 1 for n in nums))
        self.assertEqual(nums, sorted(nums))
        self.assertTrue(all(1 < n < 7 for n in nums))
        self.assertIn("A", [g[1] for g in got])
        self.assertTrue(all(g[2] == "interp" for g in got))

    def test_extrapolation_limited(self):
        c = Counter()
        got = number_side(np.array([40.0, 500.0]), np.array([0.0, 30.0]), np.array([2, 10]), 0, 1, 0.3, c)
        self.assertEqual(got[0][2], "extrap")
        self.assertIsNone(got[1])
        self.assertEqual(c["beyond_extrap"], 1)


if __name__ == "__main__":
    unittest.main()
