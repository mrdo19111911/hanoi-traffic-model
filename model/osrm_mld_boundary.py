"""Measure the cell boundaries of the OSRM MLD partition, to compare them with the gate counts of the cluster files.

Reads the tar containers of the local OSRM build (block names and dtypes as osrm-partition/customize v26.10.0 write
them, checked against the real files):
- .partition  /mld/multilevelpartition/level_data (272-byte LevelData), partition (u64 per edge-based node, cell ids
  packed in bit fields per level), cell_to_children (u32 prefix offsets of each cell's child range);
- .cells      /mld/cellstorage/cells (32-byte CellData), source_boundary, destination_boundary (u32 node ids),
  level_to_cell_offset (u64);
- .ebg_nodes  /common/ebg_node_data/nodes (12 bytes: geometry id with the forward bit 31, component, annotation);
- .geometry   /common/segment_data/index (u32 offsets) and nodes (u32 node-based node ids);
- .nbg_nodes  /common/nbn_data/coordinates (i32 lon, lat x 1e6);
- .mldgr      /mld/multilevelgraph/node_array (u32 first edge, n + 1) and edge_array (u32 target, u32 data).
An edge-based node is one direction of a road stretch; both directions share a geometry id, so they count as one
point. Boundary points are merged as in step 4a: points of the same cell pair under 100 m apart become one.
Writes reports/osrm_mld_boundary.json and reports/osrm_mld_cells.geojson. Measure only.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import tarfile
import time

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from clusters import write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "osrm/data/vietnam-latest.osrm"
TARGET = 22
COMPARE_CELLS = 8
CLOSE_FRAC = 0.25  # a level within 25% of the target cell count is "near enough"; otherwise cut the parent tree
MERGE_M = 100.0
TABLE_POINTS = 2000
TABLE_BLOCK = 100  # osrm-routed default --max-table-size
REFERENCE_G = {"natural": 350}
METRES_PER_DEGREE = 111320.0
CELL_DTYPE = np.dtype([("value_offset", "<u8"), ("source_boundary_offset", "<u8"), ("destination_boundary_offset", "<u8"),
                       ("num_source_nodes", "<u4"), ("num_destination_nodes", "<u4")])


def _members(path):
    with tarfile.open(path) as tar:
        return {m.name: (m.offset_data, m.size) for m in tar.getmembers()}


def _read(path, name, dtype, members=None):
    """A block as a numpy array of `dtype` elements (structs as sub-array dtypes), the count from <name>.meta.

    A missing block raises ValueError naming it.
    """
    members = members or _members(path)
    for needed in (name, name + ".meta"):
        if needed not in members:
            raise ValueError(f"{Path(path).name} has no block {needed}")
    offset, size = members[name + ".meta"]
    count = int(np.fromfile(path, dtype="<u8", count=1, offset=offset)[0])
    offset, size = members[name]
    dtype = np.dtype(dtype)
    if count * dtype.itemsize > size:
        raise ValueError(f"{Path(path).name}: block {name} holds {size} bytes, fewer than {count} x {dtype}")
    return np.fromfile(path, dtype=dtype, count=count, offset=offset)


def block_count(path, name):
    members = _members(path)
    if name + ".meta" not in members:
        raise ValueError(f"{Path(path).name} has no block {name}.meta")
    return int(np.fromfile(path, dtype="<u8", count=1, offset=members[name + ".meta"][0])[0])


def read_partition(path, nodes):
    """Levels, bit fields, child ranges and the packed cell ids of the first `nodes` edge-based nodes."""
    members = _members(path)
    raw = _read(path, "/mld/multilevelpartition/level_data", np.dtype((np.uint8, 272)), members).tobytes()
    if len(raw) < 272:
        raise ValueError(f"{Path(path).name}: level_data is {len(raw)} bytes, expected 272")
    num_level = int(np.frombuffer(raw[0:4], dtype="<u4")[0])
    levels = num_level - 1
    offsets = list(raw[4:4 + num_level])
    masks = np.frombuffer(raw[24:24 + 8 * 15], dtype="<u8")[:levels].tolist()
    child_offsets = np.frombuffer(raw[208:208 + 4 * 15], dtype="<u4")[:levels].tolist()
    packed = _read(path, "/mld/multilevelpartition/partition", "<u8", members)
    if len(packed) < nodes:
        raise ValueError(f"{Path(path).name}: partition has {len(packed)} entries for {nodes} edge-based nodes")
    to_children = _read(path, "/mld/multilevelpartition/cell_to_children", "<u4", members)
    part = {"levels": levels, "offsets": offsets, "masks": masks, "packed": packed[:nodes], "partition_entries": int(len(packed))}
    counts = {}
    for level in range(1, levels + 1):
        counts[level] = int(cell_ids(part, level).max()) + 1 if nodes else 0
    part["cell_counts"] = counts
    children = {}
    for level in range(2, levels + 1):
        start = child_offsets[level - 1]
        bounds = to_children[start:start + counts[level] + 1]
        children[level] = [list(range(int(a), int(b))) for a, b in zip(bounds, bounds[1:])]
    part["children"] = children
    return part


def cell_ids(part, level):
    """Cell id of every edge-based node at level (1 = finest)."""
    mask, shift = np.uint64(part["masks"][level - 1]), np.uint64(part["offsets"][level - 1])
    return ((part["packed"] & mask) >> shift).astype(np.int64)


def read_cells(path):
    members = _members(path)
    return {"cells": _read(path, "/mld/cellstorage/cells", CELL_DTYPE, members),
            "sources": _read(path, "/mld/cellstorage/source_boundary", "<u4", members),
            "destinations": _read(path, "/mld/cellstorage/destination_boundary", "<u4", members),
            "level_offsets": _read(path, "/mld/cellstorage/level_to_cell_offset", "<u8", members)}


def level_boundary(cells, level):
    """Source, destination and boundary (their union) node counts of one level, with boundary nodes per cell."""
    lo, hi = int(cells["level_offsets"][level - 1]), int(cells["level_offsets"][level])
    table = cells["cells"][lo:hi]
    src_parts, dst_parts, per_cell = [], [], []
    for row in table:
        s = cells["sources"][int(row["source_boundary_offset"]):int(row["source_boundary_offset"]) + int(row["num_source_nodes"])] \
            if row["num_source_nodes"] else np.zeros(0, dtype=np.uint32)
        d = cells["destinations"][int(row["destination_boundary_offset"]):int(row["destination_boundary_offset"]) + int(row["num_destination_nodes"])] \
            if row["num_destination_nodes"] else np.zeros(0, dtype=np.uint32)
        src_parts.append(s)
        dst_parts.append(d)
        per_cell.append(len(np.union1d(s, d)))
    sources = np.unique(np.concatenate(src_parts)) if src_parts else np.zeros(0, dtype=np.uint32)
    destinations = np.unique(np.concatenate(dst_parts)) if dst_parts else np.zeros(0, dtype=np.uint32)
    nodes = np.union1d(sources, destinations).astype(np.int64)
    per = np.array(per_cell) if per_cell else np.zeros(1)
    return {"cells": int(hi - lo), "sources": int(len(sources)), "destinations": int(len(destinations)),
            "boundary_ebg": int(len(nodes)), "nodes": nodes,
            "per_cell": {"min": int(per.min()), "median": float(np.median(per)), "max": int(per.max())}}


def read_ebg_points(base, nodes):
    """(geometry id per edge-based node, (lon, lat) per geometry): the middle node of each geometry."""
    ebg = _read(f"{base}.ebg_nodes", "/common/ebg_node_data/nodes", np.dtype(("<u4", 3)))[:nodes]
    geometry = (ebg[:, 0] & 0x7FFFFFFF).astype(np.int64)
    members = _members(f"{base}.geometry")
    index = _read(f"{base}.geometry", "/common/segment_data/index", "<u4", members).astype(np.int64)
    geo_nodes = _read(f"{base}.geometry", "/common/segment_data/nodes", "<u4", members)
    coords = _read(f"{base}.nbg_nodes", "/common/nbn_data/coordinates", np.dtype(("<i4", 2)))
    middle = geo_nodes[(index[:-1] + index[1:]) // 2]
    return geometry, coords[middle] / 1e6


def read_graph(path):
    members = _members(path)
    first = _read(path, "/mld/multilevelgraph/node_array", "<u4", members).astype(np.int64)
    targets = _read(path, "/mld/multilevelgraph/edge_array", np.dtype(("<u4", 2)), members)[:, 0].astype(np.int64)
    return {"first": first, "targets": targets}


def _edges_of(nodes, graph):
    """(source, target) of every graph edge leaving the given nodes."""
    first = graph["first"]
    starts, ends = first[nodes], first[nodes + 1]
    counts = ends - starts
    sources = np.repeat(nodes, counts)
    positions = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts) + np.repeat(starts, counts)
    return sources, graph["targets"][positions]


def merge_points(coords, pairs, metres=MERGE_M):
    """Group id per point: points of the same cell pair closer than `metres` share a group (single link)."""
    n = len(coords)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    lat = np.radians(coords[:, 1])
    xy = np.column_stack((coords[:, 0] * np.cos(lat), coords[:, 1])) * METRES_PER_DEGREE
    close = cKDTree(xy).query_pairs(metres * (1 - 1e-9), output_type="ndarray")
    if len(close):
        same = (pairs[close[:, 0], 0] == pairs[close[:, 1], 0]) & (pairs[close[:, 0], 1] == pairs[close[:, 1], 1])
        close = close[same]
    graph = coo_matrix((np.ones(len(close)), (close[:, 0], close[:, 1])) if len(close) else ([], ([], [])), shape=(n, n))
    return connected_components(graph, directed=False)[1]


def boundary_points(boundary, labels, geometry, points, graph, merge_m=MERGE_M):
    """Map boundary nodes to points: one item per (geometry, cell pair), merged per pair under merge_m metres."""
    sources, targets = _edges_of(np.asarray(boundary, dtype=np.int64), graph)
    cu, cv = labels[sources], labels[targets]
    cross = cu != cv
    geom = geometry[sources[cross]]
    lo, hi = np.minimum(cu[cross], cv[cross]), np.maximum(cu[cross], cv[cross])
    items = np.unique(np.column_stack((geom, lo, hi)), axis=0) if len(geom) else np.zeros((0, 3), dtype=np.int64)
    groups = merge_points(points[items[:, 0]], items[:, 1:], merge_m)
    first = {}
    for k, g in enumerate(groups.tolist()):
        first.setdefault(g, k)
    reps = np.array(sorted(first.values()), dtype=np.int64)
    pairs = {}
    for k in reps:
        key = f"{items[k, 1]}-{items[k, 2]}"
        pairs[key] = pairs.get(key, 0) + 1
    return {"items": int(len(items)), "boundary_points": int(len(np.unique(items[reps, 0]))) if len(reps) else 0,
            "point_pairs": int(len(reps)), "pairs": dict(sorted(pairs.items())),
            "points": [(int(items[k, 0]), int(items[k, 1]), int(items[k, 2])) for k in reps]}


def choose_level(cell_counts, target, close_frac=CLOSE_FRAC):
    """The level with the cell count nearest target, or a parent-tree cut when none is within close_frac of it."""
    level = min(cell_counts, key=lambda l: (abs(cell_counts[l] - target), -l))
    if abs(cell_counts[level] - target) <= close_frac * target:
        return {"level": level, "cells": cell_counts[level], "tree_cut": False}
    return {"level": None, "cells": None, "tree_cut": True}


def tree_frontier(part, target):
    """Cut the parent tree: start from the coarsest level with fewer cells than target, split parents into their
    children while that brings the count nearer target (fewest-children first, then lowest id).

    Returns (frontier label per node, {coarse_level, fine_level, cells, split}).
    """
    counts = part["cell_counts"]
    coarse = min((l for l in counts if counts[l] < target), default=None)
    if coarse is None or coarse == 1:
        raise ValueError(f"no level below {target} cells with a finer level under it")
    fine = coarse - 1
    children = part["children"][coarse]
    total, split = counts[coarse], []
    while True:
        options = [(abs(total + len(children[c]) - 1 - target), len(children[c]), c)
                   for c in range(counts[coarse]) if c not in split and len(children[c]) > 1]
        if not options:
            break
        best = min(options)
        if best[0] >= abs(total - target):
            break
        split.append(best[2])
        total += len(children[best[2]]) - 1
    coarse_ids, fine_ids = cell_ids(part, coarse), cell_ids(part, fine)
    is_split = np.zeros(counts[coarse], dtype=bool)
    is_split[split] = True
    raw = np.where(is_split[coarse_ids], counts[coarse] + fine_ids, coarse_ids)
    labels = np.unique(raw, return_inverse=True)[1].astype(np.int64)
    return labels, {"coarse_level": coarse, "fine_level": fine, "cells": int(labels.max()) + 1, "split": sorted(split)}


def frontier_boundary(cells, fine_level, labels, graph):
    """Boundary nodes of a tree cut: fine-level boundary nodes with a neighbour in another frontier cell."""
    nodes = level_boundary(cells, fine_level)["nodes"]
    sources, targets = _edges_of(nodes, graph)
    keep = np.unique(sources[labels[sources] != labels[targets]])
    return keep


def _check_map(build_json, edges_path):
    build = json.loads(Path(build_json).read_text(encoding="utf-8"))
    sha = build.get("input", {}).get("pbf_sha256")
    db = sqlite3.connect(f"{Path(edges_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        edges_sha = dict(db.execute("SELECT key, value FROM metadata")).get("pbf_sha256")
    finally:
        db.close()
    if not sha or sha != edges_sha:
        raise ValueError("osrm/build.json and big_road_edges.sqlite come from another map; rebuild before measuring")
    return sha, build.get("input", {}).get("osrm_version")


def _summary(labels, boundary, geometry, points, graph, merge_m):
    counts = np.bincount(labels)
    result = boundary_points(boundary, labels, geometry, points, graph, merge_m)
    return {"cells": int(len(counts)), "nodes_per_cell": {"min": int(counts.min()), "median": float(np.median(counts)),
                                                          "max": int(counts.max())},
            "boundary_ebg": int(len(boundary)), "boundary_ebg_half": int(len(boundary)) // 2, **result}


def single_block(points):
    """Block size that sends the whole points x points table in one request (needs --max-table-size >= points)."""
    return int(points)


def time_table(router, points, block=TABLE_BLOCK):
    """Snap the points by /nearest, then time one points x points /table in block x block requests."""
    from gate_matrix_experiment import blocked_table
    tick = time.perf_counter()
    snapped = [tuple(router.nearest(tuple(p))["location"]) for p in points]
    snap_s = time.perf_counter() - tick
    tick = time.perf_counter()
    table = blocked_table(router, snapped, snapped, block)
    table_s = time.perf_counter() - tick
    requests = math.ceil(len(points) / block) ** 2
    return {"points": len(points), "block": block, "requests": requests, "snap_s": round(snap_s, 2), "table_s": round(table_s, 2),
            "reachable_share": float(np.isfinite(table).mean())}


def run(base, build_json, edges_path, out, out_geojson, target=TARGET, compare_cells=COMPARE_CELLS, router=None,
        merge_m=MERGE_M, table_points=TABLE_POINTS, seed=0, single=False, log=lambda *args: None):
    started = time.monotonic()
    sha, version = _check_map(build_json, edges_path)
    nodes = block_count(f"{base}.ebg_nodes", "/common/ebg_node_data/nodes")
    part = read_partition(f"{base}.partition", nodes)
    cells = read_cells(f"{base}.cells")
    geometry, points = read_ebg_points(base, nodes)
    graph = read_graph(f"{base}.mldgr")
    log(f"{nodes} edge-based nodes, levels {part['cell_counts']} ({time.monotonic() - started:.0f} s)")
    levels, boundary_of = [], {}
    for level in range(1, part["levels"] + 1):
        info = level_boundary(cells, level)
        boundary_of[level] = info.pop("nodes")
        summary = _summary(cell_ids(part, level), boundary_of[level], geometry, points, graph, merge_m)
        summary.pop("points")
        levels.append({"level": level, **info, **{k: v for k, v in summary.items() if k not in ("boundary_ebg", "cells")}})
        log(f"level {level}: {info['cells']} cells, boundary_ebg {info['boundary_ebg']}, points {summary['boundary_points']}")

    def compare(wanted):
        choice = choose_level(part["cell_counts"], wanted)
        if choice["tree_cut"]:
            labels, cut = tree_frontier(part, wanted)
            boundary = frontier_boundary(cells, cut["fine_level"], labels, graph)
            how = {"tree_cut": True, **cut}
        else:
            labels, boundary = cell_ids(part, choice["level"]), boundary_of[choice["level"]]
            how = {"tree_cut": False, "level": choice["level"]}
        summary = _summary(labels, boundary, geometry, points, graph, merge_m)
        return {**how, **summary}, labels

    target_result, target_labels = compare(target)
    other_result, _ = compare(compare_cells)
    features = []
    seen = set()
    for geom, a, b in target_result.pop("points"):
        if geom in seen:
            continue
        seen.add(geom)
        lon, lat = points[geom]
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]},
                         "properties": {"geometry_id": geom, "cell": a, "other_cell": b}})
    other_result.pop("points")
    report = {"levels": levels,
              "comparison": {"target": target_result, f"cells_{compare_cells}": other_result, "reference_G": REFERENCE_G,
                             "merge_m": merge_m},
              "metadata": {"pbf_sha256": sha, "osrm_version": version, "edge_based_nodes": nodes,
                           "partition_entries": part["partition_entries"], "point": "middle node of each geometry",
                           "built_at": datetime.now(timezone.utc).isoformat()}}
    if router is not None:
        reason = router.unavailable(sha) if hasattr(router, "unavailable") else None
        if reason:
            raise RuntimeError(f"Cannot time /table: {reason}")
        rng = np.random.default_rng(seed)
        sample = points[rng.choice(len(points), size=table_points, replace=False)]
        report["table"] = time_table(router, sample)
        log(f"/table {report['table']}")
        if single:
            report["table_single"] = time_table(router, sample, block=single_block(len(sample)))
            log(f"/table single {report['table_single']}")
    report["metadata"]["runtime_s"] = round(time.monotonic() - started, 1)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, report)
    write_json_atomic(out_geojson, {"type": "FeatureCollection", "features": features,
                                    "properties": {"cells": target_result["cells"], "pbf_sha256": sha}})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, default=BASE)
    parser.add_argument("--build", type=Path, default=ROOT / "osrm/build.json")
    parser.add_argument("--edges", type=Path, default=ROOT / "osrm/data/big_road_edges.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/osrm_mld_boundary.json")
    parser.add_argument("--geojson", type=Path, default=ROOT / "reports/osrm_mld_cells.geojson")
    parser.add_argument("--target", type=int, default=TARGET)
    parser.add_argument("--compare-cells", type=int, default=COMPARE_CELLS)
    parser.add_argument("--table-points", type=int, default=TABLE_POINTS)
    parser.add_argument("--no-table", action="store_true")
    parser.add_argument("--single-table", action="store_true",
                        help="Also time the table as one request (osrm-routed --max-table-size must allow it)")
    parser.add_argument("--timeout", type=float, default=10, help="Seconds to wait for each OSRM request")
    args = parser.parse_args()
    router = None
    if not args.no_table:
        from gate_matrix_experiment import RouteRouter
        router = RouteRouter(timeout=args.timeout)
    report = run(args.base, args.build, args.edges, args.out, args.geojson, args.target, args.compare_cells, router,
                 table_points=args.table_points, single=args.single_table, log=lambda *a: print(*a, flush=True))
    print(json.dumps({k: v for k, v in report.items() if k != "levels"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
