"""Split the compact building layer into files of neighbouring communes (the table of contents written by
group_communes.py: up to 3 wards or 10 communes per file), so a client downloads and opens only the area it needs.

Boundaries (OSM, two-level system since the July 2025 reform), assembled once from the PBF into
data/buildings/vn_admin.parquet:
- province: admin_level=4 with ISO3166-2 VN-* (34 units);
- commune:  admin_level=6 named Xã / Phường / Đặc khu (about 3,320), placed in the province holding a point inside it.
Assignment: a building belongs to the commune that contains its centroid (lon_e6/lat_e6 of vn_buildings.parquet),
so every building is in exactly one file and keeps its id. A centroid outside every commune of its province goes
to the nearest commune there; one outside every province goes to the nearest province first. Counts are reported.

Output: data/buildings/by_area/<province>_<NN>.parquet, schema of vn_buildings_compact.parquet (zstd 19), and
by_area/index.json: per file its province, communes (name, OSM relation id, building count), building count, bytes and
lon/lat bbox, which is what a client needs to pick the files covering an area. Flat names, so the files can be
published as release assets.

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


def admin_kind(t):
    lvl, name = t.get('admin_level'), t.get('name') or ''
    if t.get('boundary') != 'administrative':
        return None
    if lvl == '4' and t.get('ISO3166-2', '').startswith('VN-'):
        return 'province'
    if lvl == '6' and name.startswith(COMMUNE_PREFIX):
        return 'commune'
    return None


class Admin(osmium.SimpleHandler):
    """Assembles the wanted boundaries; also keeps their member ways for the fallback below."""
    def __init__(self, members):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.members, self.lines, self.rows = members, {}, []

    def way(self, w):
        if w.id in self.members:
            try:
                self.lines[w.id] = shapely.from_wkb(bytes.fromhex(self.wkb.create_linestring(w)))
            except (osmium.InvalidLocationError, RuntimeError):
                pass

    def area(self, a):
        t = a.tags
        kind = None if a.from_way() else admin_kind(t)
        if not kind:
            return
        try:
            g = self.wkb.create_multipolygon(a)
        except (osmium.InvalidLocationError, RuntimeError):
            return
        self.rows.append({'kind': kind, 'rel_id': a.orig_id(), 'name': t.get('name'), 'iso': t.get('ISO3166-2'), 'wkb': bytes.fromhex(g)})


def load_admin(path, pbf):
    if not os.path.exists(path):
        t = time.time()
        want = {}
        for r in osmium.FileProcessor(pbf, osmium.osm.RELATION):
            k = admin_kind(r.tags)
            if k:
                want[r.id] = {'kind': k, 'name': r.tags.get('name'), 'iso': r.tags.get('ISO3166-2'),
                              'ways': [m.ref for m in r.members if m.type == 'w' and m.role in ('outer', '')]}
        h = Admin({w for v in want.values() for w in v['ways']})
        h.apply_file(pbf, locations=True, idx='flex_mem')
        # The extract cuts off some offshore boundary ways (Trường Sa), so osmium cannot close the ring and drops the
        # whole unit (Khánh Hòa with it). Fallback: polygonize the member ways that are present; keep the closed faces.
        done = {r['rel_id'] for r in h.rows}
        for rid, v in want.items():
            if rid in done:
                continue
            lines = [h.lines[w] for w in v['ways'] if w in h.lines]
            faces = list(shapely.get_parts(shapely.polygonize(shapely.get_parts(shapely.union_all(lines))))) if lines else []
            if faces:
                g = shapely.union_all(faces)
                h.rows.append({'kind': v['kind'], 'rel_id': rid, 'name': v['name'], 'iso': v['iso'], 'wkb': shapely.to_wkb(g)})
                print(f"fallback: {v['name']} rebuilt from {len(lines)}/{len(v['ways'])} member ways, {len(faces)} closed faces", flush=True)
            else:
                print(f"skipped: {v['name']} has no closed outline in this extract", flush=True)
        pq.write_table(pa.Table.from_pylist(h.rows), path, compression='zstd')
        print('boundaries assembled from', pbf, len(h.rows), 'of', len(want), f'{time.time() - t:.0f}s', flush=True)
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


def assign_communes(prov, comm, x, y):
    """Commune index per point: centroid containment, first by province, then by commune inside it.
    Returns (commune index, points outside every province, points outside every commune of their province)."""
    kp = np.full(len(x), -1, np.int32)
    contain([r['geom'] for r in prov], x, y, kp, range(len(prov)))
    out_p = np.flatnonzero(kp < 0)
    if len(out_p):
        kp[out_p] = nearest([r['geom'] for r in prov], x[out_p], y[out_p])
    kc = np.full(len(x), -1, np.int32)
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
    return kc, len(out_p), out_c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--compact', default='vn_buildings_compact.parquet')
    ap.add_argument('--attrs', default='vn_buildings.parquet', help='source of the centroids (heights_25d.py output)')
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--dst-dir', default='by_area')
    ap.add_argument('--groups', default='buildings/commune_groups.json', help='table of contents from group_communes.py')
    a = ap.parse_args()
    t0 = time.time()
    p = lambda n: os.path.join(a.out, n)
    prov, comm = load_admin(a.admin, a.pbf)
    print('provinces', len(prov), '| communes', len(comm), flush=True)

    c = pq.read_table(p(a.attrs), columns=['id', 'lon_e6', 'lat_e6'])
    ids, x, y = c['id'].to_numpy(), c['lon_e6'].to_numpy() / 1e6, c['lat_e6'].to_numpy() / 1e6
    kc, n_out_p, out_c = assign_communes(prov, comm, x, y)
    assert (kc >= 0).all(), 'building left without a commune (a province with no commune boundary?)'
    code = np.full(int(ids.max()) + 1, -1, np.int32); code[ids] = kc
    print(f'assigned {len(ids)} buildings | outside every province: {n_out_p} | outside every commune of their province: {out_c} '
          f'| {time.time() - t0:.0f}s', flush=True)

    # One file per group of neighbouring communes, as listed in the table of contents (group_communes.py).
    groups = json.load(open(a.groups, encoding='utf-8'))['groups']
    gof = {c['osm_relation']: gi for gi, g in enumerate(groups) for c in g['communes']}
    missing = [r['name'] for r in comm if r['rel_id'] not in gof]
    assert not missing, f'communes not in {a.groups} (boundaries changed? re-run group_communes.py): {missing[:5]}'
    cg = np.array([gof[r['rel_id']] for r in comm], np.int32)

    # The compact layer fits in memory (about 30 B/row uncompressed): sort once by group, write each file once.
    t = pq.read_table(p(a.compact))
    kcm = code[t['id'].to_numpy()]
    k = cg[kcm]
    order = np.argsort(k, kind='stable'); k, kcm = k[order], kcm[order]
    t = t.take(pa.array(order))
    lon, lat = t['lon_e6'].to_numpy(), t['lat_e6'].to_numpy()
    starts = np.flatnonzero(np.r_[True, np.diff(k) != 0]); ends = np.r_[starts[1:], len(k)]
    dst = p(a.dst_dir); os.makedirs(dst, exist_ok=True)
    per_commune = np.bincount(kcm, minlength=len(comm))
    rel = {r['rel_id']: j for j, r in enumerate(comm)}
    index = []
    for s, e in zip(starts, ends):
        g = groups[k[s]]
        f = os.path.join(dst, g['file'])
        pq.write_table(t.slice(s, e - s).cast(SCHEMA), f, compression='zstd', compression_level=19, row_group_size=500_000)
        index.append({'file': g['file'], 'province': g['province'], 'province_iso': g['province_iso'],
                      'buildings': int(e - s), 'bytes': os.path.getsize(f),
                      'bbox': [float(lon[s:e].min() / 1e6), float(lat[s:e].min() / 1e6), float(lon[s:e].max() / 1e6), float(lat[s:e].max() / 1e6)],
                      'communes': [{'name': c['name'], 'osm_relation': c['osm_relation'], 'buildings': int(per_commune[rel[c['osm_relation']]])}
                                   for c in g['communes']]})
    empty = [g['file'] for gi, g in enumerate(groups) if gi not in set(k[starts].tolist())]
    json.dump({'rule': 'centroid in OSM commune boundary (admin_level=6 Xã/Phường/Đặc khu) inside an admin_level=4 VN-* province; '
                       'nearest commune / province when outside. Files group neighbouring communes as in commune_groups.json.',
               'outside_province': int(n_out_p), 'outside_commune': int(out_c), 'files_without_buildings': empty,
               'schema': 'see buildings/compact_layer.py (decode() turns a row into a polygon)', 'files': index},
              open(os.path.join(dst, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    sizes = np.array([e['bytes'] for e in index]) / 1e6
    print(f'{sizes.sum():.0f} MB in {len(index)} files ({len(empty)} groups without buildings) | per file median {np.median(sizes):.2f} MB, '
          f'max {sizes.max():.1f} MB | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
