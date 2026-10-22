"""Closures per vehicle group: which segments are closed in which (day type, slot).

Three layers are applied in order, each overwriting the mask cells its rules touch:
OSM tags (hgv, maxweight, maxheight and their conditionals), official ban rules,
company ban rules. Inside a rule layer, close rules go first, then open rules, each
sorted by id, so an exemption wins within its own layer. Date validity is the
caller's business.
"""
from dataclasses import dataclass

import numpy as np

try:
    from .rules import BAN_LAYERS, Rule
    from .scopes import ScopeResolver
    from .segments import HGV_VALUES, SegmentTable, parse_tonnes
    from .timeslots import DAYTYPES, SLOTS, ConditionalParseError, parse_conditional, when_mask
    from .vehicles import VehicleGroup, matches
except ImportError:  # pragma: no cover - flat import
    from rules import BAN_LAYERS, Rule
    from scopes import ScopeResolver
    from segments import HGV_VALUES, SegmentTable, parse_tonnes
    from timeslots import DAYTYPES, SLOTS, ConditionalParseError, parse_conditional, when_mask
    from vehicles import VehicleGroup, matches

HGV_GROSS_T = 3.5                      # OSM "hgv" means vehicles above 3.5 t gross
_SHAPE = (len(DAYTYPES), SLOTS)
_HGV_NO = HGV_VALUES.index("no")
_HGV_OPEN = frozenset(("yes", "designated", "destination", "delivery"))
_MAX_EXAMPLES = 20


@dataclass
class ClosureSet:
    idx: np.ndarray       # (k,) int64, sorted segment indices
    mask: np.ndarray      # (k, 3, 48) bool, True where the segment is closed

    def closed_at(self, daytype: int, slot: int) -> np.ndarray:
        return self.idx[self.mask[:, daytype, slot]]


def _pack(idx: np.ndarray, mask: np.ndarray) -> ClosureSet:
    keep = mask.any(axis=(1, 2))
    return ClosureSet(np.asarray(idx[keep], dtype=np.int64), mask[keep])


class _Sparse:
    """Sorted segment indices with their (3, 48) masks; only touched segments are stored."""

    def __init__(self, idx=None, mask=None):
        self.idx = np.zeros(0, dtype=np.int64) if idx is None else idx
        self.mask = np.zeros((0,) + _SHAPE, dtype=bool) if mask is None else mask

    def apply(self, indices: np.ndarray, window: np.ndarray, value: bool) -> None:
        indices = np.asarray(indices, dtype=np.int64)
        if indices.size == 0 or not window.any():
            return
        union = np.union1d(self.idx, indices)
        new = np.zeros((union.size,) + _SHAPE, dtype=bool)
        new[np.searchsorted(union, self.idx)] = self.mask
        rows = np.searchsorted(union, indices)
        sub = new[rows]
        sub[:, window] = value
        new[rows] = sub
        self.idx, self.mask = union, new


def osm_closures(table: SegmentTable, group: VehicleGroup):
    """Closures from OSM tags for one group, plus a parse report."""
    report = {"unparsed_conditional": 0, "examples": []}
    cache = {}

    def count_bad(si):
        report["unparsed_conditional"] += 1
        if len(report["examples"]) < _MAX_EXAMPLES:
            report["examples"].append(table.strings[si])

    def parts(si):
        if si not in cache:
            try:
                cache[si] = parse_conditional(table.strings[si])
            except ConditionalParseError:
                cache[si] = None
        parsed = cache[si]
        if parsed is None:
            count_bad(si)
        return parsed or []

    masks = {}                                         # way index -> (3, 48) bool
    heavy = group.kind != "pickup"
    cand = np.zeros(len(table.way_id), dtype=bool)
    with np.errstate(invalid="ignore"):
        if group.height_m is not None:
            cand |= table.maxheight_m < group.height_m
        if heavy:
            cand |= table.cond_hgv >= 0
            cand |= table.cond_maxweight >= 0
            cand |= table.maxweight_t < group.gross_t
            if group.gross_t > HGV_GROSS_T:
                cand |= table.hgv == _HGV_NO
    for w in np.flatnonzero(cand).tolist():
        m = np.zeros(_SHAPE, dtype=bool)
        if heavy and group.gross_t > HGV_GROSS_T:
            if table.hgv[w] == _HGV_NO:
                m[:] = True
            if table.cond_hgv[w] >= 0:
                si = int(table.cond_hgv[w])
                for value, window in parts(si):
                    word = value.strip().lower()
                    if word == "no":
                        m[window] = True
                    elif word in _HGV_OPEN:
                        m[window] = False
                    else:
                        count_bad(si)
        if heavy:
            limit = table.maxweight_t[w]
            if limit < group.gross_t:                  # NaN compares False
                m[:] = True
            if table.cond_maxweight[w] >= 0:
                si = int(table.cond_maxweight[w])
                for value, window in parts(si):
                    x = parse_tonnes(value)
                    if x is None:
                        count_bad(si)
                    elif group.gross_t > x:
                        m[window] = True
        if group.height_m is not None and table.maxheight_m[w] < group.height_m:
            m[:] = True
        if m.any():
            masks[w] = m
    if not masks:
        return _pack(np.zeros(0, dtype=np.int64), np.zeros((0,) + _SHAPE, dtype=bool)), report
    ways = np.array(sorted(masks), dtype=np.int64)
    stack = np.stack([masks[w] for w in ways.tolist()])
    seg_way = table.way.astype(np.int64)
    idx = np.flatnonzero(np.isin(seg_way, ways)).astype(np.int64)
    mask = stack[np.searchsorted(ways, seg_way[idx])]
    return _pack(idx, mask), report


def group_closures(table: SegmentTable, group: VehicleGroup, rules: list, resolver: ScopeResolver):
    """OSM, official and company closures for one group; the report adds orphans and applied rules."""
    closures, report = osm_closures(table, group)
    state = _Sparse(closures.idx, closures.mask)
    report["orphans"] = {}
    report["rules_applied"] = []
    for layer in BAN_LAYERS:
        chosen = sorted((r for r in rules if r.active and r.kind == "ban" and r.layer == layer
                         and matches(r.body.get("vehicle"), group)), key=lambda r: r.id)
        ordered = ([r for r in chosen if r.body["action"] == "close"]
                   + [r for r in chosen if r.body["action"] == "open"])
        for rule in ordered:
            body = rule.body
            result = resolver.resolve(body["scope"], body.get("exempt"))
            if result.orphans:
                report["orphans"][rule.id] = result.orphans
            state.apply(result.indices, when_mask(body.get("when")), body["action"] == "close")
            report["rules_applied"].append(rule.id)
    return _pack(state.idx, state.mask), report

