"""Group neighbouring communes into download files, never across a province:
up to 3 adjacent urban wards (Phường) per file, up to 10 adjacent rural communes (Xã, Đặc khu) per file.

Rule, per province:
- Two communes are neighbours when their OSM boundaries touch (within about 10 m).
- Wards are grouped only with wards, communes only with communes, so city files stay small.
- A group starts from the unassigned unit with the fewest unassigned neighbours of its kind (an edge or corner of
  what is left, so groups do not strand single units), then repeatedly takes the unassigned neighbour of the same
  kind nearest to the group's centre, until it holds SIZE[kind] units or its estimated size would pass MAX_MB.
- A group of fewer than MIN_SIZE[kind] units is merged into a touching (or, for islands, the nearest) group of the
  same kind and province when the result stays within MERGE_MAX[kind] units and MAX_MB.
- Estimated size = buildings x BYTES_PER_BUILDING (compact layer, measured 18.9 B on Bùi Xương Trạch).
Groups are numbered north to south within a province: <province>_<NN>.parquet.

The grouping depends only on boundaries and on rough building counts, so it stays the same when footprints are
refined later. split_by_area.py writes the data files from it.

Output (committed, it is the table of contents of the published data):
  buildings/commune_groups.json  machine-readable: every group, its communes (name, OSM relation id), bbox, estimate
  buildings/commune_groups.md    the same as a readable table per province

    .venv/Scripts/python.exe buildings/group_communes.py [--footprints data/buildings/vn_buildings_clean.parquet]
"""
import argparse, json, os, sys, time
import duckdb, numpy as np, shapely
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from split_by_area import assign_communes, load_admin

SIZE = {'ward': 3, 'commune': 10}               # units per file
MIN_SIZE = {'ward': 2, 'commune': 4}
MERGE_MAX = {'ward': 3, 'commune': 13}
MAX_MB = 15.0
BYTES_PER_BUILDING = 19
TOUCH_DEG = 1e-4                                  # about 10 m: boundaries drawn with tiny gaps still touch
ISLAND_KM = 60                                    # an isolated commune joins the nearest group within this distance


def centroids(path):
    con = duckdb.connect(); con.execute('LOAD spatial; SET enable_progress_bar = false;')
    q = con.execute(f"SELECT ST_X(c) x, ST_Y(c) y FROM (SELECT ST_Centroid(geometry) c FROM '{path}')").fetchnumpy()
    return q['x'], q['y']


def group_province(units, geoms):
    """units: list of dicts with 'n' (buildings) and 'c' (x, y centre). Returns a list of lists of unit indices."""
    n = len(units)
    tree = STRtree(geoms)
    nb = [set(int(j) for j in tree.query(shapely.buffer(g, TOUCH_DEG), predicate='intersects') if j != i) for i, g in enumerate(geoms)]
    est = np.array([u['n'] * BYTES_PER_BUILDING / 1e6 for u in units])
    cen = np.array([u['c'] for u in units])
    kind = [u['kind'] for u in units]
    nb = [{j for j in s if kind[j] == kind[i]} for i, s in enumerate(nb)]      # group only with the same kind
    free = set(range(n)); groups = []
    while free:
        seed = min(free, key=lambda i: (len(nb[i] & free), -cen[i][1], cen[i][0]))
        g = [seed]; free.discard(seed); mb = est[seed]
        while len(g) < SIZE[kind[seed]]:
            cand = set().union(*(nb[i] for i in g)) & free
            if not cand:
                break
            c = cen[g].mean(axis=0)
            j = min(cand, key=lambda k: float(((cen[k] - c) ** 2).sum()))
            if mb + est[j] > MAX_MB:
                break
            g.append(j); free.discard(j); mb += est[j]
        groups.append(g)
    # Merge groups that came out small into a touching (or nearby, for islands) group.
    changed = True
    while changed:
        changed = False
        for gi in sorted(range(len(groups)), key=lambda i: len(groups[i])):
            g = groups[gi]
            if not g or len(g) >= MIN_SIZE[kind[g[0]]]:
                continue
            kd = kind[g[0]]
            touch = {k for k, h in enumerate(groups) if k != gi and h and (set().union(*(nb[i] for i in g)) & set(h))}
            if not touch:
                c = cen[g].mean(axis=0)
                dist = {k: float(np.sqrt(((cen[h].mean(axis=0) - c) ** 2).sum())) * 111 for k, h in enumerate(groups)
                        if k != gi and h and kind[h[0]] == kd}
                touch = {k for k, d in dist.items() if d <= ISLAND_KM}
            ok = [k for k in touch if len(groups[k]) + len(g) <= MERGE_MAX[kd] and est[groups[k]].sum() + est[g].sum() <= MAX_MB]
            if ok:
                k = min(ok, key=lambda k: len(groups[k]))
                groups[k] += g; groups[gi] = []; changed = True
    return [g for g in groups if g]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--footprints', default='data/buildings/vn_buildings_clean.parquet', help='buildings, for the size estimate')
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--dst', default='buildings/commune_groups')
    a = ap.parse_args()
    t0 = time.time()
    prov, comm = load_admin(a.admin, a.pbf)
    x, y = centroids(a.footprints)
    print('buildings', len(x), f'| {time.time() - t0:.0f}s', flush=True)
    kc, n_out_p, n_out_c = assign_communes(prov, comm, x, y)
    count = np.bincount(kc, minlength=len(comm))
    print(f'assigned | outside provinces {n_out_p} | outside communes {n_out_c} | {time.time() - t0:.0f}s', flush=True)

    out = []
    for i, pr in enumerate(prov):
        mine = [j for j, r in enumerate(comm) if r['prov'] == i]
        geoms = [comm[j]['geom'] for j in mine]
        units = [{'n': int(count[j]), 'kind': 'ward' if comm[j]['name'].startswith('Phường ') else 'commune',
                  'c': np.asarray(shapely.get_coordinates(comm[j]['geom'].representative_point())[0])} for j in mine]
        gs = group_province(units, geoms)
        gs.sort(key=lambda g: (-np.mean([units[k]['c'][1] for k in g]), np.mean([units[k]['c'][0] for k in g])))
        for no, g in enumerate(gs, 1):
            cs = sorted((mine[k] for k in g), key=lambda j: comm[j]['name'])
            b = shapely.bounds(shapely.union_all([comm[j]['geom'] for j in cs]))
            out.append({'file': f"{pr['slug']}_{no:02d}.parquet", 'province': pr['name'], 'province_iso': pr['iso'],
                        'province_relation': pr['rel_id'],
                        'communes': [{'name': comm[j]['name'], 'osm_relation': comm[j]['rel_id'], 'est_buildings': int(count[j])} for j in cs],
                        'bbox': [round(float(v), 5) for v in b],
                        'est_buildings': int(sum(count[j] for j in cs)),
                        'est_mb': round(float(sum(count[j] for j in cs)) * BYTES_PER_BUILDING / 1e6, 2)})
    sizes = np.array([len(g['communes']) for g in out]); mbs = np.array([g['est_mb'] for g in out])
    meta = {'rule': f"adjacent units of one province and one kind: up to {SIZE['ward']} wards (Phường) or {SIZE['commune']} "
                    f"communes (Xã, Đặc khu) per file, at most about {MAX_MB:.0f} MB; small groups merged into a neighbour "
                    'of the same kind; numbered north to south',
            'boundaries': 'OSM admin_level=4 (ISO3166-2 VN-*) and admin_level=6 (Xã/Phường/Đặc khu), ' + os.path.basename(a.pbf),
            'provinces': len(prov), 'communes': len(comm), 'files': len(out), 'groups': out}
    json.dump(meta, open(a.dst + '.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    lines = ['# Building data: table of contents', '',
             f"{len(out)} files for {len(comm)} communes in {len(prov)} provinces. Each file holds adjacent units of one province: "
             f"up to {SIZE['ward']} wards (Phường) or up to {SIZE['commune']} communes (Xã, Đặc khu), at most about {MAX_MB:.0f} MB. "
             'Building counts are estimates from the footprints '
             'before final refinement; `index.json` in the published data gives the exact counts.', '',
             'Generated by `buildings/group_communes.py`; machine-readable version: `commune_groups.json`.', '']
    for pr in prov:
        gs = [g for g in out if g['province'] == pr['name']]
        lines += [f"## {pr['name']} ({len(gs)} files, {sum(len(g['communes']) for g in gs)} communes, "
                  f"~{sum(g['est_buildings'] for g in gs):,} buildings)", '', '| File | Communes | Buildings (est.) | MB (est.) |', '|---|---|---|---|']
        for g in gs:
            lines.append(f"| `{g['file']}` | {', '.join(c['name'] for c in g['communes'])} | {g['est_buildings']:,} | {g['est_mb']:.1f} |")
        lines.append('')
    open(a.dst + '.md', 'w', encoding='utf-8').write('\n'.join(lines))
    print(f'{len(out)} files | communes per file: median {np.median(sizes):.0f}, min {sizes.min()}, max {sizes.max()} | '
          f'est MB per file: median {np.median(mbs):.1f}, max {mbs.max():.1f}, total {mbs.sum():.0f} | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
