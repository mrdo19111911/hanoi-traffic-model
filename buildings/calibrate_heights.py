"""Calibrate the Google 2.5D building heights of the published layer (buildings/by_area) against OSM-tagged heights,
and estimate a height for buildings that have none.

    .venv/Scripts/python.exe buildings/calibrate_heights.py truth   # -> data/buildings/_calib/{truth.parquet,density.npz}
    .venv/Scripts/python.exe buildings/calibrate_heights.py fit     # -> buildings/height_calibration.json
    .venv/Scripts/python.exe buildings/calibrate_heights.py tall    # adds the tall-building stage ('tall') to it
    .venv/Scripts/python.exe buildings/calibrate_heights.py apply [--workers 5] [--files f1.parquet ...] [--out-dir D] [--no-est] [--no-sources]
    # from raw: git archive 703e019 buildings/by_area | tar -x -C <tmp>; move <tmp>/buildings/by_area to
    # data/buildings/_calib/raw_by_area, then apply --src-dir data/buildings/_calib/raw_by_area --out-dir buildings/by_area

truth: every published row with height_src 'osm' / 'osm_part' (the OSM height tag, or building:levels x 3.3 m), its
  2.5D value read again from the Google tiles on the published footprint exactly as build_areas.py reads it (the
  published rows keep only the chosen height), footprint features, the IDW of the raw 2.5D heights of g25 neighbours
  within 50 / 100 / 200 m (same file), and the nationwide count of buildings per 250 m cell. It needs the uncalibrated
  layer (rows still 'g25'), as build_areas.py writes it: after apply, run it on a checkout from before apply (--dst-dir).
fit: truth rows are filtered (junk: under 2 m, over 500 m, height and levels disagreeing; mass tags: a 2 km block of
  >= 50 truth rows where one value covers >= 85 %, bulk default levels such as levels=4 on 9,000 houses in Lam Dong;
  osm_part rows, recorded only where taller than the 2.5D value, are not used). Rows with a 2.5D value >= 2 m calibrate:
  - groups by footprint (first match): tube (short side < 6.5 m, long / short >= 1.8, < 300 m2), small (< 60 m2),
    house (< 300 m2), large (< 1500 m2), xlarge;
  - per group a weighted Huber line in log space, log(real) = a + b log(h25), so alpha = real / h25 = e^a h25^(b-1)
    varies smoothly with the 2.5D value and a ratio of small numbers cannot explode; weights cap each 2 km block at
    100 buildings so no single mapped neighbourhood dominates;
  - local correction: kernel mean of the clipped log residuals of OSM anchors within R, weighted by distance and by
    similarity of the 2.5D value, shrunk toward 0 by k pseudo-anchors; calibrated = exp(line + correction) within
    [0.5, 3] x h25 and [2, 600] m.
  The grouping (among a few candidates), R and k are chosen by 5-fold block cross-validation on the training rows; a
  group whose cross-validated MAE does not beat the raw 2.5D stays raw. 20 % of the calibration rows, whole 2 km
  blocks, are held out and reported (MAE, median absolute error, bias, in metres and floors; overall, per group, per
  height band). No-2.5D rows: per area class a median regression of log(real) on the log IDW height of g25
  neighbours (else the class median), held out the same way against the one-floor default (3.3 m), on rows Google
  missed and on held-out rows whose 2.5D value is masked. Parameters, anchors and the validation go to the JSON.
tall: second stage for towers, on top of the main stage (run after fit; fit drops it). Satellite errors repeat for
  similar buildings nearby, so a target (raw h25 >= T) takes alpha = weighted median of real / h25 over OSM anchors
  (clean truth rows with h25 >= 30 m or real >= 60 m) whose h25 is within +-25 % of its own (saturated targets,
  h25 >= sat: every saturated anchor), from the smallest radius holding >= N of them; height = the share lam of the
  log step from the main-stage value to clip(alpha x raw h25). T, N, weights, sat, the radii, the fallback past the
  last radius (keep the main-stage value, or province then national band alpha), lam and osm_part anchors yes/no are
  chosen by inner block CV on the training rows; the hold-out uses the same blocks and rows as the main stage. The
  correlation of log(real / h25) between similar towers falls from 0.64 (< 200 m) to 0.1 at 1-2 km, so far anchors
  (2-25 km, province, national band) made the hold-out worse and CV picked short radii and keeping the main stage.
apply: rewrites height_dm, height_src and floors of published files in place: 'g25' rows of calibrated groups get
  'g25cal' (main stage, then the tall stage on raw h25 >= T), rows without a height get 'est'; 'osm' and 'osm_part'
  rows and every other column stay identical (checked);
  floors = 1 + round((h - 3.9) / 3.3), at least 1, as heights_25d.attrs_query. zstd 19, row groups of 500k (as
  split_by_area.py / build_areas.py), then the `bytes` of the changed files in by_area/index.json. A file that already
  holds 'g25cal' or 'est' is skipped: its raw 2.5D values are gone. build_areas.py does not call this, so a rebuilt
  area comes out uncalibrated until apply runs again.
  Then the rows listed in buildings/height_sources.json (extra_heights.py build) for the file get 'wikidata' (a matched
  Wikidata height / floors), else 'sibling' (the height of a tower of the same complex), with the listed height and
  floors; never on 'osm' / 'osm_part' rows (precedence osm / osm_part > wikidata > sibling > g25cal > est). A file
  holding any of these is skipped as well; --no-sources applies the calibration only.
  Last, the front-row houses of Ha Noi listed in buildings/frontrow_heights.json (frontrow_heights.py build) whose
  height_src is then 'g25cal' / 'est' get height >= 3 x 3.3 m and floors >= 3 ('frontrow' where that changes them):
  a domain rule from the user, not a measurement; --no-frontrow leaves it out. Before that minimum, the listed
  front-row houses of every file (alpha_frontrow.py) that are 'g25cal' with a raw 2.5D value get
  height = min(8 x 3.3 m, max(height, 1.7 x raw h25)) ('frontrow_alpha' where that lifts them; in Ha Noi only above
  the front-row minimum, the others go on to the minimum): the factor the user chose against their street photos on
  2026-10-27; --no-alpha leaves it out.
"""
import argparse, datetime, glob, json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compact_layer import SCHEMA, decode, metres_per_deg

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BY_AREA = os.path.join(REPO, 'buildings', 'by_area')
WORK = os.path.join(REPO, 'data', 'buildings', '_calib')
TRUTH_SRC = ('osm', 'osm_part')          # heights from OSM tags: ground truth, never changed
CELL_M = 250.0                           # density cell
LAT0 = 8.0                               # density grid origin (south of Vietnam)


# ---------------------------------------------------------------------------------------------------- features

def ring_geoms(tbl):
    """Footprints of a compact table as shapely polygons in local metres around each building's lon_e6/lat_e6
    (x east, y north), plus the per-row metres per 1e-6 degree (kx, ky). Keyhole rings are left as they are: their
    slits have zero width, so area, centroid and the oriented envelope are already right."""
    n = tbl.num_rows
    lat = tbl['lat_e6'].to_numpy().astype(np.float64) / 1e6
    kx = 111320 * np.cos(np.radians(lat)) / 1e6
    ky = np.full(n, 110574 / 1e6)
    ring = tbl['ring'].combine_chunks() if isinstance(tbl['ring'], pa.ChunkedArray) else tbl['ring']
    valid = ring.is_valid().to_numpy(zero_copy_only=False)
    geoms = np.empty(n, object)
    # Rings: offsets and data buffers of the binary column, int16 (dx, dy) steps.
    ri = np.flatnonzero(valid)
    if len(ri):
        off = np.frombuffer(ring.buffers()[1], np.int32, count=len(ring) + 1, offset=ring.offset * 4)
        data = np.frombuffer(ring.buffers()[2], np.int16)
        o0, o1 = off[ri] // 2, off[ri + 1] // 2              # int16 index range of each ring's steps
        nsteps = (o1 - o0) // 2
        # Per ring: vertex 0 at (0, 0), then the cumulative steps.
        nv = nsteps + 1
        ring_of_vertex = np.repeat(np.arange(len(ri)), nv)
        first = np.r_[0, np.cumsum(nv)[:-1]]
        step_idx = np.ones(nv.sum(), bool); step_idx[first] = False
        steps = np.zeros((nv.sum(), 2), np.int64)
        if nsteps.sum():
            gather = _ranges(o0, o1)
            steps[step_idx] = data[gather].reshape(-1, 2)
        cs = np.cumsum(steps, axis=0)
        cs -= np.repeat(cs[first], nv, axis=0)
        x = cs[:, 0] * np.repeat(kx[ri], nv); y = cs[:, 1] * np.repeat(ky[ri], nv)
        rings = shapely.linearrings(np.column_stack([x, y]), indices=ring_of_vertex)
        geoms[ri] = shapely.polygons(rings)
    # Rectangles: centre at (0, 0), long side clockwise from north.
    qi = np.flatnonzero(~valid)
    if len(qi):
        L = tbl['length_dm'].to_numpy()[qi] / 20.0
        W = tbl['width_dm'].to_numpy()[qi] / 20.0
        a = np.radians(tbl['angle_cdeg'].to_numpy()[qi] / 100.0)
        ux, uy = np.sin(a), np.cos(a); vx, vy = -uy, ux
        pts = np.stack([np.column_stack([sx * L * ux + sy * W * vx, sx * L * uy + sy * W * vy])
                        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))], axis=1)
        geoms[qi] = shapely.polygons(pts)
    return geoms, kx, ky


def _ranges(a, b):
    """Concatenation of arange(a[i], b[i]) for all i, vectorised."""
    n = b - a
    keep = n > 0
    a, n = a[keep], n[keep]
    if not len(n):
        return np.zeros(0, np.int64)
    idx = np.ones(n.sum(), np.int64)
    starts = np.r_[0, np.cumsum(n)[:-1]]
    idx[0] = a[0]
    idx[starts[1:]] = a[1:] - (a[:-1] + n[:-1] - 1)
    return np.cumsum(idx)


def features(tbl):
    """Per-row features of a compact table: centroid lon/lat (degrees), area (m2), long and short side of the
    oriented bounding rectangle (m). Rectangles take their stored size."""
    geoms, kx, ky = ring_geoms(tbl)
    area = shapely.area(geoms)
    c = shapely.centroid(geoms)
    cx, cy = shapely.get_x(c), shapely.get_y(c)
    lon = tbl['lon_e6'].to_numpy() / 1e6 + cx / kx / 1e6
    lat = tbl['lat_e6'].to_numpy() / 1e6 + cy / ky / 1e6
    env = shapely.oriented_envelope(geoms)
    L = np.zeros(len(geoms)); W = np.zeros(len(geoms))
    ok = shapely.get_type_id(env) == 3
    if ok.any():
        co = shapely.get_coordinates(shapely.get_exterior_ring(env[ok])).reshape(-1, 5, 2)
        e1 = np.hypot(*(co[:, 1] - co[:, 0]).T); e2 = np.hypot(*(co[:, 2] - co[:, 1]).T)
        L[ok], W[ok] = np.maximum(e1, e2), np.minimum(e1, e2)
    rect = ~tbl['ring'].is_valid().to_numpy(zero_copy_only=False)
    L[rect] = tbl['length_dm'].to_numpy()[rect] / 10.0
    W[rect] = tbl['width_dm'].to_numpy()[rect] / 10.0
    return {'lon': lon, 'lat': lat, 'area': area, 'length': L, 'width': W}


def cell_keys(lon, lat):
    """Density cell of each point: rows of CELL_M in latitude, columns of CELL_M at the row's centre latitude."""
    ky = 110574.0
    r = np.floor((lat - LAT0) * ky / CELL_M).astype(np.int64)
    latc = LAT0 + (r + 0.5) * CELL_M / ky
    c = np.floor((lon - 100.0) * 111320.0 * np.cos(np.radians(latc)) / CELL_M).astype(np.int64)
    return r * 1_000_000 + c


def src_strings(tbl):
    return np.array(tbl['height_src'].cast(pa.string()).to_pylist(), object)


def xyz(lon, lat):
    """Points on a sphere of the Earth's radius (metres): chord distance = ground distance at these scales."""
    R = 6371008.8
    lo, la = np.radians(lon), np.radians(lat)
    return np.column_stack([R * np.cos(la) * np.cos(lo), R * np.cos(la) * np.sin(lo), R * np.sin(la)])


NB_RADII = (50.0, 100.0, 200.0)          # neighbour radii tried for the no-h25 estimate (metres)
NB_DMIN = 5.0                            # IDW distance floor


def nb_stats(tree, vals, q, radii=NB_RADII, exclude=None):
    """IDW (power 1, distance floored at NB_DMIN) mean and count of `vals` (points of `tree`) within each radius
    around the query points. `exclude`: per query, an index of the tree to leave out (or -1)."""
    out = {}
    rmax = max(radii)
    lists = tree.query_ball_point(q, rmax)
    for r in radii:
        out[f'nb{int(r)}'] = np.full(len(q), np.nan); out[f'nbn{int(r)}'] = np.zeros(len(q), np.int32)
    for i, idx in enumerate(lists):
        if not idx:
            continue
        idx = np.asarray(idx)
        if exclude is not None and exclude[i] >= 0:
            idx = idx[idx != exclude[i]]
        if not len(idx):
            continue
        d = np.linalg.norm(tree.data[idx] - q[i], axis=1)
        for r in radii:
            m = d <= r
            if m.any():
                w = 1.0 / np.maximum(d[m], NB_DMIN)
                out[f'nb{int(r)}'][i] = float((w * vals[idx[m]]).sum() / w.sum())
                out[f'nbn{int(r)}'][i] = int(m.sum())
    return out


# ---------------------------------------------------------------------------------------------------- truth

def _truth_file(path):
    """One published file -> (density cell keys with counts, truth rows with features, neighbour stats and footprints)."""
    from scipy.spatial import cKDTree
    tbl = pq.read_table(path)
    src = src_strings(tbl)
    keys, cnt = np.unique(cell_keys(tbl['lon_e6'].to_numpy() / 1e6, tbl['lat_e6'].to_numpy() / 1e6), return_counts=True)
    ti = np.flatnonzero(np.isin(src, TRUTH_SRC))
    if not len(ti):
        return keys, cnt, None
    f = features(tbl)
    hdm = tbl['height_dm'].to_numpy(zero_copy_only=False)
    g = np.flatnonzero(src == 'g25')
    q = xyz(f['lon'][ti], f['lat'][ti])
    if len(g):
        nb = nb_stats(cKDTree(xyz(f['lon'][g], f['lat'][g])), hdm[g].astype(float) / 10, q)
    else:
        nb = {k: (np.full(len(ti), np.nan) if k.startswith('nb') and not k.startswith('nbn') else np.zeros(len(ti), np.int32))
              for r in NB_RADII for k in (f'nb{int(r)}', f'nbn{int(r)}')}
    sub = tbl.take(pa.array(ti))
    rows = sub.select(['lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring']).to_pylist()
    wkb = [shapely.to_wkb(decode(r)) for r in rows]
    out = {'id': tbl['id'].to_numpy()[ti], 'file': np.full(len(ti), os.path.basename(path), object), 'src': src[ti],
           'real': hdm[ti].astype(float) / 10, 'floors': tbl['floors'].to_numpy(zero_copy_only=False)[ti],
           **{k: v[ti] for k, v in f.items()}, **nb, 'wkb': np.array(wkb, object)}
    return keys, cnt, out


def cmd_truth(a):
    """Ground-truth rows of every published file plus their 2.5D height read again from the tiles (as build_areas.py
    reads them, on the published footprint), the nationwide density grid; written to data/buildings/_calib/."""
    from concurrent.futures import ProcessPoolExecutor
    from heights_25d import tile_heights
    t0 = time.time()
    os.makedirs(WORK, exist_ok=True)
    files = sorted(glob.glob(os.path.join(a.dst_dir, '*.parquet')))
    for f in files:                                  # calibrated rows: the g25 neighbours (raw 2.5D) are gone
        if np.isin(src_strings(pq.read_table(f, columns=['height_src'])), NEW_SRC).any():
            sys.exit(f'truth: {f} holds calibrated rows (g25cal / est); truth needs the raw layer: export commit '
                     f'{RAW_COMMIT} (git archive {RAW_COMMIT} buildings/by_area | tar -x) to {RAW_DIR} and pass '
                     f'--dst-dir {RAW_DIR}')
    dens = {}
    parts = []
    with ProcessPoolExecutor(a.workers) as ex:
        for k, (keys, cnt, out) in enumerate(ex.map(_truth_file, files)):
            for kk, cc in zip(keys.tolist(), cnt.tolist()):
                dens[kk] = dens.get(kk, 0) + cc
            if out is not None:
                parts.append(out)
            if (k + 1) % 50 == 0:
                print(f'{k + 1}/{len(files)} files | {time.time() - t0:.0f}s', flush=True)
    T = {c: np.concatenate([p[c] for p in parts]) for c in parts[0]}
    dk = np.array(sorted(dens), np.int64); dc = np.array([dens[k] for k in dk.tolist()], np.int32)
    np.savez(os.path.join(WORK, 'density.npz'), keys=dk, counts=dc)
    print('truth rows', len(T['id']), '| density cells', len(dk), f'| {time.time() - t0:.0f}s', flush=True)
    # 2.5D heights: every tile whose lon/lat box holds a truth centroid, manifest order, first valid value wins.
    st = json.load(open(os.path.join(REPO, 'data', 'buildings', '_areas', 'areas.json'), encoding='utf-8'))
    tiles = st['tiles']
    n = len(T['id'])
    h = np.full(n, np.nan, np.float32)
    ids = np.arange(n, dtype=np.int64)                    # tile_heights returns these positions
    wkb = T['wkb']
    jobs = []
    for x, tile in enumerate(tiles):
        lo0, la0, lo1, la1 = tile[6:]
        s = np.flatnonzero((T['lon'] >= lo0) & (T['lon'] <= lo1) & (T['lat'] >= la0) & (T['lat'] <= la1))
        if len(s):
            jobs.append((x, tuple(tile), s))
    print('tiles to read', len(jobs), flush=True)
    res = {}
    with ThreadPoolExecutor(a.threads) as ex:
        futs = {ex.submit(tile_heights, tile, ids[s], T['lon'][s], T['lat'][s], wkb[s]): x for x, tile, s in jobs}
        for k, fu in enumerate(futs):
            res[futs[fu]] = fu.result()[:2]
            if (k + 1) % 100 == 0:
                print(f'tiles {k + 1}/{len(jobs)} | {time.time() - t0:.0f}s', flush=True)
    for x in sorted(res):
        fi, fh = res[x]
        new = np.isnan(h[fi]); h[fi[new]] = fh[new]
    T['h25'] = h
    del T['wkb']
    cols = {k: (pa.array(v.tolist(), pa.string()) if v.dtype == object else pa.array(v)) for k, v in T.items()}
    pq.write_table(pa.table(cols), os.path.join(WORK, 'truth.parquet'), compression='zstd')
    print(f'wrote truth.parquet: {n} rows, 2.5D found {np.count_nonzero(~np.isnan(h))}, >= 2 m {np.count_nonzero(h >= 2)} '
          f'| {time.time() - t0:.0f}s', flush=True)


# ---------------------------------------------------------------------------------------------------- model

LEVEL_M = 3.3
BLOCK_M = 2000.0                         # spatial blocks for the hold-out and the cross-validation
HOLDOUT = 0.20                           # share of the calibration rows held out (whole blocks)
INNER_FOLDS = 5
SEED = 20261024
UNIFORM_MIN_N, UNIFORM_SHARE = 50, 0.85  # a block where one value covers this share of >= this many truth rows: mass tag
BLOCK_CAP = 100.0                        # a block weighs at most this many buildings in the fits
HUBER_DELTA = 0.25                       # log units (about 28 %)
MIN_GROUP = 300                          # truth rows a group needs for its own line (else the line of all rows)
RATIO_CLAMP = (0.5, 3.0)                 # calibrated / raw 2.5D
H_CLAMP = (2.0, 600.0)                   # calibrated height, m
EST_CLAMP = (3.0, 100.0)                 # neighbour estimate, m
LOCAL_GRID = [(0.0, 0.0)] + [(R, k) for R in (300.0, 500.0) for k in (1.0, 3.0, 10.0)]
RES_CLIP = 0.7                           # anchor residuals clipped to +-this (log units; a 2x disagreement) before averaging
LOCAL_SIGMA = 0.5                        # anchors count by similarity of raw 2.5D: exp(-(dlog h25)^2 / (2 sigma^2))
BANDS = ((0, 10), (10, 30), (30, 60), (60, 1e9))
NEW_SRC = ('g25cal', 'est', 'wikidata', 'sibling', 'frontrow', 'frontrow_alpha')
FRONTROW = os.path.join(REPO, 'buildings', 'frontrow_heights.json')   # front-row houses (frontrow_heights.py build)
SOURCES = os.path.join(REPO, 'buildings', 'height_sources.json')   # Wikidata / sibling rows (extra_heights.py build)

# Candidate groupings: lists of [name, rule], first match wins; the last rule must match everything. Rule keys:
# width_max (short side of the oriented rectangle, m), elong_min (long / short side), area_min / area_max (m2),
# dens_min / dens_max (buildings per 250 m cell), h25_min / h25_max (raw 2.5D, m).
TUBE = {'width_max': 6.5, 'elong_min': 1.8, 'area_max': 300}
CANDIDATES = {
    'one': [['all', {}]],
    'footprint': [['tube', TUBE], ['small', {'area_max': 60}], ['house', {'area_max': 300}],
                  ['large', {'area_max': 1500}], ['xlarge', {}]],
    'footprint_tall': [['tube', TUBE], ['small', {'area_max': 60}], ['house', {'area_max': 300}],
                       ['large_tall', {'area_max': 1500, 'h25_min': 25}], ['large', {'area_max': 1500}],
                       ['xlarge_tall', {'h25_min': 25}], ['xlarge', {}]],
    'footprint_dens': [['tube_dense', {**TUBE, 'dens_min': 150}], ['tube', TUBE], ['small', {'area_max': 60}],
                       ['house_dense', {'area_max': 300, 'dens_min': 150}], ['house', {'area_max': 300}],
                       ['large_tall', {'h25_min': 25}], ['large', {'area_max': 1500}], ['xlarge', {}]],
}
EST_GROUPS = [['small', {'area_max': 60}], ['house', {'area_max': 300}], ['large', {}]]


def assign(F, spec):
    """Group index of each row (dict of feature arrays) under a grouping spec (first matching rule)."""
    n = len(F['area'])
    g = np.full(n, -1, np.int16)
    elong = F['length'] / np.maximum(F['width'], 0.1)
    h25 = np.nan_to_num(F['h25'], nan=0.0)
    col = {'width_max': F['width'], 'elong_min': elong, 'area_min': F['area'], 'area_max': F['area'],
           'dens_min': F['dens'], 'dens_max': F['dens'], 'h25_min': h25, 'h25_max': h25}
    for k, (_, rule) in enumerate(spec):
        m = g < 0
        for key, v in rule.items():
            m &= (col[key] < v) if key.endswith('_max') else (col[key] >= v)
        g[m] = k
    return g


def huber_line(x, y, w, delta=HUBER_DELTA, iters=200):
    """Weighted Huber regression y = a + b x (iteratively reweighted least squares). Returns (a, b)."""
    hw = np.ones_like(y)
    a = b = np.inf
    for _ in range(iters):
        W = w * hw
        sw = W.sum(); mx = (W * x).sum() / sw; my = (W * y).sum() / sw
        vx = (W * (x - mx) ** 2).sum()
        b1 = (W * (x - mx) * (y - my)).sum() / vx if vx > 1e-9 else 0.0
        a1 = my - b1 * mx
        r = np.abs(y - a1 - b1 * x)
        hw = np.where(r <= delta, 1.0, delta / np.maximum(r, 1e-12))
        done = abs(a1 - a) < 1e-8 and abs(b1 - b) < 1e-8
        a, b = a1, b1
        if done:
            break
    return float(a), float(b)


def l1_line(x, y, w, iters=200):
    """Weighted least absolute deviations y = a + b x (median regression, IRLS). Returns (a, b)."""
    a, b = wmedian(y, w), 0.0
    X = np.column_stack([np.ones_like(x), x])
    for _ in range(iters):
        W = w / np.maximum(np.abs(y - a - b * x), 0.02)
        a1, b1 = np.linalg.solve((X * W[:, None]).T @ X, (X * W[:, None]).T @ y)
        done = abs(a1 - a) < 1e-8 and abs(b1 - b) < 1e-8
        a, b = float(a1), float(b1)
        if done:
            break
    return a, b


def wmedian(v, w):
    o = np.argsort(v); cw = np.cumsum(w[o])
    return float(v[o][np.searchsorted(cw, cw[-1] / 2)])


def block_keys(lon, lat, size=BLOCK_M):
    r = np.floor((lat - LAT0) * 110574.0 / size).astype(np.int64)
    c = np.floor((lon - 100.0) * 111320.0 * np.cos(np.radians(lat)) / size).astype(np.int64)
    return r * 1_000_000 + c


def block_weights(blk):
    _, inv, cnt = np.unique(blk, return_inverse=True, return_counts=True)
    return np.minimum(1.0, BLOCK_CAP / cnt[inv])


def fit_groups(F, m, spec):
    """Per group Huber line of log(real) on log(h25) over rows m (block-capped weights). A group with fewer than
    MIN_GROUP rows takes the line of all rows."""
    x, y = np.log(F['h25'][m]), np.log(F['real'][m])
    w = block_weights(F['block'][m])
    allfit = huber_line(x, y, w)
    g = F['group'][m]
    out = []
    for k, (name, rule) in enumerate(spec):
        s = g == k
        own = int(s.sum()) >= MIN_GROUP
        a, b = huber_line(x[s], y[s], w[s]) if own else allfit
        out.append({'name': name, 'rule': rule, 'n': int(s.sum()), 'a': a, 'b': b, 'own_line': own, 'on': True})
    return out


def line_pred(F, idx, groups):
    """log(calibrated height) of rows idx from their group line, before the local correction."""
    g = F['group'][idx]
    a = np.array([q['a'] for q in groups])[g]; b = np.array([q['b'] for q in groups])[g]
    return a + b * np.log(F['h25'][idx])


def local_corr(anchor_xyz, anchor_res, anchor_lh, q_xyz, q_lh, R, k, sigma=LOCAL_SIGMA):
    """Local correction in log units: shrunk kernel mean of the anchors' residuals within R,
    sum(w r) / (sum(w) + k) with w = (1 - d / R)^2 exp(-(log h25_anchor - log h25)^2 / (2 sigma^2)), so a few distant
    or dissimilar anchors move little (k pseudo-anchors at residual 0). The similarity term keeps the residuals of
    tagged low-rise houses off a tower next to them (pooled anchors pulled some towers to half their height)."""
    from scipy.spatial import cKDTree
    out = np.zeros(len(q_xyz))
    if R <= 0 or not len(anchor_res) or not len(q_xyz):
        return out
    tree = cKDTree(anchor_xyz)
    for i, idx in enumerate(tree.query_ball_point(q_xyz, R)):
        if idx:
            d = np.linalg.norm(anchor_xyz[idx] - q_xyz[i], axis=1)
            w = (1 - d / R) ** 2 * np.exp(-0.5 * ((anchor_lh[idx] - q_lh[i]) / sigma) ** 2)
            out[i] = (w * anchor_res[idx]).sum() / (w.sum() + k)
    return out


def calibrate(h25, logpred, corr):
    """Calibrated height (m): exp(group line + local correction), within RATIO_CLAMP of the raw value and H_CLAMP."""
    h = np.exp(logpred + corr)
    h = np.clip(h, RATIO_CLAMP[0] * h25, RATIO_CLAMP[1] * h25)
    return np.clip(h, *H_CLAMP)


def est_inputs(F, idx, nbr):
    nb = F[f'nb{nbr}'][idx]
    has = np.isfinite(nb) & (nb > 0)
    return has, np.log(np.where(has, nb, 1.0))


def fit_est(F, m, nbr):
    """No-2.5D estimate per EST_GROUPS group: log(real) = c + d log(IDW of the raw 2.5D heights of neighbours within
    nbr m) when there are neighbours, else the group's weighted median log height. Median regression: the heights of
    buildings Google sees as under 2 m are bimodal (a spike at one floor, 3.3 m, and 6-13 m), where a mean-type fit
    lands between the modes and loses to the one-floor guess."""
    idx = np.flatnonzero(m)
    w = block_weights(F['block'][idx]); y = np.log(F['real'][idx]); eg = F['egroup'][idx]
    has, lnb = est_inputs(F, idx, nbr)
    out = []
    for k, (name, rule) in enumerate(EST_GROUPS):
        s = eg == k
        med = wmedian(y[s], w[s]) if s.any() else wmedian(y, w)
        sh = s & has
        c, d = l1_line(lnb[sh], y[sh], w[sh]) if sh.sum() >= 50 else (med, 0.0)
        out.append({'name': name, 'rule': rule, 'n': int(s.sum()), 'n_with_neighbours': int(sh.sum()), 'c': c, 'd': d,
                    'median_log': med})
    return out


def est_pred(F, idx, egs, nbr):
    eg = F['egroup'][idx]
    has, lnb = est_inputs(F, idx, nbr)
    c = np.array([q['c'] for q in egs])[eg]; d = np.array([q['d'] for q in egs])[eg]
    med = np.array([q['median_log'] for q in egs])[eg]
    return np.clip(np.exp(np.where(has, c + d * lnb, med)), *EST_CLAMP)


def floors_of(h):
    """Floors as heights_25d.attrs_query: 1 + round((h - 3.9) / 3.3) (half away from zero), at least 1."""
    x = (np.asarray(h, float) - 3.9) / LEVEL_M
    return np.clip(1 + np.sign(x) * np.floor(np.abs(x) + 0.5), 1, 255).astype(np.uint8)


def dm_of(h):
    """Metres -> decimetres as attrs_query (round half up for positive values)."""
    return np.clip(np.floor(np.asarray(h, float) * 10 + 0.5), 0, 65530).astype(np.uint16)


def metrics(real, pred):
    e = np.asarray(pred, float) - np.asarray(real, float)
    if not len(e):
        return {'n': 0}
    r2 = lambda v: round(float(v), 2)
    return {'n': int(len(e)), 'mae_m': r2(np.abs(e).mean()), 'medae_m': r2(np.median(np.abs(e))), 'bias_m': r2(e.mean()),
            'mae_floors': r2(np.abs(e).mean() / LEVEL_M), 'medae_floors': r2(np.median(np.abs(e)) / LEVEL_M)}


# ---------------------------------------------------------------------------------------------------- fit

def load_density(path=None):
    d = np.load(path or os.path.join(WORK, 'density.npz'))
    return d['keys'], d['counts']


def density_of(lon_e6, lat_e6, dk, dc):
    k = cell_keys(np.asarray(lon_e6) / 1e6, np.asarray(lat_e6) / 1e6)
    p = np.minimum(np.searchsorted(dk, k), len(dk) - 1)
    return np.where(dk[p] == k, dc[p], 0).astype(np.int32)


def density_grid(files):
    """Buildings per CELL_M cell over the given files (cells by lon_e6/lat_e6, which geometry-preserving rewrites
    keep): sorted keys and counts."""
    dens = {}
    for f in files:
        t = pq.read_table(f, columns=['lon_e6', 'lat_e6'])
        k, c = np.unique(cell_keys(t['lon_e6'].to_numpy() / 1e6, t['lat_e6'].to_numpy() / 1e6), return_counts=True)
        for kk, cc in zip(k.tolist(), c.tolist()):
            dens[kk] = dens.get(kk, 0) + cc
    dk = np.array(sorted(dens), np.int64)
    return dk, np.array([dens[k] for k in dk.tolist()], np.int32)


def truth_frame():
    """truth.parquet + density -> dict of arrays with flags: junk, uniform (mass-tagged block), clean."""
    T = {k: np.array(v) for k, v in pq.read_table(os.path.join(WORK, 'truth.parquet')).to_pydict().items()}
    T['h25'] = T['h25'].astype(float)
    T['floors'] = T['floors'].astype(float)
    dk, dc = load_density()
    # Cells count buildings by their stored lon_e6 / lat_e6; a building looks up the cell of its centroid (as apply).
    T['dens'] = density_of(np.round(T['lon'] * 1e6), np.round(T['lat'] * 1e6), dk, dc)
    T['block'] = block_keys(T['lon'], T['lat'])
    T['xyz'] = xyz(T['lon'], T['lat'])
    r = T['real']
    der = np.maximum(1, 1 + np.floor((r - 3.9) / LEVEL_M + 0.5))
    T['junk'] = (r < 2) | (r > 500) | (np.abs(T['floors'] - der) > np.maximum(2, 0.5 * der))
    uni = np.zeros(len(r), bool)
    o = T['src'] == 'osm'
    for b in np.unique(T['block'][o]):
        s = o & (T['block'] == b)
        if s.sum() >= UNIFORM_MIN_N:
            _, c = np.unique(np.round(r[s], 1), return_counts=True)
            if c.max() / s.sum() >= UNIFORM_SHARE:
                uni |= s
    T['uniform'] = uni
    T['clean'] = o & ~T['junk'] & ~uni
    T['has25'] = T['h25'] >= 2
    return T


def holdout_blocks(T, m):
    """Whole BLOCK_M blocks, in a seeded random order, until HOLDOUT of the rows m are held out."""
    blocks, cnt = np.unique(T['block'][m], return_counts=True)
    order = np.random.default_rng(SEED).permutation(len(blocks))
    take = np.cumsum(cnt[order]) <= HOLDOUT * m.sum()
    take[np.argmin(take)] = True                     # the block that crosses the target
    return np.isin(T['block'], blocks[order][take])


def fold_of(T, m, k=INNER_FOLDS):
    blocks = np.unique(T['block'][m])
    perm = np.random.default_rng(SEED + 1).permutation(len(blocks))
    f = np.full(len(T['block']), -1)
    fb = dict(zip(blocks[perm].tolist(), (np.arange(len(blocks)) % k).tolist()))
    for i in np.flatnonzero(m):
        f[i] = fb[int(T['block'][i])]
    return f


def predict_cal(T, train, test, spec, R, k, groups=None):
    """Fit lines on train (unless given), anchors = train residuals, predict test. Returns (pred, groups)."""
    groups = groups or fit_groups(T, train, spec)
    ti, qi = np.flatnonzero(train), np.flatnonzero(test)
    res = np.clip(np.log(T['real'][ti]) - line_pred(T, ti, groups), -RES_CLIP, RES_CLIP)
    corr = local_corr(T['xyz'][ti], res, np.log(T['h25'][ti]), T['xyz'][qi], np.log(T['h25'][qi]), R, k)
    pred = calibrate(T['h25'][qi], line_pred(T, qi, groups), corr)
    on = np.array([q['on'] for q in groups])[T['group'][qi]]
    return np.where(on, pred, T['h25'][qi]), groups


def cmd_fit(a):
    t0 = time.time()
    T = truth_frame()
    C = T['clean'] & T['has25']                      # calibration rows
    E = T['clean'] & ~T['has25']                     # genuine no-2.5D rows
    hold = holdout_blocks(T, C)
    train = C & ~hold
    fold = fold_of(T, train | (E & ~hold))
    print(f"truth {len(T['real'])} | junk {T['junk'].sum()} | mass-tagged blocks {T['uniform'].sum()} | calibration {C.sum()} "
          f"(train {train.sum()}, hold-out {(C & hold).sum()}) | no-2.5D {E.sum()} | {time.time() - t0:.0f}s", flush=True)
    real, h25 = T['real'], T['h25']

    # 1. Grouping and local correction by inner block cross-validation on the training rows only.
    sel = {}
    best = None
    for name, spec in CANDIDATES.items():
        T['group'] = assign(T, spec)
        for R, k in LOCAL_GRID:
            if name != 'footprint' and R > 0 and (R, k) != (500.0, 3.0):
                continue                             # grouping is compared at no and one local setting
            pred = np.full(len(real), np.nan)
            for f in range(INNER_FOLDS):
                tr, te = train & (fold != f), train & (fold == f)
                pred[te], _ = predict_cal(T, tr, te, spec, R, k)
            s = metrics(real[train], pred[train])
            sel[f'{name} R={R:.0f} k={k:g}'] = s
            print(f'  inner CV {name:15s} R={R:4.0f} k={k:4g}: MAE {s["mae_m"]:.2f} m, median {s["medae_m"]:.2f} m', flush=True)
            if best is None or s['mae_m'] < best[0]:
                best = (s['mae_m'], name, R, k)
    _, gname, R, k = best
    # The local setting is then tuned for the chosen grouping.
    spec = CANDIDATES[gname]
    T['group'] = assign(T, spec)
    for R2, k2 in LOCAL_GRID:
        key = f'{gname} R={R2:.0f} k={k2:g}'
        if key not in sel:
            pred = np.full(len(real), np.nan)
            for f in range(INNER_FOLDS):
                tr, te = train & (fold != f), train & (fold == f)
                pred[te], _ = predict_cal(T, tr, te, spec, R2, k2)
            sel[key] = metrics(real[train], pred[train])
            print(f'  inner CV {gname:15s} R={R2:4.0f} k={k2:4g}: MAE {sel[key]["mae_m"]:.2f} m', flush=True)
        if sel[key]['mae_m'] < sel[f'{gname} R={R:.0f} k={k:g}']['mae_m']:
            R, k = R2, k2
    # Per group: calibrated only where the inner CV beats the raw 2.5D (MAE in metres).
    pred = np.full(len(real), np.nan)
    for f in range(INNER_FOLDS):
        tr, te = train & (fold != f), train & (fold == f)
        pred[te], _ = predict_cal(T, tr, te, spec, R, k)
    on = []
    for g, (gn, _) in enumerate(spec):
        s = train & (T['group'] == g)
        on.append(bool(s.sum()) and np.abs(pred[s] - real[s]).mean() < np.abs(h25[s] - real[s]).mean())
        print(f'  group {gn:12s} n={s.sum():6d} inner CV MAE raw {np.abs(h25[s] - real[s]).mean():.2f} '
              f'cal {np.abs(pred[s] - real[s]).mean():.2f} -> {"calibrate" if on[-1] else "leave raw"}', flush=True)
    print(f'chosen: {gname}, local R={R:.0f} m k={k:g} | {time.time() - t0:.0f}s', flush=True)

    # 2. Hold-out: fit on all training rows, predict the held-out blocks.
    groups = fit_groups(T, train, spec)
    for q, o in zip(groups, on):
        q['on'] = bool(o)
    test = C & hold
    pc, _ = predict_cal(T, train, test, spec, R, k, groups)
    ti = np.flatnonzero(test)
    val = {'overall': {'raw': metrics(real[ti], h25[ti]), 'calibrated': metrics(real[ti], pc)}, 'per_group': {}, 'per_band': {}}
    for g, (gn, _) in enumerate(spec):
        s = T['group'][ti] == g
        val['per_group'][gn] = {'raw': metrics(real[ti][s], h25[ti][s]), 'calibrated': metrics(real[ti][s], pc[s])}
    for lo, hi in BANDS:
        s = (real[ti] > lo) & (real[ti] <= hi) if lo else real[ti] <= hi
        val['per_band'][f'{lo}-{hi:g} m' if hi < 1e9 else f'>{lo} m'] = {
            'raw': metrics(real[ti][s], h25[ti][s]), 'calibrated': metrics(real[ti][s], pc[s])}
    # Rows never used in any fit: mass-tagged blocks and OSM building:part heights (with a 2.5D value).
    for nm, m in (('mass_tagged_blocks', T['uniform'] & T['has25'] & ~T['junk']),
                  ('osm_part', (T['src'] == 'osm_part') & T['has25'] & ~T['junk'])):
        qi = np.flatnonzero(m)
        p2, _ = predict_cal(T, C, m, spec, R, k, groups)
        val[nm] = {'raw': metrics(real[qi], h25[qi]), 'calibrated': metrics(real[qi], p2)}

    # 3. No-2.5D estimate: radius by inner CV on the genuine no-2.5D training rows, then hold-out.
    T['egroup'] = assign(T, EST_GROUPS)
    etrain = E & ~hold
    best_e = None
    est_sel = {'one_floor_3.3m': metrics(real[etrain], np.full(etrain.sum(), LEVEL_M))}
    for nbr in (int(r) for r in NB_RADII):
        p = np.full(len(real), np.nan)
        for f in range(INNER_FOLDS):
            tr, te = etrain & (fold != f), etrain & (fold == f)
            if te.any():
                p[np.flatnonzero(te)] = est_pred(T, np.flatnonzero(te), fit_est(T, tr, nbr), nbr)
        est_sel[f'nb{nbr}'] = metrics(real[etrain], p[etrain])
        print(f'  inner CV est nb{nbr}: MAE {est_sel[f"nb{nbr}"]["mae_m"]:.2f} m', flush=True)
        if best_e is None or est_sel[f'nb{nbr}']['mae_m'] < best_e[0]:
            best_e = (est_sel[f'nb{nbr}']['mae_m'], nbr)
    nbr = best_e[1]
    egs = fit_est(T, etrain, nbr)
    one_floor = lambda n: np.full(n, LEVEL_M)
    eh = np.flatnonzero(E & hold)
    pe = est_pred(T, eh, egs, nbr)
    val['no_h25'] = {'genuine': {'one_floor_3.3m': metrics(real[eh], one_floor(len(eh))), 'est': metrics(real[eh], pe)}}
    mh = np.flatnonzero(C & hold)                    # 2.5D masked
    pm = est_pred(T, mh, egs, nbr)
    val['no_h25']['masked'] = {'one_floor_3.3m': metrics(real[mh], one_floor(len(mh))), 'est': metrics(real[mh], pm),
                               'raw_h25_for_reference': metrics(real[mh], h25[mh])}
    for lo, hi in BANDS:
        s = (real[eh] > lo) & (real[eh] <= hi) if lo else real[eh] <= hi
        val['no_h25'].setdefault('genuine_per_band', {})[f'{lo}-{hi:g} m' if hi < 1e9 else f'>{lo} m'] = {
            'one_floor_3.3m': metrics(real[eh][s], one_floor(s.sum())), 'est': metrics(real[eh][s], pe[s])}
    est_on = best_e[0] < est_sel['one_floor_3.3m']['mae_m']        # decided on the inner CV, like the groups
    print(f'est radius {nbr} m | hold-out genuine: {val["no_h25"]["genuine"]} | {time.time() - t0:.0f}s', flush=True)

    # 4. Final parameters on every clean row; anchors are all calibration rows.
    final = fit_groups(T, C, spec)
    for q, o in zip(final, on):
        q['on'] = bool(o)
        q['ratio_at'] = {str(h): round(float(math.exp(q['a']) * h ** (q['b'] - 1)), 3) for h in (3, 5, 8, 12, 20, 30, 50, 80)}
    ci = np.flatnonzero(C)
    res = np.clip(np.log(real[ci]) - line_pred(T, ci, final), -RES_CLIP, RES_CLIP)
    egs_final = fit_est(T, E, nbr)
    out = {
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'model': 'per group: log(real) = a + b log(h25) (weighted Huber, delta %.2f), plus a local correction of nearby '
                 'anchors\' log residuals; calibrated = clip(exp(line + correction), ratio clamp x h25, height clamp)' % HUBER_DELTA,
        'truth': {'rows': int(len(real)), 'junk': int(T['junk'].sum()), 'mass_tagged_blocks': int(T['uniform'].sum()),
                  'calibration_rows': int(C.sum()), 'no_h25_rows': int(E.sum()), 'osm_part_rows': int((T['src'] == 'osm_part').sum()),
                  'filters': {'junk': 'real < 2 m, real > 500 m, or OSM levels and height disagree by more than max(2, half) floors',
                              'mass_tag': f'a {BLOCK_M:.0f} m block with >= {UNIFORM_MIN_N} OSM truth rows where one value covers '
                                          f'>= {UNIFORM_SHARE:.0%} (bulk-tagged default levels)',
                              'osm_part': 'not used in fits (only recorded where taller than the 2.5D value: biased)'}},
        'settings': {'block_m': BLOCK_M, 'holdout': HOLDOUT, 'seed': SEED, 'inner_folds': INNER_FOLDS, 'block_cap': BLOCK_CAP,
                     'huber_delta': HUBER_DELTA, 'anchor_res_clip': RES_CLIP, 'min_group': MIN_GROUP, 'ratio_clamp': RATIO_CLAMP, 'height_clamp': H_CLAMP,
                     'est_clamp': EST_CLAMP, 'density_cell_m': CELL_M, 'lat0': LAT0},
        'grouping': gname, 'groups': final,
        'local': {'radius_m': R, 'shrink_k': k, 'sigma_log_h25': LOCAL_SIGMA, 'n_anchors': int(len(ci)),
                  'kernel': 'w = (1 - d/R)^2 * exp(-(log h25_anchor - log h25)^2 / (2 sigma^2)); corr = sum(w res) / (sum(w) + k)',
                  'anchors': {'lon_e6': np.round(T['lon'][ci] * 1e6).astype(int).tolist(),
                              'lat_e6': np.round(T['lat'][ci] * 1e6).astype(int).tolist(),
                              'h25_dm': dm_of(h25[ci]).astype(int).tolist(),
                              'res_milli': np.round(res * 1000).astype(int).tolist()}},
        'est': {'on': bool(est_on), 'neighbour_radius_m': nbr, 'idw': f'power 1, distance floor {NB_DMIN:g} m, raw 2.5D heights '
                'of g25 rows of the same file', 'groups': egs_final, 'selection': est_sel},
        'selection': sel,
        'validation': val,
    }
    path = a.params
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        anchors = out['local'].pop('anchors')
        txt = json.dumps(out, ensure_ascii=False, indent=1)
        # anchors on one line each, so the file stays readable
        txt = txt[:-2] + ',\n "anchors": ' + json.dumps(anchors, separators=(',', ':')) + '\n}'
        fh.write(txt + '\n')
    print(f'wrote {path} | {time.time() - t0:.0f}s')
    print(json.dumps(val, indent=1))


# ---------------------------------------------------------------------------------------------------- tall stage

RAW_COMMIT = '703e019'                   # last commit whose by_area files hold the raw (uncalibrated) 2.5D values
RAW_DIR = os.path.join(WORK, 'raw_by_area')
TALL_ANCHOR_H25, TALL_ANCHOR_REAL = 30.0, 60.0   # an anchor has a 2.5D value >= this or a real height >= this
TALL_WINDOW = 0.25                       # anchors count when |h25_anchor - h25| <= this x h25
TALL_ALPHA_CLAMP = (0.8, 4.0)
TALL_D0 = 1000.0                         # distance weight 1 / (1 + d / D0)
TALL_SIM_FLOOR = 0.1                     # similarity weight max(floor, 1 - |dh| / (window x h25))
TALL_BAND_EDGES = (0.0, 35.0, 50.0, 70.0, 90.0, 96.0)   # lower edges of the h25 bands of the national fallback
# Cross-validated choices. Radii: the search radius grows through the list until N anchors are found; past the last
# one, fallback 'band' tries the province, then the national band alpha (the proposal), fallback 'main' keeps the
# main-stage value. lam: share of the step from the main-stage value to alpha x h25, in log units (1 = replace).
TALL_T_GRID = (25.0, 27.5, 30.0, 32.5, 35.0)
TALL_N_GRID = (1, 2, 3, 5, 8)
TALL_WEIGHTS = ('uniform', 'distance', 'similarity', 'both')
TALL_SAT_GRID = (None, 94.0, 96.0)       # h25 from which a target counts as saturated (window: anchors >= it)
TALL_RADII_GRID = ((250.0, 500.0, 1000.0), (250.0, 500.0, 1000.0, 2000.0), (250.0, 500.0, 1000.0, 2000.0, 5000.0),
                   (2000.0, 5000.0, 10000.0, 25000.0))
TALL_LAMBDA_GRID = (0.5, 0.75, 1.0)
TALL_FALLBACKS = ('main', 'band')


def tiers_of(radii):
    return [f'{r / 1000:g} km' for r in radii] + ['province', 'national band']


def province_map(index_path=None):
    """File name -> province ISO code (VN-xx) from by_area/index.json."""
    idx = json.load(open(index_path or os.path.join(BY_AREA, 'index.json'), encoding='utf-8'))
    return {e['file']: e['province_iso'] for e in idx['files']}


def tall_anchors(lon, lat, h25, real, prov, w=None):
    """Anchor set of the tall stage, sorted by h25: points, raw 2.5D, alpha = real / h25, province, block weight."""
    o = np.argsort(h25, kind='stable')
    w = np.ones(len(h25)) if w is None else w
    return {'xyz': xyz(lon[o], lat[o]), 'h25': h25[o].astype(float), 'ratio': (real[o] / h25[o]).astype(float),
            'prov': np.asarray(prov, object)[o], 'w': np.asarray(w, float)[o]}


def tall_bands(A):
    """National fallback alpha per h25 band (TALL_BAND_EDGES): block-weighted median of the anchors' alpha in the
    band (all anchors when a band is empty)."""
    out = []
    edges = list(TALL_BAND_EDGES) + [np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        s = (A['h25'] >= lo) & (A['h25'] < hi)
        out.append(round(wmedian(A['ratio'][s], A['w'][s]) if s.any() else wmedian(A['ratio'], A['w']), 4))
    return out


def tall_alpha(A, bands, q_xyz, q_h25, q_prov, N, weights, sat, radii, window=TALL_WINDOW, d0=TALL_D0,
               sim_floor=TALL_SIM_FLOOR, alpha_clamp=TALL_ALPHA_CLAMP, band_edges=TALL_BAND_EDGES):
    """Alpha of each target: weighted median of the alpha of anchors with a similar raw 2.5D value (within
    `window`; for a saturated target, h25 >= sat, every saturated anchor), the nearest first: the smallest radius of
    `radii` holding >= N of them, else the target's province, else the national band alpha (bands start at
    `band_edges`). Weights: 'uniform', 'distance' (1 / (1 + d / d0)), 'similarity' (max(sim_floor, 1 - |dh| /
    (window x h25)); 1 between saturated values) or 'both' (product). The defaults are the module constants (fit,
    CV); apply passes the values of the parameter file. Returns (alpha clamped to alpha_clamp, anchors used, tier
    index: position in radii, len(radii) = province, len(radii) + 1 = national band, distance of the farthest anchor
    used)."""
    n = len(q_h25)
    alpha = np.zeros(n); used = np.zeros(n, np.int32); tier = np.zeros(n, np.int8); dmax = np.zeros(n)
    hs = A['h25']
    edges = np.asarray(band_edges)
    for i in range(n):
        h = q_h25[i]
        saturated = sat is not None and h >= sat
        if saturated:
            lo, hi = np.searchsorted(hs, sat, 'left'), len(hs)
        else:
            lo, hi = np.searchsorted(hs, h * (1 - window), 'left'), np.searchsorted(hs, h * (1 + window), 'right')
        sel = None
        if hi > lo:
            d = np.linalg.norm(A['xyz'][lo:hi] - q_xyz[i], axis=1)
            for t, R in enumerate(radii):
                m = d <= R
                if m.sum() >= N:
                    sel, tier[i] = m, t
                    break
            else:
                m = A['prov'][lo:hi] == q_prov[i]
                if m.sum() >= N:
                    sel, tier[i] = m, len(radii)
        if sel is None:
            alpha[i] = bands[int(np.searchsorted(edges, h, 'right')) - 1]
            tier[i] = len(radii) + 1
            continue
        dd, ha, ra = d[sel], hs[lo:hi][sel], A['ratio'][lo:hi][sel]
        w = np.ones(len(dd))
        if weights in ('distance', 'both'):
            w = w / (1 + dd / d0)
        if weights in ('similarity', 'both') and not saturated:
            w = w * np.maximum(sim_floor, 1 - np.abs(ha - h) / (window * h))
        alpha[i] = wmedian(ra, w)
        used[i] = len(dd); dmax[i] = dd.max()
    return np.clip(alpha, *alpha_clamp), used, tier, dmax


def tall_height(main_h, h25, alpha, tier, n_radii, lam, fallback):
    """Tall-stage height of targets: clip(alpha x raw h25, H_CLAMP), reached by the share lam of the step from the
    main-stage value in log units; with fallback 'main', targets past the last radius keep the main-stage value."""
    h = np.clip(alpha * h25, *H_CLAMP)
    h = np.clip(np.exp(np.log(main_h) + lam * (np.log(h) - np.log(main_h))), *H_CLAMP)
    if fallback == 'main':
        h = np.where(tier >= n_radii, main_h, h)
    return h


def band_metrics(real, base, new, extra=None):
    """Hold-out metrics per real-height band (BANDS) and overall for the raw, main-stage and tall-stage values."""
    out = {}
    for lo, hi in [(None, None)] + list(BANDS):
        s = np.ones(len(real), bool) if lo is None else ((real > lo) & (real <= hi) if lo else real <= hi)
        key = 'overall' if lo is None else (f'{lo}-{hi:g} m' if hi < 1e9 else f'>{lo} m')
        out[key] = {nm: metrics(real[s], v[s]) for nm, v in (extra or {}).items()}
        out[key].update({'main_stage': metrics(real[s], base[s]), 'tall_stage': metrics(real[s], new[s])})
    return out


def write_params(path, P):
    """height_calibration.json with the anchor arrays (main and tall stage) on one line each."""
    P = dict(P)
    anchors = P.pop('anchors')
    tall_anc = None
    if 'tall' in P:
        P['tall'] = dict(P['tall']); tall_anc = P['tall'].pop('anchors')
        P['tall']['anchors'] = '__TALL_ANCHORS__'
    P['anchors'] = '__ANCHORS__'
    txt = json.dumps(P, ensure_ascii=False, indent=1)
    txt = txt.replace('"__ANCHORS__"', json.dumps(anchors, separators=(',', ':')))
    if tall_anc is not None:
        txt = txt.replace('"__TALL_ANCHORS__"', json.dumps(tall_anc, separators=(',', ':')))
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(txt + '\n')


def cmd_tall(a):
    """Tall-building stage on top of the main stage in the parameter file: inner block CV on the training rows picks
    the target threshold T, the anchor count N, the weights, the saturation rule, the radii, the fallback, the step
    share lam and whether osm_part rows serve as anchors; the hold-out (the same blocks and osm calibration rows as
    the main stage) is reported per real-height band; final anchors (all eligible rows) go to the parameter file
    under 'tall'."""
    from scipy.spatial import cKDTree
    t0 = time.time()
    P = json.load(open(a.params, encoding='utf-8'))
    P.pop('tall', None)
    T = truth_frame()
    pmap = province_map()
    T['prov'] = np.array([pmap[f] for f in T['file']], object)
    real, h25 = T['real'], T['h25']
    C = T['clean'] & T['has25']
    E = T['clean'] & ~T['has25']
    hold = holdout_blocks(T, C)
    train = C & ~hold
    fold = fold_of(T, train | (E & ~hold))
    test = C & hold
    spec = CANDIDATES[P['grouping']]
    R, k = P['local']['radius_m'], P['local']['shrink_k']
    T['group'] = assign(T, spec)
    on = np.array([q['on'] for q in P['groups']])
    # Main stage, as cmd_fit computes it: inner-CV predictions on the training rows, hold-out predictions.
    base = np.full(len(real), np.nan)
    for f in range(INNER_FOLDS):
        tr, te = train & (fold != f), train & (fold == f)
        base[te], _ = predict_cal(T, tr, te, spec, R, k)
    groups = fit_groups(T, train, spec)
    for q, o in zip(groups, on):
        q['on'] = bool(o)
    base[test], _ = predict_cal(T, train, test, spec, R, k, groups)
    chk = metrics(real[test], base[test])
    print(f'main stage hold-out MAE {chk["mae_m"]} bias {chk["bias_m"]} (parameter file: '
          f'{P["validation"]["overall"]["calibrated"]["mae_m"]} / {P["validation"]["overall"]["calibrated"]["bias_m"]}) '
          f'| {time.time() - t0:.0f}s', flush=True)
    tgt_ok = on[T['group']]                          # g25 rows of groups left raw never become g25cal: not targets
    # Anchor pools: clean osm calibration rows, optionally osm_part rows (not junk, with a 2.5D value).
    tall_cond = (h25 >= TALL_ANCHOR_H25) | (real >= TALL_ANCHOR_REAL)
    pool_osm = C & tall_cond
    pool_part = (T['src'] == 'osm_part') & ~T['junk'] & T['has25'] & tall_cond
    # Folds of osm_part blocks without a training row: seeded, as fold_of.
    pfold = fold.copy()
    extra = np.unique(T['block'][pool_part & ~hold & (pfold < 0)])
    fb = dict(zip(extra.tolist(), (np.random.default_rng(SEED + 2).permutation(len(extra)) % INNER_FOLDS).tolist()))
    for i in np.flatnonzero(pool_part & ~hold & (pfold < 0)):
        pfold[i] = fb[int(T['block'][i])]
    bw = block_weights(T['block'])

    def anchors_of(m):
        i = np.flatnonzero(m)
        return tall_anchors(T['lon'][i], T['lat'][i], h25[i], real[i], T['prov'][i], bw[i])

    # Inner CV: alpha of every training row with h25 >= min(T) per (osm_part, radii, N, weights, sat); then T, lam
    # and the fallback only select.
    cand = train & tgt_ok & (h25 >= min(TALL_T_GRID))
    ci = np.flatnonzero(cand)
    big = train & (real > 60)
    sel, best = {}, None
    for use_part in (False, True):
        fold_anchor = {}
        for f in range(INNER_FOLDS):
            m = pool_osm & train & (fold != f)
            if use_part:
                m |= pool_part & ~hold & (pfold != f)
            A = anchors_of(m)
            fold_anchor[f] = (A, tall_bands(A))
        for radii in TALL_RADII_GRID:
            rk = '/'.join(f'{r / 1000:g}' for r in radii)
            for N in TALL_N_GRID:
                for wt in TALL_WEIGHTS:
                    for sat in TALL_SAT_GRID:
                        al = np.full(len(real), np.nan); tr_ = np.zeros(len(real), np.int8)
                        for f in range(INNER_FOLDS):
                            qi = np.flatnonzero(cand & (fold == f))
                            A, bands = fold_anchor[f]
                            al[qi], _, tr_[qi], _ = tall_alpha(A, bands, T['xyz'][qi], h25[qi], T['prov'][qi], N, wt, sat, radii)
                        row = []
                        for lam in TALL_LAMBDA_GRID:
                            for fbk in TALL_FALLBACKS:
                                ph = tall_height(base[ci], h25[ci], al[ci], tr_[ci], len(radii), lam, fbk)
                                for Tt in TALL_T_GRID:
                                    p = base.copy()
                                    s = h25[ci] >= Tt
                                    p[ci[s]] = ph[s]
                                    key = f'T={Tt:g} N={N} w={wt} sat={sat} radii={rk}km fallback={fbk} lam={lam:g} osm_part={use_part}'
                                    e = p[train] - real[train]; eb = p[big] - real[big]
                                    sel[key] = {'mae_m': round(float(np.abs(e).mean()), 4), 'bias_m': round(float(e.mean()), 3),
                                                'over60_mae_m': round(float(np.abs(eb).mean()), 3),
                                                'over60_bias_m': round(float(eb.mean()), 3)}
                                    row.append(sel[key]['mae_m'])
                                    if best is None or sel[key]['mae_m'] < best[0]:
                                        best = (sel[key]['mae_m'], key, Tt, N, wt, sat, radii, fbk, lam, use_part)
                        print(f'  osm_part={use_part} radii={rk}km N={N} w={wt:10s} sat={sat}: best inner CV MAE {min(row):.4f} '
                              f'| {time.time() - t0:.0f}s', flush=True)
    eb = base[big] - real[big]
    base_cv = {**metrics(real[train], base[train]), 'mae_m_4': round(float(np.abs(base[train] - real[train]).mean()), 4),
               'over60_mae_m': round(float(np.abs(eb).mean()), 3), 'over60_bias_m': round(float(eb.mean()), 3)}
    _, ckey, Tt, N, wt, sat, radii, fbk, lam, use_part = best
    print(f'inner CV main stage MAE {base_cv["mae_m_4"]} | chosen {ckey}: MAE {best[0]}', flush=True)

    def best_where(pred):
        kb = min((kk for kk in sel if pred(kk)), key=lambda kk: sel[kk]['mae_m'])
        return {'config': kb, **sel[kb]}
    findings = {'best_without_osm_part': best_where(lambda kk: kk.endswith('osm_part=False')),
                'best_with_osm_part': best_where(lambda kk: kk.endswith('osm_part=True')),
                'best_as_proposed (2-25 km, province, national band, replace)':
                    best_where(lambda kk: 'radii=2/5/10/25km fallback=band lam=1 ' in kk),
                'best_fallback_band': best_where(lambda kk: 'fallback=band' in kk),
                'best_replace_lam1': best_where(lambda kk: 'lam=1 ' in kk)}
    for nm, v in findings.items():
        print(f'  {nm}: {v}', flush=True)

    # Hold-out: anchors from the training blocks only; targets are the held-out osm calibration rows with h25 >= T.
    def holdout_with(use_p, Tt=Tt, N=N, wt=wt, sat=sat, radii=radii, fbk=fbk, lam=lam):
        m = pool_osm & train
        if use_p:
            m |= pool_part & ~hold
        A = anchors_of(m)
        bands = tall_bands(A)
        qi = np.flatnonzero(test & tgt_ok & (h25 >= Tt))
        al, used, tier, _ = tall_alpha(A, bands, T['xyz'][qi], h25[qi], T['prov'][qi], N, wt, sat, radii)
        p = base.copy()
        p[qi] = tall_height(base[qi], h25[qi], al, tier, len(radii), lam, fbk)
        return p, qi, tier
    ti = np.flatnonzero(test)
    p_hold, qi, tier = holdout_with(use_part)
    val = band_metrics(real[ti], base[ti], p_hold[ti], {'raw': h25[ti]})
    p_alt, _, _ = holdout_with(not use_part)
    val_alt = band_metrics(real[ti], base[ti], p_alt[ti])
    pk = findings['best_as_proposed (2-25 km, province, national band, replace)']['config']
    pT, pN = float(pk.split('T=')[1].split()[0]), int(pk.split('N=')[1].split()[0])
    pw, ps = pk.split('w=')[1].split()[0], pk.split('sat=')[1].split()[0]
    p_prop, _, _ = holdout_with(False, Tt=pT, N=pN, wt=pw, sat=None if ps == 'None' else float(ps),
                                radii=TALL_RADII_GRID[-1], fbk='band', lam=1.0)
    val_prop = band_metrics(real[ti], base[ti], p_prop[ti])
    below = test & (h25 < Tt)
    unchanged = bool(np.all(p_hold[below] == base[below]))
    TI = tiers_of(radii)
    tiers = dict(zip(TI, np.bincount(tier, minlength=len(TI)).tolist()))
    print(f'hold-out targets {len(qi)} | tiers {tiers} | rows below T unchanged: {unchanged}', flush=True)
    for key, v in val.items():
        print(f'  {key:8s} n={v["main_stage"]["n"]:5d} raw {v["raw"]["mae_m"]:6.2f} / {v["raw"]["bias_m"]:7.2f} | main '
              f'{v["main_stage"]["mae_m"]:6.2f} / {v["main_stage"]["bias_m"]:7.2f} | tall {v["tall_stage"]["mae_m"]:6.2f} '
              f'/ {v["tall_stage"]["bias_m"]:7.2f} | other osm_part choice {val_alt[key]["tall_stage"]["mae_m"]:6.2f} '
              f'/ {val_alt[key]["tall_stage"]["bias_m"]:7.2f} | as proposed {val_prop[key]["tall_stage"]["mae_m"]:6.2f} '
              f'/ {val_prop[key]["tall_stage"]["bias_m"]:7.2f}', flush=True)
    # osm_part rows themselves, as a check (anchors: osm calibration rows only, so they are out of sample).
    pi = np.flatnonzero(pool_part & tgt_ok & (h25 >= Tt))
    Ao = anchors_of(pool_osm)
    alp = tall_alpha(Ao, tall_bands(Ao), T['xyz'][pi], h25[pi], T['prov'][pi], N, wt, sat, radii)[0]
    part_check = {'n': int(len(pi)), 'raw': metrics(real[pi], h25[pi]),
                  'alpha_x_h25_from_osm_anchors': metrics(real[pi], np.clip(alp * h25[pi], *H_CLAMP)),
                  'note': 'osm_part rows are tagged only where taller than the 2.5D value, so both are biased low here'}
    # Spatial reach of the systematic error: correlation of log(real / h25) between pairs of tall truth rows with a
    # similar h25, by distance.
    ai = np.flatnonzero(C & (h25 >= TALL_ANCHOR_H25))
    pr = cKDTree(T['xyz'][ai]).query_pairs(25000, output_type='ndarray')
    pd_ = np.linalg.norm(T['xyz'][ai[pr[:, 0]]] - T['xyz'][ai[pr[:, 1]]], axis=1)
    h1, h2 = h25[ai[pr[:, 0]]], h25[ai[pr[:, 1]]]
    simp = np.abs(h1 - h2) <= TALL_WINDOW * np.maximum(h1, h2)
    lr1, lr2 = np.log(real[ai[pr[:, 0]]] / h1), np.log(real[ai[pr[:, 1]]] / h2)
    reach = {}
    for lo, hi in ((0, 200), (200, 500), (500, 1000), (1000, 2000), (2000, 5000), (5000, 10000), (10000, 25000)):
        m = simp & (pd_ >= lo) & (pd_ < hi)
        reach[f'{lo}-{hi} m'] = {'pairs': int(m.sum()), 'corr_log_alpha': round(float(np.corrcoef(lr1[m], lr2[m])[0, 1]), 3)}
    # Distance from each held-out tall row to the nearest similar training anchor (what the block hold-out leaves).
    Ah = anchors_of(pool_osm & train)
    hq = np.flatnonzero(test & (h25 >= TALL_ANCHOR_H25))
    near = np.full(len(hq), np.inf)
    for j, i in enumerate(hq):
        lo_, hi_ = np.searchsorted(Ah['h25'], h25[i] * (1 - TALL_WINDOW)), np.searchsorted(Ah['h25'], h25[i] * (1 + TALL_WINDOW), 'right')
        if hi_ > lo_:
            near[j] = np.linalg.norm(Ah['xyz'][lo_:hi_] - T['xyz'][i], axis=1).min()
    reach['holdout_nearest_similar_anchor_m_p25_p50_p75'] = [round(float(v)) for v in np.percentile(near, [25, 50, 75])]

    # Saturation of the 2.5D values (truth rows with a 2.5D value, not junk / mass-tagged).
    okr = ~T['junk'] & ~T['uniform'] & T['has25']
    satf = {'max_h25_m': round(float(np.nanmax(h25[okr])), 2), 'bins': {}}
    for lo, hi in ((60, 80), (80, 90), (90, 94), (94, 96), (96, 98), (98, 100)):
        s = okr & (h25 >= lo) & (h25 < hi)
        if s.any():
            q = np.percentile(real[s], [10, 50, 90])
            satf['bins'][f'h25 {lo}-{hi} m'] = {'n': int(s.sum()), 'real_p10_p50_p90': [round(float(v), 1) for v in q],
                                               'median_alpha': round(float(np.median(real[s] / h25[s])), 3)}
    for lo, hi in ((100, 130), (130, 200), (200, 600)):
        s = okr & (real >= lo) & (real < hi)
        satf['bins'][f'real {lo}-{hi} m'] = {'n': int(s.sum()),
                                            'h25_p10_p50_p90': [round(float(v), 1) for v in np.percentile(h25[s], [10, 50, 90])],
                                            'share_h25_ge_90': round(float(np.mean(h25[s] >= 90)), 3)}

    # Final anchors: every eligible row.
    fm = pool_osm | (pool_part if use_part else np.zeros(len(real), bool))
    fi = np.flatnonzero(fm)
    fi = fi[np.argsort(h25[fi], kind='stable')]
    bands = tall_bands(anchors_of(fm))
    provs = sorted(set(T['prov'][fi].tolist()))
    P['tall'] = {
        'model': 'targets: g25 rows of calibrated groups with raw h25 >= T. alpha = weighted median of real / h25 over '
                 'anchors whose h25 is within the window of the target\'s raw h25 (saturated targets: every saturated '
                 'anchor), from the smallest radius holding >= N such anchors; past the last radius, fallback "main" keeps '
                 'the main-stage value, fallback "band" tries the province, then the national band alpha. alpha clamped; '
                 'height = exp(log main + lam (log clip(alpha x raw h25, height clamp) - log main)). Rows with raw '
                 'h25 < T keep the main-stage value. Always applied from raw h25 (see raw_source).',
        'raw_source': f'raw 2.5D values (height_src g25) exist only in the by_area files of commit {RAW_COMMIT}; export them with '
                      f'"git archive {RAW_COMMIT} buildings/by_area | tar -x" into data/buildings/_calib and apply with '
                      f'--src-dir data/buildings/_calib/raw_by_area --out-dir buildings/by_area (main + tall stage, from raw)',
        'settings': {'threshold_t_m': Tt, 'min_anchors_n': N, 'weights': wt, 'saturation_h25_m': sat,
                     'radii_m': list(radii), 'fallback': fbk, 'lam': lam,
                     'osm_part_anchors': bool(use_part), 'window': TALL_WINDOW,
                     'alpha_clamp': list(TALL_ALPHA_CLAMP), 'distance_d0_m': TALL_D0, 'similarity_floor': TALL_SIM_FLOOR,
                     'anchor_rule': f'clean truth rows (main-stage exclusions) with h25 >= {TALL_ANCHOR_H25:g} m or '
                                    f'real >= {TALL_ANCHOR_REAL:g} m',
                     'weights_rule': {'uniform': '1', 'distance': f'1 / (1 + d / {TALL_D0:g} m)',
                                      'similarity': f'max({TALL_SIM_FLOOR}, 1 - |h25_anchor - h25| / ({TALL_WINDOW} h25)); '
                                                    '1 between saturated values',
                                      'both': 'distance x similarity'}},
        'band_edges_h25_m': list(TALL_BAND_EDGES), 'band_alpha': bands,
        'grid': {'T': list(TALL_T_GRID), 'N': list(TALL_N_GRID), 'weights': list(TALL_WEIGHTS),
                 'saturation': list(TALL_SAT_GRID), 'radii': [list(r) for r in TALL_RADII_GRID],
                 'lam': list(TALL_LAMBDA_GRID), 'fallback': list(TALL_FALLBACKS), 'osm_part': [False, True]},
        'selection': {'criterion': 'inner 5-fold block CV MAE (m) over all training calibration rows',
                      'inner_cv_main_stage': base_cv, 'chosen': ckey, 'chosen_score': sel[ckey], 'findings': findings},
        'osm_part_finding': {'holdout_other_choice': val_alt, 'osm_part_rows_out_of_sample': part_check},
        'spatial_reach': reach,
        'saturation_finding': satf,
        'validation': {'holdout_rows': int(len(ti)), 'targets': int(len(qi)), 'tiers': tiers,
                       'rows_below_t_unchanged': unchanged, 'per_band': val,
                       'as_proposed': {'config': pk, 'per_band': val_prop}},
        'n_anchors': int(len(fi)), 'provinces': provs,
        'anchors': {'lon_e6': np.round(T['lon'][fi] * 1e6).astype(int).tolist(),
                    'lat_e6': np.round(T['lat'][fi] * 1e6).astype(int).tolist(),
                    'h25_dm': dm_of(h25[fi]).astype(int).tolist(),
                    'real_dm': dm_of(real[fi]).astype(int).tolist(),
                    'prov': [provs.index(p) for p in T['prov'][fi].tolist()],
                    'w_milli': np.round(bw[fi] * 1000).astype(int).tolist()},
    }
    write_params(a.params, P)
    print(f'wrote {a.params} | {time.time() - t0:.0f}s')


def tall_from_params(P):
    """Anchor set, band alphas, band edges and settings of the tall stage as stored in the parameter file (None when
    absent)."""
    tp = P.get('tall')
    if not tp:
        return None
    A = tp['anchors']
    h = np.array(A['h25_dm'], float) / 10
    prov = np.array(tp['provinces'], object)[np.array(A['prov'], int)]
    anc = tall_anchors(np.array(A['lon_e6']) / 1e6, np.array(A['lat_e6']) / 1e6, h, np.array(A['real_dm'], float) / 10,
                       prov, np.array(A['w_milli'], float) / 1000)
    return anc, tp['band_alpha'], tp['band_edges_h25_m'], tp['settings']


# ---------------------------------------------------------------------------------------------------- apply

def load_params(path, sources=None, frontrow=None, alpha=False):
    """Parameters written by fit; the clamps of the file replace the module's, so apply follows the file. `sources`:
    height_sources.json (Wikidata and sibling rows, extra_heights.py build), or None for none; `frontrow`:
    frontrow_heights.json (front-row house ids, frontrow_heights.py build), or None for none."""
    global RATIO_CLAMP, H_CLAMP, EST_CLAMP
    P = json.load(open(path, encoding='utf-8'))
    if sources:
        from extra_heights import load_sources
        P['_sources'] = load_sources(sources)
    if frontrow:
        from frontrow_heights import load_frontrow
        P['_frontrow'] = load_frontrow(frontrow)
        if alpha:
            from alpha_frontrow import alpha_file_names
            P['_alpha_files'] = alpha_file_names()
    st = P['settings']
    RATIO_CLAMP, H_CLAMP, EST_CLAMP = tuple(st['ratio_clamp']), tuple(st['height_clamp']), tuple(st['est_clamp'])
    A = P['anchors']
    P['_anchor_xyz'] = xyz(np.array(A['lon_e6']) / 1e6, np.array(A['lat_e6']) / 1e6)
    P['_anchor_res'] = np.array(A['res_milli'], float) / 1000
    P['_anchor_lh'] = np.log(np.array(A['h25_dm'], float) / 10)
    P['_tall'] = tall_from_params(P)
    return P


def apply_table(tbl, P, dk, dc, province=None, name=None):
    """Compact table -> (new table, details). Rows with height_src 'g25' in a calibrated group get 'g25cal' (main
    stage, then the tall stage for raw h25 >= T when the parameters hold one; `province`: ISO code of the file, for
    the province tier), rows without a height get 'est' (when the estimate is on); then the rows of file `name` listed
    in the height sources (P['_sources']) get 'wikidata', else 'sibling', with the listed height and floors; then the
    front-row houses of the file (P['_frontrow']) that are now 'g25cal' / 'est' get at least the minimum floors and
    height ('frontrow', where that changes them); 'osm' / 'osm_part' and every other column are untouched. A table
    that already holds a calibrated source (NEW_SRC) is returned as is (the raw 2.5D value is gone)."""
    from scipy.spatial import cKDTree
    src = src_strings(tbl)
    if np.isin(src, NEW_SRC).any():
        return tbl, {'skipped': 'already calibrated'}
    f = features(tbl)
    hdm = tbl['height_dm'].combine_chunks()
    hnull = hdm.is_null().to_numpy(zero_copy_only=False)
    hv = hdm.fill_null(0).to_numpy().astype(float) / 10
    F = {**f, 'h25': np.where(src == 'g25', hv, np.nan),
         'dens': density_of(np.round(f['lon'] * 1e6), np.round(f['lat'] * 1e6), dk, dc)}
    groups = P['groups']
    F['group'] = assign(F, [(q['name'], q['rule']) for q in groups])
    new_h = hv.copy(); new_src = src.copy()
    # Calibrated 2.5D.
    gi = np.flatnonzero((src == 'g25') & np.array([q['on'] for q in groups])[F['group']])
    if len(gi):
        q = xyz(F['lon'][gi], F['lat'][gi])
        L = P['local']
        corr = local_corr(P['_anchor_xyz'], P['_anchor_res'], P['_anchor_lh'], q, np.log(F['h25'][gi]),
                          L['radius_m'], L['shrink_k'], L['sigma_log_h25'])
        new_h[gi] = calibrate(F['h25'][gi], line_pred(F, gi, groups), corr)
        new_src[gi] = 'g25cal'
    # Tall stage: alpha of nearby anchors with a similar raw 2.5D value, from the raw h25 (never a calibrated value).
    tall_det = {}
    if len(gi) and P.get('_tall'):
        A, bands, edges, st = P['_tall']
        ti = gi[F['h25'][gi] >= st['threshold_t_m']]
        if len(ti):
            radii = tuple(st['radii_m'])
            al, used, tier, dmax = tall_alpha(A, bands, xyz(F['lon'][ti], F['lat'][ti]), F['h25'][ti],
                                              np.full(len(ti), province, object), st['min_anchors_n'], st['weights'],
                                              st['saturation_h25_m'], radii, window=st['window'],
                                              d0=st['distance_d0_m'], sim_floor=st['similarity_floor'],
                                              alpha_clamp=tuple(st['alpha_clamp']), band_edges=tuple(edges))
            main_h = new_h[ti].copy()
            new_h[ti] = tall_height(main_h, F['h25'][ti], al, tier, len(radii), st['lam'], st['fallback'])
            tall_det = {'tall_rows': ti, 'tall_alpha': al, 'tall_used': used, 'tall_tier': tier, 'tall_dmax': dmax,
                        'tall_main_h': main_h}
    # Estimates for buildings without a height, from the raw 2.5D heights of g25 neighbours in this file.
    ei = np.flatnonzero(hnull & (src == None)) if P['est']['on'] else np.zeros(0, int)   # noqa: E711
    if len(ei):
        nbr = int(P['est']['neighbour_radius_m'])
        g = np.flatnonzero(src == 'g25')
        nb = (nb_stats(cKDTree(xyz(F['lon'][g], F['lat'][g])), hv[g], xyz(F['lon'][ei], F['lat'][ei]), radii=(float(nbr),))
              if len(g) else {f'nb{nbr}': np.full(len(ei), np.nan)})
        E = {'area': F['area'][ei], 'width': F['width'][ei], 'length': F['length'][ei], 'dens': F['dens'][ei],
             'h25': np.full(len(ei), np.nan), f'nb{nbr}': nb[f'nb{nbr}']}
        egs = P['est']['groups']
        E['egroup'] = assign(E, [(q['name'], q['rule']) for q in egs])
        new_h[ei] = est_pred(E, np.arange(len(ei)), egs, nbr)
        new_src[ei] = 'est'
    # Wikidata, then sibling rows (precedence osm / osm_part > wikidata > sibling > g25cal > est).
    xi, xfl = [], []
    ex = P.get('_sources', {}).get(name) if name else None
    if ex:
        ids = tbl['id'].to_numpy()
        pos = {int(v): k for k, v in enumerate(ids.tolist())}
        done = set()
        for kind in ('wikidata', 'sibling'):
            for bid, (h_dm, fl) in sorted(ex[kind].items()):
                if bid not in pos:
                    raise RuntimeError(f'{name}: {kind} row {bid} is not in the file')
                k = pos[bid]
                if src[k] in TRUTH_SRC or k in done:
                    continue                         # OSM heights are never replaced; a Wikidata row is not a sibling
                if kind == 'sibling' and src[k] is not None and src[k] not in ('g25',):
                    raise RuntimeError(f'{name}: sibling row {bid} has raw height_src {src[k]}')
                new_h[k] = h_dm / 10; new_src[k] = kind
                xi.append(k); xfl.append(fl); done.add(k)
    xi = np.array(xi, int)
    fr = P.get('_frontrow', {}).get(name) if name else None
    from frontrow_heights import min_floors_applies
    minfl = fr is not None and len(fr) > 0 and min_floors_applies(name)
    # Front-row alpha (the user's factor, every file): listed g25cal houses get min(cap, max(h, alpha x raw h25)).
    ai = np.zeros(0, int)
    if fr is not None and len(fr) and P.get('_alpha_files'):
        from alpha_frontrow import alpha_rows
        from frontrow_heights import MIN_H_DM
        ai, ah = alpha_rows(name, tbl['id'].to_numpy(), new_src, F['h25'], new_h, fr, P['_alpha_files'])
        if minfl:
            up = ah > MIN_H_DM / 10                  # at or under the minimum the front-row rule below decides
            ai, ah = ai[up], ah[up]
        new_h[ai] = ah
        new_src[ai] = 'frontrow_alpha'
    # Front-row prior (a domain rule, Ha Noi only): listed houses now g25cal / est get at least MIN_FLOORS floors and height.
    fi = np.zeros(0, int); fr_prev = {}
    if minfl:
        from frontrow_heights import frontrow_rows, MIN_FLOORS, MIN_H_DM
        cur_fl = floors_of(new_h)
        if len(xi):
            cur_fl[xi] = np.array(xfl, np.uint8)
        fi = frontrow_rows(tbl['id'].to_numpy(), new_src, dm_of(new_h), cur_fl, fr)
        fr_prev = {s: int((new_src[fi] == s).sum()) for s in ('g25cal', 'est')}
        new_h[fi] = np.maximum(new_h[fi], MIN_H_DM / 10)
        new_src[fi] = 'frontrow'
    chg = np.unique(np.r_[gi, ei, xi, ai, fi].astype(int))
    if not len(chg):
        return tbl, {'changed': {}}
    tbl = tbl.combine_chunks()
    dm = hdm.fill_null(0).to_numpy().astype(np.uint16)
    dm[chg] = dm_of(new_h[chg])
    fl = tbl['floors'].combine_chunks()
    fnull = fl.is_null().to_numpy(zero_copy_only=False)
    flv = fl.fill_null(0).to_numpy().astype(np.uint8)
    fl_old = np.where(fnull, -1, flv.astype(int))
    flv[chg] = floors_of(new_h[chg])
    if len(xi):
        flv[xi] = np.array(xfl, np.uint8)            # listed floors (Wikidata P1101 / the siblings' floors)
    if minfl and (len(fi) or len(ai)):
        from frontrow_heights import MIN_FLOORS
        fa = np.r_[fi, ai].astype(int)
        flv[fa] = np.maximum(flv[fa], MIN_FLOORS)    # front-row minimum floors
    keep_null = hnull.copy(); keep_null[chg] = False
    fkeep_null = fnull.copy(); fkeep_null[chg] = False
    sch = tbl.schema
    cols = {'height_dm': pa.array(dm, pa.uint16(), mask=keep_null),
            'height_src': pa.array(new_src.tolist(), pa.string()).dictionary_encode(),
            'floors': pa.array(flv, pa.uint8(), mask=fkeep_null)}
    for name, v in cols.items():
        i = sch.get_field_index(name)
        tbl = tbl.set_column(i, sch.field(i), v.cast(sch.field(i).type))
    det = {'changed': {s: int((new_src[chg] == s).sum()) for s in NEW_SRC}, 'rows': chg,
           'value_changed': int((dm[chg].astype(int) != np.where(hnull[chg], -1, np.round(hv[chg] * 10)).astype(int)).sum()),
           'floors_changed': int((flv[chg].astype(int) != fl_old[chg]).sum()),
           'old_dm': np.where(hnull[chg], -1, hv[chg] * 10).round().astype(int), 'new_dm': dm[chg].astype(int),
           'group': F['group'][chg], 'area': F['area'][chg], 'width': F['width'][chg], 'length': F['length'][chg],
           'frontrow_prev': fr_prev, **tall_det}
    return tbl, det


def _apply_file(args):
    path, out_path, params, density, no_est, province, sources, frontrow, alpha = args
    P = load_params(params, sources, frontrow, alpha)
    if no_est:
        P['est']['on'] = False
    dk, dc = density
    t = pq.read_table(path)
    t2, det = apply_table(t, P, dk, dc, province, os.path.basename(path))
    if 'skipped' in det or not det.get('changed') or not sum(det['changed'].values()):
        return os.path.basename(path), det.get('skipped', 'unchanged'), None, {}
    for c in t.column_names:                         # every other column must be identical
        if c not in ('height_dm', 'height_src', 'floors') and not t[c].equals(t2[c]):
            raise RuntimeError(f'{path}: column {c} changed')
    tmp = out_path + '.tmp'
    pq.write_table(t2, tmp, compression='zstd', compression_level=19, row_group_size=500_000)
    for attempt in range(10):                        # another process (indexer, antivirus) may hold the file briefly
        try:
            os.replace(tmp, out_path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(1 + attempt)
    extra = {k: det[k] for k in ('value_changed', 'floors_changed')}
    extra['tall_targets'] = int(len(det.get('tall_rows', ())))
    for s, v in det.get('frontrow_prev', {}).items():
        extra[f'frontrow_from_{s}'] = v
    return os.path.basename(path), det['changed'], os.path.getsize(out_path), extra


def cmd_apply(a):
    from concurrent.futures import ProcessPoolExecutor
    t0 = time.time()
    src_dir = a.src_dir
    out_dir = a.out_dir or src_dir
    idx_path = os.path.join(src_dir, 'index.json')
    idx = json.load(open(idx_path, encoding='utf-8'))
    all_files = [os.path.join(src_dir, e['file']) for e in idx['files']]
    density = density_grid([os.path.join(a.density_dir, e['file']) for e in idx['files']]) if a.density_dir else density_grid(all_files)
    print(f'density cells {len(density[0])} | {time.time() - t0:.0f}s', flush=True)
    files = [os.path.join(src_dir, f) for f in a.files] if a.files else all_files
    prov = {e['file']: e['province_iso'] for e in idx['files']}
    os.makedirs(out_dir, exist_ok=True)
    sources = None if a.no_sources else a.sources
    frontrow = None if a.no_frontrow else a.frontrow
    jobs = [(f, os.path.join(out_dir, os.path.basename(f)), a.params, density, a.no_est, prov.get(os.path.basename(f)), sources,
             frontrow, not a.no_alpha) for f in files]
    changed, total = {}, {s: 0 for s in NEW_SRC}
    with ProcessPoolExecutor(min(a.workers, 6)) as ex:
        vc = {'value_changed': 0, 'floors_changed': 0, 'tall_targets': 0, 'frontrow_from_g25cal': 0, 'frontrow_from_est': 0}
        for name, ch, size, extra in ex.map(_apply_file, jobs):
            if size is None:
                print(f'{name}: {ch}', flush=True)
                continue
            changed[name] = size
            for s in NEW_SRC:
                total[s] += ch.get(s, 0)
            for k2 in vc:
                vc[k2] += extra.get(k2, 0)
            print(f'{name}: {ch} | {size} bytes | {time.time() - t0:.0f}s', flush=True)
    same_dir = os.path.normcase(os.path.abspath(out_dir)) == os.path.normcase(os.path.abspath(src_dir))
    if same_dir or os.path.exists(os.path.join(out_dir, 'index.json')):
        p = os.path.join(out_dir, 'index.json') if os.path.exists(os.path.join(out_dir, 'index.json')) else idx_path
        idx2 = json.load(open(p, encoding='utf-8'))
        for e in idx2['files']:
            if e['file'] in changed:
                e['bytes'] = changed[e['file']]
        with open(p, 'w', encoding='utf-8', newline='\n') as fh:   # LF, no final newline: the committed bytes
            json.dump(idx2, fh, ensure_ascii=False, indent=1)
    print(f'changed files {len(changed)} | buildings {total} | {vc} | {time.time() - t0:.0f}s')
    if a.log:
        json.dump({'files': changed, 'buildings': total, **vc}, open(a.log, 'w', encoding='utf-8'), indent=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('truth', help='ground-truth rows + their 2.5D values + density grid -> data/buildings/_calib/')
    p.add_argument('--dst-dir', default=BY_AREA)
    p.add_argument('--workers', type=int, default=4); p.add_argument('--threads', type=int, default=6)
    p = sub.add_parser('fit', help='fit, validate, write the parameters')
    p.add_argument('--params', default=os.path.join(REPO, 'buildings', 'height_calibration.json'))
    p = sub.add_parser('tall', help='fit and validate the tall-building stage, add it to the parameters')
    p.add_argument('--params', default=os.path.join(REPO, 'buildings', 'height_calibration.json'))
    p = sub.add_parser('apply', help='rewrite height_dm / height_src / floors of published files')
    p.add_argument('--params', default=os.path.join(REPO, 'buildings', 'height_calibration.json'))
    p.add_argument('--src-dir', default=BY_AREA)
    p.add_argument('--out-dir', help='write here instead of in place (testing)')
    p.add_argument('--density-dir', help='files to count density from (default: --src-dir)')
    p.add_argument('--files', nargs='*', help='only these files (names in --src-dir)')
    p.add_argument('--workers', type=int, default=5, help='processes (at most 6)')
    p.add_argument('--no-est', action='store_true', help='leave buildings without a height as they are')
    p.add_argument('--sources', default=SOURCES, help='Wikidata / sibling rows (extra_heights.py build)')
    p.add_argument('--no-sources', action='store_true', help='calibration only, no Wikidata / sibling rows')
    p.add_argument('--frontrow', default=FRONTROW, help='front-row house ids (frontrow_heights.py build)')
    p.add_argument('--no-frontrow', action='store_true', help='no front-row minimum (and no front-row alpha)')
    p.add_argument('--no-alpha', action='store_true', help='no front-row alpha')
    p.add_argument('--log', help='write the changed files (name -> bytes) and counts to this JSON')
    a = ap.parse_args()
    {'truth': cmd_truth, 'fit': cmd_fit, 'tall': cmd_tall, 'apply': cmd_apply}[a.cmd](a)


if __name__ == '__main__':
    main()
