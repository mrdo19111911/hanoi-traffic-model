"""Offline tests for speedmatrix.osrmfiles on tiny datasets in the real tar format (tests/sm_osrm_fixture.py)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import (INVALID, TOWN_BANNED, TOWN_COORDS, TOWN_OBSTACLES, TOWN_WAYS, grid, poke_block,
                             write_dataset, write_nbg_nodes)
from speedmatrix.osrmfiles import (INVALID_DURATION, OsrmFormatError, check_build_json, read_block, read_dataset,
                                   read_datasources, read_packed)

ROOT = Path(__file__).resolve().parents[1]


def town(root, **kw):
    return write_dataset(root, TOWN_COORDS, TOWN_WAYS, TOWN_OBSTACLES, TOWN_BANNED, **kw)


class OsrmFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.temp.name)
        cls.base = town(cls.root / "ok")
        cls.base_weight = town(cls.root / "weight", weight_delta={0: 5})
        cls.base2 = town(cls.root / "nodur", drop_blocks={"/extractor/edge_based_node_durations"})
        cls.base_backward = town(cls.root / "backward", backward_edge=True)
        cls.base_mixed = town(cls.root / "mixed", fingerprint_of={".enw": b"OSRN" + bytes(7)})

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_reads_town(self):
        d = read_dataset(self.base)
        self.assertEqual(sorted(d.nbg_osm.tolist()), [101, 102, 103, 104, 105, 106, 107])
        self.assertEqual((len(d.geo_index) - 1, len(d.ebn_geometry)), (6, 11))
        self.assertEqual(int(d.ebn_forward.sum()), 6)
        self.assertTrue((np.diff(d.geo_index) % 2 == 0).all())

    def test_town_turns_and_durations(self):
        d = read_dataset(self.base)
        self.assertEqual(len(d.turn_src), len(d.turn_dst))
        # g0 forward (101-107-102): two real segments plus the 20 ds signal at 107 on the zero-length node move.
        b = int(d.geo_index[0])
        self.assertEqual(int(d.geo_fwd_ds[b + 2]), 20)
        self.assertEqual(int(d.geo_rev_ds[b + 1]), 0)
        self.assertEqual(int(d.ebn_duration_ds[0]), int(d.geo_fwd_ds[b + 1:b + 4].sum()))
        # banned 104 via 102 to 103: g2 forward (EBN 2) has no turn onto g1 forward (EBN 1); it has one onto g3.
        pairs = set(zip(d.turn_src.tolist(), d.turn_dst.tolist()))
        self.assertNotIn((2, 1), pairs)
        self.assertIn((2, 3), pairs)
        # u-turn onto the reverse of the same geometry costs 200, other turns 20.
        by_pair = dict(zip(zip(d.turn_src.tolist(), d.turn_dst.tolist()), d.turn_ds.tolist()))
        self.assertEqual(by_pair[(1, 6 + 1)], 200)      # g1 fwd -> g1 reverse (EBN 7)
        self.assertEqual(by_pair[(0, 3)], 20)

    def test_identities_hold(self):
        d = read_dataset(self.base)
        np.testing.assert_array_equal(d.turn_total_ds, d.ebn_duration_ds[d.turn_src] + d.turn_ds)
        self.assertEqual(int(d.geo_fwd_ds[d.geo_index[0]]), INVALID_DURATION)
        self.assertEqual(INVALID_DURATION, INVALID)
        self.assertEqual(d.restricted_edges, 0)
        self.assertEqual(read_dataset(self.base_weight).restricted_edges, 1)        # weight_delta={0: 5}

    def test_datasources_and_blocks(self):
        d = read_dataset(self.base)
        fwd, rev = read_datasources(self.base)
        self.assertEqual((len(fwd), len(rev)), (len(d.geo_nodes), len(d.geo_nodes)))
        self.assertEqual(int(fwd.max()), 0)
        coords = read_block(str(self.base) + ".nbg_nodes", "/common/nbn_data/coordinates", (np.int32, 2))
        self.assertEqual(coords.shape, (7, 2))

    def test_grid_dataset_reads(self):
        coords, ways = grid(4)
        d = read_dataset(write_dataset(self.root / "grid", coords, ways))
        self.assertEqual(len(d.nbg_osm), 16)
        np.testing.assert_array_equal(d.turn_total_ds, d.ebn_duration_ds[d.turn_src] + d.turn_ds)

    def test_packed_ids_round_trip_over_a_block_boundary(self):
        # 70 ids > 64 forces two blocks: words = ceil(70/64) * 40 = 80
        ids = [10**10 + 7 * i for i in range(70)]                 # > 2^32, so all 40 bits are used
        p = write_nbg_nodes(self.root / "big", coords=[(105_800_000, 21_000_000)] * 70, ids=ids)
        self.assertEqual(read_packed(p, "/common/nbn_data/osm_node_ids", 40).tolist(), ids)

    def test_missing_block_is_named(self):
        with self.assertRaisesRegex(OsrmFormatError, "edge_based_node_durations"):
            read_dataset(self.base2)

    def test_wrong_packed_size_raises(self):
        bad = town(self.root / "short", short_packed={"/common/segment_data/forward_durations"})
        with self.assertRaisesRegex(OsrmFormatError, "packed"):
            read_packed(str(bad) + ".geometry", "/common/segment_data/forward_durations", 22)

    def test_wrong_block_size_raises(self):
        # a count in .meta that disagrees with the member size
        path = write_nbg_nodes(self.root / "five", coords=[(1, 2)] * 5, ids=list(range(5)))
        with self.assertRaisesRegex(OsrmFormatError, "coordinates"):
            read_block(path, "/common/nbn_data/coordinates", (np.int32, 3))

    def test_backward_edge_flag_and_fingerprint_mismatch_raise(self):
        with self.assertRaisesRegex(OsrmFormatError, "backward"):
            read_dataset(self.base_backward)
        with self.assertRaisesRegex(OsrmFormatError, "fingerprint"):
            read_dataset(self.base_mixed)

    def test_enw_delta_breaks_the_turn_identity(self):
        d = read_dataset(town(self.root / "enw", enw_delta={0: 7}))
        self.assertFalse(np.array_equal(d.turn_total_ds, d.ebn_duration_ds[d.turn_src] + d.turn_ds))

    def test_out_of_range_ids_raise_format_errors(self):
        cases = [(".ebg_nodes", "/common/ebg_node_data/nodes", 0, 6, "geometry id"),           # 6 geometries
                 (".geometry", "/common/segment_data/nodes", 0, 7, "node id"),                  # 7 nodes
                 (".geometry", "/common/segment_data/nodes", 0, 1 << 31, "node id"),
                 (".ebg", "/common/edge_based_edge_list", 1, 11, "target"),                     # 11 EBNs
                 (".ebg", "/common/edge_based_edge_list", 0, 11, "source")]
        for k, (suffix, block, index, value, message) in enumerate(cases):
            with self.subTest(message=message, value=value):
                base = town(self.root / f"range{k}")
                poke_block(str(base) + suffix, block, index, value, "<u4")
                with self.assertRaisesRegex(OsrmFormatError, message):
                    read_dataset(base)

    def test_bad_geometry_index_raises(self):
        # TOWN index: 0 4 6 8 10 12 14
        for k, (index, value, message) in enumerate([(2, 3, "monotonic"), (1, 3, "odd"), (0, 2, "start at 0")]):
            with self.subTest(message=message):
                base = town(self.root / f"index{k}")
                poke_block(str(base) + ".geometry", "/common/segment_data/index", index, value, "<u4")
                with self.assertRaisesRegex(OsrmFormatError, message):
                    read_dataset(base)

    def test_version_pinned(self):
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"input": {"osrm_version": "v26.11.0"}}), encoding="utf-8")
        with self.assertRaisesRegex(OsrmFormatError, "v26.10.0"):
            check_build_json(bad)
        good = self.base.parent / "build_matrix.json"
        self.assertEqual(check_build_json(good)["input"]["pbf_sha256"], "fixture")

    @unittest.skipUnless(os.environ.get("SM_REAL_DATA") == "1", "real data only")
    def test_real_dataset_identities(self):
        candidates = []
        current = ROOT / "runtime/matrix/current.json"
        if current.exists():
            doc = json.loads(current.read_text(encoding="utf-8"))
            release = doc.get("release_dir") or doc.get("release") or doc.get("dir")
            if release:
                rel = Path(release)
                candidates.append((rel if rel.is_absolute() else ROOT / rel) / "osrm/data/vietnam-latest.osrm")
        candidates += [ROOT / "runtime/matrix/releases/20261021-1400/osrm/data/vietnam-latest.osrm",
                       ROOT / "osrm/data/vietnam-latest.osrm"]
        base = next((c for c in candidates if Path(str(c) + ".ebg").exists()), None)
        if base is None:
            self.skipTest("no OSRM dataset found")
        d = read_dataset(base)
        self.assertTrue(np.array_equal(d.turn_total_ds, d.ebn_duration_ds[d.turn_src] + d.turn_ds))
        self.assertEqual(d.restricted_edges, 0)
        fd = np.where(d.geo_fwd_ds == INVALID_DURATION, 0, d.geo_fwd_ds).astype(np.int64)
        rd = np.where(d.geo_rev_ds == INVALID_DURATION, 0, d.geo_rev_ds).astype(np.int64)
        cf, cr = np.r_[0, np.cumsum(fd)], np.r_[0, np.cumsum(rd)]
        b, e = d.geo_index[d.ebn_geometry], d.geo_index[d.ebn_geometry + 1]
        fwd_sum = cf[e] - cf[b + 1]                                                  # fd[b+1 .. e-1]
        rev_sum = cr[e - 1] - cr[b]                                                  # rd[b .. e-2]
        np.testing.assert_array_equal(np.where(d.ebn_forward, fwd_sum, rev_sum), d.ebn_duration_ds.astype(np.int64))


if __name__ == "__main__":
    unittest.main()
