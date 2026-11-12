"""The local worker behind crawl and experiment jobs (previously untested): command, environment, failures."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from backend import jobs as jobs_module
from backend.jobs import subprocess_runner


class Completed:
    def __init__(self, returncode):
        self.returncode = returncode


class SubprocessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []

    def fake_run(self, returncode=0):
        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            return Completed(returncode)
        return run

    def test_crawl_passes_the_key_only_through_the_environment(self):
        runner = subprocess_runner(self.root, lambda: "very-secret")
        with mock.patch.object(jobs_module.subprocess, "run", self.fake_run()):
            result = runner("crawl", {"date": "2026-10-20", "slots": ["0800", "1730"], "max_calls": 12})
        command, kwargs = self.calls[0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("crawl_spatial.py"))
        self.assertEqual(command[2:], ["--date", "2026-10-20", "--slots", "0800", "1730", "--max-calls", "12"])
        self.assertNotIn("very-secret", " ".join(command))
        self.assertEqual(kwargs["env"]["TOMTOM_API_KEY"], "very-secret")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(result["sample_count"], 0)  # data_status of the empty root

    def test_crawl_without_a_key_never_starts_a_process(self):
        runner = subprocess_runner(self.root, lambda: None)
        with mock.patch.object(jobs_module.subprocess, "run", self.fake_run()):
            with self.assertRaises(RuntimeError):
                runner("crawl", {"date": "2026-10-20", "slots": ["0800"], "max_calls": 1})
        self.assertEqual(self.calls, [])

    def test_experiments_run_compare_spatial_and_report_availability(self):
        runner = subprocess_runner(self.root, lambda: None)
        with mock.patch.object(jobs_module.subprocess, "run", self.fake_run()):
            self.assertEqual(runner("experiments", {}), {"report_available": False})
            (self.root / "reports").mkdir()
            (self.root / "reports/spatial_comparison.json").write_text("{}", encoding="utf-8")
            self.assertEqual(runner("experiments", {}), {"report_available": True})
        self.assertTrue(self.calls[0][0][1].endswith("compare_spatial.py"))
        self.assertNotIn("TOMTOM_API_KEY", {k for k in self.calls[0][1]["env"]} - set(jobs_module.os.environ))

    def test_nonzero_exit_is_a_runtime_error_without_output(self):
        runner = subprocess_runner(self.root, lambda: "k")
        with mock.patch.object(jobs_module.subprocess, "run", self.fake_run(returncode=2)):
            with self.assertRaises(RuntimeError) as ctx:
                runner("experiments", {})
        self.assertEqual(str(ctx.exception), "Local worker failed")


if __name__ == "__main__":
    unittest.main()
