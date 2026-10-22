# Spec: hệ thống bản đồ, tốc độ và ma trận thời gian theo giờ cho xe tải

Ngày: 2026-10-21. Trạng thái: chờ duyệt.

## 1. Bối cảnh và mục tiêu

Công ty logistics cần ma trận thời gian (TM) và quãng đường (DM) cho bộ giải VRP **phụ thuộc thời gian** mà người dùng đang tự viết. Xe là xe tải nhiều cỡ, mỗi cỡ có luật cấm riêng. Hệ thống phải làm được 5 việc:

1. Xem và sửa tốc độ từng đường trên bản đồ: sửa tay từng đoạn, hoặc tăng giảm theo % trong một vùng vẽ.
2. Cập nhật bản đồ từ Geofabrik, kèm cập nhật gate.
3. Tính lại TM/DM.
4. Nạp nguồn tốc độ mới (TomTom và các nguồn sau này).
5. Nội suy tốc độ còn thiếu theo giờ, theo khu vực và theo loại đường.

Dữ liệu GPS của công ty chưa đưa vào.

**Thành công khi:**
- Bộ giải gửi danh sách điểm, nhóm xe và loại ngày, rồi nhận về **hàm thời gian đi theo giờ xuất phát** cho từng cặp điểm.
  - Chuyến dài phải tính theo tốc độ của lúc xe tới từng đoạn, không theo tốc độ của khung giờ xuất phát.
  - Kết quả bảo đảm FIFO.
- Mọi kết quả truy được về phiên bản bản đồ, phiên bản tốc độ, bộ luật và mã nguồn đã sinh ra nó.
- Mọi bản dựng đều qua bước kiểm chứng tự động. Bản dựng không qua kiểm chứng thì không được đưa vào dùng.
- Không bước nào cần LLM hay cần người sửa tay dữ liệu. Mỗi sửa đổi là một luật chạy lại được.

## 2. Phạm vi

**Trong phạm vi**
- Đồ thị đường có hướng, có chi phí rẽ, lấy từ bộ OSRM dựng bằng profile ma trận.
- Phân tầng k cấp bằng min-cut, mặc định k = 3, gồm 22 vùng, khoảng 1.000 vùng con và khoảng 14.000 ô.
- Lớp tốc độ cơ sở cho từng đoạn, theo 3 loại ngày và 48 khung giờ, có phiên bản.
- Nhóm xe: một xe cơ sở có đủ dữ liệu, các nhóm khác chỉ lưu phần khác biệt.
- Tính ma trận phụ thuộc thời gian, trả qua API và xuất ra file.
- Giao diện xem và sửa tốc độ, có lịch sử và hoàn tác.
- Kiểm chứng bắt buộc ở mỗi bản dựng.

**Ngoài phạm vi (lần này)**
- Dữ liệu GPS công ty.
- Loại ngày Tết. Sẽ là loại ngày thứ 4, làm sau.
- Giấy phép vào vùng cấm nội đô ban ngày. Công ty chưa có giấy phép này (2026-10-21).
- Xe nặng chạy chậm ở đèo dốc. Xem mục 15.
- Đọc nội dung công văn PDF của Sở Xây dựng để biến thành luật. Không có LLM thì chỉ phát hiện được và báo, không đọc được nội dung.
- Thay OSRM 5000 của web app hiện tại. OSRM đó vẫn chạy như cũ. Hệ thống mới dùng bộ OSRM ma trận riêng.

**Tách thành các dự án con.** Mỗi dự án con có kế hoạch riêng (plan → làm → kiểm thử). Spec này là bản thiết kế chung cho tất cả.

| # | Dự án con | Phụ thuộc |
|---|---|---|
| 0 | Lớp tốc độ, phiên bản, nhóm xe và luật cấm | – |
| 1 | Cập nhật bản đồ và gate (k tầng, đồ thị có hướng) | 0 (cần định danh đoạn) |
| 2 | Customize và ma trận phụ thuộc thời gian, API, xuất file, kiểm chứng | 0, 1 |
| 3 | Giao diện xem và sửa | 0, 2 (xem trước ảnh hưởng) |
| 4 | Nạp nguồn tốc độ mới | 0 |
| 5 | Nội suy tốc độ còn thiếu | 0, 4 |

## 3. Định nghĩa

- **Đoạn (segment):** một đoạn đường có hướng giữa hai nút liên tiếp của đồ thị OSRM. Hai chiều của cùng một đường là hai đoạn.
- **Cung rẽ:** cung nối đoạn vào với đoạn ra tại một nút giao. Thời gian của cung là chi phí rẽ của OSRM. Lối rẽ bị cấm thì không có cung.
- **Tầng và ô:**
  - Tầng 1 là 22 vùng.
  - Tầng 2 là các vùng con.
  - Tầng 3 là các ô, mỗi ô khoảng 300 nút giao.
  - Các tầng lồng nhau: mỗi ô nằm trọn trong một vùng con, mỗi vùng con nằm trọn trong một vùng.
  - Trong code, tầng đánh số từ 0 đến k − 1, với k = 3.
- **Gate tầng ℓ:** một cung rẽ có đoạn vào và đoạn ra thuộc hai ô khác nhau của tầng ℓ.
  - Mỗi ô có **cửa vào**: các đoạn ra của gate đi vào ô.
  - Mỗi ô có **cửa ra**: các đoạn vào của gate rời khỏi ô.
  - Gate của tầng thô cũng là gate của mọi tầng mịn hơn.
- **Ô của một đoạn:** đoạn a→b thuộc ô của nút đầu a. Phân vùng được tính trên đồ thị nút (vô hướng) như trong spike, rồi chuyển sang đoạn theo luật này.
- **Mảnh (piece):** với một ô tầng 3, một nhóm xe, một loại ngày và một khung giờ, mảnh là bảng thời gian ngắn nhất từ mỗi cửa vào tới mỗi cửa ra, chỉ đi bên trong ô.
- **Khung giờ:** 48 khung, mỗi khung 30 phút. Khung i bắt đầu lúc s_i = 1800·i giây tính từ 00:00.
- **Loại ngày:**
  - T2–T6;
  - T7;
  - CN, gồm cả ngày lễ theo lịch ngày lễ.
- **Nhóm xe:** một tập thuộc tính xe gồm tổng trọng lượng, tải trọng cho phép, số trục, kiểu xe (thường, ben, đầu kéo, container) và chiều cao. Mỗi nhóm có một bộ luật đi kèm.
- **Hàm thời gian của một cặp điểm:** T(t) là thời gian đi khi xuất phát lúc t. Hàm này tuyến tính từng khúc, cho dưới dạng danh sách điểm gãy (t, T).
- **FIFO:** xuất phát muộn hơn thì không bao giờ tới sớm hơn. Viết thành công thức: T(t₂) ≥ T(t₁) − (t₂ − t₁) với mọi t₁ < t₂.

## 4. Kiến trúc (hướng A)

Hệ thống mở rộng app hiện có. FastAPI cung cấp backend, React/Leaflet làm giao diện, và job chạy dưới dạng tiến trình con.

**Vai trò của OSRM:**
- dựng đồ thị (bộ OSRM ma trận, profile ma trận);
- bắt điểm vào đường (`/nearest`);
- làm mốc kiểm chứng.

OSRM **không** tính ma trận. Bộ máy gate k tầng của hệ thống tự tính ma trận phụ thuộc thời gian.

**7 thành phần.** Mỗi thành phần là một module Python có giao diện rõ ràng và kiểm thử được riêng.

| Thành phần | Việc | Vào | Ra |
|---|---|---|---|
| `mapdata` | Lấy PBF từ Geofabrik, dựng OSRM bằng profile ma trận, đọc đồ thị có hướng và chi phí rẽ | PBF | `graph.npz`, metadata đoạn |
| `partition` | Chia k tầng bằng min-cut. Áp các luật: mỗi ô liền một khối, luật cao tốc, luật sông và phà | đồ thị | nhãn mỗi tầng, danh sách gate |
| `speeds` | Tốc độ xe cơ sở cho từng đoạn × loại ngày × khung giờ, có phiên bản | mẫu tốc độ, luật sửa | lớp tốc độ |
| `vehicles` | Thuộc tính xe, luật cấm 3 lớp, giới hạn tốc độ, phần khác biệt của từng nhóm | luật | tập đoạn bị đóng hoặc bị giới hạn, theo nhóm và khung giờ |
| `customize` | Dựng mảnh tầng 3, dùng chung mảnh giữa các khung và các nhóm khi giống nhau, dựng bảng tầng trên, kiểm chứng | đồ thị, nhãn, tốc độ, luật | kho mảnh, bảng tầng trên |
| `matrix` | Bắt điểm, tìm đường phụ thuộc thời gian trên lớp gate, tạo hàm điểm gãy có FIFO, phục vụ API và xuất file | điểm, nhóm, loại ngày | hàm TM/DM |
| `editor` (UI) | Xem tốc độ theo khung giờ, sửa tay, sửa % theo vùng, xem lịch sử, hoàn tác | – | luật sửa |

Ngoài 7 thành phần còn có một **bộ lập lịch job**. Bộ này giữ trạng thái job trong SQLite chứ không trong bộ nhớ như `JobManager` hiện tại. Nhờ vậy job dài sống sót khi khởi động lại.

**Ba nguyên tắc**
1. **Đồ thị có hướng và có chi phí rẽ, gate nằm trên cung.** Spike đã đo trên đồ thị vô hướng không có chi phí rẽ. Bản thật phải khớp với OSRM.
2. **Mọi sửa đổi là luật chạy lại được.** Điều này áp dụng cho tốc độ, luật cấm, gate, ranh giới và polygon. Không có thao tác nào sửa thẳng vào file kết quả.
3. **Mỗi bản dựng có phiên bản.** Mọi file mang hash của đầu vào. Kiểm chứng là bắt buộc. Lỗi thì không đưa vào dùng, và bản đang chạy giữ nguyên.

**Lưu trữ**
- Metadata nằm trong SQLite, ở `runtime/matrix/state.sqlite`.
- Mảng số lưu bằng numpy (`.npy`/`.npz`) hoặc Arrow trong thư mục bản dựng.
- Code viết bằng Python/numpy. Chỉ chuyển phần nóng sang Numba (hoặc Rust) khi đo thấy chậm.

**Máy chạy:**
- Ryzen 9 3900X, 32 GB RAM, ổ E: còn khoảng 64 GB.
- Không đường dẫn hay tham số nào được gắn chết vào máy này, để sau này chuyển lên server mà không phải viết lại.

## 5. Mô hình dữ liệu

### 5.1 Bản dựng bản đồ (`runtime/matrix/releases/<id>/`)

Đây là phần mở rộng của `update_matrix.py` hiện có.

| File | Nội dung |
|---|---|
| `osrm/` | Bộ OSRM ma trận (OSRM v26.10.0, giữ cố định phiên bản) |
| `graph.npz` | CSR của đồ thị cung rẽ. Với mỗi đoạn: nút đầu/cuối (id OSM), độ dài, loại đường (`road_class` 5 nhóm), way id và chi phí rẽ |
| `labels_l{1,2,3}.npy` | Ô của từng đoạn ở mỗi tầng |
| `gates.npz` | Gate mỗi tầng; cửa vào/cửa ra của mỗi ô |
| `release.json` | Hash của PBF, hash profile, phiên bản code, kết quả kiểm tra |

### 5.2 Lớp tốc độ (`runtime/matrix/speeds/<speed_version>/`)

- `edge_speed_<daytype>.npy`: tốc độ km/h dạng uint8 cho từng đoạn của xe cơ sở.
  - Đoạn trong **ô biến thiên** có 48 cột, mỗi khung giờ một cột.
  - Đoạn trong **ô tĩnh** chỉ có một cột. Xem mục 7.4.
- `provenance`: với mỗi đoạn, nguồn của tốc độ (nội suy từ mẫu nào, phương pháp nào) và các luật sửa đã áp. Dữ liệu này để UI hiện ra khi bấm vào đoạn.
- `speed_version` là hash của: tập mẫu, phương pháp nội suy, bộ luật sửa và bản dựng bản đồ.

### 5.3 Luật (bảng SQLite `rules`)

Mỗi luật là một dòng gồm:
- `id`, `kind`;
- `scope`: tập đoạn, polygon, loại đường, nhóm xe;
- `when`: loại ngày và khoảng khung giờ;
- `action`: đặt tốc độ, nhân %, giới hạn tốc độ hoặc đóng đường;
- `source`: tay, OSM, quyết định chính thức hay nguồn tự động;
- `provenance`, `author`, `created_at`, `active`.

Các loại luật:
- **Sửa tốc độ:** sửa tay từng đoạn, hoặc nhân % trong polygon.
- **Cấm và giới hạn của nhóm xe**, theo 3 lớp:
  1. quyết định chính thức, ví dụ Hà Nội QĐ 01/2026/QĐ-UBND với các ngưỡng < 2 t, 2–10 t và ≥ 10 t;
  2. thẻ OSM `hgv`, `maxweight(:conditional)`, `maxheight`;
  3. luật riêng của công ty.
- **Luật bản đồ** (gate, ranh giới): giữ dạng luật code như trong spike.

**Neo luật vào bản đồ.** Một luật theo đoạn được neo bằng (way id, cặp nút OSM) cùng hình học.
- Khi bản đồ cập nhật, luật nào không còn tìm thấy đoạn neo thì bị đánh dấu **mồ côi** và được báo ra, không bị xóa âm thầm.
- Luật theo polygon cộng loại đường không bị ảnh hưởng khi bản đồ đổi.

**Ứng viên biển báo và lệnh cấm.** Các ứng viên từ OSM, Mapillary, thông báo của Sở Xây dựng hay báo cáo của lái xe được **tự duyệt** (người dùng chọn, 2026-10-21). Các lớp bảo vệ đều tự động:
- ngưỡng xác nhận chéo giữa các nguồn;
- ghi lại nguồn gốc;
- hoàn tác bằng một lần bấm;
- cảnh báo khi ma trận thay đổi lớn.

### 5.4 Nhóm xe (bảng `vehicle_groups`)

- **Nhóm cơ sở** là xe ít bị hạn chế nhất (mặc định là xe < 2,5 t). Nhóm này có đầy đủ mảnh.
- Mỗi nhóm khác chỉ lưu:
  - thuộc tính xe;
  - bộ luật (giới hạn tốc độ theo loại đường, các lệnh cấm, các đoạn bị đóng);
  - danh sách **ô bị ảnh hưởng** và mảnh của riêng các ô đó.
- Khoảng 3 nhóm quanh Hà Nội: < 2 t, ≥ 2 t, container/đầu kéo.

### 5.5 Kho mảnh (`runtime/matrix/pieces/<release>/<group>/`)

- Mảnh tầng 3 được lưu dạng uint16 theo đơn vị **0,1 giây**, tối đa 6.553 s.
  - Một ô chỉ rộng khoảng 9 km², nên thời gian đi trong ô không thể vượt mức này. Nếu vượt thì báo lỗi.
  - Giá trị 65535 nghĩa là không đi được.
- Có chỉ mục `(cell, daytype, slot) → (file, offset, hash)`. Nhiều khóa trỏ chung một mảnh khi mảnh giống nhau:
  - ô tĩnh dùng một mảnh cho mọi khung giờ;
  - ô không bị ảnh hưởng thì nhóm xe dùng mảnh của nhóm cơ sở.
- Hash của mảnh tính từ hash đồ thị của ô, tốc độ các đoạn trong ô ở khung đó, và luật của nhóm áp lên ô. Hash không khớp nghĩa là mảnh **cũ**, phải dựng lại. Hệ thống không bao giờ đọc mảnh cũ.
- Bảng tầng trên (mục 9.2) là **bộ nhớ đệm dẫn xuất**. Có thể xóa và dựng lại từ mảnh. Chúng không phải nguồn sự thật.

## 6. Cập nhật bản đồ và gate (dự án con 1)

Phần này mở rộng `reports/gate_compose_spike/update_matrix.py`, chuyển vào code chính:

1. Lấy `.md5` của Geofabrik. Nếu trùng với bản đang chạy thì dừng, trừ khi có `--force`.
2. Tải PBF và kiểm tra md5.
3. Dựng OSRM bằng profile ma trận (`build_matrix_osrm.py`):
   - `weight = duration`;
   - chặn `access=private/permit/...`;
   - bỏ qua thẻ `construction` cũ.
4. Đọc đồ thị có hướng từ file OSRM: `.nbg_nodes`, `.geometry`, `.ebg`/`.ebg_nodes`, `.turn_duration_penalties`.
   - Đọc danh sách thành viên tar để xác nhận đúng tên khối và kiểu dữ liệu.
   - Gắn loại đường cho từng đoạn từ `road_classes.sqlite`. Hash PBF phải khớp.
5. Phân vùng:
   - **Tầng 1, tầng 2:** polygon v3.2 đã đóng băng. Gán lại nhãn bằng polygon trên bản đồ mới, không chia lại.
   - **Tầng 3:** chia lại chỉ những vùng con có đồ thị thay đổi, xác định bằng hash tập nút và tập cạnh của vùng con. Các vùng con khác giữ nhãn cũ, ghép theo id nút OSM.
   - **Min-cut ở mọi tầng**, không bao giờ chia đôi theo hình học, để số gate nhỏ nhất (người dùng yêu cầu).
   - Các luật phân vùng là luật code:
     - **mỗi ô liền một khối**: luật mới. Vùng 6 hiện là hai mảnh rời, phải sửa;
     - luật cao tốc;
     - luật sông và phà: phà là một đơn vị giữa hai bến.
6. Kiểm tra chất lượng (giữ các kiểm tra của `update_matrix.py`, thêm kiểm chứng ở mục 12):
   - không có gate nằm trên đường bị chặn;
   - snap gate ≤ 20 m ở tầng 1 và ≤ 5 m ở tầng 2;
   - mỗi phà có gate ở bến;
   - số vùng không đổi;
   - mọi ô liền một khối.
7. Gắn lại luật vào bản đồ mới (mục 5.3) và báo các luật mồ côi.
8. Dựng lại lớp tốc độ (mục 7) và mảnh (mục 9), chạy kiểm chứng (mục 12), rồi **đưa vào dùng**: `current.json` trỏ sang bản mới. Bản lỗi không làm thay đổi bản đang chạy.

Lịch chạy: hằng tuần, bằng Windows Scheduled Task, như `update_matrix.py schedule`.

## 7. Nguồn tốc độ và nội suy (dự án con 0, 4, 5)

### 7.1 Mẫu
Mỗi nguồn có một bộ chuyển đổi (adapter), ghi ra mẫu theo một định dạng chung:

`(road_class, lat, lon, daytype, slot, speed_kmh, source, observed_at, provenance)`

- TomTom dùng đường ống hiện có (`crawl_spatial.py`):
  - ảnh chụp được cache và không bao giờ tải lại;
  - khoảng thời gian nào có đoạn thuộc loại đường hỗn hợp hoặc không rõ thì bị loại.
- Thêm nguồn mới nghĩa là viết thêm một adapter. Không sửa bộ nội suy.
- Mẫu cũ đang theo ngày 0–6 được gộp thành 3 loại ngày. Ngày lễ dùng loại CN theo lịch ngày lễ.

### 7.2 Nội suy (giữ bất biến cốt lõi)
- Mỗi `(road_class, daytype, slot)` có một `SpeedField` (`model/spatial.py`). Phương pháp mặc định là phương pháp thắng trong `reports/spatial_comparison.json`.
- Tốc độ của một đoạn là giá trị của field **cùng loại đường** tại trung điểm đoạn.
- **Không bao giờ mượn tốc độ của loại đường khác.** Nếu một bộ `(road_class, daytype, slot)` không có mẫu nào, bản dựng tốc độ dừng với `SpatialDataError` và ghi rõ bộ đó. Hệ thống không âm thầm thay bằng giá trị khác.
- Tốc độ bị kẹp vào `[v_min, v_max]` của loại đường, cấu hình trong file. Mỗi lần kẹp đều được ghi lại.

### 7.3 Áp luật sửa
Thứ tự áp luật là cố định: nội suy → luật % theo vùng → luật sửa tay từng đoạn. Luật sửa tay thắng. Kết quả lưu kèm nguồn gốc (mục 5.2).

### 7.4 Ô biến thiên và ô tĩnh
- Một ô tầng 3 là **tĩnh** trong một loại ngày khi mọi đoạn của nó thỏa: với mọi khung giờ, |v_khung − v_trung vị| / v_trung vị ≤ `static_threshold`.
  - `static_threshold` mặc định là 5%, để cùng cỡ với sai số cho phép của API.
- Ô tĩnh dùng tốc độ trung vị cho mọi khung giờ, nên chỉ cần một mảnh.
- Ô không tĩnh là ô **biến thiên** và có mảnh cho từng khung giờ.
- Trong bán kính 30 km quanh Hà Nội, ước tính khoảng 400 trên 13,8 nghìn ô là ô biến thiên (< 3% cả nước).
- Bản dựng báo tỷ lệ ô tĩnh theo từng vùng.

## 8. Nhóm xe và luật cấm (dự án con 0)

**Tốc độ của nhóm g trên đoạn e lúc t:**

v_g(e, t) = min(v_cơ sở(e, t), cap_g(loại đường của e))

Lý do: lúc đường đông, mọi cỡ xe đều chạy theo dòng xe, nên chỉ giới hạn tốc độ pháp định của từng nhóm làm chúng khác nhau.

**Đóng đường:** đoạn e đóng với nhóm g trong khung i khi có một luật cấm của g phủ e trong khung i. Các luật cấm gồm:
- cấm theo giờ;
- `maxweight` nhỏ hơn trọng lượng xe;
- `maxheight` nhỏ hơn chiều cao xe;
- `hgv=no`;
- luật của công ty.

Đoạn đóng thì không có cung đi vào. Gate không đổi, vì bỏ cạnh không làm mất tính chất tách vùng của gate.

**Ô bị ảnh hưởng** của nhóm g là ô có ít nhất một đoạn mà ở một khung giờ nào đó tốc độ hoặc trạng thái đóng của g khác với **view của nhóm cơ sở** (không phải lớp tốc độ trần). Như vậy, một đoạn chỉ bị đóng với nhóm cơ sở cũng tính là bị ảnh hưởng, ví dụ Hải Phòng cấm xe ≤1,5 t mà không cấm xe lớn hơn. Riêng nhóm cơ sở thì so với lớp tốc độ trần. Chỉ những ô này được dựng mảnh riêng cho g. Các ô khác dùng mảnh của nhóm cơ sở. Bảng tầng trên được dựng lại cho từng nhóm. Việc này rẻ.

**Bắt điểm theo nhóm:** gọi `/nearest` với `number=5`, rồi lấy ứng viên đầu tiên có đoạn mở với nhóm. Không có ứng viên nào thì điểm đó lỗi (mục 13).

## 9. Customize k tầng (dự án con 2)

### 9.1 Mảnh tầng 3
- Với mỗi ô, mỗi nhóm cần dựng, mỗi loại ngày và mỗi khung giờ (hoặc một lần nếu ô tĩnh): chạy Dijkstra từ mỗi cửa vào trên đồ thị cung rẽ **bên trong ô**, dùng thời gian đoạn của khung đó.
- **Thời gian của một đoạn trong khung i:** τ_e,i = độ dài / v_g(e, s_i) + chi phí rẽ.
- **Mô hình thời gian trong một ô:** khi vào ô lúc t, với s_i ≤ t < s_i+1, thời gian đi qua ô là P_i + (t − s_i)/1800 · (P_i+1 − P_i). Ở đây P_i là giá trị của mảnh tại khung i.
  - Nếu một trong hai giá trị là "không đi được": dùng P_i, theo luật "tính đi được theo khung lúc vào".
  - Mô hình này coi tốc độ không đổi trong lúc xe đi qua một ô. Một ô chỉ mất vài phút để đi qua.
  - Sai số của giả định này được đo trong kiểm chứng (mục 12).
  - Nội suy tuyến tính giữa hai khung giữ FIFO khi |P_i+1 − P_i| ≤ 1800 s. Với ô cỡ 9 km² điều này luôn đúng. Nếu vi phạm thì bản dựng báo lỗi.
- Chạy song song theo ô, code vector hóa.

### 9.2 Bảng tầng trên (bộ nhớ đệm, phụ thuộc thời gian)
Trong mục này và mục 10, ℓ là chỉ số tầng trong code (0 = tầng 1 vùng, 1 = tầng 2 vùng con, 2 = tầng 3 ô).

- **Clique cục bộ tầng ℓ (ℓ = 0, 1):** thời gian giữa cửa vào và cửa ra của ô tầng ℓ, **chỉ đi bên trong ô**.
  - Tính từ dưới lên bằng tìm đường phụ thuộc thời gian trên lớp phủ của các ô con, với giờ xuất phát tại mỗi s_i.
  - Giữa hai mẫu thì nội suy tuyến tính. Hàm thật có FIFO, nên nội suy giữa các mẫu cũng giữ FIFO.
  - Lý do không dùng ảnh chụp tĩnh của từng khung: ở tầng trên, đi qua một ô có thể mất hàng giờ, nên ảnh chụp tĩnh sẽ sai đúng như điều người dùng đã bác bỏ.
- **Bảng thoát (tự đủ) tầng ℓ**, ký hiệu S_ℓ(c): thời gian **đúng** giữa các gate của ô c, được phép đi ra ngoài c rồi quay lại.
  - Tính từ trên xuống: S_0 tính từ lớp phủ toàn quốc; S_ℓ+1 tính trên lớp phủ của ô cha cộng S_ℓ(cha).
  - Đây là bản phụ thuộc thời gian của "bảng vùng tự đủ" mà người dùng yêu cầu. Nhờ nó, một cặp điểm cùng ô c chỉ cần đọc dữ liệu của c.
  - Bảng này đúng mà không cần ô lồi: phần đường đi ra ngoài c luôn là một đoạn gate→gate, và S đã tính sẵn giá của đoạn đó. Với FIFO, tới gate sớm nhất luôn là tốt nhất.
- **Lưu trữ:** mỗi mục là 48 mẫu cho mỗi loại ngày, đã bỏ bớt điểm gãy với sai số rất nhỏ (1 s). Các bảng này dẫn xuất từ mảnh, dựng lại theo từng nhóm.

### 9.3 Cập nhật cục bộ
Khi luật sửa hoặc nguồn tốc độ đổi:
- chỉ dựng lại mảnh của các ô có đoạn đổi tốc độ;
- rồi dựng lại clique của các ô tổ tiên;
- rồi dựng lại S từ trên xuống.

Một ô X chỉ phải dựng lại S nếu bảng thoát của ô cha của X thực sự thay đổi, theo hash.

## 10. Truy vấn phụ thuộc thời gian và API ma trận (dự án con 2)

### 10.1 Truy vấn một cặp (p, q), xuất phát lúc t
1. Bắt p và q vào đoạn mở với nhóm (mục 8). Mỗi điểm thành một đoạn kèm vị trí trên đoạn.
2. Tìm ℓ* là tầng sâu nhất mà p và q cùng ô c. Nếu khác vùng thì ℓ* = −1, phạm vi tìm là toàn quốc.
3. Dựng đồ thị tìm kiếm trong c:
   - chi tiết từng đoạn trong ô tầng 3 chứa p và ô tầng 3 chứa q;
   - với ô con không chứa p hay q, dùng clique của ô ở tầng thô nhất có thể;
   - thêm cung thoát S_ℓ*(c).
4. Chạy Dijkstra phụ thuộc thời gian (giờ tới sớm nhất) từ p lúc t.
   - Chạy một-tới-nhiều, dừng khi đã chốt hết các đích.
   - Mỗi cung đánh giá hàm của nó tại giờ tới.
   - Thời gian chạy trên đoạn chứa p và đoạn chứa q được tính theo phần đoạn thực đi.

Cách này giống TD-CRP (Baum và cộng sự, khoảng 2016), nhưng ở tầng 3 dùng mảnh là ảnh chụp từng khung nội suy theo thời gian.

### 10.2 Hàm thời gian và quãng đường của một cặp
1. Chạy truy vấn với t = s_0 … s_47 trong loại ngày được yêu cầu, thu được 48 mẫu (s_i, T_i, D_i).
   - Chuyến vượt quá 24:00 tiếp tục dùng tốc độ từ 00:00 của **cùng loại ngày**. Đây là giới hạn đã biết.
2. **Bao FIFO:** T(t) := min_{t' ≥ t} (t' − t + T(t')). Nghĩa là xe được phép chờ ở điểm xuất phát.
   - Bước này cần thiết vì khi có lệnh cấm theo giờ, đi muộn hơn có thể tới sớm hơn. Ví dụ: chờ hết giờ cấm rồi đi đường thẳng.
3. **Bỏ điểm gãy:** dùng kiểu Douglas–Peucker trên (s_i, T_i).
   - Một điểm bị bỏ khi đường nối hai điểm giữ lại lệch khỏi mọi mẫu bị bỏ không quá **max(30 s, 5% · T)**.
   - Dây cung của một hàm FIFO vẫn có độ dốc ≥ −1, nên kết quả vẫn FIFO.
   - Cặp điểm không đổi theo giờ chỉ còn một giá trị.
4. **Quãng đường** là hàm bậc thang theo giờ xuất phát, vì tuyến đường đổi theo giờ. Ở mỗi mẫu, đó là quãng đường của tuyến tìm được. Các bậc liền nhau bằng nhau thì gộp. Quãng đường không bỏ điểm theo sai số.

### 10.3 API
`POST /api/matrix`:

```
{ "points": [{"lat":..,"lon":..}, ...],
  "group": "base" | "<tên nhóm>",
  "daytype": "weekday" | "sat" | "sun"   (hoặc "date": "2026-10-21", hệ thống tự tra lịch ngày lễ),
  "sources": [i...], "targets": [j...]   (tùy chọn, mặc định N×N) }
```

Kết quả gồm mảng kiểu CSR, thứ tự cặp là hàng nguồn × cột đích:
- `t_offsets` [P+1], `t_times` [giây kể từ 00:00], `t_values` [giây], với P là số cặp;
- `d_offsets` [P+1], `d_times`, `d_values` [mét]: hàm bậc thang;
- `unreachable`: danh sách cặp không đi được, kèm mã lý do;
- `version`: release, speed_version, rules_version, group, code_version.

Định dạng gửi đi: JSON cho N nhỏ. Từ N > 200 trở lên dùng `application/vnd.apache.arrow.stream` hoặc `.npz`.

**Xuất file theo lô:**
- Job `matrix_export` với cùng đầu vào. Kết quả nằm ở `runtime/matrix/exports/<id>.npz` hoặc `.arrow`, kèm `version`.
- Trạng thái job lưu trong SQLite.

**Hợp đồng FIFO:** API bảo đảm T(t₂) ≥ T(t₁) − (t₂ − t₁) cho mọi cặp. Kiểm thử và kiểm chứng đều xác nhận điều này.

## 11. Giao diện xem và sửa (dự án con 3)

Giao diện là một chế độ mới trên bản đồ React/Leaflet hiện có.
- **Xem:**
  - tô màu từng đoạn theo tốc độ, chọn loại ngày, khung giờ và nhóm xe;
  - có thanh trượt khung giờ;
  - lớp phủ đánh dấu ô biến thiên và đường bị cấm theo nhóm.
- **Bấm vào một đoạn:** hiện tốc độ của 48 khung, nguồn gốc (mẫu nào, nội suy thế nào) và các luật đã áp.
- **Sửa tay:**
  - chọn một hay nhiều đoạn, đặt tốc độ hoặc %;
  - chọn loại ngày và khoảng khung giờ;
  - ghi lý do.
  - Mỗi lần sửa lưu thành một luật.
- **Sửa % theo vùng:** vẽ polygon, chọn loại đường, khoảng khung giờ và %. Lưu thành một luật.
- **Xem trước ảnh hưởng:** trước khi lưu, tính lại các mảnh bị ảnh hưởng và báo thay đổi thời gian trên một tập cặp điểm chuẩn (ví dụ các khách hàng mẫu ở Hà Nội).
- **Lịch sử:** danh sách luật có lọc. Có thể tắt hoặc hoàn tác từng luật. Mỗi lần thay đổi sinh một `speed_version` mới.
- Chuỗi hiển thị bằng tiếng Việt, code viết bằng tiếng Anh.

## 12. Kiểm chứng (bắt buộc ở mỗi bản dựng)

Mọi bước sau phải qua. Một bước không qua thì không đưa bản dựng vào dùng, và log ghi rõ cặp điểm và con số sai.

1. **Đồ thị khớp OSRM.**
   - Dùng `osrm-routed` riêng trên cổng riêng, không đụng OSRM 5000 của web app.
   - Ghi tốc độ của một khung mẫu thành `--segment-speed-file`, rồi chạy `osrm-customize` trên một **bản sao** của bộ OSRM ma trận.
   - So `/table` trên 500 điểm ngẫu nhiên (seed cố định) với bộ máy chạy ở chế độ tĩnh của khung đó.
   - Sai lệch phải ≤ max(2 s, 0,2%).
   - Bước này bắt được lỗi về hướng đoạn, chi phí rẽ, cạnh trùng hay gate không nằm trên cung.
2. **Phân tầng đúng.**
   - So bộ máy k tầng với Dijkstra phụ thuộc thời gian chạy thẳng trên toàn đồ thị cung rẽ (bản tham chiếu, viết đơn giản).
   - Dùng 2.000 cặp × 4 giờ xuất phát, phân tầng theo tầng chung sâu nhất.
   - Sai lệch phải ≤ max(5 s, 0,5%). Phần sai lệch này đến từ giả định "tốc độ không đổi trong một ô".
3. **Đầu ra API:** với mọi cặp của tập chuẩn, hàm đã nén lệch khỏi 48 mẫu không quá max(30 s, 5%), và FIFO đúng.
4. **Bảo toàn và thời gian ăn khớp:**
   - không có mảnh nào cũ theo hash;
   - mọi giá trị uint16 nằm trong khoảng cho phép;
   - không có NaN.
5. **Cảnh báo biến động** (cảnh báo, không chặn):
   - so với bản đang chạy trên tập cặp chuẩn;
   - cặp nào đổi > 20% thì vào báo cáo kèm luật hay nguồn gây ra.

## 13. Xử lý lỗi

- Thiếu dữ liệu thì báo lỗi, không lặng lẽ thay bằng giá trị khác. Dùng các ngoại lệ hiện có (`MissingRoadData`, `RoadClassError`, `RouteAlignmentError`, `SpatialDataError`) và thêm:
  - `SnapError`: điểm không bắt được vào đoạn mở với nhóm trong vòng 200 m;
  - `StalePieceError`;
  - `ReleaseMismatch`: hash bản dựng, tốc độ hoặc luật không khớp nhau.
- Backend ánh xạ tên lớp ngoại lệ sang thông báo 422 tiếng Việt, như `/api/estimate` đang làm. Vì vậy đổi tên lớp là làm hỏng ánh xạ.
- Điểm không bắt được vào đường: cả yêu cầu bị từ chối, trả về 422 kèm danh sách chỉ số các điểm lỗi.
- Cặp không đi được, ví dụ do bị cấm hay do đường một chiều không có lối ra, là **dữ liệu hợp lệ**: cặp đó nằm trong `unreachable` kèm lý do, không phải lỗi.
- Job lỗi:
  - trạng thái và log nằm trong SQLite;
  - bản đang chạy giữ nguyên;
  - lỗi không bao giờ chứa khóa API. Các đường lỗi tiếp tục nuốt nội dung ngoại lệ của urllib, như hiện nay.

## 14. Kiểm thử

- **Đơn vị**, chạy offline, không cần OSRM, mạng hay khóa API:
  - đọc file OSRM trên tar nhỏ tự dựng;
  - phân vùng trên đồ thị nhỏ: liền khối, min-cut, luật phà;
  - mảnh, clique và S trên đồ thị ngẫu nhiên nhỏ với k = 2 và k = 3, so với Dijkstra trực tiếp. Kiểm thử phải có trường hợp cạnh trùng giữa cạnh thô và cung tắt, vì spike đã từng sai 13,4 s ở chỗ này;
  - FIFO với cấm theo giờ, bao FIFO, bỏ điểm gãy, hàm bậc thang quãng đường;
  - luật: thứ tự áp, luật mồ côi, giới hạn tốc độ theo nhóm;
  - không mượn tốc độ khác loại đường, và thiếu mẫu thì báo lỗi.
- **Thuộc tính:** sinh ngẫu nhiên đồ thị, tốc độ theo khung và lệnh cấm, rồi kiểm tra:
  - FIFO luôn đúng ở đầu ra;
  - hàm đã nén nằm trong sai số cho phép;
  - kết quả phân tầng bằng kết quả tìm thẳng.
- **API:** dùng fake estimator và fake kho mảnh như `create_app(...)` đang làm. Kiểm tra định dạng CSR, mã lỗi tiếng Việt, và việc không lộ khóa.
- **Frontend:** vitest cho chế độ xem/sửa, lưu luật và hoàn tác.
- **Kiểm thử khói trên dữ liệu thật:** bỏ qua nếu không có file. Chạy bước 1 và bước 2 của mục 12 trên vùng 15 (Hà Nội).

## 15. Rủi ro và hạn chế đã biết

- **Spike đo trên đồ thị vô hướng, không có chi phí rẽ.** Bản thật có hướng nên số gate tăng (mỗi cạnh cắt thành hai chiều). Mọi con số ở mục 16 phải đo lại ở dự án con 1 và 2.
- **Đọc file nội bộ của OSRM** phụ thuộc phiên bản. Phải giữ cố định OSRM v26.10.0, và kiểm chứng bước 1 bắt được nếu đọc sai.
- **Giả định tốc độ không đổi khi đi qua một ô tầng 3.** Sai số được đo ở bước 2 của mục 12.
- **Xe nặng ở đèo dốc** chậm hơn dòng xe nhưng chưa được mô hình hóa.
- **Chuyến qua 24:00** dùng cùng loại ngày.
- **Lệnh cấm theo giờ** làm cung trong lúc tìm đường không còn FIFO. Kết quả tìm được vẫn là thời gian của một tuyến đi được thật, và đầu ra được làm FIFO bằng bao chờ ở điểm xuất phát.
- **Sai số 5% với chuyến rất dài:** 5% của 10 giờ là 30 phút. Xem câu hỏi mở 1.
- **Hiệu năng truy vấn chưa đo.** Mục tiêu tạm:
  - ma trận 500 điểm × 48 giờ xuất phát xong ≤ 10 phút;
  - customize đủ 3 loại ngày cho nhóm cơ sở xong ≤ 2 giờ trên 12 luồng;
  - tổng dung lượng ≤ 10 GB.
  - Đo ở bước đầu của kế hoạch dự án con 2. Không đạt thì chuyển phần nóng sang Numba trước, rồi mới xét đổi thiết kế.

## 16. Kết quả đã đo trong spike (đồ thị vô hướng, một khung giờ)

Thư mục: `reports/gate_compose_spike/`. Đồ thị là đồ thị ma trận đã co chuỗi bậc 2, có 4,30 triệu nút.

| Cấu hình | Ô | Nút biên (T1 / T2 / T3) | Dựng | Lưu (int32) | Sai lệch lớn nhất |
|---|---|---|---|---|---|
| k = 2 | 22 / 1.009 | 936 / 17.355 | 20 s (máy rảnh) | 87 MB | 5,7e-10 s / 1.680 cặp |
| k = 3, tầng 3 chia đôi theo hình học | +20,6 nghìn | – / – / 527 nghìn | 306 s | 2,4 GB | (lỗi cạnh trùng, đã sửa) |
| k = 3, tầng 3 min-cut | +13,8 nghìn | 936 / 17.355 / 103.595 | 170 s | 161 MB | 6,4e-10 s / 2.160 cặp |

- Chỉ lưu mảnh thay vì bảng lớn: vùng 15 giảm từ 2,63 triệu số xuống 62 nghìn số, nhỏ hơn khoảng 42 lần. Ghép lại bảng lớn mất khoảng 0,3 s.
- Chia tầng 3 bằng min-cut: 13.793 ô, 46.376 cạnh cắt, mất 80 phút với 6 worker.
- Kích thước ô tầng 3 (trung vị):

  | Khu vực | Diện tích/ô | Đường | Nút giao | Gate |
  |---|---|---|---|---|
  | Lõi Hà Nội | 3,7 km² | 61 km | ~850 | 16 |
  | Cả nước | 9,4 km² | 36 km | ~230 | 6 |

- Độ lồi (đã bỏ): khoảng 7% cặp điểm đi ra ngoài vùng của chính chúng. Thuật toán bao lồi tham lam gộp mất 57% đồ thị.

## 17. Câu hỏi mở

1. **Trần cho sai số chuyến dài:** giữ max(30 s, 5%), hay thêm trần thành max(30 s, min(5%, 10 phút))?
2. **`static_threshold`:** 5% (mặc định trong spec này) hay 10%? Ngưỡng 10% cho ít ô biến thiên hơn, nhưng sai số lớn hơn sai số cho phép của API.
3. **Nhóm cơ sở:** xe < 2,5 t có đúng là nhóm ít bị hạn chế nhất trên toàn quốc không?
