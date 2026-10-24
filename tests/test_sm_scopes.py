"""Offline tests for speedmatrix scope resolution (names, routes, admin, polygon, zones).

The fixture is a synthetic SegmentTable built from metre coordinates, without a PBF.
"""
import math
from pathlib import Path
import sys
import tempfile
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
        r = ScopeResolver(self.t).resolve({"segments": [[self.w, self.a, self.b], [999, 9001, 9002]]})
        self.assertEqual(len(r.indices), 1)
        self.assertEqual(r.orphans, [{"anchor": [999, 9001, 9002]}])

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

    def test_bbox_query_bounds_are_exact_for_float32_midpoints(self):
        from speedmatrix.scopes import _index
        index = _index(self.t)
        mid_lat, mid_lon, _, _ = index.spatial()
        i = int(np.argmax(mid_lat))                    # a float32 latitude that is not exact in float64 decimals
        lat, lon = float(mid_lat[i]), float(mid_lon[i])
        box = [lon - 1e-6, lat, lon + 1e-6, lat]
        self.assertIn(i, index.query_bbox(box).tolist())
        below = float(np.nextafter(mid_lat[i], np.float32(-np.inf)))
        for south, north in ((lat + 1e-12, lat + 1.0), (below, (below + lat) / 2)):
            self.assertNotIn(i, index.query_bbox([lon - 1e-6, south, lon + 1e-6, north]).tolist())

    def test_results_are_sorted_unique_and_cached(self):
        res = ScopeResolver(self.t)
        scope = {"names": ["Phố Miễn", "phố miễn", "Vành đai A"], "bbox": BBOX}
        r = res.resolve(scope)
        self.assertTrue(np.all(np.diff(r.indices) > 0))
        self.assertIs(res.resolve(dict(reversed(list(scope.items())))), r)
        self.assertIsNot(res.resolve(scope, exempt={"routes": [{"name": "Phố Miễn"}]}), r)


def build_carriageway_fixture():
    """Dual carriageways whose second side touches no crossing, and same-named roads near a piece.

    Each road runs east-west; its crossing streets meet the south side only, at x = 1000 and x = 2000.
    - "Đại lộ Z" (y = 0): the north side (y = 40) has nodes at 2900, 2450, 2050, 1550, 1050, 550, 100, none within
      60 m of a crossing node, so it gets no source. The ring "Biên Đông/Bắc/Tây" closes Z into a 3 x 2 km zone.
    - "Đại lộ J" (y = -3000): two sides 30 m apart, joined by a same-named link at "Phố J0" (x = 0) only.
    - "Đại lộ F" (y = -5000): a detached same-named stretch 80 m north of the road, between the crossings.
    - "Đại lộ B" (y = -7000, a node every 50 m, so the piece runs on a little past its crossings): detached
      same-named stretches 30 m north, one ending before the first crossing and one starting after the second.
    - "Đại lộ S" (y = -9000): a detached same-named spur that starts 50 m north of the road and runs away from it.
    - "Đại lộ Q" (y = -11000): the north side (y = -10970) is cut by the 80 m bridge "Cầu Q" (x 1460..1540);
      "Phố Q3" crosses both sides at x = 1250.
    - "Đại lộ R" (y = -13000): two detached same-named lanes of opposite directions on the north, x 1100..1450
      eastwards at 15 m and x 1900..1460 westwards at 55 m, whose ends the overpass "Cầu vượt R3" joins.
    - "Đại lộ P" (y = -15000) and "Đại lộ U" (y = -17000): the north side (30 m) is cut at x 1400..1600 by a way
      that cannot fill the gap: the street "Phố Song Hành" on P, a 200 m unnamed way on U.
    - "Đại lộ V" (y = -19000): ends at x = 2000 in a Y whose two arms (32 m) meet a roundabout; past the
      roundabout the road goes on, again from a Y (x 2050..2080), to x = 2600. "Phố V1" crosses at x = 500.
    - "Đại lộ W" (y = -21000): one-sided from "Phố W1" (x = 500); the south side ends at x = 2000 and goes on as
      "Phố Khác", the north side (30 m) runs on to x = 3000.
    - "Đại lộ C" (y = -23000) and "Đại lộ K" (y = -25000): one-sided from x = 500, both sides cut at x 1500..1540
      by a junction, then on to x = 3540. On C the junction is an unnamed loop; on K it is the street "Phố Cắt".
    - "Đại lộ N" (y = -27000): the north side (30 m) is cut at x 1490..1510 by a 20 m unnamed way.
    - "Đại lộ G" (y = -29000): the north side (30 m) is cut at x 1460..1540 by the street "Đường Cầu Giấy".
    - "Đại lộ E" (y = -31000): the north side (30 m, westwards) bows out to 100 m between x 1700 and 1300.
    - "Đại lộ O" (y = -33000): the north side (30 m, westwards) is cut at x 1488..1512 by a 96 m unnamed arc
      that reaches 70 m from the road.
    - "Đại lộ T" (y = -35000, two-way): from "Phố T1" (x = 500) to the ref-only road "QL.77" (x = 2500), cut at
      x 1500..1540 by an unnamed junction, then on past QL.77 to x = 3540.
    - A namesake "Đại lộ Z" with its own "Phố Z1" and "Phố Z2" in another town 30 km east, outside CW_BBOX.
    """
    t = TableBuilder()
    w = {}
    w["z_south"] = t.way([(x, 0) for x in steps(0, 3000)], name="Đại lộ Z", cls="chinh", oneway=1)
    w["z_north"] = t.way([(x, 40) for x in (2900, 2450, 2050, 1550, 1050, 550, 100)], name="Đại lộ Z",
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
    for road, y in (("F", -5000), ("B", -7000), ("S", -9000)):
        step = 50 if road == "B" else 500
        w[road.lower()] = t.way([(x, y) for x in steps(0, 3000, step)], name=f"Đại lộ {road}", cls="chinh")
        w[road.lower() + "1"] = t.way([(1000, y), (1000, y - 500)], name=f"Phố {road}1")
        w[road.lower() + "2"] = t.way([(2000, y), (2000, y - 500)], name=f"Phố {road}2")
    w["f_far"] = t.way([(x, -4920) for x in (1100, 1500, 1900)], name="Đại lộ F", cls="chinh")
    w["b_before"] = t.way([(x, -6970) for x in (200, 600, 940)], name="Đại lộ B", cls="chinh")
    w["b_after"] = t.way([(x, -6970) for x in (2060, 2400, 2800)], name="Đại lộ B", cls="chinh")
    w["s_spur"] = t.way([(1500, -8950), (1500, -8600)], name="Đại lộ S", cls="chinh")
    w["q_south"] = t.way([(x, -11000) for x in (0, 500, 1000, 1250, 1500, 2000, 2500, 3000)], name="Đại lộ Q",
                         cls="chinh", oneway=1)
    w["q_north_e"] = t.way([(1900, -10970), (1540, -10970)], name="Đại lộ Q", cls="chinh", oneway=1)
    w["q_bridge"] = t.way([(1540, -10970), (1460, -10970)], name="Cầu Q", cls="chinh", oneway=1)
    w["q_north_w"] = t.way([(1460, -10970), (1250, -10970), (1100, -10970)], name="Đại lộ Q", cls="chinh",
                           oneway=1)
    w["q1"] = t.way([(1000, -11000), (1000, -11500)], name="Phố Q1")
    w["q2"] = t.way([(2000, -11000), (2000, -11500)], name="Phố Q2")
    w["q3"] = t.way([(1250, -11050), (1250, -11000), (1250, -10970), (1250, -10900)], name="Phố Q3")
    w["r_south"] = t.way([(x, -13000) for x in steps(0, 3000)], name="Đại lộ R", cls="chinh", oneway=1)
    w["r_lane_w"] = t.way([(1100, -12985), (1450, -12985)], name="Đại lộ R", cls="chinh", oneway=1)
    w["r_lane_e"] = t.way([(1900, -12945), (1460, -12945)], name="Đại lộ R", cls="chinh", oneway=1)
    w["r3"] = t.way([(1450, -12985), (1460, -12945)], name="Cầu vượt R3")
    w["r1"] = t.way([(1000, -13000), (1000, -13500)], name="Phố R1")
    w["r2"] = t.way([(2000, -13000), (2000, -13500)], name="Phố R2")
    w["v"] = t.way([(x, -19000) for x in steps(0, 2000)], name="Đại lộ V", cls="chinh")
    w["v_arms"] = [t.way([(2000, -19000), (2025, -19000 + dy)], name="Đại lộ V", cls="chinh") for dy in (20, -20)]
    w["v_past"] = [t.way([(2050, -19000 + dy), (2080, -19000)], name="Đại lộ V", cls="chinh") for dy in (20, -20)]
    w["v_past"].append(t.way([(2080, -19000), (2600, -19000)], name="Đại lộ V", cls="chinh"))
    w["v1"] = t.way([(500, -19000), (500, -19500)], name="Phố V1")
    for road, y, join in (("P", -15000, "Phố Song Hành"), ("U", -17000, "")):
        key = road.lower()
        w[key + "_south"] = t.way([(x, y) for x in steps(0, 3000)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "_north_e"] = t.way([(1900, y + 30), (1600, y + 30)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "_join"] = t.way([(1600, y + 30), (1400, y + 30)], name=join, cls="chinh", oneway=1)
        w[key + "_north_w"] = t.way([(1400, y + 30), (1100, y + 30)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "1"] = t.way([(1000, y), (1000, y - 500)], name=f"Phố {road}1")
        w[key + "2"] = t.way([(2000, y), (2000, y - 500)], name=f"Phố {road}2")
    y = -21000
    w["w_south"] = t.way([(x, y) for x in steps(0, 2000)], name="Đại lộ W", cls="chinh", oneway=1)
    w["w_north"] = t.way([(x, y + 30) for x in reversed(steps(0, 3000))], name="Đại lộ W", cls="chinh", oneway=1)
    w["w_other"] = t.way([(2000, y), (2500, y - 400)], name="Phố Khác")
    w["w1"] = t.way([(500, y - 500), (500, y), (500, y + 30), (500, y + 500)], name="Phố W1")
    for road, y, cut in (("C", -23000, ""), ("K", -25000, "Phố Cắt")):
        key = road.lower()
        w[key + "_south"] = t.way([(x, y) for x in steps(0, 1500)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "_north"] = t.way([(x, y + 30) for x in reversed(steps(0, 1500))], name=f"Đại lộ {road}", cls="chinh",
                                  oneway=1)
        w[key + "_cut"] = t.way([(1500, y), (1540, y), (1540, y + 30), (1500, y + 30), (1500, y)], name=cut)
        w[key + "_south_on"] = t.way([(1540 + x, y) for x in steps(0, 2000)], name=f"Đại lộ {road}", cls="chinh",
                                     oneway=1)
        w[key + "_north_on"] = t.way([(1540 + x, y + 30) for x in reversed(steps(0, 2000))], name=f"Đại lộ {road}",
                                     cls="chinh", oneway=1)
        w[key + "1"] = t.way([(500, y - 500), (500, y), (500, y + 30), (500, y + 500)], name=f"Phố {road}1")
    for road, y, x0, x1, join in (("N", -27000, 1490, 1510, ""), ("G", -29000, 1460, 1540, "Đường Cầu Giấy")):
        key = road.lower()
        w[key + "_south"] = t.way([(x, y) for x in steps(0, 3000)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "_north_e"] = t.way([(1900, y + 30), (x1, y + 30)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "_join"] = t.way([(x1, y + 30), (x0, y + 30)], name=join, cls="chinh", oneway=1)
        w[key + "_north_w"] = t.way([(x0, y + 30), (1100, y + 30)], name=f"Đại lộ {road}", cls="chinh", oneway=1)
        w[key + "1"] = t.way([(1000, y), (1000, y - 500)], name=f"Phố {road}1")
        w[key + "2"] = t.way([(2000, y), (2000, y - 500)], name=f"Phố {road}2")
    y = -31000
    w["e_south"] = t.way([(x, y) for x in steps(0, 3000)], name="Đại lộ E", cls="chinh", oneway=1)
    w["e_north"] = t.way([(1900, y + 30), (1700, y + 30), (1690, y + 100), (1310, y + 100), (1300, y + 30),
                          (1100, y + 30)], name="Đại lộ E", cls="chinh", oneway=1)
    w["e1"] = t.way([(1000, y), (1000, y - 500)], name="Phố E1")
    w["e2"] = t.way([(2000, y), (2000, y - 500)], name="Phố E2")
    y = -33000
    w["o_south"] = t.way([(x, y) for x in steps(0, 3000)], name="Đại lộ O", cls="chinh", oneway=1)
    w["o_north_e"] = t.way([(1900, y + 30), (1512, y + 30)], name="Đại lộ O", cls="chinh", oneway=1)
    w["o_arc"] = t.way([(1512, y + 30), (1508, y + 70), (1492, y + 70), (1488, y + 30)], name="", cls="chinh", oneway=1)
    w["o_north_w"] = t.way([(1488, y + 30), (1100, y + 30)], name="Đại lộ O", cls="chinh", oneway=1)
    w["o1"] = t.way([(1000, y), (1000, y - 500)], name="Phố O1")
    w["o2"] = t.way([(2000, y), (2000, y - 500)], name="Phố O2")
    y = -35000
    w["t_west"] = t.way([(x, y) for x in steps(0, 1500)], name="Đại lộ T", cls="chinh")
    w["t_cut"] = t.way([(1500, y), (1520, y + 15), (1540, y)], name="")
    w["t_cross"] = t.way([(1520, y + 15), (1520, y + 300)], name="")
    w["t_east"] = t.way([(1540 + x, y) for x in steps(0, 2000)], name="Đại lộ T", cls="chinh")
    w["t1"] = t.way([(500, y - 500), (500, y)], name="Phố T1")
    w["ql77"] = t.way([(2540, y - 500), (2540, y), (2540, y + 500)], ref="QL.77", cls="quoclo")
    dx = 30000
    w["town_south"] = t.way([(dx + x, 0) for x in steps(0, 3000)], name="Đại lộ Z", cls="chinh", oneway=1)
    w["town_north"] = t.way([(dx + x, 40) for x in (2900, 2450, 2050, 1550, 1050, 550, 100)], name="Đại lộ Z",
                            cls="chinh", oneway=1)
    w["town_z1"] = t.way([(dx + 1000, 0), (dx + 1000, -500)], name="Phố Z1")
    w["town_z2"] = t.way([(dx + 2000, 0), (dx + 2000, -500)], name="Phố Z2")
    return t, t.build(), w


CW_BBOX = [ll(-1000, -36000)[1], ll(-1000, -36000)[0], ll(4000, 2500)[1], ll(4000, 2500)[0]]
TOWN_BBOX = [ll(29000, -1000)[1], ll(29000, -1000)[0], ll(34000, 1000)[1], ll(34000, 1000)[0]]


class CarriagewayTests(unittest.TestCase):
    """A route piece takes in the same-named road that runs beside it between its crossings (60 m corridor)."""

    @classmethod
    def setUpClass(cls):
        cls.b, cls.t, cls.w = build_carriageway_fixture()
        cls.z_north = cls.of_way("z_north", -1e9, 1e9)
        cls.j_all = cls.of_way("j_south", -1e9, 1e9) | cls.of_way("j_north", -1e9, 1e9) | cls.of_way("j_link", -1e9, 1e9)
        cls.inner = cls.of_way("inner", -1e9, 1e9)

    @classmethod
    def of_way(cls, key, x0, x1):
        """Segments of way ``key`` lying between x0 and x1 (metres east)."""
        way = cls.w[key]
        return cls.b.seg_ids(lambda wi, p, q: wi == way and min(p[0], q[0]) >= x0 and max(p[0], q[0]) <= x1)

    @classmethod
    def of_way_index(cls, way):
        return cls.b.seg_ids(lambda wi, p, q: wi == way)

    def ways(self, idx):
        return {self.b.segs[i][2] for i in idx.tolist()}

    def assert_gap_at(self, orphan, piece, points):
        """The orphan reports gaps in ``piece`` at exactly these points (metres), each within 2 m."""
        self.assertIsNotNone(orphan, piece)
        self.assertEqual((orphan["route"], orphan["missing"]), (piece, "gap"))
        self.assertEqual(len(orphan["at"]), len(points), orphan["at"])
        for x, y in points:
            lat, lon = ll(x, y)
            self.assertTrue(any(haversine_m(lat, lon, a, b) < 2.0 for a, b in orphan["at"]), (x, y, orphan["at"]))

    def test_unseeded_carriageway_beside_a_two_sided_piece_is_included(self):
        idx, orphan = route_piece(self.t, {"name": "Đại lộ Z", "from": "Phố Z1", "to": "Phố Z2"}, CW_BBOX)
        self.assertIsNone(orphan)
        # the north segments that run beside the piece for at least half their length: 2050-1550 and 1550-1050
        self.assertEqual(set(idx.tolist()), self.of_way("z_south", 1000, 2000) | self.of_way("z_north", 1050, 2050))

    def test_unseeded_carriageway_beside_a_one_sided_piece_is_included(self):
        idx, orphan = route_piece(self.t, {"name": "Đại lộ Z", "from": "Phố Z1"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.of_way("z_south", 1000, 3000) | self.of_way("z_north", 1050, 3000))

    def test_carriageways_joined_at_the_crossing_only_are_both_kept(self):
        # the joined component has one far end; the other side is taken in as the road beside the piece
        idx, orphan = route_piece(self.t, {"name": "Đại lộ J", "from": "Phố J0"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.j_all)

    def test_same_named_road_more_than_60_m_away_is_neither_included_nor_flagged(self):
        idx, orphan = route_piece(self.t, {"name": "Đại lộ F", "from": "Phố F1", "to": "Phố F2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.of_way("f", 1000, 2000))

    def test_same_named_stretches_beyond_the_crossings_are_neither_included_nor_flagged(self):
        # both stretches lie within 60 m of the piece's own overrun past its crossings, but outside its span
        idx, orphan = route_piece(self.t, {"name": "Đại lộ B", "from": "Phố B1", "to": "Phố B2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(self.ways(idx), {self.w["b"]})
        self.assertTrue(self.of_way("b", 1000, 2000) <= set(idx.tolist()))

    def test_namesake_road_in_another_town_is_not_included(self):
        town = self.of_way("town_south", -1e9, 1e9) | self.of_way("town_north", -1e9, 1e9)
        idx, _ = route_piece(self.t, {"name": "Đại lộ Z", "from": "Phố Z1", "to": "Phố Z2"}, CW_BBOX)
        self.assertTrue(set(idx.tolist()).isdisjoint(town))
        idx, orphan = route_piece(self.t, {"name": "Đại lộ Z", "from": "Phố Z1", "to": "Phố Z2"}, TOWN_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(self.ways(idx), {self.w["town_south"], self.w["town_north"]})

    def test_same_named_road_that_only_touches_the_corridor_is_reported(self):
        # the spur is within 60 m for its first metres only, then runs away: it cannot be matched as a carriageway
        piece = {"name": "Đại lộ S", "from": "Phố S1", "to": "Phố S2"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertEqual(orphan, {"route": piece, "missing": "carriageway"})
        self.assertEqual(set(idx.tolist()), self.of_way("s", 1000, 2000))       # the found part is still applied

    def test_bridge_between_same_named_parts_of_the_other_carriageway_is_included(self):
        idx, orphan = route_piece(self.t, {"name": "Đại lộ Q", "from": "Phố Q1", "to": "Phố Q2"}, CW_BBOX)
        self.assertIsNone(orphan)
        expected = (self.of_way("q_south", 1000, 2000) | self.of_way("q_north_e", -1e9, 1e9)
                    | self.of_way("q_bridge", -1e9, 1e9) | self.of_way("q_north_w", -1e9, 1e9))
        self.assertEqual(set(idx.tolist()), expected)          # "Phố Q3", which crosses both sides, is not taken in

    def test_gap_no_way_can_fill_is_reported(self):
        # a named street, or an unnamed way longer than 100 m, does not fill the gap: the hole is reported
        for road, y in (("P", -15000), ("U", -17000)):
            key = road.lower()
            piece = {"name": f"Đại lộ {road}", "from": f"Phố {road}1", "to": f"Phố {road}2"}
            idx, orphan = route_piece(self.t, piece, CW_BBOX)
            self.assert_gap_at(orphan, piece, [(1500, y + 30)])
            self.assertEqual(self.ways(idx), {self.w[key + "_south"], self.w[key + "_north_e"], self.w[key + "_north_w"]},
                             road)

    def test_short_unnamed_way_fills_a_gap_in_the_other_carriageway(self):
        idx, orphan = route_piece(self.t, {"name": "Đại lộ N", "from": "Phố N1", "to": "Phố N2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(self.ways(idx), {self.w[k] for k in ("n_south", "n_north_e", "n_join", "n_north_w")})

    def test_carriageway_that_bows_out_of_the_corridor_is_taken_in(self):
        # the north side leaves the 60 m corridor for 400 m: the road itself joins its two parts, so it fills the gap
        idx, orphan = route_piece(self.t, {"name": "Đại lộ E", "from": "Phố E1", "to": "Phố E2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.of_way("e_south", 1000, 2000) | self.of_way("e_north", -1e9, 1e9))

    def test_short_unnamed_arc_out_of_the_corridor_fills_a_gap(self):
        # a roundabout's arc that leaves the corridor still joins the two parts within 100 m
        idx, orphan = route_piece(self.t, {"name": "Đại lộ O", "from": "Phố O1", "to": "Phố O2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(self.ways(idx), {self.w["o_south"], self.w["o_north_e"], self.w["o_arc"], self.w["o_north_w"]})
        self.assertEqual(len(set(idx.tolist()) & self.of_way("o_arc", -1e9, 1e9)), 3)

    def test_street_named_like_a_bridge_does_not_fill_a_gap(self):
        piece = {"name": "Đại lộ G", "from": "Phố G1", "to": "Phố G2"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assert_gap_at(orphan, piece, [(1500, -29000 + 30)])
        self.assertNotIn(self.w["g_join"], self.ways(idx))

    def test_bridge_tag_decides_over_the_name(self):
        # with an OSM bridge/tunnel flag per way, the flag decides: "Đường Cầu Giấy" tagged as a bridge fills the
        # gap, "Cầu Q" not tagged does not
        b, t, w = build_carriageway_fixture()
        t.bridge = np.zeros(len(t.way_id), dtype=np.int8)
        t.bridge[w["g_join"]] = 1
        idx, orphan = route_piece(t, {"name": "Đại lộ G", "from": "Phố G1", "to": "Phố G2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertIn(w["g_join"], {b.segs[i][2] for i in idx.tolist()})
        piece = {"name": "Đại lộ Q", "from": "Phố Q1", "to": "Phố Q2"}
        idx, orphan = route_piece(t, piece, CW_BBOX)
        self.assert_gap_at(orphan, piece, [(1500, -10970)])
        self.assertNotIn(w["q_bridge"], {b.segs[i][2] for i in idx.tolist()})

    def test_one_sided_piece_ending_in_a_fork_does_not_take_the_road_past_its_end(self):
        # every node within 60 m of the far end is an end, as at a crossing: the road past the roundabout,
        # within 60 m of the fork, lies outside the span. Nothing joins it to the piece, so the cut is reported
        piece = {"name": "Đại lộ V", "to": "Phố V1"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertEqual((orphan["missing"], len(orphan["at"])), ("gap", 1))
        self.assertTrue(any(haversine_m(*ll(2025, -19000 + dy), *orphan["at"][0]) < 2.0 for dy in (20, -20)))
        past = set().union(*(self.of_way_index(wi) for wi in self.w["v_past"]))
        arms = set().union(*(self.of_way_index(wi) for wi in self.w["v_arms"]))
        self.assertTrue(set(idx.tolist()).isdisjoint(past))
        self.assertTrue(arms <= set(idx.tolist()))

    def test_far_end_takes_in_only_its_own_carriageway_nodes(self):
        # the south side ends at x = 2000; the north side's own far end is x = 3000, not its node near x = 2000
        idx, orphan = route_piece(self.t, {"name": "Đại lộ W", "from": "Phố W1"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(set(idx.tolist()), self.of_way("w_south", 500, 2000) | self.of_way("w_north", 500, 3000))
        north = sum(float(self.t.length_m[i]) for i in idx.tolist() if self.b.segs[i][2] == self.w["w_north"])
        self.assertAlmostEqual(north, 2500, delta=1)

    def test_one_sided_piece_crosses_a_short_unnamed_cut(self):
        # both sides stop at an unnamed junction 40 m long; the road goes on past it to x = 3540
        idx, orphan = route_piece(self.t, {"name": "Đại lộ C", "from": "Phố C1"}, CW_BBOX)
        self.assertIsNone(orphan)
        joins = self.b.seg_ids(lambda wi, p, q: wi == self.w["c_cut"] and p[1] == q[1])     # along the road, not across
        self.assertEqual(len(joins), 2)
        expected = (self.of_way("c_south", 500, 1500) | self.of_way("c_north", 500, 1500) | joins
                    | self.of_way("c_south_on", -1e9, 1e9) | self.of_way("c_north_on", -1e9, 1e9))
        self.assertEqual(set(idx.tolist()), expected)

    def test_cut_between_two_crossings_is_crossed_and_the_piece_stops_at_the_crossing(self):
        # the crossing "QL77" is a ref-only road, matched by its ref; the cut between the crossings is crossed by
        # the unnamed junction, and the piece ends at QL.77 although the road goes on past it
        idx, orphan = route_piece(self.t, {"name": "Đại lộ T", "from": "Phố T1", "to": "QL77"}, CW_BBOX)
        self.assertIsNone(orphan)
        expected = self.of_way("t_west", 500, 1500) | self.of_way("t_cut", -1e9, 1e9) | self.of_way("t_east", 1540, 2540)
        self.assertEqual(set(idx.tolist()), expected)

    def test_crossing_by_a_ref_that_no_road_carries_is_an_orphan(self):
        piece = {"name": "Đại lộ T", "from": "Phố T1", "to": "QL78"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertEqual((len(idx), orphan), (0, {"route": piece, "missing": "to"}))

    def test_one_sided_piece_cut_by_a_street_is_reported(self):
        # the cut is the street "Phố Cắt": it is not crossed, and both cut sides are reported where the piece stops
        piece = {"name": "Đại lộ K", "from": "Phố K1"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assert_gap_at(orphan, piece, [(1500, -25000), (1500, -25000 + 30)])
        self.assertEqual(set(idx.tolist()), self.of_way("k_south", 500, 1500) | self.of_way("k_north", 500, 1500))

    def test_bridge_across_the_road_is_not_taken_in(self):
        # the overpass joins lanes of opposite directions: no carriageway runs on through it, so it fills no gap
        idx, orphan = route_piece(self.t, {"name": "Đại lộ R", "from": "Phố R1", "to": "Phố R2"}, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertEqual(self.ways(idx), {self.w["r_south"], self.w["r_lane_w"], self.w["r_lane_e"]})

    def test_carriageway_orphans_reach_the_rule(self):
        r = ScopeResolver(self.t).resolve({"routes": [{"name": "Đại lộ S", "from": "Phố S1"}], "bbox": CW_BBOX},
                                          exempt={"routes": [{"name": "Đại lộ S", "from": "Phố S1", "to": "Phố S2"}]})
        self.assertEqual([o["missing"] for o in r.orphans], ["carriageway", "carriageway"])
        self.assertEqual(set(r.indices.tolist()), self.of_way("s", 2000, 3000))

    def test_gap_orphans_reach_the_rule_and_zones_tolerate_them(self):
        piece = {"name": "Đại lộ U", "from": "Phố U1", "to": "Phố U2"}
        r = ScopeResolver(self.t).resolve({"routes": [piece], "bbox": CW_BBOX})
        self.assertEqual([o["missing"] for o in r.orphans], ["gap"])
        # a zone bounded by "Đại lộ U" (its north side holes at x 1400..1600) and a ring north of it
        b, _, w = build_carriageway_fixture()
        y = -17000
        b.way([(0, y - 100), (0, y), (0, y + 1000)], name="Biên U Tây")
        b.way([(0, y + 1000), (3000, y + 1000)], name="Biên U Bắc")
        b.way([(3000, y + 1000), (3000, y), (3000, y - 100)], name="Biên U Đông")
        inner = b.way([(1000, y + 500), (2000, y + 500)], name="Phố Trong U")
        t = b.build()
        boundary = [{"name": "Đại lộ U", "from": "Biên U Tây", "to": "Biên U Đông"}, "Biên U Tây", "Biên U Bắc",
                    "Biên U Đông"]
        self.assertEqual(route_piece(t, boundary[0], CW_BBOX)[1]["missing"], "gap")
        z = zone_segments(t, boundary, list(ll(1500, y + 600)), CW_BBOX, (1, 4))
        self.assertEqual(set(z.indices.tolist()), b.seg_ids(lambda wi, p, q: wi == inner))

    def test_zone_boundary_piece_takes_in_the_other_carriageway_and_still_seals(self):
        piece = {"name": "Đại lộ Z", "from": "Biên Tây"}
        idx, orphan = route_piece(self.t, piece, CW_BBOX)
        self.assertIsNone(orphan)
        self.assertTrue(self.z_north <= set(idx.tolist()))
        z = zone_segments(self.t, [piece, "Biên Đông", "Biên Bắc", "Biên Tây"], list(ll(1500, 1200)), CW_BBOX, (3, 8))
        self.assertEqual(set(z.indices.tolist()), self.inner)

    def test_full_pieces_report_no_carriageway(self):
        for piece in ({"name": "Biên Đông", "from": "Biên Bắc"},
                      {"name": "Biên Bắc", "from": "Biên Tây", "to": "Biên Đông"}):
            self.assertIsNone(route_piece(self.t, piece, CW_BBOX)[1], piece)


class RemapTests(unittest.TestCase):
    """Anchors on a map where way ids changed: the node pair is followed."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from sm_remap_fixture import build_maps
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.old, cls.new = build_maps(cls.temp.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_way_split_anchor_found_by_node_pair(self):
        r = ScopeResolver(self.new).resolve({"segments": [[1, 11, 12]]})
        self.assertEqual(len(r.indices), 1)
        self.assertEqual(r.orphans, [])
        self.assertEqual(r.moved, [{"anchor": [1, 11, 12], "way_ids": [91]}])

    def test_gone_pair_is_an_orphan(self):
        r = ScopeResolver(self.new).resolve({"segments": [[5, 51, 52]]})
        self.assertEqual(r.orphans, [{"anchor": [5, 51, 52]}])
        self.assertEqual((len(r.indices), r.moved), (0, []))

    def test_exact_anchor_is_not_moved(self):
        r = ScopeResolver(self.old).resolve({"segments": [[1, 11, 12]]})
        self.assertEqual((len(r.indices), r.orphans, r.moved), (1, [], []))


if __name__ == "__main__":
    unittest.main()
