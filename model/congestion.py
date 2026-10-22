#!/usr/bin/env python3
"""Hàm nội suy kẹt xe: f(ngày trong tuần, giờ) -> hệ số kẹt xe (%).

    f(day, hour, road_class="blended")

- day: 0=Thứ 2, 1=Thứ 3, ..., 6=Chủ nhật (hoặc chuỗi "Mon"..).
- hour: 0.0 - 24.0 (giờ trong ngày, ví dụ 17.5 = 17:30).
- road_class: "motorway" (cao tốc), "trunk" (vành đai), "primary"
  (đường thường), "minor" (phố/ngõ), hoặc "blended" (trộn chung).
- Trả về: % thời gian tăng thêm so với lúc đường thoáng.
  Ví dụ 67.0 nghĩa là đi mất thêm 67% thời gian (1h đường thoáng -> 1h40').

Cơ sở dữ liệu:
- Đường cong theo giờ: nội suy từ 23 slot đo Thứ 3 20/10/2026
  (4262 đoạn đường + 24 đoạn cao tốc HN-HP, TomTom Routing API historic).
- Hệ số ngày trong tuần: đo trực tiếp 3 tuyến x 7 ngày x 2 giờ cao điểm
  (so với Thứ 3), khớp với TomTom Traffic Index 2025.

Ví dụ:
    python3 congestion.py 4 17.5            # Thứ 6, 17:30 -> ?
    python3 congestion.py 6 8.0 primary     # Chủ nhật 8:00, đường thường
"""
import json, os, sys

TRAFFIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
FACTORS = json.load(open(os.path.join(TRAFFIC, "factors_seg.json")))["factors"]

SLOT_HOURS = [6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5, 10.5, 11.5, 12.5, 13.5,
              14.5, 15.5, 16.5, 17.0, 17.5, 18.0, 18.5, 19.0, 19.5,
              20.5, 21.5, 22.5]
SLOT_KEYS = ["0630","0700","0730","0800","0830","0900","0930","1030","1130",
             "1230","1330","1430","1530","1630","1700","1730","1800","1830",
             "1900","1930","2030","2130","2230"]
CLASSES = ("motorway", "trunk", "primary", "minor")

# Hệ số ngày (so với Thứ 3), đo tại 08:00 và 17:30.
# day: 0=Mon .. 6=Sun. Mỗi mục: (mult_0800, mult_1730).
DAY_MULT = {
    0: (0.98, 0.96),   # Thứ 2: như Thứ 3
    1: (1.00, 1.00),   # Thứ 3: mốc
    2: (0.94, 1.03),   # Thứ 4
    3: (0.95, 1.02),   # Thứ 5
    4: (0.95, 1.20),   # Thứ 6: chiều kẹt hơn 20%
    5: (0.60, 0.79),   # Thứ 7: sáng thoáng hơn nhiều
    6: (0.36, 0.74),   # Chủ nhật: sáng gần như thoáng
}
DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
             "t2": 0, "t3": 1, "t4": 2, "t5": 3, "t6": 4, "t7": 5, "cn": 6}

def _lerp(x, x0, x1, y0, y1):
    if x1 == x0:
        return y0
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)

def _base_congestion(hour, cls):
    """% kẹt xe của Thứ 3 tại giờ hour, nội suy tuyến tính giữa các slot."""
    if hour < 6.5 or hour >= 23.0:
        return 0.0
    # tìm 2 slot kẹp hour
    for i in range(len(SLOT_HOURS) - 1):
        h0, h1 = SLOT_HOURS[i], SLOT_HOURS[i + 1]
        if h0 <= hour < h1:
            f0 = FACTORS[SLOT_KEYS[i]][cls]
            f1 = FACTORS[SLOT_KEYS[i + 1]][cls]
            sf = _lerp(hour, h0, h1, f0, f1)  # speed factor
            return (1.0 / sf - 1.0) * 100.0
    # sau slot cuối
    sf = FACTORS[SLOT_KEYS[-1]][cls]
    return (1.0 / sf - 1.0) * 100.0

def _day_mult(day, hour):
    """Hệ số nhân theo ngày, nội suy theo giờ."""
    m8, m1730 = DAY_MULT[day]
    anchors = [(0.0, 1.0), (8.0, m8), (17.5, m1730), (24.0, 1.0)]
    for i in range(len(anchors) - 1):
        (h0, v0), (h1, v1) = anchors[i], anchors[i + 1]
        if h0 <= hour < h1:
            return _lerp(hour, h0, h1, v0, v1)
    return 1.0

def _parse_day(day):
    if isinstance(day, int) and 0 <= day <= 6:
        return day
    return DAY_NAMES[str(day).strip().lower()]

def f(day, hour, road_class="blended"):
    """Hệ số kẹt xe (%) tại (ngày, giờ). Xem docstring module."""
    d = _parse_day(day)
    hour = float(hour) % 24.0
    if road_class == "blended":
        vals = [_base_congestion(hour, c) for c in CLASSES]
        base = sum(vals) / len(vals)
    else:
        if road_class not in CLASSES:
            raise ValueError(f"road_class phải là một trong {CLASSES + ('blended',)}")
        base = _base_congestion(hour, road_class)
    return round(base * _day_mult(d, hour), 1)

def time_multiplier(day, hour, road_class="blended"):
    """Nhân tử thời gian: thời_gian_thực = thời_gian_thoáng * time_multiplier."""
    return round(1.0 + f(day, hour, road_class) / 100.0, 3)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)
    day, hour = sys.argv[1], float(sys.argv[2])
    rc = sys.argv[3] if len(sys.argv) > 3 else "blended"
    # day có thể là số hoặc tên
    try:
        day = int(day)
    except ValueError:
        pass
    pct = f(day, hour, rc)
    print(f"f({sys.argv[1]}, {hour}, {rc}) = {pct}% "
          f"(thời gian x{time_multiplier(day, hour, rc)})")
