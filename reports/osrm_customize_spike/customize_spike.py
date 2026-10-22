"""One-off spike: is per-slot OSRM (segment-speed file + osrm-customize + /table) affordable here?

Report-only code, not production. Runs entirely on a scratch COPY of the .osrm dataset and on a
private osrm-routed (port 5055); it never writes to osrm/data and never calls the network beyond
127.0.0.1.

    .venv/Scripts/python.exe reports/osrm_customize_spike/customize_spike.py <scratch_dir>

<scratch_dir> must already hold a copy of osrm/data/vietnam-latest.osrm.* files. Results go to
reports/osrm_customize_spike/results.json and run.log.
"""
import ctypes
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
TOOLS = ROOT / "osrm" / "tools"
DATA = ROOT / "osrm" / "data"
PORT = 5055
THREADS_ROUTED = 4  # same as the managed server (osrm/manage.py default)
MAX_TABLE = 2500
SEED = 20261020

BIG_SPEEDS = {"caotoc": 80, "quoclo": 50, "tinhlo": 40}
# Hanoi index classes (road_classes.sqlite); flat illustrative speeds, 'unknown' left to the profile.
HANOI_SPEEDS = {"caotoc": 80, "quoclo": 50, "chinh": 35, "noido": 25, "nho": 20}

LOG = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


# ---------------------------------------------------------------- process memory (Win32)
class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


_psapi = ctypes.WinDLL("psapi")


def mem_info(proc):
    """(working set, peak working set, private commit, peak private commit) in MB."""
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    if not _psapi.GetProcessMemoryInfo(wintypes.HANDLE(int(proc._handle)), ctypes.byref(pmc), pmc.cb):
        return None
    mb = 1024 * 1024
    return {"ws_mb": round(pmc.WorkingSetSize / mb), "peak_ws_mb": round(pmc.PeakWorkingSetSize / mb),
            "private_mb": round(pmc.PagefileUsage / mb), "peak_private_mb": round(pmc.PeakPagefileUsage / mb)}


def run_measured(cmd, logfile):
    """Run a command to completion; return wall time, peak memory and exit code."""
    t0 = time.perf_counter()
    with open(logfile, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
        peak = None
        while proc.poll() is None:
            peak = mem_info(proc) or peak
            time.sleep(0.2)
        final = mem_info(proc) or peak  # handle is still open, counters survive exit
    return {"seconds": round(time.perf_counter() - t0, 1), "exit": proc.returncode, "memory": final}


# ---------------------------------------------------------------- file snapshots
def snapshot(base):
    d = base.parent
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in d.iterdir() if p.name.startswith(base.name)}


def changed(before, after):
    return sorted(n for n in after if before.get(n) != after[n])



# ---------------------------------------------------------------- speed CSVs
def big_road_csv(path):
    """Both directions of every A-network edge pair, flat speed by big_roads.geojson group."""
    groups = {}
    for f in json.load(open(DATA / "big_roads.geojson", encoding="utf-8"))["features"]:
        p = f["properties"]
        groups.setdefault(p["way_id"], set()).add(p["group"])
    db = sqlite3.connect(f"file:{DATA / 'big_road_edges.sqlite'}?mode=ro", uri=True)
    rank = {"caotoc": 0, "quoclo": 1, "tinhlo": 2}
    stats = {"pairs": 0, "rows": 0, "skipped_ferry": 0, "skipped_unmapped": 0, "by_class": {}}
    seen = set()
    with open(path, "w", newline="") as out:
        for a, b, way_id, kind in db.execute("SELECT a, b, way_id, kind FROM edges"):
            if kind == "ferry":
                stats["skipped_ferry"] += 1
                continue
            g = groups.get(way_id)
            if not g:
                stats["skipped_unmapped"] += 1
                continue
            if (a, b) in seen:  # pair shared by two ways: first wins
                continue
            seen.add((a, b))
            cls = min(g, key=rank.get)
            s = BIG_SPEEDS[cls]
            out.write(f"{a},{b},{s}\n{b},{a},{s}\n")
            stats["pairs"] += 1
            stats["rows"] += 2
            stats["by_class"][cls] = stats["by_class"].get(cls, 0) + 1
    stats["bytes"] = path.stat().st_size
    return stats


def hanoi_csv(path):
    """Both directions of every classified edge in the Hanoi index (road_classes.sqlite)."""
    db = sqlite3.connect(f"file:{DATA / 'road_classes.sqlite'}?mode=ro", uri=True)
    stats = {"pairs": 0, "rows": 0, "skipped_unknown": 0, "by_class": {}}
    last = None
    with open(path, "w", newline="") as out:
        for a, b, cls in db.execute("SELECT a, b, road_class FROM edges ORDER BY a, b"):
            if (a, b) == last:
                continue
            last = (a, b)
            s = HANOI_SPEEDS.get(cls)
            if s is None:
                stats["skipped_unknown"] += 1
                continue
            out.write(f"{a},{b},{s}\n{b},{a},{s}\n")
            stats["pairs"] += 1
            stats["rows"] += 2
            stats["by_class"][cls] = stats["by_class"].get(cls, 0) + 1
    stats["bytes"] = path.stat().st_size
    return stats


# ---------------------------------------------------------------- routed + /table
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path, timeout=600):
    with OPENER.open(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
        return r.read()


def start_routed(base, logfile, mmap=False):
    cmd = [str(TOOLS / "osrm-routed.exe"), "--algorithm", "mld", "--ip", "127.0.0.1", "--port", str(PORT),
           "--threads", str(THREADS_ROUTED), "--max-table-size", str(MAX_TABLE)]
    if mmap:
        cmd.append("--mmap=1")
    cmd.append(str(base))
    fh = open(logfile, "w", encoding="utf-8")
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT)
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"osrm-routed exited early, see {logfile}")
        try:
            get("/nearest/v1/driving/105.8542,21.0285", timeout=5)
            break
        except Exception:
            time.sleep(0.5)
    load = round(time.perf_counter() - t0, 1)
    time.sleep(1)
    return proc, fh, load


def stop_routed(proc, fh):
    proc.terminate()
    proc.wait(30)
    fh.close()


def sample_points(n):
    feats = [f for f in json.load(open(DATA / "big_roads.geojson", encoding="utf-8"))["features"]
             if not (f["properties"]["isolated"] or f["properties"]["fragment"] or f["properties"]["bridge_kind"] == "ferry")]
    rng = random.Random(SEED)
    weights = [f["properties"]["km"] for f in feats]  # uniform along road length
    pts = []
    for f in rng.choices(feats, weights=weights, k=n):
        c = f["geometry"]["coordinates"]
        pts.append(tuple(c[rng.randrange(len(c))]))
    return pts


def table(points, sources=None, destinations=None):
    coords = ";".join(f"{x:.6f},{y:.6f}" for x, y in points)
    q = "?annotations=duration,distance"
    if sources is not None:
        q += "&sources=" + ";".join(map(str, sources))
    if destinations is not None:
        q += "&destinations=" + ";".join(map(str, destinations))
    t0 = time.perf_counter()
    raw = get(f"/table/v1/driving/{coords}{q}")
    t1 = time.perf_counter()
    data = json.loads(raw)
    t2 = time.perf_counter()
    if data.get("code") != "Ok":
        raise RuntimeError(data.get("code"))
    d = data["durations"]
    cells = sum(len(r) for r in d)
    nulls = sum(v is None for r in d for v in r)
    return {"request_s": round(t1 - t0, 2), "parse_s": round(t2 - t1, 2), "mb": round(len(raw) / 1e6, 1),
            "cells": cells, "nulls": nulls}, d


def time_tables(points, tag, reps=2):
    res = {}
    for n in (500, 1000, 2000):
        runs = []
        for _ in range(reps):
            r, _ = table(points[:n])
            runs.append(r)
            log(f"{tag} /table {n}x{n}: {r}")
        res[f"{n}x{n}"] = runs
    r, _ = table(points, sources=[0])
    log(f"{tag} /table 1x2000: {r}")
    res["1x2000"] = [r]
    # one-to-many probes: is a single source a slow path in OSRM MLD?
    for label, kw in (("2x2000", {"sources": [0, 1]}), ("8x2000", {"sources": list(range(8))}),
                      ("2000x1", {"destinations": [0]}), ("1x2000_annot_duration_only", None)):
        if kw is None:
            coords = ";".join(f"{x:.6f},{y:.6f}" for x, y in points)
            t0 = time.perf_counter()
            get(f"/table/v1/driving/{coords}?sources=0")
            r = {"request_s": round(time.perf_counter() - t0, 2)}
        else:
            r, _ = table(points, **kw)
        log(f"{tag} /table {label}: {r}")
        res[label] = [r]
    return res


def mean_duration(d):
    vals = [v for r in d for v in r if v]
    return sum(vals) / len(vals)


# ---------------------------------------------------------------- main
def main(scratch):
    scratch = Path(scratch)
    base = scratch / "vietnam-latest.osrm"
    assert base.with_suffix(".osrm.cell_metrics").exists(), "scratch copy missing"
    assert scratch.resolve() != DATA.resolve()
    res = {"machine": {"cpu": "AMD Ryzen 9 3900X 12C/24T", "ram_gb": 31.9}}

    log("building speed CSVs")
    t0 = time.perf_counter()
    res["csv_big"] = big_road_csv(scratch / "speeds_big.csv")
    res["csv_big"]["build_s"] = round(time.perf_counter() - t0, 1)
    t0 = time.perf_counter()
    res["csv_hanoi"] = hanoi_csv(scratch / "speeds_hanoi.csv")
    res["csv_hanoi"]["build_s"] = round(time.perf_counter() - t0, 1)
    log(f"csv_big {res['csv_big']}")
    log(f"csv_hanoi {res['csv_hanoi']}")

    points = sample_points(2000)
    json.dump(points, open(OUT / "points_2000.json", "w"))

    # 1. /table on the pristine copy (identical to the live dataset)
    proc, fh, load = start_routed(base, scratch / "routed_base.log")
    res["routed_base"] = {"load_s": load, "memory_after_load": mem_info(proc)}
    log(f"routed (pristine) ready in {load}s, mem {res['routed_base']['memory_after_load']}")
    res["table_base"] = time_tables(points, "base")
    _, d_base = table(points[:500])
    res["routed_base"]["memory_after_tables"] = mem_info(proc)
    stop_routed(proc, fh)

    # 1b. same with --mmap (one routed per slot without loading everything into RAM?)
    proc, fh, load = start_routed(base, scratch / "routed_mmap.log", mmap=True)
    res["routed_mmap"] = {"load_s": load, "memory_after_load": mem_info(proc)}
    r, _ = table(points[:2000])
    res["routed_mmap"]["table_2000"] = r
    res["routed_mmap"]["memory_after_table"] = mem_info(proc)
    log(f"routed --mmap ready in {load}s, 2000x2000 {r}, mem {res['routed_mmap']['memory_after_table']}")
    stop_routed(proc, fh)

    # 2. osrm-customize runs on the copy
    cust = str(TOOLS / "osrm-customize.exe")
    runs = [("no_speed_file", []),
            ("big_roads", ["--segment-speed-file", str(scratch / "speeds_big.csv")]),
            ("big_roads_6_threads", ["--segment-speed-file", str(scratch / "speeds_big.csv"), "--threads", "6"]),
            ("hanoi_all_classes", ["--segment-speed-file", str(scratch / "speeds_hanoi.csv")]),
            ("big_plus_hanoi", ["--segment-speed-file", str(scratch / "speeds_big.csv"),
                                "--segment-speed-file", str(scratch / "speeds_hanoi.csv")])]
    res["customize"] = {}
    for name, extra in runs:
        before = snapshot(base)
        r = run_measured([cust, str(base), *extra], scratch / f"customize_{name}.log")
        r["files_written"] = changed(before, snapshot(base))
        r["files_written_mb"] = {n: round((scratch / n).stat().st_size / 1e6) for n in r["files_written"]}
        res["customize"][name] = r
        log(f"customize {name}: {r}")
        if name == "big_roads":
            # 3. verify the speeds took effect and re-time the big table on customized weights
            proc, fh, load = start_routed(base, scratch / "routed_custom.log")
            _, d_cust = table(points[:500])
            r2, _ = table(points[:2000])
            res["routed_custom"] = {"load_s": load, "table_2000": r2,
                                    "mean_500_duration_base_s": round(mean_duration(d_base)),
                                    "mean_500_duration_custom_s": round(mean_duration(d_cust))}
            log(f"customized routed: {res['routed_custom']}")
            stop_routed(proc, fh)

    res["dataset_mb"] = {p.name: round(p.stat().st_size / 1e6) for p in sorted(scratch.glob("vietnam-latest.osrm.*"))}
    json.dump(res, open(OUT / "results.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    (OUT / "run.log").write_text("\n".join(LOG) + "\n", encoding="utf-8")
    log("done")


if __name__ == "__main__":
    main(sys.argv[1])
