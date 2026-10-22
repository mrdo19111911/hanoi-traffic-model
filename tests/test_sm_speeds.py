"""Offline tests for the speedmatrix base speed layer (free-flow source, speed rules, clamp, version, storage)."""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from model.spatial import MissingRoadData
from speedmatrix.rules import Rule
from speedmatrix.scopes import ScopeResolver
from speedmatrix.segments import ROAD_CLASSES, SegmentTable
from speedmatrix.speeds import (FERRY_KMH, FreeFlowSource, build_speed_layer, load_layer, save_layer,
                                speed_version)
from speedmatrix.timeslots import DAYTYPES

# ways: (name, highway, class, maxspeed)
WAYS = [("Đại lộ P", "primary", "chinh", np.nan), ("Đại lộ Q", "primary", "chinh", 60.0),
        ("Phà", "ferry", "pha", np.nan), ("Phố A", "residential", "noido", np.nan),
        ("Ngõ B", "residential", "nho", np.nan)]


def build_table():
    strings = [w[0] for w in WAYS] + ["primary", "residential", "ferry"]
    hw = {"primary": 5, "residential": 6, "ferry": 7}
    nw = len(WAYS)
    way = np.repeat(np.arange(nw, dtype=np.int32), 2)
    n = way.size
    lat = np.full(n, 21.0, dtype=np.float32)
    lon = (105.8 + np.arange(n) * 0.01).astype(np.float32)
    nan = np.full(nw, np.nan, dtype=np.float32)
    return SegmentTable(
        a=np.arange(n, dtype=np.int64), b=np.arange(1, n + 1, dtype=np.int64), way=way,
        a_lat=lat, a_lon=lon, b_lat=lat, b_lon=lon + np.float32(0.005),
        length_m=np.full(n, 500.0, dtype=np.float32),
        way_id=np.arange(100, 100 + nw, dtype=np.int64),
        highway=np.array([hw[w[1]] for w in WAYS], dtype=np.int32),
        road_class=np.array([ROAD_CLASSES.index(w[2]) for w in WAYS], dtype=np.int8),
        oneway=np.zeros(nw, dtype=np.int8), name=np.arange(nw, dtype=np.int32),
        ref=np.full(nw, -1, dtype=np.int32), maxspeed_kmh=np.array([w[3] for w in WAYS], dtype=np.float32),
        maxweight_t=nan.copy(), maxheight_m=nan.copy(), hgv=np.zeros(nw, dtype=np.int8),
        cond_hgv=np.full(nw, -1, dtype=np.int32), cond_maxweight=np.full(nw, -1, dtype=np.int32),
        strings=strings, meta={"pbf_sha256": "x"})


class SpeedLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = build_table()
        first = lambda w: int(np.flatnonzero(cls.t.way == w)[0])
        cls.primary, cls.max60, cls.ferry, cls.pho_a, cls.ngo_b = (first(i) for i in range(5))
        lon0 = float(cls.t.a_lon[cls.primary]) - 0.001
        lon1 = float(cls.t.b_lon[cls.primary + 1]) + 0.001
        cls.box = {"type": "Polygon", "coordinates": [[[lon0, 20.9], [lon1, 20.9], [lon1, 21.1],
                                                        [lon0, 21.1], [lon0, 20.9]]]}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_free_flow_layer_is_static_and_uses_maxspeed(self):
        L = build_speed_layer(self.t, FreeFlowSource(), [], ScopeResolver(self.t))
        self.assertEqual(L.dyn_idx["weekday"].size, 0)
        self.assertEqual(int(L.at("weekday", 17)[self.primary]), 65); self.assertEqual(int(L.at("sun", 0)[self.max60]), 48)
        self.assertEqual(int(L.at("sat", 3)[self.ferry]), FERRY_KMH)

    def test_pct_then_set_only_in_their_slots_and_classes(self):
        rules = [Rule("p", "speed_pct", "edit", {"scope": {"polygon": self.box}, "classes": ["chinh"], "pct": -50, "when": {"daytypes": ["weekday"], "times": [["07:00", "08:00"]]}}),
                 Rule("s", "speed_set", "edit", {"scope": {"names": ["Phố A"]}, "kmh": 12})]
        L = build_speed_layer(self.t, FreeFlowSource(), rules, ScopeResolver(self.t))
        row = L.rows("weekday", np.array([self.primary]))[0]
        self.assertEqual(int(row[14]), 32); self.assertEqual(int(row[13]), 65); self.assertEqual(int(L.at("sat", 14)[self.primary]), 65)
        self.assertTrue((L.rows("sun", np.array([self.pho_a]))[0] == 12).all())
        self.assertEqual(L.rule_ids[L.rule_offsets[self.primary]:L.rule_offsets[self.primary + 1]], ["p"])
        self.assertEqual(L.stats["dynamic_segments"]["weekday"], int(L.dyn_idx["weekday"].size))

    def test_clamp_is_counted_and_zero_never_appears(self):
        rules = [Rule("z", "speed_pct", "edit", {"scope": {"names": ["Ngõ B"]}, "pct": -99.9})]
        L = build_speed_layer(self.t, FreeFlowSource(), rules, ScopeResolver(self.t))
        self.assertEqual(int(L.at("weekday", 0)[self.ngo_b]), 3); self.assertGreater(L.stats["clamped"]["nho"], 0)
        self.assertTrue(all((L.base[d] > 0).all() for d in DAYTYPES))

    def test_missing_speed_raises(self):
        class Holey(FreeFlowSource):
            id = "holey"
            def speeds(self, table, daytype):
                v = super().speeds(table, daytype); v[0, 5] = np.nan; return v
        with self.assertRaises(MissingRoadData): build_speed_layer(self.t, Holey(), [], ScopeResolver(self.t))

    def test_version_changes_with_active_speed_rules_only(self):
        v0 = speed_version(self.t, FreeFlowSource(), [])
        ban = Rule("b", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"names": ["Phố A"]}})
        self.assertEqual(speed_version(self.t, FreeFlowSource(), [ban]), v0)
        s = Rule("s", "speed_set", "edit", {"scope": {"names": ["Phố A"]}, "kmh": 12})
        self.assertNotEqual(speed_version(self.t, FreeFlowSource(), [s]), v0)

    def test_inactive_rule_and_orphans(self):
        off = Rule("o", "speed_set", "edit", {"scope": {"names": ["Phố A"]}, "kmh": 12}, active=False)
        ghost = Rule("g", "speed_set", "edit", {"scope": {"names": ["Không có"]}, "kmh": 12})
        L = build_speed_layer(self.t, FreeFlowSource(), [off, ghost], ScopeResolver(self.t))
        self.assertEqual(int(L.at("sun", 0)[self.pho_a]), 25)
        self.assertEqual(L.stats["orphans"]["g"], [{"name": "Không có"}]); self.assertNotIn("o", L.stats["orphans"])

    def test_round_trip_and_ferry_speed_matches_profile(self):
        rules = [Rule("s", "speed_set", "edit", {"scope": {"names": ["Phố A"]}, "kmh": 12, "when": {"times": [["07:00", "08:00"]]}})]
        L = build_speed_layer(self.t, FreeFlowSource(), rules, ScopeResolver(self.t)); save_layer(L, self.root / "L")
        save_layer(L, self.root / "L")
        M = load_layer(self.root / "L"); self.assertEqual(M.version, L.version); np.testing.assert_array_equal(M.base["sat"], L.base["sat"])
        np.testing.assert_array_equal(M.rows("weekday", np.array([self.pho_a])), L.rows("weekday", np.array([self.pho_a])))
        self.assertEqual(M.rule_ids, L.rule_ids); np.testing.assert_array_equal(M.rule_offsets, L.rule_offsets)
        self.assertEqual(M.stats, L.stats); self.assertEqual(M.n, L.n)
        profile = Path(__file__).resolve().parents[1] / "osrm/tools/profiles/car.lua"
        if not profile.is_file():
            self.skipTest("osrm/tools/profiles/car.lua is absent")
        found = re.search(r"route_speeds\s*=\s*\{[^}]*?\bferry\s*=\s*(\d+)", profile.read_text(encoding="utf-8"), re.S)
        self.assertEqual(int(found.group(1)), FERRY_KMH)


if __name__ == "__main__":
    unittest.main()
