"""Offline tests for speedmatrix.graph on the fixture town (tests/sm_osrm_fixture.py)."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import (TOWN_BANNED, TOWN_COORDS, TOWN_OBSTACLES, TOWN_WAYS, poke_block, write_dataset,
                             write_osm)
from speedmatrix.graph import GRAPH_FORMAT, GraphError, build_graph, load_graph, main, save_graph, segment_ds
from speedmatrix.osrmfiles import OsrmFormatError, read_block
from speedmatrix.segments import ROAD_CLASSES, _sha256_file, build_segments, save_segments


def _table(root, name, ways):
    osm = root / f"{name}.osm"
    write_osm(osm, TOWN_COORDS, ways)
    return build_segments(osm, None), _sha256_file(osm)


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = root = Path(cls.temp.name)
        cls.table, sha = _table(root, "town", TOWN_WAYS)

        def town(name, **kw):
            return write_dataset(root / name, TOWN_COORDS, TOWN_WAYS, TOWN_OBSTACLES, TOWN_BANNED,
                                 pbf_sha256=kw.pop("pbf_sha256", sha), **kw)

        cls.base = town("ok")
        cls.bj = root / "ok" / "build_matrix.json"
        cls.base_bad = town("bad", enw_delta={0: 1})
        cls.base_weight = town("weight", weight_delta={0: 5})
        cls.base_other_sha = town("other", pbf_sha256="another")
        cls.bj_other = root / "other" / "build_matrix.json"
        no4 = [w if w[0] != 4 else (4, w[1], {"highway": "proposed"}) for w in TOWN_WAYS]
        cls.table_no4, _ = _table(root, "no4", no4)
        cls.table_dup, _ = _table(root, "dup", TOWN_WAYS + [(5, [102, 103], {"highway": "primary"})])
        cls.table_no4.meta["pbf_sha256"] = cls.table_dup.meta["pbf_sha256"] = sha

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_counts_and_obstacle_fold(self):
        g = build_graph(self.base, self.bj, self.table)
        self.assertEqual((g.n_nodes, g.n_ebn, g.n_segments), (7, 11, 13))
        s = slice(g.ebn_offsets[0], g.ebn_offsets[1])                      # g0 forward: 101-107-102
        self.assertEqual(g.node_osm[g.seg_tail[s]].tolist(), [101, 107])
        self.assertEqual(g.seg_fixed_ds[s].tolist(), [20, 0])
        r = g.ebn_offsets[6]                                                # g0 reverse
        self.assertEqual(g.node_osm[g.seg_tail[r:r + 2]].tolist(), [102, 107])
        self.assertEqual(g.seg_fixed_ds[r:r + 2].tolist(), [0, 0])
        self.assertEqual(g.node_osm.tolist(), sorted(TOWN_COORDS))
        self.assertEqual(g.meta["obstacle_entries"], 1)
        self.assertEqual(g.meta["counts"], {"nodes": 7, "segments": 13, "ebn": 11, "turns": g.n_turns})

    def test_dtypes(self):
        g = build_graph(self.base, self.bj, self.table)
        expected = {"node_osm": np.int64, "node_lon_e6": np.int32, "node_lat_e6": np.int32, "seg_tail": np.int32,
                    "seg_head": np.int32, "seg_len_m": np.float32, "seg_fixed_ds": np.uint16,
                    "seg_osrm_ds": np.uint32, "seg_row": np.int32, "seg_class": np.int8, "seg_highway": np.int16,
                    "ebn_offsets": np.int64, "ebn_geometry": np.int32, "ebn_forward": np.bool_,
                    "ebn_tiny": np.bool_, "turn_offsets": np.int64, "turn_dst": np.int32, "turn_ds": np.uint16}
        for name, dtype in expected.items():
            self.assertEqual(getattr(g, name).dtype, np.dtype(dtype), name)

    def test_lengths_and_osrm_durations(self):
        g = build_graph(self.base, self.bj, self.table)
        # 101-107 is 0.0005 deg of longitude at 21 N, about 52 m; the fixture's residential speed is 25 km/h.
        i = int(g.ebn_offsets[0])
        self.assertAlmostEqual(float(g.seg_len_m[i]), 51.90, delta=0.01)
        self.assertEqual(int(g.seg_osrm_ds[i]), round(36 * float(g.seg_len_m[i]) / 25))
        # the table's length comes from float32 coordinates (about 1 m at lon 105), the graph's from e6 integers
        np.testing.assert_allclose(g.seg_len_m, self.table.length_m[g.seg_row], atol=1.0)
        self.assertEqual(g.node_lat.dtype, np.float64)
        self.assertAlmostEqual(float(g.node_lon[0]), 105.8)
        self.assertEqual(g.seg_ebn.tolist(), np.repeat(np.arange(11), np.diff(g.ebn_offsets)).tolist())

    def test_turns_connect_and_banned_turn_absent(self):
        g = build_graph(self.base, self.bj, self.table)
        last = g.ebn_offsets[1:] - 1
        first = g.ebn_offsets[:-1]
        src = np.repeat(np.arange(g.n_ebn), np.diff(g.turn_offsets))
        np.testing.assert_array_equal(g.seg_head[last[src]], g.seg_tail[first[g.turn_dst]])
        triples = {(int(g.node_osm[g.seg_tail[last[u]]]), int(g.node_osm[g.seg_head[last[u]]]),
                    int(g.node_osm[g.seg_head[first[v]]])) for u, v in zip(src, g.turn_dst)}
        self.assertNotIn((104, 102, 103), triples)
        self.assertIn((107, 102, 103), triples)

    def test_rows_classes_and_one_way(self):
        g = build_graph(self.base, self.bj, self.table)
        self.assertTrue((g.seg_row >= 0).all())
        self.assertEqual(ROAD_CLASSES[g.seg_class[g.ebn_offsets[5]]], "nho")      # g5 = way 4, track
        self.assertEqual(g.meta["highways"][g.seg_highway[g.ebn_offsets[5]]], "track")
        self.assertEqual(int((g.ebn_geometry == 4).sum()), 1)                      # way 3 one-way: forward only
        self.assertEqual(g.meta["ambiguous_pairs"], 0)

    def test_identity_failure_raises(self):
        with self.assertRaisesRegex(GraphError, "enw identity: 1 EBNs differ"):
            build_graph(self.base_bad, self.bj, self.table)

    def test_turn_identity_failure_raises(self):
        base = write_dataset(self.root / "turn", TOWN_COORDS, TOWN_WAYS, TOWN_OBSTACLES, TOWN_BANNED,
                             pbf_sha256=self.table.meta["pbf_sha256"])
        ebg = str(base) + ".ebg"
        # edge 0: weight and duration both + 1, so it is not a restricted edge but breaks total = enw + turn
        edges = read_block(ebg, "/common/edge_based_edge_list", (np.uint32, 6))
        poke_block(ebg, "/common/edge_based_edge_list", 3, int(edges[0, 3]) + 1, "<u4")
        poke_block(ebg, "/common/edge_based_edge_list", 5, int(edges[0, 5]) + 1, "<u4")
        with self.assertRaisesRegex(GraphError, "turn duration identity: 1 turns differ"):
            build_graph(base, base.parent / "build_matrix.json", self.table)

    def test_turn_connection_failure_raises(self):
        base = write_dataset(self.root / "join", TOWN_COORDS, TOWN_WAYS, TOWN_OBSTACLES, TOWN_BANNED,
                             pbf_sha256=self.table.meta["pbf_sha256"])
        ebg = str(base) + ".ebg"
        edges = read_block(ebg, "/common/edge_based_edge_list", (np.uint32, 6))
        self.assertEqual(int(edges[0, 0]), 0)                              # from g0 forward, which ends at 102
        poke_block(ebg, "/common/edge_based_edge_list", 1, 0, "<u4")      # onto g0 forward, which starts at 101
        with self.assertRaisesRegex(GraphError, "turn connection identity: 1 turns"):
            build_graph(base, base.parent / "build_matrix.json", self.table)

    def test_reverse_obstacle_folds_into_the_segment_before_it(self):
        base = write_dataset(self.root / "rev", TOWN_COORDS, TOWN_WAYS, {(107, "reverse", 1): 30}, TOWN_BANNED,
                             pbf_sha256=self.table.meta["pbf_sha256"])
        g = build_graph(base, base.parent / "build_matrix.json", self.table)
        r = g.ebn_offsets[6]                                                # g0 reverse: 102-107-101
        self.assertEqual(g.seg_fixed_ds[r:r + 2].tolist(), [30, 0])
        self.assertEqual(g.seg_fixed_ds[g.ebn_offsets[0]:g.ebn_offsets[1]].tolist(), [0, 0])
        self.assertEqual(g.meta["obstacle_entries"], 1)

    def test_ambiguous_pair_closest_beats_a_lower_way_id(self):
        # the residential way over 102-103 is renumbered 11, so the primary duplicate (way 5) has the lower id
        ways = [(11 if w == 1 else w, refs, tags) for w, refs, tags in TOWN_WAYS]
        table, _ = _table(self.root, "renum", ways + [(5, [102, 103], {"highway": "primary"})])
        table.meta["pbf_sha256"] = self.table.meta["pbf_sha256"]
        g = build_graph(self.base, self.bj, table)
        self.assertEqual(int(table.way_id[table.way[g.seg_row[g.ebn_offsets[1]]]]), 11)

    def test_ambiguous_tie_goes_to_the_lowest_way_id(self):
        table, _ = _table(self.root, "tie", TOWN_WAYS + [(9, [103, 102], {"highway": "residential"})])
        table.meta["pbf_sha256"] = self.table.meta["pbf_sha256"]
        g = build_graph(self.base, self.bj, table)
        for i in (g.ebn_offsets[1], g.ebn_offsets[7]):                     # g1 forward and reverse
            self.assertEqual(int(table.way_id[table.way[g.seg_row[i]]]), 1)
        self.assertEqual(g.meta["ambiguous_pairs"], 2)

    def test_unmapped_pair_raises_with_example(self):
        with self.assertRaisesRegex(GraphError, r"2 segments.*\(103, 106\)"):
            build_graph(self.base, self.bj, self.table_no4)

    def test_pbf_hash_mismatch_raises(self):
        with self.assertRaisesRegex(GraphError, "another PBF"):
            build_graph(self.base_other_sha, self.bj_other, self.table)

    def test_restricted_edge_raises(self):
        with self.assertRaisesRegex(GraphError, "1 restricted-access edges"):
            build_graph(self.base_weight, self.bj, self.table)

    def test_ambiguous_pair_takes_the_closest_free_flow(self):
        g = build_graph(self.base, self.bj, self.table_dup)
        i = g.ebn_offsets[1]                                               # g1 forward
        self.assertEqual(int(self.table_dup.way_id[self.table_dup.way[g.seg_row[i]]]), 1)
        self.assertEqual(g.meta["ambiguous_pairs"], 2)

    def test_bad_ranges_raise_format_error(self):
        base = write_dataset(self.root / "range", TOWN_COORDS, TOWN_WAYS, TOWN_OBSTACLES, TOWN_BANNED,
                             pbf_sha256=self.table.meta["pbf_sha256"])
        poke_block(str(base) + ".ebg_nodes", "/common/ebg_node_data/nodes", 0, 6, "<u4")   # EBN 0 -> geometry 6
        with self.assertRaisesRegex(OsrmFormatError, "geometry id"):
            build_graph(base, base.parent / "build_matrix.json", self.table)

    def test_segment_ds(self):
        g = build_graph(self.base, self.bj, self.table)
        kmh = np.full(g.n_segments, 36.0, dtype=np.float32)
        out = segment_ds(g, kmh)
        self.assertEqual(out.dtype, np.float64)
        np.testing.assert_allclose(out, g.seg_len_m.astype(np.float64) + g.seg_fixed_ds)
        with self.assertRaises(ValueError):
            segment_ds(g, kmh[:-1])

    def test_round_trip_with_mmap(self):
        g = build_graph(self.base, self.bj, self.table)
        save_graph(g, self.root / "graph")
        save_graph(g, self.root / "graph")                                 # replaces an existing graph dir
        h = load_graph(self.root / "graph", mmap=True)
        self.assertEqual(h.sha256, g.sha256)
        self.assertEqual(h.meta, g.meta)
        self.assertIsInstance(h.seg_tail, np.memmap)
        np.testing.assert_array_equal(h.turn_dst, g.turn_dst)
        np.testing.assert_array_equal(h.seg_len_m, g.seg_len_m)
        self.assertFalse(any(p.name.endswith(".tmp") for p in self.root.iterdir()))
        self.assertEqual(g.meta["format"], GRAPH_FORMAT)
        del h                                    # Windows cannot delete a mapped file

    def test_sha_follows_meta(self):
        g = build_graph(self.base, self.bj, self.table)
        g2 = build_graph(self.base, self.bj, self.table_dup)
        self.assertNotEqual(g.sha256, g2.sha256)
        self.assertEqual(g.sha256, build_graph(self.base, self.bj, self.table).sha256)

    def test_old_format_refused(self):
        g = build_graph(self.base, self.bj, self.table)
        out = self.root / "old"
        save_graph(g, out)
        meta = (out / "meta.json").read_text(encoding="utf-8").replace('"format": 1', '"format": 0')
        (out / "meta.json").write_text(meta, encoding="utf-8")
        with self.assertRaisesRegex(GraphError, "format"):
            load_graph(out)

    def test_cli_writes_the_graph(self):
        save_segments(self.table, self.root / "segs")
        out = self.root / "cli_graph"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["--osrm-base", str(self.base), "--build-json", str(self.bj), "--segments",
                  str(self.root / "segs"), "--out", str(out)])
        self.assertEqual(buf.getvalue().strip(), str(out))
        self.assertEqual(load_graph(out).n_segments, 13)


if __name__ == "__main__":
    unittest.main()
