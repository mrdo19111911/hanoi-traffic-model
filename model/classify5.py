#!/usr/bin/env python3
"""Phân loại đường 5 lớp theo spec của Hanh (2026-10-19):

1. caotoc    — đường cao tốc (CT.01, cao tốc HN-HP, Đại lộ Thăng Long...)
2. quoclo    — đường quốc lộ (QL1, QL5, QL32, AH14...)
3. chinh     — đường chính (Giải Phóng, Trường Chinh, vành đai...)
4. noido     — đường nội đô (phố, đường nhỏ trong đô thị)
5. nho       — đường nhỏ (ngõ, ngách, hẻm)

Quy tắc nội suy vận tốc dọc tuyến (cùng loại đường):
- A->B = v1 (biết), B->C chưa biết -> B->C = v1.
- A->B = v1, B->C chưa biết, C->D = v2 -> B->C = (v1+v2)/2.
"""
import json

def classify5(street):
    """Phân loại tên đường thành 5 lớp."""
    s = street.lower()
    # 1. cao tốc
    if any(k in s for k in ["cao tốc", "ct.01", "ct.02", "ct.03", "ct.04",
                             "đại lộ thăng long", "đường trên cao"]):
        return "caotoc"
    # 2. quốc lộ (kể cả AH — đường quốc tế nhưng bản chất là quốc lộ)
    if any(k in s for k in ["quốc lộ", " ql", "ql.", "ql1", "ql5", "ql32",
                             "ah1", "ah14", " á "]):
        # " á " để bắt "AH" viết rời; kiểm tra thêm
        return "quoclo"
    if "ah" in s.replace(" ", ""):
        # bắt "AH14", "AH 14" nhưng tránh nhầm lẫn
        import re
        if re.search(r"\bah\s*\d", s):
            return "quoclo"
    # 5. đường nhỏ
    if any(k in s for k in ["ngõ", "ngách", "hẻm"]):
        return "nho"
    # 4. đường nội đô (phố)
    if "phố" in s:
        return "noido"
    # cầu: cầu lớn thuộc quốc lộ/cao tốc, cầu nhỏ là đường chính
    if "cầu" in s:
        if any(k in s for k in ["nhật tân", "thăng long", "vĩnh tuy", "thanh trì",
                                 "phù đổng", "đông trù"]):
            return "quoclo"  # cầu trên quốc lộ/vành đai
        return "chinh"
    # 3. đường chính
    if any(k in s for k in ["đường", "đại lộ", "vành đai"]):
        return "chinh"
    return "unknown"


def interp_speeds(speeds, distances=None, max_dist=500):
    """Nội suy vận tốc dọc tuyến theo quy tắc Hanh.

    speeds: list, phần tử là float hoặc None (chưa biết).
    distances: list độ dài mỗi đoạn (mét). Nếu None, coi mỗi đoạn 1 đơn vị.
    max_dist: chỉ nội suy trong phạm vi này (mét) từ điểm đã biết.
    Quy tắc: None được điền bằng giá trị trước đó (v1) trong 500m;
    nếu sau đó gặp giá trị biết v2 trong 500m, điền đoạn giữa bằng (v1+v2)/2.
    Vượt quá 500m từ mọi điểm biết -> giữ None (dùng fallback vùng).

    Thiết kế để chạy lại khi có dữ liệu mới: hàm này thuần túy
    (không lưu trạng thái), gọi lại bất cứ lúc nào với dữ liệu mới nhất.
    """
    n = len(speeds)
    if distances is None:
        distances = [1.0] * n
    out = list(speeds)
    # vị trí tích lũy (mét) của mỗi đoạn
    pos = []
    acc = 0.0
    for d in distances:
        pos.append(acc + d / 2)  # điểm giữa đoạn
        acc += d

    for i in range(n):
        if out[i] is not None:
            continue
        # tìm điểm biết gần nhất phía trước (cùng loại đường - do caller đảm bảo)
        v1, d1 = None, float("inf")
        for k in range(i - 1, -1, -1):
            if speeds[k] is not None:
                v1, d1 = speeds[k], pos[i] - pos[k]
                break
        # tìm điểm biết gần nhất phía sau
        v2, d2 = None, float("inf")
        for k in range(i + 1, n):
            if speeds[k] is not None:
                v2, d2 = speeds[k], pos[k] - pos[i]
                break
        if v1 is not None and v2 is not None and d1 <= max_dist and d2 <= max_dist:
            out[i] = (v1 + v2) / 2
        elif v1 is not None and d1 <= max_dist:
            out[i] = v1
        elif v2 is not None and d2 <= max_dist:
            out[i] = v2
        # else: giữ None
    return out


if __name__ == "__main__":
    # test phân loại
    tests = [
        ("Cao tốc Hà Nội - Hải Phòng", "caotoc"),
        ("Quốc lộ 1A", "quoclo"),
        ("QL5", "quoclo"),
        ("AH14", "quoclo"),
        ("Đường Giải Phóng", "chinh"),
        ("Vành đai 3", "chinh"),
        ("Phố Huế", "noido"),
        ("Ngõ 12 Nguyễn Trãi", "nho"),
        ("Cầu Vĩnh Tuy", "quoclo"),
        ("Cầu Chương Dương", "chinh"),
    ]
    for name, exp in tests:
        got = classify5(name)
        print(f"{'OK' if got==exp else 'SAI'}: {name} -> {got} (mong {exp})")
    # test nội suy (giới hạn 500m)
    print(interp_speeds([50, None, None, 30], [200, 200, 200, 200]))
    # [50, 40.0, 40.0, 30] - nội suy vì trong 500m
    print(interp_speeds([50, None, None, None], [200, 200, 200, 200]))
    # [50, 50, 50, None] - đoạn cuối vượt 500m, giữ None
