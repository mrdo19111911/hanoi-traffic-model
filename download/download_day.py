#!/usr/bin/env python3
"""Crawl dữ liệu đoạn đường cho một ngày cụ thể (Thứ 7 / Chủ nhật / ...).

Cách dùng:
    python3 download_day.py Sat 2026-10-24
    python3 download_day.py Sun 2026-10-25

Kết quả: segments_sat.jsonl / segments_sun.jsonl trong thư mục traffic/.
Tự tiếp tục nếu bị ngắt (bỏ qua các (route, slot) đã có).
"""
import json, time, datetime, sys, os
import importlib.util

TRAFFIC = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "cs", os.path.join(TRAFFIC, "crawl_segments.py"))
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

ROUTES = [
    ("R01_hoguom_noibai", "21.0285,105.8525", "21.2142,105.8000"),
    ("R02_royal_noibai", "20.9990,105.8130", "21.2142,105.8000"),
    ("R03_times_bigc", "20.9930,105.8670", "21.0090,105.7990"),
    ("R04_hadong_noibai", "20.9800,105.7850", "21.2142,105.8000"),
    ("R05_giapbat_ngatu", "20.9860,105.8410", "21.0080,105.8150"),
    ("R06_mydinh_hoguom", "21.0280,105.7710", "21.0285,105.8525"),
    ("R07_longbien_hadong", "21.0350,105.8750", "20.9800,105.7850"),
    ("R08_caugiay_times", "21.0350,105.7950", "20.9930,105.8670"),
    ("R09_dongxuan_vanmieu", "21.0380,105.8460", "21.0278,105.8350"),
    ("R10_chohom_bachkhoa", "21.0140,105.8560", "21.0050,105.8430"),
    ("R11_nghiatan_caugiay", "21.0350,105.7950", "21.0280,105.8000"),
    ("R12_hotay_hoguom", "21.0550,105.8300", "21.0285,105.8525"),
    ("R13_bigc_hotay", "21.0090,105.7990", "21.0550,105.8300"),
    ("R14_times_vanmieu", "20.9930,105.8670", "21.0278,105.8350"),
    ("R15_giapbat_hoguom", "20.9860,105.8410", "21.0285,105.8525"),
]
SLOTS = ["0630", "0700", "0730", "0800", "0830", "0900", "0930", "1030",
         "1130", "1230", "1330", "1430", "1530", "1630", "1700", "1730",
         "1800", "1830", "1900", "1930", "2030", "2130", "2230", "NIGHT"]

# 23 khung giờ theo spec Hanh: cao điểm 30', ban ngày 1h, đêm gộp 1 slot
# NIGHT = 03:00 hôm sau (đại diện đường thoáng)

def depart_for(day_date, slot):
    if slot == "NIGHT":
        d = (datetime.datetime.strptime(day_date, "%Y-%m-%d")
             + datetime.timedelta(days=1))
        return d.strftime("%Y-%m-%d") + "T03:00:00+07:00"
    return f"{day_date}T{slot[:2]}:{slot[2:]}:00+07:00"


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    day_name, day_date = sys.argv[1], sys.argv[2]
    outfile = os.path.join(os.path.dirname(TRAFFIC), "data", f"segments_{day_name.lower()}.jsonl")

    done = set()
    if os.path.exists(outfile):
        for line in open(outfile):
            r = json.loads(line)
            done.add((r["route"], r["slot"]))
        print(f"Đã có {len(done)} cặp (route, slot), bỏ qua.")

    out = open(outfile, "a")
    n = 0
    for rid, fr, to in ROUTES:
        for slot in SLOTS:
            if (rid, slot) in done:
                continue
            dep = depart_for(day_date, slot)
            try:
                route = cs.fetch(fr, to, dep)
                segs = cs.segments_of(route, rid, slot, dep)
                for s in segs:
                    out.write(json.dumps(s, ensure_ascii=False) + "\n")
                out.flush()
                n += 1
                if n % 30 == 0:
                    print(f"  {day_name}: {n} lượt gọi", flush=True)
            except Exception as e:
                print(f"  {day_name} {rid} {slot}: LỖI {str(e)[:80]}",
                      flush=True)
            time.sleep(0.3)
    out.close()
    print(f"XONG {day_name}: {n} lượt gọi mới -> {outfile}")


if __name__ == "__main__":
    main()
