"""Partition labels of a release: carry-over by OSM node id, the partition rules, and the level-3 re-split.

``python -m speedmatrix.partition import-seed --scratch DIR --out FILE``
``python -m speedmatrix.partition run --release DIR [--previous DIR | --seed FILE] [--polygons FILE] [--rivers FILE]
[--workers N]``

Levels 1 and 2 are frozen: their labels follow the nodes by OSM id from the previous release (the v3.2 seed the first
time), and only new nodes get labels, by the v3.2 polygons (level 1) and by network propagation (level 2). Level 3
and deeper are re-split, but only inside the level-2 cells whose network hash changed. Every partition rule
(expressway, ferry, contiguity, nesting) is code re-run on every map, in the order of ``build_partition``.

Each level is stored per segment as uint16 dense indices (``labels_l{l}.npy``); ``partition.json`` maps the dense
indices to stable int64 cell codes.
"""
import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started as a script
    sys.path.insert(0, str(_REPO))

try:
    from .geo import points_in_geometry
    from .graph import TurnGraph, load_graph
    from .mincut import (EXPRESSWAY, PER_WORKER_GB_L2, PER_WORKER_GB_L3, CellGraph, cell_hash, expressway_rule,
                         merge_fragments, split_jobs, split_k)
    from .segments import ROAD_CLASSES
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.geo import points_in_geometry
    from speedmatrix.graph import TurnGraph, load_graph
    from speedmatrix.mincut import (EXPRESSWAY, PER_WORKER_GB_L2, PER_WORKER_GB_L3, CellGraph, cell_hash,
                                    expressway_rule, merge_fragments, split_jobs, split_k)
    from speedmatrix.segments import ROAD_CLASSES
from model.clusters import _RiverIndex  # noqa: E402

PARTITION_FORMAT = 1
MAX_CELLS = 65535
DEFAULT_POLYGONS = _REPO / "data" / "partition" / "regions_l1_v3.2.geojson"
DEFAULT_RIVERS = _REPO / "osrm" / "data" / "main_rivers.geojson"
SEED_FILES = ("node_region_v3.2.npz", "node_region_h32all.npz", "labels_l3.npy")
MIN_LENGTH_M = 1e-3   # propagation edge length floor, so a zero-length edge is still an edge
_FERRY = ROAD_CLASSES.index("pha")
_CELL_KEY = 1_000_000  # river grid cell key: x * _CELL_KEY + y (|y| < 4500 for 0.02-degree cells)


class PartitionError(ValueError):
    """The partition cannot be built or saved as the rules require."""


@dataclass(frozen=True)
class PartitionConfig:
    frozen: int = 2                     # levels carried over by node id (L1, L2)
    l2_nodes: int = 25000               # fresh L2 split (no previous): k = max(l2_min_k, round(nodes / l2_nodes))
    l2_min_k: int = 4
    l2_band_km: float = 3.0
    split_nodes: tuple = (1800,)        # one entry per re-split level (L3, L4, ...): nodes per cell of a fresh split
    split_band_km: tuple = (1.0,)       # refinement band per re-split level; the last value serves deeper levels
    fresh_frac: float = 0.2

    @property
    def k(self) -> int:
        return self.frozen + len(self.split_nodes)


@dataclass
class NodeGraph:
    """Undirected node-level view of a TurnGraph: node i here is node i of ``g.node_osm``."""
    osm: np.ndarray          # int64 (N,)
    lon: np.ndarray          # float64 (N,)
    lat: np.ndarray          # float64 (N,)
    lo: np.ndarray           # int32 (M,), lo < hi, one row per node pair
    hi: np.ndarray           # int32 (M,)
    highway: np.ndarray      # int16 (M,), codes into highway_names
    length_m: np.ndarray     # float32 (M,)
    river: np.ndarray        # bool (M,), crosses a main river
    ferry: np.ndarray        # bool (M,), road class "pha"
    expressway: np.ndarray   # bool (M,), motorway or motorway_link
    component: np.ndarray    # int32 (N,), weak component of the whole graph
    highway_names: list      # the graph's meta["highways"]

    @property
    def n(self) -> int:
        return len(self.osm)

    @property
    def m(self) -> int:
        return len(self.lo)


@dataclass
class Previous:
    ids: np.ndarray          # int64 (n,), sorted, unique OSM node ids
    codes: np.ndarray        # int64 (n, k'), -1 where a node has no label
    l2_hash: "dict | None"   # {L2 code: cell_hash}; None for the v3.2 seed
    source: str


@dataclass
class Partition:
    codes: np.ndarray        # int64 (N, k), stable cell code per node and level
    cells: list              # per level, int64 codes in dense-index order (ascending)
    seg_labels: list         # per level, uint16 (S,) dense index of each segment's tail node
    l2_hash: dict            # {L2 code: cell_hash}
    info: dict
    ids: np.ndarray          # int64 (N,), OSM id of each node (NodeGraph.osm)


# ---- node graph ------------------------------------------------------------------------------------------------------

def _river_edges(lon, lat, lo, hi, rivers) -> np.ndarray:
    """Edges crossing a main river; only edges that leave their grid cell or touch a river cell are tested."""
    features = (rivers or {}).get("features") or []
    out = np.zeros(len(lo), dtype=bool)
    if not features or not len(lo):
        return out
    index = _RiverIndex(features)
    size = index.cell
    ax, ay = np.floor(lon[lo] / size).astype(np.int64), np.floor(lat[lo] / size).astype(np.int64)
    bx, by = np.floor(lon[hi] / size).astype(np.int64), np.floor(lat[hi] / size).astype(np.int64)
    river_cells = np.array(sorted(x * _CELL_KEY + y for x, y in index.grid), dtype=np.int64)
    cand = (ax != bx) | (ay != by)
    cand |= np.isin(ax * _CELL_KEY + ay, river_cells) | np.isin(bx * _CELL_KEY + by, river_cells)
    for e in np.flatnonzero(cand):
        a, b = int(lo[e]), int(hi[e])
        if index.crosses((float(lon[a]), float(lat[a])), (float(lon[b]), float(lat[b]))):
            out[e] = True
    return out


def node_graph(g: TurnGraph, rivers: dict) -> NodeGraph:
    """The undirected node pairs of the graph's directed segments.

    A pair travelled by several segments (both directions, or two ways) takes highway, length and ferry flag from
    its lowest segment index. ``river`` marks pairs crossing a feature of ``rivers`` (a main-rivers GeoJSON
    document); ``component`` is the weak component of every node.
    """
    n = g.n_nodes
    tail = np.asarray(g.seg_tail, dtype=np.int64)
    head = np.asarray(g.seg_head, dtype=np.int64)
    idx = np.flatnonzero(tail != head)
    key = np.minimum(tail[idx], head[idx]) * n + np.maximum(tail[idx], head[idx])
    del tail, head
    uk, first = np.unique(key, return_index=True)
    seg = idx[first]
    del key, idx, first
    lo, hi = (uk // max(n, 1)).astype(np.int32), (uk % max(n, 1)).astype(np.int32)
    names = list(g.meta["highways"])
    highway = np.asarray(g.seg_highway)[seg].astype(np.int16)
    express_of = np.array([h in EXPRESSWAY for h in names] or [False], dtype=bool)
    lon = np.asarray(g.node_lon_e6) / 1e6
    lat = np.asarray(g.node_lat_e6) / 1e6
    a = coo_matrix((np.ones(len(lo), dtype=np.int8), (lo, hi)), shape=(n, n))
    component = connected_components(a, directed=False)[1].astype(np.int32)
    return NodeGraph(osm=np.asarray(g.node_osm, dtype=np.int64).copy(), lon=lon, lat=lat, lo=lo, hi=hi,
                     highway=highway, length_m=np.asarray(g.seg_len_m)[seg].astype(np.float32),
                     river=_river_edges(lon, lat, lo, hi, rivers), ferry=np.asarray(g.seg_class)[seg] == _FERRY,
                     expressway=express_of[highway.astype(np.int64)] if len(seg) else np.zeros(0, dtype=bool),
                     component=component, highway_names=names)


def cell_graph(ng: NodeGraph, mask: "np.ndarray | None" = None) -> CellGraph:
    """The CellGraph of the nodes in ``mask`` (all when None) and the edges between them; ``node`` holds the
    NodeGraph indices."""
    if mask is None:
        return CellGraph.from_edges(ng.osm, ng.lon, ng.lat, ng.lo, ng.hi, ng.highway, ng.highway_names, ng.river,
                                    ng.length_m)
    mask = np.asarray(mask, dtype=bool)
    if mask.shape != (ng.n,):
        raise ValueError(f"mask has shape {mask.shape}, expected ({ng.n},)")
    nodes = np.flatnonzero(mask)
    local = np.full(ng.n, -1, dtype=np.int64)
    local[nodes] = np.arange(len(nodes))
    e = np.flatnonzero(mask[ng.lo] & mask[ng.hi])
    return CellGraph.from_edges(ng.osm[nodes], ng.lon[nodes], ng.lat[nodes], local[ng.lo[e]], local[ng.hi[e]],
                                ng.highway[e], ng.highway_names, ng.river[e], ng.length_m[e], node=nodes)


class _Cells:
    """The nodes and inner edges of every cell (label >= 0) of one labelling, grouped once in compact int32 index
    arrays; a cell's CellGraph is built only on demand (``graph``), so callers never hold all cells' graphs."""

    def __init__(self, ng: NodeGraph, labels: np.ndarray):
        labels = np.asarray(labels, dtype=np.int64)
        self.ng = ng
        order = np.argsort(labels, kind="stable")
        sl = labels[order]
        first = np.searchsorted(sl, sl, "left")
        self.local = np.empty(ng.n, dtype=np.int32)
        self.local[order] = (np.arange(ng.n) - first).astype(np.int32)
        del first
        self.codes = np.unique(sl[sl >= 0])
        self._ns = np.searchsorted(sl, self.codes, "left")
        self._ne = np.searchsorted(sl, self.codes, "right")
        self.order = order.astype(np.int32)
        del order, sl
        inner = (labels[ng.lo] == labels[ng.hi]) & (labels[ng.lo] >= 0)
        e = np.flatnonzero(inner)
        el = labels[ng.lo[e]]
        eo = np.argsort(el, kind="stable")
        e, el = e[eo], el[eo]
        self._es = np.searchsorted(el, self.codes, "left")
        self._ee = np.searchsorted(el, self.codes, "right")
        self.edges_sorted = e.astype(np.int32)
        deg = np.bincount(ng.lo[e], minlength=ng.n) + np.bincount(ng.hi[e], minlength=ng.n)
        del e, el, eo, inner
        junction = (deg >= 3).astype(np.int64)
        valid = labels >= 0
        self.junctions = np.bincount(np.searchsorted(self.codes, labels[valid]), weights=junction[valid],
                                     minlength=len(self.codes)).astype(np.int64)

    def _at(self, code) -> int:
        i = int(np.searchsorted(self.codes, code))
        if i >= len(self.codes) or self.codes[i] != code:
            raise KeyError(code)
        return i

    def nodes(self, code) -> np.ndarray:
        i = self._at(code)
        return self.order[self._ns[i]:self._ne[i]].astype(np.int64)

    def size(self, code) -> int:
        i = self._at(code)
        return int(self._ne[i] - self._ns[i])

    def junction_count(self, code) -> int:
        return int(self.junctions[self._at(code)])

    def graph(self, code) -> CellGraph:
        i = self._at(code)
        ng = self.ng
        nodes = self.order[self._ns[i]:self._ne[i]].astype(np.int64)
        ed = self.edges_sorted[self._es[i]:self._ee[i]]
        return CellGraph.from_edges(ng.osm[nodes], ng.lon[nodes], ng.lat[nodes], self.local[ng.lo[ed]],
                                    self.local[ng.hi[ed]], ng.highway[ed], ng.highway_names, ng.river[ed],
                                    ng.length_m[ed], node=nodes)


def _unique_rows(*cols) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
    """``np.unique(np.stack(cols, axis=1), axis=0, return_inverse=True, return_counts=True)`` by lexsort, without
    the stacked copy and the structured-dtype sort: (table (u, len(cols)), inverse (n,), counts (u,))."""
    n = len(cols[0])
    if not n:
        return np.zeros((0, len(cols)), dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    o = np.lexsort(cols[::-1])
    flag = np.zeros(n, dtype=bool)
    flag[0] = True
    for c in cols:
        s = c[o]
        flag[1:] |= s[1:] != s[:-1]
    starts = np.flatnonzero(flag)
    inv = np.empty(n, dtype=np.int64)
    inv[o] = np.cumsum(flag) - 1
    table = np.stack([np.asarray(c)[o[starts]] for c in cols], axis=1).astype(np.int64)
    return table, inv, np.diff(np.r_[starts, n])


# ---- seed and previous release ---------------------------------------------------------------------------------------

def _check_previous(prev: Previous) -> Previous:
    ids = np.asarray(prev.ids, dtype=np.int64)
    codes = np.asarray(prev.codes, dtype=np.int64)
    if codes.ndim != 2 or codes.shape[0] != len(ids):
        raise PartitionError(f"{prev.source}: codes shape {codes.shape} does not match {len(ids)} ids")
    if len(ids) > 1 and not (np.diff(ids) > 0).all():
        raise PartitionError(f"{prev.source}: ids are not sorted and unique")
    return replace(prev, ids=ids, codes=codes)


def _atomic(path: Path, write) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        write(f)
    os.replace(tmp, path)


def _sha256_file(path) -> str:
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def import_seed(scratch: Path, out: Path) -> dict:
    """The v3.2 seed tables (``SEED_FILES`` in ``scratch``) as one ``ids``/``codes`` npz at ``out``.

    L1 is the int8 region of ``node_region_v3.2.npz``, L2 the ``100000 + 1000 R + s`` code of
    ``node_region_h32all.npz``, L3 the codes of ``labels_l3.npy``, all aligned with the same ``ids``. Negative ids
    (barrier copies) are dropped; -1 labels are kept. Returns the counts.
    """
    scratch, out = Path(scratch), Path(out)
    with np.load(scratch / SEED_FILES[0]) as z1, np.load(scratch / SEED_FILES[1]) as z2:
        ids, l1 = z1["ids"].astype(np.int64), z1["region"].astype(np.int64)
        same = np.array_equal(ids, z2["ids"])
        l2 = z2["region"].astype(np.int64)
    l3 = np.load(scratch / SEED_FILES[2]).astype(np.int64)
    if not same or not (len(ids) == len(l1) == len(l2) == len(l3)):
        raise PartitionError("seed tables are not aligned on the same ids")
    keep = ids >= 0
    order = np.argsort(ids[keep], kind="stable")
    kept = ids[keep][order]
    if len(kept) > 1 and not (np.diff(kept) > 0).all():
        raise PartitionError("seed ids are not unique")
    codes = np.stack([l1[keep][order], l2[keep][order], l3[keep][order]], axis=1)
    del l1, l2, l3
    _atomic(out, lambda f: np.savez(f, ids=kept, codes=codes))
    info = {"ids_read": int(len(ids)), "negative_dropped": int((~keep).sum()), "nodes": int(len(kept))}
    for level in range(3):
        c = codes[:, level]
        info[f"l{level + 1}_codes"] = int(len(np.unique(c[c >= 0])))
        info[f"l{level + 1}_unlabelled"] = int((c < 0).sum())
    info["sha256"] = {name: _sha256_file(scratch / name) for name in SEED_FILES}
    return info


def load_seed(path) -> Previous:
    with np.load(path) as z:
        return _check_previous(Previous(z["ids"], z["codes"], None, f"seed {path}"))


def load_previous(release_dir) -> Previous:
    """Labels and L2 hashes of a release written by ``save_partition``."""
    release_dir = Path(release_dir)
    doc = json.loads((release_dir / "partition.json").read_text(encoding="utf-8"))
    with np.load(release_dir / "node_labels.npz") as z:
        ids, codes = z["ids"], z["codes"]
    hashes = {int(c): str(h) for c, h in (doc.get("l2_hash") or {}).items()}
    return _check_previous(Previous(ids, codes, hashes, f"release {release_dir}"))


# ---- rule helpers ----------------------------------------------------------------------------------------------------

def _pieces(n, lo, hi, labels) -> np.ndarray:
    """Component id per node over the edges whose two ends share a label >= 0."""
    same = (labels[lo] >= 0) & (labels[lo] == labels[hi])
    a = coo_matrix((np.ones(int(same.sum()), dtype=np.int8), (lo[same], hi[same])), shape=(n, n))
    return connected_components(a, directed=False)[1]


def _expressway_only(ng: NodeGraph) -> np.ndarray:
    ex = ng.expressway
    deg_ex = np.bincount(ng.lo[ex], minlength=ng.n) + np.bincount(ng.hi[ex], minlength=ng.n)
    deg_other = np.bincount(ng.lo[~ex], minlength=ng.n) + np.bincount(ng.hi[~ex], minlength=ng.n)
    return (deg_ex > 0) & (deg_other == 0)


def _keys(labels, parent, component) -> "tuple[np.ndarray, np.ndarray]":
    """Composite key per node of (component, parent, label) (-1 when unlabelled) and the key table."""
    valid = labels >= 0
    key = np.full(len(labels), -1, dtype=np.int64)
    if not valid.any():
        return key, np.zeros((0, 3), dtype=np.int64)
    table, inv, _ = _unique_rows(component[valid].astype(np.int64), parent[valid], labels[valid])
    key[valid] = inv
    return key, table


def _exempt(n, lo, hi, key, xonly) -> np.ndarray:
    """Ruling T6-c: pieces made only of expressway-only nodes whose key also has a piece with an ordinary node."""
    valid = key >= 0
    pc = _pieces(n, lo, hi, key)
    ordinary_piece = np.zeros(n, dtype=bool)
    ordinary_piece[pc[valid & ~xonly]] = True
    has_ordinary = np.zeros(int(key.max()) + 2, dtype=bool)
    has_ordinary[key[valid & ~xonly]] = True
    return valid & xonly & ~ordinary_piece[pc] & has_ordinary[np.maximum(key, 0)]


def _fragments(n, lo, hi, key, exempt) -> int:
    """Pieces beyond one per key, not counting exempt pieces."""
    k2 = np.where(exempt, -1, key)
    valid = k2 >= 0
    pc = _pieces(n, lo, hi, k2)
    return int(len(np.unique(pc[valid])) - len(np.unique(k2[valid])))


def _contiguity(cg: CellGraph, labels, parent, component, xonly) -> "tuple[np.ndarray, dict]":
    """Decision 8: in each weak component, every piece of a cell but its largest joins the sibling cell (same parent)
    it shares the most edges with (``merge_fragments``). Detached expressway-only pieces are exempt (Ruling T6-c);
    a piece with no sibling neighbour stays and is counted as ``stranded``."""
    labels = np.asarray(labels, dtype=np.int64).copy()
    key, table = _keys(labels, parent, component)
    info = {"pieces_moved": 0, "nodes_moved": 0, "expressway_detached": 0, "expressway_detached_nodes": 0,
            "stranded": 0}
    if not len(table):
        return labels, info
    sibling = parent[cg.lo] == parent[cg.hi]
    sub = replace(cg, lo=cg.lo[sibling], hi=cg.hi[sibling], cap=cg.cap[sibling], length_m=cg.length_m[sibling],
                  expressway=cg.expressway[sibling])
    exempt = _exempt(cg.n, sub.lo, sub.hi, key, xonly)
    k2 = np.where(exempt, -1, key)
    merged, moved = merge_fragments(sub, k2)
    m = k2 >= 0
    new = labels.copy()
    new[m] = table[merged[m], 2]
    pc = _pieces(cg.n, sub.lo, sub.hi, key)
    info.update({"pieces_moved": int(moved), "nodes_moved": int((new != labels).sum()),
                 "expressway_detached": int(len(np.unique(pc[exempt]))),
                 "expressway_detached_nodes": int(exempt.sum()),
                 "stranded": _fragments(cg.n, sub.lo, sub.hi, np.where(m, merged, -1), exempt)})
    return new, info


def _codes_by_min_node(part, next_code) -> "tuple[np.ndarray, int]":
    """New codes for the parts of ``part`` (int64 (n,), -1 = none): ``next_code`` onwards, in order of each part's
    lowest node index (= smallest OSM id, the ids being sorted)."""
    out = np.full(len(part), -1, dtype=np.int64)
    nodes = np.flatnonzero(part >= 0)
    if not len(nodes):
        return out, next_code
    parts, inv = np.unique(part[nodes], return_inverse=True)
    inv = np.asarray(inv).reshape(-1)
    first = np.full(len(parts), len(part), dtype=np.int64)
    np.minimum.at(first, inv, nodes)
    rank = np.empty(len(parts), dtype=np.int64)
    rank[np.argsort(first, kind="stable")] = np.arange(len(parts))
    out[nodes] = next_code + rank[inv]
    return out, next_code + len(parts)


def _nest(labels, parent, next_code) -> "tuple[np.ndarray, int, dict]":
    """Decision 9: a code over several parents keeps its largest part (ties: lowest parent code); the other parts get
    new codes (``_codes_by_min_node``)."""
    labels = np.asarray(labels, dtype=np.int64).copy()
    valid = np.flatnonzero(labels >= 0)
    info = {"cells_split": 0, "parts_relabelled": 0, "nodes_relabelled": 0}
    if not len(valid):
        return labels, next_code, info
    table, inv, cnt = _unique_rows(labels[valid], parent[valid])
    o = np.lexsort((table[:, 1], -cnt, table[:, 0]))
    best = np.zeros(len(table), dtype=bool)
    best[o[np.r_[True, table[o][1:, 0] != table[o][:-1, 0]]]] = True
    if best.all():
        return labels, next_code, info
    part = np.full(len(labels), -1, dtype=np.int64)
    move = ~best[inv]
    part[valid[move]] = inv[move]
    new, next_code = _codes_by_min_node(part, next_code)
    labels[valid[move]] = new[valid[move]]
    info.update({"cells_split": int(len(np.unique(table[~best, 0]))), "parts_relabelled": int((~best).sum()),
                 "nodes_relabelled": int(move.sum())})
    return labels, next_code, info


def _network_fill(ng: NodeGraph, labels, edge_ok) -> "tuple[np.ndarray, int]":
    """Unlabelled nodes take the label of the nearest labelled node by length over the ``edge_ok`` edges."""
    labels = np.asarray(labels, dtype=np.int64).copy()
    known = np.flatnonzero(labels >= 0)
    todo = labels < 0
    if not len(known) or not todo.any():
        return labels, 0
    e = np.flatnonzero(edge_ok)
    w = np.maximum(ng.length_m[e].astype(np.float64), MIN_LENGTH_M)
    graph = csr_matrix((w, (ng.lo[e], ng.hi[e])), shape=(ng.n, ng.n))
    _, _, src = dijkstra(graph, directed=False, indices=known, min_only=True, return_predecessors=True)
    hit = np.flatnonzero(todo & (src >= 0))
    labels[hit] = labels[src[hit]]
    return labels, int(len(hit))


def _nearest_fill(xy, labels, group) -> "tuple[np.ndarray, int]":
    """Unlabelled nodes take the label of the Euclidean-nearest labelled node of the same group; a group without any
    labelled node is left unlabelled."""
    labels = np.asarray(labels, dtype=np.int64).copy()
    todo = np.flatnonzero(labels < 0)
    cand = np.flatnonzero(labels >= 0)
    if not len(todo) or not len(cand):
        return labels, 0
    todo = todo[np.argsort(group[todo], kind="stable")]
    cand = cand[np.argsort(group[cand], kind="stable")]
    tg, cgp = group[todo], group[cand]
    filled = 0
    for grp in np.unique(tg):
        q = todo[np.searchsorted(tg, grp, "left"):np.searchsorted(tg, grp, "right")]
        c = cand[np.searchsorted(cgp, grp, "left"):np.searchsorted(cgp, grp, "right")]
        if len(c):
            labels[q] = labels[c[cKDTree(xy[c]).query(xy[q])[1]]]
            filled += len(q)
    return labels, filled


def _rings(geometry):
    kind = geometry.get("type")
    polygons = [geometry["coordinates"]] if kind == "Polygon" else geometry.get("coordinates", [])
    for rings in polygons:
        for ring in rings:
            yield np.asarray(ring, dtype=np.float64)


def _polygon_l1(ng: NodeGraph, todo: np.ndarray, polygons: dict) -> "tuple[np.ndarray, int]":
    """Region code (``properties.region``) of each node in ``todo`` by polygon; -1 outside every polygon. A node in
    several polygons takes the lowest region (counted)."""
    features = (polygons or {}).get("features") or []
    found = []
    for f in features:
        region = (f.get("properties") or {}).get("region")
        if isinstance(region, bool) or not isinstance(region, int) or region < 0:
            raise PartitionError(f"polygon feature without an integer region >= 0: {region!r}")
        found.append((region, f["geometry"]))
    out = np.full(len(todo), -1, dtype=np.int64)
    overlaps = 0
    lon, lat = ng.lon[todo], ng.lat[todo]
    for region, geometry in sorted(found, key=lambda t: t[0]):
        pts = np.concatenate(list(_rings(geometry)) or [np.zeros((0, 2))])
        if not len(pts):
            continue
        cand = np.flatnonzero((lon >= pts[:, 0].min()) & (lon <= pts[:, 0].max()) & (lat >= pts[:, 1].min())
                              & (lat <= pts[:, 1].max()))
        inside = cand[points_in_geometry(lat[cand], lon[cand], geometry)]
        overlaps += int((out[inside] >= 0).sum())
        inside = inside[out[inside] < 0]
        out[inside] = region
    return out, overlaps


def _ferry_units(ng: NodeGraph):
    """Decision 11: (members, their home landing, info). In each connected ferry component the home landing is the
    landing (a ferry node with road edges) with the most road edges, ties to the lowest OSM id; every other node
    of the component is a member."""
    f = np.flatnonzero(ng.ferry)
    info = {"units": 0, "units_without_landing": 0, "members": 0}
    if not len(f):
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), info
    n = ng.n
    a = coo_matrix((np.ones(len(f), dtype=np.int8), (ng.lo[f], ng.hi[f])), shape=(n, n))
    comp = connected_components(a, directed=False)[1]
    fnode = np.zeros(n, dtype=bool)
    fnode[ng.lo[f]] = True
    fnode[ng.hi[f]] = True
    road = ~ng.ferry
    road_deg = np.bincount(ng.lo[road], minlength=n) + np.bincount(ng.hi[road], minlength=n)
    landings = np.flatnonzero(fnode & (road_deg > 0))
    o = np.lexsort((landings, -road_deg[landings], comp[landings]))
    ls = landings[o]
    homes = ls[np.r_[True, comp[ls][1:] != comp[ls][:-1]]] if len(ls) else ls
    home_of = np.full(n, -1, dtype=np.int64)
    home_of[comp[homes]] = homes
    nodes = np.flatnonzero(fnode)
    h = home_of[comp[nodes]]
    members = nodes[(h >= 0) & (nodes != h)]
    units = int(len(np.unique(comp[nodes])))
    info.update({"units": units, "units_without_landing": units - int(len(homes)), "members": int(len(members))})
    return members, home_of[comp[members]], info


def _apply_ferry(codes, members, homes, a, b) -> int:
    """Members take their home landing's codes at levels a..b-1; returns the members whose codes changed."""
    if not len(members):
        return 0
    before = codes[members, a:b].copy()
    codes[members, a:b] = codes[homes, a:b]
    return int((codes[members, a:b] != before).any(axis=1).sum())


def _max_code(*arrays) -> int:
    best = -1
    for x in arrays:
        x = np.asarray(x)
        if x.size:
            best = max(best, int(x.max()))
    return best


# ---- build -----------------------------------------------------------------------------------------------------------

def _config_json(config: PartitionConfig) -> dict:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(config).items()}


def build_partition(g: TurnGraph, ng: NodeGraph, previous: "Previous | None", polygons: dict,
                    config: PartitionConfig, workers: "int | None") -> Partition:
    """Labels of every node and segment at every level; the steps run in the order of the brief (Task 7):

    1. carry-over by OSM id from ``previous`` (-1 for new nodes);
    2. new L1 labels by polygon, then network propagation, then the Euclidean nearest labelled node;
    3. new L2 labels by network propagation inside the L1 cell, then the Euclidean nearest within it, then one new
       code per L1 cell left without any; with no previous at all, a fresh ``split_cell`` per L1 cell instead;
    4. the expressway rule (Decision 13) for new expressway-only nodes, levels 1-2 (``_expressway_new``);
    5. the ferry rule (Decision 11), levels 1-2;
    6. L1 contiguity; 7. L2 nesting (split-then-merge); 8. L2 contiguity among siblings;
    9. ``cell_hash`` per L2 cell;
    10. levels 3 and deeper: unchanged L2 cells keep their codes, changed ones are warm- or fresh-split, then the
        ferry rule and contiguity run at that level before the next one;
    11. nesting is re-checked at every level (a violation is a bug and raises);
    12. dense indices per level (more than ``MAX_CELLS`` raises), segments take their tail node's labels.
    """
    k = config.k
    if config.frozen != 2:
        raise PartitionError(f"frozen levels must be 2 (L1 and L2), not {config.frozen}")
    if config.split_nodes and not config.split_band_km:
        raise PartitionError("split_band_km needs at least one value")
    n = ng.n
    if n != g.n_nodes:
        raise PartitionError(f"node graph has {n} nodes, the turn graph {g.n_nodes}")
    if not n:
        raise PartitionError("empty graph")
    info = {"k": k, "config": _config_json(config), "previous": previous.source if previous else None,
            "nodes": n, "segments": int(g.n_segments), "rules": {}}
    rules = info["rules"]
    cg = cell_graph(ng)
    xy = np.c_[cg.x_km, cg.y_km]
    xonly = _expressway_only(ng)
    all_edges = np.ones(ng.m, dtype=bool)

    # 1. carry-over
    codes = np.full((n, k), -1, dtype=np.int64)
    kk = 0
    hit = np.zeros(n, dtype=bool)
    if previous is not None:
        previous = _check_previous(previous)
        kk = min(k, previous.codes.shape[1])
        if len(previous.ids):
            pos = np.minimum(np.searchsorted(previous.ids, ng.osm), len(previous.ids) - 1)
            hit = previous.ids[pos] == ng.osm
            codes[hit, :kk] = previous.codes[pos[hit], :kk]
            del pos
    carried = codes.copy()
    new = carried[:, 0] < 0       # nodes to label: not in previous, or in it without a label (seed)
    info["carry_over"] = {"carried": int((~new).sum()), "carried_unlabelled": int((hit & new).sum()),
                          "new": int((~hit).sum())}
    nxt = [_max_code(carried[:, lv], previous.codes[:, lv] if lv < kk else ()) + 1 for lv in range(k)]

    # 2. L1
    l1 = codes[:, 0]
    todo = np.flatnonzero(l1 < 0)
    poly, overlaps = _polygon_l1(ng, todo, polygons) if len(todo) else (np.zeros(0, dtype=np.int64), 0)
    l1[todo] = poly
    l1, net1 = _network_fill(ng, l1, all_edges)
    l1, near1 = _nearest_fill(xy, l1, np.zeros(n, dtype=np.int64))
    if (l1 < 0).any():
        raise PartitionError("no node has an L1 label: no previous labels and no node inside a polygon")
    codes[:, 0] = l1
    info["l1"] = {"carried": int((carried[:, 0] >= 0).sum()), "polygon": int((poly >= 0).sum()),
                  "polygon_overlaps": overlaps, "network": net1, "nearest": near1}

    # 3. L2
    if previous is None:
        codes[:, 1], nxt[1], info["l2"] = _fresh_l2(ng, codes[:, 0], config, workers, nxt[1])
    else:
        l2 = codes[:, 1]
        same_l1 = codes[ng.lo, 0] == codes[ng.hi, 0]
        l2, net2 = _network_fill(ng, l2, same_l1)
        l2, near2 = _nearest_fill(xy, l2, codes[:, 0])
        left = l2 < 0
        fill, nxt[1] = _codes_by_min_node(np.where(left, codes[:, 0], -1), nxt[1])
        l2[left] = fill[left]
        codes[:, 1] = l2
        info["l2"] = {"mode": "carried", "carried": int((carried[:, 1] >= 0).sum()), "network": net2,
                      "nearest": near2, "new_cells": int(len(np.unique(codes[left, 0])))}

    # 4. expressway rule for new expressway-only nodes
    rules["expressway_l1"], rules["expressway_l2"] = _expressway_new(ng, cg, codes, new, xonly)

    # 5. ferry rule, levels 1-2
    members, homes, finfo = _ferry_units(ng)
    rules["ferry"] = dict(finfo, nodes_relabelled=_apply_ferry(codes, members, homes, 0, 2))

    # 6-8. L1 contiguity, L2 nesting, L2 contiguity
    detached = {}
    zeros = np.zeros(n, dtype=np.int64)
    codes[:, 0], rules["contiguity_l1"] = _contiguity(cg, codes[:, 0], zeros, ng.component, xonly)
    codes[:, 1], nxt[1], rules["nesting_l2"] = _nest(codes[:, 1], codes[:, 0], nxt[1])
    codes[:, 1], rules["contiguity_l2"] = _contiguity(cg, codes[:, 1], codes[:, 0], ng.component, xonly)

    # 9. L2 hashes; resplit mode per L2 cell
    cells2 = _Cells(ng, codes[:, 1])
    l2_hash = {int(c): cell_hash(cells2.graph(c)) for c in cells2.codes.tolist()}
    modes = _resplit_modes(cells2, carried, previous, l2_hash, config)
    info["resplit"] = {m: sum(1 for v in modes.values() if v == m) for m in ("unchanged", "warm", "fresh")}

    # 10-11. levels 3 and deeper
    for lv in range(2, k):
        codes[:, lv], nxt[lv], rules[f"nesting_l{lv + 1}"], rules[f"split_l{lv + 1}"] = _split_level(
            ng, codes, carried, lv, modes, cells2 if lv == 2 else None, config, workers, nxt[lv])
        rules[f"ferry_l{lv + 1}"] = {"nodes_relabelled": _apply_ferry(codes, members, homes, lv, lv + 1)}
        codes[:, lv], rules[f"contiguity_l{lv + 1}"] = _contiguity(cg, codes[:, lv], codes[:, lv - 1], ng.component,
                                                                   xonly)
    del cells2
    for lv in range(k):
        r = rules[f"contiguity_l{lv + 1}"]
        detached[f"l{lv + 1}"] = {"pieces": r["expressway_detached"], "nodes": r["expressway_detached_nodes"]}
    info["expressway_detached"] = detached
    if (codes < 0).any():
        raise PartitionError(f"{int((codes < 0).any(axis=1).sum())} nodes left without a label at some level")
    for lv in range(1, k):
        spans = _spanning(codes[:, lv], codes[:, lv - 1])
        if spans:
            raise PartitionError(f"level {lv + 1}: {spans} cells span several parent cells after the rules")

    # 12. dense indices
    cells, seg_labels = [], []
    tail = np.asarray(g.seg_tail)
    for lv in range(k):
        c = np.unique(codes[:, lv])
        if len(c) > MAX_CELLS:
            raise PartitionError(f"level {lv + 1}: {len(c)} cells, more than MAX_CELLS = {MAX_CELLS}")
        cells.append(c)
        seg_labels.append(np.searchsorted(c, codes[:, lv]).astype(np.uint16)[tail])
    info["nodes_moved"] = {f"l{lv + 1}": int(((carried[:, lv] >= 0) & (codes[:, lv] != carried[:, lv])).sum())
                           for lv in range(k)}
    info["cells_vanished"] = {
        f"l{lv + 1}": int(len(np.setdiff1d(np.unique(carried[carried[:, lv] >= 0, lv]), cells[lv])))
        for lv in range(k)}
    return Partition(codes=codes, cells=cells, seg_labels=seg_labels, l2_hash={int(c): h for c, h in l2_hash.items()},
                     info=info, ids=ng.osm.copy())


def _spanning(labels, parent) -> int:
    pairs = _unique_rows(np.asarray(labels), np.asarray(parent))[0]
    return int(len(pairs) - len(np.unique(pairs[:, 0])))


def _fresh_l2(ng: NodeGraph, l1, config: PartitionConfig, workers, next_code):
    """A fresh ``split_cell`` per L1 cell; codes from ``next_code`` in order of each cell's smallest OSM id."""
    cells = _Cells(ng, l1)
    jobs = []
    for code in cells.codes.tolist():
        size = cells.size(code)
        k = max(config.l2_min_k, int(round(size / config.l2_nodes)))
        jobs.append({"code": code, "graph": (lambda c=code: cells.graph(c)), "n": size, "old": None, "k": k,
                     "band_km": config.l2_band_km, "mode": "fresh"})
    res = split_jobs(jobs, workers, per_worker_gb=PER_WORKER_GB_L2)
    part = np.full(ng.n, -1, dtype=np.int64)
    offset = 0
    for code in sorted(res):
        part[cells.nodes(code)] = offset + res[code]
        offset += int(res[code].max()) + 1 if len(res[code]) else 0
    l2, next_code = _codes_by_min_node(part, next_code)
    return l2, next_code, {"mode": "fresh_split", "cells": int(len(np.unique(l2)))}


def _resplit_modes(cells2: _Cells, carried, previous, l2_hash, config) -> dict:
    """Per L2 code: "unchanged" (same hash, complete deeper codes), "warm" or "fresh" (Decision 10)."""
    k = carried.shape[1]
    if previous is None:
        return {c: "fresh" for c in l2_hash}
    if previous.l2_hash is None:
        return {c: "warm" for c in l2_hash}
    old_count = {}
    if previous.codes.shape[1] > 1:
        prev2 = previous.codes[:, 1]
        c, cnt = np.unique(prev2[prev2 >= 0], return_counts=True)
        old_count = dict(zip(c.tolist(), cnt.tolist()))
    modes = {}
    for code in l2_hash:
        nodes = cells2.nodes(code)
        if previous.l2_hash.get(code) == l2_hash[code] and (k == 2 or (carried[nodes, 2:] >= 0).all()):
            modes[code] = "unchanged"
            continue
        nn = len(nodes)
        n_new = int((carried[nodes, 1] < 0).sum())
        n_old = old_count.get(code, 0)
        grown = abs(nn - n_old) > config.fresh_frac * n_old
        modes[code] = "fresh" if n_new > config.fresh_frac * nn or grown else "warm"
    return modes


def _dense(local_old, uniq) -> np.ndarray:
    return np.where(local_old >= 0, np.searchsorted(uniq, local_old), -1)


def _split_level(ng, codes, carried, lv, modes, cells, config, workers, next_code):
    """Codes at level ``lv`` (>= 2): kept in unchanged L2 cells, re-split in the others, one job per parent cell.

    A node whose parent code (level ``lv - 1``) changed since the previous release loses its old code here, so a
    piece moved into another cell by contiguity or nesting is relabelled by the warm split instead of keeping a
    stray cell of its own. Each job's CellGraph is built only when the job is dispatched (``_Cells.graph``).
    """
    i = lv - 2
    band = float(config.split_band_km[min(i, len(config.split_band_km) - 1)])
    per_cell = int(config.split_nodes[i])
    keep = codes[:, lv - 1] == carried[:, lv - 1]
    old = np.where(keep, carried[:, lv], -1)
    old, next_code, nest_info = _nest(old, codes[:, lv - 1], next_code)
    nest_info["parent_changed"] = int(((carried[:, lv] >= 0) & ~keep).sum())
    del keep
    changed = np.array(sorted(c for c, m in modes.items() if m != "unchanged"), dtype=np.int64)
    in_changed = np.isin(codes[:, 1], changed)
    level = np.where(in_changed, -1, old)
    if cells is None:
        cells = _Cells(ng, np.where(in_changed, codes[:, lv - 1], -1))
        units = cells.codes
    else:
        units = changed
    jobs, meta = [], {}
    split = {"warm": 0, "fresh": 0}
    for code in units.tolist():
        nodes = cells.nodes(code)
        local_old = old[nodes]
        mode = modes[int(codes[nodes[0], 1])] if (local_old >= 0).any() else "fresh"
        uniq = np.unique(local_old[local_old >= 0])
        k = split_k(len(nodes), cells.junction_count(code), per_cell, 2)
        old_fn = (lambda c=code, u=uniq: _dense(old[cells.nodes(c)], u)) if mode == "warm" else None
        jobs.append({"code": code, "graph": (lambda c=code: cells.graph(c)), "n": len(nodes), "old": old_fn,
                     "k": k, "band_km": band, "mode": mode})
        meta[code] = (mode, uniq)
        split[mode] += 1
    res = split_jobs(jobs, workers, per_worker_gb=PER_WORKER_GB_L3) if jobs else {}
    part = np.full(ng.n, -1, dtype=np.int64)
    offset = 0
    for code in sorted(res):
        mode, uniq = meta[code]
        nodes = cells.nodes(code)
        if mode == "warm":
            level[nodes] = uniq[res[code]]
        else:
            part[nodes] = offset + res[code]
            offset += int(res[code].max()) + 1
    fresh, next_code = _codes_by_min_node(part, next_code)
    level[part >= 0] = fresh[part >= 0]
    return level, next_code, nest_info, split


def _cut_counts(ng: NodeGraph, labels) -> "tuple[int, int]":
    la, lb = labels[ng.lo], labels[ng.hi]
    cut = (la >= 0) & (lb >= 0) & (la != lb)
    return int(cut.sum()), int((cut & ng.expressway).sum())


def _expressway_new(ng: NodeGraph, cg: CellGraph, codes, new, xonly) -> "tuple[dict, dict]":
    """Decision 13 for the new expressway-only nodes, levels 1-2; returns the info of each level.

    A run is a set of expressway-only nodes joined by expressway edges. A new node in a run that also holds carried
    nodes takes the L1 and L2 codes of the nearest carried node of its run (by length along the run), so an edit
    inside an old run never splits it. Only runs made wholly of new nodes get ``expressway_rule``; its labels for
    every other node are discarded. The cut counts describe the labels actually applied.
    """
    n = ng.n
    newx = new & xonly
    run_edge = np.flatnonzero(ng.expressway & xonly[ng.lo] & xonly[ng.hi])
    lo, hi = ng.lo[run_edge], ng.hi[run_edge]
    run = connected_components(coo_matrix((np.ones(len(lo), dtype=np.int8), (lo, hi)), shape=(n, n)),
                               directed=False)[1]
    old_run = np.zeros(n, dtype=bool)
    old_run[run[xonly & ~new]] = True
    join = newx & old_run[run]
    whole = newx & ~old_run[run]
    before = [codes[new, lv].copy() for lv in (0, 1)]
    cuts_before = [_cut_counts(ng, codes[:, lv]) for lv in (0, 1)]
    if join.any():
        w = np.maximum(ng.length_m[run_edge].astype(np.float64), MIN_LENGTH_M)
        graph = csr_matrix((w, (lo, hi)), shape=(n, n))
        _, _, src = dijkstra(graph, directed=False, indices=np.flatnonzero(xonly & ~new), min_only=True,
                             return_predecessors=True)
        j = np.flatnonzero(join)
        codes[j, :2] = codes[src[j], :2]
    if whole.any():
        for lv, parent in ((0, None), (1, codes[:, 0])):
            lab, _ = expressway_rule(cg, codes[:, lv], parent)
            codes[whole, lv] = lab[whole]
    out = []
    for lv in (0, 1):
        after = _cut_counts(ng, codes[:, lv])
        out.append({"new_expressway_only_nodes": int(newx.sum()), "joined_carried_run": int(join.sum()),
                    "new_run_nodes": int(whole.sum()),
                    "new_nodes_relabelled": int((codes[new, lv] != before[lv]).sum()),
                    "cut_edges_before": cuts_before[lv][0], "cut_edges_after": after[0],
                    "expressway_cut_edges_before": cuts_before[lv][1], "expressway_cut_edges_after": after[1]})
    return out[0], out[1]


# ---- checks ----------------------------------------------------------------------------------------------------------

def check_partition(ng: NodeGraph, p: Partition, previous: "Previous | None", expected_l1: "int | None") -> dict:
    """Named checks ``{"value", "ok", "blocking", ...}``: ``labels_complete``, ``nesting``, ``contiguity_l{1..k}``,
    ``l1_cells_unchanged`` (blocking) and ``l2_cells_changed`` (warning). Contiguity counts the pieces beyond one
    per (weak component, cell), detached expressway-only pieces excepted (reported beside it). ``expected_l1``
    defaults to the previous release's L1 count; with neither, the L1 count is only reported."""
    codes = p.codes
    n, k = codes.shape
    checks = {}
    missing = int((codes < 0).any(axis=1).sum())
    checks["labels_complete"] = {"value": missing, "ok": missing == 0, "blocking": True}
    spans = sum(_spanning(codes[:, lv], codes[:, lv - 1]) for lv in range(1, k))
    checks["nesting"] = {"value": int(spans), "ok": spans == 0, "blocking": True}
    xonly = _expressway_only(ng)
    lo, hi = ng.lo.astype(np.int64), ng.hi.astype(np.int64)
    for lv in range(k):
        parent = codes[:, lv - 1] if lv else np.zeros(n, dtype=np.int64)
        key, _ = _keys(codes[:, lv], parent, ng.component)
        exempt = _exempt(n, lo, hi, key, xonly) if (key >= 0).any() else np.zeros(n, dtype=bool)
        v = _fragments(n, lo, hi, key, exempt)
        pc = _pieces(n, lo, hi, key)
        checks[f"contiguity_l{lv + 1}"] = {"value": v, "ok": v == 0, "blocking": True,
                                           "expressway_detached": int(len(np.unique(pc[exempt])))}
    n1 = int(len(np.unique(codes[codes[:, 0] >= 0, 0])))
    if expected_l1 is None and previous is not None:
        c = previous.codes[:, 0]
        expected_l1 = int(len(np.unique(c[c >= 0])))
    checks["l1_cells_unchanged"] = {"value": n1, "expected": expected_l1,
                                    "ok": expected_l1 is None or n1 == int(expected_l1), "blocking": True}
    n2 = int(len(np.unique(codes[codes[:, 1] >= 0, 1]))) if k > 1 else 0
    prev2 = None
    if previous is not None and previous.codes.shape[1] > 1:
        c = previous.codes[:, 1]
        prev2 = int(len(np.unique(c[c >= 0])))
    checks["l2_cells_changed"] = {"value": 0 if prev2 is None else n2 - prev2, "cells": n2, "previous": prev2,
                                  "ok": prev2 is None or n2 == prev2, "blocking": False}
    return checks


# ---- persistence -----------------------------------------------------------------------------------------------------

def _json_default(x):
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, np.ndarray):
        return x.tolist()
    raise TypeError(f"not JSON serialisable: {type(x).__name__}")


def _stats(p: Partition) -> dict:
    out = {"nodes": int(len(p.codes)), "segments": int(len(p.seg_labels[0])) if p.seg_labels else 0, "levels": {}}
    for lv, cells in enumerate(p.cells):
        size = np.bincount(np.searchsorted(cells, p.codes[:, lv]), minlength=len(cells))
        out["levels"][f"l{lv + 1}"] = {"cells": int(len(cells)), "nodes_per_cell": {
            "min": int(size.min()), "p50": float(np.median(size)), "max": int(size.max())}}
    for key in ("nodes_moved", "cells_vanished", "expressway_detached", "carry_over"):
        if key in p.info:
            out[key] = p.info[key]
    return out


def save_partition(p: Partition, release_dir) -> None:
    """``labels_l{l}.npy``, ``node_labels.npz`` and ``partition.json`` in ``release_dir``, each written to a temp
    file and renamed into place. ``p.info`` may carry ``checks``, ``polygons_sha256`` and ``rivers_sha256``."""
    d = Path(release_dir)
    d.mkdir(parents=True, exist_ok=True)
    k = p.codes.shape[1]
    for lv, lab in enumerate(p.seg_labels):
        _atomic(d / f"labels_l{lv + 1}.npy", lambda f, a=lab: np.save(f, np.ascontiguousarray(a, dtype=np.uint16)))
    _atomic(d / "node_labels.npz", lambda f: np.savez(f, ids=p.ids, codes=p.codes))
    parents = {}
    for lv in range(1, k):
        first = np.unique(p.codes[:, lv], return_index=True)[1]
        parents[f"l{lv + 1}"] = p.codes[first, lv - 1].tolist()
    doc = {"format": PARTITION_FORMAT, "k": k, "config": p.info.get("config"),
           "cells": [c.tolist() for c in p.cells], "parents": parents,
           "l2_hash": {str(c): h for c, h in sorted(p.l2_hash.items())}, "resplit": p.info.get("resplit"),
           "rules": p.info.get("rules", {}), "checks": p.info.get("checks", {}), "stats": _stats(p),
           "previous": p.info.get("previous"), "polygons_sha256": p.info.get("polygons_sha256"),
           "rivers_sha256": p.info.get("rivers_sha256")}
    text = json.dumps(doc, ensure_ascii=False, indent=1, default=_json_default)
    _atomic(d / "partition.json", lambda f: f.write(text.encode("utf-8")))


def load_partition(release_dir) -> Partition:
    d = Path(release_dir)
    doc = json.loads((d / "partition.json").read_text(encoding="utf-8"))
    if doc.get("format") != PARTITION_FORMAT:
        raise PartitionError(f"partition format {doc.get('format')} in {d}, expected {PARTITION_FORMAT}")
    with np.load(d / "node_labels.npz") as z:
        ids, codes = z["ids"], z["codes"]
    k = int(doc["k"])
    seg_labels = [np.load(d / f"labels_l{lv + 1}.npy") for lv in range(k)]
    return Partition(codes=codes, cells=[np.asarray(c, dtype=np.int64) for c in doc["cells"]], seg_labels=seg_labels,
                     l2_hash={int(c): h for c, h in doc["l2_hash"].items()}, info=doc, ids=ids)


# ---- CLI -------------------------------------------------------------------------------------------------------------

def _cmd_import_seed(args) -> int:
    info = import_seed(args.scratch, args.out)
    print(json.dumps(info, indent=1))
    return 0


def _cmd_run(args) -> int:
    release = Path(args.release)
    g = load_graph(release / "graph", mmap=True)
    rivers = json.loads(Path(args.rivers).read_text(encoding="utf-8"))
    polygons = json.loads(Path(args.polygons).read_text(encoding="utf-8"))
    ng = node_graph(g, rivers)
    previous = load_previous(args.previous) if args.previous else load_seed(args.seed) if args.seed else None
    p = build_partition(g, ng, previous, polygons, PartitionConfig(), args.workers)
    if previous is not None:
        expected_l1 = None
    else:
        expected_l1 = len({(f.get("properties") or {}).get("region") for f in polygons.get("features") or []})
    p.info["checks"] = check_partition(ng, p, previous, expected_l1)
    p.info["polygons_sha256"] = _sha256_file(args.polygons)
    p.info["rivers_sha256"] = _sha256_file(args.rivers)
    del g
    save_partition(p, release)
    print(release)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m speedmatrix.partition", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("import-seed", help="import the v3.2 seed tables into one npz")
    s.add_argument("--scratch", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.set_defaults(func=_cmd_import_seed)
    r = sub.add_parser("run", help="partition labels of a release (its graph/ dir)")
    r.add_argument("--release", type=Path, required=True)
    src = r.add_mutually_exclusive_group()
    src.add_argument("--previous", type=Path, help="release dir to carry labels over from")
    src.add_argument("--seed", type=Path, help="seed npz written by import-seed")
    r.add_argument("--polygons", type=Path, default=DEFAULT_POLYGONS)
    r.add_argument("--rivers", type=Path, default=DEFAULT_RIVERS)
    r.add_argument("--workers", type=int, default=None)
    r.set_defaults(func=_cmd_run)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
