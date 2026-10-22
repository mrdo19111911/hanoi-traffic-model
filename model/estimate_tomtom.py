#!/usr/bin/env python3
"""Ước lượng thời gian chuyến đi bằng TomTom Routing API trực tiếp.

Không cần OSRM server. Cần TOMTOM_API_KEY trong biến môi trường.

Cách dùng:
    export TOMTOM_API_KEY="key-của-bạn"
    python3 estimate_tomtom.py 21.0285 105.8525 21.2142 105.8000 2026-10-20T08:00:00
    # Hồ Gươm -> Nội Bài, khởi hành 8:00 ngày 20/10/2026
"""
import json, os, sys, urllib.request, urllib.parse

def tomtom_route(lat1, lon1, lat2, lon2, depart_at):
    key = os.environ.get("TOMTOM_API_KEY")
    if not key:
        print("LỖI: chưa đặt TOMTOM_API_KEY", file=sys.stderr)
        sys.exit(1)
    url = (
        "https://api.tomtom.com/routing/1/calculateRoute/"
        f"{lat1},{lon1}:{lat2},{lon2}/json"
        f"?key={key}"
        f"&departAt={urllib.parse.quote(depart_at)}"
        f"&traffic=true&computeTravelTimeFor=all"
    )
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)

def main():
    if len(sys.argv) != 6:
        print(__doc__)
        sys.exit(1)
    lat1, lon1, lat2, lon2 = map(float, sys.argv[1:5])
    depart_at = sys.argv[5]
    # TomTom cần định dạng ISO với timezone
    if "+" not in depart_at and "Z" not in depart_at:
        depart_at += "+07:00"
    data = tomtom_route(lat1, lon1, lat2, lon2, depart_at)
    route = data["routes"][0]
    s = route["summary"]
    hist = s["historicTrafficTravelTimeInSeconds"]
    notraf = s["noTrafficTravelTimeInSeconds"]
    delay = s.get("trafficDelayInSeconds", 0)
    km = s["lengthInMeters"] / 1000
    print(f"Tuyến: {km:.1f} km")
    print(f"Thời gian (có kẹt xe): {hist/60:.0f} phút")
    print(f"Thời gian (đường thoáng): {notraf/60:.0f} phút")
    print(f"Kẹt xe thêm: {delay/60:.0f} phút ({delay/hist*100:.0f}%)")

if __name__ == "__main__":
    main()
