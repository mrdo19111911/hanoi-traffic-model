"""Measurement only: house numbers read by OCR from street images -> footprints, by code.

Input: ocr.json of ocr_housenumbers.py, the pilot cache of facade_heights.py (meta/img_*.json, thumbs) and its prep output (bld.npz).
For each text box: a number followed by words ("ĐC: 148 Nguyễn Trãi", "Ad 140 N-Thanh Xuan") = address (street = the words,
unaccented, any street: ocr_housenumber_eval.py checks them against OSM), a bare 1-3 digit number = weak.
The box centre pixel -> world ray (facade_heights.pixel_rays) -> first footprint hit within 60 m = the footprint the sign is on.

    python buildings/ocr_housenumber_link.py --ocr .../ocr.json --cache DIR --out DIR_prep --json OUT.json
"""
import argparse, json, os, re, sys, unicodedata

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import facade_heights as F

ADDR = re.compile(r'(\d{1,4}[a-z]?(?:[/-]\d{1,4}[a-z]?)?)\s*[,.\-]?\s*(?:(?:pho|duong|d)\b\.?\s*)?([a-z]{2,}(?:\s+[a-z]{2,}){0,3})')
BARE = re.compile(r'^(?:so\s*)?(\d{1,3}[a-z]?)$')


def norm(t):
    t = unicodedata.normalize('NFD', t.lower().replace('đ', 'd'))
    return ''.join(c for c in t if unicodedata.category(c) != 'Mn')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ocr', required=True); ap.add_argument('--cache', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--json', required=True)
    a = ap.parse_args()
    ocr = json.load(open(a.ocr, encoding='utf-8'))
    F._init(a.out)
    pm = F._B['pm']
    z = np.load(os.path.join(a.out, 'bld.npz'), allow_pickle=True)
    res = []
    for im, boxes in ocr.items():
        mp = os.path.join(a.cache, 'meta', f'img_{im}.json')
        if not os.path.exists(mp): continue
        m = json.load(open(mp, encoding='utf-8'))
        W, H = Image.open(os.path.join(a.cache, 'thumbs', im + '.jpg')).size
        focal, k1, k2 = m['camera_parameters'][:3]; R = F.rodrigues(m['computed_rotation'])
        cx, cy = F.to_xy(*m['computed_geometry']['coordinates'])
        for b in boxes:
            t = norm(b['text']).strip()
            kind, num, street = None, None, None
            g = ADDR.search(t)
            if g and b['conf'] >= 0.5:
                kind, num = 'address', g.group(1)
                street = g.group(2)
            else:
                g = BARE.match(t)
                if g and b['conf'] >= 0.8 and 1 <= int(re.sub(r'\D', '', g.group(1)) or 0) <= 999:
                    kind, num = 'bare', g.group(1)
            if not kind: continue
            u = float(np.mean([p[0] for p in b['box']])); v = float(np.mean([p[1] for p in b['box']]))
            ray = F.pixel_rays([u], [v], W, H, focal, k1, k2, R)[0]
            az = np.arctan2(ray[0], ray[1])
            d, bi, cosi = F.ray_hits(cx, cy, np.array([az]))
            bi = int(bi[0])
            res.append(dict(image=im, kind=kind, number=num, street=street, text=b['text'], conf=round(b['conf'], 2), u=round(u), v=round(v),
                            hit_building=bi if bi >= 0 else None, hit_id=int(z['id'][bi]) if bi >= 0 else None,
                            hit_dist=round(float(d[0]), 1) if bi >= 0 else None,
                            lon=float(m['computed_geometry']['coordinates'][0]), lat=float(m['computed_geometry']['coordinates'][1]),
                            hit_cx=float(z['cx'][bi]) if bi >= 0 else None, hit_cy=float(z['cy'][bi]) if bi >= 0 else None))
    json.dump(res, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    n_img = len(ocr)
    for kind in ('address', 'bare'):
        r = [x for x in res if x['kind'] == kind]
        print(kind, 'boxes', len(r), 'images', len({x['image'] for x in r}), '| linked to a footprint', sum(x['hit_building'] is not None for x in r))
    print('images', n_img)


if __name__ == '__main__':
    main()
