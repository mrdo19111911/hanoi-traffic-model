"""Offline tests for the speedmatrix build CLI; main(argv) runs in-process on a tiny .osm fixture."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from speedmatrix.build import main, today
from speedmatrix.groups import load_group_view
from speedmatrix.segments import find_segments, load_segments
from speedmatrix.speeds import load_layer
from speedmatrix.state import open_state

NODES = {11: (21.001, 105.801), 12: (21.001, 105.802), 13: (21.001, 105.803),
         21: (21.002, 105.801), 22: (21.002, 105.802),
         31: (21.004, 105.801), 32: (21.004, 105.805)}
WAYS = [(1, [11, 12, 13], {"highway": "primary", "name": "Phố Chính"}),
        (2, [21, 22], {"highway": "residential", "name": "Ngõ Nhỏ"}),
        (3, [31, 32], {"highway": "motorway", "name": "Cao tốc Thử", "ref": "CT01"})]
BBOX = [105.79, 20.99, 105.81, 21.01]
GROUPS = [{"name": "duoi2t", "gross_t": 1.9, "payload_t": 0.99, "axles": 2, "kind": "normal", "height_m": 2.6,
           "length_m": 5.0, "is_base": True},
          {"name": "tu2t", "gross_t": 8.0, "payload_t": 5.0, "axles": 2, "kind": "normal", "height_m": 3.4,
           "length_m": 7.5, "is_base": False},
          {"name": "container", "gross_t": 40.0, "payload_t": 27.0, "axles": 5, "kind": "container",
           "height_m": 4.2, "length_m": 16.5, "is_base": False}]
SEED_DOC = {
    "area": "test",
    "groups": GROUPS,
    "holidays": [{"day": "2026-01-01", "name": "Tết Dương lịch"}],
    "rules": [
        {"id": "hn_test", "kind": "ban", "confidence": "cao",
         "body": {"vehicle": {"gross_t": [">=", 10]}, "action": "close",
                  "when": {"daytypes": ["weekday"], "times": [["06:00", "09:00"]]},
                  "scope": {"names": ["Ngõ Nhỏ"], "bbox": BBOX}}},
        {"id": "dated_ban", "kind": "ban", "confidence": "cao", "valid_from": "2026-01-01",
         "valid_to": "2026-01-31",
         "body": {"vehicle": {"gross_t": [">=", 2]}, "action": "close", "scope": {"names": ["Phố Chính"], "bbox": BBOX}}},
        {"id": "orphan_rule", "kind": "speed_set", "confidence": "tb",
         "body": {"kmh": 30, "scope": {"segments": [[999, 1, 2]]}}},
        {"id": "cap_heavy", "kind": "cap", "confidence": "cao",
         "body": {"vehicle": {"gross_t": [">=", 2]},
                  "road_classes": {"caotoc": 80, "quoclo": 70, "chinh": 60, "noido": 40, "nho": 30}}},
    ],
    "unresolved": [{"place": "Phố Chính", "what": "giờ cấm chưa rõ", "why": "chưa có văn bản"}],
}
ZONE_DOC = {
    "rules": [
        {"id": "zone_missing_road", "kind": "ban", "confidence": "cao",
         "body": {"vehicle": {"gross_t": [">=", 2]}, "action": "close",
                  "scope": {"zone": {"boundary_names": ["Đường Không Có Thật"], "seed": [21.0015, 105.8015],
                                     "area_km2": [0.1, 10]}, "bbox": BBOX}}},
    ],
}

# a "thap" zone rule (inactive by default) whose single boundary road cannot enclose the seed
LEAKY_ZONE_DOC = {
    "rules": [
        {"id": "leaky_zone", "kind": "ban", "confidence": "thap",
         "body": {"vehicle": {"gross_t": [">=", 2]}, "action": "close",
                  "scope": {"zone": {"boundary_names": ["Phố Chính"], "seed": [21.003, 105.802],
                                     "area_km2": [0.1, 10]}, "bbox": BBOX}}},
    ],
}


def write_osm(path, nodes, ways):
    """A tiny .osm XML file (same layout as tests/test_sm_segments.py)."""
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6'>"]
    for i, (lat, lon) in nodes.items():
        lines.append(f"<node id='{i}' version='1' lat='{lat}' lon='{lon}'></node>")
    for way_id, refs, tags in ways:
        lines.append(f"<way id='{way_id}' version='1'>")
        lines += [f"<nd ref='{n}'/>" for n in refs]
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</way>")
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_json(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def run(argv):
    """main(argv) with stdout and stderr captured: (exit code, stderr text + stdout text)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(a) for a in argv])
    return code, err.getvalue() + out.getvalue()      # stdout last: its final line is the printed path


def last_line(text):
    return text.strip().splitlines()[-1].strip()


class BuildCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.osm = cls.base / "tiny.osm"
        write_osm(cls.osm, NODES, WAYS)
        cls.seed = write_json(cls.base / "seed_test.json", SEED_DOC)
        cls.zone_seed = write_json(cls.base / "zone_seed.json", ZONE_DOC)
        cls.R = cls.base / "root"
        code, out = run(["--root", cls.R, "segments", "--pbf", cls.osm])
        assert code == 0, out
        cls.SEG = Path(last_line(out))
        assert run(["--root", cls.R, "import-rules", cls.seed])[0] == 0
        code, out = run(["--root", cls.R, "speeds", "--segments", cls.SEG])
        assert code == 0, out
        cls.SPD = Path(last_line(out))
        table = load_segments(cls.SEG)
        cls.PRIMARY = int(find_segments(table, [[1, 11, 12]])[0])

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def fresh_root(self):
        root = Path(tempfile.mkdtemp(dir=self.base))
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_end_to_end_and_idempotent(self):
        R = self.fresh_root()
        code, out = run(["--root", R, "segments", "--pbf", self.osm])
        self.assertEqual(code, 0, out)
        SEG = Path(last_line(out))
        self.assertEqual(SEG.parent, R / "segments")
        for name in ("segments.npz", "meta.json", "admin.json", "water.npz"):
            self.assertTrue((SEG / name).is_file(), name)
        self.assertEqual(run(["--root", R, "import-rules", self.seed])[0], 0)
        self.assertEqual(run(["--root", R, "import-rules", self.seed])[0], 0)
        state = open_state(R / "state.sqlite")
        try:
            self.assertEqual(len(state.history("hn_test")), 1)
            self.assertEqual(state.history("hn_test")[0]["author"], "seed")
            self.assertEqual(state.history("hn_test")[0]["reason"], "seed_test.json")
        finally:
            state.close()
        code, out = run(["--root", R, "speeds", "--segments", SEG])
        self.assertEqual(code, 0, out)
        SPD = Path(last_line(out))
        self.assertEqual(SPD.parent, R / "speeds")
        layer = load_layer(SPD)
        self.assertEqual(SPD.name, layer.version[:12])
        code, out = run(["--root", R, "groups", "--segments", SEG, "--speeds", SPD])
        self.assertEqual(code, 0, out)
        TODAY = today().isoformat()
        for name in ("duoi2t", "tu2t", "container"):
            self.assertTrue((SPD / "groups" / TODAY / f"{name}.npz").is_file(), name)
        self.assertEqual(sorted(p.name for p in SPD.iterdir()), ["groups", "layer.json", "layer.npz", "report.json"])
        state = open_state(R / "state.sqlite")
        try:
            self.assertEqual([v["version"] for v in state.speed_versions()], [layer.version])
        finally:
            state.close()
        # re-running segments and speeds skips the existing builds
        code, out = run(["--root", R, "segments", "--pbf", self.osm])
        self.assertEqual((code, Path(last_line(out))), (0, SEG))
        code, out = run(["--root", R, "speeds", "--segments", SEG])
        self.assertEqual((code, Path(last_line(out))), (0, SPD))

    def test_import_stores_groups_holidays_and_unresolved(self):
        state = open_state(self.R / "state.sqlite")
        try:
            self.assertEqual(sorted(g.name for g in state.groups()), ["container", "duoi2t", "tu2t"])
            self.assertEqual([g.name for g in state.groups() if g.is_base], ["duoi2t"])
            self.assertEqual([d.isoformat() for d in state.holidays()], ["2026-01-01"])
            self.assertEqual([(u["area"], u["place"]) for u in state.unresolved()], [("test", "Phố Chính")])
        finally:
            state.close()
        # the area falls back to the file stem; a re-import replaces the items instead of adding to them
        R = self.fresh_root()
        doc = dict(SEED_DOC)
        doc.pop("area")
        seed = write_json(R / "no_area.json", doc)
        self.assertEqual(run(["--root", R, "import-rules", seed])[0], 0)
        self.assertEqual(run(["--root", R, "import-rules", seed])[0], 0)
        state = open_state(R / "state.sqlite")
        try:
            self.assertEqual([u["area"] for u in state.unresolved()], ["no_area"])
        finally:
            state.close()

    def test_as_of_excludes_rules_out_of_force(self):
        R, SEG, SPD = self.R, self.SEG, self.SPD
        self.assertEqual(run(["--root", R, "groups", "--segments", SEG, "--speeds", SPD, "--as-of", "2026-01-15"])[0], 0)
        self.assertEqual(run(["--root", R, "groups", "--segments", SEG, "--speeds", SPD, "--as-of", "2026-02-15"])[0], 0)
        jan, feb = (load_group_view(SPD / "groups" / d / "tu2t.npz") for d in ("2026-01-15", "2026-02-15"))
        self.assertIn(self.PRIMARY, jan.closures.idx.tolist())
        self.assertNotIn(self.PRIMARY, feb.closures.idx.tolist())
        report = json.loads((SPD / "groups" / "2026-01-15" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["as_of"], "2026-01-15")
        self.assertEqual(report["closures"]["dated_ban"]["segments"], 2)
        self.assertEqual(sorted(report["closures"]["dated_ban"]["groups"]), ["container", "tu2t"])
        self.assertIn("container", report["osm"])
        self.assertEqual(report["orphans"], {})
        self.assertEqual(report["rules_sha"], jan.rules_sha)
        self.assertNotEqual(jan.rules_sha, feb.rules_sha)          # dated_ban is out of force in February

    def test_reimport_keeps_a_user_deactivation(self):
        R = self.fresh_root()
        seed = write_json(R / "seed_test.json", SEED_DOC)
        self.assertEqual(run(["--root", R, "import-rules", seed])[0], 0)
        state = open_state(R / "state.sqlite")
        try:
            state.set_active("hn_test", False, author="user", reason="tắt thử")
            rows = len(state.history("hn_test"))
        finally:
            state.close()
        code, out = run(["--root", R, "import-rules", seed])
        self.assertEqual(code, 0, out)
        self.assertIn("(0 changed)", out)
        state = open_state(R / "state.sqlite")
        try:
            self.assertEqual(len(state.history("hn_test")), rows)
            self.assertNotIn("hn_test", [r.id for r in state.rules(active_only=True)])
        finally:
            state.close()
        # a changed seed body is imported, the user's off-switch stays
        doc = json.loads(json.dumps(SEED_DOC))
        doc["rules"][0]["body"]["when"]["times"] = [["06:00", "10:00"]]
        code, out = run(["--root", R, "import-rules", write_json(seed, doc)])
        self.assertEqual(code, 0, out)
        self.assertIn("(1 changed)", out)
        state = open_state(R / "state.sqlite")
        try:
            rule = {r.id: r for r in state.rules(active_only=False)}["hn_test"]
            self.assertEqual(rule.body["when"]["times"], [["06:00", "10:00"]])
            self.assertFalse(rule.active)
            self.assertEqual([(h["author"], h["action"]) for h in state.history("hn_test")],
                             [("seed", "create"), ("user", "deactivate"), ("seed", "update")])
        finally:
            state.close()

    def test_rule_removed_from_seed_is_deactivated(self):
        R = self.fresh_root()
        two = {"rules": [SEED_DOC["rules"][0], SEED_DOC["rules"][3]]}            # hn_test, cap_heavy
        seed = write_json(R / "two.json", two)
        other = write_json(R / "other.json", {"rules": [SEED_DOC["rules"][1]]})  # dated_ban
        self.assertEqual(run(["--root", R, "import-rules", seed, other])[0], 0)
        code, out = run(["--root", R, "import-rules", write_json(seed, {"rules": [two["rules"][1]]})])
        self.assertEqual(code, 0, out)
        self.assertIn("deactivated hn_test", out)
        self.assertNotIn("dated_ban", out)                                       # imported from another file
        state = open_state(R / "state.sqlite")
        try:
            active = {r.id for r in state.rules(active_only=True)}
            self.assertEqual(active, {"cap_heavy", "dated_ban"})
            last = state.history("hn_test")[-1]
            self.assertEqual((last["author"], last["action"], last["reason"]),
                             ("seed", "deactivate", "removed from two.json"))
            rows = len(state.history("hn_test"))
        finally:
            state.close()
        # importing again adds no row for the rule that is already off
        code, out = run(["--root", R, "import-rules", seed])
        self.assertNotIn("hn_test", out)
        # a later import of another file that holds the id turns it back on
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "moved.json", two)])[0], 0)
        state = open_state(R / "state.sqlite")
        try:
            self.assertIn("hn_test", {r.id for r in state.rules(active_only=True)})
            self.assertEqual(len(state.history("hn_test")), rows + 1)
        finally:
            state.close()
        # and re-importing the old file no longer retires it: its latest seed import is moved.json
        code, out = run(["--root", R, "import-rules", seed])
        self.assertNotIn("hn_test", out)

    def test_rules_check_fails_on_orphans_of_trusted_rules(self):
        code, out = run(["--root", self.R, "rules-check", "--segments", self.SEG])
        self.assertEqual(code, 2)
        lines = [ln for ln in out.splitlines() if ln.startswith("orphan_rule")]
        self.assertEqual(len(lines), 1)
        self.assertIn("999", lines[0])
        self.assertTrue(any(ln.startswith("hn_test") for ln in out.splitlines()))

    def test_rules_check_passes_when_only_untrusted_rules_have_orphans(self):
        R = self.fresh_root()
        doc = json.loads(json.dumps(SEED_DOC))
        for rule in doc["rules"]:
            if rule["id"] == "orphan_rule":
                rule.update(confidence="thap", active=True)
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "s.json", doc)])[0], 0)
        code, out = run(["--root", R, "rules-check", "--segments", self.SEG])
        self.assertEqual(code, 0, out)
        self.assertIn("orphan_rule", out)

    def test_rules_check_marks_active_rules_with_orphans_partial(self):
        R = self.fresh_root()
        doc = json.loads(json.dumps(SEED_DOC))
        for rule in doc["rules"]:
            if rule["id"] == "orphan_rule":
                rule.update(confidence="thap", active=True)
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "s.json", doc)])[0], 0)
        code, out = run(["--root", R, "rules-check", "--segments", self.SEG])
        self.assertEqual(code, 0, out)
        line = [ln for ln in out.splitlines() if ln.startswith("orphan_rule")][0]
        self.assertIn("PARTIAL", line)
        self.assertNotIn("FAIL", line)
        self.assertNotIn("PARTIAL", [ln for ln in out.splitlines() if ln.startswith("hn_test")][0])
        self.assertIn("1 partial", last_line(out))
        self.assertIn("partial: orphan_rule", out)
        # a trusted rule with orphans is partial and failing
        code, out = run(["--root", self.R, "rules-check", "--segments", self.SEG])
        line = [ln for ln in out.splitlines() if ln.startswith("orphan_rule")][0]
        self.assertIn("PARTIAL", line)
        self.assertIn("FAIL", line)

    def test_rules_check_fails_on_zone_error_of_any_active_rule(self):
        # groups and speeds abort on a zone error of any active rule, so rules-check must too
        R = self.fresh_root()
        doc = json.loads(json.dumps(LEAKY_ZONE_DOC))
        doc["rules"][0]["active"] = True
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "z.json", doc)])[0], 0)
        code, out = run(["--root", R, "rules-check", "--segments", self.SEG])
        self.assertEqual(code, 2, out)
        line = [ln for ln in out.splitlines() if ln.startswith("leaky_zone")][0]
        self.assertIn("ZoneError", line)
        self.assertIn("FAIL", line)

    def test_rules_check_warns_on_errors_of_inactive_rules(self):
        R = self.fresh_root()
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "z.json", LEAKY_ZONE_DOC)])[0], 0)
        code, out = run(["--root", R, "rules-check", "--segments", self.SEG])
        self.assertEqual(code, 0, out)
        line = [ln for ln in out.splitlines() if ln.startswith("leaky_zone")][0]
        self.assertIn("inactive", line)
        self.assertIn("ZoneError", line)
        self.assertIn("WARN", line)
        self.assertNotIn("FAIL", line)
        self.assertIn("1 inactive rules in force, 1 with warnings", out)

    def test_groups_refuses_a_speed_layer_of_other_speed_rules(self):
        R = self.fresh_root()
        doc = json.loads(json.dumps(SEED_DOC))
        doc["rules"].append({"id": "jan_slow", "kind": "speed_set", "layer": "edit", "valid_from": "2026-01-01",
                             "valid_to": "2026-01-31", "body": {"kmh": 20, "scope": {"names": ["Phố Chính"]}}})
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "s.json", doc)])[0], 0)
        code, out = run(["--root", R, "speeds", "--segments", self.SEG, "--as-of", "2026-01-15"])
        self.assertEqual(code, 0, out)
        SPD = Path(last_line(out))
        self.assertEqual(run(["--root", R, "groups", "--segments", self.SEG, "--speeds", SPD,
                              "--as-of", "2026-01-20"])[0], 0)
        code, out = run(["--root", R, "groups", "--segments", self.SEG, "--speeds", SPD, "--as-of", "2026-02-15"])
        self.assertEqual(code, 2, out)
        self.assertIn("2026-02-15", out)
        self.assertIn(SPD.name, out)
        self.assertFalse((SPD / "groups" / "2026-02-15").exists())
        self.assertFalse((SPD / ".groups-2026-02-15.tmp").exists())

    def test_groups_needs_exactly_one_base_group_and_measures_against_it(self):
        R = self.fresh_root()
        doc = json.loads(json.dumps(SEED_DOC))
        for group in doc["groups"]:
            group["is_base"] = False
        self.assertEqual(run(["--root", R, "import-rules", write_json(R / "s.json", doc)])[0], 0)
        code, out = run(["--root", R, "speeds", "--segments", self.SEG])
        SPD = Path(last_line(out))
        code, out = run(["--root", R, "groups", "--segments", self.SEG, "--speeds", SPD])
        self.assertEqual(code, 2, out)
        self.assertIn("base", out)
        self.assertFalse((SPD / "groups").exists())
        # the shared root has duoi2t as base: the other views are measured against it
        self.assertEqual(run(["--root", self.R, "groups", "--segments", self.SEG, "--speeds", self.SPD,
                              "--as-of", "2026-03-01"])[0], 0)
        report = json.loads((self.SPD / "groups" / "2026-03-01" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["base_group"], "duoi2t")
        self.assertEqual(report["groups"]["tu2t"]["affected_against"], "duoi2t")
        self.assertEqual(report["groups"]["duoi2t"]["affected_against"], "layer")
        self.assertEqual(load_group_view(self.SPD / "groups" / "2026-03-01" / "tu2t.npz").base, "duoi2t")

    def test_rules_check_reports_zone_errors(self):
        R = self.fresh_root()
        self.assertEqual(run(["--root", R, "import-rules", self.zone_seed])[0], 0)
        code, out = run(["--root", R, "rules-check", "--segments", self.SEG])
        self.assertEqual(code, 2)
        line = [ln for ln in out.splitlines() if ln.startswith("zone_missing_road")][0]
        self.assertIn("ZoneError", line)
        self.assertIn("Đường Không Có Thật", line)

    def test_orphans_are_listed_in_build_report(self):
        report = json.loads((self.SPD / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["orphans"]["orphan_rule"], [{"anchor": [999, 1, 2]}])
        self.assertEqual(report["as_of"], today().isoformat())
        self.assertIn("clamped", report["stats"])

    def test_zone_error_exits_2_without_partial_output(self):
        R2 = self.fresh_root()
        self.assertEqual(run(["--root", R2, "import-rules", self.seed, self.zone_seed])[0], 0)
        code, out = run(["--root", R2, "speeds", "--segments", self.SEG])
        self.assertEqual(code, 0, out)
        SPD2 = Path(last_line(out))
        code, out = run(["--root", R2, "groups", "--segments", self.SEG, "--speeds", SPD2])
        self.assertEqual(code, 2)
        self.assertIn("Đường Không Có Thật", out)
        self.assertFalse((SPD2 / "groups").exists())
        self.assertEqual(sorted(p.name for p in SPD2.iterdir()), ["layer.json", "layer.npz", "report.json"])

    def test_rule_error_exits_2_and_imports_nothing(self):
        R = self.fresh_root()
        doc = json.loads(json.dumps(SEED_DOC))
        doc["rules"].append({"id": "bad", "kind": "ban", "body": {"vehicle": {}, "action": "shut",
                                                                  "scope": {"names": ["X"], "bbox": BBOX}}})
        code, out = run(["--root", R, "import-rules", write_json(R / "bad.json", doc)])
        self.assertEqual(code, 2)
        self.assertIn("action", out)
        state = open_state(R / "state.sqlite")
        try:
            self.assertEqual(state.rules(active_only=False), [])
            self.assertEqual(state.groups(), [])
        finally:
            state.close()

    def test_missing_road_data_exits_2_without_partial_output(self):
        R = self.fresh_root()
        osm = R / "unknown.osm"
        write_osm(osm, {1: (21.0, 105.8), 2: (21.0, 105.801)}, [(7, [1, 2], {"highway": "primary"})])
        code, out = run(["--root", R, "segments", "--pbf", osm])
        self.assertEqual(code, 0, out)
        seg = Path(last_line(out))
        # a speed source gap: the stored table loses its highway string
        with np.load(seg / "segments.npz") as z:
            data = dict(z)
        data["highway"] = np.full_like(data["highway"], -1)
        np.savez(seg / "segments.npz", **data)
        code, out = run(["--root", R, "speeds", "--segments", seg])
        self.assertEqual(code, 2)
        self.assertIn("no speed", out)
        self.assertEqual(list((R / "speeds").iterdir()) if (R / "speeds").exists() else [], [])

    def test_status_lists_builds(self):
        code, out = run(["--root", self.R, "status"])
        self.assertEqual(code, 0, out)
        self.assertIn(self.SEG.name, out)
        self.assertIn(self.SPD.name, out)
        self.assertIn("container", out)
        self.assertIn("official", out)

    def test_argparse_errors_keep_their_exit_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(["--root", str(self.R), "speeds"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
