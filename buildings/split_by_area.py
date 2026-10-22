"""Split the compact building layer into one file per commune, one folder per province, so a client downloads
and opens only the communes it needs.

Boundaries (OSM, two-level system since the July 2025 reform), assembled once from the PBF into
data/buildings/vn_admin.parquet:
- province: admin_level=4 with ISO3166-2 VN-* (34 units);
- commune:  admin_level=6 named Xã / Phường / Đặc khu (about 3,320), placed in the province holding a point inside it.
Assignment: a building belongs to the commune that contains its centroid (lon_e6/lat_e6 of vn_buildings.parquet),
so every building is in exactly one file and keeps its id. A centroid outside every commune of its province goes
to the nearest commune there; one outside every province goes to the nearest province first. Counts are reported.

Output: data/buildings/by_area/<province>/<commune>.parquet, schema of vn_buildings_compact.parquet (zstd 19), and
by_area/index.json: per province and per commune the OSM name, relation id, file, building count, bytes and lon/lat
bbox, which is what a client needs to pick the files covering an area.

    .venv/Scripts/python.exe buildings/split_by_area.py [--out data/buildings]
        [--compact vn_buildings_compact.parquet] [--attrs vn_buildings.parquet] [--pbf osrm/data/vietnam-latest.osm.pbf]
"""
import argparse, json, os, re, sys, time, unicodedata
import numpy as np, osmium, pyarrow as pa, pyarrow.parquet as pq, shapely
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compact_layer import SCHEMA

COMMUNE_PREFIX = ('Xã ', 'Phường ', 'Đặc khu ')


def slug(name, drop_prefix=False):
    s = re.sub(r'^(Thành phố|Tỉnh)\s+', '', name) if drop_prefix else name
    s = s.replace('đ', 'd').replace('Đ', 'D')
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')


class Admin(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.rows = []

    def area(self, a):
        t = a.tags
        if a.from_way() or t.get('boundary') != 'administrative':
            return
        lvl, name = t.get('admin_level'), t.get('name') or ''
        if lvl == '4' and t.get('ISO3166-2', '').startswith('VN-'):
            kind = 'province'
        elif lvl == '6' and name.startswith(COMMUNE_PREFIX):
            kind = 'commune'
        else:
            return
        try:
            g = self.wkb.create_multipolygon(a)
        except (osmium.InvalidLocationError, RuntimeError):
            return
        self.rows.append({'kind': kind, 'rel_id': a.orig_id(), 'name': name, 'iso': t.get('ISO3166-2'), 'wkb': bytes.fromhex(g)})


def load_admin(path, pbf):
    if not os.path.exists(path):
        t = time.time()
        h = Admin()
        h.apply_file(pbf, locations=True, idx='flex_mem')
        pq.write_table(pa.Table.from_pylist(h.rows), path, compression='zstd')
        print('boundaries assembled from', pbf, len(h.rows), f'{time.time() - t:.0f}s', flush=True)
    rows = pq.read_table(path).to_pylist()
    for r in rows:
        r['geom'] = shapely.make_valid(shapely.from_wkb(r.pop('wkb')))
    prov = sorted((r for r in rows if r['kind'] == 'province'), key=lambda r: r['name'])
    for r in prov:
        r['slug'] = slug(r['name'], drop_prefix=True)
    assert len({r['slug'] for r in prov}) == len(prov), 'province slugs collide'
    # A commune joins the province that holds a point inside it; communes outside Vietnam's provinces are dropped.
    tree = STRtree([r['geom'] for r in prov])
    comm = []
    for r in (r for r in rows if r['kind'] == 'commune'):
        pt = r['geom'].representative_point()
        hit = [i for i in tree.query(pt, predicate='intersects')]
        if hit:
            r['prov'] = int(hit[0]); comm.append(r)
    comm.sort(key=lambda r: (r['prov'], r['name']))
    for i, r in enumerate(prov):
        seen = set()
        for c in (c for c in comm if c['prov'] == i):
            s = slug(c['name'])
            c['slug'] = s if s not in seen else f"{s}_{c['rel_id']}"
            seen.add(c['slug'])
    return prov, comm


def contain(geoms, x, y, k, idx):
    """Set k[j] = idx[g] for points j inside geometry g (first match wins); only unassigned points are tested."""
    for gi, g in zip(idx, geoms):
        shapely.prepare(g)
        x0, y0, x1, y1 = g.bounds
        c = np.flatnonzero((k < 0) & (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1))
        if len(c):
            k[c[shapely.contains_xy(g, x[c], y[c])]] = gi


def nearest(geoms, x, y):
    return STRtree(geoms).query_nearest(shapely.points(x, y), all_matches=False)[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--compact', default='vn_buildings_compact.parquet')
    ap.add_argument('--attrs', default='vn_buildings.parquet', help='source of the centroids (heights_25d.py output)')
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--dst-dir', default='by_area')
    a = ap.parse_args()
    t0 = time.time()
    p = lambda n: os.path.join(a.out, n)
    prov, comm = load_admin(a.admin, a.pbf)
    print('provinces', len(prov), '| communes', len(comm), flush=True)

    c = pq.read_table(p(a.attrs), columns=['id', 'lon_e6', 'lat_e6'])
    ids, x, y = c['id'].to_numpy(), c['lon_e6'].to_numpy() / 1e6, c['lat_e6'].to_numpy() / 1e6
    kp = np.full(len(ids), -1, np.int32)
    contain([r['geom'] for r in prov], x, y, kp, range(len(prov)))
    out_p = np.flatnonzero(kp < 0)
    if len(out_p):
        kp[out_p] = nearest([r['geom'] for r in prov], x[out_p], y[out_p])
    kc = np.full(len(ids), -1, np.int32)
    out_c = 0
    for i in range(len(prov)):
        mine = [j for j, r in enumerate(comm) if r['prov'] == i]
        sel = np.flatnonzero(kp == i)
        if not len(sel) or not mine:
            continue
        sub = np.full(len(sel), -1, np.int32)
        contain([comm[j]['geom'] for j in mine], x[sel], y[sel], sub, mine)
        miss = np.flatnonzero(sub < 0)
        if len(miss):
            sub[miss] = np.array(mine)[nearest([comm[j]['geom'] for j in mine], x[sel][miss], y[sel][miss])]
            out_c += len(miss)
        kc[sel] = sub
    assert (kc >= 0).all(), 'building left without a commune (a province with no commune boundary?)'
    code = np.full(int(ids.max()) + 1, -1, np.int32); code[ids] = kc
    print(f'assigned {len(ids)} buildings | outside every province: {len(out_p)} | outside every commune of their province: {out_c} '
          f'| {time.time() - t0:.0f}s', flush=True)

    # The compact layer fits in memory (about 30 B/row uncompressed): sort once by commune, write each file once.
    t = pq.read_table(p(a.compact))
    k = code[t['id'].to_numpy()]
    order = np.argsort(k, kind='stable'); k = k[order]
    t = t.take(pa.array(order))
    lon, lat = t['lon_e6'].to_numpy(), t['lat_e6'].to_numpy()
    starts = np.flatnonzero(np.r_[True, np.diff(k) != 0]); ends = np.r_[starts[1:], len(k)]
    dst = p(a.dst_dir)
    stat = {}
    for s, e in zip(starts, ends):
        r = comm[k[s]]; pr = prov[r['prov']]
        os.makedirs(os.path.join(dst, pr['slug']), exist_ok=True)
        f = os.path.join(dst, pr['slug'], r['slug'] + '.parquet')
        pq.write_table(t.slice(s, e - s).cast(SCHEMA), f, compression='zstd', compression_level=19, row_group_size=500_000)
        stat[k[s]] = (int(e - s), os.path.getsize(f), [float(lon[s:e].min() / 1e6), float(lat[s:e].min() / 1e6),
                                                       float(lon[s:e].max() / 1e6), float(lat[s:e].max() / 1e6)])
    index = []
    for i, pr in enumerate(prov):
        cs = [(j, r) for j, r in enumerate(comm) if r['prov'] == i]
        units = [{'file': f"{pr['slug']}/{r['slug']}.parquet", 'name': r['name'], 'osm_relation': r['rel_id'],
                  'buildings': stat[j][0], 'bytes': stat[j][1], 'bbox': stat[j][2]} if j in stat else
                 {'file': None, 'name': r['name'], 'osm_relation': r['rel_id'], 'buildings': 0, 'bytes': 0, 'bbox': None}
                 for j, r in cs]
        boxes = [u['bbox'] for u in units if u['bbox']]
        index.append({'dir': pr['slug'], 'name': pr['name'], 'iso': pr['iso'], 'osm_relation': pr['rel_id'],
                      'buildings': sum(u['buildings'] for u in units), 'bytes': sum(u['bytes'] for u in units),
                      'bbox': [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None,
                      'communes': units})
    json.dump({'rule': 'centroid in OSM commune boundary (admin_level=6 Xã/Phường/Đặc khu) inside an admin_level=4 VN-* province; '
                       'nearest commune / province when outside',
               'outside_province': int(len(out_p)), 'outside_commune': int(out_c), 'provinces': index},
              open(os.path.join(dst, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    sizes = np.array([v[1] for v in stat.values()]) / 1e6
    for e in sorted(index, key=lambda e: -e['buildings'])[:8]:
        if e['buildings']:
            print(f"{e['dir']:22s} {e['buildings']:10d} buildings {e['bytes'] / 1e6:8.1f} MB in {sum(1 for u in e['communes'] if u['file'])} files")
    print(f'{sizes.sum():.0f} MB in {len(stat)} files | per file median {np.median(sizes):.2f} MB, max {sizes.max():.1f} MB | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
