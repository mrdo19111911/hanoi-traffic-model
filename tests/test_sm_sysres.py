"""Offline tests for speedmatrix.sysres."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

from speedmatrix.sysres import available_memory_gb, cpu_workers, default_workers, free_disk_gb, run_measured

ROOT = Path(__file__).resolve().parents[1]


class SysresTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "run.log"

    def test_worker_formula(self):
        self.assertEqual(cpu_workers(24), 14)
        self.assertEqual(cpu_workers(1), 1)
        self.assertEqual(default_workers(0.5, cpu_count=24, available_gb=10), 12)
        self.assertEqual(default_workers(2.0, cpu_count=24, available_gb=10), 3)
        self.assertEqual(default_workers(8.0, cpu_count=24, available_gb=4), 1)
        self.assertEqual(default_workers(0.5, requested=5, cpu_count=24, available_gb=1), 5)

    def test_machine_probes_are_positive(self):
        self.assertGreater(available_memory_gb(), 0)
        self.assertGreater(free_disk_gb(ROOT), 0)

    def test_run_measured_logs_and_peak(self):
        code = "import time; x = bytearray(200_000_000); print('done'); time.sleep(1.5)"
        r = run_measured([sys.executable, "-c", code], self.log, ROOT)
        self.assertEqual((r.code, r.last_line), (0, "done"))
        self.assertIn("done", self.log.read_text(encoding="utf-8"))
        if os.name == "nt":
            self.assertGreaterEqual(r.peak_bytes, 150_000_000)

    def test_run_measured_reports_the_child_pid(self):
        pids = []
        r = run_measured([sys.executable, "-c", "import os; print(os.getpid())"], self.log, ROOT, on_start=pids.append)
        self.assertEqual(r.code, 0)
        self.assertEqual(len(pids), 1)
        self.assertIsInstance(pids[0], int)

        def refuse(pid):
            raise RuntimeError("no")
        with self.assertRaisesRegex(RuntimeError, "no"):
            run_measured([sys.executable, "-c", "import time; time.sleep(30)"], self.log, ROOT, on_start=refuse)

    def test_run_measured_reports_failure_and_appends(self):
        run_measured([sys.executable, "-c", "print('one')"], self.log, ROOT)
        r = run_measured([sys.executable, "-c", "import sys; print('two'); sys.exit(3)"], self.log, ROOT)
        self.assertEqual((r.code, r.last_line), (3, "two"))
        self.assertEqual(self.log.read_text(encoding="utf-8").split(), ["one", "two"])


if __name__ == "__main__":
    unittest.main()
