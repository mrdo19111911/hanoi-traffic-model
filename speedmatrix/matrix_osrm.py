"""Build the matrix OSRM dataset (MLD) from a PBF with a patched copy of the stock car profile.

``python -m speedmatrix.matrix_osrm --pbf PATH --out DIR [--threads N]``

Patches, applied as text to a copy of ``osrm/tools/profiles`` (each must match exactly once, so an OSRM upgrade
that moves the code fails loudly instead of silently building the stock profile):
- weight_name = 'duration';
- private/delivery/destination/customers/permit/residents leave restricted_access_tag_list, so access_tag_blacklist
  makes them inaccessible;
- construction=* is ignored when the way's highway is an operating class (a stale tag on an open road).
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started as a script
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "osrm"))
try:
    import manage  # noqa: E402
finally:
    sys.path.remove(str(_REPO / "osrm"))

try:
    from .osrmfiles import OSRM_VERSION, check_build_json
    from .sysres import cpu_workers, run_measured
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.osrmfiles import OSRM_VERSION, check_build_json
    from speedmatrix.sysres import cpu_workers, run_measured

PATCHES = [
    ("car.lua", "weight_name                     = 'routability',",
     "weight_name                     = 'duration', -- matrix profile: weight equals duration"),
    ("car.lua", """    restricted_access_tag_list = Set {
      'private',
      'delivery',
      'destination',
      'customers',
      'permit',
      'residents',
""", """    -- matrix profile: private/delivery/destination/customers/permit/residents are no longer "restricted" (penalised),
    -- so access_tag_blacklist makes those ways and nodes inaccessible
    restricted_access_tag_list = Set {
"""),
    ("car.lua", """    construction_whitelist = Set {
      'no',
      'widening',
      'minor',
    },
""", """    construction_whitelist = Set {
      'no',
      'widening',
      'minor',
    },

    -- matrix profile: construction=* on a way whose highway is an operating class is a stale tag, not a closure
    construction_ignored_on_open_highway = true,
"""),
    ("lib/way_handlers.lua", "    if construction and not profile.construction_whitelist[construction] then",
     "    if construction and not profile.construction_whitelist[construction] and\n"
     "       not (profile.construction_ignored_on_open_highway and data.highway and data.highway ~= 'construction') then"),
]
STEPS = ("extract", "partition", "customize")


class ProfilePatchError(RuntimeError):
    """A profile patch did not match exactly once."""


def patch_profiles(src, dst):
    """Copy the profile tree ``src`` to ``dst`` and patch the copy; the applied patches as dicts."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    applied = []
    for name, old, new in PATCHES:
        target = dst / name
        text = target.read_bytes().decode("utf-8")
        count = text.count(old)
        if count != 1:
            raise ProfilePatchError(f"patch for {name} matches {count} times, expected 1")
        target.write_bytes(text.replace(old, new).encode("utf-8"))
        applied.append({"file": name, "new": new})
    return applied


def profiles_sha256(directory):
    """sha256 over the sorted relative paths and bytes of the files under ``directory``."""
    directory = Path(directory)
    h = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        h.update(path.relative_to(directory).as_posix().encode("utf-8") + b"\0")
        h.update(path.read_bytes())
    return h.hexdigest()


def build_matrix_osrm(pbf, out, threads, tools=manage.TOOLS, runner=run_measured):
    """Extract, partition and customize ``pbf`` into ``out``; the build record (also ``out/build_matrix.json``)."""
    pbf, out, tools = Path(pbf).resolve(), Path(out).resolve(), Path(tools).resolve()  # children run with cwd=data
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    logs = out / "logs"
    target = out / "build_matrix.json"
    target.unlink(missing_ok=True)  # a failed re-run must not leave the previous build's record
    expected = OSRM_VERSION.lstrip("v")
    found = runner([tools / "osrm-extract.exe", "--version"], logs / "version.log", out).last_line
    if not re.search(r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])", found):
        raise RuntimeError(f"osrm-extract reports {found}, expected {OSRM_VERSION}")
    applied = patch_profiles(tools / "profiles", out / "profiles")
    pbf_copy = data / pbf.name
    shutil.copyfile(pbf, pbf_copy)
    sha = manage.digest(pbf_copy)
    stem = pbf_copy.name
    for suffix in (".osm.pbf", ".pbf", ".osm"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    base = data / (stem + ".osrm")
    commands = {
        "extract": [tools / "osrm-extract.exe", "-p", out / "profiles" / "car.lua", "-t", str(threads), pbf_copy],
        "partition": [tools / "osrm-partition.exe", "-t", str(threads), base],
        "customize": [tools / "osrm-customize.exe", "-t", str(threads), base],
    }
    steps = {}
    for step in STEPS:
        log = logs / f"{step}.log"
        result = runner(commands[step], log, data)
        if result.code != 0:
            raise RuntimeError(f"osrm-{step} failed with exit {result.code}, see {log}")
        steps[step] = {"seconds": result.seconds, "peak_bytes": result.peak_bytes}
    info = {"input": {"pbf_sha256": sha, "osrm_version": OSRM_VERSION},
            "pbf_md5": manage.digest(pbf_copy, "md5"), "profiles_sha256": profiles_sha256(out / "profiles"),
            "patches": applied, "steps": steps}
    target.write_text(json.dumps(info, indent=1), encoding="utf-8")
    check_build_json(target)
    info["pbf_copy"] = pbf_copy
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pbf", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--threads", type=int, default=cpu_workers())
    args = ap.parse_args(argv)
    info = build_matrix_osrm(args.pbf, args.out, args.threads)
    print(json.dumps({k: v for k, v in info.items() if k != "patches"}, indent=1, default=str))


if __name__ == "__main__":
    main()
