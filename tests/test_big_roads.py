"""Offline tests for the big-road (A) network policy and builder."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
from osm_classes import big_road_group, is_big_road


class BigRoadPolicyTests(unittest.TestCase):
    def test_motorway_and_trunk_tags_are_big_roads(self):
        for highway, group in [("motorway", "caotoc"), ("motorway_link", "caotoc"),
                               ("trunk", "quoclo"), ("trunk_link", "quoclo")]:
            self.assertTrue(is_big_road(highway))
            self.assertEqual(big_road_group(highway), group)

    def test_national_refs_keep_roads_with_lower_tags(self):
        self.assertEqual(big_road_group("primary", "QL.1"), "quoclo")
        self.assertEqual(big_road_group("secondary", "QL1A"), "quoclo")
        self.assertEqual(big_road_group("residential", "AH1;QL.1"), "quoclo")
        self.assertEqual(big_road_group("primary", "CT.01"), "caotoc")
        self.assertEqual(big_road_group("tertiary", "AH14"), "quoclo")

    def test_provincial_refs_are_big_roads_below_national_ones(self):
        self.assertEqual(big_road_group("secondary", "ĐT.70"), "tinhlo")
        self.assertEqual(big_road_group("primary", "DT741"), "tinhlo")
        self.assertEqual(big_road_group("tertiary", "TL 10"), "tinhlo")
        self.assertEqual(big_road_group("secondary", "ĐVB;ĐT.392"), "tinhlo")
        self.assertEqual(big_road_group("primary", "QL.2;ĐT.3"), "quoclo")
        self.assertEqual(big_road_group("trunk", "ĐT.5"), "quoclo")
        self.assertEqual(big_road_group("secondary", "CT.01;ĐT.5"), "caotoc")

    def test_local_roads_and_unbuilt_roads_are_excluded(self):
        for highway, ref in [("primary", ""), ("secondary", "ĐH.12"), ("residential", ""), ("construction", "ĐT.5"),
                             ("construction", "QL.1"), ("proposed", "CT.02"), ("footway", "QL.5"), ("", "QL.1")]:
            self.assertFalse(is_big_road(highway, ref), (highway, ref))
            self.assertIsNone(big_road_group(highway, ref))


class FreeFlowSpeedTests(unittest.TestCase):
    def test_numeric_kmh_maxspeed_is_parsed_and_other_forms_are_ignored(self):
        from osm_classes import parse_maxspeed
        for value, expected in [("60", 60.0), ("60 km/h", 60.0), ("80km/h", 80.0), (" 50 ", 50.0), ("30 mph", None),
                                ("none", None), ("VN:urban", None), ("", None), (None, None), ("0", None), ("60;80", None)]:
            self.assertEqual(parse_maxspeed(value), expected, value)

    def test_maxspeed_is_reduced_like_osrm_otherwise_the_profile_default_by_highway(self):
        from osm_classes import free_flow_speed
        self.assertEqual(free_flow_speed("primary", "60"), (48.0, "osm_maxspeed"))
        self.assertEqual(free_flow_speed("trunk", ""), (85, "profile_default"))
        self.assertEqual(free_flow_speed("trunk", "40 mph"), (85, "profile_default"))
        self.assertEqual(free_flow_speed("motorway_link", None), (45, "profile_default"))

    def test_unknown_highway_has_no_speed_rather_than_a_borrowed_one(self):
        from osm_classes import free_flow_speed
        self.assertEqual(free_flow_speed("track", ""), (None, "unknown"))
        # The car profile does not drive such a way at all, so its own maxspeed does not make it known.
        self.assertEqual(free_flow_speed("track", "30"), (None, "unknown"))
        self.assertEqual(free_flow_speed("", ""), (None, "unknown"))

    def test_default_speeds_match_the_osrm_car_profile(self):
        import re
        from osm_classes import BIG_ROAD_TAGS, DEFAULT_SPEEDS_KMH, DRIVABLE_TAGS, SPEED_REDUCTION
        profile = Path(__file__).resolve().parents[1] / "osrm/tools/profiles/car.lua"
        if not profile.is_file():
            self.skipTest("osrm/tools/profiles/car.lua is absent")
        text = profile.read_text(encoding="utf-8")
        block = re.search(r"speeds\s*=\s*Sequence\s*\{\s*highway\s*=\s*\{(.*?)\}", text, re.S).group(1)
        lua = {key: int(value) for key, value in re.findall(r"^\s*(\w+)\s*=\s*(\d+)", block, re.M)}
        self.assertEqual({key: lua.get(key) for key in DEFAULT_SPEEDS_KMH}, DEFAULT_SPEEDS_KMH)
        # Every A-network highway tag the profile knows has a default here.
        self.assertEqual({key for key in lua if key in DRIVABLE_TAGS | set(BIG_ROAD_TAGS)}, set(DEFAULT_SPEEDS_KMH))
        self.assertEqual(float(re.search(r"speed_reduction\s*=\s*([\d.]+)", text).group(1)), SPEED_REDUCTION)


NODES = {1: (21.0, 105.800), 7: (21.0, 105.8005), 2: (21.0, 105.801), 3: (21.0, 105.802), 4: (21.0, 105.803),
         5: (21.0, 105.804), 20: (21.001, 105.801), 30: (20.999, 105.802), 50: (21.001, 105.804),
         60: (21.1, 105.9), 61: (21.1, 105.9002), 70: (20.999, 105.805)}
WAYS = [(10, [1, 7, 2, 3, 4], {"highway": "trunk", "ref": "QL.1", "name": "Ngọc Hồi"}),
        (11, [4, 5], {"highway": "primary", "ref": "QL.1", "name": "Giải Phóng"}),
        (12, [2, 20], {"highway": "secondary", "name": "Kim Giang"}),
        (13, [3, 30], {"highway": "tertiary", "name": "Quan Nhân"}),
        (14, [5, 50], {"highway": "residential", "name": "Ngõ 1"}),
        (15, [60, 61], {"highway": "motorway", "ref": "CT.01", "oneway": "yes"}),
        (16, [5, 70], {"highway": "construction", "ref": "QL.1"})]


# A mainland box (lat 20.9-21.2, lon 105.7-106.0) closed by a coastline that spills past
# both border ends, plus one island south of the coast carrying a national road. National roads
# also leave the box: QL.4 crosses between nodes, QL.5 is a dual carriageway 50 m apart, QL.6 shares
# a border node, QL.7 crosses only the maritime border and QL.11 ends 156 m inside the border.
LAND_NODES = {1001: (20.9, 105.7), 1002: (21.2, 105.7), 1003: (21.2, 106.0), 1004: (20.9, 106.0),
              1005: (20.7, 106.0), 1006: (20.7, 105.7), 1007: (21.05, 105.7),
              4001: (21.15, 105.8), 4002: (21.25, 105.8), 4003: (21.15, 105.9), 4004: (21.25, 105.9),
              4005: (21.25, 105.9005), 4006: (21.15, 105.9005), 4007: (21.05, 105.65), 4008: (21.05, 105.75),
              4009: (20.75, 105.9), 4010: (20.65, 105.9), 5001: (21.209, 105.8),
              4011: (21.1, 105.699), 4012: (21.1, 105.7015),
              2001: (20.9, 105.65), 2002: (20.9, 105.71), 2003: (20.9, 105.85), 2004: (20.9, 105.99), 2005: (20.9, 106.05),
              3001: (20.8, 105.8), 3002: (20.8, 105.82), 3003: (20.82, 105.82),
              3004: (20.81, 105.805), 3005: (20.81, 105.815), 3006: (20.811, 105.816)}
LAND_WAYS = [(100, [1001, 1007, 1002, 1003], {"boundary": "administrative", "admin_level": "2"}),
             (101, [1004, 1003], {"boundary": "administrative", "admin_level": "2"}),
             (102, [1004, 1005, 1006, 1001], {"boundary": "administrative", "admin_level": "2", "maritime": "yes"}),
             (200, [2001, 2002, 2003], {"natural": "coastline"}),
             (201, [2003, 2004, 2005], {"natural": "coastline"}),
             (202, [3001, 3002, 3003, 3001], {"natural": "coastline"}),
             (17, [3004, 3005], {"highway": "trunk", "ref": "QL.99", "name": "Đảo"}),
             (18, [3005, 3006], {"highway": "secondary", "name": "Bến"}),
             (19, [4001, 4002], {"highway": "trunk", "ref": "QL.4"}),
             (20, [4003, 4004], {"highway": "trunk", "ref": "QL.5", "name": "Chiều đi"}),
             (21, [4005, 4006], {"highway": "trunk", "ref": "QL.5", "name": "Chiều về"}),
             (22, [4007, 1007, 4008], {"highway": "trunk", "ref": "QL.6"}),
             (23, [4009, 4010], {"highway": "trunk", "ref": "QL.7"}),
             (24, [4011, 4012], {"highway": "trunk", "ref": "QL.11"})]
LAND_NODE_TAGS = {5001: {"barrier": "border_control", "name": "Cửa khẩu Thử"}}
LAND_RELATIONS = [(1, [100, 101, 102], {"type": "boundary", "boundary": "administrative", "admin_level": "2", "ISO3166-1": "VN"})]


def read_edges(out_dir):
    """({(a, b, way_id): kind}, {key: value}) from big_road_edges.sqlite."""
    db = sqlite3.connect(f"{(Path(out_dir) / 'big_road_edges.sqlite').as_uri()}?mode=ro", uri=True)
    try:
        edges = {(a, b, way_id): kind for a, b, way_id, kind in db.execute("SELECT a, b, way_id, kind FROM edges")}
        return edges, dict(db.execute("SELECT key, value FROM metadata"))
    finally:
        db.close()


def write_osm(path, nodes=NODES, ways=WAYS, relations=(), node_tags=None):
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6'>"]
    for i, (lat, lon) in nodes.items():
        tags = "".join(f"<tag k='{k}' v='{v}'/>" for k, v in (node_tags or {}).get(i, {}).items())
        lines.append(f"<node id='{i}' version='1' lat='{lat}' lon='{lon}'>{tags}</node>")
    for way_id, refs, tags in ways:
        lines.append(f"<way id='{way_id}' version='1'>")
        lines += [f"<nd ref='{n}'/>" for n in refs]
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</way>")
    for relation_id, members, tags in relations:
        lines.append(f"<relation id='{relation_id}' version='1'>")
        # A member is a way id (role outer) or (ref, role[, type]).
        for member in members:
            ref, role, kind = (member, "outer", "way") if isinstance(member, int) else (tuple(member) + ("way",))[:3]
            lines.append(f"<member type='{kind}' ref='{ref}' role='{role}'/>")
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</relation>")
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")


class BigRoadBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_big_roads import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_osm(root / "tiny.osm")
        cls.meta = build(root / "tiny.osm", root / "out", tolerance_m=15)
        cls.roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))
        cls.junctions = json.loads((root / "out/big_road_junctions.geojson").read_text(encoding="utf-8"))
        cls.meta_file = json.loads((root / "out/big_roads.meta.json").read_text(encoding="utf-8"))
        cls.edges, cls.edge_metadata = read_edges(root / "out")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def road(self, way_id):
        return next(f for f in self.roads["features"] if f["properties"]["way_id"] == way_id)

    def test_only_big_roads_are_kept(self):
        self.assertEqual(sorted(f["properties"]["way_id"] for f in self.roads["features"]), [10, 11, 15])
        self.assertFalse(self.road(10)["properties"]["ref_only"])
        self.assertTrue(self.road(11)["properties"]["ref_only"])
        self.assertEqual(self.road(11)["properties"]["group"], "quoclo")
        self.assertEqual(self.road(15)["properties"]["group"], "caotoc")
        self.assertTrue(self.road(15)["properties"]["oneway"])

    def test_junctions_with_b_and_c_roads_only(self):
        found = {f["properties"]["node_id"]: f["properties"] for f in self.junctions["features"] if f["properties"]["type"] in ("B", "C")}
        self.assertEqual(sorted(found), [2, 3])
        self.assertEqual(found[2]["type"], "B")
        self.assertEqual(found[2]["joins"], ["Kim Giang"])
        self.assertEqual(found[3]["type"], "C")
        self.assertEqual(found[3]["big_road"], "Ngọc Hồi (QL.1)")
        self.assertEqual(self.junctions["features"][0]["geometry"]["type"], "Point")

    def test_simplification_drops_straight_points_but_keeps_junctions(self):
        coords = self.road(10)["geometry"]["coordinates"]
        self.assertEqual(coords, [[105.8, 21.0], [105.801, 21.0], [105.802, 21.0], [105.803, 21.0]])

    def test_isolated_fragments_are_marked(self):
        self.assertFalse(self.road(10)["properties"]["isolated"])
        self.assertEqual(self.road(10)["properties"]["component"], self.road(11)["properties"]["component"])
        self.assertTrue(self.road(15)["properties"]["isolated"])
        self.assertEqual(self.meta["components"], 2)

    def test_metadata_summarises_the_build(self):
        self.assertEqual(self.meta, self.meta_file)
        self.assertEqual(self.meta["ways"], 3)
        self.assertEqual(self.meta["junctions"], {"B": 1, "C": 1, "border": 0, "deadend": 4})
        self.assertEqual(len(self.meta["pbf_sha256"]), 64)
        self.assertGreater(self.meta["km"]["quoclo"], 0)
        self.assertEqual(self.meta["ref_only_km"], self.meta["km_by_tag"]["primary"])

    def test_edge_index_holds_the_original_node_pairs_of_a_ways_sorted(self):
        # Node 7 is simplified out of the drawn line but its pairs are kept; pairs are stored with a < b.
        self.assertEqual({key: kind for key, kind in self.edges.items() if key[2] == 10},
                         {(1, 7, 10): "a", (2, 7, 10): "a", (2, 3, 10): "a", (3, 4, 10): "a"})
        self.assertEqual(self.edges[(4, 5, 11)], "a")
        self.assertEqual(self.meta["edge_pairs"], len(self.edges))

    def test_edge_index_leaves_out_fragments(self):
        self.assertTrue(self.road(15)["properties"]["fragment"])
        self.assertFalse(any(key[2] == 15 for key in self.edges))

    def test_edge_index_records_the_build_and_leaves_no_temp_file(self):
        from osm_classes import BIG_ROAD_POLICY_VERSION
        self.assertEqual(self.edge_metadata, {"pbf_sha256": self.meta["pbf_sha256"], "policy_version": str(BIG_ROAD_POLICY_VERSION)})
        self.assertEqual(sorted(p.name for p in (Path(self.temp.name) / "out").iterdir() if "tmp" in p.name), [])

    def test_map_without_country_relation_has_no_boundary_file(self):
        self.assertTrue(self.meta["boundary_status"].startswith("failed"))
        self.assertIn("relation", self.meta["boundary_status"])
        self.assertEqual(self.meta["boundary_points"], 0)
        self.assertFalse((Path(self.temp.name) / "out/vietnam_boundary.geojson").exists())

    def test_every_road_has_a_free_flow_speed(self):
        self.assertEqual((self.road(10)["properties"]["speed_kmh"], self.road(10)["properties"]["speed_source"]), (85, "profile_default"))
        self.assertEqual(self.road(11)["properties"]["speed_kmh"], 65)
        self.assertEqual(self.road(15)["properties"]["speed_kmh"], 90)

    def test_map_without_country_boundary_has_no_land_and_no_islands(self):
        self.assertNotEqual(self.meta["land_status"], "ok")
        self.assertFalse((Path(self.temp.name) / "out/vietnam_land.geojson").exists())
        self.assertFalse(any(f["properties"]["island"] for f in self.roads["features"] + self.junctions["features"]))
        self.assertEqual(self.meta["junctions"]["border"], 0)


class BigRoadLandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_big_roads import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_osm(root / "land.osm", {**NODES, **LAND_NODES}, WAYS + LAND_WAYS, LAND_RELATIONS, LAND_NODE_TAGS)
        cls.meta = build(root / "land.osm", root / "out", tolerance_m=15)
        cls.roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))
        cls.junctions = json.loads((root / "out/big_road_junctions.geojson").read_text(encoding="utf-8"))
        cls.land = json.loads((root / "out/vietnam_land.geojson").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_land_is_the_land_border_closed_by_the_trimmed_coastline(self):
        self.assertEqual(self.meta["land_status"], "ok")
        ring = self.land["geometry"]["coordinates"][0]
        self.assertEqual(self.land["geometry"]["type"], "Polygon")
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual({tuple(p) for p in ring}, {(105.7, 20.9), (105.7, 21.2), (106.0, 21.2), (106.0, 20.9), (105.99, 20.9), (105.71, 20.9)})
        self.assertEqual(self.land["properties"]["points"], len(ring))
        self.assertEqual(self.meta["land_points"], len(ring))

    def test_country_boundary_ring_keeps_the_maritime_border(self):
        self.assertEqual(self.meta["boundary_status"], "ok")
        boundary = json.loads((Path(self.temp.name) / "out/vietnam_boundary.geojson").read_text(encoding="utf-8"))
        self.assertEqual((boundary["type"], boundary["geometry"]["type"]), ("Feature", "MultiPolygon"))
        polygon, = boundary["geometry"]["coordinates"]
        ring, = polygon
        self.assertEqual(ring[0], ring[-1])
        # Way 101 runs backwards and way 102 is the maritime border reaching lat 20.7. Points on straight edges are
        # simplified away, except the ring's start, which simplification keeps.
        corners = {(105.7, 21.2), (106.0, 21.2), (106.0, 20.7), (105.7, 20.7)}
        self.assertEqual({tuple(p) for p in ring} - {tuple(ring[0])}, corners - {tuple(ring[0])})
        self.assertEqual(boundary["properties"], {"source": "osm_admin_level_2", "tolerance_m": 100.0, "points": len(ring)})
        self.assertEqual(self.meta["boundary_points"], len(ring))

    def test_roads_and_junctions_on_islands_are_marked(self):
        island = {f["properties"]["way_id"]: f["properties"]["island"] for f in self.roads["features"]}
        self.assertEqual(island, {10: False, 11: False, 15: False, 17: True, 19: False, 20: False, 21: False, 22: False, 23: True, 24: False})
        junctions = {f["properties"]["node_id"]: f["properties"]["island"] for f in self.junctions["features"] if f["properties"]["type"] in ("B", "C")}
        self.assertEqual(junctions, {2: False, 3: False, 3005: True})
        self.assertEqual(self.meta["island_ways"], 2)
        self.assertEqual(self.meta["island_junctions"], 1)
        self.assertGreater(self.meta["island_km"], 0)

    def border_points(self):
        return sorted((f for f in self.junctions["features"] if f["properties"]["type"] == "border"),
                      key=lambda f: f["geometry"]["coordinates"])

    def test_big_roads_crossing_the_land_border_are_found_once_per_crossing(self):
        points = self.border_points()
        self.assertEqual(len(points), 4)
        self.assertEqual(self.meta["junctions"]["border"], 4)
        west, ql11, ql4, ql5 = points
        self.assertEqual(ql11["geometry"]["coordinates"], [105.7, 21.1])
        self.assertEqual(west["geometry"]["coordinates"], [105.7, 21.05])
        self.assertEqual(west["properties"]["node_id"], 1007)
        self.assertEqual(ql4["geometry"]["coordinates"], [105.8, 21.2])
        self.assertIsNone(ql4["properties"]["node_id"])
        self.assertEqual(ql5["properties"]["big_road"], "Chiều về (QL.5) / Chiều đi (QL.5)")
        self.assertAlmostEqual(ql5["geometry"]["coordinates"][0], 105.90025, places=6)
        for point in points:
            self.assertEqual(point["properties"]["joins"], [])
            self.assertFalse(point["properties"]["island"])

    def test_dead_ends_at_the_border_are_skipped_and_those_abroad_are_off_the_mainland(self):
        dead = {f["properties"]["node_id"]: f["properties"] for f in self.junctions["features"] if f["properties"]["type"] == "deadend"}
        self.assertNotIn(4011, dead)
        self.assertNotIn(4012, dead)
        self.assertTrue(dead[4002]["island"])
        self.assertFalse(dead[4008]["island"])

    def test_border_crossings_take_the_nearby_border_control_name(self):
        names = {f["properties"]["big_road"]: f["properties"]["crossing"] for f in self.border_points()}
        self.assertEqual(names["QL.4"], "Cửa khẩu Thử")
        self.assertEqual(names["QL.6"], "")


class BigRoadBoundaryTests(unittest.TestCase):
    def test_boundary_holds_every_closed_outer_ring_from_tagged_and_untagged_ways(self):
        from build_big_roads import build
        # Outer ring 140-142 (142 untagged, 141 reversed), a separate outer ring 144 (an islet's waters), a 20 m
        # outer ring 145 that collapses when simplified, an inner ring 143 and a node member.
        nodes = {14001: (20.9, 105.7), 14002: (21.2, 105.7), 14003: (21.2, 106.0), 14004: (20.9, 106.0),
                 14005: (21.0, 105.8), 14006: (21.05, 105.8), 14007: (21.05, 105.85), 14008: (21.0, 105.9),
                 14010: (20.5, 107.0), 14011: (20.5, 107.05), 14012: (20.55, 107.05),
                 14020: (20.6, 107.2), 14021: (20.6, 107.2002), 14022: (20.6002, 107.2002)}
        admin = {"boundary": "administrative", "admin_level": "2"}
        ways = WAYS + [(140, [14001, 14002], admin), (141, [14003, 14002], admin), (142, [14003, 14004, 14001], {}),
                       (143, [14005, 14006, 14007, 14005], admin), (144, [14010, 14011, 14012, 14010], {**admin, "maritime": "yes"}),
                       (145, [14020, 14021, 14022, 14020], {**admin, "maritime": "yes"})]
        relation = (2, [140, (141, "outer"), 142, (143, "inner"), 144, 145, (14008, "label", "node")],
                    {"type": "boundary", "boundary": "administrative", "admin_level": "2", "ISO3166-1": "VN"})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_osm(root / "boundary.osm", {**NODES, **nodes}, ways, [relation])
            meta = build(root / "boundary.osm", root / "out")
            boundary = json.loads((root / "out/vietnam_boundary.geojson").read_text(encoding="utf-8"))
        self.assertEqual(meta["boundary_status"], "ok")
        self.assertEqual(meta["boundary_rings"], 2)
        self.assertEqual(boundary["geometry"]["type"], "MultiPolygon")
        # Largest ring first; each polygon is one ring without holes.
        (main,), (islet,) = boundary["geometry"]["coordinates"]
        self.assertEqual({tuple(p) for p in main}, {(105.7, 20.9), (105.7, 21.2), (106.0, 21.2), (106.0, 20.9)})
        self.assertEqual(len(main), 5)
        self.assertEqual({tuple(p) for p in islet}, {(107.0, 20.5), (107.05, 20.5), (107.05, 20.55)})
        self.assertEqual(boundary["properties"]["points"], len(main) + len(islet))
        self.assertEqual(meta["boundary_points"], len(main) + len(islet))
        self.assertNotEqual(meta["land_status"], "ok")

    def test_outer_ways_that_never_close_give_a_reason(self):
        from build_big_roads import assemble_boundary
        ways = {1: [(1, (105.0, 21.0)), (2, (105.1, 21.0))], 2: [(3, (105.1, 21.1)), (2, (105.1, 21.0))]}
        rings, status = assemble_boundary(ways, [1, 2, 3], 100)
        self.assertIsNone(rings)
        self.assertTrue(status.startswith("failed"))
        self.assertEqual(assemble_boundary(ways, [], 100)[1], "failed: no admin_level=2 ISO3166-1=VN relation")


# Free-flow speeds (lat 23.4-23.5): QL.80 without maxspeed, a 1.25 km ferry crossing in 15 min, QL.80 with
# maxspeed=60, and QL.81 (maxspeed in mph) whose gap OSRM drives along a track, a highway type with no default speed.
SPEED_NODES = {13001: (23.4, 105.0), 13002: (23.4, 105.01), 13003: (23.4112289, 105.01), 13004: (23.4112289, 105.02),
               13011: (23.5, 105.0), 13012: (23.5, 105.01), 13013: (23.5, 105.013), 13014: (23.5, 105.023)}
SPEED_WAYS = [(800, [13001, 13002], {"highway": "trunk", "ref": "QL.80"}),
              (801, [13002, 13003], {"route": "ferry", "name": "Phà Tốc Độ", "motor_vehicle": "yes"}),
              (802, [13003, 13004], {"highway": "primary", "ref": "QL.80", "maxspeed": "60"}),
              (810, [13011, 13012], {"highway": "trunk", "ref": "QL.81", "maxspeed": "50 mph"}),
              (811, [13012, 13013], {"highway": "track", "maxspeed": "30"}),
              (812, [13013, 13014], {"highway": "trunk", "ref": "QL.81"})]
SPEED_ROUTES = {(13002, 13003): ([13002, 13003], [1250], [900]), (13003, 13002): ([13003, 13002], [1250], [900]),
                (13012, 13013): ([13012, 13013], [306], [60]), (13013, 13012): ([13013, 13012], [306], [60])}


class BigRoadSpeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_big_roads import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_osm(root / "speed.osm", SPEED_NODES, SPEED_WAYS)
        build(root / "speed.osm", root / "out", tolerance_m=0, fragment_km=0, router=FakeRouter(SPEED_NODES, SPEED_ROUTES))
        cls.roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))["features"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def speed(self, way_id):
        props, = [f["properties"] for f in self.roads if f["properties"]["way_id"] == way_id]
        return props["speed_kmh"], props["speed_source"]

    def test_maxspeed_is_reduced_by_the_profile_factor(self):
        self.assertEqual(self.speed(802), (48, "osm_maxspeed"))

    def test_roads_without_numeric_kmh_maxspeed_take_the_profile_default(self):
        self.assertEqual(self.speed(800), (85, "profile_default"))
        self.assertEqual(self.speed(810), (85, "profile_default"))

    def test_ferry_speed_is_its_length_over_its_crossing_time(self):
        speed, source = self.speed(801)
        self.assertEqual(source, "ferry")
        self.assertAlmostEqual(speed, 5.0, places=1)

    def test_bridge_through_a_highway_without_default_has_no_speed(self):
        self.assertEqual(self.speed(811), (None, "unknown"))


# Gaps in the A network (lat 21.5-21.8). QL.8 misses its ref for 311 m, where the unnumbered primary also has a
# 111 m stub past the gap and a tertiary joins mid-gap. ĐT.9 has a 2.48 km gap, QL.10 a residential gap, and a
# 10 m trunk_link sits between two unnumbered primaries.
GAP_NODES = {8001: (21.5, 105.0), 8002: (21.5, 105.01), 8009: (21.5, 105.0115), 8003: (21.5, 105.013),
             8004: (21.5, 105.023), 8010: (21.501, 105.01), 8008: (21.499, 105.0115),
             8101: (21.6, 105.0), 8102: (21.6, 105.005), 8103: (21.6, 105.029), 8104: (21.6, 105.034),
             8201: (21.7, 105.0), 8202: (21.7, 105.005), 8203: (21.7, 105.007), 8204: (21.7, 105.012),
             8300: (21.8, 105.0), 8301: (21.8, 105.001), 8302: (21.8, 105.0011), 8303: (21.8, 105.002)}
GAP_WAYS = [(300, [8001, 8002], {"highway": "trunk", "ref": "QL.8"}),
            (301, [8010, 8002, 8009, 8003], {"highway": "primary", "name": "Phố Huyện"}),
            (302, [8003, 8004], {"highway": "trunk", "ref": "QL.8"}),
            (303, [8008, 8009], {"highway": "tertiary", "name": "Xóm"}),
            (310, [8101, 8102], {"highway": "secondary", "ref": "ĐT.9"}),
            (311, [8102, 8103], {"highway": "primary"}),
            (312, [8103, 8104], {"highway": "secondary", "ref": "ĐT.9"}),
            (320, [8201, 8202], {"highway": "trunk", "ref": "QL.10"}),
            (321, [8202, 8203], {"highway": "residential"}),
            (322, [8203, 8204], {"highway": "trunk", "ref": "QL.10"}),
            (330, [8301, 8302], {"highway": "trunk_link"}),
            (331, [8300, 8301], {"highway": "primary"}),
            (332, [8302, 8303], {"highway": "primary"})]


class BigRoadGapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_big_roads import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_osm(root / "gaps.osm", GAP_NODES, GAP_WAYS)
        cls.meta = build(root / "gaps.osm", root / "out", tolerance_m=15, bridge_km=2.0, fragment_km=0.1)
        cls.roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))["features"]
        cls.junctions = json.loads((root / "out/big_road_junctions.geojson").read_text(encoding="utf-8"))["features"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def points(self, kind):
        return {f["properties"]["node_id"]: f["properties"] for f in self.junctions if f["properties"]["type"] == kind}

    def roads_of(self, way_id):
        return [f for f in self.roads if f["properties"]["way_id"] == way_id]

    def test_short_gap_is_bridged_along_the_real_road(self):
        bridge, = self.roads_of(301)
        self.assertTrue(bridge["properties"]["bridge"])
        self.assertEqual(bridge["properties"]["group"], "quoclo")
        self.assertEqual(bridge["geometry"]["coordinates"], [[105.01, 21.5], [105.0115, 21.5], [105.013, 21.5]])
        self.assertFalse(bridge["properties"]["ref_only"])
        self.assertEqual((bridge["properties"]["speed_kmh"], bridge["properties"]["speed_source"]), (65, "profile_default"))
        edges, _ = read_edges(Path(self.temp.name) / "out")
        self.assertEqual({key: kind for key, kind in edges.items() if key[2] == 301}, {(8002, 8009, 301): "road", (8003, 8009, 301): "road"})
        self.assertFalse(any(key[2] == 330 for key in edges))
        ql8 ={f["properties"]["component"] for f in self.roads_of(300) + self.roads_of(302) + [bridge]}
        self.assertEqual(ql8, {0})
        self.assertFalse(self.roads_of(300)[0]["properties"]["bridge"])

    def test_bridge_junctions_come_from_other_roads_and_from_the_road_continuing_past_the_gap(self):
        b, c = self.points("B"), self.points("C")
        self.assertEqual(c[8009]["joins"], ["Xóm"])
        self.assertEqual(b[8002]["joins"], ["Phố Huyện"])
        self.assertNotIn(8003, b)
        self.assertNotIn(8009, b)

    def test_gaps_that_are_too_long_or_only_on_small_roads_stay_dead_ends(self):
        self.assertEqual(self.roads_of(311), [])
        self.assertEqual(self.roads_of(321), [])
        dead = self.points("deadend")
        self.assertTrue({8102, 8103, 8202, 8203} <= set(dead))
        self.assertNotIn(8002, dead)
        self.assertNotIn(8003, dead)
        self.assertEqual(dead[8102]["big_road"], "ĐT.9")
        self.assertEqual(dead[8102]["joins"], [])

    def test_dead_ends_on_national_and_expressway_mainlines_are_flagged(self):
        dead = self.points("deadend")
        self.assertTrue(dead[8202]["mainline"])
        self.assertFalse(dead[8102]["mainline"])
        self.assertFalse(dead[8301]["mainline"])

    def test_short_isolated_fragments_and_their_points_are_marked(self):
        link, = self.roads_of(330)
        self.assertTrue(link["properties"]["fragment"])
        self.assertFalse(self.roads_of(310)[0]["properties"]["fragment"])
        self.assertFalse(self.roads_of(300)[0]["properties"]["fragment"])
        b = self.points("B")
        self.assertTrue(b[8301]["fragment"])
        self.assertFalse(b[8002]["fragment"])
        self.assertTrue(self.points("deadend")[8301]["fragment"])

    def test_metadata_counts_bridges_dead_ends_and_fragments(self):
        meta = self.meta
        self.assertEqual(meta["bridges"], 1)
        self.assertAlmostEqual(meta["bridge_km"], 0.311, delta=0.01)
        self.assertEqual(meta["dead_ends_before"], 14)
        self.assertEqual(meta["dead_ends_after"], 12)
        self.assertEqual(meta["junctions"]["deadend"], 12)
        self.assertEqual(meta["fragments"], 1)
        self.assertAlmostEqual(meta["fragment_km"], 0.0104, delta=0.001)
        self.assertGreater(meta["km"]["tinhlo"], 1.0)
        self.assertTrue(meta["osrm_bridging"].startswith("skipped"))

    def test_dead_end_reasons_are_recorded_without_osrm(self):
        dead = self.points("deadend")
        self.assertEqual(dead[8102]["reason"], "continues")
        self.assertEqual(dead[8001]["reason"], "end")


# Dead ends that only OSRM can bridge (lat 22.0-22.8), one case per row.
OSRM_NODES = {9001: (22.0, 105.0), 9002: (22.0, 105.01), 9003: (22.0, 105.02), 9004: (22.0, 105.03),
              9101: (22.1, 105.0), 9102: (22.1, 105.01), 9103: (22.1, 105.03), 9104: (22.1, 105.04),
              9201: (22.2, 105.0), 9202: (22.2, 105.01), 9203: (22.2, 105.02), 9204: (22.2, 105.03),
              9301: (22.3, 105.0), 9302: (22.3, 105.01), 9305: (22.3, 105.0115), 9306: (22.3005, 105.0115),
              9303: (22.3, 105.013), 9304: (22.3, 105.023),
              9401: (22.4, 105.0), 9402: (22.4, 105.01), 9403: (22.4, 105.0342), 9404: (22.4, 105.0442),
              9501: (22.5, 105.0), 9502: (22.5, 105.008), 9503: (22.5, 105.01), 9505: (22.501, 105.008),
              9506: (22.502, 105.008), 9507: (22.503, 105.008),
              9601: (22.6, 105.0), 9602: (22.6, 105.01), 9603: (22.6, 105.02), 9604: (22.6, 105.03),
              9701: (22.7, 105.0), 9702: (22.7, 105.01), 9703: (22.7, 105.012), 9704: (22.7, 105.022),
              9801: (22.8, 105.0), 9802: (22.8, 105.01), 9803: (22.8, 105.02),
              9901: (22.9, 105.0), 9902: (22.9, 105.01), 9903: (22.91, 105.0), 9904: (22.9, 105.025), 9905: (22.9, 105.035), 9906: (22.91, 105.035)}
OSRM_WAYS = [(400, [9001, 9002], {"highway": "trunk", "ref": "QL.60"}),
             (401, [9002, 9003], {"route": "ferry", "name": "Phà Đại Ngãi", "motor_vehicle": "yes", "duration": "00:20"}),
             (402, [9003, 9004], {"highway": "trunk", "ref": "QL.60"}),
             (410, [9101, 9102], {"highway": "trunk", "ref": "QL.61"}),
             (411, [9102, 9103], {"route": "ferry"}),
             (412, [9103, 9104], {"highway": "trunk", "ref": "QL.61"}),
             (420, [9201, 9202], {"highway": "trunk", "ref": "QL.62"}),
             (421, [9202, 9203], {"route": "ferry", "motor_vehicle": "no"}),
             (422, [9203, 9204], {"highway": "trunk", "ref": "QL.62"}),
             (430, [9301, 9302], {"highway": "trunk", "ref": "QL.63"}),
             (431, [9302, 9305, 9303], {"highway": "residential", "oneway": "yes"}),
             (433, [9303, 9306, 9302], {"highway": "residential", "oneway": "yes"}),
             (432, [9303, 9304], {"highway": "trunk", "ref": "QL.63"}),
             (440, [9401, 9402], {"highway": "trunk", "ref": "QL.64"}),
             (441, [9402, 9403], {"highway": "residential"}),
             (442, [9403, 9404], {"highway": "trunk", "ref": "QL.64"}),
             (450, [9501, 9502, 9503], {"highway": "trunk", "ref": "QL.65"}),
             (451, [9502, 9505, 9506], {"highway": "service"}),
             (452, [9506, 9507], {"highway": "trunk", "ref": "QL.65B"}),
             (460, [9601, 9602], {"highway": "trunk", "ref": "QL.66"}),
             (462, [9603, 9604], {"highway": "trunk", "ref": "QL.66"}),
             (470, [9701, 9702], {"highway": "trunk", "ref": "QL.67"}),
             (472, [9703, 9704], {"highway": "trunk", "ref": "QL.67"}),
             (480, [9801, 9802], {"highway": "trunk", "ref": "QL.68"}),
             (481, [9802, 9803], {"highway": "construction", "construction": "trunk"}),
             (490, [9901, 9902], {"highway": "trunk", "ref": "QL.69"}),
             (491, [9901, 9903], {"highway": "trunk", "ref": "QL.70"}),
             (492, [9902, 9904], {"route": "ferry", "name": "Phà Xa"}),
             (493, [9904, 9905], {"highway": "trunk", "ref": "QL.69"}),
             (494, [9905, 9906], {"highway": "trunk", "ref": "QL.71"})]
# (from, to) -> (nodes, metres per segment, seconds per segment), as OSRM /route annotations would give them.
OSRM_ROUTES = {(9002, 9003): ([9002, 9003], [1030], [1200]),
               (9003, 9002): ([9003, 9002], [1030], [1200]),
               (9102, 9103): ([9102, 9103], [2060], [2160]),
               (9302, 9303): ([9302, 9305, 9303], [155, 155], [22, 22]),
               (9303, 9302): ([9303, 9306, 9302], [170, 170], [25, 25]),
               (9402, 9403): ([9402, 9403], [2490], [360]),
               (9503, 9506): ([9503, 9502, 9505, 9506], [206, 111, 111], [10, 20, 20]),
               (9702, 9703): ([9702, 9703], [205], [30]),
               # On both banks the nearest other A way is only reached back along the dead end's own road,
               # in both directions; the ferry is the second candidate.
               (9902, 9901): ([9902, 9901], [1030], [80]), (9901, 9902): ([9901, 9902], [1030], [80]),
               (9904, 9905): ([9904, 9905], [1030], [80]), (9905, 9904): ([9905, 9904], [1030], [80]),
               (9902, 9904): ([9902, 9904], [1550], [1800])}


class FakeRouter:
    """Answers like OSRM /table and /route for the fixture, keyed by OSM node."""

    def __init__(self, nodes, routes, failing=(), no_route=()):
        self.node_at = {(lon, lat): node for node, (lat, lon) in nodes.items()}
        self.coords = {node: [lon, lat] for node, (lat, lon) in nodes.items()}
        self.routes, self.failing, self.no_route = routes, set(failing), set(no_route)

    def unavailable(self, pbf_sha256):
        return None

    def table(self, points):
        ids = [self.node_at[tuple(point)] for point in points]
        if ids[0] in self.failing:
            raise OSError("fake OSRM timeout")
        return [[sum(self.routes[(a, b)][1]) if (a, b) in self.routes else None for b in ids] for a in ids]

    def route(self, a, b):
        pair = (self.node_at[tuple(a)], self.node_at[tuple(b)])
        if pair in self.no_route:
            from build_big_roads import NoRoute
            raise NoRoute("OSRM 400 NoRoute: No route found between points")
        nodes, distances, durations = self.routes[pair]
        return {"nodes": nodes, "coords": [self.coords[n] for n in nodes], "distances": distances, "durations": durations}


class BigRoadOsrmBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_big_roads import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_osm(root / "osrm.osm", OSRM_NODES, OSRM_WAYS)
        router = FakeRouter(OSRM_NODES, OSRM_ROUTES, failing={9602})
        cls.meta = build(root / "osrm.osm", root / "out", tolerance_m=0, fragment_km=0, router=router)
        cls.roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))["features"]
        cls.junctions = json.loads((root / "out/big_road_junctions.geojson").read_text(encoding="utf-8"))["features"]
        cls.edges, _ = read_edges(root / "out")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_edge_index_holds_osrm_bridges_and_ferries_by_their_own_way(self):
        self.assertEqual(self.edges[(9002, 9003, 401)], "ferry")
        self.assertEqual({key: kind for key, kind in self.edges.items() if key[2] == 431}, {(9302, 9305, 431): "small", (9303, 9305, 431): "small"})
        self.assertEqual(self.edges[(9001, 9002, 400)], "a")
        self.assertFalse(any(key[2] in (441, 421) for key in self.edges))

    def props(self, way_id):
        return [f["properties"] for f in self.roads if f["properties"]["way_id"] == way_id]

    def dead(self):
        return {f["properties"]["node_id"]: f["properties"] for f in self.junctions if f["properties"]["type"] == "deadend"}

    def test_car_ferry_becomes_one_road_with_its_crossing_time(self):
        ferry, = self.props(401)
        self.assertEqual((ferry["bridge"], ferry["bridge_kind"], ferry["group"]), (True, "ferry", "quoclo"))
        self.assertEqual(ferry["ferry_name"], "Phà Đại Ngãi")
        self.assertEqual((ferry["ferry_min"], ferry["ferry_min_source"], ferry["car_confirmed"]), (20, "osm", True))
        self.assertEqual(self.props(400)[0]["component"], self.props(402)[0]["component"])
        self.assertNotIn(9002, self.dead())
        self.assertNotIn(9003, self.dead())

    def test_later_candidates_are_tried_when_the_nearest_is_only_reached_along_the_big_road(self):
        ferry, = self.props(492)
        self.assertEqual(ferry["bridge_kind"], "ferry")
        self.assertNotIn(9902, self.dead())

    def test_untagged_ferry_uses_the_osrm_time_and_stays_unconfirmed(self):
        ferry, = self.props(411)
        self.assertEqual((ferry["ferry_min"], ferry["ferry_min_source"], ferry["car_confirmed"]), (36, "osrm_5kmh", False))

    def test_small_roads_bridge_in_each_direction_osrm_found(self):
        forward, = self.props(431)
        backward, = self.props(433)
        self.assertEqual((forward["bridge_kind"], backward["bridge_kind"]), ("small", "small"))
        self.assertTrue(forward["oneway"])

    def test_route_along_the_dead_end_road_is_trimmed(self):
        side, = self.props(451)
        self.assertEqual(side["bridge_kind"], "small")
        own = [f for f in self.roads if f["properties"]["way_id"] == 450]
        self.assertEqual(len(own), 1)
        self.assertFalse(own[0]["properties"]["bridge"])
        self.assertAlmostEqual(side["km"], 0.222, delta=0.005)

    def test_long_routes_banned_ferries_and_unmatched_routes_are_not_bridged(self):
        self.assertEqual(self.props(441), [])
        self.assertEqual(self.props(421), [])
        dead = self.dead()
        self.assertEqual(dead[9402]["reason"], "continues")
        self.assertEqual(dead[9202]["reason"], "continues")
        self.assertEqual(dead[9702]["reason"], "end")
        self.assertEqual(dead[9802]["reason"], "construction")

    def test_metadata_counts_osrm_bridges_errors_and_ferries(self):
        meta = self.meta
        self.assertEqual(meta["osrm_bridging"], "ok")
        self.assertEqual(meta["osrm_bridges"], 6)
        self.assertEqual(meta["osrm_errors"], 1)
        sample, = meta["osrm_error_samples"]
        self.assertEqual(sample["node_id"], 9602)
        self.assertEqual(sample["at"], [105.01, 22.6])
        self.assertIn("fake OSRM timeout", sample["error"])
        self.assertEqual(meta["osrm_unmatched"], 1)
        self.assertEqual((meta["ferries"], meta["ferries_unconfirmed"]), (3, 2))
        self.assertAlmostEqual(meta["bridge_km_by_kind"]["small"], 0.31 + 0.34 + 0.222, delta=0.03)
        self.assertGreater(meta["bridge_km_by_kind"]["ferry"], 3.0)
        self.assertEqual(meta["bridge_km_by_kind"]["road"], 0)
        reasons = meta["dead_end_reasons"]
        self.assertEqual(sum(reasons.values()), meta["junctions"]["deadend"])
        self.assertGreaterEqual(reasons["construction"], 1)


def ferry_town_fixture():
    """A ferry between two towns (lat 23.0); on each bank five short A roads fan out within 300 m of the landing.

    Those roads are reached along the big-road network, so they crowd the opposite bank out of the five nearest
    candidates unless such roads are ruled out first.
    """
    nodes = {10001: (23.0, 105.01), 10002: (23.0, 105.008), 10020: (23.0, 105.02), 10022: (23.0, 105.022)}
    ways = [(500, [10002, 10001], {"highway": "trunk", "ref": "QL.72"}),
            (506, [10001, 10020], {"route": "ferry", "name": "Phà Thị Trấn", "motor_vehicle": "yes"}),
            (507, [10020, 10022], {"highway": "trunk", "ref": "QL.72"})]
    offsets = [(0.001, 0.0), (-0.001, 0.0), (0.0, -0.002), (0.0015, 0.001), (-0.0015, 0.001)]
    for k, (dlat, dlon) in enumerate(offsets):
        nodes[10011 + k] = (23.0 + dlat, 105.008 + dlon)
        ways.append((510 + k, [10002, 10011 + k], {"highway": "trunk", "ref": f"QL.8{k}"}))
        nodes[10031 + k] = (23.0 + dlat, 105.022 - dlon)
        ways.append((520 + k, [10022, 10031 + k], {"highway": "trunk", "ref": f"QL.9{k}"}))
    routes = {(10001, 10002): ([10001, 10002], [205], [15]), (10002, 10001): ([10002, 10001], [205], [15]),
              (10020, 10022): ([10020, 10022], [205], [15]), (10022, 10020): ([10022, 10020], [205], [15]),
              (10001, 10020): ([10001, 10020], [1025], [900])}
    return nodes, ways, routes


class BigRoadOsrmTownTests(unittest.TestCase):
    def test_roads_reached_along_the_network_do_not_crowd_out_the_opposite_bank(self):
        from build_big_roads import build
        nodes, ways, routes = ferry_town_fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_osm(root / "town.osm", nodes, ways)
            meta = build(root / "town.osm", root / "out", tolerance_m=0, fragment_km=0, router=FakeRouter(nodes, routes))
            roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))["features"]
        ferry = [f["properties"] for f in roads if f["properties"]["way_id"] == 506]
        self.assertEqual([p["bridge_kind"] for p in ferry], ["ferry"])
        self.assertEqual(meta["ferries"], 1)

    def test_a_candidate_osrm_cannot_route_to_falls_through_to_the_next(self):
        from build_big_roads import build
        # /table reaches QL.75 first, but /route then answers NoRoute; the road to QL.76 still works.
        nodes = {12001: (23.2, 105.0), 12002: (23.2, 105.01), 12003: (23.2, 105.012), 12006: (23.21, 105.012),
                 12005: (23.2, 105.015), 12007: (23.2, 105.025)}
        ways = [(700, [12001, 12002], {"highway": "trunk", "ref": "QL.74"}),
                (701, [12003, 12006], {"highway": "trunk", "ref": "QL.75"}),
                (702, [12005, 12007], {"highway": "trunk", "ref": "QL.76"}),
                (703, [12002, 12005], {"highway": "residential"})]
        routes = {(12002, 12003): ([12002, 12003], [210], [30]), (12002, 12005): ([12002, 12005], [515], [70])}
        router = FakeRouter(nodes, routes, no_route={(12002, 12003)})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_osm(root / "noroute.osm", nodes, ways)
            meta = build(root / "noroute.osm", root / "out", tolerance_m=0, fragment_km=0, router=router)
            roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))["features"]
        self.assertEqual([f["properties"]["bridge_kind"] for f in roads if f["properties"]["way_id"] == 703], ["small"])
        self.assertEqual(meta["osrm_errors"], 0)

    def test_a_gap_whose_sides_meet_only_by_a_long_loop_is_still_bridged(self):
        from build_big_roads import build
        # QL.73 misses 310 m; its two sides also meet along ĐT.1, but only by a 4.5 km loop.
        nodes = {11001: (23.1, 105.0), 11002: (23.1, 105.01), 11003: (23.1, 105.013), 11004: (23.1, 105.023),
                 11005: (23.105, 105.0), 11006: (23.105, 105.023)}
        ways = [(600, [11001, 11002], {"highway": "trunk", "ref": "QL.73"}),
                (601, [11002, 11003], {"highway": "residential"}),
                (602, [11003, 11004], {"highway": "trunk", "ref": "QL.73"}),
                (603, [11001, 11005, 11006, 11004], {"highway": "secondary", "ref": "ĐT.1"})]
        routes = {(11002, 11003): ([11002, 11003], [310], [45]), (11003, 11002): ([11003, 11002], [310], [45])}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_osm(root / "loop.osm", nodes, ways)
            build(root / "loop.osm", root / "out", tolerance_m=0, fragment_km=0, router=FakeRouter(nodes, routes))
            roads = json.loads((root / "out/big_roads.geojson").read_text(encoding="utf-8"))["features"]
        self.assertEqual([f["properties"]["bridge_kind"] for f in roads if f["properties"]["way_id"] == 601], ["small"])


class OsrmRouterErrorTests(unittest.TestCase):
    def test_osrm_error_code_and_message_are_kept(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        from build_big_roads import OsrmRouter

        class NoRoute(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"code":"NoRoute","message":"Impossible route between points"}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), NoRoute)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            router = OsrmRouter(base=f"http://127.0.0.1:{server.server_port}")
            with self.assertRaisesRegex(ValueError, "NoRoute: Impossible route between points"):
                router.route((105.0, 21.0), (105.1, 21.0))
        finally:
            server.shutdown()
            server.server_close()


class BigRoadBuildFailureTests(unittest.TestCase):
    def test_failed_edge_index_write_keeps_the_old_file_and_no_temp(self):
        from build_big_roads import write_edges

        def rows():
            yield (1, 2, 10, "a")
            raise RuntimeError("disk full")

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "big_road_edges.sqlite"
            write_edges(path, [(5, 6, 20, "a")], {"pbf_sha256": "old", "policy_version": "6"})
            with self.assertRaises(RuntimeError):
                write_edges(path, rows(), {"pbf_sha256": "new", "policy_version": "6"})
            edges, metadata = read_edges(temp)
            self.assertEqual((edges, metadata["pbf_sha256"]), ({(5, 6, 20): "a"}, "old"))
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["big_road_edges.sqlite"])

    def test_missing_pbf_writes_nothing(self):
        from build_big_roads import build
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            with self.assertRaises(FileNotFoundError):
                build(Path(temp) / "missing.osm.pbf", out)
            self.assertFalse(out.exists() and any(out.iterdir()))

    def test_map_without_big_roads_keeps_previous_output(self):
        from build_big_roads import build
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "local.osm").write_text("<?xml version='1.0'?><osm version='0.6'>"
                "<node id='1' version='1' lat='21' lon='105.8'/><node id='2' version='1' lat='21' lon='105.81'/>"
                "<way id='1' version='1'><nd ref='1'/><nd ref='2'/><tag k='highway' v='residential'/></way></osm>", encoding="utf-8")
            (root / "out").mkdir()
            (root / "out/big_roads.meta.json").write_text('{"old": true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                build(root / "local.osm", root / "out")
            self.assertEqual(json.loads((root / "out/big_roads.meta.json").read_text(encoding="utf-8")), {"old": True})


if __name__ == "__main__":
    unittest.main()
