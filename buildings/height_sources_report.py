"""Measurement only: score the independent height sources sampled by height_sources_sample.py against truth.

Truth sets (Hà Nội): OSM height/levels truth rows of data/buildings/_calib/truth.parquet (real = height or levels * 3.3),
its levels >= 3 subset, and Wikidata / OSM building:part heights taken from the published layer.
Per source: coverage, MAE, bias, median ratio, Spearman rho and the share within one floor (3.3 m), overall, by
real-height band and on the rows every source covers. Then a spatial-block cross-validation (2 km blocks, 5 folds) of
a per-source log-log fit and of an ensemble (ridge on the log of all sources), and the distribution of each source on
129 Nguyễn Trãi (200 m) and on the inner-city box.

    python buildings/height_sources_report.py [--sources .../hn_sources.parquet] [--out reports-dir]
"""
import argparse, json, os

import numpy as np, pyarrow.parquet as pq

BANDS = [(0, 10), (10, 20), (20, 30), (30, 60), (60, 1e9)]
FLOOR = 3.3


def rank(x):
    o = np.argsort(x, kind='stable'); r = np.empty(len(x)); r[o] = np.arange(len(x)); return r


def spearman(a, b):
    if len(a) < 5: return np.nan
    return float(np.corrcoef(rank(a), rank(b))[0, 1])


def score(pred, real):
    m = np.isfinite(pred)
    if m.sum() < 5: return dict(n=int(m.sum()))
    p, r = pred[m], real[m]
    e = p - r
    return dict(n=int(m.sum()), cover=round(float(m.mean()), 3), mae=round(float(np.abs(e).mean()), 2), bias=round(float(e.mean()), 2),
                med_ratio=round(float(np.median(p / np.maximum(r, .1))), 2), rho=round(spearman(p, r), 3),
                within1=round(float((np.abs(e) <= FLOOR).mean()), 3))


def block_cv(X, y, blocks, folds=5, seed=20261024, ridge=1.0):
    """log-space linear fit on the sources in X (NaN-free rows), 5 folds of 2 km blocks; returns out-of-fold prediction (m)."""
    rng = np.random.default_rng(seed)
    ub = np.unique(blocks); fold_of = dict(zip(ub, rng.integers(0, folds, len(ub))))
    f = np.array([fold_of[b] for b in blocks])
    out = np.full(len(y), np.nan)
    A = np.c_[np.log(np.maximum(X, .5)), np.ones(len(y))]
    ly = np.log(np.maximum(y, .5))
    for k in range(folds):
        tr, te = f != k, f == k
        if tr.sum() < 20 or te.sum() == 0: continue
        reg = ridge * np.eye(A.shape[1]); reg[-1, -1] = 0
        w = np.linalg.solve(A[tr].T @ A[tr] + reg, A[tr].T @ ly[tr])
        out[te] = np.exp(A[te] @ w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sources', default='data/buildings/_ext_heights/eval/hn_sources.parquet')
    ap.add_argument('--truth', default='data/buildings/_calib/truth.parquet')
    ap.add_argument('--out', default='data/buildings/_ext_heights/eval')
    a = ap.parse_args()
    S = pq.read_table(a.sources).to_pydict()
    S = {k: np.array(v) for k, v in S.items()}
    srcs = [k for k in ('raw_g25', 'globfp', 'utg', 'ms', 'gba', 'ghsl', 'wsf3d', 'ovt') if k in S]
    T = {k: np.array(v) for k, v in pq.read_table(a.truth).to_pydict().items()}
    hn = np.char.startswith(T['file'].astype(str), 'ha_noi')
    idx = {int(i): j for j, i in enumerate(S['id'])}
    rows = [(j, idx.get(int(i), -1)) for j, i in enumerate(T['id']) if hn[j]]
    tj = np.array([r[0] for r in rows if r[1] >= 0]); sj = np.array([r[1] for r in rows if r[1] >= 0])
    real = T['real'][tj].astype(float)
    S['raw_g25'] = S['raw_g25'].astype(float)
    S['raw_g25'][sj] = T['h25'][tj]          # raw layer holds the OSM value on OSM rows; truth.parquet has the Google 2.5D re-read there
    sets = {'osm_truth': (sj, real, np.ones(len(sj), bool)), 'osm_levels>=3': (sj, real, T['floors'][tj] >= 3)}
    for tag in ('wikidata', 'osm_part'):
        m = np.where(S['pub_src'] == tag)[0]
        sets[tag] = (m, S['pub_h'][m], np.ones(len(m), bool))
    res = {}
    lines = []
    for name, (sidx, y, keep) in sets.items():
        sidx, y = sidx[keep], y[keep]
        res[name] = {'n_truth': int(len(y))}
        full = [s for s in srcs if np.isfinite(S[s][sidx]).mean() >= .8]   # sources with wide coverage define the common rows
        common = np.all([np.isfinite(S[s][sidx]) for s in full], axis=0)
        fs = ', '.join(full)
        lines.append(f'\n### Truth: {name} (n={len(y)}; rows covered by every wide-coverage source [{fs}]: {int(common.sum())})\n')
        lines.append('| source | cover | MAE m | bias m | med ratio | rho | within 1 floor | MAE (common rows) | bias (common) |')
        lines.append('|---|---|---|---|---|---|---|---|---|')
        for s in srcs:
            p = S[s][sidx]
            r_all = score(p, y); pc = np.where(common, p, np.nan); r_c = score(pc, y)
            res[name][s] = {'all': r_all, 'common': r_c, 'bands': {f'{lo}-{hi}': score(np.where((y >= lo) & (y < hi), p, np.nan), y)
                                                                    for lo, hi in BANDS}}
            lines.append(f"| {s} | {r_all.get('cover', '')} | {r_all.get('mae', '')} | {r_all.get('bias', '')} | {r_all.get('med_ratio', '')} | "
                         f"{r_all.get('rho', '')} | {r_all.get('within1', '')} | {r_c.get('mae', '')} | {r_c.get('bias', '')} |")
        # block CV of per-source fits and of the ensemble, on the common rows
        if common.sum() > 200:
            lon, lat = S['lon'][sidx][common], S['lat'][sidx][common]
            blocks = (np.floor(lon * 111320 * np.cos(np.radians(21)) / 2000) * 100000 + np.floor(lat * 110540 / 2000)).astype(np.int64)
            yc = y[common]
            lines.append(f'\nBlock-CV (2 km blocks, 5 folds, log-linear fit, common rows n={int(common.sum())}):\n')
            lines.append('| model | MAE m | bias m | rho | within 1 floor | MAE 30-60 | MAE >60 |')
            lines.append('|---|---|---|---|---|---|---|')
            res[name]['cv'] = {}
            combos = {f'fit({s})': [s] for s in full} | {'ensemble(' + '+'.join(full) + ')': full}
            for cname, cs in combos.items():
                X = np.c_[[S[s][sidx][common] for s in cs]].T
                pr = block_cv(X, yc, blocks)
                sc = score(pr, yc)
                b3 = score(np.where((yc >= 30) & (yc < 60), pr, np.nan), yc); b6 = score(np.where(yc >= 60, pr, np.nan), yc)
                res[name]['cv'][cname] = {'all': sc, '30-60': b3, '>60': b6}
                lines.append(f"| {cname} | {sc.get('mae')} | {sc.get('bias')} | {sc.get('rho')} | {sc.get('within1')} | {b3.get('mae', '')} | {b6.get('mae', '')} |")
    # distributions on 129 Nguyen Trai and the inner-city box
    kx, ky = 111320 * np.cos(np.radians(21)), 110540
    d = np.hypot((S['lon'] - 105.81563) * kx, (S['lat'] - 20.99982) * ky)
    inner = (S['lon'] > 105.775) & (S['lon'] < 105.885) & (S['lat'] > 20.965) & (S['lat'] < 21.075)
    lines.append('\n### Distribution per source (height m; share >= 9 m = 3+ floors)\n')
    lines.append('| area | n | source | covered | median | mean | share >= 9 m |')
    lines.append('|---|---|---|---|---|---|---|')
    res['areas'] = {}
    for aname, m in (('129 Nguyen Trai 200 m', d <= 200), ('inner-city box', inner)):
        res['areas'][aname] = {}
        for s in srcs + ['pub_h']:
            v = S[s][m]; ok = np.isfinite(v)
            if ok.sum() == 0: continue
            row = dict(covered=int(ok.sum()), median=round(float(np.median(v[ok])), 1), mean=round(float(v[ok].mean()), 1),
                       share3=round(float((v[ok] >= 9).mean()), 3))
            res['areas'][aname][s] = row
            lines.append(f"| {aname} | {int(m.sum())} | {s} | {row['covered']} | {row['median']} | {row['mean']} | {row['share3']} |")
    os.makedirs(a.out, exist_ok=True)
    json.dump(res, open(os.path.join(a.out, 'sources_report.json'), 'w'), indent=1)
    open(os.path.join(a.out, 'sources_report.md'), 'w', encoding='utf-8').write('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
