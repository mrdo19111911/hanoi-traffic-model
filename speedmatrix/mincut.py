"""Min-cut cell splitter: split a road graph cell into k cells whose borders cut few, preferably big, roads.

Ported from the gate spike (``reports/gate_compose_spike``): ``weighted_cut`` and the pair loop of ``refine`` come from
``refine_mincut.py``, the k-means seeds, the network Voronoi and ``merge_fragments`` from ``hier_split.py``, and the
short-run absorption from ``apply_overrides.absorb_short_runs``. Two rules differ from the spike:

- a river-crossing edge stays in the graph with capacity 0 (Decision 12), where the spike's ``matrix_graph.py`` removed
  it; contiguity and the Voronoi follow the real network while the cut still prefers the river;
- expressway-only nodes take the label of the nearest non-expressway node with the same parent (Decision 13) instead
  of the spike's land raster.

Every function here is pure: no file, network or OSRM access. ``split_jobs`` runs many cells in a process pool.
"""
from dataclasses import dataclass
import hashlib
import multiprocessing
import warnings

import numpy as np
from scipy.cluster.vq import kmeans2
from scipy.sparse import coo_matrix, csr_matrix
from scipy.sparse.csgraph import breadth_first_order, connected_components, dijkstra, maximum_flow
from scipy.spatial import cKDTree

try:
    from .geo import haversine_m
    from .sysres import default_workers
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.geo import haversine_m
    from speedmatrix.sysres import default_workers

# Min-cut capacity per highway value: when two cuts cross as many roads, the one over bigger roads wins.
CAPACITY = {"motorway": 6, "motorway_link": 6, "trunk": 6, "trunk_link": 6, "primary": 7, "primary_link": 7,
            "secondary": 8, "secondary_link": 8, "tertiary": 9, "tertiary_link": 9}
DEFAULT_CAPACITY = 10
EXPRESSWAY = ("motorway", "motorway_link")
MIN_RUN_KM = 2.0      # an expressway run shorter than this between two crossings into one cell joins that cell
SEED = 17
KM_PER_DEG = 111.32
BAND_MARGIN_KM = 1.0  # refine_pairs keeps edges with an end this far beyond the band (the spike's band + 1 km)
MIN_LENGTH_M = 1e-3   # Voronoi edge length floor, so a zero-length edge is still an edge
PER_WORKER_GB_L3 = 0.5
PER_WORKER_GB_L2 = 3.0


@dataclass
class CellGraph:
    """Undirected road graph of one cell; node arrays are (n,), edge arrays (m,), edges indexed locally."""
    node: np.ndarray        # int64, global node index
    osm: np.ndarray         # int64
    x_km: np.ndarray        # float64, local projection around the cell's mean lon/lat
    y_km: np.ndarray        # float64
    lo: np.ndarray          # int32, lo < hi
    hi: np.ndarray          # int32
    cap: np.ndarray         # int8, min-cut capacity (0 on a river crossing)
    length_m: np.ndarray    # float32
    expressway: np.ndarray  # bool, motorway or motorway_link

    @property
    def n(self) -> int:
        return len(self.osm)

    @classmethod
    def from_edges(cls, osm, lon, lat, lo, hi, highway, names, river, length_m=None, node=None) -> "CellGraph":
        """Build a cell from node coordinates and undirected edges.

        ``highway`` holds int codes into ``names`` (no per-edge strings), ``river`` marks river-crossing edges
        (capacity 0). ``length_m`` defaults to the haversine length, ``node`` to ``arange(n)``. Self-loops,
        endpoints out of range and unknown highway codes raise ``ValueError``.
        """
        osm = np.asarray(osm, dtype=np.int64)
        lon = np.asarray(lon, dtype=np.float64)
        lat = np.asarray(lat, dtype=np.float64)
        n = len(osm)
        if lon.shape != (n,) or lat.shape != (n,):
            raise ValueError(f"lon/lat shapes {lon.shape}/{lat.shape} do not match {n} nodes")
        a = np.asarray(lo, dtype=np.int64)
        b = np.asarray(hi, dtype=np.int64)
        m = len(a)
        highway = np.asarray(highway)
        river = np.asarray(river, dtype=bool)
        if b.shape != (m,) or highway.shape != (m,) or river.shape != (m,):
            raise ValueError(f"edge arrays differ in length: lo {a.shape}, hi {b.shape}, highway {highway.shape}, "
                             f"river {river.shape}")
        if m and (min(a.min(), b.min()) < 0 or max(a.max(), b.max()) >= n):
            raise ValueError(f"edge endpoint out of range 0..{n - 1}")
        if (a == b).any():
            raise ValueError(f"{int((a == b).sum())} self-loop edges")
        if highway.dtype.kind not in "iu":
            raise ValueError(f"highway must hold integer codes, not {highway.dtype}")
        if m and (highway.min() < 0 or highway.max() >= len(names)):
            raise ValueError(f"highway code outside 0..{len(names) - 1}")
        cap_of = np.array([CAPACITY.get(h, DEFAULT_CAPACITY) for h in names], dtype=np.int8)
        express_of = np.array([h in EXPRESSWAY for h in names], dtype=bool)
        codes = highway.astype(np.int64)
        cap = cap_of[codes] if m else np.zeros(0, dtype=np.int8)
        cap[river] = 0
        if length_m is None:
            length_m = haversine_m(lat[a], lon[a], lat[b], lon[b])
        length_m = np.asarray(length_m, dtype=np.float32)
        if length_m.shape != (m,):
            raise ValueError(f"length_m has shape {length_m.shape}, expected ({m},)")
        node = np.arange(n, dtype=np.int64) if node is None else np.asarray(node, dtype=np.int64)
        if node.shape != (n,):
            raise ValueError(f"node has shape {node.shape}, expected ({n},)")
        lat0 = float(lat.mean()) if n else 0.0
        lon0 = float(lon.mean()) if n else 0.0
        return cls(node=node, osm=osm,
                   x_km=(lon - lon0) * KM_PER_DEG * np.cos(np.radians(lat0)), y_km=(lat - lat0) * KM_PER_DEG,
                   lo=np.minimum(a, b).astype(np.int32), hi=np.maximum(a, b).astype(np.int32), cap=cap,
                   length_m=length_m, expressway=express_of[codes] if m else np.zeros(0, dtype=bool))


def _xy(g: CellGraph) -> np.ndarray:
    return np.c_[g.x_km, g.y_km]


def _degree(g: CellGraph) -> np.ndarray:
    return np.bincount(g.lo, minlength=g.n) + np.bincount(g.hi, minlength=g.n)


def _length_graph(g: CellGraph) -> csr_matrix:
    """Upper-triangular CSR of edge lengths (shortest of parallel edges), for undirected Dijkstra."""
    lo, hi = g.lo.astype(np.int64), g.hi.astype(np.int64)
    w = np.maximum(g.length_m.astype(np.float64), MIN_LENGTH_M)
    o = np.lexsort((w, hi, lo))
    lo, hi, w = lo[o], hi[o], w[o]
    first = np.r_[True, (lo[1:] != lo[:-1]) | (hi[1:] != hi[:-1])] if len(lo) else np.zeros(0, dtype=bool)
    return csr_matrix((w[first], (lo[first], hi[first])), shape=(g.n, g.n))


def _cut_edges(g: CellGraph, labels: np.ndarray) -> np.ndarray:
    la, lb = labels[g.lo], labels[g.hi]
    return (la >= 0) & (lb >= 0) & (la != lb)


def weighted_cut(lo, hi, cap, side: np.ndarray, n: int) -> "tuple[int, np.ndarray, np.ndarray]":
    """Min cut between the side-1 and side-2 nodes; returns (value, a_side mask, b_side mask) over the n nodes.

    ``a_side`` holds the side-1 nodes and the nodes reached from them in the residual graph, ``b_side`` the side-2
    nodes and the other nodes that reach them; a free node in neither may go either way. Capacity-0 edges carry no flow, so they never connect the two sides.
    """
    lo = np.asarray(lo, dtype=np.int64)
    hi = np.asarray(hi, dtype=np.int64)
    cap = np.asarray(cap).astype(np.int32)
    side = np.asarray(side)
    s_node, t_node = n, n + 1
    u = np.where(side[lo] == 1, s_node, np.where(side[lo] == 2, t_node, lo))
    v = np.where(side[hi] == 1, s_node, np.where(side[hi] == 2, t_node, hi))
    keep = (u != v) & (cap > 0)
    u, v, c = u[keep], v[keep], cap[keep]
    nodes = np.unique(np.concatenate([u, v, [s_node, t_node]]))
    cu, cv = np.searchsorted(nodes, u), np.searchsorted(nodes, v)
    m = len(nodes)
    graph = csr_matrix((np.r_[c, c], (np.r_[cu, cv], np.r_[cv, cu])), shape=(m, m), dtype=np.int32)
    graph.sum_duplicates()
    s, t = int(np.searchsorted(nodes, s_node)), int(np.searchsorted(nodes, t_node))
    res = maximum_flow(graph, s, t)
    residual = (graph - res.flow).tocsr()
    residual.data = (residual.data > 0).astype(np.int8)
    residual.eliminate_zeros()
    from_s = np.zeros(m, dtype=bool)
    from_s[breadth_first_order(residual, s, directed=True, return_predecessors=False)] = True
    to_t = np.zeros(m, dtype=bool)
    to_t[breadth_first_order(residual.T.tocsr(), t, directed=True, return_predecessors=False)] = True
    a_side, b_side = side[:n] == 1, side[:n] == 2
    real = nodes < n
    a_side[nodes[real & from_s]] = True
    b_side[nodes[real & to_t & ~from_s]] = True
    return int(res.flow_value), a_side, b_side


def refine_pairs(g: CellGraph, labels, band_km: float) -> "tuple[np.ndarray, list[dict]]":
    """Move each border between two cells onto a minimum cut inside a band around it.

    Pairs of cells sharing edges run one after another, largest current cut first (ties by codes), each on the labels
    left by the previous ones. In a pair (A, B), nodes farther than ``band_km`` from the current A-B border are fixed
    (A to the source, B to the sink); the min cut over the rest decides the new border. Nodes reached from the source
    become A, nodes reaching the sink become B, the others keep their label. Labels below 0 never change.
    """
    labels = np.asarray(labels, dtype=np.int64).copy()
    lo, hi = g.lo.astype(np.int64), g.hi.astype(np.int64)
    cap = g.cap.astype(np.int32)
    xy = _xy(g)
    n = g.n
    differ = _cut_edges(g, labels)
    if not differ.any():
        return labels, []
    la, lb = labels[lo[differ]], labels[hi[differ]]
    pairs, counts = np.unique(np.c_[np.minimum(la, lb), np.maximum(la, lb)], axis=0, return_counts=True)
    order = np.lexsort((pairs[:, 1], pairs[:, 0], -counts))
    rows = []
    for ca, cb in pairs[order].tolist():
        la, lb = labels[lo], labels[hi]
        in_pair = np.flatnonzero(((la == ca) | (la == cb)) & ((lb == ca) | (lb == cb)))
        e_lo, e_hi = lo[in_pair], hi[in_pair]
        cur = labels[e_lo] != labels[e_hi]
        if not cur.any():
            continue
        before = int(cur.sum())
        mid = (xy[e_lo[cur]] + xy[e_hi[cur]]) / 2
        nodes = np.unique(np.r_[e_lo, e_hi])
        dist = np.full(n, np.inf)
        dist[nodes] = cKDTree(mid).query(xy[nodes])[0]
        side = np.zeros(n, dtype=np.int8)
        far = dist[nodes] > band_km
        side[nodes[far & (labels[nodes] == ca)]] = 1
        side[nodes[far & (labels[nodes] == cb)]] = 2
        band = (dist[e_lo] <= band_km + BAND_MARGIN_KM) | (dist[e_hi] <= band_km + BAND_MARGIN_KM)
        value, a_side, b_side = weighted_cut(e_lo[band], e_hi[band], cap[in_pair][band], side, n)
        free = nodes[side[nodes] == 0]
        moved = int(((labels[free] == cb) & a_side[free]).sum() + ((labels[free] == ca) & b_side[free]).sum())
        labels[free[a_side[free]]] = ca
        labels[free[b_side[free]]] = cb
        after = int((labels[e_lo] != labels[e_hi]).sum())
        rows.append({"pair": [int(ca), int(cb)], "edges_before": before, "edges_after": after, "cut_cost": value,
                     "nodes_moved": moved})
    return labels, rows


def _pieces(g: CellGraph, labels: np.ndarray) -> np.ndarray:
    """Component id per node over the edges whose two ends share a label >= 0."""
    same = (labels[g.lo] >= 0) & (labels[g.lo] == labels[g.hi])
    a = coo_matrix((np.ones(int(same.sum()), dtype=np.int8), (g.lo[same], g.hi[same])), shape=(g.n, g.n))
    return connected_components(a, directed=False)[1]


def merge_fragments(g: CellGraph, labels) -> "tuple[np.ndarray, int]":
    """Every connected piece of a label except its largest joins the label it shares the most edges with.

    Ties go to the lowest label; among equal-size pieces the one holding the lowest node index is the largest. A piece
    with no edge to another labelled node is left as it is. In each round a piece moves only when no neighbouring piece
    moved before it in that round, so every move joins a whole piece to a neighbour and the loop ends. Returns the new
    labels and the number of pieces moved.
    """
    labels = np.asarray(labels, dtype=np.int64).copy()
    n = g.n
    lo, hi = g.lo.astype(np.int64), g.hi.astype(np.int64)
    moved = 0
    while True:
        valid = labels >= 0
        comp = _pieces(g, labels)
        size = np.bincount(comp[valid], minlength=n)
        comp_label = np.full(n, -1, dtype=np.int64)
        comp_label[comp[valid]] = labels[valid]
        found = np.flatnonzero(size)
        o = np.lexsort((found, -size[found], comp_label[found]))
        ranked = found[o]
        if not len(ranked):
            break
        largest = np.r_[True, comp_label[ranked][1:] != comp_label[ranked][:-1]]
        stray = np.zeros(n, dtype=bool)
        stray[ranked[~largest]] = True
        if not stray.any():
            break
        border = valid[lo] & valid[hi] & (labels[lo] != labels[hi])
        a = np.r_[comp[lo[border]], comp[hi[border]]]
        b = np.r_[comp[hi[border]], comp[lo[border]]]
        m = stray[a]
        if not m.any():
            break
        keys, cnt = np.unique(np.c_[a[m], comp_label[b[m]]], axis=0, return_counts=True)
        keys = keys[np.lexsort((keys[:, 1], -cnt, keys[:, 0]))]
        best = np.r_[True, keys[1:, 0] != keys[:-1, 0]]
        target = dict(zip(keys[best, 0].tolist(), keys[best, 1].tolist()))
        pairs = np.unique(np.c_[a, b], axis=0)
        starts = np.searchsorted(pairs[:, 0], np.arange(n + 1))
        blocked = np.zeros(n, dtype=bool)
        new = np.full(n, -1, dtype=np.int64)
        for p in sorted(target, key=lambda c: (int(size[c]), c)):
            if blocked[p]:
                continue
            new[p] = target[p]
            blocked[p] = True
            blocked[pairs[starts[p]:starts[p + 1], 1]] = True
            moved += 1
        hit = valid & (new[comp] >= 0)
        labels[hit] = new[comp[hit]]
    return labels, moved


def _group_nearest(xy, labels, movable, candidates, parent):
    """For each movable node, the label of the nearest candidate node with the same parent (Euclidean).

    Returns (node indices, their new labels, count of movable nodes whose parent has no candidate)."""
    q = np.flatnonzero(movable)
    c = np.flatnonzero(candidates)
    out_nodes, out_labels, kept = [], [], 0
    q = q[np.argsort(parent[q], kind="stable")]
    c = c[np.argsort(parent[c], kind="stable")]
    qp, cp = parent[q], parent[c]
    for p in np.unique(qp):
        qs = q[np.searchsorted(qp, p, "left"):np.searchsorted(qp, p, "right")]
        cs = c[np.searchsorted(cp, p, "left"):np.searchsorted(cp, p, "right")]
        if not len(cs):
            kept += len(qs)
            continue
        out_nodes.append(qs)
        out_labels.append(labels[cs[cKDTree(xy[cs]).query(xy[qs])[1]]])
    if not out_nodes:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), kept
    return np.concatenate(out_nodes), np.concatenate(out_labels), kept


def _absorb_short_runs(g: CellGraph, labels: np.ndarray, only: np.ndarray, parent: np.ndarray) -> int:
    """A run (connected movable expressway-only nodes of one label) shorter than MIN_RUN_KM whose expressway neighbours
    all carry one other label >= 0, with the run's parent, takes that label. One run per round, the shortest first
    (ties: lowest run id), so two neighbouring short runs do not swap labels. Changes ``labels`` in place."""
    e = np.flatnonzero(g.expressway)
    if not len(e) or not only.any():
        return 0
    nodes = np.unique(np.r_[g.lo[e], g.hi[e]]).astype(np.int64)
    u = np.searchsorted(nodes, g.lo[e])
    v = np.searchsorted(nodes, g.hi[e])
    km = g.length_m[e].astype(np.float64) / 1000.0
    onl = only[nodes]
    par = parent[nodes]
    k = len(nodes)
    absorbed = 0
    while True:
        lab = labels[nodes]
        inner = onl[u] & onl[v] & (lab[u] == lab[v])
        a = coo_matrix((np.ones(int(inner.sum()), dtype=np.int8), (u[inner], v[inner])), shape=(k, k))
        comp = np.where(onl, connected_components(a, directed=False)[1], -1)
        length = np.bincount(comp[u[inner]], weights=km[inner], minlength=k)
        rim = comp[u] != comp[v]
        run = np.r_[comp[u[rim]], comp[v[rim]]]
        other = np.r_[v[rim], u[rim]]
        m = run >= 0
        run, other = run[m], other[m]
        if not len(run):
            return absorbed
        runs = np.unique(run)
        own = np.full(k, -1, dtype=np.int64)
        own[comp[onl]] = lab[onl]
        run_par = np.full(k, 0, dtype=np.int64)
        run_par[comp[onl]] = par[onl]
        mixed = np.zeros(k, dtype=bool)  # a run whose own nodes span two parents never moves
        np.logical_or.at(mixed, comp[onl], par[onl] != run_par[comp[onl]])
        np.logical_or.at(mixed, run, par[other] != run_par[run])
        pairs = np.unique(np.c_[run, lab[other]], axis=0)
        n_labels = np.bincount(pairs[:, 0], minlength=k)
        target = np.full(k, -1, dtype=np.int64)
        target[pairs[:, 0]] = pairs[:, 1]
        ok = ((n_labels[runs] == 1) & (target[runs] >= 0) & (target[runs] != own[runs]) & ~mixed[runs]
              & (length[runs] < MIN_RUN_KM))
        flip = runs[ok]
        if not len(flip):
            return absorbed
        c = int(flip[np.lexsort((flip, length[flip]))[0]])
        labels[nodes[comp == c]] = target[c]
        absorbed += 1


def expressway_rule(g: CellGraph, labels, parent: "np.ndarray | None" = None) -> "tuple[np.ndarray, dict]":
    """Decision 13: expressway-only nodes (every edge motorway/motorway_link) take the label of the nearest
    non-expressway node with the same parent, then short runs are absorbed (``MIN_RUN_KM``).

    Only nodes with a label >= 0 move; candidates are non-expressway nodes with a label >= 0 and, when ``parent`` is
    given, the same parent. An expressway-only node whose parent has no candidate keeps its label (counted). The
    expressway is then cut where it crosses a border of the local network, not at an interchange.
    """
    labels = np.asarray(labels, dtype=np.int64).copy()
    n = g.n
    if parent is None:
        parent = np.zeros(n, dtype=np.int64)
    else:
        parent = np.asarray(parent, dtype=np.int64)
        if parent.shape != (n,):
            raise ValueError(f"parent has shape {parent.shape}, expected ({n},)")
    ex = g.expressway
    deg_ex = np.bincount(g.lo[ex], minlength=n) + np.bincount(g.hi[ex], minlength=n)
    deg_other = np.bincount(g.lo[~ex], minlength=n) + np.bincount(g.hi[~ex], minlength=n)
    only = (deg_ex > 0) & (deg_other == 0) & (labels >= 0)
    original = labels.copy()
    cut_before = _cut_edges(g, labels)
    moved_nodes, new_labels, kept = _group_nearest(_xy(g), labels, only, (deg_other > 0) & (labels >= 0), parent)
    labels[moved_nodes] = new_labels
    absorbed = _absorb_short_runs(g, labels, only, parent)
    cut_after = _cut_edges(g, labels)
    return labels, {"expressway_only_nodes": int(only.sum()), "nodes_relabelled": int((labels != original).sum()),
                    "nodes_without_candidate_kept": int(kept), "short_runs_absorbed": absorbed,
                    "cut_edges_before": int(cut_before.sum()), "cut_edges_after": int(cut_after.sum()),
                    "expressway_cut_edges_before": int((cut_before & ex).sum()),
                    "expressway_cut_edges_after": int((cut_after & ex).sum())}


def split_k(nodes: int, junctions: int, nodes_per_cell: int, min_k: int = 2) -> int:
    """Number of cells for a split: min(round(nodes / nodes_per_cell), junctions // 4); below ``min_k`` it is 1."""
    k = min(int(round(nodes / nodes_per_cell)), int(junctions) // 4)
    return k if k >= min_k else 1


def _finish(g: CellGraph, labels: np.ndarray, band_km: float, info: dict) -> np.ndarray:
    """Refinement, fragment merge and the expressway rule, recording each step in ``info``."""
    info["voronoi_cut_edges"] = int(_cut_edges(g, labels).sum())
    labels, info["refine"] = refine_pairs(g, labels, band_km)
    labels, info["fragments_moved"] = merge_fragments(g, labels)
    labels, info["expressway"] = expressway_rule(g, labels)
    info["final_cut_edges"] = int(_cut_edges(g, labels).sum())
    info["cells"] = int(len(np.unique(labels[labels >= 0])))
    return labels


def split_cell(g: CellGraph, k: int, band_km: float, seed: int = SEED) -> "tuple[np.ndarray, dict]":
    """Fresh split of a cell into at most k cells, labelled 0..k'-1.

    k-means (``minit="++"``, ``seed``) on the km coordinates of the junctions (degree >= 3); each centre snaps to its
    nearest junction (duplicates collapse, so k is also capped by the number of distinct junction positions). A
    network Voronoi by edge length assigns every node; nodes no seed reaches take the label of the nearest reached
    node. Then ``refine_pairs``, ``merge_fragments`` and ``expressway_rule``. Fewer than 2 seeds gives one cell.
    """
    n = g.n
    xy = _xy(g)
    junction = np.flatnonzero(_degree(g) >= 3)
    info = {"k": int(k), "band_km": band_km, "nodes": n, "junctions": int(len(junction))}
    seeds = np.zeros(0, dtype=np.int64)
    if k >= 2 and len(junction) >= 2:
        k_fit = min(int(k), len(np.unique(xy[junction], axis=0)))
        if k_fit >= 2:
            with warnings.catch_warnings():  # "one of the clusters is empty": its centre still snaps to a junction
                warnings.simplefilter("ignore")
                centres, _ = kmeans2(xy[junction], k_fit, minit="++", seed=seed)
            seeds = np.unique(junction[cKDTree(xy[junction]).query(centres)[1]])
    info["seeds"] = int(len(seeds))
    if len(seeds) < 2:
        info.update({"unreached": 0, "voronoi_cut_edges": 0, "refine": [], "fragments_moved": 0, "expressway": {},
                     "final_cut_edges": 0, "cells": 1 if n else 0})
        return np.zeros(n, dtype=np.int64), info
    _, _, src = dijkstra(_length_graph(g), directed=False, indices=seeds, min_only=True, return_predecessors=True)
    labels = np.full(n, -1, dtype=np.int64)
    reached = np.flatnonzero(src >= 0)
    labels[reached] = np.searchsorted(seeds, src[reached])
    lost = np.flatnonzero(src < 0)
    if len(lost):
        labels[lost] = labels[reached[cKDTree(xy[reached]).query(xy[lost])[1]]]
    info["unreached"] = int(len(lost))
    labels = _finish(g, labels, band_km, info)
    return np.unique(labels, return_inverse=True)[1].astype(np.int64).reshape(-1), info


def warm_split(g: CellGraph, old, band_km: float) -> "tuple[np.ndarray, dict]":
    """Re-split a cell starting from its old labels, keeping the old codes.

    Nodes with ``old == -1`` take the label of the nearest labelled node by length, or by Euclidean distance when no
    labelled node reaches them. Then ``refine_pairs``, ``merge_fragments`` and ``expressway_rule``. A cell without
    any labelled node raises ``ValueError``.
    """
    labels = np.asarray(old, dtype=np.int64).copy()
    if labels.shape != (g.n,):
        raise ValueError(f"old labels have shape {labels.shape}, expected ({g.n},)")
    known = np.flatnonzero(labels >= 0)
    if not len(known):
        raise ValueError("warm split needs at least one labelled node")
    new = np.flatnonzero(labels < 0)
    info = {"band_km": band_km, "nodes": g.n, "new_nodes": int(len(new)), "unreached": 0}
    if len(new):
        _, _, src = dijkstra(_length_graph(g), directed=False, indices=known, min_only=True,
                             return_predecessors=True)
        reached = new[src[new] >= 0]
        labels[reached] = labels[src[reached]]
        lost = new[src[new] < 0]
        if len(lost):
            labels[lost] = labels[known[cKDTree(_xy(g)[known]).query(_xy(g)[lost])[1]]]
        info["unreached"] = int(len(lost))
    return _finish(g, labels, band_km, info), info


def cell_hash(g: CellGraph) -> str:
    """sha256 over the sorted OSM node ids and the inner edges as sorted (min osm, max osm, capacity) rows."""
    osm = np.sort(np.asarray(g.osm, dtype=np.int64))
    a, b = g.osm[g.lo].astype(np.int64), g.osm[g.hi].astype(np.int64)
    rows = np.c_[np.minimum(a, b), np.maximum(a, b), g.cap.astype(np.int64)]
    rows = rows[np.lexsort((rows[:, 2], rows[:, 1], rows[:, 0]))]
    h = hashlib.sha256(f"cell nodes={len(osm)} edges={len(rows)}\n".encode("ascii"))
    h.update(osm.astype("<i8").tobytes())
    h.update(np.ascontiguousarray(rows, dtype="<i8").tobytes())
    return h.hexdigest()


def _run_job(job):
    code, mode, g, old, k, band_km = job
    if mode == "fresh":
        return code, split_cell(g, k, band_km)[0]
    return code, warm_split(g, old, band_km)[0]


def split_jobs(jobs: "list[dict]", workers: "int | None" = None,
               per_worker_gb: float = PER_WORKER_GB_L3) -> "dict[int, np.ndarray]":
    """Run split jobs, largest graph first, in a process pool; returns {code: labels}.

    A job is ``{"code", "graph", "old" (or None), "k", "band_km", "mode": "warm" | "fresh"}``. Each worker gets only
    the job's arrays and runs it end to end. ``workers`` defaults to ``sysres.default_workers(per_worker_gb)``:
    ``PER_WORKER_GB_L3`` (0.5) for L3 cells, ``PER_WORKER_GB_L2`` (3.0) for L2 cells. One worker runs in-process.
    """
    tasks, codes = [], set()
    for job in jobs:
        code, mode = int(job["code"]), job["mode"]
        if code in codes:
            raise ValueError(f"duplicate job code {code}")
        codes.add(code)
        if mode not in ("warm", "fresh"):
            raise ValueError(f"job {code}: unknown mode {mode!r}")
        if mode == "warm" and job.get("old") is None:
            raise ValueError(f"job {code}: a warm split needs old labels")
        old = job.get("old") if mode == "warm" else None
        tasks.append((code, mode, job["graph"], None if old is None else np.asarray(old, dtype=np.int64),
                      int(job["k"]), float(job["band_km"])))
    tasks.sort(key=lambda t: (-t[2].n, t[0]))
    if workers is None:
        workers = default_workers(per_worker_gb)
    workers = max(1, min(int(workers), len(tasks)))
    out = {}
    if workers <= 1:
        for task in tasks:
            code, labels = _run_job(task)
            out[code] = labels
    else:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(workers) as pool:
            for code, labels in pool.imap_unordered(_run_job, tasks, chunksize=1):
                out[code] = labels
    return {code: out[code] for code in sorted(out)}
