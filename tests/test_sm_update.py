"""Offline tests for speedmatrix.update: the release pipeline with fake steps, pruning, the lock, the download
checks, RealSteps' command lines and JSON reading, and the scheduled-task command line (never registered)."""
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sm_osrm_fixture import write_osm  # noqa: E402

from speedmatrix import update  # noqa: E402
from speedmatrix.build import main as build_main  # noqa: E402
from speedmatrix.state import open_state  # noqa: E402
from speedmatrix.sysres import StepResult  # noqa: E402
from speedmatrix.update import (GEOFABRIK_MD5, GEOFABRIK_PBF, Check, RealSteps, UpdateError,  # noqa: E402
                                check_url, download_pbf, process_identity, prune, release_size_gb, run_update,
                                schedule, serve, unschedule)

DEAD_PID = 0x7FFFFFF0
TINY_COORDS = {11: (105.801, 21.001), 12: (105.802, 21.001), 13: (105.803, 21.001), 21: (105.801, 21.002),
               22: (105.802, 21.002)}
TINY_WAYS = [(1, [11, 12, 13], {"highway": "primary", "name": "Phố Chính"}),
             (2, [21, 22], {"highway": "residential", "name": "Ngõ Nhỏ"})]
TINY_SEED = {
    "groups": [{"name": "nho", "gross_t": 1.9, "payload_t": 0.99, "axles": 2, "kind": "normal", "height_m": 2.6,
                "length_m": 5.0, "is_base": True},
               {"name": "lon", "gross_t": 8.0, "payload_t": 5.0, "axles": 2, "kind": "normal", "height_m": 3.4,
                "length_m": 7.5, "is_base": False}],
    "rules": [{"id": "orphan_tb", "kind": "speed_set", "confidence": "tb",
               "body": {"kmh": 30, "scope": {"segments": [[999, 1, 2]]}}}],
}
DEFAULT_CHECKS = {
    "partition": {"contiguity_l1": Check(0, True, True), "l2_cells_changed": Check(0, True, False)},
    "rules": {"rules_blocking": Check([], True, True), "rules_new_orphans": Check(0, True, False)},
    "verify": {"osrm_table_over_tolerance": Check(0, True, True)},
}


class FakeSteps:
    """Steps that write the minimum files and return the given checks per step name (over DEFAULT_CHECKS).

    ``download`` runs the real ``download_pbf`` on ``pbf_bytes``. Without explicit ``pbf_bytes`` the expected md5 is
    that of the default bytes (the fake's ``md5`` values are short labels); with them, the run's md5 is passed on, so
    a mismatch goes through the real check."""

    def __init__(self, md5="m", pbf_bytes=None, md5_error=None, checks=None, fail=None):
        self.md5 = md5
        self.explicit = pbf_bytes is not None
        self.pbf_bytes = pbf_bytes if pbf_bytes is not None else b"PBF " + md5.encode()
        self.md5_error = md5_error
        self.checks = checks or {}
        self.fail = fail or {}
        self.called = []
        self.args = {}
        self.root = None

    def _call(self, name, *args):
        self.called.append(name)
        self.args[name] = args
        if name in self.fail:
            raise self.fail[name]

    def _result(self, name, **extra):
        checks = {**DEFAULT_CHECKS.get(name, {}), **self.checks.get(name, {})}
        return {**extra, "checks": {k: asdict(v) for k, v in checks.items()}, "summary": {"step": name}}

    def remote_md5(self):
        self._call("remote_md5")
        if self.md5_error is not None:
            raise self.md5_error
        return self.md5

    def download(self, dest, md5):
        self._call("download", dest, md5)
        expected = md5 if self.explicit else hashlib.md5(self.pbf_bytes).hexdigest()
        download_pbf(GEOFABRIK_PBF, dest, expected, opener=lambda url: io.BytesIO(self.pbf_bytes))

    def build_osrm(self, pbf, rel):
        self._call("build_osrm", pbf, rel)
        self.root = rel.parents[1]
        data = rel / "osrm" / "data"
        data.mkdir(parents=True)
        copy = data / Path(pbf).name
        shutil.copyfile(pbf, copy)
        (rel / "osrm" / "build_matrix.json").write_text(json.dumps(
            {"input": {"pbf_sha256": "sha-" + self.md5, "osrm_version": "v26.10.0"},
             "steps": {"extract": {"seconds": 1.0, "peak_bytes": int(5e9)}}}), encoding="utf-8")
        return self._result("build_osrm", pbf_copy=copy, pbf_sha256="sha-" + self.md5, profiles_sha256="prof",
                            osrm_version="v26.10.0", extract_peak_bytes=int(5e9))

    def segments(self, pbf_copy):
        self._call("segments", pbf_copy)
        out = self.root / "segments" / f"seg_{self.md5}"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def graph(self, rel, segments_dir):
        self._call("graph", rel, segments_dir)
        (rel / "graph").mkdir()
        return self._result("graph", graph_sha256="graph-" + self.md5)

    def partition(self, rel, previous_release, seed):
        self._call("partition", rel, previous_release, seed)
        (rel / "partition.json").write_text("{}", encoding="utf-8")
        return self._result("partition")

    def rules(self, rel, segments_dir, previous_segments, as_of):
        self._call("rules", rel, segments_dir, previous_segments, as_of)
        return self._result("rules")

    def speeds(self, segments_dir, as_of):
        self._call("speeds", segments_dir, as_of)
        out = self.root / "speeds" / f"spd_{self.md5}"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def groups(self, segments_dir, speeds_dir, out_dir, as_of):
        self._call("groups", segments_dir, speeds_dir, out_dir, as_of)
        out_dir.mkdir(parents=True)
        return out_dir

    def verify(self, rel, speeds_dir):
        self._call("verify", rel, speeds_dir)
        return self._result("verify")


ALL_STEPS = ["remote_md5", "download", "build_osrm", "segments", "graph", "partition", "rules", "speeds", "groups",
             "verify"]


def ticking(start=datetime(2026, 10, 25, 3, 0, 0)):
    """A clock that advances one second per call."""
    state = {"t": start - timedelta(seconds=1)}

    def clock():
        state["t"] += timedelta(seconds=1)
        return state["t"]
    return clock


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def mkdirs(*paths):
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def mkcurrent(root, md5, segments="seg_cur", speeds="spd_cur", rid="20261018-030000", extract_peak=None):
    """A promoted release and current.json pointing at it."""
    rel = root / "releases" / rid
    mkdirs(rel / "groups", root / "segments" / segments, root / "speeds" / speeds)
    doc = {"id": rid, "release": str(rel), "status": "promoted", "pbf_md5": md5,
           "segments_dir": str(root / "segments" / segments), "speeds_dir": str(root / "speeds" / speeds),
           "groups_dir": str(rel / "groups")}
    write_json(rel / "release.json", doc)
    if extract_peak is not None:
        write_json(rel / "osrm" / "build_matrix.json", {"steps": {"extract": {"peak_bytes": extract_peak}}})
    write_json(root / "current.json", {k: doc[k] for k in ("id", "release", "pbf_md5", "segments_dir", "speeds_dir",
                                                          "groups_dir")})
    return rel


def mkrelease(root, rid, status):
    rel = root / "releases" / rid
    write_json(rel / "release.json", {"id": rid, "status": status})
    return rel


def write_lock(root, pid, identity):
    write_json(root / "update.lock", {"pid": pid, "identity": identity})


def jobs(root):
    s = open_state(root / "state.sqlite")
    try:
        return s.jobs()
    finally:
        s.close()


def release_docs(root):
    return {p.parent.name: json.loads(p.read_text(encoding="utf-8"))
            for p in (root / "releases").glob("*/release.json")}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.R = Path(self.temp.name) / "root"
        self.R.mkdir()
        self.ENV = dict(memory=lambda: 64.0, disk=lambda root: 500.0, clock=ticking())
        quiet = mock.patch("sys.stdout", io.StringIO())
        quiet.start()
        self.addCleanup(quiet.stop)

    # ---- run_update ------------------------------------------------------------------------------------------

    def test_first_run_promotes_and_records_everything(self):
        R = self.R
        f = FakeSteps(md5="x")
        self.assertEqual(run_update(R, f, **self.ENV), 0)
        self.assertEqual(f.called, ALL_STEPS)
        cur = json.loads((R / "current.json").read_text(encoding="utf-8"))
        rel = Path(cur["release"])
        self.assertEqual(rel, R / "releases" / "20261025-030000")
        self.assertEqual(set(cur), {"id", "release", "pbf_md5", "pbf_sha256", "graph_sha256", "segments_dir",
                                    "speeds_dir", "groups_dir", "summary", "finished"})
        self.assertEqual((cur["pbf_md5"], cur["pbf_sha256"], cur["graph_sha256"]), ("x", "sha-x", "graph-x"))
        self.assertEqual(Path(cur["groups_dir"]), rel / "groups")
        self.assertEqual(f.args["groups"][2], rel / "groups")
        self.assertEqual(f.args["partition"][1:], (None, None))         # no current release, no seed
        self.assertIsNone(f.args["rules"][2])
        self.assertEqual(list(rel.glob("*.pbf")), [])                    # the downloaded PBF is deleted
        self.assertTrue((rel / "osrm" / "data" / "vietnam-latest.osm.pbf").is_file())   # the build's copy stays
        job = jobs(R)[0]
        self.assertEqual((job["id"], job["kind"], job["status"], job["error"]),
                         ("20261025-030000", "update", "promoted", None))
        self.assertEqual(job["info"]["release"], str(rel))
        self.assertFalse((R / "update.lock").exists())

    def test_unchanged_md5_is_a_no_op(self):
        R = self.R
        mkcurrent(R, "abc")
        f = FakeSteps(md5="abc")
        self.assertEqual(run_update(R, f, **self.ENV), 0)
        self.assertEqual(f.called, ["remote_md5"])
        self.assertEqual(len(list((R / "releases").iterdir())), 1)
        self.assertEqual(jobs(R)[0]["status"], "unchanged")

    def test_force_rebuilds_an_unchanged_md5(self):
        R = self.R
        mkcurrent(R, "abc")
        f = FakeSteps(md5="abc")
        self.assertEqual(run_update(R, f, force=True, **self.ENV), 0)
        self.assertEqual(f.called, ALL_STEPS)
        self.assertEqual(f.args["partition"][1], R / "releases" / "20261018-030000")   # carried from current
        self.assertEqual(f.args["rules"][2], R / "segments" / "seg_cur")

    def test_md5_mismatch_keeps_current_and_removes_part(self):                  # Review Focus 2
        R = self.R
        mkcurrent(R, "old")
        before = (R / "current.json").read_bytes()
        f = FakeSteps(md5="new", pbf_bytes=b"not the PBF")
        self.assertEqual(run_update(R, f, **self.ENV), 2)
        self.assertEqual((R / "current.json").read_bytes(), before)
        self.assertEqual(list(R.rglob("*.part")), [])
        self.assertEqual(list((R / "releases").rglob("*.pbf")), [])
        self.assertNotIn("build_osrm", f.called)
        self.assertIn("does not match its md5", jobs(R)[0]["error"])
        self.assertEqual(jobs(R)[0]["status"], "error")
        failed = [d for rid, d in release_docs(R).items() if rid != "20261018-030000"]
        self.assertEqual([(d["status"], d["error"]) for d in failed],
                         [("error", "UpdateError: downloaded PBF does not match its md5")])

    def test_network_error_is_recorded_without_its_text(self):
        R = self.R
        f = FakeSteps(md5_error=urllib.error.URLError("https://x?key=secret timed out"))
        self.assertEqual(run_update(R, f, **self.ENV), 2)
        err = jobs(R)[0]["error"]
        self.assertTrue(err.startswith("URLError"))
        self.assertNotIn("secret", err)
        self.assertNotIn("secret", sys.stdout.getvalue())
        self.assertNotIn("secret", "".join(p.read_text(encoding="utf-8") for p in (R / "logs").glob("*.log")))
        self.assertFalse((R / "releases").exists() and any((R / "releases").iterdir()))

    def test_os_error_text_is_withheld(self):
        R = self.R
        f = FakeSteps(md5="x", fail={"graph": PermissionError(13, "denied", "C:\\secret\\path")})
        self.assertEqual(run_update(R, f, **self.ENV), 2)
        err = jobs(R)[0]["error"]
        self.assertTrue(err.startswith("PermissionError"))
        self.assertNotIn("secret", err)

    def test_failed_check_keeps_current(self):
        R = self.R
        mkcurrent(R, "old")
        f = FakeSteps(md5="new", checks={"partition": {"contiguity_l3": Check(2, False, True)}})
        self.assertEqual(run_update(R, f, **self.ENV), 1)
        self.assertEqual(json.loads((R / "current.json").read_text())["pbf_md5"], "old")
        self.assertNotIn("rules", f.called)
        self.assertEqual(jobs(R)[0]["status"], "failed_checks")
        doc = [d for rid, d in release_docs(R).items() if rid != "20261018-030000"][0]
        self.assertEqual(doc["status"], "failed_checks")
        self.assertEqual(doc["checks"]["contiguity_l3"], {"value": 2, "ok": False, "blocking": True})

    def test_rule_error_blocks_promotion_and_stops_the_run(self):                 # Review Focus 4
        R = self.R
        f = FakeSteps(md5="new", checks={"rules": {"rules_blocking": Check(["zone_rule"], False, True)}})
        self.assertEqual(run_update(R, f, **self.ENV), 1)
        self.assertNotIn("speeds", f.called)
        self.assertFalse((R / "current.json").exists())
        self.assertEqual(jobs(R)[0]["info"]["failed_checks"], ["rules_blocking"])

    def test_warnings_do_not_block(self):
        R = self.R
        f = FakeSteps(md5="new", checks={"rules": {"rules_new_orphans": Check(3, False, False)}})
        self.assertEqual(run_update(R, f, **self.ENV), 0)
        self.assertEqual(json.loads((R / "current.json").read_text())["pbf_md5"], "new")

    def test_promotion_and_prune_with_unique_ids(self):
        R = self.R
        for md5 in ("a", "b", "c"):                                              # three runs within one minute
            self.assertEqual(run_update(R, FakeSteps(md5=md5), **self.ENV), 0)
        rel = sorted(p.name for p in (R / "releases").iterdir())
        self.assertEqual(len(rel), 2)
        self.assertEqual(len(set(rel)), 2)
        self.assertEqual(json.loads((R / "current.json").read_text())["pbf_md5"], "c")
        self.assertEqual(sorted(p.name for p in (R / "segments").iterdir()), ["seg_b", "seg_c"])
        self.assertEqual(sorted(p.name for p in (R / "speeds").iterdir()), ["spd_b", "spd_c"])

    def test_release_ids_never_repeat_within_a_second(self):
        R = self.R
        still = dict(self.ENV, clock=lambda: datetime(2026, 10, 25, 3, 0, 0))
        self.assertEqual(run_update(R, FakeSteps(md5="a"), **still), 0)
        self.assertEqual(run_update(R, FakeSteps(md5="a"), **still), 0)            # unchanged: a job, no dir
        self.assertEqual(run_update(R, FakeSteps(md5="b"), **still), 0)
        self.assertEqual([j["id"] for j in jobs(R)], ["20261025-030000-3", "20261025-030000-2", "20261025-030000"])
        self.assertEqual(sorted(p.name for p in (R / "releases").iterdir()), ["20261025-030000", "20261025-030000-3"])

    def test_pbf_input_is_never_deleted(self):
        R = self.R
        src = R / "input.osm.pbf"
        src.write_bytes(b"pbf")
        f = FakeSteps()
        self.assertEqual(run_update(R, f, pbf=src, **self.ENV), 0)
        self.assertTrue(src.exists())
        self.assertNotIn("download", f.called)
        self.assertNotIn("remote_md5", f.called)
        self.assertEqual(json.loads((R / "current.json").read_text())["pbf_md5"], hashlib.md5(b"pbf").hexdigest())
        self.assertEqual(f.args["build_osrm"][0], src)

    def test_pbf_input_is_kept_when_the_run_fails(self):
        R = self.R
        src = R / "input.osm.pbf"
        src.write_bytes(b"pbf")
        self.assertEqual(run_update(R, FakeSteps(fail={"segments": RuntimeError("x")}), pbf=src, **self.ENV), 2)
        self.assertTrue(src.exists())

    def test_downloaded_pbf_is_deleted_when_the_build_fails(self):
        R = self.R
        f = FakeSteps(md5="x", fail={"build_osrm": RuntimeError("osrm-extract failed")})
        self.assertEqual(run_update(R, f, **self.ENV), 2)
        self.assertEqual(list((R / "releases").rglob("*.pbf")), [])
        self.assertEqual(jobs(R)[0]["error"], "RuntimeError: osrm-extract failed")

    def test_seed_is_used_only_without_a_current_release(self):
        R = self.R
        seed = R / "seed.npz"
        f = FakeSteps(md5="a")
        run_update(R, f, seed=seed, **self.ENV)
        self.assertEqual(f.args["partition"][1:], (None, seed))
        f = FakeSteps(md5="b")
        run_update(R, f, seed=seed, **self.ENV)
        self.assertEqual(f.args["partition"][1:], (R / "releases" / "20261025-030000", None))

    def test_as_of_is_passed_and_recorded(self):
        R = self.R
        f = FakeSteps(md5="a")
        run_update(R, f, as_of=date(2026, 11, 1), **self.ENV)
        self.assertEqual(f.args["speeds"][1], date(2026, 11, 1))
        self.assertEqual(f.args["rules"][3], date(2026, 11, 1))
        self.assertEqual(next(iter(release_docs(R).values()))["as_of"], "2026-11-01")

    def test_live_lock_refuses_without_touching_anything(self):                       # Review Focus 5
        R = self.R
        write_lock(R, os.getpid(), process_identity(os.getpid()))
        before = (R / "update.lock").read_bytes()
        f = FakeSteps(md5="x")
        self.assertEqual(run_update(R, f, **self.ENV), 2)
        self.assertEqual(f.called, [])
        self.assertFalse((R / "releases").exists())
        self.assertEqual((R / "update.lock").read_bytes(), before)
        self.assertFalse((R / "state.sqlite").exists())

    def test_stale_lock_is_taken_over(self):
        R = self.R
        write_lock(R, DEAD_PID, {"executable": "x", "created": 1})
        self.assertEqual(run_update(R, FakeSteps(md5="x"), **self.ENV), 0)
        self.assertFalse((R / "update.lock").exists())
        write_lock(R, os.getpid(), {"executable": "another.exe", "created": 1})        # live pid, other identity
        self.assertEqual(run_update(R, FakeSteps(md5="y"), **self.ENV), 0)
        (R / "update.lock").write_text("not json", encoding="utf-8")                   # torn write of a crash
        self.assertEqual(run_update(R, FakeSteps(md5="z"), **self.ENV), 0)

    def test_os_lock_refuses_a_second_holder_whatever_the_file_says(self):
        R = self.R
        with update.lock(R):
            # a racing second start, or a torn file, must not let a second run in while the first holds the lock
            write_lock(R, DEAD_PID, {"executable": "x", "created": 1})
            with self.assertRaisesRegex(UpdateError, "another update is running"):
                with update.lock(R):
                    pass
            (R / "update.lock").write_text("torn", encoding="utf-8")
            self.assertEqual(run_update(R, FakeSteps(md5="x"), **self.ENV), 2)
            self.assertFalse((R / "state.sqlite").exists())
            self.assertFalse((R / "releases").exists())

    def test_lock_held_by_another_process(self):
        R = self.R
        code = ("import sys; from pathlib import Path; from speedmatrix.update import lock\n"
                "with lock(Path(sys.argv[1])):\n    print('held', flush=True)\n    sys.stdin.read()\n")
        child = subprocess.Popen([sys.executable, "-c", code, str(R)], cwd=str(update._REPO), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        write_lock(R, DEAD_PID, {"executable": "x", "created": 1})        # the file looks stale; the OS lock is not
        self.assertEqual(run_update(R, FakeSteps(md5="x"), **self.ENV), 2)
        self.assertFalse((R / "state.sqlite").exists())
        child.stdin.close()
        self.assertEqual(child.wait(60), 0)
        self.assertEqual(run_update(R, FakeSteps(md5="x"), **self.ENV), 0)

    def test_lock_records_the_running_step_child(self):
        R = self.R
        seen = []

        def fake(cmd, log, cwd, env=None, on_start=None):
            on_start(os.getpid())                                      # this process stands in for the child
            seen.append(json.loads((R / "update.lock").read_text(encoding="utf-8")))
            return StepResult(0, 1.0, 1, str(R))
        steps = RealSteps(R, None, 5058, R / "l.log")
        with update.lock(R) as held:
            steps.on_child = held.set_child
            with mock.patch("speedmatrix.update.run_measured", fake):
                steps.speeds(R, date(2026, 10, 25))
            after = json.loads((R / "update.lock").read_text(encoding="utf-8"))
        self.assertEqual(seen[0]["child"], {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                                            "step": "speeds"})
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertIsNone(after["child"])

    def test_run_update_hooks_the_step_child_into_the_lock(self):
        R = self.R

        class Hooked(FakeSteps):
            on_child = None

            def graph(self, rel, segments_dir):
                self.on_child(os.getpid(), "graph")
                self.lock_doc = json.loads((rel.parents[1] / "update.lock").read_text(encoding="utf-8"))
                self.on_child(None)
                return super().graph(rel, segments_dir)
        f = Hooked(md5="x")
        self.assertEqual(run_update(R, f, **self.ENV), 0)
        self.assertEqual(f.lock_doc["child"]["pid"], os.getpid())

    def test_live_step_child_of_a_dead_run_keeps_the_lock(self):
        R = self.R
        mkcurrent(R, "a")
        orphan = mkrelease(R, "20261024-030000", "running")                 # the hard-killed run's release
        write_json(R / "update.lock", {"pid": DEAD_PID, "identity": {"executable": "x", "created": 1},
                                       "child": {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                                                 "step": "verify"}})
        before = (R / "update.lock").read_bytes()
        f = FakeSteps(md5="b")
        self.assertEqual(run_update(R, f, **self.ENV), 2)
        self.assertEqual(f.called, [])
        self.assertTrue((orphan / "release.json").is_file())
        self.assertEqual((R / "update.lock").read_bytes(), before)
        self.assertIn("verify", update.status(R)["lock"]["child"]["step"])
        # once that child is gone (or its pid was reused), the lock is stale and taken over
        write_json(R / "update.lock", {"pid": DEAD_PID, "identity": {"executable": "x", "created": 1},
                                       "child": {"pid": os.getpid(), "identity": {"executable": "y", "created": 2},
                                                 "step": "verify"}})
        self.assertEqual(run_update(R, FakeSteps(md5="b"), **self.ENV), 0)

    def test_lock_context_manager(self):
        R = self.R
        with update.lock(R):
            held = json.loads((R / "update.lock").read_text(encoding="utf-8"))
            self.assertEqual(held["pid"], os.getpid())
            with self.assertRaisesRegex(UpdateError, f"another update is running \\(pid {os.getpid()}\\)"):
                with update.lock(R):
                    pass
            self.assertTrue((R / "update.lock").exists())
        self.assertFalse((R / "update.lock").exists())

    def test_disk_floor_from_current_release(self):
        R = self.R
        mkcurrent(R, "a")
        with mock.patch("speedmatrix.update.release_size_gb", return_value=10.0):
            self.assertEqual(run_update(R, FakeSteps(md5="b"), **dict(self.ENV, disk=lambda root: 14.0)), 2)  # 15
            self.assertIn("only 14.0 GB free", jobs(R)[0]["error"])
            self.assertIn("need 15.0", jobs(R)[0]["error"])
            self.assertEqual(run_update(R, FakeSteps(md5="b"), **dict(self.ENV, disk=lambda root: 16.0)), 0)

    def test_disk_floor_without_current_and_override(self):
        R = self.R
        f = FakeSteps(md5="b")
        self.assertEqual(run_update(R, f, **dict(self.ENV, disk=lambda root: 17.9)), 2)       # needs 18
        self.assertEqual(f.called, ["remote_md5"])
        self.assertIn("need 18.0", jobs(R)[0]["error"])
        self.assertEqual(run_update(R, FakeSteps(md5="b"), min_free_gb=5, **dict(self.ENV, disk=lambda root: 6.0)), 0)

    def test_disk_floor_is_checked_after_pruning(self):
        R = self.R
        mkcurrent(R, "a")
        old = mkrelease(R, "20261011-030000", "promoted")
        seen = []

        def disk(root):
            seen.append(old.exists())
            return 500.0
        self.assertEqual(run_update(R, FakeSteps(md5="b"), **dict(self.ENV, disk=disk)), 0)
        self.assertEqual(seen, [False])

    def test_memory_gate(self):
        R = self.R
        f = FakeSteps(md5="x")
        self.assertEqual(run_update(R, f, **dict(self.ENV, memory=lambda: 10.0)), 2)
        self.assertNotIn("build_osrm", f.called)
        self.assertIn("memory", jobs(R)[0]["error"])
        self.assertIn("needs 14.1", jobs(R)[0]["error"])
        self.assertEqual(list((R / "releases").rglob("*.pbf")), [])

    def test_memory_gate_uses_the_recorded_extract_peak(self):
        R = self.R
        mkcurrent(R, "a", extract_peak=int(20e9))
        f = FakeSteps(md5="x")
        self.assertEqual(run_update(R, f, **dict(self.ENV, memory=lambda: 16.0)), 2)
        self.assertIn("the OSRM build needs 21.0", jobs(R)[0]["error"])
        self.assertEqual(run_update(R, FakeSteps(md5="x"), **dict(self.ENV, memory=lambda: 21.5)), 0)

    def test_release_json_records_versions_steps_and_checks(self):
        R = self.R
        run_update(R, FakeSteps(md5="x"), **self.ENV)
        doc = json.loads(next((R / "releases").glob("*/release.json")).read_text())
        self.assertTrue({"code_version", "steps", "checks", "pbf"} <= doc.keys())
        self.assertIn("blocking", next(iter(doc["checks"].values())))
        self.assertEqual(doc["pbf"], GEOFABRIK_PBF)
        self.assertEqual((doc["pbf_md5"], doc["pbf_sha256"], doc["profiles_sha256"], doc["osrm_version"],
                          doc["graph_sha256"]), ("x", "sha-x", "prof", "v26.10.0", "graph-x"))
        self.assertEqual(doc["status"], "promoted")
        self.assertEqual(set(doc["steps"]), set(ALL_STEPS) - {"remote_md5"})
        self.assertIn("seconds", doc["steps"]["graph"])
        self.assertRegex(doc["code_version"], r"^([0-9a-f]{40}(\+dirty)?|unknown)$")
        self.assertEqual(doc["summary"]["partition"], {"step": "partition"})

    def test_steps_measured_peaks_go_into_release_json(self):
        R = self.R
        f = FakeSteps(md5="x")
        f.measured = {"graph": {"seconds": 12.5, "peak_bytes": 123}}
        run_update(R, f, **self.ENV)
        doc = next(iter(release_docs(R).values()))
        self.assertEqual(doc["steps"]["graph"], {"seconds": 12.5, "peak_bytes": 123})

    def test_promotion_restarts_a_live_matrix_server(self):
        R = self.R
        calls = []
        live = {"pid": 1, "port": 5060, "release": "old"}
        with mock.patch("speedmatrix.update._live_server", return_value=live), \
                mock.patch("speedmatrix.update.stop", side_effect=lambda root: calls.append(("stop",)) or live), \
                mock.patch("speedmatrix.update.serve", side_effect=lambda root, port: calls.append(
                    ("serve", port, json.loads((root / "current.json").read_text())["pbf_md5"])) or {}):
            self.assertEqual(run_update(R, FakeSteps(md5="x"), **self.ENV), 0)
        self.assertEqual(calls, [("stop",), ("serve", 5060, "x")])

    def test_failed_run_never_touches_the_served_release(self):
        R = self.R
        mkcurrent(R, "old")
        served = mkrelease(R, "20261011-030000", "promoted")          # served, not current: the prune would drop it
        before = (R / "current.json").read_bytes()
        live = {"pid": 1, "port": 5060, "release": served.name}
        for checks in ({"verify": {"osrm_table_pairs": Check(1, False, True)}}, {}):
            fail = {} if checks else {"speeds": RuntimeError("crash")}
            with (mock.patch("speedmatrix.update._live_server", return_value=live),
                  mock.patch("speedmatrix.update.stop") as stop, mock.patch("speedmatrix.update.serve") as start):
                self.assertEqual(run_update(R, FakeSteps(md5="new", checks=checks, fail=fail), **self.ENV),
                                 1 if checks else 2)
            stop.assert_not_called()
            start.assert_not_called()
            self.assertTrue((served / "release.json").is_file())
            self.assertEqual((R / "current.json").read_bytes(), before)

    # ---- prune -------------------------------------------------------------------------------------------------

    def test_prune_removes_unreferenced_segments_and_speeds(self):
        R = self.R
        mkcurrent(R, "a", segments="seg_a", speeds="spd_a")
        mkdirs(R / "segments/seg_old", R / "speeds/spd_old")
        prune(R, 2, set(), before_build=False)
        self.assertEqual(sorted(p.name for p in (R / "segments").iterdir()), ["seg_a"])
        self.assertFalse((R / "speeds/spd_old").exists())
        self.assertTrue((R / "speeds/spd_a").exists())

    def test_prune_after_build_keeps_newest_promoted_and_newest_failed(self):
        R = self.R
        for rid, status in (("20261001-030000", "promoted"), ("20261008-030000", "failed_checks"),
                            ("20261015-030000", "promoted"), ("20261016-030000", "error"),
                            ("20261017-030000", "running"), ("20261022-030000", "promoted"),
                            ("20261022-030000-10", "promoted"), ("20261022-030000-2", "promoted")):
            mkrelease(R, rid, status)
        (R / "releases" / ".verify_routed_5058.json").write_text("{}", encoding="utf-8")
        (R / "releases" / ".partial").mkdir()
        removed = prune(R, 2, {"20261001-030000"}, before_build=False)
        self.assertEqual(sorted(p.name for p in (R / "releases").iterdir()),
                         [".partial", ".verify_routed_5058.json", "20261001-030000", "20261017-030000",
                          "20261022-030000-10", "20261022-030000-2"])
        self.assertEqual(len(removed), 4)

    def test_prune_before_build_keeps_only_the_protected(self):
        R = self.R
        for rid in ("20261001-030000", "20261008-030000", "20261015-030000"):
            mkrelease(R, rid, "promoted")
        (R / "releases" / ".verify_routed_5058.json").write_text("{}", encoding="utf-8")
        prune(R, 2, {"20261008-030000"}, before_build=True)
        self.assertEqual(sorted(p.name for p in (R / "releases").iterdir()),
                         [".verify_routed_5058.json", "20261008-030000"])

    def test_prune_skips_a_locked_dir(self):
        R = self.R
        mkrelease(R, "20261001-030000", "error")
        warnings = []
        with mock.patch("shutil.rmtree", side_effect=PermissionError(32, "in use", "C:\\secret")):
            self.assertEqual(prune(R, 2, set(), before_build=True, warnings=warnings), [])      # no exception
        self.assertTrue((R / "releases" / "20261001-030000").exists())
        self.assertEqual(len(warnings), 1)
        self.assertIn("PermissionError", warnings[0])
        self.assertNotIn("secret", warnings[0])

    def test_prune_warning_reaches_the_job_info(self):
        R = self.R
        mkcurrent(R, "a")
        mkrelease(R, "20261001-030000", "error")
        real = shutil.rmtree

        def rmtree(path, *a, **k):
            if Path(path).name == "20261001-030000":
                raise PermissionError(32, "in use")
            return real(path, *a, **k)
        with mock.patch("shutil.rmtree", side_effect=rmtree):
            self.assertEqual(run_update(R, FakeSteps(md5="b"), **self.ENV), 0)
        self.assertTrue(any("20261001-030000" in w for w in jobs(R)[0]["info"]["warnings"]))

    def test_prune_refuses_a_path_outside_its_dirs(self):
        R = self.R
        mkdirs(R / "other")
        with self.assertRaises(ValueError):
            update._remove_tree(R, R / "other", [])
        with self.assertRaises(ValueError):
            update._remove_tree(R, R / "releases" / ".." / "other", [])
        self.assertTrue((R / "other").exists())

    def test_release_size_gb(self):
        R = self.R
        rel = mkcurrent(R, "a", segments="seg_a", speeds="spd_a")
        (rel / "big.bin").write_bytes(b"x" * 1000)
        (rel / "groups" / "g.npz").write_bytes(b"x" * 500)                # inside the release: counted once
        (R / "segments/seg_a/s.npz").write_bytes(b"x" * 2000)
        (R / "speeds/spd_a/l.npz").write_bytes(b"x" * 3000)
        (R / "speeds/spd_other").mkdir()
        (R / "speeds/spd_other/l.npz").write_bytes(b"x" * 7000)            # not referenced
        current = json.loads((R / "current.json").read_text())
        size = release_size_gb(R, current)
        own = (rel / "release.json").stat().st_size
        self.assertAlmostEqual(size, (1000 + 500 + 2000 + 3000 + own) / 1e9)

    # ---- download ---------------------------------------------------------------------------------------------

    def test_only_geofabrik(self):
        R = self.R
        with self.assertRaises(ValueError):
            RealSteps(R, 1, 5058, R / "l.log").fetch("https://example.com/x.md5")
        with self.assertRaises(ValueError):
            download_pbf("https://example.com/x.pbf", R / "x.pbf", "0" * 32)
        for url in ("http://download.geofabrik.de/asia/x.pbf", "https://download.geofabrik.de.evil.com/x.pbf",
                    "https://download.geofabrik.de"):
            with self.assertRaises(ValueError):
                check_url(url)
        self.assertEqual(check_url(GEOFABRIK_MD5), GEOFABRIK_MD5)
        self.assertEqual(GEOFABRIK_PBF, "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf")

    def test_download_pbf_checks_md5_and_cleans_up(self):
        R = self.R
        body = b"pbf bytes" * 1000
        dest = R / "v.osm.pbf"
        download_pbf(GEOFABRIK_PBF, dest, hashlib.md5(body).hexdigest().upper(), opener=lambda url: io.BytesIO(body))
        self.assertEqual(dest.read_bytes(), body)
        dest.unlink()
        with self.assertRaisesRegex(UpdateError, "^downloaded PBF does not match its md5$"):
            download_pbf(GEOFABRIK_PBF, dest, "0" * 32, opener=lambda url: io.BytesIO(body))
        self.assertEqual(list(R.iterdir()), [])

        class CutOff(io.BytesIO):
            def read(self, n=-1):
                if self.tell():
                    raise http.client.IncompleteRead(b"", 10)
                return super().read(4)
        with self.assertRaises(UpdateError) as cm:
            download_pbf(GEOFABRIK_PBF, dest, "0" * 32, opener=lambda url: CutOff(body))
        self.assertEqual(str(cm.exception), "IncompleteRead: download from Geofabrik failed")
        self.assertEqual(list(R.iterdir()), [])

        def refuse(url):
            raise urllib.error.URLError("https://x?key=secret refused")
        with self.assertRaises(UpdateError) as cm:
            download_pbf(GEOFABRIK_PBF, dest, "0" * 32, opener=refuse)
        self.assertEqual(str(cm.exception), "URLError: download from Geofabrik failed")
        self.assertIsNone(cm.exception.__cause__)
        self.assertEqual(list(R.iterdir()), [])

    def test_real_steps_fetch_md5_and_download(self):
        R = self.R
        body = b"the pbf"
        md5 = hashlib.md5(body).hexdigest()
        seen = []

        def opener(url):
            seen.append(url)
            return io.BytesIO(f"{md5.upper()}  vietnam-latest.osm.pbf\n".encode() if url.endswith(".md5") else body)
        steps = RealSteps(R, 1, 5058, R / "l.log", opener=opener)
        self.assertEqual(steps.remote_md5(), md5)
        steps.download(R / "v.osm.pbf", md5)
        self.assertEqual(seen, [GEOFABRIK_MD5, GEOFABRIK_PBF])
        self.assertEqual((R / "v.osm.pbf").read_bytes(), body)
        bad = RealSteps(R, 1, 5058, R / "l.log", opener=lambda url: io.BytesIO(b"<html>not found</html>"))
        with self.assertRaisesRegex(UpdateError, "not an md5"):
            bad.remote_md5()

    # ---- RealSteps ----------------------------------------------------------------------------------------------

    def test_real_steps_read_checks_and_treat_crash_as_error(self):
        R = self.R
        rel = R / "releases/r"
        rel.mkdir(parents=True)
        steps = RealSteps(R, 1, 5058, R / "l.log")

        def ok(cmd, log, cwd, env=None):
            (rel / "partition.json").write_text(json.dumps(
                {"checks": {"contiguity_l3": {"value": 1, "ok": False, "blocking": True}}}))
            return StepResult(0, 1.0, 10, str(rel))
        with mock.patch("speedmatrix.update.run_measured", ok):
            self.assertFalse(steps.partition(rel, None, None)["checks"]["contiguity_l3"]["ok"])
        (rel / "partition.json").unlink()
        with mock.patch("speedmatrix.update.run_measured", lambda *a, **k: StepResult(1, 1.0, 10, "")):
            with self.assertRaisesRegex(UpdateError, "partition failed with exit 1"):
                steps.partition(rel, None, None)
        with mock.patch("speedmatrix.update.run_measured", lambda *a, **k: StepResult(0, 1.0, 10, "")):
            with self.assertRaisesRegex(UpdateError, "partition failed with exit 0, see .*l.log"):
                steps.partition(rel, None, None)                               # exit 0 but no partition.json

    def test_real_steps_command_lines(self):
        R = self.R.resolve()
        rel = R / "releases" / "r"
        seg, spd, prev = R / "segments" / "s1", R / "speeds" / "v1", R / "releases" / "p"
        mkdirs(rel / "osrm" / "data", seg, spd, rel / "groups")
        (rel / "osrm" / "data" / "vietnam-latest.osrm.ebg").write_bytes(b"")
        calls = []

        def fake(cmd, log, cwd, env=None):
            calls.append([str(c) for c in cmd])
            self.assertEqual((Path(cwd), env["PYTHONIOENCODING"], Path(log)), (update._REPO, "utf-8", R / "l.log"))
            module = cmd[2]
            if module == "speedmatrix.matrix_osrm":
                write_json(rel / "osrm" / "build_matrix.json", {
                    "input": {"pbf_sha256": "s" * 64, "osrm_version": "v26.10.0"}, "pbf_md5": "m" * 32,
                    "profiles_sha256": "p" * 64, "steps": {"extract": {"seconds": 1, "peak_bytes": 7}}})
            if module == "speedmatrix.graph":
                write_json(rel / "graph" / "meta.json", {"counts": {"segments": 3}, "ambiguous_pairs": 0})
                (rel / "graph" / "seg_len_m.npy").write_bytes(b"abc")
            if module == "speedmatrix.partition":
                write_json(rel / "partition.json", {"checks": {}, "stats": {"levels": {"l1": {"cells": 2}}}})
            if module == "speedmatrix.rulecheck":
                write_json(rel / "rules.json", {"blocking": ["b1"], "summary": {"rules": 4, "new_orphans": 2,
                                                                                "changed": 0}})
            if module == "speedmatrix.verify_osrm":
                write_json(rel / "verify.json", {"osrm_table_pairs": {"value": 9, "ok": True, "blocking": True}})
            last = {"speedmatrix.build": {"segments": seg, "speeds": spd, "groups": rel / "groups"}}
            sub = next((a for a in cmd[3:] if a in ("segments", "speeds", "groups")), None)
            return StepResult(0, 2.0, 1000, str(last.get(module, {}).get(sub, "")))

        steps = RealSteps(R, 6, 5060, R / "l.log")
        with mock.patch("speedmatrix.update.run_measured", fake):
            info = steps.build_osrm(R / "in.osm.pbf", rel)
            self.assertEqual(steps.segments(info["pbf_copy"]), seg)
            g = steps.graph(rel, seg)
            p = steps.partition(rel, prev, None)
            r = steps.rules(rel, seg, R / "segments" / "s0", date(2026, 10, 25))
            self.assertEqual(steps.speeds(seg, date(2026, 10, 25)), spd)
            self.assertEqual(steps.groups(seg, spd, rel / "groups", date(2026, 10, 25)), rel / "groups")
            v = steps.verify(rel, spd)
            steps.partition(rel, None, R / "seed.npz")
        py = sys.executable
        self.assertEqual(calls[0], [py, "-m", "speedmatrix.matrix_osrm", "--pbf", str(R / "in.osm.pbf"),
                                    "--out", str(rel / "osrm"), "--threads", "6"])
        self.assertEqual(calls[1], [py, "-m", "speedmatrix.build", "--root", str(R), "segments", "--pbf",
                                    str(rel / "osrm" / "data" / "in.osm.pbf")])
        self.assertEqual(calls[2], [py, "-m", "speedmatrix.graph", "--osrm-base",
                                    str(rel / "osrm" / "data" / "vietnam-latest.osrm"), "--build-json",
                                    str(rel / "osrm" / "build_matrix.json"), "--segments", str(seg), "--out",
                                    str(rel / "graph")])
        self.assertEqual(calls[3], [py, "-m", "speedmatrix.partition", "run", "--release", str(rel), "--previous",
                                    str(prev), "--workers", "6"])
        self.assertEqual(calls[4], [py, "-m", "speedmatrix.rulecheck", "--root", str(R), "--segments", str(seg),
                                    "--as-of", "2026-10-25", "--out", str(rel / "rules.json"), "--previous",
                                    str(R / "segments" / "s0")])
        self.assertEqual(calls[5], [py, "-m", "speedmatrix.build", "--root", str(R), "speeds", "--segments",
                                    str(seg), "--as-of", "2026-10-25"])
        self.assertEqual(calls[6], [py, "-m", "speedmatrix.build", "--root", str(R), "groups", "--segments",
                                    str(seg), "--speeds", str(spd), "--as-of", "2026-10-25", "--out",
                                    str(rel / "groups")])
        self.assertEqual(calls[7], [py, "-m", "speedmatrix.verify_osrm", "--release", str(rel), "--speeds", str(spd),
                                    "--port", "5060", "--workers", "6"])
        self.assertEqual(calls[8][-4:], ["--seed", str(R / "seed.npz"), "--workers", "6"])
        self.assertEqual((info["pbf_sha256"], info["profiles_sha256"], info["osrm_version"],
                          info["extract_peak_bytes"]), ("s" * 64, "p" * 64, "v26.10.0", 7))
        self.assertRegex(g["graph_sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(p["checks"], {})
        self.assertEqual(r["checks"]["rules_blocking"], {"value": ["b1"], "ok": False, "blocking": True})
        self.assertEqual(r["checks"]["rules_new_orphans"], {"value": 2, "ok": False, "blocking": False})
        self.assertEqual(r["checks"]["rules_changed"], {"value": 0, "ok": True, "blocking": False})
        self.assertEqual(v["checks"]["osrm_table_pairs"]["value"], 9)
        self.assertEqual(steps.measured["verify"], {"seconds": 2.0, "peak_bytes": 1000})

    def test_real_steps_without_workers_let_each_step_size_itself(self):
        R = self.R.resolve()
        rel = R / "releases" / "r"
        calls = []

        def fake(cmd, log, cwd, env=None):
            calls.append([str(c) for c in cmd])
            write_json(rel / "partition.json", {"checks": {}})
            write_json(rel / "verify.json", {})
            write_json(rel / "osrm" / "build_matrix.json", {"input": {"pbf_sha256": "s", "osrm_version": "v"},
                                                            "steps": {"extract": {"peak_bytes": 1}}})
            return StepResult(0, 1.0, 1, "")
        steps = RealSteps(R, None, 5058, R / "l.log")
        with mock.patch("speedmatrix.update.run_measured", fake):
            steps.build_osrm(R / "in.osm.pbf", rel)
            steps.partition(rel, None, None)
            steps.verify(rel, R / "spd")
        self.assertEqual(calls[0][-4:], ["--pbf", str(R / "in.osm.pbf"), "--out", str(rel / "osrm")])
        self.assertEqual(calls[1][-3:], ["run", "--release", str(rel)])
        self.assertEqual(calls[2][-2:], ["--port", "5058"])

    def test_real_steps_stop_on_a_peak_above_the_limit_or_a_missing_dir(self):
        R = self.R
        steps = RealSteps(R, 1, 5058, R / "l.log")
        with mock.patch("speedmatrix.update.run_measured", lambda *a, **k: StepResult(0, 1.0, int(25e9), str(R))):
            with self.assertRaisesRegex(UpdateError, "speeds peaked at 25.0 GB, above the 24 GB limit"):
                steps.speeds(R, date(2026, 10, 25))
        self.assertEqual(steps.measured["speeds"]["peak_bytes"], int(25e9))
        with mock.patch("speedmatrix.update.run_measured", lambda *a, **k: StepResult(0, 1.0, 1, "no such dir")):
            with self.assertRaisesRegex(UpdateError, "speeds printed no output directory"):
                steps.speeds(R, date(2026, 10, 25))

    def test_real_steps_run_the_build_and_rulecheck_clis(self):      # real subprocesses on a tiny .osm, no OSRM
        R = self.R.resolve()
        osm = R / "tiny.osm"
        write_osm(osm, TINY_COORDS, TINY_WAYS)
        self.assertEqual(build_main(["--root", str(R), "import-rules", str(write_json(R / "seed.json", TINY_SEED))]), 0)
        steps = RealSteps(R, None, 5058, R / "logs" / "u.log")
        day = date(2026, 10, 25)
        seg = steps.segments(osm)
        self.assertEqual(seg.parent, R / "segments")
        rel = R / "releases" / "r"
        rel.mkdir(parents=True)
        rules = steps.rules(rel, seg, None, day)
        self.assertEqual(rules["checks"]["rules_blocking"], {"value": ["orphan_tb"], "ok": False, "blocking": True})
        self.assertEqual(rules["summary"]["rules"], 1)
        spd = steps.speeds(seg, day)
        self.assertEqual(spd.parent, R / "speeds")
        self.assertEqual(steps.groups(seg, spd, rel / "groups", day), rel / "groups")
        self.assertEqual(sorted(p.name for p in (rel / "groups").iterdir()), ["lon.npz", "nho.npz", "report.json"])
        self.assertEqual(set(steps.measured), {"segments", "rules", "speeds", "groups"})
        self.assertIn("step groups: exit 0", (R / "logs" / "u.log").read_text(encoding="utf-8"))

    def test_real_steps_refuse_a_protected_port(self):
        with self.assertRaises(ValueError):
            RealSteps(self.R, 1, 5000, self.R / "l.log")

    # ---- server, schedule, status ------------------------------------------------------------------------------

    def test_schedule_command_line(self):
        R = self.R
        seen = []
        self.assertEqual(schedule(R, runner=lambda argv, **kw: seen.append(argv) or SimpleNamespace(returncode=0)), 0)
        argv = seen[0]
        self.assertEqual(argv[:4], ["schtasks", "/Create", "/TN", "HanoiTrafficMatrixUpdate"])
        self.assertEqual(argv[4], "/TR")
        self.assertIn("speedmatrix\\update.py\" run --root", argv[5])
        self.assertTrue(argv[5].startswith(f'"{Path(sys.executable).with_name("pythonw.exe")}" '))
        self.assertTrue(argv[5].endswith(f'--root "{R.resolve()}"'))
        self.assertEqual(argv[6:], ["/SC", "WEEKLY", "/D", "SUN", "/ST", "03:00", "/F"])
        schedule(R, day="WED", time="22:30", runner=lambda argv, **kw: seen.append(argv) or SimpleNamespace(
            returncode=0))
        self.assertEqual(seen[1][6:], ["/SC", "WEEKLY", "/D", "WED", "/ST", "22:30", "/F"])
        for bad in (dict(day="FUNDAY"), dict(time="3am"), dict(time="25:00")):
            with self.assertRaises(ValueError):
                schedule(R, runner=lambda argv, **kw: self.fail("must not run"), **bad)

    def test_unschedule_command_line(self):
        seen = []
        self.assertEqual(unschedule(runner=lambda argv, **kw: seen.append(argv) or SimpleNamespace(returncode=1)), 1)
        self.assertEqual(seen, [["schtasks", "/Delete", "/TN", "HanoiTrafficMatrixUpdate", "/F"]])

    def test_serve_refuses_protected_port(self):
        with self.assertRaises(ValueError):
            serve(self.R, 5000)
        with self.assertRaises(ValueError):
            serve(self.R, 8000)

    def test_serve_needs_a_current_release(self):
        with self.assertRaisesRegex(UpdateError, "no current release"):
            serve(self.R, 5059)

    def test_stop_ignores_a_stale_server_file(self):
        R = self.R
        write_json(R / "server.json", {"pid": DEAD_PID, "identity": {"executable": "x", "created": 1}, "port": 5059})
        self.assertIsNone(update.stop(R))
        self.assertFalse((R / "server.json").exists())
        self.assertIsNone(update.stop(R))

    def test_status_reads_the_state_read_only(self):
        from test_sm_state import SCHEMA_1_DDL, meta_schema
        R = self.R
        db = sqlite3.connect(str(R / "state.sqlite"))
        db.executescript(SCHEMA_1_DDL)
        db.close()
        before = (R / "state.sqlite").read_bytes()
        self.assertEqual(update.status(R)["jobs"], [])                     # schema 1 has no jobs: none, no upgrade
        self.assertEqual(meta_schema(R / "state.sqlite"), "1")
        self.assertEqual((R / "state.sqlite").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in R.iterdir()), ["state.sqlite"])

    def test_status(self):
        R = self.R
        self.assertEqual(update.status(R)["current"], None)
        self.assertFalse((R / "state.sqlite").exists())                     # status creates nothing
        run_update(R, FakeSteps(md5="x"), **self.ENV)
        st = update.status(R)
        self.assertEqual(st["current"]["pbf_md5"], "x")
        self.assertEqual(st["releases"], [{"id": "20261025-030000", "status": "promoted"}])
        self.assertEqual(st["jobs"][0]["status"], "promoted")
        self.assertIsNone(st["server"])
        self.assertIsNone(st["lock"])

    def test_cli_parses_run_options(self):
        R = self.R
        seen = {}

        def fake_run(root, steps, **kw):
            seen.update(root=root, steps=steps, **kw)
            return 0
        with mock.patch("speedmatrix.update.run_update", fake_run):
            code = update.main(["run", "--root", str(R), "--pbf", str(R / "in.pbf"), "--force", "--keep", "3",
                                "--workers", "4", "--build-port", "5060", "--min-free-gb", "9.5", "--seed",
                                str(R / "seed.npz"), "--as-of", "2026-11-01"])
        self.assertEqual(code, 0)
        self.assertEqual((seen["root"], seen["pbf"], seen["force"], seen["keep"], seen["min_free_gb"], seen["seed"],
                          seen["as_of"]), (R.resolve(), (R / "in.pbf").resolve(), True, 3, 9.5,
                                           (R / "seed.npz").resolve(), date(2026, 11, 1)))
        self.assertEqual((seen["steps"].workers, seen["steps"].build_port), (4, 5060))
        self.assertEqual(seen["steps"].log_path.parent, R.resolve() / "logs")


if __name__ == "__main__":
    unittest.main()
