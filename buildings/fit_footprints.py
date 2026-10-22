"""Fit footprints to the street: shift houses off roads and alleys, then remove overlaps between buildings.

In Vietnam no building stands on a carriageway or a ngõ, and no two buildings overlap (tube houses share walls,
they do not cross). Open Buildings footprints are often 1-2 m off, so both happen in the raw data.

Pass A, per footprint touching a carriageway (polygons from clip_to_roads.py: HALF + ROUND_M, SKIP exclusions):
  1. shift: move it away from the roads it touches (direction = from the nearest point of each road's centre line
     to the footprint's centroid, weighted by the overlap area), by the smallest distance up to MAX_SHIFT_M that
     clears every carriageway nearby;
  2. cut, when no such shift exists (a house between two alleys, or more than MAX_SHIFT_M on the road): footprint
     minus the carriageways, largest part kept, dropped when under 50 % or 6 m2, as clip_to_roads.py did.
Pass B, overlaps: buildings are ranked OSM first, then Open Buildings by confidence (Microsoft, which has none,
  after Google), then larger first. Each footprint loses whatever lies within WALL_GAP_M of a higher-ranked
  footprint, so overlaps go and touching walls (tube houses) end up 10 cm apart: storing coordinates at 1e-6
  degrees moves an edge by up to about 7 cm, which would otherwise push a shared wall into the neighbour.
  If less than 50 % or 6 m2 is left it was a duplicate and is dropped. Pass B only shrinks footprints, so it
  cannot put one back on a road.
Work is split into 0.25 degree cells (a building belongs to the cell of its centroid); pass B also reads the
pass A result of neighbouring buildings in adjacent cells, so overlaps across cell edges are removed too.

Input  vn_buildings_merged.parquet, osm_roads.parquet. Output vn_buildings_clean.parquet: the merged columns and ids,
fitted geometry, dropped rows removed, plus shift_m, clip_m2 (cut off at roads), overlap_m2 (cut off at neighbours),
road_gap_m and nb_gap_m (metres to the nearest carriageway / other building, NULL when over 5 m). compact_layer.py
uses the two gaps to decide where a rectangle is safe. Summary in vn_buildings_clean.meta.json.

    .venv/Scripts/python.exe buildings/fit_footprints.py [--out data/buildings] [--workers 8] [--bbox W S E N]
"""
import argparse, glob, json, os, shutil, sys, time
from concurrent.futures import ProcessPoolExecutor
import duckdb, numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely, shapely.affinity
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clip_to_roads import HALF, SKIP, ROUND_M, KEEP_SHARE, MIN_M2, GAP_M, UTM, BACK

CELL = 0.25
MAX_SHIFT_M = 3.0
WALL_GAP_M = 0.1
ROAD_MARGIN_DEG = 0.0006          # roads read around a cell: shift + widest carriageway, about 65 m
NB_MARGIN_DEG = 0.0006            # neighbours read from adjacent cells for pass B
BISECT = 8                        # shift precision MAX_SHIFT_M / 2**8, about 1 cm
CONF_OSM, CONF_MICROSOFT = 1.01, 0.69


def largest(g):
    if g.is_empty:
        return g
    return max(shapely.get_parts(g), key=lambda x: x.area) if g.geom_type != 'Polygon' else g


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
    """Pass A for one cell: returns an arrow table (id, src, rank_conf, cx, cy, wkb, a0, shift_m, clip_m2)."""
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
    return pa.table({'id': b['id'].to_numpy()[keep], 'src': b['src'].to_numpy(zero_copy_only=False)[keep],
                     'rank_conf': b['rank_conf'].to_numpy()[keep], 'cx': b['cx'].to_numpy()[keep], 'cy': b['cy'].to_numpy()[keep],
                     'wkb': shapely.to_wkb(out_g[keep]), 'a0': a0[keep], 'shift_m': shift[keep], 'clip_m2': clip[keep]}), \
        {'cell': (i, j), 'in': int(len(geoms)), 'shifted': int((shift > 0).sum()), 'cut': int(((clip > 0) & keep).sum()),
         'dropped_road': int((~keep).sum()), 'shifted_osm': int(((shift > 0) & (b['src'].to_numpy(zero_copy_only=False) == 'osm')).sum())}


def overlap_cell(args):
    """Pass B for one cell: remove overlaps by rank, measure gaps. Returns the final rows of the cell's buildings."""
    work, i, j = args
    own = pq.read_table(f'{work}/a/{i}_{j}.parquet')
    parts = [own]
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            f = f'{work}/a/{i + di}_{j + dj}.parquet'
            if (di or dj) and os.path.exists(f):
                t = pq.read_table(f)
                m = NB_MARGIN_DEG
                cx, cy = t['cx'].to_numpy(), t['cy'].to_numpy()
                sel = (cx >= i * CELL - m) & (cx < (i + 1) * CELL + m) & (cy >= j * CELL - m) & (cy < (j + 1) * CELL + m)
                parts.append(t.filter(pa.array(sel)))
    t = pa.concat_tables(parts)
    n_own = own.num_rows
    g0 = shapely.from_wkb(t['wkb'].to_numpy(zero_copy_only=False))
    area = shapely.area(g0)
    # Rank: higher confidence first (OSM 1.01 > Google 0.7..1 > Microsoft 0.69), then larger, then lower id.
    order = np.lexsort((t['id'].to_numpy(), -area, -t['rank_conf'].to_numpy()))
    rank = np.empty(len(order), np.int64); rank[order] = np.arange(len(order))
    tree = STRtree(g0)
    qi, qj = tree.query(g0[:n_own], predicate='dwithin', distance=WALL_GAP_M)
    nbs = {}
    for x, y in zip(qi, qj):
        if x != y and rank[y] < rank[x]:
            nbs.setdefault(int(x), []).append(int(y))
    final = g0.copy(); cut = np.zeros(n_own); dropped = np.zeros(n_own, bool)
    for k in sorted(range(n_own), key=lambda k: rank[k]):
        hi = nbs.get(k)
        if not hi:
            continue
        other = [final[h] if h < n_own else g0[h] for h in hi]
        other = [o for o in other if o is not None and shapely.distance(final[k], o) < WALL_GAP_M]
        if not other:
            continue
        d = largest(shapely.difference(final[k], shapely.buffer(shapely.union_all(other), WALL_GAP_M)))
        if final[k].area - (0 if d.is_empty else d.area) < 1e-6:
            continue
        cut[k] = final[k].area - (0 if d.is_empty else d.area)
        if d.is_empty or d.area < max(KEEP_SHARE * area[k], MIN_M2):
            final[k] = None; dropped[k] = True
        else:
            final[k] = d
    keep = ~dropped
    fk = final[:n_own][keep]
    # Gaps for compact_layer.py: to the nearest carriageway and to the nearest other building, up to GAP_M.
    lines, bufs = roads_near(work, i, j)
    road_gap = np.full(len(fk), np.nan)
    if len(bufs):
        rt = STRtree(bufs)
        for k, g in enumerate(fk):
            near = rt.query(g, predicate='dwithin', distance=GAP_M)
            if len(near):
                road_gap[k] = float(shapely.distance(bufs[near], g).min())
    others = np.array([f if f is not None else shapely.Polygon() for f in final], object)
    others[n_own:] = g0[n_own:]
    ot = STRtree(others)
    nb_gap = np.full(len(fk), np.nan)
    own_idx = np.flatnonzero(keep)
    for k, g in enumerate(fk):
        near = [x for x in ot.query(g, predicate='dwithin', distance=GAP_M) if x != own_idx[k] and not others[x].is_empty]
        if near:
            nb_gap[k] = float(shapely.distance(others[near], g).min())
    o = own.filter(pa.array(keep))
    res = pa.table({'id': o['id'], 'wkb': shapely.to_wkb(fk), 'area_m2': shapely.area(fk), 'shift_m': o['shift_m'],
                    'clip_m2': o['clip_m2'], 'overlap_m2': cut[keep], 'road_gap_m': road_gap, 'nb_gap_m': nb_gap})
    return res, {'cell': (i, j), 'trimmed': int(((cut > 0) & keep).sum()), 'dropped_dup': int(dropped.sum())}


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
    tabs, sb = run(overlap_cell, jobs, a.workers, 'pass B (remove overlaps)', t0)
    pq.write_table(pa.concat_tables(tabs), f'{work}/fit.parquet')

    keep = f"WHERE m.id IN (SELECT id FROM '{work}/b.parquet')" if a.bbox else ''
    con.execute(f"""COPY (
        SELECT m.* EXCLUDE (geometry, area_m2), f.area_m2, ST_Transform(ST_GeomFromWKB(f.wkb), {BACK}) geometry,
               f.shift_m::FLOAT shift_m, f.clip_m2::FLOAT clip_m2, f.overlap_m2::FLOAT overlap_m2,
               CASE WHEN isnan(f.road_gap_m) THEN NULL ELSE f.road_gap_m END::FLOAT road_gap_m,
               CASE WHEN isnan(f.nb_gap_m) THEN NULL ELSE f.nb_gap_m END::FLOAT nb_gap_m
        FROM '{p('vn_buildings_merged.parquet')}' m JOIN '{work}/fit.parquet' f USING (id) {keep}
        ORDER BY m.id) TO '{p(a.dst)}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)""")
    tot = lambda st, k: sum(s[k] for s in st)
    shifts = np.concatenate([t['shift_m'].to_numpy() for t in tabs]) if tabs else np.zeros(0)
    meta = {'rule': 'shift off carriageways (up to %.1f m), else cut; then remove overlaps by rank OSM > Google by confidence '
                    '> Microsoft, larger first, %.1f m apart; drop when under 50 %% or 6 m2' % (MAX_SHIFT_M, WALL_GAP_M),
            'buildings_in': tot(sa, 'in'), 'shifted': tot(sa, 'shifted'), 'shifted_osm': tot(sa, 'shifted_osm'),
            'shift_m_median': float(np.median(shifts[shifts > 0])) if (shifts > 0).any() else 0,
            'cut_at_road': tot(sa, 'cut'), 'dropped_on_road': tot(sa, 'dropped_road'),
            'trimmed_overlap': tot(sb, 'trimmed'), 'dropped_duplicate': tot(sb, 'dropped_dup'),
            'buildings_out': int(sum(t.num_rows for t in tabs)), 'seconds': round(time.time() - t0)}
    json.dump(meta, open(p(a.dst.replace('.parquet', '.meta.json')), 'w'), indent=1)
    print('wrote', p(a.dst), json.dumps(meta))
    if not a.bbox:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
