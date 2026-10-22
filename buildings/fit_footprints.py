"""Fit footprints to the street: shift houses off roads and alleys, then remove overlaps between buildings.

In Vietnam no building stands on a carriageway or a ngõ, and no two buildings overlap (tube houses share walls,
they do not cross). Open Buildings footprints are often 1-2 m off, so both happen in the raw data.

Pass A, per footprint touching a carriageway (polygons from clip_to_roads.py: HALF + ROUND_M, SKIP exclusions):
  1. shift: move it away from the roads it touches (direction = from the nearest point of each road's centre line
     to the footprint's centroid, weighted by the overlap area), by the smallest distance up to MAX_SHIFT_M that
     clears every carriageway nearby;
  2. cut, when no such shift exists (a house between two alleys, or more than MAX_SHIFT_M on the road): footprint
     minus the carriageways, largest part kept, dropped when under 50 % or 6 m2, as clip_to_roads.py did.
Pass B, shared walls, on the storage grid (lon/lat in units of 1e-6 degree, what compact_layer.py stores, so
  storing the result moves no vertex):
  1. close narrow gaps: tube houses in Hanoi and Ho Chi Minh City are built wall to wall, while detected footprints
     usually stop 0.3-1 m short of each other. Gaps narrower than 2 x CLOSE_M between buildings (a closing of
     their union, minus the carriageways) are filled: each building grows into the gap by up to CLOSE_M;
  2. remove overlaps: buildings are ranked OSM first, then Open Buildings by confidence (Microsoft, which has
     none, after Google), then larger first. Ground covered by several grown footprints goes to the highest-ranked
     one: final = grown - union(grown of higher-ranked neighbours). Neighbours then share the same wall, with no
     overlap and no gap. A footprint left with under 50 % of its pass A area or under 6 m2 was a duplicate and is
     dropped.
  Both steps only look at a building's own surroundings, so a building near a cell edge comes out the same from
  either cell. Pass B never adds carriageway area, so it cannot put a building back on a road.
  An overlay GEOS refuses (TopologyException) is retried on make_valid inputs, then on inputs snapped to the grid
  (overlay()); if it still fails, that tile closes no gap / that building is not grown (skipped_topology), or the
  building is dropped (dropped_topology), instead of failing the whole run.
Work is split into 0.25 degree cells (a building belongs to the cell of its centroid); pass B also reads the
pass A result of neighbouring buildings in adjacent cells.

Input  vn_buildings_merged.parquet, osm_roads.parquet. Output vn_buildings_clean.parquet: the merged columns and ids,
fitted geometry, dropped rows removed, plus shift_m, clip_m2 (cut off at roads), fill_m2 (gap filled),
overlap_m2 (given to a higher-ranked neighbour),
road_gap_m and nb_gap_m (metres to the nearest carriageway / other building, NULL when over 5 m). compact_layer.py
uses the two gaps to decide where a rectangle is safe. Summary in vn_buildings_clean.meta.json.

    .venv/Scripts/python.exe buildings/fit_footprints.py [--out data/buildings] [--workers N] [--bbox W S E N]

--workers defaults to 60 % of the logical CPUs (14 on 24).
"""
import argparse, glob, json, os, shutil, sys, time
from concurrent.futures import ProcessPoolExecutor
import duckdb, numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely, shapely.affinity
from pyproj import Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clip_to_roads import HALF, SKIP, ROUND_M, KEEP_SHARE, MIN_M2, GAP_M, UTM

CELL = 0.25
MAX_SHIFT_M = 3.0
CLOSE_M = 0.5                     # gaps under 2 x 0.5 m between buildings are closed
GRID = 1.0                        # pass B works in units of 1e-6 degree, on this integer grid
TILE = 0.005                      # degrees; gap closing runs per tile of a global grid
ROAD_MARGIN_DEG = 0.0006          # roads read around a cell: shift + widest carriageway, about 65 m
NB_MARGIN_DEG = 0.0006            # neighbours read from adjacent cells for pass B
WORKERS = max(1, round(0.6 * (os.cpu_count() or 1)))   # default processes: 60 % of the machine
BISECT = 8                        # shift precision MAX_SHIFT_M / 2**8, about 1 cm
CONF_OSM, CONF_MICROSOFT = 1.01, 0.69
GEOSError = shapely.errors.GEOSException
RETRIES = [0]                     # overlays that raised and then succeeded on repaired inputs, in this process


def overlay(fn, *geoms, snap=None, **kw):
    """fn(*geoms, **kw) for a GEOS overlay (union, intersection, difference). Only when it raises a GEOSException
    (a TopologyException such as 'side location conflict', which GEOS also raises for two valid inputs when one
    has vertices off the grid_size grid) it runs again on make_valid inputs, then on those inputs snapped to `snap`
    (default: the call's grid_size; no snapping without one). The last failure is raised. A call that does not
    raise returns exactly what fn returns."""
    try:
        return fn(*geoms, **kw)
    except GEOSError:
        pass
    s = kw.get('grid_size') if snap is None else snap
    fixes = [shapely.make_valid] + ([lambda x: shapely.set_precision(shapely.make_valid(x), s)] if s else [])
    for n, fix in enumerate(fixes):
        try:
            r = fn(*(fix(x) for x in geoms), **kw)
            RETRIES[0] += 1
            return r
        except GEOSError:
            if n == len(fixes) - 1:
                raise


def largest(g):
    if g.is_empty or g.geom_type == 'Polygon':
        return g
    p = polys(g)
    return max(shapely.get_parts(p), key=lambda x: x.area) if not p.is_empty else shapely.Polygon()


def polys(g):
    """Polygonal part of an overlay result: snapping to the grid can leave slivers as lines or points."""
    if g.geom_type in ('Polygon', 'MultiPolygon') or g.is_empty:
        return g
    p = [x for x in shapely.get_parts(g) if x.geom_type in ('Polygon', 'MultiPolygon')]
    return overlay(shapely.union_all, p) if p else shapely.Polygon()


def _each(fn, geoms, keep_types):
    """fn applied to the geometries whose type is not in keep_types (and not empty); the rest pass unchanged."""
    out = np.array(geoms, object)
    m = ~np.isin(shapely.get_type_id(out), keep_types) & ~shapely.is_empty(out)
    if m.any():
        out[m] = [fn(x) for x in out[m]]
    return out


largest_all = lambda geoms: _each(largest, geoms, (3,))          # 3 = Polygon
polys_all = lambda geoms: _each(polys, geoms, (3, 6))            # 6 = MultiPolygon


def translate(geoms, off):
    """Move each geometry by its own (dx, dy): the arithmetic of shapely.affinity.translate, for an array."""
    rep = np.repeat(np.asarray(off, float), shapely.get_num_coordinates(geoms), axis=0)
    return shapely.transform(geoms, lambda c: c + rep)


def groups(keys, n):
    """Positions of keys (ints < n) grouped by key, each group in its original order."""
    o = np.argsort(keys, kind='stable')
    return np.split(o, np.cumsum(np.bincount(keys, minlength=n))[:-1])


def group_min(keys, vals, n):
    """Minimum of vals per key (NaN where a key has none)."""
    out = np.full(n, np.inf)
    np.minimum.at(out, keys, vals)
    out[np.isinf(out)] = np.nan
    return out


_TR = None


def to_e6(geoms):
    """UTM 48N metres -> lon/lat in units of 1e-6 degree, not snapped."""
    global _TR
    if _TR is None:
        _TR = Transformer.from_crs(32648, 4326, always_xy=True)
    return shapely.transform(geoms, lambda c: np.column_stack(_TR.transform(c[:, 0], c[:, 1])) * 1e6)


def to_grid(geoms):
    """UTM 48N metres -> lon/lat in units of 1e-6 degree, snapped to the integer grid."""
    return shapely.set_precision(to_e6(geoms), GRID)


def metres_per_unit(lat):
    """Metres per grid unit east and north at a latitude."""
    return 111320 * np.cos(np.radians(lat)) * 1e-6, 110574 * 1e-6


def prepare(con, out, work, bbox):
    """Cell-sorted work copies of buildings and roads in UTM 48N, so each worker reads only its cells."""
    p = lambda n: os.path.join(out, n).replace('\\', '/')
    w = lambda n: os.path.join(work, n).replace('\\', '/')
    where = f"WHERE cx BETWEEN {bbox[0]} AND {bbox[2]} AND cy BETWEEN {bbox[1]} AND {bbox[3]}" if bbox else ''
    con.execute(f"""COPY (SELECT * FROM (
        SELECT id, src, CASE WHEN src = 'osm' THEN {CONF_OSM} ELSE coalesce(conf, {CONF_MICROSOFT}) END rank_conf,
               ST_X(ST_Centroid(geometry)) cx, ST_Y(ST_Centroid(geometry)) cy,
               ST_AsWKB(ST_MakeValid(ST_Transform(geometry, {UTM}))) wkb
        FROM '{p('vn_buildings_merged.parquet')}') {where}
        ORDER BY floor(cy / {CELL}), floor(cx / {CELL})) TO '{w('b.parquet')}' (FORMAT parquet, ROW_GROUP_SIZE 100000)""")
    con.execute(f"""COPY (SELECT ST_XMin(geometry) x0, ST_YMin(geometry) y0, ST_XMax(geometry) x1, ST_YMax(geometry) y1,
               ({HALF}) + {ROUND_M} half, ST_AsWKB(ST_Transform(geometry, {UTM})) wkb
        FROM '{p('osm_roads.parquet')}' WHERE {HALF} IS NOT NULL AND {SKIP}
        ORDER BY floor(y0 / {CELL}), floor(x0 / {CELL})) TO '{w('r.parquet')}' (FORMAT parquet, ROW_GROUP_SIZE 50000)""")
    # Largest cells first, so the dense city cells do not run alone at the end.
    return [tuple(r[:2]) for r in con.execute(f"""SELECT floor(cx / {CELL})::INT i, floor(cy / {CELL})::INT j, count(*) n
                                               FROM '{w('b.parquet')}' GROUP BY 1, 2 ORDER BY n DESC, j, i""").fetchall()]


def cell_box(i, j):
    return i * CELL, j * CELL, (i + 1) * CELL, (j + 1) * CELL


def read_box(work, box, margin=0.0):
    """Pass A input rows whose centroid lies in a lon/lat box (W S E N) grown by margin degrees."""
    x0, y0, x1, y1 = box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin
    con = duckdb.connect()
    b = con.execute(f"""SELECT id, src, rank_conf, cx, cy, wkb FROM '{work}/b.parquet'
                        WHERE cx >= {x0} AND cx < {x1} AND cy >= {y0} AND cy < {y1}""").to_arrow_table()
    return b


def roads_near(work, box, m=ROAD_MARGIN_DEG):
    """Carriageway centre lines and polygons (UTM) whose bbox meets a lon/lat box grown by m degrees."""
    con = duckdb.connect()
    r = con.execute(f"""SELECT half, wkb FROM '{work}/r.parquet'
        WHERE x1 >= {box[0] - m} AND x0 <= {box[2] + m} AND y1 >= {box[1] - m} AND y0 <= {box[3] + m}""").fetchnumpy()
    lines = shapely.from_wkb([bytes(w) for w in r['wkb']])
    bufs = shapely.buffer(lines, r['half'])
    shapely.prepare(bufs)
    return lines, bufs


def shift_cell(args):
    """Pass A for one cell: returns an arrow table (id, src, rank_conf, cx, cy, wkb on the grid, a1 = area in m2,
    shift_m, clip_m2)."""
    work, i, j = args
    b = read_box(work, cell_box(i, j))
    if not b.num_rows:
        return None
    tb, st = shift_rows(b, *roads_near(work, cell_box(i, j)))
    return tb, {'cell': (i, j), **st}


def shift_rows(b, lines, bufs):
    """Pass A on rows of read_box() against carriageways (lines, bufs) from roads_near(): (table, counts)."""
    geoms = largest_all(shapely.make_valid(shapely.from_wkb(b['wkb'].to_numpy(zero_copy_only=False))))
    a0 = shapely.area(geoms)
    out_g, shift, clip = geoms.copy(), np.zeros(len(geoms)), np.zeros(len(geoms))
    keep = np.ones(len(geoms), bool)
    if len(bufs):
        # All footprints at once: (footprint, road) pairs that overlap, then one vector step per bisection round.
        tree = STRtree(bufs)
        bi, ri = tree.query(geoms, predicate='intersects')
        ov = shapely.area(shapely.intersection(geoms[bi], bufs[ri]))
        bi, ri, ov = bi[ov > 1e-6], ri[ov > 1e-6], ov[ov > 1e-6]
        hk, inv = np.unique(bi, return_inverse=True)        # footprints on a road; pairs stay in query order
        c = shapely.centroid(geoms[hk])
        p = shapely.line_interpolate_point(lines[ri], shapely.line_locate_point(lines[ri], c[inv]))
        d = np.column_stack([shapely.get_x(c)[inv] - shapely.get_x(p), shapely.get_y(c)[inv] - shapely.get_y(p)])
        n = np.hypot(d[:, 0], d[:, 1]); far = n > 0.05
        v = np.zeros((len(hk), 2))
        np.add.at(v, inv[far], d[far] / n[far, None] * ov[far, None])
        nv = np.hypot(v[:, 0], v[:, 1])
        sk = np.flatnonzero(nv > 1e-9)                      # footprints with a direction to move in
        u = v[sk] / nv[sk, None]
        gs = geoms[hk[sk]]
        qa, qb = tree.query(shapely.buffer(gs, MAX_SHIFT_M + 0.5))

        def on_road(act, dist):
            sel = np.flatnonzero(act)
            pos = np.full(len(sk), -1); pos[sel] = np.arange(len(sel))
            moved = translate(gs[sel], u[sel] * dist[sel, None])
            pm = act[qa]
            hit = shapely.intersects(bufs[qb[pm]], moved[pos[qa[pm]]])
            out = np.zeros(len(sk), bool); out[qa[pm][hit]] = True
            return out

        lo, hi = np.zeros(len(sk)), np.full(len(sk), MAX_SHIFT_M)
        free = ~on_road(np.ones(len(sk), bool), hi)
        for _ in range(BISECT):
            mid = (lo + hi) / 2
            on = on_road(free, mid)
            lo, hi = np.where(free & on, mid, lo), np.where(free & ~on, mid, hi)
        mv = hk[sk[free]]
        out_g[mv], shift[mv] = translate(gs[free], u[free] * hi[free, None]), hi[free]
        moved = np.zeros(len(hk), bool); moved[sk[free]] = True
        hit = groups(inv, len(hk))
        for x in np.flatnonzero(~moved):
            k = hk[x]
            d = largest(shapely.difference(geoms[k], shapely.union_all(bufs[ri[hit[x]]])))
            if d.is_empty or d.area < max(KEEP_SHARE * a0[k], MIN_M2):
                keep[k] = False
            else:
                out_g[k] = d
            clip[k] = a0[k] - (0 if d.is_empty else d.area)
    grid = largest_all(to_grid(out_g[keep]))
    keep[keep] = ~shapely.is_empty(grid)
    grid = grid[~shapely.is_empty(grid)]
    return pa.table({'id': b['id'].to_numpy()[keep], 'src': b['src'].to_numpy(zero_copy_only=False)[keep],
                     'rank_conf': b['rank_conf'].to_numpy()[keep], 'cx': b['cx'].to_numpy()[keep], 'cy': b['cy'].to_numpy()[keep],
                     'wkb': shapely.to_wkb(grid), 'a1': shapely.area(out_g[keep]), 'shift_m': shift[keep], 'clip_m2': clip[keep]}), \
        {'in': int(len(geoms)), 'shifted': int((shift > 0).sum()), 'cut': int(((clip > 0) & keep).sum()),
         'dropped_road': int((~keep).sum()), 'shifted_osm': int(((shift > 0) & (b['src'].to_numpy(zero_copy_only=False) == 'osm')).sum())}


def overlap_cell(args):
    """Pass B for one cell, on the grid: close narrow gaps, give shared ground to the higher rank, measure gaps.
    Returns the final rows of the cell's buildings, geometry in lon/lat."""
    work, i, j = args
    own = pq.read_table(f'{work}/a/{i}_{j}.parquet')
    parts = [own]
    m = NB_MARGIN_DEG
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            f = f'{work}/a/{i + di}_{j + dj}.parquet'
            if (di or dj) and os.path.exists(f):
                tb = pq.read_table(f)
                cx, cy = tb['cx'].to_numpy(), tb['cy'].to_numpy()
                sel = (cx >= i * CELL - m) & (cx < (i + 1) * CELL + m) & (cy >= j * CELL - m) & (cy < (j + 1) * CELL + m)
                parts.append(tb.filter(pa.array(sel)))
    res, st = overlap_rows(pa.concat_tables(parts), own.num_rows, roads_near(work, cell_box(i, j))[1], cell_box(i, j))
    return res, {'cell': (i, j), **st}


def overlap_rows(t, n_own, bufs, box):
    """Pass B on pass A rows t, the first n_own of them the box's own buildings, the rest neighbours within
    NB_MARGIN_DEG; bufs = carriageways from roads_near(box). Returns (final rows of the own buildings, counts)."""
    own = t.slice(0, n_own)
    m = NB_MARGIN_DEG
    g = shapely.from_wkb(t['wkb'].to_numpy(zero_copy_only=False))
    kx, ky = metres_per_unit(t['cy'].to_numpy())
    mpu = float(min(kx.min(), ky))                  # metres per grid unit, the smaller axis (never overstates a gap)
    afac = kx * ky                                  # m2 per unit2, per building
    tol = CLOSE_M / mpu
    btree = STRtree(g)

    # 1. Narrow gaps: closing of the union of buildings minus buildings and carriageways, tile by tile on a global
    #    grid. Whether a point is a gap depends only on buildings within 2 tol of it, so every cell finds the same.
    lo_x, hi_x = (box[0] - m) * 1e6, (box[2] + m) * 1e6
    lo_y, hi_y = (box[1] - m) * 1e6, (box[3] + m) * 1e6
    T = TILE * 1e6
    tx, ty = np.meshgrid(np.arange(int(np.floor(lo_x / T)), int(np.floor(hi_x / T)) + 1),
                         np.arange(int(np.floor(lo_y / T)), int(np.floor(hi_y / T)) + 1), indexing='ij')
    cores = shapely.box(np.maximum(tx.ravel() * T, lo_x), np.maximum(ty.ravel() * T, lo_y),
                        np.minimum((tx.ravel() + 1) * T, hi_x), np.minimum((ty.ravel() + 1) * T, hi_y))
    qt, qb = btree.query(shapely.buffer(cores, 3 * tol, join_style='mitre'))
    tiles = [(k, qb[x]) for k, x in enumerate(groups(qt, len(cores))) if len(x)]     # tiles with buildings, in loop order
    # Carriageways, snapped to the grid only where a gap or a road gap can reach them: a gap lies in its tile core
    # and within 5 tol (mitre limit) of a building, a final footprint within 5 tol of its pass A shape.
    roads, rtree = np.zeros(0, object), None
    if len(bufs) and tiles:
        re6 = to_e6(bufs)
        e = 5 * tol + 2
        bb = shapely.bounds(g)
        tbox = np.array([[max(c[0], b[:, 0].min() - e), max(c[1], b[:, 1].min() - e), min(c[2], b[:, 2].max() + e), min(c[3], b[:, 3].max() + e)]
                         for c, b in ((shapely.bounds(cores[k]), bb[ib]) for k, ib in tiles)])
        obox = bb[:n_own] + np.array([-1, -1, 1, 1]) * (e + GAP_M / mpu)
        q = STRtree(re6).query(shapely.box(*np.vstack([tbox, obox]).T))[1]
        roads = shapely.set_precision(re6[np.unique(q)], GRID)
        rtree = STRtree(roads) if len(roads) else None
    # Every overlay goes through overlay() (retried on repaired inputs only when GEOS raises). A tile whose gaps
    # still fail closes no gap, a building whose fill still fails keeps its pass A shape (skipped_topology).
    retried0, skipped = RETRIES[0], 0
    pieces = []
    for k, ib in tiles:
        core = cores[k]
        try:
            u = polys(overlay(shapely.union_all, g[ib], grid_size=GRID))
            c = shapely.buffer(shapely.buffer(u, tol, join_style='mitre'), -tol, join_style='mitre')
            if not c.is_valid:                          # the inner buffer can self-intersect
                c = polys(shapely.make_valid(c))
            gap = polys(overlay(shapely.intersection, polys(overlay(shapely.difference, c, u, grid_size=GRID)), core, grid_size=GRID))
            if rtree is not None and not gap.is_empty:
                ir = rtree.query(gap)
                if len(ir):
                    gap = polys(overlay(shapely.difference, gap, polys(overlay(shapely.union_all, roads[ir], grid_size=GRID)), grid_size=GRID))
        except GEOSError:
            skipped += 1
            continue
        pieces += [p for p in shapely.get_parts(gap) if p.area > 0]
    grown = g.copy()
    if pieces:
        pieces = np.array(pieces, object)
        reach = shapely.buffer(g, tol, join_style='mitre')
        bi, pi = STRtree(pieces).query(reach, predicate='intersects')
        hk, inv = np.unique(bi, return_inverse=True)
        pre = np.empty(len(hk), object)
        for x, s in enumerate(groups(inv, len(hk))):
            try:
                pre[x] = polys(overlay(shapely.union_all, pieces[pi[s]], grid_size=GRID))
            except GEOSError:
                pre[x] = shapely.Polygon(); skipped += 1
        try:
            fill = polys_all(shapely.intersection(pre, reach[hk], grid_size=GRID))
            ne = ~shapely.is_empty(fill)
            grown[hk[ne]] = largest_all(shapely.union(g[hk[ne]], fill[ne], grid_size=GRID))
        except GEOSError:                           # the same, building by building, each overlay retried
            for x, k in enumerate(hk.tolist()):
                try:
                    f = polys(overlay(shapely.intersection, pre[x], reach[k], grid_size=GRID))
                    if not f.is_empty:
                        grown[k] = largest(overlay(shapely.union, g[k], f, grid_size=GRID))
                except GEOSError:
                    skipped += 1
    area = shapely.area(g)
    fill_m2 = (shapely.area(grown) - area) * afac

    # 2. Planar partition: the outlines of touching grown footprints are noded together on the grid and cut the
    #    ground into faces. Each face goes to the highest-ranked footprint covering it (OSM 1.01 > Google 0.7..1 >
    #    Microsoft 0.69, then larger, then lower id). Neighbours are built from the same faces, so they share every
    #    wall vertex for vertex; a separate difference per building would round the wall differently on each side.
    order = np.lexsort((t['id'].to_numpy(), -area * afac, -t['rank_conf'].to_numpy()))
    rank = np.empty(len(order), np.int64); rank[order] = np.arange(len(order))
    n = len(grown)
    qi, qj = STRtree(grown).query(grown, predicate='intersects')
    _, comp = connected_components(coo_matrix((np.ones(len(qi)), (qi, qj)), shape=(n, n)), directed=False)
    members = groups(comp, comp.max() + 1)
    a1 = t['a1'].to_numpy()
    small = lambda k, x: shapely.is_empty(x) | (shapely.area(x) * afac[k] < np.maximum(KEEP_SHARE * a1[k], MIN_M2))
    final = grown[:n_own].copy()
    topo = np.zeros(n_own, bool)                    # own buildings dropped because an overlay still failed
    for c in np.unique(comp[:n_own]):
        mem = members[c]
        if len(mem) == 1:
            continue
        own_k = mem[mem < n_own]
        try:
            lines = overlay(shapely.union_all, shapely.boundary(grown[mem]), grid_size=GRID)
            faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(lines)))
        except GEOSError:                           # no partition for this group: its own buildings are dropped
            final[own_k] = shapely.Polygon(); topo[own_k] = True
            continue
        faces = faces[shapely.area(faces) > 0]
        fi, mi = STRtree(grown[mem]).query(shapely.point_on_surface(faces), predicate='intersects')
        mk = mem[mi]
        alive = np.ones(len(fi), bool)
        prev = {}
        for _ in range(2):                          # the second round hands faces of dropped duplicates on
            s = np.flatnonzero(alive)
            o = s[np.lexsort((rank[mk[s]], fi[s]))]
            o = o[np.r_[True, fi[o][1:] != fi[o][:-1]]] if len(o) else o     # per face, the highest-ranked live member
            o = o[np.lexsort((fi[o], mk[o]))]
            ok, st = np.unique(mk[o], return_index=True)
            owned = dict(zip(ok.tolist(), np.split(fi[o], st[1:])))
            for k in own_k.tolist():
                fs = owned.get(k)
                if k in prev and (fs is None and prev[k] is None or fs is not None and prev[k] is not None and np.array_equal(fs, prev[k])):
                    continue
                prev[k] = fs
                try:
                    final[k] = largest(polys(overlay(shapely.union_all, faces[fs], grid_size=GRID))) if fs is not None else shapely.Polygon()
                except GEOSError:                   # dropped; its faces go to the next-ranked member, as a duplicate's
                    final[k] = shapely.Polygon(); topo[k] = True
            gone = own_k[small(own_k, final[own_k])]
            if not len(gone):
                break
            alive &= ~np.isin(mk, gone)
    lost = (shapely.area(grown[:n_own]) - shapely.area(final)) * afac[:n_own]
    keep = ~small(np.arange(n_own), final)
    fk = final[keep]

    # 3. Gaps for compact_layer.py, in metres, up to GAP_M: to the nearest carriageway and to the nearest other
    #    building (neighbours from other cells as grown here, which contains their final shape).
    road_gap = np.full(len(fk), np.nan)
    if rtree is not None and len(fk):
        a, r = rtree.query(fk, predicate='dwithin', distance=GAP_M / mpu)
        road_gap = group_min(a, shapely.distance(roads[r], fk[a]), len(fk)) * mpu
    others = grown.copy()
    others[:n_own] = np.where(keep, final, shapely.Polygon())
    own_idx = np.flatnonzero(keep)
    a, x = STRtree(others).query(fk, predicate='dwithin', distance=GAP_M / mpu)
    s = (x != own_idx[a]) & ~shapely.is_empty(others[x])
    nb_gap = group_min(a[s], shapely.distance(others[x[s]], fk[a[s]]), len(fk)) * mpu
    o = own.filter(pa.array(keep))
    res = pa.table({'id': o['id'], 'wkb': shapely.to_wkb(shapely.transform(fk, lambda c: c / 1e6)),
                    'area_m2': shapely.area(fk) * afac[:n_own][keep], 'shift_m': o['shift_m'], 'clip_m2': o['clip_m2'],
                    'fill_m2': fill_m2[:n_own][keep], 'overlap_m2': lost[keep], 'road_gap_m': road_gap, 'nb_gap_m': nb_gap})
    return res, {'filled': int(((fill_m2[:n_own] > 0.01) & keep).sum()),
                 'trimmed': int(((lost > 0.01) & keep).sum()), 'dropped_dup': int((~keep & ~topo).sum()),
                 'dropped_topology': int(topo.sum()), 'skipped_topology': skipped, 'retried_topology': RETRIES[0] - retried0}


def run(fn, jobs, workers, label, t0):
    out, stats = [], []
    ex = ProcessPoolExecutor(workers) if workers > 1 else None
    it = ex.map(fn, jobs) if ex else map(fn, jobs)
    for k, r in enumerate(it):
        if r is not None:
            out.append(r[0]); stats.append(r[1])
        if (k + 1) % 50 == 0 or k + 1 == len(jobs):
            print(f'{label} {k + 1}/{len(jobs)} cells | {time.time() - t0:.0f}s', flush=True)
    if ex:
        ex.shutdown()
    return out, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--workers', type=int, default=WORKERS, help='processes (default 60 %% of the logical CPUs)')
    ap.add_argument('--bbox', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), help='limit to a lon/lat box (testing)')
    ap.add_argument('--dst', default='vn_buildings_clean.parquet')
    a = ap.parse_args()
    t0 = time.time()
    p = lambda n: os.path.join(a.out, n).replace('\\', '/')
    work = p('_fit_work')
    shutil.rmtree(work, ignore_errors=True); os.makedirs(work + '/a')
    con = duckdb.connect(); con.execute("LOAD spatial; SET enable_progress_bar = false;")
    con.execute(f"SET temp_directory = '{p('_tmp')}'")
    cells = prepare(con, a.out, work, a.bbox)
    print('cells', len(cells), f'| {time.time() - t0:.0f}s', flush=True)

    jobs = [(work, i, j) for i, j in cells]
    tabs, sa = run(shift_cell, jobs, a.workers, 'pass A (shift off roads)', t0)
    for tb, s in zip(tabs, sa):
        pq.write_table(tb, f"{work}/a/{s['cell'][0]}_{s['cell'][1]}.parquet")
    jobs = [(work, *s['cell']) for s in sa]
    tabs, sb = run(overlap_cell, jobs, a.workers, 'pass B (shared walls)', t0)
    pq.write_table(pa.concat_tables(tabs), f'{work}/fit.parquet')

    keep = f"WHERE m.id IN (SELECT id FROM '{work}/b.parquet')" if a.bbox else ''
    con.execute(f"""COPY (
        SELECT m.* EXCLUDE (geometry, area_m2), f.area_m2, ST_GeomFromWKB(f.wkb) geometry,
               f.shift_m::FLOAT shift_m, f.clip_m2::FLOAT clip_m2, f.fill_m2::FLOAT fill_m2, f.overlap_m2::FLOAT overlap_m2,
               CASE WHEN isnan(f.road_gap_m) THEN NULL ELSE f.road_gap_m END::FLOAT road_gap_m,
               CASE WHEN isnan(f.nb_gap_m) THEN NULL ELSE f.nb_gap_m END::FLOAT nb_gap_m
        FROM '{p('vn_buildings_merged.parquet')}' m JOIN '{work}/fit.parquet' f USING (id) {keep}
        ORDER BY m.id) TO '{p(a.dst)}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)""")
    tot = lambda st, k: sum(s[k] for s in st)
    shifts = np.concatenate([t['shift_m'].to_numpy() for t in tabs]) if tabs else np.zeros(0)
    meta = {'rule': 'shift off carriageways (up to %.1f m), else cut; close gaps under %.1f m between buildings; shared ground '
                    'to the higher rank (OSM > Google by confidence > Microsoft, larger first), walls shared exactly on the '
                    '1e-6 degree grid; drop when under 50 %% or 6 m2' % (MAX_SHIFT_M, 2 * CLOSE_M),
            'buildings_in': tot(sa, 'in'), 'shifted': tot(sa, 'shifted'), 'shifted_osm': tot(sa, 'shifted_osm'),
            'shift_m_median': float(np.median(shifts[shifts > 0])) if (shifts > 0).any() else 0,
            'cut_at_road': tot(sa, 'cut'), 'dropped_on_road': tot(sa, 'dropped_road'),
            'gap_filled': tot(sb, 'filled'), 'trimmed_overlap': tot(sb, 'trimmed'), 'dropped_duplicate': tot(sb, 'dropped_dup'),
            'dropped_topology': tot(sb, 'dropped_topology'), 'skipped_topology': tot(sb, 'skipped_topology'),
            'buildings_out': int(sum(t.num_rows for t in tabs)), 'seconds': round(time.time() - t0)}
    json.dump(meta, open(p(a.dst.replace('.parquet', '.meta.json')), 'w'), indent=1)
    print('wrote', p(a.dst), json.dumps(meta))
    if not a.bbox:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
