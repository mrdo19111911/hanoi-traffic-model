"""Offline tests for speedmatrix.mincut (the min-cut cell splitter) on small synthetic road graphs."""
import unittest

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from speedmatrix.geo import haversine_m
from speedmatrix.mincut import (CAPACITY, DEFAULT_CAPACITY, MIN_RUN_KM, SEED, CellGraph, cell_hash, expressway_rule,
                                merge_fragments, refine_pairs, split_cell, split_jobs, split_k, warm_split,
                                weighted_cut)

STEP = 0.001  # degrees between grid nodes: about 111 m north-south, 104 m east-west at 21 N


def grid(nx, ny, lon0, lat0, start=0):
    """Nodes start.. of an nx * ny grid (row-major) and its horizontal and vertical edges."""
    lon, lat, edges = [], [], []
    for r in range(ny):
        for c in range(nx):
            lon.append(lon0 + c * STEP)
            lat.append(lat0 + r * STEP)
            i = start + r * nx + c
            if c + 1 < nx:
                edges.append((i, i + 1))
            if r + 1 < ny:
                edges.append((i, i + nx))
    return lon, lat, edges


def make(lon, lat, edges, highway=None, names=("residential",), river=None, osm=None):
    edges = np.asarray(edges, np.int64).reshape(-1, 2)
    m = len(edges)
    return CellGraph.from_edges(
        np.arange(1000, 1000 + len(lon), dtype=np.int64) if osm is None else np.asarray(osm, np.int64),
        np.asarray(lon, float), np.asarray(lat, float), edges[:, 0], edges[:, 1],
        highway=np.zeros(m, np.int16) if highway is None else np.asarray(highway, np.int16), names=list(names),
        river=np.zeros(m, bool) if river is None else np.asarray(river, bool))


def clique(nodes):
    return [(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1:]]


def two_grids(extra=0):
    """Two 6 x 6 grids about 2 km apart joined by 2 edges; `extra` new nodes hang off node 0 as a path westward."""
    lon1, lat1, e1 = grid(6, 6, 105.800, 21.000)
    lon2, lat2, e2 = grid(6, 6, 105.825, 21.000, start=36)
    lon, lat, edges = lon1 + lon2, lat1 + lat2, e1 + e2 + [(11, 42), (29, 60)]
    prev = 0
    for j in range(extra):
        lon.append(105.800 - (j + 1) * STEP)
        lat.append(21.000)
        edges.append((prev, 72 + j))
        prev = 72 + j
    return make(lon, lat, edges)


def pieces(g, labels):
    same = (labels[g.lo] >= 0) & (labels[g.lo] == labels[g.hi])
    a = coo_matrix((np.ones(int(same.sum())), (g.lo[same], g.hi[same])), shape=(g.n, g.n))
    return connected_components(a, directed=False)[1]


class CellGraphTests(unittest.TestCase):
    def test_from_edges_projects_orders_and_measures(self):
        lon, lat = [105.80, 105.81, 105.80], [21.00, 21.00, 21.01]
        g = CellGraph.from_edges(np.array([7, 8, 9]), np.array(lon), np.array(lat), np.array([1, 2]),
                                 np.array([0, 0]), highway=np.array([0, 1], np.int16),
                                 names=["motorway", "secondary"], river=np.array([False, False]))
        self.assertEqual(g.lo.tolist(), [0, 0])
        self.assertEqual(g.hi.tolist(), [1, 2])
        self.assertEqual((g.lo.dtype, g.hi.dtype, g.cap.dtype, g.length_m.dtype), (np.int32, np.int32, np.int8,
                                                                                   np.float32))
        self.assertEqual(g.cap.tolist(), [6, 8])
        self.assertEqual(g.expressway.tolist(), [True, False])
        self.assertEqual(g.node.tolist(), [0, 1, 2])
        lat0, lon0 = np.mean(lat), np.mean(lon)
        np.testing.assert_allclose(g.x_km, (np.array(lon) - lon0) * 111.32 * np.cos(np.radians(lat0)))
        np.testing.assert_allclose(g.y_km, (np.array(lat) - lat0) * 111.32)
        np.testing.assert_allclose(g.length_m, haversine_m(np.array([21.0, 21.0]), np.array([105.80, 105.80]),
                                                           np.array([21.0, 21.01]), np.array([105.81, 105.80])),
                                   rtol=1e-6)

    def test_from_edges_refuses_bad_input(self):
        one = dict(highway=np.zeros(1, np.int16), names=["residential"], river=np.zeros(1, bool))
        osm, lon, lat = np.array([1, 2]), np.array([105.8, 105.81]), np.array([21.0, 21.0])
        with self.assertRaisesRegex(ValueError, "self-loop"):
            CellGraph.from_edges(osm, lon, lat, np.array([1]), np.array([1]), **one)
        with self.assertRaisesRegex(ValueError, "range"):
            CellGraph.from_edges(osm, lon, lat, np.array([0]), np.array([2]), **one)
        with self.assertRaisesRegex(ValueError, "highway code"):
            CellGraph.from_edges(osm, lon, lat, np.array([0]), np.array([1]), highway=np.array([1], np.int16),
                                 names=["residential"], river=np.zeros(1, bool))

    def test_capacity_table(self):
        self.assertEqual(CAPACITY["trunk_link"], 6)
        self.assertEqual(CAPACITY["tertiary"], 9)
        self.assertEqual(DEFAULT_CAPACITY, 10)
        self.assertEqual((MIN_RUN_KM, SEED), (2.0, 17))


class WeightedCutTests(unittest.TestCase):
    def test_weighted_cut_finds_minimum(self):
        # two 10-node cliques (cap 10) joined by two edges of cap 10
        edges = np.array(clique(list(range(10))) + clique(list(range(10, 20))) + [(9, 10), (8, 11)])
        lo, hi, cap = edges[:, 0], edges[:, 1], np.full(len(edges), 10, np.int8)
        side = np.zeros(20, np.int8)
        side[0], side[19] = 1, 2
        value, a, b = weighted_cut(lo, hi, cap, side, 20)
        self.assertEqual(value, 20)
        self.assertTrue(a[:10].all() and not a[10:].any())
        self.assertTrue(b[10:].all() and not b[:10].any())

    def test_big_road_preferred_when_counts_tie(self):
        # cliques A - M - B; A-M two residential edges (cap 10), M-B two primary edges (cap 7); A fixed side 1, B side 2
        edges = clique(list(range(10))) + clique(list(range(10, 20))) + clique(list(range(20, 30)))
        caps = [10] * len(edges) + [10, 10, 7, 7]
        edges = np.array(edges + [(9, 10), (8, 11), (19, 20), (18, 21)])
        side = np.zeros(30, np.int8)
        side[:10], side[20:] = 1, 2
        value, a, _ = weighted_cut(edges[:, 0], edges[:, 1], np.array(caps, np.int8), side, 30)
        self.assertEqual(value, 14)
        self.assertTrue(a[10:20].all())

    def test_river_crossing_is_free(self):
        lon1, lat1, e1 = grid(3, 3, 105.800, 21.000)
        lon2, lat2, e2 = grid(3, 3, 105.810, 21.000, start=9)
        osm, lon, lat = np.arange(18) + 50, np.array(lon1 + lon2), np.array(lat1 + lat2)
        edges = np.array(e1 + e2 + [(5, 12)])                    # the last edge is the bridge
        lo, hi = edges[:, 0], edges[:, 1]
        only_bridge_mask = np.zeros(len(lo), bool)
        only_bridge_mask[-1] = True
        g = CellGraph.from_edges(osm, lon, lat, lo, hi, highway=np.zeros(len(lo), np.int16), names=["primary"],
                                 river=only_bridge_mask)
        self.assertEqual(int(g.cap[only_bridge_mask][0]), 0)
        self.assertTrue((g.cap[~only_bridge_mask] == 7).all())
        side = np.zeros(18, np.int8)
        side[0], side[17] = 1, 2
        value, a, b = weighted_cut(g.lo, g.hi, g.cap, side, g.n)
        self.assertEqual(value, 0)
        self.assertFalse(a[9:].any() or b[:9].any())


class RefineTests(unittest.TestCase):
    def test_refine_moves_the_border_to_the_cheaper_cut(self):
        # 10 x 3 grid, border between columns 4 and 5; the edges between columns 6 and 7 are primary (cheaper)
        lon, lat, edges = grid(10, 3, 105.800, 21.000)
        highway = [1 if b == a + 1 and a % 10 == 6 else 0 for a, b in edges]
        g = make(lon, lat, edges, highway=highway, names=("residential", "primary"))
        col = np.arange(30) % 10
        labels, rows = refine_pairs(g, np.where(col <= 4, 4, 9), band_km=0.2)
        self.assertEqual(labels.tolist(), np.where(col <= 6, 4, 9).tolist())
        self.assertEqual(rows, [{"pair": [4, 9], "edges_before": 3, "edges_after": 3, "cut_cost": 21,
                                 "nodes_moved": 6}])

    def test_refine_leaves_unlabelled_nodes(self):
        lon, lat, edges = grid(4, 1, 105.800, 21.000)
        g = make(lon, lat, edges)
        labels, rows = refine_pairs(g, np.array([0, -1, 1, 1]), band_km=1.0)
        self.assertEqual(labels.tolist(), [0, -1, 1, 1])
        self.assertEqual(rows, [])


class MergeFragmentTests(unittest.TestCase):
    def setUp(self):
        # label 1 main piece 0-3, label 2 block 4-7, label 3 block 8-11, a piece of label 1 (12, 13) with 3 edges to
        # label 2 and 1 edge to label 3, and an island of label 1 (14, 15) without any outside edge
        edges = [(0, 1), (1, 2), (2, 3), (4, 5), (5, 6), (6, 7), (8, 9), (9, 10), (10, 11), (3, 4), (7, 8),
                 (12, 13), (12, 4), (12, 5), (13, 6), (13, 8), (14, 15)]
        lon = [105.80 + 0.001 * i for i in range(16)]
        self.g3 = make(lon, [21.0] * 16, edges)
        self.labels_with_piece = np.array([1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 1, 1, 1, 1])
        self.piece = [12, 13]

    def test_merge_fragments_joins_most_connected(self):
        labels, moved = merge_fragments(self.g3, self.labels_with_piece)   # piece of label 1: 3 edges to 2, 1 to 3
        self.assertTrue((labels[self.piece] == 2).all())
        self.assertEqual(moved, 1)
        self.assertEqual(labels[[0, 1, 2, 3, 14, 15]].tolist(), [1] * 6)    # the island is left as it is
        self.assertEqual(self.labels_with_piece[12], 1)                     # the input is not modified

    def test_merge_fragments_leaves_one_piece_per_label(self):
        lon, lat, edges = grid(12, 12, 105.800, 21.000)
        g = make(lon, lat, edges)
        rng = np.random.default_rng(3)
        labels = rng.integers(0, 5, g.n) * 10 - (rng.random(g.n) < 0.05) * 1000
        labels = np.where(labels < 0, -1, labels)
        out, moved = merge_fragments(g, labels)
        comp = pieces(g, out)
        self.assertGreater(moved, 0)
        self.assertTrue((out[labels < 0] == -1).all())
        for lab in np.unique(out[out >= 0]):
            members = comp[out == lab]
            largest = np.bincount(members).argmax()
            for c in np.unique(members):
                if c == largest:
                    continue
                # an extra piece may remain only when it has no edge to another labelled cell
                touches = (((comp[g.lo] == c) & (out[g.hi] >= 0) & (out[g.hi] != lab))
                           | ((comp[g.hi] == c) & (out[g.lo] >= 0) & (out[g.lo] != lab)))
                self.assertFalse(touches.any(), f"label {lab} keeps a stray piece")


class ExpresswayTests(unittest.TestCase):
    """A residential road along lat 21.000 from lon 105.800 to 105.820 (labels: 0 west of 105.810 with a tongue of
    label 1 at 105.804-105.805, 1 from 105.810), and a motorway 55 m north over the whole of it whose ends link to
    the road's two end nodes. The min cut left every motorway node in cell 1."""

    def setUp(self):
        road_lon = [105.800 + 0.001 * i for i in range(21)]
        mw_lon = [105.8002 + 0.0005 * i for i in range(40)]
        lon, lat = road_lon + mw_lon, [21.000] * 21 + [21.0005] * 40
        edges = [(i, i + 1) for i in range(20)] + [(21 + i, 22 + i) for i in range(39)] + [(0, 21), (20, 60)]
        highway = [0] * 20 + [1] * 39 + [2, 2]
        self.gx = make(lon, lat, edges, highway=highway, names=("residential", "motorway", "motorway_link"))
        road = np.array([0 if (lo < 105.8095 and not 105.8035 < lo < 105.8055) else 1 for lo in road_lon])
        self.lx = np.r_[road, np.ones(40, np.int64)]
        self.motorway = np.arange(21, 61)
        self.motorway_in_land_a = self.motorway[np.array(mw_lon) < 105.8095]
        self.road = road

    def test_expressway_takes_nearest_ordinary_node_and_absorbs_short_run(self):
        labels, info = expressway_rule(self.gx, self.lx)
        self.assertTrue((labels[self.motorway_in_land_a] == 0).all())
        self.assertEqual(info["short_runs_absorbed"], 1)
        self.assertTrue((labels[np.setdiff1d(self.motorway, self.motorway_in_land_a)] == 1).all())
        self.assertEqual(labels[:21].tolist(), self.road.tolist())          # ordinary nodes never change
        self.assertEqual(info["expressway_only_nodes"], 40)
        self.assertEqual(info["expressway_cut_edges_after"], 1)

    def test_expressway_rule_respects_parent_and_unlabelled_nodes(self):
        lx = self.lx.copy()
        lx[21] = -1
        parent = np.r_[np.where(self.road == 1, 1, 0), np.ones(40, np.int64)]
        labels, info = expressway_rule(self.gx, lx, parent)
        self.assertEqual(labels[21], -1)
        self.assertTrue((labels[22:61] == 1).all())                        # only parent-1 land is a candidate
        self.assertEqual(info["nodes_relabelled"], 0)


class SplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.two_grids = two_grids()
        cls.two_grids_plus = two_grids(extra=3)
        cls.two_grid_labels, cls.two_grid_info = split_cell(cls.two_grids, k=2, band_km=1.0)

    def test_fresh_split_two_clusters(self):
        labels, info = self.two_grid_labels, self.two_grid_info          # two 6x6 grids 2 km apart, joined by 2 edges
        self.assertEqual(len(set(labels[:36])), 1)
        self.assertEqual(len(set(labels[36:])), 1)
        self.assertNotEqual(labels[0], labels[-1])
        self.assertEqual(sorted(set(labels.tolist())), [0, 1])
        self.assertEqual(info["final_cut_edges"], 2)

    def test_fresh_split_is_deterministic_and_relabels_densely(self):
        lon, lat, edges = grid(20, 20, 105.800, 21.000)
        g = make(lon, lat, edges)
        a, info = split_cell(g, k=4, band_km=0.3)
        b, _ = split_cell(g, k=4, band_km=0.3)
        np.testing.assert_array_equal(a, b)
        self.assertEqual(sorted(set(a.tolist())), list(range(info["cells"])))
        self.assertGreaterEqual(info["cells"], 2)
        comp = pieces(g, a)
        self.assertTrue(all(len(np.unique(comp[a == c])) == 1 for c in range(info["cells"])))

    def test_split_of_one_cell_or_too_few_junctions(self):
        g = make(*grid(4, 1, 105.800, 21.000))                            # a path: no junction at all
        labels, info = split_cell(g, k=3, band_km=1.0)
        self.assertEqual(labels.tolist(), [0, 0, 0, 0])
        self.assertEqual(info["cells"], 1)

    def test_warm_split_keeps_a_min_cut_and_labels_new_nodes(self):
        old = self.two_grid_labels.copy()
        self.assertTrue(np.array_equal(warm_split(self.two_grids, old, 1.0)[0], old))
        old2 = np.r_[old, [-1, -1, -1]]                                   # three new nodes hanging off grid 1
        self.assertTrue((warm_split(self.two_grids_plus, old2, 1.0)[0][-3:] == old[0]).all())

    def test_warm_split_keeps_old_codes(self):
        old = np.where(self.two_grid_labels == self.two_grid_labels[0], 5007, 5012)
        labels, info = warm_split(self.two_grids_plus, np.r_[old, [-1, -1, -1]], 1.0)
        self.assertEqual(sorted(set(labels.tolist())), [5007, 5012])
        self.assertEqual(info["new_nodes"], 3)

    def test_warm_split_unreached_new_node_takes_euclidean_nearest(self):
        # one new node just east of grid 2 with no edge at all
        lon1, lat1, e1 = grid(6, 6, 105.800, 21.000)
        lon2, lat2, e2 = grid(6, 6, 105.825, 21.000, start=36)
        g2 = make(lon1 + lon2 + [105.8301], lat1 + lat2 + [21.0001], e1 + e2 + [(11, 42), (29, 60)])
        old = np.r_[self.two_grid_labels, -1]
        labels, info = warm_split(g2, old, 1.0)
        self.assertEqual(labels[-1], self.two_grid_labels[-1])
        self.assertEqual(info["unreached"], 1)

    def test_warm_split_needs_a_labelled_node(self):
        with self.assertRaisesRegex(ValueError, "labelled"):
            warm_split(self.two_grids, np.full(72, -1), 1.0)

    def test_cell_hash(self):
        lon, lat, edges = grid(3, 3, 105.800, 21.000)
        g = make(lon, lat, edges, highway=[0] * len(edges), names=("residential", "primary"))
        perm = np.random.default_rng(1).permutation(9)                  # new index of old node i is where[perm == i]
        inv = np.argsort(perm)
        e = np.array(edges)[::-1]
        g_permuted = make(list(np.array(lon)[perm]), list(np.array(lat)[perm]), np.c_[inv[e[:, 1]], inv[e[:, 0]]],
                          names=("residential", "primary"), osm=(np.arange(1000, 1009))[perm])
        g_primary = make(lon, lat, edges, highway=[1] + [0] * (len(edges) - 1), names=("residential", "primary"))
        self.assertEqual(cell_hash(g), cell_hash(g_permuted))
        self.assertNotEqual(cell_hash(g), cell_hash(g_primary))
        self.assertRegex(cell_hash(g), "^[0-9a-f]{64}$")

    def test_split_k(self):
        self.assertEqual(split_k(5400, 1000, 1800), 3)
        self.assertEqual(split_k(5400, 8, 1800), 2)
        self.assertEqual(split_k(2000, 1000, 1800), 1)
        self.assertEqual(split_k(9000, 1000, 1800, min_k=6), 1)

    def test_split_jobs_parallel_equals_serial(self):
        big = make(*grid(20, 20, 105.800, 21.000))
        jobs = [{"code": 5, "graph": self.two_grids, "old": None, "k": 2, "band_km": 1.0, "mode": "fresh"},
                {"code": 9, "graph": self.two_grids_plus, "old": np.r_[self.two_grid_labels + 90, [-1, -1, -1]],
                 "k": 2, "band_km": 1.0, "mode": "warm"},
                {"code": 7, "graph": big, "old": None, "k": 4, "band_km": 0.3, "mode": "fresh"}]
        a, b = split_jobs(jobs, 1), split_jobs(jobs, 2)
        self.assertEqual(a.keys(), b.keys())
        self.assertEqual(sorted(a), [5, 7, 9])
        self.assertTrue(all(np.array_equal(a[k], b[k]) for k in a))
        np.testing.assert_array_equal(a[5], self.two_grid_labels)
        self.assertTrue((a[9][-3:] == self.two_grid_labels[0] + 90).all())

    def test_split_jobs_refuses_bad_jobs(self):
        job = {"code": 1, "graph": self.two_grids, "old": None, "k": 2, "band_km": 1.0, "mode": "warm"}
        with self.assertRaisesRegex(ValueError, "old"):
            split_jobs([job], 1)
        with self.assertRaisesRegex(ValueError, "mode"):
            split_jobs([dict(job, mode="lukewarm")], 1)
        with self.assertRaisesRegex(ValueError, "code"):
            split_jobs([dict(job, mode="fresh"), dict(job, mode="fresh")], 1)


if __name__ == "__main__":
    unittest.main()
