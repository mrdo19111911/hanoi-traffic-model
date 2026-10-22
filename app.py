#!/usr/bin/env python3
"""Local application launcher: python app.py setup|start|status|stop."""
import argparse
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import venv

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv/Scripts/python.exe"
RUNTIME = ROOT / "runtime"
STATE = RUNTIME / "web.json"
URL = "http://127.0.0.1:8000"


def manager():
    spec = importlib.util.spec_from_file_location("osrm_manager", ROOT / "osrm/manage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(command, cwd=ROOT):
    subprocess.run([str(arg) for arg in command], cwd=cwd, check=True)


def health():
    with urllib.request.urlopen(URL + "/api/health", timeout=3) as response:
        return json.load(response)


def owned():
    if not STATE.exists():
        return None
    state = json.loads(STATE.read_text(encoding="utf-8"))
    return state if manager().process_identity(state["pid"]) == state["identity"] else None


def big_roads_outdated(root, pbf_sha256):
    """True when the big-road build is missing, from another map, or from another policy."""
    spec = importlib.util.spec_from_file_location("osm_classes", ROOT / "model/osm_classes.py")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    try:
        meta = json.loads((root / "osrm/data/big_roads.meta.json").read_text(encoding="utf-8"))
        return meta["pbf_sha256"] != pbf_sha256 or meta["policy_version"] != policy.BIG_ROAD_POLICY_VERSION
    except (OSError, ValueError, KeyError, TypeError):
        return True


def _derived_outdated(root, name):
    """True when osrm/data/<name> is missing, unreadable or made from another big-road build (map or policy)."""
    try:
        meta = json.loads((root / "osrm/data/big_roads.meta.json").read_text(encoding="utf-8"))
        derived = json.loads((root / "osrm/data" / name).read_text(encoding="utf-8"))
        return any(derived[key] != meta[key] for key in ("pbf_sha256", "policy_version"))
    except (OSError, ValueError, KeyError, TypeError):
        return True


CLUSTER_PARTITIONS = {"province", "natural"}


def clusters_outdated(root):
    """Also outdated when the file is not the "regions" format or holds a partition other than province and natural."""
    if _derived_outdated(root, "big_road_clusters.json"):
        return True
    try:
        doc = json.loads((root / "osrm/data/big_road_clusters.json").read_text(encoding="utf-8"))
        return doc["method"] != "regions" or any(p["method"] not in CLUSTER_PARTITIONS for p in doc["partitions"])
    except (OSError, ValueError, KeyError, TypeError):
        return True


def provinces_outdated(root):
    return _derived_outdated(root, "vietnam_provinces.geojson")


def rivers_outdated(root):
    return _derived_outdated(root, "main_rivers.geojson")


def setup():
    if not PYTHON.exists():
        venv.create(ROOT / ".venv", with_pip=True)
    run([PYTHON, "-m", "pip", "install", "-r", "requirements-api.txt", "-r", "requirements-spatial.txt"])
    run([PYTHON, "osrm/manage.py", "setup"])
    index = ROOT / "osrm/data/road_classes.sqlite"
    expected = json.loads((ROOT / "osrm/build.json").read_text(encoding="utf-8"))["input"]["pbf_sha256"]
    indexed = None
    if index.exists():
        try:
            with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
                indexed = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])["pbf_sha256"]
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            pass
    if indexed != expected:
        run([PYTHON, "model/build_road_index.py"])
    rebuilt = big_roads_outdated(ROOT, expected)
    if rebuilt:
        # The big-road build bridges dead ends through local OSRM, so OSRM must be up first.
        run([PYTHON, "osrm/manage.py", "start"])
        run([PYTHON, "model/build_big_roads.py"])
    provinces = rebuilt or provinces_outdated(ROOT)
    if provinces:
        run([PYTHON, "model/build_provinces.py"])
    # Main rivers (for the "natural" partition) come from a survey of the OSM file, about 4 minutes.
    rivers = rebuilt or rivers_outdated(ROOT)
    if rivers:
        run([PYTHON, "model/natural_barriers.py"])
    # The cluster file carries the province and natural partitions, so it follows a new province or river file too.
    if provinces or rivers or clusters_outdated(ROOT):
        run([PYTHON, "model/clusters.py"])
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("Node.js/npm is required to build the frontend.")
    run([npm, "ci" if (ROOT / "frontend/package-lock.json").exists() else "install"], ROOT / "frontend")
    run([npm, "run", "build"], ROOT / "frontend")
    print("Setup complete. Run: python app.py start")


def start():
    if not PYTHON.exists() or not (ROOT / "frontend/dist/index.html").is_file():
        raise RuntimeError("Run python app.py setup first.")
    state = owned()
    if state:
        print("Already running:", URL, "PID", state["pid"])
        print(json.dumps(health()))
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise RuntimeError("Port 8000 is occupied by another process; it was left unchanged.")
    run([PYTHON, "osrm/manage.py", "start"])
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / "web.log").open("ab") as log:
        child = subprocess.Popen([str(PYTHON), str(ROOT / "app.py"), "serve"], cwd=ROOT,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
                                 close_fds=True)
    identity = manager().process_identity(child.pid)
    if not identity:
        raise RuntimeError("Web server exited; inspect runtime/web.log")
    manager().save_json(STATE, {"pid": child.pid, "identity": identity})
    for _ in range(30):
        if child.poll() is not None:
            raise RuntimeError("Web server exited; inspect runtime/web.log")
        try:
            result = health()
            print("Ready:", URL, "PID", child.pid)
            print(json.dumps(result))
            return
        except (OSError, ValueError):
            time.sleep(1)
    raise RuntimeError("Web server is still starting; inspect runtime/web.log")


def stop():
    state = owned()
    if not state:
        print("No managed web server is running.")
        return
    os.kill(state["pid"], 15)
    STATE.unlink(missing_ok=True)
    print("Stopped web server PID", state["pid"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="start", choices=["setup", "start", "stop", "status", "serve"])
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This local launcher targets Windows. Run uvicorn directly on other platforms.")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.action == "serve":
            import uvicorn
            uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, workers=1)
        elif args.action == "setup":
            setup()
        elif args.action == "start":
            start()
        elif args.action == "stop":
            stop()
        else:
            print("Running:", URL, json.dumps(health()))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print("ERROR:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
