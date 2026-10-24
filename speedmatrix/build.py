"""Build CLI for the speed matrix: ``python -m speedmatrix.build [--root DIR] <command>``.

Commands, in pipeline order:

- ``segments --pbf PATH [--bbox W S E N]``: segment table, admin areas and water lines into
  ``<root>/segments/<sha256[:12]>/`` (skipped if present); prints the directory.
- ``import-rules FILE...``: upserts the rules, vehicle groups and holidays of seed files into
  ``<root>/state.sqlite`` (author "seed", reason = file name) and replaces each file's unresolved items.
- ``speeds --segments DIR [--as-of DATE]``: base speed layer from the profile free-flow speeds and the
  active speed rules in force, into ``<root>/speeds/<version[:12]>/`` (skipped if present); prints it.
- ``groups --segments DIR --speeds DIR [--as-of DATE]``: one view per vehicle group of the state into
  ``<speeds>/groups/<as_of>/<name>.npz`` plus ``report.json``; prints the directory. The speed layer must
  have been built for the speed rules in force on the date (ReleaseMismatch otherwise), and the state must
  hold exactly one base group, the one the other groups' affected segments are measured against.
- ``rules-check --segments DIR [--as-of DATE]``: resolves every active rule's scope, one line per rule;
  exits 2 when any active rule has a zone or rule error (``groups`` and ``speeds`` would abort), or a rule
  of confidence "cao" or "tb" has orphans. Active rules with orphans are marked partial (``groups`` applies
  them without the missing parts); errors and orphans of inactive rules in force are printed as warnings.
- ``status``: segment builds, speed versions, groups and active rules per layer.

Exit codes: 0 on success; 2 after a MissingRoadData, RuleError, ZoneError, ReleaseMismatch or BuildError (message on stderr,
no partial output directory left behind); argparse errors keep argparse's own exit code.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started from another directory
    sys.path.insert(0, str(_REPO))

from model.spatial import MissingRoadData  # noqa: E402

try:
    from .groups import build_group_view, group_rules_sha, save_group_view
    from .rulecheck import blocks, resolve_rule
    from .rules import GROUP_NAME, LAYERS, RuleError, load_seed_file, validate_rule
    from .scopes import ScopeResolver, ZoneError
    from .segments import (SegmentTable, _sha256_file, build_admin, build_segments, build_water_lines, load_admin,
                           load_segments, load_water_lines, save_admin, save_segments, save_water_lines)
    from .speeds import (FreeFlowSource, ReleaseMismatch, build_speed_layer, check_layer_version, load_layer,
                         save_layer, speed_version)
    from .state import open_state
except ImportError:  # pragma: no cover - flat import
    from groups import build_group_view, group_rules_sha, save_group_view
    from rulecheck import blocks, resolve_rule
    from rules import GROUP_NAME, LAYERS, RuleError, load_seed_file, validate_rule
    from scopes import ScopeResolver, ZoneError
    from segments import (SegmentTable, _sha256_file, build_admin, build_segments, build_water_lines, load_admin,
                          load_segments, load_water_lines, save_admin, save_segments, save_water_lines)
    from speeds import (FreeFlowSource, ReleaseMismatch, build_speed_layer, check_layer_version, load_layer,
                        save_layer, speed_version)
    from state import open_state

DEFAULT_ROOT = _REPO / "runtime" / "matrix"
SEED_AUTHOR = "seed"
_TZ = timezone(timedelta(hours=7))


class BuildError(Exception):
    """A CLI input is unusable (missing directory, mismatched builds, no vehicle groups)."""


def today() -> date:
    """Today in Vietnam (UTC+7), the default ``--as-of``."""
    return datetime.now(_TZ).date()


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def _remove(*paths) -> None:
    for path in paths:
        if path.exists():
            shutil.rmtree(path)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class _State:
    """open_state as a context manager, so the SQLite file is always closed."""

    def __init__(self, root: Path):
        self.path = root / "state.sqlite"

    def __enter__(self):
        self.state = open_state(self.path)
        return self.state

    def __exit__(self, *exc):
        self.state.close()


def _load_inputs(segments_dir) -> tuple:
    """Segment table and a resolver with the admin areas and water lines stored next to it."""
    seg = Path(segments_dir)
    if not (seg / "segments.npz").is_file():
        raise BuildError(f"no segment build in {seg}")
    table = load_segments(seg)
    admin = load_admin(seg) if (seg / "admin.json").is_file() else None
    water = load_water_lines(seg) if (seg / "water.npz").is_file() else None
    return table, ScopeResolver(table, admin=admin, water=water)


# ---- segments --------------------------------------------------------------

def _segments_sha(pbf: Path, bbox) -> str:
    """SegmentTable.sha256 of the table build_segments would return, without reading the ways."""
    meta = {"pbf_sha256": _sha256_file(pbf), "bbox": bbox}
    return SegmentTable.sha256.fget(SimpleNamespace(meta=meta))


def cmd_segments(args) -> int:
    pbf = Path(args.pbf)
    if not pbf.is_file():
        raise BuildError(f"PBF not found: {pbf}")
    bbox = None if args.bbox is None else [float(v) for v in args.bbox]
    if bbox is not None and not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        raise BuildError(f"--bbox must be west south east north, got {bbox}")
    out = args.root / "segments" / _segments_sha(pbf, bbox)[:12]
    if out.is_dir():
        print(f"segments already built: {out.name}", file=sys.stderr)
        print(out)
        return 0
    staging = out.with_name(f".{out.name}.partial")
    _remove(staging)
    try:
        table = build_segments(pbf, bbox)
        if table.sha256[:12] != out.name:
            raise BuildError(f"segment hash changed while reading {pbf}")
        save_segments(table, staging)
        save_admin(build_admin(pbf), staging)
        save_water_lines(build_water_lines(pbf, bbox), staging)
        staging.rename(out)
    except BaseException:
        _remove(staging, staging.with_name(staging.name + ".tmp"))
        raise
    print(f"{table.n} segments, classes {table.meta['counts']}", file=sys.stderr)
    print(out)
    return 0


# ---- import-rules ----------------------------------------------------------

def _read_seed(path: Path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        seed = load_seed_file(path)
        for rule in seed.rules:
            validate_rule(rule)
    except RuleError as exc:
        raise RuleError(f"{path.name}: {exc}") from None
    except (ValueError, TypeError, KeyError) as exc:     # bad JSON, unknown group keys, bad holiday entries
        raise RuleError(f"{path.name}: {type(exc).__name__}: {exc}") from None
    area = doc.get("area") if isinstance(doc.get("area"), str) and doc.get("area") else path.stem
    return area, seed


def _last_activation_author(history: list) -> str:
    """Author of the latest history row that set ``active`` (the create row, or a row that flipped it)."""
    author, active = None, None
    for row in history:
        value = row["snapshot"]["active"]
        if author is None or value != active:
            author, active = row["author"], value
    return author


def _seed_file_of(history: list):
    """File name of the latest seed import that created or updated the rule; None if never seeded."""
    for row in reversed(history):
        if row["author"] == SEED_AUTHOR and row["action"] in ("create", "update"):
            return row["reason"]
    return None


def _retire_removed(state, file_name: str, keep: set) -> list:
    """Deactivate the rules imported from ``file_name`` that are no longer in it; returns their ids.

    Ids in ``keep`` (anywhere in this import run) are left alone, as are rules already inactive and rules
    whose on/off state a user set last.
    """
    removed = []
    for rule in state.rules(active_only=True):
        if rule.id in keep:
            continue
        history = state.history(rule.id)
        if _seed_file_of(history) != file_name or _last_activation_author(history) != SEED_AUTHOR:
            continue
        state.set_active(rule.id, False, author=SEED_AUTHOR, reason=f"removed from {file_name}")
        removed.append(rule.id)
    return removed


def cmd_import_rules(args) -> int:
    seeds, seen = [], {}
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            raise BuildError(f"seed file not found: {path}")
        area, seed = _read_seed(path)
        for rule in seed.rules:
            if rule.id in seen:
                raise RuleError(f"{path.name}: rule id {rule.id!r} also in {seen[rule.id]}")
            seen[rule.id] = path.name
        seeds.append((path, area, seed))
    with _State(args.root) as state:
        for path, area, seed in seeds:
            changed = 0
            for rule in seed.rules:
                history = state.history(rule.id)
                if history and _last_activation_author(history) != SEED_AUTHOR:
                    rule.active = history[-1]["snapshot"]["active"]      # a user's on/off choice wins
                state.upsert_rule(rule, author=SEED_AUTHOR, reason=path.name)
                changed += len(state.history(rule.id)) > len(history)
            removed = _retire_removed(state, path.name, {r.id for r in seed.rules} | set(seen))
            for group in seed.groups:
                state.upsert_group(group)
            for day, holiday in seed.holidays:
                state.add_holiday(day, holiday)
            state.add_unresolved(area, seed.unresolved)
            print(f"{path.name}: {len(seed.rules)} rules ({changed} changed), {len(seed.groups)} groups, "
                  f"{len(seed.holidays)} holidays, {len(seed.unresolved)} unresolved (area {area!r})")
            for rule_id in removed:
                print(f"{path.name}: deactivated {rule_id} (removed from {path.name})")
    return 0


# ---- speeds ----------------------------------------------------------------

def cmd_speeds(args) -> int:
    as_of = args.as_of or today()
    table, resolver = _load_inputs(args.segments)
    with _State(args.root) as state:
        rules = state.rules(active_only=True, as_of=as_of)
        source = FreeFlowSource()
        version = speed_version(table, source, rules)
        out = args.root / "speeds" / version[:12]
        if out.is_dir():
            print(f"speed layer already built: {out.name}", file=sys.stderr)
        else:
            staging = out.with_name(f".{out.name}.partial")
            _remove(staging)
            try:
                layer = build_speed_layer(table, source, rules, resolver)
                save_layer(layer, staging)
                stats = layer.stats
                _write_json(staging / "report.json", {
                    "version": layer.version, "as_of": as_of.isoformat(), "source_id": source.id,
                    "segments_dir": str(Path(args.segments)), "segments_sha256": table.sha256,
                    "speed_rules": sorted(stats["orphans"]),
                    "orphans": {k: v for k, v in stats["orphans"].items() if v}, "stats": stats})
                staging.rename(out)
            except BaseException:
                _remove(staging, staging.with_name(staging.name + ".tmp"))
                raise
        if version not in {v["version"] for v in state.speed_versions()}:
            state.record_speed_version(version, {"dir": str(out), "as_of": as_of.isoformat(),
                                                 "source_id": source.id, "segments_sha256": table.sha256})
    print(out)
    return 0


# ---- groups ----------------------------------------------------------------

def _closures_by_rule(views, rules, resolver) -> dict:
    by_id = {r.id: r for r in rules}
    out = {}
    for view in views:
        for rule_id in view.report["rules_applied"]:
            body = by_id[rule_id].body
            entry = out.setdefault(rule_id, {"action": body["action"], "groups": [], "segments": int(
                resolver.resolve(body["scope"], body.get("exempt")).indices.size)})
            entry["groups"].append(view.group.name)
    return out


def cmd_groups(args) -> int:
    as_of = args.as_of or today()
    table, resolver = _load_inputs(args.segments)
    speeds_dir = Path(args.speeds)
    if not (speeds_dir / "layer.json").is_file():
        raise BuildError(f"no speed layer in {speeds_dir}")
    layer = load_layer(speeds_dir)
    if layer.n != table.n:
        raise BuildError(f"speed layer {speeds_dir.name} has {layer.n} segments, {Path(args.segments).name} {table.n}")
    report_path = speeds_dir / "report.json"
    if report_path.is_file():
        built_on = _read_json(report_path).get("segments_sha256")
        if built_on and built_on != table.sha256:
            raise BuildError(f"speed layer {speeds_dir.name} was built on segments {built_on[:12]}, "
                             f"not {table.sha256[:12]}")
    with _State(args.root) as state:
        rules = state.rules(active_only=True, as_of=as_of)
        groups = state.groups()
    check_layer_version(layer, table, rules, as_of)
    if not groups:
        raise BuildError("no vehicle groups in the state; run import-rules first")
    bad = [g.name for g in groups if not GROUP_NAME.fullmatch(g.name)]
    if bad:
        raise BuildError(f"group names unusable as file names: {bad}")
    bases = [g.name for g in groups if g.is_base]
    if len(bases) != 1:
        raise BuildError(f"need exactly one base vehicle group (is_base), got {bases}")
    groups = sorted(groups, key=lambda g: not g.is_base)        # the base view first: the others use it
    tmp = speeds_dir / f".groups-{as_of.isoformat()}.tmp"
    final = speeds_dir / "groups" / as_of.isoformat()
    _remove(tmp)
    tmp.mkdir()
    try:
        views = []
        for group in groups:
            view = build_group_view(table, layer, group, rules, resolver, base=views[0] if views else None)
            save_group_view(view, tmp / f"{group.name}.npz")
            views.append(view)
        orphans = {}
        for view in views:
            orphans.update(view.report["orphans"])
        _write_json(tmp / "report.json", {
            "as_of": as_of.isoformat(), "speed_version": layer.version, "segments_sha256": table.sha256,
            "rules_sha": group_rules_sha(rules), "base_group": bases[0],
            "groups": {v.group.name: {"is_base": v.group.is_base, "caps": v.caps, "cap_rules": v.report["cap_rules"],
                                      "capped_segments": v.report["capped_segments"],
                                      "closed_segments": v.report["closed_segments"],
                                      "affected_segments": int(v.affected_idx.size),
                                      "affected_against": v.base or "layer",
                                      "rules_applied": v.report["rules_applied"]} for v in views},
            "closures": _closures_by_rule(views, rules, resolver),
            "orphans": orphans,
            "osm": {v.group.name: {"unparsed_conditional": v.report["unparsed_conditional"],
                                   "examples": v.report["examples"]} for v in views}})
        final.parent.mkdir(exist_ok=True)
        _remove(final)
        tmp.rename(final)
    except BaseException:
        _remove(tmp)
        raise
    for view in views:
        print(f"{view.group.name}: {view.capped_idx.size} capped, {view.closures.idx.size} closed, "
              f"{view.affected_idx.size} affected (against {view.base or 'the layer'})", file=sys.stderr)
    print(final)
    return 0


# ---- rules-check -----------------------------------------------------------

def _check_rule(resolver, rule) -> tuple:
    """(text, error, orphans) of one rule's scope resolution; text is None for a rule without scope."""
    result, error = resolve_rule(resolver, rule)
    if error:
        return error, True, []
    if result is None:
        return None, False, []
    text = f"segments={result.indices.size}	orphans={len(result.orphans)}"
    if result.orphans:
        text += "	" + json.dumps(result.orphans, ensure_ascii=False)
    return text, False, result.orphans


def cmd_rules_check(args) -> int:
    as_of = args.as_of or today()
    table, resolver = _load_inputs(args.segments)
    with _State(args.root) as state:
        in_force = state.rules(active_only=False, as_of=as_of)
    rules = [r for r in in_force if r.active]
    failing, partial = 0, []
    for rule in rules:
        head = f"{rule.id}\t{rule.kind}\t{rule.layer}\t{rule.confidence}"
        text, error, orphans = _check_rule(resolver, rule)
        if text is None:
            print(f"{head}\tno scope")
            continue
        fail = blocks(rule, error, orphans)
        failing += fail
        if orphans:
            partial.append(rule.id)
        print(f"{head}\t{text}" + ("\tPARTIAL" if orphans else "") + ("\tFAIL" if fail else ""))
    inactive = [r for r in in_force if not r.active]
    warned = 0
    for rule in inactive:
        text, error, orphans = _check_rule(resolver, rule)
        if error or orphans:
            warned += 1
            print(f"{rule.id}\t{rule.kind}\t{rule.layer}\t{rule.confidence}\tinactive\t{text}\tWARN")
    if partial:
        print("partial: " + ", ".join(partial))
    print(f"{len(inactive)} inactive rules in force, {warned} with warnings")
    print(f"{len(rules)} active rules in force on {as_of.isoformat()}, {failing} failing, {len(partial)} partial")
    return 2 if failing else 0


# ---- status ----------------------------------------------------------------

def _builds(parent: Path) -> list:
    return sorted(p for p in parent.iterdir() if p.is_dir() and not p.name.startswith(".")) \
        if parent.is_dir() else []


def cmd_status(args) -> int:
    root = args.root
    print(f"root: {root}")
    print("segments:")
    for seg in _builds(root / "segments"):
        meta = _read_json(seg / "meta.json") if (seg / "meta.json").is_file() else {}
        print(f"  {seg.name}\tsegments={sum(meta.get('counts', {}).values())}\tbbox={meta.get('bbox')}"
              f"\tpbf_sha256={str(meta.get('pbf_sha256'))[:12]}")
    print("speed layers:")
    recorded = {}
    state_file = root / "state.sqlite"
    if state_file.is_file():
        with _State(root) as state:
            recorded = {v["version"][:12]: v for v in state.speed_versions()}
            groups = state.groups()
            active = Counter(r.layer for r in state.rules(active_only=True))
    else:
        groups, active = [], Counter()
    for spd in _builds(root / "speeds"):
        info = recorded.get(spd.name, {})
        report = _read_json(spd / "report.json") if (spd / "report.json").is_file() else {}
        views = [d.name for d in _builds(spd / "groups")]
        print(f"  {spd.name}\tcreated={info.get('created_at', '?')}\tas_of={report.get('as_of', '?')}"
              f"\tgroup views={views}")
    print("vehicle groups: " + (", ".join(g.name + (" (base)" if g.is_base else "") for g in groups) or "none"))
    print("active rules: " + "  ".join(f"{layer}={active.get(layer, 0)}" for layer in LAYERS))
    if not state_file.is_file():
        print("no state yet; run import-rules")
    return 0


# ---- entry point -----------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m speedmatrix.build", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"output root (default {DEFAULT_ROOT})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("segments", help="segment table, admin areas and water lines from a PBF")
    p.add_argument("--pbf", required=True)
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    p.set_defaults(func=cmd_segments)

    p = sub.add_parser("import-rules", help="upsert seed files into the state")
    p.add_argument("files", nargs="+", metavar="FILE")
    p.set_defaults(func=cmd_import_rules)

    for name, func, help_text in (("speeds", cmd_speeds, "base speed layer"),
                                  ("groups", cmd_groups, "per-group views of a speed layer"),
                                  ("rules-check", cmd_rules_check, "resolve every active rule's scope")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--segments", required=True, metavar="DIR")
        if name == "groups":
            p.add_argument("--speeds", required=True, metavar="DIR")
        p.add_argument("--as-of", type=_iso_date, default=None, metavar="YYYY-MM-DD",
                       help="rules in force on this day (default today, UTC+7)")
        p.set_defaults(func=func)

    p = sub.add_parser("status", help="list builds, speed versions, groups and active rules")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (MissingRoadData, RuleError, ZoneError, ReleaseMismatch, BuildError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
