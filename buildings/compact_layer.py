"""Write the compact building layer: one row per building with a small footprint encoding and its height.

Footprint encoding (all coordinates in 1e-6 degrees, about 0.1 m):
- rect: the footprint covers >= 95 % of its minimum rotated rectangle, has no holes, and the rectangle stays clear
  of every carriageway and every other building: its largest bulge past the footprint (Hausdorff distance) plus
  RECT_SAFE_M is less than the footprint's road_gap_m and nb_gap_m from fit_footprints.py. A footprint that touches
  a road or a neighbour (most tube houses) is therefore always a ring. Stored as centre
  (lon_e6, lat_e6), length_dm, width_dm (metres x 10) and angle_cdeg (direction of the long side,
  clockwise from north, hundredths of a degree, 0-17999). No ring.
- ring: everything else. lon_e6/lat_e6 is the first vertex and `ring` holds int16 little-endian
  (dx, dy) steps to each next vertex of the outer ring (closing vertex dropped). Holes are dropped.
decode() in this file turns a row back into a shapely polygon.

Inputs: vn_buildings_clean.parquet (fitted geometry and gaps) and vn_buildings.parquet (heights), joined on id.
Output: data/buildings/vn_buildings_compact.parquet, zstd level 19.

    .venv/Scripts/python.exe buildings/compact_layer.py [--out data/buildings] [--batch 1000000]
"""
import argparse, math, os, time
import duckdb, numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely

RECT_FILL = 0.95
RECT_SAFE_M = 0.15                 # covers centre/size/angle rounding of the rectangle
SCHEMA = pa.schema([
    ('id', pa.uint32()), ('src', pa.dictionary(pa.int8(), pa.string())),
    ('lon_e6', pa.int32()), ('lat_e6', pa.int32()),
    ('length_dm', pa.uint16()), ('width_dm', pa.uint16()), ('angle_cdeg', pa.uint16()), ('ring', pa.binary()),
    ('height_dm', pa.uint16()), ('height_src', pa.dictionary(pa.int8(), pa.string())), ('floors', pa.uint8()),
])


def metres_per_deg(lat):
    return 111320 * math.cos(math.radians(lat)), 110574


def encode(geoms, lat_ref, gap=None):
    """Encode a batch of polygons; `gap` is metres to the nearest carriageway (NaN = far). Returns dict of numpy/obj arrays."""
    n = len(geoms)
    ext = shapely.get_exterior_ring(geoms)
    rect = shapely.oriented_envelope(geoms)
    fill = shapely.area(geoms) / np.maximum(shapely.area(rect), 1e-18)
    nholes = shapely.get_num_interior_rings(geoms)
    is_rect = (fill >= RECT_FILL) & (nholes == 0) & (shapely.get_type_id(rect) == 3)
    kx, ky = metres_per_deg(lat_ref)
    if gap is not None:
        bulge = shapely.hausdorff_distance(geoms, rect) * max(kx, ky)
        is_rect &= np.isnan(gap) | (bulge + RECT_SAFE_M < gap)
    out = {k: np.zeros(n, dt) for k, dt in (('lon_e6', np.int32), ('lat_e6', np.int32), ('length_dm', np.uint16),
                                             ('width_dm', np.uint16), ('angle_cdeg', np.uint16))}
    ring = np.full(n, None, object)
    for i in range(n):
        if is_rect[i]:
            c = np.asarray(rect[i].exterior.coords)[:4]
            e1, e2 = c[1] - c[0], c[2] - c[1]
            l1, l2 = math.hypot(e1[0] * kx, e1[1] * ky), math.hypot(e2[0] * kx, e2[1] * ky)
            long_e = e1 if l1 >= l2 else e2
            ang = math.degrees(math.atan2(long_e[0] * kx, long_e[1] * ky)) % 180
            cx, cy = c.mean(axis=0)
            out['lon_e6'][i], out['lat_e6'][i] = round(cx * 1e6), round(cy * 1e6)
            out['length_dm'][i], out['width_dm'][i] = min(round(max(l1, l2) * 10), 65535), min(round(min(l1, l2) * 10), 65535)
            out['angle_cdeg'][i] = round(ang * 100) % 18000
        else:
            c = np.round(np.asarray(ext[i].coords)[:-1] * 1e6).astype(np.int64)
            d = np.diff(c, axis=0)
            if len(d) and np.abs(d).max() > 32767:          # very large outline: fall back to its rectangle
                is_rect[i] = True; ring[i] = None
                c4 = np.asarray(rect[i].exterior.coords)[:4]; cx, cy = c4.mean(axis=0)
                e1, e2 = c4[1] - c4[0], c4[2] - c4[1]
                l1, l2 = math.hypot(e1[0] * kx, e1[1] * ky), math.hypot(e2[0] * kx, e2[1] * ky)
                long_e = e1 if l1 >= l2 else e2
                out['lon_e6'][i], out['lat_e6'][i] = round(cx * 1e6), round(cy * 1e6)
                out['length_dm'][i], out['width_dm'][i] = min(round(max(l1, l2) * 10), 65535), min(round(min(l1, l2) * 10), 65535)
                out['angle_cdeg'][i] = round(math.degrees(math.atan2(long_e[0] * kx, long_e[1] * ky)) % 180 * 100) % 18000
                continue
            out['lon_e6'][i], out['lat_e6'][i] = c[0]
            ring[i] = d.astype('<i2').tobytes()
    out['ring'] = ring
    return out, is_rect


def decode(row):
    """Row (dict) -> shapely Polygon in lon/lat."""
    x0, y0 = row['lon_e6'] / 1e6, row['lat_e6'] / 1e6
    if row['ring'] is None:
        kx, ky = metres_per_deg(y0)
        L, Wd, a = row['length_dm'] / 20, row['width_dm'] / 20, math.radians(row['angle_cdeg'] / 100)
        ux, uy = math.sin(a), math.cos(a)                 # long side direction (east, north)
        vx, vy = -uy, ux
        pts = [(x0 + (sx * L * ux + sy * Wd * vx) / kx, y0 + (sx * L * uy + sy * Wd * vy) / ky) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        return shapely.Polygon(pts)
    d = np.frombuffer(row['ring'], '<i2').reshape(-1, 2).astype(np.int64)
    c = np.vstack([[0, 0], np.cumsum(d, axis=0)]) + [row['lon_e6'], row['lat_e6']]
    return shapely.Polygon(c / 1e6)


def encode_rows(ids, src, wkb, hdm, hsrc, fl, gap):
    """Rows (id, src, footprint WKB, height_dm, height_src, floors, gap in m or None) -> (table in SCHEMA, is_rect,
    footprints). The latitude for metres per degree is the median of the batch."""
    geoms = shapely.from_wkb([bytes(w) for w in wkb])
    geoms = np.array([g if g.geom_type == 'Polygon' else max(g.geoms, key=lambda x: x.area) for g in geoms], object)
    lat_ref = float(np.median(shapely.get_y(shapely.centroid(geoms[:: max(1, len(geoms) // 1000)]))))
    enc, is_rect = encode(geoms, lat_ref, np.array([np.nan if g is None else g for g in gap], float))
    tbl = pa.table({'id': pa.array(ids, pa.uint32()), 'src': pa.array(src).dictionary_encode(),
                    **{k: pa.array(enc[k]) for k in ('lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg')},
                    'ring': pa.array(list(enc['ring']), pa.binary()),
                    'height_dm': pa.array(hdm, pa.uint16()), 'height_src': pa.array(hsrc, pa.string()).dictionary_encode(),
                    'floors': pa.array(fl, pa.uint8())}).cast(SCHEMA)
    return tbl, is_rect, geoms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--batch', type=int, default=1_000_000)
    ap.add_argument('--src', default='vn_buildings_clean.parquet')
    ap.add_argument('--attrs', default='vn_buildings.parquet')
    ap.add_argument('--dst', default='vn_buildings_compact.parquet')
    a = ap.parse_args()
    t0 = time.time()
    p = lambda n: os.path.join(a.out, n).replace('\\', '/')
    con = duckdb.connect(); con.execute("LOAD spatial; SET enable_progress_bar = false;")
    cols = {c[0] for c in con.execute(f"DESCRIBE SELECT * FROM '{p(a.src)}'").fetchall()}
    # Distance to the nearest thing a rectangle must not reach; files from clip_to_roads.py have no nb_gap_m.
    gap = ("CASE WHEN m.road_gap_m IS NULL AND m.nb_gap_m IS NULL THEN NULL "
           "ELSE least(coalesce(m.road_gap_m, 1e9), coalesce(m.nb_gap_m, 1e9)) END") if 'nb_gap_m' in cols else 'm.road_gap_m'
    q = con.execute(f"""SELECT m.id, m.src, ST_AsWKB(m.geometry) wkb, b.height_dm, b.height_src, b.floors, {gap} gap
                        FROM '{p(a.src)}' m JOIN '{p(a.attrs)}' b USING (id) ORDER BY m.id""")
    writer = pq.ParquetWriter(p(a.dst), SCHEMA, compression='zstd', compression_level=19)
    n = n_rect = 0
    check = []
    while True:
        rb = q.fetchmany(a.batch)
        if not rb:
            break
        ids, src, wkb, hdm, hsrc, fl, gap = zip(*rb)
        tbl, is_rect, geoms = encode_rows(ids, src, wkb, hdm, hsrc, fl, gap)
        writer.write_table(tbl, row_group_size=500_000)
        # Round-trip check on a sample: decoded footprint vs original (IoU).
        for i in range(0, len(geoms), max(1, len(geoms) // 200)):
            row = {k: tbl.column(k)[i].as_py() for k in ('lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring')}
            g0, g1 = geoms[i], decode(row)
            check.append((bool(is_rect[i]), shapely.area(shapely.intersection(g0, g1)) / max(shapely.area(shapely.union(g0, g1)), 1e-18)))
        n += len(ids); n_rect += int(is_rect.sum())
        print(f'{n} rows, {n_rect / n:.1%} rect | {time.time() - t0:.0f}s', flush=True)
    writer.close()
    ck = np.array(check)
    for flag, name in ((1, 'rect'), (0, 'ring')):
        v = ck[ck[:, 0] == flag, 1]
        if len(v):
            print(f'round-trip IoU {name}: n={len(v)} median {np.median(v):.3f} p5 {np.percentile(v, 5):.3f}')
    size = os.path.getsize(p(a.dst))
    print(f'wrote {p(a.dst)}: {size / 1e6:.0f} MB, {n} buildings, {size / max(n, 1):.1f} bytes/building | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
