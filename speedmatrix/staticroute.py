"""Single-slot reference router: shortest travel time between segment midpoints on the turn graph.

Dijkstra runs at EBN (edge-based node) level, in deciseconds: ``dist(v)`` is the time at the end of EBN ``v`` and an
arc u -> w costs ``turn_ds + W(w)``, with ``W`` the EBN weight (sum of its segments at the given speeds). Each source
point is a virtual node linked to the EBN of its segment and to the EBN of its twin (the same physical segment in the
other direction), at the time from the midpoint to the end of that EBN. A target is reached through the same two
EBNs: arrival = ``dist(v)`` minus the time from the target midpoint to the end of ``v``. A "half" segment is half of
its ``seg_ds`` (the folded node entry included).

Points on one geometry (either direction) give ``NaN``: the router does not model movement along a way. Unreachable
points give ``inf``. One ``dijkstra`` call per source keeps only the target columns, so memory does not grow with
``P * E``. Results are in seconds.
"""
import multiprocessing
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

try:
    from .graph import TurnGraph, load_graph
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.graph import TurnGraph, load_graph

# Private memory of one parallel_table worker on a nationwide-size graph: measured 2.26 GB peak (2026-10-24, a
# synthetic turn graph with the E = 10.1M EBNs and T = 19.3M turns of the 2026-10-21 build, one table() call):
# each worker builds its own CSR (float64 data, int32 indices) and scipy's dijkstra allocates a heap node per
# vertex. The mapped graph arrays are file-backed and shared, so they do not count. Task 12 re-measures it.
PER_WORKER_GB = 2.5


def ebn_weights(g: TurnGraph, seg_ds: np.ndarray) -> np.ndarray:
    """float64 (E,) deciseconds per EBN: the sum of its segments' ``seg_ds``."""
    seg_ds = np.asarray(seg_ds, dtype=np.float64)
    if seg_ds.shape != (g.n_segments,):
        raise ValueError(f"seg_ds has shape {seg_ds.shape}, the graph has {g.n_segments} segments")
    if g.n_ebn == 0:
        return np.zeros(0, dtype=np.float64)
    return np.add.reduceat(seg_ds, np.asarray(g.ebn_offsets[:-1]), dtype=np.float64)


class StaticRouter:
    def __init__(self, g: TurnGraph, seg_ds: np.ndarray):
        self.g = g
        self.seg_ds = np.asarray(seg_ds, dtype=np.float64)
        self.weight = ebn_weights(g, self.seg_ds)
        self._offsets = np.asarray(g.ebn_offsets)
        key = np.asarray(g.ebn_geometry).astype(np.int64) * 2 + np.asarray(g.ebn_forward)
        order = np.argsort(key, kind="stable")          # stable: the lowest EBN id comes first among equals
        sorted_key = key[order]
        pos = np.minimum(np.searchsorted(sorted_key, key ^ 1, side="left"), max(len(key) - 1, 0))
        found = (sorted_key[pos] == (key ^ 1)) if len(key) else np.zeros(0, dtype=bool)
        self._twin_ebn = np.where(found, order[pos], -1).astype(np.int32)
        del key, order, sorted_key, pos, found

    def _ebn_of(self, seg) -> np.ndarray:
        return np.searchsorted(self._offsets, np.asarray(seg), side="right") - 1

    def twin(self, seg: int) -> int:
        """The same physical segment in the other direction, or -1 (a one-way street)."""
        seg = int(seg)
        e = int(self._ebn_of(seg))
        e2 = int(self._twin_ebn[e])
        if e2 < 0:
            return -1
        m = int(self._offsets[e + 1] - self._offsets[e])
        if int(self._offsets[e2 + 1] - self._offsets[e2]) != m:
            return -1
        return int(self._offsets[e2]) + m - 1 - (seg - int(self._offsets[e]))

    def _to_end(self, seg: int, e: int) -> float:
        """Deciseconds from the midpoint of ``seg`` to the end of its EBN ``e``."""
        return 0.5 * self.seg_ds[seg] + float(self.seg_ds[seg + 1:int(self._offsets[e + 1])].sum())

    def _points(self, segs):
        """EBN and midpoint-to-end time of each point's own EBN and of its twin (EBN -1 when there is none)."""
        segs = np.asarray(segs, dtype=np.int64)
        n = len(segs)
        e1 = self._ebn_of(segs).astype(np.int64)
        e2 = np.full(n, -1, dtype=np.int64)
        c1 = np.empty(n)
        c2 = np.full(n, np.inf)
        for i, s in enumerate(segs):
            c1[i] = self._to_end(int(s), int(e1[i]))
            t = self.twin(int(s))
            if t >= 0:
                e2[i] = int(self._ebn_of(t))
                c2[i] = self._to_end(t, int(e2[i]))
        return e1, c1, e2, c2

    def table(self, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
        """float64 (P, Q) seconds between segment midpoints; inf unreachable, NaN on one geometry."""
        g = self.g
        src = np.asarray(src, dtype=np.int64)
        dst = np.asarray(dst, dtype=np.int64)
        for name, a in (("src", src), ("dst", dst)):
            if len(a) and (int(a.min()) < 0 or int(a.max()) >= g.n_segments):
                raise ValueError(f"{name} holds a segment index outside 0..{g.n_segments - 1}")
        P, Q, E = len(src), len(dst), g.n_ebn
        out = np.empty((P, Q), dtype=np.float64)
        if P == 0 or Q == 0:
            return out
        se1, sc1, se2, sc2 = self._points(src)
        de1, dc1, de2, dc2 = self._points(dst)
        # one CSR: the turn arcs, then one virtual row per source with one or two links
        links = np.where(se2 >= 0, 2, 1)
        link_to = np.empty(int(links.sum()), dtype=np.int32)
        link_cost = np.empty(len(link_to))
        at = np.concatenate(([0], np.cumsum(links)))
        link_to[at[:-1]] = se1
        link_cost[at[:-1]] = sc1
        has = np.flatnonzero(se2 >= 0)
        link_to[at[has] + 1] = se2[has]
        link_cost[at[has] + 1] = sc2[has]
        turn_dst = np.asarray(g.turn_dst)
        data = np.concatenate((np.asarray(g.turn_ds, dtype=np.float64) + self.weight[turn_dst], link_cost))
        indices = np.concatenate((turn_dst, link_to)).astype(np.int32)
        indptr = np.concatenate((np.asarray(g.turn_offsets), g.turn_offsets[-1] + at[1:]))
        graph = csr_matrix((data, indices, indptr), shape=(E + P, E + P))
        geo = np.asarray(g.ebn_geometry)
        same = geo[se1][:, None] == geo[de1][None, :]
        d2 = np.where(de2 >= 0, de2, 0)
        for p in range(P):
            d = dijkstra(graph, directed=True, indices=E + p)
            arrive = d[de1] - dc1
            via_twin = np.where(de2 >= 0, d[d2] - dc2, np.inf)
            out[p] = np.minimum(arrive, via_twin) / 10.0
        out[same] = np.nan
        return out


_ROUTER = None


def _init_worker(graph_dir, seg_ds_file):
    global _ROUTER
    _ROUTER = StaticRouter(load_graph(graph_dir, mmap=True), np.load(seg_ds_file, mmap_mode="r"))


def _work(args):
    src, dst = args
    return _ROUTER.table(src, dst)


def parallel_table(graph_dir: Path, seg_ds_file: Path, src, dst, workers: int) -> np.ndarray:
    """``StaticRouter.table`` with the sources split into chunks over ``workers`` processes.

    Each worker maps the graph read-only (``load_graph(mmap=True)``); ``PER_WORKER_GB`` is its memory budget.
    """
    src = np.asarray(src, dtype=np.int64)
    dst = np.asarray(dst, dtype=np.int64)
    if len(src) == 0 or len(dst) == 0:
        return np.empty((len(src), len(dst)), dtype=np.float64)
    if workers <= 1:
        return StaticRouter(load_graph(graph_dir, mmap=True), np.load(seg_ds_file, mmap_mode="r")).table(src, dst)
    chunks = np.array_split(src, min(len(src), workers * 4))
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(workers, initializer=_init_worker, initargs=(str(graph_dir), str(seg_ds_file))) as pool:
        parts = pool.map(_work, [(c, dst) for c in chunks])
    return np.vstack(parts)
