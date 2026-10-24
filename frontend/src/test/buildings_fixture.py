"""Write buildings_fixture.json: about 50 varied rows of a published building file and their footprints as decoded
by buildings/compact_layer.py, so the TypeScript decoder (src/buildings.ts) can be checked against Python.
Adds two synthetic v2 keyhole rows (one hole, two holes) encoded and decoded by compact_layer as well.

    .venv/Scripts/python.exe frontend/src/test/buildings_fixture.py [--file buildings/by_area/ha_noi_13.parquet]
"""
import argparse, json, os, sys
import numpy as np, pyarrow.parquet as pq, shapely

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, 'buildings'))
from compact_layer import decode, keyhole  # noqa: E402

COLS = ('id', 'src', 'lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring', 'height_dm', 'height_src', 'floors')


def shape(p):
    return {'outer': [list(c) for c in p.exterior.coords[:-1]], 'holes': [[list(c) for c in r.coords[:-1]] for r in p.interiors]}


def keyhole_row(rid, poly):
    k = keyhole(shapely.transform(poly, lambda c: np.round(c * 1e6))).astype(np.int64)
    return {'id': rid, 'src': 'osm', 'lon_e6': int(k[0, 0]), 'lat_e6': int(k[0, 1]), 'length_dm': 0, 'width_dm': 0, 'angle_cdeg': 0,
            'ring': np.diff(k, axis=0).astype('<i2').tobytes(), 'height_dm': 120, 'height_src': 'osm', 'floors': 4}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', default='buildings/by_area/ha_noi_13.parquet')
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'buildings_fixture.json'))
    a = ap.parse_args()
    rows = pq.read_table(os.path.join(ROOT, a.file), columns=list(COLS)).to_pylist()
    # Every combination of source, rect/ring and known/unknown height, up to 5 rows each, spread through the file.
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault((r['src'], r['ring'] is None, r['height_dm'] is None), []).append(i)
    pick = sorted({g[int(j)] for g in groups.values() for j in np.linspace(0, len(g) - 1, min(5, len(g)))})
    out = [dict(rows[i]) for i in pick]
    sq = lambda x, y, s: [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]
    x0, y0 = 105.85, 21.03
    out.append(keyhole_row(4_000_000_001, shapely.Polygon(sq(x0, y0, 4e-4), [sq(x0 + 1e-4, y0 + 1e-4, 1e-4)])))
    out.append(keyhole_row(4_000_000_002, shapely.Polygon(sq(x0, y0, 6e-4), [sq(x0 + 1e-4, y0 + 1e-4, 1e-4), sq(x0 + 4e-4, y0 + 3e-4, 1e-4)])))
    fixture = []
    for r in out:
        fixture.append({'row': {**r, 'ring': None if r['ring'] is None else r['ring'].hex()}, 'expected': shape(decode(r))})
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump({'file': os.path.basename(a.file), 'rows': fixture}, f, separators=(',', ':'))
    print(f'wrote {a.out}: {len(fixture)} rows from {a.file}')


if __name__ == '__main__':
    main()
