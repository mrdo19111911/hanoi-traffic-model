"""Per-group views of the base speed layer: legal speed caps and closures for one vehicle group.

A view stores the class caps, the segments where a cap is below the layer's speed in some slot
(``capped_idx``) and the closures. ``group_speeds`` applies them to one (day type, slot) of the layer:
``min(base, cap)``, and 0 where the segment is closed.

``affected_idx`` follows spec §5.4/§8: the base group (``is_base``) has full pieces, and every other group
reuses the base group's pieces wherever it is not affected. So the base group's ``affected_idx`` is
measured against the bare layer (capped or closed segments), and every other group's against the base
group's view: the segments whose speed or closure differs from the base group's in some slot.
"""
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path

import numpy as np

try:
    from .bans import ClosureSet, group_closures
    from .rules import rule_set_sha
    from .segments import ROAD_CLASSES
    from .speeds import ReleaseMismatch
    from .timeslots import DAYTYPES
    from .vehicles import VehicleGroup, matches
except ImportError:  # pragma: no cover - flat import
    from bans import ClosureSet, group_closures
    from rules import rule_set_sha
    from segments import ROAD_CLASSES
    from speeds import ReleaseMismatch
    from timeslots import DAYTYPES
    from vehicles import VehicleGroup, matches

GROUP_VIEW_FORMAT = 3            # 3: affected_idx of a non-base group is measured against the base view
GROUP_RULE_KINDS = ("ban", "cap")
_NO_CAP = 255                      # uint8 speeds never exceed it, so an uncapped class never binds


@dataclass
class GroupView:
    group: VehicleGroup
    speed_version: str
    rules_sha: str                 # rule_set_sha of the active ban and cap rules the view was built from
    caps: dict                     # road class -> km/h, the minimum over the matching cap rules
    closures: ClosureSet
    capped_idx: np.ndarray         # (k,) int64, sorted: segments where the cap binds in some slot of some day type
    affected_idx: np.ndarray       # (m,) int64, sorted: segments whose speeds differ from ``base``'s in some slot
    report: dict = field(default_factory=dict)
    base: str | None = None        # base group the affected set is measured against; None = the bare layer


def group_caps(group: VehicleGroup, rules: list) -> dict:
    """Minimum per class over the active ``cap`` rules whose vehicle condition matches the group."""
    caps = {}
    for rule in rules:
        if not rule.active or rule.kind != "cap" or not matches(rule.body.get("vehicle"), group):
            continue
        for cls, kmh in rule.body["road_classes"].items():
            caps[cls] = min(caps.get(cls, math.inf), float(kmh))
    return caps


def _cap_table(caps: dict) -> np.ndarray:
    """uint8 cap per road-class code; a fractional cap is floored (never above the legal limit), at least 1."""
    table = np.full(len(ROAD_CLASSES), _NO_CAP, dtype=np.uint8)
    for cls, kmh in caps.items():
        table[ROAD_CLASSES.index(cls)] = max(1, min(_NO_CAP, math.floor(kmh)))
    return table


def _capped(table, layer, caps: dict) -> np.ndarray:
    if not caps:
        return np.zeros(0, dtype=np.int64)
    peak = None
    for daytype in DAYTYPES:
        top = layer.base[daytype].copy()
        idx = layer.dyn_idx[daytype]
        if idx.size:
            top[idx] = layer.dyn[daytype].max(axis=1)
        peak = top if peak is None else np.maximum(peak, top)
    return np.flatnonzero(peak > _cap_table(caps)[table.segment_class]).astype(np.int64)


def group_rules_sha(rules: list) -> str:
    """rule_set_sha of the active ban and cap rules (the caller filters them to those in force)."""
    return rule_set_sha([r for r in rules if r.kind in GROUP_RULE_KINDS])


def _check_view(view: GroupView, layer) -> None:
    if view.speed_version != layer.version:
        raise ReleaseMismatch(f"group view {view.group.name} was built on speed layer {view.speed_version[:12]}, "
                              f"not {layer.version[:12]}")


def _differs(table, layer, view: GroupView, base: GroupView) -> np.ndarray:
    """Sorted segments where ``view`` and ``base`` give different speeds in some slot of some day type.

    Outside both groups' capped and closed segments both see the layer's speed, so only those are compared.
    """
    cand = np.union1d(np.union1d(view.capped_idx, base.capped_idx),
                      np.union1d(view.closures.idx, base.closures.idx)).astype(np.int64)
    if cand.size == 0:
        return cand
    cls = table.segment_class[cand]
    cap_v, cap_b = _cap_table(view.caps)[cls][:, None], _cap_table(base.caps)[cls][:, None]
    pos_v, pos_b = np.searchsorted(cand, view.closures.idx), np.searchsorted(cand, base.closures.idx)
    differ = np.zeros(cand.size, dtype=bool)
    for di, daytype in enumerate(DAYTYPES):
        rows = layer.rows(daytype, cand)
        speed_v, speed_b = np.minimum(rows, cap_v), np.minimum(rows, cap_b)
        del rows
        speed_v[pos_v] = np.where(view.closures.mask[:, di, :], 0, speed_v[pos_v])
        speed_b[pos_b] = np.where(base.closures.mask[:, di, :], 0, speed_b[pos_b])
        differ |= (speed_v != speed_b).any(axis=1)
    return cand[differ]


def build_group_view(table, layer, group: VehicleGroup, rules: list, resolver, base: GroupView | None = None) -> GroupView:
    """Caps and closures of one group against a base speed layer built on the same table.

    ``rules`` are the rules in force on the view's date; ``rules_sha`` hashes their active ban and cap rules.
    ``base`` is the base group's view on the same layer: a non-base group's ``affected_idx`` is then the set of
    segments that differ from it. Without ``base``, or for the base group itself, it is measured against the
    bare layer.
    """
    if layer.n != table.n:
        raise ValueError(f"speed layer has {layer.n} segments, the table {table.n}")
    if base is not None:
        if not base.group.is_base:
            raise ValueError(f"group {base.group.name} is not the base group")
        _check_view(base, layer)
    caps = group_caps(group, rules)
    closures, report = group_closures(table, group, rules, resolver)
    capped = _capped(table, layer, caps)
    report["cap_rules"] = sorted(r.id for r in rules if r.active and r.kind == "cap"
                                 and matches(r.body.get("vehicle"), group))
    report["capped_segments"] = int(capped.size)
    report["closed_segments"] = int(closures.idx.size)
    view = GroupView(group=group, speed_version=layer.version, rules_sha=group_rules_sha(rules), caps=caps, closures=closures, capped_idx=capped,
                     affected_idx=np.union1d(capped, closures.idx).astype(np.int64), report=report)
    if base is not None and not group.is_base:
        view.affected_idx = _differs(table, layer, view, base)
        view.base = base.group.name
    return view


def group_speeds(view: GroupView, layer, table, daytype: str, slot: int) -> np.ndarray:
    """uint8 (n,) speeds of the group in one slot: min(base, cap), 0 on segments closed in that slot.

    Raises ReleaseMismatch when the view was built on another speed layer: its capped set would be stale.
    """
    _check_view(view, layer)
    out = layer.at(daytype, slot)
    idx = view.capped_idx
    if idx.size:
        out[idx] = np.minimum(out[idx], _cap_table(view.caps)[table.road_class[table.way[idx]]])
    out[view.closures.closed_at(DAYTYPES.index(daytype), slot)] = 0
    return out


def save_group_view(view: GroupView, path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"format": GROUP_VIEW_FORMAT, "group": asdict(view.group), "speed_version": view.speed_version,
            "rules_sha": view.rules_sha, "caps": view.caps, "report": view.report, "base": view.base}
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as sink:
        np.savez_compressed(sink, capped_idx=view.capped_idx, affected_idx=view.affected_idx,
                            closure_idx=view.closures.idx, closure_mask=view.closures.mask,
                            meta=np.array(json.dumps(meta, ensure_ascii=False)))
    tmp.replace(path)


def load_group_view(path) -> GroupView:
    with np.load(Path(path)) as data:
        meta = json.loads(str(data["meta"]))
        if meta.get("format") != GROUP_VIEW_FORMAT:
            raise ValueError(f"{path}: group view format {meta.get('format')} != {GROUP_VIEW_FORMAT}")
        return GroupView(group=VehicleGroup(**meta["group"]), speed_version=meta["speed_version"],
                         rules_sha=meta["rules_sha"], caps=meta["caps"], closures=ClosureSet(data["closure_idx"], data["closure_mask"]),
                         capped_idx=data["capped_idx"], affected_idx=data["affected_idx"], report=meta["report"],
                         base=meta["base"])
