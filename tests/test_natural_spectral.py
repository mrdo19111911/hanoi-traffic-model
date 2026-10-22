"""Offline tests for the two-level "natural_spectral" partition: main-river regions, then NJW spectral inside each."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

# West region: two dense squares W (lon 105.0) and V (lon 105.3) joined by one 31 km edge (way 50).
# A main river at lon 105.6 parts it from the east region, one square E (lon 106.0), reached by the A bridge 99
# from V and by a small-road bridge (way 77) whose two ends become extra gates.
W = [(105.0, 21.0), (105.01, 21.0), (105.01, 21.01), (105.0, 21.01)]
V = [(105.3, 21.0), (105.31, 21.0), (105.31, 21.01), (105.3, 21.01)]
E = [(106.0, 21.0), (106.01, 21.0), (106.01, 21.01), (106.0, 21.01)]


def line(way_id, coords, group="quoclo"):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
            "properties": {"way_id": way_id, "group": group, "fragment": False, "isolated": False}}


def square(base, sq):
    a, b, c, d = sq
    return [line(base + 1, [a, b]), line(base + 2, [b, c]), line(base + 3, [c, d]), line(base + 4, [d, a]),
            line(base + 5, [a, c]), line(base + 6, [b, d])]


def world():
    return square(0, W) + square(10, V) + square(20, E) + [line(50, [W[1], V[0]]), line(99, [V[1], E[0]])]


def rivers_doc(sha="map-a"):
    return {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": 6,
            "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[105.6, 20.0], [105.6, 22.0]]},
                          "properties": {"river": "Sông Thử", "river_index": 0, "way_id": 900}}],
            "crossings": [{"way_id": 99, "in_a": True, "kind": "bridge", "highway": "trunk", "river": "Sông Thử",
                           "ends": [[105.599, 21.0], [105.601, 21.0]]},
                          {"way_id": 77, "in_a": False, "kind": "bridge", "highway": "residential", "river": "Sông Thử",
                           "ends": [[105.599, 21.008], [105.601, 21.008]]}]}


class AllocationTests(unittest.TestCase):
    def test_sub_clusters_follow_km_by_largest_remainder(self):
        from spectral_clusters import allocate_k
        self.assertEqual(allocate_k([600.0, 300.0, 100.0], 22), [13, 7, 2])
        self.assertEqual(allocate_k([500.0, 300.0, 150.0, 50.0], 10), [5, 3, 1, 1])

    def test_every_region_gets_at_least_one_and_the_sum_is_the_target(self):
        from spectral_clusters import allocate_k
        for km, target in (([1000.0, 10.0, 10.0], 3), ([1000.0, 10.0, 10.0], 7), ([32476.0, 10855.0, 9739.0, 4889.0, 3364.0,
                                                                                    2137.0, 545.0, 309.0], 22)):
            k = allocate_k(km, target)
            self.assertEqual(sum(k), target)
            self.assertTrue(all(v >= 1 for v in k))
            self.assertEqual(k, allocate_k(km, target))
            self.assertEqual(k, sorted(k, reverse=True))  # km is sorted, so is k
        with self.assertRaises(ValueError):
            allocate_k([1.0, 2.0, 3.0], 2)


class TwoLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from spectral_clusters import build_graph, natural_partition, natural_spectral_partition
        cls.graph = build_graph(world())
        cls.node = {tuple(np.round(p, 6)): i for i, p in enumerate(cls.graph.lonlat)}
        cls.natural = natural_partition(cls.graph, rivers_doc(), min_km=1.0)
        cls.result = natural_spectral_partition(cls.graph, cls.natural, target=3, seed=0)

    def label(self, point):
        return int(self.result["labels"][self.node[point]])

    def test_three_sub_clusters_the_long_link_is_cut_and_none_spans_the_river(self):
        labels = self.result["labels"]
        self.assertEqual(len(set(labels.tolist())), 3)
        self.assertEqual(self.result["k_by_parent"], {"0": 2, "1": 1})
        self.assertNotEqual(self.label(W[1]), self.label(V[0]))
        self.assertEqual(len({self.label(p) for p in (W[0], W[2])}), 1)
        self.assertEqual(len({self.label(p) for p in (V[1], V[2])}), 1)
        parents = self.natural["labels"]
        for c in set(labels.tolist()):
            self.assertEqual(len(set(parents[labels == c].tolist())), 1)
            self.assertEqual(self.result["parents"][c], int(parents[labels == c][0]))
        self.assertEqual(sorted(set(range(3))), sorted(set(labels.tolist())))

    def test_extra_gates_follow_the_sub_clusters_of_their_banks(self):
        gates = {tuple(g["at"]): g["clusters"] for g in self.result["extra_gates"]}
        self.assertEqual(gates, {(105.599, 21.008): [self.label(V[2]), self.label(E[0])],
                                 (105.601, 21.008): [self.label(E[0]), self.label(V[2])]})

    def test_adjacency_has_cut_edges_and_river_crossings(self):
        pairs = {tuple(sorted(p)) for p in self.result["adjacency"]}
        self.assertIn(tuple(sorted((self.label(W[1]), self.label(V[0])))), pairs)
        self.assertIn(tuple(sorted((self.label(V[1]), self.label(E[0])))), pairs)


class ClusterFileTests(unittest.TestCase):
    def test_the_cluster_file_carries_the_two_level_partition(self):
        from spectral_clusters import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": world()}), encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
            (root / "main_rivers.geojson").write_text(json.dumps(rivers_doc()), encoding="utf-8")
            run(root / "big_roads.geojson", root / "clusters.json", ks=(2,), natural_min_km=1.0, natural_target=3)
            doc = json.loads((root / "clusters.json").read_text(encoding="utf-8"))
        two = next(p for p in doc["partitions"] if p.get("method") == "natural_spectral")
        spectral = next(p for p in doc["partitions"] if p.get("method") == "spectral")
        self.assertTrue(set(spectral) <= set(two))
        self.assertEqual(two["k"], 3)
        self.assertEqual(two["stats"]["k_by_parent"], {"0": 2, "1": 1})
        self.assertEqual(sorted(two["parents"].values()), [0, 0, 1])
        boundary = {tuple(p) for p in two["boundary"]}
        # River gates (the A bridge) and the ends of the spectral cut (the long link) are both there.
        self.assertTrue({V[1], E[0], W[1], V[0]} <= boundary)
        self.assertEqual(len(two["extra_gates"]), 2)
        self.assertIn("contiguous", two["stats"])


class ExperimentReadsTests(unittest.TestCase):
    def test_experiment_names_it_and_uses_its_adjacency(self):
        from gate_matrix_experiment import partition_adjacency, partition_name
        from spectral_clusters import build_graph
        graph = build_graph(world())
        labels = np.zeros(graph.n, dtype=np.int64)
        partition = {"method": "natural_spectral", "k": 3, "adjacency": [[0, 2]]}
        self.assertEqual(partition_name(partition), "natural_spectral")
        self.assertEqual(partition_adjacency(graph, labels, partition), {frozenset((0, 2))})


if __name__ == "__main__":
    unittest.main()
