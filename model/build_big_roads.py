"""Build the big-road (A) network and its junctions with B/C roads from an OSM file.

Outputs big_roads.geojson, big_road_junctions.geojson, big_roads.meta.json, the mainland ring vietnam_land.geojson
and the country boundary (territorial sea included) vietnam_boundary.geojson. Every road carries its free-flow
speed as the local OSRM car profile drives it. big_road_edges.sqlite indexes the OSM node pairs of the network,
bridges and ferries included, so a route OSRM drives can be split into its parts on and off the network.
Geometry is simplified for display; lengths come from the original geometry. Short gaps where OSM
lost a road's ref are bridged along the real B/C road. The remaining dead ends are then bridged
along the route local OSRM drives (ferries and small roads included), so the big-road map agrees with
routing; whatever is still a dead end is reported with its reason.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import urllib.error
import urllib.request

import osmium

from osm_classes import BIG_ROAD_POLICY_VERSION, BIG_ROAD_TAGS, DRIVABLE_TAGS, big_road_group, free_flow_speed

ROOT = Path(__file__).resolve().parents[1]
B_TAGS = {"primary", "primary_link", "secondary", "secondary_link"}
C_TAGS = {"tertiary", "tertiary_link"}
METRES_PER_DEGREE = 111320
GROUP_RANK = {"caotoc": 0, "quoclo": 1, "tinhlo": 2}
ROAD_TAGS = B_TAGS | C_TAGS | DRIVABLE_TAGS | set(BIG_ROAD_TAGS) | {"track"}
CAR_VALUES = {"yes", "designated"}


def country_relation(pbf):
    """Members [(type, ref, role)] of the boundary=administrative admin_level=2 ISO3166-1=VN relation, or None.

    A relation-only pass, so the main pass can pick the relation's ways by id, tagged or not.
    """
    for relation in osmium.FileProcessor(str(pbf), osmium.osm.RELATION):
        tags = relation.tags
        if tags.get("boundary") == "administrative" and tags.get("admin_level") == "2" and tags.get("ISO3166-1") == "VN":
            return [(member.type, member.ref, member.role) for member in relation.members]
    return None


class BigRoadWays(osmium.SimpleHandler):
    """One pass: A-network ways, B/C ways, country-border ways, coastline and the outer ways of the VN relation."""

    def __init__(self, outer_ids=()):
        super().__init__()
        self.ways = []
        self.local = []
        self.border_ways = {}
        self.coast = []
        self.outer_ids, self.outer_ways = set(outer_ids), {}

    def way(self, way):
        tags = way.tags
        if way.id in self.outer_ids:
            self.outer_ways[way.id] = _located(way)
        if tags.get("boundary") == "administrative" and tags.get("admin_level") == "2":
            self.border_ways[way.id] = (tags.get("maritime") == "yes", _located(way))
        if tags.get("natural") == "coastline":
            self.coast.append(_located(way))
        highway, ref = tags.get("highway", ""), tags.get("ref", "")
        group = big_road_group(highway, ref)
        if not group and highway not in B_TAGS | C_TAGS:
            return
        nodes = [(node.ref, (node.lon, node.lat) if node.location.valid() else None) for node in way.nodes]
        (self.ways if group else self.local).append({"way_id": way.id, "group": group, "highway": highway, "ref": ref,
            "name": tags.get("name", ""), "oneway_tag": tags.get("oneway", ""), "maxspeed": tags.get("maxspeed", ""),
            "nodes": nodes})


def _located(way):
    return [(node.ref, (node.lon, node.lat)) for node in way.nodes if node.location.valid()]


def _segment_km(first, second):
    dx = (second[0] - first[0]) * math.cos(math.radians((first[1] + second[1]) / 2))
    return math.hypot(dx, second[1] - first[1]) * METRES_PER_DEGREE / 1000


def _douglas_peucker(points, tolerance_m):
    """Indices to keep from a chain whose two ends are always kept."""
    if len(points) <= 2:
        return list(range(len(points)))
    scale = math.cos(math.radians(points[0][1])) * METRES_PER_DEGREE
    xy = [(lon * scale, lat * METRES_PER_DEGREE) for lon, lat in points]
    keep, stack = {0, len(xy) - 1}, [(0, len(xy) - 1)]
    while stack:
        start, end = stack.pop()
        (x1, y1), (x2, y2) = xy[start], xy[end]
        length = math.hypot(x2 - x1, y2 - y1)
        best, best_index = -1.0, None
        for index in range(start + 1, end):
            x0, y0 = xy[index]
            if length == 0:
                distance = math.hypot(x0 - x1, y0 - y1)
            else:
                distance = abs((x2 - x1) * (y1 - y0) - (x1 - x0) * (y2 - y1)) / length
            if distance > best:
                best, best_index = distance, index
        if best_index is not None and best > tolerance_m:
            keep.add(best_index)
            stack += [(start, best_index), (best_index, end)]
    return sorted(keep)


def simplify(points, fixed, tolerance_m):
    """Simplify between fixed indices, so junctions and shared nodes survive."""
    anchors = sorted({0, len(points) - 1, *fixed})
    kept = [0]
    for start, end in zip(anchors, anchors[1:]):
        kept += [start + i for i in _douglas_peucker(points[start:end + 1], tolerance_m)[1:]]
    return [points[i] for i in kept]


def _label(way):
    if way["name"] and way["ref"]:
        return f"{way['name']} ({way['ref']})"
    return way["name"] or way["ref"] or f"({way['highway']})"


def _components(ways):
    parent = list(range(len(ways)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    first_way = {}
    for index, way in enumerate(ways):
        for node_id, _ in way["nodes"]:
            other = first_way.setdefault(node_id, index)
            if other != index:
                parent[find(index)] = find(other)
    roots = [find(index) for index in range(len(ways))]
    km = Counter()
    for index, root in enumerate(roots):
        km[root] += ways[index]["km"]
    order = {root: number for number, (root, _) in enumerate(sorted(km.items(), key=lambda item: (-item[1], item[0])))}
    return [order[root] for root in roots], km


def dead_ends(ways):
    """(node_id, way index) for each end of an open A way that no other A way touches."""
    uses = Counter(node_id for way in ways for node_id in {n for n, _ in way["nodes"]})
    found = []
    for index, way in enumerate(ways):
        ids = [node_id for node_id, _ in way["nodes"]]
        if ids[0] == ids[-1]:
            continue
        for end in (ids[0], ids[-1]):
            if uses[end] == 1 and ids.count(end) == 1:
                found.append((end, index))
    return found


def bridge_gaps(ways, local, ends, max_km):
    """Bridge each dead end along B/C roads to the nearest node of another A way, at most max_km away.

    Returns (bridge ways, {local index: used segment indices}). A way's own nodes block the search, so a
    side road looping back to the same way is no bridge.
    """
    a_ways = defaultdict(set)
    for index, way in enumerate(ways):
        for node_id, _ in way["nodes"]:
            a_ways[node_id].add(index)
    adjacent = defaultdict(list)
    for li, way in enumerate(local):
        nodes = way["nodes"]
        for seg, ((a, pa), (b, pb)) in enumerate(zip(nodes, nodes[1:])):
            km = _segment_km(pa, pb)
            adjacent[a].append((b, km, li, seg))
            adjacent[b].append((a, km, li, seg))
    used, group = defaultdict(set), {}
    for start, index in ends:
        own = {node_id for node_id, _ in ways[index]["nodes"]}
        best, previous, heap = {start: 0.0}, {}, [(0.0, start)]
        while heap:
            km, node = heapq.heappop(heap)
            if km > best.get(node, math.inf):
                continue
            if node != start and a_ways.get(node) and index not in a_ways[node]:
                while node != start:
                    node, li, seg = previous[node]
                    used[li].add(seg)
                    rank = GROUP_RANK[ways[index]["group"]]
                    if li not in group or rank < GROUP_RANK[group[li]]:
                        group[li] = ways[index]["group"]
                break
            if node != start and node in own:
                continue
            for neighbour, step, li, seg in adjacent.get(node, ()):
                total = km + step
                if total <= max_km and total < best.get(neighbour, math.inf):
                    best[neighbour], previous[neighbour] = total, (node, li, seg)
                    heapq.heappush(heap, (total, neighbour))
    bridges = []
    for li in sorted(used):
        segs = sorted(used[li])
        runs = [[segs[0], segs[0]]]
        for seg in segs[1:]:
            if seg == runs[-1][1] + 1:
                runs[-1][1] = seg
            else:
                runs.append([seg, seg])
        for first, last in runs:
            bridges.append({**local[li], "group": group[li], "nodes": local[li]["nodes"][first:last + 2], "bridge": True})
    return bridges, used


class NoRoute(ValueError):
    """OSRM found no route between two points; another candidate may still have one."""


class OsrmRouter:
    """Local OSRM /table and /route, so dead ends are bridged the way routing would drive."""

    def __init__(self, base=None, root=ROOT, timeout=10):
        self.base = (base or os.environ.get("OSRM_BASE_URL", "http://127.0.0.1:5000")).rstrip("/")
        self.root, self.timeout = Path(root), timeout

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            # OSRM answers 4xx with a JSON code and message (NoRoute, NoSegment...); keep them for the report.
            try:
                body = json.load(exc)
            except ValueError:
                body = {}
            error = NoRoute if body.get("code") == "NoRoute" else ValueError
            raise error(f"OSRM {exc.code} {body.get('code', '')}: {body.get('message', '')}") from exc
        if data.get("code") != "Ok":
            raise ValueError(f"OSRM answered {data.get('code')}")
        return data

    def unavailable(self, pbf_sha256):
        """Why OSRM cannot be used for this PBF (Vietnamese, shown in the UI), or None."""
        try:
            build = json.loads((self.root / "osrm/build.json").read_text(encoding="utf-8"))
            if build["input"]["pbf_sha256"] != pbf_sha256:
                return "OSRM dựng từ bản OSM khác"
        except (OSError, ValueError, KeyError, TypeError):
            return "không đọc được osrm/build.json"
        try:
            self._get("/nearest/v1/driving/105.85,21.03?number=1")
        except (OSError, ValueError):
            return "OSRM không chạy"
        return None

    def table(self, points):
        coords = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in points)
        return self._get(f"/table/v1/driving/{coords}?annotations=distance")["distances"]

    def route(self, a, b):
        data = self._get(f"/route/v1/driving/{a[0]:.7f},{a[1]:.7f};{b[0]:.7f},{b[1]:.7f}"
                         "?overview=full&geometries=geojson&annotations=nodes,distance,duration")
        route = data["routes"][0]
        annotation = route["legs"][0]["annotation"]
        coords = route["geometry"]["coordinates"]
        if len(coords) != len(annotation["nodes"]) or len(annotation["nodes"]) != len(annotation["distance"]) + 1:
            raise ValueError("OSRM geometry and annotation nodes do not align")
        return {"nodes": annotation["nodes"], "coords": coords, "distances": annotation["distance"],
                "durations": annotation["duration"]}


def _reached_along(start, adjacent, ways_at, limit_km):
    """{A way index: shortest distance from start along the A network}, for ways reached within limit_km."""
    best, heap, reached = {start: 0.0}, [(0.0, start)], {}
    while heap:
        km, node = heapq.heappop(heap)
        if km > best.get(node, math.inf):
            continue
        for index in ways_at[node]:
            reached.setdefault(index, km)
        for neighbour, step in adjacent[node]:
            total = km + step
            if total <= limit_km and total < best.get(neighbour, math.inf):
                best[neighbour] = total
                heapq.heappush(heap, (total, neighbour))
    return reached


def _candidates(ways, ends, radius_km=3.0, limit=5, cell=0.03, along_km=5.0, detour=2.0):
    """For each dead end, the nearest node of up to `limit` other A ways within radius_km.

    A way already reached along the big-road network in at most `detour` times the straight-line distance is
    left out: bridging to it adds nothing, and in a town such ways would crowd out the road across the river.
    """
    grid, adjacent, ways_at = defaultdict(list), defaultdict(list), defaultdict(set)
    for index, way in enumerate(ways):
        for node_id, loc in way["nodes"]:
            grid[(math.floor(loc[0] / cell), math.floor(loc[1] / cell))].append((node_id, loc, index))
            ways_at[node_id].add(index)
        for (a, pa), (b, pb) in zip(way["nodes"], way["nodes"][1:]):
            km = _segment_km(pa, pb)
            adjacent[a].append((b, km))
            adjacent[b].append((a, km))
    found = {}
    for node_id, index in ends:
        loc = next(l for n, l in ways[index]["nodes"] if n == node_id)
        nearby = _reached_along(node_id, adjacent, ways_at, along_km)
        gx, gy = math.floor(loc[0] / cell), math.floor(loc[1] / cell)
        best = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other_id, other_loc, other in grid.get((gx + dx, gy + dy), ()):
                    if other == index:
                        continue
                    km = _segment_km(loc, other_loc)
                    if other in nearby and nearby[other] <= detour * km:
                        continue
                    if km <= radius_km and (other not in best or km < best[other][0]):
                        best[other] = (km, other_id, other_loc)
        found[(node_id, index)] = (loc, [value[2] for value in sorted(best.values())[:limit]])
    return found


def _trim(nodes, ways_at, a_pairs, own):
    """(start, end) indices of the part of an OSRM route off the A network, from the dead end's way to another A way."""
    i = 0
    while i + 1 < len(nodes) and (nodes[i], nodes[i + 1]) in a_pairs:
        if own not in ways_at.get(nodes[i + 1], ()):
            return None
        i += 1
    for j in range(i + 1, len(nodes)):
        hit = ways_at.get(nodes[j])
        if hit:
            return None if hit == {own} else (i, j)
    return None


def osrm_routes(ways, ends, router):
    """Ask the router, both ways, for a route from each dead end to the nearest other A way.

    Returns (routes trimmed to their off-network part, error samples). Each route starts at the dead end's way;
    an error skips that dead end and is kept as {node_id, at, error} for the build report.
    """
    ways_at = defaultdict(set)
    a_pairs = set()
    for index, way in enumerate(ways):
        ids = [node_id for node_id, _ in way["nodes"]]
        for node_id in ids:
            ways_at[node_id].add(index)
        a_pairs.update(zip(ids, ids[1:]))
        a_pairs.update(zip(ids[1:], ids))
    routes, errors = [], []
    for (node_id, index), (loc, targets) in _candidates(ways, ends).items():
        if not targets:
            continue
        try:
            distances = router.table([loc] + targets)
            for reverse in (False, True):
                options = [(distances[j][0] if reverse else distances[0][j], j) for j in range(1, len(targets) + 1)]
                # The nearest target is often reached back along the dead end's own road, which is no bridge,
                # so try the targets in order until a route leaves the big-road network.
                for _, j in sorted(option for option in options if option[0] is not None):
                    # /table can reach a point that /route then cannot (snapping differs); try the next one.
                    try:
                        trip = router.route(targets[j - 1], loc) if reverse else router.route(loc, targets[j - 1])
                    except NoRoute:
                        continue
                    if reverse:
                        trip = {key: list(reversed(value)) for key, value in trip.items()}
                    cut = _trim(trip["nodes"], ways_at, a_pairs, index)
                    if cut:
                        start, end = cut
                        routes.append({"own": index, "nodes": trip["nodes"][start:end + 1], "coords": trip["coords"][start:end + 1],
                                       "distances": trip["distances"][start:end], "durations": trip["durations"][start:end]})
                        break
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append({"node_id": node_id, "at": [round(loc[0], 6), round(loc[1], 6)], "error": f"{type(exc).__name__}: {exc}"[:200]})
    return routes, errors


def way_tags(pbf, node_ids):
    """One way-only pass: tags of non-A ways at the given nodes.

    Returns ({(a, b): (way_id, tags, forward)} for adjacent pairs of those nodes, {node: [tags of ways touching it]}).
    """
    pairs, touching = {}, defaultdict(list)
    for way in osmium.FileProcessor(str(pbf), osmium.osm.WAY):
        refs = [node.ref for node in way.nodes]
        if node_ids.isdisjoint(refs):
            continue
        tags = {tag.k: tag.v for tag in way.tags}
        if big_road_group(tags.get("highway", ""), tags.get("ref", "")):
            continue
        for a, b in zip(refs, refs[1:]):
            if a in node_ids and b in node_ids:
                pairs.setdefault((a, b), (way.id, tags, True))
                pairs.setdefault((b, a), (way.id, tags, False))
        for node_id in set(refs) & node_ids:
            touching[node_id].append(tags)
    return pairs, touching


def _bridge_kind(tags):
    if tags.get("route") == "ferry":
        return "ferry"
    return "road" if tags.get("highway") in B_TAGS | C_TAGS else "small"


def accept_routes(routes, pairs, ways, bridge_km, ferry_km):
    """Turn OSRM routes into bridge ways, one per OSM way stretch; returns (bridge ways, accepted, unmatched)."""
    bridges, accepted, unmatched, used, seen = [], 0, 0, set(), set()
    for route in routes:
        segments = list(zip(route["nodes"], route["nodes"][1:]))
        # Both dead ends of a gap usually find the same route; judge and count it once.
        key = frozenset(frozenset(segment) for segment in segments)
        if key in seen:
            continue
        seen.add(key)
        info = [pairs.get(segment) for segment in segments]
        if any(item is None for item in info):
            unmatched += 1
            continue
        kinds = [_bridge_kind(item[1]) for item in info]
        road = sum(d for d, kind in zip(route["distances"], kinds) if kind != "ferry") / 1000
        ferry = sum(d for d, kind in zip(route["distances"], kinds) if kind == "ferry") / 1000
        if road > bridge_km or ferry > ferry_km:
            continue
        new = [k for k, segment in enumerate(segments) if frozenset(segment) not in used]
        if not new:
            continue
        accepted += 1
        used.update(frozenset(segments[k]) for k in new)
        runs = []
        for k in new:
            if runs and runs[-1][-1] == k - 1 and info[k][0] == info[k - 1][0]:
                runs[-1].append(k)
            else:
                runs.append([k])
        group = ways[route["own"]]["group"]
        for run in runs:
            way_id, tags, forward = info[run[0]]
            nodes = [(route["nodes"][k], tuple(route["coords"][k])) for k in run + [run[-1] + 1]]
            if not forward:
                nodes.reverse()
            kind = kinds[run[0]]
            bridge = {"way_id": way_id, "group": group, "highway": tags.get("highway") or ("ferry" if kind == "ferry" else ""),
                      "ref": tags.get("ref", ""), "name": tags.get("name", ""), "oneway_tag": tags.get("oneway", ""),
                      "maxspeed": tags.get("maxspeed", ""), "nodes": nodes, "bridge": True, "bridge_kind": kind}
            if kind == "ferry":
                bridge.update({"ferry_name": tags.get("name", ""),
                               "car_confirmed": tags.get("motor_vehicle") in CAR_VALUES or tags.get("motorcar") in CAR_VALUES,
                               "ferry_min": math.ceil(sum(route["durations"][k] for k in run) / 60),
                               "ferry_min_source": "osm" if tags.get("duration") else "osrm_5kmh"})
            bridges.append(bridge)
    return bridges, accepted, unmatched


def dead_end_reason(tags_list):
    if any(tags.get("highway") == "construction" or "construction" in tags for tags in tags_list):
        return "construction"
    if any(tags.get("highway") in ROAD_TAGS or tags.get("route") == "ferry" for tags in tags_list):
        return "continues"
    return "end"


def merge_chains(chains, key):
    """Join node chains end to end, reversing pieces as needed. Closed rings stay as they are."""
    pieces = [list(chain) for chain in chains if len(chain) > 1]
    while True:
        ends = defaultdict(list)
        for index, chain in enumerate(pieces):
            if key(chain[0]) == key(chain[-1]):
                continue
            ends[key(chain[0])].append(index)
            ends[key(chain[-1])].append(index)
        pair = next((indices[:2] for indices in ends.values() if len(indices) >= 2 and indices[0] != indices[1]), None)
        if not pair:
            return pieces
        first, second = pieces[pair[0]], pieces[pair[1]]
        joint = key(first[0]) if key(first[0]) in (key(second[0]), key(second[-1])) else key(first[-1])
        if key(first[-1]) != joint:
            first.reverse()
        if key(second[0]) != joint:
            second.reverse()
        pieces[pair[0]] = first + second[1:]
        pieces.pop(pair[1])


def assemble_land(border_ways, members, coast, tolerance_m, max_gap_km=5.0):
    """Mainland ring = land border chain + the coastline stretch between its two ends.

    Returns (ring or None, status, gap_km, land border chain or None). The OSM country border follows the
    territorial sea, so maritime border ways are replaced by the coastline.
    """
    if not members:
        return None, "failed: no admin_level=2 ISO3166-1=VN relation", None, None
    land = [border_ways[way_id][1] for way_id in members if way_id in border_ways and not border_ways[way_id][0]]
    chains = [chain for chain in merge_chains(land, key=lambda point: point[0]) if chain[0][0] != chain[-1][0]]
    if not chains:
        return None, "failed: no open land-border chain", None, None
    border = max(chains, key=len)
    by_node = merge_chains(coast, key=lambda point: point[0])
    open_coast = [chain for chain in by_node if chain[0][0] != chain[-1][0]]
    open_coast = merge_chains(open_coast, key=lambda point: (round(point[1][0], 7), round(point[1][1], 7)))
    if not open_coast:
        return None, "failed: no open coastline chain", None, None
    shore = max(open_coast, key=len)
    start, end = border[0][1], border[-1][1]
    near_start = min(range(len(shore)), key=lambda i: _segment_km(shore[i][1], start))
    near_end = min(range(len(shore)), key=lambda i: _segment_km(shore[i][1], end))
    gap = max(_segment_km(shore[near_start][1], start), _segment_km(shore[near_end][1], end))
    if gap > max_gap_km:
        return None, f"failed: land border ends {gap:.1f} km from the coastline", round(gap, 3), None
    stretch = shore[near_end:near_start + 1] if near_end <= near_start else shore[near_start:near_end + 1][::-1]
    points = [loc for _, loc in border] + [loc for _, loc in stretch] + [border[0][1]]
    joints = [len(border) - 1, len(border), len(border) + len(stretch) - 1]
    return simplify(points, joints, tolerance_m), "ok", round(gap, 3), border


def _ring_area(points):
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:]))) / 2


def assemble_boundary(outer_ways, outer_ids, tolerance_m):
    """Country boundary rings from the relation's outer ways, maritime ones included (the territorial sea).

    Ways are joined by node id, reversed as needed. Every closed ring is kept (mainland, island waters), largest
    first, unless it collapses when simplified. Returns (rings or None, status).
    """
    if not outer_ids:
        return None, "failed: no admin_level=2 ISO3166-1=VN relation"
    chains = merge_chains([outer_ways[way_id] for way_id in outer_ids if way_id in outer_ways], key=lambda point: point[0])
    closed = [[loc for _, loc in chain] for chain in chains if len(chain) > 3 and chain[0][0] == chain[-1][0]]
    if not closed:
        return None, f"failed: outer ways do not close into a ring ({len(outer_ways)} of {len(set(outer_ids))} ways in the file)"
    rings = [ring for ring in (simplify(ring, [], tolerance_m) for ring in sorted(closed, key=_ring_area, reverse=True)) if len(ring) >= 4]
    if not rings:
        return None, "failed: boundary rings collapse when simplified"
    return rings, "ok"


def _crossing_point(p1, p2, q1, q2):
    """Intersection of segments p1-p2 and q1-q2 (touching counts), or None when they miss or are parallel."""
    rx, ry, sx, sy = p2[0] - p1[0], p2[1] - p1[1], q2[0] - q1[0], q2[1] - q1[1]
    denom = rx * sy - ry * sx
    if denom == 0:
        return None
    qx, qy = q1[0] - p1[0], q1[1] - p1[1]
    t, u = (qx * sy - qy * sx) / denom, (qx * ry - qy * rx) / denom
    if 0 <= t <= 1 and 0 <= u <= 1:
        return (p1[0] + t * rx, p1[1] + t * ry)
    return None


def _cells(first, second, size):
    x0, x1 = sorted((first[0], second[0]))
    y0, y1 = sorted((first[1], second[1]))
    return [(x, y) for x in range(math.floor(x0 / size), math.floor(x1 / size) + 1)
            for y in range(math.floor(y0 / size), math.floor(y1 / size) + 1)]


def border_crossings(ways, border, controls, cell_deg=0.05, merge_km=0.2, name_km=2.0):
    """Points where A-network ways cross the land border, merged per crossing and named after border_control nodes."""
    grid = defaultdict(list)
    for index, (a, b) in enumerate(zip(border, border[1:])):
        for cell in _cells(a[1], b[1], cell_deg):
            grid[cell].append(index)
    border_nodes = {node_id for node_id, _ in border}
    found = []
    for way in ways:
        label = _label(way)
        for node_id, loc in way["nodes"]:
            if node_id in border_nodes:
                found.append((loc, node_id, label))
        for (_, p1), (_, p2) in zip(way["nodes"], way["nodes"][1:]):
            candidates = {index for cell in _cells(p1, p2, cell_deg) for index in grid.get(cell, ())}
            for index in candidates:
                point = _crossing_point(p1, p2, border[index][1], border[index + 1][1])
                if point:
                    found.append((point, None, label))
    clusters = []
    for loc, node_id, label in found:
        cluster = next((c for c in clusters if _segment_km(c["points"][0], loc) < merge_km), None)
        if cluster is None:
            cluster = {"points": [], "node_ids": set(), "labels": set()}
            clusters.append(cluster)
        if not any(_segment_km(p, loc) < 1e-6 for p in cluster["points"]):
            cluster["points"].append(loc)
        cluster["node_ids"].update({node_id} - {None})
        cluster["labels"].add(label)
    features = []
    for cluster in clusters:
        lon = sum(p[0] for p in cluster["points"]) / len(cluster["points"])
        lat = sum(p[1] for p in cluster["points"]) / len(cluster["points"])
        named = sorted((_segment_km((c_lon, c_lat), (lon, lat)), name) for c_lon, c_lat, name in controls)
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": {"node_id": min(cluster["node_ids"]) if cluster["node_ids"] else None, "type": "border", "joins": [],
                           "big_road": " / ".join(sorted(cluster["labels"])),
                           "crossing": named[0][1] if named and named[0][0] <= name_km else "", "island": False}})
    return sorted(features, key=lambda f: f["geometry"]["coordinates"])


def border_controls(pbf):
    """Named barrier=border_control nodes, read with a C++-side tag filter."""
    found = []
    for node in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.TagFilter(("barrier", "border_control"))):
        if node.tags.get("name") and node.location.valid():
            found.append((node.location.lon, node.location.lat, node.tags.get("name")))
    return found


def inside(ring, points):
    """Even-odd ray casting of (lon, lat) points against one ring, vectorised over edges."""
    if not points:
        return []
    import numpy as np
    vertices = np.asarray(ring, dtype=float)
    x1, y1 = vertices[:-1, 0], vertices[:-1, 1]
    x2, y2 = vertices[1:, 0], vertices[1:, 1]
    result = []
    for offset in range(0, len(points), 2000):
        chunk = np.asarray(points[offset:offset + 2000], dtype=float)
        px, py = chunk[:, :1], chunk[:, 1:]
        crosses = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            hit_x = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        result += list((crosses & (px < hit_x)).sum(axis=1) % 2 == 1)
    return [bool(value) for value in result]


def _write_json_atomic(path, value):
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def write_edges(path, rows, metadata):
    """Write edges(a, b, way_id, kind) and metadata(key, value) to a temp SQLite file, then swap it in."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    os.close(handle)
    try:
        db = sqlite3.connect(temp)
        try:
            db.execute("CREATE TABLE edges(a INTEGER, b INTEGER, way_id INTEGER, kind TEXT, PRIMARY KEY(a, b, way_id))")
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT OR IGNORE INTO edges VALUES (?, ?, ?, ?)", rows)
            db.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
            db.commit()
        finally:
            db.close()
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _valid_ways(raw):
    """Drop nodes without a location; returns (ways with >= 2 nodes, skipped segment count)."""
    kept, skipped = [], 0
    for way in raw:
        skipped += sum(1 for a, b in zip(way["nodes"], way["nodes"][1:]) if a[1] is None or b[1] is None)
        valid = [(node_id, loc) for node_id, loc in way["nodes"] if loc is not None]
        if len(valid) >= 2:
            kept.append({**way, "nodes": valid})
    return kept, skipped


def _near(loc, points, km):
    return any(_segment_km(loc, point) < km for point in points)


def build(pbf, out_dir, tolerance_m=15.0, land_tolerance_m=100.0, bridge_km=2.0, fragment_km=1.0, border_skip_km=0.2,
          router=None, osrm_bridge_km=2.0, ferry_km=15.0, skip_reason="không dùng OSRM"):
    pbf, out_dir = Path(pbf), Path(out_dir)
    if not pbf.is_file():
        raise FileNotFoundError(f"Missing OSM file {pbf}; run python osrm/manage.py setup first")
    members = country_relation(pbf) or []
    outer_ids = [ref for kind, ref, role in members if kind == "w" and role == "outer"]
    first = BigRoadWays(outer_ids)
    first.apply_file(str(pbf), locations=True, idx="flex_mem")
    if not first.ways:
        raise ValueError("No motorway, trunk or national-ref ways in the OSM file")
    ways, skipped = _valid_ways(first.ways)
    local, _ = _valid_ways(first.local)
    for way in ways:
        way["bridge"] = False
    ends_before = dead_ends(ways)
    bridges, bridged = bridge_gaps(ways, local, ends_before, bridge_km)
    for bridge in bridges:
        bridge["bridge_kind"] = "road"
    ways += bridges

    with pbf.open("rb") as source:
        sha = hashlib.file_digest(source, "sha256").hexdigest()
    ends_mid = dead_ends(ways)
    routes, osrm_errors, osrm_status = [], [], f"skipped: {skip_reason}"
    if router is not None:
        reason = router.unavailable(sha) if hasattr(router, "unavailable") else None
        if reason:
            osrm_status = f"skipped: {reason}"
        else:
            routes, osrm_errors = osrm_routes(ways, ends_mid, router)
            osrm_status = "ok"
    pairs, touching = way_tags(pbf, {node_id for node_id, _ in ends_mid} | {n for route in routes for n in route["nodes"]})
    osrm_ways, osrm_accepted, osrm_unmatched = accept_routes(routes, pairs, ways, osrm_bridge_km, ferry_km)
    bridges += osrm_ways
    ways += osrm_ways

    km_by_tag, points_before, node_location = Counter(), 0, {}
    for way in ways:
        way["km"] = sum(_segment_km(a[1], b[1]) for a, b in zip(way["nodes"], way["nodes"][1:]))
        km_by_tag[way["highway"]] += way["km"]
        points_before += len(way["nodes"])
        node_location.update(way["nodes"])
    node_uses = Counter(node_id for way in ways for node_id in {n for n, _ in way["nodes"]})
    node_ways = defaultdict(list)
    for index, way in enumerate(ways):
        for node_id in {n for n, _ in way["nodes"]}:
            node_ways[node_id].append(index)

    # A B/C way joins the A network at a node unless every segment of it there was used as a bridge.
    crossings = defaultdict(lambda: {"B": set(), "C": set()})
    for li, way in enumerate(local):
        kind = "B" if way["highway"] in B_TAGS else "C"
        label = way["name"] or f"({way['highway']})"
        last, used = len(way["nodes"]) - 1, bridged.get(li, set())
        for k, (node_id, _) in enumerate(way["nodes"]):
            if node_id in node_uses and any(0 <= seg < last and seg not in used for seg in (k - 1, k)):
                crossings[node_id][kind].add(label)

    ring, land_status, land_gap, border = assemble_land(first.border_ways, {ref for kind, ref, _ in members if kind == "w"},
                                                        first.coast, land_tolerance_m)
    boundary, boundary_status = assemble_boundary(first.outer_ways, outer_ids, land_tolerance_m)
    if ring:
        # A way is on an island only when its first, middle and last vertices all lie off the mainland,
        # so a national road running through a border gate stays on the mainland.
        probes = [way["nodes"][i][1] for way in ways for i in (0, len(way["nodes"]) // 2, -1)]
        on_land = inside(ring, probes)
        for index, way in enumerate(ways):
            way["island"] = not any(on_land[3 * index:3 * index + 3])
    else:
        for way in ways:
            way["island"] = False

    components, component_km = _components(ways)
    fragment_components = {c for c, km in enumerate(sorted(component_km.values(), reverse=True)) if c and km < fragment_km}
    for way, component in zip(ways, components):
        way["fragment"] = component in fragment_components
    # Node pairs of the original geometry: A ways except hidden fragments, and every bridge piece by its own way.
    edge_rows = sorted({(min(a, b), max(a, b), way["way_id"], way.get("bridge_kind") or "a")
                        for way in ways if way["bridge"] or not way["fragment"]
                        for (a, _), (b, _) in zip(way["nodes"], way["nodes"][1:]) if a != b})
    labels = defaultdict(set)
    for way in ways:
        for node_id, _ in way["nodes"]:
            if node_id in crossings:
                labels[node_id].add(_label(way))

    road_features, points_after, group_km, ref_only_km = [], 0, Counter(), 0.0
    for way, component in zip(ways, components):
        fixed = [i for i, (node_id, _) in enumerate(way["nodes"]) if node_uses[node_id] > 1 or node_id in crossings]
        coords = [[round(lon, 6), round(lat, 6)] for lon, lat in simplify([loc for _, loc in way["nodes"]], fixed, tolerance_m)]
        points_after += len(coords)
        ref_only = not way["bridge"] and way["highway"] not in ("motorway", "motorway_link", "trunk", "trunk_link")
        group_km[way["group"]] += way["km"]
        ref_only_km += way["km"] if ref_only else 0
        if way.get("bridge_kind") == "ferry":
            # The crossing time OSRM drives, over the drawn length.
            speed = (round(way["km"] / (way["ferry_min"] / 60), 1), "ferry") if way["ferry_min"] else (None, "unknown")
        else:
            speed = free_flow_speed(way["highway"], way.get("maxspeed", ""))
        road_features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}, "properties": {
            "way_id": way["way_id"], "group": way["group"], "highway": way["highway"], "ref": way["ref"], "name": way["name"],
            "oneway": way["oneway_tag"] in ("yes", "1") or way["highway"] in ("motorway", "motorway_link"),
            "ref_only": ref_only, "bridge": way["bridge"], "bridge_kind": way.get("bridge_kind"), "component": component,
            "isolated": component != 0, "island": way["island"], "fragment": way["fragment"], "km": round(way["km"], 3),
            "speed_kmh": speed[0], "speed_source": speed[1],
            **{key: way[key] for key in ("ferry_name", "car_confirmed", "ferry_min", "ferry_min_source") if key in way}}})

    border_points = border_crossings(ways, border, border_controls(pbf)) if border else []
    border_locs = [tuple(f["geometry"]["coordinates"]) for f in border_points]
    # A road ending right at a border gate carries on abroad, so it is no dead end.
    ends_before = [end for end in ends_before if not _near(node_location[end[0]], border_locs, border_skip_km)]
    ends_after = [end for end in dead_ends(ways) if not _near(node_location[end[0]], border_locs, border_skip_km)]
    point_ids = sorted(crossings) + [node_id for node_id, _ in ends_after]
    on_land = dict(zip(point_ids, inside(ring, [node_location[n] for n in point_ids]))) if ring else {}

    def point(node_id, kind, joins, big_road, fragment):
        lon, lat = node_location[node_id]
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                "properties": {"node_id": node_id, "type": kind, "joins": joins, "big_road": big_road,
                               "island": not on_land.get(node_id, not ring), "fragment": fragment}}

    junction_features, junction_counts = [], Counter()
    for node_id in sorted(crossings):
        crossing = crossings[node_id]
        kind = "B" if crossing["B"] else "C"
        junction_counts[kind] += 1
        junction_features.append(point(node_id, kind, sorted(crossing["B"] | crossing["C"]), " / ".join(sorted(labels[node_id])),
                                       all(ways[index]["fragment"] for index in node_ways[node_id])))
    for point_feature in border_points:
        point_feature["properties"]["fragment"] = False
    junction_features += border_points
    junction_counts["border"] = len(border_points)
    for node_id, index in ends_after:
        way = ways[index]
        feature = point(node_id, "deadend", [], _label(way), way["fragment"])
        # Expressway and national mainlines should never stop, so their dead ends point at OSM data errors.
        feature["properties"]["mainline"] = way["group"] in ("caotoc", "quoclo") and not way["highway"].endswith("_link")
        feature["properties"]["reason"] = dead_end_reason(touching.get(node_id, []))
        junction_features.append(feature)
    junction_counts["deadend"] = len(ends_after)

    ferry_ways = {way["way_id"]: way["car_confirmed"] for way in osrm_ways if way["bridge_kind"] == "ferry"}
    reasons = Counter(f["properties"]["reason"] for f in junction_features if f["properties"]["type"] == "deadend")
    main_km = component_km.most_common(1)[0][1] if component_km else 0.0
    meta = {"pbf_sha256": sha, "policy_version": BIG_ROAD_POLICY_VERSION, "tolerance_m": tolerance_m,
            "ways": len(ways), "points_before": points_before, "points_after": points_after,
            "km": {group: round(group_km[group], 3) for group in GROUP_RANK},
            "km_by_tag": {tag: round(value, 3) for tag, value in km_by_tag.most_common()},
            "ref_only_km": round(ref_only_km, 3), "junctions": {kind: junction_counts[kind] for kind in ("B", "C", "border", "deadend")},
            "components": len(component_km), "main_component_km": round(main_km, 3),
            "isolated_km": round(sum(component_km.values()) - main_km, 3), "skipped_segments": skipped,
            "bridges": len(bridges), "bridge_km": round(sum(way["km"] for way in bridges), 3),
            "dead_ends_before": len(ends_before), "dead_ends_after": len(ends_after),
            "osrm_bridging": osrm_status, "osrm_bridges": osrm_accepted, "osrm_errors": len(osrm_errors),
            "osrm_error_samples": osrm_errors[:20],
            "osrm_unmatched": osrm_unmatched,
            "bridge_km_by_kind": {kind: round(sum(b["km"] for b in bridges if b["bridge_kind"] == kind), 3) for kind in ("road", "small", "ferry")},
            "ferries": len(ferry_ways), "ferries_unconfirmed": sum(1 for confirmed in ferry_ways.values() if not confirmed),
            "dead_end_reasons": {reason: reasons[reason] for reason in ("construction", "continues", "end")},
            "fragments": len(fragment_components), "edge_pairs": len(edge_rows),
            "fragment_km": round(sum(way["km"] for way in ways if way["fragment"]), 4),
            "land_status": land_status, "land_gap_km": land_gap, "land_points": len(ring) if ring else 0,
            "boundary_status": boundary_status, "boundary_points": sum(map(len, boundary or [])), "boundary_rings": len(boundary or []),
            "island_ways": sum(1 for way in ways if way["island"]),
            "island_km": round(sum(way["km"] for way in ways if way["island"]), 3),
            "island_junctions": sum(1 for f in junction_features if f["properties"]["type"] in ("B", "C") and f["properties"]["island"]),
            "built_at": datetime.now(timezone.utc).isoformat()}
    out_dir.mkdir(parents=True, exist_ok=True)
    land_path = out_dir / "vietnam_land.geojson"
    if ring:
        coords = [[round(lon, 6), round(lat, 6)] for lon, lat in ring]
        _write_json_atomic(land_path, {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {"source": "osm_admin_level_2_land_border+coastline", "tolerance_m": land_tolerance_m, "points": len(coords)}})
    else:
        land_path.unlink(missing_ok=True)
    boundary_path = out_dir / "vietnam_boundary.geojson"
    if boundary:
        polygons = [[[[round(lon, 6), round(lat, 6)] for lon, lat in ring]] for ring in boundary]
        _write_json_atomic(boundary_path, {"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": polygons},
            "properties": {"source": "osm_admin_level_2", "tolerance_m": land_tolerance_m, "points": sum(map(len, boundary))}})
    else:
        boundary_path.unlink(missing_ok=True)
    write_edges(out_dir / "big_road_edges.sqlite", edge_rows, {"pbf_sha256": sha, "policy_version": str(BIG_ROAD_POLICY_VERSION)})
    _write_json_atomic(out_dir / "big_roads.geojson", {"type": "FeatureCollection", "features": road_features})
    _write_json_atomic(out_dir / "big_road_junctions.geojson", {"type": "FeatureCollection", "features": junction_features})
    _write_json_atomic(out_dir / "big_roads.meta.json", meta)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=ROOT / "osrm/data/vietnam-latest.osm.pbf")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "osrm/data")
    parser.add_argument("--tolerance-m", type=float, default=15.0)
    parser.add_argument("--land-tolerance-m", type=float, default=100.0)
    parser.add_argument("--bridge-km", type=float, default=2.0, help="longest gap bridged along a B/C road")
    parser.add_argument("--fragment-km", type=float, default=1.0, help="isolated pieces shorter than this are hidden")
    parser.add_argument("--osrm-bridge-km", type=float, default=2.0, help="longest road part of an OSRM bridge")
    parser.add_argument("--ferry-km", type=float, default=15.0, help="longest ferry an OSRM bridge may use")
    parser.add_argument("--no-osrm", action="store_true", help="skip bridging dead ends through local OSRM")
    args = parser.parse_args()
    if min(args.tolerance_m, args.land_tolerance_m, args.bridge_km, args.fragment_km, args.osrm_bridge_km, args.ferry_km) < 0:
        parser.error("Tolerances and distances must be non-negative")
    meta = build(args.pbf, args.out_dir, args.tolerance_m, args.land_tolerance_m, args.bridge_km, args.fragment_km,
                 router=None if args.no_osrm else OsrmRouter(), osrm_bridge_km=args.osrm_bridge_km, ferry_km=args.ferry_km,
                 skip_reason="tắt bằng --no-osrm")
    print(json.dumps({key: value for key, value in meta.items() if key != "km_by_tag"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
