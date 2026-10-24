"""Offline tests for speedmatrix.staticroute on the fixture town (tests/sm_osrm_fixture.py)."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import KMH, TOWN_BANNED, TOWN_COORDS, TOWN_OBSTACLES, TOWN_WAYS, write_dataset, write_osm
from speedmatrix.graph import build_graph, save_graph, segment_ds
from speedmatrix.segments import _sha256_file, build_segments
from speedmatrix.staticroute import StaticRouter, ebn_weights, parallel_table

ISLAND_COORDS = {**TOWN_COORDS, 201: (105.8100, 21.0100), 202: (105.8110, 21.0100)}
ISLAND_WAYS = TOWN_WAYS + [(5, [201, 202], {"highway": "residential", "name": "Phố Năm"})]


def make_graph(root, name, coords, ways, banned):
    osm = root / f"{name}.osm"
    write_osm(osm, coords, ways)
    table = build_segments(osm, None)
    base = write_dataset(root / name, coords, ways, TOWN_OBSTACLES, banned, pbf_sha256=_sha256_file(osm))
    g = build_graph(base, base.parent / "build_matrix.json", table)
    kmh = np.array([KMH[h] for h in g.meta["highways"]], dtype=np.float64)[g.seg_highway]
    return g, segment_ds(g, kmh)


def brute(g, ds, s, t):
    """Segment-level digraph: a midpoint node and an end node per segment, plus one virtual source."""
    S = g.n_segments
    R = StaticRouter(g, ds)
    rows, cols, w = [], [], []

    def arc(a, b, c):
        rows.append(a), cols.append(b), w.append(c)

    for e in range(g.n_ebn):
        lo, hi = int(g.ebn_offsets[e]), int(g.ebn_offsets[e + 1])
        for k in range(lo, hi):
            arc(2 * k, 2 * k + 1, ds[k] / 2)
            if k + 1 < hi:
                arc(2 * k + 1, 2 * (k + 1), ds[k + 1] / 2)
        for j in range(int(g.turn_offsets[e]), int(g.turn_offsets[e + 1])):
            first = int(g.ebn_offsets[g.turn_dst[j]])
            arc(2 * (hi - 1) + 1, 2 * first, float(g.turn_ds[j]) + ds[first] / 2)
    twin_s, twin_t = R.twin(s), R.twin(t)
    arc(2 * S, 2 * s + 1, ds[s] / 2)
    if twin_s >= 0:
        arc(2 * S, 2 * twin_s + 1, ds[twin_s] / 2)
    n = 2 * S + 1
    graph = csr_matrix((w, (rows, cols)), shape=(n, n))
    d = dijkstra(graph, directed=True, indices=2 * S)
    return min(d[2 * t], d[2 * twin_t] if twin_t >= 0 else np.inf)


class StaticRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = root = Path(cls.temp.name)
        cls.g, cls.ds = make_graph(root, "town", TOWN_COORDS, TOWN_WAYS, TOWN_BANNED)
        cls.g_unbanned, cls.ds_unbanned = make_graph(root, "free", TOWN_COORDS, TOWN_WAYS, set())
        cls.g_island, cls.ds_island = make_graph(root, "island", ISLAND_COORDS, ISLAND_WAYS, TOWN_BANNED)
        cls.island_seg = int(np.flatnonzero(cls.g_island.node_osm[cls.g_island.seg_tail] == 201)[0])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def seg_between(self, g, a, b):
        tail, head = g.node_osm[g.seg_tail], g.node_osm[g.seg_head]
        return int(np.flatnonzero((tail == a) & (head == b))[0])

    def test_ebn_weights_sum_segments(self):
        w = ebn_weights(self.g, self.ds)
        self.assertEqual(w.shape, (self.g.n_ebn,))
        self.assertAlmostEqual(float(w.sum()), float(self.ds.sum()), places=6)
        self.assertAlmostEqual(w[0], self.ds[0] + self.ds[1], places=9)

    def test_matches_bruteforce(self):
        R = StaticRouter(self.g, self.ds)
        segs = np.arange(self.g.n_segments)
        T = R.table(segs, segs)
        checked = 0
        for i, s in enumerate(segs):
            for j, t in enumerate(segs):
                if np.isnan(T[i, j]):
                    continue
                self.assertAlmostEqual(T[i, j], brute(self.g, self.ds, s, t) / 10, places=9)
                checked += 1
        self.assertGreater(checked, 60)

    def test_twin_and_one_way(self):
        R = StaticRouter(self.g, self.ds)
        fwd = self.g.ebn_offsets[4]                                         # way 3, one-way
        self.assertEqual(R.twin(fwd), -1)
        self.assertEqual(R.twin(R.twin(self.g.ebn_offsets[1])), self.g.ebn_offsets[1])
        # a two-segment geometry: segment k of the forward EBN is segment m-1-k of the reverse one
        a, b = self.g.ebn_offsets[0], self.g.ebn_offsets[6]
        self.assertEqual((R.twin(a), R.twin(a + 1)), (b + 1, b))

    def test_banned_turn_costs_a_detour(self):
        a, b = (self.seg_between(self.g, 104, 102), self.seg_between(self.g, 102, 103))
        banned = StaticRouter(self.g, self.ds).table(np.array([a]), np.array([b]))[0, 0]
        a, b = (self.seg_between(self.g_unbanned, 104, 102), self.seg_between(self.g_unbanned, 102, 103))
        free = StaticRouter(self.g_unbanned, self.ds_unbanned).table(np.array([a]), np.array([b]))[0, 0]
        self.assertGreater(banned, free + 15)

    def test_unreachable_and_same_geometry(self):
        isl = self.island_seg
        T = StaticRouter(self.g_island, self.ds_island).table(np.array([0, isl]), np.array([isl, 1]))
        self.assertTrue(np.isinf(T[0, 0]))
        self.assertTrue(np.isinf(T[1, 1]))
        self.assertTrue(np.isnan(StaticRouter(self.g, self.ds).table(np.array([0]), np.array([1]))[0, 0]))

    def test_one_way_target_is_reached_one_direction_only(self):
        R = StaticRouter(self.g, self.ds)
        a, t = self.seg_between(self.g, 101, 107), self.seg_between(self.g, 105, 106)
        self.assertTrue(np.isfinite(R.table(np.array([a]), np.array([t]))[0, 0]))

    def test_parallel_equals_serial(self):
        save_graph(self.g, self.root / "g")
        np.save(self.root / "ds.npy", self.ds)
        src = np.arange(self.g.n_segments)
        dst = np.arange(self.g.n_segments)[::-1].copy()
        serial = StaticRouter(self.g, self.ds).table(src, dst)
        np.testing.assert_allclose(parallel_table(self.root / "g", self.root / "ds.npy", src, dst, 2), serial,
                                   equal_nan=True)
        np.testing.assert_allclose(parallel_table(self.root / "g", self.root / "ds.npy", src, dst, 1), serial,
                                   equal_nan=True)

    def test_bad_index_raises(self):
        with self.assertRaises(ValueError):
            StaticRouter(self.g, self.ds).table(np.array([self.g.n_segments]), np.array([0]))


if __name__ == "__main__":
    unittest.main()
