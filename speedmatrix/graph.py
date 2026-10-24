"""The directed turn graph of the matrix OSRM dataset, mapped onto the segment table.

``python -m speedmatrix.graph --osrm-base BASE --build-json FILE --segments DIR --out DIR``

The graph is stored at OSRM edge-based-node (EBN) level (Decision 1): each EBN owns a CSR slice of its directed real
segments in travel order, and turns join EBNs. OSRM v26 stores every interior node of a geometry twice; the
zero-length "node entry" between the copies carries the node's obstacle duration (signal, stop, barrier), which is
folded into the real segment travelled just before it (``seg_fixed_ds``, Decision 2). The build checks that the
segments add up to OSRM's own EBN durations and that every turn joins the head of its source to the tail of its
target, so a layout change in OSRM fails loudly here. Every segment maps to exactly one segment-table row; a node
pair with no row raises, a pair shared by several ways is resolved by a stated rule and counted.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:  # pragma: no cover - when started as a script
    sys.path.insert(0, str(_REPO))

try:
    from .geo import haversine_m
    from .osrmfiles import OsrmFormatError, check_build_json, read_dataset
    from .segments import SegmentTable, load_segments, pair_lookup
    from .speeds import way_free_flow_kmh
except ImportError:  # pragma: no cover - run as a script
    from speedmatrix.geo import haversine_m
    from speedmatrix.osrmfiles import OsrmFormatError, check_build_json, read_dataset
    from speedmatrix.segments import SegmentTable, load_segments, pair_lookup
    from speedmatrix.speeds import way_free_flow_kmh

GRAPH_FORMAT = 1
_CHUNK = 1 << 22
_EXAMPLES = 5
_ARRAYS = ("node_osm", "node_lon_e6", "node_lat_e6",
           "seg_tail", "seg_head", "seg_len_m", "seg_fixed_ds", "seg_osrm_ds", "seg_row", "seg_class", "seg_highway",
           "ebn_offsets", "ebn_geometry", "ebn_forward", "ebn_tiny",
           "turn_offsets", "turn_dst", "turn_ds")


class GraphError(ValueError):
    """The OSRM dataset and the segment table do not make one consistent graph."""


@dataclass
class TurnGraph:
    node_osm: np.ndarray        # int64 (N,), sorted
    node_lon_e6: np.ndarray     # int32 (N,)
    node_lat_e6: np.ndarray     # int32 (N,)
    seg_tail: np.ndarray        # int32 (S,) node index
    seg_head: np.ndarray        # int32 (S,) node index
    seg_len_m: np.ndarray       # float32 (S,)
    seg_fixed_ds: np.ndarray    # uint16 (S,) folded node entries
    seg_osrm_ds: np.ndarray     # uint32 (S,) OSRM's own free-flow duration
    seg_row: np.ndarray         # int32 (S,) SegmentTable row
    seg_class: np.ndarray       # int8 (S,) index into ROAD_CLASSES
    seg_highway: np.ndarray     # int16 (S,) index into meta["highways"]
    ebn_offsets: np.ndarray     # int64 (E+1,)
    ebn_geometry: np.ndarray    # int32 (E,)
    ebn_forward: np.ndarray     # bool (E,)
    ebn_tiny: np.ndarray        # bool (E,)
    turn_offsets: np.ndarray    # int64 (E+1,)
    turn_dst: np.ndarray        # int32 (T,)
    turn_ds: np.ndarray         # uint16 (T,)
    meta: dict

    @property
    def n_nodes(self) -> int:
        return len(self.node_osm)

    @property
    def n_segments(self) -> int:
        return len(self.seg_tail)

    @property
    def n_ebn(self) -> int:
        return len(self.ebn_geometry)

    @property
    def n_turns(self) -> int:
        return len(self.turn_dst)

    @property
    def node_lat(self) -> np.ndarray:
        return self.node_lat_e6 / 1e6

    @property
    def node_lon(self) -> np.ndarray:
        return self.node_lon_e6 / 1e6

    @property
    def seg_ebn(self) -> np.ndarray:
        return np.repeat(np.arange(self.n_ebn, dtype=np.int32), np.diff(self.ebn_offsets))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self.meta).encode("utf-8")).hexdigest()


def _canonical(meta) -> str:
    return json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _expand(ds):
    """Per-segment arrays of every EBN in travel order (Task 1 layout); frees the geometry arrays of ``ds``."""
    entries = int(ds.geo_index[-1])
    if entries >= 2 ** 31:
        raise GraphError(f".geometry holds {entries} entries, more than int32 indexing allows")
    b = ds.geo_index[ds.ebn_geometry].astype(np.int32)
    m = ((ds.geo_index[ds.ebn_geometry + 1] - b) // 2).astype(np.int32)
    if len(m) and int(m.min()) < 1:
        raise OsrmFormatError(f".ebg_nodes: EBN {int(np.argmax(m < 1))} lies on a geometry without segments")
    offsets = np.zeros(len(m) + 1, dtype=np.int64)
    np.cumsum(m, out=offsets[1:])
    n = int(offsets[-1])
    if n >= 2 ** 31:
        raise GraphError(f"{n} segments, more than int32 indexing allows")
    fwd = np.repeat(ds.ebn_forward, m)
    mm = np.repeat(m, m)
    k = np.arange(n, dtype=np.int32)
    k -= np.repeat(offsets[:-1].astype(np.int32), m)
    j = np.where(fwd, k, mm - 1 - k)                       # real segment j of the geometry
    del k
    pos = np.repeat(b, m)
    pos += 2 * j                                           # entry 2j: node j of the geometry
    tail = ds.geo_nodes[pos + ~fwd]                        # forward 2j -> 2j+1, reverse 2j+1 -> 2j
    head = ds.geo_nodes[pos + fwd]
    ds.geo_nodes = None
    fd, rd = ds.geo_fwd_ds, ds.geo_rev_ds
    osrm = np.where(fwd, fd[pos + 1], rd[pos])                              # fd[b+2j+1] / rd[b+2j]
    # node entry folded at the segment's head: forward fd[b+2j+2] for j <= m-2, reverse rd[b+2j-1] for j >= 1
    folds = np.where(fwd, j <= mm - 2, j >= 1)
    del j, mm
    at = np.where(folds, np.where(fwd, pos + 2, pos - 1), 0)
    del pos
    fixed = np.where(folds, np.where(fwd, fd[at], rd[at]), 0).astype(np.uint32)
    del at, folds, fwd
    ds.geo_fwd_ds = ds.geo_rev_ds = None
    return offsets, tail, head, osrm.astype(np.uint32, copy=False), fixed


def _check_ebn_identity(offsets, osrm, fixed, ebn_duration):
    if len(ebn_duration) == 0:
        return
    total = np.add.reduceat(osrm, offsets[:-1], dtype=np.int64)
    total += np.add.reduceat(fixed, offsets[:-1], dtype=np.int64)
    bad = total != ebn_duration.astype(np.int64)
    if bad.any():
        e = int(np.argmax(bad))
        raise GraphError(f"enw identity: {int(bad.sum())} EBNs differ, e.g. EBN {e}: segments sum to "
                         f"{int(total[e])} ds, .enw says {int(ebn_duration[e])} ds")


def _turns(ds, offsets, tail, head):
    order = np.argsort(ds.turn_src, kind="stable")
    src, dst = ds.turn_src[order], ds.turn_dst[order]
    turn_ds, total = ds.turn_ds[order], ds.turn_total_ds[order]
    del order
    expected = ds.ebn_duration_ds[src].astype(np.int64) + turn_ds
    bad = total.astype(np.int64) != expected
    if bad.any():
        t = int(np.argmax(bad))
        raise GraphError(f"turn duration identity: {int(bad.sum())} turns differ, e.g. EBN {int(src[t])} -> "
                         f"{int(dst[t])}: total {int(total[t])} ds, EBN duration + turn = {int(expected[t])} ds")
    del expected, bad, total
    bad = head[offsets[1:][src] - 1] != tail[offsets[:-1][dst]]
    if bad.any():
        t = int(np.argmax(bad))
        raise GraphError(f"turn connection identity: {int(bad.sum())} turns do not join the head of their source "
                         f"to the tail of their target, e.g. EBN {int(src[t])} -> {int(dst[t])}")
    turn_offsets = np.zeros(len(offsets), dtype=np.int64)
    np.cumsum(np.bincount(src, minlength=len(offsets) - 1), out=turn_offsets[1:])
    return turn_offsets, dst.astype(np.int32), turn_ds.astype(np.uint16)


def _nodes(ds, tail, head):
    """Node table of the nodes used, sorted by OSM id; tail and head remapped in place to node indices."""
    used = np.zeros(len(ds.nbg_osm), dtype=bool)
    used[tail] = True
    used[head] = True
    nbg = np.flatnonzero(used)
    del used
    node_osm, first, inverse = np.unique(ds.nbg_osm[nbg], return_index=True, return_inverse=True)
    lon, lat = ds.nbg_lon_e6[nbg], ds.nbg_lat_e6[nbg]
    node_lon, node_lat = lon[first], lat[first]
    moved = (lon != node_lon[inverse]) | (lat != node_lat[inverse])
    if moved.any():
        k = int(np.argmax(moved))
        raise GraphError(f"{int(moved.sum())} OSRM nodes share an OSM id with different coordinates, "
                         f"e.g. OSM node {int(ds.nbg_osm[nbg[k]])}")
    remap = np.full(len(ds.nbg_osm), -1, dtype=np.int32)
    remap[nbg] = inverse.astype(np.int32)
    tail[:] = remap[tail]
    head[:] = remap[head]
    return node_osm.astype(np.int64), node_lon.astype(np.int32), node_lat.astype(np.int32)


def _lengths(node_lon_e6, node_lat_e6, tail, head):
    out = np.empty(len(tail), dtype=np.float32)
    for s in range(0, len(tail), _CHUNK):
        t, h = tail[s:s + _CHUNK], head[s:s + _CHUNK]
        out[s:s + _CHUNK] = haversine_m(node_lat_e6[t] / 1e6, node_lon_e6[t] / 1e6,
                                        node_lat_e6[h] / 1e6, node_lon_e6[h] / 1e6)
    return out


def _rows(table: SegmentTable, node_osm, tail, head, seg_len_m, osrm):
    """Segment-table row per segment (see the module docstring); returns (rows int32, ambiguous count)."""
    way_kmh = way_free_flow_kmh(table)
    rows = np.empty(len(tail), dtype=np.int32)
    missing, examples, ambiguous = 0, [], 0
    for s in range(0, len(tail), _CHUNK):
        a, b = node_osm[tail[s:s + _CHUNK]], node_osm[head[s:s + _CHUNK]]
        start, count = pair_lookup(table, a, b)
        none = np.flatnonzero(count == 0)
        if len(none):
            missing += len(none)
            examples += [(int(a[i]), int(b[i])) for i in none[:_EXAMPLES - len(examples)]]
            continue
        part = table.pair_order[start].astype(np.int32)
        many = np.flatnonzero(count > 1)
        if len(many):
            ambiguous += len(many)
            reps = count[many]
            group = np.repeat(np.arange(len(many)), reps)
            first = np.zeros(len(many), dtype=np.int64)
            np.cumsum(reps[:-1], out=first[1:])
            cand = table.pair_order[np.repeat(start[many], reps) + np.arange(len(group)) - first[group]]
            seg = many[group] + s
            kmh = way_kmh[table.way[cand]].astype(np.float64)
            with np.errstate(invalid="ignore", divide="ignore"):
                miss = np.abs(36.0 * seg_len_m[seg] / kmh - osrm[seg])
            miss[~np.isfinite(miss)] = np.inf
            # candidates of a pair come in way-id order and lexsort is stable: ties go to the lowest way id
            order = np.lexsort((miss, group))
            part[many] = cand[order[first]]
        rows[s:s + _CHUNK] = part
    if missing:
        shown = ", ".join(f"({x}, {y})" for x, y in examples)
        raise GraphError(f"{missing} segments have no segment-table row (OSM node pairs), e.g. {shown}")
    return rows, ambiguous


def build_graph(base, build_json: Path, table: SegmentTable) -> TurnGraph:
    """The turn graph of the OSRM dataset ``base`` (path without suffix) mapped onto ``table``."""
    doc = check_build_json(build_json)
    pbf_sha = doc["input"].get("pbf_sha256")
    if table.meta.get("pbf_sha256") != pbf_sha:
        raise GraphError("segment table built from another PBF")
    ds = read_dataset(base)
    if ds.restricted_edges:
        raise GraphError(f"{ds.restricted_edges} restricted-access edges: the matrix profile must block them")
    offsets, tail, head, osrm, fixed = _expand(ds)
    _check_ebn_identity(offsets, osrm, fixed, ds.ebn_duration_ds)
    if len(fixed) and int(fixed.max()) > np.iinfo(np.uint16).max:
        raise GraphError(f"a node entry holds {int(fixed.max())} ds, more than uint16 seg_fixed_ds allows")
    fixed = fixed.astype(np.uint16)
    turn_offsets, turn_dst, turn_ds = _turns(ds, offsets, tail, head)
    ds.turn_src = ds.turn_dst = ds.turn_ds = ds.turn_total_ds = ds.ebn_duration_ds = None
    node_osm, node_lon, node_lat = _nodes(ds, tail, head)
    ds.nbg_osm = ds.nbg_lon_e6 = ds.nbg_lat_e6 = None
    seg_len_m = _lengths(node_lon, node_lat, tail, head)
    rows, ambiguous = _rows(table, node_osm, tail, head, seg_len_m, osrm)
    way = table.way[rows]
    seg_class = table.road_class[way].astype(np.int8)
    codes, inverse = np.unique(table.highway[way], return_inverse=True)
    del way
    names = [table.strings[int(c)] if c >= 0 else "" for c in codes]
    rank = np.argsort(np.argsort(names, kind="stable"))
    seg_highway = rank[np.asarray(inverse).reshape(-1)].astype(np.int16)
    meta = {"format": GRAPH_FORMAT, "pbf_sha256": pbf_sha, "osrm_version": doc["input"]["osrm_version"],
            "segments_sha256": table.sha256, "highways": sorted(names),
            "counts": {"nodes": len(node_osm), "segments": len(tail), "ebn": len(ds.ebn_geometry),
                       "turns": len(turn_dst)},
            "ambiguous_pairs": int(ambiguous), "obstacle_entries": int(np.count_nonzero(fixed)),
            "restricted_edges": int(ds.restricted_edges)}
    return TurnGraph(node_osm=node_osm, node_lon_e6=node_lon, node_lat_e6=node_lat, seg_tail=tail, seg_head=head,
                     seg_len_m=seg_len_m, seg_fixed_ds=fixed, seg_osrm_ds=osrm, seg_row=rows, seg_class=seg_class,
                     seg_highway=seg_highway, ebn_offsets=offsets, ebn_geometry=ds.ebn_geometry,
                     ebn_forward=ds.ebn_forward, ebn_tiny=ds.ebn_tiny, turn_offsets=turn_offsets, turn_dst=turn_dst,
                     turn_ds=turn_ds, meta=meta)


def save_graph(g: TurnGraph, out_dir) -> None:
    """One ``<name>.npy`` per array plus ``meta.json``, written to ``<out_dir>.tmp`` and renamed into place."""
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_dir.with_name(out_dir.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    for name in _ARRAYS:
        np.save(tmp / f"{name}.npy", np.ascontiguousarray(getattr(g, name)))
    (tmp / "meta.json").write_text(json.dumps(g.meta, ensure_ascii=False, indent=1), encoding="utf-8")
    if out_dir.exists():
        shutil.rmtree(out_dir)          # fails on Windows while another process maps it; nothing is half-replaced
    tmp.rename(out_dir)


def load_graph(out_dir, mmap: bool = False) -> TurnGraph:
    """The graph saved by ``save_graph``; with ``mmap`` the arrays are read-only memory maps (drop them before
    deleting the directory on Windows)."""
    out_dir = Path(out_dir)
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    if meta.get("format") != GRAPH_FORMAT:
        raise GraphError(f"graph format {meta.get('format')} in {out_dir}; rebuild with this code "
                         f"(format {GRAPH_FORMAT})")
    arrays = {name: np.load(out_dir / f"{name}.npy", mmap_mode="r" if mmap else None) for name in _ARRAYS}
    return TurnGraph(meta=meta, **arrays)


def segment_ds(g: TurnGraph, seg_kmh: np.ndarray) -> np.ndarray:
    """float64 (S,) deciseconds per segment at ``seg_kmh``: ``36 * seg_len_m / seg_kmh + seg_fixed_ds``."""
    seg_kmh = np.asarray(seg_kmh)
    if seg_kmh.shape != (g.n_segments,):
        raise ValueError(f"seg_kmh has shape {seg_kmh.shape}, the graph has {g.n_segments} segments")
    return 36.0 * g.seg_len_m.astype(np.float64) / seg_kmh + g.seg_fixed_ds


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m speedmatrix.graph", description=__doc__.splitlines()[0])
    ap.add_argument("--osrm-base", type=Path, required=True, help="dataset path without suffix (....osrm)")
    ap.add_argument("--build-json", type=Path, required=True)
    ap.add_argument("--segments", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    g = build_graph(args.osrm_base, args.build_json, load_segments(args.segments))
    save_graph(g, args.out)
    print(args.out)


if __name__ == "__main__":
    main()
