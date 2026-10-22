"""Simulated delivery orders around Hanoi and their OSRM time/distance matrix.

One depot in Sóc Sơn and N orders (default 500), drawn with a fixed seed:
- --hanoi-share of the orders inside Thành phố Hà Nội (osrm/data/vietnam_provinces.geojson), of which --inner-share
  inside the 12 former urban districts (OSM boundary=historic relations, ended 2025-06-30);
- the rest outside Hà Nội, inside Vietnam, within --radius-km of central Hanoi (Hồ Hoàn Kiếm).
A drawn location is kept only if OSRM snaps it to a road within --snap-m and not onto a motorway (the snap with
exclude=motorway must land on the same spot), and the order takes the snapped location. The order must be reachable
from and to the depot.

Writes to <out> (default reports/simulated_orders):
- orders.csv: idx, id, kind (depot / noi_thanh / ngoai_thanh / tinh_khac), lon, lat, road, snap_m, km_from_hanoi;
- duration_s.csv and distance_m.csv: (N+1) x (N+1) matrices, row = from, column = to, in orders.csv order (0 = depot);
- matrix.json: the same, plus settings; inner_districts.geojson: the district polygons used (cached);
- map.html: the orders on a Leaflet map.

    .venv/Scripts/python.exe reports/simulated_orders/build_orders.py [--osrm http://127.0.0.1:5059] [--n 500] [--seed 20261021]
"""
import argparse
import csv
import json
import math
from pathlib import Path
import time
import urllib.request

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PBF = ROOT / "osrm/data/vietnam-latest.osm.pbf"
CENTER = (105.8522, 21.0287)  # Hồ Hoàn Kiếm
DEPOT = (105.848603, 21.257187)  # đường Núi Đôi, Sóc Sơn
INNER = {7115863: "Long Biên", 9421130: "Thanh Xuân", 9421131: "Hoàn Kiếm", 9421132: "Cầu Giấy", 9421133: "Hoàng Mai",
         9421134: "Hai Bà Trưng", 9421135: "Đống Đa", 9421136: "Ba Đình", 9421137: "Nam Từ Liêm",
         9421138: "Bắc Từ Liêm", 9421139: "Tây Hồ", 9424587: "Hà Đông"}


def get(url):
    with urllib.request.urlopen(url, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def rings_of(ways):
    """Join way node lists into closed rings."""
    rings, open_ = [], [list(w) for w in ways]
    while open_:
        ring = open_.pop()
        while ring[0] != ring[-1]:
            for i, w in enumerate(open_):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[::-1][1:]
                else:
                    continue
                open_.pop(i)
                break
            else:
                raise RuntimeError("ring does not close")
        rings.append(ring)
    return rings


def inner_districts(cache):
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    import osmium
    members = {}
    for r in osmium.FileProcessor(str(PBF), osmium.osm.RELATION):
        if r.id in INNER:
            members[r.id] = [m.ref for m in r.members if m.type == "w"]
    wanted = {w for ws in members.values() for w in ws}
    coords = {}
    for o in osmium.FileProcessor(str(PBF), osmium.osm.NODE | osmium.osm.WAY).with_locations():
        if o.is_way() and o.id in wanted:
            coords[o.id] = [(round(n.lon, 7), round(n.lat, 7)) for n in o.nodes]
    features = [{"type": "Feature", "properties": {"relation": rid, "name": INNER[rid]},
                 "geometry": {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings_of([coords[w] for w in ws])]}}
                for rid, ws in members.items()]
    doc = {"type": "FeatureCollection", "features": features}
    cache.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


def rings_from(features):
    out = []
    for f in features:
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        out += [np.array(ring, float) for poly in polys for ring in poly]
    return out


def inside(rings, x, y):
    """Even-odd rule over all rings (holes included)."""
    hit = False
    for r in rings:
        if not (r[:, 0].min() <= x <= r[:, 0].max() and r[:, 1].min() <= y <= r[:, 1].max()):
            continue
        x0, y0, x1, y1 = r[:-1, 0], r[:-1, 1], r[1:, 0], r[1:, 1]
        cross = ((y0 > y) != (y1 > y)) & (x < (x1 - x0) * (y - y0) / np.where(y1 == y0, 1e-12, y1 - y0) + x0)
        hit ^= bool(cross.sum() % 2)
    return hit


def km(a, b):
    dx = (a[0] - b[0]) * 111.32 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(dx, (a[1] - b[1]) * 110.57)


def snap(osrm, lon, lat, max_m):
    """Snapped (lon, lat, road, metres) or None when too far or only a motorway is near."""
    w = get(f"{osrm}/nearest/v1/driving/{lon:.6f},{lat:.6f}?number=1")["waypoints"][0]
    if w["distance"] > max_m:
        return None
    alt = get(f"{osrm}/nearest/v1/driving/{lon:.6f},{lat:.6f}?number=1&exclude=motorway")["waypoints"][0]
    if km(w["location"], alt["location"]) > 0.001:
        return None
    return w["location"][0], w["location"][1], w["name"], round(w["distance"], 1)


MAP = """<!doctype html><html><head><meta charset="utf-8"><title>Đơn giả lập</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{height:100%;margin:0}.lg{background:#fff;padding:6px 8px;font:13px sans-serif;line-height:1.5}</style></head>
<body><div id="m"></div><script>
const P=__P__, D=__D__, Q=__Q__, R=__R__;
const m=L.map('m').setView([21.03,105.85],8);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap'}).addTo(m);
L.geoJSON(D,{style:{color:'#555',weight:1,fillOpacity:0.05}}).addTo(m);
L.circle([21.0287,105.8522],{radius:R*1000,color:'#999',weight:1,fill:false,dashArray:'4'}).addTo(m);
const C={kho:'#000',noi_thanh:'#d62728',ngoai_thanh:'#1f77b4',tinh_khac:'#2ca02c'};
for(const [la,lo,k,id,r] of P) L.circleMarker([la,lo],{radius:k=='kho'?9:5,color:C[k],fillOpacity:0.9,weight:1}).bindTooltip(id+' '+k+(r?' · '+r:'')).addTo(m);
const lg=L.control({position:'topright'});lg.onAdd=()=>{const e=L.DomUtil.create('div','lg');
e.innerHTML='<b>● Kho</b> Sóc Sơn<br><span style=color:#d62728>●</span> nội thành '+Q.noi_thanh+'<br><span style=color:#1f77b4>●</span> ngoại thành HN '+Q.ngoai_thanh+'<br><span style=color:#2ca02c>●</span> tỉnh khác '+Q.tinh_khac;return e};lg.addTo(m);
</script></body></html>"""


def write_map(path, orders, districts, quota, radius_km):
    pts = [[o["lat"], o["lon"], o["kind"], o["id"], o["road"]] for o in orders]
    html = (MAP.replace("__P__", json.dumps(pts, ensure_ascii=False)).replace("__D__", json.dumps(districts, ensure_ascii=False))
            .replace("__Q__", json.dumps(quota)).replace("__R__", str(radius_km)))
    path.write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--osrm", default="http://127.0.0.1:5059")
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--hanoi-share", type=float, default=0.5)
    ap.add_argument("--inner-share", type=float, default=0.3, help="share of the Hà Nội orders in the inner districts")
    ap.add_argument("--radius-km", type=float, default=200)
    ap.add_argument("--snap-m", type=float, default=300)
    ap.add_argument("--seed", type=int, default=20261021)
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    provinces = json.loads((ROOT / "osrm/data/vietnam_provinces.geojson").read_text(encoding="utf-8"))["features"]
    hanoi = rings_from([f for f in provinces if f["properties"]["name"] == "Thành phố Hà Nội"])
    vietnam = rings_from(provinces)
    districts = inner_districts(args.out / "inner_districts.geojson")
    inner = rings_from(districts["features"])
    print(f"[{time.perf_counter() - started:6.1f}s] polygons ready", flush=True)

    n_hanoi = round(args.n * args.hanoi_share)
    n_inner = round(n_hanoi * args.inner_share)
    quota = {"noi_thanh": n_inner, "ngoai_thanh": n_hanoi - n_inner, "tinh_khac": args.n - n_hanoi}
    boxes = {"noi_thanh": inner, "ngoai_thanh": hanoi, "tinh_khac": None}
    rng = np.random.default_rng(args.seed)
    d = snap(args.osrm, *DEPOT, args.snap_m)
    orders = [{"id": "KHO", "kind": "kho", "lon": d[0], "lat": d[1], "road": d[2], "snap_m": d[3]}]
    seen = {(d[0], d[1])}
    for kind, count in quota.items():
        rings = boxes[kind]
        if rings is not None:
            pts = np.vstack(rings)
            lo, hi = pts.min(0), pts.max(0)
        drawn = 0
        while drawn < count:
            if rings is not None:
                x, y = rng.uniform(lo[0], hi[0]), rng.uniform(lo[1], hi[1])
            else:  # uniform over the disk around the centre
                r, a = args.radius_km * math.sqrt(rng.uniform()), rng.uniform(0, 2 * math.pi)
                x = CENTER[0] + r * math.cos(a) / (111.32 * math.cos(math.radians(CENTER[1])))
                y = CENTER[1] + r * math.sin(a) / 110.57
            if not inside(vietnam, x, y):
                continue
            if kind == "noi_thanh" and not inside(inner, x, y):
                continue
            if kind == "ngoai_thanh" and (not inside(hanoi, x, y) or inside(inner, x, y)):
                continue
            if kind == "tinh_khac" and inside(hanoi, x, y):
                continue
            s = snap(args.osrm, x, y, args.snap_m)
            if s is None or (s[0], s[1]) in seen:
                continue
            # the snapped spot must still be in the drawn zone
            if (kind == "noi_thanh") != inside(inner, s[0], s[1]) or (kind == "tinh_khac") == inside(hanoi, s[0], s[1]):
                continue
            seen.add((s[0], s[1]))
            drawn += 1
            orders.append({"id": f"DH{len(orders):04d}", "kind": kind, "lon": s[0], "lat": s[1], "road": s[2], "snap_m": s[3]})
        print(f"[{time.perf_counter() - started:6.1f}s] {kind}: {count}", flush=True)

    coords = ";".join(f"{o['lon']:.6f},{o['lat']:.6f}" for o in orders)
    t = get(f"{args.osrm}/table/v1/driving/{coords}?annotations=duration,distance")
    dur, dist = np.array(t["durations"], float), np.array(t["distances"], float)
    bad = int((~np.isfinite(dur)).sum())
    if bad:
        raise SystemExit(f"{bad} pairs without a route")
    for o in orders:
        o["km_from_hanoi"] = round(km((o["lon"], o["lat"]), CENTER), 2)
    with (args.out / "orders.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["idx", "id", "kind", "lon", "lat", "road", "snap_m", "km_from_hanoi"])
        for i, o in enumerate(orders):
            w.writerow([i, o["id"], o["kind"], o["lon"], o["lat"], o["road"], o["snap_m"], o["km_from_hanoi"]])
    ids = [o["id"] for o in orders]
    for name, m, fmt in (("duration_s.csv", dur, "{:.1f}"), ("distance_m.csv", dist, "{:.1f}")):
        with (args.out / name).open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["from\\to", *ids])
            for i, row in enumerate(m):
                w.writerow([ids[i], *(fmt.format(v) for v in row)])
    settings = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}
    (args.out / "matrix.json").write_text(json.dumps({"settings": settings, "quota": quota, "orders": orders,
                                                      "duration_s": dur.round(1).tolist(), "distance_m": dist.round(1).tolist()},
                                                     ensure_ascii=False), encoding="utf-8")
    write_map(args.out / "map.html", orders, districts, quota, args.radius_km)
    off = ~np.eye(len(orders), dtype=bool)
    print(f"[{time.perf_counter() - started:6.1f}s] matrix {dur.shape}, duration h median {np.median(dur[off]) / 3600:.2f}"
          f" max {dur.max() / 3600:.2f}, distance km median {np.median(dist[off]) / 1000:.1f} max {dist.max() / 1000:.1f}", flush=True)


if __name__ == "__main__":
    main()
