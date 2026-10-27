"""Measurement only: the alpha options of alpha_frontrow.py against the user's street photos, street segment by segment.

Truth: data/buildings/_truth/street_truth.csv (floors read from the user's Street View photos, x 3.3 m; houses are not
matched one by one, OSM lacks their numbers). Per street with a located segment (SEG): the street-front footprints within
RADIUS_M of the segment centre whose centroid lies within FRONT_M of a centre line carrying the street name, with their
published and raw Google 2.5D heights (data/buildings/_ext_heights/eval/hn_sources.parquet). Each option is the
alpha_frontrow.rule applied to the Google-based rows (published source g25cal / frontrow) whose raw Google height is at
most the option's limit; the segment median after the rule is compared with the median of the photo heights.

    .venv/Scripts/python.exe buildings/alpha_vs_photos.py [--json OUT]
"""
import argparse, csv, json, math, os, struct, sys

import numpy as np, pyarrow.parquet as pq
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alpha_frontrow import rule
from calibrate_heights import LEVEL_M

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRUTH = os.path.join(REPO, 'data', 'buildings', '_truth', 'street_truth.csv')
ROADS = os.path.join(REPO, 'data', 'buildings', 'osm_roads.parquet')
SOURCES = os.path.join(REPO, 'data', 'buildings', '_ext_heights', 'eval', 'hn_sources.parquet')
RADIUS_M, FRONT_M = 150.0, 20.0
SEG = {   # street in the truth file: (name in OSM roads, segment centre lon/lat, how the centre was located)
    'Nguyễn Trãi': ('Nguyễn Trãi', (105.81563, 20.99982), 'good (no. 129)'),
    'Tây Sơn': ('Tây Sơn', (105.82374, 21.00552), 'medium (OSM no. 229, opposite side)'),
    'Sài Đồng': ('Sài Đồng', (105.9108, 21.0360), 'medium (mouth of Ngõ 85)'),
    'Ngô Gia Tự': ('Ngô Gia Tự', (105.8944, 21.0613), 'low (extrapolated from no. 188)'),
}
OPTIONS = [('published now', None, None), ('alpha 1.3, Google <= 15 m', 1.3, 15), ('alpha 1.5, Google <= 15 m', 1.5, 15),
           ('alpha 1.5, no limit', 1.5, None), ('alpha 1.6, no limit', 1.6, None), ('alpha 1.7, no limit', 1.7, None),
           ('alpha 1.8, no limit', 1.8, None), ('alpha 2.0, Google <= 15 m', 2.0, 15), ('alpha 2.0, no limit', 2.0, None)]
KX, KY = 111320 * math.cos(math.radians(21)), 110540


def road_points(R, key, c):
    """Centre lines named `key` (not alleys, not paths) within 500 m of c, sampled every ~2 m, in metres."""
    pts = []
    for hw, nm, g in zip(R['hw'], R['name'], R['geometry']):
        if not nm or key not in nm or hw in ('service', 'footway', 'path', 'track', 'pedestrian'):
            continue
        if nm.lower().startswith(('ngõ', 'ngách', 'hẻm', 'kiệt')):
            continue
        bo = '<' if g[0] == 1 else '>'
        if struct.unpack(bo + 'I', g[1:5])[0] % 1000 != 2:
            continue
        n = struct.unpack(bo + 'I', g[5:9])[0]
        cc = np.frombuffer(g[9:9 + 16 * n], dtype=bo + 'f8').reshape(n, 2)
        if np.hypot((cc[:, 0] - c[0]) * KX, (cc[:, 1] - c[1]) * KY).min() > 500:
            continue
        for a, b in zip(cc[:-1], cc[1:]):
            k = max(1, int(math.hypot((b[0] - a[0]) * KX, (b[1] - a[1]) * KY) / 2)); t = np.linspace(0, 1, k + 1)
            pts.append(np.c_[(a[0] + (b[0] - a[0]) * t) * KX, (a[1] + (b[1] - a[1]) * t) * KY])
    return np.vstack(pts) if pts else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--json')
    a = ap.parse_args()
    rows = list(csv.DictReader(open(TRUTH, encoding='utf-8')))
    R = pq.read_table(ROADS, columns=['hw', 'name', 'geometry']).to_pydict()
    S = {k: np.array(v) for k, v in pq.read_table(SOURCES, columns=['lon', 'lat', 'pub_h', 'pub_src', 'raw_g25']).to_pydict().items()}
    X, Y = S['lon'] * KX, S['lat'] * KY
    out = {}
    for st in sorted({r['street'] for r in rows}):
        L = [r for r in rows if r['street'] == st]
        photo = np.array([float(r['floors']) * LEVEL_M for r in L])
        res = dict(photos=len(L), photo_floors=[float(r['floors']) for r in L],
                   photo_lower_bound=sum(r['floors_kind'] == 'lower_bound' for r in L),
                   photo_median_m=round(float(np.median(photo)), 1))
        if st not in SEG:
            res['segment'] = 'not located: no house number on OSM near the photo'
            out[st] = res; continue
        key, c, how = SEG[st]
        near = np.hypot(X - c[0] * KX, Y - c[1] * KY) <= RADIUS_M
        P = road_points(R, key, c)
        d = np.full(len(X), np.inf)
        if P is not None:
            d[near] = cKDTree(P).query(np.c_[X[near], Y[near]])[0]
        sel = near & (d <= FRONT_M)
        h = S['pub_h'][sel].astype(float); raw = S['raw_g25'][sel].astype(float)
        google = np.isin(S['pub_src'][sel], ['g25cal', 'frontrow']) & np.isfinite(raw)
        res.update(segment=how, front_houses=int(sel.sum()), options={})
        for name, alpha, lim in OPTIONS:
            nh = h.copy()
            if alpha:
                m = google & ((raw <= lim) if lim else True)
                nh[m] = rule(h[m], np.ones(m.sum(), int), raw[m], alpha, 8)[0]
            med = float(np.median(nh))
            res['options'][name] = dict(median_m=round(med, 1), floors=round(med / LEVEL_M, 1),
                                        minus_photo_m=round(med - float(np.median(photo)), 1))
        out[st] = res
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if a.json:
        json.dump(out, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
