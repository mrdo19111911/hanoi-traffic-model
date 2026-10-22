"""Offline tests for the "natural" (main river) partition of the big-road network in model/clusters.py."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Two squares of junctions (W at lon 105.0, E at lon 106.0) joined by the A bridge 99 across a main river at lon 105.5.
W0, W1, W2, W3 = (105.0, 21.0), (105.01, 21.0), (105.01, 21.01), (105.0, 21.01)
E0, E1, E2, E3 = (106.0, 21.0), (106.01, 21.0), (106.01, 21.01), (106.0, 21.01)
RIVER = [[105.5, 20.0], [105.5, 22.0]]


def line(way_id, coords, group="quoclo", **props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
            "properties": {"way_id": way_id, "group": group, "fragment": False, "isolated": False, **props}}


def square_features():
    return [line(1, [W0, W1]), line(2, [W1, W2]), line(3, [W2, W3]), line(4, [W3, W0]), line(5, [W0, W2]),
            line(11, [E0, E1]), line(12, [E1, E2]), line(13, [E2, E3]), line(14, [E3, E0]), line(15, [E0, E2]),
            line(99, [W0, E0])]


def rivers_doc(sha="map-a", crossings=None):
    return {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": 6,
            "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": RIVER},
                          "properties": {"river": "Sông Thử", "river_index": 0, "way_id": 900}}],
            "crossings": crossings if crossings is not None else [
                {"way_id": 99, "in_a": True, "kind": "bridge", "highway": "trunk", "river": "Sông Thử",
                 "ends": [[105.49, 21.0], [105.51, 21.0]]},
                {"way_id": 77, "in_a": False, "kind": "bridge", "highway": "residential", "river": "Sông Thử",
                 "ends": [[105.499, 21.008], [105.501, 21.008]]}]}


def square(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def province(iso, name, *polygons):
    return {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [list(p) for p in polygons]},
            "properties": {"iso": iso, "name": name, "relation_id": 1, "area_km2": 1.0, "points": 5}}


class MergeTests(unittest.TestCase):
    def test_small_components_join_the_neighbour_with_most_connecting_edges(self):
        from clusters import merge_components
        # 0 and 1 are large; 2 is small with 3 edges to 1 and 1 edge to 0; 3 is small and has no connection.
        km = [500.0, 300.0, 50.0, 10.0]
        links = {(0, 2): 1, (1, 2): 3, (0, 1): 2}
        self.assertEqual(merge_components(km, links, 200.0), [0, 1, 1, -1])

    def test_a_tie_goes_to_the_larger_neighbour_and_merging_repeats(self):
        from clusters import merge_components
        # 3 (linked only to 2) joins 2 first; then 2+3 ties between 0 and 1 and joins the larger 1, which becomes label 0.
        km = [300.0, 400.0, 20.0, 5.0]
        links = {(0, 2): 2, (1, 2): 2, (2, 3): 1}
        self.assertEqual(merge_components(km, links, 200.0), [1, 0, 0, 0])

    def test_small_pieces_merge_together_until_large_enough(self):
        from clusters import merge_components
        self.assertEqual(merge_components([120.0, 100.0], {(0, 1): 1}, 200.0), [0, 0])


class NaturalPartitionTests(unittest.TestCase):
    def setUp(self):
        from clusters import build_graph
        self.graph = build_graph(square_features())
        self.node = {tuple(np.round(p, 6)): i for i, p in enumerate(self.graph.lonlat)}

    def test_a_main_river_cuts_the_bridge_and_non_a_crossings_give_gates_on_both_banks(self):
        from clusters import natural_partition
        result = natural_partition(self.graph, rivers_doc(), min_km=1.0)
        labels = result["labels"]
        self.assertNotEqual(labels[self.node[W0]], labels[self.node[E0]])
        self.assertEqual(len(set(labels.tolist())), 2)
        west, east = int(labels[self.node[W2]]), int(labels[self.node[E2]])
        gates = {(tuple(g["at"]), tuple(g["clusters"])) for g in result["extra_gates"]}
        self.assertEqual(gates, {((105.499, 21.008), (west, east)), ((105.501, 21.008), (east, west))})
        self.assertEqual(result["info"]["cut_edges"], 1)

    def test_components_below_the_threshold_merge_and_leave_no_boundary_gate(self):
        from clusters import natural_partition
        result = natural_partition(self.graph, rivers_doc(), min_km=200.0)
        self.assertEqual(set(result["labels"].tolist()), {0})
        self.assertEqual(result["extra_gates"], [])

    def test_without_main_rivers_nothing_is_cut(self):
        from clusters import natural_partition
        doc = rivers_doc(crossings=[])
        doc["features"] = []
        result = natural_partition(self.graph, doc, min_km=1.0)
        self.assertEqual(set(result["labels"].tolist()), {0})


class NaturalFromOsmTests(unittest.TestCase):
    """The tiny OSM file of test_natural_barriers: an A bridge, a residential bridge and a ferry over a main river."""

    def test_two_banks_two_clusters_and_every_crossing_gives_gates(self):
        from natural_barriers import run as survey_run
        from clusters import build_graph, natural_partition
        from test_natural_barriers import A_WAYS, NODES, make_fixture
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            pbf, edges, boundary = make_fixture(root)
            rivers_out = root / "main_rivers.geojson"
            survey_run(pbf, edges, boundary, root / "report.json", build_json=None, land_path=root / "vietnam_land.geojson",
                       rivers_out=rivers_out)
            doc = json.loads(rivers_out.read_text(encoding="utf-8"))
        at = lambda n: (NODES[n][1], NODES[n][0])  # noqa: E731
        graph = build_graph([line(way_id, [at(n) for n in refs]) for way_id, refs, _ in A_WAYS])
        node = {tuple(np.round(p, 6)): i for i, p in enumerate(graph.lonlat)}
        result = natural_partition(graph, doc, min_km=5.0)
        labels = result["labels"]
        west, east = int(labels[node[at(11)]]), int(labels[node[at(21)]])
        self.assertNotEqual(west, east)
        self.assertEqual(len(set(labels.tolist())), 2)
        # The short "Suối Ngắn" divides nothing: its A bridge stays inside the east cluster.
        self.assertEqual(int(labels[node[at(41)]]), east)
        by_way = {}
        for gate in result["extra_gates"]:
            by_way.setdefault(gate["way_id"], set()).add((tuple(gate["at"]), tuple(gate["clusters"])))
        self.assertEqual(by_way, {
            31: {(at(14), (west, east)), (at(24), (east, west))},
            32: {(at(15), (west, east)), (at(25), (east, west))}})


class ClusterFileTests(unittest.TestCase):
    def test_the_cluster_file_holds_exactly_the_province_and_natural_partitions(self):
        from clusters import run
        provinces = [province("VN-W", "Tỉnh Tây", [square(104.99, 20.99, 105.02, 21.02)]),
                     province("VN-E", "Tỉnh Đông", [square(105.99, 20.99, 106.02, 21.02)])]
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": square_features()}),
                                                    encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
            (root / "main_rivers.geojson").write_text(json.dumps(rivers_doc()), encoding="utf-8")
            (root / "vietnam_provinces.geojson").write_text(json.dumps({"type": "FeatureCollection", "pbf_sha256": "map-a",
                "policy_version": 6, "failed": [], "features": provinces}, ensure_ascii=False), encoding="utf-8")
            result = run(root / "big_roads.geojson", root / "clusters.json", natural_min_km=1.0)
            doc = json.loads((root / "clusters.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["method"], "regions")
        self.assertEqual([p["method"] for p in doc["partitions"]], ["province", "natural"])
        by_province, natural = doc["partitions"]
        common = {"k", "method", "ways", "node_labels", "cuts", "boundary", "stats"}
        self.assertEqual(set(by_province), common | {"names"})
        self.assertEqual(set(natural), common | {"extra_gates", "rivers", "natural"})
        self.assertEqual(natural["k"], 2)
        self.assertEqual(sorted(map(tuple, natural["boundary"])), [W0, E0])
        self.assertEqual(len(natural["extra_gates"]), 2)
        self.assertEqual(natural["rivers"], ["Sông Thử"])
        self.assertEqual(natural["stats"]["unassigned_nodes"], 0)
        self.assertIn("natural", [p.get("method") for p in result["partitions"]])

    def test_main_rivers_from_another_map_are_skipped(self):
        from clusters import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": square_features()}),
                                                    encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")
            (root / "main_rivers.geojson").write_text(json.dumps(rivers_doc(sha="map-b")), encoding="utf-8")
            result = run(root / "big_roads.geojson", root / "clusters.json")
        self.assertNotIn("natural", [p.get("method") for p in result["partitions"]])


if __name__ == "__main__":
    unittest.main()
