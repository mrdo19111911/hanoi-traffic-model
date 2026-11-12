"""speedmatrix.common: the one place for repo paths, JSON/sha256/sqlite helpers and the osrm/manage import.

Written before the module (2026-11-12) to replace seven copies of the repo-path block, three ``import manage``
dances, three ``_write_json``, two ``_read_json``, two ``_sha256_file`` and two read-only sqlite opens.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from speedmatrix import common

ROOT = Path(__file__).resolve().parents[1]


class PathTests(unittest.TestCase):
    def test_repo_is_the_checkout_root(self):
        self.assertEqual(common.REPO, ROOT)
        self.assertTrue((common.REPO / "speedmatrix" / "update.py").is_file())
        self.assertEqual(common.DEFAULT_ROOT, ROOT / "runtime" / "matrix")

    def test_osrm_manage_is_imported_once_and_leaves_sys_path_alone(self):
        before = list(sys.path)
        first = common.import_osrm_manage()
        second = common.import_osrm_manage()
        self.assertIs(first, second)
        self.assertTrue(callable(first.process_identity))
        self.assertEqual(sys.path, before)


class JsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_write_is_atomic_indent_1_lf_and_utf8(self):
        path = self.dir / "a.json"
        common.write_json(path, {"b": 1, "a": "Hà Nội"})
        self.assertEqual(path.read_bytes(), b'{\n "b": 1,\n "a": "H\xc3\xa0 N\xe1\xbb\x99i"\n}')
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.json"])  # no .tmp left behind

    def test_write_replaces_an_existing_file_and_keeps_the_old_one_on_failure(self):
        path = self.dir / "a.json"
        common.write_json(path, {"v": 1})
        with self.assertRaises(TypeError):
            common.write_json(path, {"v": object()})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 1})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.json"])

    def test_default_converter_is_opt_in(self):
        path = self.dir / "a.json"
        common.write_json(path, {"p": Path("x")}, default=str)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"p": "x"})

    def test_read_raises_or_returns_none_for_a_missing_file(self):
        path = self.dir / "none.json"
        with self.assertRaises(FileNotFoundError):
            common.read_json(path)
        self.assertIsNone(common.read_json_or_none(path))
        common.write_json(path, [1])
        self.assertEqual(common.read_json(path), [1])
        self.assertEqual(common.read_json_or_none(path), [1])


class HashAndSqliteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def test_sha256_file_matches_hashlib(self):
        path = self.dir / "blob"
        path.write_bytes(os.urandom(5000))
        self.assertEqual(common.sha256_file(path), hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(common.sha256_file(str(path)), common.sha256_file(path))

    def test_open_sqlite_ro_gives_row_access_and_refuses_writes(self):
        path = self.dir / "s.sqlite"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE t(k TEXT, v INTEGER)")
            db.execute("INSERT INTO t VALUES ('a', 1)")
        db = common.open_sqlite_ro(path)
        try:
            row = db.execute("SELECT k, v FROM t").fetchone()
            self.assertEqual((row["k"], row["v"]), ("a", 1))
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("INSERT INTO t VALUES ('b', 2)")
        finally:
            db.close()

    def test_open_sqlite_ro_missing_file_is_an_error_not_a_new_database(self):
        path = self.dir / "none.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            common.open_sqlite_ro(path).execute("SELECT 1")
        self.assertFalse(path.exists())


class ScriptModeTests(unittest.TestCase):
    def test_update_py_still_runs_as_a_script_from_another_directory(self):
        # The scheduled task runs ``pythonw.exe <repo>/speedmatrix/update.py run --root ...`` (update.schedule).
        with tempfile.TemporaryDirectory() as elsewhere:
            done = subprocess.run([sys.executable, str(ROOT / "speedmatrix" / "update.py"), "--help"], cwd=elsewhere,
                                  capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("python -m speedmatrix.update", done.stdout)


if __name__ == "__main__":
    unittest.main()
