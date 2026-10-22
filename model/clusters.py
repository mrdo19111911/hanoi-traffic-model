"""Regions of the big-road (A) network: by province and by main river (spec 22).

Reads big_roads.geojson and big_roads.meta.json and builds the junction graph (shared vertices split ways, degree-2
chains are contracted, the main component is kept; each edge weighted by 1 / free-flow time). Writes
big_road_clusters.json: per partition the region of every way and of every junction (node_labels), the cut chains,
the boundary junctions (exactly the ends of cut edges) and summary stats. Ferry landings stay junctions.
When vietnam_provinces.geojson comes from the same map, the "province" partition is written.
When main_rivers.geojson (written by model/natural_barriers.py) comes from the same map, the "natural" partition
is written too: the network is cut where it crosses a main river and pieces under 200 km of road are merged; the
ends of non-A crossings (small-road bridges, ferries) are kept as extra gates.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
METHOD = "regions"
PARTITIONS = ("province", "natural")
SPEED_KMH = {"caotoc": 80.0, "quoclo": 50.0, "tinhlo": 40.0}
FERRY_KMH = 15.0
MIN_HOURS = 30 / 3600  # so tiny junction stubs do not dominate the weights
SNAP_KM = 2.0  # junctions just outside every province go to the nearest one within this distance
KM_PER_DEGREE = 111.32
NATURAL_MIN_KM = 200.0  # natural pieces with less A road are merged into a neighbour
RIVER_CELL_DEG = 0.02
BANK_CANDIDATES = 200  # nearest junctions tried for the bank of a crossing end


def haversine_km(first, second):
    lat1, lat2 = math.radians(first[1]), math.radians(second[1])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(second[0] - first[0]) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


class Graph:
    """Junction graph of the main component.

    n junctions at lonlat; edges (a, b) with summed 1/hours weight w and km, each made of one or more chains.
    chains: {a, b, km, coords, segs: [(way_id, km)]}, self-loops (a == b) included.
    """

    def __init__(self, n, lonlat, a, b, w, km, edge_chains, chains):
        self.n, self.lonlat, self.a, self.b, self.w, self.km = n, lonlat, a, b, w, km
        self.edge_chains, self.chains = edge_chains, chains
        loops = [c for c in chains if c["a"] == c["b"]]
        self.self_km = np.bincount(np.array([c["a"] for c in loops], dtype=np.int64), np.array([c["km"] for c in loops]), n)
        self.total_km = float(km.sum() + self.self_km.sum())


def _segments(features):
    """Split kept LineStrings at endpoints and shared vertices.

    Returns (segments (u, v, way_id, km, hours, coords), node coords, ferry landing nodes). Landings are kept as
    junctions, so a ferry crossing between clusters has its own landings as gates.
    """
    kept = []
    for feature in features:
        props = feature["properties"]
        if props.get("fragment") or props.get("isolated") or feature["geometry"]["type"] != "LineString":
            continue
        coords = [(round(lon * 1e6), round(lat * 1e6)) for lon, lat in feature["geometry"]["coordinates"]]
        if len(coords) >= 2:
            kept.append((coords, props))
    uses = defaultdict(int)
    for coords, _ in kept:
        for point in coords:
            uses[point] += 1
    node_of = {}
    for coords, _ in kept:
        for point in (coords[0], coords[-1]):
            node_of.setdefault(point, len(node_of))
    for point, count in uses.items():
        if count > 1:
            node_of.setdefault(point, len(node_of))
    segments = []
    for coords, props in kept:
        kmh = FERRY_KMH if props.get("bridge_kind") == "ferry" else SPEED_KMH[props["group"]]
        start, km = 0, 0.0
        for i in range(1, len(coords)):
            km += haversine_km((coords[i - 1][0] / 1e6, coords[i - 1][1] / 1e6), (coords[i][0] / 1e6, coords[i][1] / 1e6))
            if coords[i] in node_of:
                segments.append((node_of[coords[start]], node_of[coords[i]], props["way_id"], km, km / kmh, coords[start:i + 1]))
                start, km = i, 0.0
    points = [None] * len(node_of)
    for point, index in node_of.items():
        points[index] = point
    landings = {node_of[coords[i]] for coords, props in kept if props.get("bridge_kind") == "ferry" for i in (0, -1)}
    return segments, points, landings


def _contract(segments, n_nodes, keep=()):
    """Merge degree-2 chains between junctions: [(start, end, [(segment, forward)])]. Pure degree-2 loops are skipped.

    Nodes in `keep` stay junctions whatever their degree.
    """
    incident = [[] for _ in range(n_nodes)]
    for index, (u, v, *_) in enumerate(segments):
        incident[u].append(index)
        incident[v].append(index)
    keep = set(keep)
    junction = [len(edges) != 2 or node in keep for node, edges in enumerate(incident)]
    used = bytearray(len(segments))
    chains = []
    for start in range(n_nodes):
        if not junction[start]:
            continue
        for first in incident[start]:
            if used[first]:
                continue
            node, edge, path = start, first, []
            while True:
                used[edge] = 1
                u, v = segments[edge][:2]
                path.append((edge, u == node))
                node = v if u == node else u
                if junction[node]:
                    break
                following = [e for e in incident[node] if not used[e]]
                if not following:
                    break
                edge = following[0]
            chains.append((start, node, path))
    return chains


def build_graph(features):
    """Junction graph of the main connected component of the kept (not fragment, not isolated) features."""
    segments, points, landings = _segments(features)
    raw = _contract(segments, len(points), landings)
    jid = {}
    for start, end, _ in raw:
        for node in (start, end):
            jid.setdefault(node, len(jid))
    pairs = {}
    for index, (start, end, _) in enumerate(raw):
        if jid[start] != jid[end]:
            pairs.setdefault((min(jid[start], jid[end]), max(jid[start], jid[end])), []).append(index)
    n = len(jid)
    if n == 0:
        raise ValueError("No big roads left to cluster")
    ends = np.array(list(pairs), dtype=np.int64).reshape(-1, 2)
    _, component = connected_components(coo_matrix((np.ones(len(ends)), (ends[:, 0], ends[:, 1])), shape=(n, n)), directed=False)
    keep = component == np.bincount(component).argmax()
    remap = -np.ones(n, dtype=np.int64)
    remap[keep] = np.arange(int(keep.sum()))
    chains = []
    for start, end, path in raw:
        ia, ib = remap[jid[start]], remap[jid[end]]
        if ia < 0:
            continue
        coords, segs = [], []
        for edge, forward in path:
            piece = segments[edge][5] if forward else segments[edge][5][::-1]
            coords += piece if not coords else piece[1:]
            segs.append((segments[edge][2], segments[edge][3]))
        chains.append({"a": int(ia), "b": int(ib), "km": sum(km for _, km in segs),
                       "hours": sum(segments[edge][4] for edge, _ in path),
                       "coords": [[x / 1e6, y / 1e6] for x, y in coords], "segs": segs})
    edge_of, edge_chains, weights, kms = {}, [], [], []
    for index, chain in enumerate(chains):
        if chain["a"] == chain["b"]:
            continue
        key = (min(chain["a"], chain["b"]), max(chain["a"], chain["b"]))
        if key not in edge_of:
            edge_of[key] = len(edge_chains)
            edge_chains.append([])
            weights.append(0.0)
            kms.append(0.0)
        e = edge_of[key]
        edge_chains[e].append(index)
        weights[e] += 1.0 / max(chain["hours"], MIN_HOURS)
        kms[e] += chain["km"]
    size = int(keep.sum())
    lonlat = np.zeros((size, 2))
    for node, i in jid.items():
        if remap[i] >= 0:
            lonlat[remap[i]] = (points[node][0] / 1e6, points[node][1] / 1e6)
    edges = np.array(list(edge_of), dtype=np.int64).reshape(-1, 2)
    return Graph(size, lonlat, edges[:, 0], edges[:, 1], np.array(weights), np.array(kms), edge_chains, chains)


def _province_rings(feature):
    """All rings of a MultiPolygon feature as (m, 2) arrays; even-odd over all of them honours the holes."""
    return [np.asarray(ring, dtype=float) for polygon in feature["geometry"]["coordinates"] for ring in polygon]


def _in_rings(points, rings, chunk=1000):
    """Even-odd ray casting of (n, 2) points against a set of rings."""
    edges = np.vstack([np.hstack([ring[:-1], ring[1:]]) for ring in rings])
    x1, y1, x2, y2 = (edges[:, i] for i in range(4))
    result = np.zeros(len(points), dtype=bool)
    for offset in range(0, len(points), chunk):
        px, py = points[offset:offset + chunk, :1], points[offset:offset + chunk, 1:]
        crosses = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            hit_x = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        result[offset:offset + chunk] = (crosses & (px < hit_x)).sum(axis=1) % 2 == 1
    return result


def _boundary_km(point, rings):
    """Distance in km from one (lon, lat) point to the nearest ring edge, on a local equirectangular projection."""
    scale = np.array([math.cos(math.radians(point[1])), 1.0]) * KM_PER_DEGREE
    best = math.inf
    for ring in rings:
        a, b = (ring[:-1] - point) * scale, (ring[1:] - point) * scale
        d = b - a
        length = (d ** 2).sum(axis=1)
        t = np.clip(np.divide(-(a * d).sum(axis=1), length, out=np.zeros(len(a)), where=length > 0), 0, 1)
        best = min(best, float(np.sqrt(((a + t[:, None] * d) ** 2).sum(axis=1)).min()))
    return best


def assign_provinces(lonlat, features, snap_km=SNAP_KM):
    """(labels, names): the province of each junction, -1 when none lies within snap_km.

    Provinces are numbered by ascending iso. Inside a province means inside an outer ring and outside its holes;
    where provinces overlap the smaller iso wins, as it does on an equal snapping distance.
    """
    provinces = sorted(features, key=lambda f: f["properties"]["iso"])
    labels = -np.ones(len(lonlat), dtype=np.int64)
    rings, boxes = [], []
    for index, feature in enumerate(provinces):
        rings.append(_province_rings(feature))
        stacked = np.vstack(rings[-1])
        lo, hi = stacked.min(axis=0), stacked.max(axis=0)
        boxes.append((lo, hi))
        candidates = np.flatnonzero((labels < 0) & np.all((lonlat >= lo) & (lonlat <= hi), axis=1))
        if len(candidates):
            labels[candidates[_in_rings(lonlat[candidates], rings[-1])]] = index
    margin = snap_km / KM_PER_DEGREE
    for node in np.flatnonzero(labels < 0):
        point = lonlat[node]
        reach = np.array([margin / max(math.cos(math.radians(point[1])), 0.1), margin])
        best, best_km = -1, math.inf
        for index, (lo, hi) in enumerate(boxes):
            if np.all(point >= lo - reach) and np.all(point <= hi + reach):
                km = _boundary_km(point, rings[index])
                if km <= snap_km and km < best_km:
                    best, best_km = index, km
        labels[node] = best
    return labels, [f["properties"]["name"] for f in provinces]


def stats(graph, labels, k=None):
    """Summary of one partition (conductance by edge count).

    Junctions labelled -1 belong to no cluster: their edges are neither cuts nor inside a cluster.
    """
    k = int(labels.max()) + 1 if k is None else k
    la, lb = labels[graph.a], labels[graph.b]
    both = (la >= 0) & (lb >= 0)
    cut, inner = both & (la != lb), both & (la == lb)
    ones = np.ones(len(graph.a))
    degree = np.bincount(graph.a[both], ones[both], graph.n) + np.bincount(graph.b[both], ones[both], graph.n)
    assigned = labels >= 0
    volume = np.bincount(labels[assigned], degree[assigned], k)
    cut_count = np.bincount(la[cut], ones[cut], k) + np.bincount(lb[cut], ones[cut], k)
    denominator = np.minimum(volume, volume.sum() - volume)
    conductance = np.divide(cut_count, denominator, out=np.zeros(k), where=denominator > 0)
    km_in = np.bincount(la[inner], graph.km[inner], k) + np.bincount(labels[assigned], graph.self_km[assigned], k)
    _, piece = connected_components(coo_matrix((np.ones(int(inner.sum())), (graph.a[inner], graph.b[inner])),
                                               shape=(graph.n, graph.n)), directed=False)
    contiguous = sum(1 for c in range(k) if len(np.unique(piece[labels == c])) == 1)
    cut_km = float(graph.km[cut].sum())
    return {"cut_edges": int(cut.sum()), "cut_km": round(cut_km, 1),
            "cut_pct": round(100 * cut_km / graph.total_km, 2) if graph.total_km else 0.0,
            "boundary_nodes": len(set(graph.a[cut].tolist()) | set(graph.b[cut].tolist())), "contiguous": contiguous,
            "conductance_mean": round(float(conductance.mean()), 4), "km_min": round(float(km_in.min()), 1),
            "km_median": round(float(np.median(km_in)), 1), "km_max": round(float(km_in.max()), 1)}


def way_clusters(graph, labels):
    """{way_id: cluster} by the most length; a cut chain gives its first half to one end and the rest to the other."""
    km = defaultdict(lambda: defaultdict(float))
    for chain in graph.chains:
        ca, cb = int(labels[chain["a"]]), int(labels[chain["b"]])
        half, position = chain["km"] / 2, 0.0
        for way_id, seg_km in chain["segs"]:
            to_a = seg_km if ca == cb else max(0.0, min(position + seg_km, half) - position)
            km[way_id][ca] += to_a
            km[way_id][cb] += seg_km - to_a
            position += seg_km
    found = {}
    for way_id, by_cluster in sorted(km.items()):
        assigned = [c for c in by_cluster if c >= 0]
        if assigned:
            found[str(way_id)] = min(assigned, key=lambda c: (-by_cluster[c], c))
    return found


def _cross(p1, p2, q1, q2):
    """Whether segments p1-p2 and q1-q2 meet (touching counts); parallel segments never do."""
    rx, ry, sx, sy = p2[0] - p1[0], p2[1] - p1[1], q2[0] - q1[0], q2[1] - q1[1]
    denom = rx * sy - ry * sx
    if denom == 0:
        return False
    qx, qy = q1[0] - p1[0], q1[1] - p1[1]
    t, u = (qx * sy - qy * sx) / denom, (qx * ry - qy * rx) / denom
    return 0 <= t <= 1 and 0 <= u <= 1


class _RiverIndex:
    """Main-river segments in a lon/lat grid."""

    def __init__(self, features, cell=RIVER_CELL_DEG):
        self.cell, self.segments, self.grid = cell, [], defaultdict(list)
        for feature in features:
            coords = feature["geometry"]["coordinates"]
            for a, b in zip(coords, coords[1:]):
                index = len(self.segments)
                self.segments.append((tuple(a), tuple(b)))
                for c in self._cells(a, b):
                    self.grid[c].append(index)

    def _cells(self, a, b):
        size = self.cell
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        return [(x, y) for x in range(math.floor(x0 / size), math.floor(x1 / size) + 1)
                for y in range(math.floor(y0 / size), math.floor(y1 / size) + 1)]

    def crosses(self, a, b):
        candidates = {s for c in self._cells(a, b) for s in self.grid.get(c, ())}
        return any(_cross(a, b, *self.segments[s]) for s in candidates)

    def crosses_line(self, coords):
        return any(self.crosses(a, b) for a, b in zip(coords, coords[1:]))


def merge_components(km, links, min_km=NATURAL_MIN_KM):
    """Final label per component: pieces under min_km join the neighbour with most links (tie: larger km, then lower id).

    The smallest piece goes first, repeatedly; a small piece that has no link at all gets -1. Labels run from 0 by
    descending km. links: {(i, j): number of cut edges between components i and j}.
    """
    n = len(km)
    owner = list(range(n))
    size = {c: float(km[c]) for c in range(n)}
    neighbours = {c: defaultdict(int) for c in range(n)}
    for (i, j), count in links.items():
        if i != j:
            neighbours[i][j] += count
            neighbours[j][i] += count
    # Only a piece that never had a link (an island, a loose fragment) is left unassigned.
    alone = {c for c in range(n) if not neighbours[c] and size[c] < min_km}
    stuck = set()
    while True:
        small = [c for c in size if size[c] < min_km and c not in alone | stuck]
        if not small:
            break
        c = min(small, key=lambda x: (size[x], x))
        if not neighbours[c]:
            stuck.add(c)  # everything it could join is already in it
            continue
        target = min(neighbours[c], key=lambda t: (-neighbours[c][t], -size[t], t))
        size[target] += size.pop(c)
        for other, count in neighbours.pop(c).items():
            neighbours[other].pop(c, None)
            if other != target:
                neighbours[target][other] += count
                neighbours[other][target] += count
        for x in range(n):
            if owner[x] == c:
                owner[x] = target
    ranked = sorted((c for c in size if c not in alone), key=lambda c: (-size[c], c))
    label = {c: i for i, c in enumerate(ranked)}
    return [label.get(owner[x], -1) for x in range(n)]


def natural_partition(graph, rivers, min_km=NATURAL_MIN_KM):
    """Cut the junction graph where its chains cross a main river, merge small pieces, and place the extra gates.

    rivers: the main_rivers.geojson document. Returns {labels, extra_gates, info}; an extra gate is one end of a
    non-A crossing, {at, clusters: [its bank, the other bank], way_id, kind, river}, kept only when the banks differ.
    """
    index = _RiverIndex(rivers.get("features", []))
    cut = np.zeros(len(graph.a), dtype=bool)
    if index.segments:
        for e, chains in enumerate(graph.edge_chains):
            cut[e] = any(index.crosses_line(graph.chains[c]["coords"]) for c in chains)
    keep = ~cut
    _, piece = connected_components(coo_matrix((np.ones(int(keep.sum())), (graph.a[keep], graph.b[keep])),
                                               shape=(graph.n, graph.n)), directed=False)
    count = int(piece.max()) + 1 if graph.n else 0
    km = np.bincount(piece[graph.a[keep]], graph.km[keep], count) + np.bincount(piece, graph.self_km, count)
    links = defaultdict(int)
    for a, b in zip(piece[graph.a[cut]], piece[graph.b[cut]]):
        if a != b:
            links[(int(min(a, b)), int(max(a, b)))] += 1
    final = np.array(merge_components(km.tolist(), links, min_km), dtype=np.int64)
    labels = final[piece]

    scale = np.array([math.cos(math.radians(float(graph.lonlat[:, 1].mean()))), 1.0])
    tree = cKDTree(graph.lonlat * scale)
    k = min(BANK_CANDIDATES, graph.n)

    def bank(point):
        _, nearest = tree.query(np.asarray(point) * scale, k=k)
        for j in np.atleast_1d(nearest):
            if labels[j] >= 0 and not index.crosses(tuple(point), tuple(graph.lonlat[j])):
                return int(j)
        return None

    extra, unplaced, same = [], 0, 0
    for crossing in rivers.get("crossings", []):
        if crossing.get("in_a"):
            continue
        ends = [tuple(end) for end in crossing["ends"]]
        nodes = [bank(end) for end in ends]
        if None in nodes:
            unplaced += 1
            continue
        sides = [int(labels[j]) for j in nodes]
        if sides[0] == sides[1]:
            same += 1
            continue
        # The two ends of one crossing are stored next to each other; "node" is the bank junction of the end.
        for end, node, own, other in ((ends[0], nodes[0], sides[0], sides[1]), (ends[1], nodes[1], sides[1], sides[0])):
            extra.append({"at": [round(end[0], 7), round(end[1], 7)], "clusters": [own, other], "node": node,
                          "way_id": crossing["way_id"],
                          "kind": crossing.get("kind", ""), "highway": crossing.get("highway", ""),
                          "river": crossing.get("river", "")})
    named = list(dict.fromkeys(f["properties"].get("river", "") for f in rivers.get("features", [])))
    info = {"cut_edges": int(cut.sum()), "pieces": count, "pieces_below_min": int((km < min_km).sum()), "min_km": min_km,
            "extra_gate_crossings": len(extra) // 2, "crossings_same_cluster": same, "crossings_unplaced": unplaced,
            "rivers": named}
    return {"labels": labels, "extra_gates": extra, "info": info}


def _partition(graph, labels, k, method):
    la, lb = labels[graph.a], labels[graph.b]
    cut = np.flatnonzero((la >= 0) & (lb >= 0) & (la != lb))
    nodes = sorted(set(graph.a[cut].tolist()) | set(graph.b[cut].tolist()))
    return {"k": int(k), "method": method, "ways": way_clusters(graph, labels),
            "node_labels": [[round(float(x), 6), round(float(y), 6), int(c)] for (x, y), c in zip(graph.lonlat, labels)],
            "cuts": [[[round(x, 6), round(y, 6)] for x, y in graph.chains[c]["coords"]] for e in cut for c in graph.edge_chains[e]],
            "boundary": [[round(float(graph.lonlat[i][0]), 6), round(float(graph.lonlat[i][1]), 6)] for i in nodes],
            "stats": stats(graph, labels, k)}


def export(graph, meta, provinces=None, natural=None):
    """The big_road_clusters.json document: the (labels, names) province partition and the natural_partition()
    result, each when given."""
    partitions = []
    if provinces is not None:
        labels, names = provinces
        item = _partition(graph, labels, len(names), "province")
        item["names"] = {str(i): name for i, name in enumerate(names)}
        item["stats"]["unassigned_nodes"] = int((labels < 0).sum())
        partitions.append(item)
    if natural is not None:
        labels = natural["labels"]
        item = _partition(graph, labels, int(labels.max()) + 1 if len(labels) else 0, "natural")
        item["extra_gates"] = natural["extra_gates"]
        item["rivers"] = natural["info"]["rivers"]
        item["natural"] = {key: value for key, value in natural["info"].items() if key != "rivers"}
        item["stats"]["unassigned_nodes"] = int((labels < 0).sum())
        partitions.append(item)
    return {"method": METHOD, "created_at": datetime.now(timezone.utc).isoformat(), "pbf_sha256": meta["pbf_sha256"],
            "policy_version": meta["policy_version"], "partitions": partitions}


def write_json_atomic(path, value):
    path = Path(path)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _provinces_for(path, meta):
    """(province features from the same map, None) or (None, why they are not used)."""
    if not path.is_file():
        return None, f"no {path.name}; run python model/build_provinces.py"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("pbf_sha256") != meta["pbf_sha256"]:
        return None, f"{path.name} comes from another map; run python model/build_provinces.py"
    if not data.get("features"):
        return None, f"{path.name} holds no province"
    return data["features"], None


def _rivers_for(path, meta):
    """(main_rivers document from the same map, None) or (None, why the natural partition is skipped)."""
    if not path.is_file():
        return None, f"no {path.name}; run python model/natural_barriers.py"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("pbf_sha256") != meta["pbf_sha256"]:
        return None, f"{path.name} comes from another map; run python model/natural_barriers.py"
    return data, None


def run(roads, out, natural_min_km=NATURAL_MIN_KM):
    roads = Path(roads)
    meta = json.loads((roads.parent / "big_roads.meta.json").read_text(encoding="utf-8"))
    graph = build_graph(json.loads(roads.read_text(encoding="utf-8"))["features"])
    features, reason = _provinces_for(roads.parent / "vietnam_provinces.geojson", meta)
    if reason:
        print("Province partition skipped:", reason)
    provinces = assign_provinces(graph.lonlat, features) if features else None
    rivers, reason = _rivers_for(roads.parent / "main_rivers.geojson", meta)
    if reason:
        print("Natural partition skipped:", reason)
    natural = natural_partition(graph, rivers, natural_min_km) if rivers else None
    result = export(graph, meta, provinces, natural)
    write_json_atomic(out, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roads", type=Path, default=ROOT / "osrm/data/big_roads.geojson")
    parser.add_argument("--out", type=Path, default=ROOT / "osrm/data/big_road_clusters.json")
    args = parser.parse_args()
    result = run(args.roads, args.out)
    for item in result["partitions"]:
        print(item["method"], item["k"], json.dumps(item["stats"]))


if __name__ == "__main__":
    main()
