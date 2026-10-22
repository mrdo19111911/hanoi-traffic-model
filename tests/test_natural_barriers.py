"""Offline tests for the river and mountain survey (model/natural_barriers.py) on a tiny synthetic OSM file."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

# Nodes as (lat, lon). A dividing river "Sông Thử" (relation 900) runs north-south at lon 105.85 for 111 km;
# the A road QL.1 runs along each bank (lon 105.80 west, 105.90 east). Crossings: an A bridge (way 30, QL.2),
# a residential bridge (31), a car ferry (32); a tertiary road without bridge tag (33) is only a suspect, and a
# footway bridge (34) is not car-routable. The east bank road also crosses the short "Suối Ngắn" (11 km) on a
# bridge (40), which divides nothing. The west road has a 1 km tunnel (2) and a 200 m one (5); the east road
# has the pass "Đèo Thử" at node 23. Way 50 is a separate 1.1 km A stub.
# Main rivers (step 2): "Sông Thử" runs from the coast (lat 20.0) to the border (lat 21.0); "Sông Nhánh" runs from
# the border to 0.5 km from "Sông Thử", so it is main only through the iteration; "Sông Hai" leaves the coast but
# stops 11 km inside the land, so it is not.
NODES = {
    # Rivers.
    901: (20.0, 105.85), 902: (20.5, 105.85), 903: (20.999, 105.85),
    911: (19.0, 106.5), 912: (20.0, 106.5),
    921: (20.0, 106.8), 922: (20.5, 106.8), 923: (20.9, 106.8), 924: (20.3, 106.9), 925: (20.35, 106.9),
    931: (20.45, 105.95), 932: (20.65, 105.95), 941: (20.999, 106.95), 942: (20.7, 105.855),
    # West bank road.
    11: (20.40, 105.80), 12: (20.45, 105.80), 13: (20.50, 105.80), 16: (20.509, 105.80), 17: (20.53, 105.80),
    18: (20.5318, 105.80), 14: (20.55, 105.80), 15: (20.60, 105.80),
    # East bank road.
    21: (20.40, 105.90), 22: (20.45, 105.90), 23: (20.50, 105.90), 24: (20.55, 105.90), 25: (20.60, 105.90),
    41: (20.55, 106.0), 51: (20.3, 106.2), 52: (20.31, 106.2),
}
NODE_TAGS = {23: {"mountain_pass": "yes", "name": "Đèo Thử", "ele": "500"}}
A_WAYS = [
    (1, [11, 12, 13], {"highway": "trunk", "ref": "QL.1"}),
    (2, [13, 16], {"highway": "trunk", "ref": "QL.1", "tunnel": "yes", "name": "Hầm Thử"}),
    (3, [16, 17], {"highway": "trunk", "ref": "QL.1"}),
    (5, [17, 18], {"highway": "trunk", "ref": "QL.1", "tunnel": "yes", "name": "Hầm Ngắn"}),
    (6, [18, 14, 15], {"highway": "trunk", "ref": "QL.1"}),
    (20, [21, 22, 23, 24, 25], {"highway": "trunk", "ref": "QL.1"}),
    (30, [12, 22], {"highway": "trunk", "ref": "QL.2", "bridge": "yes", "name": "Cầu Lớn"}),
    (40, [24, 41], {"highway": "trunk", "ref": "QL.3", "bridge": "yes", "name": "Cầu Suối"}),
    (50, [51, 52], {"highway": "trunk", "ref": "QL.9"}),
]
OTHER_WAYS = [
    (31, [14, 24], {"highway": "residential", "bridge": "yes", "name": "Cầu Làng"}),
    (32, [15, 25], {"route": "ferry", "motor_vehicle": "yes", "name": "Phà Thử"}),
    (33, [11, 21], {"highway": "tertiary", "name": "Đường Ngầm"}),
    (34, [13, 23], {"highway": "footway", "bridge": "yes"}),
    (901, [901, 902], {"waterway": "river"}),
    (902, [902, 903], {"waterway": "river", "name": "Sông Thử"}),
    (910, [911, 912], {"waterway": "river", "name": "Sông Ngoài"}),
    (920, [921, 922], {"waterway": "river", "name": "Sông Hai"}),
    (921, [922, 923], {"waterway": "river", "name": "Sông Hai"}),
    (922, [924, 925], {"waterway": "river", "name": "Sông Hai"}),
    (930, [931, 932], {"waterway": "river", "name": "Suối Ngắn"}),
    (940, [941, 942], {"waterway": "river", "name": "Sông Nhánh"}),
]
RELATIONS = [(900, [(901, "main_stream"), (902, "main_stream")], {"type": "waterway", "waterway": "river", "name": "Sông Thử"})]
# Vietnam for the test: lon 105.5-107.0, lat 19.9-21.0 (sea to the south, land border at 21.0), so "Sông Ngoài" has only ~11 of its 111 km inside.
BOUNDARY = [[105.5, 19.9], [107.0, 19.9], [107.0, 21.0], [105.5, 21.0], [105.5, 19.9]]
# The mainland ring (land border and coastline): lat 20.0 is the coast, lat 21.0 the land border.
LAND = [[105.5, 20.0], [107.0, 20.0], [107.0, 21.0], [105.5, 21.0], [105.5, 20.0]]


def write_osm(path, nodes, ways, relations=(), node_tags=None):
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
        lines += [f"<member type='way' ref='{ref}' role='{role}'/>" for ref, role in members]
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</relation>")
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")


def make_fixture(root, sha=None):
    """Write tiny.osm, big_road_edges.sqlite and vietnam_boundary.geojson; returns their paths."""
    from build_big_roads import write_edges
    pbf = root / "tiny.osm"
    write_osm(pbf, NODES, A_WAYS + OTHER_WAYS, RELATIONS, NODE_TAGS)
    sha = sha or hashlib.sha256(pbf.read_bytes()).hexdigest()
    rows = [(min(a, b), max(a, b), way_id, "a") for way_id, refs, _ in A_WAYS for a, b in zip(refs, refs[1:])]
    edges = root / "big_road_edges.sqlite"
    write_edges(edges, rows, {"pbf_sha256": sha, "policy_version": "6"})
    boundary = root / "vietnam_boundary.geojson"
    boundary.write_text(json.dumps({"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [[BOUNDARY]]}}),
                        encoding="utf-8")
    (root / "vietnam_land.geojson").write_text(json.dumps({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [LAND]}}),
                                               encoding="utf-8")
    return pbf, edges, boundary


class NaturalBarrierSurveyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from natural_barriers import survey
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        pbf, edges, boundary = make_fixture(cls.root)
        cls.report = survey(pbf, edges, boundary, min_river_km=100, small_km=8, tunnel_m=500, land_path=cls.root / "vietnam_land.geojson")
        cls.rivers = {river["name"]: river for river in cls.report["rivers"]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_only_rivers_long_enough_inside_vietnam_divide(self):
        self.assertEqual(set(self.rivers), {"Sông Thử", "Sông Hai", "Sông Nhánh"})
        thu = self.rivers["Sông Thử"]
        self.assertEqual((thu["relation_id"], thu["way_ids"]), (900, [901, 902]))
        self.assertAlmostEqual(thu["km"], 111.2, delta=0.5)
        # Ways outside any relation are grouped by name and shared nodes; the detached "Sông Hai" piece stays apart.
        self.assertEqual((self.rivers["Sông Hai"]["relation_id"], self.rivers["Sông Hai"]["way_ids"]), (None, [920, 921]))
        # 111 km long, but only ~11 km of it inside the boundary.
        self.assertGreater(self.report["candidates_below_threshold"], 0)

    def test_main_rivers_reach_the_coast_border_or_another_main_river_at_both_ends(self):
        self.assertEqual(self.report["main_rivers"], ["Sông Thử", "Sông Nhánh"])
        self.assertEqual(sorted(self.rivers["Sông Thử"]["anchors"]), ["border", "coast"])
        self.assertTrue(self.rivers["Sông Thử"]["main"])
        self.assertEqual(sorted(self.rivers["Sông Nhánh"]["anchors"]), ["border", "river:Sông Thử"])
        self.assertTrue(self.rivers["Sông Nhánh"]["main"])
        self.assertEqual(sorted(self.rivers["Sông Hai"]["anchors"], key=str), [None, "coast"])
        self.assertFalse(self.rivers["Sông Hai"]["main"])

    def test_crossings_keep_the_two_ends_of_the_segment_over_the_river(self):
        by_way = {c["way_id"]: c for c in self.rivers["Sông Thử"]["crossings"]}
        self.assertEqual(sorted(map(tuple, by_way[31]["ends"])), [(105.8, 20.55), (105.9, 20.55)])
        self.assertEqual(sorted(map(tuple, by_way[32]["ends"])), [(105.8, 20.6), (105.9, 20.6)])

    def test_a_bridge_residential_bridge_and_car_ferry_are_crossings(self):
        thu = self.rivers["Sông Thử"]
        by_way = {c["way_id"]: c for c in thu["crossings"]}
        self.assertEqual(set(by_way), {30, 31, 32})
        self.assertEqual((by_way[30]["in_a"], by_way[31]["in_a"], by_way[32]["in_a"]), (True, False, False))
        self.assertEqual((by_way[30]["kind"], by_way[31]["kind"], by_way[32]["kind"]), ("bridge", "bridge", "ferry"))
        self.assertEqual((thu["crossing_count"], thu["crossing_a"], thu["crossing_other"]), (3, 1, 2))
        self.assertEqual(thu["crossings_by_highway"], {"trunk": 1, "residential": 1, "ferry": 1})
        self.assertEqual(self.rivers["Sông Hai"]["crossing_count"], 0)

    def test_untagged_road_over_a_river_is_a_suspect_not_a_crossing(self):
        suspects = self.report["suspect_crossings"]
        self.assertEqual(suspects["count"], 1)
        self.assertEqual([s["way_id"] for s in suspects["sample"]], [33])
        self.assertEqual(self.rivers["Sông Thử"]["suspect_count"], 1)

    def test_bridge_over_a_short_river_and_footway_bridge_are_ignored(self):
        all_ids = {c["way_id"] for river in self.report["rivers"] for c in river["crossings"]}
        self.assertNotIn(40, all_ids)
        self.assertNotIn(34, all_ids)

    def test_passes_and_long_tunnels_on_the_a_network(self):
        mountains = self.report["mountains"]
        self.assertEqual([(p["node_id"], p["name"], p["refs"]) for p in mountains["passes"]], [(23, "Đèo Thử", ["QL.1"])])
        self.assertEqual([t["way_id"] for t in mountains["tunnels"]], [2])
        self.assertAlmostEqual(mountains["tunnels"][0]["m"], 1002, delta=10)
        self.assertEqual((mountains["pass_count"], mountains["tunnel_count"]), (1, 1))

    def test_cutting_at_river_crossings_splits_the_a_network_in_two(self):
        components = self.report["components"]
        self.assertEqual(components["baseline"]["count"], 2)
        rivers = components["rivers"]
        self.assertEqual(rivers["count"], 3)
        self.assertEqual(rivers["cut_edges"], 1)
        self.assertEqual(rivers["small_count"], 1)
        km = sorted(c["km"] for c in rivers["components"])
        self.assertAlmostEqual(km[0], 1.11, delta=0.05)
        self.assertAlmostEqual(km[1], 22.2, delta=0.3)
        self.assertAlmostEqual(km[2], 32.6, delta=0.4)
        self.assertEqual(sum(c["nodes"] for c in rivers["components"]), 16)
        largest = rivers["components"][0]
        self.assertEqual(largest["bbox"], [105.9, 20.4, 106.0, 20.6])

    def test_passes_and_tunnels_cut_further(self):
        both = self.report["components"]["rivers_mountains"]
        self.assertEqual(both["count"], 5)
        self.assertEqual(both["removed_nodes"], 1)
        self.assertEqual(both["small_count"], 2)
        self.assertEqual(both["zero_km_count"], 0)
        self.assertEqual(sum(c["nodes"] for c in both["components"]), 15)


class NaturalBarrierRunTests(unittest.TestCase):
    def test_run_writes_the_report_with_metadata(self):
        from natural_barriers import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            pbf, edges, boundary = make_fixture(root)
            out = root / "reports" / "natural_barriers.json"
            run(pbf, edges, boundary, out, build_json=None)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["metadata"]["pbf_sha256"], hashlib.sha256(pbf.read_bytes()).hexdigest())
            self.assertEqual(data["metadata"]["parameters"]["min_river_km"], 100)
            self.assertIn("runtime_s", data["metadata"])

    def test_run_writes_main_rivers_with_their_crossings_for_the_cluster_file(self):
        from natural_barriers import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            pbf, edges, boundary = make_fixture(root)
            rivers_out = root / "main_rivers.geojson"
            run(pbf, edges, boundary, root / "report.json", build_json=None, land_path=root / "vietnam_land.geojson",
                rivers_out=rivers_out)
            data = json.loads(rivers_out.read_text(encoding="utf-8"))
            self.assertEqual(data["pbf_sha256"], hashlib.sha256(pbf.read_bytes()).hexdigest())
            self.assertEqual(data["policy_version"], 6)
            self.assertEqual(sorted({f["properties"]["river"] for f in data["features"]}), ["Sông Nhánh", "Sông Thử"])
            self.assertEqual(sorted(f["properties"]["way_id"] for f in data["features"]), [901, 902, 940])
            self.assertEqual(sorted((c["way_id"], c["in_a"], c["kind"]) for c in data["crossings"]),
                             [(30, True, "bridge"), (31, False, "bridge"), (32, False, "ferry")])

    def test_edges_from_another_map_are_refused(self):
        from natural_barriers import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            pbf, edges, boundary = make_fixture(root, sha="0" * 64)
            with self.assertRaises(ValueError):
                run(pbf, edges, boundary, root / "out.json", build_json=None)

    def test_osrm_build_from_another_map_is_refused(self):
        from natural_barriers import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            pbf, edges, boundary = make_fixture(root)
            build = root / "build.json"
            build.write_text(json.dumps({"input": {"pbf_sha256": "1" * 64}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                run(pbf, edges, boundary, root / "out.json", build_json=build)


class CarAccessTests(unittest.TestCase):
    def test_car_profile_rules(self):
        from natural_barriers import car_routable
        self.assertTrue(car_routable({"highway": "residential"}))
        self.assertTrue(car_routable({"highway": "service", "access": "private"}))
        self.assertTrue(car_routable({"route": "ferry"}))
        self.assertFalse(car_routable({"route": "ferry", "motor_vehicle": "no"}))
        self.assertFalse(car_routable({"highway": "footway"}))
        self.assertFalse(car_routable({"highway": "construction"}))
        self.assertFalse(car_routable({"highway": "primary", "access": "no"}))
        self.assertTrue(car_routable({"highway": "primary", "access": "no", "motorcar": "yes"}))
        self.assertFalse(car_routable({"highway": "residential", "area": "yes"}))


if __name__ == "__main__":
    unittest.main()


class MainRiverAnchorTests(unittest.TestCase):
    def test_an_end_outside_the_country_is_anchored(self):
        from natural_barriers import end_anchor
        land = [[(105.5, 20.0), (107.0, 20.0), (107.0, 21.0), (105.5, 21.0), (105.5, 20.0)]]
        self.assertEqual(end_anchor((106.0, 21.5), land, land), "outside")
        self.assertEqual(end_anchor((106.0, 20.01), land, [[(105.0, 19.0), (108.0, 19.0), (108.0, 22.0), (105.0, 22.0), (105.0, 19.0)]]), "coast")
        self.assertIsNone(end_anchor((106.0, 20.5), land, land))

    def test_river_ends_are_the_two_farthest_dangling_ends(self):
        from natural_barriers import river_ends
        ways = {1: {"nodes": [(1, 105.0, 20.0), (2, 105.0, 20.5)]}, 2: {"nodes": [(2, 105.0, 20.5), (3, 105.0, 21.0)]},
                3: {"nodes": [(2, 105.0, 20.5), (4, 105.1, 20.5)]}, 4: {"nodes": [(5, 106.0, 22.0), (6, 106.0, 22.1)]}}
        self.assertEqual(sorted(river_ends([1, 2, 3], ways)), [(105.0, 20.0), (105.0, 21.0)])
        # A gap splits the river; the ends are still the two farthest ends over both pieces.
        self.assertEqual(sorted(river_ends([1, 2, 4], ways)), [(105.0, 20.0), (106.0, 22.1)])
