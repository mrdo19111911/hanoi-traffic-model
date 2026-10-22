"""Survey rivers and mountains as natural cluster boundaries of the A network (round 4, step 1; survey only).

Reads the same OSM file as the big-road builder and writes reports/natural_barriers.json:
- dividing rivers: type=waterway relations, and waterway=river ways outside them grouped by name and shared
  nodes, with at least --min-river-km of their length inside the country boundary;
- crossings: car-routable ways (as the local OSRM car profile drives them) tagged bridge, tunnel or route=ferry
  whose geometry meets a dividing river, per river, on the A network or not; ways that meet a river without
  such a tag are only listed as suspect_crossings;
- mountains: mountain_pass=yes nodes and tunnels of at least --tunnel-m on the A network;
- components of the A network (big_road_edges.sqlite) cut at every edge meeting a dividing river, then also
  at the passes and long tunnels.
Nothing else reads this report; it decides whether a "natural" partition is worth building (step 2).
"""
import argparse
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
from scipy.sparse.csgraph import connected_components

from build_big_roads import _cells, _crossing_point, _segment_km, inside
from osm_classes import DEFAULT_SPEEDS_KMH
from osm_classes import BIG_ROAD_POLICY_VERSION
from spectral_clusters import _boundary_km, write_json_atomic

ROOT = Path(__file__).resolve().parents[1]
CELL_DEG = 0.02
# Access values the car profile refuses outright (car.lua access_tag_blacklist minus restricted_access_tag_list,
# which it still routes with a penalty).
BLOCKED_ACCESS = {"no", "agricultural", "forestry", "emergency", "psv", "taxi", "share_taxi", "minibus", "bus", "foot",
                  "emergency_vehicle", "restricted", "military", "official"}
ACCESS_HIERARCHY = ("motorcar", "motor_vehicle", "vehicle", "access")
CAR_HIGHWAYS = set(DEFAULT_SPEEDS_KMH)
NOT_RIVER_LINE = {"riverbank", "dam", "weir", "lock_gate", "waterfall", "dock", "boatyard"}
KM_BUCKETS = (1, 10, 50, 200, 1000)
SUSPECT_SAMPLE = 100
LIST_LIMIT = 100


def car_routable(tags):
    """Whether the OSRM car profile would route this way (highway with a profile speed, or a ferry)."""
    if tags.get("route") != "ferry" and tags.get("highway") not in CAR_HIGHWAYS:
        return False
    if tags.get("area") == "yes" or tags.get("impassable") == "yes":
        return False
    if tags.get("highway") == "service" and tags.get("service") == "emergency_access":
        return False
    for key in ACCESS_HIERARCHY:
        value = tags.get(key)
        if value is not None:
            return value not in BLOCKED_ACCESS
    return True


def _tagged(tags, key):
    value = tags.get(key)
    return value is not None and value != "no"


def waterway_relations(pbf):
    """{relation id: {name, waterway, ways}} of type=waterway relations; way members only, riverbanks left out."""
    found = {}
    for relation in osmium.FileProcessor(str(pbf), osmium.osm.RELATION):
        tags = relation.tags
        if tags.get("type") != "waterway":
            continue
        ways = [m.ref for m in relation.members if m.type == "w" and m.role not in ("riverbank", "spring", "mouth")]
        found[relation.id] = {"name": tags.get("name") or tags.get("name:vi") or "", "waterway": tags.get("waterway", ""),
                              "ways": ways}
    return found


def pass_nodes(pbf):
    """{node id: (name, lon, lat)} of mountain_pass=yes nodes."""
    found = {}
    for node in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.TagFilter(("mountain_pass", "yes"))):
        if node.location.valid():
            found[node.id] = (node.tags.get("name") or node.tags.get("name:vi") or "", node.location.lon, node.location.lat)
    return found


class RiverWays(osmium.SimpleHandler):
    """Waterway=river ways and members of waterway relations, with their geometry."""

    def __init__(self, member_ids):
        super().__init__()
        self.member_ids, self.ways = member_ids, {}

    def way(self, way):
        tags = way.tags
        waterway = tags.get("waterway")
        if way.id in self.member_ids:
            if waterway in NOT_RIVER_LINE or tags.get("natural") == "water" or tags.get("area") == "yes":
                return
        elif waterway != "river":
            return
        nodes = [(n.ref, n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(nodes) >= 2:
            self.ways[way.id] = {"name": tags.get("name") or tags.get("name:vi") or "", "nodes": nodes}


def _way_km(nodes):
    return sum(_segment_km((a[1], a[2]), (b[1], b[2])) for a, b in zip(nodes, nodes[1:]))


def group_rivers(relations, ways):
    """[{name, relation_id, way_ids}]: one group per relation, and named/unnamed loose river ways by shared nodes."""
    groups, in_relation = [], set()
    for relation_id, relation in sorted(relations.items()):
        ids = sorted({w for w in relation["ways"] if w in ways})
        in_relation.update(ids)
        if ids:
            groups.append({"name": relation["name"], "relation_id": relation_id, "way_ids": ids})
    loose = sorted(w for w in ways if w not in in_relation)
    parent = {w: w for w in loose}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    first = {}
    for way_id in loose:
        name = ways[way_id]["name"]
        for node_id, _, _ in ways[way_id]["nodes"]:
            other = first.setdefault((name, node_id), way_id)
            if other != way_id:
                parent[find(way_id)] = find(other)
    members = defaultdict(list)
    for way_id in loose:
        members[find(way_id)].append(way_id)
    for root, ids in sorted(members.items()):
        groups.append({"name": ways[root]["name"], "relation_id": None, "way_ids": sorted(ids)})
    return groups


def boundary_rings(path):
    """Outer rings [(lon, lat)] of the country boundary (Polygon or MultiPolygon feature)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    geometry = data["geometry"] if data.get("type") == "Feature" else data["features"][0]["geometry"]
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [[tuple(point) for point in polygon[0]] for polygon in polygons]


def _inside_any(rings, points):
    result = np.zeros(len(points), dtype=bool)
    for ring in rings:
        result |= np.array(inside(ring, points), dtype=bool)
    return result


def dividing_rivers(groups, ways, rings, min_km):
    """(groups at least min_km long inside the rings, sorted by that length; number of shorter groups)."""
    kept, short = [], 0
    for group in groups:
        total = sum(_way_km(ways[w]["nodes"]) for w in group["way_ids"])
        if total < min_km:
            short += 1
            continue
        mids, lengths = [], []
        for w in group["way_ids"]:
            nodes = ways[w]["nodes"]
            for a, b in zip(nodes, nodes[1:]):
                mids.append(((a[1] + b[1]) / 2, (a[2] + b[2]) / 2))
                lengths.append(_segment_km((a[1], a[2]), (b[1], b[2])))
        km = float(np.dot(_inside_any(rings, mids), lengths)) if rings else total
        if km < min_km:
            short += 1
            continue
        kept.append({**group, "km": round(km, 1), "km_total": round(total, 1)})
    kept.sort(key=lambda g: (-g["km"], g["name"], g["way_ids"][0]))
    return kept, short


class RiverGrid:
    """Segments of the dividing rivers in a lon/lat grid; intersection is exact in any affine frame."""

    def __init__(self, rivers, ways, cell=CELL_DEG):
        self.cell, self.segments, self.grid = cell, [], defaultdict(list)
        for index, river in enumerate(rivers):
            for w in river["way_ids"]:
                nodes = ways[w]["nodes"]
                for a, b in zip(nodes, nodes[1:]):
                    seg = len(self.segments)
                    self.segments.append((index, (a[1], a[2]), (b[1], b[2])))
                    for c in _cells((a[1], a[2]), (b[1], b[2]), cell):
                        self.grid[c].append(seg)

    def hits(self, p1, p2):
        """River indices whose geometry meets segment p1-p2 (touching counts)."""
        size = self.cell
        c1 = (math.floor(p1[0] / size), math.floor(p1[1] / size))
        if c1 == (math.floor(p2[0] / size), math.floor(p2[1] / size)):
            candidates = self.grid.get(c1)
            if not candidates:
                return ()
        else:
            candidates = {s for c in _cells(p1, p2, size) for s in self.grid.get(c, ())}
        found = set()
        for seg in candidates:
            river, q1, q2 = self.segments[seg]
            if river not in found and _crossing_point(p1, p2, q1, q2):
                found.add(river)
        return found


class RoadWays(osmium.SimpleHandler):
    """Car-routable ways meeting a dividing river, plus node locations, tunnels and passes of the A network."""

    def __init__(self, grid, a_way_ids, node_ids, pass_ids):
        super().__init__()
        self.grid, self.a_way_ids, self.node_ids, self.pass_ids = grid, a_way_ids, node_ids, pass_ids
        self.lon = np.full(len(node_ids), np.nan)
        self.lat = np.full(len(node_ids), np.nan)
        self.hits, self.tunnels, self.pass_refs = [], [], defaultdict(set)

    def way(self, way):
        tags = way.tags
        on_a = way.id in self.a_way_ids
        car = car_routable(tags)
        if not (car or on_a):
            return
        nodes = [(n.ref, n.lon, n.lat) for n in way.nodes if n.location.valid()]
        if len(nodes) < 2:
            return
        if on_a:
            refs = np.array([n[0] for n in nodes], dtype=np.int64)
            pos = np.searchsorted(self.node_ids, refs).clip(0, len(self.node_ids) - 1)
            match = self.node_ids[pos] == refs
            self.lon[pos[match]] = [n[1] for n, m in zip(nodes, match) if m]
            self.lat[pos[match]] = [n[2] for n, m in zip(nodes, match) if m]
            ref = tags.get("ref", "")
            for node_id, _, _ in nodes:
                if node_id in self.pass_ids:
                    self.pass_refs[node_id].add(ref or tags.get("name", "") or f"({tags.get('highway', '')})")
            if tags.get("tunnel") == "yes":
                self.tunnels.append({"way_id": way.id, "name": tags.get("name", ""), "ref": ref,
                                     "highway": tags.get("highway", ""), "nodes": nodes})
        if not car:
            return
        rivers = defaultdict(list)
        for a, b in zip(nodes, nodes[1:]):
            for river in self.grid.hits((a[1], a[2]), (b[1], b[2])):
                rivers[river].append((min(a[0], b[0]), max(a[0], b[0]), (a[1] + b[1]) / 2, (a[2] + b[2]) / 2,
                                      ((a[1], a[2]), (b[1], b[2]))))
        if not rivers:
            return
        if tags.get("route") == "ferry":
            kind = "ferry"
        elif _tagged(tags, "bridge"):
            kind = "bridge"
        elif _tagged(tags, "tunnel"):
            kind = "tunnel"
        else:
            kind = "suspect"
        for river, pairs in rivers.items():
            self.hits.append({"river": river, "way_id": way.id, "kind": kind, "highway": tags.get("highway") or "ferry",
                              "name": tags.get("name", ""), "ref": tags.get("ref", ""), "ford": tags.get("ford") == "yes",
                              "pairs": pairs})


def load_edges(path):
    """(unique (a, b) pairs as int64 [m, 2], set of way ids, metadata) of big_road_edges.sqlite."""
    db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        pairs = np.array(db.execute("SELECT DISTINCT a, b FROM edges WHERE a != b").fetchall(), dtype=np.int64).reshape(-1, 2)
        way_ids = {row[0] for row in db.execute("SELECT DISTINCT way_id FROM edges")}
        metadata = dict(db.execute("SELECT key, value FROM metadata"))
    finally:
        db.close()
    return pairs, way_ids, metadata


def _edge_index(pairs, node_ids):
    """Compact node indices of each edge end and an int64 key per edge (a dict of pairs would be large)."""
    ia, ib = np.searchsorted(node_ids, pairs[:, 0]), np.searchsorted(node_ids, pairs[:, 1])
    return ia, ib, ia * len(node_ids) + ib


def _lookup(keys_sorted, order, node_ids, pairs):
    """Edge indices of (a, b) pairs, -1 where the pair is not an A edge."""
    if not pairs:
        return np.zeros(0, dtype=np.int64)
    arr = np.array(pairs, dtype=np.int64).reshape(-1, 2)
    pa, pb = np.searchsorted(node_ids, arr[:, 0]).clip(0, len(node_ids) - 1), np.searchsorted(node_ids, arr[:, 1]).clip(0, len(node_ids) - 1)
    known = (node_ids[pa] == arr[:, 0]) & (node_ids[pb] == arr[:, 1])
    key = pa * len(node_ids) + pb
    pos = np.searchsorted(keys_sorted, key).clip(0, max(len(keys_sorted) - 1, 0))
    found = known & (keys_sorted[pos] == key) if len(keys_sorted) else np.zeros(len(arr), dtype=bool)
    return np.where(found, order[pos], -1)


def components(ia, ib, edge_km, lon, lat, keep_edge, keep_node, small_km, list_limit=LIST_LIMIT):
    """Connected components of the kept nodes over the kept edges, largest by km first."""
    n = len(lon)
    use = keep_edge & keep_node[ia] & keep_node[ib]
    graph = coo_matrix((np.ones(int(use.sum())), (ia[use], ib[use])), shape=(n, n))
    _, label = connected_components(graph, directed=False)
    nodes_idx = np.flatnonzero(keep_node)
    labels, compact = np.unique(label[nodes_idx], return_inverse=True)
    count = len(labels)
    node_count = np.bincount(compact, minlength=count)
    comp_of_node = np.full(n, -1)
    comp_of_node[nodes_idx] = compact
    km = np.bincount(comp_of_node[ia[use]], weights=edge_km[use], minlength=count)
    big = np.full(count, -np.inf)
    lon_k, lat_k = lon[nodes_idx], lat[nodes_idx]
    bbox = np.stack([np.full(count, np.inf), np.full(count, np.inf), big.copy(), big.copy()], axis=1)
    ok = ~np.isnan(lon_k)
    np.minimum.at(bbox[:, 0], compact[ok], lon_k[ok])
    np.minimum.at(bbox[:, 1], compact[ok], lat_k[ok])
    np.maximum.at(bbox[:, 2], compact[ok], lon_k[ok])
    np.maximum.at(bbox[:, 3], compact[ok], lat_k[ok])
    order = sorted(range(count), key=lambda c: (-km[c], -node_count[c]))
    listed = [{"km": round(float(km[c]), 2), "nodes": int(node_count[c]),
               "bbox": [round(float(v), 6) if np.isfinite(v) else None for v in bbox[c]]} for c in order[:list_limit]]
    edges_ = (0,) + KM_BUCKETS + (math.inf,)
    histogram = {}
    for low, high in zip(edges_, edges_[1:]):
        label_ = f"{low}-{high}" if high != math.inf else f">={low}"
        histogram[label_] = int(((km >= low) & (km < high)).sum())
    small = km < small_km
    # Nodes left alone between two cut edges (inside a bridge or tunnel) form components without road.
    return {"count": count, "zero_km_count": int((km == 0).sum()), "small_count": int(small.sum()), "small_km_total": round(float(km[small].sum()), 1),
            "large_count": int((~small).sum()), "km_histogram": histogram, "listed": len(listed), "components": listed}


def _tunnel_groups(tunnels):
    """Tunnel ways joined where they share nodes: [(ways, nodes in order of the ways)]."""
    parent = list(range(len(tunnels)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    first = {}
    for index, tunnel in enumerate(tunnels):
        for node_id, _, _ in tunnel["nodes"]:
            other = first.setdefault(node_id, index)
            if other != index:
                parent[find(index)] = find(other)
    groups = defaultdict(list)
    for index in range(len(tunnels)):
        groups[find(index)].append(tunnels[index])
    return list(groups.values())


def river_ends(way_ids, ways):
    """The two farthest dangling ends (nodes used once) over all pieces of a river; all way ends if none dangle."""
    uses, where = Counter(), {}
    for w in way_ids:
        nodes = ways[w]["nodes"]
        for k, (node_id, lon, lat) in enumerate(nodes):
            uses[node_id] += 1 if k in (0, len(nodes) - 1) else 2
            where[node_id] = (lon, lat)
    candidates = [where[n] for n, count in uses.items() if count == 1]
    if len(candidates) < 2:
        candidates = list(dict.fromkeys(where[ways[w]["nodes"][k][0]] for w in way_ids for k in (0, -1)))
    pts = np.array(candidates, dtype=float)
    scale = np.array([math.cos(math.radians(float(pts[:, 1].mean()))), 1.0])
    xy = pts * scale
    best, pair = -1.0, (0, 0)
    for i in range(len(xy)):
        d = ((xy[i + 1:] - xy[i]) ** 2).sum(axis=1)
        if len(d) and d.max() > best:
            best, pair = float(d.max()), (i, i + 1 + int(d.argmax()))
    return [tuple(float(v) for v in pts[pair[0]]), tuple(float(v) for v in pts[pair[1]])]


def _rings_np(rings):
    return [np.asarray(ring, dtype=float) for ring in rings]


def end_anchor(point, land_rings, boundary_rings, coast_km=2.0, border_km=2.0):
    """"outside" (outside the country boundary), "border", "coast" (near or beyond the mainland coast) or None."""
    land, boundary = _rings_np(land_rings), _rings_np(boundary_rings)
    if boundary and not _inside_any(boundary_rings, [point])[0]:
        return "outside"
    if boundary and _boundary_km(point, boundary) <= border_km:
        return "border"
    if land and (_boundary_km(point, land) <= coast_km or not _inside_any(land_rings, [point])[0]):
        return "coast"
    return None


def main_rivers(rivers, ways, land_rings, boundary_rings, coast_km=2.0, border_km=2.0, join_km=1.0):
    """Mark dividing rivers whose two ends are both anchored (coast, border, or a main river), iterated to a fixpoint.

    Sets river["ends"], river["anchors"] and river["main"] in place; returns the main river indices in the order found.
    """
    lines = [[np.array([(lon, lat) for _, lon, lat in ways[w]["nodes"]]) for w in river["way_ids"]] for river in rivers]
    boxes = [(np.vstack(parts).min(axis=0), np.vstack(parts).max(axis=0)) for parts in lines]
    for river in rivers:
        river["ends"] = river_ends(river["way_ids"], ways)
        river["anchors"] = [end_anchor(end, land_rings, boundary_rings, coast_km, border_km) for end in river["ends"]]
        river["main"] = False

    def near_main(index, end, main):
        reach = np.array([join_km / (111.32 * max(math.cos(math.radians(end[1])), 0.1)), join_km / 111.32])
        for other in main:
            lo, hi = boxes[other]
            if other != index and np.all(np.asarray(end) >= lo - reach) and np.all(np.asarray(end) <= hi + reach)                     and _boundary_km(end, lines[other]) <= join_km:
                return other
        return None

    order = []
    while True:
        added = []
        for index, river in enumerate(rivers):
            if river["main"]:
                continue
            anchors = list(river["anchors"])
            for k, end in enumerate(river["ends"]):
                if anchors[k] is None:
                    other = near_main(index, end, order)
                    if other is not None:
                        anchors[k] = f"river:{rivers[other]['name'] or other}"
            if all(anchors):
                added.append((index, anchors))
        if not added:
            return order
        for index, anchors in added:
            rivers[index]["anchors"], rivers[index]["main"] = anchors, True
            order.append(index)


def main_rivers_geojson(rivers, ways, sha, policy_version):
    """Main rivers as LineStrings (one per way) plus every crossing over them, for the cluster file."""
    features, crossings = [], []
    for index, river in enumerate(rivers):
        if not river.get("main"):
            continue
        for w in river["way_ids"]:
            coords = [[round(lon, 7), round(lat, 7)] for _, lon, lat in ways[w]["nodes"]]
            features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                             "properties": {"river": river["name"], "river_index": index, "way_id": w}})
        crossings += [{**c, "river": river["name"], "river_index": index} for c in river["crossings"]]
    return {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": policy_version, "features": features,
            "crossings": crossings}


def survey(pbf, edges_path, boundary_path, min_river_km=100.0, small_km=200.0, tunnel_m=500.0, idx="flex_mem",
           log=lambda *args: None, land_path=None, keep_ways=False):
    """Build the report dict (without metadata) from the OSM file, the A edges and the country boundary."""
    pbf = Path(pbf)
    started = time.monotonic()
    relations = waterway_relations(pbf)
    member_ids = {w for relation in relations.values() for w in relation["ways"]}
    log(f"{len(relations)} waterway relations, {len(member_ids)} member ways ({time.monotonic() - started:.0f} s)")
    river_handler = RiverWays(member_ids)
    river_handler.apply_file(str(pbf), locations=True, idx=idx)
    ways = river_handler.ways
    groups = group_rivers(relations, ways)
    rings = boundary_rings(boundary_path) if boundary_path and Path(boundary_path).is_file() else []
    rivers, short = dividing_rivers(groups, ways, rings, min_river_km)
    log(f"{len(ways)} river ways, {len(groups)} groups, {len(rivers)} dividing ({time.monotonic() - started:.0f} s)")

    pairs, a_way_ids, _ = load_edges(edges_path)
    node_ids = np.unique(pairs)
    ia, ib, keys = _edge_index(pairs, node_ids)
    order = np.argsort(keys, kind="stable")
    keys_sorted = keys[order]
    passes = pass_nodes(pbf)
    grid = RiverGrid(rivers, ways)
    roads = RoadWays(grid, a_way_ids, node_ids, set(passes))
    roads.apply_file(str(pbf), locations=True, idx=idx)
    log(f"{len(roads.hits)} way-river hits ({time.monotonic() - started:.0f} s)")

    lon, lat = roads.lon, roads.lat
    located = ~np.isnan(lon)
    edge_km = np.zeros(len(ia))
    both = located[ia] & located[ib]
    if both.any():
        dx = (lon[ib] - lon[ia]) * np.cos(np.radians((lat[ia] + lat[ib]) / 2))
        edge_km = np.where(both, np.hypot(dx, lat[ib] - lat[ia]) * 111.32, 0.0)

    # River crossings and suspects, with A membership of the edges that meet the river.
    river_cut = np.zeros(len(ia), dtype=bool)
    per_river = [{"crossings": [], "suspects": 0} for _ in rivers]
    suspects = []
    for hit in roads.hits:
        found = _lookup(keys_sorted, order, node_ids, [(a, b) for a, b, *_ in hit["pairs"]])
        in_a = bool((found >= 0).any())
        river_cut[found[found >= 0]] = True
        _, _, x, y, ends = hit["pairs"][0]
        entry = {"way_id": hit["way_id"], "name": hit["name"], "ref": hit["ref"], "highway": hit["highway"],
                 "kind": hit["kind"], "in_a": in_a, "at": [round(x, 6), round(y, 6)],
                 "ends": [[round(lon, 7), round(lat, 7)] for lon, lat in ends]}
        if hit["kind"] == "suspect":
            per_river[hit["river"]]["suspects"] += 1
            suspects.append({**entry, "river": rivers[hit["river"]]["name"], "ford": hit["ford"]})
        else:
            per_river[hit["river"]]["crossings"].append(entry)
    river_report = []
    for river, extra in zip(rivers, per_river):
        crossings = sorted(extra["crossings"], key=lambda c: c["way_id"])
        a_count = sum(1 for c in crossings if c["in_a"])
        river_report.append({**river, "crossing_count": len(crossings), "crossing_a": a_count,
                             "crossing_other": len(crossings) - a_count,
                             "crossings_by_highway": dict(Counter(c["highway"] for c in crossings).most_common()),
                             "crossings_by_kind": dict(Counter(c["kind"] for c in crossings).most_common()),
                             "suspect_count": extra["suspects"], "crossings": crossings})
    land = boundary_rings(land_path) if land_path and Path(land_path).is_file() else []
    main_order = main_rivers(river_report, ways, land, rings)
    log(f"{len(main_order)} main rivers ({time.monotonic() - started:.0f} s)")
    suspects.sort(key=lambda s: (not s["in_a"], s["way_id"]))
    unique_suspects = {s["way_id"] for s in suspects}

    # Mountains: passes on A ways, and tunnel=yes A ways joined where they touch, at least tunnel_m long.
    hit_ways = {hit["way_id"] for hit in roads.hits}
    pass_list = [{"node_id": node_id, "name": passes[node_id][0], "refs": sorted(refs),
                  "at": [round(passes[node_id][1], 6), round(passes[node_id][2], 6)]}
                 for node_id, refs in sorted(roads.pass_refs.items())]
    pass_idx = np.searchsorted(node_ids, np.array([p["node_id"] for p in pass_list], dtype=np.int64))
    tunnel_list, under_river, mountain_cut = [], 0, np.zeros(len(ia), dtype=bool)
    for group in _tunnel_groups(roads.tunnels):
        metres = sum(_way_km(t["nodes"]) for t in group) * 1000
        if metres < tunnel_m:
            continue
        if any(t["way_id"] in hit_ways for t in group):
            under_river += 1
            continue
        nodes = [n for t in group for n in t["nodes"]]
        mid = nodes[len(nodes) // 2]
        found = _lookup(keys_sorted, order, node_ids, [(min(a[0], b[0]), max(a[0], b[0]))
                                                        for t in group for a, b in zip(t["nodes"], t["nodes"][1:])])
        mountain_cut[found[found >= 0]] = True
        tunnel_list.append({"way_id": min(t["way_id"] for t in group), "way_ids": sorted(t["way_id"] for t in group),
                            "name": next((t["name"] for t in group if t["name"]), ""),
                            "ref": next((t["ref"] for t in group if t["ref"]), ""), "m": round(metres),
                            "at": [round(mid[1], 6), round(mid[2], 6)]})
    tunnel_list.sort(key=lambda t: -t["m"])

    keep_all = np.ones(len(ia), dtype=bool)
    nodes_all = np.ones(len(node_ids), dtype=bool)
    nodes_mountain = nodes_all.copy()
    nodes_mountain[pass_idx] = False
    baseline = components(ia, ib, edge_km, lon, lat, keep_all, nodes_all, small_km)
    by_river = components(ia, ib, edge_km, lon, lat, ~river_cut, nodes_all, small_km)
    by_both = components(ia, ib, edge_km, lon, lat, ~(river_cut | mountain_cut), nodes_mountain, small_km)
    by_river["cut_edges"] = int(river_cut.sum())
    by_both.update({"cut_edges": int((river_cut | mountain_cut).sum()), "removed_nodes": len(pass_list),
                    "tunnel_cut_edges": int((mountain_cut & ~river_cut).sum())})
    log(f"components {baseline['count']} -> {by_river['count']} -> {by_both['count']} ({time.monotonic() - started:.0f} s)")
    result = {
        "rivers": river_report, "river_count": len(river_report), "candidates_below_threshold": short,
        "main_rivers": [river_report[i]["name"] for i in main_order], "main_river_count": len(main_order),
        "river_groups": len(groups), "river_ways": len(ways), "waterway_relations": len(relations),
        "crossing_total": sum(r["crossing_count"] for r in river_report),
        "crossing_a_total": sum(r["crossing_a"] for r in river_report),
        "suspect_crossings": {"count": len(unique_suspects), "hits": len(suspects),
                              "in_a": len({s["way_id"] for s in suspects if s["in_a"]}),
                              "ford": len({s["way_id"] for s in suspects if s["ford"]}),
                              "by_highway": dict(Counter(s["highway"] for s in suspects).most_common()),
                              "sample": suspects[:SUSPECT_SAMPLE]},
        "mountains": {"pass_count": len(pass_list), "pass_nodes_total": len(passes), "passes": pass_list,
                      "tunnel_count": len(tunnel_list), "tunnels_under_rivers": under_river, "tunnels": tunnel_list},
        "a_network": {"edges": int(len(ia)), "nodes": int(len(node_ids)), "km": round(float(edge_km.sum()), 1),
                      "unlocated_nodes": int((~located).sum())},
        "components": {"baseline": baseline, "rivers": by_river, "rivers_mountains": by_both},
    }
    if keep_ways:
        result["_ways"] = ways
    return result


def _sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run(pbf, edges_path, boundary_path, out, build_json=ROOT / "osrm/build.json", min_river_km=100.0, small_km=200.0,
        tunnel_m=500.0, log=lambda *args: None, land_path=None, rivers_out=None):
    """Check that the inputs come from the same map, survey and write the report (and the main rivers) atomically."""
    started = time.monotonic()
    sha = _sha256(pbf)
    _, _, metadata = load_edges(edges_path)
    if metadata.get("pbf_sha256") != sha:
        raise ValueError(f"{Path(edges_path).name} comes from another map; run python model/build_big_roads.py")
    if build_json is not None:
        build = json.loads(Path(build_json).read_text(encoding="utf-8"))
        if build.get("input", {}).get("pbf_sha256") != sha:
            raise ValueError("osrm/build.json comes from another map; run python osrm/manage.py setup --rebuild")
    report = survey(pbf, edges_path, boundary_path, min_river_km, small_km, tunnel_m, log=log, land_path=land_path,
                    keep_ways=rivers_out is not None)
    ways = report.pop("_ways", None)
    report["metadata"] = {"pbf_sha256": sha, "parameters": {"min_river_km": min_river_km, "small_km": small_km,
                                                            "tunnel_m": tunnel_m, "cell_deg": CELL_DEG},
                          "runtime_s": round(time.monotonic() - started, 1),
                          "built_at": datetime.now(timezone.utc).isoformat()}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, report)
    if rivers_out is not None:
        policy = int(metadata.get("policy_version", BIG_ROAD_POLICY_VERSION))
        Path(rivers_out).parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(rivers_out, main_rivers_geojson(report["rivers"], ways, sha, policy))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pbf", type=Path, default=ROOT / "osrm/data/vietnam-latest.osm.pbf")
    parser.add_argument("--edges", type=Path, default=ROOT / "osrm/data/big_road_edges.sqlite")
    parser.add_argument("--boundary", type=Path, default=ROOT / "osrm/data/vietnam_boundary.geojson")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/natural_barriers.json")
    parser.add_argument("--land", type=Path, default=ROOT / "osrm/data/vietnam_land.geojson")
    parser.add_argument("--rivers-out", type=Path, default=ROOT / "osrm/data/main_rivers.geojson",
                        help="main rivers and their crossings, read by model/spectral_clusters.py")
    parser.add_argument("--min-river-km", type=float, default=100.0, help="shortest dividing river inside Vietnam")
    parser.add_argument("--small-km", type=float, default=200.0, help="components with less A road count as small")
    parser.add_argument("--tunnel-m", type=float, default=500.0, help="shortest tunnel counted as a mountain crossing")
    args = parser.parse_args()
    if min(args.min_river_km, args.small_km, args.tunnel_m) < 0:
        parser.error("Lengths must be non-negative")
    report = run(args.pbf, args.edges, args.boundary, args.out, min_river_km=args.min_river_km, small_km=args.small_km,
                 tunnel_m=args.tunnel_m, log=lambda *a: print(*a, flush=True), land_path=args.land, rivers_out=args.rivers_out)
    summary = {
        "rivers": [(r["name"], r["km"], r["crossing_a"], r["crossing_other"]) for r in report["rivers"][:20]],
        "main_rivers": report["main_rivers"],
        "suspects": report["suspect_crossings"]["count"], "passes": report["mountains"]["pass_count"],
        "tunnels": report["mountains"]["tunnel_count"],
        "components": {k: (v["count"], v["small_count"]) for k, v in report["components"].items()},
        "runtime_s": report["metadata"]["runtime_s"]}
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
