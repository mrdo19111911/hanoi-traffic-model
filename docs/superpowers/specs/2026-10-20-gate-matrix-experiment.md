# Spec: thí nghiệm ma trận thời gian qua gate (đã duyệt 2026-10-20)

## Mục tiêu
Đo xem tính thời gian giữa hai điểm qua gate có đủ chính xác và đủ rẻ không, so với gọi OSRM trực tiếp. Gate là các nút biên của một cách chia mạng A (mục 18.14 và 19 của spec bản đồ đường lớn). Kết quả dùng để chọn cách chia, giá trị k, và ngưỡng khoảng cách mà dưới đó phải gọi OSRM trực tiếp.

## Phạm vi
- Chỉ dùng thời gian tự do của OSRM (`/table`, annotations `duration` và `distance`), chưa có tốc độ theo khung giờ. Phần phụ thuộc thời gian để spec sau.
- Điểm thử là nút giao của mạng A, nên kết quả đúng cho các chuyến bắt đầu và kết thúc trên đường lớn. Điểm nằm trên đường nhỏ để spec sau.
- Chỉ là thí nghiệm: không đổi bộ ước lượng, không đổi backend, không ghi DB.

## Công thức
Với điểm i thuộc cụm A và điểm j thuộc cụm B, A khác B. Gọi G(A) là tập gate của cụm A, tức các nút biên có ít nhất một cạnh bị cắt kề cụm A.

`t_gate(i, j) = min trên g ∈ G(A), h ∈ G(B) của  t(i, g) + T(g, h) + t(h, j)`

Hai điểm cùng cụm thì không dùng công thức này; khi dựng ma trận thật, những cặp đó gọi OSRM trực tiếp.

## Script `model/gate_matrix_experiment.py`
**Đầu vào:**
- `osrm/data/big_road_clusters.json` (các cách chia, `boundary`, `ways`);
- `osrm/data/big_roads.geojson`;
- OSRM local.

**Các bước:**
1. Dựng đồ thị bằng `spectral_clusters.build_graph`. Gán mỗi nút vào một cụm theo nhãn của cách chia: nhãn lấy từ `ways` của các cạnh kề nút, chọn cụm chiếm phần lớn. Gate của mỗi cụm là các nút trong `boundary` có cạnh kề cụm đó.
2. **Chọn mẫu:** 500 cặp nút ngẫu nhiên (`seed = 0`) thuộc thành phần chính, cố định cho mọi cách chia để kết quả so sánh được.
3. **Gọi OSRM `/table` theo khối** (mỗi khối tối đa 100 × 100):
   - thời gian trực tiếp i → j cho 500 cặp;
   - t(i, g) với mọi gate g của cụm chứa i;
   - T(g, h) cho các cặp gate cần dùng;
   - t(h, j).
4. **Chỉ số cho mỗi cách chia** (spectral k = 8, 16, 32, 64 và theo tỉnh), tính trên các cặp khác cụm:
   - sai số tương đối `(t_gate − t_direct) / t_direct`: trung vị, phân vị 95, lớn nhất;
   - tỉ lệ cặp có sai số trên 5% và trên 10%;
   - số cặp có `t_gate < t_direct` (bất thường do điểm bám đường của OSRM; được đếm, không bỏ);
   - sai số theo dải khoảng cách đường chim bay: dưới 30, 30–100, 100–300, trên 300 km.
5. **Chi phí:**
   - số gate G và số ô của ma trận gate–gate (G²);
   - thời gian đo được khi gọi `/table` cho toàn bộ G × G (chỉ với k = 32 và theo tỉnh, để giới hạn thời gian chạy);
   - dung lượng ước tính khi lưu G² số thời gian 4 byte, nhân 96 khung giờ 15 phút;
   - số ô cần tính cho bài toán N = 2000 điểm: `N × trung bình số gate mỗi cụm`, so với N².
6. **Đầu ra:** ghi `reports/gate_matrix_experiment.json` theo kiểu nguyên tử, in bảng tóm tắt ra màn hình, và ghi `pbf_sha256` của bản đồ.

**Lỗi:**
- OSRM không chạy → dừng với thông báo rõ ràng, không ghi file.
- File cụm khác `pbf_sha256` với `big_roads.meta.json` → dừng.

**Tùy chọn dòng lệnh:**
- `--pairs` (mặc định 500), `--seed` (mặc định 0);
- `--partitions` (mặc định tất cả);
- `--skip-full-gate-table` (bỏ bước đo G × G).

## Kiểm thử (TDD, `tests/test_gate_matrix_experiment.py`, OSRM giả)
- **Đồ thị hai cụm nối qua đúng một gate:** `t_gate` bằng `t_direct` cho mọi cặp khác cụm.
- **Có đường tắt không qua gate:** `t_gate` lớn hơn `t_direct`, và sai số tính đúng.
- **Cặp cùng cụm** không có trong các chỉ số.
- **Chọn mẫu** cố định theo `seed`.
- **Bảng tính theo khối:** ma trận gộp từ các khối bằng một lần gọi lớn.
- **Chỉ số:** trung vị, phân vị 95, tỉ lệ trên 5%, dải khoảng cách và số cặp bất thường đúng trên dữ liệu dựng sẵn.
- **Dừng khi:** file cụm khác bản đồ; OSRM không chạy (không ghi file).

## Kết quả lượt 1 (2026-10-20)
- Thử 500 cặp nút trên mạng A, nhưng chỉ chưa tới 4% số cặp dưới 30 km.
- Sai số trung vị khoảng 0% với mọi cách chia. Phân vị 95 thấp nhất là 0,03% (theo tỉnh), cao nhất là 1,3% (k = 64).
- Sai số lớn nhất từ 3,6% (k = 8) tới 39% (theo tỉnh), chỉ đến từ 1–7 cặp mỗi cách chia.
- Tính đủ bảng G × G mất 10,9 giây (k = 32, 515 gate) và 17,1 giây (theo tỉnh, 697 gate).
- Toàn bộ số liệu nằm trong `reports/gate_matrix_experiment.json`.

## Lượt 2 (đã duyệt 2026-10-20): chuyến ngắn, điểm trên đường nhỏ, và soi các cặp sai số lớn
**Mục tiêu:** chốt ngưỡng: cặp nào phải gọi OSRM trực tiếp, cặp nào được tính qua gate.

### Mẫu phân tầng
800 cặp, `seed = 0`, chia theo dải khoảng cách đường chim bay:

| Dải | Số cặp |
|---|---|
| dưới 30 km | 300 |
| 30–100 km | 300 |
| 100–300 km | 150 |
| từ 300 km | 50 |

**Cách lấy một cặp:**
1. **Điểm đầu:** lấy một điểm ngẫu nhiên trong vòng đất liền (`vietnam_land.geojson`), cách nút mạng A gần nhất không quá 20 km, rồi snap bằng OSRM `/nearest`.
2. **Điểm cuối:** chọn hướng và khoảng cách ngẫu nhiên trong dải, rồi snap theo cùng cách.
3. Bỏ và lấy lại khi:
   - snap lệch quá 500 m;
   - điểm nằm ngoài đất liền;
   - khoảng cách sau khi snap rơi ra ngoài dải.

   Số lần lấy lại được đếm.

**Loại điểm:** điểm "đường nhỏ" khi cặp node của đoạn mà `/nearest` trả về không có trong `big_road_edges.sqlite`; ngược lại là điểm "mạng A".
- Mỗi dải gồm 50% cặp nhỏ–nhỏ, 25% cặp nhỏ–A và 25% cặp A–A.
- Điểm A–A lấy từ nút giao như lượt 1.

### Gán cụm cho điểm đường nhỏ
- **Nút vào mạng:** trong 10 nút mạng A gần nhất theo đường chim bay, chọn nút có thời gian OSRM từ điểm tới nút ngắn nhất.
- Cụm của điểm là cụm của nút vào mạng. Riêng cách chia theo tỉnh dùng tỉnh chứa điểm.
- Các đoạn `t(i, g)` và `t(h, j)` gọi OSRM thẳng từ điểm, nên phần đường nhỏ vẫn đúng.

### Chỉ số thêm (cho mỗi cách chia)
**Quan hệ giữa hai cụm của cặp:**
- `cùng cụm`: không tính qua gate;
- `kề nhau`: có nhát cắt nối hai cụm;
- `xa`: còn lại.

**Bảng chỉ số:** trung vị, p95, lớn nhất và tỉ lệ cặp sai trên 5%, tách theo từng dải khoảng cách × quan hệ cụm × kiểu cặp (nhỏ–nhỏ, nhỏ–A, A–A).

**Ngưỡng đề xuất d\*:** giá trị nhỏ nhất trong {10, 20, 30, 50, 75, 100} km thỏa: mọi cặp khác cụm, xa hơn d\* và không kề nhau, có p95 ≤ 1% và sai số lớn nhất ≤ 5%.
- Tính riêng cho quy tắc "cặp kề nhau luôn gọi trực tiếp" và quy tắc "chỉ dùng ngưỡng khoảng cách".
- In ra số ô OSRM trực tiếp mà quy tắc tạo ra cho N = 2000, dùng phân bố khoảng cách của mẫu.

### Soi các cặp sai số lớn
Với 7 cặp sai số lớn nhất và 7 cặp bất thường nhất (`t_gate < t_direct`) của mỗi cách chia, gọi OSRM `/route` (`annotations=nodes`) cho tuyến trực tiếp và cho tuyến qua cặp gate tốt nhất.

**Ghi lại:** tọa độ, hai cụm, `t_direct`, `t_gate`, hai gate dùng, km trên mạng A của mỗi tuyến, tên và số hiệu các way chính.

**Phân loại tự động:**
- `bypass_small_road`: tuyến trực tiếp đi từ cụm này sang cụm kia trên một cặp node không thuộc mạng A, tức là đi vòng qua gate;
- `ferry`: tuyến trực tiếp hoặc tuyến qua gate có đoạn `kind = ferry`;
- `gate_snap`: điểm snap của gate lệch quá 50 m so với tọa độ gate, hoặc tuyến qua gate quay đầu ngay tại gate;
- `other`: không thuộc loại nào ở trên.

**Đầu ra:**
- `reports/gate_matrix_experiment_2.json`;
- `reports/gate_matrix_worst.geojson`: tuyến trực tiếp nét liền, tuyến qua gate nét đứt, gate là điểm, `properties` có nhãn phân loại; mở được trên bản đồ hoặc geojson.io;
- bảng tóm tắt in ra màn hình.

### Tùy chọn và giới hạn
- Script `model/gate_matrix_experiment.py` thêm tùy chọn `--stratified` cùng `--band-pairs 300,300,150,50`. Lượt 1 vẫn chạy như cũ khi không có `--stratified`.
- Thời gian chạy ước khoảng 10 phút. Có `--partitions` để chạy riêng từng cách chia.
- Tốc độ vẫn là tốc độ tự do của OSRM.

### Kiểm thử (TDD, OSRM giả)
- **Chọn mẫu:** đủ số cặp mỗi dải; điểm bị loại (ngoài đất liền, snap xa, rơi khỏi dải) được lấy lại và đếm; cùng `seed` cho cùng mẫu.
- **Loại điểm:** cặp node có trong chỉ mục → `mạng A`, không có → `đường nhỏ`.
- **Nút vào mạng:** chọn theo thời gian OSRM, không theo đường chim bay.
- **Quan hệ cụm:** đúng cho cùng cụm, kề nhau và xa.
- **Ngưỡng d\*:** chọn đúng trên dữ liệu dựng sẵn, và trả "không có ngưỡng thỏa" khi không giá trị nào đạt.
- **Phân loại:** đồ thị giả có đường tắt nhỏ → `bypass_small_road`; có phà → `ferry`; gate snap lệch → `gate_snap`.
- **GeoJSON:** đúng số feature và đủ thuộc tính; ghi kiểu nguyên tử.

## Kết quả lượt 2 (2026-10-20)
- Từ 100 km trở lên, mọi cách chia có p95 từ 0 đến 3,5% và sai số lớn nhất khoảng 12%.
- Dưới 100 km sai số rất lớn. Không có ngưỡng d\* nào đạt cho k = 16, k = 32 và cách chia theo tỉnh. Ngưỡng có ở k = 8 (20 km hoặc 75 km) và k = 64 (100 km).
- Ba nguyên nhân:
  1. Có 8–98 đầu mút của cạnh bị cắt không nằm trong `boundary`. Nút được gán cụm lại bằng đa số way kề, nên hai cách gán lệch nhau.
  2. Điểm đường nhỏ sát ranh giới vào mạng A ở cụm bên cạnh.
  3. Phà nằm ở chỗ chuyển cụm.

## Lượt 3 (đã duyệt 2026-10-20): sửa định nghĩa gate và chạy lại lượt 2
1. **Gate là mọi đầu mút của cạnh bị cắt.** Định nghĩa `boundary` của mục 18.14 đổi theo.
   - `model/spectral_clusters.py` ghi thêm vào mỗi phần tử `partitions` trường `node_labels`: `[[lon, lat, cluster], …]` cho mọi nút giao của đồ thị, với tọa độ làm tròn 1e-6 như khi dựng đồ thị. Nút "chưa gán" (cách chia theo tỉnh) có `cluster = -1`.
   - Cạnh bị cắt là cạnh có hai đầu thuộc hai cụm khác nhau, cả hai đều ≥ 0, xét theo chính `node_labels`.
   - `boundary` gồm đúng các đầu mút đó, không thiếu và không thừa.
   - `gate_matrix_experiment.py` đọc `node_labels` thay vì gán lại nhãn bằng đa số way. Gate của cụm C là mọi nút trong `boundary` thuộc C, cộng mọi nút trong `boundary` có cạnh bị cắt nối vào C.
2. **Điểm đường nhỏ xét gate của cụm mình và các cụm kề.**
   - Với điểm đường nhỏ i thuộc cụm A, tập gate xuất phát là G(A) hợp với G(B) của mọi cụm B kề A.
   - `t(i, g)` vẫn gọi OSRM trực tiếp từ điểm.
   - Điểm cuối j làm tương tự. Lấy min trên mọi cặp gate. Cặp gate thuộc cùng một cụm thì `T(g, h)` tính bằng OSRM trực tiếp, như mọi cặp khác.
   - Điểm mạng A giữ cách cũ, chỉ xét gate của cụm mình.
3. **Phà ở ranh giới cụm là gate.**
   - Mọi chuỗi trong đồ thị có đoạn `bridge_kind = ferry` mà hai đầu thuộc hai cụm khác nhau: hai bến (hai đầu chuỗi) đều là gate.
   - Theo quy tắc 1 thì điều này tự đúng. Có test riêng để giữ quy tắc này.
   - Chuỗi phà nằm trọn trong một cụm thì không tạo gate.
4. **Tiêu chí mới** (người dùng duyệt 2026-10-20). Cắt cụm chỉ để tìm đường liên cụm.
   - Cặp cùng cụm hoặc ở hai cụm kề nhau luôn gọi OSRM trực tiếp, và **không tính vào sai số**.
   - Chỉ đo trên cặp ở hai cụm không kề nhau. Cách chia đạt khi p95 ≤ 1% và sai số lớn nhất ≤ 5%.
   - Bỏ ngưỡng d\* theo km.
   - Đầu ra chính của mỗi cách chia:
     - p95 và sai số lớn nhất trên cặp xa;
     - đạt hay không đạt;
     - tỉ lệ cặp phải gọi trực tiếp với bài toán 2000 điểm (cùng cụm hoặc kề cụm). Tỉ lệ này ước từ mẫu ngẫu nhiên đều của lượt 1, không từ mẫu phân tầng;
     - số ô OSRM tương ứng, so với N².
   - Vẫn soi 7 cặp xa sai nhiều nhất của mỗi cách chia.
5. **Chạy lại:**
   - sinh lại `big_road_clusters.json`;
   - chạy lại lượt 1 (500 cặp) và lượt 2 (`--stratified`, 800 cặp) với cùng `seed`;
   - in bảng so sánh lượt 2 cũ và mới: p95, sai số lớn nhất và d\* theo hai quy tắc, cho từng cách chia.

**Kiểm thử (TDD):**
- **`node_labels`:** có cho mọi nút; `boundary` đúng bằng tập đầu mút của cạnh bị cắt; nút chưa gán không tạo gate.
- **Thí nghiệm:** đọc `node_labels` đúng; đồ thị giả có cạnh bị cắt mà cách đếm đa số cũ bỏ sót thì giờ có gate và `t_gate` bằng `t_direct`.
- **Điểm đường nhỏ:** điểm sát ranh giới vào mạng ở cụm kề thì dùng gate của cụm kề, và `t_gate` bằng `t_direct` trên đồ thị giả.
- **Phà:** chuỗi phà nối hai cụm cho hai bến là gate; chuỗi phà trong một cụm không tạo gate.
- **Tương thích:** file cụm cũ không có `node_labels` thì thí nghiệm dừng và báo cần sinh lại, không quay về cách đếm đa số.

## Kết quả lượt 3 (2026-10-20)
- Chỉ `spectral_16` đạt ở cả hai mẫu (lượt 2: p95 0,00%, sai số lớn nhất 3,8%).
- Các cách chia khác đạt ở mẫu đều nhưng trượt lượt 2 vì một số ít cặp xa. Nguyên nhân thuộc ba kiểu:
  - đường nhỏ đi tắt qua ranh giới cụm ở chỗ không có gate;
  - phà hoặc cầu nhanh nhất không phải gate;
  - điểm trên đường nhỏ vào mạng lớn ở một cụm không kề cụm đích.
- Nguyên nhân chung: ranh giới hiện nay là nhát cắt của **mạng A**, không phải nhát cắt của **cả mạng đường**. Đường nhỏ vẫn vượt qua được, nên công thức gate không còn chính xác.

## Lượt 4 (chờ duyệt): chia theo sông và núi
**Ý tưởng** (người dùng, 2026-10-20): ranh giới cụm nên đặt theo sông và núi.
- Với sông lớn, mọi đường bộ muốn sang bờ bên kia đều phải đi qua cầu, phà hoặc hầm. Vì vậy tập các chỗ vượt sông là một nhát cắt thật của **cả mạng OSRM**, không chỉ của mạng A. Đó đúng là điều kiện để công thức `t_gate` chính xác.
- Với núi, chỗ vượt là đèo và hầm. Đây không phải nhát cắt tuyệt đối (đường mòn vẫn có thể qua), nhưng số chỗ vượt ít.

### Bước 1: khảo sát, không đổi code chính
Script thử `model/natural_barriers.py` đọc PBF bằng osmium và in ra số liệu, ghi vào `reports/natural_barriers.json`:
- **Sông chia:** quan hệ `type=waterway` hoặc way `waterway=river` có tổng chiều dài ≥ 100 km trong Việt Nam. Ghi danh sách tên và chiều dài, ví dụ Hồng, Đà, Lô, Mã, Cả, Tiền, Hậu, Đồng Nai.
- **Chỗ vượt sông:** mọi way có `highway` mà xe ô tô đi được, mang `bridge=*`, `tunnel=*` hoặc `route=ferry`, và cắt hình học của một sông chia. Tính cả đường nhỏ, không riêng mạng A. Đếm theo từng sông.
- **Núi:** node `mountain_pass=yes` và hầm (`tunnel=yes`) dài ≥ 500 m nằm trên mạng A, như đèo Ngang, Hải Vân và Cù Mông. Đếm số lượng và liệt kê.
- **Mảnh còn lại:** cắt mạng A ở mọi chỗ vượt sông (và các đèo) rồi đếm số thành phần liên thông. Ghi kích thước của mỗi thành phần (số nút, km đường).

### Kết quả bước 1 (2026-10-20)
- Có 60 sông dài ít nhất 100 km. Chúng có 1.808 chỗ vượt, 571 chỗ trên mạng A, và 154 đường bị nghi là chỗ vượt.
- Mạng A ban đầu đã có 70 thành phần.
  - Cắt theo sông: 236 thành phần, 22 thành phần dài ít nhất 200 km.
  - Cắt theo cả sông và núi: 24 thành phần lớn.
- Phần lớn các mảnh nhỏ sinh ra từ những sông không chạy hết ra biển hay tới biên giới, nên đường bộ vòng qua được đầu sông.

### Bước 2 (đã duyệt 2026-10-20): cách chia "sông chính"
Người dùng chốt: chỉ dùng sông chính chạy ra tới biển hoặc biên giới, chia thành khoảng 22 vùng lớn, gộp các mảnh nhỏ. Không cắt theo núi.
- **Sông chính:** là sông chia của bước 1 mà **cả hai đầu đều bám vào một mốc**. Mốc là một trong ba thứ:
  - bờ biển: đầu sông cách đường bờ (lấy từ `vietnam_land.geojson`) không quá 2 km;
  - biên giới quốc gia: đầu sông cách `vietnam_boundary.geojson` không quá 2 km, hoặc sông chạy ra ngoài lãnh thổ;
  - một sông chính khác: đầu sông cách hình học của nó không quá 1 km.

  Tập sông chính tính lặp cho tới khi không đổi. Một đầu "bám" vào sông khác chỉ được tính khi sông kia đã là sông chính. Đầu sông là hai mút xa nhất của hình học, tính trên các thành phần liên thông của nhóm way thuộc sông đó.
- **Cụm:** cắt mạng A tại các cạnh A cắt qua sông chính, rồi lấy các thành phần liên thông. Thành phần dài dưới 200 km đường A được gộp vào thành phần kề có nhiều cạnh nối với nó nhất. Nếu hòa, chọn thành phần lớn hơn. Lặp cho tới khi mọi cụm dài ít nhất 200 km.
  - Thành phần không có cạnh nối nào (đảo, mảnh rời) giữ nhãn −1, giống như các nút chưa gán của cách chia theo tỉnh.
  - Con số 22 là kỳ vọng, không phải ràng buộc. Báo cáo ghi số cụm thực tế.
- **Gate:** hai đầu của mọi chỗ vượt nằm trên ranh giới cụm, gồm cả cầu trên đường nhỏ. Điểm cuối của cầu được snap vào mạng OSRM.
- **File cụm:** thêm phân vùng `method = "natural"` vào `big_road_clusters.json` theo đúng định dạng hiện có (`node_labels`, `boundary`, `cut_edges`). Lớp "Cụm" trên bản đồ tự hiện được.
- **Thí nghiệm:** chạy lại lượt 1 và lượt 2 với `natural` đặt cạnh `spectral_16` và `province`. Dùng cùng tiêu chí: cặp xa, p95 ≤ 1%, max ≤ 5%. Báo thêm tỉ lệ ô phải gọi OSRM trực tiếp.

### Kiểm thử (TDD, PBF nhỏ tự dựng)
- Hai bờ sông nối bằng hai cầu, một cầu trên đường A và một cầu trên đường `residential`: có hai cụm, và cả hai cầu đều cho gate.
- Sông ngắn hơn ngưỡng: không chia.
- Sông có một đầu ra biển nhưng đầu kia cụt giữa đất liền: không phải sông chính, nên không chia.
- Sông nhánh đổ vào một sông chính, đầu kia ở biên giới: là sông chính, tính qua vòng lặp.
- Gộp: thành phần dưới 200 km vào thành phần kề có nhiều cạnh nối nhất; mảnh không có cạnh nối thì nhãn −1.
- File cụm có phân vùng `natural`, các trường như những phân vùng khác. Thí nghiệm đọc được phân vùng này.
- Phà qua sông chia: hai bến là gate.
- Thành phần nhỏ được gộp vào thành phần kề.
- Thiếu thẻ cầu, đường cắt qua sông nhưng không có `bridge`: không coi là chỗ vượt. Đường đó được ghi vào `suspect_crossings` để rà, không làm hỏng lượt chạy.

### Kết quả bước 2 (2026-10-20)
- Có 9 sông chính. Cắt theo chúng cho 21 mảnh; sau khi gộp mảnh nhỏ còn **8 vùng**. Vùng 0 dài 32.476 km, trải từ vĩ độ 9,6 đến 19,5.
- Có 350 gate, gồm 193 gate thêm từ cầu trên đường nhỏ. Tính bảng gate × gate mất 4,0 s.
- Sai số rất nhỏ. Lượt 1: cặp xa p95 0,10%, sai số lớn nhất 3,7%. Lượt 2: sai số lớn nhất 0,04%.
- Nhưng vùng quá lớn: 65% số cặp phải gọi OSRM trực tiếp, tức 2,6 triệu ô với N=2000.

### Bước 3 (đã duyệt 2026-10-20): hai tầng, sông chính rồi spectral
Người dùng chốt: giữ 8 vùng sông chính làm tầng trên, rồi chia mỗi vùng lớn bằng spectral để tổng cộng có khoảng 22 vùng.
- **Số vùng con:** tổng mục tiêu `--target 22`. Vùng i nhận `k_i` vùng con, tỉ lệ với số km đường A của nó, làm tròn theo phần dư lớn nhất, với `k_i ≥ 1` và `Σ k_i = target`.
- **Chia trong vùng:** dùng NJW spectral như `spectral_clusters.py` (trọng số 1/thời gian, seed 0), chỉ trên đồ thị nút giao **bên trong** vùng đó. Không vùng con nào được nằm trên hai vùng sông.
  - Vùng có `k_i = 1` giữ nguyên.
  - Vùng con nào bị đứt thành nhiều mảnh thì xử lý như các phân vùng spectral hiện có. Báo số vùng con liền mạch.
- **Nhãn và gate:**
  - Nhãn toàn cục đánh số liên tiếp. Mỗi vùng con lưu thêm `parent`, tức số của vùng sông.
  - `boundary` gồm hai loại: các gate sông của bước 2 (kể cả `extra_gates` từ cầu trên đường nhỏ), và hai đầu các cạnh do spectral cắt bên trong vùng.
  - Kề nhau nghĩa là có cạnh bị cắt giữa hai vùng con, hoặc có chỗ vượt sông giữa chúng.
- **File cụm:** thêm phân vùng `method = "natural_spectral"` (tên `natural_spectral`), cùng định dạng các phân vùng khác. Trường `stats` thêm `k_by_parent`.
- **Giao diện:** thêm nút "Sông + spectral (N)" cạnh nút "Sông (N)".
- **Thí nghiệm:** chạy lượt 1 và lượt 2 (seed 0) với `natural_spectral` đặt cạnh `natural`, `spectral_16`, `spectral_32` và `province`. Dùng cùng tiêu chí: cặp xa, p95 ≤ 1%, max ≤ 5%. Báo các số sau:
  - tỉ lệ cặp và số ô N=2000 phải gọi OSRM trực tiếp;
  - G và thời gian tính bảng G×G;
  - 7 cặp xa sai nhiều nhất, tách riêng cặp vượt ranh giới sông và cặp vượt ranh giới spectral.
- **Kiểm thử (TDD):**
  - Chia số vùng con: tổng bằng `target`, mỗi `k_i ≥ 1`, tỉ lệ theo km, và ra cùng một kết quả mỗi lần chạy.
  - Đồ thị tự dựng có hai vùng sông, trong đó một vùng gồm hai cụm dày nối với nhau bằng một cạnh. Với target 3, kết quả là 3 vùng con, cạnh nối đó bị cắt, và không vùng con nào vắt qua sông.
  - `boundary` có cả gate sông lẫn đầu cạnh spectral. `extra_gates` vẫn còn.
  - Thí nghiệm đọc được `natural_spectral`.
  - Frontend có nút mới.

### Kết quả bước 3 (2026-10-20)
- `natural_spectral` chia được 22 vùng con, có G = 585. Tính bảng G×G mất 11,1 s.
- Đạt lượt 1: cặp xa p95 0,35%, sai số lớn nhất 3,7%, tỉ lệ gọi trực tiếp 23%.
- Trượt lượt 2: sai số lớn nhất 33%. Cả 5 cặp sai nhiều nhất vượt **ranh giới spectral** bằng đường nhỏ. Cặp vượt ranh giới sông sai tối đa 0,33%.

### Bước 4 (đã duyệt 2026-10-20): ranh giới spectral là nhát cắt của cả mạng đường
Người dùng chốt: ở ranh giới spectral, mọi đường ô tô vượt qua ranh giới, kể cả đường nhỏ, đều cho gate, giống cầu ở ranh giới sông. Trước hết đo xem G tăng bao nhiêu.

**4a. Gán nhãn cả mạng đường và đếm gate (chỉ đo):**
- **Đồ thị:** mọi way ô tô đi được theo luật của `natural_barriers.py`, lấy từ PBF. Mỗi cạnh nối hai node liền kề, trọng số là thời gian theo tốc độ mặc định của profile.
- **Không vượt sông:** cạnh nào cắt qua sông chính thì bị bỏ khỏi đồ thị khi gán nhãn, nên nhãn không lan sang bờ bên kia.
- **Gán nhãn:** Dijkstra nhiều nguồn, mỗi nguồn là một nút giao A đã có nhãn vùng con. Mỗi node nhận nhãn của nguồn gần nhất theo thời gian, qua `scipy.sparse.csgraph.dijkstra(..., min_only=True)`. Node không tới được nguồn nào thì nhãn −1.
- **Gate ranh giới spectral:** là mọi cạnh mà hai đầu khác nhãn và cùng một vùng sông. Gate đặt tại đầu cạnh thuộc mỗi bên. Cạnh trên mạng A đã là gate từ trước nên không đếm lại.
- **Gộp gate trùng:** gate cách nhau dưới 100 m theo đường chim bay, cùng cặp vùng con, thì gộp làm một. Lý do: đường hai chiều tách làn và các nhánh nút giao.
- **Báo cáo:** `reports/full_network_gates.json` ghi các số sau:
  - số cạnh vượt ranh giới, trước và sau khi gộp, tách theo loại `highway`;
  - G mới (gồm gate sông, gate cầu đường nhỏ và gate spectral trên cả mạng);
  - số gate của từng cặp vùng con kề nhau;
  - thời gian đo được của bảng G×G một khung giờ;
  - thời gian ước cho 96 khung giờ.

  Không chạy thí nghiệm ở 4a.

**4b. Chỉ làm sau khi người dùng xem số liệu 4a:**
- Đưa các gate trên vào phân vùng `natural_spectral`, dưới dạng `extra_gates` với `kind = "spectral_road"`.
- Hai vùng con kề nhau khi có ít nhất một cạnh vượt giữa chúng.
- Chạy lại lượt 1 và lượt 2 với cùng tiêu chí.

**Kiểm thử (TDD, đồ thị tự dựng):**
- Hai vùng con nối bằng một cạnh A và một đường `residential` không thuộc A: đường `residential` cho gate; cạnh A không bị đếm hai lần.
- Node trên đường nhỏ nhận nhãn của nút giao A gần nhất theo thời gian, không theo đường chim bay.
- Nhãn không vượt sông chính.
- Hai gate cách nhau 50 m, cùng cặp vùng con, được gộp làm một. Hai gate cách nhau 150 m thì giữ cả hai.
- Node không tới được nguồn nào có nhãn −1 và không sinh gate.
