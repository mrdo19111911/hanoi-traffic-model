"""Tower heights from shadows in Esri World Imagery (PILOT, Ha Noi only). Nothing here writes to buildings/by_area.

    .venv/Scripts/python.exe buildings/shadow_heights.py prep      # Ha Noi frame -> data/buildings/_shadow/hanoi.pkl
    .venv/Scripts/python.exe buildings/shadow_heights.py meta      # Esri imagery capture polygons (z19) -> _shadow/patches.json
    .venv/Scripts/python.exe buildings/shadow_heights.py run [--crops DIR] [--max-tiles N]
                                                                   # azimuth, k, measurement, LOO -> _shadow/result.json
    .venv/Scripts/python.exe buildings/shadow_heights.py dryrun    # what the shadow anchors would do in the tall stage

Method (all decisions are rules of this file; the thresholds are the constants below):
  1. Imagery: Esri World Imagery tiles at z19 (~0.28 m/px in Ha Noi), z18 where the imagery stops at 18, fetched once
     with a User-Agent naming the repo, at most RATE requests per second, cached in data/buildings/_shadow/tiles
     (gitignored, never committed). Only tiles around towers are fetched. Patches: the imagery's own metadata layer
     (World_Imagery MapServer layer 0, a flat mosaic of capture polygons), grouped by (date, sensor, zoom); one patch
     is one satellite image, so one sun and one view geometry. A corridor leaving its patch is rejected (seam).
  2. Calibration towers: real height (osm / osm_part / wikidata; OSM rows only when clean in calibrate_heights
     truth_frame) and raw h25 >= CAND_H25. Candidates: no real height (not osm / osm_part / wikidata / sibling) and raw
     h25 >= CAND_H25 or footprint >= CAND_AREA; measured only with raw h25 >= --cand-min-h25 (default CAND_H25: a
     large footprint with a low h25 is a hall whose shadow is too short to matter).
  3. Roof registration. Esri is not true-ortho: a point at height t is drawn at base + o + t w (o: offset of the
     published footprints against this image, w: lean per metre of height, both constant in a patch). The roof outline
     is found by sliding the footprint outline over the image edge map (FFT correlation). Per patch, a free search on
     calibration towers gives shifts that are fitted robustly as o + h w (outliers are towers locked onto an identical
     neighbour); Google / Microsoft footprints get their own o from mid-rise buildings of that source (shift - h w).
     Then every tower is searched only along its lean segment (o + t w, t in a height range) within REG_PERP_M.
  4. The shadow tip of a roof edge is at roof + h (s - w) (s: shadow per metre of height): measured from the
     registered roof, the dark run along the apparent shadow direction is proportional to the height, whatever the
     footprint's own registration or the roof lean. Its azimuth per patch comes from the image: the darkness of a short
     strip outside each registered calibration roof per direction, curves centred and summed, peak (no acquisition time
     needed). Profiles parallel to it across the roof: for a flat-roofed prism every profile ends at the same length
     past its exit point; the result is the longest consensus cluster of profiles (lower podium parts give shorter
     clusters). A profile is rejected when it does not start dark, runs past the corridor, ends on dark ground, or its
     tip lies on another footprint or leaning roof (low houses included).
  5. k (height / run length) per patch: median over calibration towers that are ok and open (rule 6), >= K_MIN_TOWERS;
     height of a candidate = k x run. Leave-one-out within the patch validates it.
  6. "Easy" towers: the corridor (registered roof swept along the shadow azimuth by CORRIDOR_MARGIN x expected length
     + CORRIDOR_PAD_M, plus the footprint) touches no water, stays in its patch, and no other building of height
     >= max(OBST_MIN_H, OBST_SHARE x expected height) covers it with its footprint or leaning roof. One candidate per
     complex: greedy pick by clearness (free distance in a corridor twice as long), suppressing others within COMPLEX_M.
  7. A patch is used only when its azimuth is clear (>= AZ_MIN_AGREE of towers within 15 deg) and its k values agree
     (spread <= K_MAX_SPREAD). A candidate is also rejected when its roof shift along the lean line (a second, noisier
     height reading) disagrees with the shadow height by more than LEAN_TOL.
"""
import argparse, glob, io, json, math, os, pickle, sys, time, urllib.error, urllib.parse, urllib.request
import numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely
from scipy.ndimage import map_coordinates, median_filter
from scipy.spatial import cKDTree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate_heights as CH

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(REPO, 'data', 'buildings', '_shadow')
TILES = os.path.join(WORK, 'tiles')
BY_AREA = os.path.join(REPO, 'buildings', 'by_area')
RAW_DIR = os.path.join(REPO, 'data', 'buildings', '_calib', 'raw_by_area')
NAMES = os.path.join(REPO, 'data', 'buildings', '_extra', 'names.parquet')
WATER = os.path.join(REPO, 'data', 'buildings', 'osm_water.parquet')
AREA_GLOB = 'ha_noi_*.parquet'
UA = 'hanoi-traffic-model/1.0 (https://github.com/mrdo19111911/hanoi-traffic-model; shadow height pilot; contact via GitHub issues)'
TILE_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'
META_URL = 'https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/0/query'
Z = 19                                   # preferred zoom; a patch whose imagery stops at 18 is read at 18
Z_MIN = 18
RATE = 2.5                               # requests per second (task limit: <= 3)
EQ_RES = 0.29858214164761665             # metres per pixel at the equator, z19

REAL_SRC = ('osm', 'osm_part', 'wikidata')
NOT_CAND_SRC = ('osm', 'osm_part', 'wikidata', 'sibling')
CAND_H25, CAND_AREA = 25.0, 400.0
KEEP_H = 10.0                            # footprints kept as possible obstacles: height >= this (m)

# Azimuth search
AZ_STEP, AZ_FINE = 3.0, 0.5              # degrees
AZ_NEAR = (1.0, 0.25)                    # strip: from 1 m past the footprint to 0.25 x height (always in shadow)
AZ_MIN_TOWERS = 5
AZ_MIN_AGREE = 0.5                       # a patch is used only when >= this share of towers peak within 15 deg of it
AZ_MAX_TOWERS = 120                       # per patch, tallest real heights first (tile budget)
# Measurement
PROFILE_SPACING_M = 1.5
PROFILE_MARGIN = 0.1                     # share of the footprint's cross width left out at each side
MIN_PROFILES = 5
SMOOTH_PX = 5                            # running median along a profile (~1.4 m)
THR_SHARE = 0.5                          # dark: below shadow + this x (ground - shadow)
BRIGHT_END_SHARE = 0.7                   # past the tip the ground must be brighter than shadow + this x (ground - shadow)
GAP_M = 2.0                              # bright gaps up to this inside the shadow are bridged (cars, markings)
BRIGHT_RUN_M = 3.0                       # a bright stretch this long ends the shadow
START_M = (5.0, 0.25)                    # the dark run must start within max(5 m, 0.25 x expected length) of the exit
MIN_CONTRAST = 0.30                      # (ground - shadow) / ground
MIN_VALID_SHARE = 0.5                    # share of profiles with a valid tip
CLUSTER_ABS_M, CLUSTER_REL = 1.0, 0.03   # profiles agree when within max(1 m, 3 %) of each other
MIN_CLUSTER, CLUSTER_SHARE = 4, 0.3      # a consensus needs this many profiles and this share of all profiles
K_MIN_TOWERS = 10
K_MAX_SPREAD = 0.15                      # and when (p75 - p25) / median of the calibration k values is <= this
TIP_EDGE_M = 1.0                         # a tip within this of a receiving roof's edge is ambiguous (ground or roof)
ROOF_MAX_SHARE = 0.4                     # receiving roofs: known height <= this x the tower's expected height
TIP_CLEAR_M = 0.5                        # a tip closer than this to another footprint or leaning roof ends on a building
# Easy towers
OBST_MIN_H, OBST_SHARE = 10.0, 0.2
CORRIDOR_MARGIN = 1.5                    # corridor length = expected length x this (+ CORRIDOR_PAD_M)
CORRIDOR_PAD_M = 15.0
EXP_H_FACTOR = 1.5                       # expected height of a candidate = this x max(current height, raw h25); calib: real
COMPLEX_M = 150.0
WINDOW_PAD_M = 15.0


# ---------------------------------------------------------------------------------------------------- data

NEAR_TOWER_M = 400.0                     # every footprint within this of a tower (raw h25 or height >= CAND_H25) is kept


def abs_polygons(pub, rows):
    """Published footprints of the given rows as lon/lat polygons (outer ring)."""
    g, kx, ky = CH.ring_geoms(pub.take(rows))
    x0 = pub['lon_e6'].to_numpy()[rows] / 1e6; y0 = pub['lat_e6'].to_numpy()[rows] / 1e6
    co, idx = shapely.get_coordinates(shapely.get_exterior_ring(g), return_index=True)
    xy = np.column_stack([x0[idx] + co[:, 0] / kx[idx] / 1e6, y0[idx] + co[:, 1] / ky[idx] / 1e6])
    return shapely.polygons(shapely.linearrings(xy, indices=idx))


def cmd_prep(a):
    """Ha Noi frame: id, file, lon/lat, area, published height and source, footprint source, raw h25 (raw layer g25
    rows; OSM rows from truth.parquet), clean flag of truth rows, name, and the lon/lat footprint, for buildings with
    height or raw h25 >= KEEP_H or area >= CAND_AREA, and for every building within NEAR_TOWER_M of a tower (the
    shadow tip must not end on a roof, low houses included)."""
    t0 = time.time()
    T = CH.truth_frame()
    th25 = {(f, int(i)): (float(h), bool(c)) for f, i, h, c in zip(T['file'], T['id'], T['h25'], T['clean'])}
    names = dict(zip(*[pq.read_table(NAMES, columns=['id', 'name'])[c].to_pylist() for c in ('id', 'name')]))
    per = []
    for path in sorted(glob.glob(os.path.join(BY_AREA, AREA_GLOB))):
        f = os.path.basename(path)
        pub = pq.read_table(path)
        raw = pq.read_table(os.path.join(RAW_DIR, f), columns=['id', 'height_dm', 'height_src'])
        ids = pub['id'].to_numpy()
        assert np.array_equal(ids, raw['id'].to_numpy()), f
        F = CH.features(pub)
        src = CH.src_strings(pub)
        fsrc = np.array(pub['src'].cast(pa.string()).to_pylist(), object)
        h = pub['height_dm'].to_numpy(zero_copy_only=False).astype(float) / 10
        rsrc = CH.src_strings(raw)
        rh = raw['height_dm'].to_numpy(zero_copy_only=False).astype(float) / 10
        h25 = np.where(rsrc == 'g25', rh, np.nan)
        clean = np.zeros(len(ids), bool)
        for i in np.flatnonzero(np.isin(src, ('osm', 'osm_part'))):
            v = th25.get((f, int(ids[i])))
            if v:
                h25[i] = v[0] if v[0] >= 2 else np.nan
                clean[i] = v[1]
        per.append({'file': f, 'id': ids, 'lon': F['lon'], 'lat': F['lat'], 'area': F['area'], 'length': F['length'],
                    'width': F['width'], 'h': h, 'src': src, 'h25': h25, 'clean': clean, 'fsrc': fsrc})
        print(f, len(ids), f'{time.time() - t0:.0f}s', flush=True)
    tw = np.vstack([CH.xyz(e['lon'], e['lat'])[(np.nan_to_num(e['h25']) >= CAND_H25) | (np.nan_to_num(e['h']) >= CAND_H25)] for e in per])
    ttree = cKDTree(tw)
    cols = {k: [] for k in ('id', 'file', 'lon', 'lat', 'area', 'length', 'width', 'h', 'src', 'h25', 'clean', 'fsrc', 'geom')}
    for e in per:
        near = ttree.query(CH.xyz(e['lon'], e['lat']), distance_upper_bound=NEAR_TOWER_M)[0] <= NEAR_TOWER_M
        keep = (np.nan_to_num(e['h']) >= KEEP_H) | (np.nan_to_num(e['h25']) >= KEEP_H) | (e['area'] >= CAND_AREA) | near
        ki = np.flatnonzero(keep)
        pub = pq.read_table(os.path.join(BY_AREA, e['file']))
        cols['geom'].append(shapely.to_wkb(abs_polygons(pub, ki)))
        cols['file'].append(np.full(len(ki), e['file'], object))
        for k in ('id', 'lon', 'lat', 'area', 'length', 'width', 'h', 'src', 'h25', 'clean', 'fsrc'):
            cols[k].append(e[k][ki])
        print(e['file'], 'kept', len(ki), f'{time.time() - t0:.0f}s', flush=True)
    D = {k: np.concatenate(v) for k, v in cols.items()}
    D['wkb'] = D.pop('geom')
    D['name'] = np.array([names.get(int(i)) for i in D['id']], object)
    os.makedirs(WORK, exist_ok=True)
    pickle.dump(D, open(os.path.join(WORK, 'hanoi.pkl'), 'wb'))
    print('kept', len(D['id']), f'{time.time() - t0:.0f}s')


def load_frame():
    D = pickle.load(open(os.path.join(WORK, 'hanoi.pkl'), 'rb'))
    g = shapely.from_wkb(D['wkb'])
    bad = ~shapely.is_valid(g)
    g[bad] = [max(shapely.get_parts(shapely.make_valid(x)), key=lambda q: q.area if q.geom_type == 'Polygon' else -1) for x in g[bad]]
    D['geom'] = g
    return D


# ---------------------------------------------------------------------------------------------------- network

_last = [0.0]
STATS = {'requests': 0, 'tiles_fetched': 0, 'tiles_cached': 0, 'missing': 0, 't_first': None, 't_last': None}


def polite_get(url):
    """GET with the repo User-Agent, at most RATE requests per second (sequential)."""
    wait = _last[0] + 1.0 / RATE - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    STATS['requests'] += 1
    STATS['t_first'] = STATS['t_first'] or _last[0]
    STATS['t_last'] = _last[0]
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        if r.geturl() != url:                   # a followed redirect (Wayback tiles) was a second HTTP request
            STATS['requests'] += 1
            _last[0] += 1.0 / RATE
            STATS['t_last'] = _last[0]
        return body


def cmd_meta(a):
    """Capture polygons of the z19 imagery over Ha Noi (MaxMapLevel >= 19), paged, cached in patches.json."""
    out = os.path.join(WORK, 'patches.json')
    if os.path.exists(out):
        print('cached', out); return
    D = load_frame()
    box = (D['lon'].min() - 0.01, D['lat'].min() - 0.01, D['lon'].max() + 0.01, D['lat'].max() + 0.01)
    feats, off = [], 0
    while True:
        q = {'f': 'json', 'geometry': ','.join(f'{v:.4f}' for v in box), 'geometryType': 'esriGeometryEnvelope',
             'inSR': 4326, 'outSR': 4326, 'spatialRel': 'esriSpatialRelIntersects', 'where': f'MaxMapLevel>={Z_MIN} AND MinMapLevel<={Z_MIN}',
             'outFields': 'OBJECTID,SRC_DATE,SRC_RES,SRC_ACC,SRC_DESC,NICE_NAME,MinMapLevel,MaxMapLevel,DrawOrder,BlockName',
             'returnGeometry': 'true', 'geometryPrecision': 6, 'resultOffset': off, 'resultRecordCount': 200}
        d = json.loads(polite_get(META_URL + '?' + urllib.parse.urlencode(q)))
        feats += d.get('features', [])
        if not d.get('exceededTransferLimit'):
            break
        off += len(d['features'])
    json.dump({'box': box, 'features': feats, 'requests': STATS['requests']}, open(out, 'w'))
    print(len(feats), 'polygons;', STATS['requests'], 'requests')


WAYBACK_CONFIG = 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json'
WB_DIR = os.path.join(WORK, 'wayback')
WB_LAYERS = {19: '30cm Resolution Metadata'}   # z18 (60 cm) captures failed every gate in round 1 and their metadata queries take minutes


def cmd_wbmeta(a):
    """Esri World Imagery Wayback: the release list (waybackconfig.json) and, per release, the capture polygons of its
    metadata service at the z19 (30 cm) and z18 (60 cm) levels over Ha Noi. Every response is cached in
    data/buildings/_shadow/wayback/ and never fetched again."""
    os.makedirs(WB_DIR, exist_ok=True)
    cfg_path = os.path.join(WB_DIR, 'waybackconfig.json')
    if not os.path.exists(cfg_path):
        open(cfg_path, 'wb').write(polite_get(WAYBACK_CONFIG))
    cfg = json.load(open(cfg_path))
    box = json.load(open(os.path.join(WORK, 'patches.json')))['box']
    for rel, e in sorted(cfg.items(), key=lambda kv: kv[1]['itemTitle']):
        out = os.path.join(WB_DIR, f'meta_{rel}.json')
        if os.path.exists(out):
            continue
        svc = e['metadataLayerUrl'].rstrip('/')
        try:
            layers = json.loads(polite_get(svc + '?f=json'))['layers']
        except Exception as ex:                                  # a missing service is recorded, not retried
            json.dump({'release': rel, 'title': e['itemTitle'], 'error': type(ex).__name__}, open(out, 'w')); continue
        res = {'release': rel, 'title': e['itemTitle'], 'layers': {}}
        for z, name in WB_LAYERS.items():
            lid = next((l['id'] for l in layers if l['name'] == name), None)
            if lid is None:
                continue
            feats, off = [], 0
            while True:
                q = {'f': 'json', 'geometry': ','.join(f'{v:.4f}' for v in box), 'geometryType': 'esriGeometryEnvelope',
                     'inSR': 4326, 'outSR': 4326, 'spatialRel': 'esriSpatialRelIntersects', 'where': '1=1',
                     'outFields': 'OBJECTID,SRC_DATE,SRC_RES,SRC_ACC,SAMP_RES,SRC_DESC,MinMapLevel,MaxMapLevel,NICE_NAME',
                     'returnGeometry': 'true', 'geometryPrecision': 5, 'maxAllowableOffset': 0.00002, 'resultOffset': off, 'resultRecordCount': 1000}
                try:
                    d = json.loads(polite_get(f'{svc}/{lid}/query?' + urllib.parse.urlencode(q)))
                except Exception as ex:                  # timeout: this release is retried on the next call (nothing saved)
                    print(rel, 'failed', type(ex).__name__, flush=True); res = None; break
                seen = {f_['attributes'].get('OBJECTID') for f_ in feats}
                new = [f_ for f_ in d.get('features', []) if f_['attributes'].get('OBJECTID') not in seen]
                feats += new
                if not d.get('exceededTransferLimit') or not new or off > 20000:   # a service ignoring resultOffset repeats pages
                    break
                off += len(d['features'])
            if res is None:
                break
            res['layers'][str(z)] = feats
        if res is None:
            continue
        json.dump(res, open(out, 'w'))
        print(rel, e['itemTitle'], {z: len(v) for z, v in res['layers'].items()}, STATS['requests'], flush=True)


def load_patches():
    """Patches: metadata polygons (a flat, non-overlapping mosaic) grouped by (SRC_DATE, SRC_DESC, zoom) and unioned:
    pieces of one satellite image share its sun and view geometry. zoom = min(Z, MaxMapLevel). Largest first."""
    d = json.load(open(os.path.join(WORK, 'patches.json')))
    groups = {}
    for f in d['features']:
        a = f['attributes']
        rings = [shapely.LinearRing(r) for r in f['geometry']['rings'] if len(r) >= 4]
        # Esri rings: clockwise = outer, counter-clockwise = hole.
        outer = [shapely.Polygon(r) for r in rings if not r.is_ccw]
        holes = [shapely.Polygon(r) for r in rings if r.is_ccw]
        g = shapely.union_all(outer) if outer else shapely.Polygon()
        if holes:
            g = g.difference(shapely.union_all(holes))
        key = (a['SRC_DATE'], a['SRC_DESC'], min(Z, a['MaxMapLevel']))
        e = groups.setdefault(key, {'parts': [], 'objectids': [], 'attr': a, 'zoom': key[2]})
        e['parts'].append(shapely.make_valid(g)); e['objectids'].append(a['OBJECTID'])
    P = []
    for (date, sensor, z), e in groups.items():
        g = shapely.make_valid(shapely.union_all(e['parts']))
        P.append({'id': f'{date}_{sensor}_z{z}', 'attr': {k: e['attr'][k] for k in ('SRC_DATE', 'SRC_DESC', 'SRC_RES', 'SRC_ACC', 'NICE_NAME', 'BlockName')},
                  'zoom': e['zoom'], 'pieces': len(e['parts']), 'geom': g})
    P.sort(key=lambda p: -p['geom'].area)
    for p in P:
        shapely.prepare(p['geom'])
    return P


# ---------------------------------------------------------------------------------------------------- imagery

def lonlat_to_px(lon, lat, z=Z):
    """Global pixel coordinates at zoom z (web mercator)."""
    n = 256 * 2 ** z
    lat = np.clip(lat, -85, 85)
    x = (np.asarray(lon) + 180.0) / 360.0 * n
    y = (1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / np.pi) / 2 * n
    return x, y


def px_to_lonlat(x, y, z=Z):
    n = 256 * 2 ** z
    lon = np.asarray(x) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * np.asarray(y) / n))))
    return lon, lat


SOURCE = {'rel': None}                   # tile source of the patch being processed: None = live service, else a Wayback release
WB_TILE = 'https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/{rel}/{z}/{y}/{x}'


def tile_path(x, y, z):
    rel = SOURCE['rel']
    root = TILES if rel is None else os.path.join(TILES, f'wb{rel}')
    return os.path.join(root, str(z), str(x), f'{y}.jpg')


def get_tile(x, y, z, budget):
    """Tile (256 x 256 x 3 uint8) from the cache, else fetched once (when the budget allows). None if unavailable."""
    from PIL import Image
    p = tile_path(x, y, z)
    if os.path.exists(p):
        STATS['tiles_cached'] += 1
        return np.asarray(Image.open(p).convert('RGB'))
    if os.path.exists(p[:-4] + '.none'):
        return None
    if budget is not None and STATS['tiles_fetched'] >= budget:
        raise BudgetExceeded()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        url = TILE_URL.format(z=z, y=y, x=x) if SOURCE['rel'] is None else WB_TILE.format(rel=SOURCE['rel'], z=z, y=y, x=x)
        b = polite_get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            open(p[:-4] + '.none', 'w').close(); STATS['missing'] += 1
            return None
        raise
    STATS['tiles_fetched'] += 1
    im = Image.open(io.BytesIO(b)).convert('RGB')
    with open(p, 'wb') as fh:
        fh.write(b)
    return np.asarray(im)


class BudgetExceeded(Exception):
    pass


def window(x0, y0, x1, y1, z, budget=None):
    """RGB mosaic covering global pixel box [x0, x1] x [y0, y1]; returns (rgb, gray, ox, oy) with ox, oy the global
    pixel of rgb[0, 0]."""
    tx0, ty0, tx1, ty1 = int(x0 // 256), int(y0 // 256), int(x1 // 256), int(y1 // 256)
    rgb = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256, 3), np.uint8)
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            t = get_tile(tx, ty, z, budget)
            if t is not None:
                rgb[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = t
    gray = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    return rgb, gray, tx0 * 256, ty0 * 256


def tiles_needed(boxes):
    s = set()
    for x0, y0, x1, y1 in boxes:
        for ty in range(int(y0 // 256), int(y1 // 256) + 1):
            for tx in range(int(x0 // 256), int(x1 // 256) + 1):
                s.add((tx, ty))
    return s


# ---------------------------------------------------------------------------------------------------- geometry

def poly_px(g, z):
    """Footprint (lon/lat polygon) -> pixel polygon at zoom z."""
    co = shapely.get_coordinates(g.exterior)
    x, y = lonlat_to_px(co[:, 0], co[:, 1], z)
    return shapely.Polygon(np.column_stack([x, y]))


def mpp(lat, z):
    return EQ_RES * 2 ** (Z - z) * math.cos(math.radians(lat))


def direction(theta):
    """Unit vector of azimuth theta (degrees clockwise from north) in pixel coordinates (x right, y down), and its
    perpendicular."""
    t = math.radians(theta)
    return np.array([math.sin(t), -math.cos(t)]), np.array([math.cos(t), math.sin(t)])


def corridor(fp, theta, length_px):
    """Footprint swept along theta by length_px (convex hull of the footprint and its translate; exact for convex
    footprints, a cover otherwise)."""
    d, _ = direction(theta)
    moved = shapely.affinity.translate(fp, d[0] * length_px, d[1] * length_px)
    return shapely.convex_hull(shapely.union(fp, moved))


import shapely.affinity  # noqa: E402


def profile_lines(fp, theta, spacing_px):
    """Offsets across the footprint (perpendicular to theta) and, per offset, the exit point (largest s where the line
    is inside the footprint). Returns list of (origin xy of s = 0 at the exit point, d)."""
    d, p = direction(theta)
    co = np.asarray(fp.exterior.coords)
    c = co.mean(axis=0)
    t = (co - c) @ p
    t0, t1 = t.min(), t.max()
    w = t1 - t0
    lo, hi = t0 + PROFILE_MARGIN * w, t1 - PROFILE_MARGIN * w
    n = max(MIN_PROFILES, int((hi - lo) / spacing_px) + 1)
    out = []
    big = 1e4
    for tt in np.linspace(lo, hi, n):
        a = c + tt * p
        line = shapely.LineString([a - big * d, a + big * d])
        inter = fp.intersection(line)
        if inter.is_empty:
            continue
        pts = shapely.get_coordinates(inter)
        s = (pts - a) @ d
        out.append(a + s.max() * d)
    return out, d


def sample_profiles(gray, ox, oy, starts, d, s0_px, s1_px):
    """Brightness along each profile from s0 to s1 (pixels past the exit point), 1 px steps, mean of 3 parallel
    samples 1 px apart. Returns array (n_profiles, n_samples)."""
    p = np.array([-d[1], d[0]])
    s = np.arange(s0_px, s1_px)
    out = []
    for a in starts:
        vals = 0
        for off in (-1, 0, 1):
            xy = a[None, :] + s[:, None] * d[None, :] + off * p[None, :]
            vals = vals + map_coordinates(gray, [xy[:, 1] - oy, xy[:, 0] - ox], order=1, mode='nearest')
        out.append(vals / 3)
    return np.array(out)


# ---------------------------------------------------------------------------------------------------- registration

REG_SECOND_M = 6.0                       # the runner-up peak is looked for outside this radius of the best one


def edge_image(gray):
    from scipy.ndimage import gaussian_filter, sobel
    g = gaussian_filter(gray, 1.0)
    return np.hypot(sobel(g, 0), sobel(g, 1)).astype(np.float32)


def register(E, ox, oy, fp, center, R_px, m, seg=None, perp_px=None):
    """Shift of the footprint outline that best matches the roof outline in the image: mean edge strength along the
    shifted outline, for every integer shift within R_px of `center` (pixels, dx, dy). FFT correlation. Returns
    (dx, dy, score, distinctness = best / best outside REG_SECOND_M, score map median). With seg = (a, b) (pixel
    shifts), only shifts within perp_px of the segment a-b are allowed."""
    from scipy.signal import correlate
    ring = fp.exterior
    n = max(8, int(ring.length))
    pts = np.array([ring.interpolate(t).coords[0] for t in np.linspace(0, ring.length, n, endpoint=False)])
    bx0, by0 = np.floor(pts.min(axis=0)).astype(int)
    K = np.zeros((int(np.ceil(pts[:, 1].max())) - by0 + 1, int(np.ceil(pts[:, 0].max())) - bx0 + 1), np.float32)
    np.add.at(K, (np.round(pts[:, 1]).astype(int) - by0, np.round(pts[:, 0]).astype(int) - bx0), 1.0)
    K /= K.sum()
    r = int(np.ceil(R_px))
    center = (int(round(center[0])), int(round(center[1])))
    # window part covering every shifted kernel
    x0 = int(bx0 - ox + center[0]) - r; y0 = int(by0 - oy + center[1]) - r
    sub = E[max(0, y0):y0 + K.shape[0] + 2 * r + 1, max(0, x0):x0 + K.shape[1] + 2 * r + 1]
    if y0 < 0 or x0 < 0 or sub.shape[0] < K.shape[0] + 2 * r + 1 or sub.shape[1] < K.shape[1] + 2 * r + 1:
        return None
    C = correlate(sub, K, mode='valid', method='fft')          # C[i, j]: shift (x0 + j) - (bx0 - ox), ...
    yy, xx = np.mgrid[0:C.shape[0], 0:C.shape[1]]
    if seg is None:
        ok = (yy - r) ** 2 + (xx - r) ** 2 <= r * r
    else:
        a_, b_ = np.asarray(seg[0], float), np.asarray(seg[1], float)
        px_, py_ = center[0] + xx - r, center[1] + yy - r
        ab = b_ - a_; L2 = max(float(ab @ ab), 1e-9)
        t = np.clip(((px_ - a_[0]) * ab[0] + (py_ - a_[1]) * ab[1]) / L2, 0, 1)
        ok = (px_ - a_[0] - t * ab[0]) ** 2 + (py_ - a_[1] - t * ab[1]) ** 2 <= perp_px ** 2
    C = np.where(ok, C, np.nan)
    if not np.isfinite(C).any():
        return None
    j = int(np.nanargmax(C)); iy, ix = divmod(j, C.shape[1])
    far = (yy - iy) ** 2 + (xx - ix) ** 2 > (REG_SECOND_M / m) ** 2
    second = np.nanmax(np.where(far, C, np.nan)) if far.any() else np.nan
    dx, dy = center[0] + ix - r, center[1] + iy - r
    return float(dx), float(dy), float(C[iy, ix]), float(C[iy, ix] / second) if second > 0 else 9.0, float(np.nanmedian(C))


# ---------------------------------------------------------------------------------------------------- per tower

def tower_window(fp, half_extra_px, corr=None):
    b = np.array(fp.bounds if corr is None else corr.bounds)
    return b[0] - half_extra_px, b[1] - half_extra_px, b[2] + half_extra_px, b[3] + half_extra_px


def darkness_curve(gray, ox, oy, fp, href, m):
    """Mean darkness (ground - strip) / ground of the near strip outside the footprint, per azimuth (AZ_STEP grid).
    Ground = median of the window. Returns (thetas, curve)."""
    G = float(np.median(gray))
    th = np.arange(0, 360, AZ_STEP)
    s0, s1 = AZ_NEAR[0] / m, max(AZ_NEAR[0] / m + 4, AZ_NEAR[1] * href / m)
    cur = []
    for t in th:
        starts, d = profile_lines(fp, t, max(2 * PROFILE_SPACING_M / m, 1))
        v = sample_profiles(gray, ox, oy, starts, d, s0, s1)
        cur.append((G - v.mean()) / G if len(v) else np.nan)
    return th, np.array(cur)


def measure(gray, ox, oy, fp, theta, m, l_exp_px, smax_px, blockers=None):
    """Shadow length (m) of a tower along theta. Returns dict with the decision and per-profile details."""
    starts, d = profile_lines(fp, theta, PROFILE_SPACING_M / m)
    R = {'n_profiles': len(starts)}
    if len(starts) < MIN_PROFILES:
        R['status'] = 'too_few_profiles'; return R
    V = sample_profiles(gray, ox, oy, starts, d, 0, smax_px)
    Vs = median_filter(V, size=(1, SMOOTH_PX), mode='nearest')
    G = float(np.median(gray))
    near = Vs[:, int(1 / m):max(int(1 / m) + 3, int(0.3 * l_exp_px))]
    S = float(np.percentile(near, 10))
    contrast = (G - S) / G
    thr = S + THR_SHARE * (G - S)
    bright_end = S + BRIGHT_END_SHARE * (G - S)
    gap, brun = int(round(GAP_M / m)), int(round(BRIGHT_RUN_M / m))
    start_max = max(START_M[0] / m, START_M[1] * l_exp_px)
    L, why, tips, roof_r = [], [], [], []
    for v in Vs:
        roof_r.append(0.0)
        dark = v < thr
        idx = np.flatnonzero(dark)
        if not len(idx) or idx[0] > start_max:
            why.append('no_dark_start'); L.append(np.nan); tips.append(None); continue
        i, end = idx[0], None
        while i < len(v):
            if dark[i]:
                i += 1; continue
            j = i
            while j < len(v) and not dark[j]:
                j += 1
            if j - i > gap or j >= len(v):          # bright stretch longer than a gap (or to the end)
                end = i
                break
            i = j
        if end is None:
            why.append('run_too_long'); L.append(np.nan); tips.append(None); continue
        r_tip = 0.0
        if blockers is not None:
            hard, low_tree, low_polys, low_r, k_roof = blockers
            tip = shapely.Point(*(starts[len(L)] + d * end))
            if hard is not None and shapely.distance(hard, tip) * m < TIP_CLEAR_M:
                why.append('tip_on_building'); L.append(np.nan); tips.append(end); continue
            bad = False
            if low_tree is not None:
                for j in low_tree.query(shapely.buffer(tip, TIP_EDGE_M / m)):
                    pol = low_polys[j]
                    inside = pol.contains(tip)
                    if k_roof is None or not inside or shapely.distance(pol.exterior, tip) * m < TIP_EDGE_M:
                        bad = True; break               # roof tips off, or the tip is near a roof edge: ambiguous
                    r_tip = max(r_tip, low_r[j])
            if bad:
                why.append('tip_on_building'); L.append(np.nan); tips.append(end); continue
        after = v[end:end + brun * 2]
        if len(after) < brun or np.median(after) < bright_end:
            why.append('ends_on_dark'); L.append(np.nan); tips.append(end); continue
        # A tip on a roof of height r: the shadow ray meets the roof after (h - r) of descent, drawn at height r,
        # so tip - roof = (h - r)(s - w) and h = k x run + r. Stored as the ground-equivalent run + r / k.
        roof_r[-1] = r_tip
        why.append('ok' if r_tip == 0 else 'ok_roof'); L.append(end * m + (r_tip / blockers[4] if r_tip else 0.0)); tips.append(end)
    L = np.array(L)
    ok = np.isfinite(L)
    R.update({'contrast': round(contrast, 3), 'ground': round(G, 1), 'shadow': round(S, 1), 'profile_m': [None if not np.isfinite(x) else round(float(x), 2) for x in L],
              'profile_why': why, 'roof_r': roof_r, 'starts': [list(map(float, s)) for s in starts], 'tips_px': tips, 'd': list(map(float, d))})
    R['valid_share'] = round(float(ok.mean()), 3)
    # Consensus: every profile through a flat roof gives the same length, so take the longest cluster of agreeing
    # profiles (within max(CLUSTER_ABS_M, CLUSTER_REL x L)) holding >= max(MIN_CLUSTER, CLUSTER_SHARE x profiles):
    # the longest such cluster is the main roof (lower podium parts give shorter clusters).
    vals = np.sort(L[ok])[::-1]
    need = max(MIN_CLUSTER, CLUSTER_SHARE * len(L))
    mem = None
    for c in vals:
        sel = vals[np.abs(vals - c) <= max(CLUSTER_ABS_M, CLUSTER_REL * c)]
        if len(sel) >= need:
            mem = sel; break
    if mem is not None:
        q25, q50, q75 = np.percentile(mem, [25, 50, 75])
        R.update({'length_m': round(float(q50), 2), 'cluster_share': round(len(mem) / len(L), 3),
                  'iqr_rel': round(float((q75 - q25) / q50), 3) if q50 > 0 else 9})
    if contrast < MIN_CONTRAST:
        R['status'] = 'low_contrast'
    elif mem is None:
        c = {w: why.count(w) for w in set(why) if w not in ('ok', 'ok_roof')}
        R['status'] = 'profiles_' + max(c, key=c.get) if ok.mean() < CLUSTER_SHARE and c else 'profiles_disagree'
    else:
        R['status'] = 'ok'
        R['confidence'] = round(float(min(1, R['cluster_share'] / 0.6) * min(1, contrast / 0.6)), 3)
    return R


# ---------------------------------------------------------------------------------------------------- selection

def load_water(box):
    t = pq.read_table(WATER)
    g = shapely.from_wkb(t['geometry'].to_numpy(zero_copy_only=False))
    b = shapely.box(*box)
    g = g[shapely.intersects(g, b)]
    return shapely.STRtree(g), g


# ---------------------------------------------------------------------------------------------------- run

def circ_mean(deg, w=None):
    r = np.radians(deg)
    w = np.ones(len(r)) if w is None else w
    return float(np.degrees(np.arctan2((w * np.sin(r)).sum(), (w * np.cos(r)).sum())) % 360)


def circ_diff(a, b):
    return (a - b + 180) % 360 - 180


REG_FREE = (0.6, 10.0)                   # free search for the lean fit: shifts within 0.6 x height + 10 m
REG_FIT_TOL_M = 6.0                      # lean fit: towers whose shift is farther than this from the line are outliers
REG_FIT_MIN = 6
REG_PERP_M = 4.0                         # constrained search: within this of the lean segment
REG_CAL_T = (0.85, 1.15)                 # calibration towers: lean segment for heights real x this
REG_CAND_T = (0.6, 3.5)                  # candidates: heights max(current, raw h25) x this
REG_O_H = (12.0, 40.0)                   # buildings used for the offset of each footprint source: current height range
REG_O_N = 40
REG_O_MIN = 8
REG_O_MAD_MAX = 5.0                      # a source offset whose samples scatter more than this (median distance, m) is unusable
LEAN_TOL = (25.0, 0.5)                   # candidate rejected when |lean height - shadow height| > max(25 m, 0.5 x shadow height)
CAL_MAX = 150                            # calibration towers measured per patch, tallest first (tile budget)
CAND_MAX = 80                            # open candidates measured per patch, tallest raw h25 first (tile budget)
LEAN_FIT_MAX = 60                        # towers in the free-search lean fit of a Wayback patch (live: AZ_MAX_TOWERS)
# Wayback captures: a capture (date, sensor) is served once, from the release where it covers most of Ha Noi at the
# best zoom; captures already served live are skipped. Eligible when it covers >= WB_MIN_CALIB calibration towers and
# >= 1 candidate with raw h25 >= WB_MIN_TALL_H25. Order: winter captures (Nov-Feb: low sun, long shadows) first, then
# finer source resolution (SRC_RES, a proxy for a more nadir view), then more calibration towers.
WB_MIN_CALIB = 30
WB_MIN_TALL_H25 = 70.0
WB_WINTER = (11, 12, 1, 2)
# Combination over captures: accepted when >= 2 captures agree within COMBINE_TOL (height = median of the agreeing
# ones); a single capture only with confidence >= the threshold chosen on calibration LOO (SINGLE_GRID, see run).
COMBINE_TOL = 0.10
SINGLE_GRID = (0.5, 0.6, 0.7, 0.8, 0.9, 1.01)
SINGLE_TARGET = (0.9, 8)                 # share of LOO rows within 15 % and minimum rows a threshold must reach


def wayback_patches(live_keys):
    """Capture patches from the cached Wayback metadata (cmd_wbmeta): one per (date, sensor) not served live."""
    cfg = json.load(open(os.path.join(WB_DIR, 'waybackconfig.json')))
    best = {}
    for f in glob.glob(os.path.join(WB_DIR, 'meta_*.json')):
        d = json.load(open(f))
        rel = d['release']
        for z in [str(z_) for z_ in WB_LAYERS]:
            groups = {}
            for ft in d.get('layers', {}).get(z, []):
                at = ft['attributes']
                if not ft.get('geometry') or at['SRC_DATE'] is None:
                    continue
                rings = [shapely.LinearRing(r) for r in ft['geometry']['rings'] if len(r) >= 4]
                outer = [shapely.Polygon(r) for r in rings if not r.is_ccw]
                holes = [shapely.Polygon(r) for r in rings if r.is_ccw]
                g = shapely.union_all(outer) if outer else shapely.Polygon()
                if holes:
                    g = g.difference(shapely.union_all(holes))
                key = (int(at['SRC_DATE']), at['SRC_DESC'])
                groups.setdefault(key, {'g': [], 'at': at})['g'].append(shapely.make_valid(g))
            for key, e in groups.items():
                if key in live_keys:
                    continue
                g = shapely.make_valid(shapely.union_all(e['g']))
                score = (int(z), g.area)
                if key not in best or score > best[key][0]:
                    best[key] = (score, rel, int(z), g, e['at'])
    P = []
    for (date, sensor), (score, rel, z, g, at) in best.items():
        shapely.prepare(g)
        P.append({'id': f'wb{rel}_{date}_{sensor}_z{z}', 'rel': int(rel), 'zoom': z, 'geom': g, 'key': (date, sensor),
                  'release_title': cfg[str(rel)]['itemTitle'],
                  'attr': {k: at.get(k) for k in ('SRC_DATE', 'SRC_DESC', 'SRC_RES', 'SRC_ACC', 'SAMP_RES', 'NICE_NAME')}})
    return P


def combine(vals, single_conf):
    """Per-tower combination of per-capture heights [(h, confidence, patch)]: the largest group within COMBINE_TOL
    (max <= (1 + tol) min) with >= 2 captures -> median of the group; else one capture with confidence >= single_conf
    (when no other capture disagrees); else rejected. Returns (height or None, rule, n_agree)."""
    v = sorted(vals, key=lambda t: t[0])
    bestg = []
    for a_ in range(len(v)):
        g = [t for t in v[a_:] if t[0] <= (1 + COMBINE_TOL) * v[a_][0]]
        if len(g) > len(bestg):
            bestg = g
    if len(bestg) >= 2:
        return float(np.median([t[0] for t in bestg])), 'agree', len(bestg)
    if len(v) == 1 and single_conf is not None and v[0][1] >= single_conf:
        return float(v[0][0]), 'single_high_conf', 1
    return None, ('captures_disagree' if len(v) >= 2 else 'single_capture'), len(bestg)


def loo_stats(rows):
    e = np.array([x['err'] for x in rows]); rl = np.array([x['rel'] for x in rows])
    return {'n': len(rows), 'mae_m': round(float(np.abs(e).mean()), 2), 'bias_m': round(float(e.mean()), 2),
            'median_abs_err_m': round(float(np.median(np.abs(e))), 2), 'median_abs_rel': round(float(np.median(np.abs(rl))), 3),
            'mean_abs_rel': round(float(np.abs(rl).mean()), 3), 'bias_rel': round(float(np.median(rl)), 3),
            'share_within_10pct': round(float(np.mean(np.abs(rl) <= 0.10)), 3),
            'share_within_15pct': round(float(np.mean(np.abs(rl) <= 0.15)), 3)}


H25_BANDS = ((25, 30), (30, 50), (50, 70), (70, 94), (94, 1e9))


def band_of(x):
    return next((f'{lo}-{hi:g}' if hi < 1e9 else f'>={lo}') for lo, hi in H25_BANDS if lo <= x < hi) if x >= 25 else '<25'


def cmd_run(a):
    t0 = time.time()
    D = load_frame()
    live = load_patches()
    for p in live:
        p['rel'] = None
        p['key'] = (p['attr']['SRC_DATE'], p['attr']['SRC_DESC'])
    n = len(D['id'])
    tree = shapely.STRtree(D['geom'])
    box = (D['lon'].min(), D['lat'].min(), D['lon'].max(), D['lat'].max())
    water, _ = load_water(box)
    src, h, h25, fsrc = D['src'], D['h'], D['h25'], D['fsrc']
    calib_all = np.flatnonzero(np.isin(src, REAL_SRC) & (h25 >= CAND_H25) & ((src == 'wikidata') | D['clean'] | (src == 'osm_part')))
    cand_all = np.flatnonzero(~np.isin(src, NOT_CAND_SRC) & np.isfinite(h25) & ((h25 >= CAND_H25) | (D['area'] >= CAND_AREA)))
    log = {'calib_total': int(len(calib_all)), 'cand_rule_total': int(len(cand_all))}
    cand_all = cand_all[h25[cand_all] >= a.cand_min_h25]
    log['cand_measurable'] = int(len(cand_all))
    pts = shapely.points(D['lon'], D['lat'])
    # ---- patches: live ones, then Wayback captures by the eligibility / order rule
    wb = wayback_patches(set(p['key'] for p in live)) if not a.no_wayback else []
    for p in live + wb:
        p['inside'] = np.flatnonzero(shapely.contains(p['geom'], pts))
    tall = set(cand_all[h25[cand_all] >= WB_MIN_TALL_H25].tolist())
    cs, ct = set(calib_all.tolist()), set(cand_all.tolist())
    for p in wb:
        ins = set(p['inside'].tolist())
        p['n_calib'], p['n_cand'], p['n_tall'] = len(ins & cs), len(ins & ct), len(ins & tall)
    elig = [p for p in wb if p['n_calib'] >= WB_MIN_CALIB and p['n_tall'] >= 1]
    elig.sort(key=lambda p: (0 if (p['key'][0] // 100) % 100 in WB_WINTER else 1, p['attr']['SRC_RES'] or 9, -p['n_calib']))
    log['wayback'] = {'captures': len(wb), 'eligible': len(elig),
                      'eligible_list': [{'id': p['id'], 'release': p['release_title'], 'res': p['attr']['SRC_RES'],
                                         'n_calib': p['n_calib'], 'n_cand': p['n_cand'], 'n_tall': p['n_tall']} for p in elig]}
    print(f"wayback: {len(wb)} captures, {len(elig)} eligible", flush=True)
    for p in elig[:30]:
        print('  ', p['id'], p['release_title'], p['attr']['SRC_RES'], p['n_calib'], p['n_cand'], p['n_tall'], flush=True)
    P = live + elig
    if a.patches:
        P = [p for p in P if p['id'] in a.patches]

    AZ, KP, LEAN, OSRC = {}, {}, {}, {}
    cal_res, cand_res = {}, {}                  # keyed (patch id, row)
    budget_hit = False
    for k, pk in enumerate(P):
        if budget_hit:
            break
        pid, z = pk['id'], pk['zoom']
        SOURCE['rel'] = pk['rel']
        ins = set(pk['inside'].tolist())
        ck = np.array([i for i in calib_all if i in ins], int)
        ck = ck[np.argsort(-h[ck], kind='stable')][:CAL_MAX]
        kc = np.array([i for i in cand_all if i in ins], int)
        if len(ck) < REG_FIT_MIN or not len(kc):
            continue
        ctx = patch_ctx(D, pk, AZ, tree, water, OSRC, a.max_tiles)
        fpx, m_of, roof_px = ctx['fpx'], ctx['m_of'], ctx['roof_px']

        def reg(i, seg_m=None, R_m=None):
            fp, m = fpx(i), m_of(i)
            if seg_m is None:
                center, R, seg, perp = (0, 0), R_m / m, None, None
            else:
                A = np.array([seg_m[0][0], -seg_m[0][1]]) / m; B = np.array([seg_m[1][0], -seg_m[1][1]]) / m
                center = (A + B) / 2; perp = REG_PERP_M / m
                R = np.linalg.norm(B - A) / 2 + perp; seg = (A, B)
            b = fp.bounds
            cx, cy = round(center[0]), round(center[1])
            pad = R + 12
            rgb, gray, ox, oy = window(b[0] + cx - pad, b[1] + cy - pad, b[2] + cx + pad, b[3] + cy + pad, z, budget=a.max_tiles)
            r = register(edge_image(gray), ox, oy, fp, (cx, cy), R, m, seg, perp)
            if r is None:
                return None
            dx, dy, sc, dist, med = r
            return np.array([dx * m, -dy * m]), sc, dist

        try:
            # ---- 1. lean line
            H, O = [], []
            for i in ck[:(AZ_MAX_TOWERS if pk['rel'] is None else LEAN_FIT_MAX)]:
                r = reg(i, R_m=REG_FREE[0] * h[i] + REG_FREE[1])
                if r is not None:
                    H.append(h[i]); O.append(r[0])
            H, O = np.array(H), np.array(O).reshape(-1, 2)
            if len(H) < REG_FIT_MIN:
                LEAN[pid] = {'unused': 'too_few_registrations'}; continue
            w = np.median(O / H[:, None], axis=0); o = np.zeros(2)
            inl = np.ones(len(H), bool)
            for _ in range(10):
                res = np.linalg.norm(O - (o + H[:, None] * w), axis=1)
                inl = res <= REG_FIT_TOL_M
                if inl.sum() < REG_FIT_MIN:
                    break
                X = np.column_stack([np.ones(inl.sum()), H[inl]])
                sol, *_ = np.linalg.lstsq(X, O[inl], rcond=None)
                o, w = sol[0], sol[1]
            if inl.sum() < REG_FIT_MIN:
                print(f'patch {pid}: lean fit failed ({inl.sum()} inliers of {len(H)})', flush=True)
                LEAN[pid] = {'unused': 'lean_fit_failed', 'n': int(len(H)), 'inliers': int(inl.sum())}
                continue
            res = np.linalg.norm(O - (o + H[:, None] * w), axis=1)
            LEAN[pid] = {'o_osm_m': [round(float(v), 2) for v in o], 'w_m_per_m': [round(float(v), 4) for v in w],
                         'lean_deg': round(math.degrees(math.atan(np.linalg.norm(w))), 1),
                         'lean_azimuth': round(math.degrees(math.atan2(w[0], w[1])) % 360, 1),
                         'n': int(len(H)), 'inliers': int(inl.sum()), 'rmse_inliers_m': round(float(np.sqrt((res[inl] ** 2).mean())), 2)}
            ctx['w'] = w
            LEAN[pid]['_w'] = w
            # ---- 2. offsets of the other footprint sources
            OSRC[pid] = {'osm': o}
            for fs in sorted(set(fsrc[kc].tolist()) - {'osm'}):
                mid = np.array([i for i in np.flatnonzero((fsrc == fs) & (src == 'g25cal') & (h >= REG_O_H[0]) & (h <= REG_O_H[1])) if i in ins], int)
                mid = mid[np.argsort(-D['area'][mid], kind='stable')][:REG_O_N]
                os_ = []
                for i in mid:
                    r = reg(i, R_m=0.3 * h[i] + 12)
                    if r is not None:
                        os_.append(r[0] - h[i] * w)
                if len(os_) >= REG_O_MIN:
                    os_ = np.array(os_)
                    oo = np.median(os_, axis=0)
                    mad = float(np.median(np.linalg.norm(os_ - oo, axis=1)))
                    if mad <= REG_O_MAD_MAX:
                        OSRC[pid][fs] = oo
                    LEAN[pid][f'o_{fs}_m'] = [round(float(v), 2) for v in oo]
                    LEAN[pid][f'o_{fs}_mad_m'] = round(mad, 2)
            print(f'patch {pid}: lean {LEAN[pid]}', flush=True)
            # ---- 3. constrained registration of calibration towers, azimuth
            roofs = {}
            for i in ck:
                r = reg(i, seg_m=(o + REG_CAL_T[0] * h[i] * w, o + REG_CAL_T[1] * h[i] * w))
                if r is not None:
                    roofs[int(i)] = r[0]
            curves = []
            for i in ck[:(AZ_MAX_TOWERS if pk['rel'] is None else LEAN_FIT_MAX)]:
                if int(i) not in roofs:
                    continue
                rf = roof_px(i, roofs[int(i)]); m = m_of(i)
                ext = (AZ_NEAR[1] * h[i] + WINDOW_PAD_M) / m
                rgb, gray, ox, oy = window(*tower_window(rf, ext), z, budget=a.max_tiles)
                th, cur = darkness_curve(gray, ox, oy, rf, h[i], m)
                if np.isfinite(cur).all():
                    curves.append(cur)
            if len(curves) < AZ_MIN_TOWERS:
                LEAN[pid]['unused'] = 'too_few_azimuth_towers'; continue
            th = np.arange(0, 360, AZ_STEP)
            C = np.array([c - np.median(c) for c in curves])
            tot = C.sum(axis=0)
            j = int(np.argmax(tot))
            y0, y1, y2 = tot[j - 1], tot[j], tot[(j + 1) % len(tot)]
            den = y0 - 2 * y1 + y2
            peak = float((th[j] + (0.5 * (y0 - y2) / den if den != 0 else 0) * AZ_STEP) % 360)
            dev = circ_diff(np.array([th[int(np.argmax(c))] for c in C]), peak)
            AZ[pid] = {'azimuth': round(peak, 1), 'towers': len(curves), 'agree_15deg': round(float(np.mean(np.abs(dev) <= 15)), 3),
                       'per_tower_median_abs_dev_deg': round(float(np.median(np.abs(dev))), 1)}
            print(f'patch {pid}: apparent shadow azimuth {AZ[pid]}', flush=True)
            if AZ[pid]['agree_15deg'] < AZ_MIN_AGREE:
                LEAN[pid]['unused'] = 'azimuth_unreliable'; continue
            # ---- 4. calibration: ground tips only -> k_prelim; with roof tips (k_prelim) -> openness -> k
            pre = {}
            for i in ck:
                if int(i) in roofs:
                    pre[int(i)] = measure_tower(ctx, i, roofs[int(i)], h[i], a.k0, None, check_open=False)
            ok = [r for r in pre.values() if r.get('status') == 'ok']
            if len(ok) < 3:
                LEAN[pid]['unused'] = 'too_few_calibration'; print(f'patch {pid}: only {len(ok)} measured calibration towers', flush=True); continue
            k_pre = float(np.median([h[r['row']] / r['length_m'] for r in ok]))
            for i in ck:
                if int(i) not in roofs:
                    cal_res[(pid, int(i))] = {'id': int(D['id'][i]), 'row': int(i), 'patch': pid, 'status': 'no_registration', 'h_exp': float(h[i])}
                    continue
                r = measure_tower(ctx, i, roofs[int(i)], h[i], a.k0, None if a.no_roof_tips else k_pre, check_open=False)
                r['ground_only'] = {kk: pre[int(i)].get(kk) for kk in ('status', 'length_m', 'confidence')}
                if 'length_m' in r:
                    r['open'] = openness(ctx, i, roof_px(i, roofs[int(i)]), h[i], k_pre)
                cal_res[(pid, int(i))] = r
            rows = [r for (p_, _), r in cal_res.items() if p_ == pid]
            final = [r for r in rows if r.get('status') == 'ok' and r.get('open') == 'open']
            ks = np.array([h[r['row']] / r['length_m'] for r in final])
            spread = float(np.subtract(*np.percentile(ks, [75, 25])) / np.median(ks)) if len(ks) else 9.0
            KP[pid] = {'k_prelim': round(k_pre, 4), 'n_prelim': len(ok), 'n_open': int(len(ks)), 'spread': round(spread, 3)}
            if len(ks) >= K_MIN_TOWERS and spread <= K_MAX_SPREAD:
                KP[pid].update({'k': round(float(np.median(ks)), 4), 'n': int(len(ks)),
                                'p25_p75': [round(float(v), 4) for v in np.percentile(ks, [25, 75])],
                                'min_max': [round(float(ks.min()), 4), round(float(ks.max()), 4)]})
            print(f'patch {pid}: k {KP[pid]}', flush=True)
            if 'k' not in KP[pid]:
                LEAN[pid]['unused'] = 'k_too_few_or_spread'; continue
            kk_ = KP[pid]['k']
            # ---- 5. candidates: openness, then the tallest open ones measured
            prer = []
            for i in kc:
                hg = max(np.nan_to_num(h[i]), h25[i])
                h_exp = hg * (EXP_SAT if h25[i] >= SAT_H25 else EXP_H)
                o_s = OSRC[pid].get(fsrc[i])
                if o_s is None:
                    prer.append((i, 'no_offset_for_source')); continue
                prer.append((i, openness(ctx, i, roof_px(i, o_s + h_exp * w), h_exp, kk_)))
            LEAN[pid]['cand_prefilter'] = count([p_[1] for p_ in prer])
            openi = sorted([p_[0] for p_ in prer if p_[1] == 'open'], key=lambda i: -h25[i])[:CAND_MAX]
            LEAN[pid]['cand_measured'] = len(openi)
            print(f'patch {pid}: candidates {LEAN[pid]["cand_prefilter"]} -> measuring {len(openi)}', flush=True)
            for i in openi:
                hg = max(np.nan_to_num(h[i]), h25[i])
                o_s = OSRC[pid][fsrc[i]]
                r = reg(i, seg_m=(o_s + REG_CAND_T[0] * hg * w, o_s + REG_CAND_T[1] * hg * w))
                if r is None:
                    cand_res[(pid, int(i))] = {'id': int(D['id'][i]), 'row': int(i), 'patch': pid, 'status': 'no_registration', 'h_exp': float(hg)}
                    continue
                h_exp = hg * (EXP_SAT if h25[i] >= SAT_H25 else EXP_H)
                R = measure_tower(ctx, i, r[0], h_exp, kk_, None if a.no_roof_tips else kk_, check_open=True)
                R['lean_h'] = round(float((r[0] - o_s) @ w / (w @ w)), 1)
                if R.get('status') == 'ok':
                    R['shadow_h'] = round(kk_ * R['length_m'], 1)
                    if abs(R['lean_h'] - R['shadow_h']) > max(LEAN_TOL[0], LEAN_TOL[1] * R['shadow_h']):
                        R['status'] = 'lean_disagrees'
                cand_res[(pid, int(i))] = R
        except BudgetExceeded:
            print('tile budget reached'); budget_hit = True
            LEAN.setdefault(pid, {})['unused'] = 'tile_budget'
    SOURCE['rel'] = None
    used = [pid for pid in KP if 'k' in KP[pid]]
    # ---- per-capture LOO (calibration towers passing the candidate rules: ok + open), k of the other towers
    loo_rows = []
    for pid in used:
        rows = [r for (p_, _), r in cal_res.items() if p_ == pid and r.get('status') == 'ok' and r.get('open') == 'open']
        ks = np.array([h[r['row']] / r['length_m'] for r in rows])
        for j, r in enumerate(rows):
            kk = float(np.median(np.delete(ks, j)))
            pred = kk * r['length_m']; real = float(h[r['row']])
            loo_rows.append({'id': r['id'], 'row': r['row'], 'patch': pid, 'real': real, 'pred': round(pred, 1), 'err': round(pred - real, 1),
                             'rel': round((pred - real) / real, 3), 'h25': float(h25[r['row']]), 'name': D['name'][r['row']],
                             'confidence': r.get('confidence'), 'roof_profiles': r.get('profile_why', []).count('ok_roof') if 'profile_why' in r else None})
    log['loo_per_capture'] = {pid: loo_stats([x for x in loo_rows if x['patch'] == pid]) for pid in used if any(x['patch'] == pid for x in loo_rows)}
    if loo_rows:
        log['loo_all_captures'] = loo_stats(loo_rows)
    # ground-only variant of the same towers (roof-tip correction validation): same k rule on ground-only lengths
    gl = []
    for pid in used:
        rows = [r for (p_, _), r in cal_res.items() if p_ == pid and r.get('open') == 'open' and r.get('ground_only', {}).get('status') == 'ok']
        ks = np.array([h[r['row']] / r['ground_only']['length_m'] for r in rows])
        for j, r in enumerate(rows):
            pred = float(np.median(np.delete(ks, j))) * r['ground_only']['length_m']; real = float(h[r['row']])
            gl.append({'err': pred - real, 'rel': (pred - real) / real})
    if gl:
        log['loo_ground_only_tips'] = loo_stats(gl)
    # single-capture confidence threshold: the smallest grid value whose LOO rows reach SINGLE_TARGET
    single = None
    thr_tab = {}
    for c in SINGLE_GRID:
        sel = [x for x in loo_rows if (x['confidence'] or 0) >= c]
        if sel:
            st_ = loo_stats(sel); thr_tab[str(c)] = {'n': st_['n'], 'within_15pct': st_['share_within_15pct']}
            if single is None and st_['n'] >= SINGLE_TARGET[1] and st_['share_within_15pct'] >= SINGLE_TARGET[0]:
                single = c
    log['single_capture_threshold'] = {'chosen': single, 'grid': thr_tab}
    # combined LOO: towers with LOO predictions in several captures, combined by the same rule
    byt = {}
    for x in loo_rows:
        byt.setdefault(x['row'], []).append((x['pred'], x['confidence'] or 0, x['patch']))
    comb = []
    comb_rules = []
    for row, vals in byt.items():
        hh, rule, nag = combine(vals, single)
        comb_rules.append(rule)
        if hh is not None:
            real = float(h[row])
            comb.append({'row': row, 'real': real, 'pred': round(hh, 1), 'err': hh - real, 'rel': (hh - real) / real, 'rule': rule, 'n': len(vals)})
    log['loo_combined'] = loo_stats(comb) if comb else None
    log['loo_combined_rules'] = count(comb_rules)
    log['loo_combined_multi'] = loo_stats([x for x in comb if x['rule'] == 'agree']) if any(x['rule'] == 'agree' for x in comb) else None
    log['loo_rows'] = loo_rows
    # ---- candidates: combine over captures, one tower per complex
    byc = {}
    for (pid, i), r in cand_res.items():
        byc.setdefault(i, []).append(r)
    att = {}
    acc = []
    for i, rs in byc.items():
        oks = [(r['shadow_h'], r['confidence'], r['patch']) for r in rs if r.get('status') == 'ok']
        hh, rule, nag = combine(oks, single) if oks else (None, 'no_ok_capture', 0)
        b = band_of(h25[i])
        e = att.setdefault(b, {'measured': 0, 'ok_in_any_capture': 0, 'accepted': 0})
        e['measured'] += 1; e['ok_in_any_capture'] += bool(oks)
        if hh is None:
            continue
        e['accepted'] += 1
        acc.append({'id': int(D['id'][i]), 'row': int(i), 'file': D['file'][i], 'name': D['name'][i], 'lon': round(float(D['lon'][i]), 6),
                    'lat': round(float(D['lat'][i]), 6), 'h25': float(h25[i]), 'height_now': float(h[i]), 'src_now': src[i],
                    'footprint_src': fsrc[i], 'shadow_h': round(hh, 1), 'alpha': round(hh / h25[i], 3), 'rule': rule, 'n_agree': nag,
                    'captures': [{'patch': r['patch'], 'h': r.get('shadow_h'), 'status': r.get('status'), 'conf': r.get('confidence')} for r in rs],
                    'confidence': round(float(np.median([c for _, c, _ in oks])), 3), 'area': round(float(D['area'][i]))})
    log['cand_bands'] = att
    xy = CH.xyz(D['lon'], D['lat'])
    acc.sort(key=lambda r: (-r['n_agree'], -r['confidence']))
    kept, taken = [], []
    for r in acc:
        if any(np.linalg.norm(xy[r['row']] - xy[t]) <= COMPLEX_M for t in taken):
            r['complex_suppressed'] = True; continue
        kept.append(r); taken.append(r['row'])
    log['accepted_before_complex_rule'] = len(acc)
    log['accepted'] = sorted(kept, key=lambda r: -r['shadow_h'])
    log['accepted_suppressed'] = [r for r in acc if r.get('complex_suppressed')]
    for b in att:
        att[b]['accepted_one_per_complex'] = sum(1 for r in kept if band_of(r['h25']) == b)
    log.update({'lean': LEAN, 'azimuth': AZ, 'k': KP,
                'calib_status': count([r.get('status') if r.get('status') != 'ok' else 'ok_' + str(r.get('open')) for r in cal_res.values()]),
                'cand_status': count([r.get('status') for r in cand_res.values()])})
    print('used patches', used, '\nLOO per capture', log['loo_per_capture'], '\nLOO combined', log['loo_combined'],
          '\nground-only', log.get('loo_ground_only_tips'), '\nsingle', log['single_capture_threshold'], '\nbands', att, flush=True)
    if a.crops:
        log['crops'] = save_crops(a.crops, D, P, cal_res, cand_res, log['accepted'], a.n_crops, tree, water, OSRC, AZ, LEAN)
    for v in LEAN.values():
        v.pop('_w', None)
    log['network'] = dict(STATS)
    if STATS['t_first'] and STATS['requests'] > 1:
        log['network']['rate_req_s'] = round((STATS['requests'] - 1) / (STATS['t_last'] - STATS['t_first']), 2)
    log['calib_rows'] = [strip(r, h, h25, src, D) for r in cal_res.values()]
    log['cand_rows'] = [strip(r, h, h25, src, D) for r in cand_res.values()]
    log['patches'] = {p['id']: dict(p['attr'], zoom=p['zoom'], release=p.get('release_title', 'live')) for p in P if p['id'] in LEAN}
    json.dump(log, open(os.path.join(WORK, 'result.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=float)
    print(f'done {time.time() - t0:.0f}s', STATS)


def patch_ctx(D, pk, AZ, tree, water, OSRC, budget, w=None):
    """Per-patch helpers: footprint in pixels at the patch zoom, metres per pixel, roof polygon from a shift."""
    z = pk['zoom']
    cache = {}

    def fpx(i):
        if i not in cache:
            cache[i] = poly_px(D['geom'][i], z)
        return cache[i]

    def m_of(i):
        return mpp(D['lat'][i], z)

    def roof_px(i, off_m):
        m = m_of(i)
        return shapely.affinity.translate(fpx(i), off_m[0] / m, -off_m[1] / m)

    return dict(D=D, pk=pk, AZ=AZ, fpx=fpx, m_of=m_of, zoom=lambda i: z, roof_px=roof_px, tree=tree, water=water,
                OSRC=OSRC, budget=budget, w=w)


EXP_H, EXP_SAT, SAT_H25 = 1.2, 1.5, 94.0   # expected height of a candidate: max(current, raw h25) x this (saturated: EXP_SAT)


def openness(ctx, i, roof, h_exp, k_ratio):
    """Easy-tower rule on the apparent shadow corridor (the roof swept along the apparent shadow azimuth by
    CORRIDOR_MARGIN x expected length + CORRIDOR_PAD_M, plus the footprint): 'seam' when it leaves the patch, 'water'
    when it touches OSM water, 'obstacle' when another building of height >= max(OBST_MIN_H, OBST_SHARE x h_exp)
    covers it with its footprint or its leaning roof (footprint shifted by o + h w)."""
    D, pk = ctx['D'], ctx['pk']
    z, m, theta, w = pk['zoom'], ctx['m_of'](i), ctx['AZ'][pk['id']]['azimuth'], ctx['w']
    OS = ctx['OSRC'][pk['id']]
    clen = h_exp / k_ratio / m * CORRIDOR_MARGIN + CORRIDOR_PAD_M / m
    corr = shapely.convex_hull(shapely.union(corridor(roof, theta, clen), ctx['fpx'](i)))
    cx, cy = px_to_lonlat(*np.asarray(corr.exterior.coords).T, z)
    corr_ll = shapely.Polygon(np.column_stack([cx, cy]))
    if not pk['geom'].contains(corr_ll):
        return 'seam'
    if ctx['water'] is not None and len(ctx['water'].query(corr_ll, predicate='intersects')):
        return 'water'
    thr = max(OBST_MIN_H, OBST_SHARE * h_exp)
    near = ctx['tree'].query(shapely.buffer(corr_ll, 0.25 * 300 / 111000))
    hh = np.fmax(np.nan_to_num(D['h'][near]), np.nan_to_num(D['h25'][near]))
    near = near[(near != i) & (hh >= thr)]
    for j in near:
        fpj = poly_px(D['geom'][j], z)
        o_s = OS.get(D['fsrc'][j], OS['osm'])
        sh = o_s + max(np.nan_to_num(D['h'][j]), np.nan_to_num(D['h25'][j])) * w
        for g in (shapely.affinity.translate(fpj, o_s[0] / m, -o_s[1] / m), shapely.affinity.translate(fpj, sh[0] / m, -sh[1] / m)):
            if shapely.area(shapely.intersection(g, corr)) > 0.02 * g.area:
                return 'obstacle'
    return 'open'


def blockers_px(ctx, i, corr, h_exp, k_roof):
    """Other buildings near the corridor, in pixel coordinates: 'hard' = union of footprints (shifted by o) and leaning
    roofs (shifted by o + h w) of buildings without a known height or taller than ROOF_MAX_SHARE x h_exp; 'low' =
    roofs (shifted by o + r w) of buildings with a known height r <= ROOF_MAX_SHARE x h_exp, on which a tip may land
    (used when k_roof is given)."""
    D, pk, w = ctx['D'], ctx['pk'], ctx['w']
    z, m = pk['zoom'], ctx['m_of'](i)
    OS = ctx['OSRC'][pk['id']]
    cx, cy = px_to_lonlat(*np.asarray(corr.exterior.coords).T, z)
    near = ctx['tree'].query(shapely.buffer(shapely.Polygon(np.column_stack([cx, cy])), 0.25 * 300 / 111000))
    hard, low, lr = [], [], []
    for j in near[near != i]:
        fpj = poly_px(D['geom'][j], z)
        o_s = OS.get(D['fsrc'][j], OS['osm'])
        hj = D['h'][j] if np.isfinite(D['h'][j]) else np.nan_to_num(D['h25'][j])
        known = np.isfinite(D['h'][j]) and D['src'][j] is not None
        roof = shapely.affinity.translate(fpj, (o_s[0] + hj * w[0]) / m, -(o_s[1] + hj * w[1]) / m)
        if known and hj <= ROOF_MAX_SHARE * h_exp:
            low.append(roof); lr.append(float(hj))
        else:
            hard.append(shapely.affinity.translate(fpj, o_s[0] / m, -o_s[1] / m))
            if hj >= KEEP_H:
                hard.append(roof)
    return (shapely.union_all(hard) if hard else None, shapely.STRtree(low) if low else None, low, lr, k_roof)


def measure_tower(ctx, i, roof_off, h_exp, k_ratio, k_roof, check_open, keep_img=False):
    """Shadow run of tower i from its registered roof along the patch's apparent shadow azimuth. k_ratio sizes the
    corridor; k_roof (None = tips on roofs rejected) converts the height of a receiving roof into run length."""
    D, pk = ctx['D'], ctx['pk']
    m, theta = ctx['m_of'](i), ctx['AZ'][pk['id']]['azimuth']
    roof = ctx['roof_px'](i, roof_off)
    l_exp = h_exp / k_ratio / m
    clen = l_exp * CORRIDOR_MARGIN + CORRIDOR_PAD_M / m
    R = {'id': int(D['id'][i]), 'row': int(i), 'patch': pk['id'], 'theta': theta, 'h_exp': round(float(h_exp), 1),
         'l_exp_m': round(float(l_exp * m), 1), 'roof_shift_m': [round(float(v), 2) for v in roof_off], 'k_ratio': k_ratio, 'k_roof': k_roof}
    if check_open:
        st = openness(ctx, i, roof, h_exp, k_ratio)
        R['open'] = st
        if st != 'open':
            R['status'] = st
            return R
    corr = corridor(roof, theta, clen)
    rgb, gray, ox, oy = window(*tower_window(roof, WINDOW_PAD_M / m, corr), pk['zoom'], budget=ctx['budget'])
    R.update(measure(gray, ox, oy, roof, theta, m, l_exp, clen, blockers_px(ctx, i, corr, h_exp, k_roof)))
    if keep_img:
        R['_img'] = (rgb, ox, oy, ctx['fpx'](i), roof, corr)
    return R


def strip(r, h, h25, src, D):
    o = {k: v for k, v in r.items() if k not in ('_img', 'starts', 'tips_px', 'd', 'profile_why')}
    i = r['row']
    o.update({'h_now': float(h[i]), 'h25': float(h25[i]), 'src': src[i], 'name': D['name'][i]})
    return o


def count(v):
    out = {}
    for x in v:
        out[str(x)] = out.get(str(x), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def save_crops(out_dir, D, P, cal_res, cand_res, accepted, n_each, tree, water, OSRC, AZ, LEAN):
    """PNG crops: imagery, published footprint (yellow, thin), registered roof (orange), corridor (white), profiles
    (cyan = ground tip, green = tip on a low roof, magenta = rejected), tips (red). Calibration: accepted (ok + open)
    and one rejection per distinct reason; candidates: each capture of the accepted towers (tallest first) and one
    rejection per reason. The measurement is redone from the cached tiles to draw it."""
    from PIL import Image, ImageDraw
    os.makedirs(out_dir, exist_ok=True)
    byid = {p['id']: p for p in P}
    groups = []
    rows = sorted([r for r in cal_res.values() if 'n_profiles' in r], key=lambda r: -r['h_exp'])
    good = [r for r in rows if r.get('status') == 'ok' and r.get('open') == 'open']
    roofy = [r for r in good if 'ok_roof' in r.get('profile_why', [])]
    groups.append(('calib_ok', (roofy[:1] + [r for r in good if r not in roofy[:1]])[:n_each]))
    bad, seen = [], set()
    for r in rows:
        why = r.get('status') if r.get('status') != 'ok' else 'open_' + str(r.get('open'))
        if why == 'open_open' or why in seen:
            continue
        seen.add(why); r['reject'] = why; bad.append(r)
    groups.append(('calib_rej', bad[:n_each]))
    acc_rows = []
    for t in sorted(accepted, key=lambda t: -t['h25']):
        acc_rows += [r for (pid, i), r in cand_res.items() if i == t['row'] and r.get('status') == 'ok']
    groups.append(('cand_ok', acc_rows[:n_each + 1]))
    rows = sorted([r for r in cand_res.values() if 'n_profiles' in r and r.get('status') != 'ok'], key=lambda r: -r['h_exp'])
    bad, seen = [], set()
    for r in rows:
        if r['status'] in seen:
            continue
        seen.add(r['status']); r['reject'] = r['status']; bad.append(r)
    groups.append(('cand_rej', bad[:n_each]))
    for gname, rr in groups:
        for r in rr:
            pk = byid[r['patch']]
            SOURCE['rel'] = pk['rel']
            ctx = patch_ctx(D, pk, AZ, tree, water, OSRC, None, LEAN[pk['id']]['_w'])
            r.update({kk: v for kk, v in measure_tower(ctx, r['row'], np.array(r['roof_shift_m']), r['h_exp'], r['k_ratio'], r['k_roof'],
                                                       False, keep_img=True).items() if kk in ('_img', 'starts', 'tips_px', 'd', 'profile_why')})
    SOURCE['rel'] = None
    paths = []
    for gname, rows in groups:
        for r in rows:
            rgb, ox, oy, fp, roof, corr = r['_img']
            im = Image.fromarray(rgb).convert('RGB'); dr = ImageDraw.Draw(im)
            sh = lambda pts: [(x - ox, y - oy) for x, y in pts]
            dr.line(sh(corr.exterior.coords), fill=(255, 255, 255), width=1)
            dr.line(sh(fp.exterior.coords), fill=(255, 220, 0), width=1)
            dr.line(sh(roof.exterior.coords), fill=(255, 140, 0), width=2)
            d = np.array(r.get('d', [0, 0]))
            for st, tip, why in zip(r.get('starts', []), r.get('tips_px', []), r.get('profile_why', [])):
                a = np.array(st)
                if tip is not None:
                    b = a + d * tip
                    dr.line(sh([tuple(a), tuple(b)]), fill={'ok': (0, 255, 255), 'ok_roof': (0, 255, 0)}.get(why, (255, 0, 255)), width=1)
                    dr.ellipse([b[0] - ox - 2, b[1] - oy - 2, b[0] - ox + 2, b[1] - oy + 2], fill=(255, 0, 0))
                else:
                    dr.ellipse([a[0] - ox - 2, a[1] - oy - 2, a[0] - ox + 2, a[1] - oy + 2], outline=(255, 0, 255))
            x0, y0, x1, y1 = corr.bounds
            pad = 40
            box = (max(0, int(x0 - ox - pad)), max(0, int(y0 - oy - pad)), min(im.width, int(x1 - ox + pad)), min(im.height, int(y1 - oy + pad)))
            im = im.crop(box)
            lab = f"{gname} {r['patch']} id {r['id']} {r.get('reject', 'accepted')} L={r.get('length_m')} m"
            if gname.startswith('calib'):
                lab += f" real={float(D['h'][r['row']]):.0f}"
            if r.get('shadow_h'):
                lab += f" H={r['shadow_h']}"
            ImageDraw.Draw(im).rectangle([0, 0, 8 * len(lab), 14], fill=(0, 0, 0))
            ImageDraw.Draw(im).text((2, 1), lab, fill=(255, 255, 255))
            p = os.path.join(out_dir, f"{gname}_{r['id']}_{r['patch']}.png")
            im.save(p); paths.append(p)
            r.pop('_img', None)
    print('crops', len(paths))
    return paths


# ---------------------------------------------------------------------------------------------------- dry run

DRY_BANDS = ((30, 50), (50, 70), (70, 94), (94, 1e9))


def select_anchors(Ax, Ah, q_xyz, h, radii, N, window_, sat):
    """Anchors the tall stage uses for one target (same rule as calibrate_heights.tall_alpha with fallback 'main'):
    similar raw h25 (within window; saturated targets: every saturated anchor), the smallest radius holding >= N.
    Returns (indices, distances, tier) or (None, None, len(radii))."""
    if sat is not None and h >= sat:
        cand = np.flatnonzero(Ah >= sat)
    else:
        cand = np.flatnonzero(np.abs(Ah - h) <= window_ * h)
    if not len(cand):
        return None, None, len(radii)
    d = np.linalg.norm(Ax[cand] - q_xyz, axis=1)
    for t, R in enumerate(radii):
        m = d <= R
        if m.sum() >= N:
            return cand[m], d[m], t
    return None, None, len(radii)


def _dry(include_wd):
    """Tall stage of calibrate_heights recomputed for Ha Noi g25cal rows (raw h25 >= T) with the published OSM anchors
    plus Wikidata rows plus the accepted shadow towers as anchors (alpha = height / raw h25, same anchor rule: raw
    h25 >= 30 m or height >= 60 m). The main-stage value of each row is recovered exactly from the published height
    and the published tall alpha. Nothing is written except data/buildings/_shadow/dryrun.json."""
    R = json.load(open(os.path.join(WORK, 'result.json'), encoding='utf-8'))
    P = json.load(open(os.path.join(REPO, 'buildings', 'height_calibration.json'), encoding='utf-8'))['tall']
    st = P['settings']; an = P['anchors']
    radii, N, window_, sat, lam = tuple(st['radii_m']), st['min_anchors_n'], st['window'], st['saturation_h25_m'], st['lam']
    d0, clamp, T = st['distance_d0_m'], tuple(st['alpha_clamp']), st['threshold_t_m']
    D = load_frame()
    ah = np.array(an['h25_dm']) / 10; ar = np.array(an['real_dm']) / 10
    alon, alat = np.array(an['lon_e6']) / 1e6, np.array(an['lat_e6']) / 1e6
    # Wikidata anchors in Ha Noi (same anchor rule)
    wd = np.flatnonzero((D['src'] == 'wikidata') & np.isfinite(D['h25']) & ((D['h25'] >= CH.TALL_ANCHOR_H25) | (D['h'] >= CH.TALL_ANCHOR_REAL)))
    if not include_wd:
        wd = wd[:0]
    sh = [r for r in R['accepted'] if r['h25'] >= CH.TALL_ANCHOR_H25 or r['shadow_h'] >= CH.TALL_ANCHOR_REAL]
    sh_ids = set(r['id'] for r in R['accepted'])
    parts_x = [CH.xyz(alon, alat), CH.xyz(D['lon'][wd], D['lat'][wd])]
    if sh:
        parts_x.append(CH.xyz(np.array([r['lon'] for r in sh]), np.array([r['lat'] for r in sh])))
    Ax = np.vstack(parts_x)
    Ah = np.concatenate([ah, D['h25'][wd], np.array([r['h25'] for r in sh], float)])
    Ar = np.concatenate([ar / ah, D['h'][wd] / D['h25'][wd], np.array([r['shadow_h'] / r['h25'] for r in sh], float)])
    Asrc = np.array(['osm'] * len(ah) + ['wikidata'] * len(wd) + ['shadow'] * len(sh), object)
    Aid = [None] * len(ah) + [int(D['id'][j]) for j in wd] + [r['id'] for r in sh]
    osm_idx = np.flatnonzero(Asrc == 'osm')
    tg = np.flatnonzero((D['src'] == 'g25cal') & (D['h25'] >= T) & ~np.isin(D['id'], list(sh_ids)))
    q = CH.xyz(D['lon'][tg], D['lat'][tg])
    rows = []

    def alpha_of(sel, dist):
        w = 1 / (1 + dist / d0)
        return float(np.clip(CH.wmedian(Ar[sel], w), *clamp)), w

    for j, i in enumerate(tg):
        hh = D['h25'][i]
        so, do, to = select_anchors(Ax[osm_idx], Ah[osm_idx], q[j], hh, radii, N, window_, sat)
        sn, dn, tn = select_anchors(Ax, Ah, q[j], hh, radii, N, window_, sat)
        pub = D['h'][i]
        if so is not None:
            al_o, _ = alpha_of(osm_idx[so], do)
            h_old = np.clip(al_o * hh, *CH.H_CLAMP)
            main = math.exp((math.log(pub) - lam * math.log(h_old)) / (1 - lam))
        else:
            al_o, main = None, pub
        if sn is not None:
            al_n, w = alpha_of(sn, dn)
            new = float(CH.tall_height(np.array([main]), np.array([hh]), np.array([al_n]), np.array([tn]), len(radii), lam, 'main')[0])
            ws = {s_: float(w[Asrc[sn] == s_].sum() / w.sum()) for s_ in ('osm', 'wikidata', 'shadow')}
            shadow_ids = sorted(set(Aid[x] for x in sn if Asrc[x] == 'shadow'))
        else:
            al_n, new, ws, shadow_ids = None, main, {}, []
        rows.append({'id': int(D['id'][i]), 'file': D['file'][i], 'name': D['name'][i], 'h25': float(hh), 'before': round(float(pub), 1),
                     'after': round(new, 1), 'main_stage': round(main, 1), 'alpha_before': al_o, 'alpha_after': al_n, 'weights': ws,
                     'shadow_ids': shadow_ids, 'lead': max(ws, key=ws.get) if ws else 'none'})
    summ = {}
    for r in rows:
        band = next((f'{lo}-{hi:g}' if hi < 1e9 else f'>={lo}') for lo, hi in DRY_BANDS if lo <= r['h25'] < hi)
        for key in (r['file'][:-8], 'ALL'):
            e = summ.setdefault(key, {}).setdefault(band, {'targets': 0, 'lead_osm': 0, 'lead_wikidata': 0, 'lead_shadow': 0, 'no_anchor': 0,
                                                            'any_shadow': 0, 'changed_vs_published': 0, 'mean_abs_change_m': 0.0})
            e['targets'] += 1
            e['lead_' + r['lead'] if r['lead'] != 'none' else 'no_anchor'] += 1
            e['any_shadow'] += int(bool(r['shadow_ids']))
            ch = abs(r['after'] - r['before'])
            e['changed_vs_published'] += int(ch >= 0.1)
            e['mean_abs_change_m'] += ch
    for v in summ.values():
        for e in v.values():
            e['mean_abs_change_m'] = round(e['mean_abs_change_m'] / max(1, e['targets']), 2)
    by = {}
    for r in rows:
        for sid in r['shadow_ids']:
            by.setdefault(sid, []).append(r)
    ex = None
    if by:
        sid = max(by, key=lambda s_: (len(by[s_]), s_))
        t = next(r for r in R['accepted'] if r['id'] == sid)
        aff = sorted(by[sid], key=lambda r: -abs(r['after'] - r['before']))[:3]
        ex = {'tower': t, 'affected_total': len(by[sid]), 'examples': aff}
    out = {'settings': st, 'anchors': {'osm_national': int(len(osm_idx)), 'wikidata_hanoi': int(len(wd)), 'shadow': len(sh)},
           'targets': len(rows), 'summary': summ, 'worked_example': ex,
           'rows_changed': [r for r in rows if abs(r['after'] - r['before']) >= 0.1]}
    return out


def cmd_dryrun(a):
    """Two variants: OSM + shadow anchors (Wikidata rows keep their own heights but are no anchors, as published),
    and OSM + Wikidata + shadow anchors. Written to data/buildings/_shadow/dryrun.json."""
    out = {'osm_shadow': _dry(False), 'osm_wikidata_shadow': _dry(True)}
    json.dump(out, open(os.path.join(WORK, 'dryrun.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=float)
    for kk, v in out.items():
        print(kk, json.dumps({k: v[k] for k in ('anchors', 'targets')}), json.dumps(v['summary'].get('ALL', {}), indent=1, default=float))
    print(json.dumps(out['osm_shadow']['worked_example'], ensure_ascii=False, indent=1, default=float))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest='cmd', required=True)
    sp.add_parser('prep'); sp.add_parser('meta'); sp.add_parser('wbmeta')
    r = sp.add_parser('run')
    r.add_argument('--crops'); r.add_argument('--n-crops', type=int, default=3)
    r.add_argument('--max-tiles', type=int, default=None)
    r.add_argument('--k0', type=float, default=1.0, help='first-pass height / shadow length for calibration corridors')
    r.add_argument('--cand-min-h25', type=float, default=CAND_H25)
    r.add_argument('--patches', nargs='*', help='only these patch ids (debugging)')
    r.add_argument('--no-wayback', action='store_true'); r.add_argument('--no-roof-tips', action='store_true')
    sp.add_parser('dryrun')
    a = ap.parse_args()
    {'prep': cmd_prep, 'meta': cmd_meta, 'wbmeta': cmd_wbmeta, 'run': cmd_run, 'dryrun': lambda a: cmd_dryrun(a)}[a.cmd](a)


if __name__ == '__main__':
    main()
