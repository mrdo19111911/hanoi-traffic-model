"""Measurement only: why house numbers from OSM, Overture places and Foursquare disagree on the same footprint.

Input: poi_points.parquet of poi_coverage.py (every point with its source, number, street or free-form address,
Overture confidence, the footprint it was assigned to and how: in / near / none).

Street of a point: OSM addr:street; for Overture and Foursquare the free-form address right after the house number
(or the next comma segment), see street_of(). street_key() drops accents and the generic words (phố, đường, ...),
keeps the alley numbers (ngõ 12 -> 12) apart. Two streets are the same when the alley numbers agree (or one has
none) and one name's word list is a prefix of the other's ("nguyen trai" ~ "nguyen trai thanh xuan ha noi").
Numbers are compared on their leading digits (12A ~ 12, 25/7 ~ 25).

Per pair of sources, over footprints that got a number from both:
  agree             some number of one source equals some number of the other
  single / multi    multi when either source put 2+ distinct numbers on the footprint (tower, mall, market)
  by_street         same_agree / same_neighbour (same street and parity, numbers within NEIGHBOUR) / same_far /
                    other_street / unknown street
  by_how            in/in, in/near, near/near
  by_confidence     Overture points kept only at confidence >= t
Position test: for each numbered point with a street, the nearest point of the reference source with the same number
on the same street anywhere in the commune, and its distance (a right number placed at the wrong house).

    python buildings/poi_number_agreement.py --points out/poi_points.parquet --json out/poi_number_agreement.json
"""
import argparse, collections, itertools, json, math, re, sys, unicodedata

import numpy as np, pyarrow.parquet as pq
from scipy.spatial import cKDTree

GENERIC = {'pho', 'duong', 'd', 'p', 'so', 'sn', 'nha', 'dg', 'ql', 'tl', 'street', 'road', 'st', 'rd'}
ALLEY = {'ngo', 'ng', 'ngach', 'hem', 'kiet', 'alley', 'lane'}
NEIGHBOUR = 6                      # same street, same parity, numbers this close = the next house or two
SKIP_SEG = re.compile(r'^(?:(?:tang|lau|phong|lo|toa|tower|block|kiot|ki ot|can ho|ch)\b|p\.?\s*\d)')
NUM_SEG = re.compile(r'^(?:so nha|so|sn|nha)?\s*(\d{1,4}[a-z]?(?:\s*[/-]\s*\d{1,4}[a-z]?)*)\b(?!\s*(?:tang|m\b))')
CONF = (0.0, 0.3, 0.5, 0.77, 0.9)
DIST = (10, 30, 100, 300)


def norm(t):
    t = unicodedata.normalize('NFD', (t or '').lower().replace('đ', 'd'))
    return ''.join(c for c in t if unicodedata.category(c) != 'Mn')


def digits(n):
    m = re.match(r'\d+', str(n or '').strip())
    return (m.group(0).lstrip('0') or '0') if m else None


def street_of(addr):
    """Free-form address -> street text: the rest of the segment after the house number, else the next segment."""
    segs = [s.strip() for s in norm(addr).split(',')]
    for i, seg in enumerate(segs):
        if not seg or SKIP_SEG.match(seg):
            continue
        m = NUM_SEG.match(seg)
        rest = seg[m.end():].strip() if m else seg
        if rest:
            return rest
        return segs[i + 1] if i + 1 < len(segs) else None
    return None


def street_key(s):
    """Street text -> (name words tuple, alley numbers tuple, outer alley first) or None.
    The numbers after an alley word are its levels: "ngõ 43" = (43), "ngách 43/66" = (43, 66), a lone
    "ngách 66" is level 2, so "ngách 66 ngõ 43" also gives (43, 66). Other numbers are dropped."""
    if not s:
        return None
    toks = re.findall(r'[a-z]+|\d+', norm(s))
    words, levels, i = [], {}, 0
    while i < len(toks):
        t = toks[i]
        if t in ALLEY:
            j = i + 1
            while j < len(toks) and toks[j].isdigit():
                j += 1
            nums = [x.lstrip('0') or '0' for x in toks[i + 1:j]]
            first = 2 if t == 'ngach' and len(nums) == 1 else 1
            for n, x in enumerate(nums):
                levels.setdefault(first + n, x)
            i = j
            continue
        if not t.isdigit() and t not in GENERIC:
            words.append(t)
        i += 1
    alley = tuple(levels[l] for l in sorted(levels))
    return (tuple(words), alley) if words or alley else None


def house_parts(number):
    """House number text -> (extra alley levels, house int) or None: "25/7" = house 7 in alley 25 of the street,
    "12/34/5" = house 5 in ngách 12/34; "12A", "12-14" = 12."""
    s = str(number or '')
    parts = re.findall(r'\d+', s)
    if not parts:
        return None
    if '/' in s and len(parts) > 1:
        return tuple(p.lstrip('0') or '0' for p in parts[:-1]), int(parts[-1])
    return (), int(parts[0])


def same_street(a, b):
    if a is None or b is None:
        return None
    (wa, la), (wb, lb) = a, b
    if la and lb and la != lb:
        return False
    if not wa or not wb:
        return bool(la and lb)
    n = min(len(wa), len(wb))
    return wa[:n] == wb[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--points', required=True)
    ap.add_argument('--json', required=True)
    a = ap.parse_args()
    pts = pq.read_table(a.points).to_pylist()
    for p in pts:
        p['num'] = digits(p['number'])
        p['skey'] = street_key(p['street'] if p['src'] == 'osm' else street_of(p['addr']))
    num_pts = [p for p in pts if p['num']]
    srcs = sorted({p['src'] for p in num_pts})
    lat0 = np.mean([p['lat'] for p in pts])
    k = np.array([111320 * math.cos(math.radians(lat0)), 110574])
    out = {'points_with_number': dict(collections.Counter(p['src'] for p in num_pts)),
           'with_street': dict(collections.Counter(p['src'] for p in num_pts if p['skey'])), 'pairs': {}, 'position': {}}

    by_fp = collections.defaultdict(lambda: collections.defaultdict(list))
    for p in num_pts:
        if p['building'] is not None:
            by_fp[p['building']][p['src']].append(p)

    for s1, s2 in itertools.combinations(srcs, 2):
        res = {'footprints': 0, 'agree': 0}
        tallies = {n: collections.Counter() for n in ('single_multi', 'by_street', 'by_how')}
        conf = {t: collections.Counter() for t in CONF}
        samples = []
        for fp, d in by_fp.items():
            A, B = d.get(s1), d.get(s2)
            if not A or not B:
                continue
            ok = bool({p['num'] for p in A} & {p['num'] for p in B})
            res['footprints'] += 1
            res['agree'] += ok
            multi = 'multi' if max(len({p['num'] for p in A}), len({p['num'] for p in B})) > 1 else 'single'
            tallies['single_multi'][(multi, ok)] += 1
            ss = [same_street(p['skey'], q['skey']) for p in A for q in B]
            if any(x is True for x in ss):
                pairs = [(int(p['num']), int(q['num'])) for p in A for q in B if same_street(p['skey'], q['skey'])]
                if any(x == y for x, y in pairs):
                    st = 'same_agree'
                elif any(x % 2 == y % 2 and abs(x - y) <= NEIGHBOUR for x, y in pairs):
                    st = 'same_neighbour'
                else:
                    st = 'same_far'
            elif any(x is False for x in ss):
                st = 'other_street'
            else:
                st = 'unknown'
            tallies['by_street'][st] += 1
            hw = '/'.join(sorted({p['how'] for p in A})[:1] + sorted({q['how'] for q in B})[:1])
            tallies['by_how'][(hw, ok)] += 1
            if 'overture' in (s1, s2):
                ov, other = (A, B) if s1 == 'overture' else (B, A)
                for t in CONF:
                    keep = [p for p in ov if (p['confidence'] or 0) >= t]
                    if keep:
                        conf[t][bool({p['num'] for p in keep} & {q['num'] for q in other})] += 1
            if not ok and len(samples) < 25:
                samples.append({'building': fp, s1: [(p['number'], p['street'] or p['addr']) for p in A][:3],
                                s2: [(q['number'], q['street'] or q['addr']) for q in B][:3]})
        res['single_multi'] = {f"{m}_{'agree' if o else 'disagree'}": n for (m, o), n in sorted(tallies['single_multi'].items())}
        res['by_street'] = dict(tallies['by_street'])
        res['by_how'] = {f"{h}_{'agree' if o else 'disagree'}": n for (h, o), n in sorted(tallies['by_how'].items())}
        if any(conf[t] for t in CONF):
            res['overture_confidence'] = {str(t): {'footprints': conf[t][True] + conf[t][False], 'agree': conf[t][True]}
                                          for t in CONF}
        res['disagree_samples'] = samples
        out['pairs'][f'{s1}~{s2}'] = res
        print(s1, s2, {k_: v for k_, v in res.items() if k_ != 'disagree_samples'}, flush=True)

    # position test: same number + same street anywhere in the commune -> distance to the nearest such reference point
    for ref in srcs:
        R = [p for p in num_pts if p['src'] == ref and p['skey']]
        idx = collections.defaultdict(list)
        for p in R:
            idx[p['num']].append(p)
        for src in srcs:
            if src == ref:
                continue
            d_all, found, no_ref = [], 0, 0
            cand = [p for p in num_pts if p['src'] == src and p['skey']]
            for p in cand:
                same = [q for q in idx.get(p['num'], []) if same_street(p['skey'], q['skey'])]
                if not same:
                    no_ref += 1
                    continue
                found += 1
                xy = (np.array([[q['lon'], q['lat']] for q in same]) - [p['lon'], p['lat']]) * k
                d_all.append(float(np.hypot(xy[:, 0], xy[:, 1]).min()))
            d = np.array(d_all)
            out['position'][f'{src}->{ref}'] = {
                'points': len(cand), 'same_number_street_in_ref': found, 'not_in_ref': no_ref,
                **({'dist_m_p25_p50_p75_p90': [round(float(x), 1) for x in np.percentile(d, [25, 50, 75, 90])],
                    **{f'within_{m}m': int((d <= m).sum()) for m in DIST}} if len(d) else {})}
            print('position', src, '->', ref, out['position'][f'{src}->{ref}'], flush=True)

    # same source, same number + street at two places far apart: ambiguity of the address itself
    for src in srcs:
        g = collections.defaultdict(list)
        for p in num_pts:
            if p['src'] == src and p['skey']:
                g[(p['num'], p['skey'])].append((p['lon'], p['lat']))
        spread = [float(np.ptp((np.array(v) * k), axis=0).max()) for v in g.values() if len(v) > 1]
        out.setdefault('self_spread', {})[src] = {'keys': len(g), 'keys_with_2plus_points': len(spread),
                                                  'spread_over_50m': int(sum(s > 50 for s in spread))}
    with open(a.json, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    sys.exit(main())
