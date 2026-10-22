"""Throwaway spike: spectral clustering of Vietnam's big-road (A) network.

Reads osrm/data/big_roads.geojson, builds the junction graph (degree-2 chains
contracted), runs normalized-Laplacian spectral clustering (Ng-Jordan-Weiss)
for several k, compares against k-means on projected coordinates, and writes
summary.json plus clusters_k16.geojson next to this script.

Run from the repo root:
    .venv/Scripts/python.exe reports/spectral_spike/spectral_spike.py
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.cluster.vq import kmeans2
from scipy.sparse import coo_matrix, diags, identity
from scipy.sparse.csgraph import connected_components
from scipy.sparse.linalg import eigsh

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SRC = ROOT / "osrm" / "data" / "big_roads.geojson"

SPEED_KMH = {"caotoc": 80.0, "quoclo": 50.0, "tinhlo": 40.0}
FERRY_KMH = 15.0
MIN_HOURS = 0.5 / 60.0  # clip so tiny junction stubs do not dominate weights
KS = (8, 16, 32, 64)
RESTARTS = 10
SEED = 20261020
SIGMA_KM = 10.0  # for the alternative exp(-km/sigma) affinity
N_CUSTOMERS = 2000  # hypothetical VRP size for the work estimate

PALETTE = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#800000", "#aaffc3", "#000075",
]


def haversine_km(lon1, lat1, lon2, lat2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_segments():
    """Split every kept LineString at shared vertices into node-to-node segments."""
    data = json.loads(SRC.read_text(encoding="utf-8"))
    feats = []
    dropped = {"fragment": 0, "isolated": 0, "nonline": 0}
    for f in data["features"]:
        p = f["properties"]
        if p.get("fragment"):
            dropped["fragment"] += 1
            continue
        if p.get("isolated"):
            dropped["isolated"] += 1
            continue
        if f["geometry"]["type"] != "LineString":
            dropped["nonline"] += 1
            continue
        coords = [(round(x * 1e6), round(y * 1e6)) for x, y in f["geometry"]["coordinates"]]
        feats.append((coords, p))

    uses = defaultdict(int)
    for coords, _ in feats:
        for c in coords:
            uses[c] += 1
    node_of = {}
    for coords, _ in feats:
        for c in (coords[0], coords[-1]):
            node_of.setdefault(c, len(node_of))
    for c, n in uses.items():
        if n > 1:
            node_of.setdefault(c, len(node_of))

    segs = []  # (u, v, km, hours, group)
    for coords, p in feats:
        kmh = FERRY_KMH if p.get("bridge_kind") == "ferry" else SPEED_KMH[p["group"]]
        start, km = node_of[coords[0]], 0.0
        for prev, cur in zip(coords, coords[1:]):
            km += haversine_km(prev[0] / 1e6, prev[1] / 1e6, cur[0] / 1e6, cur[1] / 1e6)
            if cur in node_of:
                segs.append((start, node_of[cur], km, km / kmh, p["group"]))
                start, km = node_of[cur], 0.0
    xy = np.zeros((len(node_of), 2))
    for (x, y), i in node_of.items():
        xy[i] = (x / 1e6, y / 1e6)
    return segs, xy, len(feats), dropped


def contract_chains(segs, n_nodes):
    """Merge degree-2 chains; return junction ids and junction-graph chains."""
    inc = [[] for _ in range(n_nodes)]
    for i, (u, v, *_rest) in enumerate(segs):
        inc[u].append(i)
        inc[v].append(i)
    key = [len(e) != 2 for e in inc]
    used = bytearray(len(segs))
    chains = []  # (a, b, km, hours)
    for s in range(n_nodes):
        if not key[s]:
            continue
        for e0 in inc[s]:
            if used[e0]:
                continue
            node, e, km, hrs = s, e0, 0.0, 0.0
            while True:
                used[e] = 1
                u, v, k, h, _ = segs[e]
                km += k
                hrs += max(h, 0.0)
                node = v if u == node else u
                if key[node]:
                    break
                nxt = [x for x in inc[node] if not used[x]]
                if not nxt:
                    break
                e = nxt[0]
            chains.append((s, node, km, hrs))
    loops_skipped = len(segs) - sum(used)
    return key, chains, loops_skipped


class JunctionGraph:
    def __init__(self, chains, key, xy):
        jid = {}
        for a, b, *_ in chains:
            for n in (a, b):
                if n not in jid:
                    jid[n] = len(jid)
        pairs = defaultdict(lambda: [0.0, 0.0, 0.0, math.inf, 0])  # cond, km, sum w_exp, min h, n
        self_km = defaultdict(float)
        for a, b, km, h in chains:
            ia, ib = jid[a], jid[b]
            if ia == ib:
                self_km[ia] += km
                continue
            k = (min(ia, ib), max(ia, ib))
            rec = pairs[k]
            rec[0] += 1.0 / max(h, MIN_HOURS)
            rec[1] += km
            rec[2] += math.exp(-km / SIGMA_KM)
            rec[3] = min(rec[3], h)
            rec[4] += 1
        n = len(jid)
        a = np.array([k[0] for k in pairs], dtype=np.int64)
        b = np.array([k[1] for k in pairs], dtype=np.int64)
        recs = np.array(list(pairs.values()), dtype=float)
        orig = np.empty(n, dtype=np.int64)
        for o, i in jid.items():
            orig[i] = o
        sk = np.zeros(n)
        for i, km in self_km.items():
            sk[i] = km

        # keep the largest connected component
        w = coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n))
        ncomp, lab = connected_components(w, directed=False)
        main = np.bincount(lab).argmax()
        keep = lab == main
        remap = -np.ones(n, dtype=np.int64)
        remap[keep] = np.arange(keep.sum())
        em = keep[a] & keep[b]
        self.n = int(keep.sum())
        self.components_before = int(ncomp)
        self.a, self.b = remap[a[em]], remap[b[em]]
        self.w_time = recs[em, 0]
        self.km = recs[em, 1]
        self.w_exp = recs[em, 2]
        self.chain_count = recs[em, 4].astype(int)
        self.self_km = sk[keep]
        self.lonlat = xy[orig[keep]]
        self.dropped_nodes = int(n - self.n)
        self.dropped_km = float(recs[~em, 1].sum() + sk[~keep].sum())

    def adjacency(self, weights):
        n = self.n
        m = coo_matrix((weights, (self.a, self.b)), shape=(n, n)).tocsr()
        return (m + m.T).tocsr()

    def projected_km(self):
        lon, lat = self.lonlat[:, 0], self.lonlat[:, 1]
        lat0 = math.radians(float(np.mean(lat)))
        return np.column_stack(
            (np.radians(lon) * 6371.0 * math.cos(lat0), np.radians(lat) * 6371.0)
        )


def spectral_embedding(W, kmax):
    deg = np.asarray(W.sum(axis=1)).ravel()
    dinv = diags(1.0 / np.sqrt(deg))
    L = identity(W.shape[0], format="csc") - dinv @ W @ dinv
    vals, vecs = eigsh(L.tocsc(), k=kmax, sigma=-1e-5, which="LM")
    order = np.argsort(vals)
    return vals[order], vecs[:, order]


def best_kmeans(X, k, rng):
    best, best_inertia = None, math.inf
    for _ in range(RESTARTS):
        cent, lab = kmeans2(X, k, minit="++", iter=50, missing="warn",
                            seed=int(rng.integers(1 << 31)))
        inertia = float(((X - cent[lab]) ** 2).sum())
        if inertia < best_inertia:
            best, best_inertia = lab, inertia
    # relabel to 0..m-1 in case a cluster came out empty
    _, best = np.unique(best, return_inverse=True)
    return best


def evaluate(g, labels, W_weights):
    k = int(labels.max()) + 1
    la, lb = labels[g.a], labels[g.b]
    cut = la != lb
    deg_w = np.bincount(g.a, W_weights, g.n) + np.bincount(g.b, W_weights, g.n)
    vol = np.bincount(labels, deg_w, k)
    cut_w = np.bincount(la[cut], W_weights[cut], k) + np.bincount(lb[cut], W_weights[cut], k)
    total_vol = vol.sum()
    conductance = cut_w / np.minimum(vol, total_vol - vol)
    ones = np.ones(len(g.a))
    deg_u = np.bincount(g.a, ones, g.n) + np.bincount(g.b, ones, g.n)
    vol_u = np.bincount(labels, deg_u, k)
    cut_u = np.bincount(la[cut], ones[cut], k) + np.bincount(lb[cut], ones[cut], k)
    cond_u = cut_u / np.minimum(vol_u, vol_u.sum() - vol_u)

    nodes = np.bincount(labels, minlength=k)
    inner = ~cut
    km_in = np.bincount(la[inner], g.km[inner], k) + np.bincount(labels, g.self_km, k)

    # contiguity: components of each cluster's induced subgraph
    m = coo_matrix((np.ones(inner.sum()), (g.a[inner], g.b[inner])), shape=(g.n, g.n))
    _, comp = connected_components(m, directed=False)
    pieces = np.zeros(k, dtype=int)
    largest_share = np.zeros(k)
    for c in range(k):
        cc = comp[labels == c]
        counts = np.bincount(cc)
        counts = counts[counts > 0]
        pieces[c] = len(counts)
        largest_share[c] = counts.max() / counts.sum()

    boundary = np.zeros(g.n, dtype=bool)
    boundary[g.a[cut]] = True
    boundary[g.b[cut]] = True
    b_per = np.bincount(labels[boundary], minlength=k)

    # VRP work estimate: customers spread proportionally to road km per cluster
    share = km_in / km_in.sum()
    n_i = share * N_CUSTOMERS
    B = int(boundary.sum())
    full_pairs = N_CUSTOMERS * (N_CUSTOMERS - 1)
    intra = float((n_i * (n_i - 1)).clip(min=0).sum())
    access = float(2 * (n_i * b_per).sum())
    transit = B * (B - 1)
    inter_compose = float(np.outer(n_i * b_per, n_i * b_per).sum()
                          - ((n_i * b_per) ** 2).sum())

    def stats(x):
        x = np.asarray(x, dtype=float)
        return {"min": round(float(x.min()), 1), "median": round(float(np.median(x)), 1),
                "max": round(float(x.max()), 1)}

    return {
        "k_effective": k,
        "nodes": stats(nodes),
        "km": stats(km_in),
        "cut_edges": int(cut.sum()),
        "cut_chains": int(g.chain_count[cut].sum()),
        "cut_km": round(float(g.km[cut].sum()), 1),
        "cut_km_share": round(float(g.km[cut].sum() / (g.km.sum() + g.self_km.sum())), 4),
        "conductance": {"mean": round(float(conductance.mean()), 4),
                        "max": round(float(conductance.max()), 4),
                        "min": round(float(conductance.min()), 4)},
        "conductance_unweighted": {"mean": round(float(cond_u.mean()), 4),
                                   "max": round(float(cond_u.max()), 4)},
        "contiguous_clusters": int((pieces == 1).sum()),
        "worst_cluster_pieces": int(pieces.max()),
        "min_largest_piece_share": round(float(largest_share.min()), 3),
        "boundary_nodes": B,
        "boundary_per_cluster": stats(b_per),
        "vrp_work_estimate": {
            "customers": N_CUSTOMERS,
            "full_matrix_pairs": full_pairs,
            "intra_cluster_pairs": round(intra),
            "point_to_boundary_pairs": round(access),
            "boundary_transit_pairs": transit,
            "routes_needed_clustered": round(intra + access + transit),
            "min_plus_ops_for_inter_pairs": round(inter_compose),
        },
        "per_cluster": [
            {"cluster": c, "nodes": int(nodes[c]), "km": round(float(km_in[c]), 1),
             "boundary_nodes": int(b_per[c]), "conductance": round(float(conductance[c]), 4),
             "pieces": int(pieces[c])}
            for c in range(k)
        ],
    }


def write_geojson(g, labels, path):
    cut = labels[g.a] != labels[g.b]
    boundary = np.zeros(g.n, dtype=bool)
    boundary[g.a[cut]] = True
    boundary[g.b[cut]] = True
    feats = [
        {"type": "Feature",
         "geometry": {"type": "Point",
                      "coordinates": [round(float(x), 6), round(float(y), 6)]},
         "properties": {"cluster": int(c), "color": PALETTE[int(c) % len(PALETTE)],
                        "boundary": bool(bd)}}
        for (x, y), c, bd in zip(g.lonlat, labels, boundary)
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                               separators=(",", ":")), encoding="utf-8")


def main():
    t = {}
    t0 = time.perf_counter()
    segs, xy, kept, dropped = load_segments()
    t["load_split_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    key, chains, loops = contract_chains(segs, len(xy))
    g = JunctionGraph(chains, key, xy)
    t["contract_s"] = time.perf_counter() - t0

    graph_info = {
        "features_kept": kept,
        "features_dropped": dropped,
        "split_nodes": len(xy),
        "split_segments": len(segs),
        "split_km": round(sum(s[2] for s in segs), 1),
        "degree2_only_loops_skipped_segments": loops,
        "junction_components_before_main": g.components_before,
        "junction_nodes_main": g.n,
        "junction_edges_main": int(len(g.a)),
        "junction_chains_main": int(g.chain_count.sum()),
        "main_km": round(float(g.km.sum() + g.self_km.sum()), 1),
        "dropped_off_main_km": round(g.dropped_km, 1),
    }
    print(json.dumps(graph_info, indent=1))

    rng = np.random.default_rng(SEED)
    results = {}
    variants = {"time": g.w_time, "exp_km": g.w_exp}
    for name, wts in variants.items():
        W = g.adjacency(wts)
        t0 = time.perf_counter()
        vals, vecs = spectral_embedding(W, max(KS))
        t[f"eigsh_{name}_s"] = time.perf_counter() - t0
        results[name] = {"eigenvalues_first_10": [float(f"{v:.3e}") for v in vals[:10]],
                         "eigenvalue_k": {str(k): float(f"{vals[k - 1]:.3e}") for k in KS},
                         "k": {}}
        for k in KS:
            t0 = time.perf_counter()
            U = vecs[:, :k]
            U = U / np.linalg.norm(U, axis=1, keepdims=True)
            lab = best_kmeans(U, k, rng)
            dt = time.perf_counter() - t0
            res = evaluate(g, lab, g.w_time)
            res["kmeans_s"] = round(dt, 2)
            results[name]["k"][str(k)] = res
            print(name, k, {x: res[x] for x in ("cut_edges", "cut_km", "contiguous_clusters",
                                                "boundary_nodes")}, res["conductance"])
            if name == "time" and k == 16:
                write_geojson(g, lab, OUT / "clusters_k16.geojson")

    # baseline: k-means on projected coordinates, same weights for conductance
    P = g.projected_km()
    results["baseline_xy_kmeans"] = {"k": {}}
    for k in KS:
        t0 = time.perf_counter()
        lab = best_kmeans(P, k, rng)
        dt = time.perf_counter() - t0
        res = evaluate(g, lab, g.w_time)
        res["kmeans_s"] = round(dt, 2)
        results["baseline_xy_kmeans"]["k"][str(k)] = res
        print("xy", k, {x: res[x] for x in ("cut_edges", "cut_km", "contiguous_clusters",
                                            "boundary_nodes")}, res["conductance"])

    summary = {
        "source": str(SRC.relative_to(ROOT)).replace("\\", "/"),
        "settings": {"speeds_kmh": SPEED_KMH, "ferry_kmh": FERRY_KMH,
                     "min_hours_clip": MIN_HOURS, "ks": KS, "kmeans_restarts": RESTARTS,
                     "seed": SEED, "sigma_km_exp_variant": SIGMA_KM,
                     "affinity_time": "sum over parallel chains of 1/hours",
                     "affinity_exp_km": "sum over parallel chains of exp(-km/sigma)",
                     "method": "NJW: smallest-k eigvecs of I - D^-1/2 W D^-1/2 "
                               "(eigsh shift-invert), row-normalised, kmeans2 ++",
                     "conductance_weights": "time affinity for all rows incl. baseline",
                     "baseline": "k-means on equirectangular-projected junction coords"},
        "graph": graph_info,
        "runtime_s": {k: round(v, 2) for k, v in t.items()},
        "results": results,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    print(json.dumps(summary["runtime_s"]))


if __name__ == "__main__":
    main()
