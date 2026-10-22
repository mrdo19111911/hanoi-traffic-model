"""Offline tests for model/osrm_mld_boundary.py on tiny OSRM-style tar files with the real block names and dtypes."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = b"OSRP\x1a\n\x00\x02\x01\x08\xfa"
# 8 edge-based nodes: geometry g has the forward node 2g and the reverse node 2g + 1. Level 1 puts geometry g in
# cell g; level 2 joins cells 0, 1 (parent 0) and 2, 3 (parent 1). Graph edges join 0-2-4-6 and 1-3-5-7.
N = 8
LEVEL1 = np.array([0, 0, 1, 1, 2, 2, 3, 3])
LEVEL2 = LEVEL1 // 2
EDGES = [(0, 2), (2, 4), (4, 6), (1, 3), (3, 5), (5, 7)]
# Geometry g runs from nbg node 2g to 2g + 1, 1 km apart along lat 21.
COORDS = [(105.0 + 0.0096 * k, 21.0) for k in range(8)]


def _block(tar, name, payload, count):
    for member, data in ((name, payload), (name + ".meta", np.array([count], dtype=np.uint64).tobytes())):
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def write_tar(path, blocks):
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("osrm_fingerprint.meta")
        info.size = len(FINGERPRINT)
        tar.addfile(info, io.BytesIO(FINGERPRINT))
        for name, (payload, count) in blocks.items():
            _block(tar, name, payload, count)


def level_data(offsets, masks, children_offsets):
    """The 272-byte LevelData: num_level u32, lidx_to_offset u8[16], masks u64[15] at 24, bit_to_level u8[64] at 144,
    lidx_to_children_offsets u32[15] at 208."""
    raw = bytearray(272)
    raw[0:4] = np.uint32(len(offsets)).tobytes()
    raw[4:4 + len(offsets)] = bytes(offsets)
    raw[24:24 + 8 * len(masks)] = np.array(masks, dtype=np.uint64).tobytes()
    raw[208:208 + 4 * len(children_offsets)] = np.array(children_offsets, dtype=np.uint32).tobytes()
    return bytes(raw)


def cells_blocks(per_level):
    """per_level: [[(sources, destinations) per cell] per level] -> cellstorage blocks."""
    rows, sources, destinations, offsets = [], [], [], [0]
    for cells in per_level:
        for src, dst in cells:
            rows.append((0, len(sources) if src else 2 ** 64 - 1, len(destinations) if dst else 2 ** 64 - 1, len(src), len(dst)))
            sources += src
            destinations += dst
        offsets.append(len(rows))
    dtype = np.dtype([("value_offset", "<u8"), ("source_boundary_offset", "<u8"), ("destination_boundary_offset", "<u8"),
                      ("num_source_nodes", "<u4"), ("num_destination_nodes", "<u4")])
    table = np.array(rows, dtype=dtype)
    return {"/mld/cellstorage/source_boundary": (np.array(sources, dtype=np.uint32).tobytes(), len(sources)),
            "/mld/cellstorage/destination_boundary": (np.array(destinations, dtype=np.uint32).tobytes(), len(destinations)),
            "/mld/cellstorage/cells": (table.tobytes(), len(table)),
            "/mld/cellstorage/level_to_cell_offset": (np.array(offsets, dtype=np.uint64).tobytes(), len(offsets))}


def make_osrm(root, coords=COORDS, drop=None):
    base = root / "map.osrm"
    # Like the real file, the partition has one entry more than there are edge-based nodes.
    partition = np.append(LEVEL1 | (LEVEL2 << 2), 0).astype(np.uint64)
    blocks = {"/mld/multilevelpartition/level_data": (level_data([0, 2, 3], [0b11, 0b100], [0, 0]), 1),
              "/mld/multilevelpartition/partition": (partition.tobytes(), len(partition)),
              "/mld/multilevelpartition/cell_to_children": (np.array([0, 2, 4], dtype=np.uint32).tobytes(), 3)}
    files = {"partition": blocks}
    # Level 1: every node is a boundary node; node 0 is a source and a destination of cell 0 (counted once).
    level1 = [([0], [0, 1]), ([2, 3], [2, 3]), ([4, 5], [4, 5]), ([6, 7], [6, 7])]
    level2 = [([2], [2, 3]), ([4], [4, 5])]
    files["cells"] = cells_blocks([level1, level2])
    nodes = np.zeros((N, 3), dtype=np.uint32)
    for node in range(N):
        nodes[node, 0] = (node // 2) | ((1 - node % 2) << 31)
    files["ebg_nodes"] = {"/common/ebg_node_data/nodes": (nodes.tobytes(), N)}
    index = np.array([0, 2, 4, 6, 8], dtype=np.uint32)
    files["geometry"] = {"/common/segment_data/index": (index.tobytes(), len(index)),
                         "/common/segment_data/nodes": (np.arange(8, dtype=np.uint32).tobytes(), 8)}
    fixed = np.array([(round(lon * 1e6), round(lat * 1e6)) for lon, lat in coords], dtype=np.int32)
    files["nbg_nodes"] = {"/common/nbn_data/coordinates": (fixed.tobytes(), len(fixed))}
    adjacency = [[] for _ in range(N)]
    for a, b in EDGES:
        adjacency[a].append(b)
        adjacency[b].append(a)
    first = np.cumsum([0] + [len(x) for x in adjacency]).astype(np.uint32)
    edge_array = np.array([(t, 0) for x in adjacency for t in x], dtype=np.uint32)
    files["mldgr"] = {"/mld/multilevelgraph/node_array": (first.tobytes(), len(first)),
                      "/mld/multilevelgraph/edge_array": (edge_array.tobytes(), len(edge_array))}
    for ext, blocks in files.items():
        if drop and drop[0] == ext:
            blocks = {k: v for k, v in blocks.items() if k != drop[1]}
        write_tar(Path(f"{base}.{ext}"), blocks)
    return base


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from osrm_mld_boundary import read_cells, read_partition
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = make_osrm(Path(cls.temp.name))
        cls.partition = read_partition(f"{cls.base}.partition", N)
        cls.cells = read_cells(f"{cls.base}.cells")

    @classmethod
    def tearDownClass(cls):
        cls.partition = cls.cells = None
        cls.temp.cleanup()

    def test_levels_cells_and_cell_of_each_node(self):
        from osrm_mld_boundary import cell_ids
        self.assertEqual(self.partition["levels"], 2)
        self.assertEqual(self.partition["cell_counts"], {1: 4, 2: 2})
        np.testing.assert_array_equal(cell_ids(self.partition, 1), LEVEL1)
        np.testing.assert_array_equal(cell_ids(self.partition, 2), LEVEL2)
        self.assertEqual(self.partition["children"][2], [[0, 1], [2, 3]])

    def test_boundary_is_the_union_of_sources_and_destinations(self):
        from osrm_mld_boundary import level_boundary
        one = level_boundary(self.cells, 1)
        self.assertEqual((one["sources"], one["destinations"], one["boundary_ebg"]), (7, 8, 8))
        two = level_boundary(self.cells, 2)
        self.assertEqual((two["sources"], two["destinations"], two["boundary_ebg"]), (2, 4, 4))
        np.testing.assert_array_equal(two["nodes"], [2, 3, 4, 5])
        self.assertEqual(two["per_cell"], {"min": 2, "median": 2.0, "max": 2})

    def test_a_missing_block_is_named(self):
        from osrm_mld_boundary import read_cells
        with tempfile.TemporaryDirectory() as name:
            base = make_osrm(Path(name), drop=("cells", "/mld/cellstorage/destination_boundary"))
            with self.assertRaisesRegex(ValueError, "/mld/cellstorage/destination_boundary"):
                read_cells(f"{base}.cells")


class PointTests(unittest.TestCase):
    def test_two_directions_of_a_segment_are_one_point(self):
        from osrm_mld_boundary import boundary_points, read_ebg_points, read_graph
        with tempfile.TemporaryDirectory() as name:
            base = make_osrm(Path(name))
            geometry, points = read_ebg_points(base, N)
            graph = read_graph(f"{base}.mldgr")
            result = boundary_points(np.array([2, 3, 4, 5]), LEVEL2, geometry, points, graph, merge_m=100)
            geometry = points = graph = None
        # Nodes 2, 3 are geometry 1 and nodes 4, 5 geometry 2: two points, both between parents 0 and 1.
        self.assertEqual((result["items"], result["boundary_points"]), (2, 2))
        self.assertEqual(result["pairs"], {"0-1": 2})

    def test_points_under_100_m_apart_for_the_same_pair_merge(self):
        from osrm_mld_boundary import merge_points
        step = 50 / (111320 * 0.9336)
        coords = np.array([(105.0, 21.0), (105.0 + step, 21.0), (105.0 + 4 * step, 21.0), (105.0 + step, 21.0)])
        pairs = np.array([(0, 1), (0, 1), (0, 1), (1, 2)])
        groups = merge_points(coords, pairs, metres=100)
        self.assertEqual(groups[0], groups[1])
        self.assertNotEqual(groups[1], groups[2])
        self.assertNotEqual(groups[1], groups[3])
        self.assertEqual(len(set(groups.tolist())), 3)


class LevelChoiceTests(unittest.TestCase):
    def test_the_level_nearest_the_target_is_taken_when_close(self):
        from osrm_mld_boundary import choose_level
        self.assertEqual(choose_level({1: 52565, 2: 3788, 3: 24, 4: 8}, 22), {"level": 3, "cells": 24, "tree_cut": False})
        self.assertEqual(choose_level({1: 52565, 2: 3788, 3: 235, 4: 8}, 22)["tree_cut"], True)

    def test_a_tree_cut_splits_parents_and_drops_nodes_between_siblings(self):
        from osrm_mld_boundary import frontier_boundary, read_cells, read_graph, read_partition, tree_frontier
        with tempfile.TemporaryDirectory() as name:
            base = make_osrm(Path(name))
            partition = read_partition(f"{base}.partition", N)
            labels, info = tree_frontier(partition, target=3)
            self.assertEqual(info, {"coarse_level": 2, "fine_level": 1, "cells": 3, "split": [0]})
            # Parent 0 is split into cells 0 and 1; parent 1 stays whole.
            self.assertEqual(len({labels[0], labels[2], labels[4]}), 3)
            self.assertEqual(labels[4], labels[6])
            boundary = frontier_boundary(read_cells(f"{base}.cells"), info["fine_level"], labels, read_graph(f"{base}.mldgr"))
            partition = None
        # Nodes 6 and 7 lie only between cells 2 and 3, siblings under parent 1: no longer boundary.
        np.testing.assert_array_equal(boundary, [0, 1, 2, 3, 4, 5])


class RunTests(unittest.TestCase):
    def write_inputs(self, root, build_sha="map-a", edges_sha="map-a"):
        from build_big_roads import write_edges
        base = make_osrm(root)
        (root / "build.json").write_text(json.dumps({"input": {"osrm_version": "v26.10.0", "pbf_sha256": build_sha}}), encoding="utf-8")
        write_edges(root / "edges.sqlite", [], {"pbf_sha256": edges_sha, "policy_version": "6"})
        return base

    def test_run_writes_the_report_and_the_cells_map(self):
        from osrm_mld_boundary import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            base = self.write_inputs(root)
            report = run(base, root / "build.json", root / "edges.sqlite", root / "out.json", root / "cells.geojson",
                         target=3, compare_cells=2, router=None)
            saved = json.loads((root / "out.json").read_text(encoding="utf-8"))
            cells = json.loads((root / "cells.geojson").read_text(encoding="utf-8"))
        self.assertEqual(saved["metadata"]["pbf_sha256"], "map-a")
        self.assertEqual(saved["metadata"]["osrm_version"], "v26.10.0")
        self.assertEqual([lvl["cells"] for lvl in saved["levels"]], [4, 2])
        self.assertEqual(report["comparison"]["target"]["cells"], 3)
        self.assertEqual(report["comparison"]["target"]["boundary_ebg"], 6)
        self.assertEqual(report["comparison"]["cells_2"]["boundary_ebg"], 4)
        self.assertEqual(report["comparison"]["reference_G"], {"natural": 350})
        self.assertEqual(len(cells["features"]), 3)  # geometries 0, 1, 2 at the 3-cell cut
        self.assertTrue(all("cell" in f["properties"] for f in cells["features"]))

    def test_a_map_mismatch_stops(self):
        from osrm_mld_boundary import run
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            base = self.write_inputs(root, edges_sha="map-b")
            with self.assertRaisesRegex(ValueError, "another map"):
                run(base, root / "build.json", root / "edges.sqlite", root / "out.json", root / "cells.geojson", router=None)
            self.assertFalse((root / "out.json").exists())


class RealFileSmokeTest(unittest.TestCase):
    def test_partition_covers_the_edge_based_nodes(self):
        from osrm_mld_boundary import block_count
        base = ROOT / "osrm/data/vietnam-latest.osrm"
        if not Path(f"{base}.partition").is_file() or not Path(f"{base}.ebg_nodes").is_file():
            self.skipTest("OSRM files are absent")
        partition = block_count(f"{base}.partition", "/mld/multilevelpartition/partition")
        nodes = block_count(f"{base}.ebg_nodes", "/common/ebg_node_data/nodes")
        # OSRM sizes the partition like the graph's node array: one entry per edge-based node plus a sentinel.
        self.assertIn(partition - nodes, (0, 1))


if __name__ == "__main__":
    unittest.main()


class SingleTableTests(unittest.TestCase):
    def test_single_call_times_the_whole_table_in_one_request(self):
        import osrm_mld_boundary as mb

        class Router:
            def __init__(self):
                self.calls = 0

            def nearest(self, point):
                return {"location": list(point)}

            def durations(self, sources, destinations):
                self.calls += 1
                return [[1.0] * len(destinations) for _ in sources]

        router = Router()
        points = [(105.0 + i * 0.01, 21.0) for i in range(7)]
        result = mb.time_table(router, points, block=mb.single_block(len(points)))
        self.assertEqual(router.calls, 1)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["block"], 7)


class SquareTableRequestTests(unittest.TestCase):
    def test_square_table_sends_each_coordinate_once(self):
        from gate_matrix_experiment import TableRouter
        router = TableRouter.__new__(TableRouter)
        urls = []
        router._get = lambda url: urls.append(url) or {"durations": [[0.0, 1.0], [1.0, 0.0]]}
        points = [(105.0, 21.0), (105.1, 21.1)]
        router.durations(points, points)
        self.assertEqual(urls[0].split("?")[0].count(";"), 1)
        self.assertNotIn("sources=", urls[0])
        router.durations(points, points[:1])
        self.assertIn("sources=0;1", urls[1])
