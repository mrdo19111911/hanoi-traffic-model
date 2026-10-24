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
| 9 | `rules-check … --as-of 2026-10-22` (sau vòng sửa `route_piece`) | 12 s | – | – | 88 rule, **0 lỗi**, 0 rule "partial", exit 0 |
| 9 | `rules-check … --as-of 2026-10-01` (sau vòng sửa `route_piece`) | 13 s | – | – | 84 rule, **0 lỗi**, 0 rule "partial", exit 0 |
| 9 | `groups … --as-of 2026-10-22` (dựng lại) | 57,0 s | 3,46 GB | 4,90 GB | thay view cũ |
| 9 | `groups … --as-of 2026-10-01` (dựng lại) | 57,6 s | 3,46 GB | 4,89 GB | thay view cũ |
| 9.1 | `rules-check … --as-of 2026-10-22` (sau sửa vòng 1 của `route_piece`) | 10 s | – | – | 88 rule, **3 lỗi** (orphan `gap`), 3 rule "partial", exit 2 |
| 9.1 | `rules-check … --as-of 2026-10-01` (sau sửa vòng 1 của `route_piece`) | 11 s | – | – | 84 rule, **3 lỗi**, 3 rule "partial", exit 2 |
| 9.1 | `groups … --as-of 2026-10-22` (dựng lại) | 48,4 s | 3,46 GB | 4,90 GB | thay view cũ |
| 9.1 | `groups … --as-of 2026-10-01` (dựng lại) | 45,3 s | 3,46 GB | 4,89 GB | thay view cũ |
| 9.1 vòng 2 | `rules-check … --as-of 2026-10-22` (sau vòng sửa 2: seed ĐT.390, Võ Chí Công) | 11 s | – | – | 88 rule, **1 lỗi** (orphan `gap`), 1 rule "partial", exit 2 |
| 9.1 vòng 2 | `rules-check … --as-of 2026-10-01` (sau vòng sửa 2) | 11 s | – | – | 84 rule, **1 lỗi**, 1 rule "partial", exit 2 |
| 9.1 vòng 2 | `groups … --as-of 2026-10-22` (dựng lại) | 42,5 s | 3,46 GB | 4,90 GB | thay view cũ |
| 9.1 vòng 2 | `groups … --as-of 2026-10-01` (dựng lại) | 42,0 s | 3,46 GB | 4,90 GB | thay view cũ |
| 9.1 Lê Trọng Tấn | `rules-check … --as-of 2026-10-22` (sau khi người dùng chọn đầu mút Lê Trọng Tấn) | 12 s | – | – | 88 rule, **0 lỗi**, 0 rule "partial", exit 0 |
| 9.1 Lê Trọng Tấn | `rules-check … --as-of 2026-10-01` | 11 s | – | – | 84 rule, **0 lỗi**, 0 rule "partial", exit 0 |
| 9.1 Lê Trọng Tấn | `groups … --as-of 2026-10-22` (dựng lại) | 50,9 s | 3,46 GB | 4,90 GB | thay view cũ |
| 9.1 Lê Trọng Tấn | `groups … --as-of 2026-10-01` (dựng lại) | 49,9 s | 3,31 GB | 4,89 GB | thay view cũ |

Đợt sửa sau review cuối (mục 8) và vòng sửa `route_piece` (mục 9, 9.1) không đổi lớp tốc độ: `groups` chấp nhận `speeds/dba5f7c3e555` cho cả hai ngày, vì không ngày nào có rule tốc độ. Ở mục 8, `rules-check` báo 20 lỗi `carriageway`; mục 9 sửa `route_piece` để lấy luôn chiều đường còn lại, và cả hai ngày trở về 0 lỗi. Mục 9.1 (sau review độc lập) bắt thêm hai kiểu mất đường mà mục 9 bỏ qua lặng lẽ. Sau mục 9.1, 3 rule lỗi vì orphan `gap`: hai rule Võ Chí Công và hành lang Lê Trọng Tấn. Vòng sửa 2 (cuối mục 9.1) cho đoạn tuyến vượt chỗ cắt giữa hai đường giao và sửa seed ĐT.390 và Võ Chí Công. Còn hành lang Lê Trọng Tấn lỗi; người dùng đã chọn lấy cả Lê Trọng Tấn trong `bbox` (22/10/2026), nên nay cả hai ngày 0 lỗi, exit 0 (mục 2.3, 5.6).

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

**Sau vòng sửa `route_piece` (mục 9)**, một đoạn tuyến tự lấy cả phần đường cùng tên chạy cạnh nó giữa hai đầu mút (hành lang 60 m), và orphan `carriageway` dùng đúng định nghĩa đó:
- Một điểm "nằm cạnh" đoạn tuyến khi cách phần tìm được không quá 60 m và chân đường vuông góc rơi hẳn vào giữa hai đầu mút. Chân rơi ra ngoài khi dừng ở một đầu cụt của đoạn, hoặc khi theo đường đi nó cách một đường giao ít nhất bằng khoảng cách giữa hai đường giao (D). Vì vậy phần đoạn tuyến tự chạy lố qua đường giao (dung sai 1,1·D + 200 m) không tính là "giữa".
- Mỗi đoạn đường cùng tên bị bỏ mà có ít nhất một nửa số điểm (lấy mẫu 10 m) nằm cạnh đoạn tuyến thì được lấy vào.
- Cầu mang tên riêng ("Cầu …", "Hầm …") nằm giữa hai phần của chiều đường được lấy vào thì được lấy luôn. Điều kiện: cầu chạy dọc đoạn tuyến, không cắt ngang, và lấp một chỗ hở mà bản thân con đường không nối được. Đường khác tên, nhánh không tên và vòng xuyến không bao giờ được lấy. (Mục 9.1 thay quy tắc này, xem dưới.)
- Orphan `carriageway` nay chỉ còn là lưới an toàn: một điểm nằm cạnh đoạn tuyến (cùng định nghĩa) thuộc một đoạn không lấy được (dưới một nửa nằm cạnh), và theo đường đi cách phần đã lấy hơn 300 m hoặc không nối với nó.
- Đầu xa của đoạn tuyến một phía (chỉ có `from` hoặc chỉ có `to`) nay cũng gieo mọi nút trong vòng 60 m như ở đường giao, để cả hai nhánh của một đường kết thúc bằng ngã ba chữ Y đều là đầu mút. (Mục 9.1: chỉ các nút cùng thành phần liên thông.)

**Sau sửa vòng 1 của `route_piece` (mục 9.1).** Nguyên tắc: mỗi phần đường bị mất phải được lấp hoặc báo thành orphan, không được bỏ lặng lẽ.
- **Đầu xa chỉ gieo nút của chính nhánh đó.** Đầu xa của đoạn tuyến một phía chỉ lấy các nút trong vòng 60 m thuộc cùng thành phần liên thông. Trước đó nó lấy cả nút giữa một chiều đường khác chạy ngang qua, làm chiều đó bị cắt ngắn mà không báo.
- **Chỗ hở trong một chiều đường.** Chỗ hở nối một đầu cụt nơi xe đi tới với một đầu cụt phía trước nơi xe đi tiếp cùng hướng (đầu đường hai chiều tính cả hai vai). Đầu cụt của hai chiều ngược nhau, hay một làn tự kết thúc, không tạo chỗ hở. Chỗ hở được lấp theo thứ tự:
  1. bằng chính các đoạn cùng tên bị bỏ, nếu chúng nối hai đầu trong khoảng cách thẳng + 300 m (một chiều đường lượn ra xa hơn 60 m);
  2. bằng một đường nối dài tối đa 100 m: đường không tên, hoặc cầu/hầm. Cầu/hầm xét theo thẻ OSM nếu bảng đoạn có cột đó; bảng hiện tại chưa có, nên xét theo tên: tên phải bắt đầu bằng "Cầu " hoặc "Hầm ". Vì thế "Đường Cầu Giấy" hay "Phố Cầu …" không phải cầu. Cung vòng xuyến không tên được tính là đường nối.

  Chỗ hở không lấp được (hai đầu đều nằm cạnh đoạn tuyến) thành orphan `{"missing": "gap", "at": [[lat, lon], …]}`, với tọa độ là điểm giữa hai đầu. Vùng chấp nhận orphan này như orphan `carriageway`.
- **Đoạn tuyến một phía bị cắt trước khi hết đường.** Nếu đường cùng tên còn tiếp tục trong vòng 300 m sau đầu xa nhưng bị cắt rời (một đoạn nối, một vòng xuyến), `route_piece` vượt chỗ cắt bằng cùng quy tắc đường nối rồi tìm lại đầu xa, tối đa 8 lần. Chỗ cắt không vượt được thành orphan `gap` tại đầu xa.

| Vòng | Ngày | Rule bật | Lỗi | Partial | Rule tắt có cảnh báo | Exit |
|---|---|---|---|---|---|---|
| Mục 9.1 | 22/10/2026 | 88 | **3** | 3 | 8 / 25 | 2 |
| Mục 9.1 | 1/10/2026 | 84 | **3** | 3 | 8 / 25 | 2 |

- Ba rule lỗi ở cả hai ngày, đều vì orphan `gap`:
  - `dng_dt603b_hoian_10t` và `dng_dt603b_hoian_somi` (Võ Chí Công tới cầu Cửa Đại). Đoạn tuyến bị cắt bởi hai đoạn khoảng 30 m mang tên "Đường Ven Biển 129 (Võ Chí Công)". Tên này không khớp "Võ Chí Công", và một đường có tên thì không được dùng làm đường nối, nên chỗ cắt không vượt được và được báo.
  - `hcm_hl_kcn_tan_binh` (Lê Trọng Tấn từ QL1, hành lang mở). Đây là phát hiện mới: hai làn ngoài dừng ở một đường ngang ("Đường CN1"), còn Lê Trọng Tấn chạy tiếp khoảng 2,8–3,0 km mỗi chiều mà trước đây không thuộc rule.
- Số đoạn của các rule đổi như sau:
  - `hcm_hl_dang_kiem_5003s`: 510 → 550, cả hai ngày;
  - `qng_5t_6_18`: 164 → 174, cả hai ngày;
  - `hp_dt390_4truc_gio_cao_diem`: 295 → 389, chỉ ngày 22/10.

  Vùng `hcm_qd23_*` và `hn_zone_*` giữ nguyên từng đoạn, cùng diện tích (272,666 km² và 229,024 km²), dù các đoạn ranh giới Võ Nguyên Giáp và Mai Chí Thọ lấy thêm đường.
- Các dòng `WARN` của rule đang tắt giống hệt mục 9.

**Sau vòng sửa 2 (cuối mục 9.1).**
- **Đường giao theo ref.** `from`/`to` khớp theo tên hoặc theo ref, như tên đường: "QL77" tìm được đường chỉ mang `ref=QL.77`. Ref không đường nào mang, hoặc đường mang nó không chung nút với đoạn tuyến, là orphan `{"missing": "from" | "to"}`, không bao giờ chạy tiếp tới hết đường.
- **Chỗ cắt giữa hai đường giao.** Trước đây, nếu hai đường giao nằm trên hai phần bị cắt rời của con đường thì đoạn tuyến là orphan `path`. Nay chỗ cắt được vượt bằng cùng quy tắc đường nối (không tên hoặc cầu/hầm, tối đa 100 m, trong vòng 300 m quanh chỗ hai phần gần nhau nhất). Sau đó đoạn tuyến chạy giữa hai đường giao như thường, nên dừng ở đường giao được ghi, không chạy tới hết đường. Không vượt được thì vẫn là orphan `path`.
- **Seed:** ĐT.390 thêm đầu `from` "Quốc lộ 5"; hai rule Võ Chí Công dùng `{"ref": "ĐT619"}`. Chi tiết ở mục 5.6 b.

| Vòng | Ngày | Rule bật | Lỗi | Partial | Rule tắt có cảnh báo | Exit |
|---|---|---|---|---|---|---|
| Vòng sửa 2 | 22/10/2026 | 88 | **1** | 1 | 8 / 25 | 2 |
| Vòng sửa 2 | 1/10/2026 | 84 | **1** | 1 | 8 / 25 | 2 |

- Rule lỗi duy nhất ở cả hai ngày: `hcm_hl_kcn_tan_binh`, orphan `gap` tại "Đường CN1". Seed giữ nguyên vì văn bản không nêu đầu mút (mục 5.6 b).
- Số đoạn của các rule đổi như sau:
  - `dng_dt603b_hoian_10t`, `dng_dt603b_hoian_somi`: 399 → 565, hết orphan, cả hai ngày;
  - `hp_dt390_4truc_gio_cao_diem`: 389 → 363, chỉ ngày 22/10.
- Mọi rule khác giữ nguyên số đoạn và orphan. Các dòng `WARN` của rule đang tắt không đổi.

**Sau khi người dùng chọn đầu mút Lê Trọng Tấn (22/10/2026).** `hcm_hl_kcn_tan_binh` lấy cả Lê Trọng Tấn trong `bbox` (mục 5.6 b).

| Vòng | Ngày | Rule bật | Lỗi | Partial | Rule tắt có cảnh báo | Exit |
|---|---|---|---|---|---|---|
| Lê Trọng Tấn | 22/10/2026 | 88 | **0** | 0 | 8 / 25 | 0 |
| Lê Trọng Tấn | 1/10/2026 | 84 | **0** | 0 | 8 / 25 | 0 |

- `hcm_hl_kcn_tan_binh`: 110 → 274 đoạn (Lê Trọng Tấn 225, Đường M1 49), hết orphan, cả hai ngày.
- Mọi rule khác và các dòng `WARN` không đổi.

Kết quả của mục 8 và mục 9:

| Vòng | Ngày | Rule bật | Lỗi | Partial | Rule tắt có cảnh báo | Exit |
|---|---|---|---|---|---|---|
| Mục 8 | 22/10/2026 | 88 | 20 | 20 | 8 / 25 | 2 |
| Mục 8 | 1/10/2026 | 84 | 19 | 19 | 8 / 25 | 2 |
| Mục 9 | 22/10/2026 | 88 | **0** | 0 | 8 / 25 | 0 |
| Mục 9 | 1/10/2026 | 84 | **0** | 0 | 8 / 25 | 0 |

- Ở mục 8, cả 20 lỗi (19 ngày 1/10; khác nhau ở `hn_tu_lien_nghi_tam_yen_phu`, chỉ có hiệu lực ngày 22/10) đều là orphan `carriageway`. Không có lỗi vùng hay lỗi rule nào ở rule đang bật.
- Ở mục 9, cả hai ngày không còn lỗi hay rule partial nào (nhưng có hai kiểu mất đường không được báo, sửa ở mục 9.1). Trên toàn bộ 185 đoạn tuyến có `from`/`to` trong seed, số đoạn có orphan `carriageway` giảm từ 32 (tính cả mục ranh giới vùng) xuống 0. Không đoạn nào mất đoạn đường nào so với trước.
- Số đoạn của các rule đổi như sau (giống nhau cho cả hai ngày): `hcm_hl_dang_kiem_5003s` 362 → 510, `na_vinh_pl1_18t` 891 → 899, `bd_6_tuyen_*` 1.281 → 1.284, `hcm_hl_cang_nha_rong_ra` 127 → 130, `hcm_hl_nha_may_sua` 72 → 75, `hcm_hl_cang_phu_dinh` 265 → 267, `hn_zone_*` 184.833 → 184.826 (7 đoạn Tam Trinh nay được miễn), `hcm_qd23_*` 239.977 → 239.943 (vùng TP.HCM, xem mục 9). Mọi rule khác giữ nguyên số đoạn. Chi tiết ở mục 5.6.
- Các dòng `WARN` của rule đang tắt giống hệt mục 8.
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
| 2026-10-22 | 4,05 MB | 3,46 MB | 1,65 MB | 17,6 KB |
| 2026-10-01 | 4,05 MB | 3,46 MB | 1,65 MB | 17,0 KB |

Cả thư mục `speeds/dba5f7c3e555` khoảng 20 MB.

**Số đoạn bị ảnh hưởng theo nhóm** (cả hai ngày dựng lại sau khi người dùng chọn đầu mút Lê Trọng Tấn, cuối mục 9.1; trong ngoặc là ngày 1/10/2026). Từ đợt này, "bị ảnh hưởng" của nhóm gốc `duoi2t` vẫn tính so với lớp tốc độ trần, còn của `tu2t` và `container` tính so với **view của nhóm gốc**: các đoạn có tốc độ hoặc trạng thái cấm khác `duoi2t` ở ít nhất một khung giờ. Đó là các đoạn mà dự án con 2 phải dựng mảnh riêng; mọi đoạn khác dùng lại mảnh của nhóm gốc.

| Nhóm | Gốc | Bị ảnh hưởng | So với | Bị cấm | Bị giới hạn tốc độ | Rule áp dụng | Giới hạn tốc độ (km/h) theo lớp | Rule giới hạn |
|---|---|---|---|---|---|---|---|---|
| duoi2t | có | 461.512 (461.494) | lớp tốc độ | 456.214 (456.196) | 7.395 | 19 (17) | caotoc 100, quoclo 90, chinh 80, noido 50, nho 50 | vn_cap_tai_den_3_5t |
| tu2t | không | 1.042.550 (1.042.700) | duoi2t | 562.715 (562.697) | 461.750 | 53 (51) | caotoc 90, quoclo 80, chinh 70, noido 50, nho 50 | vn_cap_tai_tren_3_5t |
| container | không | 1.229.481 (1.229.289) | duoi2t | 569.487 (569.106) | 653.448 | 69 (66) | caotoc 90, quoclo 70, chinh 60, noido 50, nho 50 | vn_cap_dau_keo_so_mi_ro_mooc |

Thay đổi khi lấy cả Lê Trọng Tấn (so với view của vòng sửa 2, giống nhau cho hai ngày):
- Số đoạn bị cấm, bị ảnh hưởng và bị giới hạn tốc độ của cả ba nhóm không đổi.
- `tu2t` và `container`: 164 đoạn Lê Trọng Tấn (5.778 m) vẫn bị cấm nhưng ít khung giờ hơn, vì nay được mở 9h–16h và 21h–22h như hành lang. Các đoạn này vốn đã nằm trong tập bị ảnh hưởng.
- `duoi2t`: không đổi (hành lang chỉ mở cho xe chở trên 2,5 t).
- `report.json` của cả hai ngày không còn orphan nào.

Thay đổi ở vòng sửa 2 so với view của mục 9.1 (số đoạn bị giới hạn tốc độ không đổi):
- `duoi2t` và `tu2t`: không đổi ở cả hai ngày, từng đoạn và từng khung giờ. Rule Võ Chí Công (`dng_dt603b_hoian_10t`, chở trên 10 t) chỉ áp cho `container`, còn rule ĐT.390 (từ 4 trục) cũng vậy.
- `container` ngày 22/10: bị cấm 569.347 → 569.487 (+166 đoạn, 8.600 m; −26 đoạn, 1.845 m), bị ảnh hưởng 1.229.506 → 1.229.481.
  - Thêm 166 đoạn: Võ Chí Công 162, cầu Đế Võng 2, "Đường Ven Biển 129 (Võ Chí Công)" 2. Các đoạn này vốn đã khác `duoi2t` vì giới hạn tốc độ, nên số bị ảnh hưởng không tăng.
  - Bớt 26 đoạn ĐT.390 phía tây bắc nút QL5 (1.845 m). 25 đoạn trong đó nay giống hệt `duoi2t` nên ra khỏi tập bị ảnh hưởng.
- `container` ngày 1/10 (rule ĐT.390 chưa có hiệu lực): bị cấm 568.940 → 569.106 (+166 đoạn Võ Chí Công như trên), bị ảnh hưởng không đổi.
- Không đoạn nào đổi khung giờ cấm. `report.json` của cả hai ngày nay chỉ còn orphan của `hcm_hl_kcn_tan_binh`.

Thay đổi ở mục 9.1 so với view của mục 9 (số đoạn bị giới hạn tốc độ không đổi; không đoạn nào thôi bị cấm):
- `duoi2t`: không đổi ở cả hai ngày.
- `tu2t`: bị ảnh hưởng 1.042.540 → 1.042.550, bị cấm 562.705 → 562.715 (ngày 1/10: 1.042.690 → 1.042.700, 562.687 → 562.697).
  - Thêm 10 đoạn (226 m) bị cấm: 201 m Quang Trung bên kia vòng xuyến Hai Bà Trưng và 25 m cung vòng xuyến. Đây là phần **thừa** so với văn bản ("Quang Trung (Hai Bà Trưng – …)"), xem mục 5.6 b.
  - 40 đoạn (336 m) vẫn bị cấm nhưng ít khung giờ hơn: chỗ hở của hành lang Phạm Văn Đồng nay được mở 9h–16h. Gồm 6 đoạn Phạm Văn Đồng (279 m, làn song hành lượn ra xa hơn 60 m) và 34 đoạn không tên (58 m, có đoạn trunk 20,7 m, way 1219668784).
- `container` ngày 22/10: bị ảnh hưởng 1.229.433 → 1.229.506, bị cấm 569.243 → 569.347 (+104 đoạn, 4.831 m). Gồm 10 đoạn Quang Trung như trên và 94 đoạn ĐT.390 (3.948 m ĐT.390, 486 m cầu vượt nút giao Ba Hàng, 171 m cung vòng xuyến). Phần lớn đoạn ĐT.390 thêm vào nằm phía tây bắc nút QL5, ngoài văn bản, xem mục 5.6 b. Cùng 40 đoạn Phạm Văn Đồng ít khung giờ hơn.
- `container` ngày 1/10 (rule ĐT.390 chưa có hiệu lực): bị cấm 568.930 → 568.940 (10 đoạn Quang Trung), bị ảnh hưởng không đổi (các đoạn này vốn đã khác `duoi2t` vì giới hạn tốc độ).
- `report.json` của cả hai ngày nay ghi orphan của `dng_dt603b_hoian_10t` và `hcm_hl_kcn_tan_binh`. Rule `dng_dt603b_hoian_somi` không áp cho nhóm nào nên không có trong view.

Thay đổi ở mục 9 so với view của mục 8 (giống nhau cho hai ngày; số đoạn bị giới hạn tốc độ không đổi):
- `duoi2t`: bị ảnh hưởng 461.551 → 461.512, bị cấm 456.253 → 456.214 (−39 đoạn, 1.517 m, không thêm đoạn nào).
  - 7 đoạn Tam Trinh (541 m) nay được miễn khỏi vùng Hà Nội, như văn bản.
  - 32 đoạn quanh ranh giới vùng TP.HCM ra khỏi vùng.
    - 9 đoạn thuộc chiều còn lại của chính đường ranh giới (Mai Chí Thọ 5, Nguyễn Văn Linh 4), nay là ranh giới. Đường nằm trên ranh giới thì không thuộc vùng.
    - 23 đoạn là nhánh hay đường khác nằm sát chiều đó, trong dải bịt kín 60 m quanh ranh giới: 13 đoạn không tên, Nguyễn Lương Bằng 4, Nguyễn Đổng Chi 3, Đường song hành 1, Thung Lũng Xanh 1, Đường số 2 1.
    - Diện tích vùng 272,707 → 272,666 km² (khoảng cho phép 120–400 km²); vùng không bị tách. Vùng Hà Nội không đổi (229,024 km²).
- `tu2t`: bị ảnh hưởng 1.042.566 → 1.042.540; bị cấm 562.734 → 562.705 (+8, −37).
  - Thêm 8 đoạn V.I Lênin (825 m, chiều còn lại).
  - Bớt 37 đoạn: 7 đoạn Tam Trinh; 27 đoạn quanh ranh giới vùng TP.HCM (các đoạn ở trên mà nhóm này vốn bị cấm); 3 đoạn Tôn Thất Thuyết (làn song song của hành lang cảng Nhà Rồng, nay được mở).
  - Ngoài ra 153 đoạn (8.604 m) vẫn bị cấm nhưng ít khung giờ hơn: 148 đoạn Phạm Văn Đồng (chiều còn lại của hành lang đăng kiểm 50-03S, nay mở 9h–16h), 3 đoạn Võ Văn Ngân, 2 đoạn Võ Văn Kiệt. **Đính chính (mục 9.1):** sau mục 9, hành lang này vẫn chưa đi được theo chiều QL13 → QL1. Trong tập được mở có hai chỗ hở: một đoạn trunk không tên 20,7 m (way 1219668784) và 279 m làn song hành Phạm Văn Đồng lượn ra xa hơn 60 m. Mục 9.1 lấp cả hai; nay đi được cả hai chiều QL1 ↔ QL13 trong tập được mở.
- `container`: bị ảnh hưởng 1.229.456 → 1.229.433; bị cấm 569.269 → 569.243 (+11, −37). Giống `tu2t`, thêm 3 đoạn Huỳnh Văn Cù (107 m).
- View của cả hai ngày không còn orphan nào trong `report.json` (mục 8 có 17 rule).

Thay đổi ở mục 8 so với view trước nữa (ngày 22/10; số bị cấm và bị giới hạn không đổi):
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
| hcm_qd23_dau_keo_so_mi | 239.943 | container |
| hcm_qd23_xe_tai_nang | 239.943 | container, tu2t |
| hcm_qd23_xe_tai_nhe | 239.943 | duoi2t |
| hn_zone_ge2t_day | 184.826 | container, tu2t |
| hn_zone_lt2t_peak | 184.826 | duoi2t |
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

   **b) Một chiều của đường đôi bị sót** (orphan `"missing": "carriageway"`). Mục 8 phát hiện lỗi này ở 20 rule `cao`/`tb`. Mục 9 sửa `route_piece` để lấy hành lang 60 m cạnh đoạn tuyến (mục 2.3), nên **không còn rule nào lỗi**. Cột "Mục 8" là phần mục 8 báo bị sót; cột "Mục 9" là phần nay được lấy thêm (đo ngày 22/10/2026, cùng số cho ngày 1/10):

   | Rule | Đoạn tuyến | Mục 8 | Mục 9 |
   |---|---|---|---|
   | `hcm_hl_dang_kiem_5003s` | Phạm Văn Đồng, cầu Gò Dưa → Ngã Tư Bình Triệu | 8.412 m | **+8.275 m** (153 đoạn) |
   | | Phạm Văn Đồng, Bình Triệu → QL13 | 295 m | 0 (phần bị báo nằm ngoài đoạn, xem dưới) |
   | `na_vinh_pl1_18t` | V.I Lênin, Nguyễn Công Nghiệm → Nguyễn Sỹ Sách | 1.107 m | **+825 m** (8 đoạn) |
   | | Nguyễn Đức Cảnh (2 đoạn) | 89 m + 83 m | 0 |
   | `hn_zone_ge2t_day`, `hn_zone_lt2t_peak` | ngoại lệ Tam Trinh, Tân Mai → Đỗ Mười | 541 m | **+541 m** (7 đoạn, nay được miễn) |
   | `bd_6_tuyen_dau_keo`, `bd_6_tuyen_tren_10t` | Huỳnh Văn Cù từ Ngã tư Chợ Cây Dừa | 107 m | **+107 m** (3 đoạn) |
   | `na_vinh_pl2_4t` | Phùng Chí Kiên (2 đoạn); Nguyễn Công Trứ (2 đoạn) | 139 m + 80 m; 57 m + 129 m | 0 |
   | `qng_10t_6_18` | Phan Bội Châu (2 đoạn) | 76 m + 205 m | 0 |
   | `qng_5t_tonducthang_canngay` | Tôn Đức Thắng (2 đoạn) | 54 m + 65 m | 0 |
   | `qng_5t_6_18` | Quang Trung → Lê Lợi | 84 m | 0 |
   | `dni_dong_khoi_tren_2t`, `dni_dong_khoi_tren_5t` | Đồng Khởi, QL1 → Trương Định | 148 m | 0 |
   | `hcm_hl_pham_the_hien_ql50` | Phạm Thế Hiển, Trịnh Quang Nghị → cầu Bà Tàng | 100 m | 0 |
   | `dng_dt603b_hoian_10t`, `dng_dt603b_hoian_somi` | Võ Chí Công tới cầu Cửa Đại | 91 m | 0 (xem dưới) |
   | `hn_tu_lien_nghi_tam_yen_phu` (chỉ ngày 22/10) | Yên Phụ, Yên Hoa → Thanh Niên | 64 m | 0 |
   | `cm_bac_lieu_noi_o_tu_3_5t` | Cách Mạng (2 đoạn) | 63 m + 47 m | 0 |
   | `ct_5_truc_noi_o_tren_3_5t` | Nguyễn Văn Cừ, Mậu Thân → CMT8 | 52 m | 0 |
   | `bd_dong_hoa_dau_keo`, `bd_dong_hoa_tren_10t` | Mỹ Phước – Tân Vạn, ĐT743A → QL1 | 43 m | 0 |

   - **Phần lấy thêm là chiều đường còn lại thật**, đã xem trên bản đồ từng trường hợp lớn.
     - Phạm Văn Đồng: chiều còn lại và hai đường song hành cùng tên, song song suốt 3 km. Đây là hành lang mở (`action: open`, 9h–16h cho xe chở trên 2,5 t). Mục 9 mở thêm 148 đoạn chiều kia, nhưng **chưa đủ để đi theo chiều QL13 → QL1** (mục 9.1 đính chính): còn hở 20,7 m trunk không tên và 279 m làn song hành. Sau mục 9.1, hành lang đi được cả hai chiều.
     - V.I Lênin: chiều kia của đường đôi, cách 15–20 m.
     - Tam Trinh: làn song song cách khoảng 15 m.
     - Huỳnh Văn Cù: một làn cùng tên dài 107 m.
   - **Mười bốn rule báo ở mục 8 không có gì để lấy thêm**, cùng hai đoạn trong các rule ở trên (Nguyễn Đức Cảnh, Phạm Văn Đồng Bình Triệu → QL13). **Đính chính (mục 9.1):** điều dưới đây đúng với 12 rule. Hai rule Võ Chí Công là mất đường thật, và đoạn Phạm Văn Đồng Bình Triệu → QL13 không đánh giá được vì suy biến. Với 12 rule kia, phần bị báo không phải chiều đường bị sót. Đó là đường cùng tên **bên kia đầu mút**: đoạn kế tiếp của chính rule đó (Nguyễn Đức Cảnh, Phùng Chí Kiên, Nguyễn Công Trứ, Phan Bội Châu, Tôn Đức Thắng, Cách Mạng có hai đoạn nối nhau và báo lẫn nhau), hoặc phần đường tiếp sau một nút hay vòng xuyến (Đồng Khởi, Yên Phụ, Phạm Thế Hiển, Nguyễn Văn Cừ, Mỹ Phước – Tân Vạn, Quang Trung, Võ Chí Công). Phép kiểm ở mục 8 không biết đầu mút ở đâu nên báo nhầm. Định nghĩa mới chỉ tính điểm có chân vuông góc nằm giữa hai đầu mút, nên các trường hợp này hết báo mà đoạn tuyến không đổi.
   - **Đoạn ranh giới vùng**: chiều còn lại nay cũng là ranh giới. Nguyễn Văn Linh +2.648 m và +2.609 m; Võ Nguyên Giáp +5.059 m, +375 m, +341 m; Mai Chí Thọ +532 m. CT.37 (Hà Nội) và Nguyễn Thị Định không thêm gì. Vùng TP.HCM bớt 34 đoạn sát ranh giới, diện tích giảm 0,04 km²; vùng Hà Nội không đổi (mục 2.4).
   - Ba đoạn khác không có orphan cũng lấy thêm làn cùng tên chạy song song mà trước đây bị dung sai 1,1·D + 200 m cắt: Võ Văn Kiệt (QL1 → cầu Nước Lên) +327 m, Tôn Thất Thuyết +182 m, Võ Văn Ngân +139 m. Cả ba đều là hành lang mở ở TP.HCM.
   - **Còn lại, cần người dùng xem:**
     - **(Đã sửa ở vòng sửa 2, xem dưới.)** Võ Chí Công tới cầu Cửa Đại (`dng_dt603b_*`): đoạn tuyến một phía dừng ở chỗ cả hai chiều bị cắt, nên chỉ phủ 783 m gần cầu. Phần Võ Chí Công phía bắc chỗ cắt (khoảng 2,4 km mỗi chiều, tới QL14H) không thuộc rule. Mục 9 tả nhầm chỗ cắt là đoạn nối không tên: thật ra là hai đoạn khoảng 30 m mang tên "Đường Ven Biển 129 (Võ Chí Công)". Từ mục 9.1, `rules-check` báo orphan `gap` tại chỗ cắt và hai rule này lỗi. Muốn phủ thì thêm vào seed một đoạn cho phần phía bắc, hoặc thêm tên "Đường Ven Biển 129 (Võ Chí Công)".
     - Phạm Văn Đồng, Bình Triệu → QL13 (`hcm_hl_dang_kiem_5003s`): hai đầu mút cùng nằm ở nút Bình Triệu, nên đoạn tuyến chỉ còn 465 m nhánh nối quanh nút. Phạm Văn Đồng hai bên nút nằm ngoài đoạn. Cần xem lại đầu mút của đoạn này trong seed. Văn bản ghi "QL1 – Phạm Văn Đồng – QL13", và vị trí trung tâm đăng kiểm trên QL13 chưa biết.
     - Không có cầu mang tên riêng nào được lấy vào trên bản đồ thật: không đoạn tuyến nào có chiều đường bị một cây cầu như vậy cắt giữa hai đầu mút. Một đường song song khác tên cạnh Huỳnh Văn Cù ("Nguyễn Văn Lộng" và một nhánh không tên, khoảng 580 m) không được lấy, vì không phải cầu.
   - **Sau mục 9.1** (so với mục 9, đo ngày 22/10/2026; trên 185 đoạn tuyến, 7 đoạn lấy thêm tổng cộng 10.722 m, không đoạn nào mất đoạn đường nào):

     | Rule | Đoạn tuyến | Thay đổi | Cách lấp |
     |---|---|---|---|
     | `hcm_hl_dang_kiem_5003s` | Phạm Văn Đồng, cầu Gò Dưa → Ngã Tư Bình Triệu | +336 m (40 đoạn) | đoạn cùng tên lượn ra ngoài 60 m (279 m); đường nối không tên (58 m, gồm way 1219668784) |
     | `qng_5t_6_18` | Quang Trung → Lê Lợi | +226 m (10 đoạn) | vượt vòng xuyến Hai Bà Trưng (cung 25 m); 201 m bên kia **thừa** so với văn bản |
     | `hp_dt390_4truc_gio_cao_diem` | ĐT.390 tới Tỉnh lộ 360 | +4.604 m (94 đoạn) | vượt chỗ OSM đứt (cung vòng xuyến 82 m), rồi chạy tiếp qua nút Ba Hàng (QL5) tới hết đường; phần phía tây bắc QL5 **thừa** so với văn bản |
     | `hcm_qd23_*` (ranh giới) | Võ Nguyên Giáp từ Xa lộ Hà Nội | +5.305 m (122 đoạn) | vượt một chỗ cắt ở nút (đường nối không tên), đường chạy tiếp khoảng 1,2 km × 4 làn |
     | `hcm_qd23_*` (ranh giới) | Võ Nguyên Giáp, cầu Rạch Chiếc → Tây Hòa / → Mai Chí Thọ; Mai Chí Thọ, Võ Nguyên Giáp → Đồng Văn Cống | +69 m, +37 m, +144 m | đường nối không tên trong chiều đường |
     | `dng_dt603b_*` | Võ Chí Công tới cầu Cửa Đại | 0, orphan `gap` | chỗ cắt mang tên "Đường Ven Biển 129 (Võ Chí Công)", không vượt được |
     | `hcm_hl_kcn_tan_binh` | Lê Trọng Tấn từ QL1 | 0, orphan `gap` | chỗ cắt là đường ngang có tên ("Đường CN1"); Lê Trọng Tấn còn khoảng 2,8–3,0 km mỗi chiều phía sau |

     - Vùng TP.HCM và Hà Nội không đổi đoạn nào: phần ranh giới lấy thêm nằm trên chính đường ranh giới.
     - **Hai rule nay phủ thừa** vì seed dựa vào chỗ OSM dừng làm đầu mút. Quy tắc mới (vượt chỗ cắt nếu đường cùng tên còn tiếp tục trong 300 m) đã đi qua chỗ dừng đó. Cách sửa đúng là ghi đủ đầu mút trong seed:
       - `qng_5t_6_18`: thêm `"from": "Hai Bà Trưng"` cho Quang Trung.
       - `hp_dt390_4truc_gio_cao_diem`: thêm đầu mút ở QL5. QL5 chỉ có ref nên không dùng được làm `from` theo tên; cần một tên giao cắt có trong OSM.
     - **Phần lố ở đầu đoạn.** Một đoạn đường cùng tên có ít nhất một nửa nằm cạnh đoạn tuyến được lấy cả đoạn, kể cả phần chìa ra ngoài đầu mút. Ví dụ: một đoạn V.I Lênin dài 300 m, 78% nằm trong, chìa ra ngoài khoảng 66 m. Phần lố này nằm trong dung sai mà đoạn tuyến vốn có ở mỗi đường giao: tối đa 0,05·D + 100 m.
     - Đoạn dưới một nửa nằm cạnh thì bị bỏ, nên ở đầu đoạn có thể hở tối đa nửa đoạn.
   - **Sau vòng sửa 2** (cuối mục 9.1). Seed sửa theo văn bản và file nghiên cứu; mỗi thay đổi có ghi chú trong `note` của rule:

     | Rule | Sửa | Căn cứ | Kết quả trên bản đồ thật |
     |---|---|---|---|
     | `hp_dt390_4truc_gio_cao_diem` | `{"ref": "ĐT.390", "from": "Quốc lộ 5", "to": "Tỉnh lộ 360"}` | research_north_v2: "chiều QL5 → cầu Quang Thanh" | 26,84 → 25,00 km, không orphan. Đoạn chạy từ nút Ba Hàng tới hết cầu Quang Thanh, gồm cả 360 m trước đây chưa phủ. |
     | `dng_dt603b_hoian_10t`, `dng_dt603b_hoian_somi` | đoạn Võ Chí Công thay bằng `{"ref": "ĐT619"}` | tên văn bản: "ĐT619 (Võ Chí Công)"; research_central_v2: "ĐT603B và Võ Chí Công đến cầu Cửa Đại" | 0,78 km → 12,36 km, hết orphan `gap` |

     - **ĐT.390.** Các đường mang `ref=QL.5` vượt cấp qua ĐT.390 ở nút Ba Hàng, không chung nút, nên `"from": "QL5"` cho orphan `from`. Các đường mang tên "Quốc lộ 5" chung 3 nút với ĐT.390 tại nút, nên seed dùng tên này. Chỗ OSM đứt khoảng 100 m phía nam nút được vượt bằng đường nối không tên (quy tắc mới, mục 2.3). So với mục 9.1, đoạn tuyến bớt 26 đoạn (1.845 m) phía tây bắc QL5. So với mục 9, thêm 2.759 m: 2.103 m ĐT.390, 486 m cầu vượt nút giao Ba Hàng và 171 m cung vòng xuyến.
     - **Võ Chí Công.** Trong OSM, Võ Chí Công đứt ở cầu Đế Võng (259 m), ở các nút giao và ở hai đoạn khoảng 30 m mang tên "Đường Ven Biển 129 (Võ Chí Công)". Mọi đoạn này đều mang ref ĐT.619. Trong `bbox`, ĐT.619 chạy đúng từ chỗ nối ĐT603B (đầu Võ Chí Công, 15,9029; 108,3531) tới hết cầu Cửa Đại: Võ Chí Công 8,8 km, cầu Cửa Đại 3,0 km, cầu Đế Võng 0,5 km, "Đường Ven Biển 129" 61 m. Võ Chí Công không dừng ở QL14H như mục 9.1 ghi: nó cắt QL14H (15,8916; 108,3598) rồi chạy tiếp 1,6 km lên phía bắc tới ĐT603B. Văn bản tính từ đầu Võ Chí Công nên phần này cũng thuộc rule.
   - **Cần người dùng xác nhận (seed giữ nguyên):**
     - **Quang Trung (`qng_5t_6_18`).** Ghi chú của seed (chép TB 485) nêu "Quang Trung (Hai Bà Trưng – ranh giới Cẩm Thành)". Các file nghiên cứu chỉ liệt kê tên Quang Trung, không nêu đầu mút. Thêm `"from": "Hai Bà Trưng"` cho orphan `from`: trong OSM, Hai Bà Trưng dừng cách Quang Trung 4 m ở vòng xuyến, không chung nút. Seed giữ nguyên, nên vẫn cấm thừa 201 m Quang Trung bên kia vòng xuyến và 25 m cung vòng xuyến. Muốn bỏ phần này thì cần nối Hai Bà Trưng vào vòng xuyến trong OSM, hoặc chấp nhận phần thừa.
     - **(Đã quyết định 22/10/2026: người dùng chọn lựa chọn (1), lấy cả Lê Trọng Tấn trong `bbox`; seed đã sửa, ghi chú trong `note`. Rule nay 274 đoạn, không orphan.)** **Lê Trọng Tấn (`hcm_hl_kcn_tan_binh`).** Văn bản ghi "Lê Trọng Tấn (QL1 → KCN Tân Bình)", không nêu đường nào là đầu mút. "Tới cuối đường" trong ghi chú seed là cách đọc của người lập seed. "Đường CN1" (đường nội bộ KCN) cắt Lê Trọng Tấn ở 10,8128; 106,6079, cách QL1 khoảng 1 km. Hai lựa chọn:
       - (1) cả Lê Trọng Tấn trong `bbox` (`{"name": "Lê Trọng Tấn"}`): 7,45 km, không orphan, đúng ghi chú "từ QL1 đến cuối đường";
       - (2) chỉ QL1 → cổng KCN ở "Đường CN1": `"to": "Đường CN1"` cho orphan `path`, vì CN1 là đường có tên cắt ngang nên không vượt được. Giữ seed hiện tại thì được 1,67 km kèm orphan `gap` ở CN1.

       Rule này lỗi ở `rules-check` cho tới khi người dùng chọn; sau khi sửa theo lựa chọn (1), cả hai ngày 0 lỗi.
     - **ĐT.390 còn chạy lố khoảng 1,1 km phía tây bắc QL5** (22 đoạn, 1.076 m ĐT.390). Đây không phải phần vượt chỗ cắt, mà là dung sai mọi đoạn tuyến hai đầu có ở mỗi đường giao: tối đa 0,05·D + 100 m, với D khoảng 22 km. Muốn bỏ thì phải sửa phép tìm đoạn để cắt các nút đã qua đường giao được ghi, mà không cắt chiều đường dài hơn ở gần đầu mút. Việc này đổi nhiều đoạn tuyến nên chưa làm.

## 6. Lưu ý và rủi ro còn lại

- **Đầu mút bị bỏ có thể cấm dài hơn văn bản.** Ở Vinh, Quảng Ngãi và Bắc Giang, nhiều đoạn chạy tới đầu mút của đường trong OSM.
  - Đã kiểm tra hướng bằng điểm neo.
  - Độ dài thực của đoạn bị cấm có thể vượt mốc trong văn bản, ví dụ Nguyễn Gia Thiều, Lê Huân, Lục Niên, Ngô Gia Tự.
- **Phần chưa phủ:**
  - V.I Lênin: khoảng 400 m sát Đảo Hải Quan.
  - Lê Mao: khoảng 380 m từ ngã 5 Bưu Điện tới Ngư Hải.
  - ĐT.390: khoảng 360 m từ nút QL5 tới chỗ OSM đứt. Đã phủ từ vòng sửa 2 (cuối mục 9.1).
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

## 9. Vòng sửa `route_piece` (22/10/2026)

Vấn đề: ở mục 8, `rules-check` báo 20 rule `cao`/`tb` lỗi vì orphan `carriageway`. `route_piece` chỉ lấy chiều đường nối được với đường giao, nên bỏ chiều còn lại khi chiều đó không có nút nào trong vòng 60 m quanh đường giao, hoặc bị một đoạn mang tên khác cắt rời.

Thay đổi code (gói `speedmatrix`, có test viết trước cho từng trường hợp):
- **Hành lang 60 m** (`_Corridor`): đoạn tuyến lấy thêm mọi đoạn đường cùng tên chạy cạnh nó giữa hai đầu mút, tức đoạn có ít nhất một nửa số điểm nằm cạnh. Việc lấy vào và orphan `carriageway` dùng chung một định nghĩa "nằm cạnh" (mục 2.3). Orphan chỉ còn là lưới an toàn.
- Không bao giờ lấy: đường cùng tên cách quá 60 m; phần đường cùng tên bên kia đầu mút, kể cả phần nằm cạnh chỗ đoạn tuyến tự chạy lố qua đường giao; đường cùng tên ở thị xã khác (ngoài `bbox`).
- **Cầu mang tên riêng** ("Cầu …", "Hầm …") được lấy khi nằm giữa hai phần của chiều đường được lấy vào. Lý do: chiếc cầu là một phần con đường xe đi. Nếu bỏ cầu, chỗ hở này vô hại với lệnh cấm (xe không tới được), nhưng với đoạn miễn thì nó cắt đứt hành lang được đi. Cầu phải chạy dọc đoạn tuyến (hai đầu cách nhau theo dọc đoạn ít nhất 0,8 chiều dài cầu) và lấp một chỗ hở mà chính con đường không nối được. Cầu vượt cắt ngang, đường song song khác tên, nhánh không tên và vòng xuyến không được lấy. Lần thử đầu cho phép mọi tên đã lấy nhầm 580 m đường "Nguyễn Văn Lộng" cạnh Huỳnh Văn Cù và một cung vòng xuyến 13 m ở Bắc Giang, nên mới giới hạn theo tên.
- **Đầu xa của đoạn tuyến một phía** gieo mọi nút trong vòng 60 m như ở đường giao. Trước đây Quang Trung (Quảng Ngãi) kết thúc bằng ngã ba chữ Y ở vòng xuyến, nên một nhánh nằm "giữa" đoạn và phần đường bên kia vòng xuyến bị báo nhầm.
- `query_bbox` tìm theo khóa float32. Khóa float64 làm sao chép cả mảng 26,7 triệu vĩ độ ở mỗi lần gọi, khoảng 50 ms. Kết quả giống hệt, có test ở biên.

Chạy lại trên dữ liệu thật (cùng `segments/816249187dfd` và `speeds/dba5f7c3e555`):
- `rules-check` ngày 22/10: 88 rule, 0 lỗi, 0 partial, exit 0 (12 s). Ngày 1/10: 84 rule, 0 lỗi, 0 partial, exit 0 (13 s). Cảnh báo của rule đang tắt không đổi.
- Trên 185 đoạn tuyến có `from`/`to` trong seed: orphan `carriageway` giảm từ 32 xuống 0; 13 đoạn được lấy thêm tổng cộng 21.962 m; không đoạn nào mất đoạn đường nào. Tính thêm hành lang, thời gian resolve cả 185 đoạn khoảng 3,5 s, phần lớn là lần dựng chỉ mục không gian đầu tiên.
- `groups` ngày 22/10 (57,0 s) và 1/10 (57,6 s), peak WS 3,46 GB, private 4,90 GB. Số đoạn và thay đổi ở mục 2.4; `report.json` của cả hai ngày không còn orphan.
- Phần còn lại cần người dùng xem: mục 5.6 b (Võ Chí Công bị một đoạn nối không tên cắt, đoạn Phạm Văn Đồng Bình Triệu → QL13 gần như suy biến).

### 9.1 Sửa vòng 1 sau review độc lập (22/10/2026)

Review độc lập chỉ ra ba chỗ mất đường mà mục 9 bỏ qua lặng lẽ:
- **I1.** Đoạn tuyến một phía dừng ở chỗ đường bị cắt (Võ Chí Công) mà không báo.
- **I2.** Chỗ hở trong một chiều đường không được lấp cũng không được báo. Hành lang mở Phạm Văn Đồng vì thế không đi được theo chiều QL13 → QL1.
- **I3.** Đầu xa của đoạn tuyến một phía gieo cả nút của chiều đường khác, làm chiều đó bị cắt ngắn.

Thay đổi code (commit `3ba5aff`, chỉ `speedmatrix/scopes.py` và `tests/test_sm_scopes.py`), quy tắc ở mục 2.3:
- **I3:** đầu xa chỉ gieo nút cùng thành phần liên thông. Có test: chiều bắc dài hơn giữ đủ 2.500 m.
- **I2:** chỗ hở được lấp bằng chính đường cùng tên, hoặc bằng đường nối không tên hay cầu/hầm dài tối đa 100 m; không lấp được thì báo orphan `gap` kèm tọa độ.
  - Bỏ điều kiện "cầu chạy dọc, không cắt ngang" (0,8 chiều dài). Thay vào đó, hai đầu phải cùng hướng xe chạy. Một cầu vượt nối hai làn ngược chiều vì thế không tạo chỗ hở.
  - Bỏ việc tìm ứng viên theo ô 500 m dọc đoạn tuyến, vốn phụ thuộc mật độ nút. Nay tìm quanh từng cặp đầu.
- **I1:** vượt chỗ cắt sau đầu xa bằng cùng quy tắc; không vượt được thì báo orphan `gap`.
- **Tên cầu:** chỉ tên bắt đầu bằng "Cầu "/"Hầm " (không qua `normalize_name`, nên "Đường Cầu Giấy" không phải cầu). Thẻ OSM `bridge`/`tunnel` được ưu tiên nếu bảng đoạn có cột theo way; bảng hiện tại chưa có.
- **Vùng:** chấp nhận orphan `gap` của đoạn ranh giới như orphan `carriageway`.

Test: 52 test trong `test_sm_scopes` (thêm 9), toàn bộ 405 test đạt.
- Mỗi kiểu mất đường có test: cắt ở nút không tên, cắt bởi đường có tên, chỗ hở do đường dài hơn 100 m hay đường có tên, chiều đường lượn ra ngoài 60 m, cung vòng xuyến ra ngoài hành lang, chiều đường dài hơn, vùng chấp nhận orphan `gap`.
- Mỗi thay đổi có một đột biến làm ít nhất một test hỏng.

Chạy lại trên dữ liệu thật (cùng `segments/816249187dfd` và `speeds/dba5f7c3e555`):
- `rules-check` ngày 22/10: 88 rule, 3 lỗi, 3 partial, exit 2 (10 s). Ngày 1/10: 84 rule, 3 lỗi, 3 partial, exit 2 (11 s). Ba rule lỗi là `dng_dt603b_hoian_10t`, `dng_dt603b_hoian_somi` và `hcm_hl_kcn_tan_binh`. Cả ba lỗi vì đường thật bị mất và cần sửa seed (mục 2.3, 5.6 b).
- Hành lang Phạm Văn Đồng: trong tập được mở, nay đi được cả QL1 → QL13 và QL13 → QL1. Ở mục 9 chỉ đi được QL1 → QL13.
- `groups` ngày 22/10 (48,4 s) và 1/10 (45,3 s), peak WS 3,46 GB, private 4,90 GB. Thay đổi ở mục 2.4.
- Vùng: mọi vùng giữ nguyên từng đoạn và diện tích.

#### Vòng sửa 2: đường giao theo ref, sửa seed (22/10/2026)

Yêu cầu:
- (a) Đường giao ghi bằng ref ("QL5", "ĐT390") khớp theo ref như tên. Phần vượt chỗ cắt phải dừng ở đường giao được ghi khi đã tìm thấy nó. Nhờ đó `hp_dt390_4truc_gio_cao_diem` thôi lấy 4,6 km phía tây bắc QL5.
- (b) Sửa seed theo văn bản và file nghiên cứu, không tự thêm đường hay đầu mút.

Thay đổi code (commit `c0cb4de`, `speedmatrix/scopes.py` và `tests/test_sm_scopes.py`):
- **Đường giao theo ref** vốn đã khớp: `from`/`to` đi qua `ways_by_text`, so cả tên chuẩn hóa lẫn từng phần ref chuẩn hóa. Hai test mới chốt lại:
  - "QL77" tìm được đường chỉ mang `ref=QL.77`;
  - ref không đường nào mang ("QL78") là orphan `{"missing": "to"}`, không chạy tiếp tới hết đường.
- **Vượt chỗ cắt giữa hai đường giao** (`_join_crossings`). Khi hai đường giao nằm trên hai phần bị cắt rời, chỗ cắt được vượt bằng `_connector` như ở đầu xa của đoạn tuyến một phía. Sau đó `_piece` chạy lại, và đoạn tuyến là phần giữa hai đường giao, nên dừng ở đường giao được ghi. Không vượt được thì vẫn là orphan `path`. Trước đây trường hợp này luôn là `path`, nên seed ĐT.390 phải bỏ đầu QL5 và đoạn tuyến một phía chạy lố qua nút tới hết đường.
- Test: fixture T, "Đại lộ T" từ "Phố T1" tới đường chỉ có ref "QL.77", bị cắt bởi một nút không tên 40 m; đường chạy tiếp 1 km sau QL.77. Đoạn tuyến vượt chỗ cắt và dừng ở QL.77. Bỏ `_join_crossings` thì test này hỏng.

Sửa seed (`north.json`, `central.json`; giữ định dạng từng file, ghi chú trong `note`): xem mục 5.6 b. `qng_5t_6_18` và `hcm_hl_kcn_tan_binh` giữ nguyên, chờ người dùng xác nhận. Chạy `import-rules` cho hai file đã sửa: 3 rule đổi.

Test: 54 test trong `test_sm_scopes` (thêm 2), toàn bộ 407 test đạt.

Chạy lại trên dữ liệu thật (cùng `segments/816249187dfd` và `speeds/dba5f7c3e555`):
- `rules-check` ngày 22/10: 88 rule, 1 lỗi, 1 partial, exit 2 (11 s). Ngày 1/10: 84 rule, 1 lỗi, 1 partial, exit 2 (11 s). Rule lỗi duy nhất là `hcm_hl_kcn_tan_binh` (mục 5.6 b).
- `groups` ngày 22/10 (42,5 s) và 1/10 (42,0 s), peak WS 3,46 GB, private 4,90 GB, chạy lần lượt. Lần chạy đầu cho ngày 22/10 hỏng vì máy hết bộ nhớ ảo (các tiến trình Python khác chiếm gần hết); lần chạy lại chờ tới khi đủ bộ nhớ. Thay đổi ở mục 2.4: chỉ nhóm `container` đổi.
- ĐT.390 nay bắt đầu ở nút Ba Hàng. Phần vượt tới hết đường đã bỏ (−1.845 m); còn khoảng 1,1 km chạy lố do dung sai của phép tìm đoạn (mục 5.6 b).

**Đầu mút Lê Trọng Tấn (22/10/2026).** Người dùng chọn lấy cả Lê Trọng Tấn trong `bbox` (7,45 km). `south.json` thay đoạn `{"name": "Lê Trọng Tấn", "from": "QL1"}` bằng `{"name": "Lê Trọng Tấn"}`, kèm ghi chú (commit `efaa9a5`). Sau `import-rules` (1 rule đổi), `rules-check` cả hai ngày 0 lỗi, 0 partial, exit 0. Hai view được dựng lại lần lượt: số đoạn không đổi, 164 đoạn Lê Trọng Tấn được mở theo giờ hành lang cho `tu2t` và `container` (mục 2.4).
