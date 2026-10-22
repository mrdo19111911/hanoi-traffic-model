"""Offline tests for the province and natural regions of the big-road (A) network (model/clusters.py), on tiny graphs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

# Two dense squares of junctions 1 km across (A at lon 105, B at lon 106), joined only by a ~104 km edge A0-B0.
A0, A1, A2, A3 = (105.0, 21.0), (105.01, 21.0), (105.01, 21.01), (105.0, 21.01)
B0, B1, B2, B3 = (106.0, 21.0), (106.01, 21.0), (106.01, 21.01), (106.0, 21.01)
A_WAYS, B_WAYS = [1, 2, 3, 4, 5], [11, 12, 13, 14]


def line(way_id, coords, group="quoclo", **props):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(c) for c in coords]},
            "properties": {"way_id": way_id, "group": group, "fragment": False, "isolated": False, **props}}


def features():
    return [line(1, [A1, A2]), line(2, [A2, A3]), line(3, [A3, A0]), line(4, [A0, A2]), line(5, [A1, A3]),
            line(11, [B2, B3]), line(12, [B3, B0]), line(13, [B0, B2]), line(14, [B1, B3]),
            # Way 77 runs 1 km inside A, along the long edge, then 2.2 km inside B: the longer side is B.
            line(77, [A1, A0, B0, B1, B2], group="caotoc"),
            line(90, [A2, (105.02, 21.02)], fragment=True), line(91, [A3, (104.99, 21.02)], isolated=True),
            # Off the main component, so left out as well.
            line(92, [(108.0, 15.0), (108.01, 15.0)])]


def square(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]


def province(iso, name, *polygons):
    return {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": [list(p) for p in polygons]},
            "properties": {"iso": iso, "name": name, "relation_id": 1, "area_km2": 1.0, "points": 5}}


TWO_PROVINCES = [province("VN-A", "Tỉnh A", [square(104.99, 20.99, 105.02, 21.02)]),
                 province("VN-B", "Tỉnh B", [square(105.99, 20.99, 106.02, 21.02)])]


def two_regions(graph):
    from clusters import assign_provinces, export
    return export(graph, {"pbf_sha256": "map-a", "policy_version": 6}, assign_provinces(graph.lonlat, TWO_PROVINCES))


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from clusters import build_graph
        cls.graph = build_graph(features())
        cls.result = two_regions(cls.graph)
        cls.k2, = cls.result["partitions"]

    def test_graph_keeps_the_main_component_without_fragments(self):
        self.assertEqual(self.graph.n, 8)

    def test_two_clusters_cut_exactly_the_long_edge(self):
        stats = self.k2["stats"]
        self.assertEqual(self.k2["k"], 2)
        self.assertEqual(stats["cut_edges"], 1)
        self.assertEqual(stats["boundary_nodes"], 2)
        self.assertEqual({tuple(p) for p in self.k2["boundary"]}, {A0, B0})
        cut, = self.k2["cuts"]
        self.assertIn([tuple(p) for p in cut], ([A0, B0], [B0, A0]))
        self.assertAlmostEqual(stats["cut_km"], 103.9, delta=0.5)
        self.assertEqual(stats["contiguous"], 2)
        self.assertEqual(self.k2["method"], "province")
        self.assertEqual(set(stats), {"cut_edges", "cut_km", "cut_pct", "boundary_nodes", "contiguous", "conductance_mean",
                                      "km_min", "km_median", "km_max", "unassigned_nodes"})

    def test_ways_take_the_cluster_holding_most_of_their_length(self):
        ways = self.k2["ways"]
        a, b = ways["1"], ways["11"]
        self.assertNotEqual(a, b)
        self.assertEqual({ways[str(w)] for w in A_WAYS}, {a})
        self.assertEqual({ways[str(w)] for w in B_WAYS}, {b})
        self.assertEqual(ways["77"], b)

    def test_fragments_isolated_and_off_main_ways_are_absent(self):
        self.assertEqual(set(self.k2["ways"]), {str(w) for w in A_WAYS + B_WAYS + [77]})

    def test_runs_repeat_exactly(self):
        from clusters import build_graph
        self.assertEqual(two_regions(build_graph(features()))["partitions"], self.result["partitions"])

    def test_file_header(self):
        self.assertEqual((self.result["method"], self.result["pbf_sha256"], self.result["policy_version"]),
                         ("regions", "map-a", 6))
        self.assertTrue(self.result["created_at"])


class ProvinceAssignmentTests(unittest.TestCase):
    # VN-01 has a hole that VN-02 fills exactly; VN-03 overlaps VN-01's east edge. Listed out of iso order.
    PROVINCES = [province("VN-03", "Tỉnh Ba", [square(105.08, 21.0, 105.2, 21.1)]),
                 province("VN-01", "Tỉnh Một", [square(105.0, 21.0, 105.1, 21.1), square(105.04, 21.04, 105.06, 21.06)]),
                 province("VN-02", "Tỉnh Hai", [square(105.04, 21.04, 105.06, 21.06)])]

    def assign(self, points):
        import numpy as np
        from clusters import assign_provinces
        return assign_provinces(np.array(points, dtype=float), self.PROVINCES)

    def test_labels_follow_iso_order(self):
        labels, names = self.assign([(105.02, 21.02), (105.15, 21.05)])
        self.assertEqual(names, ["Tỉnh Một", "Tỉnh Hai", "Tỉnh Ba"])
        self.assertEqual(labels.tolist(), [0, 2])

    def test_node_in_an_inner_hole_is_not_in_that_province(self):
        labels, _ = self.assign([(105.05, 21.05)])
        self.assertEqual(labels.tolist(), [1])

    def test_node_where_provinces_overlap_takes_the_smaller_iso(self):
        labels, _ = self.assign([(105.09, 21.05)])
        self.assertEqual(labels.tolist(), [0])

    def test_nodes_outside_are_snapped_within_2_km_else_unassigned(self):
        # About 1.0 km and 3.0 km south of VN-01's southern edge.
        labels, _ = self.assign([(105.02, 21.0 - 0.009), (105.02, 21.0 - 0.027)])
        self.assertEqual(labels.tolist(), [0, -1])


class ProvincePartitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from clusters import assign_provinces, build_graph, export
        # A spur from B2 54 km north ends outside every province.
        cls.graph = build_graph(features() + [line(95, [B2, (106.01, 21.5)])])
        cls.result = export(cls.graph, {"pbf_sha256": "map-a", "policy_version": 6},
                            assign_provinces(cls.graph.lonlat, TWO_PROVINCES[::-1]))
        cls.by_province, = cls.result["partitions"]

    def test_province_partition_has_names(self):
        self.assertEqual(self.by_province["method"], "province")
        self.assertEqual(self.by_province["k"], 2)
        self.assertEqual(self.by_province["names"], {"0": "Tỉnh A", "1": "Tỉnh B"})

    def test_road_between_two_provinces_is_the_one_cut(self):
        stats = self.by_province["stats"]
        self.assertEqual(stats["cut_edges"], 1)
        self.assertEqual({tuple(p) for p in self.by_province["boundary"]}, {A0, B0})
        self.assertEqual(len(self.by_province["cuts"]), 1)

    def test_edge_to_an_unassigned_node_is_no_cut(self):
        self.assertEqual(self.by_province["stats"]["unassigned_nodes"], 1)
        ways = self.by_province["ways"]
        self.assertEqual(({ways[str(w)] for w in A_WAYS}, {ways[str(w)] for w in B_WAYS + [77, 95]}), ({0}, {1}))


    def test_every_junction_has_a_label_and_the_boundary_is_exactly_the_cut_ends(self):
        item = self.by_province
        labels = {(lon, lat): c for lon, lat, c in item["node_labels"]}
        self.assertEqual(len(item["node_labels"]), self.graph.n)
        self.assertEqual(set(labels), {(round(float(x), 6), round(float(y), 6)) for x, y in self.graph.lonlat})
        ends = set()
        for a, b in zip(self.graph.a.tolist(), self.graph.b.tolist()):
            pa, pb = (tuple(round(float(v), 6) for v in self.graph.lonlat[n]) for n in (a, b))
            if labels[pa] >= 0 and labels[pb] >= 0 and labels[pa] != labels[pb]:
                ends |= {pa, pb}
        self.assertEqual({tuple(p) for p in item["boundary"]}, ends)

    def test_unassigned_junction_is_labelled_minus_one_and_is_no_gate(self):
        labels = {(lon, lat): c for lon, lat, c in self.by_province["node_labels"]}
        self.assertEqual(labels[(106.01, 21.5)], -1)
        self.assertNotIn([106.01, 21.5], self.by_province["boundary"])
        self.assertNotIn(list(B2), self.by_province["boundary"])


class FerryLandingTests(unittest.TestCase):
    """QL ends at a landing (degree 2 with the ferry), so without care the landing would be contracted away."""

    L1, L2, L3 = (105.03, 21.0), (105.97, 21.0), (105.005, 21.005)

    def build(self):
        from clusters import assign_provinces, build_graph, export
        feats = features() + [line(60, [A1, self.L1]), line(61, [self.L1, self.L2], bridge_kind="ferry"), line(62, [self.L2, B0]),
                              # A ferry inside province A: from A3's spur end across to A2's.
                              line(63, [A3, self.L3]), line(64, [self.L3, A2], bridge_kind="ferry")]
        graph = build_graph(feats)
        provinces = [province("VN-A", "Tỉnh A", [square(104.99, 20.99, 105.5, 21.02)]),
                     province("VN-B", "Tỉnh B", [square(105.5, 20.99, 106.02, 21.02)])]
        result = export(graph, {"pbf_sha256": "map-a", "policy_version": 6}, assign_provinces(graph.lonlat, provinces))
        return graph, result["partitions"][-1]

    def test_ferry_landings_stay_junctions(self):
        graph, _ = self.build()
        nodes = {tuple(round(float(v), 6) for v in p) for p in graph.lonlat}
        self.assertTrue({self.L1, self.L2, self.L3} <= nodes)

    def test_ferry_between_clusters_makes_both_landings_gates_and_one_inside_makes_none(self):
        _, by_province = self.build()
        boundary = {tuple(p) for p in by_province["boundary"]}
        self.assertTrue({self.L1, self.L2} <= boundary)
        self.assertNotIn(self.L3, boundary)


class ClusterFileTests(unittest.TestCase):
    def test_run_writes_the_cluster_file_from_the_big_road_build(self):
        from clusters import run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features()}), encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-b", "policy_version": 6}), encoding="utf-8")
            run(root / "big_roads.geojson", root / "big_road_clusters.json")
            data = json.loads((root / "big_road_clusters.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["big_road_clusters.json", "big_roads.geojson", "big_roads.meta.json"])
        self.assertEqual((data["method"], data["pbf_sha256"], data["policy_version"]), ("regions", "map-b", 6))
        self.assertEqual(data["partitions"], [])

    def test_run_adds_provinces_from_the_same_map_only(self):
        from clusters import run
        provinces = [province("VN-A", "Tỉnh A", [square(104.99, 20.99, 105.02, 21.02)]),
                     province("VN-B", "Tỉnh B", [square(105.99, 20.99, 106.02, 21.02)])]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "big_roads.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features()}), encoding="utf-8")
            (root / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-b", "policy_version": 6}), encoding="utf-8")
            methods = []
            for sha in ("map-b", "map-old"):
                (root / "vietnam_provinces.geojson").write_text(json.dumps({"type": "FeatureCollection", "pbf_sha256": sha,
                    "policy_version": 6, "failed": [], "features": provinces}, ensure_ascii=False), encoding="utf-8")
                result = run(root / "big_roads.geojson", root / "big_road_clusters.json")
                methods.append([p["method"] for p in result["partitions"]])
        self.assertEqual(methods, [["province"], []])

    def test_failed_write_keeps_the_old_file_and_no_temp(self):
        from clusters import write_json_atomic
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "big_road_clusters.json"
            write_json_atomic(path, {"old": True})
            with self.assertRaises(TypeError):
                write_json_atomic(path, {"bad": object()})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["big_road_clusters.json"])


if __name__ == "__main__":
    unittest.main()
