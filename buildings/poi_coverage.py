"""Measurement only: how many footprints of one commune get a house number and a business from open POI data.

Sources, each a set of points:
  osm       nodes and ways of the PBF with addr:housenumber or a business tag (shop, craft, office, healthcare,
            tourism, amenity outside NON_BUSINESS); a way is its mean node location.
  overture  Overture Maps places (latest release from the STAC catalog, CDLA-Permissive-2.0): business = any
            basic_category, house number = parsed from addresses[1].freeform by house_number().
  ov_addr   Overture Maps addresses theme: house number = `number`.
  fsq       Foursquare OS Places (Apache-2.0) from the gated Hugging Face copy; only with --fsq and HF_TOKEN set.

A point belongs to the footprint that contains it (`in`). A point inside no footprint goes to the nearest
footprint within NEAR_M (`near`), since geocoded POIs often sit on the street in front of the house.
Footprints = the published file of the commune (buildings/by_area), centroid inside the commune polygon.

    python buildings/poi_coverage.py --commune-rel 19335051 --area buildings/by_area/ha_noi_15.parquet \
        --pbf osrm/data/vietnam-latest.osm.pbf --admin data/buildings/vn_admin.parquet --out DIR
"""
import argparse, collections, json, math, os, re, sys, time, unicodedata, urllib.request

import duckdb, numpy as np, osmium, pyarrow as pa, pyarrow.parquet as pq, shapely
from shapely import wkb as swkb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compact_layer import decode

STAC = "https://stac.overturemaps.org"
FSQ = "hf://datasets/foursquare/fsq-os-places/release/dt={dt}/places/parquet/*.parquet"
NEAR_M = 10.0
BUSINESS_KEYS = ('shop', 'craft', 'office', 'healthcare', 'tourism')
NON_BUSINESS = {'parking', 'parking_space', 'parking_entrance', 'motorcycle_parking', 'bicycle_parking', 'bench',
                'toilets', 'waste_basket', 'waste_disposal', 'recycling', 'drinking_water', 'fountain', 'shelter',
                'telephone', 'post_box', 'clock', 'vending_machine', 'grave_yard', 'place_of_worship', 'townhall',
                'police', 'fire_station', 'bus_station', 'taxi', 'charging_station', 'loading_dock', 'water_point',
                'atm', 'public_bookcase', 'social_facility', 'community_centre', 'courthouse', 'prison'}
SKIP_SEG = re.compile(r'^(?:(?:tang|lau|phong|lo|toa|tower|block|kiot|ki ot|can ho|ch)\b|p\.?\s*\d)')
NUM_SEG = re.compile(r'^(?:so nha|so|sn|nha)?\s*(\d{1,4}[a-z]?(?:\s*[/-]\s*\d{1,4}[a-z]?)*)\b(?!\s*(?:tang|m\b))')


def norm(t):
    t = unicodedata.normalize('NFD', (t or '').lower().replace('đ', 'd'))
    return ''.join(c for c in t if unicodedata.category(c) != 'Mn')


def house_number(addr):
    """Free-form Vietnamese address -> house number or None. Comma segments in order: floor/room/lot/tower ones
    are skipped, the first other segment counts only when it starts with a number ("12A", "so 5", "34/2") that
    is not an alley number ("ngo 12" starts with a word, so it gives none)."""
    for seg in norm(addr).split(','):
        seg = seg.strip()
        if not seg or SKIP_SEG.match(seg):
            continue
        m = NUM_SEG.match(seg)
        return re.sub(r'\s+', '', m.group(1)) if m else None
    return None


def get_json(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'hanoi-traffic-model'}),
                                            timeout=120))


def overture_files(release, theme, typ, bb):
    coll = get_json(f"{STAC}/{release}/{theme}/{typ}/collection.json")
    items = [l['href'] for l in coll['links'] if l['rel'] == 'item']
    boxes = coll['extent']['spatial']['bbox'][1:]
    return [get_json(h)['assets']['aws']['href'] for h, b in zip(items, boxes)
            if b[0] <= bb[2] and b[2] >= bb[0] and b[1] <= bb[3] and b[3] >= bb[1]]


def bbox_sql(bb):
    return f"bbox.xmax >= {bb[0]} AND bbox.xmin <= {bb[2]} AND bbox.ymax >= {bb[1]} AND bbox.ymin <= {bb[3]}"


def fetch_overture(con, release, bb, cache):
    """Places and addresses inside bb -> cached parquet files; returns their paths."""
    out = {}
    for name, theme, typ, cols in (
            ('overture', 'places', 'place',
             "id, names.primary AS name, basic_category AS category, confidence, addresses[1].freeform AS addr, "
             "list_transform(sources, s -> s.dataset) AS datasets"),
            ('ov_addr', 'addresses', 'address', "id, number, street, unit")):
        dst = os.path.join(cache, f"{name}_{release}.parquet")
        if not os.path.exists(dst):
            src = ', '.join(f"'{h}'" for h in overture_files(release, theme, typ, bb))
            con.execute(f"COPY (SELECT {cols}, ST_X(geometry) lon, ST_Y(geometry) lat FROM read_parquet([{src}]) "
                        f"WHERE {bbox_sql(bb)}) TO '{dst}.tmp' (FORMAT parquet)")
            os.replace(dst + '.tmp', dst)
        out[name] = dst
    return out


def fetch_fsq(con, dt, bb, cache):
    dst = os.path.join(cache, f"fsq_{dt}.parquet")
    if not os.path.exists(dst):
        token = os.environ['HF_TOKEN'].replace("'", '')
        con.execute(f"CREATE SECRET hf (TYPE huggingface, TOKEN '{token}')")
        con.execute(f"COPY (SELECT fsq_place_id id, name, fsq_category_labels[1] AS category, address AS addr, "
                    f"longitude lon, latitude lat FROM read_parquet('{FSQ.format(dt=dt)}') WHERE date_closed IS NULL "
                    f"AND longitude BETWEEN {bb[0]} AND {bb[2]} AND latitude BETWEEN {bb[1]} AND {bb[3]}) "
                    f"TO '{dst}.tmp' (FORMAT parquet)")
        os.replace(dst + '.tmp', dst)
    return dst


class OsmPoints(osmium.SimpleHandler):
    def __init__(self, bb):
        super().__init__()
        self.bb, self.rows = bb, []

    def _take(self, kind, oid, tags, lon, lat):
        if not (self.bb[0] <= lon <= self.bb[2] and self.bb[1] <= lat <= self.bb[3]):
            return
        biz = next((f"{k}={tags[k]}" for k in BUSINESS_KEYS if k in tags), None)
        if biz is None and 'amenity' in tags and tags['amenity'] not in NON_BUSINESS:
            biz = f"amenity={tags['amenity']}"
        num = tags.get('addr:housenumber')
        if biz or num:
            self.rows.append(dict(id=f"{kind}{oid}", lon=lon, lat=lat, number=num, category=biz,
                                  name=tags.get('name'), street=tags.get('addr:street')))

    def node(self, n):
        if n.tags and n.location.valid():
            self._take('n', n.id, n.tags, n.location.lon, n.location.lat)

    def way(self, w):
        if not w.tags or not ('addr:housenumber' in w.tags or 'amenity' in w.tags
                              or any(k in w.tags for k in BUSINESS_KEYS)):
            return
        xy = [(r.location.lon, r.location.lat) for r in w.nodes if r.location.valid()]
        if xy:
            x, y = np.mean(xy, axis=0)
            self._take('w', w.id, w.tags, float(x), float(y))


def assign(lonlat, tree, to_m):
    """Points (n x 2 lon/lat) -> (footprint index or -1, 'in'/'near'/'none') per point; tree holds metric footprints."""
    pts = shapely.points(to_m(lonlat))
    idx = np.full(len(pts), -1)
    how = np.full(len(pts), 'none', dtype=object)
    pi, fi = tree.query(pts, predicate='within')
    idx[pi], how[pi] = fi, 'in'
    rest = np.flatnonzero(idx < 0)
    if len(rest):
        pi2, fi2 = tree.query_nearest(pts[rest], max_distance=NEAR_M, all_matches=False)
        idx[rest[pi2]], how[rest[pi2]] = fi2, 'near'
    return idx, how


def load_commune(con, admin, rel, area):
    """-> name, commune polygon, bbox (+~200 m), footprints whose centroid is inside (lon/lat), their ids, and
    to_m: lon/lat array -> local metres around the bbox centre."""
    name, w = con.execute(f"SELECT name, wkb FROM '{admin}' WHERE kind = 'commune' AND rel_id = {rel}").fetchone()
    ward = swkb.loads(bytes(w))
    bb = [round(v, 5) for v in ward.buffer(0.002).bounds]
    rows = con.execute(f"SELECT * FROM '{area}'").fetch_arrow_table().to_pylist()
    polys = [decode(r) for r in rows]
    keep = shapely.contains(ward, shapely.centroid(polys))
    lon0, lat0 = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    k = np.array([111320 * math.cos(math.radians(lat0)), 110574])
    return (name, ward, bb, np.array(polys, dtype=object)[keep], np.array([r['id'] for r in rows])[keep],
            lambda xy: (np.asarray(xy) - [lon0, lat0]) * k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commune-rel', type=int, required=True)
    ap.add_argument('--area', required=True, help='published by_area parquet that holds the commune')
    ap.add_argument('--pbf', required=True)
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--release', help='Overture release, default the latest')
    ap.add_argument('--fsq', metavar='DT', help='also Foursquare OS Places release dt (needs HF_TOKEN and accepted terms)')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial")
    name, ward, bb, fps, ids, to_m = load_commune(con, a.admin, a.commune_rel, a.area)
    tree = shapely.STRtree(shapely.transform(fps, to_m))
    print(name, '|', len(fps), 'footprints | bbox', bb, flush=True)

    release = a.release or get_json(f"{STAC}/catalog.json")['latest']
    srcs = fetch_overture(con, release, bb, a.out)
    h = OsmPoints(bb)
    h.apply_file(a.pbf, locations=True)
    print('osm points', len(h.rows), '|', round(time.time() - t0), 's', flush=True)

    tables = {'osm': h.rows}
    ov = con.execute(f"SELECT * FROM '{srcs['overture']}'").fetch_arrow_table().to_pylist()
    for r in ov:
        r['number'] = house_number(r['addr'])
    tables['overture'] = ov
    tables['ov_addr'] = [dict(r, category=None) for r in
                         con.execute(f"SELECT * FROM '{srcs['ov_addr']}'").fetch_arrow_table().to_pylist()]
    if a.fsq:
        fq = con.execute(f"SELECT * FROM '{fetch_fsq(con, a.fsq, bb, a.out)}'").fetch_arrow_table().to_pylist()
        for r in fq:
            r['number'] = house_number(r['addr'])
        tables['fsq'] = fq

    n_fp = len(fps)
    per_fp = collections.defaultdict(lambda: collections.defaultdict(dict))   # fp -> src -> {number, category}
    points = []                                                                # every point in the ward, for poi_number_agreement.py
    report = {'commune': name, 'osm_relation': a.commune_rel, 'footprints': n_fp, 'overture_release': release,
              'near_m': NEAR_M, 'sources': {}}
    for src, recs in tables.items():
        inside = shapely.contains_xy(ward.buffer(0.0005), [r['lon'] for r in recs], [r['lat'] for r in recs]) if recs else []
        recs = [r for r, ok in zip(recs, inside) if ok]
        st = {'points': len(recs), 'with_number': sum(bool(r.get('number')) for r in recs),
              'with_category': sum(bool(r.get('category')) for r in recs)}
        if recs:
            idx, how = assign(np.array([[r['lon'], r['lat']] for r in recs]), tree, to_m)
            st['matched'] = dict(collections.Counter(how))
            for r, f, hw in zip(recs, idx, how):
                points.append(dict(src=src, id=str(r['id']), lon=r['lon'], lat=r['lat'], number=r.get('number'),
                                   street=r.get('street'), addr=r.get('addr'), category=r.get('category'),
                                   confidence=r.get('confidence'), building=int(ids[f]) if f >= 0 else None, how=hw))
                if f < 0:
                    continue
                for key in ('number', 'category'):
                    if r.get(key):
                        d = per_fp[f][src]
                        if key not in d or (hw == 'in' and d.get('how') != 'in'):
                            d[key], d['how'] = r[key], hw
            st['fp_with_number'] = sum(1 for f in per_fp if per_fp[f][src].get('number'))
            st['fp_with_category'] = sum(1 for f in per_fp if per_fp[f][src].get('category'))
            st['fp_with_number_in'] = sum(1 for f in per_fp if per_fp[f][src].get('number') and per_fp[f][src].get('how') == 'in')
            if src in ('overture', 'fsq'):
                st['top_categories'] = collections.Counter(r['category'] for r in recs if r.get('category')).most_common(25)
                st['datasets'] = collections.Counter(d for r in recs for d in (r.get('datasets') or [])).most_common()
                if src == 'overture':
                    c = np.array([r['confidence'] or 0 for r in recs])
                    st['confidence'] = {k: int((c >= k).sum()) for k in (0.3, 0.5, 0.77, 0.9)}
            if src == 'osm':
                st['top_categories'] = collections.Counter(r['category'] for r in recs if r.get('category')).most_common(25)
        report['sources'][src] = st
        print(src, json.dumps(st, ensure_ascii=False)[:400], flush=True)

    any_num = {f for f, d in per_fp.items() if any(v.get('number') for v in d.values())}
    any_cat = {f for f, d in per_fp.items() if any(v.get('category') for v in d.values())}
    pct = lambda n: round(100 * n / n_fp, 2)
    both_src = [f for f, d in per_fp.items() if sum(bool(v.get('number')) for v in d.values()) >= 2]
    digits = lambda s: (re.match(r'\d+', str(s)) or [None])[0]
    agree = sum(1 for f in both_src if len({digits(v['number']) for v in per_fp[f].values() if v.get('number')}) == 1)
    report['union'] = {'fp_with_number': len(any_num), 'pct_number': pct(len(any_num)),
                       'fp_with_category': len(any_cat), 'pct_category': pct(len(any_cat)),
                       'fp_with_both': len(any_num & any_cat), 'pct_both': pct(len(any_num & any_cat)),
                       'fp_number_from_2plus_sources': len(both_src), 'of_those_same_number': agree}
    for src in report['sources']:
        s = report['sources'][src]
        if 'fp_with_number' in s:
            s['pct_fp_number'], s['pct_fp_category'] = pct(s['fp_with_number']), pct(s['fp_with_category'])
    with open(os.path.join(a.out, 'poi_coverage.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    pq.write_table(pa.Table.from_pylist(points), os.path.join(a.out, 'poi_points.parquet'))
    with open(os.path.join(a.out, 'poi_per_building.jsonl'), 'w', encoding='utf-8') as f:
        for i in sorted(per_fp):
            f.write(json.dumps({'id': int(ids[i]), **{s: v for s, v in per_fp[i].items() if v}}, ensure_ascii=False) + '\n')
    print(json.dumps(report['union'], ensure_ascii=False), '|', round(time.time() - t0), 's')


if __name__ == '__main__':
    sys.exit(main())
