"""The weekly release pipeline of the speed matrix: ``python -m speedmatrix.update <command> [--root DIR]``.

``run [--pbf PATH] [--force] [--keep 2] [--workers N] [--build-port 5058] [--min-free-gb GB] [--seed FILE]
[--as-of DATE]`` builds a new release in ``<root>/releases/<YYYYMMDD-HHMMSS>/`` from the newest Geofabrik PBF (or
``--pbf``):

1. takes ``<root>/update.lock`` (a live lock exits 2 before anything is written; a stale one is taken over), starts
   a job row in ``<root>/state.sqlite``;
2. compares the PBF md5 with ``current.json``: unchanged and no ``--force`` finishes the job as ``unchanged``;
3. prunes every release but the current and served ones, then refuses to start below the disk floor
   (1.5 x the current release's measured size, 18 GB without one, or ``--min-free-gb``);
4. runs the steps, each a measured subprocess: download (md5 checked), the memory gate, the matrix OSRM build,
   segments, the turn graph, partition and gates, rules, speeds, group views (into the release) and §12.1;
5. stops at the first step with a failed blocking check (status ``failed_checks``, exit 1; Decision 16);
6. promotes: ``release.json`` status ``promoted``, ``current.json`` written atomically, a live matrix server
   restarted on the new release, old releases and unreferenced segments/speeds dirs pruned.

A failed run never changes ``current.json``, the served release or the rules in ``state.sqlite``. Any error gives
status ``error`` and exit 2; the job records the exception class and, for urllib and OS errors, a fixed text instead
of the exception's own (spec §13). The only network calls go to ``https://download.geofabrik.de/``.

``serve [--port 5059]`` starts osrm-routed on the current release (detached, recorded in ``<root>/server.json``);
``stop`` stops it (identity checked); ``status`` prints the current release, server, lock, releases and last jobs;
``schedule [--day SUN] [--time 03:00]`` / ``unschedule`` register or remove the weekly Windows scheduled task
``HanoiTrafficMatrixUpdate`` (Decision 24). This replaces ``reports/gate_compose_spike/update_matrix.py``.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Protocol
import urllib.error
import urllib.request

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started as a script (the scheduled task)
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "osrm"))
try:
    import manage  # noqa: E402
finally:
    sys.path.remove(str(_REPO / "osrm"))

try:
    from .state import open_state
    from .sysres import available_memory_gb, free_disk_gb, run_measured
    from .verify_osrm import (ROUTED_MAX_TABLE, ROUTED_READY_S, ROUTED_THREADS, _answers, _check_port, _free_port,
                              _terminate_if, dataset_base)
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.state import open_state
    from speedmatrix.sysres import available_memory_gb, free_disk_gb, run_measured
    from speedmatrix.verify_osrm import (ROUTED_MAX_TABLE, ROUTED_READY_S, ROUTED_THREADS, _answers, _check_port,
                                         _free_port, _terminate_if, dataset_base)

TASK_NAME = "HanoiTrafficMatrixUpdate"
BUILD_PORT = 5058
SERVE_PORT = 5059
KEEP = 2
MAX_STEP_PEAK_GB = 24
DISK_FACTOR = 1.5
BOOTSTRAP_RELEASE_GB = 12
EXTRACT_PEAK_DEFAULT_GB = 13.1
GEOFABRIK_PBF = manage.MAP_URL
GEOFABRIK_MD5 = GEOFABRIK_PBF + ".md5"
GEOFABRIK_PREFIX = "https://download.geofabrik.de/"
PBF_NAME = GEOFABRIK_PBF.rsplit("/", 1)[1]
DEFAULT_ROOT = _REPO / "runtime" / "matrix"
DOWNLOAD_TIMEOUT_S = 120
DOWNLOAD_CHUNK = 1 << 20
WITHHELD = "error text withheld (spec 13)"
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
EXIT = {"promoted": 0, "unchanged": 0, "failed_checks": 1}
_TZ = timezone(timedelta(hours=7))
_RELEASE_NAME = re.compile(r"^(?P<base>\d{8}-\d{6})(?:-(?P<n>\d+))?$")


class UpdateError(RuntimeError):
    """A pipeline failure with a fixed message (never the text of a urllib or OS error)."""


class _ChecksFailed(Exception):
    def __init__(self, names):
        super().__init__(", ".join(names))
        self.names = list(names)


@dataclass
class Check:
    value: object
    ok: bool
    blocking: bool


def describe(exc: BaseException) -> str:
    """``<Class>: <message>`` for the job row and the log; urllib, http.client and OS errors get a fixed text."""
    name = type(exc).__name__
    if isinstance(exc, UpdateError):
        return f"{name}: {exc}"
    if isinstance(exc, (OSError, http.client.HTTPException)):          # urllib.error.URLError is an OSError
        return f"{name}: {WITHHELD}"
    return f"{name}: {exc}"


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _read_json(path: Path):
    """The parsed file, or None when it does not exist."""
    path = Path(path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, doc) -> None:
    """Write to a temp sibling and rename it into place."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str), encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _abs(root: Path, p) -> Path:
    p = Path(p)
    return p if p.is_absolute() else Path(root) / p


def _release_dir(root: Path, current: dict) -> Path:
    return _abs(root, current["release"]) if current.get("release") else Path(root) / "releases" / current["id"]


class _RunLog:
    """Timestamped lines appended to the run log (and printed when there is a console)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __call__(self, message: str) -> None:
        line = f"[{datetime.now(_TZ):%Y-%m-%d %H:%M:%S}] {message}"
        print(line, flush=True)                              # a no-op under pythonw (sys.stdout is None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---- processes ---------------------------------------------------------------------------------------------------

def process_identity(pid: int):
    """{executable, created} of a live process on Windows (``osrm/manage.py``); elsewhere {pid} when
    ``os.kill(pid, 0)`` finds it. None when there is no such process."""
    if os.name == "nt":
        return manage.process_identity(int(pid))
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    except (OSError, OverflowError):
        return None
    return {"pid": int(pid)}


def _is_live(doc) -> bool:
    """True when ``doc`` records a pid whose process identity is still the recorded one."""
    if not isinstance(doc, dict) or not isinstance(doc.get("pid"), int) or not doc.get("identity"):
        return False
    return process_identity(doc["pid"]) == doc["identity"]


def _terminate(pid: int, identity: dict) -> str:
    if os.name == "nt":
        return _terminate_if(pid, identity)
    import signal
    os.kill(pid, signal.SIGTERM)
    return "stopped"


# ---- the lock ----------------------------------------------------------------------------------------------------

def _read_lock(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                      # unreadable (a crash during the write): stale


def _take_lock(root: Path) -> dict:
    path = Path(root) / "update.lock"
    me = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = _read_lock(path)
            if _is_live(held):
                raise UpdateError(f"another update is running (pid {held['pid']})") from None
            path.unlink(missing_ok=True)                # gone, or a reused pid with another identity
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(me, f)
        return json.loads(json.dumps(me))
    raise UpdateError("could not take update.lock")


def _release_lock(root: Path, me: dict) -> None:
    path = Path(root) / "update.lock"
    if _read_lock(path) == me:
        path.unlink(missing_ok=True)


@contextmanager
def lock(root):
    """Hold ``<root>/update.lock`` ({pid, identity}) for the block. A live lock raises UpdateError; a lock whose
    process is gone or has another identity is taken over. The spike used the same file (Decision 24)."""
    me = _take_lock(root)
    try:
        yield me
    finally:
        _release_lock(root, me)


# ---- download ----------------------------------------------------------------------------------------------------

def check_url(url) -> str:
    if not isinstance(url, str) or not url.startswith(GEOFABRIK_PREFIX):
        raise ValueError(f"only {GEOFABRIK_PREFIX} may be fetched, not {url!r}")
    return url


class _GeofabrikRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another Geofabrik download URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GeofabrikRedirects())


def geofabrik_open(url):
    """``urlopen`` with a timeout, refusing any non-Geofabrik URL, redirects included."""
    return _OPENER.open(check_url(url), timeout=DOWNLOAD_TIMEOUT_S)


def download_pbf(url, dest: Path, md5: str, opener=geofabrik_open) -> None:
    """Stream ``url`` into ``<dest>.part``, check its md5 and rename it to ``dest``. The ``.part`` is removed on a
    mismatch or a failure; a urllib or OS error raises UpdateError with a fixed text (spec §13)."""
    check_url(url)
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            with opener(url) as response, part.open("wb") as out:
                while chunk := response.read(DOWNLOAD_CHUNK):
                    out.write(chunk)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise UpdateError(f"{type(exc).__name__}: download from Geofabrik failed") from None
        if manage.digest(part, "md5") != str(md5).strip().lower():
            raise UpdateError("downloaded PBF does not match its md5")
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


# ---- disk --------------------------------------------------------------------------------------------------------

def _dir_bytes(d: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(d):
        for name in files:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def release_size_gb(root, current: dict) -> float:
    """Measured size of the current release dir plus the segments, speeds and groups dirs it references (a dir
    inside another one is counted once)."""
    dirs = [_release_dir(root, current)] + [_abs(root, current[k]) for k in ("segments_dir", "speeds_dir",
                                                                            "groups_dir") if current.get(k)]
    dirs = list(dict.fromkeys(d.resolve() for d in dirs if d.is_dir()))
    outer = [d for d in dirs if not any(o != d and d.is_relative_to(o) for o in dirs)]
    return sum(_dir_bytes(d) for d in outer) / 1e9


def _drive(root: Path) -> str:
    return Path(root).resolve().anchor or str(root)


# ---- prune -------------------------------------------------------------------------------------------------------

def _release_key(name: str):
    """(timestamp, n) so that ``<id>-10`` sorts after ``<id>-2``; other names (the spike's) sort by name."""
    m = _RELEASE_NAME.match(name)
    return (m.group("base"), int(m.group("n") or 1)) if m else (name, 1)


def _releases(root: Path) -> list:
    """Release dirs, newest first; dot entries (``.verify_routed_<port>.json``, temp dirs) are not releases."""
    d = Path(root) / "releases"
    if not d.is_dir():
        return []
    return sorted((p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: _release_key(p.name), reverse=True)


def _status(rel: Path):
    try:
        return (_read_json(rel / "release.json") or {}).get("status")
    except (OSError, ValueError):
        return None


def _remove_tree(root: Path, path: Path, warnings) -> bool:
    """Remove a release, segments or speeds dir; anything else raises ValueError. A dir that cannot be removed (a
    mapped .npy, an open file) is skipped with a warning."""
    allowed = {(Path(root) / s).resolve() for s in ("releases", "segments", "speeds")}
    target = Path(path).resolve()
    if target.parent not in allowed:
        raise ValueError(f"refusing to remove {path}: not a dir of {Path(root)}/releases, segments or speeds")
    try:
        shutil.rmtree(target)
        return True
    except OSError as exc:
        message = f"could not remove {target} ({type(exc).__name__}); skipped"
        if warnings is None:
            print(f"warning: {message}", file=sys.stderr)
        else:
            warnings.append(message)
        return False


def _references(root: Path, releases) -> set:
    """Resolved segments and speeds dirs referenced by ``releases`` and by ``current.json``."""
    docs = [_status_doc(p) for p in releases]
    try:
        docs.append(_read_json(Path(root) / "current.json") or {})
    except (OSError, ValueError):
        pass
    refs = set()
    for doc in docs:
        for key in ("segments_dir", "speeds_dir"):
            if doc.get(key):
                refs.add(_abs(root, doc[key]).resolve())
    return refs


def _status_doc(rel: Path) -> dict:
    try:
        return _read_json(rel / "release.json") or {}
    except (OSError, ValueError):
        return {}


def prune(root, keep: int, protect: set, before_build: bool, warnings: list | None = None) -> list:
    """Remove old releases and the segments/speeds dirs no kept release references; returns the removed paths.

    Before a build every release but ``protect`` goes; after one, the newest ``keep`` promoted releases, the newest
    failed one and ``protect`` stay (Decision 22). Skipped dirs are reported in ``warnings`` (else on stderr)."""
    root = Path(root)
    rels = _releases(root)
    if before_build:
        kept = [p for p in rels if p.name in protect]
    else:
        promoted = [p for p in rels if _status(p) == "promoted"]
        failed = [p for p in rels if _status(p) != "promoted"]
        kept = [p for p in rels if p in promoted[:keep] or p in failed[:1] or p.name in protect]
    removed = []
    for p in rels:
        if p not in kept and _remove_tree(root, p, warnings):
            removed.append(str(p))
    refs = _references(root, kept)
    for sub in ("segments", "speeds"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and p.resolve() not in refs and _remove_tree(root, p,
                                                                                                      warnings):
                removed.append(str(p))
    return removed


# ---- the steps ---------------------------------------------------------------------------------------------------

class Steps(Protocol):
    """The pipeline steps. Each dict result may hold ``"checks": {name: {value, ok, blocking, ...}}`` and
    ``"summary"``."""

    def remote_md5(self) -> str: ...

    def download(self, dest: Path, md5: str) -> None: ...

    def build_osrm(self, pbf, rel) -> dict: ...                  # pbf_copy, pbf_sha256, extract_peak_bytes, ...

    def segments(self, pbf_copy) -> Path: ...

    def graph(self, rel, segments_dir) -> dict: ...                # graph_sha256

    def partition(self, rel, previous_release: Path | None, seed: Path | None) -> dict: ...

    def rules(self, rel, segments_dir, previous_segments: Path | None, as_of) -> dict: ...

    def speeds(self, segments_dir, as_of) -> Path: ...

    def groups(self, segments_dir, speeds_dir, out_dir: Path, as_of) -> Path: ...

    def verify(self, rel, speeds_dir) -> dict: ...


def _tree_sha256(directory: Path) -> str:
    """sha256 over the sorted relative paths and bytes of the files under ``directory``, read in chunks."""
    directory = Path(directory)
    h = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        h.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
        with path.open("rb") as f:
            while chunk := f.read(1 << 24):
                h.update(chunk)
    return h.hexdigest()


class RealSteps:
    """Each heavy step is a measured subprocess ``python -m speedmatrix.<module> ...`` run from the repo root; a
    step CLI exits 0 once it wrote its JSON, anything else is an error (Decision 27)."""

    def __init__(self, root, workers, build_port, log_path, opener=geofabrik_open):
        self.root = Path(root)
        self.workers = workers
        self.build_port = _check_port(build_port)
        self.log_path = Path(log_path)
        self.opener = opener
        self.measured = {}
        self._log = _RunLog(self.log_path)

    # network: Geofabrik only
    def fetch(self, url) -> bytes:
        check_url(url)
        with self.opener(url) as response:
            return response.read()

    def remote_md5(self) -> str:
        tokens = self.fetch(GEOFABRIK_MD5).decode("ascii", "replace").split()
        md5 = tokens[0].lower() if tokens else ""
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            raise UpdateError("the Geofabrik .md5 file is not an md5")
        return md5

    def download(self, dest: Path, md5: str) -> None:
        download_pbf(GEOFABRIK_PBF, dest, md5, self.opener)

    # subprocesses
    def _workers(self, flag="--workers") -> list:
        return [flag, str(self.workers)] if self.workers else []

    def _failed(self, name, code) -> UpdateError:
        return UpdateError(f"{name} failed with exit {code}, see {self.log_path}")

    def _run(self, name: str, module: str, args: list):
        cmd = [sys.executable, "-m", f"speedmatrix.{module}", *[str(a) for a in args]]
        self._log(f"step {name}: {' '.join(cmd)}")
        result = run_measured(cmd, self.log_path, _REPO, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.measured[name] = {"seconds": round(result.seconds, 1), "peak_bytes": result.peak_bytes}
        peak = "unknown" if result.peak_bytes is None else f"{result.peak_bytes / 1e9:.1f} GB"
        self._log(f"step {name}: exit {result.code} after {result.seconds:.0f} s, peak {peak}")
        if result.code != 0:
            raise self._failed(name, result.code)
        if result.peak_bytes is not None and result.peak_bytes > MAX_STEP_PEAK_GB * 1e9:
            raise UpdateError(f"{name} peaked at {result.peak_bytes / 1e9:.1f} GB, above the {MAX_STEP_PEAK_GB} GB "
                              f"limit")
        return result

    def _json(self, name, result, path: Path):
        if not Path(path).is_file():
            raise self._failed(name, result.code)
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def _dir(self, name, result) -> Path:
        out = Path(result.last_line) if result.last_line else None
        if out is None or not out.is_dir():
            raise UpdateError(f"{name} printed no output directory, see {self.log_path}")
        return out

    def build_osrm(self, pbf, rel) -> dict:
        out = Path(rel) / "osrm"
        result = self._run("build_osrm", "matrix_osrm", ["--pbf", pbf, "--out", out, *self._workers("--threads")])
        doc = self._json("build_osrm", result, out / "build_matrix.json")
        return {"pbf_copy": out / "data" / Path(pbf).name, "pbf_sha256": doc["input"]["pbf_sha256"],
                "osrm_version": doc["input"]["osrm_version"], "profiles_sha256": doc.get("profiles_sha256"),
                "pbf_md5": doc.get("pbf_md5"), "extract_peak_bytes": doc["steps"]["extract"]["peak_bytes"],
                "summary": {"osrm_steps": doc["steps"]}}

    def segments(self, pbf_copy) -> Path:
        return self._dir("segments", self._run("segments", "build", ["--root", self.root, "segments", "--pbf",
                                                                       pbf_copy]))

    def graph(self, rel, segments_dir) -> dict:
        rel = Path(rel)
        result = self._run("graph", "graph", [
            "--osrm-base", dataset_base(rel / "osrm" / "data"), "--build-json", rel / "osrm" / "build_matrix.json",
            "--segments", segments_dir, "--out", rel / "graph"])
        meta = self._json("graph", result, rel / "graph" / "meta.json")
        return {"graph_sha256": _tree_sha256(rel / "graph"),
                "summary": {k: meta.get(k) for k in ("counts", "ambiguous_pairs", "obstacle_entries")}}

    def partition(self, rel, previous_release, seed) -> dict:
        rel = Path(rel)
        source = ["--previous", previous_release] if previous_release else ["--seed", seed] if seed else []
        result = self._run("partition", "partition", ["run", "--release", rel, *source, *self._workers()])
        doc = self._json("partition", result, rel / "partition.json")
        return {"checks": doc.get("checks") or {},
                "summary": {"stats": doc.get("stats"), "gate_stats": doc.get("gate_stats")}}

    def rules(self, rel, segments_dir, previous_segments, as_of) -> dict:
        out = Path(rel) / "rules.json"
        args = ["--root", self.root, "--segments", segments_dir]
        args += ["--as-of", as_of.isoformat()] if as_of else []
        args += ["--out", out] + (["--previous", previous_segments] if previous_segments else [])
        doc = self._json("rules", self._run("rules", "rulecheck", args), out)
        summary = doc["summary"]
        blocking = list(doc["blocking"])
        checks = {"rules_blocking": Check(blocking, not blocking, True),
                  "rules_new_orphans": Check(summary["new_orphans"], summary["new_orphans"] == 0, False),
                  "rules_changed": Check(summary["changed"], summary["changed"] == 0, False)}
        return {"checks": {k: asdict(v) for k, v in checks.items()}, "summary": summary}

    def speeds(self, segments_dir, as_of) -> Path:
        args = ["--root", self.root, "speeds", "--segments", segments_dir]
        args += ["--as-of", as_of.isoformat()] if as_of else []
        return self._dir("speeds", self._run("speeds", "build", args))

    def groups(self, segments_dir, speeds_dir, out_dir: Path, as_of) -> Path:
        args = ["--root", self.root, "groups", "--segments", segments_dir, "--speeds", speeds_dir]
        args += (["--as-of", as_of.isoformat()] if as_of else []) + ["--out", out_dir]
        return self._dir("groups", self._run("groups", "build", args))

    def verify(self, rel, speeds_dir) -> dict:
        rel = Path(rel)
        result = self._run("verify", "verify_osrm", ["--release", rel, "--speeds", speeds_dir, "--port",
                                                     self.build_port, *self._workers()])
        checks = self._json("verify", result, rel / "verify.json")
        return {"checks": checks, "summary": {name: c.get("value") for name, c in checks.items()}}


# ---- run ---------------------------------------------------------------------------------------------------------

def code_version(runner=subprocess.run) -> str:
    """``git rev-parse HEAD`` plus ``+dirty`` when ``git status --porcelain -- speedmatrix`` is not empty."""
    kw = dict(cwd=str(_REPO), capture_output=True, text=True, check=True)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        head = runner(["git", "rev-parse", "HEAD"], **kw).stdout.strip()
        dirty = runner(["git", "status", "--porcelain", "--", "speedmatrix"], **kw).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return head + ("+dirty" if dirty else "") if head else "unknown"


def _extract_peak_gb(root: Path, current) -> float:
    if current:
        doc = _read_json(_release_dir(root, current) / "osrm" / "build_matrix.json") or {}
        peak = ((doc.get("steps") or {}).get("extract") or {}).get("peak_bytes")
        if peak:
            return peak / 1e9
    return EXTRACT_PEAK_DEFAULT_GB


def _live_server(root: Path):
    try:
        doc = _read_json(Path(root) / "server.json")
    except (OSError, ValueError):
        return None
    return doc if _is_live(doc) else None


def _new_id(root: Path, state, now: datetime) -> str:
    base = now.strftime("%Y%m%d-%H%M%S")
    rid, n = base, 1
    while (Path(root) / "releases" / rid).exists() or state.job(rid) is not None:
        n += 1
        rid = f"{base}-{n}"
    return rid


class _Pipeline:
    """One update run after the lock is held and the job row started."""

    def __init__(self, root, steps, rid, log, clock, warnings):
        self.root, self.steps, self.rid, self.log, self.clock = Path(root), steps, rid, log, clock
        self.rel = self.root / "releases" / rid
        self.record = None
        self.downloaded = None
        self.warnings = warnings
        self.info = {"release": None}

    def write_record(self) -> None:
        _write_json(self.rel / "release.json", self.record)

    def step(self, name, fn):
        """Run one step; record its time, peak, checks and summary; stop on a failed blocking check."""
        started = time.monotonic()
        try:
            result = fn()
        finally:
            entry = {"seconds": round(time.monotonic() - started, 1)}
            entry.update((getattr(self.steps, "measured", None) or {}).get(name, {}))
            self.record["steps"][name] = entry
            self.write_record()
        if isinstance(result, dict):
            failed = []
            for check_name, check in (result.get("checks") or {}).items():
                check = asdict(check) if isinstance(check, Check) else dict(check)
                if not {"value", "ok", "blocking"} <= check.keys():
                    raise UpdateError(f"{name}: check {check_name} lacks value, ok or blocking")
                self.record["checks"][check_name] = check
                if check["blocking"] and not check["ok"]:
                    failed.append(check_name)
            if result.get("summary") is not None:
                self.record["summary"][name] = result["summary"]
            self.write_record()
            if failed:
                self.log(f"step {name}: blocking checks failed: {', '.join(failed)}")
                raise _ChecksFailed(failed)
        self.log(f"step {name}: done")
        return result

    def run(self, pbf, force, keep, min_free_gb, as_of, seed, memory, disk) -> str:
        root, steps, rel = self.root, self.steps, self.rel
        current = _read_json(root / "current.json")
        if pbf is not None:
            pbf = Path(pbf)
            if not pbf.is_file():
                raise UpdateError(f"PBF not found: {pbf}")
            md5 = manage.digest(pbf, "md5")
        else:
            md5 = steps.remote_md5()
        self.info["pbf_md5"] = md5
        self.log(f"PBF md5 {md5}, current release {current.get('id') if current else None} "
                 f"md5 {current.get('pbf_md5') if current else None}")
        if current and current.get("pbf_md5") == md5 and not force:
            self.log("PBF unchanged, nothing to do")
            return "unchanged"

        protect = {current["id"]} if current else set()
        served = _live_server(root)
        if served:
            protect.add(served.get("release"))
        removed = prune(root, keep, protect, before_build=True, warnings=self.warnings)
        self.info["pruned"] = removed
        for path in removed:
            self.log(f"pruned {path}")
        if min_free_gb is not None:
            need = float(min_free_gb)
        elif current:
            need = DISK_FACTOR * release_size_gb(root, current)
        else:
            need = DISK_FACTOR * BOOTSTRAP_RELEASE_GB
        free = disk(root)
        self.log(f"disk: {free:.1f} GB free on {_drive(root)}, floor {need:.1f} GB")
        if free < need:
            raise UpdateError(f"only {free:.1f} GB free on {_drive(root)}, need {need:.1f}")

        rel.mkdir(parents=True)
        self.info["release"] = str(rel)
        as_of = as_of or datetime.now(_TZ).date()
        previous = _release_dir(root, current) if current else None
        seed_used = None if current else (Path(seed) if seed else None)
        if current and seed:
            self.log("--seed ignored: labels carry over from the current release")
        self.record = {"id": self.rid, "status": "running", "started": self.clock().isoformat(timespec="seconds"),
                       "pbf": str(pbf) if pbf is not None else GEOFABRIK_PBF, "pbf_md5": md5, "force": bool(force),
                       "as_of": as_of.isoformat(), "code_version": code_version(),
                       "previous": str(previous) if previous else None,
                       "seed": str(seed_used) if seed_used else None, "steps": {}, "checks": {}, "summary": {}}
        self.write_record()

        if pbf is None:
            pbf = rel / PBF_NAME
            self.downloaded = pbf
            self.step("download", lambda: steps.download(pbf, md5))
        need_gb = _extract_peak_gb(root, current) + 1.0
        have_gb = memory()
        self.log(f"memory: {have_gb:.1f} GB available, the OSRM build needs {need_gb:.1f}")
        if have_gb < need_gb:
            raise UpdateError(f"only {have_gb:.1f} GB memory available, the OSRM build needs {need_gb:.1f}")
        built = self.step("build_osrm", lambda: steps.build_osrm(pbf, rel))
        self.drop_download()
        for key in ("pbf_sha256", "profiles_sha256", "osrm_version"):
            self.record[key] = built.get(key)
        pbf_copy = Path(built["pbf_copy"])
        segments_dir = Path(self.step("segments", lambda: steps.segments(pbf_copy)))
        self.record["segments_dir"] = str(segments_dir)
        graph = self.step("graph", lambda: steps.graph(rel, segments_dir))
        self.record["graph_sha256"] = graph.get("graph_sha256")
        self.step("partition", lambda: steps.partition(rel, previous, seed_used))
        prev_segments = _abs(root, current["segments_dir"]) if current and current.get("segments_dir") else None
        self.step("rules", lambda: steps.rules(rel, segments_dir, prev_segments, as_of))
        speeds_dir = Path(self.step("speeds", lambda: steps.speeds(segments_dir, as_of)))
        self.record["speeds_dir"] = str(speeds_dir)
        groups_dir = Path(self.step("groups", lambda: steps.groups(segments_dir, speeds_dir, rel / "groups", as_of)))
        self.record["groups_dir"] = str(groups_dir)
        self.step("verify", lambda: steps.verify(rel, speeds_dir))
        self.promote(md5, keep)
        return "promoted"

    def drop_download(self) -> None:
        """Delete the PBF this run downloaded (the OSRM build keeps its own copy); a --pbf input is never touched."""
        if self.downloaded is not None:
            self.downloaded.unlink(missing_ok=True)
            self.downloaded = None

    def promote(self, md5, keep) -> None:
        record, root = self.record, self.root
        record["status"] = "promoted"
        record["finished"] = self.clock().isoformat(timespec="seconds")
        self.write_record()
        _write_json(root / "current.json", {
            "id": self.rid, "release": str(self.rel), "pbf_md5": md5, "pbf_sha256": record.get("pbf_sha256"),
            "graph_sha256": record.get("graph_sha256"), "segments_dir": record["segments_dir"],
            "speeds_dir": record["speeds_dir"], "groups_dir": record["groups_dir"], "summary": record["summary"],
            "finished": record["finished"]})
        self.log(f"promoted {self.rid}")
        # The release is in use from here on: what follows only warns.
        served = _live_server(root)
        if served:
            try:
                stop(root)
                serve(root, served["port"])
                self.log(f"matrix server restarted on port {served['port']}")
            except Exception as exc:  # noqa: BLE001 - the promotion stands; the warning says what failed
                self.warnings.append(f"matrix server restart failed: {describe(exc)}")
        protect = {self.rid}
        now_served = _live_server(root)
        if now_served:
            protect.add(now_served.get("release"))
        try:
            removed = prune(root, keep, protect, before_build=False, warnings=self.warnings)
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"prune after promotion failed: {describe(exc)}")
            removed = []
        self.info["pruned"] = self.info.get("pruned", []) + removed
        for path in removed:
            self.log(f"pruned {path}")


def run_update(root, steps, *, pbf=None, force=False, keep=KEEP, min_free_gb=None, as_of=None, seed=None,
               clock=datetime.now, memory=available_memory_gb, disk=free_disk_gb) -> int:
    """One update run; 0 when promoted or unchanged, 1 on failed checks, 2 on an error or a held lock."""
    root = Path(root)
    if keep < 1:
        raise ValueError("keep must be at least 1")
    root.mkdir(parents=True, exist_ok=True)
    try:
        me = _take_lock(root)
    except (UpdateError, OSError) as exc:
        print(f"error: {describe(exc)}", file=sys.stderr)
        return 2
    try:
        state = open_state(root / "state.sqlite")
        try:
            return _run_job(root, steps, state, clock, memory, disk, pbf=pbf, force=force, keep=keep,
                            min_free_gb=min_free_gb, as_of=as_of, seed=seed)
        finally:
            state.close()
    except Exception as exc:  # noqa: BLE001 - e.g. a newer state schema: nothing was started
        print(f"error: {describe(exc)}", file=sys.stderr)
        return 2
    finally:
        _release_lock(root, me)


def _run_job(root, steps, state, clock, memory, disk, **options) -> int:
    rid = _new_id(root, state, clock())
    log_path = Path(getattr(steps, "log_path", None) or root / "logs" / f"{rid}.log")
    log = _RunLog(log_path)
    state.start_job(rid, "update", log_path)
    log(f"update {rid} started (pid {os.getpid()})")
    warnings = []
    run = _Pipeline(root, steps, rid, log, clock, warnings)
    status, error = "error", None
    try:
        status = run.run(memory=memory, disk=disk, **options)
    except _ChecksFailed as exc:
        status = "failed_checks"
        run.info["failed_checks"] = exc.names
    except BaseException as exc:
        status, error = "error", describe(exc)
        log(f"ERROR {error}")
        if not isinstance(exc, Exception):
            raise
    finally:
        try:
            run.drop_download()
        except OSError as exc:
            warnings.append(f"could not delete the downloaded PBF ({type(exc).__name__})")
        for message in warnings:
            log(f"warning: {message}")
        run.info["warnings"] = warnings
        if run.record is not None:
            if run.record["status"] != "promoted":
                run.record["status"] = status
                run.record["finished"] = clock().isoformat(timespec="seconds")
            if error:
                run.record["error"] = error
            if warnings:
                run.record["warnings"] = warnings
            try:
                run.write_record()
            except OSError as exc:
                log(f"could not write release.json ({type(exc).__name__})")
        state.finish_job(rid, status, run.info, error)
        log(f"update {rid} finished: {status}")
    return EXIT.get(status, 2)


# ---- the matrix server -------------------------------------------------------------------------------------------

def serve(root, port=SERVE_PORT) -> dict:
    """Start osrm-routed (detached) on the current release at 127.0.0.1:``port``; record it in ``server.json``."""
    port = _check_port(port)
    root = Path(root)
    current = _read_json(root / "current.json")
    if not current:
        raise UpdateError("no current release; run `python -m speedmatrix.update run` first")
    running = _live_server(root)
    if running:
        raise UpdateError(f"a matrix server is already running (pid {running['pid']}, port {running['port']})")
    base = dataset_base(_release_dir(root, current) / "osrm" / "data")
    _free_port(port)
    log_path = root / "logs" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(manage.TOOLS / "osrm-routed.exe"), "--algorithm", "mld", "--ip", "127.0.0.1", "--port", str(port),
           "--threads", str(ROUTED_THREADS), "--max-table-size", str(ROUTED_MAX_TABLE), str(base.resolve())]
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0
    with log_path.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(base.parent), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
    identity = process_identity(proc.pid)
    if identity is None:
        raise UpdateError(f"osrm-routed exited before its identity was recorded, see {log_path}")
    doc = {"pid": proc.pid, "identity": identity, "port": port, "release": current["id"], "started": now_iso()}
    _write_json(root / "server.json", doc)
    deadline = time.monotonic() + ROUTED_READY_S
    while True:
        code = proc.poll()
        if code is not None:
            (root / "server.json").unlink(missing_ok=True)
            raise UpdateError(f"osrm-routed exited with code {code}, see {log_path}")
        if _answers(port):
            return doc
        if time.monotonic() > deadline:
            stop(root)
            raise UpdateError(f"osrm-routed did not answer within {ROUTED_READY_S} s, see {log_path}")
        time.sleep(0.5)


def stop(root) -> dict | None:
    """Stop the recorded matrix server when it is still the process that was started; returns its record, or
    None (no server, or a stale record, which is removed)."""
    path = Path(root) / "server.json"
    try:
        doc = _read_json(path)
    except (OSError, ValueError):
        doc = None
    if not _is_live(doc):
        path.unlink(missing_ok=True)
        return None
    if _terminate(doc["pid"], doc["identity"]) == "still_running":
        raise UpdateError(f"matrix server pid {doc['pid']} did not stop")
    path.unlink(missing_ok=True)
    return doc


def status(root) -> dict:
    """Current release, live server and lock, releases and the last jobs; creates nothing."""
    root = Path(root)
    jobs = []
    if (root / "state.sqlite").is_file():
        state = open_state(root / "state.sqlite")
        try:
            jobs = state.jobs(limit=10)
        finally:
            state.close()
    held = _read_lock(root / "update.lock") if (root / "update.lock").exists() else None
    return {"root": str(root), "current": _read_json(root / "current.json"), "server": _live_server(root),
            "lock": held if _is_live(held) else None,
            "releases": [{"id": p.name, "status": _status(p)} for p in _releases(root)], "jobs": jobs}


# ---- the scheduled task ------------------------------------------------------------------------------------------

def schedule(root, day="SUN", time="03:00", runner=subprocess.run) -> int:
    """Register the weekly scheduled task (current user): ``pythonw.exe <repo>/speedmatrix/update.py run --root``.
    Persistent system configuration: only after the user said yes (Decision 24)."""
    day = str(day).upper()
    if day not in DAYS:
        raise ValueError(f"day must be one of {', '.join(DAYS)}, not {day!r}")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(time)):
        raise ValueError(f"time must be HH:MM, not {time!r}")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    command = f'"{pythonw}" "{_REPO / "speedmatrix" / "update.py"}" run --root "{Path(root).resolve()}"'
    argv = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", command, "/SC", "WEEKLY", "/D", day, "/ST", time, "/F"]
    return runner(argv, check=False).returncode


def unschedule(runner=subprocess.run) -> int:
    return runner(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False).returncode


# ---- CLI ---------------------------------------------------------------------------------------------------------

def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return value


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"matrix root (default {DEFAULT_ROOT})")
    ap = argparse.ArgumentParser(prog="python -m speedmatrix.update", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", parents=[common], help="build, check and promote a release")
    r.add_argument("--pbf", type=Path, help="use this PBF instead of downloading (never deleted)")
    r.add_argument("--force", action="store_true", help="rebuild even when the PBF md5 is unchanged")
    r.add_argument("--keep", type=_positive, default=KEEP, help="promoted releases kept, the current one included")
    r.add_argument("--workers", type=_positive, default=None, help="workers/threads of every step (default: sized "
                                                                   "per step by CPU and free memory)")
    r.add_argument("--build-port", type=int, default=BUILD_PORT, help="port of the §12.1 osrm-routed")
    r.add_argument("--min-free-gb", type=float, default=None, help="disk floor override")
    r.add_argument("--seed", type=Path, default=None, help="seed npz for a run without a current release")
    r.add_argument("--as-of", type=_iso_date, default=None, metavar="YYYY-MM-DD",
                   help="rules in force on this day (default today, UTC+7)")
    s = sub.add_parser("serve", parents=[common], help="start the matrix osrm-routed on the current release")
    s.add_argument("--port", type=int, default=SERVE_PORT)
    sub.add_parser("stop", parents=[common], help="stop the matrix osrm-routed")
    sub.add_parser("status", parents=[common], help="current release, server, releases and last jobs")
    c = sub.add_parser("schedule", parents=[common], help="register the weekly scheduled task")
    c.add_argument("--day", default="SUN", help="MON..SUN")
    c.add_argument("--time", default="03:00", help="HH:MM local time")
    sub.add_parser("unschedule", parents=[common], help="remove the scheduled task")
    return ap


def main(argv=None) -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    if args.command == "run":
        log_path = root / "logs" / f"update-{datetime.now():%Y%m%d-%H%M%S}.log"
        try:
            steps = RealSteps(root, args.workers, args.build_port, log_path)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return run_update(root, steps, pbf=args.pbf.resolve() if args.pbf else None, force=args.force,
                          keep=args.keep, min_free_gb=args.min_free_gb, as_of=args.as_of,
                          seed=args.seed.resolve() if args.seed else None)
    try:
        if args.command == "serve":
            print(json.dumps(serve(root, args.port), indent=1, default=str))
        elif args.command == "stop":
            doc = stop(root)
            print(f"stopped matrix server pid {doc['pid']}" if doc else "no matrix server running")
        elif args.command == "status":
            print(json.dumps(status(root), indent=1, ensure_ascii=False, default=str))
        elif args.command == "schedule":
            return schedule(root, args.day, args.time)
        elif args.command == "unschedule":
            return unschedule()
    except Exception as exc:  # noqa: BLE001 - one line, never a urllib/OS error's text
        print(f"error: {describe(exc)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
