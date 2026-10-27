"""Measurement only: building heights from Copernicus GLO-30 (a surface model: roofs, trees, ground) minus nearby open ground.

GLO-30 is a DSM (TanDEM-X radar, acquired 2011-2015, 30 m posting). Where there are buildings it reads (part of) the
roofs, so height ~ DSM - ground, with ground taken from nearby open land:
  open pixel  = a pixel whose published footprints cover less than OPEN_COVER of it (footprint area summed at the
                centroid pixel, Ha Noi files of buildings/by_area);
  ground      = mean DSM of the open pixels within a square window of +-W pixels ('open' method), or the P-th percentile
                of every pixel in that window ('pct' method, no footprint information);
  nDSM        = DSM - ground at the building's centroid pixel.
Scored against the OSM-tagged heights of data/buildings/_calib/truth.parquet (Ha Noi rows inside the bbox), next to the
raw Google 2.5D height of the same rows. Nothing here writes to buildings/by_area.

    .venv/Scripts/python.exe buildings/dem_heights.py [--bbox 105.72 20.93 105.95 21.13] [--json OUT]
"""
import argparse, glob, json, os

import sys

import numpy as np, pyarrow.parquet as pq, rasterio, shapely
from rasterio.merge import merge
from scipy import ndimage
from scipy.stats import spearmanr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calibrate_heights import ring_geoms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEM = os.path.join(REPO, 'data', 'dem', 'glo30')
TRUTH = os.path.join(REPO, 'data', 'buildings', '_calib', 'truth.parquet')
OPEN_COVER = 0.05


def load_dem(bbox):
    files = []
    for la in range(int(np.floor(bbox[1])), int(np.floor(bbox[3])) + 1):
        for lo in range(int(np.floor(bbox[0])), int(np.floor(bbox[2])) + 1):
            files += glob.glob(os.path.join(DEM, f'Copernicus_DSM_COG_10_N{la:02d}_00_E{lo:03d}_00_DEM.tif'))
    arr, tr = merge([rasterio.open(f) for f in files])     # whole tiles: no resampling of the native grid
    r0, c0 = pix(tr, bbox[0], bbox[3]); r1, c1 = pix(tr, bbox[2], bbox[1])
    return arr[0, r0:r1 + 1, c0:c1 + 1].astype(np.float64), tr * tr.translation(c0, r0)


def pix(tr, lon, lat):
    c, r = ~tr * (np.asarray(lon), np.asarray(lat))
    return np.floor(r).astype(int), np.floor(c).astype(int)


def footprint_cover(shape, tr, bbox):
    cover = np.zeros(shape)
    px_m2 = abs(tr.a) * 111320 * np.cos(np.radians((bbox[1] + bbox[3]) / 2)) * abs(tr.e) * 110574
    for f in sorted(glob.glob(os.path.join(REPO, 'buildings', 'by_area', 'ha_noi_*.parquet'))):
        t = pq.read_table(f, columns=['lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring'])
        lon, lat = t['lon_e6'].to_numpy() / 1e6, t['lat_e6'].to_numpy() / 1e6
        k = (lon >= bbox[0]) & (lon < bbox[2]) & (lat >= bbox[1]) & (lat < bbox[3])
        if not k.any():
            continue
        r, c = pix(tr, lon[k], lat[k])
        ok = (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
        area = shapely.area(ring_geoms(t.filter(k))[0])        # m2, rings and rectangles alike
        np.add.at(cover, (r[ok], c[ok]), area[ok])
    return cover / px_m2


def score(real, pred):
    m = np.isfinite(pred) & np.isfinite(real)
    real, pred = real[m], pred[m]
    e = pred - real
    out = dict(n=int(m.sum()), mae_m=round(float(np.abs(e).mean()), 2), bias_m=round(float(np.median(e)), 2),
               spearman=round(float(spearmanr(real, pred)[0]), 3), bands={})
    for lo, hi in ((0, 10), (10, 20), (20, 40), (40, 400)):
        b = (real >= lo) & (real < hi)
        if b.sum() >= 5:
            out['bands'][f'{lo}-{hi} m'] = dict(n=int(b.sum()), real_median=round(float(np.median(real[b])), 1),
                                                pred_median=round(float(np.median(pred[b])), 1),
                                                mae_m=round(float(np.abs(e[b]).mean()), 1))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--bbox', type=float, nargs=4, default=[105.72, 20.93, 105.95, 21.13])
    ap.add_argument('--json')
    a = ap.parse_args()
    dsm, tr = load_dem(tuple(a.bbox))
    cover = footprint_cover(dsm.shape, tr, a.bbox)
    is_open = (cover < OPEN_COVER).astype(float)
    T = pq.read_table(TRUTH).to_pydict()
    T = {k: np.array(v) for k, v in T.items()}
    k = np.char.startswith(T['file'].astype(str), 'ha_noi_') & (T['lon'] >= a.bbox[0]) & (T['lon'] < a.bbox[2]) \
        & (T['lat'] >= a.bbox[1]) & (T['lat'] < a.bbox[3])
    real, h25 = T['real'][k].astype(float), T['h25'][k].astype(float)
    r, c = pix(tr, T['lon'][k], T['lat'][k])
    rep = dict(pixels=int(dsm.size), open_share=round(float(is_open.mean()), 3), truth_rows=int(k.sum()),
               google_raw=score(real, np.where(h25 > 0, h25, np.nan)), dem={})
    for W in (3, 5, 10):                                           # +-W pixels: 90, 150, 300 m
        size = 2 * W + 1
        num = ndimage.uniform_filter(dsm * is_open, size); den = ndimage.uniform_filter(is_open, size)
        ground = np.where(den > 0.05, num / np.maximum(den, 1e-9), np.nan)
        rep['dem'][f'open_W{W}'] = score(real, (dsm - ground)[r, c])
        for P in (5, 20):
            g = ndimage.percentile_filter(dsm, P, size=size)
            rep['dem'][f'pct{P}_W{W}'] = score(real, (dsm - g)[r, c])
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json:
        json.dump(rep, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
