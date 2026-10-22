"""Offline tests for per-group closures (OSM tags, official and company ban rules)."""
import unittest

import numpy as np

from speedmatrix.bans import HGV_GROSS_T, ClosureSet, group_closures, osm_closures
from speedmatrix.rules import Rule
from speedmatrix.scopes import ScopeResolver
from speedmatrix.segments import HGV_VALUES, ROAD_CLASSES, SegmentTable
from speedmatrix.vehicles import VehicleGroup

DUOI2T = VehicleGroup("duoi2t", 1.9, 1.0, 2, "normal", 2.6, 4.5)
TU2T = VehicleGroup("tu2t", 8.0, 5.0, 2, "normal", 3.0, 7.0)
CONTAINER = VehicleGroup("container", 40.0, 28.0, 5, "container", 4.2, 16.0)
PICKUP_4T = VehicleGroup("pickup", 4.0, 1.0, 2, "pickup", 2.0, 5.5)

NAMES = ["Phố A", "Hgv No", "Mw10", "Mh35", "Mwcond5", "Bad", "Hgv Night", "Hgv Cond No",
         "Mw Garbage", "Hgv Garbage"]


def build_table():
    nw = len(NAMES)
    strings = list(NAMES) + ["5 @ (06:00-09:00)", "garbage text", "yes @ (21:00-06:00)",
                             "no @ (06:00-09:00)", "none @ (06:00-09:00)", "maybe @ (06:00-09:00)"]
    base = len(NAMES)
    nan = np.full(nw, np.nan, dtype=np.float32)
    maxweight, maxheight = nan.copy(), nan.copy()
    hgv = np.zeros(nw, dtype=np.int8)
    cond_hgv = np.full(nw, -1, dtype=np.int32)
    cond_mw = np.full(nw, -1, dtype=np.int32)
    hgv[1] = HGV_VALUES.index("no")
    maxweight[2] = 10.0
    maxheight[3] = 3.5
    cond_mw[4] = base
    cond_mw[5] = base + 1
    hgv[6] = HGV_VALUES.index("no")
    cond_hgv[6] = base + 2
    cond_hgv[7] = base + 3
    cond_mw[8] = base + 4
    cond_hgv[9] = base + 5
    way = np.repeat(np.arange(nw, dtype=np.int32), 2)
    n = way.size
    lat = np.full(n, 21.0, dtype=np.float32)
    lon = (105.8 + np.arange(n) * 0.001).astype(np.float32)
    return SegmentTable(
        a=np.arange(n, dtype=np.int64), b=np.arange(1, n + 1, dtype=np.int64), way=way,
        a_lat=lat, a_lon=lon, b_lat=lat, b_lon=lon + np.float32(0.0005),
        length_m=np.full(n, 50.0, dtype=np.float32),
        way_id=np.arange(100, 100 + nw, dtype=np.int64), highway=np.zeros(nw, dtype=np.int32),
        road_class=np.full(nw, ROAD_CLASSES.index("noido"), dtype=np.int8),
        oneway=np.zeros(nw, dtype=np.int8), name=np.arange(nw, dtype=np.int32),
        ref=np.full(nw, -1, dtype=np.int32), maxspeed_kmh=nan.copy(), maxweight_t=maxweight,
        maxheight_m=maxheight, hgv=hgv, cond_hgv=cond_hgv, cond_maxweight=cond_mw,
        strings=strings, meta={})


class BansTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = build_table()
        first = lambda w: int(np.flatnonzero(cls.t.way == w)[0])
        cls.pho_a, cls.hgv_no, cls.mw10 = first(0), first(1), first(2)
        cls.mh35, cls.mwcond5, cls.badcond = first(3), first(4), first(5)
        cls.hgv_night, cls.hgv_cond_no = first(6), first(7)
        cls.mw_garbage, cls.hgv_garbage = first(8), first(9)

    def test_light_truck_ignores_hgv_and_weight_limits_it_meets(self):
        c, _ = osm_closures(self.t, DUOI2T)
        self.assertEqual(c.idx.size, 0)

    def test_heavy_truck_closed_by_hgv_weight_height(self):
        c, _ = osm_closures(self.t, CONTAINER)
        self.assertTrue({self.hgv_no, self.mw10, self.mh35} <= set(c.idx.tolist()))
        self.assertTrue(c.mask[c.idx.tolist().index(self.mw10)].all())
        self.assertEqual(HGV_GROSS_T, 3.5)
        self.assertEqual(c.idx.dtype, np.int64)
        self.assertTrue(np.all(np.diff(c.idx) > 0))

    def test_conditional_weight_only_in_its_window(self):
        c, _ = osm_closures(self.t, TU2T)
        m = c.mask[c.idx.tolist().index(self.mwcond5)]
        self.assertEqual(np.flatnonzero(m[0]).tolist(), [12, 13, 14, 15, 16, 17])

    def test_unparsed_conditional_is_reported_not_applied(self):
        c, rep = osm_closures(self.t, CONTAINER)
        self.assertEqual(rep["unparsed_conditional"], 3)
        self.assertNotIn(self.badcond, c.idx.tolist())
        self.assertEqual(sorted(rep["examples"]),
                         ["garbage text", "maybe @ (06:00-09:00)", "none @ (06:00-09:00)"])

    def test_non_numeric_maxweight_part_is_counted_not_applied(self):
        c, rep = osm_closures(self.t, CONTAINER)
        self.assertNotIn(self.mw_garbage, c.idx.tolist())
        self.assertIn("none @ (06:00-09:00)", rep["examples"])

    def test_unknown_hgv_conditional_value_is_counted_not_applied(self):
        c, rep = osm_closures(self.t, CONTAINER)
        self.assertNotIn(self.hgv_garbage, c.idx.tolist())
        self.assertIn("maybe @ (06:00-09:00)", rep["examples"])

    def test_hgv_conditional_yes_opens_night_window(self):
        c, _ = osm_closures(self.t, CONTAINER)
        m = c.mask[c.idx.tolist().index(self.hgv_night)]
        for d in range(3):
            self.assertEqual(np.flatnonzero(m[d]).tolist(), list(range(12, 42)))
            self.assertFalse(m[d][42:].any() or m[d][:12].any())

    def test_hgv_conditional_no_closes_only_its_window(self):
        c, _ = osm_closures(self.t, TU2T)
        m = c.mask[c.idx.tolist().index(self.hgv_cond_no)]
        self.assertEqual(np.flatnonzero(m[0]).tolist(), [12, 13, 14, 15, 16, 17])
        c, _ = osm_closures(self.t, DUOI2T)
        self.assertNotIn(self.hgv_cond_no, c.idx.tolist())

    def test_pickup_keeps_height_limit_only(self):
        c, _ = osm_closures(self.t, PICKUP_4T)
        self.assertEqual(c.idx.tolist(), [])

    def test_pickup_is_still_stopped_by_a_low_bridge(self):
        tall_pickup = VehicleGroup("pickup2", 4.0, 1.0, 2, "pickup", 3.8, 5.5)
        c, _ = osm_closures(self.t, tall_pickup)
        self.assertEqual(set(c.idx.tolist()), {self.mh35, self.mh35 + 1})

    def test_closed_at(self):
        c, _ = osm_closures(self.t, TU2T)
        self.assertIn(self.mwcond5, c.closed_at(0, 12).tolist())
        self.assertNotIn(self.mwcond5, c.closed_at(0, 20).tolist())
        self.assertIn(self.mwcond5, c.closed_at(2, 12).tolist())

    def test_empty_masks_are_dropped(self):
        rules = [Rule("o", "ban", "official", {"vehicle": {}, "action": "open", "scope": {"names": ["Phố A"]}})]
        c, _ = group_closures(self.t, DUOI2T, rules, ScopeResolver(self.t))
        self.assertIsInstance(c, ClosureSet)
        self.assertEqual(c.idx.size, 0)

    def test_official_close_then_company_open_wins(self):
        rules = [Rule("o1", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"names": ["Phố A"]}, "when": {"times": [["06:00", "09:00"]]}}),
                 Rule("c1", "ban", "company", {"vehicle": {}, "action": "open", "scope": {"names": ["Phố A"]}, "when": {"times": [["08:00", "09:00"]]}})]
        c, rep = group_closures(self.t, TU2T, rules, ScopeResolver(self.t))
        row = c.mask[c.idx.tolist().index(self.pho_a)][0]
        self.assertEqual(np.flatnonzero(row).tolist(), [12, 13, 14, 15])
        self.assertEqual(rep["rules_applied"], ["o1", "c1"])

    def test_open_wins_inside_one_layer_and_vehicle_cond_filters(self):
        rules = [Rule("a", "ban", "official", {"vehicle": {"gross_t": [">=", 2]}, "action": "close", "scope": {"names": ["Phố A"]}}),
                 Rule("b", "ban", "official", {"vehicle": {}, "action": "open", "scope": {"names": ["Phố A"]}, "when": {"times": [["00:00", "06:00"]]}})]
        c, _ = group_closures(self.t, DUOI2T, rules, ScopeResolver(self.t))
        self.assertNotIn(self.pho_a, c.idx.tolist())
        c, _ = group_closures(self.t, TU2T, rules, ScopeResolver(self.t))
        self.assertFalse(c.mask[c.idx.tolist().index(self.pho_a)][:, :12].any())

    def test_ignored_rules_and_orphans_and_osm_overwrite(self):
        base = {"vehicle": {}, "action": "close", "scope": {"names": ["Phố A", "Không Có"]}}
        rules = [Rule("x1", "ban", "official", base, active=False),
                 Rule("x2", "cap", "official", {"scope": {"names": ["Phố A"]}}),
                 Rule("x3", "ban", "official", base)]
        c, rep = group_closures(self.t, TU2T, rules, ScopeResolver(self.t))
        self.assertEqual(rep["rules_applied"], ["x3"])
        self.assertEqual(rep["orphans"], {"x3": [{"name": "Không Có"}]})
        self.assertIn(self.pho_a, c.idx.tolist())
        # an official open rule overwrites the OSM hgv=no closure on its segments
        rules = [Rule("o", "ban", "official", {"vehicle": {}, "action": "open", "scope": {"names": ["Hgv No"]}})]
        c, _ = group_closures(self.t, TU2T, rules, ScopeResolver(self.t))
        self.assertNotIn(self.hgv_no, c.idx.tolist())


if __name__ == "__main__":
    unittest.main()
