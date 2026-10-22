#!/usr/bin/env python3
"""Mô hình ước lượng thời gian di chuyển chính xác nhất.

    estimate(lat1, lon1, lat2, lon2, day, hour) -> phút

Kết hợp:
1. Tốc độ gốc THỰC ĐO ở Hà Nội theo từng vùng (9 ô lưới) x loại đường
   — không dùng tốc độ mặc định châu Âu của OSRM.
2. Hệ số kẹt xe theo vùng x loại đường x khung giờ (23 slot, nội suy mượt).
3. Hệ số ngày trong tuần (đo thực tế: T6 chiều x1.2, T7/CN sáng thấp).

Cách chạy:
    python3 estimate.py 21.0285 105.8525 21.2142 105.8000 1 8.0
    (Hồ Gươm -> Nội Bài, Thứ 3, 8:00)
"""
import json, os, math, sys, urllib.request

TRAFFIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
_ZD = json.load(open(f"{TRAFFIC}/zone_factors.json", encoding="utf-8"))
ZONE = _ZD["speeds"]
ZCOUNTS = _ZD["counts"]
_GRID = _ZD["grid"]
# trung bình toàn thành phố theo (class, slot) để fallback
CITY_AVG = {}
for z, cd in ZONE.items():
    for cls, sd in cd.items():
        for slot, v in sd.items():
            CITY_AVG.setdefault((cls, slot), []).append(v)
for k in CITY_AVG:
    CITY_AVG[k] = sum(CITY_AVG[k]) / len(CITY_AVG[k])
MIN_SAMPLES = 5
DAY_MULT = {0: (0.98, 0.96), 1: (1.00, 1.00), 2: (0.94, 1.03),
            3: (0.95, 1.02), 4: (0.95, 1.20), 5: (0.60, 0.79),
            6: (0.36, 0.74)}
SLOT_HOURS = [6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.5, 11.5, 12.5, 13.5,
              14.5, 15.5, 16.5, 17.0, 17.5, 18.0, 18.5, 19.0, 19.5,
              20.5, 21.5, 22.5]
SLOT_KEYS = ["0630","0700","0730","0800","0830","0900","0930","1030","1130",
             "1230","1330","1430","1530","1630","1700","1730","1800","1830",
             "1900","1930","2030","2130","2230"]
CLASSES = ("motorway", "trunk", "primary", "minor")

# lưới vùng (đọc từ zone_factors.json)
LON0, LON1 = _GRID["lon0"], _GRID["lon1"]
LAT0, LAT1 = _GRID["lat0"], _GRID["lat1"]
N = _GRID["n"]
def zone_of(lat, lon):
    if not (LON0 <= lon <= LON1 and LAT0 <= lat <= LAT1):
        return "ngoại thành"
    c = min(N-1, max(0, int((lon - LON0) / (LON1 - LON0) * N)))
    r = min(N-1, max(0, int((lat - LAT0) / (LAT1 - LAT0) * N)))
    return f"z{r}{c}"

def _lerp(x, x0, x1, y0, y1):
    t = (x - x0) / (x1 - x0) if x1 != x0 else 0
    return y0 + t * (y1 - y0)

def _raw_speed(zone, cls, slot):
    """Tốc độ gốc, fallback về trung bình thành phố nếu thiếu mẫu."""
    v = ZONE.get(zone, {}).get(cls, {}).get(slot)
    n = ZCOUNTS.get(zone, {}).get(cls, {}).get(slot, 0)
    if v is not None and n >= MIN_SAMPLES:
        return v
    return CITY_AVG.get((cls, slot), 25.0)

def zone_class_speed(zone, cls, hour):
    """Tốc độ (km/h) của (vùng, loại đường) tại giờ hour, nội suy slot."""
    if hour < 6.5 or hour >= 23.0:
        return _raw_speed(zone, cls, "NIGHT")
    for i in range(len(SLOT_HOURS) - 1):
        h0, h1 = SLOT_HOURS[i], SLOT_HOURS[i + 1]
        if h0 <= hour < h1:
            v0 = _raw_speed(zone, cls, SLOT_KEYS[i])
            v1 = _raw_speed(zone, cls, SLOT_KEYS[i + 1])
            return _lerp(hour, h0, h1, v0, v1)
    return _raw_speed(zone, cls, SLOT_KEYS[-1])

def day_mult(day, hour):
    m8, m17 = DAY_MULT[day]
    for (h0, v0), (h1, v1) in [((0, 1.0), (8, m8)), ((8, m8), (17.5, m17)),
                               ((17.5, m17), (24, 1.0))]:
        if h0 <= hour < h1:
            return _lerp(hour, h0, h1, v0, v1)
    return 1.0

# tỷ trọng loại đường theo vùng (từ dữ liệu thực đo)
ZONE_CLASS_W = {
    "ngoại thành": {"trunk": 0.75, "primary": 0.15, "minor": 0.07, "motorway": 0.03},
    "z00": {"trunk": 0.25, "primary": 0.72, "minor": 0.03},
    "z01": {"primary": 0.61, "minor": 0.35, "trunk": 0.04},
    "z10": {"primary": 0.52, "minor": 0.46, "trunk": 0.02},
    "z11": {"primary": 0.37, "minor": 0.60, "trunk": 0.03},
    "z20": {"primary": 0.82, "minor": 0.18},
    "z21": {"primary": 0.73, "minor": 0.27},
}
DEFAULT_W = {"trunk": 0.2, "primary": 0.4, "minor": 0.35, "motorway": 0.05}

def zone_speed_blended(zone, day, hour):
    w = ZONE_CLASS_W.get(zone, DEFAULT_W)
    tot = sum(w.values())
    return sum(zone_speed(zone, c, day, hour) * w[c] / tot for c in w)

def zone_speed(zone, cls, day, hour):
    """Tốc độ thực (km/h) của (vùng, loại đường) tại (ngày, giờ)."""
    v_night = zone_class_speed(zone, cls, 23.5)  # tốc độ đêm = gốc
    v_tue = zone_class_speed(zone, cls, hour)    # tốc độ Thứ 3 tại giờ đó
    if v_tue <= 0:
        return v_night
    c_tue = v_night / v_tue - 1                  # % kẹt xe Thứ 3 (dạng phân số)
    c_day = max(0.0, c_tue * day_mult(day, hour))  # % kẹt xe ngày đó
    return v_night / (1 + c_day)

def osrm_route(lat1, lon1, lat2, lon2):
    url = (f"{OSRM_BASE_URL}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
           f"?overview=full&geometries=geojson&steps=true")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)

def estimate(lat1, lon1, lat2, lon2, day, hour):
    """Ước lượng thời gian (phút) cho chuyến đi."""
    route = osrm_route(lat1, lon1, lat2, lon2)["routes"][0]
    coords = route["geometry"]["coordinates"]  # [lon, lat]
    total_s = 0.0
    total_m = 0.0
    # đi dọc geometry, gán từng chặng vào vùng
    for i in range(1, len(coords)):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        mid_lat, mid_lon = (lat_a + lat_b) / 2, (lon_a + lon_b) / 2
        d_m = math.hypot((lat_b - lat_a) * 111320,
                         (lon_b - lon_a) * 111320 * math.cos(math.radians(mid_lat)))
        if d_m < 1:
            continue
        zone = zone_of(mid_lat, mid_lon)
        v = zone_speed_blended(zone, day, hour)
        total_s += d_m / (v / 3.6)
        total_m += d_m
    return {"minutes": round(total_s / 60, 1),
            "km": round(total_m / 1000, 2)}

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 7:
        print(__doc__)
        sys.exit(0)
    lat1, lon1, lat2, lon2 = map(float, sys.argv[1:5])
    day, hour = int(sys.argv[5]), float(sys.argv[6])
    r = estimate(lat1, lon1, lat2, lon2, day, hour)
    print(f"Ước lượng: {r['km']} km, {r['minutes']} phút (ngày {day}, {hour}h)")
