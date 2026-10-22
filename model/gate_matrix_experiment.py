"""Experiment: how close and how cheap are travel times composed through the gates of a big-road partition?

For points i in cluster A and j in cluster B (A != B), t_gate(i, j) = min over gates g of A and h of B of
t(i, g) + T(g, h) + t(h, j), all free-flow OSRM /table durations. The gates of a cluster are the boundary junctions
of the partition with an edge touching it. Compared with the direct OSRM time on a fixed random sample of junction
pairs, for every partition in big_road_clusters.json; also measures the cost of a full gate-to-gate table.
A natural (main river) partition also lists extra gates, the two ends of small-road bridges and ferries across a
river boundary; they are snapped by OSRM /nearest and join the gates of both banks.
Writes reports/gate_matrix_experiment.json. Experiment only: no estimator, backend or database changes.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

import numpy as np
from scipy.spatial import cKDTree

from build_big_roads import OsrmRouter, _write_json_atomic
from spectral_clusters import _in_rings, assign_provinces, build_graph, haversine_km

ROOT = Path(__file__).resolve().parents[1]
BLOCK = 100  # OSRM's default --max-table-size allows 100 x 100 per /table request
FULL_TABLE_PARTITIONS = ("spectral_16", "spectral_32", "province", "natural", "natural_spectral")
MAX_GATE_SNAP_M = 500.0  # an extra gate OSRM snaps farther than this is dropped
BANDS = ((0, 30, "<30"), (30, 100, "30-100"), (100, 300, "100-300"), (300, math.inf, ">=300"))
SLOTS = 96  # 15-minute departure slots per day
N_POINTS = 2000


class TableRouter(OsrmRouter):
    """OSRM /table durations between explicit sources and destinations."""

    def durations(self, sources, destinations):
        if list(sources) == list(destinations):
            # Square table: send each coordinate once, so 2000 points stay one 2000-coordinate request.
            coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in sources)
            return self._get(f"/table/v1/driving/{coords}?annotations=duration")["durations"]
        points = list(sources) + list(destinations)
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
        src = ";".join(str(i) for i in range(len(sources)))
        dst = ";".join(str(len(sources) + i) for i in range(len(destinations)))
        return self._get(f"/table/v1/driving/{coords}?sources={src}&destinations={dst}&annotations=duration")["durations"]


def partition_name(partition):
    method = partition.get("method")
    return method if method in ("province", "natural", "natural_spectral") else f"spectral_{partition['k']}"


def extra_gate_points(partition, router):
    """({cluster: points}, dropped): extra gates of the partition, each in both clusters it lists.

    With /nearest available the point is OSRM's snapped location, dropped when farther than MAX_GATE_SNAP_M.
    """
    points, dropped = defaultdict(list), 0
    for gate in partition.get("extra_gates", []):
        point = (round(float(gate["at"][0]), 6), round(float(gate["at"][1]), 6))
        if hasattr(router, "nearest"):
            snapped = router.nearest(point)
            if snapped["distance"] > MAX_GATE_SNAP_M:
                dropped += 1
                continue
            point = (round(float(snapped["location"][0]), 6), round(float(snapped["location"][1]), 6))
        for cluster in dict.fromkeys(int(c) for c in gate["clusters"] if int(c) >= 0):
            if point not in points[cluster]:
                points[cluster].append(point)
    return dict(points), dropped


def _with_extra(own, extra):
    merged = {c: list(points) for c, points in own.items()}
    for c, points in (extra or {}).items():
        merged.setdefault(c, [])
        merged[c] += [p for p in points if p not in merged[c]]
    return merged


def partition_labels(graph, partition):
    """Cluster of each graph junction from the partition's node_labels (-1 unassigned); no majority recount."""
    if "node_labels" not in partition:
        raise ValueError(f"partition {partition_name(partition)} has no node_labels; regenerate with python model/spectral_clusters.py")
    at = {(round(float(lon), 6), round(float(lat), 6)): int(c) for lon, lat, c in partition["node_labels"]}
    labels = np.array([at.get((round(float(x), 6), round(float(y), 6)), -2) for x, y in graph.lonlat], dtype=np.int64)
    if (labels == -2).any():
        raise ValueError(f"node_labels of {partition_name(partition)} miss {int((labels == -2).sum())} junctions of this graph; "
                         "regenerate with python model/spectral_clusters.py")
    return labels


def require_node_labels(doc, partitions=None):
    for partition in doc["partitions"]:
        if (not partitions or partition_name(partition) in partitions) and "node_labels" not in partition:
            raise ValueError(f"{partition_name(partition)} in the cluster file has no node_labels; "
                             "regenerate with python model/spectral_clusters.py")


def gate_sets(graph, labels, boundary, extra=None):
    """{("own", c): gate points of cluster c, ("ext", c): those of c and every cluster adjacent to it}.

    Junctions on the network use their own cluster's gates; small-road points also those of the neighbours,
    since such a point may reach the network across the border. extra: {cluster: points} added to the own gates.
    """
    own = {c: [tuple(graph.lonlat[g]) for g in nodes] for c, nodes in gates(graph, labels, boundary).items()}
    own = _with_extra(own, extra)
    adjacency = cluster_adjacency(graph, labels)
    sets = {}
    for c, points in own.items():
        sets[("own", c)] = points
        merged = dict.fromkeys(points)
        for other in sorted(o for pair in adjacency if c in pair for o in pair if o != c):
            merged.update(dict.fromkeys(own.get(other, [])))
        sets[("ext", c)] = list(merged)
    return sets


def passes(far_errors, p95_max=0.01, max_max=0.05):
    """True when far pairs keep p95 <= 1% and max <= 5%; None without far pairs."""
    far_errors = np.asarray(far_errors, dtype=float)
    if not len(far_errors):
        return None
    return bool(np.percentile(far_errors, 95) <= p95_max and far_errors.max() <= max_max)


def gates(graph, labels, boundary):
    """{cluster: sorted gate junctions}: boundary junctions in that cluster or with an edge to it."""
    at = {(round(float(x), 6), round(float(y), 6)): i for i, (x, y) in enumerate(graph.lonlat)}
    boundary_nodes = {at[(round(x, 6), round(y, 6))] for x, y in boundary if (round(x, 6), round(y, 6)) in at}
    found = defaultdict(set)
    for a, b in zip(graph.a.tolist(), graph.b.tolist()):
        for node, other in ((a, b), (b, a)):
            if node in boundary_nodes:
                for cluster in {labels[node], labels[other]} - {-1}:
                    found[int(cluster)].add(node)
    return {cluster: sorted(nodes) for cluster, nodes in sorted(found.items())}


def sample_pairs(n, count, seed=0):
    """`count` random (i, j) junction pairs with i != j, fixed by the seed."""
    rng = np.random.default_rng(seed)
    pairs = []
    while len(pairs) < count:
        i, j = (int(v) for v in rng.integers(n, size=2))
        if i != j:
            pairs.append((i, j))
    return pairs


def blocked_table(router, sources, destinations, block=BLOCK):
    """Durations (len(sources), len(destinations)) in seconds, NaN where OSRM has none, in block x block requests."""
    result = np.full((len(sources), len(destinations)), np.nan)
    for s in range(0, len(sources), block):
        for d in range(0, len(destinations), block):
            rows = router.durations(sources[s:s + block], destinations[d:d + block])
            result[s:s + block, d:d + block] = np.array([[np.nan if v is None else v for v in row] for row in rows], dtype=float)
    return result


def direct_times(graph, pairs, router):
    """OSRM time for each pair, from one blocked table over the distinct sources and destinations."""
    sources, destinations = sorted({i for i, _ in pairs}), sorted({j for _, j in pairs})
    table = blocked_table(router, [tuple(graph.lonlat[i]) for i in sources], [tuple(graph.lonlat[j]) for j in destinations])
    row, col = {n: k for k, n in enumerate(sources)}, {n: k for k, n in enumerate(destinations)}
    return np.array([table[row[i], col[j]] for i, j in pairs])


def metrics(t_gate, t_direct, distance_km):
    """Relative error (t_gate - t_direct) / t_direct over the given pairs, overall and by straight-line distance band."""
    errors = (np.asarray(t_gate) - np.asarray(t_direct)) / np.asarray(t_direct)
    distance_km = np.asarray(distance_km)

    def summary(values):
        if not len(values):
            return {"pairs": 0, "median": None}
        return {"pairs": int(len(values)), "median": float(np.median(values))}

    if not len(errors):
        return {"pairs": 0, "median": None, "p95": None, "max": None, "min": None, "over_5pct": None, "over_10pct": None, "anomalies": 0,
                "bands": {name: summary(errors[:0]) for _, _, name in BANDS}}
    return {"pairs": int(len(errors)), "median": float(np.median(errors)),
            "p95": float(np.percentile(errors, 95)), "max": float(errors.max()), "min": float(errors.min()),
            "over_5pct": float((errors > 0.05).mean()), "over_10pct": float((errors > 0.10).mean()),
            "anomalies": int((np.asarray(t_gate) < np.asarray(t_direct)).sum()),
            "bands": {name: summary(errors[(distance_km >= lo) & (distance_km < hi)]) for lo, hi, name in BANDS}}


def compose(cases, gate_points, router):
    """Gate times for cases [(src point, src cluster, dst point, dst cluster)], clusters differing.

    One table per source cluster for t(i, g), one per destination cluster for t(h, j) and one per cluster pair for
    T(g, h). Returns (times, best (g index, h index) per case, None where no gate route exists).
    """
    out_of, into, between = {}, {}, {}
    for cluster in sorted({c[1] for c in cases}):
        sources = sorted({c[0] for c in cases if c[1] == cluster})
        table = blocked_table(router, sources, gate_points[cluster])
        out_of.update({(p, cluster): table[k] for k, p in enumerate(sources)})
    for cluster in sorted({c[3] for c in cases}):
        destinations = sorted({c[2] for c in cases if c[3] == cluster})
        table = blocked_table(router, gate_points[cluster], destinations)
        into.update({(p, cluster): table[:, k] for k, p in enumerate(destinations)})
    for ca, cb in sorted({(c[1], c[3]) for c in cases}):
        between[(ca, cb)] = blocked_table(router, gate_points[ca], gate_points[cb])
    times, best = [], []
    for src, ca, dst, cb in cases:
        total = out_of[(src, ca)][:, None] + between[(ca, cb)] + into[(dst, cb)][None, :]
        if np.isfinite(total).any():
            flat = int(np.nanargmin(total))
            times.append(float(total.flat[flat]))
            best.append(divmod(flat, total.shape[1]))
        else:
            times.append(math.nan)
            best.append(None)
    return times, best


def evaluate_partition(graph, partition, pairs, router, direct=None):
    """Gate times against direct times for one partition.

    Pairs in the same or adjacent clusters are always answered by OSRM directly, so only far pairs (clusters not
    adjacent) are measured ("metrics", "pass"); "cross_metrics" keeps every cross-cluster pair for comparison.
    """
    labels = partition_labels(graph, partition)
    gate_nodes = gates(graph, labels, partition["boundary"])
    adjacency = partition_adjacency(graph, labels, partition)
    direct = direct_times(graph, pairs, router) if direct is None else np.asarray(direct)
    point = lambda n: tuple(graph.lonlat[n])  # noqa: E731
    extra, dropped = extra_gate_points(partition, router)
    gate_points = _with_extra({c: [point(g) for g in nodes] for c, nodes in gate_nodes.items()}, extra)
    counts = Counter()
    used = []
    for index, (i, j) in enumerate(pairs):
        ca, cb = int(labels[i]), int(labels[j])
        if ca < 0 or cb < 0:
            counts["unassigned"] += 1
            continue
        kind = relation(ca, cb, adjacency)
        counts[{"same": "same_cluster"}.get(kind, kind)] += 1
        if kind == "same":
            continue
        if not gate_points.get(ca) or not gate_points.get(cb) or not np.isfinite(direct[index]) or direct[index] <= 0:
            counts["skipped"] += 1
        else:
            used.append((index, i, j, ca, cb, kind))
    composed, _ = compose([(point(i), ca, point(j), cb) for _, i, j, ca, cb, _ in used], gate_points, router)
    keep = [k for k, value in enumerate(composed) if np.isfinite(value)]
    counts["skipped"] += len(composed) - len(keep)
    t_gate = np.array([composed[k] for k in keep])
    t_direct = np.array([float(direct[used[k][0]]) for k in keep])
    distance = np.array([haversine_km(point(used[k][1]), point(used[k][2])) for k in keep])
    far = np.array([used[k][5] == "far" for k in keep], dtype=bool)
    errors = (t_gate - t_direct) / t_direct if len(keep) else np.zeros(0)
    # Without clusters a pair cannot use gates either, so unassigned pairs count as direct.
    direct_pairs = counts["same_cluster"] + counts["adjacent"] + counts["unassigned"]
    return {"labels": labels, "gates": gate_nodes, "gate_points": gate_points, "extra_gates": extra,
            "extra_dropped": dropped, "t_gate": t_gate.tolist(), "t_direct": t_direct.tolist(),
            "relations": [used[k][5] for k in keep], "metrics": metrics(t_gate[far], t_direct[far], distance[far]) if len(keep) else metrics([], [], []),
            "cross_metrics": metrics(t_gate, t_direct, distance), "pass": passes(errors[far] if len(keep) else []),
            "direct_share": direct_pairs / len(pairs) if pairs else None,
            "same_cluster": counts["same_cluster"], "adjacent": counts["adjacent"], "far": counts["far"],
            "unassigned": counts["unassigned"], "skipped": counts["skipped"]}


def run(roads, clusters, out, router, pairs=500, seed=0, partitions=None, full_gate_table=FULL_TABLE_PARTITIONS):
    roads, clusters, out = Path(roads), Path(clusters), Path(out)
    meta = json.loads((roads.parent / "big_roads.meta.json").read_text(encoding="utf-8"))
    doc = json.loads(clusters.read_text(encoding="utf-8"))
    if doc.get("pbf_sha256") != meta["pbf_sha256"]:
        raise ValueError(f"{clusters.name} comes from another map; run python model/spectral_clusters.py")
    require_node_labels(doc, partitions)
    reason = router.unavailable(meta["pbf_sha256"]) if hasattr(router, "unavailable") else None
    if reason:
        raise RuntimeError(f"Cannot run the experiment: {reason}")
    started = time.perf_counter()
    graph = build_graph(json.loads(roads.read_text(encoding="utf-8"))["features"])
    sample = sample_pairs(graph.n, pairs, seed)
    clock = time.perf_counter()
    direct = direct_times(graph, sample, router)
    direct_s = time.perf_counter() - clock
    report_partitions = []
    for partition in doc["partitions"]:
        name = partition_name(partition)
        if partitions and name not in partitions:
            continue
        clock = time.perf_counter()
        result = evaluate_partition(graph, partition, sample, router, direct)
        all_gates = sorted({p for points in result["gate_points"].values() for p in points})
        extra_count = len({p for points in result["extra_gates"].values() for p in points})
        mean_gates = float(np.mean([len(points) for points in result["gate_points"].values()])) if result["gate_points"] else 0.0
        full_s = None
        if name in full_gate_table and all_gates:
            points = list(all_gates)
            tick = time.perf_counter()
            blocked_table(router, points, points)
            full_s = round(time.perf_counter() - tick, 2)
        share = result["direct_share"]
        report_partitions.append({
            "name": name, "k": partition["k"], "metrics": result["metrics"], "cross_metrics": result["cross_metrics"],
            "pass": result["pass"], "direct_share": share,
            "direct_cells_n2000": None if share is None else int(round(share * N_POINTS ** 2)),
            "same_cluster": result["same_cluster"], "adjacent": result["adjacent"], "far": result["far"],
            "unassigned": result["unassigned"], "skipped": result["skipped"],
            "cost": {"gates": len(all_gates), "extra_gates": extra_count, "extra_dropped": result["extra_dropped"],
                     "gate_cells": len(all_gates) ** 2, "mean_gates_per_cluster": round(mean_gates, 2),
                     "full_gate_table_s": full_s, "db_bytes_96_slots": len(all_gates) ** 2 * 4 * SLOTS,
                     "cells_n2000": round(N_POINTS * mean_gates), "cells_full_n2000": N_POINTS ** 2},
            "runtime_s": round(time.perf_counter() - clock, 2)})
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "pbf_sha256": meta["pbf_sha256"],
              "policy_version": meta["policy_version"], "pairs": pairs, "seed": seed, "junctions": graph.n,
              "direct_table_s": round(direct_s, 2), "partitions": report_partitions,
              "runtime_s": round(time.perf_counter() - started, 2)}
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(out, report)
    return report


def summary_table(report):
    head = ("partition", "far", "far p95", "far max", "pass", "direct share", "direct cells", "cross p95", "cross max",
            "G", "G^2", "GxG s", "DB MB", "N=2000 gate cells")
    rows = [head]
    pct = lambda v: "-" if v is None else f"{100 * v:.2f}%"  # noqa: E731
    for item in report["partitions"]:
        m, x, c = item["metrics"], item["cross_metrics"], item["cost"]
        rows.append((item["name"], str(m["pairs"]), pct(m["p95"]), pct(m["max"]), str(item["pass"]), pct(item["direct_share"]),
                     str(item["direct_cells_n2000"]), pct(x["p95"]), pct(x["max"]), str(c["gates"]), str(c["gate_cells"]),
                     "-" if c["full_gate_table_s"] is None else str(c["full_gate_table_s"]), f"{c['db_bytes_96_slots'] / 1e6:.1f}",
                     str(c["cells_n2000"])))
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    return "\n".join("  ".join(cell.rjust(w) for cell, w in zip(row, widths)) for row in rows)


# ---------------------------------------------------------------- round 2: stratified sample, small roads, worst pairs

STRATA = ((0, 30, "<30"), (30, 100, "30-100"), (100, 300, "100-300"), (300, 1800, ">=300"))
BAND_PAIRS = (300, 300, 150, 50)
MAX_SNAP_M = 500.0
NEAR_NETWORK_KM = 20.0
ENTRY_CANDIDATES = 10
INSPECT = 7
GATE_SNAP_M = 50.0
EARTH_KM = 6371.0088
MAX_ATTEMPTS = 5000


class EdgeIndex:
    """Read-only big_road_edges.sqlite: A-network node pairs (a < b) with way_id and kind."""

    def __init__(self, path):
        self.db = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
        self.metadata = dict(self.db.execute("SELECT key, value FROM metadata"))
        self._nodes = None

    def close(self):
        self.db.close()

    def has_pair(self, a, b):
        return self.db.execute("SELECT 1 FROM edges WHERE a = ? AND b = ? LIMIT 1", (min(a, b), max(a, b))).fetchone() is not None

    def has_node(self, node):
        if self._nodes is None:
            ends = np.array(self.db.execute("SELECT a, b FROM edges").fetchall(), dtype=np.int64).reshape(-1, 2)
            self._nodes = np.unique(ends)
        i = int(np.searchsorted(self._nodes, node))
        return i < len(self._nodes) and int(self._nodes[i]) == node

    def lookup(self, pairs):
        """{pair as given: (way_id, kind)} for the pairs on the network, either direction."""
        wanted = {(min(p), max(p)) for p in pairs}
        firsts, found = sorted({a for a, _ in wanted}), {}
        for k in range(0, len(firsts), 500):
            chunk = firsts[k:k + 500]
            for a, b, way_id, kind in self.db.execute(
                    f"SELECT a, b, way_id, kind FROM edges WHERE a IN ({','.join('?' * len(chunk))})", chunk):
                if (a, b) in wanted:
                    found.setdefault((a, b), (way_id, kind))
        return {p: found[(min(p), max(p))] for p in pairs if (min(p), max(p)) in found}


class RouteRouter(TableRouter):
    """TableRouter plus /nearest and a multi-leg /route with node annotations and step names."""

    def nearest(self, point):
        waypoint = self._get(f"/nearest/v1/driving/{point[0]:.6f},{point[1]:.6f}?number=1")["waypoints"][0]
        return {"location": waypoint["location"], "nodes": waypoint["nodes"], "distance": waypoint["distance"]}

    def route_detail(self, points):
        coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
        data = self._get(f"/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=true&annotations=nodes,distance")
        route = data["routes"][0]
        nodes, distances, legs, steps = [], [], [], []
        for leg in route["legs"]:
            leg_nodes, leg_distances = leg["annotation"]["nodes"], leg["annotation"]["distance"]
            legs.append(leg_nodes)
            if nodes and leg_nodes and nodes[-1] == leg_nodes[0]:
                nodes += leg_nodes[1:]
            else:
                distances += [0.0] if nodes and leg_nodes else []
                nodes += leg_nodes
            distances += leg_distances
            steps += [(step.get("name", ""), step.get("ref", ""), step["distance"]) for step in leg["steps"]]
        return {"nodes": nodes, "distances": distances, "legs": legs, "steps": steps, "duration": route["duration"],
                "waypoints": [w["location"] for w in data["waypoints"]], "coords": route["geometry"]["coordinates"]}


def _in_land(ring, point):
    return bool(_in_rings(np.array([point], dtype=float), [np.asarray(ring, dtype=float)])[0])


def _km_many(point, points):
    lon1, lat1 = np.radians(point[0]), np.radians(point[1])
    lon2, lat2 = np.radians(points[:, 0]), np.radians(points[:, 1])
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(a))


def _xyz(lonlat):
    lonlat = np.radians(np.asarray(lonlat, dtype=float).reshape(-1, 2))
    return EARTH_KM * np.column_stack((np.cos(lonlat[:, 1]) * np.cos(lonlat[:, 0]), np.cos(lonlat[:, 1]) * np.sin(lonlat[:, 0]),
                                       np.sin(lonlat[:, 1])))


def _destination(origin, bearing_deg, km):
    lat1, lon1, bearing, d = math.radians(origin[1]), math.radians(origin[0]), math.radians(bearing_deg), km / EARTH_KM
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(bearing))
    lon2 = lon1 + math.atan2(math.sin(bearing) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lon2), math.degrees(lat2))


def point_kind(nodes, edges):
    """"A" when the snapped segment's node pair is on the network (or, snapped onto a node, that node is), else "small"."""
    a, b = int(nodes[0]), int(nodes[1])
    if a == b:
        return "A" if edges.has_node(a) else "small"
    return "A" if edges.has_pair(a, b) else "small"


def rejection(snapped, ring, edges, need_small=False, origin=None, band=None):
    """Why a /nearest result cannot be used ("snap_far", "outside_land", "not_small", "out_of_band"), or None."""
    if snapped["distance"] > MAX_SNAP_M:
        return "snap_far"
    location = tuple(snapped["location"])
    if not _in_land(ring, location):
        return "outside_land"
    if need_small and point_kind(snapped["nodes"], edges) != "small":
        return "not_small"
    if origin is not None and not band[0] <= haversine_km(origin, location) < band[1]:
        return "out_of_band"
    return None


def sample_stratified(seed, band_pairs, ring, junctions, router, edges, max_attempts=MAX_ATTEMPTS):
    """Pairs per distance band, half small-small, a quarter small-A, the rest A-A; returns (pairs, rejection counts).

    Small-road points are random land points within 20 km of the network, snapped by /nearest onto a non-A
    segment; a small-small destination lies at a random bearing and band distance. A points are junctions.
    """
    rng = np.random.default_rng(seed)
    ring, junctions = np.asarray(ring, dtype=float), np.asarray(junctions, dtype=float)
    tree = cKDTree(_xyz(junctions))
    lo, hi = ring.min(axis=0), ring.max(axis=0)
    rejections = Counter()

    def near_network(point):
        return tree.query(_xyz(point)[0])[0] <= NEAR_NETWORK_KM

    def land_point():
        while True:
            point = tuple(rng.uniform(lo, hi))
            if _in_land(ring, point):
                return point
            rejections["outside_land"] += 1

    def small_point(origin=None, band=None, attempts=200):
        for _ in range(attempts):
            if origin is None:
                point = land_point()
            else:
                point = _destination(origin, rng.uniform(0, 360), rng.uniform(*band))
                if not _in_land(ring, point):
                    rejections["outside_land"] += 1
                    continue
            if not near_network(point):
                rejections["far_from_network"] += 1
                continue
            snapped = router.nearest(point)
            reason = rejection(snapped, ring, edges, need_small=True, origin=origin, band=band)
            if reason:
                rejections[reason] += 1
                continue
            return (round(float(snapped["location"][0]), 6), round(float(snapped["location"][1]), 6))
        return None

    def junction_in_band(origin, band):
        km = _km_many(origin, junctions)
        candidates = np.flatnonzero((km >= band[0]) & (km < band[1]))
        return int(rng.choice(candidates)) if len(candidates) else None

    pairs = []
    for (band_lo, band_hi, name), count in zip(STRATA, band_pairs):
        small_small = count // 2
        small_a = (count - small_small) // 2
        for kind, wanted in (("small-small", small_small), ("small-A", small_a), ("A-A", count - small_small - small_a)):
            made = attempts = 0
            while made < wanted:
                attempts += 1
                if attempts > max_attempts:
                    raise RuntimeError(f"Could not draw {wanted} {kind} pairs in band {name} after {max_attempts} attempts")
                src_node = int(rng.integers(len(junctions))) if kind == "A-A" else None
                src = tuple(float(v) for v in junctions[src_node]) if kind == "A-A" else small_point()
                if src is None:
                    raise RuntimeError("No small-road point found on land near the network")
                if kind == "small-small":
                    dst, dst_node = small_point(src, (band_lo, band_hi)), None
                else:
                    dst_node = junction_in_band(src, (band_lo, band_hi))
                    dst = None if dst_node is None else tuple(float(v) for v in junctions[dst_node])
                if dst is None:
                    rejections["no_destination"] += 1
                    continue
                pairs.append({"band": name, "kind": kind, "src": list(src), "dst": list(dst), "src_node": src_node, "dst_node": dst_node,
                              "src_kind": "A" if src_node is not None else "small", "dst_kind": "A" if dst_node is not None else "small",
                              "km": haversine_km(src, dst)})
                made += 1
    return pairs, dict(sorted(rejections.items()))


def entry_node(point, junctions, router, count=ENTRY_CANDIDATES, tree=None):
    """Of the `count` junctions nearest by straight line, the one OSRM reaches soonest from the point."""
    junctions = np.asarray(junctions, dtype=float)
    count = min(count, len(junctions))
    if tree is not None:
        _, nearest = tree.query(_xyz(point)[0], k=count)
        nearest = np.atleast_1d(nearest)
    else:
        nearest = np.argsort(_km_many(point, junctions), kind="stable")[:count]
    times = blocked_table(router, [tuple(point)], [tuple(junctions[i]) for i in nearest])[0]
    return int(nearest[int(np.nanargmin(times))] if np.isfinite(times).any() else nearest[0])


def cluster_adjacency(graph, labels):
    """Pairs of clusters joined by at least one cut edge."""
    la, lb = labels[graph.a], labels[graph.b]
    cut = (la >= 0) & (lb >= 0) & (la != lb)
    return {frozenset((int(a), int(b))) for a, b in zip(la[cut], lb[cut])}


def partition_adjacency(graph, labels, partition):
    """Cut-edge adjacency plus the pairs the partition lists itself (natural_spectral: river crossings too)."""
    return cluster_adjacency(graph, labels) | {frozenset((int(a), int(b))) for a, b in partition.get("adjacency", [])}


def relation(ca, cb, adjacency):
    if ca == cb:
        return "same"
    return "adjacent" if frozenset((ca, cb)) in adjacency else "far"


def classify(direct, via, gate_points, edges, way_cluster, clusters=None):
    """Labels for a badly composed pair, in order: bypass_small_road, ferry, gate_snap; ["other"] when none apply.

    bypass_small_road: along the direct route the cluster changes across node pairs off the network. With the
    pair's (source, destination) clusters given, the route's ends count too, so a small-road point whose route
    joins the network only in the other cluster is a bypass as well.
    """
    direct_pairs = list(zip(direct["nodes"], direct["nodes"][1:]))
    via_pairs = list(zip(via["nodes"], via["nodes"][1:]))
    info = edges.lookup(direct_pairs + via_pairs)
    labels = []
    last, gap = (clusters[0] if clusters else None), False
    bypass = False
    for pair in direct_pairs:
        item = info.get(pair)
        if item is None:
            gap = gap or last is not None
            continue
        cluster = way_cluster.get(str(item[0]))
        if cluster is None:
            continue
        if last is not None and cluster != last and gap:
            bypass = True
            break
        last, gap = cluster, False
    if not bypass and clusters and last is not None and last != clusters[1]:
        bypass = gap or not direct_pairs or info.get(direct_pairs[-1]) is None
    if bypass:
        labels.append("bypass_small_road")
    if any(info.get(pair, (None, None))[1] == "ferry" for pair in direct_pairs + via_pairs):
        labels.append("ferry")
    snapped_far = any(haversine_km(tuple(w), tuple(g)) * 1000 > GATE_SNAP_M for w, g in zip(via["waypoints"][1:3], gate_points))
    u_turn = any(len(a) >= 2 and len(b) >= 2 and a[-2] == b[1] for a, b in zip(via["legs"][:-1], via["legs"][1:]))
    if snapped_far or u_turn:
        labels.append("gate_snap")
    return labels or ["other"]


def worst_geojson(cases):
    """Direct route (solid), route through the gates (dashed) and the two gates for every inspected pair."""
    features = []
    for case in cases:
        common = {key: case[key] for key in ("partition", "case", "label", "labels", "clusters", "t_direct", "t_gate", "error", "km",
                                             "direct_ways", "via_ways", "direct_a_km", "via_a_km")}
        features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": case["direct_coords"]},
                         "properties": {**common, "role": "direct", "stroke": "#c0392b", "stroke-width": 3}})
        features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": case["via_coords"]},
                         "properties": {**common, "role": "via_gate", "stroke": "#1f4fd1", "stroke-width": 3, "stroke-dasharray": "6 4"}})
        for gate in case["gates"]:
            features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": gate},
                             "properties": {**common, "role": "gate", "marker-color": "#000000"}})
    return {"type": "FeatureCollection", "features": features}


def _main_ways(steps, top=3):
    km = Counter()
    for name, ref, metres in steps:
        label = " ".join(part for part in (ref, name) if part)
        if label:
            km[label] += metres / 1000
    return [f"{label} ({value:.1f} km)" for label, value in km.most_common(top)]


def _a_km(route, edges):
    pairs = list(zip(route["nodes"], route["nodes"][1:]))
    on_a = edges.lookup(pairs)
    return round(sum(d for pair, d in zip(pairs, route["distances"]) if pair in on_a) / 1000, 3)


def _inspect(case, router, edges, way_cluster):
    """Route both ways for one pair; returns the detail record (labels "route_error" when OSRM fails)."""
    try:
        direct = router.route_detail([case["src"], case["dst"]])
        via = router.route_detail([case["src"], *case["gates"], case["dst"]])
    except (OSError, ValueError, KeyError, IndexError) as exc:
        return {**case, "label": "route_error", "labels": ["route_error"], "error_message": f"{type(exc).__name__}: {exc}"[:200],
                "direct_coords": [case["src"], case["dst"]], "via_coords": [case["src"], *case["gates"], case["dst"]],
                "direct_ways": [], "via_ways": [], "direct_a_km": None, "via_a_km": None}
    labels = classify(direct, via, case["gates"], edges, way_cluster, case["clusters"])
    return {**case, "label": labels[0], "labels": labels, "direct_coords": direct["coords"], "via_coords": via["coords"],
            "direct_ways": _main_ways(direct["steps"]), "via_ways": _main_ways(via["steps"]),
            "direct_a_km": _a_km(direct, edges), "via_a_km": _a_km(via, edges),
            "via_gate_snap_m": [round(haversine_km(tuple(w), tuple(g)) * 1000, 1) for w, g in zip(via["waypoints"][1:3], case["gates"])]}


def run_stratified(roads, clusters, out, out_geojson, router, edges, land_ring, provinces=None, band_pairs=BAND_PAIRS, seed=0,
                   partitions=None, round1=None):
    """Rounds 2-3: stratified pairs with small-road points; errors by band x cluster relation x pair kind.

    Same- and adjacent-cluster pairs are answered by OSRM directly, so the verdict ("pass") uses far pairs only and
    the 7 worst far pairs are inspected. The direct share for N=2000 comes from the uniform round-1 report.
    """
    roads, clusters, out, out_geojson = Path(roads), Path(clusters), Path(out), Path(out_geojson)
    meta = json.loads((roads.parent / "big_roads.meta.json").read_text(encoding="utf-8"))
    doc = json.loads(clusters.read_text(encoding="utf-8"))
    if doc.get("pbf_sha256") != meta["pbf_sha256"]:
        raise ValueError(f"{clusters.name} comes from another map; run python model/spectral_clusters.py")
    require_node_labels(doc, partitions)
    uniform = {}
    if round1 is not None and Path(round1).is_file():
        first = json.loads(Path(round1).read_text(encoding="utf-8"))
        if first.get("pbf_sha256") == meta["pbf_sha256"]:
            uniform = {item["name"]: item.get("direct_share") for item in first["partitions"]}
    if getattr(edges, "metadata", {}).get("pbf_sha256", meta["pbf_sha256"]) != meta["pbf_sha256"]:
        raise ValueError("big_road_edges.sqlite comes from another map; run python model/build_big_roads.py")
    reason = router.unavailable(meta["pbf_sha256"]) if hasattr(router, "unavailable") else None
    if reason:
        raise RuntimeError(f"Cannot run the experiment: {reason}")
    started = time.perf_counter()
    graph = build_graph(json.loads(roads.read_text(encoding="utf-8"))["features"])
    sample, rejections = sample_stratified(seed, band_pairs, land_ring, graph.lonlat, router, edges)
    sample_s = time.perf_counter() - started
    tree = cKDTree(_xyz(graph.lonlat))
    for pair in sample:
        for end in ("src", "dst"):
            pair[f"{end}_entry"] = pair[f"{end}_node"] if pair[f"{end}_node"] is not None else \
                entry_node(pair[end], graph.lonlat, router, tree=tree)
    sources, destinations = sorted({tuple(p["src"]) for p in sample}), sorted({tuple(p["dst"]) for p in sample})
    table = blocked_table(router, sources, destinations)
    row, col = {p: k for k, p in enumerate(sources)}, {p: k for k, p in enumerate(destinations)}
    t_direct = np.array([table[row[tuple(p["src"])], col[tuple(p["dst"])]] for p in sample])
    province_of = {}
    if provinces:
        small = sorted({tuple(p[end]) for p in sample for end in ("src", "dst") if p[f"{end}_kind"] == "small"})
        if small:
            labels, _ = assign_provinces(np.array(small, dtype=float), provinces)
            province_of = dict(zip(small, labels.tolist()))
    km = np.array([p["km"] for p in sample])
    report_partitions, all_cases = [], []
    for partition in doc["partitions"]:
        name = partition_name(partition)
        if partitions and name not in partitions:
            continue
        clock = time.perf_counter()
        labels = partition_labels(graph, partition)
        extra, _ = extra_gate_points(partition, router)
        gate_points = gate_sets(graph, labels, partition["boundary"], extra)
        adjacency = partition_adjacency(graph, labels, partition)
        parents = partition.get("parents")
        is_province = partition.get("method") == "province"

        def cluster_of(pair, end):
            if pair[f"{end}_node"] is None and is_province and province_of.get(tuple(pair[end]), -1) >= 0:
                return int(province_of[tuple(pair[end])])
            return int(labels[pair[f"{end}_entry"]])

        counts, cases, used = Counter(), [], []
        relations = []
        for index, pair in enumerate(sample):
            ca, cb = cluster_of(pair, "src"), cluster_of(pair, "dst")
            relations.append(relation(ca, cb, adjacency) if ca >= 0 and cb >= 0 else "unassigned")
            if relations[-1] == "unassigned":
                counts["unassigned"] += 1
            elif ca == cb:
                counts["same_cluster"] += 1
            elif not gate_points.get(("own", ca)) or not gate_points.get(("own", cb)) or not np.isfinite(t_direct[index]) \
                    or t_direct[index] <= 0:
                counts["skipped"] += 1
            else:
                # Small-road points may join the network across the border: they also try the neighbours' gates.
                ka = ("ext" if pair["src_kind"] == "small" else "own", ca)
                kb = ("ext" if pair["dst_kind"] == "small" else "own", cb)
                cases.append((tuple(pair["src"]), ka, tuple(pair["dst"]), kb))
                used.append(index)
        times, best = compose(cases, gate_points, router)
        keep = [k for k, t in enumerate(times) if np.isfinite(t)]
        counts["skipped"] += len(times) - len(keep)
        idx = np.array([used[k] for k in keep], dtype=np.int64)
        t_gate = np.array([times[k] for k in keep])
        errors = (t_gate - t_direct[idx]) / t_direct[idx] if len(idx) else np.zeros(0)
        rel = np.array(relations)
        groups = {}
        for band in [s[2] for s in STRATA]:
            for relation_name in ("adjacent", "far"):
                for kind in ("small-small", "small-A", "A-A"):
                    mask = np.array([sample[i]["band"] == band and rel[i] == relation_name and sample[i]["kind"] == kind for i in idx], dtype=bool)
                    if mask.any():
                        m = metrics(t_gate[mask], t_direct[idx][mask], km[idx][mask])
                        groups[f"{band} | {relation_name} | {kind}"] = {key: m[key] for key in ("pairs", "median", "p95", "max", "over_5pct")}
        far = rel[idx] == "far" if len(idx) else np.zeros(0, dtype=bool)
        far_keys = [k for k in range(len(idx)) if far[k]]
        worst = sorted(far_keys, key=lambda k: -errors[k])[:INSPECT]
        inspected = []
        for k in worst:
            i = int(idx[k])
            g, h = best[keep[k]]
            ka, kb = cases[keep[k]][1], cases[keep[k]][3]
            record = {"partition": name, "case": "worst", "clusters": [ka[1], kb[1]], "relation": relations[i], "band": sample[i]["band"],
                      "kind": sample[i]["kind"], "src": sample[i]["src"], "dst": sample[i]["dst"],
                      "gates": [list(gate_points[ka][g]), list(gate_points[kb][h])], "t_direct": round(float(t_direct[i]), 1),
                      "t_gate": round(float(t_gate[k]), 1), "error": float(errors[k]), "km": round(float(km[i]), 2)}
            if parents:
                # Two-level partition: does the pair cross a river boundary or only a spectral one inside a region?
                record["boundary_kind"] = "river" if parents.get(str(ka[1])) != parents.get(str(kb[1])) else "spectral"
            inspected.append(_inspect(record, router, edges, partition["ways"]))
        share = uniform.get(name)
        all_cases += inspected
        report_partitions.append({
            "name": name, "k": partition["k"], "cross_pairs": int(len(idx)), "far_pairs": int(far.sum()),
            "same_cluster": counts["same_cluster"], "unassigned": counts["unassigned"], "skipped": counts["skipped"],
            "far_metrics": metrics(t_gate[far], t_direct[idx][far], km[idx][far]) if len(idx) else metrics([], [], []),
            "pass": passes(errors[far]), "metrics": metrics(t_gate, t_direct[idx], km[idx]), "anomalies": int((errors < 0).sum()),
            "groups": groups, "direct_share_round1": share,
            "direct_cells_n2000": None if share is None else int(round(share * N_POINTS ** 2)), "cells_full_n2000": N_POINTS ** 2,
            "label_counts": dict(Counter(c["label"] for c in inspected)),
            "inspected": [{key: value for key, value in c.items() if key not in ("direct_coords", "via_coords")} for c in inspected],
            "runtime_s": round(time.perf_counter() - clock, 2)})
    kinds = Counter(f"{p['band']} | {p['kind']}" for p in sample)
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "pbf_sha256": meta["pbf_sha256"],
              "policy_version": meta["policy_version"], "seed": seed, "band_pairs": list(band_pairs),
              "sample": {"pairs": len(sample), "by_band_kind": dict(sorted(kinds.items())), "rejections": rejections,
                         "sample_s": round(sample_s, 2)},
              "partitions": report_partitions, "runtime_s": round(time.perf_counter() - started, 2)}
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(out_geojson, worst_geojson(all_cases))
    _write_json_atomic(out, report)
    return report


def summary_stratified(report):
    pct = lambda v: "-" if v is None else f"{100 * v:.2f}%"  # noqa: E731
    lines = [f"sample {report['sample']['pairs']} pairs, rejections {report['sample']['rejections']}"]
    for item in report["partitions"]:
        m, x = item["far_metrics"], item["metrics"]
        lines.append(f"\n{item['name']}: far {item['far_pairs']} of cross {item['cross_pairs']} (same {item['same_cluster']}), "
                     f"far p95 {pct(m['p95'])}, far max {pct(m['max'])}, pass {item['pass']}; all cross p95 {pct(x['p95'])}, "
                     f"max {pct(x['max'])}; direct share (round 1) {pct(item['direct_share_round1'])}, "
                     f"direct cells N=2000 {item['direct_cells_n2000']} of {item['cells_full_n2000']}; worst far labels {item['label_counts']}")
        width = max((len(key) for key in item["groups"]), default=10)
        for key, g in item["groups"].items():
            lines.append(f"  {key:<{width}}  n={g['pairs']:>3}  median {pct(g['median']):>7}  p95 {pct(g['p95']):>7}  "
                         f"max {pct(g['max']):>7}  >5% {pct(g['over_5pct']):>7}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roads", type=Path, default=ROOT / "osrm/data/big_roads.geojson")
    parser.add_argument("--clusters", type=Path, default=ROOT / "osrm/data/big_road_clusters.json")
    parser.add_argument("--out", type=Path, help="report path (default reports/gate_matrix_experiment.json, _2 with --stratified)")
    parser.add_argument("--pairs", type=int, default=500)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--partitions", nargs="+", help="e.g. spectral_32 province (default: all)")
    parser.add_argument("--skip-full-gate-table", action="store_true")
    parser.add_argument("--stratified", action="store_true", help="round 2: stratified pairs with small-road points")
    parser.add_argument("--band-pairs", default=",".join(map(str, BAND_PAIRS)), help="pairs for <30,30-100,100-300,>=300 km")
    parser.add_argument("--land", type=Path, default=ROOT / "osrm/data/vietnam_land.geojson")
    parser.add_argument("--edges", type=Path, default=ROOT / "osrm/data/big_road_edges.sqlite")
    parser.add_argument("--provinces", type=Path, default=ROOT / "osrm/data/vietnam_provinces.geojson")
    parser.add_argument("--worst-geojson", type=Path, default=ROOT / "reports/gate_matrix_worst.geojson")
    parser.add_argument("--round1", type=Path, default=ROOT / "reports/gate_matrix_experiment.json",
                        help="uniform round-1 report giving the direct share for N=2000")
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    if not args.stratified:
        # RouteRouter adds /nearest, which snaps the extra gates of the natural partition.
        report = run(args.roads, args.clusters, args.out or ROOT / "reports/gate_matrix_experiment.json", RouteRouter(), args.pairs,
                     args.seed, args.partitions, () if args.skip_full_gate_table else FULL_TABLE_PARTITIONS)
        print(summary_table(report))
        print(f"junctions {report['junctions']}, direct table {report['direct_table_s']} s, total {report['runtime_s']} s")
        return
    try:
        band_pairs = tuple(int(v) for v in args.band_pairs.split(","))
    except ValueError:
        band_pairs = ()
    if len(band_pairs) != len(STRATA) or min(band_pairs) < 0:
        parser.error("--band-pairs takes four non-negative integers, e.g. 300,300,150,50")
    land = json.loads(args.land.read_text(encoding="utf-8"))["geometry"]["coordinates"][0]
    provinces = json.loads(args.provinces.read_text(encoding="utf-8"))["features"] if args.provinces.is_file() else None
    edges = EdgeIndex(args.edges)
    try:
        report = run_stratified(args.roads, args.clusters, args.out or ROOT / "reports/gate_matrix_experiment_2.json",
                                args.worst_geojson, RouteRouter(), edges, land, provinces, band_pairs, args.seed, args.partitions,
                                round1=args.round1)
    finally:
        edges.close()
    print(summary_stratified(report))
    print(f"total {report['runtime_s']} s")


if __name__ == "__main__":
    main()
