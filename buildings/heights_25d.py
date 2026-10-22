"""Per-building height from Google Open Buildings 2.5D Temporal (2023), read at 1 m from the public COGs.

Nothing is stored but the result: each 12.5 km tile is read window by window (about 1 km, height band only,
built-in 2x overview = 1 m pixels) where buildings exist, then dropped. Tested on Bui Xuong Trach:
1 m reads stay within 1 m of the 0.5 m read for 98.6 % of buildings.

Height of a building = median of the height pixels inside its footprint; footprints smaller than a pixel
take the pixel under their centroid. Tiles of neighbouring S2 cells overlap and one of them is nodata
there, so a building keeps the first valid value found.

Each finished tile is saved to data/buildings/_25d_done/<tile>.npz (ids and heights found there). A run that is
interrupted (a Windows Update restart, a power cut) resumes: saved tiles are applied in tile order and skipped.
Delete _25d_done/ after the footprints change.

Input  data/buildings/vn_buildings_clean.parquet (clip_to_roads.py; id = row key from merge_buildings.py, geometry kept there).
Output data/buildings/vn_buildings.parquet, compact and zstd-compressed, one row per building:
  id uint32, src, lon_e6/lat_e6 int32 centroid (1e-6 deg, about 0.1 m), area_m2 float32, conf_pct uint8,
  h25_dm uint16 (2.5D height in decimetres), osm_h_dm uint16 (OSM height, or levels x 3.3 m),
  height_dm uint16 + height_src ('osm' > 'g25'; 2.5D values under 2 m are treated as no building),
  floors uint8 (OSM levels when tagged, else 1 + round((height - 3.9) / 3.3), at least 1),
  osm_id, name, addr.

    .venv/Scripts/python.exe buildings/heights_25d.py [--out data/buildings] [--workers 8] [--year 2023] [--bbox W S E N]
"""
import argparse, hashlib, json, os, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
import duckdb, numpy as np, pyarrow as pa, rasterio, shapely
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.windows import Window

BUCKET = 'open-buildings-temporal-data'
GCS = f'https://storage.googleapis.com/{BUCKET}'
ZONES = (32647, 32648, 32649)          # UTM 47N-49N cover Vietnam
OVR = 2                                # read the 2x overview: 1 m pixels
WIN_M = 1000                           # group buildings into ~1 km windows
VALID = -98.0                          # nodata is -99
UA = {'User-Agent': 'hanoi-traffic-model/buildings'}


def get_json(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120))


def tiles_for(year, cache):
    """All tiles of the year in the UTM zones over Vietnam: (url, epsg, x0, y0, x1, y1)."""
    out = []
    for z in ZONES:
        q = urllib.parse.urlencode({'matchGlob': f'v1/manifests/*_EPSG_{z}_{year}_*', 'fields': 'items(name)'})
        for it in get_json(f'https://storage.googleapis.com/storage/v1/b/{BUCKET}/o?{q}').get('items', []):
            p = os.path.join(cache, os.path.basename(it['name']))
            if not os.path.exists(p):
                json.dump(get_json(f"{GCS}/{it['name']}"), open(p, 'w'))
            m = json.load(open(p))
            pre = m['uriPrefix'].replace(f'gs://{BUCKET}/', '')   # a string prefix, joined without '/'
            for ts in m['tilesets']:
                for s in ts['sources']:
                    a, d = s['affineTransform'], s['dimensions']
                    x0, y1 = a['translateX'], a['translateY']
                    out.append((f"{GCS}/{pre}{s['uris'][0]}", z, x0, y1 + d['height'] * a['scaleY'], x0 + d['width'] * a['scaleX'], y1))
    return out


def group_medians(lab, H):
    """Median of H per positive label, vectorised. Returns (labels, medians)."""
    ok = (lab > 0) & (H > VALID)
    li, hv = lab[ok], H[ok]
    if not len(li):
        return li, hv
    o = np.lexsort((hv, li)); li, hv = li[o], hv[o]
    st = np.r_[0, np.flatnonzero(np.diff(li)) + 1]; cnt = np.diff(np.r_[st, len(li)])
    return li[st], (hv[st + (cnt - 1) // 2] + hv[st + cnt // 2]) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--year', type=int, default=2023)
    ap.add_argument('--bbox', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), help='limit to a lon/lat box (testing)')
    ap.add_argument('--src', default='vn_buildings_clean.parquet', help='footprints (clip_to_roads.py output)')
    ap.add_argument('--dst', default='vn_buildings.parquet')
    a = ap.parse_args()
    t0 = time.time()
    src = os.path.join(a.out, a.src).replace('\\', '/')
    con = duckdb.connect(); con.execute("LOAD spatial; SET enable_progress_bar = false;")
    where = f"WHERE cx BETWEEN {a.bbox[0]} AND {a.bbox[2]} AND cy BETWEEN {a.bbox[1]} AND {a.bbox[3]}" if a.bbox else ''
    con.execute(f"""CREATE TABLE b AS SELECT * FROM (SELECT id, ST_X(c) cx, ST_Y(c) cy, ST_AsWKB(geometry) wkb
                    FROM (SELECT id, geometry, ST_Centroid(geometry) c FROM '{src}')) {where} ORDER BY id""")
    pts = con.execute("SELECT id, cx, cy FROM b").fetchnumpy()
    n_all = con.execute(f"SELECT max(id) + 1 FROM '{src}'").fetchone()[0]
    print('buildings', len(pts['id']), 'of', n_all, round(time.time() - t0), 's', flush=True)
    h = np.full(n_all, np.nan, np.float32)          # indexed by id
    cache = os.path.join(a.out, '_25d_manifests'); os.makedirs(cache, exist_ok=True)
    # The manifests list ~24k tiles over whole UTM zones; an occupancy grid of 0.05 degree cells picks the ones
    # with buildings without scanning every building once per tile.
    G = 0.05
    gx0, gy0 = np.floor(pts['cx'].min() / G) * G, np.floor(pts['cy'].min() / G) * G
    occ = np.zeros((int((pts['cy'].max() - gy0) / G) + 2, int((pts['cx'].max() - gx0) / G) + 2), bool)
    occ[((pts['cy'] - gy0) / G).astype(int), ((pts['cx'] - gx0) / G).astype(int)] = True
    work = []
    for url, z, x0, y0, x1, y1 in tiles_for(a.year, cache):
        lo, la = Transformer.from_crs(z, 4326, always_xy=True).transform([x0, x1, x0, x1], [y0, y0, y1, y1])
        i0, i1 = int((min(la) - gy0) / G), int((max(la) - gy0) / G) + 1
        j0, j1 = int((min(lo) - gx0) / G), int((max(lo) - gx0) / G) + 1
        if i1 > 0 and j1 > 0 and occ[max(i0, 0):i1, max(j0, 0):j1].any():
            work.append((url, z, x0, y0, x1, y1, min(lo), min(la), max(lo), max(la)))
    done_dir = os.path.join(a.out, '_25d_done' if not a.bbox else '_25d_done_bbox'); os.makedirs(done_dir, exist_ok=True)
    ckpt = lambda url: os.path.join(done_dir, hashlib.sha1(url.encode()).hexdigest()[:16] + '.npz')
    resumed = 0
    for tile in work:
        if os.path.exists(ckpt(tile[0])):
            d = np.load(ckpt(tile[0])); fi, fh = d['i'], d['h']
            new = np.isnan(h[fi]); h[fi[new]] = fh[new]; resumed += 1
    work_left = [t for t in work if not os.path.exists(ckpt(t[0]))]
    print('tiles with buildings', len(work), '| resumed from checkpoints', resumed, '| to read', len(work_left), flush=True)
    lock = threading.Lock(); prog = {'tiles': resumed, 'windows': 0, 'px': 0}

    def run(tile):
        url, z, x0, y0, x1, y1, lo0, la0, lo1, la1 = tile
        q = con.cursor().execute(f"SELECT id, cx, cy, wkb FROM b WHERE cx BETWEEN {lo0} AND {lo1} AND cy BETWEEN {la0} AND {la1}").fetchnumpy()
        bid = q['id'].astype(np.int64)
        with lock:
            todo = np.isnan(h[bid])
        if not todo.any():
            return
        tr = Transformer.from_crs(4326, z, always_xy=True)
        ex, ey = tr.transform(q['cx'][todo], q['cy'][todo])
        ins = (ex >= x0) & (ex < x1) & (ey >= y0) & (ey < y1)
        if not ins.any():
            return
        bid, ex, ey = bid[todo][ins], ex[ins], ey[ins]
        geoms = shapely.from_wkb([bytes(w) for w in q['wkb'][todo][ins]])
        geoms = shapely.transform(geoms, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
        gkey = ((ex - x0) // WIN_M).astype(np.int64) * 100 + ((y1 - ey) // WIN_M).astype(np.int64)
        found_i, found_h, nwin, npx = [], [], 0, 0
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR', GDAL_HTTP_MAX_RETRY='5', GDAL_HTTP_RETRY_DELAY='2'):
            with rasterio.open('/vsicurl/' + url) as r:
                inv = ~r.transform
                for key in np.unique(gkey):
                    m = np.flatnonzero(gkey == key)
                    gb = shapely.bounds(geoms[m])
                    c0, r0 = inv * (gb[:, 0].min() - 2, gb[:, 3].max() + 2)
                    c1, r1 = inv * (gb[:, 2].max() + 2, gb[:, 1].min() - 2)
                    c0, r0 = max(int(c0) // OVR * OVR, 0), max(int(r0) // OVR * OVR, 0)
                    c1, r1 = min(-(-int(c1) // OVR) * OVR, r.width), min(-(-int(r1) // OVR) * OVR, r.height)
                    if c1 <= c0 or r1 <= r0:
                        continue
                    win = Window(c0, r0, c1 - c0, r1 - r0)
                    oh, ow = (r1 - r0) // OVR, (c1 - c0) // OVR
                    H = r.read(2, window=win, out_shape=(oh, ow))
                    nwin += 1; npx += H.size
                    if not (H > VALID).any():
                        continue
                    T = r.window_transform(win) * rasterio.Affine.scale(OVR)
                    lab = rasterize(zip(geoms[m], range(1, len(m) + 1)), out_shape=H.shape, transform=T, fill=0, dtype='int32')
                    lbl, med = group_medians(lab, H)
                    have = np.zeros(len(m), bool); have[lbl - 1] = True
                    found_i.append(bid[m[lbl - 1]]); found_h.append(med)
                    # Footprints smaller than a pixel: value under the centroid.
                    rest = np.flatnonzero(~have)
                    if len(rest):
                        cc, rr = ~T * (ex[m[rest]], ey[m[rest]])
                        cc, rr = np.floor(cc).astype(int), np.floor(rr).astype(int)
                        okp = (rr >= 0) & (rr < oh) & (cc >= 0) & (cc < ow)
                        rest, cc, rr = rest[okp], cc[okp], rr[okp]
                        v = H[rr, cc]; good = v > VALID
                        found_i.append(bid[m[rest[good]]]); found_h.append(v[good])
        fi = np.concatenate(found_i) if found_i else np.zeros(0, np.int64)
        fh = np.concatenate(found_h).astype(np.float32) if found_h else np.zeros(0, np.float32)
        tmp = ckpt(url) + '.tmp.npz'
        np.savez(tmp, i=fi, h=fh); os.replace(tmp, ckpt(url))
        with lock:
            new = np.isnan(h[fi]); h[fi[new]] = fh[new]
            prog['tiles'] += 1; prog['windows'] += nwin; prog['px'] += npx
            if prog['tiles'] % 25 == 0 or prog['tiles'] == len(work):
                print(f"tiles {prog['tiles']}/{len(work)} | with height {np.count_nonzero(~np.isnan(h))} | windows {prog['windows']} "
                      f"| decoded {prog['px'] * 4 / 1e9:.1f} GB | {time.time() - t0:.0f}s", flush=True)

    with ThreadPoolExecutor(a.workers) as pool:
        for f in [pool.submit(run, t) for t in work_left]:
            f.result()

    con.register('hh', pa.table({'id': np.arange(n_all, dtype=np.uint32), 'h25': h}))
    dst = os.path.join(a.out, a.dst).replace('\\', '/')
    keep = f"WHERE m.id IN (SELECT id FROM b)" if a.bbox else ''
    # least()/greatest() skip NULLs in DuckDB, so a missing height must be guarded explicitly.
    dm = lambda x: f"CASE WHEN {x} IS NULL THEN NULL ELSE CAST(round(least({x}, 6553) * 10) AS USMALLINT) END"
    con.execute(f"""COPY (
        WITH t AS (
            SELECT m.id, m.src, ST_Centroid(m.geometry) c, m.area_m2, m.conf, m.osm_id, m.name,
                   nullif(trim(concat_ws(' ', m.addr_housenumber, m.addr_street)), '') addr,
                   CASE WHEN isnan(hh.h25) THEN NULL ELSE hh.h25 END h25,
                   coalesce(try_cast(m.height AS DOUBLE), try_cast(m.building_levels AS DOUBLE) * 3.3) osm_h,
                   try_cast(m.building_levels AS DOUBLE) osm_levels
            FROM '{src}' m JOIN hh USING (id) {keep})
        , u AS (SELECT *, coalesce(osm_h, CASE WHEN h25 >= 2 THEN h25 END) hm FROM t)
        SELECT id, src, CAST(round(ST_X(c) * 1e6) AS INTEGER) lon_e6, CAST(round(ST_Y(c) * 1e6) AS INTEGER) lat_e6,
               area_m2::FLOAT area_m2, CAST(round(conf * 100) AS UTINYINT) conf_pct,
               {dm('h25')} h25_dm, {dm('osm_h')} osm_h_dm, {dm('hm')} height_dm,
               CASE WHEN osm_h IS NOT NULL THEN 'osm' WHEN hm IS NOT NULL THEN 'g25' END height_src,
               CASE WHEN osm_levels IS NOT NULL THEN CAST(least(osm_levels, 255) AS UTINYINT)
                    WHEN hm IS NOT NULL THEN CAST(least(greatest(1, 1 + round((hm - 3.9) / 3.3)), 255) AS UTINYINT) END floors,
               osm_id, name, addr
        FROM u ORDER BY id) TO '{dst}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 9, ROW_GROUP_SIZE 500000)""")
    s = con.execute(f"""SELECT count(*), count(h25_dm), count(height_dm), median(height_dm) / 10, quantile_cont(height_dm, 0.9) / 10
                        FROM '{dst}'""").fetchone()
    print(f'wrote {dst} {os.path.getsize(dst) / 1e6:.0f} MB | rows {s[0]} | 2.5D {s[1]} | height {s[2]} | median {s[3]} m | p90 {s[4]} m | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
