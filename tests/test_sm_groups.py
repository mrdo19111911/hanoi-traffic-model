"""Offline tests for per-group views: caps, closures and the speeds a group sees."""
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from speedmatrix.groups import build_group_view, group_caps, group_speeds, load_group_view, save_group_view
from speedmatrix.rules import Rule, rule_set_sha
from speedmatrix.scopes import ScopeResolver
from speedmatrix.segments import ROAD_CLASSES, SegmentTable
from speedmatrix.speeds import ReleaseMismatch, build_speed_layer
from speedmatrix.timeslots import DAYTYPES, SLOTS
from speedmatrix.vehicles import VehicleGroup

DUOI2T = VehicleGroup("duoi2t", 1.9, 1.0, 2, "normal", 2.6, 4.5, is_base=True)
TU2T = VehicleGroup("tu2t", 8.0, 5.0, 2, "normal", 3.0, 7.0)
CONTAINER = VehicleGroup("container", 40.0, 28.0, 5, "container", 4.2, 16.0)

LAT0, LON0 = 21.0, 105.8
KX = 111320.0 * math.cos(math.radians(LAT0))
CLASS_KMH = {"caotoc": 100.0, "quoclo": 70.0, "chinh": 60.0, "noido": 40.0, "nho": 25.0, "pha": 5.0}


def ll(x, y):
    """(lat, lon) of a point given in metres east (x) and north (y) of the origin."""
    return LAT0 + y / 111320.0, LON0 + x / KX


RING = ["Vành đai A", "Vành đai B", "Vành đai C", "Vành đai D"]
BBOX = [ll(-1000, -1000)[1], ll(-1000, -1000)[0], ll(3000, 3000)[1], ll(3000, 3000)[0]]
# ways: (name, class, polyline in metres); the ring encloses a 2 km square around "Phố Trong"
WAYS = [("Vành đai A", "chinh", [(0, 0), (1000, 0), (2000, 0)]),
        ("Vành đai B", "chinh", [(2000, 0), (2000, 1000), (2000, 2000)]),
        ("Vành đai C", "chinh", [(2000, 2000), (1000, 2000), (0, 2000)]),
        ("Vành đai D", "chinh", [(0, 2000), (0, 1000), (0, 0)]),
        ("Phố Trong", "noido", [(800, 1000), (1200, 1000)]),
        ("Cao tốc X", "caotoc", [(-800, -800), (2800, -800)]),
        ("Ngõ Ngoài", "noido", [(2500, 2500), (2800, 2500)])]


def build_table():
    nodes, segs = {}, []
    for wi, (_, _, pts) in enumerate(WAYS):
        for p, q in zip(pts, pts[1:]):
            a = nodes.setdefault(p, len(nodes) + 1)
            b = nodes.setdefault(q, len(nodes) + 1)
            segs.append((a, b, wi, ll(*p), ll(*q)))
    nw, n = len(WAYS), len(segs)
    nan = np.full(nw, np.nan, dtype=np.float32)
    pa = np.array([s[3] for s in segs])
    pb = np.array([s[4] for s in segs])
    return SegmentTable(
        a=np.array([s[0] for s in segs], dtype=np.int64), b=np.array([s[1] for s in segs], dtype=np.int64),
        way=np.array([s[2] for s in segs], dtype=np.int32),
        a_lat=pa[:, 0].astype(np.float32), a_lon=pa[:, 1].astype(np.float32),
        b_lat=pb[:, 0].astype(np.float32), b_lon=pb[:, 1].astype(np.float32),
        length_m=np.full(n, 100.0, dtype=np.float32),
        way_id=np.arange(100, 100 + nw, dtype=np.int64), highway=np.full(nw, -1, dtype=np.int32),
        road_class=np.array([ROAD_CLASSES.index(w[1]) for w in WAYS], dtype=np.int8),
        oneway=np.zeros(nw, dtype=np.int8), name=np.arange(nw, dtype=np.int32),
        ref=np.full(nw, -1, dtype=np.int32), maxspeed_kmh=nan.copy(), maxweight_t=nan.copy(),
        maxheight_m=nan.copy(), hgv=np.zeros(nw, dtype=np.int8), cond_hgv=np.full(nw, -1, dtype=np.int32),
        cond_maxweight=np.full(nw, -1, dtype=np.int32), strings=[w[0] for w in WAYS], meta={"pbf_sha256": "x"})


class ClassSource:
    """A fixed speed per road class, the same in every slot."""
    id = "test_class_speeds:v1"

    def speeds(self, table, daytype):
        kmh = np.array([CLASS_KMH[c] for c in ROAD_CLASSES], dtype=np.float32)
        return np.repeat(kmh[table.segment_class][:, None], SLOTS, axis=1)


def cap(rule_id, vehicle, classes, active=True):
    return Rule(id=rule_id, kind="cap", layer="official", active=active,
                body={"vehicle": vehicle, "road_classes": classes})


CAP_HEAVY = cap("cap_heavy", {"gross_t": [">=", 2]},
                {"caotoc": 90, "quoclo": 80, "chinh": 70, "noido": 50, "nho": 40})
CAP_CONTAINER = cap("cap_container", {"kind": ["in", ["container"]]}, {"caotoc": 80, "quoclo": 75})
CAP_OFF = cap("cap_off", {"gross_t": [">=", 2]}, {"caotoc": 30}, active=False)
ZONE_BAN = Rule(id="zone_ban", kind="ban", layer="official", body={
    "vehicle": {"gross_t": [">=", 2]}, "action": "close",
    "when": {"daytypes": ["weekday"], "times": [["06:00", "21:00"]]},
    "scope": {"zone": {"boundary_names": RING, "seed": list(ll(1000, 1400)), "area_km2": [1, 10]},
              "bbox": BBOX}})
# closes the inner street for small trucks only (the Hải Phòng KV1/KV2 inversion)
SMALL_ONLY_BAN = Rule(id="small_only_ban", kind="ban", layer="official", body={
    "vehicle": {"gross_t": ["<", 2]}, "action": "close", "when": {"times": [["06:00", "21:00"]]},
    "scope": {"names": ["Phố Trong"], "bbox": BBOX}})
# closes ring road A for every group at all times
ALL_BAN = Rule(id="all_ban", kind="ban", layer="official", body={
    "vehicle": {}, "action": "close", "scope": {"names": ["Vành đai A"], "bbox": BBOX}})
# a speed rule that makes the outer lane faster than the heavy cap on Saturday evening only
FAST_EVENING = Rule(id="fast_evening", kind="speed_set", layer="edit", body={
    "kmh": 65, "when": {"daytypes": ["sat"], "times": [["18:00", "19:00"]]},
    "scope": {"names": ["Ngõ Ngoài"]}})


class GroupViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = build_table()
        first = lambda w: int(np.flatnonzero(cls.t.way == w)[0])
        cls.ring_a, cls.inner_street, cls.motorway, cls.outer_lane = first(0), first(4), first(5), first(6)
        cls.residential = cls.inner_street
        cls.rules = [CAP_HEAVY, CAP_CONTAINER, CAP_OFF]
        cls.rules_with_zone_ban = cls.rules + [ZONE_BAN]
        cls.L = build_speed_layer(cls.t, ClassSource(), [FAST_EVENING], ScopeResolver(cls.t))

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_cap_binds_only_where_base_is_faster(self):
        v = build_group_view(self.t, self.L, CONTAINER, self.rules, ScopeResolver(self.t))
        self.assertEqual(v.caps["caotoc"], 80)
        self.assertIn(self.motorway, v.capped_idx.tolist())
        self.assertNotIn(self.residential, v.capped_idx.tolist())
        self.assertEqual(int(group_speeds(v, self.L, self.t, "weekday", 10)[self.motorway]), 80)
        self.assertEqual(int(group_speeds(v, self.L, self.t, "weekday", 10)[self.residential]), 40)
        self.assertEqual(v.speed_version, self.L.version)

    def test_caps_are_the_minimum_of_matching_active_rules(self):
        self.assertEqual(group_caps(CONTAINER, self.rules),
                         {"caotoc": 80.0, "quoclo": 75.0, "chinh": 70.0, "noido": 50.0, "nho": 40.0})
        self.assertEqual(group_caps(TU2T, self.rules)["caotoc"], 90.0)    # cap_off is inactive
        self.assertEqual(group_caps(DUOI2T, self.rules), {})

    def test_cap_binding_in_one_slot_only_counts(self):
        v = build_group_view(self.t, self.L, TU2T, self.rules, ScopeResolver(self.t))
        self.assertIn(self.outer_lane, v.capped_idx.tolist())             # 65 > 50 on Saturday evening
        self.assertEqual(int(group_speeds(v, self.L, self.t, "sat", 36)[self.outer_lane]), 50)
        self.assertEqual(int(group_speeds(v, self.L, self.t, "sat", 10)[self.outer_lane]), 40)
        self.assertNotIn(self.ring_a, v.capped_idx.tolist())               # 60 < 70

    def test_closed_slot_is_zero_and_base_group_untouched(self):
        v = build_group_view(self.t, self.L, TU2T, self.rules_with_zone_ban, ScopeResolver(self.t))
        s = group_speeds(v, self.L, self.t, "weekday", 14)
        self.assertEqual(int(s[self.inner_street]), 0)
        self.assertGreater(int(group_speeds(v, self.L, self.t, "weekday", 44)[self.inner_street]), 0)
        self.assertIn(self.inner_street, v.affected_idx.tolist())
        self.assertIn("zone_ban", v.report["rules_applied"])
        b = build_group_view(self.t, self.L, DUOI2T, self.rules, ScopeResolver(self.t))
        self.assertEqual(b.affected_idx.size, 0)
        np.testing.assert_array_equal(group_speeds(b, self.L, self.t, "weekday", 14), self.L.at("weekday", 14))

    def test_affected_is_capped_union_closed(self):
        v = build_group_view(self.t, self.L, CONTAINER, self.rules_with_zone_ban, ScopeResolver(self.t))
        np.testing.assert_array_equal(v.affected_idx, np.union1d(v.capped_idx, v.closures.idx))
        self.assertEqual(v.affected_idx.dtype, np.int64)

    def test_rules_sha_hashes_the_ban_and_cap_rules(self):
        v = build_group_view(self.t, self.L, TU2T, self.rules_with_zone_ban, ScopeResolver(self.t))
        self.assertEqual(v.rules_sha, rule_set_sha(self.rules_with_zone_ban))
        night = Rule(id="zone_ban", kind="ban", layer="official", body=dict(
            ZONE_BAN.body, when={"daytypes": ["weekday"], "times": [["21:00", "06:00"]]}))
        w = build_group_view(self.t, self.L, TU2T, self.rules + [night], ScopeResolver(self.t))
        self.assertNotEqual(w.rules_sha, v.rules_sha)
        # speed rules belong to the speed layer's version, not to the view's rule hash
        x = build_group_view(self.t, self.L, TU2T, self.rules_with_zone_ban + [FAST_EVENING], ScopeResolver(self.t))
        self.assertEqual(x.rules_sha, v.rules_sha)

    def _brute_affected(self, view, base):
        """Segments where the group's speeds differ from the base group's in some slot of some day type."""
        out = np.zeros(self.t.n, dtype=bool)
        for daytype in DAYTYPES:
            for slot in range(SLOTS):
                out |= group_speeds(view, self.L, self.t, daytype, slot) != group_speeds(base, self.L, self.t, daytype, slot)
        return np.flatnonzero(out)

    def test_non_base_affected_is_measured_against_the_base_view(self):
        rules = self.rules + [SMALL_ONLY_BAN, ALL_BAN]
        res = ScopeResolver(self.t)
        base = build_group_view(self.t, self.L, DUOI2T, rules, res)
        # the base group's own set stays against the bare layer
        np.testing.assert_array_equal(base.affected_idx, np.union1d(base.capped_idx, base.closures.idx))
        self.assertIsNone(base.base)
        self.assertIn(self.inner_street, base.closures.idx.tolist())
        for group in (TU2T, CONTAINER):
            v = build_group_view(self.t, self.L, group, rules, res, base=base)
            self.assertEqual(v.base, "duoi2t")
            np.testing.assert_array_equal(v.affected_idx, self._brute_affected(v, base))
            self.assertEqual(v.affected_idx.dtype, np.int64)
            # closed for the base group only: the group differs although it has no closure there
            self.assertIn(self.inner_street, v.affected_idx.tolist())
            self.assertNotIn(self.inner_street, v.closures.idx.tolist())
            # closed for both in every slot, uncapped: the base group's pieces are right for the group
            self.assertIn(self.ring_a, v.closures.idx.tolist())
            self.assertNotIn(self.ring_a, v.affected_idx.tolist())
            self.assertIn(self.motorway, v.affected_idx.tolist())          # capped for the group only

    def test_base_view_must_be_the_base_group_on_the_same_layer(self):
        res = ScopeResolver(self.t)
        tu2t = build_group_view(self.t, self.L, TU2T, self.rules, res)
        with self.assertRaisesRegex(ValueError, "base"):
            build_group_view(self.t, self.L, CONTAINER, self.rules, res, base=tu2t)
        other = build_speed_layer(self.t, ClassSource(), [], res)
        stale = build_group_view(self.t, other, DUOI2T, self.rules, res)
        with self.assertRaises(ReleaseMismatch):
            build_group_view(self.t, self.L, CONTAINER, self.rules, res, base=stale)

    def test_group_speeds_refuses_a_view_of_another_layer(self):
        res = ScopeResolver(self.t)
        other = build_speed_layer(self.t, ClassSource(), [], res)
        self.assertNotEqual(other.version, self.L.version)
        v = build_group_view(self.t, self.L, TU2T, self.rules, res)
        with self.assertRaisesRegex(ReleaseMismatch, other.version[:12]):
            group_speeds(v, other, self.t, "sat", 36)
        self.assertTrue(issubclass(ReleaseMismatch, ValueError))

    def test_round_trip(self):
        v = build_group_view(self.t, self.L, CONTAINER, self.rules_with_zone_ban, ScopeResolver(self.t))
        save_group_view(v, self.root / "c.npz")
        w = load_group_view(self.root / "c.npz")
        np.testing.assert_array_equal(w.affected_idx, v.affected_idx)
        np.testing.assert_array_equal(w.capped_idx, v.capped_idx)
        np.testing.assert_array_equal(w.closures.idx, v.closures.idx)
        np.testing.assert_array_equal(w.closures.mask, v.closures.mask)
        self.assertEqual(w.caps, v.caps)
        self.assertEqual(w.group, v.group)
        self.assertEqual(w.speed_version, v.speed_version)
        self.assertEqual(w.rules_sha, v.rules_sha)
        self.assertEqual(w.report, v.report)
        self.assertIsNone(w.base)
        np.testing.assert_array_equal(group_speeds(w, self.L, self.t, "weekday", 14),
                                      group_speeds(v, self.L, self.t, "weekday", 14))
        base = build_group_view(self.t, self.L, DUOI2T, self.rules_with_zone_ban, ScopeResolver(self.t))
        x = build_group_view(self.t, self.L, CONTAINER, self.rules_with_zone_ban, ScopeResolver(self.t), base=base)
        save_group_view(x, self.root / "x.npz")
        y = load_group_view(self.root / "x.npz")
        self.assertEqual(y.base, "duoi2t")
        np.testing.assert_array_equal(y.affected_idx, x.affected_idx)

    def test_old_view_format_is_refused(self):
        v = build_group_view(self.t, self.L, CONTAINER, self.rules, ScopeResolver(self.t))
        save_group_view(v, self.root / "c.npz")
        with np.load(self.root / "c.npz") as z:
            data = dict(z)
        meta = json.loads(str(data["meta"]))
        meta["format"] = 2
        data["meta"] = np.array(json.dumps(meta))
        np.savez_compressed(self.root / "old.npz", **data)
        with self.assertRaisesRegex(ValueError, "format"):
            load_group_view(self.root / "old.npz")


if __name__ == "__main__":
    unittest.main()
