"""Download Copernicus GLO-30 DEM tiles (AWS open data, free) covering Vietnam into data/dem/glo30.

    .venv/Scripts/python.exe buildings/fetch_dem.py [--out data/dem/glo30] [--workers 3]
"""
import argparse, os, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
LAT, LON = range(8, 24), range(102, 110)          # tile SW corners; covers 8-24 N, 102-110 E


def get(args):
    lat, lon, out = args
    name = f"Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM"
    dst = os.path.join(out, name + '.tif')
    if os.path.exists(dst):
        return name, 'skip'
    try:
        data = urllib.request.urlopen(f"{BASE}/{name}/{name}.tif", timeout=300).read()
    except urllib.error.HTTPError as e:
        return name, f'no tile ({e.code})'
    with open(dst + '.tmp', 'wb') as f:
        f.write(data)
    os.replace(dst + '.tmp', dst)
    return name, f'ok {len(data) / 1e6:.1f} MB'


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('--out', default='data/dem/glo30'); ap.add_argument('--workers', type=int, default=3)
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    with ThreadPoolExecutor(a.workers) as ex:
        for name, st in ex.map(get, [(la, lo, a.out) for la in LAT for lo in LON]):
            print(name, st, flush=True)
