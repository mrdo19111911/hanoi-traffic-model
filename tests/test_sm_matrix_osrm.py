"""Offline tests for speedmatrix.matrix_osrm (a fake runner stands in for the OSRM binaries)."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from speedmatrix.matrix_osrm import PATCHES, ProfilePatchError, build_matrix_osrm, patch_profiles, profiles_sha256
from speedmatrix.sysres import StepResult

ROOT = Path(__file__).resolve().parents[1]


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_profiles(root, repeat=1):
    """A profile dir holding every patch's `old` string `repeat` times in its file."""
    for name, old, _ in PATCHES:
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8", newline="") as h:
            h.write((old + "\n") * repeat)
    return root


def fake_runner(calls, code=0, version="v26.10.0"):
    def run(cmd, log, cwd, env=None):
        calls.append([Path(cmd[0]).name] + [str(c) for c in cmd[1:]])
        if "--version" in cmd:
            return StepResult(0, 0.1, None, version)
        return StepResult(code, 1.0, 10, "")
    return run


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def test_patches_apply_once(self):
        src = write_profiles(self.tmp / "src")
        applied = patch_profiles(src, self.tmp / "dst")
        text = (self.tmp / "dst" / "car.lua").read_text(encoding="utf-8")
        self.assertEqual(len(applied), 4)
        self.assertIn("weight_name                     = 'duration'", text)
        self.assertIn("construction_ignored_on_open_highway", text)

    def test_patch_matching_twice_raises(self):
        src = write_profiles(self.tmp / "src", repeat=2)
        with self.assertRaisesRegex(ProfilePatchError, "car.lua.*2"):
            patch_profiles(src, self.tmp / "dst")

    def test_patch_matching_never_raises(self):
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "car.lua").write_text("nothing", encoding="utf-8")
        with self.assertRaisesRegex(ProfilePatchError, "car.lua.*0"):
            patch_profiles(self.tmp / "src", self.tmp / "dst")

    def test_profiles_sha_depends_on_paths_and_bytes(self):
        for name, content in (("a/x.lua", b"1"), ("b/y.lua", b"1"), ("c/x.lua", b"1")):
            path = self.tmp / name
            path.parent.mkdir()
            path.write_bytes(content)
        self.assertEqual(profiles_sha256(self.tmp / "a"), profiles_sha256(self.tmp / "c"))
        self.assertNotEqual(profiles_sha256(self.tmp / "a"), profiles_sha256(self.tmp / "b"))

    @unittest.skipUnless((ROOT / "osrm/tools/profiles/car.lua").exists(), "profiles absent")
    def test_stock_profiles_patch_cleanly(self):
        applied = patch_profiles(ROOT / "osrm/tools/profiles", self.tmp / "p")
        self.assertEqual(len(applied), 4)
        self.assertIn("weight_name                     = 'duration'",
                      (self.tmp / "p" / "car.lua").read_text(encoding="utf-8"))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        tmp = Path(self.temp.name)
        self.tools = tmp / "tools"
        write_profiles(self.tools / "profiles")
        self.pbf = tmp / "tiny.osm.pbf"
        self.pbf.write_bytes(b"pbf bytes")
        self.out = tmp / "out"

    def test_build_runs_three_steps_and_records(self):
        calls = []
        info = build_matrix_osrm(self.pbf, self.out, 14, tools=self.tools, runner=fake_runner(calls))
        self.assertEqual([c[0] for c in calls],
                         ["osrm-extract.exe", "osrm-extract.exe", "osrm-partition.exe", "osrm-customize.exe"])
        self.assertEqual(calls[0][1:], ["--version"])
        self.assertEqual(calls[1][calls[1].index("-t") + 1], "14")
        doc = json.loads((self.out / "build_matrix.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["input"]["pbf_sha256"], doc["input"]["osrm_version"]),
                         (sha256_of(self.pbf), "v26.10.0"))
        self.assertEqual(doc["pbf_md5"], hashlib.md5(b"pbf bytes").hexdigest())
        self.assertEqual(doc["profiles_sha256"], profiles_sha256(self.out / "profiles"))
        self.assertEqual(sorted(doc["steps"]), ["customize", "extract", "partition"])
        self.assertEqual(len(doc["patches"]), 4)
        self.assertEqual(info["pbf_copy"], self.out / "data" / self.pbf.name)
        self.assertTrue(info["pbf_copy"].exists())

    def test_wrong_binary_version_raises(self):
        with self.assertRaisesRegex(RuntimeError, "v26.11.0"):
            build_matrix_osrm(self.pbf, self.out, 2, tools=self.tools, runner=fake_runner([], version="v26.11.0"))

    def test_failed_step_leaves_no_build_json(self):
        with self.assertRaisesRegex(RuntimeError, "osrm-extract failed"):
            build_matrix_osrm(self.pbf, self.out, 2, tools=self.tools, runner=fake_runner([], code=1))
        self.assertFalse((self.out / "build_matrix.json").exists())

    def test_relative_out_gives_the_children_absolute_paths(self):
        (ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "runtime", ignore_cleanup_errors=True) as work:
            work = Path(work)
            pbf = work / "tiny.osm.pbf"
            pbf.write_bytes(b"pbf bytes")
            calls = []
            build_matrix_osrm(pbf.relative_to(ROOT), (work / "out").relative_to(ROOT), 2, tools=self.tools,
                              runner=fake_runner(calls))
            for call in calls[1:]:
                paths = [c for c in call[1:] if c.endswith((".pbf", ".osrm", ".lua"))]
                self.assertTrue(paths)
                for path in paths:
                    self.assertTrue(Path(path).is_absolute(), path)

    def test_failed_rerun_removes_the_stale_build_json(self):
        build_matrix_osrm(self.pbf, self.out, 2, tools=self.tools, runner=fake_runner([]))
        self.assertTrue((self.out / "build_matrix.json").exists())
        with self.assertRaisesRegex(RuntimeError, "osrm-extract failed"):
            build_matrix_osrm(self.pbf, self.out, 2, tools=self.tools, runner=fake_runner([], code=1))
        self.assertFalse((self.out / "build_matrix.json").exists())


if __name__ == "__main__":
    unittest.main()
