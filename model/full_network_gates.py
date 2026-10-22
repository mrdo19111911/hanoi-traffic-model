"""Step 4a (measure only): label the whole car network by natural_spectral sub-cluster and count the gates it needs.

Every car-routable way of the OSM file (car rules of natural_barriers.py) becomes edges between adjacent nodes,
weighted by free-flow time at the car profile's default speed (ferries 5 km/h); edges crossing a main river are
dropped. A multi-source Dijkstra from the labelled A junctions of natural_spectral gives every node the sub-cluster
of the quickest junction (-1 when none is reachable). Edges whose ends differ in label inside one river region are
spectral boundary crossings of the whole network; A edges are already gates and are left out. Crossings of the same
sub-cluster pair less than 100 m apart are merged. The gate-to-gate /table of the resulting gate set is timed on
local OSRM (sampled rows, extrapolated, when the set is large). Writes reports/full_network_gates.json; changes no
partition and runs no experiment.
"""
import argparse
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

import numpy as np
import osmium
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from gate_matrix_experiment import BLOCK, TableRouter, blocked_table
from natural_barriers import car_routable
from osm_classes import DEFAULT_SPEEDS_KMH
from spectral_clusters import _RiverIndex, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
FERRY_KMH = 5.0  # car.lua route_speeds.ferry
MERGE_M = 100.0
MAX_FULL_TABLE = 3000
SAMPLE_ROWS = 300
SLOTS = 96
METRES_PER_DEGREE = 111320.0
MIN_SECONDS = 1e-3  # csgraph drops explicit zeros, so co-located nodes keep a tiny weight


def _key(lon, lat):
    return (round(lon * 1e6), round(lat * 1e6))


def _sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def merge_crossings(points, pairs, metres=MERGE_M):
    """Group id per crossing: crossings of the same sub-cluster pair closer than `metres` share a group (single link)."""
    groups = [-1] * len(points)
    by_pair = defaultdict(list)
    for index, pair in enumerate(pairs):
        by_pair[tuple(pair)].append(index)
    next_id = 0
    for pair in sorted(by_pair):
        members = by_pair[pair]
        pts = np.array([points[i] for i in members], dtype=float)
        scale = np.array([math.cos(math.radians(float(pts[:, 1].mean()))), 1.0]) * METRES_PER_DEGREE
        parent = list(range(len(members)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        if len(members) > 1:
            for i, j in cKDTree(pts * scale).query_pairs(metres * (1 - 1e-9)):
                parent[find(i)] = find(j)
        local = {}
        for k, i in enumerate(members):
            root = find(k)
            if root not in local:
                local[root] = next_id
                next_id += 1
            groups[i] = local[root]
    return groups


def time_gate_table(points, router, max_full=MAX_FULL_TABLE, sample_rows=SAMPLE_ROWS, block=BLOCK):
    """Seconds for one G x G /table: measured in full up to max_full gates, else on evenly spaced rows, scaled up."""
    calls = {"n": 0}

    class Counting:
        def durations(self, sources, destinations):
            calls["n"] += 1
            return router.durations(sources, destinations)

    points = [tuple(p) for p in points]
    n = len(points)
    if n <= max_full:
        rows, mode = points, "full"
    else:
        rows, mode = [points[int(i)] for i in np.linspace(0, n - 1, sample_rows).round().astype(int)], "sampled"
    tick = time.perf_counter()
    blocked_table(Counting(), rows, points, block)
    measured = time.perf_counter() - tick
    fraction = len(rows) / n if n else 1.0
    estimated = measured / fraction if fraction else 0.0
    return {"mode": mode, "gates": n, "rows": len(rows), "requests": calls["n"], "row_fraction": fraction,
            "measured_s": round(measured, 3), "estimated_s": estimated, "slots_96_s": SLOTS * estimated}


class _Collector:
    """Edges of car-routable ways as compact arrays, the OSM ids of the source junctions, river drops."""

    def __init__(self, rivers, source_keys):
        self.rivers, self.source_keys = rivers, source_keys
        self.a, self.b, self.way = array("q"), array("q"), array("q")
        self.sec, self.hw = array("f"), array("b")
        self.highways = sorted(DEFAULT_SPEEDS_KMH) + ["ferry"]
        self.code = {h: i for i, h in enumerate(self.highways)}
        self.sources, self.dropped, self.ways = {}, 0, 0
        self.dropped_nodes = array("q")
        cell = rivers.cell
        self.river_cells = set(rivers.grid)
        self.cell = cell

    def way_(self, way):
        tags = way.tags
        if not car_routable(tags):
            return
        ferry = tags.get("route") == "ferry"
        highway = "ferry" if ferry else tags.get("highway")
        kmh = FERRY_KMH if ferry else DEFAULT_SPEEDS_KMH[highway]
        code = self.code[highway]
        nodes = [(n.ref, n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(nodes) < 2:
            return
        self.ways += 1
        for node_id, lon, lat in nodes:
            key = _key(lon, lat)
            if key in self.source_keys and key not in self.sources:
                self.sources[key] = node_id
        size = self.cell
        for (ra, xa, ya), (rb, xb, yb) in zip(nodes, nodes[1:]):
            if ra == rb:
                continue
            ca = (math.floor(xa / size), math.floor(ya / size))
            cb = (math.floor(xb / size), math.floor(yb / size))
            if (ca in self.river_cells or cb in self.river_cells or ca != cb) and self.rivers.crosses((xa, ya), (xb, yb)):
                self.dropped += 1
                self.dropped_nodes.extend((ra, rb))
                continue
            km = math.hypot((xb - xa) * math.cos(math.radians((ya + yb) / 2)), yb - ya) * METRES_PER_DEGREE / 1000
            self.a.append(ra)
            self.b.append(rb)
            self.way.append(way.id)
            self.sec.append(max(km / kmh * 3600, MIN_SECONDS))
            self.hw.append(code)


def _a_pairs(edges_path, pairs):
    """The (a, b) pairs, a < b, that are A-network edges in big_road_edges.sqlite."""
    db = sqlite3.connect(f"{Path(edges_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        db.execute("CREATE TEMP TABLE wanted(a INTEGER, b INTEGER)")
        db.executemany("INSERT INTO wanted VALUES (?, ?)", pairs)
        return {(a, b) for a, b in db.execute("SELECT DISTINCT w.a, w.b FROM wanted w JOIN edges e ON e.a = w.a AND e.b = w.b")}
    finally:
        db.close()


def survey_gates(pbf, partition, rivers_doc, edges_path, merge_m=MERGE_M, index_file=None, keep_node_labels=False,
                 log=lambda *args: None):
    """Label the car network by sub-cluster and count the spectral boundary crossings; returns {report, crossings, ...}.

    Node locations go to a sparse_file_array index at index_file (in memory when None). libosmium keeps that file
    open until the process ends, so it is replaced at the start of the next run rather than deleted here.
    """
    started = time.monotonic()
    rivers = _RiverIndex(rivers_doc.get("features", []))
    source_keys = {_key(lon, lat): int(c) for lon, lat, c in partition["node_labels"] if int(c) >= 0}
    parents = {int(c): int(p) for c, p in partition.get("parents", {}).items()}
    collector = _Collector(rivers, source_keys)
    if index_file is not None:
        Path(index_file).unlink(missing_ok=True)
    index = osmium.index.create_map(f"sparse_file_array,{index_file}" if index_file else "sparse_mem_array")
    try:
        processor = osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY).with_locations(index) \
            .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        for way in processor:
            collector.way_(way)
        log(f"{collector.ways} car ways, {len(collector.a)} edges, {collector.dropped} dropped at main rivers "
            f"({time.monotonic() - started:.0f} s)")

        a = np.frombuffer(collector.a, dtype=np.int64)
        b = np.frombuffer(collector.b, dtype=np.int64)
        sec = np.frombuffer(collector.sec, dtype=np.float32).astype(np.float64)
        hw = np.frombuffer(collector.hw, dtype=np.int8)
        ways = np.frombuffer(collector.way, dtype=np.int64)
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        # Nodes reached only over a main river stay in, unreachable.
        node_ids = np.unique(np.concatenate([lo, hi, np.frombuffer(collector.dropped_nodes, dtype=np.int64)]))
        il, ih = np.searchsorted(node_ids, lo), np.searchsorted(node_ids, hi)
        # One edge per node pair, the quickest; csgraph would sum duplicates.
        order = np.lexsort((sec, ih, il))
        il, ih, sec, hw, ways, lo, hi = il[order], ih[order], sec[order], hw[order], ways[order], lo[order], hi[order]
        first = np.ones(len(il), dtype=bool)
        first[1:] = (il[1:] != il[:-1]) | (ih[1:] != ih[:-1])
        il, ih, sec, hw, ways, lo, hi = il[first], ih[first], sec[first], hw[first], ways[first], lo[first], hi[first]
        n = len(node_ids)
        graph = coo_matrix((sec, (il, ih)), shape=(n, n)).tocsr()

        source_nodes = np.array(sorted({int(np.searchsorted(node_ids, osm)) for osm in collector.sources.values()}), dtype=np.int64)
        label_of_source = np.full(n, -1, dtype=np.int64)
        for key, osm in collector.sources.items():
            label_of_source[int(np.searchsorted(node_ids, osm))] = source_keys[key]
        if len(source_nodes):
            _, _, nearest = dijkstra(graph, directed=False, indices=source_nodes, min_only=True, return_predecessors=True)
            labels = np.where(nearest >= 0, label_of_source[np.maximum(nearest, 0)], -1)
        else:
            labels = np.full(n, -1, dtype=np.int64)
        log(f"{n} nodes labelled, {int((labels < 0).sum())} unreachable ({time.monotonic() - started:.0f} s)")

        la, lb = labels[il], labels[ih]
        parent_of = np.full(max(parents, default=-1) + 2, -1, dtype=np.int64)
        for c, p in parents.items():
            parent_of[c] = p
        pa, pb = parent_of[la], parent_of[lb]
        differ = (la >= 0) & (lb >= 0) & (la != lb)
        same_region = differ & (pa == pb) & (pa >= 0)
        cross_region = differ & ~same_region
        candidates = np.flatnonzero(same_region)
        on_a = _a_pairs(edges_path, [(int(lo[e]), int(hi[e])) for e in candidates])
        is_a = np.array([(int(lo[e]), int(hi[e])) in on_a for e in candidates], dtype=bool)
        boundary = candidates[~is_a]
        a_boundary = candidates[is_a]

        def location(osm_id):
            loc = index.get(int(osm_id))
            return (round(loc.lon, 7), round(loc.lat, 7))

        crossings = []
        for e in boundary:
            pu, pv = location(lo[e]), location(hi[e])
            cu, cv = int(la[e]), int(lb[e])
            if cu > cv:
                (cu, pu), (cv, pv) = (cv, pv), (cu, pu)
            crossings.append({"pair": [cu, cv], "gates": [list(pu), list(pv)], "highway": collector.highways[int(hw[e])],
                              "way_id": int(ways[e]), "mid": ((pu[0] + pv[0]) / 2, (pu[1] + pv[1]) / 2)})
    finally:
        index = processor = None

    groups = merge_crossings([c["mid"] for c in crossings], [c["pair"] for c in crossings], merge_m)
    merged = {}
    for crossing, group in zip(crossings, groups):
        crossing["group"] = group
        merged.setdefault(group, crossing)
    kept = [merged[g] for g in sorted(merged)]

    boundary_points = {_key(*p) for p in partition.get("boundary", [])}
    extra_points = {_key(*g["at"]) for g in partition.get("extra_gates", [])} - boundary_points
    new_points = {_key(*p) for c in kept for p in c["gates"]} - boundary_points - extra_points
    pairs = defaultdict(lambda: {"a_cut_edges": 0, "extra_crossings": 0, "full_network_crossings": 0})
    for e in a_boundary:
        u, v = sorted((int(la[e]), int(lb[e])))
        pairs[f"{u}-{v}"]["a_cut_edges"] += 1
    for gate in partition.get("extra_gates", [])[0::2]:
        u, v = sorted(int(c) for c in gate["clusters"])
        pairs[f"{u}-{v}"]["extra_crossings"] += 1
    for crossing in kept:
        pairs["{}-{}".format(*crossing["pair"])]["full_network_crossings"] += 1
    report = {
        "graph": {"car_ways": collector.ways, "nodes": int(n), "edges": int(len(il)), "sources": int(len(source_nodes)),
                  "sources_missing": len(source_keys) - len(collector.sources)},
        "boundary_edges": {
            "before_merge": {"total": len(crossings), "by_highway": dict(Counter(c["highway"] for c in crossings).most_common())},
            "after_merge": {"total": len(kept), "by_highway": dict(Counter(c["highway"] for c in kept).most_common())},
            "a_edges_skipped": int(len(a_boundary)), "merge_m": merge_m},
        "gates": {"boundary_junctions": len(boundary_points), "small_bridge": len(extra_points),
                  "full_network_spectral": len(new_points), "total": len(boundary_points | extra_points | new_points)},
        "pairs": {key: pairs[key] for key in sorted(pairs, key=lambda k: tuple(map(int, k.split("-"))))},
        "cross_region_edges": int(cross_region.sum()),
        "cross_region_by_highway": dict(Counter(collector.highways[int(h)] for h in hw[cross_region]).most_common()),
        "unreachable_nodes": int((labels < 0).sum()), "dropped_river_edges": collector.dropped,
        "labelled_nodes_by_cluster": {str(c): int(v) for c, v in enumerate(np.bincount(labels[labels >= 0])) if v},
    }
    for crossing in crossings:
        crossing.pop("mid")
    result = {"report": report, "crossings": kept, "unreachable_nodes": report["unreachable_nodes"],
              "dropped_river_edges": collector.dropped,
              "gate_points": sorted({tuple(p) for p in partition.get("boundary", [])} | {tuple(g["at"]) for g in partition.get("extra_gates", [])}
                                    | {tuple(p) for c in kept for p in c["gates"]})}
    if keep_node_labels:
        result["node_labels"] = dict(zip(node_ids.tolist(), labels.tolist()))
    return result


def run(pbf, clusters, rivers, edges_path, out, router=None, build_json=ROOT / "osrm/build.json", merge_m=MERGE_M,
        max_full=MAX_FULL_TABLE, sample_rows=SAMPLE_ROWS, index_file=None, log=lambda *args: None):
    started = time.monotonic()
    sha = _sha256(pbf)
    doc = json.loads(Path(clusters).read_text(encoding="utf-8"))
    rivers_doc = json.loads(Path(rivers).read_text(encoding="utf-8"))
    for name, value in ((Path(clusters).name, doc.get("pbf_sha256")), (Path(rivers).name, rivers_doc.get("pbf_sha256"))):
        if value != sha:
            raise ValueError(f"{name} comes from another map")
    if build_json is not None:
        build = json.loads(Path(build_json).read_text(encoding="utf-8"))
        if build.get("input", {}).get("pbf_sha256") != sha:
            raise ValueError("osrm/build.json comes from another map")
    partition = next((p for p in doc["partitions"] if p.get("method") == "natural_spectral"), None)
    if partition is None:
        raise ValueError("no natural_spectral partition in the cluster file; run python model/spectral_clusters.py")
    result = survey_gates(pbf, partition, rivers_doc, edges_path, merge_m, index_file, log=log)
    report = result["report"]
    if router is not None:
        reason = router.unavailable(sha) if hasattr(router, "unavailable") else None
        if reason:
            raise RuntimeError(f"Cannot time the gate table: {reason}")
        report["table"] = time_gate_table(result["gate_points"], router, max_full, sample_rows)
        log(f"G x G table: {report['table']}")
    report["crossings"] = [{key: c[key] for key in ("pair", "highway", "way_id", "gates")} for c in result["crossings"]]
    report["metadata"] = {"pbf_sha256": sha, "partition": "natural_spectral", "k": partition["k"],
                          "parameters": {"merge_m": merge_m, "max_full_table": max_full, "sample_rows": sample_rows,
                                         "ferry_kmh": FERRY_KMH, "speeds": "car profile defaults (osm_classes.DEFAULT_SPEEDS_KMH)",
                                         "directed": False},
                          "runtime_s": round(time.monotonic() - started, 1), "built_at": datetime.now(timezone.utc).isoformat()}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pbf", type=Path, default=ROOT / "osrm/data/vietnam-latest.osm.pbf")
    parser.add_argument("--clusters", type=Path, default=ROOT / "osrm/data/big_road_clusters.json")
    parser.add_argument("--rivers", type=Path, default=ROOT / "osrm/data/main_rivers.geojson")
    parser.add_argument("--edges", type=Path, default=ROOT / "osrm/data/big_road_edges.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/full_network_gates.json")
    parser.add_argument("--merge-m", type=float, default=MERGE_M)
    parser.add_argument("--max-full-table", type=int, default=MAX_FULL_TABLE, help="larger gate sets are timed on sampled rows")
    parser.add_argument("--sample-rows", type=int, default=SAMPLE_ROWS)
    parser.add_argument("--no-table", action="store_true", help="skip timing the gate table on local OSRM")
    args = parser.parse_args()
    (ROOT / "runtime").mkdir(exist_ok=True)
    report = run(args.pbf, args.clusters, args.rivers, args.edges, args.out, None if args.no_table else TableRouter(),
                 merge_m=args.merge_m, max_full=args.max_full_table, sample_rows=args.sample_rows,
                 index_file=ROOT / "runtime/full_network_nodes.idx",
                 log=lambda *a: print(*a, flush=True))
    print(json.dumps({key: value for key, value in report.items() if key not in ("crossings", "pairs")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
