"""Offline tests for speedmatrix.gates and the gate part of the partition CLI, on tiny maps from sm_osrm_fixture."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import TOWN_COORDS, TOWN_WAYS, grid, make_graphs
from speedmatrix.gates import (LevelGates, build_gates, check_gates, gate_drift, gate_stats, level_gates, load_gates,
                               save_gates)
from speedmatrix.graph import save_graph
from speedmatrix.partition import Partition, PartitionConfig, build_partition, main

REPO = Path(__file__).resolve().parents[1]
N = 12                     # grid(12): node id r * 12 + c + 1, node index id - 1; columns 0-5 west, 6-11 east
MID = 105.8055
EMPTY = {"type": "FeatureCollection", "features": []}
CFG3 = PartitionConfig(split_nodes=(12,), l2_nodes=40)


def _box(region, x0, x1):
    ring = [[x0, 20.0], [x1, 20.0], [x1, 22.0], [x0, 22.0], [x0, 20.0]]
    return {"type": "Feature", "properties": {"region": region}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


POLY = {"type": "FeatureCollection", "features": [_box(0, 105.0, MID), _box(1, MID, 106.5)]}
BAD_POLY = {"type": "FeatureCollection", "features": POLY["features"] + [_box(2, 110.0, 111.0)]}   # region 2: no node


def node(r, c):
    return r * N + c


def partition_of(g, codes) -> Partition:
    """A Partition whose labels are ``codes`` (int64 (N, k)) as given, no rule applied."""
    cells = [np.unique(codes[:, lv]) for lv in range(codes.shape[1])]
    seg = [np.searchsorted(c, codes[:, lv]).astype(np.uint16)[g.seg_tail] for lv, c in enumerate(cells)]
    return Partition(codes=codes, cells=cells, seg_labels=seg, l2_hash={}, info={}, ids=g.node_osm.copy())


def brute_entries_exits(g, labels, n_cells):
    """Per cell (sorted entry segments, sorted exit segments) by walking every arc in Python."""
    off, toff = g.ebn_offsets.tolist(), g.turn_offsets.tolist()
    arcs = []
    for e in range(g.n_ebn):
        arcs += [(s, s + 1) for s in range(off[e], off[e + 1] - 1)]
        arcs += [(off[e + 1] - 1, off[int(v)]) for v in g.turn_dst[toff[e]:toff[e + 1]]]
    entries, exits = [set() for _ in range(n_cells)], [set() for _ in range(n_cells)]
    for a, b in arcs:
        if labels[a] != labels[b]:
            exits[labels[a]].add(a)
            entries[labels[b]].add(b)
    return [(sorted(en), sorted(ex)) for en, ex in zip(entries, exits)]


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = cls.root = Path(cls.temp.name)
        _, cls.g, _ = make_graphs(root / "town", TOWN_COORDS, TOWN_WAYS)
        cls.labels_101_alone = np.where(cls.g.node_osm[cls.g.seg_tail] == 101, 0, 1).astype(np.uint16)

        coords, ways = grid(N)
        cls.coords, cls.ways = coords, ways
        _, cls.gg, cls.nggrid = make_graphs(root / "grid", coords, ways)
        cls.pgrid = build_partition(cls.gg, cls.nggrid, None, POLY, CFG3, 1)
        lab3 = cls.pgrid.seg_labels[2]
        cls.entries_exits_l3 = brute_entries_exits(cls.gg, lab3.tolist(), len(cls.pgrid.cells[2]))

        # the grid again with one L1 gate via node (5, 6) moved 8 m north and one L2-only gate via node deleted
        gates = build_gates(cls.gg, cls.pgrid)
        v1 = set(gates[0].via_osm.tolist())
        cls.moved_osm = node(5, 6) + 1
        assert cls.moved_osm in v1
        cls.gone_osm = min(set(gates[1].via_osm.tolist()) - v1 - {cls.moved_osm})
        mc = {i: xy for i, xy in coords.items() if i != cls.gone_osm}
        lon, lat = mc[cls.moved_osm]
        mc[cls.moved_osm] = (lon, lat + 72e-6)                              # 8.0 m
        mw = [(w, [i for i in refs if i != cls.gone_osm], t) for w, refs, t in ways]
        _, cls.gg_moved, _ = make_graphs(root / "moved", mc, mw)

        # a ferry from west boundary node (6, 0) over inner node 3002 to landing 3001, linked to east node (6, 11)
        fc = {3001: (105.814, 21.006), 3002: (105.806, 21.0065)}
        fw = [(400, [node(6, 11) + 1, 3001], {"highway": "residential"}),
              (401, [node(6, 0) + 1, 3002, 3001], {"route": "ferry"})]
        _, cls.gf, cls.ngf = make_graphs(root / "ferry", {**coords, **fc}, ways + fw)
        cls.pferry = build_partition(cls.gf, cls.ngf, None, POLY, CFG3, 1)
        codes = cls.pferry.codes.copy()
        idx = {int(o): i for i, o in enumerate(cls.ngf.osm)}
        codes[idx[3002]] = codes[idx[node(6, 11) + 1]]                      # inner ferry node in the east cells
        assert codes[idx[3002], 0] != codes[idx[node(6, 0) + 1], 0]
        cls.pferry_without_rule = partition_of(cls.gf, codes)

        for name, doc in (("poly", POLY), ("bad_poly", BAD_POLY), ("rivers", EMPTY)):
            (root / f"{name}.geojson").write_text(json.dumps(doc), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.rel = Path(tempfile.mkdtemp(dir=self.root))

    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([str(a) for a in argv])
        return code, out.getvalue()

    def _cli_args(self, release, poly="poly", workers=1, extra=()):
        return ["run", "--release", release, "--polygons", self.root / f"{poly}.geojson",
                "--rivers", self.root / "rivers.geojson", "--workers", workers, "--l2-nodes", 40,
                "--split-nodes", 12, *extra]

    # ---- the brief's tests -------------------------------------------------------------------------------------------

    def test_chain_gate_inside_a_geometry_and_turn_gate(self):
        # TOWN, one level: node 101 in cell 0, every other node in cell 1
        lg = level_gates(self.g, self.labels_101_alone, 2)
        got = {(int(k), int(self.g.node_osm[v])) for k, v in zip(lg.kind, lg.via)}
        self.assertEqual(got, {(0, 107), (1, 101)})   # 101->107 then 107->102 leaves cell 0; U-turn at 101 enters it
        entries = lg.entries[lg.entry_offsets[0]:lg.entry_offsets[1]]
        exits = lg.exits[lg.exit_offsets[0]:lg.exit_offsets[1]]
        self.assertEqual(entries.tolist(), exits.tolist())                  # both are the single segment 101->107
        s = int(entries[0])
        self.assertEqual((int(self.g.node_osm[self.g.seg_tail[s]]), int(self.g.node_osm[self.g.seg_head[s]])),
                         (101, 107))
        self.assertEqual(lg.via_osm.tolist(), self.g.node_osm[lg.via].tolist())
        self.assertEqual(lg.via_lat_e6.tolist(), self.g.node_lat_e6[lg.via].tolist())
        self.assertTrue((lg.cell_out != lg.cell_in).all())
        self.assertEqual((lg.src_seg.dtype, lg.kind.dtype, lg.via.dtype, lg.via_osm.dtype, lg.cell_in.dtype,
                          lg.entry_offsets.dtype, lg.entries.dtype),
                         (np.int32, np.int8, np.int32, np.int64, np.uint16, np.int64, np.int32))

    def test_coarse_gates_are_fine_gates(self):
        gates = build_gates(self.gg, self.pgrid)
        self.assertEqual(len(gates), 3)
        self.assertTrue(check_gates(self.gg, self.nggrid, gates)["gates_nested"]["ok"])
        self.assertTrue(all(c["blocking"] for c in check_gates(self.gg, self.nggrid, gates).values()))
        self.assertLess(len(gates[0].src_seg), len(gates[1].src_seg))
        # levels 1 and 2 swapped: the L2 arcs that are no L1 arc break the nesting
        c = check_gates(self.gg, self.nggrid, [gates[1], gates[0], gates[2]])["gates_nested"]
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], len(gates[1].src_seg) - len(gates[0].src_seg))

    def test_ferry_gates_only_at_landings(self):
        gates = build_gates(self.gf, self.pferry)
        self.assertTrue(check_gates(self.gf, self.ngf, gates)["ferry_gates_at_landings"]["ok"])
        bad = build_gates(self.gf, self.pferry_without_rule)        # labels written by hand: inner ferry node elsewhere
        c = check_gates(self.gf, self.ngf, bad)["ferry_gates_at_landings"]
        self.assertFalse(c["ok"])
        self.assertGreater(c["value"], 0)

    def test_stats_and_round_trip(self):
        gates = build_gates(self.gg, self.pgrid)
        s = gate_stats(self.gg, gates)
        self.assertEqual(s["l3"]["piece_cells"], sum(len(e) * len(x) for e, x in self.entries_exits_l3))
        self.assertEqual(s["l3"]["cells"], len(self.pgrid.cells[2]))
        self.assertEqual(s["l3"]["gate_arcs"], len(gates[2].src_seg))
        self.assertEqual(s["l3"]["via_nodes"], len(np.unique(gates[2].via)))
        self.assertEqual(s["l3"]["entries_per_cell"]["max"], max(len(e) for e, _ in self.entries_exits_l3))
        lg = gates[2]
        got = [(lg.entries[lg.entry_offsets[c]:lg.entry_offsets[c + 1]].tolist(),
                lg.exits[lg.exit_offsets[c]:lg.exit_offsets[c + 1]].tolist()) for c in range(len(self.pgrid.cells[2]))]
        self.assertEqual(got, self.entries_exits_l3)
        save_gates(gates, self.rel / "gates.npz")
        again = load_gates(self.rel / "gates.npz")
        self.assertEqual(len(again), 3)
        np.testing.assert_array_equal(again[2].dst_seg, gates[2].dst_seg)
        for a, b in zip(again, gates):
            for f in LevelGates.__dataclass_fields__:
                x, y = getattr(a, f), getattr(b, f)
                np.testing.assert_array_equal(x, y)
                self.assertEqual(x.dtype, y.dtype)
        with np.load(self.rel / "gates.npz") as z:
            self.assertIn("l1_via_osm", z.files)

    def test_partition_cli_end_to_end(self):
        r = self.rel / "r1"
        save_graph(self.gg, r / "graph")
        code, out = self.run_cli(self._cli_args(r, workers=2))
        self.assertEqual(code, 0)
        self.assertIn(str(r), out)
        doc = json.loads((r / "partition.json").read_text(encoding="utf-8"))
        self.assertTrue(all(v["ok"] for v in doc["checks"].values() if v["blocking"]), doc["checks"])
        self.assertIn("gates_nested", doc["checks"])
        self.assertIn("ferry_gates_at_landings", doc["checks"])
        self.assertEqual(np.load(r / "labels_l3.npy").dtype, np.uint16)
        self.assertTrue((r / "gates.npz").is_file())
        self.assertEqual(doc["k"], 3)
        self.assertEqual(doc["gate_stats"]["l3"]["cells"], len(doc["cells"][2]))
        self.assertNotIn("gate_drift_l1", doc["checks"])

    def test_cli_previous_release_adds_gate_drift_warnings(self):
        r1, r2, r3 = self.rel / "r1", self.rel / "r2", self.rel / "r3"
        save_graph(self.gg, r1 / "graph")
        save_graph(self.gg, r2 / "graph")
        save_graph(self.gg_moved, r3 / "graph")
        self.assertEqual(self.run_cli(self._cli_args(r1))[0], 0)
        self.assertEqual(self.run_cli(self._cli_args(r2, extra=["--previous", r1]))[0], 0)
        checks = json.loads((r2 / "partition.json").read_text(encoding="utf-8"))["checks"]
        for lv in (1, 2):
            c = checks[f"gate_drift_l{lv}"]
            self.assertEqual((c["value"], c["ok"], c["blocking"], c["gone"]), (0, True, False, 0))
        self.assertEqual(self.run_cli(self._cli_args(r3, extra=["--previous", r1]))[0], 0)
        checks = json.loads((r3 / "partition.json").read_text(encoding="utf-8"))["checks"]
        self.assertEqual((checks["gate_drift_l1"]["value"], checks["gate_drift_l1"]["ok"],
                          checks["gate_drift_l1"]["blocking"]), (1, False, False))
        self.assertEqual(checks["gate_drift_l2"]["gone"], 1)

    def test_cli_exits_0_with_a_failing_check(self):
        # Ruling T7-b: no previous release, a third polygon region holding no node -> 2 L1 cells against 3 expected
        r = self.rel / "r"
        save_graph(self.gg, r / "graph")
        code, out = self.run_cli(self._cli_args(r, poly="bad_poly"))
        self.assertEqual(code, 0)
        self.assertIn(str(r), out)
        c = json.loads((r / "partition.json").read_text(encoding="utf-8"))["checks"]["l1_cells_unchanged"]
        self.assertEqual((c["value"], c["expected"], c["ok"], c["blocking"]), (2, 3, False, True))

    def test_cli_crash_exits_non_zero(self):
        r = self.rel / "no_graph_here"                                       # no graph/ dir: the run crashes
        done = subprocess.run([sys.executable, "-m", "speedmatrix.partition",
                               *[str(a) for a in self._cli_args(r)]], cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse((r / "partition.json").exists())

    def test_gate_drift_counts_moved_and_gone(self):
        old = build_gates(self.gg, self.pgrid)
        d = gate_drift(old, self.gg_moved)            # one L1 gate via node shifted 8 m, one L2 gate via node deleted
        self.assertEqual((d["l1"]["moved"], d["l2"]["gone"]), (1, 1))
        self.assertEqual((d["l1"]["gone"], d["l2"]["moved"]), (0, 1))
        self.assertEqual(d["l1"]["checked"], len(np.unique(old[0].via_osm)))
        ex = {e["osm"]: e for e in d["l2"]["examples"]}
        self.assertAlmostEqual(ex[self.moved_osm]["metres"], 8.0, delta=0.1)
        self.assertIsNone(ex[self.gone_osm]["new"])
        self.assertEqual(gate_drift(old, self.gg)["l1"]["moved"], 0)
        self.assertEqual(sorted(gate_drift(old, self.gg)), ["l1", "l2"])
        self.assertEqual(gate_drift(old, self.gg_moved, tol_m=10.0)["l1"]["moved"], 0)


if __name__ == "__main__":
    unittest.main()
