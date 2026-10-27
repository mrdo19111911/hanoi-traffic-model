"""Measurement only: ground-truth floors of Hà Nội houses from street photos, several streets, scored against the layer.

Truth file data/buildings/_truth/street_truth.csv, one row per house:
  street, house_no, lon, lat, floors, floors_kind (exact | eye_pm1 | lower_bound), confidence (high|medium|low),
  recorder (user | user_photo_read_by_claude | ...), photo_source, note.
A row is matched to a footprint by code: the nearest footprint centroid within MATCH_M of (lon, lat). Rows without lon/lat
(house number only) stay unmatched until coordinates are supplied. `lower_bound` rows are never used as truth.
Height = floors x 3.3 m. alpha = truth height / baseline height (raw Google 2.5D, published), per street and pooled.
Dev / hold-out split BY STREET (street name hashed with SEED), never by house within a street.

    python buildings/height_truth_eval.py [--truth ...csv] [--sources .../hn_sources.parquet]
"""
import argparse, csv, hashlib, json, math

import numpy as np, pyarrow.parquet as pq
from scipy.spatial import cKDTree

FLOOR_M, MATCH_M, SEED = 3.3, 10.0, 'streets-20261029'


def split_of(street):
    return 'holdout' if int(hashlib.sha256((SEED + street).encode()).hexdigest(), 16) % 3 == 0 else 'dev'


def summary(a):
    a = np.asarray(a, float); a = a[np.isfinite(a) & (a > 0)]
    if not len(a): return dict(n=0)
    return dict(n=int(len(a)), median=round(float(np.median(a)), 2), min=round(float(a.min()), 2), max=round(float(a.max()), 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--truth', default='data/buildings/_truth/street_truth.csv')
    ap.add_argument('--sources', default='data/buildings/_ext_heights/eval/hn_sources.parquet')
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.truth, encoding='utf-8')))
    S = {k: np.array(v) for k, v in pq.read_table(a.sources, columns=['id', 'lon', 'lat', 'pub_h', 'raw_g25']).to_pydict().items()}
    kx = 111320 * math.cos(math.radians(21.0)); ky = 110540
    tree = cKDTree(np.c_[S['lon'] * kx, S['lat'] * ky])
    out = []
    for r in rows:
        r['split'] = split_of(r['street']); r['matched'] = False
        if r['lon'] and r['lat'] and r['floors_kind'] != 'lower_bound':
            d, i = tree.query([float(r['lon']) * kx, float(r['lat']) * ky])
            if d <= MATCH_M:
                h = float(r['floors']) * FLOOR_M
                r.update(matched=True, id=int(S['id'][i]), h_truth=h, google=float(S['raw_g25'][i]), pub=float(S['pub_h'][i]),
                         a_google=h / float(S['raw_g25'][i]) if np.isfinite(S['raw_g25'][i]) and S['raw_g25'][i] > 0 else float('nan'),
                         a_pub=h / float(S['pub_h'][i]))
        out.append(r)
    rep = dict(rows=len(rows), matched=sum(r['matched'] for r in out), streets={}, pooled={}, note='rows without matched footprints are not scored')
    for st in sorted({r['street'] for r in out}):
        m = [r for r in out if r['street'] == st and r['matched']]
        rep['streets'][st] = dict(split=split_of(st), rows=sum(r['street'] == st for r in out), matched=len(m),
                                  alpha_google=summary([r['a_google'] for r in m]), alpha_published=summary([r['a_pub'] for r in m]))
    for sp in ('dev', 'holdout'):
        m = [r for r in out if r['split'] == sp and r['matched']]
        rep['pooled'][sp] = dict(alpha_google=summary([r['a_google'] for r in m]), alpha_published=summary([r['a_pub'] for r in m]))
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
