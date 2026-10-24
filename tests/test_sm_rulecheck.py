"""Offline tests for speedmatrix.rulecheck: re-resolving rules on a new map and the orphan report."""
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_remap_fixture import BBOX, COORDS, ZONE, build_maps  # noqa: E402

from speedmatrix.rulecheck import blocks, main, rules_report  # noqa: E402
from speedmatrix.rules import Rule  # noqa: E402
from speedmatrix.scopes import ScopeResolver  # noqa: E402
from speedmatrix.segments import save_segments  # noqa: E402
from speedmatrix.state import open_state  # noqa: E402

VEHICLE = {"gross_t": [">=", 2]}


def ban(rule_id, scope, confidence="cao", active=True):
    return Rule(id=rule_id, kind="ban", layer="official", confidence=confidence, active=active,
                body={"vehicle": VEHICLE, "action": "close", "scope": scope})


ANCHOR_RULE = ban("anchor_rule", {"segments": [[1, 11, 12]]})
NAMES_A = ban("names_a", {"names": ["Phố A"], "bbox": BBOX})
NAMES_B = ban("names_b", {"names": ["Phố B"], "bbox": BBOX})
NAMES_C = ban("names_c", {"names": ["Phố C"], "bbox": BBOX}, confidence="thap")
ZONE_RULE = ban("zone_rule", {"zone": ZONE, "bbox": BBOX})
RULES = [ANCHOR_RULE, NAMES_A, NAMES_B, NAMES_C, ZONE_RULE]
ORPHAN_ANCHOR_RULE = ban("orphan_anchor", {"segments": [[5, 51, 52]]}, confidence="thap")
NO_SCOPE_RULE = Rule(id="no_scope", kind="cap", layer="official", confidence="cao",
                     body={"vehicle": VEHICLE, "road_classes": {"chinh": 50}})


class RulecheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.base = Path(cls.temp.name)
        cls.old_table, cls.new_table = build_maps(cls.base)
        cls.old, cls.new = ScopeResolver(cls.old_table), ScopeResolver(cls.new_table)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_statuses(self):
        rep = rules_report(self.new, RULES, self.old)
        st = {k: v["status"] for k, v in rep["rules"].items()}
        self.assertEqual(st, {"anchor_rule": "moved", "names_a": "ok", "names_b": "changed",
                              "names_c": "new_orphans", "zone_rule": "error"})
        self.assertEqual(rep["summary"]["error"], 1)
        self.assertEqual(rep["rules"]["anchor_rule"]["moved"], [{"anchor": [1, 11, 12], "way_ids": [91]}])
        self.assertEqual((rep["rules"]["names_b"]["segments_old"], rep["rules"]["names_b"]["segments_new"]), (2, 32))

    def test_zone_error_is_blocking(self):
        rep = rules_report(self.new, RULES, self.old)
        self.assertEqual(rep["blocking"], ["zone_rule"])
        self.assertIn("Vành đai D", rep["rules"]["zone_rule"]["error"])

    def test_zone_resolves_on_old_map(self):
        rep = rules_report(self.old, [ZONE_RULE], self.old)
        self.assertEqual((rep["rules"]["zone_rule"]["status"], rep["blocking"]), ("ok", []))

    def test_inactive_rule_errors_do_not_block(self):
        rep = rules_report(self.new, [replace(ZONE_RULE, active=False)])
        self.assertEqual(rep["blocking"], [])
        self.assertEqual(rep["rules"]["zone_rule"]["status"], "error")

    def test_orphan_keeps_its_old_geometry(self):
        rep = rules_report(self.new, [ORPHAN_ANCHOR_RULE], self.old)
        lon51, lat51 = COORDS[51]
        lon52, lat52 = COORDS[52]
        was = rep["rules"]["orphan_anchor"]["orphans_new"][0]["was"]
        self.assertEqual(len(was), 2)
        for got, want in zip(was, ([lat51, lon51], [lat52, lon52])):
            self.assertAlmostEqual(got[0], want[0], places=5)
            self.assertAlmostEqual(got[1], want[1], places=5)

    def test_trusted_orphan_blocks(self):
        rep = rules_report(self.new, [replace(NAMES_C, confidence="cao")], self.old)
        self.assertEqual(rep["blocking"], ["names_c"])
        self.assertEqual(rules_report(self.new, [NAMES_C], self.old)["blocking"], [])

    def test_orphan_already_on_old_map_is_not_new(self):
        rep = rules_report(self.new, [NAMES_C], self.new)
        self.assertEqual(rep["rules"]["names_c"]["status"], "ok")

    def test_precedence_changed_beats_moved(self):
        # moved + changed at once: same rule id on a map where both hold
        from speedmatrix import rulecheck
        status = rulecheck._status(None, [], [], 32, 2, [{"anchor": [1, 11, 12], "way_ids": [91]}])
        self.assertEqual(status, "changed")
        self.assertEqual(rulecheck._status("boom", [{"name": "x"}], [], 32, 2, [{"x": 1}]), "error")
        self.assertEqual(rulecheck._status(None, [{"name": "x"}], [], 32, 2, [{"x": 1}]), "new_orphans")

    def test_no_scope_never_blocks(self):
        rep = rules_report(self.new, [NO_SCOPE_RULE], self.old)
        self.assertEqual(rep["rules"]["no_scope"]["status"], "no_scope")
        self.assertEqual(rep["blocking"], [])

    def test_blocking_policy_is_the_one_of_rules_check(self):
        self.assertTrue(blocks(NAMES_A, True, []))
        self.assertTrue(blocks(replace(NAMES_A, confidence="tb"), False, [{"name": "x"}]))
        self.assertFalse(blocks(replace(NAMES_A, confidence="thap"), False, [{"name": "x"}]))
        self.assertFalse(blocks(replace(NAMES_A, active=False), True, [{"name": "x"}]))

    def test_cli_writes_report_and_exits_0(self):
        root = self.base / "root"
        state = open_state(root / "state.sqlite")
        for rule in RULES:
            state.upsert_rule(rule, "test", "fixture")
        state.close()
        old_dir, new_dir = self.base / "seg_old", self.base / "seg_new"
        save_segments(self.old_table, old_dir)
        save_segments(self.new_table, new_dir)
        out = self.base / "out" / "report.json"
        before = (root / "state.sqlite").read_bytes()
        self.assertEqual(main(["--root", root, "--segments", new_dir, "--previous", old_dir, "--out", out]), 0)
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(doc["summary"]["error"], 1)
        self.assertEqual(doc["blocking"], ["zone_rule"])
        self.assertEqual((root / "state.sqlite").read_bytes(), before)

    def test_cli_does_not_create_a_missing_state(self):
        with self.assertRaises(FileNotFoundError):
            main(["--root", self.base / "nowhere", "--segments", self.base, "--out", self.base / "x.json"])
        self.assertFalse((self.base / "nowhere").exists())


if __name__ == "__main__":
    unittest.main()
