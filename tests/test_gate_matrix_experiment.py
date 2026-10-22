"""Offline tests for the gate travel-time matrix experiment, with a fake OSRM /table."""
import contextlib
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

# Two dense squares (A at lon 105, B at lon 106) whose only link is way 99, A0-B0; cluster A = 0, B = 1.
A0, A1, A2, A3 = (105.0, 21.0), (105.01, 21.0), (105.01, 21.01), (105.0, 21.01)
B0, B1, B2, B3 = (106.0, 21.0), (106.01, 21.0), (106.01, 21.01), (106.0, 21.01)
A_WAYS = {1: [A0, A1], 2: [A1, A2], 3: [A2, A3], 4: [A3, A0], 5: [A0, A2], 6: [A1, A3]}
B_WAYS = {11: [B0, B1], 12: [B1, B2], 13: [B2, B3], 14: [B3, B0], 15: [B0, B2], 16: [B1, B3]}


def line(way_id, coords):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
            "properties": {"way_id": way_id, "group": "quoclo", "fragment": False, "isolated": False}}


def features():
    return [line(w, c) for w, c in {**A_WAYS, **B_WAYS, 99: [A0, B0]}.items()]


def clusters_doc(sha="map-a", ways=None):
    ways = ways or {**{str(w): 0 for w in A_WAYS}, **{str(w): 1 for w in B_WAYS}, "99": 0}
    node_labels = [[*p, 0] for p in (A0, A1, A2, A3)] + [[*p, 1] for p in (B0, B1, B2, B3)]
    partition = {"k": 2, "method": "natural", "ways": ways, "node_labels": node_labels, "cuts": [[list(A0), list(B0)]],
                 "boundary": [list(A0), list(B0)], "stats": {}}
    return {"method": "regions", "pbf_sha256": sha, "policy_version": 6, "partitions": [partition]}


def km(a, b):
    return math.hypot((b[0] - a[0]) * math.cos(math.radians(21.0)), b[1] - a[1]) * 111.32


class FakeRouter:
    """Shortest free-flow seconds (50 km/h) over an explicit road list, which may hold roads the graph lacks."""

    def __init__(self, roads, unavailable=None):
        self.points = sorted({p for road in roads for p in road})
        index = {p: i for i, p in enumerate(self.points)}
        rows, cols, secs = [], [], []
        for a, b in roads:
            rows += [index[a], index[b]]
            cols += [index[b], index[a]]
            secs += [km(a, b) / 50 * 3600] * 2
        n = len(self.points)
        self.times = dijkstra(coo_matrix((secs, (rows, cols)), shape=(n, n)).tocsr())
        self.reason, self.calls = unavailable, []

    def unavailable(self, pbf_sha256):
        return self.reason

    def durations(self, sources, destinations):
        self.calls.append((len(sources), len(destinations)))
        at = {p: i for i, p in enumerate(self.points)}
        return [[float(self.times[at[(round(s[0], 6), round(s[1], 6))], at[(round(d[0], 6), round(d[1], 6))]])
                 for d in destinations] for s in sources]


ROADS = [tuple(c) for c in list(A_WAYS.values()) + list(B_WAYS.values()) + [[A0, B0]]]
CROSS_PAIRS = [(A1, B2), (A3, B1), (B3, A2), (A2, B2)]


class GateTimeTests(unittest.TestCase):
    def setUp(self):
        from clusters import build_graph
        self.graph = build_graph(features())
        self.node = {tuple(np.round(p, 6)): i for i, p in enumerate(self.graph.lonlat)}

    def pairs(self, pairs):
        return [(self.node[a], self.node[b]) for a, b in pairs]

    def run_partition(self, router, pairs, partition=None):
        from gate_matrix_experiment import evaluate_partition
        return evaluate_partition(self.graph, partition or clusters_doc()["partitions"][0], self.pairs(pairs), router)

    def test_labels_come_from_node_labels_not_from_the_ways(self):
        from gate_matrix_experiment import gates, partition_labels
        # Every way claims cluster 0, as a majority count over ways could; node_labels say otherwise.
        doc = clusters_doc(ways={str(w): 0 for w in [*A_WAYS, *B_WAYS, 99]})
        labels = partition_labels(self.graph, doc["partitions"][0])
        self.assertEqual([labels[self.node[p]] for p in (A0, A1, B0, B2)], [0, 0, 1, 1])
        result = self.run_partition(FakeRouter(ROADS), CROSS_PAIRS, doc["partitions"][0])
        np.testing.assert_allclose(result["t_gate"], result["t_direct"], rtol=1e-9)
        self.assertEqual(len(result["t_gate"]), 4)

    def test_gates_come_from_the_boundary(self):
        from gate_matrix_experiment import gates, partition_labels
        labels = partition_labels(self.graph, clusters_doc()["partitions"][0])
        self.assertEqual({c: sorted(map(tuple, (self.graph.lonlat[g] for g in nodes))) for c, nodes in
                          gates(self.graph, labels, clusters_doc()["partitions"][0]["boundary"]).items()},
                         {0: [A0, B0], 1: [A0, B0]})

    def test_a_single_gate_gives_the_direct_time(self):
        result = self.run_partition(FakeRouter(ROADS), CROSS_PAIRS)
        np.testing.assert_allclose(result["t_gate"], result["t_direct"], rtol=1e-9)
        self.assertEqual(result["cross_metrics"]["max"], 0.0)
        # Two clusters are adjacent, so no pair is far and every pair is answered by OSRM directly.
        self.assertEqual((result["metrics"]["pairs"], result["pass"], result["direct_share"]), (0, None, 1.0))

    def test_a_shortcut_around_the_gates_makes_the_gate_time_longer(self):
        router = FakeRouter(ROADS + [(A2, B2)])
        result = self.run_partition(router, [(A2, B2)])
        direct = km(A2, B2) / 50 * 3600
        via_gate = (km(A2, A0) + km(A0, B0) + km(B0, B2)) / 50 * 3600
        self.assertAlmostEqual(result["t_direct"][0], direct, places=6)
        self.assertAlmostEqual(result["t_gate"][0], via_gate, places=6)
        self.assertAlmostEqual(result["cross_metrics"]["max"], (via_gate - direct) / direct, places=9)

    def test_pairs_in_the_same_cluster_are_left_out(self):
        result = self.run_partition(FakeRouter(ROADS), CROSS_PAIRS + [(A1, A2), (B1, B3)])
        self.assertEqual(result["cross_metrics"]["pairs"], 4)
        self.assertEqual(result["same_cluster"], 2)
        self.assertEqual(len(result["t_gate"]), 4)


class SamplingAndTableTests(unittest.TestCase):
    def test_sample_is_fixed_by_the_seed(self):
        from gate_matrix_experiment import sample_pairs
        first, again, other = sample_pairs(100, 50, 0), sample_pairs(100, 50, 0), sample_pairs(100, 50, 1)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 50)
        self.assertTrue(all(i != j for i, j in first))

    def test_blocked_table_matches_one_large_call(self):
        from gate_matrix_experiment import blocked_table
        points = [(105 + i / 100, 21.0) for i in range(7)]
        router = FakeRouter([(a, b) for a, b in zip(points, points[1:])])
        whole = blocked_table(router, points, points[2:], block=100)
        router.calls.clear()
        blocks = blocked_table(router, points, points[2:], block=3)
        np.testing.assert_array_equal(blocks, whole)
        self.assertEqual(whole.shape, (7, 5))
        self.assertTrue(all(s <= 3 and d <= 3 for s, d in router.calls))
        self.assertEqual(len(router.calls), 3 * 2)


class MetricTests(unittest.TestCase):
    def test_metrics_on_prepared_errors(self):
        from gate_matrix_experiment import metrics
        direct = np.full(10, 100.0)
        gate = np.array([100, 101, 102, 103, 104, 106, 108, 112, 120, 99], dtype=float)
        distance = np.array([10, 20, 50, 60, 150, 200, 250, 400, 500, 5], dtype=float)
        result = metrics(gate, direct, distance)
        errors = (gate - direct) / direct
        self.assertEqual(result["pairs"], 10)
        self.assertAlmostEqual(result["median"], float(np.median(errors)))
        self.assertAlmostEqual(result["p95"], float(np.percentile(errors, 95)))
        self.assertAlmostEqual(result["max"], 0.2)
        self.assertAlmostEqual(result["min"], -0.01)
        self.assertAlmostEqual(result["over_5pct"], 0.4)
        self.assertAlmostEqual(result["over_10pct"], 0.2)
        self.assertEqual(result["anomalies"], 1)
        self.assertEqual({band: value["pairs"] for band, value in result["bands"].items()},
                         {"<30": 3, "30-100": 2, "100-300": 3, ">=300": 2})
        self.assertAlmostEqual(result["bands"]["<30"]["median"], 0.0)
        self.assertAlmostEqual(result["bands"][">=300"]["median"], 0.16)


class RunTests(unittest.TestCase):
    def write_inputs(self, root, clusters_sha="map-a"):
        (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features()}), encoding="utf-8")
        (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
        (root / "big_road_clusters.json").write_text(json.dumps(clusters_doc(clusters_sha)), encoding="utf-8")

    def test_run_writes_the_report(self):
        from gate_matrix_experiment import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_inputs(root)
            report = run(root / "big_roads.geojson", root / "big_road_clusters.json", root / "out.json", FakeRouter(ROADS), pairs=20)
            saved = json.loads((root / "out.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(p.name for p in root.iterdir()),
                             ["big_road_clusters.json", "big_roads.geojson", "big_roads.meta.json", "out.json"])
        self.assertEqual(saved, report)
        self.assertEqual(report["pbf_sha256"], "map-a")
        item, = report["partitions"]
        self.assertEqual(item["name"], "natural")
        self.assertEqual(item["cost"]["gates"], 2)
        self.assertEqual(item["cost"]["gate_cells"], 4)
        self.assertEqual(item["cost"]["db_bytes_96_slots"], 4 * 4 * 96)
        self.assertEqual(item["cost"]["cells_n2000"], 2000 * 2)
        self.assertIn("full_gate_table_s", item["cost"])

    def test_an_unknown_partition_is_skipped_with_a_warning(self):
        from gate_matrix_experiment import run
        doc = clusters_doc()
        doc["partitions"].insert(0, {**doc["partitions"][0], "method": "kmeans", "k": 7})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_inputs(root)
            (root / "big_road_clusters.json").write_text(json.dumps(doc), encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                report = run(root / "big_roads.geojson", root / "big_road_clusters.json", root / "out.json", FakeRouter(ROADS), pairs=20)
        self.assertEqual([item["name"] for item in report["partitions"]], ["natural"])
        self.assertIn("Skipping unknown partition 'kmeans'", printed.getvalue())

    def test_clusters_from_another_map_stop_without_a_file(self):
        from gate_matrix_experiment import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_inputs(root, clusters_sha="map-old")
            with self.assertRaises(ValueError):
                run(root / "big_roads.geojson", root / "big_road_clusters.json", root / "out.json", FakeRouter(ROADS))
            self.assertFalse((root / "out.json").exists())

    def test_unavailable_osrm_stops_without_a_file(self):
        from gate_matrix_experiment import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write_inputs(root)
            with self.assertRaisesRegex(RuntimeError, "OSRM không chạy"):
                run(root / "big_roads.geojson", root / "big_road_clusters.json", root / "out.json", FakeRouter(ROADS, "OSRM không chạy"))
            self.assertFalse((root / "out.json").exists())


# ---------------------------------------------------------------- round 2: stratified sample, small roads, worst pairs


def haversine(a, b):
    from clusters import haversine_km
    return haversine_km(a, b)


class FakeEdges:
    """Edge index stand-in: {(a, b) sorted: (way_id, kind)}."""

    def __init__(self, pairs=None):
        self.pairs = dict(pairs or {})
        self.nodes = {n for pair in self.pairs for n in pair}

    def has_pair(self, a, b):
        return (min(a, b), max(a, b)) in self.pairs

    def has_node(self, node):
        return node in self.nodes

    def lookup(self, pairs):
        return {pair: self.pairs[(min(pair), max(pair))] for pair in pairs if (min(pair), max(pair)) in self.pairs}


class EuclidRouter:
    """Free-flow seconds at 50 km/h along the straight line; /nearest returns the point itself."""

    def __init__(self, snap_m=lambda p: 10.0, nodes=lambda p: (3, 4)):
        self.snap_m, self.nodes = snap_m, nodes

    def unavailable(self, pbf_sha256):
        return None

    def nearest(self, point):
        return {"location": [round(point[0], 6), round(point[1], 6)], "nodes": list(self.nodes(point)), "distance": self.snap_m(point)}

    def durations(self, sources, destinations):
        return [[haversine(s, d) / 50 * 3600 for d in destinations] for s in sources]

    def route_detail(self, points):
        return {"nodes": [], "legs": [[] for _ in points[1:]], "waypoints": [list(p) for p in points], "coords": [list(p) for p in points],
                "distances": [], "steps": [], "duration": sum(haversine(a, b) for a, b in zip(points, points[1:])) / 50 * 3600}


class RejectionTests(unittest.TestCase):
    RING = [(105.0, 21.0), (106.0, 21.0), (106.0, 22.0), (105.0, 22.0), (105.0, 21.0)]

    def test_points_are_rejected_for_far_snaps_land_kind_and_band(self):
        from gate_matrix_experiment import rejection
        edges = FakeEdges({(1, 2): (10, "a")})
        snap = lambda loc, m=10.0, nodes=(3, 4): {"location": loc, "distance": m, "nodes": list(nodes)}  # noqa: E731
        self.assertEqual(rejection(snap([105.5, 21.5], 800.0), self.RING, edges), "snap_far")
        self.assertEqual(rejection(snap([104.5, 21.5]), self.RING, edges), "outside_land")
        self.assertEqual(rejection(snap([105.5, 21.5], nodes=(2, 1)), self.RING, edges, need_small=True), "not_small")
        origin = (105.5, 21.5)
        self.assertEqual(rejection(snap([105.5, 21.9]), self.RING, edges, origin=origin, band=(30, 100)), None)
        self.assertEqual(rejection(snap([105.5, 21.52]), self.RING, edges, origin=origin, band=(30, 100)), "out_of_band")

    def test_point_kind_follows_the_edge_index(self):
        from gate_matrix_experiment import point_kind
        edges = FakeEdges({(1, 2): (10, "a")})
        self.assertEqual(point_kind([2, 1], edges), "A")
        self.assertEqual(point_kind([3, 4], edges), "small")
        # Snapped exactly onto a node: A when that node is on the network.
        self.assertEqual((point_kind([1, 1], edges), point_kind([5, 5], edges)), ("A", "small"))


class StratifiedSampleTests(unittest.TestCase):
    RING = [(105.0, 20.0), (106.5, 20.0), (106.5, 22.0), (105.0, 20.0)]

    def sample(self, seed=0):
        from gate_matrix_experiment import sample_stratified
        junctions = np.array([(lon, lat) for lon in np.arange(105.0, 106.5, 0.05) for lat in np.arange(20.0, 22.0, 0.05)
                              if lat - 20.0 <= (lon - 105.0) * 2 / 1.5])
        # Snaps along every fifth thousandth of a degree of longitude are 800 m off; north of 21.5 the road is on A.
        router = EuclidRouter(snap_m=lambda p: 800.0 if int(p[0] * 1000) % 5 == 0 else 10.0,
                              nodes=lambda p: (1, 2) if p[1] > 21.5 else (3, 4))
        return junctions, sample_stratified(seed, (4, 4, 2, 0), self.RING, junctions, router, FakeEdges({(1, 2): (10, "a")}))

    def test_each_band_gets_its_pairs_split_by_kind(self):
        _, (pairs, rejections) = self.sample()
        by_band = {}
        for pair in pairs:
            by_band.setdefault(pair["band"], []).append(pair["kind"])
        self.assertEqual({band: sorted(kinds) for band, kinds in by_band.items()},
                         {"<30": ["A-A", "small-A", "small-small", "small-small"],
                          "30-100": ["A-A", "small-A", "small-small", "small-small"], "100-300": ["A-A", "small-small"]})
        limits = {"<30": (0, 30), "30-100": (30, 100), "100-300": (100, 300)}
        for pair in pairs:
            lo, hi = limits[pair["band"]]
            self.assertTrue(lo <= pair["km"] < hi, pair)
            self.assertAlmostEqual(pair["km"], haversine(pair["src"], pair["dst"]), places=6)

    def test_rejected_points_are_redrawn_and_counted(self):
        junctions, (pairs, rejections) = self.sample()
        self.assertGreater(rejections["snap_far"], 0)
        self.assertGreater(rejections["outside_land"], 0)
        for pair in pairs:
            for end in ("src", "dst"):
                if pair[f"{end}_kind"] == "small":
                    self.assertLessEqual(pair[end][1], 21.5)
                else:
                    self.assertIsNotNone(pair[f"{end}_node"])
                    self.assertEqual(tuple(pair[end]), tuple(junctions[pair[f"{end}_node"]]))

    def test_same_seed_same_sample(self):
        self.assertEqual(self.sample(0)[1], self.sample(0)[1])
        self.assertNotEqual(self.sample(0)[1][0], self.sample(1)[1][0])


class EntryAndRelationTests(unittest.TestCase):
    def test_entry_node_is_the_quickest_by_osrm_not_the_nearest(self):
        from gate_matrix_experiment import entry_node
        junctions = np.array([(105.0, 21.001), (105.0, 21.01), (105.0, 21.02)])

        class Router:
            def durations(self, sources, destinations):
                # The closest junction sits across a river: 20 minutes by road.
                return [[1200.0, 90.0, 150.0]]

        self.assertEqual(entry_node((105.0, 21.0), junctions, Router(), count=3), 1)

    def test_cluster_relation_is_same_adjacent_or_far(self):
        from types import SimpleNamespace
        from gate_matrix_experiment import cluster_adjacency, relation
        # Clusters 0 - 1 - 2 in a row: 0 and 2 only meet through 1.
        graph = SimpleNamespace(a=np.array([0, 1, 2, 3]), b=np.array([1, 2, 3, 4]))
        adjacency = cluster_adjacency(graph, np.array([0, 0, 1, 1, 2]))
        self.assertEqual([relation(0, 0, adjacency), relation(0, 1, adjacency), relation(2, 1, adjacency), relation(0, 2, adjacency)],
                         ["same", "adjacent", "adjacent", "far"])


class PassRuleTests(unittest.TestCase):
    def test_far_pairs_pass_at_p95_1pct_and_max_5pct(self):
        from gate_matrix_experiment import passes
        self.assertTrue(passes(np.array([0.0] * 19 + [0.05])))
        self.assertFalse(passes(np.array([0.0] * 19 + [0.051])))
        self.assertFalse(passes(np.array([0.0] * 10 + [0.02] * 10)))
        self.assertIsNone(passes(np.array([])))


class ClassifyTests(unittest.TestCase):
    EDGES = FakeEdges({(1, 2): (100, "a"), (2, 3): (100, "a"), (6, 7): (200, "a"), (7, 8): (200, "a"), (3, 6): (300, "a"),
                       (8, 9): (400, "ferry")})
    WAYS = {"100": 0, "300": 0, "200": 1, "400": 1}
    GATES = [[105.0, 21.0], [105.1, 21.0]]

    def via(self, nodes=(1, 2, 3, 6, 7, 8), waypoints=None, legs=None):
        waypoints = waypoints or [[104.9, 21.0], self.GATES[0], self.GATES[1], [105.2, 21.0]]
        return {"nodes": list(nodes), "legs": legs or [[1, 2, 3], [3, 6], [6, 7, 8]], "waypoints": waypoints}

    def classify(self, direct, via):
        from gate_matrix_experiment import classify
        return classify(direct, via, self.GATES, self.EDGES, self.WAYS)

    def test_direct_route_leaving_the_network_between_clusters_is_a_bypass(self):
        direct = {"nodes": [1, 2, 50, 51, 7, 8], "legs": [[1, 2, 50, 51, 7, 8]], "waypoints": []}
        self.assertEqual(self.classify(direct, self.via())[0], "bypass_small_road")

    def test_leaving_the_source_cluster_on_small_roads_is_a_bypass(self):
        from gate_matrix_experiment import classify
        # The source belongs to cluster 0, but its route reaches the network only on cluster 1's road.
        direct = {"nodes": [50, 51, 7, 8], "legs": [[50, 51, 7, 8]], "waypoints": []}
        self.assertEqual(classify(direct, self.via(), self.GATES, self.EDGES, self.WAYS, clusters=(0, 1))[0], "bypass_small_road")
        on_network = {"nodes": [1, 2, 3, 6, 7, 8], "legs": [[1, 2, 3, 6, 7, 8]], "waypoints": []}
        self.assertEqual(classify(on_network, self.via(), self.GATES, self.EDGES, self.WAYS, clusters=(0, 1)), ["other"])

    def test_ferry_on_either_route(self):
        direct = {"nodes": [1, 2, 3, 6, 7, 8, 9], "legs": [[1, 2, 3, 6, 7, 8, 9]], "waypoints": []}
        self.assertEqual(self.classify(direct, self.via()), ["ferry"])

    def test_gate_snapped_far_or_u_turn_at_a_gate(self):
        direct = {"nodes": [1, 2, 3, 6, 7, 8], "legs": [[1, 2, 3, 6, 7, 8]], "waypoints": []}
        off = self.via(waypoints=[[104.9, 21.0], [105.0, 21.001], self.GATES[1], [105.2, 21.0]])
        self.assertEqual(self.classify(direct, off), ["gate_snap"])
        u_turn = self.via(nodes=(1, 2, 3, 2, 3, 6, 7, 8), legs=[[1, 2, 3], [3, 2, 3, 6], [6, 7, 8]])
        self.assertEqual(self.classify(direct, u_turn), ["gate_snap"])
        self.assertEqual(self.classify(direct, self.via()), ["other"])


class WorstGeojsonTests(unittest.TestCase):
    def test_each_case_has_two_routes_and_two_gates(self):
        from gate_matrix_experiment import worst_geojson
        case = {"partition": "natural", "case": "worst", "label": "other", "labels": ["other"], "clusters": [0, 1],
                "t_direct": 100.0, "t_gate": 130.0, "error": 0.3, "km": 12.0, "src": [105.0, 21.0], "dst": [105.2, 21.0],
                "gates": [[105.05, 21.0], [105.1, 21.0]], "direct_coords": [[105.0, 21.0], [105.2, 21.0]],
                "via_coords": [[105.0, 21.0], [105.05, 21.0], [105.1, 21.0], [105.2, 21.0]], "direct_ways": ["QL.1"], "via_ways": ["QL.1"],
                "direct_a_km": 10.0, "via_a_km": 11.0}
        collection = worst_geojson([case, {**case, "case": "anomaly"}])
        self.assertEqual(collection["type"], "FeatureCollection")
        self.assertEqual(len(collection["features"]), 8)
        kinds = [(f["geometry"]["type"], f["properties"]["role"]) for f in collection["features"][:4]]
        self.assertEqual(kinds, [("LineString", "direct"), ("LineString", "via_gate"), ("Point", "gate"), ("Point", "gate")])
        props = collection["features"][1]["properties"]
        self.assertEqual((props["label"], props["partition"], props["case"], props["stroke-dasharray"]), ("other", "natural", "worst", "6 4"))
        for key in ("t_direct", "t_gate", "error", "km", "clusters"):
            self.assertIn(key, props)


def line_world(edges=(105.4,)):
    """Towns every 0.1 degree along lat 21 from lon 105.0 to 105.9, each with a short spur north.

    Clusters run west to east, a new one starting after each lon in `edges` (default: west <= 105.4 is cluster 0).
    """
    towns = [(round(105.0 + 0.1 * k, 1), 21.0) for k in range(10)]
    cluster = lambda lon: sum(lon > e + 1e-9 for e in edges)  # noqa: E731
    feats, ways, node_labels = [], {}, []
    for k, town in enumerate(towns):
        feats.append(line(100 + k, [town, (town[0], 21.005)]))
        ways[str(100 + k)] = cluster(town[0])
        node_labels += [[town[0], 21.0, cluster(town[0])], [town[0], 21.005, cluster(town[0])]]
        if k + 1 < len(towns):
            feats.append(line(200 + k, [town, towns[k + 1]]))
            ways[str(200 + k)] = cluster(town[0])
    boundary = [[lon, 21.0] for e in edges for lon in (e, round(e + 0.1, 1))]
    partition = {"k": len(edges) + 1, "method": "natural", "ways": ways, "node_labels": node_labels,
                 "cuts": [[[e, 21.0], [round(e + 0.1, 1), 21.0]] for e in edges], "boundary": boundary, "stats": {}}
    return feats, {"method": "regions", "pbf_sha256": "map-a", "policy_version": 6, "partitions": [partition]}


class FarPairTests(unittest.TestCase):
    def test_only_pairs_in_non_adjacent_clusters_are_measured(self):
        from gate_matrix_experiment import evaluate_partition
        from clusters import build_graph
        feats, doc = line_world(edges=(105.2, 105.5))
        graph = build_graph(feats)
        node = {tuple(np.round(p, 6)): i for i, p in enumerate(graph.lonlat)}
        # same (105.1-105.2), adjacent (105.2-105.4), far (105.1-105.8, 105.2 spur end-105.7).
        pairs = [(node[(105.1, 21.0)], node[(105.2, 21.0)]), (node[(105.2, 21.0)], node[(105.4, 21.0)]),
                 (node[(105.1, 21.0)], node[(105.8, 21.0)]), (node[(105.2, 21.005)], node[(105.7, 21.0)])]
        result = evaluate_partition(graph, doc["partitions"][0], pairs, EuclidRouter())
        self.assertEqual((result["same_cluster"], result["adjacent"], result["far"]), (1, 1, 2))
        self.assertEqual(result["metrics"]["pairs"], 2)
        self.assertEqual(result["cross_metrics"]["pairs"], 3)
        self.assertEqual(result["direct_share"], 0.5)
        self.assertIn(result["pass"], (True, False))


class SmallPointGateTests(unittest.TestCase):
    """Clusters 0 | 1 | 2 in a row (a1-a2 | b1-b2 | c1-c2). S enters the network fastest at a1 (cluster 0) but its
    road to the east joins at b2 (cluster 1), far from cluster 0's gates."""

    a1, a2, b1, b2, c1, c2 = (105.0, 21.0), (105.0, 21.1), (105.1, 21.1), (105.1, 21.0), (105.2, 21.0), (105.3, 21.0)
    S = (105.02, 21.0)

    def world(self):
        from clusters import build_graph
        chain = [self.a1, self.a2, self.b1, self.b2, self.c1, self.c2]
        feats = [line(300 + k, [p, q]) for k, (p, q) in enumerate(zip(chain, chain[1:]))]
        feats += [line(400 + k, [p, (p[0] + 0.002, p[1] + 0.002)]) for k, p in enumerate(chain[1:-1])]
        graph = build_graph(feats)
        cluster = {self.a1: 0, self.a2: 0, self.b1: 1, self.b2: 1, self.c1: 2, self.c2: 2}
        labels = np.array([cluster.get(min(cluster, key=lambda c: km(c, tuple(p)))) for p in graph.lonlat])
        roads = [tuple(c) for c in zip(chain, chain[1:])] + [(p, (p[0] + 0.002, p[1] + 0.002)) for p in chain[1:-1]]
        return graph, labels, FakeRouter(roads + [(self.S, self.a1), (self.S, self.b2)])

    def test_small_road_point_also_uses_the_gates_of_adjacent_clusters(self):
        from gate_matrix_experiment import cluster_adjacency, compose, gate_sets
        graph, labels, router = self.world()
        boundary = [list(p) for p in (self.a2, self.b1, self.b2, self.c1)]
        sets = gate_sets(graph, labels, boundary)
        self.assertEqual(sorted(map(tuple, sets[("own", 0)])), sorted([self.a2, self.b1]))
        self.assertEqual(set(map(tuple, sets[("ext", 0)])), {self.a2, self.b1, self.b2, self.c1})
        self.assertEqual(cluster_adjacency(graph, labels), {frozenset((0, 1)), frozenset((1, 2))})
        direct = router.durations([self.S], [self.c2])[0][0]
        (extended,), _ = compose([(self.S, ("ext", 0), self.c2, ("own", 2))], sets, router)
        (own,), _ = compose([(self.S, ("own", 0), self.c2, ("own", 2))], sets, router)
        self.assertAlmostEqual(extended, direct, places=6)
        self.assertGreater(own, direct * 1.5)


class NodeLabelRequiredTests(unittest.TestCase):
    def test_cluster_file_without_node_labels_stops_both_rounds_without_a_file(self):
        from gate_matrix_experiment import run, run_stratified
        doc = clusters_doc()
        del doc["partitions"][0]["node_labels"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features()}), encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
            (root / "big_road_clusters.json").write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "node_labels.*model/clusters"):
                run(root / "big_roads.geojson", root / "big_road_clusters.json", root / "out.json", FakeRouter(ROADS))
            ring = [(104.9, 20.9), (106.1, 20.9), (106.1, 21.1), (104.9, 21.1), (104.9, 20.9)]
            with self.assertRaisesRegex(ValueError, "node_labels.*model/clusters"):
                run_stratified(root / "big_roads.geojson", root / "big_road_clusters.json", root / "r2.json", root / "w.geojson",
                               EuclidRouter(), FakeEdges(), ring, band_pairs=(2, 0, 0, 0))
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["big_road_clusters.json", "big_roads.geojson", "big_roads.meta.json"])


class StratifiedRunTests(unittest.TestCase):
    def test_stratified_run_writes_the_report_and_the_worst_pairs_map(self):
        from gate_matrix_experiment import run_stratified
        feats, doc = line_world(edges=(105.2, 105.5))
        ring = [(104.95, 20.95), (105.95, 20.95), (105.95, 21.05), (104.95, 21.05), (104.95, 20.95)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
            (root / "big_road_clusters.json").write_text(json.dumps(doc), encoding="utf-8")
            report = run_stratified(root / "big_roads.geojson", root / "big_road_clusters.json", root / "r2.json", root / "worst.geojson",
                                    EuclidRouter(), FakeEdges(), ring, band_pairs=(8, 8, 0, 0))
            saved = json.loads((root / "r2.json").read_text(encoding="utf-8"))
            worst = json.loads((root / "worst.geojson").read_text(encoding="utf-8"))
            leftovers = [p.name for p in root.iterdir() if "tmp" in p.name]
        self.assertEqual(saved, report)
        self.assertEqual(leftovers, [])
        self.assertEqual(report["sample"]["pairs"], 16)
        item, = report["partitions"]
        self.assertEqual(item["name"], "natural")
        self.assertIn(item["pass"], (True, False, None))
        self.assertEqual(set(item["far_metrics"]) >= {"pairs", "p95", "max"}, True)
        self.assertTrue(all(c["relation"] == "far" and c["case"] == "worst" for c in item["inspected"]))
        self.assertTrue(all(set(g) >= {"pairs", "median", "p95", "max", "over_5pct"} for g in item["groups"].values()))
        self.assertEqual(len(worst["features"]), 4 * len(item["inspected"]))
        self.assertEqual(item["anomalies"], 0)  # straight lines never beat the detour through gates
        self.assertIsNone(item["direct_share_round1"])  # no round 1 report given


# ---------------------------------------------------------------- round 4: natural partition, gates off the network


def natural_doc(extra=None):
    """clusters_doc() as a natural partition with a small-road bridge A2-B2 whose two ends are extra gates."""
    doc = clusters_doc()
    partition = doc["partitions"][0]
    partition["method"] = "natural"
    partition["extra_gates"] = extra if extra is not None else [
        {"at": list(A2), "clusters": [0, 1], "way_id": 77, "kind": "bridge"},
        {"at": list(B2), "clusters": [1, 0], "way_id": 77, "kind": "bridge"}]
    return doc


class SnapRouter(FakeRouter):
    """FakeRouter with /nearest: each point snaps onto itself, at the given distance in metres."""

    def __init__(self, roads, snap_m):
        super().__init__(roads)
        self.snap_m = snap_m

    def nearest(self, point):
        return {"location": [round(point[0], 6), round(point[1], 6)], "nodes": [1, 2], "distance": self.snap_m(tuple(point))}


class NaturalGateTests(unittest.TestCase):
    def setUp(self):
        from clusters import build_graph
        self.graph = build_graph(features())
        self.node = {tuple(np.round(p, 6)): i for i, p in enumerate(self.graph.lonlat)}

    def test_natural_partition_is_named_and_read(self):
        from gate_matrix_experiment import partition_labels, partition_name
        partition = natural_doc()["partitions"][0]
        self.assertEqual(partition_name(partition), "natural")
        labels = partition_labels(self.graph, partition)
        self.assertEqual([labels[self.node[p]] for p in (A0, B0)], [0, 1])

    def test_ends_of_a_small_road_bridge_are_gates_of_both_banks(self):
        from gate_matrix_experiment import evaluate_partition
        router = FakeRouter(ROADS + [(A2, B2)])
        pairs = [(self.node[A2], self.node[B2])]
        plain = evaluate_partition(self.graph, clusters_doc()["partitions"][0], pairs, router)
        natural = evaluate_partition(self.graph, natural_doc()["partitions"][0], pairs, router)
        self.assertGreater(plain["t_gate"][0], plain["t_direct"][0] * 1.01)
        self.assertAlmostEqual(natural["t_gate"][0], natural["t_direct"][0], places=6)
        self.assertEqual({c: sorted(map(tuple, points)) for c, points in natural["gate_points"].items()},
                         {0: sorted([A0, B0, A2, B2]), 1: sorted([A0, B0, A2, B2])})

    def test_extra_gates_are_snapped_and_far_snaps_are_dropped(self):
        from gate_matrix_experiment import extra_gate_points
        router = SnapRouter(ROADS + [(A2, B2)], lambda p: 900.0 if p == B2 else 5.0)
        points, dropped = extra_gate_points(natural_doc()["partitions"][0], router)
        self.assertEqual(points, {0: [A2], 1: [A2]})
        self.assertEqual(dropped, 1)
        # Without /nearest (round 1's plain table router) the points are used as they are.
        points, dropped = extra_gate_points(natural_doc()["partitions"][0], FakeRouter(ROADS))
        self.assertEqual((points, dropped), ({0: [A2, B2], 1: [A2, B2]}, 0))

    def test_round_two_gate_sets_include_the_extra_gates(self):
        from gate_matrix_experiment import gate_sets, partition_labels
        partition = natural_doc()["partitions"][0]
        labels = partition_labels(self.graph, partition)
        sets = gate_sets(self.graph, labels, partition["boundary"], {0: [A2, B2], 1: [A2, B2]})
        self.assertEqual(sorted(map(tuple, sets[("own", 0)])), sorted([A0, B0, A2, B2]))

    def test_run_counts_extra_gates_in_the_cost(self):
        from gate_matrix_experiment import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features()}), encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
            (root / "big_road_clusters.json").write_text(json.dumps(natural_doc()), encoding="utf-8")
            report = run(root / "big_roads.geojson", root / "big_road_clusters.json", root / "out.json", FakeRouter(ROADS + [(A2, B2)]),
                         pairs=20)
        item, = report["partitions"]
        self.assertEqual(item["name"], "natural")
        self.assertEqual((item["cost"]["gates"], item["cost"]["extra_gates"]), (4, 2))
        self.assertIsNotNone(item["cost"]["full_gate_table_s"])


if __name__ == "__main__":
    unittest.main()
