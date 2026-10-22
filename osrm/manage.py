#!/usr/bin/env python3
"""Install and run the official Windows OSRM backend locally (Python stdlib only)."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = "v26.10.0"
TOOLS = ROOT / "tools"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DOWNLOADS = ROOT / "downloads"
PBF = DATA / "vietnam-latest.osm.pbf"
BASE = DATA / "vietnam-latest.osrm"
BUILD = ROOT / "build.json"
SERVER = ROOT / "server.json"
MAP_URL = "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf"
SAMPLE = "/route/v1/driving/105.8525,21.0285;105.8000,21.2142?overview=full&geometries=geojson&steps=true"


def save_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def digest(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, path):
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, temp.open("wb") as target:
        total, reported = 0, 0
        while chunk := response.read(1024 * 1024):
            target.write(chunk)
            total += len(chunk)
            if total - reported >= 32 * 1024 * 1024:
                print(f"  {total / 1024**2:.0f} MiB", flush=True)
                reported = total
    temp.replace(path)


def process_identity(pid):
    """Check the executable and creation time before acting on a saved PID."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return None
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return {"executable": str(Path(name.value).resolve()), "created": created}
    finally:
        kernel.CloseHandle(handle)


def managed_server():
    state = read_json(SERVER)
    if state and process_identity(state["pid"]) == state.get("identity"):
        return state
    return None


def route(port=5000):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{SAMPLE}", timeout=5) as response:
        data = json.load(response)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"Routing failed: {data.get('code')}")
    return data


def setup(args):
    if managed_server():
        raise RuntimeError("Stop the managed server before preparing its data.")
    for directory in [TOOLS, DATA, LOGS, DOWNLOADS]:
        directory.mkdir(parents=True, exist_ok=True)
    binary = DOWNLOADS / f"osrm-{VERSION}-win32-x64.tar.gz"
    # Reuse the archive from the initial installation, if present.
    initial = ROOT / binary.name
    if initial.exists() and not binary.exists():
        initial.replace(binary)
    download(f"https://github.com/Project-OSRM/osrm-backend/releases/download/{VERSION}/node_osrm-{VERSION}-8-win32-x64-Release.tar.gz", binary)
    with tarfile.open(binary) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith(".exe"):
                with archive.extractfile(member) as source:
                    (TOOLS / PurePosixPath(member.name).name).write_bytes(source.read())
    source_tar = DOWNLOADS / f"osrm-{VERSION}-source.tar.gz"
    download(f"https://codeload.github.com/Project-OSRM/osrm-backend/tar.gz/refs/tags/{VERSION}", source_tar)
    with tarfile.open(source_tar) as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if member.isfile() and len(parts) > 2 and parts[1] == "profiles":
                if any(part in ("..", ".") for part in parts[1:]):
                    raise RuntimeError("Invalid profile archive path")
                target = TOOLS.joinpath(*parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source:
                    target.write_bytes(source.read())
    version = subprocess.check_output([str(TOOLS / "osrm-extract.exe"), "--version"], text=True).strip()
    if version != VERSION:
        raise RuntimeError(f"Unexpected OSRM version: {version}")
    download(MAP_URL, PBF)
    checksum = Path(str(PBF) + ".md5")
    download(MAP_URL + ".md5", checksum)
    if digest(PBF, "md5") != checksum.read_text().split()[0]:
        raise RuntimeError("Map checksum mismatch; retain the files for inspection, then download a matching PBF and MD5 again.")
    fingerprint = {"osrm_version": VERSION, "pbf_sha256": digest(PBF),
                   "binary_sha256": digest(binary), "profiles_sha256": digest(source_tar)}
    state = read_json(BUILD)
    if args.rebuild or state.get("input") != fingerprint:
        state = {"input": fingerprint, "completed": []}
        save_json(BUILD, state)
    stages = [
        ("extract", [str(PBF), "-p", str(TOOLS / "profiles" / "car.lua")], ".ebg"),
        ("partition", [str(BASE)], ".partition"),
        ("customize", [str(BASE)], ".cell_metrics"),
    ]
    for name, parameters, output in stages:
        if name in state["completed"] and Path(str(BASE) + output).exists():
            print(f"Already completed: {name}", flush=True)
            continue
        # A rerun invalidates all downstream stages.
        index = [stage[0] for stage in stages].index(name)
        state["completed"] = [s for s in state["completed"] if s in [x[0] for x in stages[:index]]]
        save_json(BUILD, state)
        command = [str(TOOLS / f"osrm-{name}.exe"), *parameters, "--threads", str(args.threads)]
        print("Running: " + subprocess.list2cmdline(command), flush=True)
        with (LOGS / f"{name}.log").open("w", encoding="utf-8") as log:
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log.write(line)
                    log.flush()
                if process.wait():
                    raise RuntimeError(f"{name} failed; see {LOGS / (name + '.log')}")
        state["completed"].append(name)
        save_json(BUILD, state)
    print("Data ready. Start with: python osrm/manage.py start", flush=True)


def server_command(args):
    return [str(TOOLS / "osrm-routed.exe"), "--algorithm", "mld", "--ip", "127.0.0.1",
            "--port", str(args.port), "--threads", str(args.threads),
            "--max-table-size", str(args.max_table_size), str(BASE)]


def start(args):
    existing = managed_server()
    if existing:
        print(f"Already running: PID {existing['pid']}, port {existing['port']}")
        route(existing["port"])
        return
    if "customize" not in read_json(BUILD).get("completed", []):
        raise RuntimeError("Run: python osrm/manage.py setup")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError(f"Port {args.port} is already occupied; no process was changed.")
    LOGS.mkdir(exist_ok=True)
    command = server_command(args)
    with (LOGS / "server.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                                   close_fds=True)
    state = {"pid": process.pid, "identity": process_identity(process.pid), "port": args.port,
             "max_table_size": args.max_table_size}
    if state["identity"] is None:
        raise RuntimeError("Server exited before its process identity could be recorded; see osrm/logs/server.log")
    save_json(SERVER, state)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("OSRM exited; see osrm/logs/server.log")
        try:
            result = route(args.port)
            print(f"Ready: http://127.0.0.1:{args.port} (PID {process.pid})")
            print(f"Ho Guom -> Noi Bai: {result['routes'][0]['distance']/1000:.2f} km")
            return
        except (OSError, ValueError, RuntimeError):
            time.sleep(1)
    raise RuntimeError("Server is still starting; inspect osrm/logs/server.log and run status.")


def stop(_args):
    state = managed_server()
    if not state:
        print("No managed OSRM process is running.")
        return
    os.kill(state["pid"], 15)
    SERVER.unlink(missing_ok=True)
    print(f"Stopped OSRM PID {state['pid']}.")


def status(args):
    state = managed_server()
    if not state:
        raise RuntimeError("Managed OSRM server is not running.")
    result = route(state["port"])
    print(f"Running: PID {state['pid']}, http://127.0.0.1:{state['port']}, max table size {state.get('max_table_size', 100)}")
    print(f"Route check: {result['code']}, {result['routes'][0]['distance']/1000:.2f} km")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "start", "stop", "status"])
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--max-table-size", type=int, default=2500,
                        help="Largest coordinate count osrm-routed accepts in one /table request")
    parser.add_argument("--rebuild", action="store_true", help="Repeat all preprocessing stages")
    return parser


def validate(parser, args):
    if args.threads < 1 or not 1 <= args.port <= 65535:
        parser.error("threads must be positive and port must be between 1 and 65535")
    if args.max_table_size < 1:
        parser.error("max-table-size must be positive")


def main():
    parser = build_parser()
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This launcher uses the official Windows x64 binaries.")
    validate(parser, args)
    try:
        {"setup": setup, "start": start, "stop": stop, "status": status}[args.action](args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
