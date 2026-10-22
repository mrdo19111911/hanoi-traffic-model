# Mô hình ước lượng thời gian di chuyển Hà Nội

Ước lượng thời gian đi từ A đến B tại bất kỳ ngày/giờ nào, dựa trên dữ liệu
giao thông thực đo từ TomTom.

## Cài đặt

Yêu cầu: Python 3.8+ (không cần thư viện ngoài cho mô hình cơ bản).

```bash
cd hanoi-traffic-model
# Không cần pip install gì thêm cho estimate.py và congestion.py
```

## Sử dụng nhanh

### 1. Ước lượng thời gian chuyến đi

```bash
python3 model/estimate.py 21.0285 105.8525 21.2142 105.8000 1 8.0
# Hồ Gươm -> Nội Bài, Thứ 3 (1), 8:00
# Kết quả: 26.76 km, ~50 phút
```

Tham số: `vĩ_độ_đi kinh_độ_đi vĩ_độ_đến kinh_độ_đến ngày giờ`
- ngày: 0=Thứ 2, 1=Thứ 3, ..., 6=Chủ nhật
- giờ: số thập phân (8.5 = 8:30)

Lưu ý: `estimate.py` cần OSRM server chạy ở `localhost:5000` để lấy tuyến đường.
Nếu không có OSRM, dùng `estimate_tomtom.py` (gọi trực tiếp TomTom API).

#### Dựng OSRM trên Windows

Chạy tại thư mục gốc dự án, cần Python 3.8+ và Internet cho lần chuẩn bị đầu:

```powershell
python osrm/manage.py setup
python osrm/manage.py start
python osrm/manage.py status
```

Lệnh `setup` tải các chương trình Windows x64 chính thức của **OSRM v26.10.0**,
profile ô tô cùng phiên bản và bản đồ Việt Nam từ Geofabrik (khoảng 331 MB
tại thời điểm thiết lập). Bản đồ được kiểm tra MD5 trước khi xử lý theo
pipeline `extract -> partition -> customize` với thuật toán MLD. Không cần
Docker, WSL hay TomTom API key. Mặc định dùng 4 luồng; có thể đổi bằng
`python osrm/manage.py setup --threads 2`.

Server chạy nền tại **http://127.0.0.1:5000**, chỉ nhận kết nối trên máy này.
Chạy lại `start` nếu đã tắt máy; lệnh không tự cấu hình khởi động cùng Windows.
Mô hình dùng địa chỉ IPv4 này để tránh độ trễ thử kết nối IPv6 của `localhost`
trên một số máy Windows. Nếu dùng server hoặc cổng khác, đặt biến môi trường
`OSRM_BASE_URL` trước khi chạy `estimate.py`.

```powershell
# Kiểm tra toàn bộ luồng lấy tuyến + ước lượng thời gian
python model/estimate.py 21.0285 105.8525 21.2142 105.8000 1 8.0

# Tắt server do script này quản lý
python osrm/manage.py stop
```

Dữ liệu sinh ra nằm trong `osrm/data/`, log trong `osrm/logs/`; các tệp lớn
được loại khỏi Git. `setup` ghi nhận từng bước đã hoàn thành để chạy tiếp nếu
bị gián đoạn. Dùng `setup --rebuild` để xử lý lại bản đồ đã tải. Muốn cập nhật
bản đồ, trước tiên dừng server, chuyển cả tệp `vietnam-latest.osm.pbf` và
`vietnam-latest.osm.pbf.md5` sang thư mục lưu trữ khác rồi chạy lại `setup`.

OSRM chọn tuyến theo profile ô tô; `estimate.py` dùng hình tuyến này để tính
thời gian bằng mô hình tốc độ riêng. Dữ liệu OpenStreetMap không bổ sung
giao thông thời gian thực cho OSRM.

Nguồn: [OSRM v26.10.0](https://github.com/Project-OSRM/osrm-backend/releases/tag/v26.10.0),
[hướng dẫn OSRM](https://github.com/Project-OSRM/osrm-backend#quick-start),
[bản đồ Việt Nam](https://download.geofabrik.de/asia/vietnam.html).
Bản đồ: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright),
giấy phép ODbL.

### 2. Hàm nội suy kẹt xe

```bash
python3 model/congestion.py 4 17.5 primary
# Thứ 6, 17:30, đường chính -> % thời gian tăng thêm
```

### 3. Phân loại đường 5 lớp

```bash
python3 model/classify5.py
# Test phân loại: cao tốc / quốc lộ / đường chính / đường nội đô / đường nhỏ
```

## Tải dữ liệu mới (cần TomTom API key)

1. Đăng ký key miễn phí tại https://developer.tomtom.com/
2. Đặt key vào biến môi trường:
   ```bash
   export TOMTOM_API_KEY="key-của-bạn"
   ```
3. Crawl dữ liệu cho một ngày:
   ```bash
   python3 download/download_day.py Sat 2026-10-24
   # Kết quả: data/segments_sat.jsonl
   ```

## Cấu trúc

```
hanoi-traffic-model/
├── README.md
├── model/
│   ├── estimate.py          # Ước lượng thời gian (cần OSRM hoặc TomTom)
│   ├── estimate_tomtom.py   # Ước lượng dùng trực tiếp TomTom API
│   ├── congestion.py        # f(ngày, giờ, loại đường) -> % kẹt xe
│   ├── classify5.py         # Phân loại 5 lớp + nội suy 500m
│   └── build_zone_factors.py# Tính hệ số theo vùng từ dữ liệu thô
├── download/
│   ├── download_day.py      # Crawl 1 ngày (15 tuyến × 24 thời điểm)
│   └── crawl_segments.py    # Code crawl gốc (dùng bởi download_day.py)
└── data/
    ├── segments_raw.jsonl   # Thứ 3 20/10/2026: 4262 đoạn
    ├── segments_sat.jsonl   # Thứ 7 24/10/2026 (đang crawl)
    ├── segments_sun.jsonl   # Chủ nhật 25/10/2026
    ├── segments_exp.jsonl   # Cao tốc HN-HP: 24 đoạn
    ├── factors_5class.json  # Hệ số 5 lớp đường
    ├── zone_factors.json    # Tốc độ theo vùng (lưới 8x8)
    └── dayofweek.jsonl      # Đo kiểm chứng 7 ngày
```

## Nguyên lý mô hình

1. **5 loại đường**: cao tốc / quốc lộ / đường chính / đường nội đô / đường nhỏ
   (tốc độ đêm thực đo: 100 / 58 / 31 / 24 / 10 km/h)
2. **Theo vùng**: Hà Nội chia lưới 8x8, mỗi ô có tốc độ riêng theo loại đường
   và khung giờ (vì phố cổ 15 km/h khác hẳn hành lang sân bay 33 km/h).
3. **Theo thời gian**: 23 khung giờ (cao điểm chia 30 phút), nội suy mượt.
4. **Theo ngày**: Thứ 2-6 giống nhau, chiều Thứ 6 kẹt hơn 20%,
   sáng Thứ 7 bằng 60%, sáng Chủ nhật gần như thoáng.
5. **Nội suy 500m**: điền giá trị thiếu từ điểm đo lân cận cùng loại đường;
   vượt 500m thì dùng trung bình lịch sử của chính con đường đó,
   rồi đến trung bình vùng, rồi trung bình thành phố.
6. **Thiết kế lặp**: dữ liệu gốc lưu riêng, chạy lại nội suy khi có dữ liệu mới.

## Dữ liệu

- Nguồn: TomTom Routing API (historic traffic), gói miễn phí.
- Đã dùng ~1600/20000 lượt gọi (tháng 10/2026).
- Dữ liệu trong `data/` là kết quả đo thực tế, dùng được ngay không cần crawl lại.
