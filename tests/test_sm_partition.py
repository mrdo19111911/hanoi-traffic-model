"""Offline tests for speedmatrix.partition on tiny grid maps built with tests/sm_osrm_fixture.make_graphs."""
import contextlib
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import grid, make_graphs
from speedmatrix import partition
from speedmatrix.graph import save_graph
from speedmatrix.partition import (MAX_CELLS, PartitionConfig, PartitionError, Previous, build_partition,
                                   cell_graph, check_partition, import_seed, load_partition, load_previous, load_seed,
                                   main, save_partition)

N = 12                     # grid(12): node id r * 12 + c + 1, node index id - 1; columns 0-5 west, 6-11 east
MID = 105.8055
EMPTY = {"type": "FeatureCollection", "features": []}


def _box(region, x0, x1):
    ring = [[x0, 20.0], [x1, 20.0], [x1, 22.0], [x0, 22.0], [x0, 20.0]]
    return {"type": "Feature", "properties": {"region": region}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


POLY = {"type": "FeatureCollection", "features": [_box(0, 105.0, MID), _box(1, MID, 106.5)]}
CFG = PartitionConfig(split_nodes=(), l2_nodes=40)
CFG3 = PartitionConfig(split_nodes=(12,), l2_nodes=40)


def node(r, c):
    return r * N + c


def previous_of(p, ng):
    return Previous(ng.osm.copy(), p.codes.copy(), p.l2_hash, "test")


def _chain(start_id, attach, points, highway="residential", way_id=None):
    """coords and one way for new nodes ``start_id..`` at ``points`` hanging off grid node id ``attach``."""
    coords = {start_id + j: xy for j, xy in enumerate(points)}
    return coords, (way_id or start_id, [attach] + list(coords), {"highway": highway})


class PartitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = cls.root = Path(cls.temp.name)
        coords, ways = grid(N)
        cls.coords, cls.ways = coords, ways
        _, cls.g, cls.ng = make_graphs(root / "base", coords, ways)
        cls.p = build_partition(cls.g, cls.ng, None, POLY, CFG, 1)
        cls.p3 = build_partition(cls.g, cls.ng, None, POLY, CFG3, 1)
        cls.prev = previous_of(cls.p3, cls.ng)
        cls.n_l2 = len(cls.p3.cells[1])

        # way 1 (row 0) split into ways 91 and 92 over the same nodes
        split = [w for w in ways if w[0] != 1] + [(91, list(range(1, 7)), ways[0][2]),
                                                  (92, list(range(6, 13)), ways[0][2])]
        _, cls.g_split, cls.ng_split = make_graphs(root / "split", coords, split)

        # a 3-node spur east of grid node (5, 11)
        c, w = _chain(1001, node(5, 11) + 1, [(105.812 + 0.001 * j, 21.005) for j in range(3)])
        _, cls.g_spur, cls.ng_spur = make_graphs(root / "spur", {**coords, **c}, ways + [w])
        cls.spur, cls.attach = [N * N, N * N + 1, N * N + 2], node(5, 11)

        # an island of 3 nodes in the east half, linked to nothing
        island = {2001: (105.815, 21.02), 2002: (105.816, 21.02), 2003: (105.817, 21.02)}
        _, cls.g_island, cls.ng_island = make_graphs(root / "island", {**coords, **island},
                                                     ways + [(300, [2001, 2002, 2003], {"highway": "residential"})])

        # removed bridge: edge (0,0)-(0,1) gone; L2 X = column 0 + row 0 columns 1-3, Y = columns 1-3 rows 1-11,
        # Z = columns 4-5, W = east half. Without the bridge, X's row-0 part has 3 edges to Y and 1 to Z.
        nob = [w if w[0] != 1 else (1, w[1][1:], w[2]) for w in ways]
        _, cls.g_nobridge, cls.ng_nobridge = make_graphs(root / "nobridge", coords, nob)
        codes = np.zeros((N * N, 2), dtype=np.int64)
        for r in range(N):
            for col in range(N):
                i = node(r, col)
                codes[i, 0] = 0 if col < 6 else 1
                codes[i, 1] = (20 if col >= 6 else 12 if col >= 4 else 10 if col == 0 or r == 0 else 11)
        cls.prev_x_spans_bridge = Previous(cls.ng_nobridge.osm.copy(), codes, None, "test")
        cls.half2, cls.code_y = [node(0, 1), node(0, 2), node(0, 3)], 11

        # previous L1 cell 0 = west half + a detached 5-node strip (row 11, columns 7-11) inside the east half
        cls.piece = [node(11, col) for col in range(7, 12)]
        codes = cls.p.codes.copy()
        codes[cls.piece, 0] = 0
        cls.prev_detached = Previous(cls.ng.osm.copy(), codes, None, "test")

        # a ferry from west boundary node (6, 0) (3 road edges) over inner node 3002 to landing 3001, which has one
        # road edge to east boundary node (6, 11)
        fc = {3001: (105.814, 21.006), 3002: (105.806, 21.0065)}
        fw = [(400, [node(6, 11) + 1, 3001], {"highway": "residential"}),
              (401, [node(6, 0) + 1, 3002, 3001], {"route": "ferry"})]
        _, cls.g_ferry, cls.ng_ferry = make_graphs(root / "ferry", {**coords, **fc}, ways + fw)
        idx = {int(o): i for i, o in enumerate(cls.ng_ferry.osm)}
        cls.ferry_inner, cls.landing_w, cls.landing_e = idx[3002], idx[node(6, 0) + 1], idx[3001]

        # previous L2: column 6 (east) takes the L2 code of west node (0, 5), so that code spans both L1 cells
        codes = cls.p.codes.copy()
        codes[[node(r, 6) for r in range(N)], 1] = codes[node(0, 5), 1]
        cls.prev_l2_across_l1 = Previous(cls.ng.osm.copy(), codes, None, "test")

        # two edits: a 2-node spur on the L2 cell of east node (5, 11), a chain of +40% on the cell of west (5, 0)
        a, b = node(5, 11), node(5, 0)
        size = lambda i: int((cls.p3.codes[:, 1] == cls.p3.codes[i, 1]).sum())  # noqa: E731
        m = math.ceil(0.4 * size(b))
        assert size(a) >= 10 and m / size(b) > 0.2, (size(a), size(b))
        c1, w1 = _chain(4001, a + 1, [(105.812 + 0.001 * j, 21.005) for j in range(2)])
        c2, w2 = _chain(4101, b + 1, [(105.799 - 0.001 * j, 21.005) for j in range(m)])
        _, cls.g_two_changes, cls.ng_two_changes = make_graphs(root / "two", {**coords, **c1, **c2}, ways + [w1, w2])

        # a motorway run 5001-5003 north of the grid from (11, 3) to (11, 5); previous L2: X = columns 0-2 (10),
        # Y = columns 3-5 (11), W = east (20), and the run labelled X although it touches only Y
        mc = {5001: (105.8035, 21.012), 5002: (105.8042, 21.0125), 5003: (105.805, 21.012)}
        run = [node(11, 3) + 1, 5001, 5002, 5003, node(11, 5) + 1]
        for name, hw in (("motorway", "motorway"), ("residential_run", "residential")):
            _, g, ng = make_graphs(root / name, {**coords, **mc}, ways + [(600, run, {"highway": hw})])
            codes = np.zeros((ng.n, 2), dtype=np.int64)
            col = np.round((ng.lon - 105.8) / 0.001).astype(int)
            codes[:, 0] = (ng.lon > MID).astype(int)
            codes[:, 1] = np.where(col >= 6, 20, np.where(col >= 3, 11, 10))
            codes[ng.osm >= 5001, 1] = 10
            setattr(cls, f"g_{name}", g)
            setattr(cls, f"ng_{name}", ng)
            setattr(cls, f"prev_{name}", Previous(ng.osm.copy(), codes, None, "test"))
        cls.run_nodes = [int(i) for i in np.flatnonzero(cls.ng_motorway.osm >= 5001)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.rel = Path(tempfile.mkdtemp(dir=self.root))

    # ---- the brief's tests -------------------------------------------------------------------------------------------

    def test_carry_over_ignores_way_ids(self):                         # Review Focus 1
        p1 = build_partition(self.g, self.ng, None, POLY, CFG, 1)
        prev = previous_of(p1, self.ng)
        p2 = build_partition(self.g_split, self.ng_split, prev, POLY, CFG, 1)   # way 1 split into ways 91 and 92
        np.testing.assert_array_equal(p2.codes, p1.codes)
        self.assertEqual(p2.info["resplit"], {"unchanged": len(p1.cells[1]), "warm": 0, "fresh": 0})

    def test_saved_hashes_survive_the_json_round_trip(self):
        p1 = build_partition(self.g, self.ng, None, POLY, CFG3, 1)
        save_partition(p1, self.rel)
        prev = load_previous(self.rel)
        self.assertTrue(all(isinstance(c, int) for c in prev.l2_hash))
        p2 = build_partition(self.g, self.ng, prev, POLY, CFG3, 1)
        self.assertEqual(p2.info["resplit"], {"unchanged": len(p1.cells[1]), "warm": 0, "fresh": 0})
        np.testing.assert_array_equal(p2.codes, p1.codes)

    def test_seed_without_hashes_warm_splits_every_l2_cell(self):
        p = build_partition(self.g, self.ng, replace(previous_of(self.p3, self.ng), l2_hash=None), POLY, CFG3, 1)
        self.assertEqual(p.info["resplit"], {"unchanged": 0, "warm": len(self.p3.cells[1]), "fresh": 0})

    def test_new_nodes_l1_by_polygon_l2_by_network(self):
        p = build_partition(self.g_spur, self.ng_spur, self.prev, POLY, CFG, 1)
        self.assertEqual(p.codes[self.spur, 0].tolist(), [1, 1, 1])
        self.assertTrue((p.codes[self.spur, 1] == p.codes[self.attach, 1]).all())
        self.assertEqual(p.info["l1"]["polygon"], 3)
        self.assertEqual(p.info["l2"]["network"], 3)

    def test_removed_bridge_fragment_joins_most_connected_sibling(self):   # Review Focus 3
        p = build_partition(self.g_nobridge, self.ng_nobridge, self.prev_x_spans_bridge, POLY, CFG, 1)
        self.assertTrue((p.codes[self.half2, 1] == self.code_y).all())     # 3 edges to Y, 1 to Z
        self.assertTrue(check_partition(self.ng_nobridge, p, self.prev_x_spans_bridge, 2)["contiguity_l2"]["ok"])
        self.assertEqual(len(p.cells[1]), 4)

    def test_contiguity_check_fails_on_a_cell_in_two_pieces(self):
        p = build_partition(self.g_nobridge, self.ng_nobridge, self.prev_x_spans_bridge, POLY, CFG, 1)
        broken = replace(p, codes=self.prev_x_spans_bridge.codes.copy())
        c = check_partition(self.ng_nobridge, broken, self.prev_x_spans_bridge, 2)
        self.assertEqual((c["contiguity_l2"]["value"], c["contiguity_l2"]["ok"]), (1, False))
        self.assertTrue(c["contiguity_l1"]["ok"])

    def test_island_without_link_is_not_a_violation(self):
        p = build_partition(self.g_island, self.ng_island, self.prev, POLY, CFG, 1)
        checks = check_partition(self.ng_island, p, self.prev, 2)
        self.assertTrue(all(v["ok"] for k, v in checks.items() if v["blocking"]), checks)
        island = np.flatnonzero(self.ng_island.osm >= 2001)
        self.assertEqual(p.codes[island, 0].tolist(), [1, 1, 1])
        self.assertEqual(p.info["l2"]["nearest"], 3)

    def test_region_split_in_two_merges_the_small_piece(self):
        # previous L1 cell 0 = west half + a detached 5-node strip inside the east half, linked only to the east half
        p = build_partition(self.g, self.ng, self.prev_detached, POLY, CFG, 1)
        self.assertTrue((p.codes[self.piece, 0] == 1).all())
        self.assertEqual(len(p.cells[0]), 2)
        self.assertEqual(p.info["rules"]["contiguity_l1"]["pieces_moved"], 1)
        self.assertTrue(all(v["ok"] for v in check_partition(self.ng, p, self.prev_detached, 2).values()
                            if v["blocking"]))

    def test_ferry_unit_takes_home_landing_labels(self):
        p = build_partition(self.g_ferry, self.ng_ferry, None, POLY, CFG, 1)   # landing W: 3 road edges, landing E: 1
        self.assertTrue((p.codes[self.ferry_inner] == p.codes[self.landing_w]).all())
        self.assertTrue((p.codes[self.landing_e] == p.codes[self.landing_w]).all())
        self.assertEqual(p.info["rules"]["ferry"]["units"], 1)
        p3 = build_partition(self.g_ferry, self.ng_ferry, None, POLY, CFG3, 1)
        self.assertTrue((p3.codes[self.landing_e] == p3.codes[self.landing_w]).all())

    def test_nesting_split_then_merge(self):
        p = build_partition(self.g, self.ng, self.prev_l2_across_l1, POLY, CFG, 1)
        l1_of_l2 = {}
        for c1, c2 in p.codes[:, :2].tolist():
            l1_of_l2.setdefault(c2, set()).add(c1)
        self.assertTrue(all(len(s) == 1 for s in l1_of_l2.values()))
        self.assertEqual(p.info["rules"]["nesting_l2"]["cells_split"], 1)

    def test_only_changed_l2_cells_are_resplit_and_grown_ones_fresh(self):
        p = build_partition(self.g_two_changes, self.ng_two_changes, self.prev, POLY, CFG3, 1)
        self.assertEqual(p.info["resplit"], {"unchanged": self.n_l2 - 2, "warm": 1, "fresh": 1})   # +2 nodes, +40%

    def test_k2_and_k3_levels(self):
        self.assertEqual(len(build_partition(self.g, self.ng, None, POLY, PartitionConfig(split_nodes=(), l2_nodes=40),
                                             1).seg_labels), 2)
        p3 = build_partition(self.g, self.ng, None, POLY, PartitionConfig(split_nodes=(12,), l2_nodes=40), 1)
        self.assertEqual(len(p3.seg_labels), 3)
        self.assertEqual(p3.seg_labels[2].dtype, np.uint16)
        p4 = build_partition(self.g, self.ng, None, POLY, PartitionConfig(split_nodes=(12, 4), l2_nodes=40), 1)
        self.assertEqual(len(p4.seg_labels), 4)
        self.assertTrue(all(v["ok"] for v in check_partition(self.ng, p4, None, 2).values() if v["blocking"]))
        self.assertGreater(len(p4.cells[3]), len(p4.cells[2]))

    def test_seed_import(self):
        scratch = self.rel / "scratch"
        scratch.mkdir()
        ids = np.array([-1, 5, 6, 7], dtype=np.int64)
        np.savez(scratch / "node_region_v3.2.npz", ids=ids, region=np.array([3, 3, 3, -1], dtype=np.int8))
        np.savez(scratch / "node_region_h32all.npz", ids=ids,
                 region=np.array([103001, 103001, 103002, -1], dtype=np.int32))
        np.save(scratch / "labels_l3.npy", np.array([103001000, 103001001, 103002000, -1], dtype=np.int64))
        out = self.rel / "seed.npz"
        info = import_seed(scratch, out)
        s = load_seed(out)
        self.assertEqual(s.ids.tolist(), [5, 6, 7])
        self.assertEqual(s.codes[2].tolist(), [-1, -1, -1])
        self.assertEqual(s.codes[0].tolist(), [3, 103001, 103001001])
        self.assertIsNone(s.l2_hash)
        self.assertEqual((info["ids_read"], info["negative_dropped"], info["l1_codes"], info["l1_unlabelled"],
                          info["l2_codes"], info["l3_codes"]), (4, 1, 1, 1, 2, 2))

    def test_seed_import_refuses_misaligned_tables(self):
        scratch = self.rel / "scratch"
        scratch.mkdir()
        np.savez(scratch / "node_region_v3.2.npz", ids=np.array([5, 6]), region=np.array([3, 3], dtype=np.int8))
        np.savez(scratch / "node_region_h32all.npz", ids=np.array([5, 7]), region=np.array([1, 1], dtype=np.int32))
        np.save(scratch / "labels_l3.npy", np.array([1, 1], dtype=np.int64))
        with self.assertRaisesRegex(PartitionError, "ids"):
            import_seed(scratch, self.rel / "seed.npz")

    def test_too_many_cells_raises(self):
        with mock.patch.object(partition, "MAX_CELLS", 3):
            with self.assertRaisesRegex(PartitionError, "cells"):
                build_partition(self.g, self.ng, None, POLY, CFG3, 1)
        self.assertEqual(MAX_CELLS, 65535)

    # ---- rulings of the Task 6 review --------------------------------------------------------------------------------

    def test_detached_expressway_run_is_exempt_from_contiguity(self):          # T6-c
        p = build_partition(self.g_motorway, self.ng_motorway, self.prev_motorway, POLY, CFG, 1)
        self.assertEqual(p.codes[self.run_nodes, 1].tolist(), [10, 10, 10])
        self.assertEqual(p.info["expressway_detached"]["l2"], {"pieces": 1, "nodes": 3})
        c = check_partition(self.ng_motorway, p, self.prev_motorway, 2)
        self.assertTrue(c["contiguity_l2"]["ok"])
        self.assertEqual(c["contiguity_l2"]["expressway_detached"], 1)
        # the same run as a residential road is an ordinary fragment and joins Y
        q = build_partition(self.g_residential_run, self.ng_residential_run, self.prev_residential_run, POLY, CFG, 1)
        run = np.flatnonzero(self.ng_residential_run.osm >= 5001)
        self.assertEqual(q.codes[run, 1].tolist(), [11, 11, 11])
        self.assertEqual(q.info["expressway_detached"]["l2"], {"pieces": 0, "nodes": 0})

    def test_vanished_cells_and_moved_nodes_are_counted(self):              # T6-d and vanished cells
        cfg = PartitionConfig(split_nodes=(12,), l2_nodes=40, split_band_km=(0.15,))
        base = build_partition(self.g, self.ng, None, POLY, cfg, 1)
        l2 = base.codes[:, 1]
        cell = int(np.bincount(np.searchsorted(base.cells[1], l2)).argmax())
        code = int(base.cells[1][cell])
        inside = l2 == code
        deg_in = np.bincount(self.ng.lo[inside[self.ng.lo] & inside[self.ng.hi]], minlength=self.ng.n) + \
            np.bincount(self.ng.hi[inside[self.ng.lo] & inside[self.ng.hi]], minlength=self.ng.n)
        v = int(np.flatnonzero(inside & (deg_in == 4))[0])
        codes = base.codes.copy()
        a = int(codes[np.flatnonzero(inside)[0], 2])
        codes[inside, 2] = a
        codes[v, 2] = 999999                       # a one-node L3 cell inside a cell that reaches beyond its band
        hashes = dict(base.l2_hash)
        hashes[code] = "stale"
        p = build_partition(self.g, self.ng, Previous(self.ng.osm.copy(), codes, hashes, "test"), POLY, cfg, 1)
        self.assertEqual(p.info["resplit"], {"unchanged": len(base.cells[1]) - 1, "warm": 1, "fresh": 0})
        self.assertEqual(int(p.codes[v, 2]), a)
        self.assertNotIn(999999, p.cells[2].tolist())
        self.assertEqual(p.info["cells_vanished"], {"l1": 0, "l2": 0, "l3": 1})
        self.assertGreaterEqual(p.info["nodes_moved"]["l3"], 1)
        self.assertEqual(p.info["nodes_moved"]["l1"], 0)

    # ---- node graph, persistence and CLI -----------------------------------------------------------------------------

    def test_node_graph_marks_rivers_ferries_and_components(self):
        rivers = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "LineString",
                                                               "coordinates": [[105.8015, 20.99], [105.8015, 21.02]]}}]}
        _, g, ng = make_graphs(self.root / "river", self.coords, self.ways, rivers=rivers)
        self.assertEqual(ng.osm.tolist(), g.node_osm.tolist())
        self.assertEqual(ng.m, 2 * N * (N - 1))
        self.assertTrue((ng.lo < ng.hi).all())
        self.assertEqual(int(ng.river.sum()), N)                   # every row crosses the line once
        crossing = ng.lon[ng.lo[ng.river]]
        self.assertTrue(np.allclose(crossing, 105.801))
        cg = cell_graph(ng)
        self.assertTrue((cg.cap[ng.river] == 0).all() and (cg.cap[~ng.river] > 0).all())
        self.assertEqual(set(ng.component.tolist()), {0})
        sub = cell_graph(ng, ng.lon < MID)
        self.assertEqual((sub.n, len(sub.lo)), (72, 12 * 5 + 6 * 11))
        f = self.ng_ferry
        self.assertEqual(int(f.ferry.sum()), 2)
        self.assertFalse(f.expressway.any())
        self.assertEqual(int(self.ng_motorway.expressway.sum()), 4)
        self.assertEqual(len(set(self.ng_island.component.tolist())), 2)

    def test_save_and_load_partition(self):
        save_partition(self.p3, self.rel)
        q = load_partition(self.rel)
        np.testing.assert_array_equal(q.codes, self.p3.codes)
        np.testing.assert_array_equal(q.ids, self.p3.ids)
        for a, b in zip(q.seg_labels, self.p3.seg_labels):
            np.testing.assert_array_equal(a, b)
            self.assertEqual(a.dtype, np.uint16)
        self.assertEqual([c.tolist() for c in q.cells], [c.tolist() for c in self.p3.cells])
        self.assertEqual(q.l2_hash, self.p3.l2_hash)
        doc = json.loads((self.rel / "partition.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["k"], 3)
        self.assertEqual(len(doc["parents"]["l3"]), len(self.p3.cells[2]))
        self.assertEqual(sorted(doc), sorted(["format", "k", "config", "cells", "parents", "l2_hash", "resplit",
                                              "rules", "checks", "stats", "previous", "polygons_sha256",
                                              "rivers_sha256"]))

    def _cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def test_cli_run_then_run_again_from_the_previous_release(self):
        rel1, rel2 = self.rel / "r1", self.rel / "r2"
        for r in (rel1, rel2):
            save_graph(self.g, r / "graph")
        poly, riv = self.rel / "poly.geojson", self.rel / "rivers.geojson"
        poly.write_text(json.dumps(POLY), encoding="utf-8")
        riv.write_text(json.dumps(EMPTY), encoding="utf-8")
        code, out = self._cli(["run", "--release", str(rel1), "--polygons", str(poly), "--rivers", str(riv),
                               "--workers", "1"])
        self.assertEqual(code, 0)
        self.assertIn(str(rel1), out)
        doc = json.loads((rel1 / "partition.json").read_text(encoding="utf-8"))
        self.assertTrue(all(v["ok"] for v in doc["checks"].values() if v["blocking"]), doc["checks"])
        self.assertEqual(np.load(rel1 / "labels_l3.npy").dtype, np.uint16)
        self.assertEqual(doc["checks"]["l1_cells_unchanged"]["value"], 2)
        code, _ = self._cli(["run", "--release", str(rel2), "--previous", str(rel1), "--polygons", str(poly),
                             "--rivers", str(riv), "--workers", "1"])
        doc2 = json.loads((rel2 / "partition.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(doc2["resplit"], {"unchanged": len(doc["cells"][1]), "warm": 0, "fresh": 0})
        self.assertEqual(doc2["previous"], f"release {rel1}")
        self.assertTrue(doc2["checks"]["l2_cells_changed"]["ok"])


if __name__ == "__main__":
    unittest.main()
