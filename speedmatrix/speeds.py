"""Base speed layer: a speed per segment for 3 day types x 48 half-hour slots, and its content version.

A source gives raw speeds (NaN = no data); speed rules (``speed_pct`` then ``speed_set``) edit them inside
their scope and time window; every value is clamped to its road class and rounded to uint8. Rows that are
constant over the day are stored once in ``base``; only the others are kept in ``dyn``. Everything is array
code, because the nationwide table has about 25M segments.
"""
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Protocol

import numpy as np

from model.osm_classes import free_flow_speed
from model.spatial import MissingRoadData

try:
    from .rules import Rule, rule_set_sha
    from .segments import EXTRA_HIGHWAYS, ROAD_CLASSES, SegmentTable
    from .timeslots import DAYTYPES, SLOTS, when_mask
except ImportError:  # pragma: no cover - flat import
    from rules import Rule, rule_set_sha
    from segments import EXTRA_HIGHWAYS, ROAD_CLASSES, SegmentTable
    from timeslots import DAYTYPES, SLOTS, when_mask

SPEED_CODE_VERSION = 1
OSRM_DEFAULT_KMH = 10  # osrm/tools/profiles/car.lua default_speed; a test keeps them equal
FERRY_KMH = 5  # osrm/tools/profiles/car.lua speeds.route.ferry; a test keeps them equal
CLASS_LIMITS_KMH = {"caotoc": (20, 120), "quoclo": (10, 100), "chinh": (5, 90), "noido": (5, 70),
                    "nho": (3, 50), "pha": (3, 30)}
SPEED_KINDS = ("speed_set", "speed_pct")
_CHUNK = 1_000_000


class ReleaseMismatch(ValueError):
    """Outputs built from different inputs are combined (spec §13): a speed layer or group view whose
    version does not match the speed rules in force, or another layer."""


class BaseSource(Protocol):
    id: str

    def speeds(self, table: SegmentTable, daytype: str) -> np.ndarray:
        """float32 (n, 48) km/h; NaN means no data."""


def way_free_flow_kmh(table: SegmentTable) -> np.ndarray:
    """float32 (ways,): the OSRM car profile's free-flow speed per way, NaN when the profile has none."""
    nw = len(table.way_id)
    hw = np.asarray(table.highway, dtype=np.int64)
    ms = np.asarray(table.maxspeed_kmh, dtype=np.float32)
    key = np.stack([hw.astype(np.float64), np.where(np.isnan(ms), -1.0, ms.astype(np.float64))], axis=1)
    uniq, inverse = np.unique(key, axis=0, return_inverse=True)
    values = np.full(len(uniq), np.nan, dtype=np.float32)
    for i, (h, m) in enumerate(uniq):
        name = table.strings[int(h)] if h >= 0 else ""
        if name == "ferry":
            values[i] = FERRY_KMH
        elif name in EXTRA_HIGHWAYS:
            values[i] = OSRM_DEFAULT_KMH
        else:
            speed, _ = free_flow_speed(name, "%g" % m if m >= 0 else "")
            if speed is not None:
                values[i] = speed
    return values[np.asarray(inverse).reshape(-1)[:nw]]


class FreeFlowSource:
    """The OSRM car profile's free-flow speed, the same in every slot and day type."""
    id = "profile_free_flow:v2"

    def speeds(self, table: SegmentTable, daytype: str) -> np.ndarray:
        per_way = way_free_flow_kmh(table)
        v = np.empty((table.n, SLOTS), dtype=np.float32)
        v[:] = per_way[np.asarray(table.way)][:, None]
        return v


@dataclass
class SpeedLayer:
    version: str
    source_id: str
    n: int
    base: dict
    dyn_idx: dict
    dyn: dict
    rule_offsets: np.ndarray
    rule_ids: list
    stats: dict = field(default_factory=dict)

    def at(self, daytype: str, slot: int) -> np.ndarray:
        out = self.base[daytype].copy()
        idx = self.dyn_idx[daytype]
        if idx.size:
            out[idx] = self.dyn[daytype][:, slot]
        return out

    def rows(self, daytype: str, idx: np.ndarray) -> np.ndarray:
        idx = np.asarray(idx, dtype=np.int64)
        out = np.repeat(self.base[daytype][idx][:, None], SLOTS, axis=1)
        di = self.dyn_idx[daytype]
        if di.size and idx.size:
            pos = np.minimum(np.searchsorted(di, idx), di.size - 1)
            hit = di[pos] == idx
            out[hit] = self.dyn[daytype][pos[hit]]
        return out


def speed_version(table: SegmentTable, source, rules: list) -> str:
    """Content version of a layer: table, source id, active speed rules, class limits, code version."""
    speed_rules = [r for r in rules if r.kind in SPEED_KINDS]
    payload = json.dumps([table.sha256, source.id, rule_set_sha(speed_rules),
                          json.dumps(CLASS_LIMITS_KMH, sort_keys=True, separators=(",", ":")),
                          SPEED_CODE_VERSION], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_layer_version(layer: SpeedLayer, table: SegmentTable, rules: list, as_of=None) -> None:
    """Raise ReleaseMismatch unless ``layer`` was built from ``table`` and exactly the speed rules in ``rules``.

    ``rules`` are the rules in force on the date the caller builds for; only the source id is taken from
    the layer itself, so a layer of another source is checked against its own source.
    """
    expected = speed_version(table, SimpleNamespace(id=layer.source_id), rules)
    if layer.version != expected:
        when = f" on {as_of.isoformat()}" if as_of is not None else ""
        raise ReleaseMismatch(f"speed layer {layer.version[:12]} was not built for the speed rules in force{when} "
                              f"(expected version {expected[:12]}); rebuild it with speeds --as-of")


def _select(rules, resolver, table, stats):
    """Active speed rules in application order with their segment indices (and orphans into stats)."""
    segment_class = table.segment_class
    chosen = []
    for kind in ("speed_pct", "speed_set"):
        for rule in sorted((r for r in rules if r.active and r.kind == kind), key=lambda r: r.id):
            res = resolver.resolve(rule.body["scope"])
            stats["orphans"][rule.id] = list(res.orphans)
            idx = np.asarray(res.indices, dtype=np.int64)
            classes = rule.body.get("classes")
            if classes and idx.size:
                codes = [ROAD_CLASSES.index(c) for c in classes]
                idx = idx[np.isin(segment_class[idx], codes)]
            chosen.append((rule, idx))
    return chosen


def _csr(chosen, n):
    if not chosen:
        return np.zeros(n + 1, dtype=np.int64), []
    seg = np.concatenate([idx for _, idx in chosen])
    rank = np.concatenate([np.full(idx.size, i, dtype=np.int64) for i, (_, idx) in enumerate(chosen)])
    order = np.lexsort((rank, seg))
    offsets = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(seg, minlength=n), out=offsets[1:])
    ids = [chosen[i][0].id for i in rank[order].tolist()]
    return offsets, ids


def build_speed_layer(table: SegmentTable, source, rules: list, resolver) -> SpeedLayer:
    n = table.n
    stats = {"clamped": {c: 0 for c in ROAD_CLASSES}, "dynamic_segments": {}, "orphans": {}}
    chosen = _select(rules, resolver, table, stats)
    segment_class = table.segment_class
    lo_table = np.array([CLASS_LIMITS_KMH[c][0] for c in ROAD_CLASSES], dtype=np.float32)
    hi_table = np.array([CLASS_LIMITS_KMH[c][1] for c in ROAD_CLASSES], dtype=np.float32)
    base, dyn_idx, dyn = {}, {}, {}
    for di, daytype in enumerate(DAYTYPES):
        v = np.ascontiguousarray(source.speeds(table, daytype), dtype=np.float32)
        holes = np.isnan(v).any(axis=1)
        if holes.any():
            rows = np.flatnonzero(holes)
            examples = [(int(table.way_id[table.way[r]]), int(table.a[r]), int(table.b[r])) for r in rows[:5]]
            raise MissingRoadData(f"no speed for {rows.size} segments on {daytype}, e.g. (way_id, a, b) {examples}")
        for rule, idx in chosen:
            slots = np.flatnonzero(when_mask(rule.body.get("when"))[di])
            if idx.size == 0 or slots.size == 0:
                continue
            cell = np.ix_(idx, slots)
            if rule.kind == "speed_pct":
                v[cell] *= np.float32(1 + rule.body["pct"] / 100)
            else:
                v[cell] = np.float32(rule.body["kmh"])
        out = np.empty((n, SLOTS), dtype=np.uint8)
        for s in range(0, n, _CHUNK):
            part = v[s:s + _CHUNK]
            cls = segment_class[s:s + _CHUNK]
            lo, hi = lo_table[cls][:, None], hi_table[cls][:, None]
            outside = (part < lo) | (part > hi)
            counts = np.bincount(cls, weights=outside.sum(axis=1), minlength=len(ROAD_CLASSES))
            for ci, c in enumerate(ROAD_CLASSES):
                stats["clamped"][c] += int(counts[ci])
            out[s:s + _CHUNK] = np.rint(np.clip(part, lo, hi)).astype(np.uint8)
        del v
        dynamic = np.flatnonzero((out != out[:, :1]).any(axis=1)).astype(np.int64)
        base[daytype] = out[:, 0].copy()
        dyn_idx[daytype] = dynamic
        dyn[daytype] = out[dynamic]
        stats["dynamic_segments"][daytype] = int(dynamic.size)
    offsets, ids = _csr(chosen, n)
    return SpeedLayer(version=speed_version(table, source, rules), source_id=source.id, n=n, base=base,
                      dyn_idx=dyn_idx, dyn=dyn, rule_offsets=offsets, rule_ids=ids, stats=stats)


def save_layer(layer: SpeedLayer, out_dir) -> None:
    out_dir = Path(out_dir)
    tmp = out_dir.with_name(out_dir.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    arrays = {"rule_offsets": layer.rule_offsets}
    for d in DAYTYPES:
        arrays[f"base_{d}"], arrays[f"dyn_idx_{d}"], arrays[f"dyn_{d}"] = layer.base[d], layer.dyn_idx[d], layer.dyn[d]
    np.savez_compressed(tmp / "layer.npz", **arrays)
    meta = {"version": layer.version, "source_id": layer.source_id, "n": layer.n, "stats": layer.stats,
            "rule_ids": layer.rule_ids}
    (tmp / "layer.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    tmp.replace(out_dir)


def load_layer(out_dir) -> SpeedLayer:
    out_dir = Path(out_dir)
    meta = json.loads((out_dir / "layer.json").read_text(encoding="utf-8"))
    with np.load(out_dir / "layer.npz") as z:
        return SpeedLayer(
            version=meta["version"], source_id=meta["source_id"], n=meta["n"],
            base={d: z[f"base_{d}"] for d in DAYTYPES}, dyn_idx={d: z[f"dyn_idx_{d}"] for d in DAYTYPES},
            dyn={d: z[f"dyn_{d}"] for d in DAYTYPES}, rule_offsets=z["rule_offsets"],
            rule_ids=meta["rule_ids"], stats=meta["stats"])
