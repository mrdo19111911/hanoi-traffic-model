"""Check the turn graph against OSRM itself (spec §12.1, Decision 18).

``python -m speedmatrix.verify_osrm --release DIR --speeds DIR [--port 5058] [--workers N] [--points 500]``

The base speed layer at one sample slot is written as an OSRM ``--segment-speed-file`` and applied with
``osrm-customize`` to a full copy of the release's matrix dataset. Four blocking checks follow:

- every graph segment's real-segment entry in the customized ``.geometry`` has data source 1 (the speed file);
- at least 99.9% of the segments agree with OSRM's customized duration within max(1 ds, 1%), ours rounded the way
  osrm-customize rounds (``max(1, round_half_away(36 * len / kmh))``);
- a private ``osrm-routed`` on the copy answers ``/table`` between sampled segment midpoints, and the reference router
  (``staticroute``) on our own durations ``36 * len / kmh + seg_fixed_ds`` agrees within max(2 s, 0.2%) on every
  pair; a pair reachable on one side only counts as over. The graph measures ``len`` with OSRM's own formula
  (``geo.osrm_length_m``), so both sides price a segment alike;
- at least 90% of the planned pairs were compared.

A directed node pair carried by several graph segments with different speeds gets the lowest speed in the speed
file (OSRM keys it by node pair); both checks use that resolved speed for those segments, and the pairs are counted
(``speed_file_conflicts``). The pid and identity of the private osrm-routed are kept in
``<release>/../.verify_routed_<port>.json`` while it runs, so the next run stops one that a hard-killed run left.

OSRM is queried first and stopped before the router workers start, so the two never hold memory together
(Decision 30). ``verify.json`` holds one entry per check (``value``, ``ok``, ``blocking`` and details); the copy and
the speed file (``<release>/verify/``) are removed in every case. The CLI exits 0 whenever it wrote ``verify.json``,
whatever the checks say (Decision 27).
"""
import argparse
import gc
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started as a script
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "osrm"))
try:
    import manage  # noqa: E402
finally:
    sys.path.remove(str(_REPO / "osrm"))

try:
    from .geo import haversine_m
    from .graph import TurnGraph, load_graph, segment_ds
    from .osrmfiles import DURATION_BITS, read_block, read_datasources, read_packed
    from .speeds import ReleaseMismatch, load_layer
    from .staticroute import PER_WORKER_GB, parallel_table
    from .sysres import StepResult, cpu_workers, default_workers, free_disk_gb, run_measured
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.geo import haversine_m
    from speedmatrix.graph import TurnGraph, load_graph, segment_ds
    from speedmatrix.osrmfiles import DURATION_BITS, read_block, read_datasources, read_packed
    from speedmatrix.speeds import ReleaseMismatch, load_layer
    from speedmatrix.staticroute import PER_WORKER_GB, parallel_table
    from speedmatrix.sysres import StepResult, cpu_workers, default_workers, free_disk_gb, run_measured

PROTECTED_PORTS = (5000, 8000)
SAMPLE_SLOT = ("weekday", 16)
SAMPLE_POINTS = 500
SAMPLE_SEED = 12
TABLE_ABS_S = 2.0
TABLE_REL = 0.002
SEGMENT_TOL = (1, 0.01)            # (deciseconds, share of OSRM's duration)
SEGMENT_MIN_SHARE = 0.999
PAIRS_MIN_SHARE = 0.9
SNAP_TOL_M = 1.0
MIN_SEGMENT_M = 20.0
EXCLUDED_HIGHWAYS = ("motorway", "motorway_link")
COPY_MARGIN_GB = 2.0
ROUTED_THREADS = 4
ROUTED_MAX_TABLE = 2500
ROUTED_READY_S = 600
STOP_WAIT_S = 60
KILL_WAIT_S = 30
SPEED_ROWS_CHUNK = 1_000_000
_CHUNK = 1 << 22
_EXAMPLES = 20
_GEO = "/common/segment_data/"
_DATASET_FILE = re.compile(r"^(?P<stem>.+)\.osrm\.[A-Za-z_]+$")
_READY_PATH = "/nearest/v1/driving/105.850000,21.030000?number=1"


# ---- speeds and the speed file ----------------------------------------------------------------------------------

def _table_rows(speeds_report: dict) -> int:
    """Row count of the segment table the speeds report names (the sum of its per-class counts)."""
    seg_dir = speeds_report.get("segments_dir")
    meta_path = Path(seg_dir) / "meta.json" if seg_dir else None
    if meta_path is None or not meta_path.is_file():
        raise ReleaseMismatch(f"the speeds report names no readable segment table ({seg_dir!r}); "
                              "cannot confirm the layer's row count")
    counts = json.loads(meta_path.read_text(encoding="utf-8")).get("counts")
    if not isinstance(counts, dict):
        raise ReleaseMismatch(f"{meta_path} has no per-class counts")
    return int(sum(counts.values()))


def seg_kmh_from_layer(g: TurnGraph, layer, speeds_report: dict) -> np.ndarray:
    """float32 (S,) km/h of every graph segment at ``SAMPLE_SLOT`` (base layer: no caps, no closures)."""
    built_on = speeds_report.get("segments_sha256")
    if built_on != g.meta.get("segments_sha256"):
        raise ReleaseMismatch(f"speed layer built on segments {str(built_on)[:12]}, the graph on "
                              f"{str(g.meta.get('segments_sha256'))[:12]}")
    rows = _table_rows(speeds_report)
    if layer.n != rows:
        raise ReleaseMismatch(f"speed layer has {layer.n} rows, its segment table {rows}")
    return layer.at(*SAMPLE_SLOT)[np.asarray(g.seg_row)].astype(np.float32)


def _write_speed_rows(g: TurnGraph, seg_kmh, path) -> tuple[int, int, np.ndarray]:
    """Write the speed file; (rows, conflicting pairs, resolved float32 (S,) km/h). A directed node pair carried by
    several graph segments with different speeds gets the lowest one (OSRM keys the file by node pair); such pairs
    are counted, and the resolved km/h is what OSRM applies to every segment."""
    kmh = np.asarray(seg_kmh, dtype=np.float64)
    if kmh.shape != (g.n_segments,):
        raise ValueError(f"seg_kmh has shape {kmh.shape}, the graph has {g.n_segments} segments")
    kmh = np.rint(kmh)
    bad = ~np.isfinite(kmh) | (kmh < 1) | (kmh > 65535)
    if bad.any():
        raise ValueError(f"{int(bad.sum())} segments have no usable speed (first: segment {int(np.argmax(bad))})")
    kmh = kmh.astype(np.uint16)
    tail = np.asarray(g.node_osm)[np.asarray(g.seg_tail)]
    head = np.asarray(g.node_osm)[np.asarray(g.seg_head)]
    order = np.lexsort((kmh, head, tail))
    tail = tail[order]
    head = head[order]
    kmh = kmh[order]
    first = np.ones(len(tail), dtype=bool)
    first[1:] = (tail[1:] != tail[:-1]) | (head[1:] != head[:-1])
    differs = ~first
    differs[1:] &= kmh[1:] != kmh[:-1]          # sorted by km/h inside a pair: any change is a conflict
    group = np.cumsum(first, dtype=np.int32) - 1
    conflicts = len(np.unique(group[differs]))
    del differs
    tail, head, kmh = tail[first], head[first], kmh[first]
    resolved = np.empty(len(order), dtype=np.float32)
    resolved[order] = kmh[group]                # the lowest km/h of the segment's node pair
    del order, group
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="ascii", newline="\n") as out:
        for s in range(0, len(tail), SPEED_ROWS_CHUNK):
            e = s + SPEED_ROWS_CHUNK
            out.write("".join(f"{a},{b},{k}\n" for a, b, k in zip(tail[s:e].tolist(), head[s:e].tolist(),
                                                                  kmh[s:e].tolist())))
    tmp.replace(path)
    return len(tail), conflicts, resolved


def write_speed_file(g: TurnGraph, seg_kmh, path) -> int:
    """``tail_osm,head_osm,kmh`` per unique directed node pair, sorted, integer km/h; returns the row count."""
    return _write_speed_rows(g, seg_kmh, path)[0]


# ---- the dataset copy and osrm-customize --------------------------------------------------------------------------

def _dataset_files(data_dir) -> tuple[str, list]:
    data_dir = Path(data_dir)
    files, stems = [], set()
    for p in sorted(data_dir.iterdir()):
        m = _DATASET_FILE.match(p.name)
        if p.is_file() and m:
            files.append(p)
            stems.add(m.group("stem"))
    if len(stems) != 1:
        raise RuntimeError(f"{data_dir} holds {len(stems)} OSRM datasets ({sorted(stems)}), expected exactly one")
    return stems.pop(), files


def dataset_base(data_dir) -> Path:
    """``<data_dir>/<stem>.osrm`` of the one OSRM dataset in ``data_dir``."""
    stem, _ = _dataset_files(data_dir)
    return Path(data_dir) / f"{stem}.osrm"


def _existing(path: Path) -> Path:
    path = Path(path).resolve()
    while not path.exists():
        path = path.parent
    return path


def copy_dataset(src_data_dir, dst_dir) -> None:
    """Copy every file of the OSRM dataset in ``src_data_dir`` into ``dst_dir`` (a full copy, not links)."""
    _, files = _dataset_files(src_data_dir)
    size_gb = sum(p.stat().st_size for p in files) / 1e9
    free = free_disk_gb(_existing(dst_dir))
    need = size_gb + COPY_MARGIN_GB
    if free < need:
        raise RuntimeError(f"only {free:.1f} GB free for the OSRM dataset copy, need {need:.1f} GB")
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.copyfile(p, dst_dir / p.name)


def customize_copy(tools, base_copy, speed_file, threads, log_path=None) -> StepResult:
    """``osrm-customize --segment-speed-file <file> -t <threads> <base>`` on the copy, measured."""
    base_copy = Path(base_copy)
    log_path = Path(log_path) if log_path else base_copy.parent / "customize.log"
    cmd = [Path(tools) / "osrm-customize.exe", "--segment-speed-file", Path(speed_file).resolve(), "-t", str(threads),
           base_copy.resolve()]
    return run_measured(cmd, log_path, base_copy.parent)


# ---- osrm-routed and its HTTP API ---------------------------------------------------------------------------------

def _check_port(port) -> int:
    port = int(port)
    if port in PROTECTED_PORTS:
        raise ValueError(f"port {port} belongs to the production OSRM or the web app; use another port")
    if not 1 <= port <= 65535:
        raise ValueError(f"port {port} is out of range")
    return port


def _free_port(port: int) -> None:
    """RuntimeError when anything listens on, or holds, 127.0.0.1:port; nothing is started or stopped."""
    with socket.socket() as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} is in use; nothing was started or stopped")
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"port {port} is in use; nothing was started or stopped") from None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _answers(port: int) -> bool:
    """True once osrm-routed answers HTTP on the port (any status)."""
    try:
        with _OPENER.open(f"http://127.0.0.1:{port}{_READY_PATH}", timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _handle_identity(kernel, handle):
    """``manage.process_identity``'s {executable, created} read through an already open process handle."""
    from ctypes import byref, create_unicode_buffer
    from ctypes import wintypes
    size = wintypes.DWORD(32768)
    name = create_unicode_buffer(size.value)
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.QueryFullProcessImageNameW(handle, 0, name, byref(size)):
        return None
    if not kernel.GetProcessTimes(handle, *[byref(t) for t in times]):
        return None
    return {"executable": str(Path(name.value).resolve()),
            "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime}


def _terminate_if(pid: int, identity: dict, timeout_s: float = None) -> str:
    """Terminate ``pid`` only when its identity equals ``identity``. The check and the termination go through one
    process handle, so a reused pid can never be hit. Returns "not_ours" (gone, or another process now),
    "stopped", or "still_running" (ours, but it did not exit within ``timeout_s``)."""
    import ctypes
    from ctypes import wintypes
    timeout_s = STOP_WAIT_S if timeout_s is None else timeout_s
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, int(pid))  # QUERY_LIMITED | TERMINATE | SYNC
    if not handle:
        return "not_ours"
    try:
        if _handle_identity(kernel, handle) != identity:
            return "not_ours"
        if kernel.WaitForSingleObject(handle, 0) == 0:          # already exited
            return "stopped"
        kernel.TerminateProcess(handle, 1)
        return "stopped" if kernel.WaitForSingleObject(handle, int(timeout_s * 1000)) == 0 else "still_running"
    finally:
        kernel.CloseHandle(handle)


def _reap_stale(run_file: Path):
    """Stop the osrm-routed that a hard-killed earlier run left behind, as recorded in ``run_file``, when it is an
    osrm-routed whose identity still matches; then drop the file. Returns None when there was no file."""
    if not run_file.is_file():
        return None
    try:
        doc = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    pid, identity = doc.get("pid"), doc.get("identity")
    result = "not_ours"
    if isinstance(pid, int) and isinstance(identity, dict) \
            and Path(str(identity.get("executable", ""))).name.lower() == "osrm-routed.exe":
        result = _terminate_if(pid, identity)
    if result != "still_running":
        run_file.unlink(missing_ok=True)
    print(f"stale osrm-routed from {run_file.name} (pid {pid}): {result}", file=sys.stderr)
    return result


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


class Routed:
    """A private ``osrm-routed`` (MLD) on ``base`` at 127.0.0.1:``port`` for the duration of a ``with`` block.

    With ``run_file`` the pid and process identity are recorded there while it runs, so a later run stops an
    osrm-routed that a hard-killed run left behind (``_reap_stale``, identity checked), with no manual step.
    """

    def __init__(self, base, port, tools, *, threads=ROUTED_THREADS, max_table_size=ROUTED_MAX_TABLE, log_path=None,
                 ready_s=ROUTED_READY_S, run_file=None):
        self.port = _check_port(port)
        self.base = Path(base)
        self.tools = Path(tools)
        self.threads = threads
        self.max_table_size = max_table_size
        self.log_path = Path(log_path) if log_path else self.base.parent / "routed.log"
        self.ready_s = ready_s
        self.run_file = Path(run_file) if run_file else None
        self.stale = None
        self.proc = None
        self.identity = None
        self._log = None

    def command(self) -> list:
        return [str(self.tools / "osrm-routed.exe"), "--algorithm", "mld", "--ip", "127.0.0.1",
                "--port", str(self.port), "--threads", str(self.threads),
                "--max-table-size", str(self.max_table_size), str(self.base.resolve())]

    def __enter__(self):
        if self.run_file is not None:
            self.stale = _reap_stale(self.run_file)
        _free_port(self.port)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(self.command(), cwd=str(self.base.parent), stdin=subprocess.DEVNULL,
                                         stdout=self._log, stderr=subprocess.STDOUT, creationflags=flags)
            self.identity = manage.process_identity(self.proc.pid)
            if self.identity is None:
                raise RuntimeError(f"osrm-routed exited before its identity was recorded, see {self.log_path}")
            if self.run_file is not None:
                self.run_file.parent.mkdir(parents=True, exist_ok=True)
                _write_json(self.run_file, {"pid": self.proc.pid, "identity": self.identity, "port": self.port,
                                            "base": str(self.base.resolve())})
            deadline = time.monotonic() + self.ready_s
            while True:
                code = self.proc.poll()
                if code is not None:
                    raise RuntimeError(f"osrm-routed exited with code {code}, see {self.log_path}")
                if _answers(self.port):
                    return self
                if time.monotonic() > deadline:
                    raise RuntimeError(f"osrm-routed did not answer within {self.ready_s} s, see {self.log_path}")
                time.sleep(0.5)
        except BaseException:
            try:
                self._stop()
            except Exception as e:                    # never hide the error that brought us here
                _warn(f"stopping osrm-routed failed ({type(e).__name__}: {e})")
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            self._stop()
        except Exception as e:
            if exc_type is None:
                raise
            _warn(f"stopping osrm-routed failed ({type(e).__name__}: {e}); the original error follows")
        return False

    def _stop(self) -> None:
        """Terminate the process this object started, only while its identity is unchanged; escalate to kill()
        when it does not exit in time. The run file goes once the process is gone."""
        proc = self.proc
        try:
            if proc is None:
                return
            if proc.poll() is None:
                if self.identity is None or manage.process_identity(proc.pid) != self.identity:
                    _warn(f"osrm-routed pid {proc.pid} no longer has the identity it started with; left alone")
                    return
                proc.terminate()
                try:
                    proc.wait(STOP_WAIT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(KILL_WAIT_S)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError(f"osrm-routed pid {proc.pid} did not exit after kill()") from None
            if self.run_file is not None:
                self.run_file.unlink(missing_ok=True)
        finally:
            self.proc = None
            if self._log is not None:
                self._log.close()
                self._log = None


class OsrmClient:
    """``/table`` and ``/nearest`` of an osrm-routed on 127.0.0.1 (never a protected port)."""

    def __init__(self, port, timeout_s=600):
        self.port = _check_port(port)
        self.timeout_s = timeout_s

    def _get(self, path: str) -> dict:
        try:
            with _OPENER.open(f"http://127.0.0.1:{self.port}{path}", timeout=self.timeout_s) as response:
                data = json.load(response)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:  # spec §13
            raise RuntimeError(f"{type(e).__name__}: osrm-routed request failed") from None
        if data.get("code") != "Ok":
            raise RuntimeError(f"osrm-routed answered {data.get('code')!r}")
        return data

    def table(self, lonlat: np.ndarray):
        """(seconds (P, P), snapped source locations (P, 2)); unreachable pairs are inf."""
        lonlat = np.asarray(lonlat, dtype=np.float64)
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in lonlat)
        data = self._get(f"/table/v1/driving/{coords}?annotations=duration")
        seconds = np.array([[np.inf if v is None else float(v) for v in row] for row in data["durations"]],
                           dtype=np.float64).reshape(len(lonlat), len(lonlat))
        locations = np.array([s["location"] for s in data["sources"]], dtype=np.float64).reshape(len(lonlat), 2)
        return seconds, locations

    def nearest(self, lon, lat, number=2) -> list:
        """[(lon, lat, distance_m)] of up to ``number`` snapped candidates, nearest first."""
        data = self._get(f"/nearest/v1/driving/{float(lon):.6f},{float(lat):.6f}?number={int(number)}")
        return [(float(w["location"][0]), float(w["location"][1]), float(w["distance"])) for w in data["waypoints"]]


# ---- sample points -----------------------------------------------------------------------------------------------

def _largest_weak_component(g: TurnGraph) -> np.ndarray:
    """bool (E,): the EBNs of the largest weakly connected component of the turn graph (lowest label on a tie)."""
    E = g.n_ebn
    if E == 0:
        return np.zeros(0, dtype=bool)
    graph = csr_matrix((np.ones(g.n_turns, dtype=np.int8), np.asarray(g.turn_dst), np.asarray(g.turn_offsets)),
                       shape=(E, E))
    _, labels = connected_components(graph, directed=True, connection="weak")
    return labels == int(np.argmax(np.bincount(labels)))


def sample_points(g: TurnGraph, n: int, seed: int, mask: np.ndarray | None = None) -> np.ndarray:
    """int64 segment ids: the first ``min(n, eligible)`` of a seeded permutation of the eligible segments
    (Decision 18: no motorway, no folded obstacle, at least 20 m, not tiny, an EBN whose (geometry, direction) is
    unique, in the largest weak component, and in ``mask`` when given)."""
    S = g.n_segments
    ok = (np.asarray(g.seg_fixed_ds) == 0) & (np.asarray(g.seg_len_m) >= MIN_SEGMENT_M)
    bad_hw = [i for i, h in enumerate(g.meta["highways"]) if h in EXCLUDED_HIGHWAYS]
    if bad_hw:
        ok &= ~np.isin(np.asarray(g.seg_highway), bad_hw)
    key = np.asarray(g.ebn_geometry).astype(np.int64) * 2 + np.asarray(g.ebn_forward)
    _, inverse, counts = np.unique(key, return_inverse=True, return_counts=True)
    ebn_ok = (counts[np.asarray(inverse).reshape(-1)] == 1) & ~np.asarray(g.ebn_tiny) & _largest_weak_component(g)
    del key, inverse, counts
    ok &= np.repeat(ebn_ok, np.diff(np.asarray(g.ebn_offsets)))
    if mask is not None:
        mask = np.asarray(mask)
        if mask.shape != (S,) or mask.dtype != bool:
            raise ValueError(f"mask must be bool of shape ({S},), got {mask.dtype} {mask.shape}")
        ok &= mask
    eligible = np.flatnonzero(ok).astype(np.int64)
    perm = np.random.default_rng(seed).permutation(eligible)
    return perm[:min(int(n), len(perm))]


def _midpoints(g: TurnGraph, segs) -> np.ndarray:
    segs = np.asarray(segs, dtype=np.int64)
    t, h = np.asarray(g.seg_tail)[segs], np.asarray(g.seg_head)[segs]
    lon = (np.asarray(g.node_lon_e6)[t].astype(np.float64) + np.asarray(g.node_lon_e6)[h]) / 2e6
    lat = (np.asarray(g.node_lat_e6)[t].astype(np.float64) + np.asarray(g.node_lat_e6)[h]) / 2e6
    return np.stack([lon, lat], axis=1)


def _osrm_answers(client, g: TurnGraph, candidates: np.ndarray, points: int):
    """OSRM's table between up to ``points`` candidates, taken in order. A candidate whose ``/nearest`` gives two
    snaps within 1 m, or whose table source lies more than 1 m from its midpoint, is dropped and replaced by the
    next one. Returns (segments, seconds, stats)."""
    mid = _midpoints(g, candidates)
    stats = {"dropped_snap": 0, "dropped_ambiguous": 0}
    chosen, k = [], 0
    seconds = np.zeros((0, 0))
    while True:
        while len(chosen) < points and k < len(candidates):
            near = client.nearest(mid[k, 0], mid[k, 1], number=2)
            if len(near) >= 2 and near[0][2] <= SNAP_TOL_M and near[1][2] <= SNAP_TOL_M:
                stats["dropped_ambiguous"] += 1
            elif not near:
                stats["dropped_snap"] += 1
            else:
                chosen.append(k)
            k += 1
        if not chosen:
            seconds = np.zeros((0, 0))
            break
        seconds, loc = client.table(mid[chosen])
        far = haversine_m(loc[:, 1], loc[:, 0], mid[chosen, 1], mid[chosen, 0]) > SNAP_TOL_M
        if not far.any():
            break
        stats["dropped_snap"] += int(far.sum())
        keep = ~far
        chosen = [c for c, kept in zip(chosen, keep) if kept]
        if k >= len(candidates):                     # nothing left to replace them: keep the rest of this table
            seconds = seconds[np.ix_(keep, keep)]
            break
    stats["candidates_tried"] = k
    return np.asarray(candidates, dtype=np.int64)[chosen], seconds, stats


# ---- comparisons -----------------------------------------------------------------------------------------------

def compare_tables(ours: np.ndarray, theirs: np.ndarray) -> dict:
    """Pairs where both tables are finite, with tolerance max(TABLE_ABS_S, TABLE_REL * theirs); a NaN of ours (one
    geometry) is skipped; ``unreachable_mismatch`` counts pairs that are inf on exactly one side."""
    ours = np.asarray(ours, dtype=np.float64)
    theirs = np.asarray(theirs, dtype=np.float64)
    if ours.shape != theirs.shape:
        raise ValueError(f"tables differ in shape: {ours.shape} vs {theirs.shape}")
    both = np.isfinite(ours) & np.isfinite(theirs)
    unreachable = ~np.isnan(ours) & ~np.isnan(theirs) & (np.isinf(ours) != np.isinf(theirs))
    diff = np.where(both, np.abs(np.where(both, ours, 0.0) - np.where(both, theirs, 0.0)), 0.0)
    tol = np.maximum(TABLE_ABS_S, TABLE_REL * np.where(both, theirs, 0.0))
    over = both & (diff > tol)
    rel_ok = both & (theirs > 0)
    rel = np.divide(diff, theirs, out=np.zeros_like(diff), where=rel_ok)
    ij = np.argwhere(over)
    order = np.argsort(-diff[over], kind="stable")[:_EXAMPLES]
    examples = [{"i": int(i), "j": int(j), "ours": float(ours[i, j]), "osrm": float(theirs[i, j])}
                for i, j in ij[order]]
    return {"pairs": int(both.sum()), "over": int(over.sum()), "max_abs_s": float(diff.max(initial=0.0)),
            "max_rel": float(rel.max(initial=0.0)), "unreachable_mismatch": int(unreachable.sum()),
            "examples": examples}


def _geo_index(g: TurnGraph) -> np.ndarray:
    """int64 (G+1,) ``.geometry`` index rebuilt from the graph: geometry k holds 2 entries per real segment."""
    m = np.diff(np.asarray(g.ebn_offsets))
    geometry = np.asarray(g.ebn_geometry).astype(np.int64)
    G = int(geometry.max()) + 1 if len(geometry) else 0
    per = np.zeros(G, dtype=np.int64)
    per[geometry] = m
    if (per[geometry] != m).any():
        raise ValueError("two EBNs of one geometry differ in segment count")
    if len(geometry) and (np.bincount(geometry, minlength=G) == 0).any():
        raise ValueError("a geometry has no EBN; the graph cannot locate its .geometry entries")
    index = np.zeros(G + 1, dtype=np.int64)
    np.cumsum(2 * per, out=index[1:])
    return index


def _entries(g: TurnGraph, geo_index: np.ndarray, start: int, stop: int):
    """(forward bool, entry int64) of segments start..stop-1: the ``.geometry`` entry of their real segment, read in
    the forward arrays when forward is True and in the reverse arrays otherwise (Task 1 layout)."""
    offsets = np.asarray(g.ebn_offsets)
    s = np.arange(start, stop, dtype=np.int64)
    e = np.searchsorted(offsets, s, side="right") - 1
    fwd = np.asarray(g.ebn_forward)[e]
    k = s - offsets[e]
    j = np.where(fwd, k, offsets[e + 1] - offsets[e] - 1 - k)
    pos = geo_index[np.asarray(g.ebn_geometry)[e]] + 2 * j
    return fwd, np.where(fwd, pos + 1, pos)


def datasource_coverage(g: TurnGraph, fwd_src: np.ndarray, rev_src: np.ndarray) -> dict:
    """Graph segments whose real-segment entry was not updated by the speed file (data source != 1)."""
    geo_index = _geo_index(g)
    if not len(fwd_src) == len(rev_src) == int(geo_index[-1]):
        raise ValueError(f"data sources hold {len(fwd_src)}/{len(rev_src)} entries, the graph's geometries "
                         f"{int(geo_index[-1])}")
    not_updated, examples = 0, []
    for start in range(0, g.n_segments, _CHUNK):
        stop = min(start + _CHUNK, g.n_segments)
        fwd, entry = _entries(g, geo_index, start, stop)
        src = np.where(fwd, fwd_src[entry], rev_src[entry])
        bad = np.flatnonzero(src != 1)
        not_updated += len(bad)
        for b in bad[:5 - len(examples)]:
            s = start + int(b)
            examples.append([int(g.node_osm[g.seg_tail[s]]), int(g.node_osm[g.seg_head[s]]), int(src[b])])
    return {"segments": int(g.n_segments), "not_updated": int(not_updated), "examples": examples}


def _customized_ds(g: TurnGraph, base_copy) -> np.ndarray:
    """int32 (S,) OSRM's customized duration of every graph segment's real-segment entry in ``.geometry``."""
    path = str(base_copy) + ".geometry"
    geo_index = _geo_index(g)
    if not np.array_equal(read_block(path, _GEO + "index", "<u4").astype(np.int64), geo_index):
        raise ValueError(f"{Path(path).name}: the geometry index does not match the graph")
    fd = read_packed(path, _GEO + "forward_durations", DURATION_BITS).astype(np.uint32)
    rd = read_packed(path, _GEO + "reverse_durations", DURATION_BITS).astype(np.uint32)
    out = np.empty(g.n_segments, dtype=np.int32)
    for start in range(0, g.n_segments, _CHUNK):
        stop = min(start + _CHUNK, g.n_segments)
        fwd, entry = _entries(g, geo_index, start, stop)
        out[start:stop] = np.where(fwd, fd[entry], rd[entry])
    return out


def osrm_segment_ds(len_m, kmh) -> np.ndarray:
    """osrm-customize's duration of a speed-file row: ``max(1, round_half_away(36 * len / kmh))`` deciseconds."""
    x = 36.0 * np.asarray(len_m, dtype=np.float64) / np.asarray(kmh, dtype=np.float64)
    return np.maximum(1.0, np.floor(x + 0.5))


def _agreement(g: TurnGraph, seg_kmh, theirs: np.ndarray) -> dict:
    S = g.n_segments
    diff = np.empty(S, dtype=np.int32)
    rel = np.empty(S, dtype=np.float32)
    within = 0
    for start in range(0, S, _CHUNK):
        stop = min(start + _CHUNK, S)
        ours = osrm_segment_ds(g.seg_len_m[start:stop], seg_kmh[start:stop])
        t = theirs[start:stop].astype(np.float64)
        d = np.abs(ours - t)
        diff[start:stop] = d
        rel[start:stop] = np.divide(ours - t, t, out=np.zeros(len(t)), where=t > 0)
        within += int((d <= np.maximum(SEGMENT_TOL[0], SEGMENT_TOL[1] * t)).sum())
    return {"segments": int(S), "within": within, "share_within": within / S if S else 0.0,
            "p99_abs_ds": float(np.percentile(diff, 99)) if S else 0.0, "max_abs_ds": int(diff.max(initial=0)),
            "rel_p01": float(np.percentile(rel, 1)) if S else 0.0,
            "rel_p99": float(np.percentile(rel, 99)) if S else 0.0}


def segment_agreement(g: TurnGraph, seg_kmh, base_copy) -> dict:
    """Share of graph segments whose ``osrm_segment_ds(len, kmh)`` is within max(1 ds, 1%) of the customized
    ``.geometry`` duration of the same entry; ``rel_p01``/``rel_p99`` are percentiles of (ours - OSRM) / OSRM."""
    return _agreement(g, seg_kmh, _customized_ds(g, base_copy))


# ---- the whole check ------------------------------------------------------------------------------------------

def _remove(path: Path) -> None:
    for attempt in range(5):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            gc.collect()                             # a mapped .npy cannot be deleted on Windows while mapped
            time.sleep(0.5 * (attempt + 1))
    shutil.rmtree(path)


def _cleanup(work: Path, quiet: bool) -> None:
    gc.collect()
    try:
        _remove(work)
    except OSError as e:
        if not quiet:
            raise
        _warn(f"could not remove {work} ({type(e).__name__})")


def _write_json(path: Path, doc) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    tmp.replace(path)


def verify_release(release_dir, speeds_dir, tools, port, workers, points: int = SAMPLE_POINTS,
                   mask: np.ndarray | None = None) -> dict:
    """Run the §12.1 checks on ``<release>/graph`` and ``<release>/osrm/data``; write and return ``verify.json``."""
    release, speeds = Path(release_dir), Path(speeds_dir)
    port = _check_port(port)
    out = release / "verify.json"
    out.unlink(missing_ok=True)
    work, logs = release / "verify", release / "logs"
    g = load_graph(release / "graph", mmap=True)
    report = json.loads((speeds / "report.json").read_text(encoding="utf-8"))
    seg_kmh = seg_kmh_from_layer(g, load_layer(speeds), report)
    data_dir = release / "osrm" / "data"
    _remove(work)
    work.mkdir(parents=True)
    try:
        speed_file = work / "speeds.csv"
        rows, conflicts, seg_kmh = _write_speed_rows(g, seg_kmh, speed_file)
        copy_dataset(data_dir, work / "osrm")
        base_copy = dataset_base(work / "osrm")
        log = logs / "verify_customize.log"
        step = customize_copy(tools, base_copy, speed_file, cpu_workers(), log_path=log)
        if step.code != 0:
            raise RuntimeError(f"osrm-customize failed with exit {step.code}, see {log}")
        fwd_src, rev_src = read_datasources(base_copy)
        coverage = datasource_coverage(g, fwd_src, rev_src)
        del fwd_src, rev_src
        agreement = _agreement(g, seg_kmh, _customized_ds(g, base_copy))
        candidates = sample_points(g, 2 * points, SAMPLE_SEED, mask)
        with Routed(base_copy, port, tools, log_path=logs / "verify_routed.log",
                    run_file=release.parent / f".verify_routed_{port}.json"):
            chosen, theirs, drops = _osrm_answers(OsrmClient(port), g, candidates, points)
        # osrm-routed is stopped: only now the router workers start (Decision 30). Our side is our own engine's
        # durations, 36 * len / kmh + seg_fixed_ds, at the speeds OSRM got from the speed file.
        seg_ds_file = work / "seg_ds.npy"
        np.save(seg_ds_file, segment_ds(g, seg_kmh))
        if len(chosen):
            ours = parallel_table(release / "graph", seg_ds_file, chosen, chosen,
                                  default_workers(PER_WORKER_GB, workers))
        else:
            ours = np.zeros((0, 0))
    except BaseException:
        del g
        _cleanup(work, quiet=True)               # a failed cleanup must not hide the error
        raise
    del g
    _cleanup(work, quiet=False)
    table = compare_tables(ours, theirs)
    p = len(chosen)
    planned = p * (p - 1)
    over = table["over"] + table["unreachable_mismatch"]
    checks = {
        "osrm_table_over_tolerance": {
            "value": over, "ok": over == 0, "blocking": True, "over": table["over"],
            "unreachable_mismatch": table["unreachable_mismatch"], "max_abs_s": table["max_abs_s"],
            "max_rel": table["max_rel"], "tolerance": {"abs_s": TABLE_ABS_S, "rel": TABLE_REL},
            "router_durations": "36*seg_len_m/kmh+seg_fixed_ds (OSRM segment lengths, speed-file km/h)",
            "examples": [{**ex, "segment_i": int(chosen[ex["i"]]), "segment_j": int(chosen[ex["j"]])}
                         for ex in table["examples"]]},
        "osrm_table_pairs": {
            "value": table["pairs"], "ok": planned > 0 and table["pairs"] >= PAIRS_MIN_SHARE * planned,
            "blocking": True, "planned": planned, "points": p, "points_requested": int(points),
            "shortfall": int(points) - p, "candidates": int(len(candidates)), **drops,
            "slot": list(SAMPLE_SLOT), "seed": SAMPLE_SEED, "masked": mask is not None,
            "segments": [int(s) for s in chosen]},
        "osrm_datasource_not_updated": {
            "value": coverage["not_updated"], "ok": coverage["not_updated"] == 0, "blocking": True,
            "segments": coverage["segments"], "examples": coverage["examples"]},
        "osrm_segment_agreement": {
            "value": agreement["share_within"], "ok": agreement["share_within"] >= SEGMENT_MIN_SHARE,
            "blocking": True, "segments": agreement["segments"], "within": agreement["within"],
            "p99_abs_ds": agreement["p99_abs_ds"], "max_abs_ds": agreement["max_abs_ds"],
            "tolerance": {"abs_ds": SEGMENT_TOL[0], "rel": SEGMENT_TOL[1]},
            "speed_file_rows": rows, "speed_file_conflicts": conflicts},
    }
    _write_json(out, checks)
    return checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m speedmatrix.verify_osrm", description=__doc__.splitlines()[0])
    ap.add_argument("--release", type=Path, required=True)
    ap.add_argument("--speeds", type=Path, required=True, help="speed layer dir (layer.npz, report.json)")
    ap.add_argument("--port", type=int, default=5058)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--points", type=int, default=SAMPLE_POINTS)
    ap.add_argument("--tools", type=Path, default=manage.TOOLS)
    args = ap.parse_args(argv)
    verify_release(args.release, args.speeds, args.tools, args.port, args.workers, points=args.points)
    print(args.release / "verify.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
