import unittest
from datetime import date

import numpy as np

from speedmatrix.timeslots import (
    ConditionalParseError, daytype_of, parse_conditional, slot_index,
    times_mask, when_mask,
)


class TimeslotTests(unittest.TestCase):
    def test_slot_index_and_bad_slot(self):
        self.assertEqual(slot_index("0000"), 0)
        self.assertEqual(slot_index("0830"), 17)
        self.assertEqual(slot_index("2330"), 47)
        with self.assertRaises(ValueError):
            slot_index("0815")

    def test_daytype_with_holiday(self):
        hol = {date(2026, 9, 2)}
        self.assertEqual(daytype_of(date(2026, 10, 21), hol), "weekday")
        self.assertEqual(daytype_of(date(2026, 10, 24), hol), "sat")
        self.assertEqual(daytype_of(date(2026, 10, 25), hol), "sun")
        self.assertEqual(daytype_of(date(2026, 9, 2), hol), "sun")

    def test_overlap_closes_partial_slots(self):
        m = times_mask([("06:00", "09:00"), ("16:00", "19:30")])
        self.assertEqual(np.flatnonzero(m).tolist(), list(range(12, 18)) + list(range(32, 39)))
        self.assertEqual(np.flatnonzero(times_mask([("06:10", "06:20")])).tolist(), [12])

    def test_interval_crossing_midnight_wraps(self):
        self.assertEqual(np.flatnonzero(times_mask([("21:00", "06:00")])).tolist(),
                         list(range(0, 12)) + list(range(42, 48)))

    def test_end_at_2400(self):
        self.assertEqual(np.flatnonzero(times_mask([("22:00", "24:00")])).tolist(), [44, 45, 46, 47])

    def test_when_mask_defaults_and_daytypes(self):
        self.assertTrue(when_mask(None).all())
        m = when_mask({"daytypes": ["sat"], "times": [["06:00", "07:00"]]})
        self.assertEqual(m.sum(), 2)
        self.assertTrue(m[1, 12] and m[1, 13])

    def test_parse_conditional_forms(self):
        (value, m), = parse_conditional("no @ (06:00-09:00,16:00-19:30)")
        self.assertEqual(value, "no")
        self.assertTrue(m.all(axis=0)[12] and not m[:, 11].any())
        (value, m), = parse_conditional("10 @ (Mo-Fr 06:00-22:00)")
        self.assertEqual(value, "10")
        self.assertTrue(m[0, 12])
        self.assertFalse(m[1].any() or m[2].any())
        parts = parse_conditional("no @ (Sa,Su,PH 08:00-12:00); 5 @ (22:00-06:00)")
        self.assertEqual([p[0] for p in parts], ["no", "5"])
        self.assertTrue(parts[0][1][2, 16])

    def test_unsupported_conditional_raises(self):
        for bad in ("no @ (sunrise-sunset)", "no @ (weight>7)", "no @ (Jan-Mar 08:00-09:00)", "no"):
            with self.assertRaises(ConditionalParseError):
                parse_conditional(bad)

    def test_when_mask_rejects_unknown_daytype(self):
        with self.assertRaisesRegex(ValueError, "daytype"):
            when_mask({"daytypes": ["weekdays"]})

    def test_when_mask_rejects_unknown_key(self):
        with self.assertRaisesRegex(ValueError, "key"):
            when_mask({"daytypes": ["sat"], "time": [["06:00", "09:00"]]})


if __name__ == "__main__":
    unittest.main()
