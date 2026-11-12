# Ước lượng thời gian di chuyển Hà Nội

Ứng dụng cục bộ trên Windows: OSRM chọn tuyến, thời gian đi được tính từ mẫu tốc độ TomTom nội suy theo **lớp đường / thứ / khung giờ**. Backend FastAPI + giao diện React/Leaflet. Cùng kho mã còn có bản đồ đường lớn toàn quốc (`/api/big-roads/*`), lớp nhà toàn quốc (`buildings/`) và lớp tốc độ cho ma trận VRP (`speedmatrix/`); xem `CLAUDE.md` để biết kiến trúc chi tiết.

Dữ liệu TomTom (`data/*.json*`, `data/api_raw/`) **không nằm trong Git**: bản clone mới phải crawl trước khi ước lượng được.

## Cài đặt và chạy (Windows)

Yêu cầu: Python 3.14, Node.js (npm), Internet cho lần chuẩn bị đầu (tải OSRM và bản đồ Việt Nam từ Geofabrik, ~331 MB).

```powershell
python app.py setup     # tạo .venv, pip, dựng OSRM, chỉ mục lớp đường, bản đồ đường lớn, npm ci + build
python app.py start     # chạy OSRM (cổng 5000) rồi web (cổng 8000), chạy nền; log ở runtime/web.log
python app.py status
python app.py stop
```

Mở http://127.0.0.1:8000. Cả OSRM lẫn web chỉ nhận kết nối từ máy này. Muốn dùng OSRM ở địa chỉ khác, đặt biến môi trường `OSRM_BASE_URL`.

Chỉ dựng/chạy OSRM:

```powershell
python osrm/manage.py setup|start|stop|status [--threads N] [--port P] [--rebuild]
```

## Lấy mẫu tốc độ (cần TomTom API key)

1. Đăng ký key miễn phí tại https://developer.tomtom.com/ và đặt `TOMTOM_API_KEY` (biến môi trường, hoặc `HKCU\Environment`). Key không bao giờ được ghi vào log, phản hồi hay snapshot.
2. Crawl từ giao diện (nút crawl, tối đa 60 lượt gọi mỗi lần) hoặc dòng lệnh:

```powershell
.venv/Scripts/python.exe download/crawl_spatial.py --date 2026-10-20 --slots 0800 1730 --max-calls 12
```

Mỗi tuyến trong `download/routes.py` được gọi một lần cho mỗi (ngày, khung giờ); snapshot lưu ở `data/api_raw/` và không bao giờ gọi lại. Mọi snapshot được map-match qua OSRM `/match`, tra lớp đường trong `osrm/data/road_classes.sqlite` và gộp thành `data/spatial_samples.jsonl`. Đoạn nào lớp đường lẫn hoặc không rõ thì bị loại, không lấy trung bình.

So sánh các phương pháp nội suy trên dữ liệu giữ lại:

```powershell
.venv/Scripts/python.exe model/compare_spatial.py [--holdout route|snapshot|street]   # -> reports/spatial_comparison.json
```

## Kiểm thử

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests      # Python, offline (không cần OSRM, mạng, API key)
cd frontend; npm test                                          # vitest
```

## Nguyên tắc của mô hình

- Năm lớp đường theo tag OSM (`model/osm_classes.py`): `caotoc / quoclo / chinh / noido / nho`.
- Tốc độ **không bao giờ mượn giữa các lớp**; thiếu dữ liệu thì báo lỗi, không thay thế ngầm.
- Chỉ mục lớp đường và OSRM phải dựng từ cùng một bản PBF (`pbf_sha256` khớp), nếu không crawler và bộ ước lượng từ chối chạy.
- Quy ước: thứ 0 = Thứ Hai … 6 = Chủ nhật; khung giờ `HHMM`; giờ địa phương UTC+7.

## Nguồn

[OSRM v26.10.0](https://github.com/Project-OSRM/osrm-backend/releases/tag/v26.10.0) · [bản đồ Việt Nam](https://download.geofabrik.de/asia/vietnam.html) · © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL · TomTom Routing API (gói miễn phí).
