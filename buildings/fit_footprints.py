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
Work is split into 0.25 degree cells (a building belongs to the cell of its centroid); pass B also reads the
pass A result of neighbouring buildings in adjacent cells.

Input  vn_buildings_merged.parquet, osm_roads.parquet. Output vn_buildings_clean.parquet: the merged columns and ids,
fitted geometry, dropped rows removed, plus shift_m, clip_m2 (cut off at roads), fill_m2 (gap filled),
overlap_m2 (given to a higher-ranked neighbour),
road_gap_m and nb_gap_m (metres to the nearest carriageway / other building, NULL when over 5 m). compact_layer.py
uses the two gaps to decide where a rectangle is safe. Summary in vn_buildings_clean.meta.json.

    .venv/Scripts/python.exe buildings/fit_footprints.py [--out data/buildings] [--workers 8] [--bbox W S E N]
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
BISECT = 8                        # shift precision MAX_SHIFT_M / 2**8, about 1 cm
CONF_OSM, CONF_MICROSOFT = 1.01, 0.69


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
    return shapely.union_all(p) if p else shapely.Polygon()


_TR = None


def to_grid(geoms):
    """UTM 48N metres -> lon/lat in units of 1e-6 degree, snapped to the integer grid."""
    global _TR
    if _TR is None:
        _TR = Transformer.from_crs(32648, 4326, always_xy=True)
    g = shapely.transform(geoms, lambda c: np.column_stack(_TR.transform(c[:, 0], c[:, 1])) * 1e6)
    return shapely.set_precision(g, GRID)


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
    return [tuple(r) for r in con.execute(f"""SELECT DISTINCT floor(cx / {CELL})::INT, floor(cy / {CELL})::INT
                                               FROM '{w('b.parquet')}' ORDER BY 2, 1""").fetchall()]


def read_cell(work, i, j, margin=0.0):
    x0, y0 = i * CELL - margin, j * CELL - margin
    x1, y1 = (i + 1) * CELL + margin, (j + 1) * CELL + margin
    con = duckdb.connect()
    b = con.execute(f"""SELECT id, src, rank_conf, cx, cy, wkb FROM '{work}/b.parquet'
                        WHERE cx >= {x0} AND cx < {x1} AND cy >= {y0} AND cy < {y1}""").to_arrow_table()
    return b


def roads_near(work, i, j):
    m = ROAD_MARGIN_DEG
    con = duckdb.connect()
    r = con.execute(f"""SELECT half, wkb FROM '{work}/r.parquet'
        WHERE x1 >= {i * CELL - m} AND x0 <= {(i + 1) * CELL + m} AND y1 >= {j * CELL - m} AND y0 <= {(j + 1) * CELL + m}""").fetchnumpy()
    lines = shapely.from_wkb([bytes(w) for w in r['wkb']])
    bufs = shapely.buffer(lines, r['half'])
    shapely.prepare(bufs)
    return lines, bufs


def shift_cell(args):
    """Pass A for one cell: returns an arrow table (id, src, rank_conf, cx, cy, wkb on the grid, a1 = area in m2,
    shift_m, clip_m2)."""
    work, i, j = args
    b = read_cell(work, i, j)
    if not b.num_rows:
        return None
    geoms = shapely.from_wkb(b['wkb'].to_numpy(zero_copy_only=False))
    geoms = np.array([largest(shapely.make_valid(g)) for g in geoms], object)
    a0 = shapely.area(geoms)
    lines, bufs = roads_near(work, i, j)
    out_g, shift, clip = geoms.copy(), np.zeros(len(geoms)), np.zeros(len(geoms))
    if len(bufs):
        tree = STRtree(bufs)
        bi, ri = tree.query(geoms, predicate='intersects')
        hits = {}
        for x, y in zip(bi, ri):
            hits.setdefault(int(x), []).append(int(y))
        for k, hit in hits.items():
            g = geoms[k]
            hit = [r for r in hit if shapely.intersection(g, bufs[r]).area > 1e-6]
            if not hit:
                continue
            c = g.centroid; v = np.zeros(2)
            for r in hit:
                p = lines[r].interpolate(lines[r].project(c))
                d = np.array([c.x - p.x, c.y - p.y]); n = np.hypot(*d)
                if n > 0.05:
                    v += d / n * shapely.intersection(g, bufs[r]).area
            moved = None
            if np.hypot(*v) > 1e-9:
                u = v / np.hypot(*v)
                near = tree.query(shapely.buffer(g, MAX_SHIFT_M + 0.5))
                on_road = lambda dist: bool(shapely.intersects(bufs[near], shapely.affinity.translate(g, *(u * dist))).any())
                if not on_road(MAX_SHIFT_M):
                    lo, hi = 0.0, MAX_SHIFT_M
                    for _ in range(BISECT):
                        mid = (lo + hi) / 2
                        lo, hi = (mid, hi) if on_road(mid) else (lo, mid)
                    moved = (shapely.affinity.translate(g, *(u * hi)), hi)
            if moved:
                out_g[k], shift[k] = moved
            else:
                d = largest(shapely.difference(g, shapely.union_all(bufs[hit])))
                if d.is_empty or d.area < max(KEEP_SHARE * a0[k], MIN_M2):
                    out_g[k] = None
                else:
                    out_g[k] = d
                clip[k] = a0[k] - (0 if d.is_empty else d.area)
    keep = np.array([g is not None for g in out_g])
    grid = np.array([largest(g) for g in to_grid(out_g[keep])], object)
    keep[keep] = ~shapely.is_empty(grid)
    grid = grid[~shapely.is_empty(grid)]
    return pa.table({'id': b['id'].to_numpy()[keep], 'src': b['src'].to_numpy(zero_copy_only=False)[keep],
                     'rank_conf': b['rank_conf'].to_numpy()[keep], 'cx': b['cx'].to_numpy()[keep], 'cy': b['cy'].to_numpy()[keep],
                     'wkb': shapely.to_wkb(grid), 'a1': shapely.area(out_g[keep]), 'shift_m': shift[keep], 'clip_m2': clip[keep]}), \
        {'cell': (i, j), 'in': int(len(geoms)), 'shifted': int((shift > 0).sum()), 'cut': int(((clip > 0) & keep).sum()),
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
    t = pa.concat_tables(parts)
    n_own = own.num_rows
    g = shapely.from_wkb(t['wkb'].to_numpy(zero_copy_only=False))
    kx, ky = metres_per_unit(t['cy'].to_numpy())
    mpu = float(min(kx.min(), ky))                  # metres per grid unit, the smaller axis (never overstates a gap)
    afac = kx * ky                                  # m2 per unit2, per building
    tol = CLOSE_M / mpu
    _, bufs = roads_near(work, i, j)
    roads = to_grid(bufs) if len(bufs) else np.zeros(0, object)
    rtree = STRtree(roads) if len(roads) else None
    btree = STRtree(g)

    # 1. Narrow gaps: closing of the union of buildings minus buildings and carriageways, tile by tile on a global
    #    grid. Whether a point is a gap depends only on buildings within 2 tol of it, so every cell finds the same.
    lo_x, hi_x = (i * CELL - m) * 1e6, ((i + 1) * CELL + m) * 1e6
    lo_y, hi_y = (j * CELL - m) * 1e6, ((j + 1) * CELL + m) * 1e6
    pieces = []
    for tx in range(int(np.floor(lo_x / (TILE * 1e6))), int(np.floor(hi_x / (TILE * 1e6))) + 1):
        for ty in range(int(np.floor(lo_y / (TILE * 1e6))), int(np.floor(hi_y / (TILE * 1e6))) + 1):
            core = shapely.box(max(tx * TILE * 1e6, lo_x), max(ty * TILE * 1e6, lo_y),
                               min((tx + 1) * TILE * 1e6, hi_x), min((ty + 1) * TILE * 1e6, hi_y))
            ib = btree.query(shapely.buffer(core, 3 * tol, join_style='mitre'))
            if not len(ib):
                continue
            u = polys(shapely.union_all(g[ib], grid_size=GRID))
            c = shapely.buffer(shapely.buffer(u, tol, join_style='mitre'), -tol, join_style='mitre')
            gap = polys(shapely.intersection(polys(shapely.difference(c, u, grid_size=GRID)), core, grid_size=GRID))
            if rtree is not None and not gap.is_empty:
                ir = rtree.query(gap)
                if len(ir):
                    gap = polys(shapely.difference(gap, polys(shapely.union_all(roads[ir], grid_size=GRID)), grid_size=GRID))
            pieces += [p for p in shapely.get_parts(gap) if p.area > 0]
    grown = g.copy()
    if pieces:
        pieces = np.array(pieces, object)
        reach = shapely.buffer(g, tol, join_style='mitre')
        bi, pi = STRtree(pieces).query(reach, predicate='intersects')
        near = {}
        for x, y in zip(bi, pi):
            near.setdefault(int(x), []).append(int(y))
        for k, ps in near.items():
            fill = polys(shapely.intersection(polys(shapely.union_all(pieces[ps], grid_size=GRID)), reach[k], grid_size=GRID))
            if not fill.is_empty:
                grown[k] = largest(shapely.union(g[k], fill, grid_size=GRID))
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
    members = {}
    for k in range(n):
        members.setdefault(int(comp[k]), []).append(k)
    a1 = t['a1'].to_numpy()
    small = lambda k, x: x.is_empty or x.area * afac[k] < max(KEEP_SHARE * a1[k], MIN_M2)
    final = grown[:n_own].copy()
    for c in np.unique(comp[:n_own]):
        mem = np.array(members[int(c)])
        if len(mem) == 1:
            continue
        lines = shapely.union_all(shapely.boundary(grown[mem]), grid_size=GRID)
        faces = np.array([f for f in shapely.get_parts(shapely.polygonize(shapely.get_parts(lines))) if f.area > 0], object)
        fi, mi = STRtree(grown[mem]).query(shapely.point_on_surface(faces), predicate='intersects')
        cover = {}
        for f, x in zip(fi, mi):
            cover.setdefault(int(f), []).append(int(mem[x]))
        alive = set(mem.tolist())
        for _ in range(2):                          # the second round hands faces of dropped duplicates on
            owned = {}
            for f, cs in cover.items():
                cs = [x for x in cs if x in alive]
                if cs:
                    owned.setdefault(min(cs, key=lambda x: rank[x]), []).append(f)
            gone = []
            for k in mem[mem < n_own]:
                final[k] = largest(polys(shapely.union_all(faces[owned[k]], grid_size=GRID))) if k in owned else shapely.Polygon()
                if small(k, final[k]):
                    gone.append(k)
            if not gone or not alive.intersection(gone):
                break
            alive -= set(gone)
    lost = (shapely.area(grown[:n_own]) - shapely.area(final)) * afac[:n_own]
    keep = np.array([not small(k, final[k]) for k in range(n_own)], bool)
    fk = final[keep]

    # 3. Gaps for compact_layer.py, in metres, up to GAP_M: to the nearest carriageway and to the nearest other
    #    building (neighbours from other cells as grown here, which contains their final shape).
    road_gap = np.full(len(fk), np.nan)
    if rtree is not None:
        for k, f in enumerate(fk):
            near = rtree.query(f, predicate='dwithin', distance=GAP_M / mpu)
            if len(near):
                road_gap[k] = float(shapely.distance(roads[near], f).min()) * mpu
    others = grown.copy()
    others[:n_own] = np.where(keep, final, shapely.Polygon())
    ot = STRtree(others)
    nb_gap = np.full(len(fk), np.nan)
    own_idx = np.flatnonzero(keep)
    for k, f in enumerate(fk):
        near = [x for x in ot.query(f, predicate='dwithin', distance=GAP_M / mpu) if x != own_idx[k] and not others[x].is_empty]
        if near:
            nb_gap[k] = float(shapely.distance(others[near], f).min()) * mpu
    o = own.filter(pa.array(keep))
    res = pa.table({'id': o['id'], 'wkb': shapely.to_wkb(shapely.transform(fk, lambda c: c / 1e6)),
                    'area_m2': shapely.area(fk) * afac[:n_own][keep], 'shift_m': o['shift_m'], 'clip_m2': o['clip_m2'],
                    'fill_m2': fill_m2[:n_own][keep], 'overlap_m2': lost[keep], 'road_gap_m': road_gap, 'nb_gap_m': nb_gap})
    return res, {'cell': (i, j), 'filled': int(((fill_m2[:n_own] > 0.01) & keep).sum()),
                 'trimmed': int(((lost > 0.01) & keep).sum()), 'dropped_dup': int((~keep).sum())}


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
    ap.add_argument('--workers', type=int, default=8)
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
            'buildings_out': int(sum(t.num_rows for t in tabs)), 'seconds': round(time.time() - t0)}
    json.dump(meta, open(p(a.dst.replace('.parquet', '.meta.json')), 'w'), indent=1)
    print('wrote', p(a.dst), json.dumps(meta))
    if not a.bbox:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
