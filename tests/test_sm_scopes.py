"""Offline tests for speedmatrix scope resolution (names, routes, admin, polygon, zones).

The fixture is a synthetic SegmentTable built from metre coordinates, without a PBF.
"""
import math
import unittest

import numpy as np

from speedmatrix.geo import haversine_m
from speedmatrix.rules import RuleError
from speedmatrix.scopes import (ScopeResolver, ZoneError, normalize_name, normalize_ref, route_piece,
                                zone_segments)
from speedmatrix.segments import ROAD_CLASSES, SegmentTable, WaterLines

LAT0, LON0 = 21.0, 105.8
KX = 111320.0 * math.cos(math.radians(LAT0))


def ll(x, y):
    """(lat, lon) of a point given in metres east (x) and north (y) of the origin."""
    return LAT0 + y / 111320.0, LON0 + x / KX


def lonlat(x, y):
    lat, lon = ll(x, y)
    return [lon, lat]


def box_geometry(x0, y0, x1, y1):
    return {"type": "Polygon", "coordinates": [[lonlat(x0, y0), lonlat(x1, y0), lonlat(x1, y1),
                                                 lonlat(x0, y1), lonlat(x0, y0)]]}


def steps(start, stop, step=500):
    n = int(round((stop - start) / step))
    return [start + i * step for i in range(n + 1)]


class TableBuilder:
    """Collects ways as metre polylines; ways passing through the same point share a node."""

    def __init__(self):
        self.nodes = {}
        self.strings = []
        self.ways = []        # (way_id, name, ref, class, oneway)
        self.segs = []        # (a, b, way_index, (x1, y1), (x2, y2))

    def _node(self, x, y):
        return self.nodes.setdefault((round(x, 3), round(y, 3)), len(self.nodes) + 1)

    def _string(self, text):
        if not text:
            return -1
        if text not in self.strings:
            self.strings.append(text)
        return self.strings.index(text)

    def way(self, pts, name="", ref="", cls="noido", oneway=0):
        index = len(self.ways)
        self.ways.append((1000 + index, name, ref, cls, oneway))
        for p, q in zip(pts, pts[1:]):
            self.segs.append((self._node(*p), self._node(*q), index, p, q))
        return index

    def build(self):
        strings_highway = self._string("residential")
        a = np.array([s[0] for s in self.segs], dtype=np.int64)
        b = np.array([s[1] for s in self.segs], dtype=np.int64)
        way = np.array([s[2] for s in self.segs], dtype=np.int32)
        pa = np.array([ll(*s[3]) for s in self.segs])
        pb = np.array([ll(*s[4]) for s in self.segs])
        a_lat, a_lon = pa[:, 0].astype(np.float32), pa[:, 1].astype(np.float32)
        b_lat, b_lon = pb[:, 0].astype(np.float32), pb[:, 1].astype(np.float32)
        length = haversine_m(a_lat.astype(float), a_lon.astype(float), b_lat.astype(float),
                             b_lon.astype(float)).astype(np.float32)
        nw = len(self.ways)
        nan = np.full(nw, np.nan, dtype=np.float32)
        return SegmentTable(
            a=a, b=b, way=way, a_lat=a_lat, a_lon=a_lon, b_lat=b_lat, b_lon=b_lon, length_m=length,
            way_id=np.array([w[0] for w in self.ways], dtype=np.int64),
            highway=np.full(nw, strings_highway, dtype=np.int32),
            road_class=np.array([ROAD_CLASSES.index(w[3]) for w in self.ways], dtype=np.int8),
            oneway=np.array([w[4] for w in self.ways], dtype=np.int8),
            name=np.array([self._string(w[1]) for w in self.ways], dtype=np.int32),
            ref=np.array([self._string(w[2]) for w in self.ways], dtype=np.int32),
            maxspeed_kmh=nan.copy(), maxweight_t=nan.copy(), maxheight_m=nan.copy(),
            hgv=np.zeros(nw, dtype=np.int8), cond_hgv=np.full(nw, -1, dtype=np.int32),
            cond_maxweight=np.full(nw, -1, dtype=np.int32), strings=list(self.strings), meta={})

    def seg_ids(self, pred):
        """Segment indices whose (way_index, p, q) satisfies pred."""
        return {i for i, (_, _, w, p, q) in enumerate(self.segs) if pred(w, p, q)}


RING = ["Vành đai A", "Vành đai B", "Vành đai C", "Vành đai D"]
BBOX = [ll(-2000, -2000)[1], ll(-2000, -2000)[0], ll(6000, 6000)[1], ll(6000, 6000)[0]]
SEED = list(ll(2250, 2750))
ZONE = {"boundary_names": RING, "seed": SEED, "area_km2": [5, 30]}
VERTICAL_Y = steps(0, 2500) + [2990, 3010, 3500, 4000]

ADMIN_FIXTURE = [
    {"type": "Feature", "geometry": box_geometry(-1500, -1500, 5800, 5800),
     "properties": {"name": "Tỉnh T", "admin_level": 4, "osm_id": 1}},
    {"type": "Feature", "geometry": box_geometry(2200, 200, 4900, 1800),
     "properties": {"name": "Phường P", "admin_level": 6, "osm_id": 2}},
    # a namesake ward in another province: must not be picked
    {"type": "Feature", "geometry": box_geometry(6500, -1500, 12000, 5800),
     "properties": {"name": "Tỉnh U", "admin_level": 4, "osm_id": 3}},
    {"type": "Feature", "geometry": box_geometry(7000, 0, 11000, 1000),
     "properties": {"name": "Phường P", "admin_level": 6, "osm_id": 4}},
]


def build_fixture(variant=1):
    """variant 1: four named ring roads; variant 2: side C has only ref "QL.9", side D is a river."""
    t = TableBuilder()
    ring = {}
    ring["A"] = t.way([(x, 0) for x in steps(0, 4000)], name="Vành đai A", cls="chinh")
    ring["B"] = t.way([(4000, y) for y in steps(0, 4000)], name="Vành đai B", cls="chinh")
    if variant == 1:
        ring["C"] = t.way([(x, 4000) for x in steps(0, 4000)], name="Vành đai C", cls="chinh")
        ring["D"] = t.way([(0, y) for y in steps(0, 4000)], name="Vành đai D", cls="chinh")
    else:
        ring["C"] = t.way([(x, 4000) for x in steps(0, 4000)], ref="QL.9", cls="quoclo")
    w = {}
    for k, x in ((1, 1000), (2, 2000), (3, 3000)):
        w[f"pho{k}"] = t.way([(x, y) for y in VERTICAL_Y], name=f"Phố {k}")
    w["mien"] = t.way([(x, 1000) for x in steps(0, 4000)], name="Phố Miễn")
    w["giua"] = t.way([(x, 2000) for x in steps(0, 4000)], name="Phố Giữa")
    w["ngo"] = t.way([(x, 1500) for x in steps(1000, 2000)], name="Ngõ 5", cls="nho")
    w["ngoai"] = t.way([(x, -1000) for x in steps(500, 3500)], name="Phố Ngoài")
    w["ngam"] = t.way([(3600, 2250), (3900, 2250), (4100, 2250), (4400, 2250)], name="Phố Ngầm")
    xs = steps(500, 3500)
    w["x_east"] = t.way([(x, 2990) for x in xs], name="Đại lộ X", cls="chinh", oneway=1)
    w["x_west"] = t.way([(x, 3010) for x in reversed(xs)], name="Đại lộ X", cls="chinh", oneway=1)
    w["ql99"] = t.way([(x, 500) for x in steps(2500, 5500)], ref="QL.99", cls="quoclo")
    w["xa"] = t.way([(x, 500) for x in steps(8000, 9000)], name="Phố Xa")
    # north of the ring: dual carriageway "Đại lộ Y" (30 m apart) with side streets that each touch one side only
    ys = steps(0, 3000)
    w["y_south"] = t.way([(x, 5000) for x in ys], name="Đại lộ Y", cls="chinh", oneway=1)
    w["y_north"] = t.way([(x, 5030) for x in reversed(ys)], name="Đại lộ Y", cls="chinh", oneway=1)
    w["cut_a"] = t.way([(1000, 5030), (1000, 5500)], name="Phố Cụt A")        # T-junction, north side only
    w["cut_b"] = t.way([(2000, 5000), (2000, 4500)], name="Phố Cụt B")        # T-junction, south side only
    # south of the ring: a long road crossing "Phố Ngang", and a separate same-named stub touching it
    w["dai"] = t.way([(x, -1500) for x in steps(-1500, 5500)], name="Phố Dài")
    w["ngang"] = t.way([(0, -1800), (0, -1500), (0, -1300), (0, -1200)], name="Phố Ngang")
    w["dai_stub"] = t.way([(0, -1300), (-100, -1300)], name="Phố Dài")
    return t, t.build(), ring, w


def mid(p, q):
    return (p[0] + q[0]) / 2, (p[1] + q[1]) / 2


def inside_ring(p, q, margin=100):
    mx, my = mid(p, q)
    return margin < mx < 4000 - margin and margin < my < 4000 - margin


class ScopeFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        b, cls.t, ring, w = build_fixture(1)
        ring_ways = set(ring.values())
        cls.inner = b.seg_ids(lambda wi, p, q: wi not in ring_ways and inside_ring(p, q))
        cls.outside_part_of_underpass = b.seg_ids(lambda wi, p, q: wi == w["ngam"] and min(p[0], q[0]) >= 4100).pop()
        cls.mien = b.seg_ids(lambda wi, p, q: wi == w["mien"])
        cls.nho = b.seg_ids(lambda wi, p, q: wi == w["ngo"])
        cls.x_between_1_and_2_both_ways = b.seg_ids(
            lambda wi, p, q: wi in (w["x_east"], w["x_west"]) and min(p[0], q[0]) >= 1000 and max(p[0], q[0]) <= 2000)
        cls.ql99 = b.seg_ids(lambda wi, p, q: wi == w["ql99"])
        cls.ql99_up_to_pho3 = b.seg_ids(lambda wi, p, q: wi == w["ql99"] and min(p[0], q[0]) >= 3000)
        cls.in_ward_p = b.seg_ids(lambda wi, p, q: 2200 < mid(p, q)[0] < 4900 and 200 < mid(p, q)[1] < 1800)
        cls.y_between_a_and_b_both_ways = b.seg_ids(
            lambda wi, p, q: wi in (w["y_south"], w["y_north"]) and min(p[0], q[0]) >= 1000 and max(p[0], q[0]) <= 2000)
        cls.dai_east_and_stub = b.seg_ids(lambda wi, p, q: (wi == w["dai"] and min(p[0], q[0]) >= 0)
                                          or wi == w["dai_stub"])
        seg0 = b.segs[0]
        cls.w, cls.a, cls.b = int(cls.t.way_id[seg0[2]]), seg0[0], seg0[1]

        b2, cls.t2, ring2, _ = build_fixture(2)
        ring2_ways = set(ring2.values())
        cls.inner2 = b2.seg_ids(lambda wi, p, q: wi not in ring2_ways and inside_ring(p, q))
        river = [ll(0, y) for y in steps(-200, 4200, 300)]
        lat = np.array([p[0] for p in river], dtype=np.float32)
        lon = np.array([p[1] for p in river], dtype=np.float32)
        cls.water = WaterLines(a_lat=lat[:-1], a_lon=lon[:-1], b_lat=lat[1:], b_lon=lon[1:],
                               name=np.zeros(len(river) - 1, dtype=np.int32), strings=["Sông Thử"])


class ZoneTests(ScopeFixture):
    def test_zone_contains_inner_streets_only(self):
        z = zone_segments(self.t, RING, SEED, BBOX, (5, 30))
        self.assertEqual(set(z.indices.tolist()), self.inner)          # outside street and ring roads excluded
        self.assertAlmostEqual(z.area_km2, 16.0, delta=1.5)
        self.assertEqual(z.indices.dtype, np.int64)
        self.assertEqual(z.missing_names, [])

    def test_underpass_does_not_leak(self):
        z = zone_segments(self.t, RING, SEED, BBOX, (5, 30))
        self.assertNotIn(self.outside_part_of_underpass, z.indices.tolist())

    def test_gap_in_boundary_raises_instead_of_leaking(self):
        with self.assertRaisesRegex(ZoneError, "leaks"):
            zone_segments(self.t, RING[:3], SEED, BBOX, (5, 30))        # road D left out -> open side

    def test_unknown_boundary_name_raises(self):
        with self.assertRaisesRegex(ZoneError, "Vành đai Z"):
            zone_segments(self.t, RING + ["Vành đai Z"], SEED, BBOX, (5, 30))

    def test_river_side_and_ref_boundaries(self):
        z = zone_segments(self.t2, ["Vành đai A", "Vành đai B", "QL9", "Sông Thử"], SEED, BBOX, (5, 30),
                          water=self.water)
        self.assertEqual(set(z.indices.tolist()), self.inner2)

    def test_river_without_water_lines_is_missing(self):
        with self.assertRaisesRegex(ZoneError, "Sông Thử"):
            zone_segments(self.t2, ["Vành đai A", "Vành đai B", "QL9", "Sông Thử"], SEED, BBOX, (5, 30))

    def test_boundary_road_crossing_the_zone_is_caught(self):
        with self.assertRaisesRegex(ZoneError, "not adjacent"):
            zone_segments(self.t, RING + ["Phố Giữa"], SEED, BBOX, (1, 30))

    def test_area_bounds(self):
        with self.assertRaisesRegex(ZoneError, "area"):
            zone_segments(self.t, RING, SEED, BBOX, (20, 30))

    def test_seed_on_boundary(self):
        with self.assertRaisesRegex(ZoneError, "seed lies on the boundary"):
            zone_segments(self.t, RING, list(ll(4000, 2000)), BBOX, (5, 30))

    def test_route_piece_as_boundary_entry(self):
        # the northern part of the ring, closed by Phố Miễn and the matching piece of road D
        pieces = ["Vành đai B", "Vành đai C", {"name": "Vành đai D", "from": "Phố Miễn", "to": "Vành đai C"},
                  {"name": "Phố Miễn", "from": "Vành đai D", "to": "Vành đai B"}]
        z = zone_segments(self.t, pieces, SEED, BBOX, (5, 30))
        self.assertGreater(z.area_km2, 9.0)
        self.assertLess(z.area_km2, 12.0)

    def test_zone_scope_errors_propagate(self):
        with self.assertRaises(ZoneError):
            ScopeResolver(self.t).resolve({"zone": dict(ZONE, boundary_names=RING[:3]), "bbox": BBOX})


class ScopeResolverTests(ScopeFixture):
    def test_exempt_and_class_filters(self):
        r = ScopeResolver(self.t).resolve({"zone": ZONE, "bbox": BBOX, "exclude_classes": ["nho"]},
                                          exempt={"routes": [{"name": "Phố Miễn"}]})
        self.assertTrue(set(r.indices.tolist()).isdisjoint(self.mien | self.nho))
        self.assertEqual(r.orphans, [])
        self.assertEqual(set(r.indices.tolist()), self.inner - self.mien - self.nho)

    def test_classes_keep_only(self):
        r = ScopeResolver(self.t).resolve({"zone": ZONE, "bbox": BBOX, "classes": ["nho"]})
        self.assertEqual(set(r.indices.tolist()), self.nho)

    def test_route_piece_between_crossings_keeps_both_carriageways(self):
        r = ScopeResolver(self.t).resolve({"routes": [{"name": "Đại lộ X", "from": "Phố 1", "to": "Phố 2"}], "bbox": BBOX})
        self.assertEqual(set(r.indices.tolist()), self.x_between_1_and_2_both_ways)

    def test_route_piece_by_ref_and_missing_crossing(self):
        self.assertEqual(normalize_ref("QL.13"), "QL13")
        self.assertEqual(normalize_ref("ĐT 743"), "DT743")
        r = ScopeResolver(self.t).resolve({"routes": [{"ref": "ql 99", "to": "Phố 3"},
                                                      {"name": "Đại lộ X", "from": "Phố Không"}], "bbox": BBOX})
        self.assertEqual(set(r.indices.tolist()), self.ql99_up_to_pho3)
        self.assertEqual(r.orphans, [{"route": {"name": "Đại lộ X", "from": "Phố Không"}, "missing": "from"}])

    def test_crossing_on_one_carriageway_keeps_both(self):
        # each side street meets only one carriageway; nodes within 60 m of the crossing seed the other side
        idx, orphan = route_piece(self.t, {"name": "Đại lộ Y", "from": "Phố Cụt A", "to": "Phố Cụt B"}, BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.y_between_a_and_b_both_ways)

    def test_one_sided_piece_is_not_cut_by_a_stub_component(self):
        # a 100 m same-named stub also touches the crossing; the long road must still run to its far end
        idx, orphan = route_piece(self.t, {"name": "Phố Dài", "from": "Phố Ngang"}, BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.dai_east_and_stub)

    def test_route_piece_function_and_missing_name(self):
        idx, orphan = route_piece(self.t, {"name": "Đại lộ X", "from": "Phố 2", "to": "Phố 1"}, BBOX)
        self.assertEqual(set(idx.tolist()), self.x_between_1_and_2_both_ways)
        self.assertIsNone(orphan)
        idx, orphan = route_piece(self.t, {"name": "Không Có"}, BBOX)
        self.assertEqual(len(idx), 0)
        self.assertEqual(orphan, {"route": {"name": "Không Có"}, "missing": "name"})
        idx, orphan = route_piece(self.t, {"name": "Đại lộ X", "to": "Phố Không"}, BBOX)
        self.assertEqual(orphan["missing"], "to")

    def test_route_piece_bbox_limits_the_road(self):
        west_bbox = [ll(-2000, -2000)[1], ll(-2000, -2000)[0], ll(4500, 0)[1], ll(4500, 6000)[0]]
        idx, orphan = route_piece(self.t, {"ref": "QL99"}, west_bbox)
        self.assertIsNone(orphan)
        self.assertEqual(len(idx), 4)                                   # midpoints 2750..4250

    def test_admin_units_except_national_roads(self):
        res = ScopeResolver(self.t, admin=ADMIN_FIXTURE)
        r = res.resolve({"admin": {"province": "Tỉnh T", "units": ["Phường P", "Phường Q"], "except_refs": ["QL"]}})
        self.assertEqual(set(r.indices.tolist()), self.in_ward_p - self.ql99)
        self.assertEqual(r.orphans, [{"unit": "Phường Q"}])
        with self.assertRaisesRegex(RuleError, "admin"):
            ScopeResolver(self.t).resolve({"admin": {"province": "Tỉnh T", "units": ["Phường P"]}})

    def test_admin_names_ignore_admin_type_prefix(self):
        res = ScopeResolver(self.t, admin=ADMIN_FIXTURE)
        r = res.resolve({"admin": {"province": "T", "units": ["P"], "except_refs": ["QL"]}})
        self.assertEqual(set(r.indices.tolist()), self.in_ward_p - self.ql99)
        self.assertEqual(r.orphans, [])

    def test_missing_province_is_an_orphan(self):
        r = ScopeResolver(self.t, admin=ADMIN_FIXTURE).resolve(
            {"admin": {"province": "Tỉnh Không", "units": ["Phường P"]}})
        self.assertEqual(len(r.indices), 0)
        self.assertEqual(r.orphans, [{"province": "Tỉnh Không"}])

    def test_exempt_orphans_reported_for_empty_scope_without_bbox(self):
        r = ScopeResolver(self.t, admin=ADMIN_FIXTURE).resolve(
            {"admin": {"province": "Tỉnh T", "units": ["Phường P"]}, "classes": ["caotoc"]},
            exempt={"routes": [{"name": "Không Có"}]})
        self.assertEqual(len(r.indices), 0)
        self.assertEqual(r.orphans, [{"route": {"name": "Không Có"}, "missing": "name"}])

    def test_polygon_scope(self):
        r = ScopeResolver(self.t).resolve({"polygon": box_geometry(2200, 200, 4900, 1800)})
        self.assertEqual(set(r.indices.tolist()), self.in_ward_p)

    def test_missing_anchor_is_an_orphan(self):
        r = ScopeResolver(self.t).resolve({"segments": [[self.w, self.a, self.b], [999, 1, 2]]})
        self.assertEqual(len(r.indices), 1)
        self.assertEqual(r.orphans, [{"anchor": [999, 1, 2]}])

    def test_names_match_after_normalisation(self):
        self.assertEqual(normalize_name("  Đường  Vành đai A "), normalize_name("vành đai a"))
        self.assertEqual(normalize_name("Phố  Miễn"), "miễn")
        r = ScopeResolver(self.t).resolve({"names": ["vành đai a", "Không Có"]})
        self.assertEqual(r.orphans, [{"name": "Không Có"}])
        self.assertGreater(len(r.indices), 0)
        r = ScopeResolver(self.t).resolve({"names": ["QL 99"], "bbox": BBOX})
        self.assertEqual(set(r.indices.tolist()), self.ql99)

    def test_normalize_ref_forms(self):
        for raw in ("QL.13", "QL 13", "ql13", "QL-13"):
            self.assertEqual(normalize_ref(raw), "QL13")
        self.assertEqual(normalize_ref("ĐT743"), "DT743")
        self.assertEqual(normalize_ref("DT 743"), "DT743")
        self.assertEqual(normalize_ref("QL1A"), "QL1A")
        self.assertEqual(normalize_ref("ql.1a"), "QL1A")

    def test_results_are_sorted_unique_and_cached(self):
        res = ScopeResolver(self.t)
        scope = {"names": ["Phố Miễn", "phố miễn", "Vành đai A"], "bbox": BBOX}
        r = res.resolve(scope)
        self.assertTrue(np.all(np.diff(r.indices) > 0))
        self.assertIs(res.resolve(dict(reversed(list(scope.items())))), r)
        self.assertIsNot(res.resolve(scope, exempt={"routes": [{"name": "Phố Miễn"}]}), r)


def build_carriageway_fixture():
    """Dual carriageways that route pieces can lose, inside a ring for the zone case.

    "Đại lộ Z": south side y=0 (nodes every 500 m), north side y=40 with nodes offset by 250 m, so no north
    node lies within 60 m of a crossing node; "Phố Z1"/"Phố Z2" meet the south side only. The ring
    "Biên Đông/Bắc/Tây" closes Z's south side into a 3 x 2 km zone.
    "Đại lộ J": two sides 30 m apart, joined by a same-named link at "Phố J0" only.
    """
    t = TableBuilder()
    w = {}
    w["z_south"] = t.way([(x, 0) for x in steps(0, 3000)], name="Đại lộ Z", cls="chinh", oneway=1)
    w["z_north"] = t.way([(x, 40) for x in (2900, 2250, 1750, 1250, 750, 250, 100)], name="Đại lộ Z",
                         cls="chinh", oneway=1)
    w["z1"] = t.way([(1000, 0), (1000, -500)], name="Phố Z1")
    w["z2"] = t.way([(2000, 0), (2000, -500)], name="Phố Z2")
    w["east"] = t.way([(3000, y) for y in steps(0, 2000)], name="Biên Đông")
    w["north"] = t.way([(x, 2000) for x in steps(0, 3000)], name="Biên Bắc")
    w["west"] = t.way([(0, y) for y in steps(0, 2000)], name="Biên Tây")
    w["inner"] = t.way([(1000, 1000), (2000, 1000)], name="Phố Trong")
    w["j_south"] = t.way([(x, -3000) for x in steps(0, 3000)], name="Đại lộ J", cls="chinh", oneway=1)
    w["j_north"] = t.way([(x, -2970) for x in reversed(steps(0, 3000))], name="Đại lộ J", cls="chinh", oneway=1)
    w["j_link"] = t.way([(0, -3000), (0, -2970)], name="Đại lộ J", cls="chinh")
    w["j0"] = t.way([(0, -3000), (-500, -3000)], name="Phố J0")
    return t, t.build(), w


CW_BBOX = [ll(-1000, -3500)[1], ll(-1000, -3500)[0], ll(4000, 2500)[1], ll(4000, 2500)[0]]


class CarriagewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        b, cls.t, w = build_carriageway_fixture()
        cls.z_south_east_of_z1 = b.seg_ids(lambda wi, p, q: wi == w["z_south"] and min(p[0], q[0]) >= 1000)
        cls.z_south_z1_z2 = b.seg_ids(lambda wi, p, q: wi == w["z_south"] and min(p[0], q[0]) >= 1000
                                      and max(p[0], q[0]) <= 2000)
        cls.j_south = b.seg_ids(lambda wi, p, q: wi == w["j_south"])
        cls.j_north = b.seg_ids(lambda wi, p, q: wi == w["j_north"])
        cls.inner = b.seg_ids(lambda wi, p, q: wi == w["inner"])

    def test_one_sided_piece_reports_a_carriageway_without_seed(self):
        piece = {"name": "Đại lộ Z", "from": "Phố Z1"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertEqual(orphan, {"route": piece, "missing": "carriageway"})
        self.assertEqual(set(idx.tolist()), self.z_south_east_of_z1)     # the found side is still applied

    def test_two_sided_piece_reports_a_carriageway_without_seed(self):
        piece = {"name": "Đại lộ Z", "from": "Phố Z1", "to": "Phố Z2"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertEqual(orphan, {"route": piece, "missing": "carriageway"})
        self.assertEqual(set(idx.tolist()), self.z_south_z1_z2)

    def test_one_sided_piece_on_carriageways_joined_at_the_crossing_only(self):
        # one far end for the joined component: the other side's far part is reached only through the link
        piece = {"name": "Đại lộ J", "from": "Phố J0"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertEqual(orphan, {"route": piece, "missing": "carriageway"})
        kept = set(idx.tolist())
        self.assertTrue(self.j_south <= kept or self.j_north <= kept)
        self.assertFalse(self.j_south <= kept and self.j_north <= kept)

    def test_carriageway_orphans_reach_the_rule(self):
        r = ScopeResolver(self.t).resolve({"routes": [{"name": "Đại lộ Z", "from": "Phố Z1"}], "bbox": CW_BBOX},
                                          exempt={"routes": [{"name": "Đại lộ J", "from": "Phố J0"}]})
        self.assertEqual([o["missing"] for o in r.orphans], ["carriageway", "carriageway"])
        self.assertEqual(set(r.indices.tolist()), self.z_south_east_of_z1)

    def test_zone_boundary_piece_that_loses_a_carriageway_still_seals(self):
        # the kept side encloses the zone; the lost side lies within the boundary's seal
        pieces = [{"name": "Đại lộ Z", "from": "Biên Tây"}, "Biên Đông", "Biên Bắc", "Biên Tây"]
        z = zone_segments(self.t, pieces, list(ll(1500, 1200)), CW_BBOX, (3, 8))
        self.assertEqual(set(z.indices.tolist()), self.inner)

    def test_full_pieces_report_no_carriageway(self):
        for piece in ({"name": "Biên Đông", "from": "Biên Bắc"}, {"name": "Biên Bắc", "from": "Biên Tây", "to": "Biên Đông"}):
            self.assertIsNone(route_piece(self.t, piece, CW_BBOX)[1], piece)


if __name__ == "__main__":
    unittest.main()
