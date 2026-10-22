"""Build the published building layer area by area: one task per commune group of commune_groups.json runs
fit_footprints.py pass A and pass B, the 2.5D heights, the compact encoding and the road / overlap check for that
group alone, writes buildings/by_area/<file>, and the parent process commits each finished file.

Per area (worker process):
  1. pass A (shift off roads) on every building whose centroid lies in the area's window (the bbox of its communes
     and of the buildings pre-assigned to it, plus NB_MARGIN_DEG), against the roads around it. Pass A depends only
     on a building and the roads near it, so recomputing it in the margin gives the same footprints.
  2. pass B (shared walls) for the window, the margin buildings as neighbours.
  3. keep the buildings whose final centroid falls in one of the area's communes (split_by_area.assign_communes, the
     rule split_by_area.py uses), read their 2.5D heights from the tiles covering them (HEIGHT_THREADS tiles at a
     time, values taken in manifest order: first valid value wins), heights and floors as heights_25d.py computes them;
  4. encode as compact_layer.py;
  5. check as check_road_overlap.py, on the compact rows (decoded) and the clean rows: no building on a carriageway
     and no overlapping pair over 0.05 m2 (an area GEOS still cannot compute after the retries of
     fit_footprints.overlay counts as failing). Buildings that fail are deleted from both (on a road: the building; an
     overlapping pair: the lower-ranked one, pass B order) and the check runs again, up to CHECK_ROUNDS times;
     the count is 'dropped_check'. Write buildings/by_area/<file>, under 95 MB. An area still failing is written
     to the state file and never committed; the next run builds it again.
Parent: prepares the work copies once (fit_footprints.prepare), runs the areas largest first on up to --workers
processes (default 60 % of the logical CPUs; two starts at least START_GAP_S apart, so the memory peaks of pass B
interleave; the next area starts only when free memory, less what the running areas are still expected to grow by,
holds its estimated peak, mem_estimate, plus MEM_HEADROOM; an area past pass B counts at AFTER_B_SHARE of its peak),
commits each passed file with git and pushes
every 10 commits or 2 minutes, and at the end writes and commits buildings/by_area/index.json. Re-running skips
areas that passed (state in <out>/_areas/done/) and builds failed ones again.

    .venv/Scripts/python.exe buildings/build_areas.py [--out data/buildings] [--workers N] [--no-push]
    .venv/Scripts/python.exe buildings/build_areas.py --area ha_noi_15.parquet --bbox 105.815 20.985 105.82 20.99 --no-push
        (test: one area, only its buildings inside the box, which is then also the whole input window)
"""
import argparse, ctypes, json, os, shutil, subprocess, sys, time
# Before numpy loads: each OpenBLAS copy (numpy's and scipy's) commits a buffer per core, 1.6 GB per process on 24
# cores, and nothing here uses BLAS. GDAL's block cache defaults to 5 % of RAM per process; the tiles are read once.
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('GDAL_CACHEMAX', '64')
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
import duckdb, numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely
from pyproj import Transformer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fit_footprints as fit
from check_road_overlap import carriageways, on_road_m2, pair_overlaps, utm
from compact_layer import SCHEMA, decode, encode_rows
from heights_25d import attrs_query, tile_heights, tiles_for
from split_by_area import assign_communes, load_admin

MAX_MB = 95                        # GitHub refuses files over 100 MB
OVER_M2 = 0.05                     # check threshold, as check_road_overlap.py reports
CHECK_ROUNDS = 3                   # rounds of deleting what fails the check before an area is marked failed
HEIGHT_THREADS = 2                 # tiles read at once per worker (network bound)
PUSH_EVERY, PUSH_SECONDS = 10, 120
START_GAP_S = 45                   # at least this long between two starts, so the pass B peaks of the areas interleave
AFTER_B_SHARE = 0.65               # an area past pass B holds about this share of its peak (heights 1.2, check 1.6 of 2.4 GB)
MEM_BASE, MEM_PER_BUILDING = 1.0e9, 7.0e3   # peak private bytes of a worker for an area of n buildings, with
                                   # margin: measured 1.3 GB at n = 110k, 2.4 GB at 308k (its window 506k)
MEM_HEADROOM = 2e9                 # kept free when starting another area
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_W = {}                            # per worker: admin units and the group of each commune


def limit_threads(n):
    """DuckDB and Arrow use every core in every process by default; cap them so the run stays within its CPU budget."""
    connect = duckdb.connect
    duckdb.connect = lambda *a, **k: connect(*a, **{**k, 'config': {**(k.get('config') or {}), 'threads': n}})
    pa.set_cpu_count(n)


def _init(admin, pbf, groups_json):
    limit_threads(1)                # one core per worker; the pool size is the budget
    prov, comm = load_admin(admin, pbf)
    gof = {c['osm_relation']: gi for gi, g in enumerate(json.load(open(groups_json, encoding='utf-8'))['groups']) for c in g['communes']}
    _W.update(prov=prov, comm=comm, cg=np.array([gof.get(r['rel_id'], -1) for r in comm], np.int32))


def area_group(x, y):
    """Group index (commune_groups.json) of lon/lat points, by the split_by_area.py rule."""
    kc, _, _ = assign_communes(_W['prov'], _W['comm'], x, y)
    return _W['cg'][kc]


def prepare(out, work, bbox, groups_json, admin, pbf):
    """Work copies (fit_footprints.prepare plus the merged attributes), and per area its window and building count."""
    os.makedirs(work, exist_ok=True)
    w = lambda n: os.path.join(work, n).replace('\\', '/')
    if not os.path.exists(w('areas.json')):
        con = duckdb.connect(); con.execute("LOAD spatial; SET enable_progress_bar = false;")
        con.execute(f"SET temp_directory = '{w('_tmp')}'")
        fit.prepare(con, out, work, bbox)
        merged = os.path.join(out, 'vn_buildings_merged.parquet').replace('\\', '/')
        con.execute(f"""COPY (SELECT m.* EXCLUDE (geometry, area_m2), b.cx, b.cy FROM '{merged}' m JOIN '{w('b.parquet')}' b USING (id)
                     ORDER BY floor(b.cy / {fit.CELL}), floor(b.cx / {fit.CELL}), m.id) TO '{w('attrs.parquet')}' (FORMAT parquet, ROW_GROUP_SIZE 100000)""")
        p = con.execute(f"SELECT cx, cy FROM '{w('b.parquet')}'").fetchnumpy()
        _init(admin, pbf, groups_json)
        k = area_group(p['cx'], p['cy'])
        groups = json.load(open(groups_json, encoding='utf-8'))['groups']
        rel = {r['rel_id']: r for r in _W['comm']}
        areas = []
        for gi, g in enumerate(groups):
            m = k == gi
            if not m.any():
                continue
            bb = [p['cx'][m].min(), p['cy'][m].min(), p['cx'][m].max(), p['cy'][m].max()]
            for c in g['communes']:
                if c['osm_relation'] in rel:
                    x0, y0, x1, y1 = rel[c['osm_relation']]['geom'].bounds
                    bb = [min(bb[0], x0), min(bb[1], y0), max(bb[2], x1), max(bb[3], y1)]
            if bbox:
                bb = [max(bb[0], bbox[0]), max(bb[1], bbox[1]), min(bb[2], bbox[2]), min(bb[3], bbox[3])]
            areas.append({'gi': gi, 'file': g['file'], 'n': int(m.sum()), 'box': [float(v) for v in bb]})
        tiles = []
        for url, z, x0, y0, x1, y1 in tiles_for(2023, os.path.join(out, '_25d_manifests')):
            lo, la = Transformer.from_crs(z, 4326, always_xy=True).transform([x0, x1, x0, x1], [y0, y0, y1, y1])
            tiles.append((url, z, x0, y0, x1, y1, min(lo), min(la), max(lo), max(la)))
        json.dump({'bbox': bbox, 'areas': sorted(areas, key=lambda a: -a['n']), 'tiles': tiles}, open(w('areas.json'), 'w'))
    return json.load(open(w('areas.json')))


def build_area(args):
    """One area end to end. Returns its index entry with 'status' ('ok' or 'failed: ...')."""
    work, dst_dir, area, tiles, test = args
    t0 = time.time()
    gi, file = area['gi'], area['file']
    g = json.load(open(os.path.join(REPO, 'buildings', 'commune_groups.json'), encoding='utf-8'))['groups'][gi]
    entry = {'file': file, 'province': g['province'], 'province_iso': g['province_iso']}
    core = np.array(area['box']) + np.array([-1, -1, 1, 1]) * (1e-7 if test else 1e-4)
    margin = 0.0 if test else fit.NB_MARGIN_DEG
    # Pass A on the window, own buildings (centroid in the core box) first, then pass B. The whole window, not only the
    # buildings near the area: pass B's unions on the 1e-6 degree grid depend on the order their inputs come out of
    # STRtrees built over the whole window, so a smaller window moves a few shared walls by a fraction of a m2.
    b = fit.read_box(work, core, margin)
    if not b.num_rows:
        return {**entry, 'buildings': 0, 'status': 'ok', 'seconds': round(time.time() - t0)}
    lines, bufs = fit.roads_near(work, core, margin + fit.ROAD_MARGIN_DEG)
    ta, sa = fit.shift_rows(b, lines, bufs)
    del b, lines
    cx, cy = ta['cx'].to_numpy(), ta['cy'].to_numpy()
    own = (cx >= core[0]) & (cx < core[2]) & (cy >= core[1]) & (cy < core[3])
    ta = pa.concat_tables([ta.filter(pa.array(own)), ta.filter(pa.array(~own))])
    res, sb = fit.overlap_rows(ta, int(own.sum()), bufs, core)
    del bufs
    ta = ta.select(['id', 'a1', 'rank_conf'])        # what the check needs; the pass A footprints go
    t_fit = time.time() - t0
    open(os.path.join(work, 'phase', file), 'w').close()   # past its memory peak (pass B), for the scheduler

    # The rows vn_buildings_clean.parquet would hold (fit_footprints.main), then the area's own buildings.
    con = duckdb.connect(); con.execute("LOAD spatial; SET enable_progress_bar = false;")
    con.register('f', res)
    con.execute(f"""CREATE TABLE clean AS SELECT m.* EXCLUDE (cx, cy), f.area_m2, ST_GeomFromWKB(f.wkb) geometry,
               f.shift_m::FLOAT shift_m, f.clip_m2::FLOAT clip_m2, f.fill_m2::FLOAT fill_m2, f.overlap_m2::FLOAT overlap_m2,
               CASE WHEN isnan(f.road_gap_m) THEN NULL ELSE f.road_gap_m END::FLOAT road_gap_m,
               CASE WHEN isnan(f.nb_gap_m) THEN NULL ELSE f.nb_gap_m END::FLOAT nb_gap_m
        FROM (SELECT * FROM '{work}/attrs.parquet' WHERE cx >= {core[0] - 1e-3} AND cx < {core[2] + 1e-3}
              AND cy >= {core[1] - 1e-3} AND cy < {core[3] + 1e-3}) m JOIN f USING (id) ORDER BY m.id""")
    con.unregister('f'); del res
    q = con.execute("""SELECT id, ST_X(c) cx, ST_Y(c) cy, CAST(round(ST_X(c) * 1e6) AS INTEGER) lon_e6,
                       CAST(round(ST_Y(c) * 1e6) AS INTEGER) lat_e6, ST_AsWKB(geometry) wkb
                       FROM (SELECT id, geometry, ST_Centroid(geometry) c FROM clean) ORDER BY id""").fetchnumpy()
    mine = area_group(q['lon_e6'] / 1e6, q['lat_e6'] / 1e6) == gi
    if test:
        bx = test['bbox']
        mine &= (q['cx'] >= bx[0]) & (q['cx'] <= bx[2]) & (q['cy'] >= bx[1]) & (q['cy'] <= bx[3])
    ids, hx, hy, wkb = q['id'][mine].astype(np.int64), q['cx'][mine], q['cy'][mine], q['wkb'][mine]
    q_id, q_lon, q_lat = q['id'][mine], q['lon_e6'][mine], q['lat_e6'][mine]
    del q
    if not len(ids):
        return {**entry, 'buildings': 0, 'status': 'ok', 'seconds': round(time.time() - t0)}

    # Heights: every tile covering buildings is read (HEIGHT_THREADS at a time, the reads wait on the network), then
    # the values are taken in manifest order; a building keeps the first valid value. A tile's value for a building
    # does not depend on which other buildings are read with it, so this equals reading tile after tile.
    h = np.full(len(ids), np.nan, np.float32)
    pos = {int(k): x for x, k in enumerate(ids)}
    tb = np.array([t[6:] for t in tiles], float)
    near = (tb[:, 0] <= hx.max()) & (tb[:, 2] >= hx.min()) & (tb[:, 1] <= hy.max()) & (tb[:, 3] >= hy.min())
    jobs = []
    for tile in (tiles[x] for x in np.flatnonzero(near)):
        lo0, la0, lo1, la1 = tile[6:]
        s = np.flatnonzero((hx >= lo0) & (hx <= lo1) & (hy >= la0) & (hy <= la1))
        if len(s):
            jobs.append((tuple(tile), s))
    with ThreadPoolExecutor(HEIGHT_THREADS) as ex:
        for fi, fh, nwin, _ in ex.map(lambda j: tile_heights(j[0], ids[j[1]], hx[j[1]], hy[j[1]], wkb[j[1]]), jobs):
            for k, v in zip(fi.tolist(), fh.tolist()):
                if np.isnan(h[pos[k]]):
                    h[pos[k]] = v
    t_h = time.time() - t0 - t_fit
    con.register('hh', pa.table({'id': ids.astype(np.uint32), 'h25': h}))
    con.execute(f"CREATE TABLE attrs AS {attrs_query('clean', 'hh')}")
    gap = ("CASE WHEN m.road_gap_m IS NULL AND m.nb_gap_m IS NULL THEN NULL "
           "ELSE least(coalesce(m.road_gap_m, 1e9), coalesce(m.nb_gap_m, 1e9)) END")
    rows = con.execute(f"""SELECT m.id, m.src, ST_AsWKB(m.geometry) wkb, b.height_dm, b.height_src, b.floors, {gap} gap
                           FROM clean m JOIN attrs b USING (id) ORDER BY m.id""").fetchall()
    tbl, is_rect, geoms = encode_rows(*zip(*rows))
    del rows, wkb

    # Check what is published (decoded) and the fitted footprints. A building on a carriageway, and the lower-ranked
    # building of each overlapping pair (pass B rank: OSM, then rank_conf, then larger pass A area, then lower id),
    # in either layer, is deleted from both; then the check runs again, up to CHECK_ROUNDS times.
    lon, lat = tbl['lon_e6'].to_numpy() / 1e6, tbl['lat_e6'].to_numpy() / 1e6
    bbox = [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]
    R, tree = carriageways(con, os.path.join(test['out'] if test else os.path.dirname(work), 'osm_roads.parquet').replace('\\', '/'),
                           [bbox[0] - 0.001, bbox[1] - 0.001, bbox[2] + 0.001, bbox[3] + 0.001])
    cols = ['lon_e6', 'lat_e6', 'length_dm', 'width_dm', 'angle_cdeg', 'ring']
    gd = utm([decode(r) for r in tbl.select(cols).to_pylist()])
    gc = utm(geoms)
    ri = {int(k): x for x, k in enumerate(ta['id'].to_numpy())}
    ti = np.array([ri[int(k)] for k in tbl['id'].to_numpy()])
    key = np.lexsort((tbl['id'].to_numpy(), -ta['a1'].to_numpy()[ti], -ta['rank_conf'].to_numpy()[ti]))
    rank = np.empty(len(key), np.int64); rank[key] = np.arange(len(key))      # 0 = highest
    alive = np.ones(tbl.num_rows, bool)
    for rnd in range(CHECK_ROUNDS + 1):
        k = np.flatnonzero(alive)
        res_c = {}
        for name, gu in (('', gd), ('_clean', gc)):
            onr = on_road_m2(gu[k], R, tree, projected=True)
            pi, pj, ov = pair_overlaps(gu[k], projected=True, pairs=True)
            o = ov > OVER_M2
            res_c[name] = (k[onr > OVER_M2], np.where(rank[k[pi[o]]] > rank[k[pj[o]]], k[pi[o]], k[pj[o]]))
        chk = {'on_road': len(res_c[''][0]), 'overlaps': len(res_c[''][1]),
               'on_road_clean': len(res_c['_clean'][0]), 'overlaps_clean': len(res_c['_clean'][1])}
        if not any(chk.values()) or rnd == CHECK_ROUNDS:
            break
        alive[np.concatenate([x for v in res_c.values() for x in v])] = False
    dropped = int((~alive).sum())
    if dropped:
        gone = tbl['id'].to_numpy()[~alive]
        tbl, geoms, is_rect = tbl.filter(pa.array(alive)), geoms[alive], is_rect[alive]
        con.register('gone', pa.table({'id': gone}))
        con.execute("DELETE FROM attrs WHERE id IN (SELECT id FROM gone)")
        con.execute("DELETE FROM clean WHERE id IN (SELECT id FROM gone)")
        keep_c = ~np.isin(q_id, gone)
    else:
        keep_c = np.ones(len(q_id), bool)
    lon, lat = tbl['lon_e6'].to_numpy() / 1e6, tbl['lat_e6'].to_numpy() / 1e6
    bbox = [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]
    os.makedirs(dst_dir, exist_ok=True)
    path = os.path.join(dst_dir, file)
    pq.write_table(tbl, path, compression='zstd', compression_level=19, row_group_size=500_000)
    if test and test.get('attrs_out'):
        con.execute(f"COPY attrs TO '{test['attrs_out']}' (FORMAT parquet)")
        con.execute(f"COPY (SELECT * FROM clean WHERE id IN (SELECT id FROM attrs)) TO '{test['clean_out']}' (FORMAT parquet)")
    mb = os.path.getsize(path) / 1e6
    bad = [k for k, v in chk.items() if v] + ([f'{mb:.0f} MB'] if mb >= MAX_MB else [])
    per_c = {}
    kc = assign_communes(_W['prov'], _W['comm'], q_lon[keep_c] / 1e6, q_lat[keep_c] / 1e6)[0]
    for x in kc.tolist():
        rid = _W['comm'][x]['rel_id']; per_c[rid] = per_c.get(rid, 0) + 1
    return {**entry, 'buildings': int(tbl.num_rows), 'bytes': os.path.getsize(path), 'bbox': bbox,
            'communes': [{'name': c['name'], 'osm_relation': c['osm_relation'], 'buildings': per_c.get(c['osm_relation'], 0)} for c in g['communes']],
            'check': chk, 'dropped_check': dropped, 'rect_share': round(float(is_rect.mean()), 3), 'pass_a': sa, 'pass_b': sb,
            'status': 'ok' if not bad else 'failed: ' + ', '.join(bad),
            'seconds': round(time.time() - t0), 'seconds_fit': round(t_fit), 'seconds_heights': round(t_h)}


def mem_estimate(n):
    """Estimated peak private bytes of a worker building an area of n buildings (areas.json 'n')."""
    return MEM_BASE + MEM_PER_BUILDING * n


class _MemStatus(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] + \
               [(k, ctypes.c_ulonglong) for k in ('total_phys', 'avail_phys', 'total_commit', 'avail_commit',
                                                  'total_virtual', 'avail_virtual', 'avail_ext_virtual')]


class _ProcMem(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('faults', ctypes.c_ulong)] + [(k, ctypes.c_size_t) for k in (
        'peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]


def avail_bytes():
    """Memory free for another worker: on Windows the smaller of available RAM and available commit (RAM + page
    file; a process fails to allocate when commit runs out, even with RAM free)."""
    if os.name != 'nt':
        return os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE')
    m = _MemStatus(); m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return min(m.avail_phys, m.avail_commit)


def private_bytes(pids):
    """Private (committed) bytes of processes, summed; 0 where unknown."""
    if os.name != 'nt':
        return 0
    k32, tot = ctypes.windll.kernel32, 0
    k32.OpenProcess.restype = ctypes.c_void_p
    for pid in pids:
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            c = _ProcMem(); c.cb = ctypes.sizeof(c)
            if k32.K32GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(c), c.cb):
                tot += c.private
            k32.CloseHandle(ctypes.c_void_p(h))
    return tot


def git(*a):
    for k in range(5):
        r = subprocess.run(['git', *a], cwd=REPO, capture_output=True, text=True)
        if r.returncode == 0:
            return True
        print('git', *a, 'failed:', (r.stderr or r.stdout).strip()[:300], flush=True)
        time.sleep(5 * (k + 1))
    return False


def commit(rel_path, msg):
    """Commit one file, and only that file (other staged changes in the checkout are left alone)."""
    msg += '\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'
    return git('add', '--', rel_path) and git('commit', '-q', '-m', msg, '--', rel_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings', help='inputs (vn_buildings_merged, osm_roads) and work files')
    ap.add_argument('--dst-dir', default='buildings/by_area')
    ap.add_argument('--workers', type=int, default=fit.WORKERS, help='processes (default 60 %% of the logical CPUs)')
    ap.add_argument('--area', help='only this file of commune_groups.json (testing)')
    ap.add_argument('--bbox', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), help='only buildings in this lon/lat box (testing)')
    ap.add_argument('--no-push', action='store_true', help='commit, but do not push')
    ap.add_argument('--no-git', action='store_true', help='neither commit nor push')
    ap.add_argument('--test-out', help='testing: also write the area\'s clean and attribute rows to this directory')
    ap.add_argument('--groups', default='buildings/commune_groups.json')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    a = ap.parse_args()
    limit_threads(max(1, a.workers))   # the shared prepare step gets the same budget as the pool
    t0 = time.time()
    tag = '' if not a.bbox else '_test_' + '_'.join(f'{v:.4f}' for v in a.bbox)
    work = os.path.join(a.out, '_areas' + tag).replace('\\', '/')
    st = prepare(a.out, work, a.bbox, a.groups, a.admin, a.pbf)
    print('areas', len(st['areas']), '| tiles', len(st['tiles']), f'| prepared {time.time() - t0:.0f}s', flush=True)
    done_dir = os.path.join(work, 'done'); os.makedirs(done_dir, exist_ok=True)
    done = {f[:-5]: json.load(open(os.path.join(done_dir, f), encoding='utf-8')) for f in os.listdir(done_dir) if f.endswith('.json')}
    done = {f: e for f, e in done.items() if e['status'] == 'ok'}      # a failed area is built again
    todo = [x for x in st['areas'] if (not a.area or x['file'] == a.area) and x['file'] not in done]
    test = None
    if a.bbox:
        test = {'bbox': a.bbox, 'out': a.out}
        if a.test_out:
            os.makedirs(a.test_out, exist_ok=True)
            test.update(attrs_out=os.path.join(a.test_out, 'attrs.parquet').replace('\\', '/'),
                        clean_out=os.path.join(a.test_out, 'clean.parquet').replace('\\', '/'))
    print('to build', len(todo), '| already done', len(done), flush=True)
    rel = lambda f: os.path.relpath(os.path.join(a.dst_dir, f), REPO).replace('\\', '/')
    state = {'commits': 0, 'push_t': time.time()}

    def push(force=False):
        if a.no_git or a.no_push or not state['commits']:
            return
        if force or state['commits'] >= PUSH_EVERY or time.time() - state['push_t'] > PUSH_SECONDS:
            if git('push', '-q', 'origin', 'main'):
                state['commits'], state['push_t'] = 0, time.time()

    def finish(e):
        if e['status'] == 'ok' and e['buildings'] and not a.no_git and not e.get('committed'):
            c = e['check']
            if commit(rel(e['file']), f"Buildings: {e['file']} ({e['buildings']} buildings, {c['on_road']} on roads, {c['overlaps']} overlaps)"):
                e['committed'] = True; state['commits'] += 1
        json.dump(e, open(os.path.join(done_dir, e['file'] + '.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        done[e['file']] = e
        print(f"{e['file']}: {e['status']} | {e['buildings']} buildings | {e.get('check')} | dropped_check {e.get('dropped_check', 0)} | {e['seconds']}s "
              f"(fit {e.get('seconds_fit')}s, heights {e.get('seconds_heights')}s) | {len(done)} done | {time.time() - t0:.0f}s", flush=True)
        push()

    for e in list(done.values()):                   # done before an interruption but not committed yet
        if e['status'] == 'ok' and e['buildings'] and not e.get('committed') and not a.no_git:
            finish(e)
    jobs = [(work, a.dst_dir, x, st['tiles'], test) for x in todo]
    phase_dir = os.path.join(work, 'phase'); shutil.rmtree(phase_dir, ignore_errors=True); os.makedirs(phase_dir)
    with ProcessPoolExecutor(max(1, a.workers), initializer=_init, initargs=(a.admin, a.pbf, a.groups)) as ex:
        running, k, last = {}, 0, 0.0              # future -> (file, estimated peak bytes of its area)
        while k < len(jobs) or running:
            # Memory gate, at most one start per START_GAP_S: free memory, less what the running areas are still
            # expected to grow by, must hold the next area's estimated peak plus MEM_HEADROOM (one area always runs).
            # An area past pass B is counted at AFTER_B_SHARE of its peak, so starts that interleave in time let more
            # areas run than peaks that coincide.
            if k < len(jobs) and len(running) < a.workers and time.time() - last >= START_GAP_S:
                need = mem_estimate(jobs[k][2]['n'])
                past = set(os.listdir(phase_dir))
                held = sum(p * (AFTER_B_SHARE if f in past else 1.0) for f, p in running.values())
                grow = max(0.0, held - private_bytes(list(getattr(ex, '_processes', None) or {})))
                free = avail_bytes() - grow
                if not running or free >= need + MEM_HEADROOM:
                    print(f"start {jobs[k][2]['file']} ({jobs[k][2]['n']} buildings) | est {need / 1e9:.1f} GB | "
                          f"free {free / 1e9:.1f} GB after {grow / 1e9:.1f} GB still to grow | running {len(running) + 1}", flush=True)
                    running[ex.submit(build_area, jobs[k])] = (jobs[k][2]['file'], need); k += 1; last = time.time()
            fin, _ = wait(list(running), timeout=5 if k < len(jobs) else 30, return_when=FIRST_COMPLETED)
            for f in fin:
                running.pop(f)
                try:
                    finish(f.result())
                except Exception as err:            # one bad area must not stop the others
                    print('area failed:', repr(err)[:500], flush=True)
            push()
    failed = sorted(f for f, e in done.items() if e['status'] != 'ok')
    ok = [e for e in done.values() if e['status'] == 'ok' and e['buildings']]
    if not a.area and not a.bbox:
        groups = json.load(open(a.groups, encoding='utf-8'))['groups']
        idx = os.path.join(a.dst_dir, 'index.json')
        json.dump({'rule': 'centroid in OSM commune boundary (admin_level=6 Xã/Phường/Đặc khu) inside an admin_level=4 VN-* province; '
                           'nearest commune / province when outside. Files group neighbouring communes as in commune_groups.json.',
                   'files_without_buildings': [g['file'] for g in groups if g['file'] not in {e['file'] for e in ok}],
                   'failed': failed, 'schema': 'see buildings/compact_layer.py (decode() turns a row into a polygon)',
                   'files': [{k: e.get(k, 0) for k in ('file', 'province', 'province_iso', 'buildings', 'dropped_check', 'bytes', 'bbox', 'communes')}
                             for e in sorted(ok, key=lambda e: e['file'])]},
                  open(idx, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        if not a.no_git and commit(rel('index.json'), f'Buildings: by_area/index.json ({len(ok)} files, {sum(e["buildings"] for e in ok)} buildings)'):
            state['commits'] += 1
    push(force=True)
    print(f'done {len(ok)} areas, {sum(e["buildings"] for e in ok)} buildings | failed {len(failed)}: {failed} | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
