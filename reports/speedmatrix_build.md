# Dựng speedmatrix trên bản đồ Việt Nam thật (Task 9)

Ngày chạy: 22/10/2026. Máy: Windows, Python 3.14 trong `.venv`, `PYTHONIOENCODING=utf-8`. Mọi đầu ra nằm trong `runtime/matrix/` (gitignore). Không động tới OSRM (cổng 5000) hay web app (cổng 8000).

Đo bộ nhớ:
- Mỗi lệnh chạy qua một trình bọc nhỏ (`memrun.py` trong scratchpad, dùng ctypes, không cài thêm gói).
- Trình bọc theo dõi cả cây tiến trình, vì `.venv\Scripts\python.exe` là launcher sinh ra tiến trình con.
- "Peak WS" là peak working set cộng trên cả cây tiến trình; "peak private" là bộ nhớ riêng.
- Không bước nào chạm ngưỡng dừng 32 GB.

## Tóm tắt

| Bước | Lệnh | Thời gian | Peak WS | Peak private | Kết quả |
|---|---|---|---|---|---|
| 1 | `segments --pbf osrm/data/vietnam-latest.osm.pbf` | 367,6 s | 4,48 GB | 5,93 GB | 26.667.076 đoạn, thư mục `segments/816249187dfd` |
| 1 | `import-rules` (7 file, một lệnh) | khoảng 1 s | – | – | 116 rule, 3 nhóm xe, 4 ngày lễ, 69 mục `unresolved` |
| 2 | `rules-check --segments runtime/matrix/segments/816249187dfd` | khoảng 13 s | – | – | 88 rule có hiệu lực ngày 22/10/2026, **0 lỗi** (exit 0) |
| 4 | `speeds --segments …` | 89,2 s | 12,18 GB | 15,48 GB | `speeds/dba5f7c3e555` |
| 4 | `groups … --as-of 2026-10-22` | 43,5 s | 3,46 GB | 4,89 GB | `speeds/dba5f7c3e555/groups/2026-10-22` |
| 4 | `groups … --as-of 2026-10-01` | 44,1 s | 3,46 GB | 4,90 GB | `speeds/dba5f7c3e555/groups/2026-10-01` |
| 8 | `rules-check … --as-of 2026-10-22` (sau đợt sửa review cuối) | khoảng 14 s | – | – | 88 rule, **20 lỗi** (đều là orphan `carriageway`), 20 rule "partial", exit 2 |
| 8 | `rules-check … --as-of 2026-10-01` (sau đợt sửa review cuối) | khoảng 14 s | – | – | 84 rule, **19 lỗi**, 19 rule "partial", exit 2 |
| 8 | `groups … --as-of 2026-10-22` (dựng lại) | 50,3 s | 3,46 GB | 4,89 GB | thay view cũ |
| 8 | `groups … --as-of 2026-10-01` (dựng lại) | 48,8 s | 3,46 GB | 4,89 GB | thay view cũ (view trước chưa có sửa vòng 1) |

Đợt sửa sau review cuối (mục 8) không đổi lớp tốc độ: `groups` chấp nhận `speeds/dba5f7c3e555` cho cả hai ngày, vì không ngày nào có rule tốc độ. Lỗi mới của `rules-check` không phải lỗi dữ liệu mới: code nay báo những chiều đường đôi mà trước đây bị bỏ lặng lẽ (mục 2.3 và 5.6).

Peak cao nhất là 12,18 GB working set (15,48 GB private), ở bước `speeds`.

## 1. Các lệnh

```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\python.exe -m speedmatrix.build segments --pbf osrm/data/vietnam-latest.osm.pbf
.venv\Scripts\python.exe -m speedmatrix.build import-rules data/rules/groups.json data/rules/holidays.json data/rules/official/hanoi.json data/rules/official/north.json data/rules/official/central.json data/rules/official/south.json data/rules/official/national.json
.venv\Scripts\python.exe -m speedmatrix.build rules-check --segments runtime/matrix/segments/816249187dfd
.venv\Scripts\python.exe -m speedmatrix.build rules-check --segments runtime/matrix/segments/816249187dfd --as-of 2026-10-01
.venv\Scripts\python.exe -m speedmatrix.build speeds --segments runtime/matrix/segments/816249187dfd
.venv\Scripts\python.exe -m speedmatrix.build groups --segments runtime/matrix/segments/816249187dfd --speeds runtime/matrix/speeds/dba5f7c3e555 --as-of 2026-10-22
.venv\Scripts\python.exe -m speedmatrix.build groups --segments runtime/matrix/segments/816249187dfd --speeds runtime/matrix/speeds/dba5f7c3e555 --as-of 2026-10-01
```

## 2. Số liệu

### 2.1 Bước 1: bảng đoạn đường

Đầu vào: `osrm/data/vietnam-latest.osm.pbf`, 330,9 MB, sha256 `d408722f…`.

Đầu ra: `runtime/matrix/segments/816249187dfd/`, tổng khoảng 1,29 GB:

| File | Kích thước |
|---|---|
| segments.npz | 1.186,3 MB |
| admin.json | 84,7 MB |
| water.npz | 12,0 MB |
| strings.json | 2,4 MB |
| meta.json | 305 B |

Số đoạn theo lớp đường:

| caotoc | quoclo | chinh | noido | nho | pha | Tổng |
|---|---|---|---|---|---|---|
| 58.759 | 772.301 | 2.830.420 | 19.137.603 | 3.863.309 | 4.684 | 26.667.076 |

Thẻ OSM không đọc được: `maxheight` 42 giá trị, `maxweight` 0, `maxspeed` 0.

### 2.2 Nhập seed

Lệnh `import-rules` nhập 7 file trong một lần:

| File | Rule | Mục `unresolved` |
|---|---|---|
| groups.json | 0 | 0 (3 nhóm xe) |
| holidays.json | 0 | 0 (4 ngày lễ) |
| hanoi.json | 11 | 13 |
| north.json | 16 | 14 |
| central.json | 32 | 13 |
| south.json | 50 | 17 |
| national.json | 7 | 12 |
| **Tổng** | **116** | **69** |

### 2.3 Bước 2: các vòng `rules-check`

Số rule `cao`/`tb` còn lỗi (orphan hoặc lỗi vùng) qua các vòng sửa seed:

43 → 40 → 19 → 16 → 6 → **0**

- Ngày 22/10/2026: 88 rule có hiệu lực, 0 lỗi, exit 0.
- Ngày 1/10/2026: 84 rule có hiệu lực, 0 lỗi, exit 0.
- Ngày 1/3/2026 (kiểm tra thêm, không bắt buộc): 66 rule có hiệu lực, 2 lỗi.
  - Hai rule lỗi là `hn_vd3_pilot_ge10t` và `hn_vd3_pilot_tractor`, đã hết hạn 31/3/2026.
  - Cả hai có đoạn "Đại lộ Thăng Long từ Đường 70" mà OSM không có nút chung, nên chưa sửa. Xem mục 6.

**Sau đợt sửa review cuối (mục 8)**, `rules-check` kiểm chặt hơn:
- Lỗi vùng hay lỗi rule của **mọi** rule đang bật làm lệnh lỗi, bất kể độ tin cậy, vì `groups` và `speeds` cũng dừng ở các lỗi này.
- Rule đang bật mà có orphan được đánh dấu `PARTIAL`: `groups` vẫn áp phần định vị được. Rule `cao`/`tb` có orphan vẫn là lỗi như trước.
- Rule đang tắt nhưng có hiệu lực được kiểm luôn; lỗi và orphan của chúng in thành cảnh báo `WARN` (không làm lệnh lỗi), để người dùng thấy trước khi bật.
- Orphan loại mới `"missing": "carriageway"`: một đoạn tuyến (`from`/`to`) bỏ sót một chiều của đường đôi, tức phần đường cùng tên chạy song song trong vòng 60 m mà theo đường đi thì cách phần đã lấy hơn 300 m hoặc không nối với nó.

Kết quả:

| Ngày | Rule bật | Lỗi | Partial | Rule tắt có cảnh báo | Exit |
|---|---|---|---|---|---|
| 22/10/2026 | 88 | 20 | 20 | 8 / 25 | 2 |
| 1/10/2026 | 84 | 19 | 19 | 8 / 25 | 2 |

- Cả 20 lỗi (19 ngày 1/10; khác nhau ở `hn_tu_lien_nghi_tam_yen_phu`, chỉ có hiệu lực ngày 22/10) đều là orphan `carriageway`. Không có lỗi vùng hay lỗi rule nào ở rule đang bật.
- Phần định vị được của các rule này không đổi so với trước; view nhóm xe vẫn áp chúng như cũ. Chỉ khác là phần bị sót nay được báo ra. Danh sách và độ dài phần sót ở mục 5.6.
- 8 rule `thap` đang tắt có cảnh báo: `hcm_du_thao_2026_den_1t` và `hcm_du_thao_2026_tren_1t` (vùng rò ra mép bbox phía nam và đông; nếu bật thì `groups` sẽ dừng), `gl_quynhon_thd_hv_35t`, `hcm_nguyen_van_linh_hon_hop`, `hcm_tay_lan_binh_chanh`, `hue_ql49_somi_ngay`, và hai rule Sa Pa `lc_sa_pa_31_tuyen_tren_10t_*` (6 tên không có trong OSM).

### 2.4 Bước 4: lớp tốc độ và view nhóm xe

**Lớp tốc độ `speeds/dba5f7c3e555`** (dựng với các rule có hiệu lực ngày 22/10/2026):
- File: layer.npz 2,7 MB, layer.json và report.json.
- Nguồn tốc độ: `profile_free_flow:v1`.
- Không có rule tốc độ nào bị orphan.
- Số đoạn bị kẹp tốc độ (`clamped`): quoclo 7.344, chinh 31.104, noido 12.096, nho 9.360.
- Không có đoạn động theo ngày (`dynamic_segments` = 0).

**Kích thước view nhóm xe:**

| Ngày | container.npz | tu2t.npz | duoi2t.npz | report.json |
|---|---|---|---|---|
| 2026-10-22 | 4,05 MB | 3,46 MB | 1,65 MB | 22,6 KB |
| 2026-10-01 | 4,05 MB | 3,46 MB | 1,65 MB | 21,8 KB |

Cả thư mục `speeds/dba5f7c3e555` khoảng 20 MB.

**Số đoạn bị ảnh hưởng theo nhóm** (cả hai ngày dựng lại ở đợt sửa review cuối, mục 8; trong ngoặc là ngày 1/10/2026). Từ đợt này, "bị ảnh hưởng" của nhóm gốc `duoi2t` vẫn tính so với lớp tốc độ trần, còn của `tu2t` và `container` tính so với **view của nhóm gốc**: các đoạn có tốc độ hoặc trạng thái cấm khác `duoi2t` ở ít nhất một khung giờ. Đó là các đoạn mà dự án con 2 phải dựng mảnh riêng; mọi đoạn khác dùng lại mảnh của nhóm gốc.

| Nhóm | Gốc | Bị ảnh hưởng | So với | Bị cấm | Bị giới hạn tốc độ | Rule áp dụng | Giới hạn tốc độ (km/h) theo lớp | Rule giới hạn |
|---|---|---|---|---|---|---|---|---|
| duoi2t | có | 461.551 (461.533) | lớp tốc độ | 456.253 (456.235) | 7.395 | 19 (17) | caotoc 100, quoclo 90, chinh 80, noido 50, nho 50 | vn_cap_tai_den_3_5t |
| tu2t | không | 1.042.566 (1.042.716) | duoi2t | 562.734 (562.716) | 461.750 | 53 (51) | caotoc 90, quoclo 80, chinh 70, noido 50, nho 50 | vn_cap_tai_tren_3_5t |
| container | không | 1.229.456 (1.229.312) | duoi2t | 569.269 (568.956) | 653.448 | 69 (66) | caotoc 90, quoclo 70, chinh 60, noido 50, nho 50 | vn_cap_dau_keo_so_mi_ro_mooc |

Thay đổi so với view trước (ngày 22/10; số bị cấm và bị giới hạn không đổi):
- `tu2t`: 1.021.053 → 1.042.566. Thêm 29.558 đoạn bị cấm với `duoi2t` nhưng không với `tu2t` (chủ yếu Hải Phòng KV1/KV2, xem mục 5.3); bớt 8.045 đoạn mà `tu2t` thấy giống hệt `duoi2t` ở mọi khung giờ (cùng giờ cấm, cùng tốc độ khi mở).
- `container`: 1.208.284 → 1.229.456 (thêm 28.998, bớt 7.826), cùng lý do.
- `duoi2t`: không đổi.
- View ngày 1/10 nay đã có sửa vòng 1 (thêm 367 đoạn Phạm Hùng / Khuất Duy Tiến bị cấm so với view cũ của ngày này).

Hai ngày khác nhau ở các rule sau:
- Chỉ có hiệu lực ngày 22/10: `hn_cau_thang_long_tram_can_dot2`, `hn_cau_xay_dt422`, `hn_tu_lien_nghi_tam_yen_phu`, `hp_dt390_4truc_gio_cao_diem`.
- Chỉ có hiệu lực ngày 1/10: `hn_cau_thang_long_tram_can_dot1`.

**Mười rule cấm lớn nhất** (số đoạn trong phạm vi rule, ngày 22/10/2026). Có 74 rule cấm trong tổng số.

| Rule | Số đoạn | Nhóm bị cấm |
|---|---|---|
| hcm_qd23_dau_keo_so_mi | 239.977 | container |
| hcm_qd23_xe_tai_nang | 239.977 | container, tu2t |
| hcm_qd23_xe_tai_nhe | 239.977 | duoi2t |
| hn_zone_ge2t_day | 184.833 | container, tu2t |
| hn_zone_lt2t_peak | 184.833 | duoi2t |
| ht_phuong_tren35t_ngay | 66.695 | container, tu2t |
| hue_trungtam_35t_caodiem | 41.346 | container, tu2t |
| hp_kv1_tai_nho_ban_ngay | 24.419 | duoi2t |
| dng_trungtam_tren25t | 12.290 | container, tu2t |
| hp_kv2_tai_nho_ban_ngay | 5.288 | duoi2t |

**Báo cáo OSM: `hgv:conditional` không đọc được**
- Số giá trị không đọc được: 381 cho tu2t và container, 128 cho duoi2t.
- Ví dụ (trong ngoặc là số lần xuất hiện trong 20 ví dụ đầu):
  - `no @ (weight>1)` (7), `no @ weight>1` (3);
  - `2.5 @ (06:00-22:00); <=2.5 @ (06:00-09:00,16:00-20:00)` (4 với tu2t, 14 với duoi2t);
  - `no @ (06:00-22:00 AND weight>5)` (2);
  - `no @ (6:00-7:30; 16:30-17:30)`, `no @ (weight>1.5)`, `no @ (6:00-22:00)`;
  - `5 @ (06:00-22:00); <=5 @ (06:00-09:00,16:00-20:00)`.
- Các giá trị này rơi vào hai dạng:
  - Điều kiện trọng lượng viết trong ngoặc của `hgv:conditional`.
  - Giá trị số đặt trước `@`, tức là cú pháp `maxweight:conditional` bị gắn nhầm vào khóa `hgv:conditional`.
- Có giá trị gõ sai, ví dụ `22:000;`.

### 2.5 Bước 3: kiểm tra mẫu trên view `tu2t`

Kiểm tra trên view ngày 22/10/2026, ngày thường (`weekday`). Slot 16 là 08:00, slot 20 là 10:00, slot 44 là 22:00. Mỗi ô ghi số đoạn bị cấm trên tổng số đoạn.

| Kiểm tra | Mong đợi | Kết quả | Đạt |
|---|---|---|---|
| Phố Huế, slot 16 | cấm | 61/61 | có |
| Phố Huế, slot 44 | mở | 0/61 | có |
| Vành đai 3 (cao tốc trên cao, ref CT.37, trong bbox Hà Nội), slot 16 | mở | 0/805 | có |
| QL1A (ref QL.1) phía nam nút Ngọc Hồi – Phan Trọng Tuệ (vĩ độ 20,85–20,94), slot 16 | mở | 0/244 | có |
| Nguyễn Huệ (TP.HCM), slot 16 | cấm | 52/52 | có |
| Trần Xuân Soạn, Huỳnh Tấn Phát → Lê Văn Lương, slot 20 | mở | 0/71 | có |
| Trần Xuân Soạn, cùng đoạn, slot 16 | cấm | 71/71 | có |
| Mai Chí Thọ, phần là ranh giới (Đồng Văn Cống → Võ Nguyên Giáp), slot 16 | mở | 219/255 mở, 36 cấm | gần đạt, xem dưới |
| Mai Chí Thọ, toàn tuyến, slot 16 | mở (theo brief) | 399/647 cấm | không, xem dưới |

Kiểm tra cả hai chiều trên một đường đôi:
- **Nguyễn Văn Linh (Quận 7)**: đây là đường đôi thật. Ba đoạn ranh giới đều phủ cả hai chiều đường chính và đều mở ở slot 16 (0 đoạn bị cấm).
  - Cầu Phú Mỹ → cầu Thầy Tiêu: chiều đông 474 m, chiều tây 1.012 m (lệch do nhánh nút giao).
  - Cầu Thầy Tiêu → cầu Tư Dinh: 3.915 m và 3.799 m.
  - Cầu Tư Dinh → cầu Ông Lớn: 1.256 m và 1.272 m.
  - Như vậy rủi ro "gieo nguồn 60 m" không làm sót chiều đường nào ở đây.
- **Trần Xuân Soạn**: OSM chỉ có một chiều một-chiều (9 đoạn, hướng ≥180°); phần còn lại là đường hai chiều. Đoạn rule bắt đủ mọi way của đường.

Ghi chú:
- **Mai Chí Thọ**: brief cho rằng cả tuyến là đường bao nên mở ở slot 16. Trong seed thì khác:
  - Vòng ranh TP.HCM chỉ dùng Mai Chí Thọ từ Võ Nguyên Giáp tới Đồng Văn Cống.
  - Vòng ranh theo Điều 4 (Xa lộ Hà Nội → Mai Chí Thọ → Đồng Văn Cống) để đoạn phía tây (hầm sông Sài Gòn → Đồng Văn Cống) nằm trong vùng; seed cũng ghi đoạn này trong các rule `hcm_qd23_*_tuyen_bo_sung` (cấm 6h–22h).
  - Vì vậy 399/647 đoạn bị cấm là đúng theo seed và vòng ranh Điều 4. Đây là mâu thuẫn giữa brief và seed, vẫn là câu hỏi mở cho người dùng.
  - Vùng TP.HCM không chứa đoạn nào của phần ranh giới Mai Chí Thọ (0 đoạn chung).
  - 36 đoạn bị cấm trên phần ranh giới (795 m, 8 way) đến từ đoạn "Cầu Cá Trê Nhỏ → Đồng Văn Cống" của các rule `*_tuyen_bo_sung`. Tất cả nằm trong vòng 81 m (trung vị 42 m) quanh 4 nút giao Đồng Văn Cống × Mai Chí Thọ: đó là chính nút giao cộng phần gieo nguồn 60 m quanh nút giao (`CROSSING_SEED_M`), không phải dung sai 1,1·D + 200 m. Hai đoạn gặp nhau ở một nút giao đều sở hữu các đoạn của nút giao trong khoảng 60–80 m; đây là độ chính xác đã biết của `route_piece`.
- **Nguyễn Văn Linh**: đoạn từ nút cầu Phú Mỹ chạy lên phía bắc tới Huỳnh Tấn Phát (khoảng 6 km cộng cả hai chiều và nhánh) không thuộc vòng ranh, nên nằm trong vùng và bị cấm 6h–22h. Điều này đúng theo hình học vòng ranh, nhưng nên được xác nhận.
- **Đường mặt đất dưới Vành đai 3**: số liệu dòng này đo trước sửa vòng 1, khi Phạm Hùng và Khuất Duy Tiến còn được miễn cả tuyến; sau sửa xem mục 7. Phạm Văn Đồng có 7/404 đoạn bị cấm. Nguyễn Xiển bị cấm 90/90 vì không có trong danh sách ngoại lệ của seed. Brief chỉ yêu cầu Vành đai 3 trên cao mở, và điều đó đạt.

## 3. Các thay đổi trong seed (Bước 2)

Nguyên tắc: chỉ sửa tên, ref, from/to và mục ranh giới vùng; không đổi ngưỡng, giờ, điều kiện xe hay nguồn. Mỗi rule bị sửa có thêm câu "Task 9 (đối chiếu OSM 2026-10-22)" (hoặc "Tên OSM (Task 9)" ở south.json) trong `note`. Không rule nào phải hạ xuống `thap`: mọi rule đều còn định vị được ít nhất một phần.

Các cách sửa dùng lặp lại:
- **Sửa chính tả theo OSM**: tên trong OSM khác văn bản (dấu, chữ cái).
- **Tách tại cầu hoặc nút**: đường bị một cây cầu, hầm hay nút giao mang tên riêng cắt thành nhiều thành phần, nên `route_piece` báo `path`. Đoạn được tách thành các đoạn nhỏ nối qua way đó.
- **Bỏ đầu mút**: đầu mút là một mốc (đảo, ngã 5, chợ, khách sạn) không có tên trong OSM, hoặc đường OSM dừng cách đường giao vài chục đến vài trăm mét mà không chung nút. Khi đó đoạn chạy tới đầu mút của đường trong OSM. Đã kiểm tra bằng điểm neo rằng đoạn còn lại đi đúng hướng; với hai đường đôi (V.I Lênin và Lê Mao ở Vinh) bỏ đầu mút sẽ chạy sai hướng nên chỉ giữ phần chắc chắn. Bỏ đầu mút có thể làm đoạn cấm dài hơn văn bản.
- **Dùng đường giao kề bên**: dùng đường giao nằm ở đúng chỗ mốc trong OSM.
- **Bỏ hẳn một đoạn**: tên không có trong OSM trong bbox, hoặc đường cùng tên nằm sai chỗ.
- **Khép vùng**: thêm cầu vượt, cầu hay đường nối ngắn vào ranh giới để khép chỗ hở; bỏ mục ranh giới không giáp vùng.

### 3.1 Hà Nội (hanoi.json)

**hn_zone_lt2t_peak, hn_zone_ge2t_day** (cùng một scope):

Ranh giới:
- **Đường 70**: không có tên trong OSM. Thay bằng `{"ref":"ĐT.70","from":"Đường Phương Canh","to":"Ngõ 6 Phố Miêu Nha"}`, cùng "Miêu Nha" và "Cầu vượt Tây Mỗ". Cần dùng đoạn vì ĐT.70 còn chạy qua Hà Đông, bên trong vùng.
- **Đại lộ Thăng Long**: bỏ. Trong OSM các đường gom mang tên này chạy 105,70–105,79, tức sâu 4 km vào trong vùng; còn Hoàng Tùng (VĐ3,5) dừng cách ĐLTL 350 m. Thay bằng chuỗi Cầu vượt Cầu Cốc – Đại Việt – Cầu vượt đường sắt Đại Việt 2 – Thiên Phúc, nối Đường 70 với Hoàng Tùng.
  - Hệ quả: một dải khoảng 0,5 km² giữa ĐLTL và chuỗi này nằm ngoài vùng. "Đại Việt" có hai nhánh ngắn bên trong vùng.
- **Lê Trọng Tấn**: đổi thành "Dương Nội" cộng `{"name":"Lê Trọng Tấn","from":"Vòng xuyến La Khê","to":"Quang Trung"}`.
  - Trong OSM đoạn VĐ3,5 từ Hoàng Tùng tới La Khê mang tên Dương Nội.
  - Lê Trọng Tấn ở Thanh Xuân nằm trong vùng, nên không được làm ranh giới.
- **Vành đai 3**: không có tên này trong OSM; đó là cao tốc ref CT.37. Thay bằng `{"ref":"CT.37","from":"Đi Cao tốc Pháp Vân - Cầu Giẽ","to":"Đường cao tốc Hà Nội - Bắc Giang"}`.
  - Đoạn này gồm cầu Thanh Trì, nên bỏ mục "Cầu Thanh Trì".
- **Khép chỗ hở giữa Trường Sa, Hoàng Sa và Võ Văn Kiệt** (phía bắc sông Hồng; bản trước ghi nhầm là QL5): thêm "Cầu vượt Vĩnh Ngọc" (hở 240 m) và "Cầu vượt Đường sắt Bắc Hồng - Văn Điển".
- Kết quả: vùng 229,0 km², 184.833 đoạn sau khi trừ ngoại lệ (184.466 trước sửa vòng 1).

Ngoại lệ:
- **Nguyễn Văn Cừ**: `from` đổi từ "Nguyễn Văn Linh" thành "Vòng xoay Cầu vượt Nút giao Long Biên", vì hai đường không chung nút. Văn bản ghi "từ cầu vượt nút giao Trung tâm quận Long Biên".
- **Ngô Gia Khảm**: đổi thành `{"from":"Nguyễn Văn Cừ"}`, vì cả tên còn khớp một Ngô Gia Khảm khác ở Hà Đông.
- **Bắc Thăng Long – Vực Dê**: dùng tên OSM, gồm "Bắc Thăng Long - Hải Bối", "Cầu Phương Trạch" và `{"name":"Đường 6","from":"Cầu Phương Trạch","to":"Phương Trạch"}`.
- **Phương Trạch**: `from` đổi thành "Đường 6".
- **Tam Trinh**: đoạn "Chợ đầu mối Đền Lừ → Vành đai 3" đổi thành "Tân Mai → Đỗ Mười". Chợ là mốc; Đỗ Mười là đường mặt đất dưới VĐ3.
- **Đại lộ Thăng Long (Phạm Hùng → Đường 70)**: đổi thành toàn tên "Đại lộ Thăng Long" và "Đại lộ Thăng Long (Láng - Hoà Lạc)". Không có nút chung với ĐT.70, và phần phía tây Đường 70 nằm ngoài vùng.
- **Vành đai 3 trên cao**: đổi thành `{"ref":"CT.37"}`.
- **Phạm Hùng, Khuất Duy Tiến** (sửa vòng 1): Điều 4.2.c chỉ miễn "đoạn tiếp đất từ Vành đai 3 trên cao đi Đại lộ Thăng Long và ngược lại". Bản đầu của Task 9 miễn cả hai tuyến (426 đoạn, khoảng 11,6 km), rộng hơn văn bản. Nay giới hạn mỗi đoạn từ điểm tiếp đất của nhánh lên xuống CT.37 tới Đại lộ Thăng Long:
  - Khuất Duy Tiến: `{"from":"Lối xuống Trần Duy Hưng","to":"Đại lộ Thăng Long"}`; nhánh có tên này chạm Khuất Duy Tiến tại điểm tiếp đất (21,0029; 105,7943). 22 đoạn, 835 m cộng hai chiều.
  - Phạm Hùng: nhánh tiếp đất (21,0076; 105,7905) không có tên trong OSM, nên dùng `{"from":"Tú Mỡ","to":"Đại lộ Thăng Long"}`; phố Tú Mỡ giao Phạm Hùng cách điểm tiếp đất 20 m. 37 đoạn, 868 m cộng hai chiều.
  - Các điểm tiếp đất được tìm bằng cách đi theo các way `motorway_link` từ CT.37 xuống mặt đất.
- Thanh Lâm và CN3–CN5: giữ nguyên, đều định vị được.

**hn_tu_lien_nghi_tam_yen_phu**: Yên Phụ đổi `from` "Khách sạn Thắng Lợi" (mốc) thành "Yên Hoa". Đó là đường giao ở đầu bắc phố Yên Phụ, gần khách sạn nhất. OSM tách "Phố Yên Phụ" (phố nhỏ) khỏi "Đường Yên Phụ", nên đoạn chỉ phủ phố Yên Phụ nhỏ, đúng như ghi chú của rule.

### 3.2 Miền Nam (south.json)

**Vùng nội đô TP.HCM** (bốn rule hcm_qd23_*): vòng ranh được viết lại theo OSM, gồm:
- Xa lộ Hà Nội;
- Võ Nguyên Giáp, chia thành các đoạn quanh cầu Rạch Chiếc;
- Mai Chí Thọ (Võ Nguyên Giáp → Đồng Văn Cống) và Đồng Văn Cống;
- cầu Giồng Ông Tố 2;
- Nguyễn Thị Định (Đồng Văn Cống → cầu Mỹ Thủy 2), cầu Mỹ Thủy 2 và cầu vượt Mỹ Thủy;
- cầu Kỳ Hà 1, 2 và 4, cùng Võ Chí Công;
- cầu Phú Mỹ;
- Nguyễn Văn Linh, chia thành bốn đoạn qua cầu Thầy Tiêu, Tư Dinh và Ông Lớn, tới Nút giao Bình Thuận;
- Lê Khả Phiêu, Lê Đức Anh và Đỗ Mười;
- "QL1" (ref).

Cách tìm và kết quả:
- Các chỗ hở được tìm bằng đường rò từ điểm seed ra mép bbox (cầu Tư Dinh, Giồng Ông Tố, Kỳ Hà).
- Kết quả: vùng 272,7 km².
- Các mục để nguyên cả tên (Võ Chí Công, QL1) có phần chạy ra ngoài vòng. Phần đó chỉ bị loại khỏi vùng, không bị cấm thêm.

Các hành lang và tuyến khác:

| Rule | Sửa | Lý do |
|---|---|---|
| Tuyến bổ sung (4 rule) | Mai Chí Thọ (hầm sông Sài Gòn → Đồng Văn Cống) tách qua cầu Kênh 1, Kênh 2, Cá Trê Lớn, Cá Trê Nhỏ | các cầu có tên riêng cắt rời đường |
| hcm_hl_mai_chi_tho | tách qua cầu Kênh 2, Cá Trê Lớn, Cá Trê Nhỏ | như trên |
| hcm_hl_cang_lotus | Nguyễn Thị Thập tách qua cầu Đa Khoa | như trên |
| hcm_hl_cang_nha_rong_ra | Tôn Thất Thuyết dừng ở "Nguyễn Tất Thành" | không chung nút với cầu Tân Thuận 2 |
| hcm_hl_cang_phu_dinh | Võ Văn Kiệt tách qua cầu Nước Lên | cầu cắt rời đường |
| hcm_hl_cang_phuc_long | Nguyễn Văn Bá bắt đầu từ "Đường Số 1" (ngã tư Tây Hòa) | chạy song song Võ Nguyên Giáp, không chung nút |
| hcm_hl_dang_kiem_5003s | Phạm Văn Đồng tách qua cầu Gò Dưa và "Ngã Tư Bình Triệu" | cầu và nút là way riêng |
| hcm_hl_pham_the_hien_ql50 | Phạm Thế Hiển tách qua cầu Bà Tàng | cầu cắt rời đường |
| bd_6_tuyen_dau_keo, bd_6_tuyen_tren_10t | QL13 thành `{"ref":"QL13","from":"Phạm Ngọc Thạch","to":"Đường Quốc lộ 13 cũ"}`; Nguyễn Thị Minh Khai `{"from":"QL13"}`; Huỳnh Văn Cù `{"from":"Ngã tư Chợ Cây Dừa"}`; bỏ đoạn ĐT743 và đoạn ĐT747 (Miếu Ông Cù) | tên và mốc theo OSM; ĐT743 và Miếu Ông Cù không định vị được |
| dni_dong_khoi (2 rule) | Đồng Khởi tách tại "Đường Trương Định", "Hầm chui Tân Phong", "Nguyễn Văn Tiên" | hầm chui là way tên riêng |
| ct_5_truc_noi_o_tren_3_5t | Hòa Bình để cả tên; Trần Hưng Đạo tới cầu Nhị Kiều (thêm cầu); Hùng Vương từ cầu Nhị Kiều tới "Vòng Xoay Hùng Vương" (thêm vòng xoay); Nguyễn Văn Cừ tách tại "Cầu Rạch Ngổng 2", nối lại từ Mậu Thân; CMT8 dừng ở "Vòng Xoay Hùng Vương"; Mậu Thân tách qua cầu Rạch Ngỗng 1 | OSM tách nhiều way không nối, cầu và vòng xoay có tên riêng |
| cm_bac_lieu_noi_o_tu_3_5t | Cách Mạng tách tại Tôn Đức Thắng | nút chia đường thành hai way không nối |

### 3.3 Miền Trung (central.json)

**Vinh** (na_vinh_pl1_18t, na_vinh_pl1_247, na_vinh_pl2_4t; 44 đoạn lỗi):

| Loại sửa | Đoạn |
|---|---|
| Sửa chính tả | Nguyễn Cảnh Hoan `to` "Lệ Ninh" → "Lê Ninh"; "Hồ Sỹ Đống" → "Hồ Sỹ Đồng" (dùng cả tên vì OSM đứt hai đoạn); "Đề Thám" → "Dế Thám" (đoạn 113 m) |
| Mốc không có trong OSM, bỏ đầu mút (đã kiểm tra đường kết thúc tại mốc) | Nguyễn Phong Sắc (Đảo Hải Quan); Đặng Thái Thân (Ngã 4 Hồ Cá Cửa Nam); Đinh Công Tráng và Hồ Tùng Mậu (Ngã 5 Bưu Điện); Lê Huân (Phạm Nguyễn Du); Nguyễn Gia Thiều (Tôn Thất Tùng); Hồ Xuân Hương (Ngõ 25A); Lục Niên (Đường tàu cũ); Nguyễn Sơn (Đường bao KĐT Vinh Tân); Ngô Gia Tự (Đường 72m) |
| Mốc không có trong OSM, dùng cả tên | Cao Thắng (Chợ Vinh, Ngã 4 Chợ Vinh); Trường Thi (Đảo KS Phương Đông, Đảo Hải Quan); Nguyễn Thị Minh Khai và Lê Hồng Phong (OSM đứt đoạn, không có nút nối) |
| Đường OSM dừng cách đường giao 80–500 m, bỏ đầu mút | Thái Phiên, Tân Tiến, Lê Hoàn, Tân Phúc, Nguyễn Khánh Toàn, Nguyễn Xuân Ôn, Nguyễn Xí, Phan Sỹ Thục, Phan Vân; Hồng Bàng `{"from":"Ngư Hải"}` |
| Dùng đường giao kề bên | Phong Định Cảng `from` "Nguyễn Viết Xuân" (giao tại điểm gần Phan Đăng Lưu nhất, 105 m), chạy tới đầu mút OSM (cách Nguyễn Du 400 m) |
| Tách tại nút lệch | Nguyễn Đức Cảnh tại Lê Hồng Phong; Phùng Chí Kiên tại V.I Lênin; Chu Văn An tại Lý Thường Kiệt; Nguyễn Công Trứ tại Phan Đình Phùng |
| Đường đôi, chỉ giữ phần chắc chắn | V.I Lênin: Nguyễn Công Nghiệm → Nguyễn Sỹ Sách (khoảng 400 m sát Đảo Hải Quan chưa phủ); Lê Mao: Ngư Hải → Trần Phú (khoảng 380 m từ ngã 5 tới Ngư Hải chưa phủ) |
| Bỏ hẳn | Võ Nguyên Hiến (đường cùng tên trong OSM cách Phan Đăng Lưu 1,8 km), Hoàng Nghĩa Lương (đường cùng tên trong OSM cách Trần Phú 1,4 km), Nguyễn Bỉnh Khiêm, Tuệ Tĩnh, Minh Tân, Cao Bá Quát, Nguyễn Huy Oánh, Nguyễn Nghiễm, Tôn Thất Tùng, Trần Cảnh Bình (không có trong OSM) |

Vị trí hai mốc được xác định bằng điểm mà các đường văn bản nêu cùng kết thúc:
- Đảo Hải Quan ở (18,6762; 105,6943): Nguyễn Phong Sắc, V.I Lênin, Lê Hồng Phong và Trường Thi đều kết thúc trong vòng 40 m.
- Ngã 5 Bưu Điện ở (18,6700; 105,6840): bốn đường kết thúc trong vòng 33 m.

**Đà Nẵng:**
- **dng_trungtam_15_25t_gio, dng_trungtam_tren25t**: thêm "Cầu vượt Ngã ba Huế" vào ranh giới, giữa Hoàng Thị Loan và Điện Biên Phủ (OSM hở 560 m qua nút giao). Vùng dựng được 11,2 km².
- **dng_ql1_hoakhanh_105t, dng_ql1_hoakhanh_somi**: "Cổng chính KCN Hòa Khánh" không có trong OSM. Bbox đã cắt ở vĩ độ 16,093 để xấp xỉ mốc này, nên dùng toàn bộ Nguyễn Lương Bằng trong bbox.

**Quảng Ngãi:**
- **qng_10t_6_18**: Phan Bội Châu tách tại Hùng Vương.
- **qng_5t_tonducthang_canngay**: Tôn Đức Thắng tách tại Chu Văn An.
- **qng_5t_6_18**:
  - Trương Định bỏ đầu Hùng Vương (OSM dừng cách 26 m); Quang Trung bỏ đầu Hai Bà Trưng (cách 4 m).
  - Bỏ "Trần Cẩm" và "Phạm Xuân Hòa": không có trong OSM, chỉ có hẻm mang tên Phạm Xuân Hòa.
- **qng_cam_xetai_canngay**:
  - Bỏ "Duy Tân": không có ở Quảng Ngãi trong OSM.
  - Bỏ "Tôn Đức Thắng (Bà Triệu – Quang Trung)": Tôn Đức Thắng trong OSM kết thúc tại Quang Trung; đoạn khoảng 120 m tới Bà Triệu không mang tên này.
- **qng_oto_truongdinh_gio**: `from` "Nguyễn Bỉnh Khiêm" đổi thành "Nguyễn Bình Khiêm", là cách OSM ghi tên.

**Đà Lạt (ld_dalat_15t_caodiem)**: "Đặng Thùy Trâm" đổi thành "Đặng Thuỳ Trâm" (OSM bỏ dấu kiểu cũ). `normalize_name` không gộp hai cách bỏ dấu này.

### 3.4 Miền Bắc (north.json)

**Hải Phòng KV1** (hp_kv1_tai_nho_ban_ngay, hp_kv1_tai_lon_ban_ngay):
- Thêm hai đường nối vào ranh giới:
  - "Liên Phương": nối đầu đông Bùi Viện với Đặng Kinh, OSM hở 440 m.
  - "Cầu Niệm": nối Trường Chinh với Nguyễn Văn Linh, hở 156 m.
- Bỏ "Đình Vũ" và "Nguyễn Trường Tộ". Trong OSM chúng nằm ngoài vòng ranh giới đã khép và không giáp vùng: Đình Vũ ở phía đông nút Chùa Vẽ – Đặng Kinh, Nguyễn Trường Tộ ở phía tây nam cầu Lãm Khê.
- Vùng dựng được 33,9 km².

**Hải Phòng KV2** (hp_kv2_*): tên OSM là "Đại lộ Võ Nguyên Giáp" và "Đại lộ Lê Thanh Nghị". `normalize_name` chỉ bỏ tiền tố "đường "/"phố ", không bỏ "đại lộ ". Sông Sặt (đường nước) khép được phía nam. Vùng 11,1 km².

**ĐT.390 (hp_dt390_4truc_gio_cao_diem)**:
- Cầu Quang Thanh mang ref "ĐT.360;ĐT.390", nên thuộc chính tuyến chứ không phải đường giao. ĐT.390 còn đứt khoảng 100 m ở phía nam nút Ba Hàng (QL5).
- Thay bằng `{"ref":"ĐT.390","to":"Tỉnh lộ 360"}`: từ chỗ đứt gần QL5 tới hết cầu Quang Thanh, dài 22,2 km. Khoảng 360 m từ nút QL5 tới chỗ đứt chưa phủ.

**Quảng Ninh (qn_bao_bien_ha_long_cam_pha_tren_3_5t)**: không có đường nào trong bbox mang tên "Đường bao biển Hạ Long - Cẩm Phả", nên bỏ tên này; chỉ còn Trần Quốc Nghiễn.

**Bắc Giang:**
- **bn_bac_giang_phu_luc1_tren_2_5t_gio_cao_diem**:
  - Bỏ 8 tên không có trong OSM: Võ Thị Sáu, Tiền Giang, Châu Xuyên, Tân Ninh, Đàm Thuận Huy, Phùng Trạm, Nguyễn Duy Năng, Phạm Liêu.
  - Hùng Vương (Quốc lộ 1 → Huyền Quang): QL1 cách Hùng Vương hơn 3 km trong bbox, nên dùng toàn bộ Hùng Vương trong bbox. Phần dư ra (Huyền Quang → Xương Giang) đã bị cấm cả ngày cho cùng loại xe ở phu_luc2.
  - Lê Lợi: đường OSM dừng cách Đồng Cửa 130 m, tại nút Lý Thái Tổ / Cô Bắc, nên `from` đổi thành "Lý Thái Tổ".
- **bn_bac_giang_phu_luc2_tren_2_5t**: Nguyễn Văn Cừ và Hùng Vương dừng sát Xương Giang (2–3 m) mà không chung nút, nên bỏ đầu `to`.

## 4. 69 mục `unresolved` của phần nghiên cứu (vẫn chưa mã hóa)

Các mục này nằm trong khóa `unresolved` của từng file seed. `import-rules` đọc chúng nhưng không biến thành rule. Danh sách giữ nguyên nội dung nghiên cứu, nhóm theo vùng.

### Hà Nội (hanoi.json, 13 mục)

1. **Vùng hạn chế QĐ 01/2026 (Hà Nội)**: Văn bản chấp thuận cho xe hoạt động ngoài khung giờ: xe <2 t trong giờ cao điểm và xe 2–<10 t ngoài 21h–6h do Công an TP chấp thuận; xe ≥10 t, xe chuyên dùng do Sở Xây dựng chấp thuận theo lộ trình cụ thể Lý do chưa mã hóa: Giấy phép theo từng xe, từng lộ trình; không phải quy tắc mạng lưới
2. **Vùng hạn chế QĐ 01/2026 (Hà Nội)**: Ô tô tải chuyên dùng, ô tô chuyên dùng (xi téc xăng dầu/khí, chở xe, chở bùn, chở thủy hải sản sống, trộn/bơm bê tông, trộn vữa, quét đường, hút chất thải, cần cẩu, thang, khoan, kéo xe và tương tự) chỉ chạy 21h–6h bất kể khối lượng; xe tưới cây, bán hàng lưu động, nâng người, kiểm tra cầu cống: ngoài giờ cao điểm; xe chở rác: 19h30–6h; xe sự cố hạ tầng, cứu hộ, hút bùn: 24/24 khi có chấp thuận Lý do chưa mã hóa: Thuộc tính kind không có các loại xe chuyên dùng này; xe chuyên dùng ≥2 t đã bị rule hn_zone_ge2t_day bao, nhưng xe chuyên dùng <2 t thì chưa
3. **Vùng hạn chế QĐ 01/2026 (Hà Nội)**: Đầu kéo, container (sơ mi rơ moóc) không được nêu tên trong QĐ 01/52; TT 53/2024 xếp ô tô đầu kéo riêng, không thuộc 'ô tô tải' Lý do chưa mã hóa: Không rõ đầu kéo bị cấm theo Điều 5 hay không; đã mã hóa theo khối lượng toàn bộ (cấm 6h–21h), cần hỏi Sở Xây dựng
4. **Vùng hạn chế QĐ 01/2026 (Hà Nội)**: Xe tải VAN (và pickup cabin kép) khối lượng toàn bộ ≤3,5 t: NĐ 241/2026 (từ 1/7/2026) cho tổ chức giao thông như ô tô con, trừ khi địa phương có hạn chế riêng Lý do chưa mã hóa: Chưa có văn bản Hà Nội nói van ≤3,5 t có được chạy như xe con trong vùng không; rule hiện vẫn tính van là xe tải
5. **Vùng hạn chế QĐ 01/2026 (Hà Nội)**: Vị trí các tuyến được đi Thanh Lâm, CN3, CN4, CN5 (QĐ 52/2026 bổ sung) Lý do chưa mã hóa: Văn bản không nêu phường hay khu công nghiệp; chưa xác định trên bản đồ
6. **Hồ Tùng Mậu, Đại lộ Thăng Long hướng lên Vành đai 3 trên cao**: Thí điểm cấm xe ≥10 t, container, đầu kéo giờ cao điểm 1/1–31/3/2026: không tìm được tin gia hạn, chính thức hóa hay chấm dứt; cấm chỉ một chiều Lý do chưa mã hóa: Trạng thái sau 31/3/2026 chưa rõ; định dạng không có hướng
7. **Vùng phát thải thấp Vành đai 1 (Hà Nội)**: NQ 57/2025/NQ-HĐND Điều 11: từ 1/7/2026 đến 31/12/2027 cấm xe tải khối lượng toàn bộ trên 3.500 kg dùng nhiên liệu hóa thạch trong 9 phường (Hai Bà Trưng, Cửa Nam, Hoàn Kiếm, Ô Chợ Dừa, Văn Miếu - Quốc Tử Giám, Ba Đình, Giảng Võ, Ngọc Hà, Tây Hồ); nhưng Đề án (QĐ 3273/QĐ-UBND) thu hẹp giai đoạn 1 về khu vực 1, 2 phường Hoàn Kiếm và vẫn cho xe >3,5 t chạy 21h–6h. Từ 2027 (giai đoạn 2, khu vực 1–3 Hoàn Kiếm, Cửa Nam): cấm hoàn toàn xe tải >3,5 t Lý do chưa mã hóa: Hai nguồn mâu thuẫn cho năm 2026; chưa đọc được QĐ 3273 và bản đồ ranh giới; không có thuộc tính nhiên liệu
8. **Cầu Thăng Long**: Từ 22/10 đến 15/12/2026, 22h–5h: rào nửa mặt đường mỗi chiều để sửa khe co giãn, thảm hai đầu cầu; xe đi chậm qua phần còn lại Lý do chưa mã hóa: Không cấm theo loại xe, chỉ giảm làn; không có rule cấm
9. **Cầu Thăng Long**: Từ 16/10 đến 10/11/2026, 5h–22h: mọi ô tô không được rẽ phải từ đường gom phía Bắc (trước cổng B KCN Thăng Long) lên cầu Lý do chưa mã hóa: Cấm lượt rẽ; định dạng không mã hóa lượt rẽ
10. **Hầm chui, cầu vượt, cầu nhỏ ở Hà Nội**: Giới hạn chiều cao, tải trọng cầu theo biển báo Lý do chưa mã hóa: Không có văn bản tập trung; dùng thẻ OSM maxheight, maxweight
11. **Cầu Đuống (QL1B/QL1 cũ, Hà Nội)**: Cầu mới dự kiến thông xe giữa tháng 9/2026, cầu cũ chuẩn bị tháo dỡ Lý do chưa mã hóa: Không tìm được phương án tổ chức giao thông hay hạn chế xe tải cho cầu mới
12. **Toàn TP Hà Nội**: Xe quá tải trọng, quá khổ, bánh xích, hàng siêu trường siêu trọng: chạy theo giấy phép lưu hành của Sở Xây dựng (Điều 24 TT 12/2025/TT-BXD) Lý do chưa mã hóa: Giấy phép theo từng chuyến
13. **Toàn TP Hà Nội**: Không tìm thấy quyết định riêng về giờ hoạt động xe chở vật liệu xây dựng, phế thải rời (khoản 4 Điều 44 Luật TTATGTĐB) như các tỉnh khác Lý do chưa mã hóa: Có thể chưa ban hành hoặc nằm trong văn bản khác; xe chở vật liệu là ô tô tải nên đã thuộc QĐ 01/52

### Miền Bắc (north.json, 14 mục)

1. **Hải Phòng (toàn thành phố)**: QĐ 11/2026 Điều 6.1.a: xe ô tô chở hàng 'hạn chế lưu thông trên đường đô thị' trong giờ cao điểm 6h–8h, 11h30–12h30, 16h30–18h30 và ban đêm 22h–6h Lý do chưa mã hóa: Chỉ áp trên các 'tuyến hạn chế' theo biển báo (Điều 3.5); văn bản không liệt kê tuyến. Dựa vào thẻ OSM hgv:conditional.
2. **Hải Phòng (đô thị)**: QĐ 11/2026 Điều 5.2: xe chở vật liệu xây dựng, phế thải rời chỉ được chạy trên đường đô thị 22h–6h Lý do chưa mã hóa: Điều kiện theo loại hàng, không theo thông số xe
3. **Hải Phòng, ĐT.390**: Lệnh cấm xe ≥4 trục 6h–8h, 17h–19h chỉ áp chiều QL5 → cầu Quang Thanh, Km15+100–Km35+280 Lý do chưa mã hóa: Định dạng không có chiều đi và mốc Km; đã mã hóa cả hai chiều trên cả đoạn trong hp_dt390_4truc_gio_cao_diem
4. **Bắc Ninh (đô thị: toàn bộ các phường và trung tâm các xã)**: QĐ 06/2026 Điều 10.2.b: cấm xe ben, xe đầu kéo, xe chở container hoạt động giờ cao điểm 'trên một số tuyến đường' Lý do chưa mã hóa: Đã đọc toàn văn: quyết định không có danh sách tuyến và không định nghĩa giờ cao điểm bằng số; Điều 13 giao Sở Xây dựng khảo sát, cắm biển. Chỉ có thể lấy từ biển báo (OSM).
5. **Bắc Ninh (khu Bắc Giang cũ), đê sông Thương**: QĐ 516/2014 sửa bởi QĐ 38/2021: xe cơ giới trên đê sông Thương có tổng tải trọng không quá 12 t Lý do chưa mã hóa: Không tìm thấy tên 'đê sông Thương' trên đường trong OSM để định vị
6. **Bắc Ninh (khu Bắc Giang cũ)**: QĐ 516/2014: xe tải ≥7,5 t cấm trên đường rộng dưới 7 m; ≥15 t cấm trên đường mặt nhựa/láng nhựa/bê tông (có ngoại lệ) Lý do chưa mã hóa: Điều kiện theo bề rộng và kết cấu mặt đường, không có danh sách tuyến
7. **Bắc Ninh (đô thị)**: QĐ 09/2026: xe chở phế thải rời cấm 6h–8h, 16h30–18h30; giờ xe chở VLXD do UBND cấp xã quyết định Lý do chưa mã hóa: Điều kiện theo loại hàng
8. **Tuyên Quang (đô thị)**: QĐ 19/2026 Điều 2.2: xe chở VLXD, phế thải rời không được chạy trong đô thị 6h–7h30 và 16h30–18h Lý do chưa mã hóa: Điều kiện theo loại hàng
9. **Lào Cai (đô thị)**: QĐ 04/2026: xe chở VLXD ≥3,5 t dưới 3 trục chỉ chạy 8h–11h, 14h–16h, 21h–6h; trên tuyến có biển cấm xe ≥3 trục chỉ chạy 21h–6h Lý do chưa mã hóa: Điều kiện theo loại hàng; tuyến lấy theo biển báo
10. **Lào Cai, cửa khẩu Kim Thành**: Phương án phân luồng số 51 (hiệu lực 21/2/2024): xe chở hàng nhập khẩu làm thủ tục tại bãi 379 hoặc Nghĩa Anh phải đi T1 → D3 → Hương Sơn (D1) Lý do chưa mã hóa: Là lộ trình bắt buộc, không phải lệnh cấm; định dạng không có kiểu 'bắt buộc đi'. Đề xuất tổ chức lại nút cuối cao tốc (9/2026) chưa được duyệt.
11. **Thái Nguyên (gồm Bắc Kạn cũ), 28 cầu hạn chế tải trọng**: Công văn 1426/SXD-QLKCHTGT (26/8/2025), khối lượng toàn bộ: QL.3C Khuôn Lồng Km1+950 17 t, Dốc Đỏ Km20+803 30 t; QL.279 Pi-O Km248+330, Lạng San Km258+502, Khuổi Lụa Km261+657 25-35-45 t, Nà Làng Km264+710 30-45-45 t, Phiêng Giản Km325+995 17 t; QL.3B Hát Deng Km76+310 và Thác Giềng cũ Km129+450 13 t; ĐT.261D Lăng Tạ Km4+480, Xóm Đồng Trong Km7+380 10 t; ĐT.261E Cầu Thủng Km0 30 t; ĐT.262 Na Giang Km7+402 17 t; ĐT.263B Cầu Thông Km0+271 30 t; ĐT.264 Quản Thơ Km16+516, Đá To Km23+870 17 t; ĐT.265 Suối Lũ Km2 17 t; ĐT.269D Khe Mo Km1+110 10 t; ĐT.252B Bản Giang Km0+350 10-15-20 t; ĐT.253 Phúc Lộc II Km20+012, Khuổi Chả Km26+182 13 t (cao 2,3 m); ĐT.256B Quang Phong Km0+070 13 t; ĐT.258B Tin Đồn Km3+070 3,5 t, Khuổi Trà Km44+370 13 t; ĐT.258C Nà Lại Km14+590, Khuổi Lạn Km19+450 10 t; ĐT.259 Nà Vịt Km23+145, Nà Bản Km23+930 13 t. Bộ ba XX-YY-ZZ: xe tải - đầu kéo sơ mi rơ moóc - xe kéo rơ moóc. Lý do chưa mã hóa: Không có tên cầu nào trong OSM và định dạng không nhận mốc Km, nên không lập được bbox chặt. Cần ghép lý trình với dữ liệu cột Km hoặc tọa độ.
12. **Thái Nguyên, 6 vị trí hạn chế chiều cao**: Công văn 1426/SXD-QLKCHTGT: QL.3 Km54+100 (4,75 m, dưới cầu vượt CT07), QL.17 Km133+100 (4,75 m), ĐT.261 Km45+270 (4,75 m), ĐT.274 Km10+300 (4,5 m), ĐT.269E Km10+950, ĐT.256 Km0+950 (cầu vòm đường sắt) Lý do chưa mã hóa: Hạn chế chiều cao theo mốc Km; nên lấy từ thẻ maxheight của OSM
13. **Ninh Bình (đô thị)**: QĐ 124/2026: xe chở VLXD, phế thải >3,5–10 t (khối lượng toàn bộ thiết kế) cấm 6h–8h, 16h–18h; >10 t chỉ chạy 22h–6h Lý do chưa mã hóa: Điều kiện theo loại hàng
14. **Hưng Yên (gồm Thái Bình cũ)**: QĐ 46/2026: xe từ 1,5 t không được lưu thông trên các tuyến, đoạn đô thị có biển cấm, hạn chế trọng tải Lý do chưa mã hóa: Chỉ dẫn chiếu biển báo, không có tuyến hay khung giờ chung

### Miền Trung (central.json, 13 mục)

1. **Nghệ An (Vinh cũ)**: Xe tải từ 4 tấn chuyên chở không giao nhận hàng tại Vinh cấm vào thành phố 5h-24h, phải đi QL1A tuyến tránh Vinh (QĐ 70/2016 Điều 3 khoản 4) Lý do chưa mã hóa: Điều kiện theo mục đích chuyến đi (xe đi qua) và phạm vi 'thành phố Vinh' cũ không mã hóa được trong vehicle/scope
2. **Nghệ An (Vinh cũ)**: Cấm mọi xe tải trên đường mương thoát nước số 1 (Nguyễn Thái Học - Hồ Thành) và mương thoát nước số 3 Lý do chưa mã hóa: Đường không có tên định vị được
3. **Hà Tĩnh (các phường)**: Xe chở vật liệu xây dựng, phế thải rời, xe vệ sinh môi trường chỉ hoạt động 20h-6h (QĐ 53/2025 Điều 12 khoản 5) Lý do chưa mã hóa: Không mã hóa được loại hàng
4. **Thanh Hóa**: QĐ 136/2025/QĐ-UBND (hiệu lực 15/11/2025) quy định giờ hoạt động xe vệ sinh môi trường và xe chở vật liệu xây dựng, phế thải rời trong đô thị; không tìm thấy quy định giờ hay vùng cấm cho xe tải nói chung Lý do chưa mã hóa: Chỉ áp cho loại hàng; chưa đọc được khung giờ (vbpl.vn trả về trang chặn); nguồn chỉ thấy qua kết quả tìm kiếm, chưa mở được
5. **Quảng Trị (Đồng Hới)**: Xe tải chở trên 10 tấn không vào trung tâm Đồng Hới, đi tuyến tránh 24/24, từ 15/8/2019 Lý do chưa mã hóa: 'Trung tâm' không có ranh giới đường; tin từ 2019, chưa thấy xác nhận còn áp dụng. Các tuyến cấm theo giờ (Hai Bà Trưng, Thanh Niên, Lê Trực, Mẹ Suốt) trong bản nghiên cứu trước không tìm lại được nguồn
6. **Huế (Hai Bà Trưng)**: Cấm ô tô tải trên 5 tấn đoạn Ngô Quyền - Nguyễn Huệ (thí điểm từ 3/2023, gắn với phố đi bộ) Lý do chưa mã hóa: Thí điểm 1-6 tháng, không rõ đã thành chính thức; không có giờ
7. **Huế (cửa Đông Ba, Thái Phiên nối dài)**: Xe tải dưới 5 tấn qua cửa Đông Ba một chiều 6h-20h; Thái Phiên nối dài (Mang Cá - Đinh Tiên Hoàng) hai chiều hoặc một chiều ra 6h-20h Lý do chưa mã hóa: Quy tắc theo chiều đi, không mã hóa được
8. **Quảng Ngãi (Nghĩa Lộ, Cẩm Thành)**: Cấm ô tô một chiều theo giờ: Trần Hưng Đạo (Tây → Đông, Phan Bội Châu - Ngã 5 cũ, 16h30-17h30); Mạc Đĩnh Chi (Nguyễn Cư Trinh → Phan Bội Châu, 11h-11h30, 17h-17h30 ngày thường); cấm xe ≥10 tấn rẽ phải Lê Lợi → Phan Đình Phùng; cấm dừng đỗ xe ≥3,5 tấn trên một số tuyến Lý do chưa mã hóa: Quy tắc theo chiều, rẽ, hoặc dừng đỗ
9. **Đắk Lắk (Buôn Ma Thuột cũ)**: Biển cấm rẽ phải với xe tải trên 3,5 tấn đổi từ 6h-22h thành 6h-8h và 16h30-19h (10/2025) Lý do chưa mã hóa: Bài báo không nêu nút giao nào; quy tắc cấm rẽ. Các biển ≥10 tấn trên Nguyễn Tất Thành, Đinh Tiên Hoàng, Lê Duẩn trong bản nghiên cứu trước không tìm lại được nguồn
10. **Lâm Đồng (đèo Bảo Lộc, QL20)**: Mỗi dịp lễ (30/4, 2/9) Sở GTVT/Sở Xây dựng thường cấm xe tải có tổng trọng tải trên 20 tấn qua đèo theo chiều, thường 10h-17h Lý do chưa mã hóa: Lệnh tạm thời theo từng dịp; chưa thấy thông báo nào cho năm 2026
11. **Lâm Đồng (cầu Đơn Dương, QL27)**: Cấm xe trên 20 tấn từ 4/12/2025, đi vòng đèo Ngoạn Mục - ĐT729 - ĐT720; đã cho xe từ 20 tấn đi lại từ 8/6/2026 Lý do chưa mã hóa: Đã hết hiệu lực, ghi lại để không dùng nhầm
12. **Đà Nẵng (hầm và đèo Hải Vân), Gia Lai (Cù Mông), Đắk Lắk/Khánh Hòa (Đèo Cả), Quảng Ngãi (Lò Xo, Violak, Măng Đen), Lâm Đồng (Mimosa, Ngoạn Mục)**: Không tìm thấy lệnh cấm thường xuyên với xe tải nặng hay container; chỉ có đóng đường tạm khi sạt lở, gió bão Lý do chưa mã hóa: Không có quy tắc để mã hóa; hàng nguy hiểm qua hầm không thuộc phạm vi; nguồn (đóng hầm tạm do gió) chỉ thấy qua kết quả tìm kiếm
13. **Gia Lai**: QĐ 40/2025/QĐ-UBND (hiệu lực 13/6/2025) cho ô tô chở hàng hoạt động trên đường đô thị theo phương án tổ chức giao thông, không đặt giờ hay ngưỡng; Pleiku (≥10 tấn đi tránh) không tìm lại được nguồn Lý do chưa mã hóa: Không có quy tắc cụ thể

### Miền Nam (south.json, 17 mục)

1. **TP.HCM, chợ đầu mối Bình Điền**: Hành lang Phụ lục 1 mục 5 (Nguyễn Văn Linh vào chợ, không giới hạn giờ) Lý do chưa mã hóa: Đường vào chợ không có tên; chợ nằm ngoài vành đai nên có thể không cần.
2. **TP.HCM, Bình Tân**: Hành lang Phụ lục 1 mục 10: QL1 – Kinh Dương Vương – Trung tâm đăng kiểm 50.01S, 9h–16h Lý do chưa mã hóa: Không biết vị trí trung tâm 50.01S trên Kinh Dương Vương; mở cả đường sẽ quá rộng.
3. **TP.HCM, Quận 7 cũ**: Đoạn Trần Xuân Soạn – Tân Thuận 4 – đường nối trong hành lang Lưu Trọng Lư hướng 2 Lý do chưa mã hóa: Đường Tân Thuận 4 và đường nối không có tên trên OSM.
4. **TP.HCM, cầu Phú Mỹ, Rạch Chiếc, hầm Thủ Thiêm**: Quy định riêng cho xe tải Lý do chưa mã hóa: Không tìm thấy văn bản thường trực. Cầu Phú Mỹ và Xa lộ Hà Nội/Rạch Chiếc là tuyến ranh (được đi mọi giờ); hầm Thủ Thiêm nằm trong vùng nội đô (áp giờ cấm chung). Biển giới hạn tải trọng cầu (nếu có) cần lấy từ OSM.
5. **TP.HCM, cầu vượt thép ngã tư Thủ Đức**: Cấm container và xe tải trên 5 t (hướng Thủ Đức → cầu Sài Gòn) từ 27/7/2024 khi thi công cầu bộ hành metro Lý do chưa mã hóa: Lệnh tạm thời gắn với thi công; không rõ đã dỡ chưa.
6. **TP.HCM, cửa ngõ sân bay Tân Sơn Nhất**: Đề xuất cấm xe tải trên Trường Sơn, nhánh Bạch Đằng, nhánh Hồng Hà 6h–20h (sau đó 6h–22h) Lý do chưa mã hóa: Chỉ là đề xuất năm 2024, không rõ đã áp dụng; không nêu ngưỡng tải trọng.
7. **TP.HCM, khu Bà Rịa – Vũng Tàu cũ (Vũng Tàu, Bà Rịa, Phú Mỹ, Cái Mép – Thị Vải)**: Giờ cấm xe tải trong đô thị Vũng Tàu; giờ cho xe container vào cảng Cái Mép – Thị Vải Lý do chưa mã hóa: Không tìm thấy văn bản hay thông báo đang hiệu lực (chỉ có lệnh COVID 2021 đã thu hồi và biển cấm đậu). Dự thảo 2026 của TP.HCM nói sẽ hạn chế theo tuyến ở khu này nhưng chưa có danh sách.
8. **Đồng Nai, Bùi Hữu Nghĩa (cầu Ông Tiếp – cầu Tân Vạn)**: Xe từ 5 t cấm 5h–22h từ 1/12/2025 (theo research_south.md cũ) Lý do chưa mã hóa: Không tìm lại được nguồn; các bài đã đọc không nhắc. Có thể nhầm với Bùi Hữu Nghĩa ở Thuận An (bd_thuan_an_bui_huu_nghia).
9. **Đồng Nai (toàn tỉnh)**: QĐ 20/2026/QĐ-UBND (ký 24/3/2026, hiệu lực 10/4/2026): khung giờ cao điểm 5h–8h, 11h–13h, 16h–19h và cơ chế hạn chế xe tải trong đô thị Lý do chưa mã hóa: Chưa đọc được toàn văn; tin báo chỉ nêu khung, không có ngưỡng và danh sách đường. Các tuyến cụ thể do Sở Xây dựng, UBND xã công bố riêng.
10. **Cần Thơ, nút IC3 (QL1)**: Sơ mi rơ moóc, rơ moóc trên QL1 hướng TP.HCM → Cà Mau cấm rẽ phải vào IC3 lúc 6h–8h, 11h–13h30, 16h30–18h30; đi thẳng tới nút Trần Hoàng Na rồi quay lại Lý do chưa mã hóa: Là cấm rẽ tại nút, không phải cấm theo đoạn đường; định dạng không biểu diễn được.
11. **Cần Thơ, phường Thuận Hưng (Thốt Nốt cũ)**: Cầu 30 Tháng 4 qua sông Thốt Nốt (tải trọng cho phép 8 t) sập ngày 28/2/2026, chưa thông xe đường bộ Lý do chưa mã hóa: Tên cầu không có trên OSM; dễ trùng với đường 30 Tháng 4.
12. **Tây Ninh (gồm Long An cũ: Tân An)**: Vùng hoặc tuyến cấm xe tải Lý do chưa mã hóa: QĐ 60/2026/QĐ-UBND không có giờ cấm hay vùng cấm xe tải (theo research_south.md); không tìm thấy thông báo riêng cho Tân An, Tây Ninh.
13. **An Giang (Long Xuyên, Châu Đốc, Rạch Giá)**: Vùng hoặc tuyến cấm xe tải Lý do chưa mã hóa: Không tìm thấy văn bản; Long Xuyên giảm xe lớn nhờ tuyến tránh, không có lệnh cấm được công bố.
14. **Đồng Tháp (Cao Lãnh, Sa Đéc, Mỹ Tho)**: QĐ 76/2026/QĐ-UBND (7/7/2026) về thời gian, phạm vi hoạt động của xe thô sơ, xe chở hàng bốn bánh có gắn động cơ, vận tải đường bộ trong đô thị Lý do chưa mã hóa: Chỉ đọc được tiêu đề; chưa biết có cấm xe tải không. Mỹ Tho chỉ có đường một chiều, không thấy lệnh cấm xe tải.
15. **Vĩnh Long (gồm Bến Tre, Trà Vinh cũ)**: Vùng hoặc tuyến cấm xe tải Lý do chưa mã hóa: QĐ 51/2026/QĐ-UBND chỉ quy định xe vệ sinh môi trường, xe chở vật liệu xây dựng (chưa đọc toàn văn); QĐ 28/2017 của Bến Tre đã hết hiệu lực 30/1/2023.
16. **Cà Mau (TP Cà Mau cũ, phường An Xuyên)**: Vùng hoặc tuyến cấm xe tải Lý do chưa mã hóa: Chỉ có QĐ 35/2026 (xe vệ sinh môi trường, vật liệu xây dựng) và QĐ 37/2026 (xe thô sơ, xe bốn bánh có gắn động cơ); không thấy cấm ô tô tải.
17. **Đồng bằng sông Cửu Long**: Cầu yếu giới hạn tải trọng, giới hạn tải trọng phà Lý do chưa mã hóa: Không có danh mục tập trung; chỉ có trên biển báo (dùng thẻ maxweight của OSM). Giá vé phà Đồng Tháp (QĐ 250/QĐ-UBND-HC 2025) không phải giới hạn tải trọng.

### Toàn quốc (national.json, 12 mục)

1. **Toàn quốc**: NĐ 241/2026/NĐ-CP (ký 26/6/2026, hiệu lực 1/7/2026), Điều 11 sửa khoản 2 Điều 24 NĐ 165/2024: 'Xe ô tô tải Pickup cabin kép và xe ô tô tải Van có khối lượng toàn bộ không lớn hơn 3500 kg' 'được tổ chức giao thông như đối với xe ô tô con'. Biển cấm xe tải và cấm xe tải theo giờ phải loại trừ các xe này. Phân loại xe, thuế, phí và đăng kiểm không đổi. Lý do chưa mã hóa: Đây là quy tắc về xe, không gắn với địa điểm. Nên xử lý ở bộ so khớp: mọi rule cấm xe tải (của tỉnh, của OSM hgv=no, và các rule ở đây) bỏ qua xe có kind pickup (cabin kép) hoặc van với gross_t ≤ 3,5, trừ khi văn bản địa phương ghi rõ khác. Pickup cabin đơn không được nêu, nên vẫn xem là xe tải.
2. **Toàn quốc**: TT 38/2024 Bảng 2: ô tô kéo rơ moóc, ô tô kéo xe khác, ô tô trộn vữa, ô tô trộn bê tông lưu động, ô tô xi téc, đầu kéo kéo sơ mi rơ moóc xi téc chỉ được 60 km/h (đường đôi) và 50 km/h (đường hai chiều) ngoài khu đông dân cư. Ô tô chuyên dùng khác: 70/60. Lý do chưa mã hóa: Mô hình xe chưa có thuộc tính rơ moóc, xi téc hay chuyên dùng. Nếu bổ sung, thêm rule cap {caotoc 90, quoclo 60, chinh 50, noido 50, nho 50}.
3. **Toàn quốc, đường cao tốc**: TT 38/2024 Điều 9: tốc độ tối đa 120, tối thiểu 60 km/h; trị số cụ thể theo phương án tổ chức giao thông và biển báo của từng tuyến. Xe tải nặng chở đầy không giữ được 60 km/h khi lên dốc vẫn phải tuân thủ tốc độ tối thiểu. Lý do chưa mã hóa: Không có mức riêng theo loại xe để mã hóa. Tốc độ tối thiểu không phải lệnh cấm. Mức cap caotoc trong các rule là đề xuất.
4. **Cao tốc Hà Nội – Hải Phòng; TP.HCM – Long Thành – Dầu Giây; TP.HCM – Trung Lương**: Thí điểm cấm xe tải ở làn 1 (làn sát dải phân cách). Hà Nội – Hải Phòng (QĐ của Cục Đường bộ 13/8/2025, từ 15/8/2025, thí điểm 1 tháng): cấm ô tô tải, chuyên dùng, đầu kéo, xe kéo rơ moóc; pickup và van không bị cấm. Làn 1 tối đa 120, tối thiểu 90; làn 2 tối đa 120, tối thiểu 80; làn 3 tối đa 100, tối thiểu 60. Hai tuyến phía Nam: theo đoạn trích tìm kiếm, cấm xe tải khối lượng toàn bộ trên 7,5 t và xe khách trên 29 chỗ ở làn 1 (chưa đọc toàn văn). Lý do chưa mã hóa: Đây là quy định theo làn, không cấm cả tuyến, nên không ảnh hưởng tới việc chọn đường. Chưa rõ thí điểm đã chính thức hóa hay chưa.
5. **QL.1 qua Nghệ An: Km404+320, Km456+150, Km466+240**: Cầu vượt phía trên với chiều cao hạn chế 4,3 m, 4,0 m và 4,5 m. Riêng Km456+150 (4,0 m) thấp hơn chiều cao xếp hàng 4,2 m cho xe từ 5 t và 4,35 m cho xe công te nơ. Km441+400: 4,35 m. Lý do chưa mã hóa: Không định vị được lý trình Km trên QL.1 khi không có dữ liệu cọc Km. OSM không có thẻ maxheight ở các vị trí này. Cần định vị rồi thêm rule cấm theo height_m.
6. **Đường Hồ Chí Minh Km2321+418 (Đồng Tháp); đường dẫn cao tốc TP.HCM – Trung Lương Km1+525, Km2+000**: Cầu vượt Đường Thét có chiều cao hạn chế 4,25 m. Cổng trạm thu phí Đồng Tâm và khung hạn chế chiều cao trên đường dẫn có chiều cao hạn chế 4,3 m. Lý do chưa mã hóa: Chưa định vị được.
7. **Cầu yếu trên quốc lộ (toàn quốc)**: Cục Đường bộ và các Sở Xây dựng công bố danh sách cầu hạn chế tải trọng theo Điều 6, 7 TT 12/2025/TT-BXD. Ví dụ: QL.46C cầu Hói Nậy 10 t, cầu tại Km105+970 5 t; QL.48E cầu Thống Nhất 10 t; QL.15 cầu Ba Ra I (cũ) cấm ô tô; nhiều cầu QL.1 cũ ở Quảng Trị và Huế ghi ba số, ví dụ 25-35-40. Lý do chưa mã hóa: Mỗi tỉnh và mỗi Khu quản lý có một file riêng, cần định vị theo Km. Dạng ba số đi kèm biển S.505B (giới hạn theo từng loại xe) và chưa được giải mã chắc chắn. Phần lớn cầu cũ có cầu mới song song. Nên xử lý bằng một pipeline đọc file và định vị tự động, cộng với thẻ maxweight của OSM.
8. **Hầm Đèo Cả (QL.1, Phú Yên cũ – Khánh Hòa)**: Ngày 17/4/2024, khi QL.1 qua đèo Cả bị cấm ô tô vì sự cố hầm đường sắt Bãi Gió, hầm đường bộ cấm xe chở hàng nguy hiểm, xe cao trên 4,95 m, xe rộng từ 3,5 m, mô tô và xe thô sơ. OSM ghi maxheight 6.8. Lý do chưa mã hóa: Hàng nguy hiểm không mã hóa. Giới hạn chiều cao và chiều rộng gắn với một tình huống tạm thời năm 2024, và cao hơn khổ xe hợp pháp, nên không mã hóa.
9. **Hầm Hải Vân, hầm Cù Mông, hầm Cổ Mã**: Có thể có hạn chế với xe chở hàng nguy hiểm, nhưng chưa đọc được văn bản. Bài VnExpress 2019 về hầm Hải Vân chỉ nói về phí. OSM ghi maxheight 4.75 cho hầm Cù Mông và hầm Cổ Mã. Lý do chưa mã hóa: Hàng nguy hiểm không mã hóa. Chiều cao 4,75 m cao hơn khổ xe hợp pháp (4,2 m; 4,35 m với xe công te nơ). Không tìm thấy lệnh cấm xe tải ở các hầm này.
10. **Toàn quốc, dịp Tết và lễ**: Mỗi năm, Cục CSGT ra kế hoạch cao điểm Tết. Năm 2026: 'Đối với các tuyến quốc lộ có mật độ xe máy cao, CSGT sẽ áp dụng biện pháp cấm xe ô tô tải trọng lớn theo khung giờ.' Ngoài ra dự kiến tạm cấm chiều ra khỏi Hà Nội và TP.HCM trên một số cao tốc vào cuối kỳ nghỉ. Cục Đường bộ yêu cầu các đơn vị chuẩn bị phương án phân luồng. Các tỉnh và thành phố ra phương án phân luồng riêng, không có tuyến cố định hằng năm. Lý do chưa mã hóa: Lệnh cấm là tạm thời, thay đổi theo từng năm và không có danh sách tuyến chính thức trước. Không mã hóa thành rule đang hoạt động. Mỗi dịp Tết hoặc lễ cần nhập riêng, với valid_to.
11. **Toàn quốc**: Giới hạn xe theo TT 12/2025/TT-BXD (hiệu lực 1/7/2025, thay TT 39/2024; sửa bởi TT 19/2026/TT-BXD từ 1/7/2026). Trục đơn ≤10 t; cụm trục kép ≤11/16/18 t; cụm trục ba ≤21/24 t. Khối lượng toàn bộ: xe 2 trục 16 t, 3 trục 24 t, 4 trục 30 t, 5 trục trở lên 32/34 t; đầu kéo kéo sơ mi rơ moóc 26–48 t. Chiều cao xếp hàng 4,2 m, công te nơ 4,35 m; chiều dài ≤20 m. Lý do chưa mã hóa: Đây là giới hạn của xe, không phải lệnh cấm đường. Nên kiểm tra ở hồ sơ xe; xe vượt mức phải có giấy phép lưu hành.
12. **Toàn quốc**: Thời gian lái xe (Luật TTATGT 2024 Điều 64). Ban đầu: không quá 10 giờ/ngày, 48 giờ/tuần, liên tục không quá 4 giờ. Luật 118/2025/QH15 (hiệu lực 1/7/2026) sửa thành: liên tục không quá 4 giờ, trừ bất khả kháng; giờ làm việc theo ngày và tuần theo Bộ luật Lao động. Lý do chưa mã hóa: Quy định này ràng buộc lịch trình (VRP), không ràng buộc đường đi. Chỉ đọc tin báo về luật sửa đổi, chưa đọc toàn văn.

## 5. Việc chờ người dùng quyết định

1. **Giá trị thuộc tính nhóm xe** (`data/rules/groups.json`): các con số hiện là giả định và cần đối chiếu với đội xe thật. Ngưỡng nào áp được cho nhóm nào phụ thuộc trực tiếp vào các giá trị này.

   | Nhóm | Khối lượng toàn bộ | Hàng chuyên chở | Trục | Loại | Cao | Dài |
   |---|---|---|---|---|---|---|
   | duoi2t (gốc) | 1,9 t | 0,99 t | 2 | normal | 2,6 m | 5,0 m |
   | tu2t | 8 t | 5 t | 2 | normal | 3,4 m | 7,5 m |
   | container | 40 t | 27 t | 5 | container | 4,2 m | 16,5 m |

   Ví dụ: tu2t có hàng chuyên chở 5 t, nên rule Vinh ≥1,8 t có áp cho nhóm này, còn rule ngưỡng chuyên chở >10,5 t ở Hòa Khánh thì không.
2. **Ngày lễ âm lịch**: `holidays.json` chỉ có 4 ngày lễ cố định của năm 2026. Tết Nguyên đán và Giỗ Tổ Hùng Vương phải thêm từng năm (`State.add_holiday`). Chưa có ngày lễ nào của năm 2027.
3. **Các rule đã chọn cách đọc chặt hơn**, cần xác nhận:
   - **Hải Phòng, xe đến 1,5 t** (`hp_kv1_tai_nho_ban_ngay`, `hp_kv2_tai_nho_ban_ngay`): QĐ 11/2026 Điều 6.1.b chỉ nói "các tuyến đường giới hạn" mà không liệt kê tuyến. Seed áp cho mọi đường trong Khu vực 1 và 2.
     - Hai rule cho xe trên 1,5 t ban ngày (`hp_kv1_tai_lon_ban_ngay`, `hp_kv2_tai_lon_ban_ngay`) là `thap` nên đang tắt.
     - Hệ quả là một nghịch lý: trong KV1/KV2, `duoi2t` (hàng 0,99 t) bị cấm 6h–20h, còn `tu2t` và `container` được chạy ban ngày. Khoảng 29,6 nghìn đoạn bị cấm với `duoi2t` mà không bị cấm với hai nhóm kia (mục 2.4).
     - Cần chọn: bật hai rule `*_tai_lon_ban_ngay`, hay giữ như hiện nay.
   - **Đầu kéo ở Hà Nội** (`hn_zone_ge2t_day`): TT 53/2024 xếp "ô tô đầu kéo" thành loại riêng, và QĐ 01/52 không nêu tên loại này. Seed vẫn cấm đầu kéo theo khối lượng toàn bộ.
   - **Xe van theo NĐ 241/2026**: pickup cabin kép và van có khối lượng toàn bộ ≤3,5 t "được tổ chức giao thông như ô tô con", nên được loại khỏi các lệnh cấm xe tải (`kind not_in [pickup, van]`). Cách đọc này cần kiểm tra theo từng biển. Riêng vùng Hà Nội (`hn_zone_*`) chỉ loại pickup, vì QĐ 52 nói rõ "tải VAN" thuộc diện.
   - **Các cặp tuyến ở Đà Lạt** (`ld_dalat_15t_caodiem`): báo chỉ nêu 4 "cặp tuyến" mà không nói cấm cả hai đường, một đoạn hay chỉ hướng rẽ. Seed cấm cả 7 đường trong bbox trung tâm.
4. **Ngoại lệ Phạm Hùng, Khuất Duy Tiến** (`hn_zone_*`, sửa vòng 1): nay chỉ miễn đoạn từ điểm tiếp đất của nhánh Vành đai 3 trên cao tới Đại lộ Thăng Long (khoảng 290 m và 340 m mỗi chiều). Điểm đầu của Phạm Hùng là phố Tú Mỡ, cách điểm tiếp đất 20 m, vì nhánh không có tên. Cần xác nhận cách hiểu "đoạn tiếp đất" này; bản trước miễn cả hai tuyến (khoảng 11,6 km).
5. **Phát hiện ở bước kiểm tra mẫu** cần xác nhận (mục 2.5):
   - Mai Chí Thọ phía tây Đồng Văn Cống bị cấm (brief nói mở).
   - Nguyễn Văn Linh từ nút cầu Phú Mỹ lên Huỳnh Tấn Phát nằm trong vùng TP.HCM.
   - Nguyễn Xiển (đường dưới Vành đai 3) không được miễn.
6. **Rule "partial": vẫn bật nhưng chỉ phủ một phần văn bản.** `groups` áp phần định vị được; phần còn lại không bị cấm (hoặc, với đoạn ngoại lệ, không được miễn). Có hai nhóm.

   **a) Phần bị bỏ khỏi seed ở Task 9.** `rules-check` không thấy được nhóm này, vì phần bị bỏ không còn trong seed.
   - `qn_bao_bien_ha_long_cam_pha_tren_3_5t` (`tb`): bỏ "Đường bao biển Hạ Long - Cẩm Phả" (không có trong bbox OSM); rule chỉ còn phủ Trần Quốc Nghiễn.
   - Vinh (`na_vinh_pl1_18t`, `na_vinh_pl1_247`, `na_vinh_pl2_4t`): bỏ hẳn 10 đường (Võ Nguyên Hiến, Hoàng Nghĩa Lương, Nguyễn Bỉnh Khiêm, Tuệ Tĩnh, Minh Tân, Cao Bá Quát, Nguyễn Huy Oánh, Nguyễn Nghiễm, Tôn Thất Tùng, Trần Cảnh Bình). V.I Lênin còn khoảng 400 m và Lê Mao khoảng 380 m chưa phủ.
   - Bắc Giang (`bn_bac_giang_phu_luc1_tren_2_5t_gio_cao_diem`): bỏ 8 tên không có trong OSM (Võ Thị Sáu, Tiền Giang, Châu Xuyên, Tân Ninh, Đàm Thuận Huy, Phùng Trạm, Nguyễn Duy Năng, Phạm Liêu).
   - Quảng Ngãi: `qng_5t_6_18` bỏ Trần Cẩm và Phạm Xuân Hòa; `qng_cam_xetai_canngay` bỏ Duy Tân và đoạn Tôn Đức Thắng (Bà Triệu – Quang Trung).
   - Bình Dương cũ (`bd_6_tuyen_dau_keo`, `bd_6_tuyen_tren_10t`): bỏ đoạn ĐT743 và đoạn ĐT747 (Miếu Ông Cù).
   - ĐT.390 (`hp_dt390_4truc_gio_cao_diem`): khoảng 360 m từ nút QL5 tới chỗ OSM đứt chưa phủ.

   **b) Một chiều của đường đôi bị sót** (orphan `"missing": "carriageway"`, phát hiện từ đợt sửa review cuối). Các rule này đều là `cao`/`tb`, nên `rules-check` báo lỗi. Độ dài là tổng độ dài các đoạn bị sót, đo ngày 22/10/2026:

   | Rule | Đoạn tuyến | Phần bị sót |
   |---|---|---|
   | `hcm_hl_dang_kiem_5003s` | Phạm Văn Đồng, cầu Gò Dưa → Ngã Tư Bình Triệu; Bình Triệu → QL13 | 8.412 m; 295 m |
   | `na_vinh_pl1_18t` | V.I Lênin, Nguyễn Công Nghiệm → Nguyễn Sỹ Sách; Nguyễn Đức Cảnh (2 đoạn) | 1.107 m; 89 m + 83 m |
   | `hn_zone_ge2t_day`, `hn_zone_lt2t_peak` | ngoại lệ Tam Trinh, Tân Mai → Đỗ Mười | 541 m (phần này bị cấm thay vì được miễn) |
   | `na_vinh_pl2_4t` | Phùng Chí Kiên (2 đoạn); Nguyễn Công Trứ (2 đoạn) | 139 m + 80 m; 57 m + 129 m |
   | `qng_10t_6_18` | Phan Bội Châu (2 đoạn) | 76 m + 205 m |
   | `qng_5t_tonducthang_canngay` | Tôn Đức Thắng (2 đoạn) | 54 m + 65 m |
   | `qng_5t_6_18` | Quang Trung → Lê Lợi | 84 m |
   | `dni_dong_khoi_tren_2t`, `dni_dong_khoi_tren_5t` | Đồng Khởi, QL1 → Trương Định | 148 m |
   | `bd_6_tuyen_dau_keo`, `bd_6_tuyen_tren_10t` | Huỳnh Văn Cù từ Ngã tư Chợ Cây Dừa | 107 m |
   | `hcm_hl_pham_the_hien_ql50` | Phạm Thế Hiển, Trịnh Quang Nghị → cầu Bà Tàng | 100 m |
   | `dng_dt603b_hoian_10t`, `dng_dt603b_hoian_somi` | Võ Chí Công tới cầu Cửa Đại | 91 m |
   | `hn_tu_lien_nghi_tam_yen_phu` (chỉ ngày 22/10) | Yên Phụ, Yên Hoa → Thanh Niên | 64 m |
   | `cm_bac_lieu_noi_o_tu_3_5t` | Cách Mạng (2 đoạn) | 63 m + 47 m |
   | `ct_5_truc_noi_o_tren_3_5t` | Nguyễn Văn Cừ, Mậu Thân → CMT8 | 52 m |
   | `bd_dong_hoa_dau_keo`, `bd_dong_hoa_tren_10t` | Mỹ Phước – Tân Vạn, ĐT743A → QL1 | 43 m |

   - Nguyên nhân: phần đường cùng tên nằm giữa hai đầu mút nhưng không chạm đường giao nào, ví dụ một chiều đường đôi bị cầu hay nút mang tên riêng cắt rời, hoặc một làn phụ ngắn cùng tên. `route_piece` chỉ lấy các thành phần có nguồn ở đường giao, nên bỏ chúng; trước đợt sửa này việc bỏ là lặng lẽ.
   - Đã thử gieo nguồn theo khoảng cách điểm–đoạn (thay vì nút–nút) như review gợi ý: không sửa được trường hợp nào ở trên.
   - Các đoạn ranh giới vùng cũng sót chiều (Võ Nguyên Giáp, Nguyễn Văn Linh, Mai Chí Thọ, Nguyễn Thị Định ở TP.HCM; CT.37 ở Hà Nội), nhưng chiều còn lại vẫn khép vùng, nên vùng không đổi và không báo lỗi.
   - Cần chọn: sửa seed (tách đoạn qua cầu, như Task 9 đã làm), chấp nhận phần sót, hay đổi code để tự gộp phần đường cùng tên chạy song song sát đoạn đã lấy.

## 6. Lưu ý và rủi ro còn lại

- **Đầu mút bị bỏ có thể cấm dài hơn văn bản.** Ở Vinh, Quảng Ngãi và Bắc Giang, nhiều đoạn chạy tới đầu mút của đường trong OSM.
  - Đã kiểm tra hướng bằng điểm neo.
  - Độ dài thực của đoạn bị cấm có thể vượt mốc trong văn bản, ví dụ Nguyễn Gia Thiều, Lê Huân, Lục Niên, Ngô Gia Tự.
- **Phần chưa phủ:**
  - V.I Lênin: khoảng 400 m sát Đảo Hải Quan.
  - Lê Mao: khoảng 380 m từ ngã 5 Bưu Điện tới Ngư Hải.
  - ĐT.390: khoảng 360 m từ nút QL5 tới chỗ OSM đứt.
  - Vùng Hà Nội: dải khoảng 0,5 km² giữa Đại lộ Thăng Long và chuỗi Đại Việt.
- **Mỹ Phước – Tân Vạn (Bình Dương cũ)**: đoạn "từ Phú Lợi tới ĐT743A" chỉ bắt được khoảng 1 km, vì ĐT.743A cũng giao Mỹ Phước – Tân Vạn gần Phú Lợi. Chưa sửa.
- **Mục ranh giới để cả tên** có phần chạy ra ngoài vòng: "Đại Việt" (Hà Nội), Võ Chí Công và QL1 (TP.HCM), Đình Vũ (đã bỏ ở Hải Phòng KV1). Phần đó chỉ bị loại khỏi vùng, không bị cấm thêm.
- **Rule đã hết hạn không được `rules-check` kiểm** (chỉ kiểm rule có hiệu lực):
  - `hn_vd3_pilot_ge10t` và `hn_vd3_pilot_tractor` vẫn lỗi khi chạy với ngày trước 31/3/2026. Đoạn "Đại lộ Thăng Long từ Đường 70" không định vị được, vì OSM tách ĐLTL thành 7 thành phần và không chung nút với ĐT.70.
  - Nếu cần dựng lại view cho ngày cũ thì phải sửa thêm.
- **`normalize_name` không gộp hai cách bỏ dấu** ("Thùy" và "Thuỳ") và không bỏ tiền tố "Đại lộ ". Hai trường hợp này được sửa bằng tên OSM trong seed, không phải trong code.
- **Các thay đổi ranh giới là đường nối**, không có trong văn bản. Gồm Cầu vượt Ngã ba Huế (Đà Nẵng), Liên Phương và Cầu Niệm (Hải Phòng), cầu vượt Vĩnh Ngọc và cầu vượt đường sắt Bắc Hồng - Văn Điển (Hà Nội), và các cầu ở TP.HCM. Chúng chỉ khép chỗ hở giữa hai đường văn bản đã nêu.

## 7. Sửa vòng 1 (sau review)

- **Ngoại lệ Phạm Hùng, Khuất Duy Tiến** thu hẹp về đoạn tiếp đất (xem mục 3.1). Hai rule `hn_zone_lt2t_peak`, `hn_zone_ge2t_day` có thêm ghi chú. Phạm vi vùng tăng từ 184.466 lên 184.833 đoạn (+367: 426 đoạn trước được miễn, nay còn 59).
- **Nguyên nhân Mai Chí Thọ** trong mục 2.5 được sửa: 36 đoạn ở nút Đồng Văn Cống do gieo nguồn 60 m quanh nút giao, không do dung sai 1,1·D + 200 m.
- **Ghi chú QL5** sửa: hai cầu vượt khép chỗ hở giữa Trường Sa, Hoàng Sa và Võ Văn Kiệt, không nằm trên QL5.
- Chạy lại: `import-rules` (7 file, một lệnh), `rules-check` (88 rule, 0 lỗi, exit 0) và `groups --as-of 2026-10-22` (43,0 s, peak WS 3,46 GB, private 4,89 GB). View ngày 1/10/2026 chưa dựng lại.
- Kiểm tra lại trên view `tu2t` ngày 22/10/2026, slot 16 (08:00):

| Kiểm tra | Kết quả |
|---|---|
| Phố Huế, slot 16 | 61/61 cấm |
| Phố Huế, slot 44 | 0/61 cấm |
| Phạm Hùng, đoạn miễn (Tú Mỡ → ĐLTL) | 0/37 cấm |
| Khuất Duy Tiến, đoạn miễn (Lối xuống Trần Duy Hưng → ĐLTL) | 0/22 cấm |
| Phạm Hùng, toàn tuyến | 298/340 cấm |
| Khuất Duy Tiến, toàn tuyến | 69/91 cấm |

## 8. Đợt sửa sau review cuối (22/10/2026)

Thay đổi code (gói `speedmatrix`, có test cho từng sửa):
- **Ô bị ảnh hưởng tính so với nhóm gốc.** View của nhóm không phải gốc nay ghi các đoạn khác `duoi2t` ở ít nhất một khung giờ (tốc độ hoặc trạng thái cấm), đúng spec §5.4 và §8: ngoài các đoạn này, dự án con 2 dùng lại mảnh của nhóm gốc. `groups` đòi đúng một nhóm gốc. `report.json` có thêm `base_group` và `affected_against` cho từng nhóm. Định dạng view tăng lên 3, nên view cũ bị từ chối khi đọc.
- **`groups` kiểm phiên bản tốc độ.** Nếu lớp tốc độ không được dựng cho đúng các rule tốc độ có hiệu lực ngày `--as-of`, lệnh dừng với `ReleaseMismatch` (exit 2), ghi cả hai phiên bản. `group_speeds` cũng từ chối một view dựng trên lớp tốc độ khác.
- **`rules-check`** thống nhất với `groups`: xem mục 2.3.
- **Rule cấm chỉ được nằm ở lớp `official` hoặc `company`**, là các lớp `groups` áp dụng. Rule cấm lớp `edit` trước đây được nhận rồi bị bỏ qua lặng lẽ.
- **Kiểm giá trị nhóm xe** khi đọc `groups.json`: `kind` phải thuộc danh sách loại xe, các số đo phải dương, `payload_t` không vượt `gross_t`, số trục là số nguyên ≥ 1, tên dùng được làm tên file, không trùng tên và không quá một nhóm gốc. Trước đây một lỗi gõ như `"Container"` được lưu và làm mọi rule theo loại xe thôi khớp.
- **Orphan `carriageway`** cho đoạn tuyến sót một chiều đường đôi: mục 2.3 và 5.6.

Chạy lại trên dữ liệu thật (cùng `segments/816249187dfd` và `speeds/dba5f7c3e555`, không dựng lại lớp tốc độ):
- `rules-check` cho 22/10 và 1/10: kết quả ở mục 2.3.
- `groups` cho 22/10 (50,3 s) và 1/10 (48,8 s), peak WS 3,46 GB, private 4,89 GB. Số đoạn ở mục 2.4.
- Kiểm tra trên view mới: mọi đoạn bị cấm với `duoi2t` mà không nằm trong tập bị ảnh hưởng của `tu2t` (2.964 đoạn) hay `container` (2.767 đoạn) đều bị cấm giống hệt ở nhóm đó (cùng các khung giờ), và ở các khung giờ còn mở thì tốc độ hai nhóm bằng nhau (ví dụ cùng giới hạn noido 50 km/h). Vì vậy mảnh của nhóm gốc đúng cho các nhóm này ở những đoạn đó.
