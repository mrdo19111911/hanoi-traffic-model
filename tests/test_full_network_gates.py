"""Offline tests for step 4a: labelling the whole car network by natural_spectral sub-cluster and counting gates."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Nodes as (lat, lon). The A road QL.1 runs A1-A2-A3 (sub-clusters 0 | 1, both in river region 0) and crosses the
# main river (lon 105.5) on the A bridge A3-A4 to sub-cluster 2 (region 1). A residential road R1-R2-R3 runs
# parallel to it, joined to A1 and A3; S hangs off R1. On the east bank R4 joins A4. X is reached only by a road
# over the river, Y by one over the river and one from R4.
NODES = {1: (21.0, 105.0), 2: (21.0, 105.09), 3: (21.0, 105.2), 4: (21.0, 105.6),
         11: (21.05, 105.0), 12: (21.05, 105.09), 13: (21.05, 105.2), 14: (21.05, 105.6),
         21: (21.1, 105.15), 31: (21.07, 105.55), 32: (21.03, 105.52)}
A_WAYS = [(1, [1, 2, 3], {"highway": "trunk", "ref": "QL.1"}),
          (2, [3, 4], {"highway": "trunk", "ref": "QL.1", "bridge": "yes"})]
OTHER_WAYS = [(3, [11, 12, 13], {"highway": "residential"}), (4, [1, 11], {"highway": "residential"}),
              (5, [3, 13], {"highway": "residential"}), (6, [4, 14], {"highway": "residential"}),
              (7, [21, 11], {"highway": "residential"}), (8, [13, 14], {"highway": "residential", "bridge": "yes"}),
              (9, [13, 31], {"highway": "residential", "bridge": "yes"}), (10, [13, 32], {"highway": "residential", "bridge": "yes"}),
              (11, [32, 14], {"highway": "residential"}), (12, [11, 21], {"highway": "footway"})]
at = lambda n: [NODES[n][1], NODES[n][0]]  # noqa: E731


def clusters_doc(sha):
    partition = {"method": "natural_spectral", "k": 3, "node_labels": [[*at(1), 0], [*at(3), 1], [*at(4), 2]],
                 "parents": {"0": 0, "1": 0, "2": 1}, "boundary": [at(1), at(3), at(4)],
                 "extra_gates": [{"at": at(13), "clusters": [1, 2], "way_id": 8, "kind": "bridge"},
                                 {"at": at(14), "clusters": [2, 1], "way_id": 8, "kind": "bridge"}],
                 "adjacency": [[0, 1], [1, 2]], "ways": {}, "cuts": [], "stats": {}}
    return {"method": "spectral_njw_inv_time", "pbf_sha256": sha, "policy_version": 6, "partitions": [partition]}


def rivers_doc(sha):
    return {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": 6, "crossings": [],
            "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[105.5, 20.0], [105.5, 22.0]]},
                          "properties": {"river": "Sông Thử", "river_index": 0, "way_id": 900}}]}


def make_inputs(root):
    import hashlib
    from build_big_roads import write_edges
    from test_natural_barriers import write_osm
    pbf = root / "tiny.osm"
    write_osm(pbf, NODES, A_WAYS + OTHER_WAYS)
    sha = hashlib.sha256(pbf.read_bytes()).hexdigest()
    rows = [(min(a, b), max(a, b), way_id, "a") for way_id, refs, _ in A_WAYS for a, b in zip(refs, refs[1:])]
    write_edges(root / "big_road_edges.sqlite", rows, {"pbf_sha256": sha, "policy_version": "6"})
    (root / "big_road_clusters.json").write_text(json.dumps(clusters_doc(sha)), encoding="utf-8")
    (root / "main_rivers.geojson").write_text(json.dumps(rivers_doc(sha)), encoding="utf-8")
    return pbf, sha


class LabelAndGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from full_network_gates import survey_gates
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        pbf, sha = make_inputs(root)
        cls.result = survey_gates(pbf, clusters_doc(sha)["partitions"][0], rivers_doc(sha), root / "big_road_edges.sqlite",
                                  keep_node_labels=True)
        cls.labels = cls.result["node_labels"]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_nodes_take_the_label_of_the_quickest_junction_not_the_nearest(self):
        self.assertEqual([self.labels[n] for n in (1, 2, 11, 12)], [0, 0, 0, 0])
        self.assertEqual([self.labels[n] for n in (3, 13)], [1, 1])
        # S lies closer to A3 as the crow flies, but its only road leads to A1.
        self.assertEqual(self.labels[21], 0)

    def test_labels_do_not_cross_a_main_river(self):
        self.assertEqual([self.labels[n] for n in (4, 14, 32)], [2, 2, 2])
        self.assertEqual(self.labels[31], -1)
        self.assertEqual(self.result["unreachable_nodes"], 1)
        self.assertEqual(self.result["dropped_river_edges"], 4)

    def test_the_residential_road_gives_a_gate_and_the_a_edge_is_not_counted_again(self):
        report = self.result["report"]
        self.assertEqual(report["boundary_edges"]["before_merge"], {"total": 1, "by_highway": {"residential": 1}})
        self.assertEqual(report["boundary_edges"]["after_merge"], {"total": 1, "by_highway": {"residential": 1}})
        self.assertEqual(report["boundary_edges"]["a_edges_skipped"], 1)
        crossing, = self.result["crossings"]
        self.assertEqual(sorted(map(tuple, crossing["gates"])), sorted([tuple(at(12)), tuple(at(13))]))
        self.assertEqual(crossing["pair"], [0, 1])

    def test_new_gate_count_and_gates_per_pair(self):
        report = self.result["report"]
        self.assertEqual(report["gates"], {"boundary_junctions": 3, "small_bridge": 2, "full_network_spectral": 1, "total": 6})  # R3 is already a small-bridge gate
        self.assertEqual(report["pairs"]["0-1"], {"a_cut_edges": 1, "extra_crossings": 0, "full_network_crossings": 1})
        self.assertEqual(report["pairs"]["1-2"]["extra_crossings"], 1)
        self.assertEqual(report["cross_region_edges"], 0)


class MergeTests(unittest.TestCase):
    def test_crossings_under_100_m_apart_for_the_same_pair_merge(self):
        from full_network_gates import merge_crossings
        # Along a parallel, 0.000476 degrees of longitude is about 50 m at lat 21.
        step = 50 / (111320 * 0.9336)
        points = [(105.0, 21.0), (105.0 + step, 21.0), (105.0 + 4 * step, 21.0), (105.0 + step, 21.0)]
        pairs = [(0, 1), (0, 1), (0, 1), (1, 2)]
        groups = merge_crossings(points, pairs, metres=100)
        self.assertEqual(groups[0], groups[1])  # 50 m apart
        self.assertNotEqual(groups[1], groups[2])  # 150 m apart
        self.assertNotEqual(groups[1], groups[3])  # same place, other pair
        self.assertEqual(len(set(groups)), 3)


class TableTimingTests(unittest.TestCase):
    def test_full_table_is_timed_when_small_and_sampled_when_large(self):
        from full_network_gates import time_gate_table

        class Router:
            calls = 0

            def durations(self, sources, destinations):
                Router.calls += 1
                return [[1.0] * len(destinations) for _ in sources]

        points = [(105.0 + i * 0.001, 21.0) for i in range(250)]
        full = time_gate_table(points, Router(), max_full=300, block=100)
        self.assertEqual((full["mode"], full["requests"]), ("full", 9))
        sampled = time_gate_table(points, Router(), max_full=200, sample_rows=100, block=100)
        self.assertEqual((sampled["mode"], sampled["requests"]), ("sampled", 3))
        self.assertAlmostEqual(sampled["row_fraction"], 100 / 250)
        self.assertIn("estimated_s", sampled)
        self.assertAlmostEqual(sampled["slots_96_s"], 96 * sampled["estimated_s"], places=6)


if __name__ == "__main__":
    unittest.main()
