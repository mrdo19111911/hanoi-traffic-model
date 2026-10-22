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
CARRIAGEWAY_M = 60.0        # a dropped part of the road this close to the kept piece runs beside it ...
CARRIAGEWAY_DETOUR_M = 300.0  # ... when along the road it is farther than this from the piece: a lost carriageway
CARRIAGEWAY_STEP_M = 10.0   # sampling step along segments for that test
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
        lo, hi = np.searchsorted(sorted_lat, south, "left"), np.searchsorted(sorted_lat, north, "right")
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
    of the road from that crossing.

    A carriageway can still be lost: one with no node within 60 m of a crossing node gets no
    source, and with one crossing, carriageways joined only at it share a single far end. When a
    dropped part of the road runs beside the kept piece (see ``_lost_carriageway``), the kept
    segments are returned with the orphan ``{"missing": "carriageway"}``.
    """
    index = _index(table)

    def orphan(what):
        return _EMPTY, {"route": dict(piece), "missing": what}

    parts = []
    if piece.get("name"):
        parts.append(index.ways_by_text(piece["name"]))
    if piece.get("ref"):
        parts.append(index.ways_by_ref(piece["ref"]))
    ways = np.unique(np.concatenate(parts)) if parts else _EMPTY
    road = _mid_in(table, index.segments_of_ways(ways), bbox)
    if len(road) == 0:
        return orphan("name")
    start, end = piece.get("from"), piece.get("to")
    if not start and not end:
        return road, None
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
        return orphan("from")
    far = crossing(end) if end else None
    if far is not None and len(far) == 0:
        return orphan("to")
    # every road node near a crossing node seeds the piece too, so a carriageway that the crossing
    # street does not touch (T-junction) gets its own source
    near = None if near is None else _nodes_near(lat, lon, near, CROSSING_SEED_M)
    far = None if far is None else _nodes_near(lat, lon, far, CROSSING_SEED_M)
    graph = _graph(len(nodes), ea, eb, table.length_m[road])
    n_comp, labels = connected_components(graph, directed=False)
    if near is None:
        near = _far_ends(graph, labels, far)
    if far is None:
        far = _far_ends(graph, labels, near)
    d_near = dijkstra(graph, directed=False, indices=near, min_only=True)
    d_far = dijkstra(graph, directed=False, indices=far, min_only=True)
    # D per connected component (a carriageway, a stub): each runs between its own crossing nodes
    span = np.full(n_comp, np.inf)
    np.minimum.at(span, labels[far], d_near[far])
    if not np.isfinite(span).any():
        return orphan("path")
    total = d_near + d_far
    ok = np.isfinite(total) & (total <= PIECE_SLACK * span[labels] + PIECE_EXTRA_M)
    keep = ok[ea] & ok[eb]
    if _lost_carriageway(graph, lat, lon, ea, eb, table.length_m[road], keep):
        return road[keep], {"route": dict(piece), "missing": "carriageway"}
    return road[keep], None


def _samples(x, y, ea, eb, step):
    """Points every ``step`` metres along the segments (ends included): (px, py, segment, t in [0, 1])."""
    n = np.maximum(1, np.ceil(np.hypot(x[eb] - x[ea], y[eb] - y[ea]) / step)).astype(np.int64)
    seg = np.repeat(np.arange(len(n)), n + 1)
    k = np.arange(int((n + 1).sum())) - np.repeat(np.cumsum(n + 1) - (n + 1), n + 1)
    t = k / n[seg]
    a, b = ea[seg], eb[seg]
    return x[a] + t * (x[b] - x[a]), y[a] + t * (y[b] - y[a]), seg, t


def _lost_carriageway(graph, lat, lon, ea, eb, length, keep) -> bool:
    """Does a dropped part of the road run beside the kept piece, i.e. is a carriageway lost?

    A point of a dropped segment counts when it lies within CARRIAGEWAY_M (straight line) of a kept
    segment while along the road it is more than CARRIAGEWAY_DETOUR_M from every kept segment, or not
    connected to them at all. The road beyond the piece's ends is near the piece only where it is also
    near along the road, so it does not count.
    """
    if keep.all() or not keep.any():
        return False
    kept_nodes = np.unique(np.concatenate([ea[keep], eb[keep]]))
    along = dijkstra(graph, directed=False, indices=kept_nodes, min_only=True)
    drop = np.flatnonzero(~keep)
    seg_len = np.maximum(length[drop].astype(np.float64), 0.01)
    lo = np.minimum(along[ea[drop]], along[eb[drop]])
    reach = lo + seg_len > CARRIAGEWAY_DETOUR_M          # some point may be farther than the detour
    drop, seg_len = drop[reach], seg_len[reach]
    if drop.size == 0:
        return False
    kx = M_PER_DEG * math.cos(math.radians(float(np.mean(lat))))
    x, y = (lon - lon[0]) * kx, (lat - lat[0]) * M_PER_DEG
    px, py, seg, t = _samples(x, y, ea[drop], eb[drop], CARRIAGEWAY_STEP_M)
    sample_along = np.minimum(along[ea[drop]][seg] + t * seg_len[seg], along[eb[drop]][seg] + (1 - t) * seg_len[seg])
    far = sample_along > CARRIAGEWAY_DETOUR_M
    if not far.any():
        return False
    kx_pts, ky_pts, _, _ = _samples(x, y, ea[keep], eb[keep], CARRIAGEWAY_STEP_M)
    near, _ = cKDTree(np.column_stack([kx_pts, ky_pts])).query(
        np.column_stack([px[far], py[far]]), distance_upper_bound=CARRIAGEWAY_M)
    return bool(np.isfinite(near).any())


def _nodes_near(lat: np.ndarray, lon: np.ndarray, found: np.ndarray, radius_m: float) -> np.ndarray:
    """Nodes within ``radius_m`` (haversine) of any ``found`` node, the found nodes included."""
    kx = M_PER_DEG * math.cos(math.radians(float(np.mean(lat[found]))))
    xy = np.column_stack([(lon - lon[found[0]]) * kx, (lat - lat[found[0]]) * M_PER_DEG])
    lists = cKDTree(xy).query_ball_point(xy[found], r=radius_m * 1.01 + 1.0)
    pair_f = np.repeat(found, [len(c) for c in lists])
    pair_c = np.fromiter((c for cs in lists for c in cs), dtype=np.int64, count=len(pair_f))
    close = haversine_m(lat[pair_f], lon[pair_f], lat[pair_c], lon[pair_c]) <= radius_m
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
            if orphan is not None and orphan["missing"] == "carriageway":
                orphan = None           # the kept side still bounds the zone (the leak and area checks hold)
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
