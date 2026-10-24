"""Gates of every partition level: the arcs of the turn graph whose two segments lie in different cells.

An arc joins two directed segments the vehicle travels one after the other: a continuation ``(s, s+1)`` inside one
OSRM edge-based node (a geometry, kind 0) or a turn ``(last(u), first(v))`` between two of them (kind 1). Segments
carry their tail node's cell, so a gate's via node (the head of its source segment, the tail of its target) is the
node where the vehicle crosses the border. Per cell, the entry segments (targets of incoming gates) and exit segments
(sources of outgoing gates) are what a later step pre-computes the cell's distances between.

``gate_drift`` (Decision 19) compares the via nodes of an older release's gates with the new graph's coordinates, so
a border node that moved or vanished in the map update is counted instead of going unnoticed.
"""
from dataclasses import dataclass, fields
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

import numpy as np

try:
    from .geo import haversine_m
    from .graph import TurnGraph
    from .segments import ROAD_CLASSES
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.geo import haversine_m
    from speedmatrix.graph import TurnGraph
    from speedmatrix.segments import ROAD_CLASSES

if TYPE_CHECKING:  # pragma: no cover
    from .partition import NodeGraph, Partition

KIND_CONTINUATION = 0
KIND_TURN = 1
_FERRY = ROAD_CLASSES.index("pha")
_EXAMPLES = 20
_KEY = re.compile(r"^l(\d+)_(\w+)$")


@dataclass
class LevelGates:
    src_seg: np.ndarray        # int32 (A,), sorted by (src_seg, dst_seg)
    dst_seg: np.ndarray        # int32 (A,)
    kind: np.ndarray           # int8 (A,), 0 = continuation inside a geometry, 1 = turn
    via: np.ndarray            # int32 (A,) node index
    via_osm: np.ndarray        # int64 (A,)
    via_lon_e6: np.ndarray     # int32 (A,)
    via_lat_e6: np.ndarray     # int32 (A,)
    cell_out: np.ndarray       # uint16 (A,), cell of src_seg
    cell_in: np.ndarray        # uint16 (A,), cell of dst_seg
    entry_offsets: np.ndarray  # int64 (C+1,)
    entries: np.ndarray        # int32, unique sorted entry segments per cell
    exit_offsets: np.ndarray   # int64 (C+1,)
    exits: np.ndarray          # int32, unique sorted exit segments per cell

    @property
    def n_cells(self) -> int:
        return len(self.entry_offsets) - 1

    @property
    def n_arcs(self) -> int:
        return len(self.src_seg)


def _arcs(g: TurnGraph) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
    """(continuation sources int32, turn sources int32, turn targets int32) of every arc of the graph."""
    off = np.asarray(g.ebn_offsets, dtype=np.int64)
    s = int(off[-1])
    inner = np.ones(max(s - 1, 0), dtype=bool)
    ends = off[1:-1] - 1                                     # last segment of every EBN but the final one
    inner[ends[(ends >= 0) & (ends < s - 1)]] = False
    cont = np.flatnonzero(inner).astype(np.int32)
    del inner
    u = np.repeat(np.arange(g.n_ebn, dtype=np.int32), np.diff(np.asarray(g.turn_offsets)))
    t_src = (off[1:][u] - 1).astype(np.int32)
    del u
    t_dst = off[:-1][np.asarray(g.turn_dst)].astype(np.int32)
    return cont, t_src, t_dst


def _csr(cells: np.ndarray, segs: np.ndarray, n_cells: int, n_segs: int) -> "tuple[np.ndarray, np.ndarray]":
    """Unique sorted segments per cell as (offsets int64 (C+1,), values int32)."""
    key = np.unique(cells.astype(np.int64) * max(n_segs, 1) + segs)
    cell = key // max(n_segs, 1)
    offsets = np.zeros(n_cells + 1, dtype=np.int64)
    np.cumsum(np.bincount(cell, minlength=n_cells), out=offsets[1:])
    return offsets, (key - cell * max(n_segs, 1)).astype(np.int32)


def level_gates(g: TurnGraph, seg_labels_level: np.ndarray, n_cells: int, arcs=None) -> LevelGates:
    """The gates of one level whose segment labels (uint16 (S,), dense cell indices < ``n_cells``) are given.
    ``arcs`` is ``_arcs(g)`` when the caller already holds it."""
    lab = np.asarray(seg_labels_level)
    if lab.shape != (g.n_segments,):
        raise ValueError(f"labels have shape {lab.shape}, the graph has {g.n_segments} segments")
    if len(lab) and int(lab.max()) >= n_cells:
        raise ValueError(f"label {int(lab.max())} >= n_cells = {n_cells}")
    cont, t_src, t_dst = arcs if arcs is not None else _arcs(g)
    c = cont[lab[cont] != lab[cont + 1]]
    t = np.flatnonzero(lab[t_src] != lab[t_dst])
    src = np.concatenate([c, t_src[t]]).astype(np.int32)
    dst = np.concatenate([c + 1, t_dst[t]]).astype(np.int32)
    kind = np.concatenate([np.full(len(c), KIND_CONTINUATION, dtype=np.int8),
                           np.full(len(t), KIND_TURN, dtype=np.int8)])
    o = np.lexsort((dst, src))
    src, dst, kind = src[o], dst[o], kind[o]
    via = np.asarray(g.seg_head)[src].astype(np.int32)
    cell_out, cell_in = lab[src].astype(np.uint16), lab[dst].astype(np.uint16)
    s = g.n_segments
    entry_offsets, entries = _csr(cell_in, dst, n_cells, s)
    exit_offsets, exits = _csr(cell_out, src, n_cells, s)
    return LevelGates(src_seg=src, dst_seg=dst, kind=kind, via=via,
                      via_osm=np.asarray(g.node_osm)[via].astype(np.int64),
                      via_lon_e6=np.asarray(g.node_lon_e6)[via].astype(np.int32),
                      via_lat_e6=np.asarray(g.node_lat_e6)[via].astype(np.int32),
                      cell_out=cell_out, cell_in=cell_in, entry_offsets=entry_offsets, entries=entries,
                      exit_offsets=exit_offsets, exits=exits)


def build_gates(g: TurnGraph, p: "Partition") -> "list[LevelGates]":
    """The gates of every level of ``p`` (one ``LevelGates`` per level, coarse first)."""
    arcs = _arcs(g)
    return [level_gates(g, lab, len(cells), arcs) for lab, cells in zip(p.seg_labels, p.cells)]


def _arc_keys(lg: LevelGates, n_segs: int) -> np.ndarray:
    return lg.src_seg.astype(np.int64) * max(n_segs, 1) + lg.dst_seg


def _landings(ng: "NodeGraph") -> np.ndarray:
    """bool (N,): ferry nodes that also have a road edge (Decision 11)."""
    f = np.asarray(ng.ferry)
    fnode = np.zeros(ng.n, dtype=bool)
    fnode[ng.lo[f]] = True
    fnode[ng.hi[f]] = True
    road = np.zeros(ng.n, dtype=bool)
    road[ng.lo[~f]] = True
    road[ng.hi[~f]] = True
    return fnode & road


def check_gates(g: TurnGraph, ng: "NodeGraph", gates) -> dict:
    """Blocking checks ``{"value", "ok", "blocking"}``: ``gates_nested`` (arcs of level l missing from level l+1)
    and ``ferry_gates_at_landings`` (gate arcs touching a ferry segment whose via node is no landing)."""
    s = g.n_segments
    missing = 0
    for coarse, fine in zip(gates, gates[1:]):
        missing += int((~np.isin(_arc_keys(coarse, s), _arc_keys(fine, s))).sum())
    ferry_seg = np.asarray(g.seg_class) == _FERRY
    landing = _landings(ng)
    bad, examples = 0, []
    for lv, lg in enumerate(gates):
        hit = (ferry_seg[lg.src_seg] | ferry_seg[lg.dst_seg]) & ~landing[lg.via]
        bad += int(hit.sum())
        examples += [{"level": lv + 1, "via_osm": int(o)} for o in lg.via_osm[hit][:_EXAMPLES - len(examples)]]
    return {"gates_nested": {"value": missing, "ok": missing == 0, "blocking": True},
            "ferry_gates_at_landings": {"value": bad, "ok": bad == 0, "blocking": True, "examples": examples}}


def _spread(counts: np.ndarray) -> dict:
    if not len(counts):
        return {"p50": 0.0, "p95": 0.0, "max": 0}
    return {"p50": float(np.percentile(counts, 50)), "p95": float(np.percentile(counts, 95)),
            "max": int(counts.max())}


def gate_stats(g: TurnGraph, gates) -> dict:
    """Per level ``l{l}``: cells, gate arcs, unique via nodes, entries and exits per cell ({p50, p95, max}) and
    ``piece_cells`` = sum over cells of entries x exits (the size of the cells' entry-to-exit tables)."""
    out = {}
    for lv, lg in enumerate(gates):
        ne, nx = np.diff(lg.entry_offsets), np.diff(lg.exit_offsets)
        out[f"l{lv + 1}"] = {"cells": lg.n_cells, "gate_arcs": lg.n_arcs, "via_nodes": int(len(np.unique(lg.via))),
                             "entries_per_cell": _spread(ne), "exits_per_cell": _spread(nx),
                             "piece_cells": int((ne * nx).sum())}
    return out


def gate_drift(old: "list[LevelGates]", g: TurnGraph, levels=(0, 1), tol_m: float = 5.0) -> dict:
    """Decision 19: every unique via node of ``old`` (an earlier release's gates) at ``levels`` is looked up by OSM id
    in ``g``; per level ``l{l}``: ``checked``, ``moved`` (more than ``tol_m`` from its old position), ``gone`` (no
    longer in the graph) and up to 20 ``examples`` {osm, old, new, metres} ([lon, lat] in degrees; ``new`` and
    ``metres`` are None for a gone node)."""
    node_osm = np.asarray(g.node_osm)
    out = {}
    for lv in levels:
        if lv >= len(old):
            raise ValueError(f"old gates have {len(old)} levels, level {lv + 1} asked for")
        lg = old[lv]
        osm, first = np.unique(lg.via_osm, return_index=True)
        olon, olat = lg.via_lon_e6[first], lg.via_lat_e6[first]
        pos = np.minimum(np.searchsorted(node_osm, osm), max(len(node_osm) - 1, 0))
        found = node_osm[pos] == osm if len(node_osm) else np.zeros(len(osm), dtype=bool)
        nlon = np.asarray(g.node_lon_e6)[pos[found]]
        nlat = np.asarray(g.node_lat_e6)[pos[found]]
        metres = np.full(len(osm), np.nan)
        metres[found] = haversine_m(olat[found] / 1e6, olon[found] / 1e6, nlat / 1e6, nlon / 1e6)
        moved = found & (metres > tol_m)
        gone = ~found
        new_of = dict(zip(np.flatnonzero(found).tolist(), zip(nlon.tolist(), nlat.tolist())))
        examples = []
        for i in np.flatnonzero(moved | gone)[:_EXAMPLES].tolist():
            new = new_of.get(i)
            examples.append({"osm": int(osm[i]), "old": [int(olon[i]) / 1e6, int(olat[i]) / 1e6],
                             "new": [new[0] / 1e6, new[1] / 1e6] if new else None,
                             "metres": round(float(metres[i]), 2) if new else None})
        out[f"l{lv + 1}"] = {"checked": int(len(osm)), "moved": int(moved.sum()), "gone": int(gone.sum()),
                             "examples": examples}
    return out


def save_gates(gates, path) -> None:
    """All levels in one ``gates.npz`` (keys ``l{l}_<field>``), written to a temp file and renamed into place."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays = {f"l{lv + 1}_{f.name}": np.ascontiguousarray(getattr(lg, f.name))
              for lv, lg in enumerate(gates) for f in fields(LevelGates)}
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        np.savez(fh, **arrays)
    os.replace(tmp, path)


def load_gates(path) -> "list[LevelGates]":
    names = [f.name for f in fields(LevelGates)]
    with np.load(path) as z:
        levels = {}
        for key in z.files:
            m = _KEY.match(key)
            if not m or m.group(2) not in names:
                raise ValueError(f"{path}: unexpected array {key!r}")
            levels.setdefault(int(m.group(1)), {})[m.group(2)] = z[key]
    if sorted(levels) != list(range(1, len(levels) + 1)):
        raise ValueError(f"{path}: levels {sorted(levels)} are not 1..{len(levels)}")
    out = []
    for lv in range(1, len(levels) + 1):
        missing = [n for n in names if n not in levels[lv]]
        if missing:
            raise ValueError(f"{path}: level {lv} lacks {missing}")
        out.append(LevelGates(**levels[lv]))
    return out
