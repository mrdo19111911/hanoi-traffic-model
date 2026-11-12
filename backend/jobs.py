"""Bounded in-memory background jobs and the local worker that runs them.

Spec:
- ``JobManager(runner)``: one queued/running job per kind (409 otherwise), at most 100 remembered jobs,
  ``get`` raises 404 for unknown ids. A failing runner never leaks its exception text (subprocess and
  HTTP messages can contain the API key); the job gets a fixed Vietnamese error.
- ``subprocess_runner(root, key_reader)`` runs ``download/crawl_spatial.py`` (kind ``crawl``, key passed
  only through the child environment) or ``model/compare_spatial.py`` (kind ``experiments``) and raises
  ``RuntimeError`` on a missing key or a non-zero exit. State lives in the process: run one worker.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import subprocess
import sys
import threading
import uuid

from fastapi import HTTPException

from .samples import data_status

JOB_TIMEOUT_S = 1800
MAX_JOBS = 100
JOB_FAILED = "Tác vụ chưa hoàn tất. Kiểm tra dữ liệu, cấu hình API và kết nối dịch vụ trên máy chủ."


def now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, runner):
        self.runner = runner
        self.lock = threading.Lock()
        self.jobs = {}

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise HTTPException(404, "Không tìm thấy tác vụ.")
            return dict(job)

    def submit(self, kind, payload):
        with self.lock:
            if any(job["kind"] == kind and job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise HTTPException(409, "Tác vụ cùng loại đang chạy. Vui lòng đợi hoàn tất.")
            # Retain recent jobs without allowing an unbounded process history.
            if len(self.jobs) >= MAX_JOBS:
                oldest = next((key for key, job in self.jobs.items() if job["status"] in ("completed", "failed")), None)
                if oldest:
                    del self.jobs[oldest]
            job = {"id": uuid.uuid4().hex, "kind": kind, "status": "queued", "created_at": now(),
                   "started_at": None, "finished_at": None, "result": None, "error": None}
            self.jobs[job["id"]] = job
            snapshot = dict(job)
        threading.Thread(target=self._run, args=(job["id"], kind, payload), daemon=True).start()
        return snapshot

    def _run(self, job_id, kind, payload):
        with self.lock:
            self.jobs[job_id].update(status="running", started_at=now())
        try:
            result = self.runner(kind, payload)
            with self.lock:
                self.jobs[job_id].update(status="completed", result=result, finished_at=now())
        except Exception:
            # Subprocess/HTTP exception strings can include API keys and URLs.
            with self.lock:
                self.jobs[job_id].update(status="failed", finished_at=now(), error=JOB_FAILED)


def subprocess_runner(root, key_reader):
    """Return a ``runner(kind, payload)`` that executes the crawl or the comparison as a child process."""
    def run_job(kind, payload):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        if kind == "crawl":
            key = key_reader()
            if not key:
                raise RuntimeError("Missing API configuration")
            environment["TOMTOM_API_KEY"] = key
            command = [sys.executable, str(root / "download/crawl_spatial.py"), "--date", payload["date"],
                       "--slots", *payload["slots"], "--max-calls", str(payload["max_calls"])]
        else:
            command = [sys.executable, str(root / "model/compare_spatial.py")]
        completed = subprocess.run(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=JOB_TIMEOUT_S, check=False)
        if completed.returncode:
            raise RuntimeError("Local worker failed")
        return data_status(root) if kind == "crawl" else {"report_available": (root / "reports/spatial_comparison.json").is_file()}
    return run_job
