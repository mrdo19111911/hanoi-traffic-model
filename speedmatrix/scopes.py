"""Scope resolution: turn a rule's scope into the segment indices it applies to.

Scopes come from the seed files (``reports/truck_rules/RULE_FORMAT.md``): explicit
segments, street names, route pieces between two crossings, administrative units,
a polygon, or a zone enclosed by boundary roads. Missing data is reported as an
orphan (names, pieces, units, anchors) or raised (zones), never silently replaced.

Everything works on index arrays: the name/ref indices and the midpoint spatial
index are built once per table and cached on it, so the nationwide table (~25M
segments) is scanned only once.
"""
from dataclasses import dataclass, field
import json
import math
import re
import unicodedata

import numpy as np
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

try:
    from .geo import haversine_m, points_in_geometry
    from .rules import RuleError
    from .segments import ROAD_CLASSES, find_segments
except ImportError:  # pragma: no cover - flat import
    from geo import haversine_m, points_in_geometry
    from rules import RuleError
    from segments import ROAD_CLASSES, find_segments

M_PER_DEG = 111320.0
PIECE_SLACK = 1.1           # a piece keeps nodes with d_from + d_to <= PIECE_SLACK * D + PIECE_EXTRA_M
PIECE_EXTRA_M = 200.0
CROSSING_SEED_M = 60.0      # road nodes this close to a crossing node also seed the piece (other carriageway)
CARRIAGEWAY_M = 60.0        # a point this close to the found piece, foot between its ends, is beside the piece
CARRIAGEWAY_DETOUR_M = 300.0  # a beside point left out and farther than this along the road: a lost carriageway
CARRIAGEWAY_STEP_M = 10.0   # sampling step along segments for the corridor
SPAN_EPS_M = 0.5            # a foot this close to an end of the span counts as past it
GAP_FILL_M = 100.0          # longest joining way (unnamed, a bridge or a tunnel) that fills a gap in the road
GAP_MAX_M = 3000.0          # longest gap along the piece between two parts of the road that is looked at
BRIDGE_NAMES = ("cầu ", "hầm ")  # name prefixes of a bridge (or tunnel), for a table without the OSM flag
CUT_REACH_M = 300.0         # same-named road this close past the far end of a one-sided piece: the road goes on
CUT_ROUNDS = 8              # most cuts crossed past the far end of one piece
EXEMPT_PAD_DEG = 0.02       # exempt pieces of a scope without bbox are searched around the scope's extent
MAX_ZONE_CELLS = 60_000_000
_NAME_PREFIXES = ("đường ", "phố ")
_EMPTY = np.zeros(0, dtype=np.int64)


class ZoneError(ValueError):
    """A zone cannot be built safely (missing boundary, leak, wrong area, split ring)."""


@dataclass
class ScopeResult:
    indices: np.ndarray            # sorted, unique int64 segment indices
    orphans: list = field(default_factory=list)


@dataclass
class ZoneResult:
    indices: np.ndarray
    area_km2: float
    missing_names: list = field(default_factory=list)   # always empty: missing names raise ZoneError


# ---- normalisation ---------------------------------------------------------

def normalize_name(s: str) -> str:
    """NFC, casefold, collapse whitespace, strip one leading "đường " or "phố "."""
    text = unicodedata.normalize("NFC", unicodedata.normalize("NFC", s or "").casefold())
    text = " ".join(text.split())
    for prefix in _NAME_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def normalize_ref(s: str) -> str:
    """Road number in one form: "QL.13", "ql 13" -> "QL13"; "ĐT 743" -> "DT743"; suffixes kept ("QL1A")."""
    text = unicodedata.normalize("NFC", s or "").replace("Đ", "D").replace("đ", "d")
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    return re.sub(r"[^0-9A-Z]", "", text.upper())


def _ref_parts(s: str) -> list:
    """Normalized parts of an OSM ref, which may hold several numbers ("QL1A;AH1")."""
    return [r for r in (normalize_ref(p) for p in (s or "").split(";")) if r]


_ADMIN_TYPES = ("thành phố ", "thị trấn ", "đặc khu ", "tỉnh ", "phường ", "xã ")


def _norm_admin(s: str) -> str:
    """normalize_name, then strip one leading admin-type word ("Tỉnh Hà Tĩnh" and "Hà Tĩnh" match)."""
    text = normalize_name(s)
    for prefix in _ADMIN_TYPES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


# ---- per-table index -------------------------------------------------------

def _concat_ranges(starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Concatenation of arange(s, s + c) for every (s, c), vectorized."""
    total = int(counts.sum())
    if total == 0:
        return _EMPTY
    offsets = np.repeat(starts - (np.cumsum(counts) - counts), counts)
    return np.arange(total, dtype=np.int64) + offsets


class _TableIndex:
    """Name/ref → ways, way → segments, and a midpoint index sorted by latitude."""

    def __init__(self, table):
        self.table = table
        way = np.asarray(table.way)
        nw = len(table.way_id)
        if len(way) == 0 or np.all(way[1:] >= way[:-1]):
            self.seg_order = np.arange(len(way), dtype=np.int64)
            sorted_way = way
        else:
            self.seg_order = np.argsort(way, kind="stable").astype(np.int64)
            sorted_way = way[self.seg_order]
        self.way_ptr = np.searchsorted(sorted_way, np.arange(nw + 1)).astype(np.int64)
        self.by_name = self._group(np.asarray(table.name), lambda s: [normalize_name(s)])
        self.by_ref = self._group(np.asarray(table.ref), _ref_parts)
        self._ref_drop = {}
        self._spatial = None

    def _group(self, codes, keys_of):
        valid = np.nonzero(codes >= 0)[0]
        uniq, inverse = np.unique(codes[valid], return_inverse=True)
        lookup = {}
        for pos, sid in enumerate(uniq.tolist()):
            for key in keys_of(self.table.strings[sid]):
                if key:
                    lookup.setdefault(key, []).append(pos)
        order = np.argsort(inverse, kind="stable")
        ptr = np.searchsorted(inverse[order], np.arange(len(uniq) + 1))
        return lookup, ptr, valid[order].astype(np.int64)

    @staticmethod
    def _ways(group, key):
        lookup, ptr, ways = group
        positions = lookup.get(key)
        if not positions:
            return _EMPTY
        return np.concatenate([ways[ptr[p]:ptr[p + 1]] for p in positions])

    def ways_by_text(self, text: str) -> np.ndarray:
        """Ways whose normalized name, or one normalized ref part, equals the text's."""
        return np.unique(np.concatenate([self._ways(self.by_name, normalize_name(text)),
                                         self._ways(self.by_ref, normalize_ref(text))]))

    def ways_by_ref(self, ref: str) -> np.ndarray:
        return np.unique(self._ways(self.by_ref, normalize_ref(ref)))

    def segments_of_ways(self, ways) -> np.ndarray:
        ways = np.unique(np.asarray(ways, dtype=np.int64))
        if len(ways) == 0:
            return _EMPTY
        starts = self.way_ptr[ways]
        rows = _concat_ranges(starts, self.way_ptr[ways + 1] - starts)
        return np.sort(self.seg_order[rows])

    def spatial(self):
        if self._spatial is None:
            t = self.table
            mid_lat = ((t.a_lat.astype(np.float64) + t.b_lat) / 2).astype(np.float32)
            mid_lon = ((t.a_lon.astype(np.float64) + t.b_lon) / 2).astype(np.float32)
            order = np.argsort(mid_lat, kind="stable")
            order = order.astype(np.int32 if len(order) < 2 ** 31 else np.int64)
            self._spatial = (mid_lat, mid_lon, order, mid_lat[order])
        return self._spatial

    def query_bbox(self, bbox) -> np.ndarray:
        """Sorted segment indices whose midpoint lies in [west, south, east, north]."""
        mid_lat, mid_lon, order, sorted_lat = self.spatial()
        west, south, east, north = (float(v) for v in bbox)
        # search with float32 keys (a float64 key would copy the whole float32 array): the smallest float32 not
        # below south and the largest not above north select exactly the float32 latitudes in [south, north]
        lo_key, hi_key = np.float32(south), np.float32(north)
        lo_key = np.nextafter(lo_key, np.float32(np.inf)) if float(lo_key) < south else lo_key
        hi_key = np.nextafter(hi_key, np.float32(-np.inf)) if float(hi_key) > north else hi_key
        lo, hi = np.searchsorted(sorted_lat, lo_key, "left"), np.searchsorted(sorted_lat, hi_key, "right")
        cand = order[lo:hi]
        lon = mid_lon[cand]
        return np.sort(cand[(lon >= west) & (lon <= east)]).astype(np.int64)

    def ref_drop_mask(self, ref_codes: np.ndarray, prefixes: tuple) -> np.ndarray:
        """Per ref string code: does one of its parts start with one of the prefixes."""
        codes, inverse = np.unique(ref_codes, return_inverse=True)
        drop = np.zeros(len(codes), dtype=bool)
        for i, code in enumerate(codes.tolist()):
            if code < 0:
                continue
            key = (code, prefixes)
            if key not in self._ref_drop:
                self._ref_drop[key] = any(part.startswith(prefixes) for part in _ref_parts(self.table.strings[code]))
            drop[i] = self._ref_drop[key]
        return drop[inverse]


def _index(table) -> _TableIndex:
    """The scope index of a table, built on first use and kept on the table instance."""
    index = getattr(table, "_scope_index", None)
    if index is None:
        index = _TableIndex(table)
        table._scope_index = index
    return index


def _water_codes(water, text: str) -> np.ndarray:
    lookup = getattr(water, "_scope_names", None)
    if lookup is None:
        lookup = {}
        for code, name in enumerate(water.strings):
            lookup.setdefault(normalize_name(name), []).append(code)
        water._scope_names = lookup
    return np.array(lookup.get(normalize_name(text), []), dtype=np.int64)


def _mid_in(table, idx: np.ndarray, bbox) -> np.ndarray:
    if bbox is None or len(idx) == 0:
        return idx
    west, south, east, north = (float(v) for v in bbox)
    lat = (table.a_lat[idx].astype(np.float64) + table.b_lat[idx]) / 2
    lon = (table.a_lon[idx].astype(np.float64) + table.b_lon[idx]) / 2
    return idx[(lat >= south) & (lat <= north) & (lon >= west) & (lon <= east)]


def _envelope_mask(a_lat, a_lon, b_lat, b_lon, bbox) -> np.ndarray:
    west, south, east, north = (float(v) for v in bbox)
    return ((np.minimum(a_lon, b_lon) <= east) & (np.maximum(a_lon, b_lon) >= west)
            & (np.minimum(a_lat, b_lat) <= north) & (np.maximum(a_lat, b_lat) >= south))


def _envelope_in(table, idx: np.ndarray, bbox) -> np.ndarray:
    if bbox is None or len(idx) == 0:
        return idx
    return idx[_envelope_mask(table.a_lat[idx], table.a_lon[idx], table.b_lat[idx], table.b_lon[idx], bbox)]


# ---- route pieces ----------------------------------------------------------

def _graph(n: int, ea: np.ndarray, eb: np.ndarray, length: np.ndarray):
    """Undirected graph of the piece; parallel edges keep their shortest length."""
    lo, hi = np.minimum(ea, eb), np.maximum(ea, eb)
    w = np.maximum(length.astype(np.float64), 0.01)    # explicit zeros would vanish from the sparse graph
    keep = lo != hi
    lo, hi, w = lo[keep], hi[keep], w[keep]
    order = np.lexsort((w, hi, lo))
    lo, hi, w = lo[order], hi[order], w[order]
    first = np.r_[True, (lo[1:] != lo[:-1]) | (hi[1:] != hi[:-1])] if len(lo) else np.zeros(0, dtype=bool)
    return coo_matrix((w[first], (lo[first], hi[first])), shape=(n, n)).tocsr()


def _far_ends(graph, labels: np.ndarray, sources: np.ndarray) -> np.ndarray:
    """Per connected component that holds a source, the node farthest from the sources."""
    dist = dijkstra(graph, directed=False, indices=sources, min_only=True)
    reached = np.nonzero(np.isfinite(dist))[0]
    lab = labels[reached]
    order = np.lexsort((dist[reached], lab))
    last = np.r_[lab[order][1:] != lab[order][:-1], True]
    return reached[order][last]


def route_piece(table, piece: dict, bbox) -> tuple:
    """Segments of one route piece ``{name | ref, from?, to?}`` inside ``bbox``, plus an orphan dict or None.

    The piece is the named road between two crossings (both carriageways kept): with
    d_F, d_T the distances on the road from the ``from`` and ``to`` crossing nodes and
    D = min d_F over the ``to`` nodes, a segment is kept when both its ends satisfy
    d_F + d_T <= 1.1 D + 200 m. With one crossing only, the other end is the far end
    of the road from that crossing (with the nodes of its own carriageway within 60 m, as at a crossing).

    That finds a carriageway only when it is joined to a crossing. One with no node within 60 m of a
    crossing node, or cut off from it by another way, gets no source; with one crossing, carriageways
    joined only at it share a single far end. So the piece then takes in its corridor (``_Corridor``):
    every other segment of the same road that runs beside the found piece, between its two ends, for at
    least half its length. A road loss is then either filled or reported, never dropped silently:

    - a part of the same road that comes beside the piece but could not be taken in, and is not joined to
      the piece along the road within CARRIAGEWAY_DETOUR_M: orphan ``{"missing": "carriageway"}``;
    - a gap between two parts of the road along the piece is filled by a joining way of at most GAP_FILL_M
      (``_fill_gaps``), else reported: orphan ``{"missing": "gap", "at": [[lat, lon], ...]}``;
    - with one crossing, where the same-named road goes on within CUT_REACH_M past the far end, cut off
      from the piece (a junction way, a roundabout), the cut is crossed the same way and the far end
      looked for again past it (``_cross_cuts``), else reported as a gap at the far end;
    - with two crossings on parts of the road cut apart, the cut is crossed the same way
      (``_join_crossings``), else the piece is the orphan ``{"missing": "path"}``. The piece then runs
      between the crossings as usual, so it stops at the requested crossing, not at the road's end.

    A crossing is matched by name or by ref (``ways_by_text``: "QL5" finds the ways with ref "QL.5"); one that
    shares no node with the road is the orphan ``{"missing": "from" | "to"}``.

    The segments found are returned with the orphan, so a rule applies what it has.
    """
    index = _index(table)
    parts = []
    if piece.get("name"):
        parts.append(index.ways_by_text(piece["name"]))
    if piece.get("ref"):
        parts.append(index.ways_by_ref(piece["ref"]))
    ways = np.unique(np.concatenate(parts)) if parts else _EMPTY
    road = _mid_in(table, index.segments_of_ways(ways), bbox)
    if len(road) == 0:
        return _EMPTY, {"route": dict(piece), "missing": "name"}
    if not piece.get("from") and not piece.get("to"):
        return road, None
    joins = _EMPTY
    for rounds_left in range(CUT_ROUNDS, 0, -1):
        found = _piece(table, index, piece, bbox, ways, np.union1d(road, joins) if len(joins) else road,
                       cross=rounds_left > 1)
        if isinstance(found, str):
            return _EMPTY, {"route": dict(piece), "missing": found}
        idx, lost, at, crossed = found
        if len(crossed) == 0:
            break
        joins = np.union1d(joins, crossed)
    if lost:
        orphan = {"route": dict(piece), "missing": "carriageway"}
        if at:
            orphan["at"] = at
        return idx, orphan
    return idx, ({"route": dict(piece), "missing": "gap", "at": at} if at else None)


def _piece(table, index, piece, bbox, ways, road, cross):
    """One pass of ``route_piece`` over the segments ``road``: (indices, lost, gaps, crossed) where ``lost``
    says a carriageway was left out, ``gaps`` lists [lat, lon] of the gaps not filled and ``crossed`` holds
    the segments that cross a cut past the far end (the caller adds them to the road and looks again);
    or the missing part as a string ("from", "to", "path")."""
    start, end = piece.get("from"), piece.get("to")
    nodes, inverse = np.unique(np.concatenate([table.a[road], table.b[road]]), return_inverse=True)
    ea, eb = inverse[:len(road)], inverse[len(road):]

    lat, lon = np.empty(len(nodes)), np.empty(len(nodes))
    lat[ea], lon[ea] = table.a_lat[road], table.a_lon[road]
    lat[eb], lon[eb] = table.b_lat[road], table.b_lon[road]

    def crossing(text):
        segs = _envelope_in(table, index.segments_of_ways(np.setdiff1d(index.ways_by_text(text), ways)), bbox)
        return np.nonzero(np.isin(nodes, np.concatenate([table.a[segs], table.b[segs]])))[0]

    near = crossing(start) if start else None
    if near is not None and len(near) == 0:
        return "from"
    far = crossing(end) if end else None
    if far is not None and len(far) == 0:
        return "to"
    # every road node near a crossing node seeds the piece too, so a carriageway that the crossing
    # street does not touch (T-junction) gets its own source
    near = None if near is None else _nodes_near(lat, lon, near, CROSSING_SEED_M)
    far = None if far is None else _nodes_near(lat, lon, far, CROSSING_SEED_M)
    length = table.length_m[road]
    graph = _graph(len(nodes), ea, eb, length)
    n_comp, labels = connected_components(graph, directed=False)
    # the far end of the road stands in for a missing crossing, and like one it takes in the nodes within
    # 60 m (both arms of a road that ends in a fork), but only of its own component: another carriageway
    # passing near it keeps its own far end
    inferred = _EMPTY
    if near is None:
        inferred = _far_ends(graph, labels, far)
        near = _nodes_near(lat, lon, inferred, CROSSING_SEED_M, labels)
    if far is None:
        inferred = _far_ends(graph, labels, near)
        far = _nodes_near(lat, lon, inferred, CROSSING_SEED_M, labels)
    d_near = dijkstra(graph, directed=False, indices=near, min_only=True)
    d_far = dijkstra(graph, directed=False, indices=far, min_only=True)
    # D per connected component (a carriageway, a stub): each runs between its own crossing nodes
    span = np.full(n_comp, np.inf)
    np.minimum.at(span, labels[far], d_near[far])
    if not np.isfinite(span).any():
        # the road is cut between the crossings: cross the cut like one past a far end, then look again
        joined = _join_crossings(table, index, bbox, road, nodes, lat, lon, labels, near, far) if cross else _EMPTY
        return (_EMPTY, False, [], joined) if len(joined) else "path"
    total = d_near + d_far
    ok = np.isfinite(total) & (total <= PIECE_SLACK * span[labels] + PIECE_EXTRA_M)
    keep = ok[ea] & ok[eb]
    if not keep.any():
        return road[keep], False, [], _EMPTY

    lost, gaps, fills = False, [], _EMPTY
    if not keep.all():
        kx = M_PER_DEG * math.cos(math.radians(float(np.mean(lat))))
        lat0, lon0 = float(lat[0]), float(lon[0])

        def project(la, lo):
            return ((np.asarray(lo, dtype=np.float64) - lon0) * kx,
                    (np.asarray(la, dtype=np.float64) - lat0) * M_PER_DEG)

        x, y = project(lat, lon)
        corridor = _Corridor(x, y, ea, eb, length, keep, d_near, d_far, span[labels])
        drop = np.flatnonzero(~keep)
        runs, beside, seg, t = corridor.split(x[ea[drop]], y[ea[drop]], x[eb[drop]], y[eb[drop]])
        keep[drop[runs]] = True
        take, fills, gaps = _fill_gaps(table, index, bbox, road, nodes, lat, lon, ea, eb, length, keep, graph,
                                       corridor)
        keep |= take

        # safety net: a beside point of the road that was left out and is not joined to the piece along the road
        loose = beside & ~runs[seg]
        if loose.any():
            along = dijkstra(graph, directed=False, indices=np.unique(np.concatenate([ea[keep], eb[keep]])),
                             min_only=True)
            seg_len = np.maximum(length[drop].astype(np.float64), 0.01)
            sample_along = np.minimum(along[ea[drop]][seg] + t * seg_len[seg],
                                      along[eb[drop]][seg] + (1 - t) * seg_len[seg])
            lost = bool((loose & (sample_along > CARRIAGEWAY_DETOUR_M)).any())
    crossed = _EMPTY
    if len(inferred):
        crossed, cuts = _cross_cuts(table, index, bbox, road, nodes, lat, lon, n_comp, labels, ea[keep], eb[keep],
                                    inferred, cross)
        gaps = gaps + cuts
    idx = np.union1d(road[keep], fills) if len(fills) else road[keep]
    return idx, lost, gaps, crossed


def _samples(x, y, ea, eb, step):
    """Points every ``step`` metres along the segments (ends included): (px, py, segment, t in [0, 1])."""
    n = np.maximum(1, np.ceil(np.hypot(x[eb] - x[ea], y[eb] - y[ea]) / step)).astype(np.int64)
    seg = np.repeat(np.arange(len(n)), n + 1)
    k = np.arange(int((n + 1).sum())) - np.repeat(np.cumsum(n + 1) - (n + 1), n + 1)
    t = k / n[seg]
    a, b = ea[seg], eb[seg]
    return x[a] + t * (x[b] - x[a]), y[a] + t * (y[b] - y[a]), seg, t


class _Corridor:
    """The found piece as a corridor, in local metres (x east, y north).

    One definition serves the inclusion and the safety net. A point is *beside* the piece when it lies
    within CARRIAGEWAY_M of a found segment whose foot (the nearest point of that segment) falls strictly
    between the piece's two ends. The foot is past an end when it stops at a dead end of the piece, or
    when along the road it is as far from either crossing as the crossings are from each other
    (d_F >= D or d_T >= D, with D of the foot's own component), so the piece's own overrun past its
    crossings (the 1.1 D + 200 m slack) lies outside the span.
    """

    def __init__(self, x, y, ea, eb, length, keep, d_from, d_to, span_of_node):
        a, b = ea[keep], eb[keep]
        self.dead_end = _dead_ends(a, b, len(x))
        self.x, self.y, self.a, self.b = x, y, a, b
        self.length = np.maximum(length[keep].astype(np.float64), 0.01)
        self.d_from, self.d_to, self.span = d_from, d_to, span_of_node[a]
        sx, sy, self.sample_seg, _ = _samples(x, y, a, b, CARRIAGEWAY_STEP_M)
        self.tree = cKDTree(np.column_stack([sx, sy]))

    def locate(self, px, py):
        """Per point: its distance to the piece through a foot between the ends (inf when there is none
        within CARRIAGEWAY_M), and that foot's distance along the road from the ``from`` end (nan)."""
        dist, pos = np.full(len(px), np.inf), np.full(len(px), np.nan)
        if len(px) == 0:
            return dist, pos
        lists = self.tree.query_ball_point(np.column_stack([px, py]), r=CARRIAGEWAY_M + CARRIAGEWAY_STEP_M)
        count = np.fromiter((len(c) for c in lists), dtype=np.int64, count=len(lists))
        if count.sum() == 0:
            return dist, pos
        point = np.repeat(np.arange(len(px)), count)
        found = self.sample_seg[np.fromiter((s for c in lists for s in c), dtype=np.int64, count=int(count.sum()))]
        pairs = np.unique(np.column_stack([point, found]), axis=0)
        point, seg = pairs[:, 0], pairs[:, 1]
        a, b = self.a[seg], self.b[seg]
        ax, ay = self.x[a], self.y[a]
        dx, dy = self.x[b] - ax, self.y[b] - ay
        t_raw = ((px[point] - ax) * dx + (py[point] - ay) * dy) / np.maximum(dx * dx + dy * dy, 1e-12)
        t = np.clip(t_raw, 0.0, 1.0)
        d = np.hypot(ax + t * dx - px[point], ay + t * dy - py[point])
        stop = np.where(t_raw <= 0, a, np.where(t_raw >= 1, b, -1))
        length = self.length[seg]
        along_from = np.minimum(self.d_from[a] + t * length, self.d_from[b] + (1 - t) * length)
        along_to = np.minimum(self.d_to[a] + t * length, self.d_to[b] + (1 - t) * length)
        limit = self.span[seg] - SPAN_EPS_M
        between = ~((stop >= 0) & self.dead_end[np.maximum(stop, 0)]) & (along_from < limit) & (along_to < limit)
        d = np.where(between & (d <= CARRIAGEWAY_M), d, np.inf)
        order = np.lexsort((d, point))
        best = order[np.r_[True, point[order][1:] != point[order][:-1]]]
        dist[point[best]] = d[best]
        pos[point[best]] = np.where(np.isfinite(d[best]), along_from[best], np.nan)
        return dist, pos

    def split(self, x1, y1, x2, y2):
        """For segments from (x1, y1) to (x2, y2): (runs, beside, seg, t). A segment *runs beside* the piece
        when at least half of its points, sampled every CARRIAGEWAY_STEP_M, are beside it; ``beside`` holds
        the sample points' test, ``seg`` and ``t`` their segment and place on it."""
        n = len(x1)
        runs = np.zeros(n, dtype=bool)
        if n == 0:
            return runs, np.zeros(0, dtype=bool), _EMPTY, np.zeros(0)
        # a segment whose nearer end is farther than this from every sample of the piece cannot come beside it
        end_d, _ = self.tree.query(np.column_stack([np.r_[x1, x2], np.r_[y1, y2]]))
        reach = np.minimum(end_d[:n], end_d[n:]) - np.hypot(x2 - x1, y2 - y1) / 2
        cand = np.flatnonzero(reach <= CARRIAGEWAY_M + CARRIAGEWAY_STEP_M)
        m = len(cand)
        px, py, sub, t = _samples(np.r_[x1[cand], x2[cand]], np.r_[y1[cand], y2[cand]],
                                  np.arange(m), np.arange(m, 2 * m), CARRIAGEWAY_STEP_M)
        beside = np.isfinite(self.locate(px, py)[0])
        seg = cand[sub]
        share = np.bincount(seg, weights=beside, minlength=n) / np.maximum(np.bincount(seg, minlength=n), 1)
        runs[cand] = share[cand] >= 0.5
        return runs, beside, seg, t


def _dead_ends(a: np.ndarray, b: np.ndarray, n: int) -> np.ndarray:
    """Per node: does it have exactly one distinct neighbour over the segments (a, b)?"""
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    links = np.unique(np.column_stack([lo, hi])[lo != hi], axis=0)
    return np.bincount(links.ravel(), minlength=n) == 1


def _bridge_name(text: str) -> bool:
    """Is a way named as a bridge or tunnel ("Cầu X", "Hầm Y")? "Đường Cầu Giấy" is a street."""
    text = " ".join(unicodedata.normalize("NFC", (text or "").casefold()).split())
    return text.startswith(BRIDGE_NAMES)


def _may_join(table, idx: np.ndarray) -> np.ndarray:
    """Per segment: may its way join two cut parts of a road? Unnamed ways may, and bridges or tunnels: by the
    table's per-way OSM flag ``bridge`` (bridge or tunnel) when it has one, else by the name (``_bridge_name``).
    A ferry never does."""
    way = table.way[idx]
    code = table.name[way]
    flag = getattr(table, "bridge", None)
    if flag is not None:
        ok = (code < 0) | (np.asarray(flag)[way] != 0)
    else:
        codes, inverse = np.unique(code, return_inverse=True)
        ok = np.array([c < 0 or _bridge_name(table.strings[c]) for c in codes.tolist()], dtype=bool)[inverse]
    return ok & (table.road_class[way] != ROAD_CLASSES.index("pha"))


def _connector(table, index, bbox, road, sources, targets, la, lo) -> tuple:
    """The shortest path of at most GAP_FILL_M from a table node in ``sources`` to one in ``targets`` over
    segments that are not ``road`` and may join a road (``_may_join``), searched around the points (la, lo):
    (segment indices, length), or (None, inf)."""
    pad_lat = GAP_FILL_M / M_PER_DEG
    pad_lon = pad_lat / max(math.cos(math.radians(float(np.mean(la)))), 0.1)
    cand = index.query_bbox([float(lo.min()) - pad_lon, float(la.min()) - pad_lat,
                             float(lo.max()) + pad_lon, float(la.max()) + pad_lat])
    cand = np.setdiff1d(_mid_in(table, cand, bbox), road)
    cand = cand[_may_join(table, cand)] if len(cand) else cand
    if len(cand) == 0:
        return None, np.inf
    cnodes, inv = np.unique(np.concatenate([table.a[cand], table.b[cand]]), return_inverse=True)
    ca, cb = inv[:len(cand)], inv[len(cand):]
    src = np.flatnonzero(np.isin(cnodes, sources))
    dst = np.flatnonzero(np.isin(cnodes, targets))
    if len(src) == 0 or len(dst) == 0:
        return None, np.inf
    seg_len = table.length_m[cand].astype(np.float64)
    dist, prev, _ = dijkstra(_graph(len(cnodes), ca, cb, seg_len), directed=False, indices=src, min_only=True,
                             return_predecessors=True)
    node = dst[np.argmin(dist[dst])]
    path_len = float(dist[node])
    if not path_len <= GAP_FILL_M or prev[node] < 0:
        return None, np.inf
    path = []
    while prev[node] >= 0:
        back = prev[node]
        on = np.flatnonzero(((ca == back) & (cb == node)) | ((ca == node) & (cb == back)))
        path.append(cand[on[np.argmin(seg_len[on])]])
        node = back
    return np.array(path, dtype=np.int64), path_len


def _fill_gaps(table, index, bbox, road, nodes, lat, lon, ea, eb, length, keep, graph, corridor) -> tuple:
    """Gaps in a carriageway along the piece: (road segments that fill them as a mask over ``road``, other
    segments that fill them, [[lat, lon], ...] of the gaps left open).

    A gap runs from a dead end of the piece where traffic arrives to one ahead of it where traffic departs in
    the same direction (a two-way end does both), at most GAP_MAX_M apart, when the piece does not join them
    within their distance plus CARRIAGEWAY_DETOUR_M. Ends of opposite carriageways, or a lane that ends, form
    no gap. When one end is beside the piece, the road's own left-out segments (``graph``) fill the gap if
    they join the ends within their distance plus CARRIAGEWAY_DETOUR_M (a carriageway that runs on more than
    60 m from the found piece, or past a junction where the piece is cut). When both are, a joining way of at
    most GAP_FILL_M (``_connector``: unnamed, or a bridge or tunnel, such as a roundabout's arc) fills it; a
    street or a longer way does not, and the gap is reported at its middle. A pair with an end outside the
    corridor lies past the span and is not reported.
    """
    take = np.zeros(len(keep), dtype=bool)
    ka, kb = ea[keep], eb[keep]
    ends = np.flatnonzero(_dead_ends(ka, kb, len(nodes)))
    if len(ends) < 2:
        return take, _EMPTY, []
    beside = np.isfinite(corridor.locate(corridor.x[ends], corridor.y[ends])[0])
    # the kept segment at each end, and the way traffic runs there (a unit vector) when it arrives or departs
    kept = np.flatnonzero(keep)
    at_node, at_seg = np.concatenate([ea[kept], eb[kept]]), np.concatenate([kept, kept])
    order = np.argsort(at_node, kind="stable")
    seg = at_seg[order][np.searchsorted(at_node[order], ends)]
    x, y = corridor.x[ends], corridor.y[ends]
    other = np.where(eb[seg] == ends, ea[seg], eb[seg])
    out = np.column_stack([x - corridor.x[other], y - corridor.y[other]])       # from the segment into the end
    out /= np.maximum(np.hypot(out[:, 0], out[:, 1]), 1e-9)[:, None]
    oneway = table.oneway[table.way[road[seg]]].astype(np.int64)
    forward = np.where(eb[seg] == ends, 1, -1) * oneway          # +1: traffic runs into the end, -1: out of it
    arrive, depart = forward >= 0, forward <= 0
    straight = np.hypot(x[None, :] - x[:, None], y[None, :] - y[:, None])
    ahead = ((x[None, :] - x[:, None]) * out[:, 0:1] + (y[None, :] - y[:, None]) * out[:, 1:2]) > 0
    same_way = (out[:, None, 0] * -out[None, :, 0] + out[:, None, 1] * -out[None, :, 1]) > 0
    cand = (arrive[:, None] & depart[None, :] & ahead & same_way & (straight <= GAP_MAX_M)
            & (beside[:, None] | beside[None, :]))
    if not cand.any():
        return take, _EMPTY, []
    on_piece = dijkstra(_graph(len(nodes), ka, kb, length[keep]), directed=False, indices=ends)[:, ends]
    pairs = np.argwhere(cand & (on_piece > straight + CARRIAGEWAY_DETOUR_M))
    if len(pairs) == 0:
        return take, _EMPTY, []
    on_road, back = dijkstra(graph, directed=False, indices=ends, return_predecessors=True)

    fills, used, at = [], set(), []
    for i, j in sorted(pairs.tolist(), key=lambda p: straight[p[0], p[1]]):
        if i in used or j in used:
            continue
        used.update((i, j))
        if on_road[i, ends[j]] <= straight[i, j] + CARRIAGEWAY_DETOUR_M:
            node = ends[j]
            while node != ends[i]:
                prev = back[i, node]
                on = np.flatnonzero(((ea == prev) & (eb == node)) | ((ea == node) & (eb == prev)))
                take[on[np.argmin(length[on])]] = True
                node = prev
            continue
        if not (beside[i] and beside[j]):
            continue
        if straight[i, j] <= GAP_FILL_M:
            pair = np.array([ends[i], ends[j]])
            path, path_len = _connector(table, index, bbox, road, nodes[pair[:1]], nodes[pair[1:]], lat[pair],
                                        lon[pair])
            if path is not None and on_piece[i, j] > path_len + CARRIAGEWAY_DETOUR_M:
                fills.append(path)
                continue
        at.append([round(float(lat[ends[i]] + lat[ends[j]]) / 2, 6), round(float(lon[ends[i]] + lon[ends[j]]) / 2, 6)])
    return take, (np.unique(np.concatenate(fills)) if fills else _EMPTY), at


def _cross_cuts(table, index, bbox, road, nodes, lat, lon, n_comp, labels, ka, kb, ends, cross) -> tuple:
    """Cuts past the inferred far ends ``ends`` of a one-sided piece whose found segments run over (ka, kb):
    (segments that cross them, [[lat, lon], ...] of the ends at a cut not crossed).

    The far end is the end of the road only when the road does not go on. Where a component of the same road
    that the piece does not reach has a node within CUT_REACH_M of a far end, the road was cut there (a
    junction way, a roundabout): with ``cross``, a joining way of at most GAP_FILL_M (``_connector``) from the
    end's own nodes within 60 m to that component crosses it, else the far end is reported.
    """
    reached = np.zeros(n_comp, dtype=bool)
    reached[labels[np.concatenate([ka, kb])]] = True
    loose = np.flatnonzero(~reached[labels])
    if len(loose) == 0:
        return _EMPTY, []
    crossed, at = [], []
    for e in ends.tolist():
        past = loose[haversine_m(np.full(len(loose), lat[e]), np.full(len(loose), lon[e]), lat[loose], lon[loose])
                     <= CUT_REACH_M]
        if len(past) == 0:
            continue
        path = None
        if cross:
            src = _nodes_near(lat, lon, np.array([e]), CROSSING_SEED_M, labels)
            ids = np.concatenate([src, past])
            path, _ = _connector(table, index, bbox, road, nodes[src], nodes[past], lat[ids], lon[ids])
        if path is None:
            at.append([round(float(lat[e]), 6), round(float(lon[e]), 6)])
        else:
            crossed.append(path)
    return (np.unique(np.concatenate(crossed)) if crossed else _EMPTY), at


def _join_crossings(table, index, bbox, road, nodes, lat, lon, labels, near, far) -> np.ndarray:
    """Segments that cross a cut between the road's parts at the two crossings (``near``, ``far``), or none.

    Where the parts holding the ``near`` nodes come within CUT_REACH_M of the parts holding the ``far`` nodes,
    a joining way of at most GAP_FILL_M (``_connector``) between their nodes within CUT_REACH_M of that
    closest approach crosses the cut."""
    a_comp = np.unique(labels[near])
    b_comp = np.setdiff1d(np.unique(labels[far]), a_comp)
    side_a = np.flatnonzero(np.isin(labels, a_comp))
    side_b = np.flatnonzero(np.isin(labels, b_comp))
    if len(side_a) == 0 or len(side_b) == 0:
        return _EMPTY
    kx = M_PER_DEG * math.cos(math.radians(float(np.mean(lat))))
    xy = np.column_stack([(lon - lon[0]) * kx, (lat - lat[0]) * M_PER_DEG])
    dist, j = cKDTree(xy[side_b]).query(xy[side_a], distance_upper_bound=CUT_REACH_M)
    if not np.isfinite(dist).any():
        return _EMPTY
    k = int(np.argmin(dist))
    u, v = side_a[k], side_b[j[k]]
    src = side_a[np.hypot(*(xy[side_a] - xy[v]).T) <= CUT_REACH_M]
    dst = side_b[np.hypot(*(xy[side_b] - xy[u]).T) <= CUT_REACH_M]
    ids = np.concatenate([src, dst])
    path, _ = _connector(table, index, bbox, road, nodes[src], nodes[dst], lat[ids], lon[ids])
    return _EMPTY if path is None else path


def _nodes_near(lat: np.ndarray, lon: np.ndarray, found: np.ndarray, radius_m: float, labels=None) -> np.ndarray:
    """Nodes within ``radius_m`` (haversine) of any ``found`` node, the found nodes included; with ``labels``
    (connected components), only nodes of the same component as the found node they are near."""
    kx = M_PER_DEG * math.cos(math.radians(float(np.mean(lat[found]))))
    xy = np.column_stack([(lon - lon[found[0]]) * kx, (lat - lat[found[0]]) * M_PER_DEG])
    lists = cKDTree(xy).query_ball_point(xy[found], r=radius_m * 1.01 + 1.0)
    pair_f = np.repeat(found, [len(c) for c in lists])
    pair_c = np.fromiter((c for cs in lists for c in cs), dtype=np.int64, count=len(pair_f))
    close = haversine_m(lat[pair_f], lon[pair_f], lat[pair_c], lon[pair_c]) <= radius_m
    if labels is not None:
        close &= labels[pair_f] == labels[pair_c]
    return np.union1d(found, pair_c[close])


# ---- zones -----------------------------------------------------------------

def _raster_cells(a_lat, a_lon, b_lat, b_lon, project, cell_m, nx, ny) -> np.ndarray:
    """Flat grid cells touched by line segments, sampled every cell_m / 2."""
    if len(a_lat) == 0:
        return _EMPTY
    x1, y1 = project(a_lat, a_lon)
    x2, y2 = project(b_lat, b_lon)
    n = np.maximum(1, np.ceil(np.hypot(x2 - x1, y2 - y1) / (cell_m / 2))).astype(np.int64)
    counts = n + 1
    seg = np.repeat(np.arange(len(n)), counts)
    k = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    t = k / n[seg]
    cx = np.floor((x1[seg] + t * (x2 - x1)[seg]) / cell_m).astype(np.int64)
    cy = np.floor((y1[seg] + t * (y2 - y1)[seg]) / cell_m).astype(np.int64)
    ok = (cx >= 0) & (cx < nx) & (cy >= 0) & (cy < ny)
    return np.unique(cy[ok] * nx + cx[ok])


def _entry_label(entry) -> str:
    return entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False, sort_keys=True)


def zone_segments(table, boundary_names, seed, bbox, area_km2, cell_m=20.0, seal_m=60.0,
                  water=None) -> ZoneResult:
    """Segments inside the region that the boundary roads (and water lines) enclose around ``seed``.

    The boundary is rasterized on a ``cell_m`` grid over ``bbox`` and dilated by
    ``seal_m`` so roads crossing it without a shared node (underpasses) do not leak.
    Raises ZoneError when a boundary entry matches nothing, the seed is on the
    boundary, the region reaches the bbox edge, the area is outside ``area_km2``, or
    a boundary entry does not touch the region.
    """
    index = _index(table)
    west, south, east, north = (float(v) for v in bbox)
    kx = M_PER_DEG * math.cos(math.radians((south + north) / 2))

    def project(lat, lon):
        return ((np.asarray(lon, dtype=np.float64) - west) * kx,
                (np.asarray(lat, dtype=np.float64) - south) * M_PER_DEG)

    nx = max(1, math.ceil((east - west) * kx / cell_m))
    ny = max(1, math.ceil((north - south) * M_PER_DEG / cell_m))
    if nx * ny > MAX_ZONE_CELLS:
        raise ZoneError(f"zone bbox too large for a {cell_m:g} m grid ({nx} x {ny} cells)")

    labels, cells, missing = [], [], []
    for entry in boundary_names:
        label = _entry_label(entry)
        lines = []
        if isinstance(entry, str):
            segs = _envelope_in(table, index.segments_of_ways(index.ways_by_text(entry)), bbox)
            if len(segs):
                lines.append((table.a_lat[segs], table.a_lon[segs], table.b_lat[segs], table.b_lon[segs]))
            if water is not None:
                codes = _water_codes(water, entry)
                if len(codes):
                    sel = np.isin(water.name, codes)
                    sel &= _envelope_mask(water.a_lat, water.a_lon, water.b_lat, water.b_lon, bbox)
                    if sel.any():
                        lines.append((water.a_lat[sel], water.a_lon[sel], water.b_lat[sel], water.b_lon[sel]))
            if not lines:
                missing.append(label)
                continue
        else:
            segs, orphan = route_piece(table, entry, bbox)
            if orphan is not None and orphan["missing"] in ("carriageway", "gap"):
                orphan = None           # the found piece still bounds the zone (the leak and area checks hold)
            if orphan is not None or len(segs) == 0:
                missing.append(label + (f" (missing {orphan['missing']})" if orphan else ""))
                continue
            lines.append((table.a_lat[segs], table.a_lon[segs], table.b_lat[segs], table.b_lon[segs]))
        labels.append(label)
        cells.append(np.unique(np.concatenate([_raster_cells(*ln, project, cell_m, nx, ny) for ln in lines])))
    if missing:
        raise ZoneError("boundary names not found: " + ", ".join(missing))

    seed_x, seed_y = project(seed[0], seed[1])
    sx, sy = int(math.floor(seed_x / cell_m)), int(math.floor(seed_y / cell_m))
    if not (0 <= sx < nx and 0 <= sy < ny):
        raise ZoneError(f"seed {list(seed)} lies outside the bbox")
    iterations = max(1, int(round(seal_m / cell_m)))
    blocked = np.zeros((ny, nx), dtype=bool)
    for c in cells:
        blocked.flat[c] = True
    blocked = ndimage.binary_dilation(blocked, iterations=iterations)
    if blocked[sy, sx]:
        raise ZoneError(f"seed lies on the boundary: {list(seed)}")
    grid_labels, _ = ndimage.label(~blocked)
    region = grid_labels == grid_labels[sy, sx]
    del grid_labels

    sides = [name for name, edge in (("south", region[0, :]), ("north", region[-1, :]),
                                     ("west", region[:, 0]), ("east", region[:, -1])) if edge.any()]
    if sides:
        raise ZoneError(f"zone leaks to the bbox edge: {', '.join(sides)} side; "
                        f"a gap in the boundary {', '.join(labels)}")
    area = float(region.sum()) * cell_m * cell_m / 1e6
    low, high = float(area_km2[0]), float(area_km2[1])
    if not low <= area <= high:
        raise ZoneError(f"zone area {area:.2f} km2 outside [{low:g}, {high:g}]")

    fringe = ndimage.binary_dilation(region) & ~region
    near_fringe = ndimage.binary_dilation(fringe, iterations=iterations)
    apart = [label for label, c in zip(labels, cells) if not near_fringe.flat[c].any()]
    if apart:
        raise ZoneError("boundary not adjacent to the zone (does it run through it?): " + ", ".join(apart))

    cand = index.query_bbox(bbox)
    mid_lat, mid_lon, _, _ = index.spatial()
    x, y = project(mid_lat[cand], mid_lon[cand])
    cx, cy = np.floor(x / cell_m).astype(np.int64), np.floor(y / cell_m).astype(np.int64)
    ok = (cx >= 0) & (cx < nx) & (cy >= 0) & (cy < ny)
    cand, cx, cy = cand[ok], cx[ok], cy[ok]
    inside = region[cy, cx]
    return ZoneResult(indices=cand[inside].astype(np.int64), area_km2=area, missing_names=[])


# ---- scope resolver --------------------------------------------------------

def _geometry_bbox(geometry: dict) -> list:
    coords = []

    def walk(node):
        if node and isinstance(node[0], (int, float)):
            coords.append(node[:2])
        else:
            for child in node:
                walk(child)

    walk(geometry["coordinates"])
    arr = np.asarray(coords, dtype=np.float64)
    return [arr[:, 0].min(), arr[:, 1].min(), arr[:, 0].max(), arr[:, 1].max()]


def _geometry_vertices(geometry: dict, limit: int = 256) -> np.ndarray:
    rings = geometry["coordinates"] if geometry["type"] == "Polygon" else \
        [ring for polygon in geometry["coordinates"] for ring in polygon]
    arr = np.asarray([pt[:2] for ring in rings for pt in ring], dtype=np.float64)
    if len(arr) > limit:
        arr = arr[np.linspace(0, len(arr) - 1, limit).astype(int)]
    return arr


class ScopeResolver:
    """Resolves rule scopes against one segment table; results are cached per (scope, exempt)."""

    def __init__(self, table, admin: list | None = None, water=None):
        self.table = table
        self.admin = admin
        self.water = water
        self._index = _index(table)
        self._cache = {}
        self._within = {}

    def resolve(self, scope: dict, exempt: dict | None = None) -> ScopeResult:
        key = json.dumps([scope, exempt], sort_keys=True, ensure_ascii=False)
        found = self._cache.get(key)
        if found is None:
            found = self._cache[key] = self._resolve(scope, exempt)
        return found

    # -- kinds --

    def _resolve(self, scope, exempt) -> ScopeResult:
        table, bbox, orphans, area_box = self.table, scope.get("bbox"), [], None
        if "segments" in scope:
            anchors = [list(a) for a in scope["segments"]]
            rows = find_segments(table, anchors) if anchors else _EMPTY
            orphans += [{"anchor": [int(v) for v in a]} for a, r in zip(anchors, rows.tolist()) if r < 0]
            idx = rows[rows >= 0]
        elif "names" in scope:
            parts = []
            for name in scope["names"]:
                segs = _mid_in(table, self._index.segments_of_ways(self._index.ways_by_text(name)), bbox)
                if len(segs) == 0:
                    orphans.append({"name": name})
                parts.append(segs)
            idx = np.concatenate(parts) if parts else _EMPTY
        elif "polygon" in scope:
            geometry = scope["polygon"]
            geometry = geometry.get("geometry", geometry) if geometry.get("type") == "Feature" else geometry
            idx = self._in_geometry(geometry, bbox)
            area_box = _geometry_bbox(geometry)
        elif "zone" in scope:
            if bbox is None:
                raise RuleError("scope.bbox: required with zone")
            idx = zone_segments(table, bbox=bbox, water=self.water, **scope["zone"]).indices
        elif "routes" in scope:
            idx, found = self._routes(scope["routes"], bbox)
            orphans += found
        elif "admin" in scope:
            idx, found, area_box = self._admin(scope["admin"])
            orphans += found
        else:
            raise RuleError("scope: needs one of segments, polygon, names, routes, zone, admin")
        idx = np.unique(np.asarray(idx, dtype=np.int64))
        # exempt pieces are searched in the bbox, else around the scope before class filtering, else in the
        # scope's own geometry, else nationwide; they are always resolved so their orphans are reported
        box = bbox if bbox is not None else (self._extent(idx) or area_box)
        idx = self._class_filter(idx, scope)
        if exempt and exempt.get("routes"):
            removed, found = self._routes(exempt["routes"], box)
            orphans += found
            idx = np.setdiff1d(idx, removed, assume_unique=True)
        return ScopeResult(indices=idx.astype(np.int64), orphans=orphans)

    def _routes(self, pieces, bbox):
        parts, orphans = [], []
        for piece in pieces:
            segs, orphan = route_piece(self.table, piece, bbox)
            parts.append(segs)
            if orphan is not None:
                orphans.append(orphan)
        return (np.unique(np.concatenate(parts)) if parts else _EMPTY), orphans

    def _in_geometry(self, geometry, bbox=None) -> np.ndarray:
        west, south, east, north = _geometry_bbox(geometry)
        if bbox is not None:
            west, south = max(west, float(bbox[0])), max(south, float(bbox[1]))
            east, north = min(east, float(bbox[2])), min(north, float(bbox[3]))
            if west > east or south > north:
                return _EMPTY
        cand = self._index.query_bbox([west, south, east, north])
        if len(cand) == 0:
            return _EMPTY
        mid_lat, mid_lon, _, _ = self._index.spatial()
        return cand[points_in_geometry(mid_lat[cand], mid_lon[cand], geometry)]

    def _inside_province(self, feature, province) -> bool:
        """A unit is in the province when most of its vertices are (shared border vertices are ambiguous)."""
        key = (id(feature), id(province))
        if key not in self._within:
            pts = _geometry_vertices(feature["geometry"])
            inside = points_in_geometry(pts[:, 1], pts[:, 0], province["geometry"])
            self._within[key] = bool(inside.mean() > 0.5) if len(pts) else False
        return self._within[key]

    def _admin(self, spec):
        if self.admin is None:
            raise RuleError("admin areas not loaded")
        target = _norm_admin(spec["province"])
        provinces = [f for f in self.admin if f["properties"].get("admin_level") == 4
                     and _norm_admin(f["properties"].get("name")) == target]
        if not provinces:
            return _EMPTY, [{"province": spec["province"]}], None
        boxes = np.array([_geometry_bbox(p["geometry"]) for p in provinces])
        box = [boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max()]
        parts, orphans = [], []
        for unit in spec["units"]:
            name = _norm_admin(unit)
            features = [f for f in self.admin if f["properties"].get("admin_level") != 4
                        and _norm_admin(f["properties"].get("name")) == name
                        and any(self._inside_province(f, p) for p in provinces)]
            if not features:
                orphans.append({"unit": unit})
            parts += [self._in_geometry(f["geometry"]) for f in features]
        idx = np.unique(np.concatenate(parts)) if parts else _EMPTY
        prefixes = tuple(p for p in (normalize_ref(s) for s in spec.get("except_refs", [])) if p)
        if prefixes and len(idx):
            ways, inverse = np.unique(self.table.way[idx], return_inverse=True)
            drop = self._index.ref_drop_mask(np.asarray(self.table.ref)[ways], prefixes)
            idx = idx[~drop[inverse]]
        return idx, orphans, box

    def _class_filter(self, idx, scope):
        def codes(key):
            try:
                return [ROAD_CLASSES.index(c) for c in scope[key]]
            except ValueError:
                raise RuleError(f"scope.{key}: values must be from {list(ROAD_CLASSES)}") from None

        if len(idx) == 0 or ("classes" not in scope and "exclude_classes" not in scope):
            return idx
        cls = np.asarray(self.table.road_class)[self.table.way[idx]]
        keep = np.ones(len(idx), dtype=bool)
        if "classes" in scope:
            keep &= np.isin(cls, codes("classes"))
        if "exclude_classes" in scope:
            keep &= ~np.isin(cls, codes("exclude_classes"))
        return idx[keep]

    def _extent(self, idx):
        if len(idx) == 0:
            return None
        mid_lat, mid_lon, _, _ = self._index.spatial()
        lat, lon = mid_lat[idx], mid_lon[idx]
        return [float(lon.min()) - EXEMPT_PAD_DEG, float(lat.min()) - EXEMPT_PAD_DEG,
                float(lon.max()) + EXEMPT_PAD_DEG, float(lat.max()) + EXEMPT_PAD_DEG]
