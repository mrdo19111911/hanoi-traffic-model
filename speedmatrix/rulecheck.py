"""Rules on a new map: which rules still resolve, which moved or changed, which lost their roads.

``python -m speedmatrix.rulecheck --segments DIR [--previous DIR] [--as-of YYYY-MM-DD] --out FILE [--root ROOT]``
resolves every rule in force on the new segment table (and on the previous one, when given) and writes a JSON
report. It reads ``<root>/state.sqlite`` read-only and exits 0 whenever the report was written, whatever
``blocking`` holds: the caller decides (Decisions 16 and 27). The blocking policy is the one of
``speedmatrix.build rules-check``: ``blocks`` below is shared by both.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started from another directory
    sys.path.insert(0, str(_REPO))

try:
    from .rules import RuleError
    from .scopes import ScopeResolver, ZoneError
    from .segments import find_segments, load_admin, load_segments, load_water_lines
    from .state import State
except ImportError:  # pragma: no cover - flat import
    from rules import RuleError
    from scopes import ScopeResolver, ZoneError
    from segments import find_segments, load_admin, load_segments, load_water_lines
    from state import State

TRUSTED = ("cao", "tb")
CHANGED_FRACTION = 0.2      # "changed": the segment count moves by more than this share of the old one ...
CHANGED_MIN = 10            # ... and by more than this many segments
STATUSES = ("ok", "moved", "changed", "new_orphans", "error", "no_scope")
_TZ = timezone(timedelta(hours=7))


def resolve_rule(resolver, rule) -> tuple:
    """``(ScopeResult | None, error text | None)``; ``(None, None)`` for a rule without a scope."""
    if "scope" not in rule.body:
        return None, None
    try:
        return resolver.resolve(rule.body["scope"], rule.body.get("exempt")), None
    except (ZoneError, RuleError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def blocks(rule, error, orphans) -> bool:
    """True when an active rule must fail the check: any error, or orphans on a trusted (cao/tb) rule.

    ``groups`` and ``speeds`` abort on any active rule's error; they apply a rule with orphans without the
    missing parts, which fails the check for trusted rules only.
    """
    return bool(rule.active) and (bool(error) or (bool(orphans) and rule.confidence in TRUSTED))


def _key(orphan: dict) -> str:
    return json.dumps(orphan, sort_keys=True, ensure_ascii=False)


def _was(old: ScopeResolver | None, orphan: dict):
    """``[[lat, lon], [lat, lon]]`` of an orphan anchor on the old table, in the anchor's node order."""
    anchor = orphan.get("anchor")
    if old is None or anchor is None:
        return None
    t = old.table
    row = int(find_segments(t, [anchor])[0])
    if row < 0:
        return None
    first = [float(t.a_lat[row]), float(t.a_lon[row])]
    second = [float(t.b_lat[row]), float(t.b_lon[row])]
    return [first, second] if int(t.a[row]) == int(anchor[1]) else [second, first]


def _status(error, orphans_new, orphans_old, n_new, n_old, moved) -> str:
    """The first that applies of error, new_orphans, changed, moved, ok."""
    if error:
        return "error"
    seen = {_key(o) for o in orphans_old}
    if any(_key(o) not in seen for o in orphans_new):
        return "new_orphans"
    if n_old is not None:
        delta = abs(n_new - n_old)
        if delta > CHANGED_FRACTION * n_old and delta > CHANGED_MIN:
            return "changed"
    return "moved" if moved else "ok"


def rules_report(new: ScopeResolver, rules: list, old: ScopeResolver | None = None) -> dict:
    """Per-rule resolution on the new map (compared with the old one), a summary and the blocking rule ids."""
    out, blocking = {}, []
    for rule in rules:
        entry = {"kind": rule.kind, "layer": rule.layer, "confidence": rule.confidence, "active": bool(rule.active),
                 "segments_new": None, "segments_old": None, "orphans_new": [], "orphans_old": [],
                 "moved": [], "error": None, "status": "no_scope"}
        out[rule.id] = entry
        result, error = resolve_rule(new, rule)
        if result is None and error is None:
            continue
        if old is not None:
            before, _ = resolve_rule(old, rule)
            if before is not None:
                entry["segments_old"] = int(before.indices.size)
                entry["orphans_old"] = list(before.orphans)
        orphans = []
        if error:
            entry["error"] = error
        else:
            orphans = result.orphans
            entry["segments_new"] = int(result.indices.size)
            entry["orphans_new"] = [dict(o, was=_was(old, o)) if _was(old, o) else dict(o) for o in orphans]
            entry["moved"] = list(result.moved)
        entry["status"] = _status(error, orphans, entry["orphans_old"], entry["segments_new"] or 0,
                                  entry["segments_old"], entry["moved"])
        if blocks(rule, error, orphans):
            blocking.append(rule.id)
    counts = Counter(e["status"] for e in out.values())
    summary = {s: counts.get(s, 0) for s in STATUSES}
    summary["rules"] = len(out)
    summary["blocking"] = len(blocking)
    return {"rules": out, "summary": summary, "blocking": blocking}


# ---- CLI -------------------------------------------------------------------

def _resolver(segments_dir) -> ScopeResolver:
    seg = Path(segments_dir)
    if not (seg / "segments.npz").is_file():
        raise FileNotFoundError(f"no segment build in {seg}")
    admin = load_admin(seg) if (seg / "admin.json").is_file() else None
    water = load_water_lines(seg) if (seg / "water.npz").is_file() else None
    return ScopeResolver(load_segments(seg), admin=admin, water=water)


def rules_in_force(root, as_of: date) -> list:
    """Rules (active or not) in force on ``as_of`` through a read-only connection: no DDL, no schema upgrade."""
    path = Path(root) / "state.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"no state in {path}")
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        return State(db).rules(active_only=False, as_of=as_of)
    finally:
        db.close()


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m speedmatrix.rulecheck", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=_REPO / "runtime" / "matrix")
    parser.add_argument("--segments", required=True, metavar="DIR")
    parser.add_argument("--previous", default=None, metavar="DIR")
    parser.add_argument("--as-of", type=_iso_date, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args([str(a) for a in argv] if argv is not None else None)
    as_of = args.as_of or datetime.now(_TZ).date()
    rules = rules_in_force(args.root, as_of)
    new = _resolver(args.segments)
    old = _resolver(args.previous) if args.previous else None
    report = rules_report(new, rules, old)
    report["as_of"] = as_of.isoformat()
    report["segments"] = str(args.segments)
    report["previous"] = str(args.previous) if args.previous else None
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(args.out)
    print(f"{report['summary']['rules']} rules, blocking: {', '.join(report['blocking']) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
