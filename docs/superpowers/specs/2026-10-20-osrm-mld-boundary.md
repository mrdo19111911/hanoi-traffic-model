# Spec: đo biên các tầng ô của OSRM MLD (đã duyệt 2026-10-20)

## Mục đích
Bước 4a của thí nghiệm gate cho G = 1.832. Đó là số gate cần để ranh giới `natural_spectral` (22 vùng con) chặn được cả mạng đường. OSRM MLD cũng chia đồ thị thành nhiều tầng ô, và biên mỗi ô là một separator thật của cả mạng. Câu hỏi đặt ra: với cùng số ô, biên do OSRM chọn nhỏ hơn hay lớn hơn 1.832?
- Nếu nhỏ hơn nhiều: nên dùng ô của OSRM làm cụm.
- Nếu không: giữ cách chia sông + spectral.

Bước này chỉ đo. Không đổi file cụm, thí nghiệm, backend hay frontend.

## Dữ liệu
Các file sau trong `osrm/data/`, dựng bằng OSRM v26.10.0 với `pbf_sha256` trong `osrm/build.json`:
- `vietnam-latest.osrm.partition`
- `vietnam-latest.osrm.cells`
- `vietnam-latest.osrm.ebg_nodes`

Mỗi file là một tar gồm các khối nhị phân có tên, ví dụ `/mld/multilevelpartition/...` và `/mld/cellstorage/...`. Phải đọc danh sách thành viên trong tar để xác nhận đúng tên và kiểu dữ liệu, không đoán.

**Lưu ý:** OSRM chia **đồ thị cạnh** (edge-based graph). Mỗi nút của đồ thị này là một đoạn đường **có hướng**, không phải một nút giao OSM. Vì vậy phải quy đổi trước khi so với G.

## Script mới `model/osrm_mld_boundary.py`
1. **Kiểm tra hash:** `pbf_sha256` của `osrm/build.json` phải khớp với metadata của `big_road_edges.sqlite`, giống các script khác. Không khớp thì dừng.
2. **Đọc phân vùng:** với mỗi tầng ℓ, lấy:
   - số ô;
   - mã ô của từng nút đồ thị cạnh;
   - số nút trong mỗi ô (nhỏ nhất, trung vị, lớn nhất).
3. **Đọc biên từ `.cells`:** với mỗi tầng, lấy tập nút biên nguồn và tập nút biên đích của mọi ô, rồi tính:
   - `boundary_ebg`: số nút đồ thị cạnh nằm ở biên, tức hợp của nguồn và đích, không trùng;
   - `sources`, `destinations` riêng từng loại;
   - phân bố số nút biên trên mỗi ô.
4. **Quy đổi sang điểm trên bản đồ**, để so được với G:
   - Mỗi nút biên được đổi thành đoạn đường OSM tương ứng: lấy từ `.ebg_nodes` và các file hình học liên quan, sao cho ra cặp node OSM hoặc tọa độ.
   - Hai chiều của cùng một đoạn tính là một.
   - `boundary_points`: số điểm sau khi gộp theo đúng luật của 4a, tức các điểm cách nhau dưới 100 m giữa cùng một cặp ô thì gộp làm một.
   - Nếu không đọc được hình học: ghi rõ lý do, chỉ báo `boundary_ebg`, kèm cận dưới `boundary_ebg / 2`.
5. **So sánh:** chọn tầng có số ô gần 22 nhất. Nếu không có tầng nào đủ gần, gộp các ô của tầng ngay dưới tầng có ít ô hơn 22. Gộp bằng cây cha con của OSRM, không gộp tự do.
   - Báo `boundary_points` của tầng đó, đặt cạnh G = 1.832 (4a), 590 (`natural_spectral` chỉ trên mạng A) và 350 (`natural`, 8 vùng).
   - Báo thêm hai con số với 8 ô (cùng cách lấy tầng) để so với `natural`.
6. **Ghi kết quả:** `reports/osrm_mld_boundary.json`, gồm từng tầng, phần so sánh ở bước 5, và metadata (pbf_sha256, phiên bản OSRM, thời gian chạy).
   - Thêm `reports/osrm_mld_cells.geojson`: nút biên của tầng được so sánh, mỗi nút là một điểm mang mã ô, để xem trên bản đồ sau này.
7. **Đo thêm `/table` với 2000 điểm:** đây là phép đo của thẻ đề xuất "OSRM customize / table timing" còn mở.
   - Gọi `/table` của OSRM cục bộ một lần với 2000 điểm. Điểm lấy ngẫu nhiên trên mạng (seed 0), có snap.
   - Ghi thời gian chạy, chia khối nếu OSRM giới hạn kích thước bảng.
   - Đặt cạnh thời gian tính bảng G×G 1.832 × 1.832 (110 s).

## Kiểm thử (TDD)
Kiểm thử chạy trên file tar nhỏ tự dựng, đúng tên và kiểu của các khối thật.
- Đọc đúng số tầng, số ô và mã ô của từng nút.
- Hợp biên nguồn và biên đích không đếm trùng.
- Hai chiều của cùng một đoạn tính là một điểm. Hai điểm cách nhau 50 m giữa cùng cặp ô thì gộp; cách nhau 150 m thì giữ cả hai.
- Chọn tầng gần 22 ô nhất. Trường hợp phải gộp ô theo cây cha con thì biên sau khi gộp bỏ đi các nút chỉ nằm giữa hai ô con của cùng một ô cha.
- `pbf_sha256` lệch thì dừng.
- File thiếu một khối thì báo lỗi kèm tên khối.

Ngoài ra, một bài kiểm thử khói (smoke test) trên file thật, bỏ qua nếu file không có: số nút đồ thị cạnh trong `.partition` phải bằng số trong `.ebg_nodes`.

## Kết quả (2026-10-20)
- Bốn tầng ô có số ô lần lượt 52.565 / 3.788 / 235 / 8.
- Tách ô thứ 7 của tầng 4 ra 19 ô con thì được 26 ô, có 765 điểm biên. Con số này chưa bằng nửa G = 1.832 của bước 4a, nhưng lớn hơn 590 (mạng A) và 350 (cách chia sông, 8 vùng).
- Với 8 ô (tầng 4): có 396 điểm biên.
- `/table` 2000×2000 mất 128,3 s cho 400 lần gọi 100×100. OSRM cục bộ từ chối bảng lớn hơn 100 điểm (`TooBig`), vì `osrm-routed` dùng mặc định `--max-table-size 100`.

## Bổ sung (đã duyệt 2026-10-20): tăng giới hạn bảng và đo lại
- `osrm/manage.py start` có thêm tùy chọn `--max-table-size N`, mặc định **2500**. Lệnh chạy `osrm-routed` nhận thêm `--max-table-size N`.
  - N phải ≥ 1, nếu không thì dừng với thông báo lỗi giống kiểu kiểm tra `threads` và `port`.
  - `osrm/server.json` lưu thêm `max_table_size`, và `status` in ra giá trị này.
- `app.py start` gọi `manage.py start` không kèm tham số, nên nhận mặc định mới.
- Khởi động lại OSRM (`stop` rồi `start`), rồi đo lại `/table` 2000×2000 bằng **một** lần gọi, với cùng 2000 điểm, seed 0. Ghi vào `reports/osrm_mld_boundary.json` khóa `table_single`, đặt cạnh 128,3 s (chia khối) và 110 s (bảng G×G chia khối).
- **Kiểm thử (TDD):** tách hàm dựng parser và hàm dựng lệnh `osrm-routed` để kiểm thử được, không cần Windows hay OSRM.
  - Mặc định là 2500 và có trong lệnh.
  - Giá trị truyền vào được dùng đúng.
  - Giá trị 0 bị từ chối.

### Kết quả đo lại (2026-10-20)
- OSRM chạy với `--max-table-size 2500`.
- `/table` 2000×2000 gửi trong **một** lần gọi mất **17,6 s**. Log máy chủ ghi 16,5 s.
  - Chia khối 100×100 thì mất 128,1 s, nên một lần gọi nhanh hơn khoảng 7 lần.
  - Bảng G×G 1.832×1.832 chia khối mất 110 s.
- Mọi ô của bảng đều có giá trị.
- Bảng vuông chỉ gửi mỗi tọa độ một lần: `TableRouter.durations` bỏ tham số `sources` và `destinations` khi hai danh sách trùng nhau.
- Script có thêm tùy chọn `--single-table` và `--timeout`.
