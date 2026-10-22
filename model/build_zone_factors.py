#!/usr/bin/env python3
"""Tính hệ số tốc độ theo VÙNG (9 ô lưới) x loại đường x khung giờ.

Dữ liệu: segments_raw.jsonl (4262 đoạn) + geometries_full.json (hình tuyến).
Mỗi đoạn được gán vào ô lưới qua vị trí ước tính trên polyline.
Kết quả: zone_factors.json — tốc độ trung bình theo (zone, class, slot).
"""
import json, os
from collections import defaultdict

TRAFFIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Lưới 6x6 trên vùng nội thành Hà Nội (nơi có dữ liệu dày)
LON0, LON1 = 105.77, 106.03
LAT0, LAT1 = 20.98, 21.09
N = 8

def zone_of(lat, lon):
    if not (LON0 <= lon <= LON1 and LAT0 <= lat <= LAT1):
        return "ngoại thành"
    c = min(N-1, max(0, int((lon - LON0) / (LON1 - LON0) * N)))
    r = min(N-1, max(0, int((lat - LAT0) / (LAT1 - LAT0) * N)))
    # r: 0=nam .. 3=bắc, c: 0=tây .. 3=đông
    return f"z{r}{c}"

def poly_cumdist(poly):
    """Khoảng cách tích lũy dọc polyline (độ, xấp xỉ)."""
    import math
    cum = [0.0]
    for i in range(1, len(poly)):
        lat1, lon1 = poly[i - 1]
        lat2, lon2 = poly[i]
        d = math.hypot((lat2 - lat1) * 111, (lon2 - lon1) * 111 * math.cos(math.radians(lat1)))
        cum.append(cum[-1] + d)
    return cum

def point_at(cum, poly, dist):
    """Nội suy tọa độ tại khoảng cách dist dọc polyline."""
    for i in range(1, len(cum)):
        if cum[i] >= dist:
            t = (dist - cum[i - 1]) / max(1e-9, cum[i] - cum[i - 1])
            lat1, lon1 = poly[i - 1]
            lat2, lon2 = poly[i]
            return (lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1))
    return poly[-1]

def main():
    geoms = json.load(open(f"{TRAFFIC}/geometries_full.json"))
    # nhóm đoạn theo (route, slot), giữ thứ tự
    groups = defaultdict(list)
    for line in open(f"{TRAFFIC}/segments_raw.jsonl"):
        r = json.loads(line)
        groups[(r["route"], r["slot"])].append(r)

    agg = defaultdict(lambda: [0.0, 0.0, 0])  # (zone, cls, slot) -> [m, s, n_seg]
    unmapped = 0
    for (route, slot), segs in groups.items():
        g = geoms.get(route)
        if not g:
            unmapped += len(segs)
            continue
        poly = g["poly"]
        cum = poly_cumdist(poly)
        total_seg_m = sum(s["m"] for s in segs)
        total_poly_km = cum[-1]
        # phân bổ đoạn dọc polyline theo tỷ lệ khoảng cách
        d_acc = 0.0
        for s in segs:
            frac = s["m"] / max(1, total_seg_m)
            d_mid = (d_acc + frac / 2) * total_poly_km
            d_acc += frac
            lat, lon = point_at(cum, poly, d_mid)
            z = zone_of(lat, lon)
            cls = {"highway": "trunk"}.get(s["cls"], s["cls"])
            a = agg[(z, cls, slot)]
            a[0] += s["m"]
            a[1] += s["s"]
            a[2] += 1

    # tính tốc độ trung bình + số mẫu
    result = {}
    counts = {}
    for (z, cls, slot), (m, s, n) in agg.items():
        if s > 0:
            result.setdefault(z, {}).setdefault(cls, {})[slot] = round(m / s * 3.6, 1)
            counts.setdefault(z, {}).setdefault(cls, {})[slot] = n

    out = {"speeds": result, "counts": counts,
           "grid": {"lon0": LON0, "lon1": LON1, "lat0": LAT0, "lat1": LAT1, "n": N}}
    json.dump(out, open(f"{TRAFFIC}/zone_factors.json", "w"),
              ensure_ascii=False, indent=1)
    print(f"zones: {len(result)}, unmapped: {unmapped}")
    # tóm tắt tốc độ đêm theo vùng/lớp
    print(f"\n{'vùng':<12} {'trunk':>7} {'primary':>8} {'minor':>7}")
    for z in sorted(result.keys()):
        row = result[z]
        def v(c):
            return row.get(c, {}).get("NIGHT", row.get(c, {}).get("2230", "-"))
        print(f"{z:<12} {v('trunk'):>7} {v('primary'):>8} {v('minor'):>7}")

if __name__ == "__main__":
    main()
