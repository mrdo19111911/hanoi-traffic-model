# Spike: ma trận thời gian theo khung giờ bằng `osrm-customize` + `/table`

Đo xem khuyến nghị trong `reports/spectral_spike/README.md` có chạy nổi trên máy này không. Khuyến nghị đó là: mỗi khung giờ xuất một file tốc độ theo đoạn, chạy `osrm-customize`, rồi gọi `/table` và lưu ma trận vào DB.

Đây là thử nghiệm dùng một lần, không phải code production. Spike không sửa file nào ngoài thư mục này, không gọi TomTom, không gọi mạng ngoài 127.0.0.1, và không đụng vào `osrm/data` (file `.geometry` của bản đang chạy vẫn mang ngày 19/10).

- Script: `customize_spike.py`. Chạy từ gốc repo: `.venv/Scripts/python.exe reports/osrm_customize_spike/customize_spike.py <thư mục nháp>`. Thư mục nháp phải chứa sẵn bản sao `vietnam-latest.osrm.*`. Một lượt chạy mất khoảng 5 phút.
- Số liệu đầy đủ ở `results.json`, log ở `run.log`. Điểm thử là 2.000 toạ độ cố định, lưu trong `points_2000.json` (seed 20261020).
- Máy đo: Ryzen 9 3900X (12 nhân, 24 luồng), RAM 32 GB (lúc đo còn trống khoảng 17 GB). Ổ E còn trống 63 GB, ổ C 222 GB.

## Cách làm, và chỗ khác với đề bài

1. **OSRM ở cổng 5000 không chạy lúc đo.** Không có tiến trình nào, `manage.py status` báo "not running". Vì vậy spike không bật server chung. Thay vào đó, toàn bộ `vietnam-latest.osrm.*` (4,3 GB) được chép sang thư mục nháp, và một `osrm-routed` riêng chạy ở cổng 5055 với đúng cờ của `manage.py`: `--algorithm mld --threads 4 --max-table-size 2500`. Trước khi customize, bản sao giống hệt từng byte với dữ liệu thật, nên số đo `/table` ở đây thay được cho server thật. (Sau đó người dùng yêu cầu bật app, nên OSRM 5000 và web 8000 đã chạy bằng `app.py start`. Spike không gọi gì vào hai server này.)
2. **Điểm thử:** 2.000 điểm lấy ngẫu nhiên theo chiều dài đường trên `osrm/data/big_roads.geojson`, đã bỏ các đoạn `isolated`, `fragment` và phà. Các điểm trải khắp cả nước. Ma trận 500 và 1000 dùng 500 và 1000 điểm đầu. Mọi lần gọi lấy `annotations=duration,distance`.
3. **File tốc độ:** đề bài bảo dùng `road_classes.sqlite`, nhưng index đó chỉ phủ bbox Hà Nội (105,3–107,1 E, 20,5–21,8 N) và dùng các lớp `caotoc/quoclo/chinh/noido/nho`, không có `tinhlo`. Vì vậy file chính được dựng từ **`big_road_edges.sqlite`** (mạng A toàn quốc, cặp nút OSM `a<b`), còn lớp đường lấy theo `way_id` → `group` trong `big_roads.geojson`. Tốc độ phẳng: caotoc 80, quoclo 50, tinhlo 40 km/h. Mỗi cặp được ghi cả hai chiều, phà bị bỏ. Ngoài ra có một file thứ hai dựng từ `road_classes.sqlite` (Hà Nội, mọi lớp: 80/50/35/25/20 km/h, bỏ `unknown`) để xem file chi tiết làm customize tốn thêm bao nhiêu.

| File tốc độ | Cặp nút | Dòng CSV | Dung lượng | Thời gian dựng (Python) | OSRM áp được |
|---|---|---|---|---|---|
| `speeds_big.csv` (toàn quốc, mạng A) | 1.552.012 | 3.104.024 | 78 MB | 6,2 s | 3.086.448 đoạn (99,4%; phần còn lại là chiều ngược của đường một chiều) |
| `speeds_hanoi.csv` (Hà Nội, mọi lớp) | 5.695.854 | 11.391.708 | 287 MB | 15,0 s | 11.007.156 đoạn |

## 1. `/table` (MLD, 4 luồng, dữ liệu gốc)

| Lời gọi | Lần 1 | Lần 2 | Parse JSON (Python) | Kích thước phản hồi |
|---|---|---|---|---|
| 500 × 500 | 2,21 s | 2,14 s | 0,11 s | 4,5 MB |
| 1000 × 1000 | 5,81 s | 5,99 s | 0,43 s | 17,4 MB |
| **2000 × 2000** | **17,44 s** | **16,95 s** | 1,75 s | 68,4 MB |
| 1 → 2000 (một điểm tới tất cả) | **31,03 s** | | 0,03 s | 0,6 MB |
| 2000 → 1 | 31,01 s | | | |
| 1 → 2000, chỉ lấy duration | 30,90 s | | | |
| 2 → 2000 | 2,72 s | | | |
| 8 → 2000 | 2,76 s | | | |

- Thời gian tăng khoảng N^1,5. Mọi ô đều có giá trị (0 null).
- **Bẫy cần nhớ:** khi chỉ có đúng 1 nguồn (hoặc đúng 1 đích), OSRM MLD chạy theo nhánh one-to-many và chậm hơn khoảng 11 lần. Thêm nguồn thứ hai thì còn 2,7 s. Nếu cần "một điểm tới tất cả", ví dụ khách mới tới mọi gate, hãy gom nhiều điểm vào một lời gọi, hoặc thêm một nguồn giả rồi bỏ hàng đó đi.
- Sau khi customize với tốc độ mới, 2000 × 2000 mất 17,3 s, tức tốc độ không đổi.
- **RAM của `osrm-routed`:** chế độ nạp thường dùng 2,9 GB riêng sau khi nạp, đỉnh 3,6 GB khi chạy bảng 2000. Chế độ `--mmap=1` chỉ dùng 17 MB riêng (đỉnh 0,8 GB), và bảng 2000 × 2000 vẫn mất 17,5 s. Phần còn lại nằm trong file cache của Windows, dùng chung giữa các tiến trình. Thời gian khởi động 0,5–2 s.

## 2. `osrm-customize` (trên bản sao)

| Lần chạy | Thời gian | Đỉnh RAM (private) | File bị ghi lại |
|---|---|---|---|
| không có file tốc độ | 13,5 s | 2,8 GB | `.cell_metrics` 700 MB, `.mldgr` 488 MB, `.datasource_names` |
| `speeds_big.csv`, 24 luồng | **14,9 s** | 2,8 GB | ba file trên, cộng **`.geometry` 929 MB** |
| `speeds_big.csv`, 6 luồng | 22,1 s | 2,5 GB | như trên |
| `speeds_hanoi.csv` (11,4 triệu dòng) | 16,9 s | 3,1 GB | như trên |
| cả hai file | 16,7 s | 3,4 GB | như trên |

Log của lần `speeds_big.csv` cho thấy thời gian chia ra như sau: áp tốc độ 1,5 s, nạp partition 7,4 s, tính lại các cell 4,8 s, ghi file khoảng 2,2 s. Kích thước file tốc độ ảnh hưởng rất ít. Phần lớn thời gian là chi phí cố định của việc nạp và tính lại cả nước. Các cảnh báo "unreachable boundary nodes" xuất hiện cả ở lần không có file tốc độ, nên không do file tốc độ gây ra.

**Tốc độ đi vào đâu:** thời gian trung bình của ma trận 500 × 500 tăng từ 43.798 s lên 50.973 s (+16%) sau khi áp tốc độ phẳng, nên tốc độ đã có hiệu lực. Chạy lại customize không có file thì quay về 43.783 s. Đồ thị định tuyến lấy lại tốc độ gốc, nhưng `.geometry` vẫn giữ giá trị của lần trước. Lệch 15 s có lẽ do phần đoạn lẻ khi gắn toạ độ vào đường đọc từ `.geometry`. Vì vậy mỗi khung giờ nên bắt đầu từ một bản `.geometry` sạch: chép lại 929 MB, mất khoảng 1–4 s.

## 3. Chi phí cho mỗi khung giờ

**Phần dữ liệu đổi theo khung giờ:** `.cell_metrics` + `.mldgr` + `.geometry` = **khoảng 2,1 GB mỗi khung**. Khoảng 2,2 GB còn lại của bộ dữ liệu (tổng 4,3 GB) dùng chung được.

| Cách tổ chức | Ổ đĩa | Thời gian dựng | Ghi chú |
|---|---|---|---|
| Giữ sẵn một bộ OSRM cho mỗi khung, 96 khung/ngày | 96 × 2,1 ≈ **203 GB** (+2,2 GB chung) | như dòng dưới | Không vừa ổ E (63 GB). 7 ngày × 96 = 672 khung ≈ 1,4 TB. Không khả thi. |
| **Dựng tuần tự, chỉ giữ ma trận** | một bộ làm việc 4,3 GB + CSV ≤ 0,3 GB (xoá sau mỗi khung) | mỗi khung ≈ chép `.geometry` 2 s + customize 15–17 s + khởi động routed 2 s + `/table` 2000² 17,5 s + parse 2 s ≈ **38–41 s** | 96 khung ≈ **1 giờ**. 672 khung (7 ngày) ≈ **7–8 giờ**, chạy được một lần qua đêm. |
| Ma trận 2000 × 2000 lưu dạng blob int32 (giây) | **16 MB mỗi khung**. 96 khung ≈ 1,5 GB, 672 khung ≈ 10,8 GB. Thêm distance thì gấp đôi. | | Lưu một dòng cho mỗi cặp trong SQLite (khoảng 30 byte/dòng) thì khoảng 120 MB mỗi khung, 96 khung ≈ 12 GB. Nên dùng blob cho mỗi (ngày, khung). |

**RAM** cho mỗi luồng xử lý (customize, sau đó routed) đạt đỉnh khoảng 3,4 GB. Máy còn trống khoảng 17 GB, nên chạy song song 2–3 luồng xử lý được. Tuy vậy customize đã dùng hết 24 luồng CPU, nên chạy song song chỉ lợi được phần `/table`, vì routed chỉ dùng 4 luồng. Ước lượng thực tế khoảng 40–45 phút cho 96 khung nếu xếp customize của khung sau chạy chồng lên `/table` của khung trước. Không được chạy customize trên `osrm/data` khi server đang dùng: phải luôn làm trên bản sao.

**Với số điểm lớn hơn 2.500** (giới hạn `--max-table-size` hiện tại), cần chia ma trận thành nhiều khối nguồn × đích. Thời gian tăng khoảng N^1,5 đến N². Ví dụ ma trận 1.832 × 1.832 gate từ `full_network_gates.json` mất khoảng 15 s mỗi khung.

## Kết luận

Cách này **làm được trên máy này, với điều kiện không giữ dữ liệu OSRM theo từng khung giờ**. Mỗi khung giờ chỉ tốn khoảng 15–17 giây `osrm-customize`, khoảng 3 GB RAM và khoảng 17 giây cho một `/table` 2000 × 2000. Toàn bộ 96 khung của một ngày dựng xong trong khoảng một giờ, và 672 khung của cả tuần trong một đêm, từ một bộ dữ liệu làm việc 4,3 GB. Thứ không chịu được là ổ đĩa: mỗi khung đổi 2,1 GB file OSRM (`.cell_metrics`, `.mldgr`, `.geometry`), nên giữ 96 bộ sẵn để truy vấn trực tiếp cần khoảng 200 GB. Vì vậy nên dựng tuần tự trên một bản sao: mỗi khung chép lại `.geometry` sạch, customize, `/table`, rồi chỉ lưu ma trận vào DB dạng blob khoảng 16 MB cho mỗi (ngày, khung). Cần nhớ hai điều. Thứ nhất, đừng gọi `/table` với đúng một nguồn hoặc một đích, vì lời gọi đó chậm hơn khoảng 11 lần (31 s so với 2,7 s). Thứ hai, phải tính lại toàn bộ ma trận mỗi khi đổi tập điểm. Cũng có thể giảm số khung: các khung đêm hoặc ngoài giờ cao điểm có tốc độ gần nhau thì gộp lại. Spike này chưa đo phần đó.
