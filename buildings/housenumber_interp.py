"""House numbers for every street-front footprint, interpolated between known numbers along each street (code only).

Anchors (known numbers), each with its source:
  osm     OSM addr:housenumber + addr:street of buildings (osm_buildings.parquet) and of nodes (`points`, from the PBF);
  mouth   an alley named "Ngõ 85 <Phố X>" (or "Ngách 85/12 ...") gives number 85 (12) on its parent street where it starts;
  ocr     OCR reads (ocr_housenumber_link.py) are NOT used: 2 of 28 checkable reads were right (ocr_housenumber_eval.py);
  truth   street_truth.csv rows carry no coordinates, so none can be placed.
Numbers parse as "<n>[letter]" or "<a>/<n>[letter]" (= number n of alley a of addr:street); others are counted, unused.

Streets: OSM ways with a name, keyed by the unaccented name without the Phố / Đường prefix plus the alley path
("Ngách 421/4 Hoàng Quốc Việt" -> ('hoang quoc viet', (421, 4))); ways of one key closer than JOIN_M form one street;
its axis is the longest line of their merged centre lines. A position on the street is s (m along the axis) and a side
(left / right of the axis direction).
Footprints (published buildings/by_area) between AREA_M2: a footprint takes the nearest named way within half-width +
SETBACK_M of its outline whose shortest segment to it crosses no other footprint (the front-row test of
frontrow_heights.py, here for every named way, alleys included); position = projection of its centroid on the axis.

Per street side (code rules, nothing assumed per street):
  direction  sign of the Theil-Sen slope of number against s over all anchors of the street (both sides);
  parity     majority parity of the side's anchors; a side without anchors takes the opposite of the other side;
  conflicts  anchors of the wrong parity, then anchors outside the longest run increasing along the direction, are
             dropped (counted per reason);
  interp     between two kept anchors n = linear in s, rounded to the side's parity, kept strictly inside (n1, n2);
             when the gap has fewer free numbers than houses, the extra houses repeat the number before with a letter
             suffix in s order (12, 12A, 12B), at most MAX_SUFFIX letters (more: no number);
  extrap     beyond the end anchors up to EXTRAP_M, at the side's own anchor rate (numbers / m, anchors spread over
             RATE_SPAN_M or more) or the city median rate, never below 1; source 'extrap', confidence low;
  rule       a street or alley with no kept anchor gets nothing unless RULES measured on the dev streets say so.
A footprint holding an OSM address of its own street and side keeps it (src 'osm') unless that anchor was
  dropped as a conflict.

Evaluation (`eval`): streets split into dev / hold-out by a hash of the base street name (alleys go with their street).
On hold-out streets every anchor is dropped in turn and predicted from the others; reported: median / p90 |delta n|,
share of right parity, per road class and source. Constants were chosen on dev streets only.

    .venv/Scripts/python.exe buildings/housenumber_interp.py points                       # PBF nodes -> data/buildings/osm_addr_points.parquet
    .venv/Scripts/python.exe buildings/housenumber_interp.py run [--files ha_noi_13.parquet ...] [--bbox W S E N] [--out DIR]
    .venv/Scripts/python.exe buildings/housenumber_interp.py eval [--bbox W S E N] [--json OUT]
Output per file (OUT/<file>): id, hn (text), hn_num, hn_street (OSM name), hn_src (osm / interp / extrap), hn_conf
(high / medium / low), hn_side (L / R), hn_s (m along the axis); report.json beside it.
"""
import argparse, glob, hashlib, json, os, re, sys, time, unicodedata
from collections import Counter, defaultdict

import numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from frontrow_heights import common_frame, half_width, PARAMS as FR

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB_DIR = os.path.join(REPO, 'buildings', 'by_area')
ROADS = os.path.join(REPO, 'data', 'buildings', 'osm_roads.parquet')
OSM_B = os.path.join(REPO, 'data', 'buildings', 'osm_buildings.parquet')
POINTS = os.path.join(REPO, 'data', 'buildings', 'osm_addr_points.parquet')
PBF = os.path.join(REPO, 'osrm', 'data', 'vietnam-latest.osm.pbf')
OUT_DIR = os.path.join(REPO, 'data', 'buildings', '_housenumber')

HW = FR['major_classes'] + FR['named_classes'] + ['service', 'pedestrian', 'footway', 'path', 'track']
NARROW_HALF_M = 2.0                  # half-width of service / pedestrian / footway / path / track ways
SETBACK_M = FR['setback_m']
AREA_M2 = (15.0, 2000.0)
JOIN_M = 60.0                        # ways of one name closer than this are one street
MOUTH_M = 40.0                       # alley end to parent axis, at most
EXTRAP_M = 150.0
RATE_CLAMP = (0.05, 2.0)             # numbers per metre along one side
RATE_SPAN_M = 50.0                   # a side's own rate needs anchors spread over at least this
MAX_SUFFIX = 3                       # 12A-12C; a house that would need more gets no number
RULE_MIN = 0.8                       # alley rule used only when the dev alleys agree this often
INTERP_CONF_M = (60.0, 200.0)        # anchor gap: <= high, <= medium, else low
LON0, LAT0 = 105.85, 21.03
KX, KY = 111320 * np.cos(np.radians(LAT0)), 110574.0
ALLEY = re.compile(r'^(ngo|ngach|hem|kiet)\s+(\d+(?:/\d+)*)\b\s*(.*)$')
NUM = re.compile(r'^(\d{1,4})\s*([a-z])?$')
SLASH = re.compile(r'^(\d{1,4})\s*/\s*(\d{1,4})\s*([a-z])?$')


# ---------------------------------------------------------------------------------------------------- names

def unaccent(t):
    t = unicodedata.normalize('NFD', (t or '').lower().replace('đ', 'd'))
    return ' '.join(''.join(c for c in t if unicodedata.category(c) != 'Mn').replace('.', ' ').split())


def strip_prefix(t):
    for p in ('pho ', 'duong ', 'p ', 'd '):
        if t.startswith(p):
            return t[len(p):]
    return t


def street_key(name):
    """('base', (alley numbers...)) of an OSM street name, or None."""
    t = strip_prefix(unaccent(name))
    if not t:
        return None
    m = ALLEY.match(t)
    if not m:
        return (t, ())
    nums = tuple(int(x) for x in m.group(2).split('/'))
    rest = m.group(3)
    if len(nums) == 1 and rest:
        inner = street_key(rest)
        if inner and inner[1]:                       # "Ngách 124 Ngõ 20 Phú Minh"
            return (inner[0], inner[1] + nums)
    return (strip_prefix(rest), nums) if rest else None


def parse_number(hn):
    """(alley path, number, suffix) or a reason string."""
    t = unaccent(hn).replace(' ', '')
    m = NUM.match(t)
    if m:
        return (), int(m.group(1)), (m.group(2) or '').upper()
    m = SLASH.match(t)
    if m:
        return (int(m.group(1)),), int(m.group(2)), (m.group(3) or '').upper()
    return 'range' if re.match(r'^\d+[-–]\d+', t) else 'other'


def xy(lon, lat):
    return (np.asarray(lon) - LON0) * KX, (np.asarray(lat) - LAT0) * KY


def holdout(base):
    return int(hashlib.md5(base.encode()).hexdigest(), 16) % 2 == 1


# ---------------------------------------------------------------------------------------------------- streets

class Streets:
    """Named ways of a bbox joined into streets (key + nearby), each with one axis in the metric frame."""

    def __init__(self, bbox):
        import duckdb
        con = duckdb.connect(); con.execute('LOAD spatial')
        W, S, E, N = bbox
        rows = con.execute(f"""SELECT hw, lanes, name, ST_AsWKB(geometry) FROM '{ROADS}' WHERE name IS NOT NULL
            AND hw IN ({','.join(repr(h) for h in HW)}) AND coalesce(bridge, 'no') IN ('no', '')
            AND coalesce(tunnel, 'no') IN ('no', '') AND coalesce(area, 'no') IN ('no', '')
            AND ST_XMax(geometry) >= {W} AND ST_XMin(geometry) <= {E} AND ST_YMax(geometry) >= {S} AND ST_YMin(geometry) <= {N}""").fetchall()
        g = shapely.from_wkb([bytes(r[3]) for r in rows])
        c, ri = shapely.get_coordinates(g, return_index=True)
        x, y = xy(c[:, 0], c[:, 1])
        g = shapely.set_coordinates(g, np.c_[x, y])
        keys = [street_key(r[2]) for r in rows]
        ok = np.array([k is not None for k in keys])
        self.way_geom, self.way_hw, self.way_name = g[ok], [r[0] for r, o in zip(rows, ok) if o], [r[2] for r, o in zip(rows, ok) if o]
        self.way_key = [k for k in keys if k is not None]
        self.way_reach = np.array([(half_width(h, r[1]) if h in FR['major_classes'] + FR['named_classes'] else NARROW_HALF_M)
                                   + SETBACK_M for h, r in zip(self.way_hw, [r for r, o in zip(rows, ok) if o])])
        # join ways of one key closer than JOIN_M (union-find over an STRtree query)
        n = len(self.way_geom)
        parent = np.arange(n)

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        a, b = shapely.STRtree(self.way_geom).query(self.way_geom, predicate='dwithin', distance=JOIN_M)
        for i, j in zip(a, b):
            if i < j and self.way_key[i] == self.way_key[j]:
                ri_, rj = find(i), find(j)
                if ri_ != rj:
                    parent[rj] = ri_
        roots = np.array([find(i) for i in range(n)])
        _, self.way_street = np.unique(roots, return_inverse=True)
        m = self.way_street.max() + 1 if n else 0
        self.key, self.name, self.hw, self.axis = [None] * m, [None] * m, [None] * m, [None] * m
        groups = defaultdict(list)
        for i, s in enumerate(self.way_street):
            groups[s].append(i)
        rank = {h: k for k, h in enumerate(FR['major_classes'] + FR['named_classes'] + ['service', 'pedestrian', 'footway', 'path', 'track'])}
        for s, idx in groups.items():
            merged = shapely.line_merge(shapely.union_all(self.way_geom[idx]))
            parts = shapely.get_parts(merged)
            self.axis[s] = parts[np.argmax(shapely.length(parts))]
            self.key[s] = self.way_key[idx[0]]
            self.name[s] = Counter(self.way_name[i] for i in idx).most_common(1)[0][0]
            self.hw[s] = min((self.way_hw[i] for i in idx), key=lambda h: rank[h])
        self.axis = np.array(self.axis, object)
        self.by_key = defaultdict(list)
        for s, k in enumerate(self.key):
            self.by_key[k].append(s)
        self.tree = shapely.STRtree(self.way_geom)
        self.mouth = {}                             # alley street -> s of its mouth on its own axis

    def locate(self, st, px, py):
        """s along the axis of street st and side (+1 left, -1 right) of points."""
        ax = self.axis[st]
        pts = shapely.points(px, py)
        s = shapely.line_locate_point(ax, pts)
        L = shapely.length(ax)
        p0 = shapely.get_coordinates(shapely.line_interpolate_point(ax, np.clip(s - 1, 0, L)))
        p1 = shapely.get_coordinates(shapely.line_interpolate_point(ax, np.clip(s + 1, 0, L)))
        cross = (p1[:, 0] - p0[:, 0]) * (py - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (px - p0[:, 0])
        return s, np.where(cross >= 0, 1, -1)

    def street_of_key(self, key, px, py, max_d):
        """Nearest street with this key to each point within max_d, else -1."""
        cand = self.by_key.get(key, [])
        out = np.full(len(px), -1)
        if not cand:
            return out
        pts = shapely.points(px, py)
        d = np.stack([shapely.distance(self.axis[c], pts) for c in cand])
        k = d.argmin(0)
        ok = d[k, np.arange(len(px))] <= max_d
        out[ok] = np.array(cand)[k[ok]]
        return out


# ---------------------------------------------------------------------------------------------------- footprints

def frontage(path, streets, bbox=None):
    """Footprints of one file facing a named way: ids, street index, s, side, centroid xy, polygons in the frame."""
    t = pq.read_table(path, columns=['id', 'lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring'])
    if bbox:
        lon, lat = t['lon_e6'].to_numpy() / 1e6, t['lat_e6'].to_numpy() / 1e6
        t = t.filter(pa.array((lon >= bbox[0]) & (lon <= bbox[2]) & (lat >= bbox[1]) & (lat <= bbox[3])))
    if not t.num_rows:
        return None
    geoms, (lon0, lat0, kx, ky) = common_frame(t)
    c = shapely.get_coordinates(geoms)
    _, ri = shapely.get_coordinates(geoms, return_index=True)
    gx, gy = xy(lon0 + c[:, 0] / kx, lat0 + c[:, 1] / ky)
    geoms = shapely.set_coordinates(geoms.copy(), np.c_[gx, gy])          # same frame as the streets
    ids = t['id'].to_numpy()
    area = shapely.area(geoms)
    house = np.flatnonzero((area >= AREA_M2[0]) & (area <= AREA_M2[1]))
    hi, wi = streets.tree.query(geoms[house], predicate='dwithin', distance=float(streets.way_reach.max()))
    hb = house[hi]
    d = shapely.distance(geoms[hb], streets.way_geom[wi])
    ok = d <= streets.way_reach[wi]
    hb, wi, d = hb[ok], wi[ok], d[ok]
    seg = shapely.shortest_line(geoms[hb], streets.way_geom[wi])
    si, bi = shapely.STRtree(geoms).query(seg, predicate='intersects')
    other = bi != hb[si]
    si, bi = si[other], bi[other]
    cross = shapely.length(shapely.intersection(seg[si], geoms[bi])) > FR['cross_tolerance_m']
    blocked = np.zeros(len(seg), bool); blocked[si[cross]] = True
    hb, wi, d = hb[~blocked], wi[~blocked], d[~blocked]
    o = np.lexsort((d, hb))                                                # nearest way per house
    hb, wi = hb[o], wi[o]
    first = np.r_[True, hb[1:] != hb[:-1]]
    hb, wi = hb[first], wi[first]
    st = streets.way_street[wi]
    cen = shapely.centroid(geoms[hb])
    cx, cy = shapely.get_x(cen), shapely.get_y(cen)
    s, side = np.zeros(len(hb)), np.zeros(len(hb), int)
    for u in np.unique(st):
        k = st == u
        s[k], side[k] = streets.locate(u, cx[k], cy[k])
    return dict(ids=ids, geoms=geoms, house=hb, street=st, s=s, side=side)


# ---------------------------------------------------------------------------------------------------- anchors

def osm_addresses(bbox):
    """(x, y, street name, housenumber, kind) of OSM building addresses (centroid) and address nodes in the bbox."""
    import duckdb
    con = duckdb.connect(); con.execute('LOAD spatial')
    W, S, E, N = bbox
    rows = con.execute(f"""SELECT ST_X(c), ST_Y(c), addr_street, addr_housenumber FROM (SELECT ST_Centroid(geometry) c,
        addr_street, addr_housenumber FROM '{OSM_B}' WHERE addr_housenumber IS NOT NULL AND addr_street IS NOT NULL)
        WHERE ST_X(c) BETWEEN {W} AND {E} AND ST_Y(c) BETWEEN {S} AND {N}""").fetchall()
    kind = ['building'] * len(rows)
    if os.path.exists(POINTS):
        p = con.execute(f"""SELECT lon, lat, street, hn FROM '{POINTS}' WHERE lon BETWEEN {W} AND {E} AND lat BETWEEN {S} AND {N}""").fetchall()
        rows += p; kind += ['node'] * len(p)
    x, y = xy([r[0] for r in rows], [r[1] for r in rows])
    return x, y, [r[2] for r in rows], [r[3] for r in rows], kind


def build_anchors(streets, bbox, counts):
    """Anchors: dict street -> list of (s, side, n, suffix, src, x, y)."""
    A = defaultdict(list)
    x, y, st_names, hns, kind = osm_addresses(bbox)
    for i in range(len(x)):
        k = street_key(st_names[i])
        p = parse_number(hns[i])
        if k is None:
            counts['osm_no_street'] += 1; continue
        if isinstance(p, str):
            counts[f'osm_number_{p}'] += 1; continue
        path, n, suf = p
        key = (k[0], k[1] + path)
        st = streets.street_of_key(key, np.array([x[i]]), np.array([y[i]]), 60.0)[0]
        if st < 0:
            counts['osm_no_matching_way'] += 1; continue
        s, side = streets.locate(st, np.array([x[i]]), np.array([y[i]]))
        A[st].append((float(s[0]), int(side[0]), n, suf, 'osm', x[i], y[i]))
        counts[f'osm_{kind[i]}'] += 1
    for st, (base, path) in enumerate(streets.key):                        # alley mouths on the parent street
        if not path:
            continue
        ax = streets.axis[st]
        ends = shapely.get_coordinates(ax)[[0, -1]]
        par = [streets.street_of_key((base, path[:-1]), ends[j:j + 1, 0], ends[j:j + 1, 1], MOUTH_M)[0] for j in (0, 1)]
        dd = [shapely.distance(streets.axis[p], shapely.points(*ends[j])) if p >= 0 else np.inf for j, p in enumerate(par)]
        j = int(np.argmin(dd))
        if not np.isfinite(dd[j]):
            counts['mouth_no_parent'] += 1; continue
        p = par[j]
        L = shapely.length(ax)
        inner = shapely.get_coordinates(shapely.line_interpolate_point(ax, min(15.0, L / 2) if j == 0 else max(L - 15.0, L / 2)))
        s, _ = streets.locate(p, ends[j:j + 1, 0], ends[j:j + 1, 1])
        _, side = streets.locate(p, inner[:, 0], inner[:, 1])
        A[p].append((float(s[0]), int(side[0]), path[-1], '', 'mouth', ends[j, 0], ends[j, 1]))
        streets.mouth[st] = 0.0 if j == 0 else float(L)
        counts['mouth'] += 1
    return A


# ---------------------------------------------------------------------------------------------------- numbering

def theil_sen_sign(s, n):
    if len(s) < 2:
        return 0
    i, j = np.triu_indices(len(s), 1)
    ds = s[j] - s[i]
    k = np.abs(ds) > 1.0
    if not k.any():
        return 0
    return int(np.sign(np.median((n[j] - n[i])[k] / ds[k])))


def lis_keep(n):
    """Indices of a longest strictly increasing subsequence of n (in the given order)."""
    import bisect
    tails, tail_i, prev = [], [], np.full(len(n), -1)
    for i, v in enumerate(n):
        p = bisect.bisect_left(tails, v)
        if p == len(tails):
            tails.append(v); tail_i.append(i)
        else:
            tails[p] = v; tail_i[p] = i
        prev[i] = tail_i[p - 1] if p else -1
    out, i = [], tail_i[-1] if tail_i else -1
    while i >= 0:
        out.append(i); i = prev[i]
    return out[::-1]


def clean_side(anc, direction, parity, counts):
    """Kept anchors of one side as arrays (s, n), sorted along the numbering direction."""
    a = [r for r in anc if r[2] % 2 == parity]
    counts['dropped_parity'] += len(anc) - len(a)
    if not a:
        return np.zeros(0), np.zeros(0, int)
    by_n = defaultdict(list)                       # one position per number (several nodes of one building)
    for r in a:
        by_n[r[2]].append(r[0])
    s = np.array([np.median(v) for v in by_n.values()]); n = np.array(list(by_n), int)
    o = np.argsort(s * direction)
    s, n = s[o], n[o]
    keep = lis_keep(list(n))
    counts['dropped_order'] += len(n) - len(keep)
    return s[keep], n[keep]


def number_side(hs, s_a, n_a, parity, direction, rate, counts):
    """Numbers (n, suffix, src, conf) for houses at positions hs on one side with kept anchors (s_a, n_a)."""
    out = [None] * len(hs)
    if not len(s_a):
        return out
    t = hs * direction; ta = s_a * direction                     # increasing numbers along t
    order = np.argsort(t)
    snap = lambda v: int(2 * np.round((v - parity) / 2) + parity)
    if len(ta) >= 2 and ta[-1] - ta[0] >= RATE_SPAN_M:
        own = np.clip(np.median(np.diff(n_a) / np.maximum(np.diff(ta), 1.0)), *RATE_CLAMP)
    else:
        own = rate
    prev_n, prev_cnt, gap_id = None, 0, None
    for i in order:
        v = t[i]
        j = np.searchsorted(ta, v)
        if 0 < j < len(ta):
            t1, t2, n1, n2 = ta[j - 1], ta[j], n_a[j - 1], n_a[j]
            raw = n1 + (v - t1) / max(t2 - t1, 1e-6) * (n2 - n1)
            n = min(max(snap(raw), n1 + 2), n2 - 2) if n2 - n1 >= 4 else n1
            gap = t2 - t1
            conf = 'high' if gap <= INTERP_CONF_M[0] else ('medium' if gap <= INTERP_CONF_M[1] else 'low')
            src, g = 'interp', j
        else:
            e = ta[0] - v if j == 0 else v - ta[-1]
            if e > EXTRAP_M:
                counts['beyond_extrap'] += 1; continue
            n = snap(n_a[0] - e * own) if j == 0 else snap(n_a[-1] + e * own)
            if n < 1:
                n = parity if parity else 2
                if j == 0 and n >= n_a[0]:
                    counts['beyond_extrap'] += 1; continue
            src, conf, g = 'extrap', 'low', ('lo' if j == 0 else 'hi')
        if prev_n is not None and g == gap_id and n <= prev_n:   # monotone: repeat with a letter suffix
            n = prev_n; prev_cnt += 1
            if prev_cnt > MAX_SUFFIX:
                counts['suffix_overflow'] += 1; continue
            suf = chr(ord('A') + prev_cnt - 1)
        else:
            prev_cnt, suf = 0, ''
            if 0 < j < len(ta) and n == n_a[j - 1]:
                prev_cnt, suf = 1, 'A'
        prev_n, gap_id = n, g
        out[i] = (n, suf, src, conf)
    return out


def number_streets(F_all, A, streets, counts, rate=None, only=None):
    """Assign numbers to every footprint row of F_all (concatenated frontage tables)."""
    st_all, s_all, side_all = F_all['street'], F_all['s'], F_all['side']
    res = {}
    rates = []
    plan = {}
    for st, anc in A.items():
        if only is not None and st not in only:
            continue
        s = np.array([r[0] for r in anc]); n = np.array([r[2] for r in anc], int)
        direction = theil_sen_sign(s, n) or (1 if len(anc) == 1 else 0)
        if not direction:
            counts['street_no_direction'] += 1; continue
        par = {}
        for sd in (1, -1):
            p = [r[2] % 2 for r in anc if r[1] == sd]
            par[sd] = Counter(p).most_common(1)[0][0] if p else None
        if par[1] is None and par[-1] is None:
            continue
        for sd in (1, -1):
            if par[sd] is None:
                par[sd] = 1 - par[-sd]
        if par[1] == par[-1]:
            counts['street_same_parity_both_sides'] += 1
        sides = {}
        for sd in (1, -1):
            sa, na = clean_side([r for r in anc if r[1] == sd], direction, par[sd], counts)
            sides[sd] = (sa, na)
            if len(sa) >= 2:
                rates.append(np.median(np.diff(na) / np.maximum(np.abs(np.diff(sa)), 1.0)))
        plan[st] = (direction, par, sides)
    if not rate:
        rate = float(np.clip(np.median(rates), *RATE_CLAMP)) if rates else 0.5
    for st, (direction, par, sides) in plan.items():
        rows = np.flatnonzero(st_all == st)
        for sd in (1, -1):
            r = rows[side_all[rows] == sd]
            if not len(r):
                continue
            sa, na = sides[sd]
            got = number_side(s_all[r], sa, na, par[sd], direction, rate, counts)
            for k, v in zip(r, got):
                if v is not None:
                    res[k] = v
    return res, rate, plan


# ---------------------------------------------------------------------------------------------------- alley rule

def measure_alley_rules(A, streets, use):
    """On the alleys in `use` with a mouth and >= 2 OSM anchors: share numbered upward from the mouth, share with the
    odd side on the left looking in from the mouth, median numbers per metre and median number at the mouth."""
    inc, left_odd, rate, a0 = [], [], [], []
    for st in use:
        anc = [r for r in A.get(st, []) if r[4] == 'osm']
        if st not in streets.mouth or len(anc) < 2:
            continue
        m = streets.mouth[st]; f = 1 if m == 0 else -1
        u = np.array([abs(r[0] - m) for r in anc]); n = np.array([r[2] for r in anc], float)
        sg = theil_sen_sign(u, n)
        if not sg:
            continue
        inc.append(sg > 0)
        left = [r[2] % 2 for r in anc if r[1] * f > 0]; right = [r[2] % 2 for r in anc if r[1] * f < 0]
        if left:
            left_odd.append(Counter(left).most_common(1)[0][0] == 1)
        elif right:
            left_odd.append(Counter(right).most_common(1)[0][0] == 0)
        if sg > 0 and np.ptp(u) > 5:
            b = np.polyfit(u, n, 1)
            rate.append(b[0] / 2); a0.append(b[1])       # per side: numbers step by 2
    R = dict(alleys=len(inc), up_from_mouth=round(float(np.mean(inc)), 3) if inc else None,
             odd_left=round(float(np.mean(left_odd)), 3) if left_odd else None,
             rate_per_m=round(float(np.clip(np.median(rate), *RATE_CLAMP)), 3) if rate else None,
             n_at_mouth=round(float(np.median(a0)), 1) if a0 else None)
    R['apply'] = bool(inc and left_odd and rate and R['up_from_mouth'] >= RULE_MIN
                      and max(R['odd_left'], 1 - R['odd_left']) >= RULE_MIN)
    return R


def number_by_rule(st, hs, side, streets, R, counts):
    """Numbers for houses of an alley without anchors from the measured rule: n = n_at_mouth + 2 x rate x u."""
    m = streets.mouth[st]; f = 1 if m == 0 else -1
    out = [None] * len(hs)
    for sd in (1, -1):
        k = np.flatnonzero(side == sd)
        if not len(k):
            continue
        parity = 1 if (sd * f > 0) == (R['odd_left'] >= 0.5) else 0
        L = float(shapely.length(streets.axis[st]))
        base = max(1, int(round(R['n_at_mouth'])))
        lo = base - 2 if (base - 2) % 2 == parity else base - 1
        hi_n = lo + 2 * R['rate_per_m'] * L + 4
        sa, na = np.array([m, L - m if m == 0 else 0.0]), np.array([lo, int(hi_n) + (int(hi_n) % 2 != parity)])
        got = number_side(hs[k], sa, na, parity, f, R['rate_per_m'], counts)
        for i, v in zip(k, got):
            if v is not None and v[0] >= 1:
                out[i] = (v[0], v[1], 'rule', 'low')
    return out


# ---------------------------------------------------------------------------------------------------- commands

def file_list(files, bbox):
    idx = json.load(open(os.path.join(PUB_DIR, 'index.json'), encoding='utf-8'))
    ents = [e for e in idx['files'] if e['file'].startswith('ha_noi_')]
    if files:
        ents = [e for e in ents if e['file'] in files]
    if bbox:
        ents = [e for e in ents if e['bbox'][2] >= bbox[0] and e['bbox'][0] <= bbox[2] and e['bbox'][3] >= bbox[1] and e['bbox'][1] <= bbox[3]]
    bb = np.array([e['bbox'] for e in ents], float)
    full = (bb[:, 0].min() - .01, bb[:, 1].min() - .01, bb[:, 2].max() + .01, bb[:, 3].max() + .01)
    return [e['file'] for e in ents], (bbox or full)


def load_all(files, bbox, t0):
    names, bb = file_list(files, bbox)
    streets = Streets(bb)
    print(f'streets {len(streets.axis)} from {len(streets.way_geom)} ways | {time.time() - t0:.0f}s', flush=True)
    parts = []
    for f in names:
        F = frontage(os.path.join(PUB_DIR, f), streets, bbox)
        if F is None:
            continue
        F['file'] = f
        parts.append(F)
        print(f'{f}: {len(F["house"])} street-front of {len(F["ids"])} | {time.time() - t0:.0f}s', flush=True)
    F_all = dict(street=np.concatenate([p['street'] for p in parts]), s=np.concatenate([p['s'] for p in parts]),
                 side=np.concatenate([p['side'] for p in parts]))
    return names, bb, streets, parts, F_all


def osm_owner(parts, A, streets, plan):
    """Street-front rows (in F_all order) whose footprint holds an OSM address kept as an anchor (not dropped for parity
    or order): row -> (n, suffix, street name)."""
    kept = {(st, sd, int(n)) for st, (_, _, sides) in plan.items() for sd, (_, na) in sides.items() for n in na}
    pts = [(r[5], r[6], r[2], r[3], streets.name[st], st, r[1]) for st, anc in A.items() for r in anc
           if r[4] == 'osm' and (st, r[1], r[2]) in kept]
    if not pts:
        return {}
    P = shapely.points([q[0] for q in pts], [q[1] for q in pts])
    own, off = {}, 0
    for p in parts:
        tree = shapely.STRtree(p['geoms'][p['house']])
        for a_i, g_i in zip(*tree.query(P, predicate='within')):
            q = pts[a_i]
            if p['street'][g_i] == q[5] and p['side'][g_i] == q[6]:          # same street and side as the house
                own[off + int(g_i)] = q[2:5]
        off += len(p['house'])
    return own


def cmd_run(a):
    t0 = time.time()
    counts = Counter()
    names, bb, streets, parts, F_all = load_all(a.files, a.bbox, t0)
    A = build_anchors(streets, bb, counts)
    res, rate, plan = number_streets(F_all, A, streets, counts)
    R = measure_alley_rules(A, streets, [st for st in A if not holdout(streets.key[st][0])])
    if R['apply']:
        st_all = F_all['street']
        for st in streets.mouth:
            if st in plan:
                continue
            rows = np.flatnonzero(st_all == st)
            if len(rows):
                for g, v in zip(rows, number_by_rule(st, F_all['s'][rows], F_all['side'][rows], streets, R, counts)):
                    if v is not None:
                        res[g] = v
    own = osm_owner(parts, A, streets, plan)
    os.makedirs(a.out, exist_ok=True)
    tot, off = Counter(), 0
    for p in parts:
        k = len(p['house'])
        rows = []
        for i in range(k):
            g = off + i
            st = p['street'][i]
            name = streets.name[st]
            if g in own:
                n, suf, name = own[g]; src, conf = 'osm', 'high'
            elif g in res:
                n, suf, src, conf = res[g]
            else:
                continue
            rows.append((int(p['ids'][p['house'][i]]), f'{n}{suf}', int(n), name, src, conf,
                         'L' if p['side'][i] > 0 else 'R', round(float(p['s'][i]), 1)))
            tot[src] += 1; tot[f'conf_{conf}'] += 1
        tot['street_front'] += k
        off += k
        cols = list(zip(*rows)) if rows else [[]] * 8
        t = pa.table({'id': pa.array(cols[0], pa.int64()), 'hn': pa.array(cols[1], pa.string()),
                      'hn_num': pa.array(cols[2], pa.int32()), 'hn_street': pa.array(cols[3], pa.string()),
                      'hn_src': pa.array(cols[4], pa.string()), 'hn_conf': pa.array(cols[5], pa.string()),
                      'hn_side': pa.array(cols[6], pa.string()), 'hn_s': pa.array(cols[7], pa.float32())})
        pq.write_table(t, os.path.join(a.out, p['file']), compression='zstd')
    rep = dict(bbox=bb, files=names, numbers=dict(tot), anchors=dict(counts), rate_numbers_per_m=round(rate, 3), alley_rule=R,
               streets=len(streets.axis), streets_with_anchors=len(plan),
               params=dict(AREA_M2=AREA_M2, JOIN_M=JOIN_M, MOUTH_M=MOUTH_M, EXTRAP_M=EXTRAP_M, RATE_CLAMP=RATE_CLAMP,
                           INTERP_CONF_M=INTERP_CONF_M, SETBACK_M=SETBACK_M, NARROW_HALF_M=NARROW_HALF_M))
    json.dump(rep, open(os.path.join(a.out, 'report.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False)[:2000])
    print(f'{time.time() - t0:.0f}s')


def cmd_eval(a):
    """Leave-one-anchor-out on hold-out streets (OSM anchors only are scored)."""
    t0 = time.time()
    counts = Counter()
    names, bb, streets, parts, F_all = load_all(a.files, a.bbox, t0)
    A = build_anchors(streets, bb, counts)
    _, rate, _ = number_streets(F_all, {k: v for k, v in A.items() if not holdout(streets.key[k][0])}, streets, Counter())
    errs = defaultdict(list)
    for split in ('dev', 'holdout'):
        for st, anc in A.items():
            if holdout(streets.key[st][0]) != (split == 'holdout'):
                continue
            for i, r in enumerate(anc):
                if r[4] != 'osm':
                    continue
                rest = [q for q in anc if not (q[2] == r[2] and q[1] == r[1])]   # every anchor of this number
                if not rest:
                    continue
                F1 = dict(street=np.array([st]), s=np.array([r[0]]), side=np.array([r[1]]))
                res, _, _ = number_streets(F1, {st: rest}, streets, Counter(), rate=rate)
                if 0 not in res:
                    errs[(split, 'none')].append(None); continue
                n, _, src, conf = res[0]
                cls = 'alley' if streets.key[st][1] else ('major' if streets.hw[st] in FR['major_classes'] else 'minor')
                for grp in ('all', cls, f'{src}_{conf}'):
                    errs[(split, grp)].append((abs(n - r[2]), n % 2 == r[2] % 2))
    R = measure_alley_rules(A, streets, [st for st in A if not holdout(streets.key[st][0])])
    for st, anc in A.items():                     # street-level hold-out of the rule: alleys predicted with no anchor
        if not holdout(streets.key[st][0]) or st not in streets.mouth or not R['rate_per_m']:
            continue
        o = [r for r in anc if r[4] == 'osm']
        if not o:
            continue
        got = number_by_rule(st, np.array([r[0] for r in o]), np.array([r[1] for r in o]), streets, R, Counter())
        for r, v in zip(o, got):
            if v is None:
                errs[('holdout', 'rule_none')].append(None)
            else:
                errs[('holdout', 'rule_alley_no_anchor')].append((abs(v[0] - r[2]), v[0] % 2 == r[2] % 2))
    rep = {'rate_numbers_per_m': round(rate, 3), 'alley_rule_dev': R, 'anchors': dict(counts)}
    for (split, grp), v in sorted(errs.items()):
        if grp in ('none', 'rule_none'):
            rep.setdefault(split, {})[f'{grp}_no_prediction'] = len(v); continue
        d = np.array([e[0] for e in v]); p = np.array([e[1] for e in v])
        rep.setdefault(split, {})[grp] = dict(n=len(v), median_abs=float(np.median(d)), p90_abs=float(np.percentile(d, 90)),
                                              exact=round(float((d == 0).mean()), 3), within_4=round(float((d <= 4).mean()), 3),
                                              parity_ok=round(float(p.mean()), 3))
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json:
        json.dump(rep, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'{time.time() - t0:.0f}s')


def cmd_points(a):
    import osmium
    rows = []
    fp = osmium.FileProcessor(PBF, osmium.osm.NODE).with_filter(osmium.filter.KeyFilter('addr:housenumber'))
    for o in fp:
        st = o.tags.get('addr:street')
        if st and o.location.valid():
            rows.append((o.location.lon, o.location.lat, st, o.tags['addr:housenumber']))
    t = pa.table({'lon': [r[0] for r in rows], 'lat': [r[1] for r in rows], 'street': [r[2] for r in rows], 'hn': [r[3] for r in rows]})
    pq.write_table(t, POINTS, compression='zstd')
    print(f'{len(rows)} address nodes -> {POINTS}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('points')
    for c in ('run', 'eval'):
        p = sub.add_parser(c)
        p.add_argument('--files', nargs='*')
        p.add_argument('--bbox', type=float, nargs=4)
        if c == 'run':
            p.add_argument('--out', default=OUT_DIR)
        else:
            p.add_argument('--json')
    a = ap.parse_args()
    {'points': cmd_points, 'run': cmd_run, 'eval': cmd_eval}[a.cmd](a)


if __name__ == '__main__':
    main()
