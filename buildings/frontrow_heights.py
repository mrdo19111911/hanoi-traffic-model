"""Front-row houses of every file, and the front-row prior for Ha Noi: a house facing a street there has at least 3 floors.

This is a domain rule given by the user, not a measurement: almost every house on a Ha Noi street front today has
3 floors or more, while the 2.5D model (Sentinel-2, 4 m) under-reads narrow tube houses and the OSM levels of
front-row houses look under-tagged. The rule lifts calibrated or estimated heights only; OSM, building:part, Wikidata
and sibling heights are never touched.

build (needs the gitignored data/buildings/osm_roads.parquet and the raw layer): finds the front-row houses of every
  file (files_prefix; the whole country since 2026-10-27, for the front-row alpha of alpha_frontrow.py) and writes their ids, the rule and its constants to the committed buildings/frontrow_heights.json.
  A footprint (decoded as calibrate_heights.ring_geoms does, keyhole rings made valid) is a front-row house when
  - its area is 20-300 m2 (houses, not blocks or kiosks);
  - a street centre line lies within D = half-width + setback of its outline (nearest distance outline -> line):
    streets are OSM highway trunk / primary / secondary / tertiary (+ _link), at any name, and residential /
    unclassified / living_street carrying a name that does not start with Ngõ / Ngách / Hẻm / Kiệt (an unnamed
    residential way in Ha Noi is almost always an alley); bridges, tunnels, covered / indoor ways and area=yes are
    not streets; half-width = the class default, or lanes x 1.75 m when tagged and wider, clamped to 3-12 m (OSM
    lanes count motor lanes only: Nguyen Trai, lanes=2 per carriageway, has front houses 8-13 m out);
  - it is the first building along the shortest segment from its outline to that centre line: no other footprint
    crosses that segment by more than 0.1 m (shared walls and corners touch, they do not cross), so second-row houses
    behind a front house do not qualify.
  All constants are in the JSON (`params`).
apply (calibrate_heights.py apply, after calibration, Wikidata and sibling rows): in the files named
  min_floors_files_prefix (ha_noi_*) only, a listed row whose height_src is then g25cal or est gets height = max(height, 3 x 3.3 m) and floors = max(floors, 3); height_src becomes 'frontrow' only
  where the height or the floors change. Needs nothing but the raw layer and the committed JSON.

    .venv/Scripts/python.exe buildings/frontrow_heights.py build [--raw-dir DIR] [--workers 6]
"""
import argparse, json, os, sys, time, unicodedata
import numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(REPO, 'data', 'buildings', '_calib', 'raw_by_area')
ROADS = os.path.join(REPO, 'data', 'buildings', 'osm_roads.parquet')
OUT_JSON = os.path.join(REPO, 'buildings', 'frontrow_heights.json')

PARAMS = {
    'files_prefix': '',
    'min_floors_files_prefix': 'ha_noi_',
    'area_m2': [20.0, 300.0],
    'major_classes': ['trunk', 'primary', 'secondary', 'tertiary',
                      'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'],
    'named_classes': ['residential', 'unclassified', 'living_street'],
    'alley_name_prefixes': ['ngõ', 'ngách', 'hẻm', 'kiệt', 'ngo ', 'ngach ', 'hem ', 'kiet '],
    'lane_width_m': 3.5,
    'half_width_default_m': {'trunk': 10.0, 'primary': 8.0, 'secondary': 6.0, 'tertiary': 5.0, 'link': 4.0,
                             'residential': 3.0, 'unclassified': 3.0, 'living_street': 3.0},
    'half_width_clamp_m': [3.0, 12.0],
    'setback_m': 4.0,
    'cross_tolerance_m': 0.1,
    'not_streets': 'bridge, tunnel, covered, indoor or area tagged (anything but no / empty)',
    'min_floors': 3,
    'level_m': 3.3,
    'scope_height_src': ['g25cal', 'est'],
}
MIN_FLOORS = PARAMS['min_floors']
MIN_H_DM = int(round(MIN_FLOORS * PARAMS['level_m'] * 10))     # 99 dm = 9.9 m


# ---------------------------------------------------------------------------------------------------- roads

def is_alley_name(name):
    if not name:
        return True
    s = unicodedata.normalize('NFC', name).strip().lower()
    return s.startswith(tuple(PARAMS['alley_name_prefixes']))


def half_width(hw, lanes):
    lo, hi = PARAMS['half_width_clamp_m']
    d = PARAMS['half_width_default_m']
    h = d['link'] if hw.endswith('_link') else d[hw]
    try:
        n = float(lanes) if lanes not in (None, '') else None
    except ValueError:
        n = None
    if n is not None and n > 0:                      # tagged lanes count motor lanes only (no mixed / motorbike lane)
        h = max(h, n * PARAMS['lane_width_m'] / 2)
    return float(min(max(h, lo), hi))


def load_streets(bbox):
    """Street centre lines inside lon/lat bbox (W, S, E, N): (WKB list, reach D in m)."""
    cols = ['hw', 'lanes', 'bridge', 'tunnel', 'covered', 'indoor', 'area', 'name', 'geometry']
    t = pq.read_table(ROADS, columns=cols)
    hw = np.array(t['hw'].to_pylist(), object)
    keep = np.isin(hw, PARAMS['major_classes'] + PARAMS['named_classes'])
    for c in ('bridge', 'tunnel', 'covered', 'indoor', 'area'):
        v = np.array(t[c].to_pylist(), object)
        keep &= np.array([x in (None, '', 'no') for x in v])
    idx = np.flatnonzero(keep)
    t = t.take(pa.array(idx))
    hw = hw[idx]
    names = t['name'].to_pylist(); lanes = t['lanes'].to_pylist()
    g = shapely.from_wkb(t['geometry'].to_numpy(zero_copy_only=False))
    b = shapely.bounds(g)
    W, S, E, N = bbox
    ok = (b[:, 2] >= W) & (b[:, 0] <= E) & (b[:, 3] >= S) & (b[:, 1] <= N)
    out_g, out_d = [], []
    named = set(PARAMS['named_classes'])
    for k in np.flatnonzero(ok):
        if hw[k] in named and is_alley_name(names[k]):
            continue
        out_g.append(shapely.to_wkb(g[k])); out_d.append(half_width(hw[k], lanes[k]) + PARAMS['setback_m'])
    return out_g, np.array(out_d, float)


# ---------------------------------------------------------------------------------------------------- footprints

def common_frame(tbl):
    """Footprints of a compact table in one metric frame per file (equirectangular around the file's mean
    position), as shapely geometries (keyhole rings made valid), plus the projection (lon0, lat0, kx, ky)."""
    from calibrate_heights import ring_geoms
    geoms, kx_row, ky_row = ring_geoms(tbl)
    lon = tbl['lon_e6'].to_numpy().astype(np.float64) / 1e6
    lat = tbl['lat_e6'].to_numpy().astype(np.float64) / 1e6
    lon0, lat0 = float(lon.mean()), float(lat.mean())
    kx, ky = 111320 * np.cos(np.radians(lat0)), 110574.0
    xy, ri = shapely.get_coordinates(geoms, return_index=True)
    lo = lon[ri] + xy[:, 0] / (kx_row[ri] * 1e6)
    la = lat[ri] + xy[:, 1] / (ky_row[ri] * 1e6)
    geoms = shapely.set_coordinates(geoms.copy(), np.column_stack([(lo - lon0) * kx, (la - lat0) * ky]))
    bad = ~shapely.is_valid(geoms)
    if bad.any():
        geoms[bad] = shapely.make_valid(geoms[bad])
    return geoms, (lon0, lat0, kx, ky)


def frontrow_ids(tbl, street_wkb, street_reach):
    """Ids (sorted) of the front-row houses of one compact table, plus counts."""
    geoms, (lon0, lat0, kx, ky) = common_frame(tbl)
    ids = tbl['id'].to_numpy()
    area = shapely.area(geoms)
    lo, hi = PARAMS['area_m2']
    house = np.flatnonzero((area >= lo) & (area <= hi))
    det = {'buildings': int(len(ids)), 'houses_20_300': int(len(house)), 'near_street': 0, 'frontrow': 0}
    if not len(house) or not len(street_wkb):
        return [], det
    roads = shapely.from_wkb(np.array(street_wkb, object))
    xy, ri = shapely.get_coordinates(roads, return_index=True)
    roads = shapely.set_coordinates(roads, np.column_stack([(xy[:, 0] - lon0) * kx, (xy[:, 1] - lat0) * ky]))
    gb = shapely.bounds(geoms)
    rb = shapely.bounds(roads)
    m = 50.0
    near = (rb[:, 2] >= gb[:, 0].min() - m) & (rb[:, 0] <= gb[:, 2].max() + m) & \
           (rb[:, 3] >= gb[:, 1].min() - m) & (rb[:, 1] <= gb[:, 3].max() + m)
    roads, reach = roads[near], street_reach[near]
    if not len(roads):
        return [], det
    rtree = shapely.STRtree(roads)
    hi_idx, ro_idx = rtree.query(geoms[house], predicate='dwithin', distance=float(reach.max()))
    hb = house[hi_idx]
    d = shapely.distance(geoms[hb], roads[ro_idx])
    ok = d <= reach[ro_idx]
    hb, ro_idx = hb[ok], ro_idx[ok]
    det['near_street'] = int(len(np.unique(hb)))
    if not len(hb):
        return [], det
    seg = shapely.shortest_line(geoms[hb], roads[ro_idx])
    btree = shapely.STRtree(geoms)
    si, bi = btree.query(seg, predicate='intersects')
    other = bi != hb[si]
    si, bi = si[other], bi[other]
    cross = shapely.length(shapely.intersection(seg[si], geoms[bi])) > PARAMS['cross_tolerance_m']
    blocked = np.zeros(len(seg), bool)
    blocked[si[cross]] = True
    front = np.unique(hb[~blocked])
    det['frontrow'] = int(len(front))
    return sorted(int(v) for v in ids[front]), det


def _build_file(args):
    path, street_wkb, street_reach = args
    t = pq.read_table(path, columns=['id', 'lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring'])
    ids, det = frontrow_ids(t, street_wkb, street_reach)
    return os.path.basename(path), ids, det


def cmd_build(a):
    from concurrent.futures import ProcessPoolExecutor
    t0 = time.time()
    idx = json.load(open(os.path.join(a.raw_dir, 'index.json'), encoding='utf-8'))
    ents = [e for e in idx['files'] if e['file'].startswith(PARAMS['files_prefix'])]
    if a.files:
        ents = [e for e in ents if e['file'] in a.files]
    bb = np.array([e['bbox'] for e in ents], float)
    bbox = (bb[:, 0].min() - 0.01, bb[:, 1].min() - 0.01, bb[:, 2].max() + 0.01, bb[:, 3].max() + 0.01)
    wkb, reach = load_streets(bbox)
    print(f'files {len(ents)} | streets {len(wkb)} | {time.time() - t0:.0f}s', flush=True)
    out_ids, counts = {}, {}
    sb = shapely.bounds(shapely.from_wkb(np.array(wkb, object)))
    args = []
    for e in ents:                                   # streets near this file only (0.002 deg ~ 200 m margin)
        eb = e['bbox']
        k = np.flatnonzero((sb[:, 2] >= eb[0] - 0.002) & (sb[:, 0] <= eb[2] + 0.002) &
                           (sb[:, 3] >= eb[1] - 0.002) & (sb[:, 1] <= eb[3] + 0.002))
        args.append((os.path.join(a.raw_dir, e['file']), [wkb[i] for i in k], reach[k]))
    with ProcessPoolExecutor(min(a.workers, 6)) as ex:
        for name, ids, det in ex.map(_build_file, args):
            out_ids[name] = ids; counts[name] = det
            print(f'{name}: {det} | {time.time() - t0:.0f}s', flush=True)
    names = sorted(out_ids)
    total = {k: sum(counts[n][k] for n in names) for k in ('buildings', 'houses_20_300', 'near_street', 'frontrow')}
    R = {
        'rule': ('Domain rule from the user, not a measurement: a house on a Ha Noi street front has at least '
                 f'{MIN_FLOORS} floors. calibrate_heights.py apply, after calibration, Wikidata and sibling rows: a row '
                 'of a file named min_floors_files_prefix listed below whose height_src is then g25cal or est gets height = max(height, '
                 f'{MIN_FLOORS} x {PARAMS["level_m"]} m) and floors = max(floors, {MIN_FLOORS}); height_src frontrow '
                 'where the height or the floors change. osm / osm_part / wikidata / sibling rows are never touched.'),
        'frontrow_definition': ('footprint area within area_m2; a street centre line within half-width + setback_m of '
                                'the outline (nearest distance); streets = major_classes at any name, named_classes '
                                'with a name not starting with alley_name_prefixes (case-insensitive; unnamed ones are '
                                'alleys); not_streets excluded; half-width = max(half_width_default_m, lanes x lane_width_m / '
                                '2 when tagged), clamped to half_width_clamp_m; the shortest segment from the '
                                'outline to that centre line crosses no other footprint by more than cross_tolerance_m. '
                                'Built by buildings/frontrow_heights.py build from the raw layer and osm_roads.parquet.'),
        'params': PARAMS,
        'totals': total,
        'counts': {n: counts[n] for n in names},
        'ids_encoding': 'per file: sorted building ids as deltas (first value is the id itself)',
        'ids': {n: np.diff(np.r_[0, out_ids[n]]).astype(int).tolist() for n in names},
    }
    with open(a.out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(json.dumps({k: v for k, v in R.items() if k != 'ids'}, ensure_ascii=False, indent=1)[:-2])
        fh.write(',\n "ids": {\n')
        fh.write(',\n'.join(f'  {json.dumps(n)}: {json.dumps(R["ids"][n], separators=(",", ":"))}' for n in names))
        fh.write('\n }\n}\n')
    print(f'{total} | {os.path.getsize(a.out)} bytes | {time.time() - t0:.0f}s')


# ---------------------------------------------------------------------------------------------------- apply

def load_frontrow(path):
    """{file: sorted int64 array of front-row ids} from frontrow_heights.json."""
    R = json.load(open(path, encoding='utf-8'))
    if any(R['params'].get(k) != PARAMS[k] for k in ('min_floors', 'level_m', 'min_floors_files_prefix')):
        raise RuntimeError(f'{path}: min_floors / level_m / min_floors_files_prefix differ from frontrow_heights.py')
    return {n: np.cumsum(np.array(d, np.int64)) for n, d in R['ids'].items()}


def min_floors_applies(name):
    """True for the files the front-row minimum covers (Ha Noi); the ids of the other files serve the front-row alpha."""
    return bool(name) and name.startswith(PARAMS['min_floors_files_prefix'])


def frontrow_rows(ids, new_src, new_dm, new_fl, fr_ids):
    """Positions of rows the rule changes: listed ids whose source is g25cal / est and whose height (dm) or floors
    are under the minimum."""
    if fr_ids is None or not len(fr_ids):
        return np.zeros(0, int)
    listed = np.isin(ids, fr_ids)
    scope = np.isin(new_src, PARAMS['scope_height_src'])
    return np.flatnonzero(listed & scope & ((new_dm < MIN_H_DM) | (new_fl < MIN_FLOORS)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('build', help='front-row house ids of every file -> buildings/frontrow_heights.json')
    p.add_argument('--raw-dir', default=RAW_DIR)
    p.add_argument('--out', default=OUT_JSON)
    p.add_argument('--files', nargs='*')
    p.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()
    {'build': cmd_build}[a.cmd](a)


if __name__ == '__main__':
    main()
