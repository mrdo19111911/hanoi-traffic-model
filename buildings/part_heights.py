"""Heights from OSM building:part: complex towers are mapped as `building:part` ways and relations that carry the
height, while the outline (`building=*`) often has none (Keangnam Landmark 72: outline w1327061631 untagged, the
tower parts 336 m). extract_osm.py reads only `building=*`, so these heights were lost.

    .venv/Scripts/python.exe buildings/part_heights.py extract [--pbf osrm/data/vietnam-latest.osm.pbf]
        -> data/buildings/osm_parts.parquet: every closed way / multipolygon tagged building:part (not 'no'),
           geometry plus building:part, height, min_height, building:levels, building:min_level, roof:height, name.
    .venv/Scripts/python.exe buildings/part_heights.py apply [--dst-dir buildings/by_area] [--workers 5]
        -> applies the rule below to the published files in place: rewrites only the files that change (same schema,
           zstd 19, row groups of 500k, row order and every other column unchanged), then the `bytes` of those files
           in by_area/index.json and in the build state (data/buildings/_areas/done/<file>.json).

Rule (apply_table; build_areas.py and compact_layer.py call it on every rebuild, so a rebuild gives the same result):
- Part height: `height` (plain number, optionally 'm'; feet converted). When it is missing or unusable:
  building:levels x 3.3 m, plus roof:height when that is a plain number of 0-100 m. Values <= 0 or above 900 m are
  ignored. Part floors: building:levels when tagged, else 1 + round((height - 3.9) / 3.3), at least 1 (as heights_25d).
- A part belongs to a building when its representative point lies inside the building footprint, or when at least
  50 % of the part's area overlaps the footprint. The footprint is the published one (compact_layer.decode).
- Parts height of a building = maximum over its parts; floors = maximum of its parts' floors.
- It replaces the building's height when the building has none or the parts height is greater; height_src becomes
  'osm_part' (height_src is a dictionary of strings, so a new value needs no schema change). Nothing else changes.
"""
import argparse, gzip, json, os, re, sys, time
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PART_TAGS = ('building:part', 'height', 'min_height', 'building:levels', 'building:min_level', 'roof:height', 'name')
LEVEL_M = 3.3
MAX_H = 900.0                      # metres; taller values are tagging errors (the tallest building in Vietnam is 461 m)
MAX_ROOF_H = 100.0
MIN_OVERLAP = 0.5                  # share of the part's area that must lie on the footprint (or its representative point inside)
SRC = 'osm_part'
PARTS = 'data/buildings/osm_parts.parquet'
_NUM = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|meters?|metres?|ft|feet|')?\s*$", re.I)


def parse_m(v):
    """Tag value -> metres, or None for junk ('336', '336 m', '336m', '12,5', '100 ft')."""
    if v is None:
        return None
    m = _NUM.match(str(v))
    if not m:
        return None
    x = float(m.group(1).replace(',', '.'))
    return x * 0.3048 if (m.group(2) or '').lower() in ('ft', 'feet', "'") else x


def parse_levels(v):
    m = re.match(r'^\s*([0-9]+(?:[.,][0-9]+)?)\s*$', str(v)) if v is not None else None
    x = float(m.group(1).replace(',', '.')) if m else None
    return x if x and x > 0 else None


def part_height(height, levels, roof_height):
    """(height in m or None, floors or None) of one part from its tags."""
    h = parse_m(height)
    lv = parse_levels(levels)
    if h is None or not 0 < h <= MAX_H:
        h = None
        if lv:
            r = parse_m(roof_height)
            h = lv * LEVEL_M + (r if r is not None and 0 <= r <= MAX_ROOF_H else 0.0)
            if not 0 < h <= MAX_H:
                h = None
    if h is None:
        return None, None
    fl = int(np.floor(lv + 0.5)) if lv else max(1, 1 + int(np.floor((h - 3.9) / LEVEL_M + 0.5)))
    return h, min(max(fl, 1), 255)


# ---------------------------------------------------------------------------------------------------- extract

def extract(pbf, out):
    import duckdb, osmium

    class Handler(osmium.SimpleHandler):
        def __init__(self, f):
            super().__init__()
            self.wkb, self.f, self.n = osmium.geom.WKBFactory(), f, 0

        def area(self, a):
            t = a.tags
            if 'building:part' not in t or t.get('building:part') == 'no':
                return
            try:
                g = self.wkb.create_multipolygon(a)
            except (osmium.InvalidLocationError, RuntimeError):
                return
            oid = ('w' if a.from_way() else 'r') + str(a.orig_id())
            self.f.write(json.dumps({'osm_id': oid, **{k.replace(':', '_'): t.get(k) for k in PART_TAGS}, 'g': g}, ensure_ascii=False) + '\n')
            self.n += 1

    t0 = time.time()
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    tmp = out + '.ndjson.gz'
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        h = Handler(f)
        h.apply_file(pbf, locations=True, idx='flex_mem')      # one pass; osmium assembles the multipolygons
    print('building:part areas', h.n, f'{time.time() - t0:.0f}s', flush=True)
    con = duckdb.connect(); con.execute('INSTALL spatial; LOAD spatial;')
    cols = ', '.join(f"{k.replace(':', '_')}: 'VARCHAR'" for k in ('osm_id', *PART_TAGS)) + ", g: 'VARCHAR'"
    src, dst = tmp.replace('\\', '/'), out.replace('\\', '/')
    con.execute(f"""COPY (SELECT * EXCLUDE (g), ST_GeomFromHEXWKB(g) AS geometry FROM read_json('{src}', format='newline_delimited',
                    columns={{{cols}}})) TO '{dst}' (FORMAT parquet, COMPRESSION zstd)""")
    os.remove(tmp)
    print('->', dst, round(os.path.getsize(dst) / 1e6, 1), 'MB', f'{time.time() - t0:.0f}s', flush=True)


# ---------------------------------------------------------------------------------------------------- rule

def load_parts(path=PARTS, bbox=None):
    """Usable parts (height known) as dict(geom, h, floors, osm_id, name), optionally only those meeting a lon/lat box.
    Raises FileNotFoundError when the extract is missing: a rebuild without it would silently lose these heights."""
    if not os.path.exists(path):
        raise FileNotFoundError(f'{path} missing: run buildings/part_heights.py extract')
    t = pq.read_table(path, columns=['osm_id', 'name', 'height', 'building_levels', 'roof_height', 'geometry']).to_pydict()
    hf = [part_height(a, b, c) for a, b, c in zip(t['height'], t['building_levels'], t['roof_height'])]
    ok = np.array([h is not None for h, _ in hf], bool)
    geom = shapely.from_wkb([g for g, k in zip(t['geometry'], ok) if k])
    p = {'geom': geom, 'h': np.array([h for h, _ in hf if h is not None], float),
         'floors': np.array([f for h, f in hf if h is not None], np.int64),
         'osm_id': [x for x, k in zip(t['osm_id'], ok) if k], 'name': [x for x, k in zip(t['name'], ok) if k]}
    if bbox is not None:
        b = shapely.bounds(geom)
        s = (b[:, 0] <= bbox[2]) & (b[:, 2] >= bbox[0]) & (b[:, 1] <= bbox[3]) & (b[:, 3] >= bbox[1])
        p = subset(p, s)
    p['geom'] = shapely.make_valid(p['geom'])
    return p


def subset(p, s):
    idx = np.flatnonzero(s)
    return {k: (v[idx] if isinstance(v, np.ndarray) else [v[i] for i in idx]) for k, v in p.items()}


def footprint_bounds(tbl):
    """(x0, y0, x1, y1) in degrees of every decoded footprint, vectorised (rings: cumulative steps; rects: centre
    plus half diagonal, a slight overestimate)."""
    from compact_layer import metres_per_deg
    lon, lat = tbl['lon_e6'].to_numpy().astype(np.int64), tbl['lat_e6'].to_numpy().astype(np.int64)
    x0, x1, y0, y1 = lon.copy(), lon.copy(), lat.copy(), lat.copy()
    ring = tbl['ring'].combine_chunks()
    has = ~np.asarray(ring.is_null().to_numpy(zero_copy_only=False))
    ri = np.flatnonzero(has)
    if len(ri):
        off = np.frombuffer(ring.buffers()[1], np.int32, len(ring) + 1, ring.offset * 4)
        data = np.frombuffer(ring.buffers()[2], np.uint8)
        st, en = off[ri], off[ri + 1]
        nb = (en - st) // 4
        keep = nb > 0
        ri, st, nb = ri[keep], st[keep], nb[keep]
        sel = np.repeat(st, nb * 4) + (np.arange(int(nb.sum()) * 4) - np.repeat(np.cumsum(nb * 4) - nb * 4, nb * 4))
        d = data[sel].view('<i2').reshape(-1, 2).astype(np.int64)
        seg = np.r_[0, np.cumsum(nb)[:-1]]
        c = np.cumsum(d, axis=0)
        c -= np.repeat(np.vstack([[0, 0], c[seg[1:] - 1]]) if len(seg) > 1 else np.zeros((1, 2), np.int64), nb, axis=0)
        x0[ri] += np.minimum(np.minimum.reduceat(c[:, 0], seg), 0); x1[ri] += np.maximum(np.maximum.reduceat(c[:, 0], seg), 0)
        y0[ri] += np.minimum(np.minimum.reduceat(c[:, 1], seg), 0); y1[ri] += np.maximum(np.maximum.reduceat(c[:, 1], seg), 0)
    rr = np.flatnonzero(~has)
    if len(rr):
        half = np.hypot(tbl['length_dm'].to_numpy()[rr], tbl['width_dm'].to_numpy()[rr]) / 20 * 1.01 + 0.5   # metres
        dx = np.ceil(half / (111320 * np.cos(np.radians(lat[rr] / 1e6))) * 1e6)     # metres_per_deg, per row
        dy = np.ceil(half / metres_per_deg(0)[1] * 1e6)
        x0[rr] -= dx.astype(np.int64); x1[rr] += dx.astype(np.int64); y0[rr] -= dy.astype(np.int64); y1[rr] += dy.astype(np.int64)
    return x0 / 1e6, y0 / 1e6, x1 / 1e6, y1 / 1e6


def building_part_heights(tbl, parts):
    """Rows of `tbl` (compact SCHEMA) that parts cover: (row index, parts height in m, floors, index of the tallest
    part), by the rule in the module docstring."""
    from compact_layer import decode
    empty = (np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int64), np.zeros(0, np.int64))
    if not tbl.num_rows or not len(parts['h']):
        return empty
    x0, y0, x1, y1 = footprint_bounds(tbl)
    pb = shapely.total_bounds(parts['geom'])
    cand = np.flatnonzero((x0 <= pb[2]) & (x1 >= pb[0]) & (y0 <= pb[3]) & (y1 >= pb[1]))
    if not len(cand):
        return empty
    tree = shapely.STRtree(parts['geom'])
    bi, pi = tree.query(shapely.box(x0[cand], y0[cand], x1[cand], y1[cand]))
    if not len(bi):
        return empty
    rows = cand[bi]
    u, inv = np.unique(rows, return_inverse=True)
    cols = ['lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring']
    sub = tbl.take(pa.array(u)).select(cols).to_pylist()
    fp = np.array([decode(r) for r in sub], object)
    fp = np.where(shapely.is_valid(fp), fp, shapely.make_valid(fp))
    g, pg = fp[inv], parts['geom'][pi]
    inside = shapely.contains(g, shapely.point_on_surface(pg))
    pa_ = shapely.area(pg)
    over = shapely.area(shapely.intersection(g, pg)) >= MIN_OVERLAP * pa_
    hit = (inside | over) & (pa_ > 0)
    if not hit.any():
        return empty
    rows, pi = rows[hit], pi[hit]
    h, fl = parts['h'][pi], parts['floors'][pi]
    o = np.lexsort((-h, rows))                         # per row, tallest part first
    rows, pi, h, fl = rows[o], pi[o], h[o], fl[o]
    st = np.r_[0, np.flatnonzero(np.diff(rows)) + 1]
    return rows[st], h[st], np.maximum.reduceat(fl, st), pi[st]


def apply_table(tbl, parts):
    """Compact table -> (table, changed row indices, details). The table object is returned unchanged when no
    building changes, so files without parts stay byte-identical."""
    rows, h, fl, top = building_part_heights(tbl, parts)
    if not len(rows):
        return tbl, rows, {}
    dm = np.minimum(np.floor(h * 10 + 0.5), 65535).astype(np.int64)
    cur = tbl['height_dm'].combine_chunks()
    cur_null = np.asarray(cur.is_null().to_numpy(zero_copy_only=False))[rows]
    cur_v = cur.fill_null(0).to_numpy().astype(np.int64)[rows]
    chg = cur_null | (dm > cur_v)
    if not chg.any():
        return tbl, rows[:0], {}
    rows, dm, fl, top, old = rows[chg], dm[chg], fl[chg], top[chg], np.where(cur_null[chg], -1, cur_v[chg])
    tbl = tbl.combine_chunks()
    hd = tbl['height_dm'].to_pylist(); hs = tbl['height_src'].to_pylist(); fo = tbl['floors'].to_pylist()
    for r, d, f in zip(rows.tolist(), dm.tolist(), fl.tolist()):
        hd[r], hs[r], fo[r] = d, SRC, f
    sch = tbl.schema
    for name, v in (('height_dm', pa.array(hd, pa.uint16())), ('height_src', pa.array(hs, pa.string()).dictionary_encode()),
                    ('floors', pa.array(fo, pa.uint8()))):
        i = sch.get_field_index(name)
        tbl = tbl.set_column(i, sch.field(i), v.cast(sch.field(i).type))
    return tbl, rows, {'old_dm': old, 'new_dm': dm, 'floors': fl,
                       'part_osm_id': [parts['osm_id'][i] for i in top], 'part_name': [parts['name'][i] for i in top]}


def apply_in_build(tbl, parts_path=PARTS):
    """Hook for build_areas.py / compact_layer.py: the rule on a freshly encoded table."""
    lon, lat = tbl['lon_e6'].to_numpy() / 1e6, tbl['lat_e6'].to_numpy() / 1e6
    m = 0.05                                           # degrees; footprints reach at most this far from their anchor
    parts = load_parts(parts_path, [lon.min() - m, lat.min() - m, lon.max() + m, lat.max() + m])
    tbl, rows, _ = apply_table(tbl, parts)
    return tbl, len(rows)


# ---------------------------------------------------------------------------------------------------- apply in place

def _apply_file(args):
    path, bbox, parts_path = args
    m = 0.05
    parts = load_parts(parts_path, [bbox[0] - m, bbox[1] - m, bbox[2] + m, bbox[3] + m])
    if not len(parts['h']):
        return os.path.basename(path), 0, None, []
    t = pq.read_table(path)
    t2, rows, det = apply_table(t, parts)
    if not len(rows):
        return os.path.basename(path), 0, None, []
    ids = t2['id'].to_numpy()[rows]
    lon, lat = t2['lon_e6'].to_numpy()[rows], t2['lat_e6'].to_numpy()[rows]
    tmp = path + '.tmp'
    pq.write_table(t2, tmp, compression='zstd', compression_level=19, row_group_size=500_000)
    os.replace(tmp, path)
    out = [{'file': os.path.basename(path), 'id': int(i), 'lon': float(x) / 1e6, 'lat': float(y) / 1e6, 'old_dm': int(o), 'new_dm': int(d),
            'floors': int(f), 'part': p, 'name': n} for i, x, y, o, d, f, p, n in
           zip(ids, lon, lat, det['old_dm'], det['new_dm'], det['floors'], det['part_osm_id'], det['part_name'])]
    return os.path.basename(path), len(rows), os.path.getsize(path), out


def apply_published(dst_dir, workers, parts_path, state_dir, log):
    t0 = time.time()
    idx_path = os.path.join(dst_dir, 'index.json')
    idx = json.load(open(idx_path, encoding='utf-8'))
    pb = shapely.total_bounds(load_parts(parts_path)['geom'])
    jobs = [(os.path.join(dst_dir, e['file']), e['bbox'], parts_path) for e in idx['files']
            if e['bbox'][0] - 0.05 <= pb[2] and e['bbox'][2] + 0.05 >= pb[0] and e['bbox'][1] - 0.05 <= pb[3] and e['bbox'][3] + 0.05 >= pb[1]]
    print('files', len(idx['files']), '| near parts', len(jobs), flush=True)
    changed, rows = {}, []
    with ProcessPoolExecutor(workers) as ex:
        for f, n, size, out in ex.map(_apply_file, jobs):
            if n:
                changed[f] = size; rows += out
                print(f'{f}: {n} buildings | {size} bytes | {time.time() - t0:.0f}s', flush=True)
    for e in idx['files']:
        if e['file'] in changed:
            e['bytes'] = changed[e['file']]
    json.dump(idx, open(idx_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    for f, size in changed.items():                 # build state, which build_areas.py writes index.json from
        p = os.path.join(state_dir, f + '.json')
        if os.path.exists(p):
            e = json.load(open(p, encoding='utf-8')); e['bytes'] = size
            json.dump(e, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if log:
        json.dump(rows, open(log, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f'changed {len(rows)} buildings in {len(changed)} files | {time.time() - t0:.0f}s')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=('extract', 'apply'))
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    ap.add_argument('--parts', default=PARTS)
    ap.add_argument('--dst-dir', default='buildings/by_area')
    ap.add_argument('--state-dir', default='data/buildings/_areas/done')
    ap.add_argument('--workers', type=int, default=5, help='processes for apply (memory bound; default 5)')
    ap.add_argument('--log', help='apply: write every changed building (file, id, old/new height, part) to this JSON')
    a = ap.parse_args()
    if a.cmd == 'extract':
        extract(a.pbf, a.parts)
    else:
        apply_published(a.dst_dir, a.workers, a.parts, a.state_dir, a.log)


if __name__ == '__main__':
    main()
