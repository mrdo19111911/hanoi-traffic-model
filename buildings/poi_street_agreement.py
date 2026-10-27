"""Measurement only: give every footprint of one commune the name of its nearest OSM street or alley, then check
that name against the street written in the POI addresses assigned to the same footprint.

Roads: PBF ways with a highway tag in ROAD_HW, minus bridges, tunnels, driveways, parking aisles and sidewalks
(a house does not front them). A name matching JUNK (lanes "Làn xe máy", ramps "Lối xuống ...") drops the way.
Footprint street = the name of the nearest road within MAX_M, distance polygon edge to line; when that road has no
name the footprint's street is unnamed (a named road a bit farther away is reported apart, never substituted).

POI street: OSM addr:street, else the free-form address after the house number (poi_number_agreement.street_of).
Both sides go through road_key(): street_key() of poi_number_agreement with one-letter words and a leading lot /
row prefix ("Lô A", "Dãy nhà 16") dropped. compare():
  exact    same name words (prefix) and the same alley levels
  loose    same name words, one alley path is a prefix of the other ("Vũ Hữu" ~ "Ngõ 172 Vũ Hữu", the house at the
           mouth of the alley, or the address names only the main street)
  differ   anything else
`near_any`: the POI street matches (exact or loose) some named road within MAX_M of the footprint, an upper bound
for corner houses and houses between two streets.

    python buildings/poi_street_agreement.py --commune-rel 19335051 --area buildings/by_area/ha_noi_15.parquet \
        --pbf osrm/data/vietnam-latest.osm.pbf --points out/poi_points.parquet --out DIR
"""
import argparse, collections, json, os, re, sys, time

import duckdb, numpy as np, osmium, pyarrow as pa, pyarrow.parquet as pq, shapely

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from poi_coverage import load_commune
from poi_number_agreement import norm, street_key, street_of

MAX_M = 30.0
ROAD_HW = {'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'living_street', 'service',
           'pedestrian', 'track', 'footway', 'path', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}
SKIP_SUB = {'driveway', 'parking_aisle', 'drive-through', 'sidewalk', 'crossing'}
JUNK = re.compile(r'^(?:lan|loi|cau|ham)\b')
PREFIX = re.compile(r'^(?:lo [a-z0-9]+|day nha \d+|day [a-z0-9]+)\s+')
CONF = (0.0, 0.5, 0.77)


class Roads(osmium.SimpleHandler):
    def __init__(self, bb):
        super().__init__()
        self.bb, self.rows = bb, []

    def way(self, w):
        t = w.tags
        if t.get('highway') not in ROAD_HW or t.get('area') == 'yes' or t.get('service') in SKIP_SUB \
                or t.get('footway') in SKIP_SUB or t.get('bridge', 'no') != 'no' or t.get('tunnel', 'no') != 'no':
            return
        name = t.get('name')
        if name and JUNK.match(norm(name)):
            return
        xy = [(n.location.lon, n.location.lat) for n in w.nodes if n.location.valid()]
        if len(xy) < 2 or not any(self.bb[0] <= x <= self.bb[2] and self.bb[1] <= y <= self.bb[3] for x, y in xy):
            return
        self.rows.append(dict(id=w.id, highway=t['highway'], name=name, xy=xy))


def road_key(s):
    if not s:
        return None
    k = street_key(PREFIX.sub('', norm(s).strip()))
    if k is None:
        return None
    words = tuple(w for w in k[0] if len(w) > 1)
    return (words, k[1]) if words or k[1] else None


def compare(a, b):
    """-> 'exact' / 'loose' / 'differ', or None when either side has no key."""
    if a is None or b is None:
        return None
    (wa, la), (wb, lb) = a, b
    n = min(len(wa), len(wb))
    if wa[:n] != wb[:n] or (not n and not (la and lb)):
        return 'differ'
    m = min(len(la), len(lb))
    if la[:m] != lb[:m]:
        return 'differ'
    return 'exact' if la == lb else 'loose'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commune-rel', type=int, required=True)
    ap.add_argument('--area', required=True, help='published by_area parquet that holds the commune')
    ap.add_argument('--pbf', required=True)
    ap.add_argument('--points', required=True, help='poi_points.parquet of poi_coverage.py for the same commune')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial")
    name, ward, bb, fps, ids, to_m = load_commune(con, a.admin, a.commune_rel, a.area)
    h = Roads(bb)
    h.apply_file(a.pbf, locations=True)
    roads = h.rows
    print(name, '|', len(fps), 'footprints |', len(roads), 'roads,', sum(bool(r['name']) for r in roads), 'named |',
          round(time.time() - t0), 's', flush=True)

    fp_m = shapely.transform(fps, to_m)
    lines = np.array([shapely.LineString(to_m(np.array(r['xy']))) for r in roads], dtype=object)
    keys = [road_key(r['name']) for r in roads]
    named = np.array([k is not None for k in keys])
    tree_all, tree_named = shapely.STRtree(lines), shapely.STRtree(lines[named])
    named_idx = np.flatnonzero(named)

    fi, ri = tree_all.query_nearest(fp_m, max_distance=MAX_M, all_matches=False)
    nearest = np.full(len(fps), -1)
    nearest[fi] = ri
    fi2, ri2 = tree_named.query_nearest(fp_m, max_distance=MAX_M, all_matches=False)
    nearest_named = np.full(len(fps), -1)
    nearest_named[fi2] = named_idx[ri2]
    dist = lambda f, r: float(shapely.distance(fp_m[f], lines[r])) if r >= 0 else None
    # every named road within MAX_M of each footprint, for near_any
    pf, pr = tree_named.query(fp_m, predicate='dwithin', distance=MAX_M)
    around = collections.defaultdict(list)
    for f, r in zip(pf, named_idx[pr]):
        around[f].append(r)

    per_fp, status = [], collections.Counter()
    for f in range(len(fps)):
        r, rn = nearest[f], nearest_named[f]
        st = 'no_road' if r < 0 else ('named' if keys[r] else 'unnamed')
        status[st] += 1
        per_fp.append(dict(id=int(ids[f]), street=roads[r]['name'] if st == 'named' else None,
                           highway=roads[r]['highway'] if r >= 0 else None, dist_m=dist(f, r),
                           named_street=roads[rn]['name'] if rn >= 0 else None, named_dist_m=dist(f, rn)))
    hw_named = collections.Counter(p['highway'] for p in per_fp if p['street'])
    report = {'commune': name, 'osm_relation': a.commune_rel, 'footprints': len(fps), 'max_m': MAX_M,
              'roads': len(roads), 'named_roads': int(named.sum()),
              'footprint_street': {**dict(status), 'pct_named': round(100 * status['named'] / len(fps), 2),
                                   'named_by_highway': dict(hw_named.most_common()),
                                   'unnamed_with_named_within_max_m': sum(1 for p in per_fp if not p['street'] and p['named_street'])},
              'sources': {}}
    print(json.dumps(report['footprint_street'], ensure_ascii=False), flush=True)

    fp_of = {int(i): f for f, i in enumerate(ids)}
    pts = [p for p in pq.read_table(a.points).to_pylist() if p['building'] is not None and p['building'] in fp_of]
    samples = collections.defaultdict(list)
    for src in sorted({p['src'] for p in pts}):
        P = [p for p in pts if p['src'] == src]
        res = {'points_on_footprint': len(P), 'with_street': 0, 'fp_unnamed': 0, 'fp_no_road': 0,
               'exact': 0, 'loose': 0, 'differ': 0, 'near_any': 0, 'by_how': collections.Counter(),
               'by_highway': collections.Counter()}
        conf = {t: collections.Counter() for t in CONF}
        for p in P:
            pk = road_key(p['street'] if p['src'] == 'osm' else street_of(p['addr']))
            if pk is None:
                continue
            res['with_street'] += 1
            f = fp_of[p['building']]
            r = nearest[f]
            if r < 0 or keys[r] is None:
                res['fp_no_road' if r < 0 else 'fp_unnamed'] += 1
                c = None
            else:
                c = compare(pk, keys[r])
                res[c] += 1
                res['by_how'][(p['how'], c)] += 1
                res['by_highway'][(roads[r]['highway'], c)] += 1
                if src == 'overture':
                    for t in CONF:
                        if (p['confidence'] or 0) >= t:
                            conf[t][c] += 1
            hit = any(compare(pk, keys[q]) in ('exact', 'loose') for q in around.get(f, []))
            res['near_any'] += hit
            if c == 'differ' and len(samples[src]) < 30:
                samples[src].append({'building': p['building'], 'poi': p['street'] or p['addr'],
                                     'osm_nearest': roads[r]['name'], 'dist_m': round(dist(f, r), 1),
                                     'match_within_max_m': hit})
        comp = res['exact'] + res['loose'] + res['differ']
        res['pct_exact_or_loose'] = round(100 * (res['exact'] + res['loose']) / comp, 2) if comp else None
        res['pct_near_any'] = round(100 * res['near_any'] / res['with_street'], 2) if res['with_street'] else None
        res['by_how'] = {f'{h_}_{c}': n for (h_, c), n in sorted(res['by_how'].items())}
        res['by_highway'] = {f'{h_}_{c}': n for (h_, c), n in sorted(res['by_highway'].items())}
        if src == 'overture':
            res['overture_confidence'] = {str(t): dict(conf[t]) for t in CONF}
        res['differ_samples'] = samples[src]
        report['sources'][src] = res
        print(src, json.dumps({k: v for k, v in res.items() if k != 'differ_samples'}, ensure_ascii=False)[:600], flush=True)

    with open(os.path.join(a.out, 'poi_street_agreement.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    pq.write_table(pa.Table.from_pylist(per_fp), os.path.join(a.out, 'building_streets.parquet'))
    print('done |', round(time.time() - t0), 's')


if __name__ == '__main__':
    sys.exit(main())
