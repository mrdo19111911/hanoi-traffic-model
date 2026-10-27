"""Measurement only: how often OCR reads a house number right, scored against OSM addr:housenumber by code.

Inputs: the cache of facade_heights.py fetch (meta/img_*.json), ocr.json of ocr_housenumbers.py, the links of
ocr_housenumber_link.py, and data/buildings/osm_buildings.parquet (OSM buildings with addr:housenumber / addr:street).

A read (address = number + words, bare = 1-3 digits alone) is
  checkable  when an OSM building with a house number lies within R_M of the camera (otherwise OSM cannot tell);
  correct    when one of those buildings carries the same number (digits compared, letter suffix ignored) and, for
             an address read, the OSM street (unaccented, "phố"/"đường" dropped) shares a word of 3+ letters with it;
  linked     when the footprint its pixel ray hits lies within LINK_M of that OSM building (centroid to centroid).
Recall: OSM-numbered buildings within NEAR_M of at least one camera, and the share of them read correctly at least once.

    python buildings/ocr_housenumber_eval.py --cache DIR --ocr ocr.json --links links.json [--json OUT]
"""
import argparse, glob, json, os, re, sys, unicodedata

import duckdb, numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import facade_heights as F

R_M, LINK_M, NEAR_M = 60.0, 8.0, 25.0
OSM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'buildings', 'osm_buildings.parquet')


def norm(t):
    t = unicodedata.normalize('NFD', (t or '').lower().replace('đ', 'd'))
    return ''.join(c for c in t if unicodedata.category(c) != 'Mn')


def digits(n):
    m = re.match(r'\d+', str(n or '').strip())
    return m.group(0).lstrip('0') if m else None


def street_words(s):
    return {w for w in re.findall(r'[a-z]+', norm(s)) if len(w) >= 3 and w not in ('pho', 'duong', 'ngo', 'ngach')}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', required=True); ap.add_argument('--ocr', required=True); ap.add_argument('--links', required=True)
    ap.add_argument('--json')
    a = ap.parse_args()
    cams = {}
    for p in glob.glob(os.path.join(a.cache, 'meta', 'img_*.json')):
        m = json.load(open(p, encoding='utf-8'))
        if m.get('computed_geometry'):
            cams[os.path.basename(p)[4:-5]] = m['computed_geometry']['coordinates']
    ocr = json.load(open(a.ocr, encoding='utf-8'))
    links = json.load(open(a.links, encoding='utf-8'))
    lon = np.array([c[0] for c in cams.values()]); lat = np.array([c[1] for c in cams.values()])
    bb = (lon.min() - .002, lat.min() - .002, lon.max() + .002, lat.max() + .002)
    con = duckdb.connect(); con.execute('LOAD spatial')
    rows = con.execute(f"""SELECT addr_housenumber, addr_street, ST_X(ST_Centroid(geometry)), ST_Y(ST_Centroid(geometry))
        FROM '{OSM}' WHERE addr_housenumber IS NOT NULL AND ST_X(ST_Centroid(geometry)) BETWEEN {bb[0]} AND {bb[2]}
        AND ST_Y(ST_Centroid(geometry)) BETWEEN {bb[1]} AND {bb[3]}""").fetchall()
    on = np.array([digits(r[0]) for r in rows], object); ost = [street_words(r[1]) for r in rows]
    ox, oy = F.to_xy(np.array([r[2] for r in rows]), np.array([r[3] for r in rows]))
    otree = cKDTree(np.c_[ox, oy])
    read_ok = set()
    per = {k: dict(reads=0, checkable=0, correct=0, linked_of_correct=0, with_hit_of_correct=0) for k in ('address', 'bare')}
    for r in links:
        P = per[r['kind']]; P['reads'] += 1
        cx, cy = F.to_xy(r['lon'], r['lat'])
        near = otree.query_ball_point([cx, cy], R_M)
        if not near:
            r['check'] = 'no_osm'; continue
        P['checkable'] += 1
        n, sw = digits(r['number']), street_words(r['street']) if r['kind'] == 'address' else None
        hit = [j for j in near if on[j] == n and (sw is None or ost[j] & sw)]
        r['check'] = 'correct' if hit else 'wrong'
        if not hit:
            continue
        P['correct'] += 1; read_ok.update(hit)
        if r.get('hit_cx') is not None:
            P['with_hit_of_correct'] += 1
            d = min(float(np.hypot(ox[j] - r['hit_cx'], oy[j] - r['hit_cy'])) for j in hit)
            r['link_err_m'] = round(d, 1)
            P['linked_of_correct'] += d <= LINK_M
    ix, iy = F.to_xy(lon, lat)
    dd, _ = cKDTree(np.c_[ix, iy]).query(np.c_[ox, oy])
    near_cam = np.flatnonzero(dd <= NEAR_M)
    with_text = sum(1 for v in ocr.values() if v)
    with_num = len({r['image'] for r in links})
    rep = dict(images=len(ocr), images_with_text=with_text, images_with_number_read=with_num,
               osm_numbered_in_area=len(rows), osm_numbered_within_25m_of_a_camera=int(len(near_cam)),
               osm_numbered_read_correctly=int(len(set(near_cam.tolist()) & read_ok)), reads=per,
               params=dict(R_M=R_M, LINK_M=LINK_M, NEAR_M=NEAR_M))
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json:
        json.dump(dict(report=rep, reads=links), open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
