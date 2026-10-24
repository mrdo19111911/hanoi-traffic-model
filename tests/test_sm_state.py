import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from speedmatrix.rules import Rule, RuleError
from speedmatrix.state import open_state

SCHEMA_1_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE rules(
  id TEXT PRIMARY KEY, kind TEXT, layer TEXT, body TEXT, confidence TEXT,
  active INTEGER, source TEXT, document TEXT, note TEXT,
  valid_from TEXT, valid_to TEXT, updated_at TEXT);
CREATE TABLE unresolved(area TEXT, item TEXT);
CREATE TABLE rule_history(
  hid INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, at TEXT, author TEXT,
  reason TEXT, action TEXT, snapshot TEXT);
CREATE TABLE vehicle_groups(name TEXT PRIMARY KEY, attrs TEXT, is_base INTEGER);
CREATE TABLE holidays(day TEXT PRIMARY KEY, name TEXT);
CREATE TABLE speed_versions(version TEXT PRIMARY KEY, created_at TEXT, info TEXT);
INSERT INTO meta(key, value) VALUES('schema', '1');
INSERT INTO rules VALUES('old', 'speed_set', 'edit', '{"kmh": 20, "scope": {"names": ["X"]}}', 'tb', 1, '', '', '',
  NULL, NULL, '2026-10-01T00:00:00+07:00');
"""


def meta_schema(path):
    db = sqlite3.connect(str(path))
    try:
        return db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]
    finally:
        db.close()
from speedmatrix.vehicles import VehicleGroup


def R(kmh=20, **kw):
    return Rule("r", "speed_set", "edit", {"scope": {"names": ["X"]}, "kmh": kmh}, **kw)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "state.sqlite"

    def open(self):
        s = open_state(self.db)
        self.addCleanup(s.close)
        return s

    def test_history_and_revert(self):
        s = self.open()
        s.upsert_rule(R(20), "an", "first")
        s.upsert_rule(R(35), "an", "second")
        s.set_active("r", False, "an", "off")
        first = s.history("r")[0]["hid"]
        back = s.revert("r", first, "an")
        self.assertEqual(back.body["kmh"], 20)
        self.assertTrue(back.active)
        self.assertEqual(len(s.history("r")), 4)
        self.assertTrue(s.history("r")[0]["at"].endswith("+07:00"))
        s.close()

    def test_identical_upsert_is_noop(self):
        s = self.open()
        s.upsert_rule(R(20), "an", "first")
        s.upsert_rule(R(20), "an", "again")
        self.assertEqual(len(s.history("r")), 1)
        s.upsert_rule(R(20, valid_to=date(2027, 1, 1)), "an", "dates")
        self.assertEqual(len(s.history("r")), 2)

    def test_invalid_rule_not_written(self):
        s = self.open()
        with self.assertRaises(RuleError):
            s.upsert_rule(R(-1), "an", "bad")
        self.assertEqual(s.rules(), [])

    def test_rules_filters(self):
        s = self.open()
        s.upsert_rule(R(20, valid_to=date(2026, 2, 1)), "an", "x")
        s.upsert_rule(Rule("b", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"segments": [[1, 2, 3]]}}), "an", "x")
        s.upsert_rule(Rule("c", "speed_set", "edit", {"scope": {"names": ["Y"]}, "kmh": 9}, active=False), "an", "x")
        self.assertEqual([r.id for r in s.rules()], ["b", "r"])
        self.assertEqual([r.id for r in s.rules(active_only=False)], ["b", "c", "r"])
        self.assertEqual([r.id for r in s.rules(kinds=["ban"])], ["b"])
        self.assertEqual([r.id for r in s.rules(as_of=date(2026, 3, 1))], ["b"])
        self.assertEqual(s.rules()[1].valid_to, date(2026, 2, 1))

    def test_single_base_group_and_holidays(self):
        s = self.open()
        s.upsert_group(VehicleGroup("a", 1.9, 1, 2, "normal", 2.5, 5.0, is_base=True))
        s.upsert_group(VehicleGroup("b", 8, 5, 2, "normal", 3.2, 7.5, is_base=True))
        self.assertEqual([g.name for g in s.groups() if g.is_base], ["b"])
        s.add_holiday(date(2026, 9, 2), "Quốc khánh")
        self.assertEqual(s.holidays(), {date(2026, 9, 2)})

    def test_unresolved_replaced_per_area(self):
        s = self.open()
        s.add_unresolved("south", [{"place": "a"}, {"place": "b"}])
        s.add_unresolved("north", [{"place": "c"}])
        s.add_unresolved("south", [{"place": "d"}])
        self.assertEqual(sorted(i["place"] for i in s.unresolved()), ["c", "d"])

    def test_speed_versions_and_persistence(self):
        s = self.open()
        s.record_speed_version("v1", {"n": 1})
        s.upsert_rule(R(), "an", "x")
        s.close()
        s = self.open()
        self.assertEqual(s.speed_versions()[0]["version"], "v1")
        self.assertEqual(s.speed_versions()[0]["info"], {"n": 1})
        self.assertEqual(len(s.rules()), 1)

    def test_newer_schema_refuses(self):
        s = self.open()
        s._db.execute("UPDATE meta SET value='3' WHERE key='schema'")
        s._db.commit()
        s.close()
        with self.assertRaises(RuntimeError):
            self.open()

    def test_schema_1_upgrades(self):
        db = sqlite3.connect(str(self.db))
        db.executescript(SCHEMA_1_DDL)
        db.close()
        s = self.open()
        self.assertEqual(s.jobs(), [])
        self.assertEqual(meta_schema(self.db), "2")
        self.assertEqual([r.id for r in s.rules()], ["old"])     # the rules survive the upgrade

    def test_jobs_start_finish_and_order(self):
        s = self.open()
        s.start_job("20261024-030000", "update", "logs/a.log")
        s.start_job("20261024-030001", "update", "logs/b.log")
        s.start_job("x", "other", None)
        running = s.jobs(kind="update")[1]
        self.assertEqual((running["id"], running["status"], running["finished_at"], running["info"]),
                         ("20261024-030000", "running", None, None))
        s.finish_job("20261024-030000", "error", {"release": "r"}, "UpdateError: boom")
        s.finish_job("20261024-030001", "promoted", {"n": 1})
        jobs = s.jobs(kind="update")
        self.assertEqual([j["id"] for j in jobs], ["20261024-030001", "20261024-030000"])   # newest first
        self.assertEqual((jobs[1]["status"], jobs[1]["info"], jobs[1]["error"], jobs[1]["log_path"]),
                         ("error", {"release": "r"}, "UpdateError: boom", "logs/a.log"))
        self.assertTrue(jobs[0]["finished_at"].endswith("+07:00"))
        self.assertIsNone(jobs[0]["error"])
        self.assertEqual(len(s.jobs()), 3)
        self.assertEqual(len(s.jobs(limit=1)), 1)
        self.assertEqual(s.job("x")["kind"], "other")
        self.assertIsNone(s.job("nope"))
        with self.assertRaises(KeyError):
            s.finish_job("nope", "error", {})
        with self.assertRaises(sqlite3.IntegrityError):
            s.start_job("x", "other", None)


if __name__ == "__main__":
    unittest.main()
