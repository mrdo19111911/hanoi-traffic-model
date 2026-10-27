"""Front-row alpha: raise the front-row houses by a factor alpha over the raw Google 2.5D height.

Decided by the user on 2026-10-27: alpha = 1.7 over raw Google, no limit on the Google value, cap 8 floors (ALPHA,
CAP_FLOORS); first the front-row houses of the Ha Noi wards, the same day every ward and commune of the country. Measured against the user's own street photos
(alpha_vs_photos.py): on Nguyen Trai and Tay Son the segment medians end within +0.6 / -0.3 m of the photos (1.5:
-1.1 / -2.2 m, 2.0: +3.0 / +2.5 m); on the two Long Bien streets every alpha overshoots by 3-5 m. calibrate_heights.py
apply calls alpha_rows() before the front-row minimum (height_src 'frontrow_alpha'). This script keeps the grid report
and the scratch-dir trial for other values; `apply` here refuses the published folder.

Rule (one rule for every file in scope, all constants on the command line):
  scope   = rows of every file of buildings/commune_groups.json (wards and communes), listed as front-row houses in
            buildings/frontrow_heights.json, whose
            published height_src is g25cal / frontrow (Google based) and whose raw layer row has a Google 2.5D height.
            'est' rows (no Google reading of their own), OSM, building:part, Wikidata and sibling rows are never touched.
  height  = min(cap, max(published, alpha x raw Google)): never lowers a house, never lifts one past the cap
            (cap = cap_floors x 3.3 m; a house already above the cap keeps its height).
  floors  = max(published floors, calibrate_heights.floors_of(height)); height_src 'frontrow_alpha' where height or
            floors change. In Ha Noi the front-row minimum (3 floors, frontrow_heights.py) still applies after it, so
            calibrate_heights apply labels 'frontrow_alpha' there only above 9.9 m; elsewhere there is no minimum.
Re-runnable from the raw layer: the result depends on the raw height and the published height only, and applying it to
its own output gives the same output (max with alpha x raw is idempotent).

    .venv/Scripts/python.exe buildings/alpha_frontrow.py grid  [--alphas 1.3 1.5 2.0] [--caps 6 8 0]
    .venv/Scripts/python.exe buildings/alpha_frontrow.py apply --alpha 2.0 --cap-floors 8 --dst-dir DIR [--report R.json]
"""
import argparse, json, os, sys, time

import numpy as np, pyarrow as pa, pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calibrate_heights import LEVEL_M, dm_of, floors_of
from frontrow_heights import load_frontrow

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB_DIR = os.path.join(REPO, 'buildings', 'by_area')
RAW_DIR = os.path.join(REPO, 'data', 'buildings', '_calib', 'raw_by_area')
GROUPS = os.path.join(REPO, 'buildings', 'commune_groups.json')
FRONTROW = os.path.join(REPO, 'buildings', 'frontrow_heights.json')
SCOPE_SRC = ('g25cal', 'frontrow')
ALPHA, CAP_FLOORS = 1.7, 8                  # the user's decision, 2026-10-27
FLOOR_BINS = [(1, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 255)]


def alpha_files():
    """(file, commune names) of every commune group: the files in scope."""
    G = json.load(open(GROUPS, encoding='utf-8'))
    return [(g['file'], [c['name'] for c in g['communes']]) for g in G['groups']]


def alpha_file_names():
    return {f for f, _ in alpha_files()}


def alpha_rows(name, ids, new_src, h25, new_h, fr_ids, files):
    """For calibrate_heights apply: (positions, new heights in m) of the rows of file `name` the rule raises: a file in
    scope, a listed front-row id, now 'g25cal', with a raw Google height; heights by rule() with ALPHA and CAP_FLOORS."""
    if name not in files or fr_ids is None or not len(fr_ids):
        return np.zeros(0, int), np.zeros(0)
    k = np.flatnonzero(np.isin(ids, fr_ids) & (new_src == 'g25cal') & np.isfinite(h25) & (h25 > 0))
    nh, _ = rule(new_h[k], np.ones(len(k), int), h25[k], ALPHA, CAP_FLOORS)
    up = nh > new_h[k] + 1e-9
    return k[up], nh[up]


def load(name, fr_ids):
    pub = pq.read_table(os.path.join(PUB_DIR, name))
    raw = pq.read_table(os.path.join(RAW_DIR, name), columns=['id', 'height_dm', 'height_src'])
    ids = pub.column('id').to_numpy()
    if not np.array_equal(ids, raw.column('id').to_numpy()):
        raise RuntimeError(f'{name}: published and raw ids differ')
    src = np.array(pub.column('height_src').to_pylist(), object)
    raw_g = np.array([s == 'g25' for s in raw.column('height_src').to_pylist()])
    front = np.isin(ids, fr_ids.get(name, np.zeros(0, np.int64)))
    scope = front & np.isin(src, SCOPE_SRC) & raw_g
    return dict(pub=pub, src=src, h=pub.column('height_dm').to_numpy().astype(float) / 10,
                fl=pub.column('floors').to_numpy().astype(int), raw_h=raw.column('height_dm').to_numpy().astype(float) / 10,
                front=front, scope=scope, est_front=front & (src == 'est'))


def rule(h, fl, raw_h, alpha, cap_floors):
    cap = cap_floors * LEVEL_M if cap_floors else np.inf
    new_h = np.where(h >= cap, h, np.minimum(cap, np.maximum(h, alpha * raw_h)))
    new_h = dm_of(new_h).astype(float) / 10
    new_fl = np.maximum(fl, floors_of(new_h).astype(int))
    return new_h, new_fl


def dist(h, fl):
    if not len(h):
        return dict(n=0)
    r = lambda v: round(float(v), 1)
    return dict(n=int(len(h)), median_m=r(np.median(h)), p90_m=r(np.percentile(h, 90)), mean_m=r(h.mean()),
                floors_share={f'{a}-{b}' if b < 255 and a != b else (f'{a}+' if b == 255 else str(a)):
                              round(float(((fl >= a) & (fl <= b)).mean()), 3) for a, b in FLOOR_BINS})


def run(names, fr_ids, alpha, cap_floors, dst=None):
    per, H0, F0, H1, F1 = {}, [], [], [], []
    for name, wards in names:
        D = load(name, fr_ids)
        s = D['scope']
        nh, nf = rule(D['h'][s], D['fl'][s], D['raw_h'][s], alpha, cap_floors)
        changed = (nh != D['h'][s]) | (nf != D['fl'][s])
        per[name] = dict(wards=wards, front_row=int(D['front'].sum()), scope=int(s.sum()), changed=int(changed.sum()),
                         est_front_row_untouched=int(D['est_front'].sum()),
                         before=dist(D['h'][s], D['fl'][s]), after=dist(nh, nf))
        H0.append(D['h'][s]); F0.append(D['fl'][s]); H1.append(nh); F1.append(nf)
        if dst:
            h_dm = D['pub'].column('height_dm').to_numpy().copy()
            fl = D['pub'].column('floors').to_numpy().copy()
            src = D['src'].copy()
            pos = np.flatnonzero(s)[changed]
            h_dm[pos] = dm_of(nh[changed]); fl[pos] = nf[changed]; src[pos] = 'frontrow_alpha'
            t = D['pub']
            t = t.set_column(t.schema.get_field_index('height_dm'), 'height_dm', pa.array(h_dm, pa.uint16()))
            t = t.set_column(t.schema.get_field_index('floors'), 'floors', pa.array(fl, pa.uint8()))
            t = t.set_column(t.schema.get_field_index('height_src'), 'height_src',
                             pa.array(src.tolist(), pa.string()).dictionary_encode().cast(t.schema.field('height_src').type))
            out = os.path.join(dst, name)
            pq.write_table(t, out + '.tmp', compression='zstd', compression_level=19, row_group_size=500_000)
            os.replace(out + '.tmp', out)
            per[name]['file_mb'] = round(os.path.getsize(out) / 1e6, 1)
    cat = lambda L: np.concatenate(L) if L else np.zeros(0)
    total = dict(scope=sum(p['scope'] for p in per.values()), changed=sum(p['changed'] for p in per.values()),
                 est_front_row_untouched=sum(p['est_front_row_untouched'] for p in per.values()),
                 before=dist(cat(H0), cat(F0).astype(int)), after=dist(cat(H1), cat(F1).astype(int)))
    return dict(alpha=alpha, cap_floors=cap_floors or None, total=total, files=per)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('grid', help='before/after distributions for several alpha x cap, no files written')
    g.add_argument('--alphas', type=float, nargs='+', default=[1.3, 1.5, 2.0])
    g.add_argument('--caps', type=int, nargs='+', default=[6, 8, 0], help='cap in floors, 0 = no cap')
    g.add_argument('--report')
    p = sub.add_parser('apply', help='write the files with the rule applied into --dst-dir (never by_area)')
    p.add_argument('--alpha', type=float, default=ALPHA)
    p.add_argument('--cap-floors', type=int, default=CAP_FLOORS, help='0 = no cap')
    p.add_argument('--dst-dir', required=True)
    p.add_argument('--report')
    a = ap.parse_args()
    names, fr_ids, t = alpha_files(), load_frontrow(FRONTROW), time.time()
    if a.cmd == 'grid':
        res = [run(names, fr_ids, al, cap) for al in a.alphas for cap in a.caps]
        for r in res:
            T = r['total']
            print(f"alpha {r['alpha']:.2f} cap {r['cap_floors'] or '-':>2}: scope {T['scope']:,} changed {T['changed']:,} | "
                  f"median {T['before']['median_m']} -> {T['after']['median_m']} m, p90 {T['before']['p90_m']} -> "
                  f"{T['after']['p90_m']} m | floors after {T['after']['floors_share']}")
        print('before', res[0]['total']['before'])
    else:
        dst = os.path.abspath(a.dst_dir)
        if os.path.normcase(dst) == os.path.normcase(os.path.abspath(PUB_DIR)):
            raise SystemExit('refusing to write into buildings/by_area: calibrate_heights.py apply writes it')
        os.makedirs(dst, exist_ok=True)
        res = run(names, fr_ids, a.alpha, a.cap_floors, dst)
        print(json.dumps(res['total'], ensure_ascii=False))
    if a.report:
        json.dump(res, open(a.report, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'{len(names)} files, {time.time() - t:.0f} s')


if __name__ == '__main__':
    main()
