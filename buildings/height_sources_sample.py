"""Measurement only: sample independent height sources at the centroid of every Hà Nội building of the published layer.

Per building of buildings/by_area/ha_noi_*.parquet (centroid = lon_e6/lat_e6) it records the published height, its
source, the raw Google 2.5D height, and one value per independent source:
  globfp   3D-GloBFP footprint polygon containing the centroid (Height, m)
  utg      UT-GLOBUS footprint polygon containing the centroid (height, m; layers Hanoi_1 and Hanoi_2, UTM 48N)
  ms       Microsoft footprint polygon containing the centroid (height, m, -1 = none -> NaN)
  gba      Global Building Atlas 3 m raster, median of the valid cells in a 5x5 window (core of Hà Nội only)
  ghsl     GHS-BUILT-H ANBH 100 m raster (average net building height, m)
  wsf3d    WSF3D 90 m raster
  ovt      Overture footprint (height or num_floors * 3.3) when data/overture/vn_building.parquet exists

Output data/buildings/_ext_heights/eval/hn_sources.parquet. Nothing is written to buildings/by_area.

    python buildings/height_sources_sample.py [--root .] [--threads 16] [--skip ovt,ms]
"""
import argparse, glob, gzip, os, shutil, time

import duckdb, numpy as np, pyarrow as pa, pyarrow.parquet as pq, rasterio
from pyproj import Transformer

EXT = 'data/buildings/_ext_heights'
GF_SHP = f'{EXT}/globfp/x2155/2155_105.0_20.0_106.25_21.25_VM.shp'
GBA = f'{EXT}/gba_height/105.8_21.2_106.0_21.0_sr_ss.tif'
GHSL = f'{EXT}/ghsl_built_h/GHS_BUILT_H_ANBH_E2018_GLOBE_R2023A_54009_100_V1_0_R7_C29.tif'
WSF = f'{EXT}/wsf3d/wsf3d_height_hanoi_clip.tif'
MS = f'{EXT}/ms_hanoi/132210112.csv.gz'


def load_points(root, pattern='ha_noi_*'):
    cols = ['id', 'src', 'lon_e6', 'lat_e6', 'height_dm', 'height_src', 'floors']
    out = []
    for f in sorted(glob.glob(f'{root}/buildings/by_area/{pattern}.parquet')):
        name = os.path.basename(f)
        t = pq.read_table(f, columns=cols).to_pydict()
        r = pq.read_table(f'{root}/data/buildings/_calib/raw_by_area/{name}', columns=['id', 'height_dm', 'height_src'])
        raw = dict(zip(r['id'].to_pylist(), [h / 10 if s == 'g25' else np.nan
                                                for h, s in zip(r['height_dm'].to_pylist(), r['height_src'].to_pylist())]))
        n = len(t['id'])
        out.append(dict(id=np.array(t['id'], dtype=np.uint32), file=np.full(n, name),
                        fsrc=np.array(t['src']), lon=np.array(t['lon_e6']) / 1e6, lat=np.array(t['lat_e6']) / 1e6,
                        pub_h=np.array(t['height_dm'], dtype=float) / 10, pub_src=np.array(t['height_src']),
                        floors=np.array(t['floors'], dtype=float), raw_g25=np.array([raw.get(i, np.nan) for i in t['id']])))
    return {k: np.concatenate([o[k] for o in out]) for k in out[0]}


def sample_raster(path, lon, lat, win=0, scale=1.0, bad=()):
    """Median of the valid cells in a (2*win+1)^2 window around each point; NaN where none is valid."""
    with rasterio.open(path) as ds:
        arr = ds.read(1).astype(np.float32)
        nod = ds.nodata
        x, y = Transformer.from_crs('EPSG:4326', ds.crs, always_xy=True).transform(lon, lat)
        rows, cols = rasterio.transform.rowcol(ds.transform, x, y)
    rows, cols = np.asarray(rows), np.asarray(cols)
    inside = (rows >= win) & (rows < arr.shape[0] - win) & (cols >= win) & (cols < arr.shape[1] - win)
    if nod is not None:
        arr[arr == nod] = np.nan
    for b in bad:
        arr[arr == b] = np.nan
    stack = []
    for dr in range(-win, win + 1):
        for dc in range(-win, win + 1):
            v = np.full(len(lon), np.nan, np.float32)
            v[inside] = arr[rows[inside] + dr, cols[inside] + dc]
            stack.append(v)
    with np.errstate(all='ignore'):
        res = np.nanmedian(np.stack(stack), axis=0) if len(stack) > 1 else stack[0]
    return (res * scale).astype(float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.')
    ap.add_argument('--threads', type=int, default=16)
    ap.add_argument('--skip', default='')
    ap.add_argument('--files', default='ha_noi_*', help='by_area glob without .parquet (trial runs)')
    ap.add_argument('--out', default='hn_sources.parquet')
    a = ap.parse_args()
    skip = set(filter(None, a.skip.split(',')))
    os.chdir(a.root)
    out_dir = f'{EXT}/eval'
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    log = lambda *m: print(f'[{time.time() - t0:6.0f}s]', *m, flush=True)

    P = load_points('.', a.files)
    n = len(P['id'])
    log('points', n)
    con = duckdb.connect()
    con.execute(f"INSTALL spatial; LOAD spatial; SET threads={a.threads}")
    con.register('pts_np', pa.table({'i': np.arange(n, dtype=np.int64), 'lon': P['lon'], 'lat': P['lat']}))
    con.execute("CREATE TABLE pts2 AS SELECT i, lon, lat, ST_Point(lon, lat) g FROM pts_np")

    def join(name, src_sql, col, utm=False):
        """src_sql: SELECT <col>, geom FROM ... ; first polygon containing each point."""
        con.execute(f"CREATE OR REPLACE TABLE poly AS {src_sql}")
        g = "ST_Transform(g, 'EPSG:4326', 'EPSG:32648', always_xy := true)" if utm else 'g'
        con.execute(f"CREATE OR REPLACE TABLE pp AS SELECT i, {g} g FROM pts2")
        r = con.execute(f"""SELECT p.i, any_value(q.{col}) v FROM pp p JOIN poly q ON ST_Contains(q.geom, p.g) GROUP BY p.i""").fetchall()
        v = np.full(n, np.nan)
        if r:
            ii, vv = zip(*r)
            v[np.array(ii)] = np.array(vv, dtype=float)
        log(name, 'covered', int(np.isfinite(v).sum()))
        return v

    R = dict(P)
    if 'globfp' not in skip:
        R['globfp'] = join('globfp', f"SELECT Height AS v, geom FROM st_read('{GF_SHP}')", 'v')
    if 'utg' not in skip:
        v = np.full(n, np.nan)
        for lay in ('Hanoi_1', 'Hanoi_2'):
            w = join(f'utg {lay}', f"SELECT height AS v, geom FROM st_read('{EXT}/utglobus/{lay}.gpkg')", 'v', utm=True)
            v = np.where(np.isfinite(v), v, w)
        R['utg'] = v
    if 'ms' not in skip:
        gj = f'{out_dir}/ms_hanoi.geojsonl'
        if not os.path.exists(gj):
            with gzip.open(MS, 'rb') as f, open(gj, 'wb') as o:
                shutil.copyfileobj(f, o)
        v = join('ms', f"SELECT height AS v, geom FROM st_read('{gj}') WHERE height > 0", 'v')
        R['ms'] = v
        con.execute(f"CREATE OR REPLACE TABLE msall AS SELECT count(*) c, count(*) FILTER (height>0) h FROM st_read('{gj}')")
        log('ms rows / with height', con.execute('SELECT * FROM msall').fetchall())
    if 'gba' not in skip:
        R['gba'] = sample_raster(GBA, P['lon'], P['lat'], win=2, bad=(0.0,))
        log('gba covered', int(np.isfinite(R['gba']).sum()))
    if 'ghsl' not in skip:
        R['ghsl'] = sample_raster(GHSL, P['lon'], P['lat'], bad=(0.0,))
        log('ghsl covered', int(np.isfinite(R['ghsl']).sum()))
    if 'wsf3d' not in skip:
        R['wsf3d'] = sample_raster(WSF, P['lon'], P['lat'], scale=0.1, bad=(0.0,))
        log('wsf3d covered', int(np.isfinite(R['wsf3d']).sum()))
    if 'ovt' not in skip and os.path.exists('data/overture/vn_building.parquet'):
        R['ovt'] = join('overture', "SELECT coalesce(height, num_floors * 3.3) AS v, ST_GeomFromWKB(wkb) geom "
                                    "FROM 'data/overture/vn_building.parquet' WHERE province = 'VN-HN' "
                                    "AND (height IS NOT NULL OR num_floors IS NOT NULL)", 'v')
    pq.write_table(pa.table(R), f'{out_dir}/{a.out}', compression='zstd')
    log('written', f'{out_dir}/{a.out}')


if __name__ == '__main__':
    main()
