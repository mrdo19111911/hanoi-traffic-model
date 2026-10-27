"""Facade heights from Mapillary street images: an independent check of the published building heights.

Measurement only: nothing here writes to buildings/by_area.

For each street image (Mapillary, CC BY-SA 4.0) with semantic detections and an SfM camera pose
(computed_rotation, angle-axis world->camera, OpenSfM convention: camera x right, y down, z forward; world
x east, y north, z up), every image column whose building mask has a clear top edge (sky directly above it)
gives one measurement: the column's world ray at the mask top has azimuth az and elevation el; a horizontal ray
from the camera along az hits the first footprint edge at distance d; facade height = cam_h + d * tan(el).
Column measurements are pooled per (image, building), then per building.

Subcommands (all outputs go to --out, a scratch directory):
  prep     buildings near the images -> bld.npz (footprints in a local metric frame, published / raw / OSM heights,
           tube and front-row flags)
  measure  per-column measurements -> cols.npz
  report   validation, ratio tables and the dry run -> report.json, plus debug overlays
  national counts of the proposed rule's scope over every by_area file -> national.json
"""
import argparse, base64, glob, json, math, os, struct, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import shapely
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BY_AREA = ROOT / 'buildings' / 'by_area'
RAW = ROOT / 'data' / 'buildings' / '_calib' / 'raw_by_area'
TRUTH = ROOT / 'data' / 'buildings' / '_calib' / 'truth.parquet'
ROADS = ROOT / 'data' / 'buildings' / 'osm_roads.parquet'

LON0, LAT0 = 105.83, 21.02
KX, KY = 111320 * math.cos(math.radians(LAT0)), 110574
MAIN_HW = {'trunk', 'trunk_link', 'primary', 'primary_link', 'secondary', 'secondary_link', 'tertiary', 'tertiary_link'}

CAM_H = 1.3          # camera height above the street (m); dashcam shots, see the Mapillary README
D_MIN, D_MAX = 2.0, 40.0
COL_STEP = 2         # every 2nd thumbnail column
SKY_ABOVE = 2        # rows directly above the mask top that must be sky
MIN_COLS = 5         # per (image, building)
FRONT_M = 15.0
NEAR_IMG_M = 80.0

# paint order: later overwrites earlier
CLASSES = [
    ('sky', ['nature--sky']),
    ('vegetation', ['nature--vegetation', 'nature--terrain']),
    ('building', ['construction--structure--building']),
    ('wall', ['construction--barrier--wall', 'construction--barrier--fence']),
    ('ground', ['construction--flat--', 'construction--barrier--curb', 'marking--', 'object--manhole',
                'object--catch-basin', 'void--ground']),
    ('wire', ['object--wire-group']),
    ('pole', ['object--support--', 'object--street-light', 'object--traffic-light', 'object--traffic-sign']),
    ('sign', ['object--sign--', 'object--banner']),
    ('vehicle', ['object--vehicle--']),
    ('person', ['human--']),
    ('bridge', ['construction--structure--bridge', 'construction--structure--tunnel']),
    ('exclude', ['void--ego-vehicle', 'void--car-mount']),
]
CI = {c: i + 1 for i, (c, _) in enumerate(CLASSES)}


def to_xy(lon, lat):
    return (np.asarray(lon) - LON0) * KX, (np.asarray(lat) - LAT0) * KY


# ---------------------------------------------------------------- vector tile decoding (own implementation)

def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]; i += 1
        r |= (c & 0x7F) << s; s += 7
        if not c & 0x80:
            return r, i


def _fields(b):
    i, n = 0, len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            L, i = _varint(b, i); v = b[i:i + L]; i += L
        elif w == 1:
            v = b[i:i + 8]; i += 8
        elif w == 5:
            v = b[i:i + 4]; i += 4
        else:
            raise ValueError('wire type')
        yield f, w, v


def _packed(b):
    out, i = [], 0
    while i < len(b):
        v, i = _varint(b, i); out.append(v)
    return out


def _zz(n):
    return (n >> 1) ^ -(n & 1)


def mvt_polygons(blob):
    """Base64 MVT -> list of (extent, rings) for polygon features."""
    out = []
    for f, w, v in _fields(base64.b64decode(blob)):
        if f != 3:
            continue
        extent, feats = 4096, []
        for lf, lw, lv in _fields(v):
            if lf == 2: feats.append(lv)
            elif lf == 5: extent = lv
        for fb in feats:
            typ, geom = 0, []
            for ff, fw, fv in _fields(fb):
                if ff == 3: typ = fv
                elif ff == 4: geom = _packed(fv)
            if typ != 3:
                continue
            rings, cur, x, y, i = [], None, 0, 0, 0
            while i < len(geom):
                c = geom[i]; i += 1
                cid, cnt = c & 7, c >> 3
                if cid in (1, 2):
                    for _ in range(cnt):
                        x += _zz(geom[i]); y += _zz(geom[i + 1]); i += 2
                        if cid == 1:
                            cur = [(x, y)]; rings.append(cur)
                        else:
                            cur.append((x, y))
            out.append((extent, rings))
    return out


def class_of(value):
    for i, (c, prefs) in enumerate(CLASSES):
        if any(value.startswith(p) for p in prefs):
            return i + 1
    return 0


def label_map(det_path, W, H):
    det = json.loads(Path(det_path).read_text(encoding='utf-8'))
    per = {}
    for d in det.get('data', []):
        ci = class_of(d['value'])
        if not ci:
            continue
        try:
            per.setdefault(ci, []).extend(mvt_polygons(d['geometry']))
        except Exception:
            continue
    lab = np.zeros((H, W), np.uint8)
    for ci in sorted(per):
        m = Image.new('L', (W, H), 0); dr = ImageDraw.Draw(m)
        for ext, rings in per[ci]:
            sx, sy = W / ext, H / ext
            sign0 = None
            for r in rings:
                if len(r) < 3:
                    continue
                a = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(r, r[1:] + r[:1]))
                if sign0 is None:
                    sign0 = np.sign(a)
                dr.polygon([(x * sx, y * sy) for x, y in r], fill=255 if np.sign(a) == sign0 else 0)
        lab[np.asarray(m) > 0] = ci
    return lab


# ---------------------------------------------------------------- camera

def rodrigues(r):
    r = np.asarray(r, float); th = np.linalg.norm(r)
    if th < 1e-12:
        return np.eye(3)
    k = r / th
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + math.sin(th) * K + (1 - math.cos(th)) * K @ K


def pixel_rays(u, v, W, H, focal, k1, k2, R):
    """Thumbnail pixel centres -> unit world directions (E, N, U). OpenSfM perspective model, normalised by max(W, H)."""
    S = max(W, H)
    px = (np.asarray(u, float) + 0.5 - W / 2) / S / focal
    py = (np.asarray(v, float) + 0.5 - H / 2) / S / focal
    qx, qy = px.copy(), py.copy()
    for _ in range(20):                       # invert the radial distortion
        r2 = qx * qx + qy * qy
        d = 1 + k1 * r2 + k2 * r2 * r2
        qx, qy = px / d, py / d
    cam = np.stack([qx, qy, np.ones_like(qx)], -1)
    cam /= np.linalg.norm(cam, axis=-1, keepdims=True)
    return cam @ R                            # R^T applied to row vectors


# ---------------------------------------------------------------- images

def load_images(mly):
    mly = Path(mly)
    out = []
    for cam in sorted(glob.glob(str(mly / 'cache' / 'cam_*.json'))):
        c = json.loads(Path(cam).read_text(encoding='utf-8'))
        i = c['id']
        img = mly / 'cache' / f'img_{i}.json'; det = mly / 'cache' / f'det_{i}.json'; th = mly / 'thumbs' / f'{i}.jpg'
        if not (img.exists() and det.exists() and th.exists()):
            continue
        m = json.loads(img.read_text(encoding='utf-8'))
        out.append(dict(id=i, set='main', cam=c, geom=m['computed_geometry']['coordinates'], det=str(det), thumb=str(th),
                        captured_at=m.get('captured_at')))
    site = mly / 'site_nguyentrai'
    for det in sorted(glob.glob(str(site / 'cache' / 'det_*.json'))):
        i = Path(det).stem[4:]
        img = site / 'cache' / f'img_{i}.json'; th = site / f'{i}.jpg'
        if not (img.exists() and th.exists()):
            continue
        m = json.loads(img.read_text(encoding='utf-8'))
        c = {k: m[k] for k in ('camera_parameters', 'computed_rotation', 'width', 'height') if k in m}
        out.append(dict(id=i, set='site', cam=c, geom=m['computed_geometry']['coordinates'], det=det, thumb=str(th),
                        captured_at=m.get('captured_at')))
    val = MLY_CACHE
    for img in sorted(glob.glob(str(val / 'meta' / 'img_*.json'))):
        i = Path(img).stem[4:]
        det = val / 'meta' / f'det_{i}.json'; th = val / 'thumbs' / f'{i}.jpg'
        if not (det.exists() and th.exists()):
            continue
        m = json.loads(Path(img).read_text(encoding='utf-8'))
        if not m.get('computed_geometry'):
            continue
        c = {k: m[k] for k in ('camera_parameters', 'computed_rotation', 'width', 'height') if k in m}
        out.append(dict(id=i, set='val', cam=c, geom=m['computed_geometry']['coordinates'], det=str(det), thumb=str(th),
                        captured_at=m.get('captured_at')))
    seen, uniq = set(), []
    for d in out:
        if d['id'] not in seen and 'computed_rotation' in d['cam'] and 'camera_parameters' in d['cam']:
            seen.add(d['id']); uniq.append(d)
    return uniq


# ---------------------------------------------------------------- buildings

def decode_rows(t):
    """by_area table -> list of shapely polygons (lon/lat). Same geometry as compact_layer.decode()."""
    lon, lat = t['lon_e6'].to_numpy() / 1e6, t['lat_e6'].to_numpy() / 1e6
    L, Wd = t['length_dm'].to_numpy() / 20, t['width_dm'].to_numpy() / 20
    a = np.radians(t['angle_cdeg'].to_numpy() / 100)
    ring = t['ring'].to_pylist()
    out = []
    for k in range(len(lon)):
        if ring[k] is None:
            kx = 111320 * math.cos(math.radians(lat[k])); ky = 110574
            ux, uy = math.sin(a[k]), math.cos(a[k]); vx, vy = -uy, ux
            pts = [(lon[k] + (sx * L[k] * ux + sy * Wd[k] * vx) / kx, lat[k] + (sx * L[k] * uy + sy * Wd[k] * vy) / ky)
                   for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            out.append(shapely.Polygon(pts))
        else:
            d = np.frombuffer(ring[k], '<i2').reshape(-1, 2).astype(np.int64)
            c = np.vstack([[0, 0], np.cumsum(d, axis=0)]) + [int(round(lon[k] * 1e6)), int(round(lat[k] * 1e6))]
            p = shapely.Polygon(c / 1e6)
            if len(np.unique(c, axis=0)) < len(c):
                parts = [x for x in shapely.get_parts(shapely.make_valid(p)) if x.geom_type == 'Polygon']
                p = max(parts, key=lambda x: x.area) if parts else p
            out.append(p)
    return out


def footprint_shape(polys_m):
    """Metric polygons -> (area, long side, short side) from the oriented envelope."""
    area = shapely.area(polys_m)
    env = shapely.oriented_envelope(polys_m)
    L = np.zeros(len(polys_m)); S = np.zeros(len(polys_m))
    for k, e in enumerate(env):
        if e.geom_type != 'Polygon':
            continue
        c = np.asarray(e.exterior.coords)
        a, b = np.hypot(*(c[1] - c[0])), np.hypot(*(c[2] - c[1]))
        L[k], S[k] = max(a, b), min(a, b)
    return area, L, S


def is_tube(area, L, S):
    return (S < 6.5) & (S > 0) & (L / np.maximum(S, 1e-9) >= 1.8) & (area < 300)


def main_roads(bbox, to_metric):
    t = pq.read_table(ROADS, columns=['hw', 'geometry', 'bridge', 'tunnel'])
    hw = np.asarray(t['hw'].to_pylist(), object)
    keep = np.isin(hw, list(MAIN_HW))
    g = shapely.from_wkb(np.asarray(t['geometry'].to_pylist(), object)[keep])
    x0, y0, x1, y1 = bbox
    g = g[shapely.intersects(g, shapely.box(x0, y0, x1, y1))]
    return shapely.transform(g, to_metric)


def cmd_prep(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    imgs = load_images(a.mly)
    lon = np.array([d['geom'][0] for d in imgs]); lat = np.array([d['geom'][1] for d in imgs])
    ix, iy = to_xy(lon, lat)
    from scipy.spatial import cKDTree
    itree = cKDTree(np.c_[ix, iy])
    idx = json.loads((BY_AREA / 'index.json').read_text(encoding='utf-8'))
    m = 0.002
    bb = (lon.min() - m, lat.min() - m, lon.max() + m, lat.max() + m)
    files = [f['file'] for f in idx['files'] if f['file'].startswith('ha_noi_')
             and not (f['bbox'][2] < bb[0] or f['bbox'][0] > bb[2] or f['bbox'][3] < bb[1] or f['bbox'][1] > bb[3])]
    truth = pq.read_table(TRUTH, columns=['id', 'file', 'real', 'floors', 'h25']).to_pydict()
    tmap = {(f, i): (r, fl, h) for i, f, r, fl, h in zip(truth['id'], truth['file'], truth['real'], truth['floors'], truth['h25'])}
    rec = {k: [] for k in ('file', 'id', 'h_pub', 'src', 'h25', 'real', 'floors_truth', 'cx', 'cy')}
    polys = []
    for f in files:
        t = pq.read_table(BY_AREA / f)
        x, y = to_xy(t['lon_e6'].to_numpy() / 1e6, t['lat_e6'].to_numpy() / 1e6)
        near = itree.query(np.c_[x, y], distance_upper_bound=NEAR_IMG_M + 150)[0] <= NEAR_IMG_M + 150
        if not near.any():
            continue
        sel = np.flatnonzero(near)
        ts = t.take(sel)
        ps = decode_rows(ts)
        raw = pq.read_table(RAW / f, columns=['id', 'height_dm', 'height_src']).take(sel)
        assert (raw['id'].to_numpy() == ts['id'].to_numpy()).all()
        rs = np.asarray(raw['height_src'].to_pylist(), object)
        rh = raw['height_dm'].to_numpy(zero_copy_only=False) / 10
        ids = ts['id'].to_numpy()
        for k in range(len(sel)):
            tr = tmap.get((f, int(ids[k])), (np.nan, np.nan, np.nan))
            rec['file'].append(f); rec['id'].append(int(ids[k]))
            rec['h_pub'].append(ts['height_dm'][k].as_py() / 10); rec['src'].append(ts['height_src'][k].as_py())
            rec['h25'].append(rh[k] if rs[k] == 'g25' else (tr[2] if tr[2] is not None and tr[2] >= 2 else np.nan))
            rec['real'].append(tr[0]); rec['floors_truth'].append(tr[1])
        polys.extend(ps)
        print(f, len(sel), flush=True)
    to_metric = lambda c: np.c_[(c[:, 0] - LON0) * KX, (c[:, 1] - LAT0) * KY]
    pm = shapely.transform(np.asarray(polys, object), to_metric)
    cen = shapely.centroid(pm)
    rec['cx'] = shapely.get_x(cen); rec['cy'] = shapely.get_y(cen)
    area, L, S = footprint_shape(pm)
    roads = main_roads(bb, to_metric)
    tree = shapely.STRtree(roads)
    dist = np.full(len(pm), np.inf)
    pi, ri = tree.query(pm, predicate='dwithin', distance=60)
    if len(pi):
        dd = shapely.distance(pm[pi], roads[ri])
        np.minimum.at(dist, pi, dd)
    np.savez_compressed(out / 'bld.npz', fname=np.asarray(rec['file']), id=np.asarray(rec['id']),
                        h_pub=np.asarray(rec['h_pub'], float), src=np.asarray(rec['src']), h25=np.asarray(rec['h25'], float),
                        real=np.asarray(rec['real'], float), floors_truth=np.asarray(rec['floors_truth'], float),
                        cx=np.asarray(rec['cx']), cy=np.asarray(rec['cy']), area=area, L=L, S=S,
                        road_d=dist, wkb=np.asarray(shapely.to_wkb(pm), object))
    print('buildings', len(pm), 'files', len(files), 'images', len(imgs))


# ---------------------------------------------------------------- measurement

_B = {}


def _init(out):
    z = np.load(Path(out) / 'bld.npz', allow_pickle=True)
    pm = shapely.from_wkb(z['wkb'])
    segs, owner = [], []
    for k, p in enumerate(pm):
        for r in [p.exterior, *p.interiors]:
            c = np.asarray(r.coords)
            segs.append(np.c_[c[:-1], c[1:]]); owner.append(np.full(len(c) - 1, k))
    _B['segs'] = np.vstack(segs); _B['owner'] = np.concatenate(owner)
    mid = (_B['segs'][:, :2] + _B['segs'][:, 2:]) / 2
    from scipy.spatial import cKDTree
    _B['tree'] = cKDTree(mid)
    _B['pm'] = pm


def ray_hits(cx, cy, az):
    """Horizontal rays from (cx, cy) at azimuths az (rad, clockwise from north) -> (d, building, |cos incidence|)."""
    near = _B['tree'].query_ball_point([cx, cy], D_MAX + 30)
    n = len(az)
    if not near:
        return np.full(n, np.inf), np.full(n, -1), np.zeros(n)
    s = _B['segs'][near]; own = _B['owner'][near]
    dx, dy = np.sin(az)[:, None], np.cos(az)[:, None]
    ax, ay = s[:, 0] - cx, s[:, 1] - cy
    ex, ey = s[:, 2] - s[:, 0], s[:, 3] - s[:, 1]
    den = dx * ey - dy * ex                                   # cross(dir, e)
    with np.errstate(divide='ignore', invalid='ignore'):
        t = (ax * ey - ay * ex) / den                         # along the ray
        u = (ax * dy - ay * dx) / den                         # along the segment
    ok = (np.abs(den) > 1e-12) & (u >= 0) & (u <= 1) & (t > 0.3)
    t = np.where(ok, t, np.inf)
    j = np.argmin(t, axis=1)
    d = t[np.arange(n), j]
    el = np.hypot(ex, ey)[j]
    cosi = np.abs(dx[:, 0] * (-ey[j]) + dy[:, 0] * ex[j]) / np.maximum(el, 1e-9)   # |dir . normal|
    b = np.where(np.isfinite(d), own[j], -1)
    return d, b, cosi


def measure_image(d):
    W, H = Image.open(d['thumb']).size
    lab = label_map(d['det'], W, H)
    c = d['cam']
    focal, k1, k2 = c['camera_parameters'][:3]
    R = rodrigues(c['computed_rotation'])
    cx, cy = to_xy(d['geom'][0], d['geom'][1])
    cols = np.arange(0, W, COL_STEP)
    bmask = lab == CI['building']
    sky = lab == CI['sky']
    ground = lab == CI['ground']
    rows = []
    for u in cols:
        colb = bmask[:, u]
        if not colb.any():
            continue
        y = int(np.argmax(colb))
        status = 0
        if y < SKY_ABOVE + 1:
            status = 2                                        # cut by the frame top: censored
        elif not sky[y - SKY_ABOVE:y, u].all():
            status = 3                                        # top occluded or not against sky
        elif colb[y:y + 10].sum() < 8:
            status = 4                                        # thin sliver
        # base: lowest building pixel with ground directly below it
        yb = -1
        gb = np.flatnonzero(colb[:-3] & ground[1:-2, u] & ground[2:-1, u])
        if len(gb):
            yb = int(gb[-1])
        rows.append((u, y, yb, status))
    if not rows:
        return d['id'], np.zeros((0, 12))
    r = np.array(rows, float)
    top = pixel_rays(r[:, 0], r[:, 1], W, H, focal, k1, k2, R)
    az = np.arctan2(top[:, 0], top[:, 1]); el = np.arcsin(np.clip(top[:, 2], -1, 1))
    base = pixel_rays(r[:, 0], np.maximum(r[:, 2], 0), W, H, focal, k1, k2, R)
    elb = np.where(r[:, 2] >= 0, np.arcsin(np.clip(base[:, 2], -1, 1)), np.nan)
    dist, b, cosi = ray_hits(cx, cy, az)
    h = CAM_H + dist * np.tan(el)
    with np.errstate(divide='ignore', invalid='ignore'):
        dbase = np.where(elb < -0.01, CAM_H / np.tan(-elb), np.nan)
    # columns: u, ytop, ybase, status, az, el, d, building, cos, h, d_base, W
    return d['id'], np.c_[r, az, el, dist, b, cosi, h, dbase, np.full(len(r), W)]


def cmd_measure(a):
    out = Path(a.out)
    imgs = load_images(a.mly)
    res = {}
    with ProcessPoolExecutor(a.workers, initializer=_init, initargs=(str(out),)) as ex:
        for i, arr in ex.map(measure_image, imgs, chunksize=4):
            res[i] = arr
    ids = [d['id'] for d in imgs]
    allc = np.vstack([np.c_[np.full(len(res[i]), k), res[i]] for k, i in enumerate(ids) if len(res[i])])
    np.savez_compressed(out / 'cols.npz', cols=allc, ids=np.asarray(ids), sets=np.asarray([d['set'] for d in imgs]))
    print('images', len(ids), 'columns', len(allc))


# ---------------------------------------------------------------- report

def per_building(cols, z, cos_min, d_max=D_MAX, base_tol=None):
    """Valid columns -> per (image, building) median -> per building median over images."""
    st, d, b, cosi, h = cols[:, 4], cols[:, 7], cols[:, 8].astype(int), cols[:, 9], cols[:, 10]
    ok = (st == 0) & (d >= D_MIN) & (d <= d_max) & (b >= 0) & (cosi >= cos_min)
    if base_tol is not None:
        db = cols[:, 11]
        ok &= np.isfinite(db) & (np.abs(db / d - 1) <= base_tol)
    # censored share per (image, building): columns hitting the building whose top is cut by the frame
    key = cols[:, 0].astype(np.int64) * 10_000_000 + b
    per_ib = {}
    for k in np.unique(key[ok]):
        m = ok & (key == k)
        if m.sum() < MIN_COLS:
            continue
        allk = (key == k) & (d <= d_max)
        cens = ((st == 2) & allk).sum() / max(allk.sum(), 1)
        if cens > 0.2:
            continue
        per_ib[k] = (int(k % 10_000_000), float(np.median(h[m])), int(m.sum()), float(np.median(d[m])))
    byb = {}
    for k, (bi, hm, n, dm) in per_ib.items():
        byb.setdefault(bi, []).append((hm, n, dm, int(k // 10_000_000)))
    out = {bi: dict(h=float(np.median([x[0] for x in v])), n_img=len(v), n_col=sum(x[1] for x in v),
                    d=float(np.median([x[2] for x in v])), imgs=[x[3] for x in v], each=[x[0] for x in v])
           for bi, v in byb.items()}
    return out


def stats(err):
    err = np.asarray(err, float)
    if not len(err):
        return dict(n=0)
    return dict(n=int(len(err)), mae=round(float(np.mean(np.abs(err))), 2), bias=round(float(np.mean(err)), 2),
                median=round(float(np.median(err)), 2), mad=round(float(np.median(np.abs(err))), 2))


def ratio_stats(r):
    r = np.asarray(r, float); r = r[np.isfinite(r) & (r > 0)]
    if not len(r):
        return dict(n=0)
    lr = np.log(r)
    bs = np.random.default_rng(0).choice(lr, (1000, len(lr))).mean(1) if len(lr) > 1 else lr
    return dict(n=int(len(r)), median=round(float(np.median(r)), 3), geomean=round(float(np.exp(lr.mean())), 3),
                gm_ci90=[round(float(np.exp(np.percentile(bs, 5))), 3), round(float(np.exp(np.percentile(bs, 95))), 3)],
                p25=round(float(np.percentile(r, 25)), 3), p75=round(float(np.percentile(r, 75)), 3))


def cmd_report(a):
    out = Path(a.out)
    z = np.load(out / 'bld.npz', allow_pickle=True)
    cz = np.load(out / 'cols.npz', allow_pickle=True)
    cols, ids, sets = cz['cols'], cz['ids'], cz['sets']
    src, real, h25, hpub = z['src'], z['real'], z['h25'], z['h_pub']
    tube = is_tube(z['area'], z['L'], z['S'])
    front = z['road_d'] < FRONT_M
    rep = dict(images_total=int(len(ids)), images_with_columns=int(len(np.unique(cols[:, 0]))),
               columns=int(len(cols)), status_counts={s: int((cols[:, 4] == k).sum()) for k, s in
                                                      enumerate(['ok', '-', 'censored', 'occluded', 'sliver'])})
    # ---- validation against OSM real heights, for several column filters (chosen by the numbers, not by eye)
    clean = lambda bi: (src[bi] in ('osm', 'osm_part')) and np.isfinite(real[bi]) and 2 <= real[bi] <= 500
    val = {}
    for cos_min in (0.0, 0.26, 0.5):
        for tol, dmx in ((None, 40.0), (None, 25.0), (None, 15.0), (0.3, 40.0)):
            pb = per_building(cols, z, cos_min, d_max=dmx, base_tol=tol)
            bis = [bi for bi in pb if clean(bi)]
            e = [pb[bi]['h'] - real[bi] for bi in bis]
            e25 = [h25[bi] - real[bi] for bi in bis if np.isfinite(h25[bi])]
            val[f'cos>={cos_min}|d<={dmx}|base_tol={tol}'] = dict(facade=stats(e), raw_h25_same_rows=stats(e25),
                                                         n_buildings_all=len(pb),
                                                         ratio_facade_over_real=ratio_stats([pb[bi]['h'] / real[bi] for bi in bis]))
    rep['validation'] = val
    cos_min, tol = a.cos_min, (None if a.base_tol < 0 else a.base_tol)
    pb = per_building(cols, z, cos_min, d_max=a.d_max, base_tol=tol)
    rep['chosen_filter'] = dict(cos_min=cos_min, base_tol=tol, d_max=a.d_max)
    bis = np.array(sorted(pb), int)
    fh = np.array([pb[bi]['h'] for bi in bis])
    # validation details on the chosen filter
    vb = np.array([clean(bi) for bi in bis], bool)
    if vb.any():
        rv = real[bis[vb]]; e = fh[vb] - rv
        rep['validation_chosen'] = dict(all=stats(e), by_real_band={
            f'{lo}-{hi}': stats(e[(rv >= lo) & (rv < hi)]) for lo, hi in ((0, 8), (8, 12), (12, 20), (20, 40), (40, 500))},
            tube=stats(e[tube[bis[vb]]]), rows=[dict(file=str(z['fname'][bi]), id=int(z['id'][bi]), real=float(real[bi]),
                                                    floors=float(z['floors_truth'][bi]), facade=round(float(pb[bi]['h']), 1),
                                                    h25=float(h25[bi]), n_img=pb[bi]['n_img'], d=round(pb[bi]['d'], 1))
                                               for bi in bis[vb]])
    # ---- g25cal ratios
    g = np.array([src[bi] == 'g25cal' for bi in bis], bool)
    gb = bis[g]; gh = fh[g]
    rp, rr = gh / hpub[gb], gh / h25[gb]
    def table(mask):
        return dict(over_published=ratio_stats(rp[mask]), over_raw_h25=ratio_stats(rr[mask]),
                    facade_median_m=round(float(np.median(gh[mask])), 2) if mask.any() else None,
                    published_median_m=round(float(np.median(hpub[gb][mask])), 2) if mask.any() else None,
                    raw_median_m=round(float(np.nanmedian(h25[gb][mask])), 2) if mask.any() else None)
    tb, fr = tube[gb], front[gb]
    hr = h25[gb]
    area_of = []
    for bi in gb:
        ii = pb[bi]['imgs']
        area_of.append(img_area(z['cx'][bi], z['cy'][bi]))
    area_of = np.asarray(area_of)
    T = {'all': table(np.ones(len(gb), bool)),
         'front': table(fr), 'inner': table(~fr), 'tube': table(tb), 'other': table(~tb),
         'tube_front': table(tb & fr), 'tube_inner': table(tb & ~fr), 'other_front': table(~tb & fr), 'other_inner': table(~tb & ~fr)}
    for lo, hi in ((0, 8), (8, 12), (12, 20), (20, 999)):
        mm = (hr >= lo) & (hr < hi)
        T[f'h25_{lo}-{hi}'] = table(mm)
        T[f'tube_front_h25_{lo}-{hi}'] = table(mm & tb & fr)
        T[f'other_h25_{lo}-{hi}'] = table(mm & ~(tb & fr))
    for ar in np.unique(area_of):
        T[f'area_{ar}'] = table(area_of == ar)
        T[f'area_{ar}_tube_front'] = table((area_of == ar) & tb & fr)
    rep['g25cal_ratios'] = T
    # ---- OSM floors vs facade on the same buildings
    of = []
    for bi in bis[vb]:
        fl = z['floors_truth'][bi]
        if np.isfinite(fl) and fl > 0:
            of.append((fl, pb[bi]['h'], real[bi], tube[bi], front[bi]))
    if of:
        of = np.array(of, float)
        fl_fac = 1 + np.maximum(0, np.round((of[:, 1] - 3.9) / 3.3))
        rep['osm_floors_vs_facade'] = dict(n=len(of), facade_floors_minus_osm_floors=stats(fl_fac - of[:, 0]),
                                           tube_front=stats((fl_fac - of[:, 0])[(of[:, 3] > 0) & (of[:, 4] > 0)]),
                                           facade_over_real=ratio_stats(of[:, 1] / of[:, 2]))
    (out / 'report.json').write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=float), encoding='utf-8')
    np.savez_compressed(out / 'measured.npz', bis=bis, fh=fh, n_img=[pb[bi]['n_img'] for bi in bis])
    print(json.dumps({k: rep[k] for k in ('images_total', 'images_with_columns', 'columns', 'status_counts')}))
    print(json.dumps(rep['validation'], indent=0))
    print(json.dumps(rep.get('validation_chosen', {}).get('by_real_band'), indent=0))


AREAS = {  # lon0, lat0, lon1, lat1 (palette_measured.json meta bboxes are similar; these are by nearest centre)
    'old_quarter': (105.8485, 21.0335), 'lang_thanh_cong': (105.8130, 21.0200), 'cau_giay': (105.7980, 21.0330),
    'khuong_thuong_kim_lien': (105.8320, 21.0080), 'nguyen_trai_site': (105.8156, 20.9998)}


def img_area(cx, cy):
    best, bd = None, 1e18
    for k, (lo, la) in AREAS.items():
        x, y = to_xy(lo, la)
        dd = (x - cx) ** 2 + (y - cy) ** 2
        if dd < bd:
            best, bd = k, dd
    return best


# ---------------------------------------------------------------- overlays

def cmd_overlay(a):
    out = Path(a.out)
    z = np.load(out / 'bld.npz', allow_pickle=True)
    cz = np.load(out / 'cols.npz', allow_pickle=True)
    cols, ids = cz['cols'], cz['ids']
    imgs = {d['id']: d for d in load_images(a.mly)}
    pb = per_building(cols, z, a.cos_min, d_max=a.d_max, base_tol=(None if a.base_tol < 0 else a.base_tol))
    tube = is_tube(z['area'], z['L'], z['S'])
    front = z['road_d'] < FRONT_M
    for iid in a.ids:
        k = int(np.flatnonzero(ids == iid)[0]); d = imgs[iid]
        im = Image.open(d['thumb']).convert('RGB'); W, H = im.size
        lab = label_map(d['det'], W, H)
        ov = np.asarray(im).astype(float)
        ov[lab == CI['building']] = ov[lab == CI['building']] * 0.6 + np.array([255, 120, 0]) * 0.4
        ov[lab == CI['sky']] = ov[lab == CI['sky']] * 0.7 + np.array([0, 120, 255]) * 0.3
        im = Image.fromarray(ov.astype(np.uint8)); dr = ImageDraw.Draw(im)
        c = cols[cols[:, 0] == k]
        rng = np.random.default_rng(1)
        colour = {}
        for row in c:
            u, y, st, dd, b, cosi, h = row[1], row[2], row[4], row[7], int(row[8]), row[9], row[10]
            good = st == 0 and D_MIN <= dd <= a.d_max and b >= 0 and cosi >= a.cos_min
            if not good:
                dr.point((u, y), fill=(255, 0, 0) if st == 0 else (120, 120, 120))
                continue
            colour.setdefault(b, tuple(int(x) for x in rng.integers(60, 255, 3)))
            dr.rectangle([u - 1, y - 1, u + 1, y + 1], fill=colour[b])
        for b, colr in colour.items():
            m = (c[:, 8] == b) & (c[:, 4] == 0)
            if m.sum() < MIN_COLS:
                continue
            u = float(np.median(c[m, 1])); y = float(np.median(c[m, 2]))
            hm = float(np.median(c[m, 10])); dm = float(np.median(c[m, 7]))
            txt = f'{hm:.1f}m d{dm:.0f} pub{z["h_pub"][b]:.1f} {z["src"][b]}' + (' T' if tube[b] else '') + (' F' if front[b] else '')
            if np.isfinite(z['real'][b]):
                txt += f' osm{z["real"][b]:.1f}'
            dr.rectangle([u - 2, y - 26, u + 7 * len(txt) - 2, y - 12], fill=(0, 0, 0))
            dr.text((u, y - 25), txt, fill=colr)
        # top-down panel: footprints within 45 m, camera, rays of the measured columns to their hit
        P = H; sc = P / 100.0
        cxm, cym = to_xy(d['geom'][0], d['geom'][1])
        pan = Image.new('RGB', (P, P), (250, 250, 250)); pd = ImageDraw.Draw(pan)
        tf = lambda x, y: (P / 2 + (x - cxm) * sc, P / 2 - (y - cym) * sc)
        pm = shapely.from_wkb(z['wkb'])
        near = np.flatnonzero(np.hypot(z['cx'] - cxm, z['cy'] - cym) < 60)
        for b in near:
            ring = [tf(x, y) for x, y in np.asarray(pm[b].exterior.coords)]
            fill = colour.get(b, (215, 215, 215))
            pd.polygon(ring, fill=fill, outline=(90, 90, 90))
        for row in c:
            b = int(row[8])
            if b in colour and row[4] == 0 and D_MIN <= row[7] <= a.d_max and row[9] >= a.cos_min:
                x1 = cxm + row[7] * math.sin(row[5]); y1 = cym + row[7] * math.cos(row[5])
                pd.line([tf(cxm, cym), tf(x1, y1)], fill=tuple(int(v * 0.6) for v in colour[b]), width=1)
        pd.ellipse([P / 2 - 4, P / 2 - 4, P / 2 + 4, P / 2 + 4], fill=(255, 0, 0))
        pd.text((5, 5), f'{iid}  100 m x 100 m, north up', fill=(0, 0, 0))
        comb = Image.new('RGB', (W + P, H), (255, 255, 255)); comb.paste(im, (0, 0)); comb.paste(pan, (W, 0))
        comb.save(out / f'overlay_{iid}.png')
        print('overlay', iid)


# ---------------------------------------------------------------- national scope count

def _national_file(args):
    f, = args
    t = pq.read_table(BY_AREA / f, columns=['lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring', 'height_src', 'height_dm'])
    src = np.asarray(t['height_src'].to_pylist(), object)
    g = src == 'g25cal'
    nb = 101
    empty = np.zeros(nb, int)
    if not g.any():
        return f, 0, 0, 0, empty, empty
    raw = pq.read_table(RAW / f, columns=['height_dm', 'height_src'])
    rh = np.where(np.asarray(raw['height_src'].to_pylist(), object) == 'g25',
                  raw['height_dm'].to_numpy(zero_copy_only=False) / 10, np.nan)[g]
    t = t.filter(pa_mask(g))
    lat0 = float(np.median(t['lat_e6'].to_numpy())) / 1e6; lon0 = float(np.median(t['lon_e6'].to_numpy())) / 1e6
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110574
    tm = lambda c: np.c_[(c[:, 0] - lon0) * kx, (c[:, 1] - lat0) * ky]
    polys = shapely.transform(np.asarray(decode_rows(t), object), tm)
    area, L, S = footprint_shape(polys)
    tb = is_tube(area, L, S)
    if not tb.any():
        return f, int(g.sum()), 0, 0, empty, empty
    hist = lambda m: np.bincount(np.clip(np.nan_to_num(rh[m], nan=0).astype(int), 0, nb - 1), minlength=nb)
    x0, y0, x1, y1 = shapely.total_bounds(polys)
    rt = _NAT['roads']
    bx = shapely.box(lon0 + (x0 - 100) / kx, lat0 + (y0 - 100) / ky, lon0 + (x1 + 100) / kx, lat0 + (y1 + 100) / ky)
    rr = rt[_NAT['tree'].query(bx, predicate='intersects')]
    if not len(rr):
        return f, int(g.sum()), int(tb.sum()), 0, hist(tb), empty
    rr = shapely.transform(rr, tm)
    tree = shapely.STRtree(rr)
    ti = np.flatnonzero(tb)
    pi, _ = tree.query(polys[ti], predicate='dwithin', distance=FRONT_M)
    fm = np.zeros(len(tb), bool); fm[ti[np.unique(pi)]] = True
    return f, int(g.sum()), int(tb.sum()), int(fm.sum()), hist(tb), hist(fm)


_NAT = {}


def pa_mask(m):
    import pyarrow as pa
    return pa.array(m)


def _nat_init():
    t = pq.read_table(ROADS, columns=['hw', 'geometry'])
    hw = np.asarray(t['hw'].to_pylist(), object)
    _NAT['roads'] = shapely.from_wkb(np.asarray(t['geometry'].to_pylist(), object)[np.isin(hw, list(MAIN_HW))])
    _NAT['tree'] = shapely.STRtree(_NAT['roads'])


def cmd_national(a):
    out = Path(a.out)
    idx = json.loads((BY_AREA / 'index.json').read_text(encoding='utf-8'))
    files = [f['file'] for f in idx['files']]
    res = []
    with ProcessPoolExecutor(a.workers, initializer=_nat_init) as ex:
        for r in ex.map(_national_file, [(f,) for f in files]):
            res.append(r); print(*r[:4], flush=True)
    tot = dict(files=len(res), g25cal=sum(r[1] for r in res), g25cal_tube=sum(r[2] for r in res),
               g25cal_tube_front=sum(r[3] for r in res),
               ha_noi=dict(g25cal=sum(r[1] for r in res if r[0].startswith('ha_noi_')),
                           g25cal_tube=sum(r[2] for r in res if r[0].startswith('ha_noi_')),
                           g25cal_tube_front=sum(r[3] for r in res if r[0].startswith('ha_noi_'))),
               h25_hist_tube=np.sum([r[4] for r in res], 0).tolist(),
               h25_hist_tube_front=np.sum([r[5] for r in res], 0).tolist(),
               h25_hist_tube_front_ha_noi=np.sum([r[5] for r in res if r[0].startswith('ha_noi_')], 0).tolist(),
               per_file=[r[:4] for r in res])
    (out / 'national.json').write_text(json.dumps(tot, indent=1), encoding='utf-8')
    print({k: v for k, v in tot.items() if k != 'per_file'})


# ---------------------------------------------------------------- fetching more images near validation buildings

MLY_CACHE = ROOT / 'data' / 'buildings' / '_mapillary'


def _token():
    t = os.environ.get('MAPILLARY_TOKEN')
    if not t and sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as k:
                t = winreg.QueryValueEx(k, 'MAPILLARY_TOKEN')[0]
        except OSError:
            t = None
    if not t:
        raise SystemExit('MAPILLARY_TOKEN missing')
    return t


_LAST = [0.0]


def _get(url, token, binary=False):
    """GET with the token as a query parameter; at most 2 requests per second. Errors never carry the URL."""
    import time, urllib.request
    wait = 0.5 - (time.time() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.time()
    full = url + (('&' if '?' in url else '?') + 'access_token=' + token if token else '')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(full, timeout=60) as r:
                b = r.read()
            return b if binary else json.loads(b)
        except Exception as e:
            code = getattr(e, 'code', None)
            if code in (400, 401, 403, 404):
                return None
            time.sleep(2 + 3 * attempt)
    return None


def mvt_points(b):
    """MVT bytes -> {layer: (extent, [(props, x, y)])} for point features."""
    out = {}
    for f, w, v in _fields(b):
        if f != 3:
            continue
        name, feats, keys, vals, extent = None, [], [], [], 4096
        for lf, lw, lv in _fields(v):
            if lf == 1: name = lv.decode()
            elif lf == 2: feats.append(lv)
            elif lf == 3: keys.append(lv.decode())
            elif lf == 4:
                val = None
                for vf, vw, vv in _fields(lv):
                    if vf == 1: val = vv.decode('utf-8', 'replace')
                    elif vf == 2: val = struct.unpack('<f', vv)[0]
                    elif vf == 3: val = struct.unpack('<d', vv)[0]
                    elif vf in (4, 5): val = vv
                    elif vf == 6: val = _zz(vv)
                    elif vf == 7: val = bool(vv)
                vals.append(val)
            elif lf == 5: extent = lv
        pts = []
        for fb in feats:
            tags, typ, geom = [], 0, []
            for ff, fw, fv in _fields(fb):
                if ff == 2: tags = _packed(fv)
                elif ff == 3: typ = fv
                elif ff == 4: geom = _packed(fv)
            if typ != 1 or len(geom) < 3:
                continue
            props = {keys[tags[j]]: vals[tags[j + 1]] for j in range(0, len(tags), 2)}
            pts.append((props, _zz(geom[1]), _zz(geom[2])))
        out[name] = (extent, pts)
    return out


def validation_targets():
    """Clean OSM-tagged Hà Nội buildings usable as validation: 6-40 m, not junk, not in a mass-tagged 2 km block
    (>= 50 tagged rows where one value covers >= 85 %, the calibration's rule)."""
    t = pq.read_table(TRUTH, columns=['id', 'file', 'src', 'real', 'lon', 'lat']).to_pydict()
    f = np.asarray(t['file'], str); real = np.asarray(t['real'], float)
    lon, lat = np.asarray(t['lon'], float), np.asarray(t['lat'], float)
    src = np.asarray(t['src'], str)
    blk = np.floor(lon / 0.0193).astype(int) * 100000 + np.floor(lat / 0.018).astype(int)
    mass = np.zeros(len(f), bool)
    for b in np.unique(blk):
        m = blk == b
        if m.sum() >= 50:
            _, cnt = np.unique(np.round(real[m], 1), return_counts=True)
            if cnt.max() / m.sum() >= 0.85:
                mass |= m
    keep = np.char.startswith(f, 'ha_noi_') & (src == 'osm') & (real >= 6) & (real <= 40) & ~mass
    return lon[keep], lat[keep], np.asarray(t['id'])[keep], f[keep]


def cmd_fetch(a):
    """Coverage tiles (z14 mly1_public image layer) -> images within 30 m of a validation building, local 07:00-17:30,
    non-pano; one image per 20 m cell, at most 3 per sequence; per image: metadata, detections, 1024 px thumb."""
    token = _token()
    cache = MLY_CACHE; (cache / 'tiles').mkdir(parents=True, exist_ok=True)
    (cache / 'meta').mkdir(exist_ok=True); (cache / 'thumbs').mkdir(exist_ok=True)
    if a.center:     # pilot area: every published building within --radius m of the centre, not only OSM-validated ones
        clon, clat = (float(v) for v in a.center.split(','))
        ts = [pq.read_table(f, columns=['lon_e6', 'lat_e6', 'id']) for f in sorted(glob.glob(str(BY_AREA / 'ha_noi_*.parquet')))]
        lon = np.concatenate([t['lon_e6'].to_numpy() / 1e6 for t in ts]); lat = np.concatenate([t['lat_e6'].to_numpy() / 1e6 for t in ts])
        ids = np.concatenate([t['id'].to_numpy() for t in ts]); files = None
        cx, cy = to_xy(lon, lat); x0, y0 = to_xy(clon, clat)
        k = np.hypot(cx - x0, cy - y0) <= a.radius
        lon, lat, ids = lon[k], lat[k], ids[k]
    else:
        lon, lat, ids, files = validation_targets()
    print('validation buildings', len(lon))
    z = 14
    tx = np.floor((lon + 180) / 360 * 2 ** z).astype(int)
    ty = np.floor((1 - np.log(np.tan(np.radians(lat)) + 1 / np.cos(np.radians(lat))) / math.pi) / 2 * 2 ** z).astype(int)
    from scipy.spatial import cKDTree
    bx, by = to_xy(lon, lat); btree = cKDTree(np.c_[bx, by])
    cand = []
    for x, y in sorted(set(zip(tx.tolist(), ty.tolist()))):
        p = cache / 'tiles' / f'{z}_{x}_{y}.mvt'
        if not p.exists():
            b = _get(f'https://tiles.mapillary.com/maps/vtp/mly1_public/2/{z}/{x}/{y}', token, binary=True)
            if b is None:
                continue
            p.write_bytes(b)
        L = mvt_points(p.read_bytes()).get('image')
        if not L:
            continue
        ext, pts = L
        for props, px, py in pts:
            if props.get('is_pano'):
                continue
            ln = (x + px / ext) / 2 ** z * 360 - 180
            n = math.pi - 2 * math.pi * (y + py / ext) / 2 ** z
            la = math.degrees(math.atan(math.sinh(n)))
            ca = props.get('captured_at') or 0
            hour = ((ca / 3.6e6) + 7) % 24
            if not (7 <= hour <= 17.5) or ca < 1.546e12:      # since 2019
                continue
            cand.append((ln, la, int(props['id']), str(props.get('sequence_id')), ca))
    print('candidates', len(cand))
    cx, cy = to_xy(np.array([c[0] for c in cand]), np.array([c[1] for c in cand]))
    dd, _ = btree.query(np.c_[cx, cy])
    order = np.argsort(dd)
    cells, seqs, chosen = set(), {}, []
    for k in order:
        if dd[k] > 30 or len(chosen) >= a.max_images:
            break
        cell = (int(cx[k] // 20), int(cy[k] // 20))
        s = cand[k][3]
        if cell in cells or seqs.get(s, 0) >= 3:
            continue
        cells.add(cell); seqs[s] = seqs.get(s, 0) + 1; chosen.append(cand[k][2])
    print('chosen', len(chosen))
    fields = 'id,captured_at,computed_geometry,computed_rotation,computed_compass_angle,camera_parameters,camera_type,width,height,thumb_1024_url,is_pano,sequence,creator'
    ok = 0
    for i in chosen:
        mp = cache / 'meta' / f'img_{i}.json'; dp = cache / 'meta' / f'det_{i}.json'; tp = cache / 'thumbs' / f'{i}.jpg'
        if not mp.exists():
            m = _get(f'https://graph.mapillary.com/{i}?fields={fields}', token)
            if m is None:
                continue
            url = m.pop('thumb_1024_url', None)
            mp.write_text(json.dumps(m), encoding='utf-8')
            if url and not tp.exists() and m.get('computed_rotation') and m.get('camera_type') == 'perspective':
                b = _get(url, None, binary=True)
                if b:
                    tp.write_bytes(b)
        m = json.loads(mp.read_text(encoding='utf-8'))
        if not (m.get('computed_rotation') and m.get('camera_type') == 'perspective' and tp.exists()):
            continue
        if not dp.exists():
            dt = _get(f'https://graph.mapillary.com/{i}/detections?fields=value,geometry', token)
            if dt is None:
                continue
            dp.write_text(json.dumps(dt), encoding='utf-8')
        ok += 1
    for p in list(cache.rglob('*.json')):
        assert token not in p.read_text(encoding='utf-8'), 'token leaked into cache'
    print('usable', ok)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=['prep', 'measure', 'report', 'overlay', 'national', 'fetch'])
    ap.add_argument('--mly', help='Mapillary folder (cache/, thumbs/, site_nguyentrai/)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--cos-min', type=float, default=0.26)
    ap.add_argument('--base-tol', type=float, default=-1)
    ap.add_argument('--d-max', type=float, default=40.0)
    ap.add_argument('--ids', nargs='*', default=[])
    ap.add_argument('--max-images', type=int, default=700)
    ap.add_argument('--cache', help='fetch: Mapillary cache folder (default data/buildings/_mapillary)')
    ap.add_argument('--center', help='fetch: lon,lat of a pilot area (all buildings within --radius count as targets)')
    ap.add_argument('--radius', type=float, default=200.0)
    a = ap.parse_args()
    if a.cache:
        global MLY_CACHE
        MLY_CACHE = Path(a.cache)
    {'prep': cmd_prep, 'measure': cmd_measure, 'report': cmd_report, 'overlay': cmd_overlay, 'national': cmd_national, 'fetch': cmd_fetch}[a.cmd](a)


if __name__ == '__main__':
    main()
