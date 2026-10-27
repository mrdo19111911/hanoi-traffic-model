"""Measurement only: a per-area factor alpha = Mapillary facade height / baseline height from the few buildings the street images
measure, with frame-cut facades (censored) used as lower bounds (Tobit), instead of the biased exact-only subset.

Inputs: the `prep` + `measure` outputs of facade_heights.py for a pilot area (bld.npz, cols.npz), the per-source samples of
height_sources_sample.py (hn_sources.parquet). Nothing here writes to buildings/by_area.

    python buildings/facade_alpha.py --out data/buildings/_mapillary_nt/out --center 105.81563,20.99982 --radius 200
"""
import argparse, json, math, sys, os

import numpy as np
import pyarrow.parquet as pq
from scipy.optimize import minimize
from scipy.stats import norm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import facade_heights as F

COS_MIN, D_MIN, D_MAX, MIN_COLS = 0.26, 2.0, 40.0, 5


def observations(cols, sample_mask):
    """Per building: ('exact', h) from images whose columns are mostly complete, else ('lower', L) from frame-cut columns."""
    st, d, b, cosi, h = cols[:, 4], cols[:, 7], cols[:, 8].astype(int), cols[:, 9], cols[:, 10]
    use = (d >= D_MIN) & (d <= D_MAX) & (b >= 0) & (cosi >= COS_MIN) & np.isfinite(h)
    use &= np.where(b >= 0, sample_mask[np.clip(b, 0, len(sample_mask) - 1)], False)
    key = cols[:, 0].astype(np.int64) * 10_000_000 + b
    ex, lo = {}, {}
    for k in np.unique(key[use]):
        m = use & (key == k)
        n_ok, n_cut = int(((st == 0) & m).sum()), int(((st == 2) & m).sum())
        bi = int(k % 10_000_000)
        if n_ok >= MIN_COLS and n_cut / max(m.sum(), 1) <= 0.2:
            ex.setdefault(bi, []).append(float(np.median(h[(st == 0) & m])))
        elif n_cut >= MIN_COLS:
            lo.setdefault(bi, []).append(float(np.median(h[(st == 2) & m])))
    obs = {}
    for bi, v in ex.items():
        obs[bi] = ('exact', float(np.median(v)))
    for bi, v in lo.items():
        if bi not in obs:
            obs[bi] = ('lower', float(max(v)))
    return obs


def tobit(ly, cens, base_ly=None):
    """MLE of mu (= log alpha) and sigma for y = log(obs/base) ~ N(mu, sigma); cens: observation is a lower bound."""
    def nll(p):
        mu, ls = p; s = math.exp(ls)
        z = (ly - mu) / s
        ll = np.where(cens, norm.logsf(z), norm.logpdf(z) - ls)
        return -ll.sum()
    r = minimize(nll, [float(np.mean(ly)), math.log(max(np.std(ly), .1))], method='Nelder-Mead')
    return r.x[0], math.exp(r.x[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--center', default='105.81563,20.99982')
    ap.add_argument('--radius', type=float, default=200.0)
    ap.add_argument('--sources', default='data/buildings/_ext_heights/eval/hn_sources.parquet')
    a = ap.parse_args()
    z = np.load(os.path.join(a.out, 'bld.npz'), allow_pickle=True)
    cols = np.load(os.path.join(a.out, 'cols.npz'), allow_pickle=True)['cols']
    x0, y0 = F.to_xy(*[float(v) for v in a.center.split(',')])
    in_s = np.hypot(z['cx'] - x0, z['cy'] - y0) <= a.radius
    obs = observations(cols, in_s)
    S = {k: np.array(v) for k, v in pq.read_table(a.sources).to_pydict().items()}
    idx = {int(i): j for j, i in enumerate(S['id'])}
    sj = np.array([idx.get(int(i), -1) for i in z['id']])
    def src(name):
        v = np.full(len(sj), np.nan); ok = sj >= 0; v[ok] = S[name][sj[ok]]; return v
    h25 = z['h25']                                                   # raw Google 2.5D re-read (NaN where the raw layer had OSM)
    pub = np.where(np.isin(z['src'], ['g25cal']), z['h_pub'], np.nan)  # calibrated Google only (no OSM, est, frontrow rule)
    res = dict(n_in_sample=int(in_s.sum()), n_obs_exact=sum(1 for v in obs.values() if v[0] == 'exact'),
               n_obs_lower=sum(1 for v in obs.values() if v[0] == 'lower'))
    print(json.dumps(res))
    lines = []
    for bname, base in (('raw Google 2.5D', h25), ('Google calibrated (g25cal rows)', pub)):
        bi = np.array([b for b in obs if np.isfinite(base[b]) and base[b] > 1.0])
        if len(bi) < 6:
            print(bname, 'too few', len(bi)); continue
        kind = np.array([obs[b][0] for b in bi]); val = np.array([obs[b][1] for b in bi])
        ly = np.log(np.maximum(val, 0.5) / base[bi]); cens = kind == 'lower'
        n_e, n_c = int((~cens).sum()), int(cens.sum())
        # naive: exact-only geometric mean; Tobit: all
        naive_mu = float(ly[~cens].mean()) if n_e else float('nan')
        mu, s = tobit(ly, cens)
        rng = np.random.default_rng(7); boots = []
        for _ in range(500):
            k = rng.integers(0, len(ly), len(ly))
            try: boots.append(tobit(ly[k], cens[k])[0])
            except Exception: pass
        lo, hi = np.percentile(boots, [5, 95])
        # leave-one-out on exact observations: predicted log height vs observed, alpha=1 vs Tobit alpha (without the building)
        err1, errt = [], []
        for j in np.flatnonzero(~cens):
            m = np.ones(len(ly), bool); m[j] = False
            mu_j, _ = tobit(ly[m], cens[m])
            err1.append(abs(ly[j])); errt.append(abs(ly[j] - mu_j))
        # corrected heights for the whole sample
        sb = np.flatnonzero(in_s & np.isfinite(base) & (base > 1.0))
        corr = base[sb] * math.exp(mu)
        out = dict(baseline=bname, n_exact=n_e, n_lower=n_c, alpha_naive_exact_only=round(math.exp(naive_mu), 2) if n_e else None,
                   alpha_tobit=round(math.exp(mu), 2), alpha_ci90=[round(math.exp(lo), 2), round(math.exp(hi), 2)],
                   sigma_log=round(s, 2), loo_mae_log_alpha1=round(float(np.mean(err1)), 3) if err1 else None,
                   loo_mae_log_tobit=round(float(np.mean(errt)), 3) if errt else None,
                   n_sample_with_base=int(len(sb)), base_median_m=round(float(np.median(base[sb])), 1),
                   corrected_median_m=round(float(np.median(corr)), 1), corrected_share_ge13=round(float((corr >= 13).mean()), 2),
                   base_share_ge13=round(float((base[sb] >= 13).mean()), 2),
                   n_for_pm10pct=int(math.ceil((1.645 * s / 0.10) ** 2)))
        print(json.dumps(out, ensure_ascii=False)); lines.append(out)
    # other sources relative to the same baseline, on the whole sample and on the observed buildings
    conv = {}
    for name in ('utg', 'globfp', 'wsf3d', 'ghsl', 'gba'):
        v = src(name)
        for bname, base in (('raw Google 2.5D', h25),):
            m = in_s & np.isfinite(v) & np.isfinite(base) & (base > 1.0) & (v > 0.5)
            if m.sum() >= 5:
                r = np.log(v[m] / base[m])
                conv[name] = dict(n=int(m.sum()), alpha=round(float(math.exp(r.mean())), 2), median_ratio=round(float(np.exp(np.median(r))), 2),
                                  median_m=round(float(np.median(v[m])), 1))
    print('other sources / raw Google (same sample):', json.dumps(conv))
    # reference: how the published layer sits
    m = in_s
    print('published median %.1f, share>=13 m %.2f' % (np.median(z['h_pub'][m]), (z['h_pub'][m] >= 13).mean()))
    json.dump(dict(summary=res, fits=lines, other_sources=conv), open(os.path.join(a.out, 'alpha_report.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()
