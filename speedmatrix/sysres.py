"""Machine resources: worker counts sized by CPU and free RAM, disk space, and a measured subprocess runner."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

CPU_FRACTION = 0.6
SAMPLE_SECONDS = 0.5


def cpu_workers(cpu_count=None):
    """60% of the CPUs, at least 1."""
    cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, round(CPU_FRACTION * cpu))


def available_memory_gb():
    """Physical memory that is free right now, in GB."""
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("GlobalMemoryStatusEx failed")
        return status.ullAvailPhys / 1e9
    return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e9


def default_workers(per_worker_gb, requested=None, *, cpu_count=None, available_gb=None):
    """``requested`` if given, else min(60% of the CPUs, 60% of the free RAM / per_worker_gb), at least 1."""
    if requested is not None:
        return requested
    available = available_memory_gb() if available_gb is None else available_gb
    return max(1, min(cpu_workers(cpu_count), math.floor(CPU_FRACTION * available / per_worker_gb)))


def free_disk_gb(path):
    return shutil.disk_usage(path).free / 1e9


@dataclass
class StepResult:
    code: int
    seconds: float
    peak_bytes: "int | None"
    last_line: str


def _tree_private_bytes(root_pid):
    """Sum of PrivateUsage over the process and its descendants (Windows)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        raise OSError("CreateToolhelp32Snapshot failed")
    children = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32First(wintypes.HANDLE(snap), ctypes.byref(entry))
        while ok:
            children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = k32.Process32Next(wintypes.HANDLE(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(wintypes.HANDLE(snap))
    pids, todo = {root_pid}, [root_pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in pids:
                pids.add(child)
                todo.append(child)
    total = 0
    for pid in pids:
        handle = k32.OpenProcess(0x1000 | 0x0400, False, pid)  # QUERY_LIMITED_INFORMATION | QUERY_INFORMATION
        if not handle:
            continue
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            if psapi.GetProcessMemoryInfo(wintypes.HANDLE(handle), ctypes.byref(counters), counters.cb):
                total += counters.PrivateUsage
        finally:
            k32.CloseHandle(wintypes.HANDLE(handle))
    return total


def run_measured(cmd, log_path, cwd, env=None):
    """Run ``cmd`` with stdout and stderr appended to ``log_path``; keep the last stdout line and the peak memory."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    last = ""
    peak = {"value": None}
    stop = threading.Event()
    with log_path.open("ab") as log:
        proc = subprocess.Popen([str(c) for c in cmd], cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)

        def sample():
            while True:
                try:
                    size = _tree_private_bytes(proc.pid)
                    if peak["value"] is None or size > peak["value"]:
                        peak["value"] = size
                except Exception:  # sampling is best effort: the step still runs
                    return
                if stop.wait(SAMPLE_SECONDS):
                    return

        sampler = None
        if os.name == "nt":
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
        for raw in proc.stdout:
            log.write(raw)
            log.flush()
            line = raw.decode("utf-8", "replace").strip()
            if line:
                last = line
        proc.stdout.close()
        code = proc.wait()
        stop.set()
        if sampler:
            sampler.join()
    if os.name != "nt":
        try:
            import resource
            peak["value"] = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * (
                1 if sys.platform == "darwin" else 1024)
        except Exception:
            peak["value"] = None
    return StepResult(code, time.monotonic() - started, peak["value"], last)
