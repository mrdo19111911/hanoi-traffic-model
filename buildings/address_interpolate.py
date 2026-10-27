"""Measurement only: house numbers for the footprints of one commune by address interpolation along named streets.

Streets: named OSM highways around the commune, grouped by street_key() (poi_number_agreement.py: name words + alley
levels, so "Ngõ 52 Phố Quan Nhân" and "17 Ng. 52 P. Quan Nhân" share a key) and merged into parts (line_merge).
Name words are mapped to the one main street they fit (canon_words), so a trailing locality does not split a street.
Unnamed alley ways: a way takes the alley key of the anchors within VOTE_M that have no named street (at least VOTE_MIN
of them, VOTE_SHARE of the votes), when a way of the parent key (alley minus its last level) lies within PARENT_M.
Frontage: a footprint takes the nearest highway within FRONT_M of its centroid, only when the segment to it crosses
no other footprint (front row). A named highway gives the street part, the position s along it and the side.
Anchors: numbered points of poi_points.parquet (poi_coverage.py) whose street key matches a part within ANCHOR_M.
"25/7 X" is house 7 of ngõ 25 of X. An OSM address on a building way sits at that footprint (weight W), other points
where they are. Points of one source with the same number on one part are merged (median s).
Fit per part: numbers increase or decrease along s (weighted correlation sign); isotonic regression of number on s,
anchors off the fit by more than OUTLIER dropped and refitted. Each side's parity is the anchor majority (>= PARITY
share). A footprint gets the fitted number at its s (not beyond EXTRAP_M past the outer anchors), rounded to its side's
parity.
Hold-out: OSM addresses inside a footprint (building way or node), where that footprint holds one OSM address, are the
truth. 5 folds: the fold's OSM addresses are dropped from the anchors, their
footprints are numbered by frontage, compared with the truth (exact, same parity within 2 = next house, within 6).
Also with no OSM anchors at all (Overture + Foursquare only, as in most of the country), and the position method of
poi_coverage.py (numbers of the points inside or near the same footprint) on the same buildings.

    python buildings/address_interpolate.py --commune-rel 19335051 --area ha_noi_15.parquet --roads osm_roads.parquet \
        --points out/poi_points.parquet --admin vn_admin.parquet --out out
"""
import argparse, collections, json, os, sys, time

import duckdb, numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely
from scipy.optimize import isotonic_regression

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poi_coverage import load_commune
from poi_number_agreement import house_parts, street_key, street_of

NOT_FRONT = ('construction', 'proposed', 'motorway', 'motorway_link', 'steps', 'platform', 'corridor', 'elevator',
             'bus_stop', 'raceway')
FRONT_M, ANCHOR_M, SIDE_M, EXTRAP_M = 30.0, 150.0, 2.0, 20.0
MIN_ANCHORS, PARITY, FOLDS = 3, 0.7, 5
VOTE_M, VOTE_MIN, VOTE_SHARE, PARENT_M = 25.0, 2, 0.6, 15.0     # naming an unnamed alley way by anchor vote
SNAP_M = 30.0                      # snap: real anchor numbers this far along the street from the house
W = {'osm_building': 4.0, 'osm': 2.0, 'overture': 1.0, 'fsq': 1.0}


def outlier(n):
    return max(8.0, 0.25 * n)


def compatible(a, b):
    """Address key a fits street key b: same alley levels, one name word list a prefix of the other."""
    (wa, la), (wb, lb) = a, b
    if la != lb or not wa or not wb:
        return False
    n = min(len(wa), len(wb))
    return wa[:n] == wb[:n]


def key_text(k):
    return ' '.join(k[0]) + (' ngo ' + '/'.join(k[1]) if k[1] else '')


def locate(part, p):
    """Metric point -> (s along part, side +1/-1 or 0 when within SIDE_M, distance)."""
    s = part.project(p)
    a = part.interpolate(max(0.0, min(s, part.length - 1.0)))
    b = part.interpolate(max(0.0, min(s, part.length - 1.0)) + 1.0)
    cr = (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)
    d = part.distance(p)
    return s, (0 if d < SIDE_M else (1 if cr > 0 else -1)), d


def fit(anchors):
    """Anchors [(s, side, n, w)] of one part -> model dict or None."""
    if len(anchors) < MIN_ANCHORS or len({a[2] for a in anchors}) < 2:
        return None
    s, side, n, w = (np.array(x, float) for x in zip(*anchors))
    c = np.cov(s, n, aweights=w)[0, 1]
    if c == 0:
        return None
    sg = 1.0 if c > 0 else -1.0
    keep = np.ones(len(s), bool)
    for it in range(len(s)):                      # drop the worst anchor at a time (one wild number drags its pool)
        o = np.argsort(sg * s[keep], kind='stable')
        x, y, ww = (sg * s[keep])[o], n[keep][o], w[keep][o]
        iso = isotonic_regression(y, weights=ww, increasing=True).x
        r = np.abs(y - iso) / np.array([outlier(v) for v in iso])
        if r.max() <= 1:
            break
        keep[np.flatnonzero(keep)[o][int(r.argmax())]] = False
        if keep.sum() < MIN_ANCHORS:
            return None
    par = {}
    for sd in (1, -1):
        m = keep & (side == sd)
        tot = w[m].sum()
        odd = w[m & (n % 2 == 1)].sum()
        if (m.sum() >= 2) and tot:
            par[sd] = 1 if odd / tot >= PARITY else (0 if odd / tot <= 1 - PARITY else None)
    return {'sg': sg, 'x': x, 'y': iso, 'parity': par, 'anchors': int(keep.sum())}


def predict(m, s, side):
    x = m['sg'] * s
    if x < m['x'][0] - EXTRAP_M or x > m['x'][-1] + EXTRAP_M:
        return None
    v = float(np.interp(x, m['x'], m['y']))
    p = m['parity'].get(side)
    n = int(round((v - p) / 2) * 2 + p) if p is not None else int(round(v))
    return max(n, 1)


def canon_words(key, main_words):
    """Address key -> same key with the name words of the one main street (no alley) they fit, so that
    "quan nhan thanh xuan ha noi" and "quan nhan" vote and match as one street."""
    fit_ = [w for w in main_words if compatible((key[0], ()), (w, ()))]
    return (fit_[0], key[1]) if len(fit_) == 1 else key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commune-rel', type=int, required=True)
    ap.add_argument('--area', required=True)
    ap.add_argument('--roads', required=True, help='osm_roads.parquet (extract_osm.py)')
    ap.add_argument('--points', required=True, help='poi_points.parquet of poi_coverage.py')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial")
    name, ward, bb, fps, ids, to_m = load_commune(con, a.admin, a.commune_rel, a.area)
    fps_m = shapely.transform(fps, to_m)
    fp_tree = shapely.STRtree(fps_m)
    cen = shapely.centroid(fps_m)
    id_ix = {int(b): i for i, b in enumerate(ids)}
    excl = ', '.join(f"'{h}'" for h in NOT_FRONT)
    rows = con.execute(f"""SELECT name, ST_AsWKB(geometry) FROM '{a.roads}'
        WHERE hw NOT IN ({excl}) AND ST_Intersects(geometry, ST_MakeEnvelope({bb[0]}, {bb[1]}, {bb[2]}, {bb[3]}))""").fetchall()
    road_geom = [shapely.transform(shapely.from_wkb(bytes(g)), to_m) for _, g in rows]
    road_key = [(k if k and k[0] else None) for k in (street_key(nm) if nm else None for nm, _ in rows)]
    road_tree = shapely.STRtree(road_geom)
    main_words = sorted({k[0] for k in road_key if k and not k[1]}, key=len)
    road_key = [canon_words(k, main_words) if k else None for k in road_key]
    stat = collections.Counter(roads=len(rows), named_roads=sum(k is not None for k in road_key))

    # anchors: key, location, number
    pts = pq.read_table(a.points).to_pylist()
    anc = []
    for p in pts:
        hp = house_parts(p['number'])
        base = street_key(p['street'] if p['src'] == 'osm' else street_of(p['addr']))
        if not hp or not base or not base[0]:
            continue
        kind = 'osm_building' if p['src'] == 'osm' and p['id'].startswith('w') and p['how'] == 'in' else p['src']
        b = p['building'] if p['building'] in id_ix else None
        loc = cen[id_ix[b]] if kind == 'osm_building' and b is not None else shapely.Point(to_m([[p['lon'], p['lat']]])[0])
        anc.append(dict(key=canon_words((base[0], base[1] + hp[0]), main_words), loc=loc, n=hp[1], src=kind,
                        building=b, how=p['how']))

    # unnamed alleys named by anchor vote (two passes: a ngach needs its ngo named first)
    for _ in range(2):
        named = {k for k in road_key if k}
        orphan = [r for r in anc if r['key'][1] and not any(compatible(r['key'], k) for k in named)]
        if not orphan:
            break
        otree = shapely.STRtree([r['loc'] for r in orphan])
        new = {}
        for ri, g in enumerate(road_geom):
            if road_key[ri]:
                continue
            votes = collections.Counter(orphan[h]['key'] for h in otree.query(g, predicate='dwithin', distance=VOTE_M))
            if not votes:
                continue
            k, c = votes.most_common(1)[0]
            if c < VOTE_MIN or c / sum(votes.values()) < VOTE_SHARE:
                continue
            parent = (k[0], k[1][:-1])
            near = road_tree.query(g, predicate='dwithin', distance=PARENT_M)
            if any(road_key[j] and compatible(parent, road_key[j]) for j in near if j != ri):
                new[ri] = k
        for ri, k in new.items():
            road_key[ri] = k
        stat['alley_ways_named'] += len(new)

    by_key = collections.defaultdict(list)
    for g, k in zip(road_geom, road_key):
        if k:
            by_key[k].append(g)
    parts, part_key = [], []
    for k, gs in by_key.items():
        for p in shapely.get_parts(shapely.line_merge(shapely.union_all(gs))):
            if p.geom_type == 'LineString' and p.length > 5:
                parts.append(p)
                part_key.append(k)
    keys_parts = collections.defaultdict(list)
    for i, k in enumerate(part_key):
        keys_parts[k].append(i)
    stat['parts'] = len(parts)

    # frontage
    front, freason = {}, {}
    ri_fp, ri_rd = road_tree.query_nearest(cen, max_distance=FRONT_M, all_matches=False)
    for f, r in zip(ri_fp, ri_rd):
        hits = fp_tree.query(shapely.shortest_line(cen[f], road_geom[r]), predicate='intersects')
        k = road_key[r]
        if any(h != f for h in hits):
            freason[f] = 'behind_other'
        elif not k or k not in keys_parts:
            freason[f] = 'unnamed_road'
        else:
            pi = min(keys_parts[k], key=lambda i: parts[i].distance(cen[f]))
            s, side, _ = locate(parts[pi], cen[f])
            front[f] = (pi, s, side)
            freason[f] = 'front_named'
    stat.update(collections.Counter(freason.values()))
    stat['no_road_within'] = len(fps) - len(freason)

    # anchors onto street parts
    raw = []
    for r in anc:
        cand = [i for k, ii in keys_parts.items() if compatible(r['key'], k) for i in ii]
        if not cand:
            stat['anchor_no_street'] += 1
            continue
        pi = min(cand, key=lambda i: parts[i].distance(r['loc']))
        s, side, d = locate(parts[pi], r['loc'])
        if d > ANCHOR_M:
            stat['anchor_far'] += 1
            continue
        raw.append(dict(r, part=pi, s=s, side=side))
    stat['anchors'] = len(raw)
    print(name, '|', len(fps), 'footprints |', dict(stat), '|', round(time.time() - t0), 's', flush=True)

    def models(an_):
        g = collections.defaultdict(list)
        for r in an_:
            g[(r['part'], r['src'], r['n'])].append(r)
        per = collections.defaultdict(list)
        for (pi, src, n), rs in g.items():
            sides = collections.Counter(r['side'] for r in rs).most_common(1)[0][0]
            per[pi].append((float(np.median([r['s'] for r in rs])), sides, n, W[src]))
        return {pi: m for pi, an in per.items() if (m := fit(an))}

    def number_of(ms, f, by_part=None):
        """Interpolated number; with by_part (anchors per part) the real anchor number of the same part, within
        SNAP_M along it and of the side's parity, nearest to the interpolated one (snap)."""
        if f not in front:
            return None, freason.get(f, 'no_road_within')
        pi, s, side = front[f]
        if pi not in ms:
            return None, 'street_without_model'
        n = predict(ms[pi], s, side)
        if not n:
            return None, 'beyond_anchors'
        if by_part is not None:
            par = ms[pi]['parity'].get(side)
            c = [r['n'] for r in by_part.get(pi, ()) if abs(r['s'] - s) <= SNAP_M and (par is None or r['n'] % 2 == par)]
            if c:
                return min(c, key=lambda x: (abs(x - n), x)), 'ok'
        return n, 'ok'

    # truth: OSM addresses inside a footprint that holds exactly one OSM address
    osm_in = collections.defaultdict(list)
    for r in anc:
        if r['src'].startswith('osm') and r['how'] == 'in' and r['building'] is not None:
            osm_in[r['building']].append(r)
    truth = [rs[0] for rs in osm_in.values() if len({(r['key'], r['n']) for r in rs}) == 1]
    pos = collections.defaultdict(set)
    for p in pts:
        if p['src'] in ('overture', 'fsq') and p['building'] is not None and house_parts(p['number']):
            pos[p['building']].add(house_parts(p['number'])[1])

    def score(rows_):
        c = collections.Counter(truth=len(rows_))
        for t, n, why in rows_:
            c[why] += 1
            if n is None:
                continue
            if not compatible(t['key'], part_key[front[id_ix[t['building']]][0]]):
                c['other_street'] += 1
                continue
            d = abs(n - t['n'])
            c['exact'] += d == 0
            c['within_2'] += d <= 2 and d % 2 == 0
            c['within_6'] += d <= 6 and d % 2 == 0
            c['parity_ok'] += d % 2 == 0
        return dict(c)

    def per_part(an_):
        g = collections.defaultdict(list)
        for r in an_:
            g[r['part']].append(r)
        return g

    evals = {}
    for name_, base in (('all_sources', raw), ('no_osm', [r for r in raw if not r['src'].startswith('osm')])):
        rows_, rows_snap = [], []
        for k in range(FOLDS):
            test = [t for t in truth if t['building'] % FOLDS == k]
            tb = {t['building'] for t in test}
            train = [r for r in base if not (r['src'].startswith('osm') and r['building'] in tb)]
            ms, bp = models(train), per_part(train)
            rows_ += [(t, *number_of(ms, id_ix[t['building']])) for t in test]
            rows_snap += [(t, *number_of(ms, id_ix[t['building']], bp)) for t in test]
        evals[name_] = score(rows_)
        evals[name_ + '_snap'] = score(rows_snap)
        print('holdout', name_, evals[name_], '\n  snap', evals[name_ + '_snap'], flush=True)
    pc = collections.Counter(truth=len(truth))
    for t in truth:
        got = pos.get(t['building'])
        if got:
            pc['has_number'] += 1
            pc['exact'] += t['n'] in got
            pc['within_2'] += any(abs(g - t['n']) <= 2 and (g - t['n']) % 2 == 0 for g in got)
    evals['position_method'] = dict(pc)
    print('holdout position_method', evals['position_method'], flush=True)

    # final: all anchors, every footprint
    ms = models(raw)
    out, why = [], collections.Counter()
    for f in range(len(fps)):
        n, w_ = number_of(ms, f)
        why[w_] += 1
        if n:
            pi, s, side = front[f]
            out.append(dict(id=int(ids[f]), street=key_text(part_key[pi]), number=n,
                            parity_known=ms[pi]['parity'].get(side) is not None, anchors=ms[pi]['anchors']))
    pq.write_table(pa.Table.from_pylist(out), os.path.join(a.out, 'address_numbers.parquet'))
    rep = {'commune': name, 'osm_relation': a.commune_rel, 'footprints': len(fps), 'stat': dict(stat),
           'parts_with_model': len(ms), 'numbered': len(out), 'pct_numbered': round(100 * len(out) / len(fps), 2),
           'why_not': dict(why), 'holdout': evals,
           'params': dict(FRONT_M=FRONT_M, ANCHOR_M=ANCHOR_M, SIDE_M=SIDE_M, EXTRAP_M=EXTRAP_M, MIN_ANCHORS=MIN_ANCHORS,
                          PARITY=PARITY, VOTE_M=VOTE_M, VOTE_MIN=VOTE_MIN, VOTE_SHARE=VOTE_SHARE, PARENT_M=PARENT_M, W=W)}
    with open(os.path.join(a.out, 'address_interpolate.json'), 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: rep[k] for k in ('numbered', 'pct_numbered', 'why_not')}, ensure_ascii=False),
          '|', round(time.time() - t0), 's')


if __name__ == '__main__':
    sys.exit(main())


