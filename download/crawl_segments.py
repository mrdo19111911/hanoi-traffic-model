#!/usr/bin/env python3
"""Crawl instruction-level segment speeds cho Hà Nội, số lượng lớn.

15 tuyến tự nhiên x 24 thời điểm (23 slot + mốc đêm 03:00).
Mỗi tuyến: tách từng chỉ dẫn -> đoạn đường (tên đường, loại đường,
quãng đường, thời gian, vận tốc). Lưu raw để gom hệ số theo loại đường.
Chạy lại an toàn: bỏ qua cặp (route, depart) đã có.
"""
import datetime, json, os, re, sys, time, urllib.parse, urllib.request

BASE = "https://api.tomtom.com"

def _api_key():
    k = os.environ.get("TOMTOM_API_KEY")
    if not k:
        raise RuntimeError(
            "Chưa đặt TOMTOM_API_KEY. Đăng ký key miễn phí tại "
            "https://developer.tomtom.com/ rồi chạy: "
            "export TOMTOM_API_KEY='key-của-bạn'")
    return k

ROUTES = [
    ("R01_hoguom_noibai",  "21.0285,105.8525", "21.2142,105.8000"),
    ("R02_royal_noibai",   "20.9990,105.8130", "21.2142,105.8000"),
    ("R03_times_bigc",     "20.9930,105.8670", "21.0090,105.7990"),
    ("R04_hadong_noibai",  "20.9800,105.7850", "21.2142,105.8000"),
    ("R05_giapbat_ngatu",  "20.9860,105.8410", "21.0080,105.8150"),
    ("R06_mydinh_hoguom",  "21.0280,105.7710", "21.0285,105.8525"),
    ("R07_longbien_hadong","21.0350,105.8750", "20.9800,105.7850"),
    ("R08_caugiay_times",  "21.0350,105.7950", "20.9930,105.8670"),
    ("R09_dongxuan_vanmieu","21.0380,105.8460", "21.0278,105.8350"),
    ("R10_chohom_bachkhoa","21.0140,105.8560", "21.0050,105.8430"),
    ("R11_nghiatan_caugiay","21.0350,105.7950", "21.0280,105.8000"),
    ("R12_hotay_hoguom",   "21.0550,105.8300", "21.0285,105.8525"),
    ("R13_bigc_hotay",     "21.0090,105.7990", "21.0550,105.8300"),
    ("R14_times_vanmieu",  "20.9930,105.8670", "21.0278,105.8350"),
    ("R15_giapbat_hoguom", "20.9860,105.8410", "21.0285,105.8525"),
]

SLOTS = ["0630","0700","0730","0800","0830","0900",
         "0930","1030","1130","1230","1330","1430","1530",
         "1630","1700","1730","1800","1830","1900",
         "1930","2030","2130","2230"]

def depart_at(slot):
    if slot == "2230":
        return "2026-10-21T02:00:00+07:00"
    if slot == "NIGHT":
        return "2026-10-21T03:00:00+07:00"
    return f"2026-10-20T{slot[:2]}:{slot[2:]}:00+07:00"

HIGHWAY_KWS = ["võ chí công", "võ nguyên giáp", "nhật tân", "thăng long",
               "vành đai", "cao tốc", "đại lộ thăng long", "ah14", "/ah",
               "trên cao", "đường dẫn"]

def split_street(message):
    m = re.search(r"(?:onto|at|on)\s+(.+)$", message or "", re.IGNORECASE)
    s = m.group(1).strip() if m else ""
    s = re.split(r"\s+toward\s+", s, flags=re.IGNORECASE)[0].strip()
    return s

def classify(street):
    s = street.lower()
    if any(kw in s for kw in HIGHWAY_KWS):
        return "highway"
    if "ngõ" in s or "ngách" in s or "hẻm" in s:
        return "minor"
    if "phố" in s:
        return "minor"
    if "cầu" in s:
        if any(kw in s for kw in ["nhật tân", "thăng long", "vĩnh tuy", "thanh trì"]):
            return "highway"
        return "primary"
    if "đường" in s or "đại lộ" in s or "quốc lộ" in s:
        return "primary"
    return "unknown"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused")

def fetch(fr, to, depart):
    params = {"travelMode": "car", "traffic": "false",
              "computeTravelTimeFor": "all", "instructionsType": "text",
              "departAt": depart, "key": _api_key()}
    url = (f"{BASE}/routing/1/calculateRoute/{fr}:{to}/json?"
           f"{urllib.parse.urlencode(params)}")
    req = urllib.request.Request(url)
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as r:
        return json.load(r)["routes"][0]

def segments_of(route, rid, slot, depart):
    ins = route["guidance"]["instructions"]
    dep = datetime.datetime.fromisoformat(depart)
    rows, prev_street, prev_cls = [], "", ""
    # tiền xử lý: tên đường cho từng chỉ dẫn (nhìn trước nếu trống)
    streets = []
    for x in ins:
        streets.append(split_street(x.get("message", "")))
    for i in range(len(streets)):
        if not streets[i]:
            for j in range(i + 1, len(streets)):
                if streets[j]:
                    streets[i] = streets[j]
                    break
    for i in range(len(ins) - 1):
        x0, x1 = ins[i], ins[i + 1]
        t0, t1 = x0.get("travelTimeInSeconds", 0), x1.get("travelTimeInSeconds", 0)
        o0, o1 = x0.get("routeOffsetInMeters", 0), x1.get("routeOffsetInMeters", 0)
        dt, dd = t1 - t0, o1 - o0
        if dt <= 0 or dd <= 0:
            continue
        street = streets[i] or prev_street
        cls = classify(street) if street else prev_cls
        if street:
            prev_street, prev_cls = street, cls
        rows.append({
            "route": rid, "slot": slot, "depart": depart,
            "t0": (dep + datetime.timedelta(seconds=t0)).strftime("%H:%M"),
            "t1": (dep + datetime.timedelta(seconds=t1)).strftime("%H:%M"),
            "street": street or "(tiếp tục)",
            "cls": cls or "unknown",
            "m": dd, "s": dt, "kmh": round(dd / dt * 3.6, 1),
        })
    # sanity: tổng thời gian đoạn ≈ tổng tuyến
    tot = sum(r["s"] for r in rows)
    exp = route["summary"].get("travelTimeInSeconds", 0)
    if exp and abs(tot - exp) > max(120, exp * 0.05):
        print(f"  WARN {rid} {slot}: segs sum {tot}s vs route {exp}s", flush=True)
    return rows

def done_keys():
    keys = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                r = json.loads(line)
                keys.add((r["route"], r["slot"]))
            except Exception:
                pass
    return keys

def main():
    done = done_keys()
    print(f"resume: {len(done)} cặp đã có", flush=True)
    fh = open(OUT, "a")
    n_new = 0
    for rid, fr, to in ROUTES:
        for slot in SLOTS + ["NIGHT"]:
            if (rid, slot) in done:
                continue
            dep = depart_at(slot)
            try:
                rows = segments_of(fetch(fr, to, dep), rid, slot, dep)
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                n_new += 1
                print(f"{rid} {slot}: {len(rows)} đoạn", flush=True)
            except Exception as e:
                print(f"{rid} {slot}: ERROR {e}", flush=True)
            time.sleep(0.35)
    fh.close()
    print(f"DONE: thêm {n_new} lượt, tổng file: {sum(1 for _ in open(OUT))} đoạn")

if __name__ == "__main__":
    main()
