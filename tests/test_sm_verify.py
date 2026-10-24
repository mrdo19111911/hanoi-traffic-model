"""Offline tests for speedmatrix.verify_osrm (spec §12.1) on fixture datasets; OSRM itself is replaced by fakes."""
import contextlib
import dataclasses
import io
import json
import math
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import (FINGERPRINT, KMH, TOWN_BANNED, TOWN_COORDS, TOWN_OBSTACLES, TOWN_WAYS, _packed_blocks,
                             _plain, _tar, grid, write_dataset, write_osm)
from speedmatrix import verify_osrm
from speedmatrix.graph import build_graph, save_graph, segment_ds
from speedmatrix.osrmfiles import read_block, read_datasources, read_packed
from speedmatrix.segments import _sha256_file, build_segments, save_segments
from speedmatrix.speeds import FreeFlowSource, ReleaseMismatch, build_speed_layer, save_layer
from speedmatrix.staticroute import StaticRouter
from speedmatrix.sysres import StepResult
from speedmatrix.verify_osrm import (OsrmClient, Routed, compare_tables, copy_dataset, datasource_coverage,
                                     sample_points, seg_kmh_from_layer, segment_agreement, verify_release,
                                     write_speed_file)

GEO = "/common/segment_data/"


def make(root, name, coords, ways, obstacles={}, banned=set()):
    """(table, graph, base) of a tiny map, the graph built from a fixture dataset in ``root/name``."""
    root.mkdir(parents=True, exist_ok=True)
    osm = root / f"{name}.osm"
    write_osm(osm, coords, ways)
    table = build_segments(osm, None)
    base = write_dataset(root / name, coords, ways, obstacles, banned, pbf_sha256=_sha256_file(osm))
    return table, build_graph(base, base.parent / "build_matrix.json", table), base


def seg_of(g, a, b):
    hit = np.flatnonzero((g.node_osm[g.seg_tail] == a) & (g.node_osm[g.seg_head] == b))
    return int(hit[0])


def _haversine_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def fake_customize(tools, base, speed_file, threads, **kw):
    """What osrm-customize --segment-speed-file does to ``.geometry``: every real-segment entry whose directed node
    pair is in the file gets max(1, round(36 * length / kmh)) ds and data source 1; nothing else changes."""
    speeds = {}
    for line in Path(speed_file).read_text(encoding="utf-8").splitlines():
        a, b, k = line.split(",")
        speeds[(int(a), int(b))] = int(k)
    geo, nbg = str(base) + ".geometry", str(base) + ".nbg_nodes"
    index = read_block(geo, GEO + "index", "<u4")
    nodes = read_block(geo, GEO + "nodes", "<u4")
    fd = read_packed(geo, GEO + "forward_durations", 22).astype(np.int64)
    rd = read_packed(geo, GEO + "reverse_durations", 22).astype(np.int64)
    fs, rs = read_datasources(base)
    fs, rs = fs.copy(), rs.copy()
    osm = read_packed(nbg, "/common/nbn_data/osm_node_ids", 40).astype(np.int64)
    xy = read_block(nbg, "/common/nbn_data/coordinates", (np.int32, 2)) / 1e6
    for start, end in zip(index[:-1], index[1:]):
        for e in range(int(start), int(end), 2):          # real segment between nodes[e] and nodes[e + 1]
            u, v = int(nodes[e]), int(nodes[e + 1])
            length = _haversine_m(xy[u, 0], xy[u, 1], xy[v, 0], xy[v, 1])
            for pair, durations, sources, at in (((osm[u], osm[v]), fd, fs, e + 1), ((osm[v], osm[u]), rd, rs, e)):
                if pair in speeds:
                    durations[at] = max(1, round(36 * length / speeds[pair]))
                    sources[at] = 1
    blocks = {**_plain(GEO + "index", index), **_plain(GEO + "nodes", nodes),
              **_packed_blocks(GEO + "forward_durations", fd.tolist(), 22),
              **_packed_blocks(GEO + "reverse_durations", rd.tolist(), 22),
              **_plain(GEO + "forward_data_sources", fs), **_plain(GEO + "reverse_data_sources", rs)}
    _tar(geo, blocks, FINGERPRINT)
    return StepResult(0, 0.0, None, "")


class VerifyUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = root = Path(cls.temp.name)
        cls.table, cls.g, cls.base = make(root / "town", "town", TOWN_COORDS, TOWN_WAYS, TOWN_OBSTACLES, TOWN_BANNED)
        cls.kmh = np.array([KMH[h] for h in cls.g.meta["highways"]], dtype=np.float32)[cls.g.seg_highway]
        cls.tools = root / "tools"
        cls.layer = build_speed_layer(cls.table, FreeFlowSource(), [], None)
        save_segments(cls.table, root / "segments")
        cls.report = {"segments_sha256": cls.table.sha256, "segments_dir": str(root / "segments")}
        # mixed map: the town plus a motorway, a 15 m stub and an island; one EBN made tiny, two made duplicates
        coords = {**TOWN_COORDS, 301: (105.8030, 21.0000), 302: (105.8020 + 15 / 103_930, 20.9990),
                  201: (105.8100, 21.0100), 202: (105.8110, 21.0100)}
        ways = TOWN_WAYS + [(5, [103, 301], {"highway": "motorway", "name": "Cao tốc"}),
                            (6, [106, 302], {"highway": "residential", "name": "Ngõ"}),
                            (7, [201, 202], {"highway": "residential", "name": "Đảo"})]
        _, g, _ = make(root / "mixed", "mixed", coords, ways, TOWN_OBSTACLES)
        ebn = g.seg_ebn
        tiny_ebn = int(ebn[seg_of(g, 105, 106)])
        dup_a, dup_b = int(ebn[seg_of(g, 104, 102)]), int(ebn[seg_of(g, 103, 106)])
        ebn_tiny = g.ebn_tiny.copy()
        ebn_tiny[tiny_ebn] = True
        geometry, forward = g.ebn_geometry.copy(), g.ebn_forward.copy()
        geometry[dup_b], forward[dup_b] = geometry[dup_a], forward[dup_a]
        cls.g_mixed = gm = dataclasses.replace(g, ebn_tiny=ebn_tiny, ebn_geometry=geometry, ebn_forward=forward)
        pairs = {(103, 301), (301, 103), (106, 302), (302, 106), (201, 202), (202, 201), (101, 107)}
        excluded = {seg_of(gm, a, b) for a, b in pairs}
        excluded |= set(np.flatnonzero(np.isin(ebn, [tiny_ebn, dup_a, dup_b])).tolist())
        cls.excluded = excluded
        cls.n_eligible = gm.n_segments - len(excluded)
        cls.ebn0_segments = np.flatnonzero(ebn == 0).tolist()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_speed_file_rows(self):
        n = write_speed_file(self.g, self.kmh, self.root / "s.csv")
        lines = (self.root / "s.csv").read_text().splitlines()
        self.assertEqual(n, 13)
        self.assertEqual(lines[0], "101,107,25")
        self.assertEqual(len(set(lines)), 13)
        keys = [tuple(map(int, line.split(",")[:2])) for line in lines]
        self.assertEqual(keys, sorted(keys))
        self.assertIn("105,106,25", lines)
        self.assertNotIn("106,105,25", lines)           # the one-way street has no reverse row

    def test_speed_file_duplicate_pair_takes_the_lowest_speed(self):
        g = self.g
        s = seg_of(g, 102, 103)
        tail, head = g.seg_tail.copy(), g.seg_head.copy()
        t = seg_of(g, 104, 102)                          # make this segment a second copy of 102 -> 103
        tail[t], head[t] = tail[s], head[s]
        g2 = dataclasses.replace(g, seg_tail=tail, seg_head=head)
        kmh = self.kmh.copy()
        kmh[t] = 7
        self.assertEqual(write_speed_file(g2, kmh, self.root / "d.csv"), 12)
        self.assertIn("102,103,7", (self.root / "d.csv").read_text().splitlines())
        self.assertEqual(verify_osrm._write_speed_rows(g2, kmh, self.root / "d.csv"), (12, 1))
        self.assertEqual(verify_osrm._write_speed_rows(self.g, self.kmh, self.root / "d.csv"), (13, 0))

    def test_speed_file_refuses_non_positive_speed(self):
        kmh = self.kmh.copy()
        kmh[0] = 0
        with self.assertRaises(ValueError):
            write_speed_file(self.g, kmh, self.root / "z.csv")

    def test_protected_and_busy_ports_refused(self):
        for port in (5000, 8000):
            with self.assertRaises(ValueError):
                Routed(self.base, port, self.tools).__enter__()
            with self.assertRaises(ValueError):
                OsrmClient(port)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            busy = s.getsockname()[1]
            with mock.patch("subprocess.Popen", side_effect=AssertionError("must not start")):
                with self.assertRaisesRegex(RuntimeError, "in use"):
                    Routed(self.base, busy, self.tools).__enter__()

    def test_routed_stops_only_its_own_process(self):
        class Proc:
            pid = 4242
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                Proc.terminated = True

            def wait(self, timeout=None):
                return 0

        identities = iter([{"executable": "osrm-routed.exe", "created": 1},
                           {"executable": "other.exe", "created": 2}])
        with mock.patch("subprocess.Popen", return_value=Proc()), \
                mock.patch.object(verify_osrm, "_free_port", return_value=None), \
                mock.patch.object(verify_osrm.manage, "process_identity", side_effect=lambda pid: next(identities)), \
                mock.patch.object(verify_osrm, "_answers", return_value=True):
            with Routed(self.base, 5058, self.tools, log_path=self.root / "routed.log"):
                pass
        self.assertFalse(Proc.terminated)                # the pid now belongs to another program

    def test_sample_points_rules(self):
        pts = sample_points(self.g_mixed, 50, 12)
        self.assertTrue(set(pts.tolist()).isdisjoint(self.excluded))
        np.testing.assert_array_equal(pts, sample_points(self.g_mixed, 50, 12))
        self.assertEqual(len(pts), self.n_eligible)
        self.assertEqual(pts.dtype, np.int64)
        self.assertEqual(len(sample_points(self.g_mixed, 3, 12)), 3)
        np.testing.assert_array_equal(sample_points(self.g_mixed, 3, 12), pts[:3])

    def test_sample_points_mask(self):
        mask = np.zeros(self.g_mixed.n_segments, bool)
        mask[self.ebn0_segments] = True
        got = set(sample_points(self.g_mixed, 50, 12, mask).tolist())
        self.assertTrue(got <= set(self.ebn0_segments))
        self.assertEqual(len(got), 1)                    # 101 -> 107 carries the folded signal
        with self.assertRaises(ValueError):
            sample_points(self.g_mixed, 50, 12, mask[:-1])

    def test_seg_kmh_refuses_another_segment_table(self):
        with self.assertRaises(ReleaseMismatch):
            seg_kmh_from_layer(self.g, self.layer, {"segments_sha256": "other"})
        with self.assertRaises(ReleaseMismatch):
            seg_kmh_from_layer(self.g, dataclasses.replace(self.layer, n=self.layer.n + 1), self.report)
        with self.assertRaises(ReleaseMismatch):                # the row count cannot be confirmed
            seg_kmh_from_layer(self.g, self.layer, {"segments_sha256": self.table.sha256})

    def test_seg_kmh_is_the_sample_slot(self):
        kmh = seg_kmh_from_layer(self.g, self.layer, self.report)
        self.assertEqual(kmh.dtype, np.float32)
        np.testing.assert_array_equal(kmh, self.layer.at("weekday", 16)[self.g.seg_row])

    def test_compare_tolerance(self):
        r = compare_tables(np.array([[501.9, 3607.1]]), np.array([[500.0, 3600.0]]))
        self.assertEqual(r["over"], 0)
        r = compare_tables(np.array([[502.1, 3607.3]]), np.array([[500.0, 3600.0]]))
        self.assertEqual(r["over"], 2)
        self.assertEqual(r["pairs"], 2)
        self.assertEqual(len(r["examples"]), 2)
        self.assertEqual(r["examples"][0], {"i": 0, "j": 1, "ours": 3607.3, "osrm": 3600.0})

    def test_compare_skips_nan_and_counts_unreachable_mismatch(self):
        ours = np.array([[np.nan, 10.0, np.inf, np.inf]])
        theirs = np.array([[0.0, 10.5, np.inf, 30.0]])
        r = compare_tables(ours, theirs)
        self.assertEqual((r["pairs"], r["over"], r["unreachable_mismatch"]), (1, 0, 1))

    def sources(self):
        """Data sources of the town dataset as osrm-customize leaves them: 1 on every real-segment entry except
        the reverse of the one-way street; node entries stay 0."""
        index = read_block(str(self.base) + ".geometry", GEO + "index", "<u4")
        nodes = read_block(str(self.base) + ".geometry", GEO + "nodes", "<u4")
        osm = read_packed(str(self.base) + ".nbg_nodes", "/common/nbn_data/osm_node_ids", 40)
        fwd, rev = np.zeros(len(nodes), np.uint8), np.zeros(len(nodes), np.uint8)
        entry = None
        for start, end in zip(index[:-1], index[1:]):
            for e in range(int(start), int(end), 2):
                fwd[e + 1] = 1
                if (osm[nodes[e]], osm[nodes[e + 1]]) == (105, 106):
                    continue
                rev[e] = 1
                if (osm[nodes[e]], osm[nodes[e + 1]]) == (101, 107):
                    entry = e + 1
        return fwd, rev, entry

    def test_datasource_coverage_counts_graph_directions_only(self):
        fwd, rev, entry_of_101_107 = self.sources()
        self.assertEqual(datasource_coverage(self.g, fwd, rev)["not_updated"], 0)
        fwd[entry_of_101_107] = 0
        r = datasource_coverage(self.g, fwd, rev)
        self.assertEqual((r["segments"], r["not_updated"]), (13, 1))
        self.assertEqual(r["examples"][0][:2], [101, 107])

    def test_segment_agreement(self):
        root = self.root / "agree"
        copy_dataset(self.base.parent, root)
        base = root / self.base.name
        write_speed_file(self.g, self.kmh, root / "s.csv")
        fake_customize(self.tools, base, root / "s.csv", 1)
        r = segment_agreement(self.g, self.kmh, base)
        self.assertEqual((r["share_within"], r["max_abs_ds"]), (1.0, 0))
        kmh = self.kmh.copy()
        kmh[seg_of(self.g, 102, 103)] *= 2
        r = segment_agreement(self.g, kmh, base)
        self.assertAlmostEqual(r["share_within"], 12 / 13)

    def test_copy_dataset(self):
        dst = self.root / "copy"
        copy_dataset(self.base.parent, dst)
        names = sorted(p.name for p in dst.iterdir())
        self.assertEqual(names, sorted(p.name for p in self.base.parent.glob("map.osrm.*")))
        self.assertNotIn("build_matrix.json", names)
        with mock.patch.object(verify_osrm, "free_disk_gb", return_value=1.0):
            with self.assertRaisesRegex(RuntimeError, "free"):
                copy_dataset(self.base.parent, self.root / "copy2")
        self.assertFalse((self.root / "copy2").exists())


class VerifyFlowTests(unittest.TestCase):
    """verify_release with osrm-customize, osrm-routed and the HTTP client replaced by fakes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        coords, ways = grid(6)
        self.release = root / "release"
        osm = root / "map.osm"
        write_osm(osm, coords, ways)
        table = build_segments(osm, None)
        base = write_dataset(self.release / "osrm" / "data", coords, ways, pbf_sha256=_sha256_file(osm))
        self.g = build_graph(base, base.parent / "build_matrix.json", table)
        save_graph(self.g, self.release / "graph")
        save_segments(table, root / "segments")
        self.speeds = root / "speeds"
        layer = build_speed_layer(table, FreeFlowSource(), [], None)
        save_layer(layer, self.speeds)
        (self.speeds / "report.json").write_text(json.dumps(
            {"segments_sha256": table.sha256, "segments_dir": str(root / "segments")}), encoding="utf-8")
        self.kmh = layer.at("weekday", 16)[self.g.seg_row].astype(np.float64)
        self.tools = root / "tools"
        self.events = []

    def tearDown(self):
        self.temp.cleanup()

    def fakes(self):
        test, g = self, self.g
        mid = np.stack([(g.node_lon_e6[g.seg_tail] + g.node_lon_e6[g.seg_head]) / 2e6,
                        (g.node_lat_e6[g.seg_tail] + g.node_lat_e6[g.seg_head]) / 2e6], axis=1)

        class FakeRouted:
            def __init__(self, base, port, tools, **kw):
                self.base = base

            def __enter__(self):
                assert Path(str(self.base) + ".geometry").is_file()
                test.events.append("routed_enter")
                return self

            def __exit__(self, *exc):
                test.events.append("routed_exit")

        class FakeClient:
            def __init__(self, port):
                pass

            def table(self, lonlat):
                segs = [int(np.argmin(np.abs(mid - p).sum(axis=1))) for p in lonlat]
                ours = StaticRouter(g, segment_ds(g, test.kmh)).table(segs, segs)
                out = np.nan_to_num(ours, nan=0.0) + 1.0
                i, j = np.argwhere(np.isfinite(ours) & (ours > 0))[0]
                out[i, j] += 30.0
                return out, np.array(lonlat, dtype=np.float64)

            def nearest(self, lon, lat, number=2):
                return [(lon, lat, 0.0)]

        real_parallel = verify_osrm.parallel_table

        def fake_parallel(graph_dir, seg_ds_file, src, dst, workers):
            assert test.events[-1] == "routed_exit", test.events
            test.events.append("parallel_table")
            test.seg_ds = np.load(seg_ds_file)
            return real_parallel(graph_dir, seg_ds_file, src, dst, 1)

        return [mock.patch.object(verify_osrm, "Routed", FakeRouted),
                mock.patch.object(verify_osrm, "OsrmClient", FakeClient),
                mock.patch.object(verify_osrm, "customize_copy", fake_customize),
                mock.patch.object(verify_osrm, "parallel_table", fake_parallel)]

    def run_with_fakes(self, fn, *args, **kw):
        patches = self.fakes()
        for p in patches:
            p.start()
        try:
            return fn(*args, **kw)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_verify_flow_with_fakes(self):
        checks = self.run_with_fakes(verify_release, self.release, self.speeds, self.tools, 5058, 1)
        self.assertFalse(checks["osrm_table_over_tolerance"]["ok"])
        self.assertEqual(checks["osrm_table_over_tolerance"]["value"], 1)
        self.assertEqual(self.events, ["routed_enter", "routed_exit", "parallel_table"])
        self.assertFalse((self.release / "verify").exists())
        for name in ("osrm_table_pairs", "osrm_datasource_not_updated", "osrm_segment_agreement"):
            self.assertTrue(checks[name]["ok"], (name, checks[name]))
            self.assertTrue(checks[name]["blocking"])
        self.assertEqual(checks["osrm_datasource_not_updated"]["value"], 0)
        self.assertEqual(checks["osrm_segment_agreement"]["value"], 1.0)
        pairs = checks["osrm_table_pairs"]
        p = pairs["points"]
        self.assertEqual(pairs["planned"], p * (p - 1))
        self.assertEqual(json.loads((self.release / "verify.json").read_text(encoding="utf-8")), checks)
        # the router ran on OSRM's integer customized durations, not on 36 * len / kmh
        np.testing.assert_array_equal(self.seg_ds, np.round(self.seg_ds))
        np.testing.assert_allclose(self.seg_ds, segment_ds(self.g, self.kmh), atol=0.5 + 1e-6)
        self.assertFalse(np.array_equal(self.seg_ds, segment_ds(self.g, self.kmh)))

    def test_points_and_mask(self):
        mask = np.zeros(self.g.n_segments, bool)
        mask[:40] = True
        checks = self.run_with_fakes(verify_release, self.release, self.speeds, self.tools, 5058, 1,
                                     points=10, mask=mask)
        pairs = checks["osrm_table_pairs"]
        self.assertEqual((pairs["points"], pairs["shortfall"]), (10, 0))
        self.assertTrue(set(pairs["segments"]) <= set(range(40)))

    def test_far_snaps_and_ambiguous_points_are_replaced(self):
        g = self.g
        first = sample_points(g, 20, 12)

        def mid(s):
            return np.array([(g.node_lon_e6[g.seg_tail[s]] + g.node_lon_e6[g.seg_head[s]]) / 2e6,
                             (g.node_lat_e6[g.seg_tail[s]] + g.node_lat_e6[g.seg_head[s]]) / 2e6])

        patches = self.fakes()
        for p in patches:
            p.start()
        try:
            class Shifted(verify_osrm.OsrmClient):
                def table(self, lonlat):
                    out, loc = super().table(lonlat)
                    hit = np.flatnonzero(np.abs(np.asarray(lonlat) - mid(first[0])).sum(axis=1) < 1e-9)
                    loc[hit, 1] += 2e-5                   # about 2 m: the first candidate snaps elsewhere
                    return out, loc

                def nearest(self, lon, lat, number=2):
                    if np.abs(np.array([lon, lat]) - mid(first[1])).sum() < 1e-9:
                        return [(lon, lat, 0.0), (lon, lat, 0.5)]
                    return [(lon, lat, 0.0)]

            with mock.patch.object(verify_osrm, "OsrmClient", Shifted):
                checks = verify_release(self.release, self.speeds, self.tools, 5058, 1, points=5)
        finally:
            for p in reversed(patches):
                p.stop()
        pairs = checks["osrm_table_pairs"]
        self.assertEqual(pairs["points"], 5)
        self.assertGreaterEqual(pairs["dropped_ambiguous"], 1)
        self.assertGreaterEqual(pairs["dropped_snap"], 1)
        self.assertNotIn(int(first[0]), pairs["segments"])
        self.assertNotIn(int(first[1]), pairs["segments"])

    def test_cli_exits_0_on_failed_checks(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.run_with_fakes(verify_osrm.main, ["--release", str(self.release), "--speeds",
                                                          str(self.speeds), "--workers", "1"])
        self.assertEqual((code, out.getvalue().strip()), (0, str(self.release / "verify.json")))
        self.assertFalse(json.loads((self.release / "verify.json").read_text())["osrm_table_over_tolerance"]["ok"])

    def test_crash_removes_the_work_dir(self):
        def broken(*a, **kw):
            raise RuntimeError("customize broke")

        patches = self.fakes()
        for p in patches:
            p.start()
        try:
            with mock.patch.object(verify_osrm, "customize_copy", broken):
                with self.assertRaisesRegex(RuntimeError, "customize broke"):
                    verify_release(self.release, self.speeds, self.tools, 5058, 1)
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertFalse((self.release / "verify").exists())
        self.assertFalse((self.release / "verify.json").exists())


if __name__ == "__main__":
    unittest.main()
