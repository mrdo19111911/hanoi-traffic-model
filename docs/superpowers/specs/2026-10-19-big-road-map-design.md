# Spec: Bản đồ đường lớn (mạng A) và chế độ "Chỉ đường lớn"

Ngày: 2026-10-19. Trạng thái: chờ duyệt.

## 1. Bối cảnh và mục tiêu

Mục tiêu lâu dài là tạo **ma trận thời gian (time matrix) liên thành phố** cho bài toán VRP phụ thuộc thời gian, chạy được ở mọi cỡ và có phiên bản để cache vào DB của người dùng. Đường dài thực tế chỉ đi trên một số trục lớn, nên bài toán liên thành phố dùng **mạng A: cao tốc và quốc lộ**. Mạng B (`primary`, `secondary`) và C (`tertiary`) để dành cho bài toán giao hàng nội thành ("local") sau này.

Spec này chỉ bao gồm **bước đầu**: dựng mạng A từ OSM, đánh dấu các điểm mạng A giao với đường B và C, và hiển thị chúng ở chế độ chỉ xem trên bản đồ. Ma trận thời gian, vành vùng thành phố và bảng vận tốc là các spec sau.

**Thành công khi:** người dùng bật "Chỉ đường lớn" và thấy toàn bộ mạng cao tốc và quốc lộ Việt Nam cùng các điểm giao B và C. Các chỗ mạng bị đứt nhìn thấy được. Mọi số liệu đều truy được về dữ liệu OSM gốc.

## 2. Phạm vi

**Trong phạm vi**
- Chính sách xác định đường thuộc mạng A, đặt ở một chỗ duy nhất.
- Script dựng mạng A và điểm giao từ file PBF cả nước.
- API chỉ đọc để phục vụ dữ liệu đã dựng.
- Chế độ "Chỉ đường lớn" trên bản đồ hiện có.
- Tự dựng lại trong `app.py setup` khi bản đồ OSM đổi.

**Ngoài phạm vi**
- Tìm đường hay tính thời gian trên mạng A, và bộ OSRM riêng cho mạng A.
- **Tự động nối khúc** những chỗ mạng đứt. Bước này chỉ phát hiện và hiển thị chỗ đứt. *(Đã đổi: xem mục 14.)*
- Vành vùng thành phố, cửa ngõ, bảng vận tốc, ma trận.
- Xe tải và các loại xe khác.

## 3. Định nghĩa

**Mạng A.** Một way OSM thuộc mạng A khi:
1. `highway` là `motorway`, `motorway_link`, `trunk` hoặc `trunk_link`; **hoặc**
2. `highway` là đường xe đi được (`primary`, `secondary`, `tertiary`, `unclassified`, `residential`, `living_street`, `service`, `road` và các `_link`), **và** `ref` có số hiệu `CT…`, `QL…` hay `AH…`.

Đường đang xây (`construction`), đường dự kiến (`proposed`) và đường không cho xe đi (`footway`…) **không** thuộc mạng A, dù có số hiệu.

**Nhóm của một way A:** `caotoc` nếu thẻ là `motorway` hoặc `motorway_link`, hoặc nếu số hiệu là `CT…`. Các trường hợp còn lại là `quoclo`.

**Đoạn chỉ giữ nhờ số hiệu (`ref_only`):** way A không có thẻ `motorway` hay `trunk`, tức là chỉ thuộc mạng A nhờ điều kiện 2.

**Điểm giao:** một node OSM nằm trên ít nhất một way A, đồng thời nằm trên ít nhất một way **không thuộc A** có thẻ:
- loại **B**: `primary`, `primary_link`, `secondary`, `secondary_link`;
- loại **C**: `tertiary`, `tertiary_link`.

Một node giao với cả B và C thì ghi là loại **B**. Giao với đường nhỏ hơn (`residential`, `service`…) không tính.

**Khúc (component):** tập các way A nối với nhau qua node chung, không xét chiều đi. **Khúc chính** là khúc có tổng chiều dài lớn nhất. Mọi way không thuộc khúc chính có `isolated = true`.

## 4. Thiết kế

### 4.1 Chính sách: `model/osm_classes.py`
- `big_road_group(highway, ref="") -> "caotoc" | "quoclo" | None`
- `is_big_road(highway, ref="") -> bool`
- Hằng số `BIG_ROAD_POLICY_VERSION = 1`, tăng lên mỗi khi định nghĩa ở mục 3 thay đổi.
- Hàm `classify_osm` có sẵn **không đổi**.

### 4.2 Script dựng: `model/build_big_roads.py`
Cách chạy:

```
python model/build_big_roads.py [--pbf PATH] [--out-dir DIR] [--tolerance-m 15]
```

Giá trị mặc định: `--pbf osrm/data/vietnam-latest.osm.pbf`, `--out-dir osrm/data`. Script có hàm `build(pbf, out_dir, tolerance_m=15) -> meta` để test gọi trực tiếp.

Script đọc PBF **một lượt** bằng osmium (có tọa độ node), giữ lại:
- các way A gồm id, node, tọa độ và thẻ;
- tập node của các way B và tập node của các way C (chỉ những way không thuộc A).

Sau đó:
1. **Điểm giao** = node của way A giao với tập node B hoặc C, xếp loại theo mục 3.
2. **Khúc**: gộp các way A bằng union-find theo node chung, tính chiều dài từng khúc, xác định khúc chính.
3. **Làm gọn hình** từng way bằng thuật toán Douglas–Peucker với sai lệch `tolerance_m`, tính trên mặt phẳng mét cục bộ. **Luôn giữ** hai đầu way, mọi điểm giao, và mọi node chung với way A khác, để không phá vỡ cách các đoạn nối nhau.
4. **Chiều dài** tính trên hình gốc, trước khi làm gọn.
5. Tọa độ làm tròn **6 chữ số thập phân**.
6. Ghi file **nguyên tử**: ghi ra file tạm rồi đổi tên, để khi lỗi giữa chừng file cũ vẫn còn nguyên.

**Ba file đầu ra trong `out_dir`**

`big_roads.geojson`: FeatureCollection gồm các LineString. `properties`:

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `way_id` | int | id way OSM |
| `group` | `caotoc` \| `quoclo` | nhóm theo mục 3 |
| `highway` | str | thẻ gốc |
| `ref`, `name` | str | rỗng nếu không có |
| `oneway` | bool | `oneway=yes`, `oneway=1`, hoặc `highway` là `motorway` hay `motorway_link` |
| `ref_only` | bool | theo mục 3 |
| `component` | int | số thứ tự khúc, 0 là khúc chính |
| `isolated` | bool | `component != 0` |
| `km` | float | chiều dài way, làm tròn 3 chữ số |

`big_road_junctions.geojson`: FeatureCollection gồm các Point. `properties`:

| Trường | Kiểu | Ý nghĩa |
|---|---|---|
| `node_id` | int | id node OSM |
| `type` | `B` \| `C` | loại giao |
| `joins` | list[str] | tên các đường B hoặc C giao vào. Sắp theo thứ tự chữ, bỏ trùng. Đường không tên ghi theo thẻ, ví dụ `"(secondary)"`. |
| `big_road` | str | tên đường A, kèm số hiệu nếu có, ví dụ `"Ngọc Hồi (QL.1)"`. Nhiều đường A thì nối bằng `" / "`. |

`big_roads.meta.json`:

```json
{"pbf_sha256": "...", "policy_version": 1, "tolerance_m": 15,
 "ways": 0, "points_before": 0, "points_after": 0,
 "km": {"caotoc": 0, "quoclo": 0}, "km_by_tag": {"trunk": 0, "primary": 0},
 "ref_only_km": 0, "junctions": {"B": 0, "C": 0},
 "components": 0, "main_component_km": 0, "isolated_km": 0, "skipped_segments": 0, "built_at": "ISO-8601 UTC"}
```

Trong đó `km_by_tag` là chiều dài theo thẻ `highway` gốc.

### 4.3 Backend: `backend/app.py`
- `GET /api/big-roads` trả `osrm/data/big_roads.geojson`.
- `GET /api/big-roads/junctions` trả `osrm/data/big_road_junctions.geojson`.
- `GET /api/big-roads/meta` trả `osrm/data/big_roads.meta.json`.
- Cả ba trả kiểu `application/geo+json` hoặc `application/json`, **nén gzip** khi trình duyệt chấp nhận, bằng `GZipMiddleware` của Starlette với ngưỡng 1 KB.
- Nếu file chưa có: trả **404** với `detail` tiếng Việt: "Chưa dựng bản đồ đường lớn. Chạy python model/build_big_roads.py."
- Đường dẫn file tính từ `root` của `create_app`, để test dùng được thư mục tạm.
- `/api/health` thêm trường `big_roads_current`: `true` khi `meta.pbf_sha256` trùng `osrm/build.json` và `meta.policy_version` trùng `BIG_ROAD_POLICY_VERSION`, ngược lại `false`. Thiếu file thì cũng là `false`.

### 4.4 Thiết lập: `app.py setup`
Sau bước dựng chỉ mục loại đường, nếu `big_roads.meta.json` không có, hoặc `pbf_sha256` trong đó khác `osrm/build.json`, hoặc `policy_version` khác `BIG_ROAD_POLICY_VERSION`, thì chạy `model/build_big_roads.py`.

### 4.5 Giao diện: `frontend/src`
- Thêm nút bật tắt **"Chỉ đường lớn"** ở góc trên bên phải bản đồ.
- Lần đầu bật thì tải ba API ở mục 4.3 song song. Các lần sau dùng lại dữ liệu đã tải. Đang tải thì hiện chữ "Đang tải bản đồ đường lớn…". Lỗi thì hiện `detail` của API trong hộp lỗi, và nút trở lại trạng thái tắt.
- **Khi bật:**
  - ẩn tuyến ước lượng, hai điểm A và B, chú thích loại đường, cùng thẻ tuyến;
  - tạm khóa chức năng chọn điểm trên bản đồ;
  - mỗi lần bật, phóng bản đồ vừa toàn bộ mạng A;
  - nếu `/api/health` báo `big_roads_current = false`, hiện cảnh báo "Bản đồ đường lớn dựng từ bản OSM cũ hơn. Chạy lại python model/build_big_roads.py.";
  - vẽ bằng canvas (`preferCanvas`).
- **Kiểu vẽ:**

  | Thành phần | Kiểu |
  |---|---|
  | `caotoc` | đỏ `#dc3e48`, dày 3 |
  | `quoclo` | cam `#e89b36`, dày 2,5 |
  | `ref_only` | nét đứt, màu theo nhóm (cao tốc mang số hiệu CT vẫn là đỏ) |
  | `isolated` | xám `#8a949a`, chồng lên các kiểu trên |
  | Điểm giao B | chấm tròn bán kính 3, xanh `#3974d8` |
  | Điểm giao C | chấm tròn bán kính 3, xanh lá `#2a9d8f` |

- **Rê chuột** vào đường: hiện tên, số hiệu, thẻ gốc, km, và nhãn "chỉ giữ nhờ số hiệu" hay "khúc bị cô lập" nếu có. Rê vào điểm giao: hiện `big_road`, loại giao và `joins`.
- **Ô tích** "Giao với B" và "Giao với C" để bật tắt từng lớp điểm giao, mặc định bật cả hai.
- **Chú thích** thay bằng: Cao tốc · Quốc lộ · Chỉ nhờ số hiệu · Khúc cô lập · Giao B · Giao C, kèm tóm tắt từ `meta`: tổng km, số điểm giao, số khúc.
- Tắt chế độ thì mọi thứ trở lại như trước. Tuyến đã ước lượng, nếu có, hiện lại.

## 5. Xử lý lỗi
- **PBF không tồn tại:** script báo lỗi rõ ràng, thoát với mã khác 0, không ghi file nào.
- **Không tìm thấy way A nào:** script báo lỗi, không ghi đè file cũ.
- **Way có node thiếu tọa độ:** bỏ đoạn đó, đếm vào `meta.skipped_segments`. Không làm hỏng cả lượt dựng.
- **Dữ liệu cũ so với bản đồ mới:** API vẫn phục vụ. Giao diện hiện cảnh báo dựa trên `big_roads_current` (mục 4.3 và 4.5).

## 6. Kiểm thử (TDD: viết test trước rồi mới viết code)

**Chính sách (`tests/test_big_roads.py`)**
- `motorway`, `motorway_link`, `trunk`, `trunk_link` → đúng nhóm.
- `primary`+`QL.1`, `secondary`+`QL1A`, `residential`+`AH1;QL.1`, `tertiary`+`AH14` → `quoclo`. `primary`+`CT.01` → `caotoc`.
- `primary` không số hiệu, `secondary`+`ĐT.70`, `construction`+`QL.1`, `proposed`+`CT.02`, `footway`+`QL.5`, thẻ rỗng → không thuộc A.

**Script dựng**, trên file `.osm` giả lập có đủ các trường hợp:
- chỉ giữ đúng các way A, đúng `ref_only`, `group` và `oneway`;
- điểm giao B và C đúng, bỏ qua giao với `residential`, đúng `joins` và `big_road`;
- làm gọn bỏ điểm thẳng hàng nhưng giữ điểm giao và hai đầu way;
- khúc cô lập được đánh dấu, đúng số khúc;
- `meta` khớp với file, đúng số đếm, `ref_only_km` bằng chiều dài các đoạn `ref_only`;
- PBF không tồn tại thì không có file đầu ra.

**Backend (`tests/test_backend.py`)**
- Ba API trả đúng nội dung khi có file, có nén gzip khi gửi `Accept-Encoding: gzip`.
- Trả 404 với thông báo tiếng Việt khi chưa có file.
- `/api/health` có `big_roads_current`.

**Giao diện (`src/App.test.tsx`)**
- Bật nút thì gọi ba API và truyền dữ liệu vào bản đồ (bản đồ được mock).
- Tắt lớp B thì dữ liệu truyền vào bản đồ không còn lớp B.
- API lỗi thì hiện hộp lỗi và nút trở về trạng thái tắt.

**Chạy thật:** dựng trên PBF cả nước. Kiểm tra các con số trong `meta` gần với số đã đo: khoảng 38.300 km, khoảng 9.750 điểm giao B và 8.100 điểm giao C. File GeoJSON sau nén gzip dưới 15 MB. Mở trình duyệt kiểm tra trực quan.

## 7. Tiêu chí nghiệm thu
1. Mọi test Python và frontend đều qua.
2. `python model/build_big_roads.py` chạy xong trên PBF cả nước, ra ba file, các con số khớp mục 6 trong khoảng 5%.
3. Trên trình duyệt, chế độ "Chỉ đường lớn" hiện toàn bộ mạng A và hai lớp điểm giao, bật tắt được, có tooltip, tắt đi thì giao diện trở lại như cũ.
4. Hiển thị cả nước không làm trình duyệt treo: phóng to, thu nhỏ và kéo bản đồ mượt trên máy hiện tại.

## 8. Bổ sung (2026-10-19): chỉ hiển thị đất liền Việt Nam

Người dùng chọn: ở chế độ đường lớn **bỏ hẳn ảnh nền**, chỉ còn **đất liền Việt Nam tô nhạt**, **bỏ các đảo**, **khóa vùng xem** trong Việt Nam.

### 8.1 Dựng hình đất liền (trong `build_big_roads.py`)
Đường biên quốc gia trên OSM chạy theo lãnh hải, nên không dùng trực tiếp được. Hình đất liền được ghép như sau:
1. **Quan hệ biên giới:** quan hệ OSM có `boundary=administrative`, `admin_level=2`, `ISO3166-1=VN`.
2. **Biên giới trên bộ:** các way thành viên **không** có `maritime=yes`. Ghép chúng thành **một chuỗi**, nối đầu với đầu theo id node và cho phép đảo chiều từng way.
3. **Bờ biển đất liền:** các way `natural=coastline` được ghép thành chuỗi theo id node. Vòng khép kín là **đảo**. Các chuỗi hở được nối với nhau khi đầu mút trùng tọa độ. Chuỗi hở dài nhất sau khi nối là **bờ biển đất liền**.
4. **Cắt và khép vòng:** trên bờ biển đất liền, tìm hai đỉnh gần nhất với hai đầu của chuỗi biên giới trên bộ. Giữ đoạn bờ biển nằm giữa hai đỉnh đó, bỏ phần lấn sang nước khác. Vòng đất liền = chuỗi biên giới trên bộ + đoạn bờ biển đó theo chiều ngược lại.
5. **Làm gọn** vòng bằng Douglas–Peucker với sai lệch `land_tolerance_m` (mặc định **100 m**), giữ nguyên hai điểm nối.
6. **Không ghép được** (thiếu quan hệ, biên giới trên bộ không thành một chuỗi, không có bờ biển, hoặc hai đầu biên giới cách bờ biển quá **5 km**): không ghi `vietnam_land.geojson`, ghi lý do vào `meta.land_status`. Mọi đường được coi là đất liền. Các file đường lớn khác vẫn được dựng bình thường.

### 8.2 Đánh dấu đảo
- Mỗi way trong `big_roads.geojson` có thêm `island` (bool): `true` khi **đỉnh đầu, đỉnh giữa và đỉnh cuối** của way (trên hình gốc) đều nằm **ngoài** vòng đất liền. Như vậy đoạn quốc lộ chạy qua cửa khẩu vẫn tính là đất liền.
- Mỗi điểm trong `big_road_junctions.geojson` có thêm `island` (bool): `true` khi điểm nằm ngoài vòng đất liền.
- Dữ liệu đảo được **giữ lại trong file**, chỉ ẩn khi hiển thị, vì bài toán sau này có thể cần, ví dụ với phà.

### 8.3 Đầu ra mới
- `vietnam_land.geojson`: một Feature dạng Polygon (vòng đất liền), `properties`: `{"source": "osm_admin_level_2_land_border+coastline", "tolerance_m": 100, "points": n}`.
- `meta` thêm các trường: `land_status` (`"ok"` hoặc lý do lỗi), `land_points`, `island_ways`, `island_km`, `island_junctions`.
- `BIG_ROAD_POLICY_VERSION` tăng lên **2**, nên `app.py setup` tự dựng lại.

### 8.4 Backend
- `GET /api/big-roads/land` trả `vietnam_land.geojson`. Chưa có file thì trả 404 với thông báo tiếng Việt như mục 4.3.

### 8.5 Giao diện (chế độ đường lớn)
- Tải thêm `/api/big-roads/land`, cùng lúc với ba API kia. Nếu riêng API này lỗi thì **vẫn bật chế độ**, giữ ảnh nền như cũ, không khóa vùng xem, và hiện ghi chú "Chưa có hình đất liền; đang dùng ảnh nền."
- **Có hình đất liền:**
  - **ẩn lớp ảnh nền**, nền bản đồ màu trắng `#ffffff`;
  - vẽ hình đất liền: tô `#eef2ef`, viền `#c3cec9` dày 1;
  - **khóa vùng xem**: `maxBounds` là khung bao của đất liền nới thêm 5%, `maxBoundsViscosity = 1`, và mức phóng nhỏ nhất bằng mức vừa khít toàn bộ đất liền.
- **Ẩn mọi đường và điểm giao có `island = true`.** Số liệu tóm tắt trong chú thích vẫn lấy từ `meta`, kèm thêm "đã ẩn X km trên đảo".
- **Tắt chế độ** thì hiện lại ảnh nền, bỏ khóa vùng xem, đưa mức phóng nhỏ nhất về 0.
- **Nút bật/tắt "Ảnh nền"** (người dùng yêu cầu thêm):
  - có ở cả hai chế độ;
  - mặc định **tắt** khi vào chế độ đường lớn có hình đất liền, **bật** khi về chế độ tuyến đường;
  - người dùng đổi tùy ý trong từng chế độ;
  - khi ảnh nền tắt thì không tải ô ảnh nền nào.

### 8.6 Kiểm thử bổ sung
- **File OSM giả lập** có quan hệ biên giới:
  - biên giới trên bộ gồm hai way **ngược chiều nhau**;
  - một way biên giới trên biển;
  - bờ biển gồm hai way, **lấn ra ngoài** hai đầu biên giới;
  - một **đảo** (vòng bờ biển khép kín) có một quốc lộ và một điểm giao B.

  Kiểm tra:
  - vòng đất liền đúng các đỉnh, không chứa phần bờ biển lấn ra;
  - quốc lộ trên đảo có `island = true`, quốc lộ trên đất liền có `island = false`;
  - điểm giao trên đảo có `island = true`;
  - `meta` có `land_status = "ok"` và đúng số đếm.
- **File không có quan hệ biên giới:** không có `vietnam_land.geojson`, `land_status` ghi lý do, mọi `island = false`.
- **Backend:** `/api/big-roads/land` trả file khi có, trả 404 khi chưa có.
- **Giao diện:**
  - dữ liệu truyền vào bản đồ không còn đường và điểm giao trên đảo, và có hình đất liền;
  - API `land` lỗi thì chế độ vẫn bật và hiện ghi chú.
- **Kiểm tra trên trình duyệt:** không còn ảnh nền; không kéo hay thu nhỏ được ra ngoài Việt Nam; không thấy Phú Quốc, Côn Đảo, Cát Bà.

## 10. Bổ sung (2026-10-19): điểm đường lớn giao biên giới (cửa khẩu đường bộ)

Người dùng yêu cầu: ngoài điểm giao với đường B và C, phải có cả **điểm mạng A cắt đường biên giới trên bộ**.

### 10.1 Định nghĩa
- **Điểm giao biên giới:** chỗ một way A cắt **chuỗi biên giới trên bộ** (mục 8.1, bước 2). Biên giới trên biển không tính.
- **Cách tìm:** một đoạn thẳng của way A (giữa hai node liền kề, trên **hình gốc**) cắt một đoạn thẳng của biên giới trên bộ. Trường hợp hai bên dùng chung node cũng tính. Để không phải so mọi cặp đoạn, các đoạn biên giới được xếp vào lưới ô 0,05°, và mỗi đoạn đường chỉ so với các đoạn biên giới trong những ô nó đi qua.
- **Gộp điểm:** các điểm cắt cách nhau dưới **200 m** được gộp làm một, ví dụ hai chiều của đường đôi qua cùng một cửa khẩu. Vị trí là trung bình các điểm. Tên đường A là danh sách các nhãn, nối bằng `" / "`.
- **Tên cửa khẩu:** nếu trong vòng **2 km** có node OSM `barrier=border_control` có tên, thì lấy tên node gần nhất. Không có thì để rỗng.

### 10.2 Đầu ra
- Trong `big_road_junctions.geojson`, thêm feature với `type = "border"`. `properties` gồm:
  - `node_id`: `null` nếu là giao cắt hình học; id node nếu hai bên dùng chung node;
  - `joins`: `[]`;
  - `big_road`: tên đường A;
  - `crossing`: tên cửa khẩu;
  - `island`: `false`.
- Một điểm vừa là giao B hoặc C vừa là giao biên giới thì xuất hiện **hai lần**, mỗi lần một loại.
- `meta.junctions` thêm khóa `border`.
- Không có chuỗi biên giới trên bộ (`land_status` khác `"ok"`) thì không có điểm giao biên giới, `meta.junctions.border = 0`.
- `BIG_ROAD_POLICY_VERSION` tăng lên **3**.

### 10.3 Giao diện
- Điểm giao biên giới vẽ **ô vuông** cạnh 7 px, đỏ sẫm `#8e1b2c` viền trắng.
- Thêm ô tích **"Giao biên giới"** (mặc định bật).
- Rê chuột thì hiện tên cửa khẩu nếu có, tên đường A và chữ "Giao biên giới".
- Tóm tắt trong chú thích có thêm "N giao biên giới".

### 10.4 Kiểm thử
- **File OSM giả lập:**
  - một quốc lộ **cắt** biên giới trên bộ giữa hai node → đúng 1 điểm `border`, đúng tọa độ giao;
  - một quốc lộ **dùng chung node** với biên giới → 1 điểm `border` có `node_id`;
  - hai chiều đường đôi cắt biên giới cách nhau 50 m → **gộp** thành 1 điểm, `big_road` có cả hai nhãn;
  - một quốc lộ cắt **biên giới trên biển** → không có điểm `border`;
  - một node `barrier=border_control` có tên, cách điểm giao 1 km → `crossing` có tên đó;
  - không có quan hệ biên giới → không có điểm `border`.
- **Giao diện:** tắt ô "Giao biên giới" thì dữ liệu truyền vào bản đồ không còn điểm `border`. Chú thích hiện đúng số.
- **Chạy thật:** số điểm giao biên giới có cỡ hàng chục, tương đương số cửa khẩu đường bộ có quốc lộ đi qua. Kiểm tra trên trình duyệt các cửa khẩu Hữu Nghị (Lạng Sơn), Mộc Bài (Tây Ninh), Cầu Treo (Hà Tĩnh).

## 11. Bổ sung (đã duyệt 2026-10-19): ẩn điểm giao B và C khi thu nhỏ
Khi nhìn cả nước, 17.500 điểm giao B và C che gần hết mạng đường. Đề xuất: **chỉ vẽ điểm giao B và C khi mức phóng ≥ 9**, tức tầm nhìn khoảng một vài tỉnh. Ở mức nhỏ hơn thì hiện ghi chú "Phóng to để xem điểm giao B/C". Điểm giao biên giới **luôn hiện**, vì chỉ có vài chục điểm. Bỏ được bớt việc vẽ cũng làm thao tác kéo và phóng cả nước nhẹ hơn.

## 12. Bổ sung (đã duyệt 2026-10-19): ảnh nền chỉ trong Việt Nam, ngoài Việt Nam trắng tinh

**Lỗi hiện tại.** Hình đất liền được tô kín (`fillOpacity = 1`), nên bật "Ảnh nền" cũng không thấy ảnh nền trong Việt Nam. Ảnh nền chỉ lộ ra ở bên ngoài, đúng phần người dùng không muốn thấy.

**Thiết kế mới.** Lớp đất liền đổi thành **mặt nạ**: một đa giác **trắng `#ffffff`, tô kín**, phủ cả thế giới (vĩ độ −85…85, kinh độ −180…180), có **lỗ** là vòng đất liền Việt Nam. Viền đất liền vẫn vẽ riêng: `#c3cec9`, dày 1, không tô. Kết quả:

| | Ngoài Việt Nam (gồm cả các đảo) | Trong Việt Nam |
|---|---|---|
| Ảnh nền **bật** | trắng tinh | ảnh nền OSM |
| Ảnh nền **tắt** | trắng tinh | tô nhạt `#eef2ef` (màu nền của khung bản đồ) |

- Các đảo nằm ngoài vòng đất liền nên cũng trắng, đúng với mục 8 (bỏ các đảo).
- Mặt nạ nằm **trên ảnh nền và dưới đường**, không nhận chuột.
- Thiếu hình đất liền thì giữ cách xử lý ở mục 8.5: không có mặt nạ, ảnh nền bật, hiện ghi chú.
- **Sửa mục 8.5** (người dùng, 2026-10-19: "ấn chỉ đường lớn mà mất ảnh nền Việt Nam"): vào hay ra chế độ đường lớn **không tự đổi ảnh nền**. Ảnh nền mặc định bật, chỉ đổi khi người dùng bấm nút "Ảnh nền". Mục 8 trước đây hiểu nhầm ý người dùng: cái cần bỏ là ảnh nền **nước ngoài**, không phải ảnh nền Việt Nam.

**Kiểm thử.** Truyền vào bản đồ một hình đất liền, kiểm tra lớp mặt nạ có hai vòng (khung thế giới và vòng Việt Nam), tô trắng, độ mờ 1. Trên trình duyệt, bật ảnh nền thì thấy ảnh nền trong Việt Nam và trắng ở ngoài; tắt thì trong Việt Nam tô nhạt, ngoài vẫn trắng.

## 13. Bổ sung (đã duyệt 2026-10-19): đường tỉnh (tỉnh lộ) thuộc mạng A

Người dùng yêu cầu: **mọi đường tỉnh đều là đường lớn**.

### 13.1 Định nghĩa
- Ký hiệu đường tỉnh trong `ref` của OSM: `ĐT`, và hai cách viết ít gặp hơn là `DT` và `TL`. Sau ký hiệu là số, có thể có dấu chấm hoặc khoảng trắng, ví dụ `ĐT.70`, `ĐT 419`, `DT741`, `TL.10`.
- Điều kiện 2 ở mục 3 mở rộng: way có thẻ xe đi được **và** `ref` mang số hiệu `CT`, `QL`, `AH` **hoặc đường tỉnh**.
- **Đường huyện (`ĐH`) không thuộc mạng A.**
- **Nhóm mới `tinhlo`.** Thứ tự ưu tiên khi một way mang nhiều số hiệu: `caotoc` > `quoclo` > `tinhlo`. Ví dụ `QL.2;ĐT.3` là `quoclo`. Way có thẻ `trunk` vẫn là `quoclo`, kể cả khi số hiệu là đường tỉnh.

### 13.2 Số đo trên PBF hiện tại
Thêm khoảng **28.100 km** đường tỉnh: `secondary` 21.800 km, `primary` 5.500 km, `tertiary` 700 km, còn lại không đáng kể. Mạng A tăng từ 38.300 km lên khoảng **66.500 km**.

### 13.3 Hệ quả
- Nhiều đường `secondary` và `primary` trở thành đường A, nên số điểm giao B và C sẽ thay đổi (đường đã là A thì không còn là "đường B giao vào").
- `meta.km` thêm khóa `tinhlo`.
- `BIG_ROAD_POLICY_VERSION` tăng lên **4**.

### 13.4 Giao diện
- Đường tỉnh vẽ màu **vàng đậm `#d4b12f`**, dày **1,8**, nét liền.
- Nét đứt "chỉ nhờ số hiệu" **chỉ áp dụng cho cao tốc và quốc lộ**. Gần như mọi đường tỉnh đều chỉ thuộc A nhờ số hiệu, nên nếu cũng vẽ nét đứt thì chỉ thêm rối.
- Chú thích thêm mục "Đường tỉnh". Tóm tắt km tính cả đường tỉnh.

### 13.5 Kiểm thử
- Chính sách: `secondary`+`ĐT.70`, `primary`+`DT741`, `tertiary`+`TL 10` → `tinhlo`; `secondary`+`ĐH.12` → không thuộc A; `primary`+`QL.2;ĐT.3` → `quoclo`; `trunk`+`ĐT.5` → `quoclo`. Test cũ "`secondary`+`ĐT.70` không thuộc A" được đổi theo.
- Chạy thật: tổng km trong khoảng 5% quanh 66.500 km.

## 14. Bổ sung (đã duyệt 2026-10-19): tự nối khúc ở các điểm cụt

**Nguyên tắc** (người dùng): mạng đường lớn không được cụt. Ngoài đời, đi hết một quốc lộ thì luôn gặp đường khác. Các điểm cụt trên bản đồ chủ yếu do OSM **thiếu số hiệu** trên một vài đoạn, ví dụ quốc lộ qua thị trấn chỉ ghi tên phố. Mục 2 trước đây để việc nối khúc ra ngoài phạm vi; mục này đưa nó vào phạm vi.

### 14.1 Định nghĩa
- **Điểm cụt:** node ở đầu một way A mà không thuộc way A nào khác. Way khép kín không tính.
- **Đoạn nối bù:** từ mỗi điểm cụt, tìm đường ngắn nhất (theo km, không xét chiều) đi trên các way **không thuộc A** có thẻ `primary`, `secondary`, `tertiary` hoặc `_link` của chúng. Đường đi dừng khi gặp node đầu tiên thuộc **một way A khác** với way của điểm cụt. Đoạn nối phải dài **không quá 2 km**, điều chỉnh bằng tham số `--bridge-km`.
- Các phần way OSM nằm trên đoạn nối được thêm vào mạng A. Mỗi phần có `bridge = true`, nhóm lấy theo way A ở điểm cụt, các thẻ gốc giữ nguyên.
- Nhiều điểm cụt dùng chung một phần way thì phần đó chỉ thêm một lần.
- **Điểm giao trên đoạn nối bù:** một node trên đoạn nối là điểm giao khi có đường B hoặc C **khác** đi vào. Ngoài ra, ở hai đầu đoạn nối, nếu chính way gốc còn chạy tiếp ra ngoài đoạn nối thì đó cũng là điểm giao.
- **Điểm cụt còn lại** sau khi nối: ghi vào `big_road_junctions.geojson` với `type = "deadend"`. Điểm cụt cách một điểm giao biên giới dưới 200 m thì bỏ qua, vì đường chạy tiếp sang nước bạn.
- Khúc và khúc chính (mục 3) được tính **sau** khi nối.

### 14.2 Số đo (gồm cả đường tỉnh)
| Khoảng cách tới mạng A | Số điểm cụt |
|---|---|
| ≤ 0,5 km | 2.408 |
| 0,5–1 km | 189 |
| 1–2 km | 158 |
| 2–5 km | 139 |
| không tới được trong 5 km | 935 |
| **Tổng** | **3.829** |

Với ngưỡng 2 km, khoảng **72 %** số điểm cụt được nối. Ngưỡng 5 km chỉ thêm 4 % nhưng dễ nối sai qua đường không liên quan, nên chọn 2 km.

Số điểm cụt còn lại, khoảng 1.000, gồm các loại: cửa khẩu, bến phà, đoạn đang xây, đoạn nối tiếp là đường nhỏ (`residential`), và **đường tỉnh cụt thật**. Đường tỉnh có thể kết thúc ở cảng, xã vùng cao hay khu du lịch, nên không phải điểm cụt nào cũng là lỗi dữ liệu. Các điểm này được hiện lên để người dùng xem.

### 14.3 Đầu ra
- Đường: thêm trường `bridge` (bool).
- `meta` thêm: `bridges` (số phần way), `bridge_km`, `dead_ends_before`, `dead_ends_after`; `junctions` thêm khóa `deadend`.

### 14.4 Giao diện
- Đoạn nối bù vẽ **nét chấm** `2 3`, màu theo nhóm, để thấy ngay chỗ nào do máy tự nối. Rê chuột thì có thêm dòng "Nối bù (OSM thiếu số hiệu)".
- Điểm cụt vẽ **vòng tròn rỗng** bán kính 4, viền đen `#222` dày 1,5. Có ô tích **"Điểm cụt"**, mặc định bật.
- **Sửa sau khi chạy thật:** điểm cụt chỉ hiện **từ mức phóng 9**, giống điểm giao B/C (mục 11). Lúc đầu spec cho luôn hiện, nhưng 867 vòng tròn phủ đen bản đồ cả nước. Phân bố: khoảng 600 điểm trên đường tỉnh, 154 điểm ở nhánh nối, khoảng 90 điểm trên tuyến chính quốc lộ hoặc cao tốc.
- **Ngoại lệ** (người dùng duyệt 2026-10-19): điểm cụt trên **tuyến chính** quốc lộ hoặc cao tốc thì hiện **ở mọi mức phóng**, để rà lỗi dữ liệu. Tuyến chính nghĩa là nhóm `caotoc` hoặc `quoclo`, và thẻ `highway` không phải `_link`. Builder ghi trường `mainline` (bool) cho mỗi điểm `deadend`.
- Tóm tắt có thêm "N km nối bù · N điểm cụt".

### 14.5 Kiểm thử (file OSM giả lập)
- Quốc lộ đứt một đoạn 300 m chỉ ghi `primary` không số hiệu → có một đoạn `bridge`, hai khúc thành một, không còn `deadend`.
- Khoảng hở dài hơn `bridge-km` → không nối, có hai `deadend`.
- Khoảng hở chỉ nối được qua `residential` → không nối.
- Điểm cụt sát điểm giao biên giới → không ghi `deadend`.
- Một đường C cắt ngang đoạn nối bù → có điểm giao C ở đó; các node giữa đoạn nối không thành điểm giao.

## 15. Bổ sung (đã duyệt 2026-10-19): ẩn mảnh đường mồ côi

**Hiện tượng** (người dùng gọi là "giao điểm mồ côi"): có những chấm giao nằm một mình. Chúng thật ra nằm trên **mẩu đường A rất ngắn, tách khỏi mạng**, thường là nhánh nối `trunk_link` dài vài mét mà đường chính hai đầu lại không được ghi là quốc lộ. Hiện trên đất liền có 74 mẩu ngắn dưới 1 km, mang 169 điểm giao.

**Thiết kế.**
- Sau khi nối khúc (mục 14), khúc nào **không phải khúc chính và ngắn dưới 1 km** thì đánh dấu `fragment = true`, tham số `--fragment-km`.
- Giao diện **ẩn** đường và điểm giao thuộc mảnh này. File vẫn giữ để tra cứu.
- `meta` thêm `fragments` và `fragment_km`. Tóm tắt có thêm "đã ẩn N mảnh ngắn".

**Kiểm thử.** Một `trunk_link` 10 m nằm giữa hai đường `primary` không số hiệu, có một điểm giao B → đường có `fragment = true`. Dữ liệu truyền vào bản đồ không còn đường đó, cũng không còn điểm giao của nó.

## 16. Bổ sung (đã duyệt 2026-10-19): hỏi OSRM để nối bù ở các điểm cụt còn lại

**Yêu cầu của người dùng** (2026-10-19):
- nối qua phà có chở ô tô;
- nối qua đoạn đường nhỏ ngắn;
- đoạn đang xây giữ là điểm cụt nhưng ghi lý do;
- phà vẽ thành một đoạn đường và mang thời gian qua phà;
- và cách làm: **thử từ mỗi điểm cụt xem OSRM có đi tiếp được tới đường lớn gần đó không; đi được thì thêm đoạn đó vào bản đồ.**

**Vì sao hỏi OSRM thay vì tự viết luật.** OSRM tìm đường trên toàn bộ đường ô tô đi được (profile `osrm/tools/profiles/car.lua`):
- đi qua đường nhỏ (`residential` 25 km/h, `service` 15 km/h);
- đi qua phà, thời gian theo thẻ `duration` của OSM, không có thẻ thì tính 5 km/h;
- không đi đường đang xây, đường dự kiến, đường có `access=no` hoặc `access=private`;
- tôn trọng đường một chiều.

Chỗ cụt chỉ có trên bản đồ đường lớn, do cách lọc. Lấy chính OSRM làm trọng tài thì bản đồ đường lớn và việc tìm đường **luôn khớp nhau**, không phải viết lại luật lần thứ hai. Mỗi điểm cụt cũng thành một ca kiểm tra OSRM: chỗ OSRM không đi tiếp được trong cự ly ngắn là chỗ đáng xem.

### 16.1 Hiện trạng (sau mục 14)
Còn 867 điểm cụt đang hiện, 100 điểm trên tuyến chính.

Tại 100 điểm trên tuyến chính:
- khoảng 24 điểm là **bến phà** (khoảng 12 tuyến, mỗi tuyến hai bờ, gần hết ở đồng bằng sông Cửu Long);
- khoảng 19 điểm chạy tiếp bằng **đường nhỏ**;
- khoảng 17 điểm chạm **đường đang xây**;
- còn lại là đường liền nhưng quá xa, hoặc hết đường.

Lượt đo thử theo luật tương tự OSRM ước lượng: số điểm cụt đang hiện giảm còn khoảng **570**, trên tuyến chính còn khoảng **63**.

### 16.2 Cách nối (bước 2, chạy sau bước nối B/C của mục 14)
Bước nối B/C của mục 14 **giữ nguyên** và vẫn chạy offline. Bước này chỉ xử lý các điểm cụt còn lại, mỗi điểm cụt A như sau:

1. **Ứng viên:**
   - mỗi way A khác (không phải way của A) lấy **node gần A nhất**;
   - chỉ lấy node cách A **không quá 3 km** đường chim bay;
   - giữ tối đa **5 ứng viên** gần nhất.
   - *Sửa khi chạy thật:* **bỏ** một node ứng viên khi từ điểm cụt đi **dọc mạng đường lớn** (xét trong vòng 5 km) tới way của nó mà quãng đường **không quá 2 lần** đường chim bay. Nối tới những way đó không có ích. Ở bến phà giữa thị trấn, ví dụ Đình Khao, chúng còn chiếm hết 5 chỗ ứng viên, khiến bờ bên kia bị loại. Bản đầu loại mọi way tới được trong 5 km, kể cả chỗ hở mà hai bên chỉ gặp nhau qua một vòng đường dài, nên đã bỏ sót các chỗ đó.
2. **Hỏi OSRM bằng một lệnh `/table`** gồm A và các ứng viên, lấy cả quãng đường lẫn thời gian, **cả hai chiều** (A → ứng viên và ứng viên → A).
3. **Mỗi chiều chọn ứng viên có quãng đường ngắn nhất**, rồi gọi **`/route`** với `annotations=nodes,distance,duration` (giống `estimate_spatial.py`) để lấy dãy id node OSM, quãng đường và thời gian từng đoạn.
4. **Cắt tuyến:** chỉ giữ phần **không nằm trên way A nào**, tính từ chỗ tuyến rời way của A tới node đầu tiên thuộc một way A khác. Phần chạy dọc đường A bị bỏ.
5. **Nhận nối khi:**
   - phần **đường bộ** của đoạn đã cắt dài **≤ 2 km** (tham số `--osrm-bridge-km`);
   - phần **phà**, nếu có, dài **≤ 15 km** (tham số `--ferry-km`).

   Hai chiều được xét riêng. Cả hai đạt thì thêm cả hai, ví dụ đường đôi; một chiều đạt thì thêm chiều đó.
6. **Gắn thẻ:** đọc PBF thêm **một lượt** (chỉ way, không cần tọa độ) để lấy thẻ của các way chứa những cặp node trên đoạn nối. Đoạn nối được tách thành các phần theo way, mỗi phần có `bridge_kind`:
   - `ferry` nếu way có `route=ferry`;
   - `road` nếu way là đường B/C;
   - `small` nếu là loại đường khác.

   Phần nào không tìm thấy way OSM chứa cặp node đó thì **bỏ cả đoạn nối**, và ghi vào `meta.osrm_unmatched`.

Mọi phần đều có `bridge = true` và nhóm lấy theo way của điểm cụt, như mục 14.

### 16.3 Phà
Phần `ferry` có thêm:
- `ferry_name`: tên phà, rỗng nếu không có;
- `car_confirmed` (bool): `true` khi way có `motor_vehicle` hoặc `motorcar` là `yes` hay `designated`;
- `ferry_min`: **tổng thời gian OSRM** trên các đoạn phà, làm tròn lên phút. Như vậy con số khớp với thời gian OSRM dùng khi tìm đường thật;
- `ferry_min_source`: `"osm"` nếu way có thẻ `duration`, ngược lại `"osrm_5kmh"`.

`ferry_min` chỉ là thời gian phà chạy. Thời gian chờ phà và giờ hoạt động thuộc spec ma trận thời gian.

### 16.4 Lý do điểm cụt
Lượt đọc PBF ở bước 6 cũng lấy các way chạm vào điểm cụt còn lại. Mỗi điểm `deadend` có thêm `reason`, xét theo thứ tự:

| `reason` | Khi nào | Chữ trong tooltip |
|---|---|---|
| `construction` | node chạm way có `highway=construction` hoặc thẻ `construction=*` | "Đường đang xây" |
| `continues` | node chạm đường xe đi được hoặc phà, nhưng OSRM không đi tiếp tới đường lớn trong giới hạn ở mục 16.2 | "Đường chạy tiếp nhưng OSRM không nối được trong 2 km" |
| `end` | không chạm đường nào khác | "Hết đường trong OSM" |

### 16.5 Khi không có OSRM
- Builder kiểm tra OSRM trước bước 2:
  - gọi `/nearest` như `/api/health`;
  - so `osrm/build.json` `input.pbf_sha256` với PBF đang dựng.
- OSRM không chạy hoặc dựng từ PBF khác thì **bỏ qua bước 2**, ghi `meta.osrm_bridging = "skipped: <lý do>"`. Mọi phần khác vẫn dựng bình thường, `reason` vẫn được tính.
- Địa chỉ OSRM lấy theo `OSRM_BASE_URL`, mặc định `http://127.0.0.1:5000`.
- Lỗi khi gọi OSRM cho **một** điểm cụt (hết giờ chờ, mã lỗi) thì bỏ qua điểm đó và đếm vào `meta.osrm_errors`, không làm hỏng cả lượt dựng.
- *Bổ sung khi chạy thật* (người dùng duyệt 2026-10-20):
  - `/route` báo **`NoRoute`** cho một ứng viên mà `/table` vẫn tới được (do cách bắt điểm khác nhau) thì **thử ứng viên tiếp theo**, không tính là lỗi;
  - lỗi khác thì vẫn bỏ qua điểm cụt đó;
  - `meta.osrm_error_samples` giữ tối đa 20 lỗi, mỗi lỗi gồm vị trí và thông báo OSRM (mã và lời nhắn).

### 16.6 Thiết kế mã
- `build(...)` nhận thêm `router=None`. Mặc định là bộ tìm đường gọi OSRM thật, có hai hàm:
  - `table(points) -> (distances, durations)`;
  - `route(a, b) -> {"nodes", "distances", "durations"}`.
- Test truyền bộ tìm đường giả, nên **không cần OSRM**, giống cách backend tiêm bộ ước lượng.
- *Ghi chú khi làm:*
  - `route` trả thêm `coords`, là tọa độ khớp từng node, để vẽ đoạn nối mà không phải đọc lại PBF có tọa độ;
  - `build()` mặc định `router=None`, nghĩa là bỏ qua bước 2 với lý do "không dùng OSRM", để test luôn offline;
  - dòng lệnh dùng `OsrmRouter` thật, trừ khi có `--no-osrm`;
  - một tuyến mà hai điểm cụt ở hai đầu cùng tìm ra thì chỉ xét và đếm **một lần**.
- Tham số dòng lệnh mới: `--osrm-bridge-km`, `--ferry-km`, `--no-osrm`.
- `meta` thêm:
  - `osrm_bridging` (`"ok"` hoặc lý do bỏ qua);
  - `osrm_bridges` (số đoạn), `osrm_errors`, `osrm_unmatched`;
  - `bridge_km_by_kind`: `{"road", "small", "ferry"}`;
  - `ferries` và `ferries_unconfirmed` (số tuyến phà được nối, số tuyến không ghi chở ô tô);
  - `dead_end_reasons`: `{"construction", "continues", "end"}`.
- `BIG_ROAD_POLICY_VERSION` tăng lên **5**.
- `app.py setup` dựng lại bản đồ đường lớn **sau khi OSRM đã chạy**, để có bước 2.

### 16.7 Giao diện
- **Phà** vẽ thành đường: xanh dương `#2f6fb5`, dày 2,5, nét đứt `8 6`. Phà chưa xác nhận chở ô tô có độ mờ 0,6.
- Rê chuột vào phà hiện:
  - tên phà;
  - "khoảng N phút";
  - nguồn thời gian: "theo OSM" hoặc "OSRM ước tính 5 km/h";
  - "OSM chưa ghi chở ô tô" nếu `car_confirmed = false`.
- Đoạn nối qua **đường nhỏ** vẽ nét chấm như mục 14.4, tooltip "Nối bù qua đường nhỏ (theo OSRM)".
- **Điểm cụt:** tooltip thêm lý do theo bảng 16.4.
- **Vị trí chú thích** (người dùng, 2026-10-19: chú thích đang đè lên bản đồ): chú thích chế độ đường lớn nằm **dưới khung bản đồ**, không đè lên bản đồ. Các ghi chú góc trên phải xếp ngay dưới hai nút bật/tắt.
- **Chú thích:** thêm mục "Phà". Tóm tắt thêm "N tuyến phà". Nếu `osrm_bridging` khác `"ok"`, hiện ghi chú "Chưa nối bù qua OSRM: <lý do>".

### 16.8 Kiểm thử
**Builder**, dùng file OSM giả lập và bộ tìm đường giả:
- **Phà có chở ô tô:** quốc lộ đứt ở hai bến phà, phà có `motor_vehicle=yes` và `duration=00:20`. Bộ tìm đường trả tuyến qua phà, thời gian 1.200 s → có phần `ferry` với `ferry_min = 20`, nguồn `osm`, `car_confirmed = true`; hai khúc thành một; không còn `deadend` ở hai bến.
- **Phà không thẻ:** bộ tìm đường trả thời gian 2.160 s → `ferry_min = 36`, nguồn `osrm_5kmh`, `car_confirmed = false`.
- **Đường nhỏ:** khoảng hở bằng `residential`, bộ tìm đường trả tuyến 300 m → có phần `small`.
- **Tuyến quá dài:** bộ tìm đường trả tuyến 2,5 km → không nối, `reason = "continues"`.
- **Cắt tuyến:** tuyến chạy dọc way A 200 m rồi mới rẽ → phần đi dọc đường A không có trong đoạn nối.
- **Hai chiều:** chỉ chiều đi có tuyến → nối một đoạn; hai chiều khác tuyến → nối hai đoạn.
- **Lỗi và thiếu dữ liệu:**
  - bộ tìm đường báo lỗi ở một điểm → `osrm_errors = 1`, các điểm khác vẫn nối;
  - tuyến có cặp node không thuộc way nào → bỏ, `osrm_unmatched = 1`.
- **Lý do điểm cụt:** quốc lộ chạm `highway=construction` → `construction`; không chạm đường nào → `end`.
- **Không có bộ tìm đường** (`--no-osrm`) → `osrm_bridging` bắt đầu bằng `"skipped"`, bước nối B/C của mục 14 vẫn chạy.

**Giao diện:**
- kiểu vẽ phà: màu, nét đứt, độ mờ khi chưa xác nhận;
- tooltip điểm cụt có lý do;
- chú thích có "Phà";
- có ghi chú khi `osrm_bridging` khác `"ok"`.

**Chạy thật** (cần OSRM đang chạy):
- số điểm cụt đang hiện nằm trong khoảng ±15% quanh ước lượng ở mục 16.1;
- thời gian dựng thêm không quá 5 phút;
- trên trình duyệt kiểm tra phà Đại Ngãi (QL.60) và Đình Khao (QL.57), cùng một điểm cụt `construction` trên CT.41.

## 17. Rủi ro
- **Hiệu năng trình duyệt.** Mạng ban đầu có khoảng 40 nghìn đường và 18 nghìn điểm, đã giảm tải bằng canvas, làm gọn hình và ẩn điểm giao B/C dưới mức phóng 9 (mục 11). Thêm đường tỉnh (mục 13) làm dữ liệu tăng khoảng 1,7 lần, file nén ước khoảng 4,5 MB. Nếu kéo và phóng cả nước bị giật thì đường tỉnh chỉ vẽ từ mức phóng 7; thay đổi này phải ghi lại vào spec.
- **Nhiều khúc rời nhau**, do thiếu số hiệu hoặc do đảo. Đó chính là thông tin cần thấy ở bước này. Việc nối khúc thuộc spec sau.
- **Bộ nhớ khi đọc PBF:** lượt đo thử đã chạy được trên máy hiện tại (32 GB RAM).

## 18. Bổ sung (đã duyệt 2026-10-20): thiết kế lại giao diện "Chỉ đường lớn"

Người dùng chọn phạm vi **A + B + C**: sửa lỗi và bố cục (A), bộ công cụ rà lỗi dữ liệu (B), và ba lớp gắn với ma trận VRP (C). Bản đồ này phục vụ hai việc: **rà lỗi mạng A** trước khi tính ma trận thời gian, và **cho thấy ma trận được tính trên mạng nào**.

### 18.1 Hiện trạng (kiểm tra trên trình duyệt, 2026-10-20)
- Đã chạy đúng: ảnh nền chỉ trong Việt Nam; chú thích nằm dưới bản đồ; tooltip phà ("Phà Đại Ân - Long Phú, khoảng 15 phút, OSRM ước tính 5 km/h").
- Có 74 điểm cụt trên tuyến chính: 46 `continues`, 15 `construction`, 13 `end`. Mục 16.8 ghi một điểm `construction` trên CT.41 nhưng thực tế không có. Các điểm `construction` nằm trên CT.40 (vành đai 3, cầu Bình Gởi), CT.24 (cầu Km29), QL.20B (đường T1), QL.60 và QL.21. Lần kiểm tra trình duyệt của mục 16.8 dùng các điểm này thay cho CT.41.
- **Lỗi L1:** state zoom khởi tạo bằng 11, nên khi vừa bật chế độ có khoảng 30 nghìn điểm B/C hiện ở mức cả nước rồi mới tắt.
- **Lỗi L2:** mặt nạ trắng cắt vào đất liền ven biển, ví dụ vạt Đại An, Duyên Hải (Trà Vinh). Vòng đất liền ghép từ bờ biển OSM nên không khớp ảnh nền. Nguyên nhân cụ thể (đường bờ biển OSM lệch, hay do bước làm gọn) chưa xác minh. Cách sửa ở 18.2 tránh được lỗi này dù nguyên nhân là gì, vì lỗ của mặt nạ không còn bám theo bờ biển.
- **Lỗi L3:** bản đồ chỉ rộng khoảng nửa khung, vì form lập hành trình vẫn chiếm nửa còn lại.
- **Lỗi L4:** từ mức phóng 9, các chấm B/C che kín đường. Ở mức cả nước, ô biên giới và vòng điểm cụt lấn át màu tuyến. Nét phà mảnh, khó thấy.

### 18.2 Sửa lỗi (A)
- **L1:** zoom ban đầu là "chưa biết". Khi chưa biết thì ẩn B/C và các điểm cụt không thuộc tuyến chính. `ZoomReporter` báo mức thật ngay khi bản đồ sẵn sàng.
- **L2:** lỗ của mặt nạ chuyển sang **vòng ranh giới quốc gia** (quan hệ `admin_level=2`, `ISO3166-1=VN`, gồm cả đoạn trên biển, tức lãnh hải). Ghép vòng ngoài từ mọi way thành viên `outer`, theo id node, cho phép đảo chiều.
  - Đầu ra mới `vietnam_boundary.geojson`: một Polygon, `properties: {"source": "osm_admin_level_2", "tolerance_m": 100, "points": n}`.
  - API mới `GET /api/big-roads/boundary`. Chưa có file thì trả 404 tiếng Việt như mục 4.3.
  - Giao diện: có ranh giới thì dùng ranh giới làm lỗ mặt nạ. Không có thì dùng vòng đất liền như cũ.
  - Vòng đất liền vẫn dùng để đánh dấu đảo (mục 8.2) và khóa vùng xem.
  - Kết quả: biển trong lãnh hải hiện ảnh nền; nước ngoài và biển xa vẫn trắng.
  - Ghép không được: không ghi file, ghi lý do vào `meta.boundary_status`.
- `BIG_ROAD_POLICY_VERSION` tăng lên **6** (cho cả 18.2 và 18.9), để `app.py setup` tự dựng lại.

### 18.3 Bố cục mới (A)
- Ở chế độ đường lớn, form lập hành trình **thu thành thanh dọc hẹp** (khoảng 56 px) chứa nút "Lập hành trình". Bấm nút thì form mở lại và chế độ đường lớn tắt.
- Khung chia hai cột:
  - **bản đồ** chiếm phần còn lại, cao `min(78vh, 820px)`;
  - **cột phụ** rộng 340 px bên phải (18.5, 18.7), thu gọn được về 0 bằng một nút. Trên màn hẹp dưới 900 px, cột phụ xuống dưới bản đồ.
- **Thanh công cụ một hàng, trên bản đồ:**
  - ô tìm tuyến (18.6);
  - ba chip nhóm đường: Cao tốc, Quốc lộ, Tỉnh lộ (18.8);
  - nút "Màu theo: Loại đường / Vận tốc" (18.9);
  - menu "Lớp" gom các checkbox hiện có (B, C, biên giới, điểm cụt) cùng Phà, Cụm (18.10), Ảnh nền.
  - Nút "Chỉ đường lớn" giữ nguyên chỗ.
- **Dưới bản đồ:**
  - hàng thẻ số liệu: tổng km, km theo nhóm, điểm cụt (tuyến chính / tất cả), số tuyến phà, km nối bù, số khúc;
  - một dòng chú thích ký hiệu;
  - dòng tóm tắt dài hiện tại bị bỏ.
- Dòng "Bản đồ nền cần kết nối mạng" không còn đè lên bản đồ. Nó chuyển xuống dòng chú thích và chỉ hiện khi ảnh nền bật.

### 18.4 Mật độ hiển thị (A)
- `MIN_LOCAL_JUNCTION_ZOOM` đổi từ 9 thành **11**.
- Dưới mức phóng 8:
  - ô biên giới 5×5 (từ 8 trở lên giữ 7×7);
  - vòng điểm cụt bán kính 3 (từ 8 trở lên giữ 4).
- Phà dày **4**, nét đứt `6 4`, độ mờ 0,9; phà chưa xác nhận chở ô tô mờ 0,55.

### 18.5 Bảng "Việc cần rà" (B)
- Nằm ở cột phụ. Có ba loại mục:
  - **điểm cụt** (mặc định chỉ tuyến chính; có công tắc "Tất cả");
  - **phà chưa xác nhận** chở ô tô;
  - **lỗi OSRM** (`meta.osrm_error_samples`).
- Mỗi dòng hiện: biểu tượng loại, tên tuyến (`big_road` hoặc `ferry_name`), lý do bằng tiếng Việt (bảng 16.4), trạng thái.
- Lọc theo lý do (`continues`, `construction`, `end`) và theo trạng thái. Đầu bảng hiện số đếm "còn N / tổng M".
- **Bấm một dòng:** bản đồ bay tới (mức phóng 15) và nhấp nháy điểm đó khoảng 2 giây. Bấm vào điểm cụt trên bản đồ thì chọn đúng dòng tương ứng.
- **Trạng thái mỗi mục:** "chưa xem" (mặc định), "đã xem" hoặc "bỏ qua", kèm một ghi chú ngắn tùy chọn. Trong API, ba trạng thái này có mã `unseen`, `seen`, `skipped`.
  - Lưu ở máy chủ, file `data/big_road_review.json`, khóa theo `node_id`. Dùng phà thì khóa là `ferry:<way_id>`.
  - Id node OSM giữ nguyên khi dựng lại, nên trạng thái còn qua các lần dựng.
  - Mục không còn trong dữ liệu mới thì giữ trong file nhưng không hiện.
- **API:**
  - `GET /api/big-roads/review` trả `{items: {key: {status, note, updated_at}}}`;
  - `PUT /api/big-roads/review/{key}` nhận `{status, note}`. `status` ngoài ba giá trị trên → 422. `note` dài quá 500 ký tự → 422.
  - Ghi file theo kiểu ghi file tạm rồi đổi tên.
- **Liên kết OSM** (mở tab mới, `rel="noopener noreferrer"`):
  - "Xem trên OSM": `https://www.openstreetmap.org/node/<id>` (phà: `/way/<id>`);
  - "Sửa trên OSM": `https://www.openstreetmap.org/edit?node=<id>`.
- Nút **"Xuất CSV"** tải danh sách đang lọc. Cột: loại, khóa, tuyến, lý do, trạng thái, ghi chú, kinh độ, vĩ độ. Ghi UTF-8 có BOM để Excel đọc đúng tiếng Việt.
- Chữ lấy từ OSM chỉ hiển thị dạng text node, không chèn HTML (như tooltip hiện có).

### 18.6 Tìm tuyến theo số hiệu (B)
- Ô nhập tìm theo hai cách:
  - **số hiệu**, chuẩn hóa trước khi so: bỏ dấu chấm và khoảng trắng, không phân biệt hoa thường, `DT` và `TL` coi như `ĐT`. Ví dụ "ql1" khớp "QL.1" nhưng không khớp "QL.1A". So khớp theo từng số hiệu trong `ref` tách bằng `;`.
  - **tên**: chuỗi con không phân biệt hoa thường và dấu.
- Danh sách gợi ý tối đa 10 tuyến, mỗi dòng có tổng km.
- **Chọn một tuyến:**
  - các đường của tuyến vẽ dày gấp đôi, các đường khác mờ còn 0,25;
  - bản đồ phóng vừa tuyến;
  - cột phụ hiện tổng km tuyến, số khúc rời, số điểm cụt trên tuyến.
- Esc hoặc nút × thì bỏ chọn.

### 18.7 Chi tiết một đoạn (B)
- Bấm vào một đường thì cột phụ hiện:
  - số hiệu, tên, nhóm, chiều dài đoạn (tính từ hình, công thức haversine);
  - loại nối bù (`road`, `small`, `ferry`), hoặc thông tin phà như tooltip 16.7;
  - vận tốc và nguồn vận tốc (18.9);
  - `way_id` có link "Xem trên OSM";
  - nút "Tô cả tuyến" (chạy 18.6 với số hiệu đầu tiên).
- Bấm ra chỗ trống thì đóng chi tiết, quay về bảng "Việc cần rà".

### 18.8 Chip nhóm đường (B)
- Ba chip Cao tốc, Quốc lộ, Tỉnh lộ bật hoặc tắt việc vẽ từng nhóm. Mặc định cả ba bật.
- Đoạn nối bù và phà theo nhóm của đường mà chúng nối.
- Tắt cả ba thì hiện ghi chú "Đã tắt mọi nhóm đường".

### 18.9 Tô theo vận tốc (C)
- Builder ghi thêm cho mỗi đường:
  - `speed_kmh`: là `maxspeed × 0,8` nếu OSM có `maxspeed` dạng số km/h. Hệ số 0,8 lấy theo `speed_reduction` của `car.lua`.
  - Nếu không có thì lấy vận tốc mặc định theo `highway` của `car.lua`: motorway 90, motorway_link 45, trunk 85, trunk_link 40, primary 65, primary_link 30, secondary 55, secondary_link 25, tertiary 40, tertiary_link 20, unclassified 25, residential 25, service 15.
  - Phà lấy `chiều dài / ferry_min`.
  - `speed_source` bằng `osm_maxspeed`, `profile_default` hoặc `ferry`.
- Bảng vận tốc mặc định nằm trong `model/osm_classes.py`, và có test so khớp với `car.lua` khi file này có mặt.
- **"Màu theo: Vận tốc"** dùng năm khoảng:
  - dưới 30 `#c0392b`;
  - 30–50 `#e67e22`;
  - 50–70 `#f1c40f`;
  - 70–90 `#7fb800`;
  - từ 90 trở lên `#1e8449`.
- Nét đứt, nét chấm và độ dày giữ như cũ. Chú thích đổi thành thang màu, kèm dòng "Vận tốc tự do theo profile OSRM, chưa tính kẹt xe".
- Khi có mẫu vận tốc thật trên mạng A (TomTom), nguồn này sẽ được thay theo một spec sau. Mục này không đổi bộ ước lượng.

### 18.10 Lớp cụm (C)
- Nếu `osrm/data/big_road_clusters.json` tồn tại, `GET /api/big-roads/clusters` trả nó. Nội dung `{method, k, created_at, ways: {way_id: cluster}}`. Không có file thì trả 404 và menu "Lớp" ghi "Cụm: chưa có dữ liệu".
- Bật lớp "Cụm" thì tô đường theo số cụm, bảng 12 màu xoay vòng. Lớp này thay "Màu theo".
- File cụm do một spec sau sinh ra, sau khi xem kết quả thử spectral clustering ở `reports/spectral_spike/`. Mục này chỉ định nghĩa định dạng file và cách hiển thị.

### 18.11 Tuyến A→B trên mạng A (C)
- Trong chế độ đường lớn, cột phụ có ô "Thử tuyến". Hai điểm chọn bằng cách bấm bản đồ, hoặc lấy từ tọa độ của form.
- **Chỉ mục cặp node của mạng A** (sửa ngày 2026-10-20, người dùng duyệt). `road_classes.sqlite` chỉ phủ khung Hà Nội–Hải Phòng và không có phà, nên không dùng được cho cả nước.
  - Builder ghi thêm `osrm/data/big_road_edges.sqlite`. Bảng `edges(a INTEGER, b INTEGER, way_id INTEGER, kind TEXT)` gồm mọi cặp node OSM liền nhau, lấy trên **hình gốc, trước khi làm gọn**, của:
    - mọi way A không phải `fragment` (vẫn gồm way trên đảo và khúc cô lập);
    - mọi đoạn nối bù B/C (mục 14) và nối bù qua OSRM (mục 16), cả phà.
  - Cột `kind` nhận `a`, `road`, `small` hoặc `ferry`. Cặp lưu theo thứ tự `a < b`, khóa chính `(a, b, way_id)`.
  - Bảng `metadata(key TEXT PRIMARY KEY, value TEXT)` có hai dòng `pbf_sha256` và `policy_version`.
  - File được ghi kiểu nguyên tử, như các file khác.
- **API** `POST /api/big-roads/route` nhận `{origin, destination}` và gọi OSRM `/route` với `annotations=nodes,distance,duration`. Với mỗi cặp node liên tiếp:
  - sắp lại cặp để `a < b`, rồi tra trong `big_road_edges.sqlite`;
  - có trong bảng thì cặp đó **trên mạng A**, gồm cả đoạn nối bù và phà;
  - không có thì **ngoài mạng A**, không suy đoán.
- `pbf_sha256` của file phải khớp `osrm/build.json`. Thiếu file → 404 "Chưa dựng bản đồ đường lớn…". Lệch hash → 422, cùng thông điệp như `/api/estimate`.
- Không còn dùng `road_classes.sqlite` và `big_roads.geojson` cho việc này.
- **Trả về:**
  - các đoạn liền nhau cùng cờ `on_a`, mỗi đoạn có tọa độ, km và giây;
  - tổng km, tổng phút, km trên A, km ngoài A.
- **Lỗi:**
  - OSRM không chạy → 503 "OSRM chưa chạy";
  - NoRoute → 422 "Không tìm được tuyến giữa hai điểm";
  - chỉ mục khác bản đồ → 422, cùng thông điệp như `/api/estimate`.
- **Vẽ:** đoạn trên A màu `#1f4fd1` dày 5; đoạn ngoài A cùng màu, nét đứt `4 4`. Cột phụ hiện "X km trên mạng A, Y km ngoài mạng A, khoảng N phút (OSRM, chưa tính kẹt xe)".
- **Không làm ở mục này:** định tuyến **chỉ** trên mạng A, vì cần dựng thêm một bộ dữ liệu OSRM riêng. Việc đó để spec sau, khi bắt đầu ma trận.

### 18.12 Kiểm thử (TDD)
**Builder:**
- ghép vòng ranh giới quốc gia từ way `outer`, có đoạn trên biển và có way đảo chiều;
- thiếu quan hệ → `boundary_status` ghi lý do, không có file;
- `speed_kmh` và `speed_source`: có `maxspeed=60` → 48 `osm_maxspeed`; trunk không có maxspeed → 85 `profile_default`; phà 1,25 km trong 15 phút → 5 `ferry`;
- bảng vận tốc mặc định khớp `car.lua` (bỏ qua test nếu không có file).

**Backend:**
- `/boundary` và `/clusters` trả file, hoặc 404 khi thiếu;
- review: GET khi chưa có file trả rỗng; PUT ghi được; `status` sai → 422; `note` dài → 422; ghi kiểu nguyên tử;
- `/route` với OSRM giả: tách đúng các đoạn `on_a`; cặp không có trong chỉ mục là ngoài A; NoRoute → 422; OSRM không chạy → 503.

**Giao diện:**
- zoom chưa biết → không vẽ B/C (L1);
- mặt nạ dùng ranh giới khi có, vòng đất liền khi không;
- form thu thành thanh khi bật chế độ;
- chip nhóm ẩn đúng nhóm;
- chuẩn hóa số hiệu: "ql1" khớp "QL.1", không khớp "QL.1A"; "dt915" khớp "ĐT.915";
- bảng "Việc cần rà": lọc, đếm, đổi trạng thái gọi PUT, bấm dòng gọi bay tới;
- link OSM đúng dạng;
- CSV có BOM và đúng cột;
- thang màu vận tốc đúng ở các ngưỡng 29,9, 30, 90;
- lớp cụm báo "chưa có dữ liệu" khi API trả 404;
- tuyến thử vẽ hai kiểu nét.

**Trình duyệt:**
- vạt Đại An, Duyên Hải không còn bị che;
- bật chế độ không thấy B/C ở mức cả nước;
- rà một điểm `construction` trên CT.40 qua bảng "Việc cần rà";
- thử tuyến Hà Nội → Hải Phòng.

### 18.14 Xem các nhát cắt spectral trên bản đồ (đã duyệt 2026-10-20)
Người dùng muốn xem phân cụm phổ cắt mạng A như thế nào. Mục này thay định dạng file của 18.10 và mô tả cách sinh file đó.

**Sinh dữ liệu:** script mới `model/spectral_clusters.py`, chuyển từ `reports/spectral_spike/spectral_spike.py` sang code chính thức có test. Các bước:
1. Đọc `big_roads.geojson`, bỏ đường `fragment` và `isolated`.
2. Cắt đường tại đầu mút và đỉnh dùng chung (tọa độ làm tròn 1e-6).
3. Gộp các chuỗi nút bậc 2 thành đồ thị nút giao, giữ thành phần liên thông chính.
4. Trọng số cạnh là `1 / thời gian`, dùng vận tốc như bản thử: cao tốc 80, quốc lộ 50, tỉnh lộ 40, phà 15 km/h; thời gian tối thiểu 30 s.
5. Lấy Laplacian chuẩn hóa, giải `eigsh` một lần cho 64 vector riêng.
6. Với mỗi k thuộc {8, 16, 32, 64}: chạy NJW (chuẩn hóa từng hàng, `kmeans2 ++` với 10 lần khởi tạo, hạt giống cố định `seed = 0` để chạy lại ra cùng kết quả).

**File** `osrm/data/big_road_clusters.json`:
```
{method: "spectral_njw_inv_time", created_at, pbf_sha256, policy_version,
 partitions: [{k, ways: {way_id: cluster}, cuts: [[[lon,lat],...], ...], boundary: [[lon,lat],...],
               stats: {cut_edges, cut_km, cut_pct, boundary_nodes, contiguous, conductance_mean, km_min, km_median, km_max}}]}
```
- **`ways`:** mỗi way thuộc cụm chiếm phần lớn chiều dài của nó.
- **`cuts`:** hình của các cạnh đồ thị có hai đầu thuộc hai cụm khác nhau, sau khi gộp chuỗi; đây chính là các nhát cắt.
- **`boundary`:** các nút giao có ít nhất một cạnh bị cắt.
- **`stats`:** tính như bảng trong `reports/spectral_spike/README.md`.
- File được ghi kiểu nguyên tử. `pbf_sha256` lấy từ `big_roads.meta.json`.
- `app.py setup` chạy script này sau mỗi lần dựng lại đường lớn (khoảng 1 phút).

**API:** `GET /api/big-roads/clusters` vẫn như 18.10. Thiếu file thì trả 404. Khác `pbf_sha256` với `big_roads.meta.json` thì vẫn trả file, giao diện hiện cảnh báo "Dữ liệu cụm dựng từ bản đồ cũ".

**Giao diện:**
- Bật lớp "Cụm" thì hiện bộ chọn **k = 8 / 16 / 32 / 64**, mặc định 16.
- Đường tô theo số cụm, bảng 12 màu xoay vòng. Hai cụm kề nhau phải khác màu: tô màu tham lam trên đồ thị kề giữa các cụm.
- **Nhát cắt** vẽ đen, dày 4, kèm viền trắng để nổi trên mọi màu. Nút biên vẽ chấm đen bán kính 3.
- Cột phụ hiện bảng `stats` của k đang chọn.
- Rê chuột vào một nhát cắt thì hiện "Cắt giữa cụm X và cụm Y, Z km".

**Kiểm thử:**
- **script**, trên đồ thị giả gồm hai cụm dày nối với nhau bằng một cạnh dài:
  - k = 2 cắt đúng cạnh đó (`cut_edges = 1`, `boundary` có hai nút);
  - hai lần chạy cho cùng kết quả;
  - way nằm vắt hai cụm được gán theo phần dài hơn;
  - fragment không có trong `ways`;
- **giao diện:** bộ chọn k đổi phân vùng; tô màu kề nhau khác màu; nhát cắt có kiểu vẽ đúng; thiếu dữ liệu hiện "Cụm: chưa có dữ liệu".

### 18.15 Ba lỗi nhỏ sau lần kiểm tra trình duyệt (đã duyệt 2026-10-20)
1. **Vùng biển đảo bị che trắng.** Ranh giới có 4 vòng khép kín: đất liền cùng lãnh hải, hai vòng ở Trường Sa, một vòng ở Bạch Long Vĩ. File cũ chỉ giữ vòng lớn nhất.
   - `vietnam_boundary.geojson` đổi thành **MultiPolygon** chứa mọi vòng khép kín.
   - Mặt nạ dùng mọi vòng làm lỗ.
   - `properties.points` là tổng số điểm của các vòng.
2. **Dòng chú thích dưới bản đồ tràn ngang**, sinh thanh cuộn. Dòng này phải xuống hàng (wrap), không còn cuộn ngang.
3. **Tuyến thử không đưa bản đồ tới tuyến.** Khi có kết quả tuyến thử, bản đồ phóng vừa tuyến, giống như khi chọn tuyến trong ô tìm kiếm.

### 18.13 Thứ tự làm
1. A: L1, L2 (builder và API ranh giới), bố cục, mật độ.
2. B: bảng "Việc cần rà" (API trước), tìm tuyến, chi tiết đoạn, chip.
3. C: vận tốc (builder và giao diện), tuyến thử, lớp cụm (chỉ phần hiển thị).

Dựng lại builder một lần, sau khi xong cả 18.2 và 18.9.

## 19. Bổ sung (đã duyệt 2026-10-20): lớp "Tỉnh" và so sánh nhát cắt theo tỉnh với spectral

Người dùng muốn thấy 34 tỉnh, thành trên bản đồ, và biết ranh giới tỉnh cắt mạng A bao nhiêu chỗ so với spectral k = 32. Mục đích là chọn cách chia vùng cho bài toán VRP.

### 19.1 Dữ liệu có sẵn (đo trên PBF hiện tại, 2026-10-20)
- PBF có **34 quan hệ** `boundary=administrative`, `admin_level=4`, `ISO3166-2` bắt đầu bằng `VN-`. Đây là cách chia tỉnh sau sáp nhập 2025, ví dụ `VN-HN` Thành phố Hà Nội, `VN-SG` Thành phố Hồ Chí Minh, `VN-49` Tỉnh Vĩnh Long.
- Các quan hệ đó có từ 43 đến 234 way vai `outer`. Khánh Hòa có thêm một way vai `inner`.
- PBF còn 25 quan hệ `admin_level=4` của Lào, Campuchia và Trung Quốc. Các quan hệ này **bị bỏ**, vì không có `ISO3166-2` bắt đầu bằng `VN-`.

### 19.2 Dựng ranh giới tỉnh: script mới `model/build_provinces.py`
- **Lượt 1:** chỉ đọc quan hệ, lấy 34 quan hệ trên cùng danh sách way thành viên và vai (`outer`, `inner`).
- **Lượt 2:** đọc riêng các way đó, có tọa độ.
- **Ghép vòng:** theo id node, cho phép đảo chiều, dùng lại `merge_chains` của `build_big_roads.py`.
  - Mỗi vòng `inner` gắn vào vòng `outer` chứa nó.
  - Làm gọn bằng Douglas–Peucker, sai lệch 100 m như mục 8.1.
  - Các tỉnh làm gọn độc lập với nhau, nên chỗ hai tỉnh giáp nhau có thể hở hoặc chồng vài chục mét. Mức sai lệch này chấp nhận được cho việc hiển thị và gán nút giao (19.3).
- **Đầu ra** `osrm/data/vietnam_provinces.geojson`:
  - một FeatureCollection, kèm hai trường `pbf_sha256` và `policy_version` ở cấp ngoài cùng;
  - mỗi tỉnh là một Feature MultiPolygon, `properties: {iso, name, relation_id, area_km2, points}`;
  - ghi kiểu nguyên tử.
- **Tỉnh không ghép được:** bỏ tỉnh đó, ghi `{iso, name, reason}` vào trường `failed` ở cấp ngoài cùng của file. Thiếu cả 34 tỉnh thì không ghi file.

### 19.3 Cách chia mạng A theo tỉnh
- Dùng đúng đồ thị nút giao của `model/spectral_clusters.py` (`build_graph`), để hai cách chia được đo trên cùng một đồ thị.
- **Gán mỗi nút giao vào một tỉnh:**
  - nút nằm trong vùng của một tỉnh (trong vòng ngoài, ngoài vòng trong) thì thuộc tỉnh đó;
  - nút không thuộc tỉnh nào: gán vào tỉnh có ranh giới gần nhất nếu cách không quá 2 km; xa hơn thì đánh dấu "chưa gán";
  - nút rơi vào chỗ hai tỉnh chồng nhau: chọn tỉnh có mã `iso` nhỏ hơn (thứ tự chữ), để chạy lại ra cùng kết quả.
- Nhãn mỗi tỉnh là số thứ tự từ 0, theo `iso` tăng dần. Tính `ways`, `cuts`, `boundary` và `stats` như mục 18.14.
- Cạnh có ít nhất một đầu "chưa gán" thì không tính là nhát cắt. Số nút chưa gán ghi vào `stats.unassigned_nodes`.

### 19.4 File cụm (thay đổi định dạng của 18.14, chỉ thêm, không đổi trường cũ)
- Mỗi phần tử `partitions` có thêm:
  - `method`: `"spectral"` hoặc `"province"`;
  - `names` (chỉ có ở cách chia theo tỉnh): `{cluster: "Tỉnh …"}`.
- Cách chia theo tỉnh có `k` bằng số tỉnh thực có (bình thường là 34).
- `spectral_clusters.py` thêm cách chia theo tỉnh vào cuối `partitions` khi có `vietnam_provinces.geojson` cùng `pbf_sha256` với `big_roads.meta.json`. Không có file đó thì bỏ qua, và in ra lý do.
- Cuối lượt chạy, script in bảng so sánh "spectral k = 32 / tỉnh" ra màn hình.

### 19.5 Backend
- `GET /api/big-roads/provinces` trả `vietnam_provinces.geojson`. Thiếu file thì trả 404 như các file đường lớn khác.
- `/api/big-roads/clusters` giữ nguyên.

### 19.6 Giao diện
- **Lớp "Ranh giới tỉnh"** trong menu "Lớp", mặc định tắt.
  - Viền màu `#6b5b95`, dày 1,5, nét đứt `4 3`, không tô nền.
  - Rê chuột vào một tỉnh hiện tên tỉnh, dạng text node.
  - API trả 404 thì menu ghi "Ranh giới tỉnh: chưa có dữ liệu".
- **Bộ chọn cách chia** khi bật lớp "Cụm": các nút k = 8 / 16 / 32 / 64 như cũ, thêm nút **"Tỉnh (34)"**.
  - Chọn "Tỉnh" thì đường tô theo tỉnh, theo cách tô màu kề nhau khác màu của 18.14.
  - Nhát cắt và nút biên vẽ như 18.14.
  - Tooltip nhát cắt dùng tên tỉnh: "Cắt giữa Tỉnh A và Tỉnh B, Z km".
- **Bảng "So sánh cách chia"** ở cột phụ, hiện khi lớp "Cụm" bật và có cả hai cách chia. Hai cột: spectral k = 32 và tỉnh. Các dòng:
  - số cạnh bị cắt;
  - km bị cắt và tỉ lệ;
  - số nút biên;
  - số cụm liền mạch trên k;
  - conductance trung bình;
  - km mỗi cụm (nhỏ nhất / trung vị / lớn nhất);
  - số nút chưa gán (chỉ cột tỉnh).

  Ở mỗi dòng (trừ dòng km mỗi cụm), giá trị tốt hơn được tô đậm.

### 19.7 Thiết lập
`app.py setup` chạy `model/build_provinces.py` khi file tỉnh thiếu, hoặc khi `pbf_sha256`/`policy_version` của file khác `big_roads.meta.json`. Script tỉnh chạy trước `spectral_clusters.py`.

### 19.8 Kiểm thử (TDD)
**`build_provinces.py`** (file OSM giả lập):
- giữ hai tỉnh `VN-` và bỏ một quan hệ `admin_level=4` không có mã `VN-`;
- ghép được vòng từ way đảo chiều;
- vòng `inner` khoét đúng lỗ;
- tỉnh có vòng hở → có trong `failed`, các tỉnh khác vẫn ghi;
- ghi kiểu nguyên tử.

**Chia theo tỉnh:**
- nút trong lỗ `inner` không thuộc tỉnh đó;
- nút cách ranh giới 1 km được gán, nút cách 3 km thành "chưa gán";
- nút trong vùng chồng chọn `iso` nhỏ hơn;
- đồ thị giả hai tỉnh nối bằng một đường → `cut_edges = 1`;
- cạnh có đầu chưa gán không tính là nhát cắt;
- `names` và `method` đúng.

**Backend:** `/provinces` trả file, hoặc 404 khi thiếu.

**Giao diện:**
- kiểu vẽ ranh giới tỉnh;
- 404 hiện "chưa có dữ liệu";
- nút "Tỉnh (34)" đổi cách chia;
- tooltip nhát cắt dùng tên tỉnh;
- bảng so sánh đủ dòng và tô đậm đúng giá trị tốt hơn;
- không có cách chia theo tỉnh thì không hiện bảng.

**Chạy thật:** đủ 34 tỉnh, `failed` rỗng; số nút chưa gán dưới 1% số nút giao; chép bảng so sánh vào báo cáo cuối lượt.

## 20. Bổ sung (chờ duyệt): bố cục mới, bản đồ chiếm gần trọn màn hình

### 20.1 Hiện trạng (đo ngày 2026-10-20, khung 1440×900, chế độ "Chỉ đường lớn")
- Đầu trang gồm topbar 85px, tiêu đề lớn 93px và hàng tab 41px. Bản đồ bắt đầu từ y = 300.
- Bản đồ rộng 877px, cao 702px. Phần nhìn thấy không cuộn khoảng 877×600, tức chừng 40% màn hình.
- Thanh công cụ (667×72) và cụm nút góc phải (158×72) đè lên mép trên bản đồ.
- Cột phụ bên phải rộng 324px và dài khoảng 5700px.
- Thống kê và chú giải nằm dưới bản đồ, phải cuộn mới thấy.

### 20.2 Bố cục mới, áp dụng cho tab "Ước lượng hành trình" ở cả hai chế độ
- Bỏ khối tiêu đề lớn ("Hiểu đường. Ước lượng đúng.") và footer ở tab này. Hai tab còn lại giữ nguyên.
- Topbar còn 48px. Ba tab không gian làm việc chuyển vào topbar.
- Bên dưới topbar là hàng ngang cao `100vh − 48px`, không cuộn trang, gồm:
  - rail trái 56px;
  - ngăn kéo trái;
  - bản đồ chiếm phần còn lại.
- **Ngăn kéo trái** rộng 360px, có nút thu gọn. Nó đứng cạnh bản đồ, không đè lên. Khi đóng hay mở, gọi `invalidateSize()` cho bản đồ.
  - Bên trong là các thẻ, mỗi thẻ cuộn riêng.
  - Chế độ thường có thẻ "Hành trình" (form điểm A và B, ngày, giờ, phương pháp, kết quả).
  - Chế độ "Chỉ đường lớn" có các thẻ:
    - "Lớp": tìm tuyến, bật tắt nhóm đường, ảnh nền;
    - "Cụm": chọn cách chia, bảng so sánh;
    - "Duyệt";
    - "Thử tuyến";
    - "Tổng quan": các thẻ số liệu của BigStats.
  - Chọn một đoạn hay tuyến trên bản đồ thì ngăn kéo tự mở thẻ chứa chi tiết của nó (SegmentDetail hoặc RouteSummary).
- **Trên bản đồ chỉ còn:**
  - nút zoom của Leaflet;
  - nút "Chỉ đường lớn" và nút thu gọn ngăn kéo, gộp thành một cụm nhỏ ở góc trên phải, cao tối đa 36px;
  - chú giải ở góc dưới trái, thu được thành một chip. Chế độ thường chỉ có chú giải loại đường. Chế độ "Chỉ đường lớn" có chú giải nhóm đường và dải vận tốc;
  - khi đã có hành trình: một thẻ kết quả tóm tắt (phút, km) ở góc dưới phải, tối đa 280×64.
- Chỉ trạng thái của ngăn kéo (mở hay đóng, thẻ đang mở) được nhớ bằng `localStorage`. Mọi lần đọc và ghi đều bọc try/catch. Mặc định là mở, thẻ đầu tiên.
- Phím `[` mở hoặc đóng ngăn kéo. Phím này không tác dụng khi con trỏ đang ở ô nhập.
- Màn hình hẹp (< 768px): ngăn kéo thành tấm trượt từ dưới lên, cao tối đa 60vh, đè lên bản đồ. Đây là trường hợp duy nhất được đè.

### 20.3 Không đổi
- Logic, API, dữ liệu, màu, lớp bản đồ, tooltip, chuỗi tiếng Việt của các panel. Chỉ đổi chỗ đặt và khung chứa.

### 20.4 Kiểm thử (TDD, vitest)
- Tab hành trình không còn tiêu đề lớn và footer. Tab "Dữ liệu & kiểm định" vẫn có.
- Nút thu gọn ẩn ngăn kéo (`aria-expanded` = false) và bản đồ vẫn được hiển thị. Phím `[` cũng làm vậy, trừ khi đang gõ trong ô nhập.
- Chế độ "Chỉ đường lớn":
  - ngăn kéo có đủ năm thẻ;
  - thanh công cụ và BigStats nằm trong ngăn kéo, không nằm trong khung bản đồ;
  - chọn một đoạn thì thẻ chi tiết được mở.
- Chú giải thu thành chip rồi mở lại được.
- Trạng thái ngăn kéo được nhớ qua lần render lại. `localStorage` ném lỗi thì vẫn render bình thường.
- Toàn bộ 112 bài kiểm thử frontend hiện có vẫn qua, sửa selector nếu chỉ vì đổi chỗ.
- Kiểm tra bằng mắt ở 1440×900 và 375×812. Đạt khi bản đồ chiếm ≥ 70% diện tích khung ở 1440×900 lúc ngăn kéo mở, và ≥ 90% lúc đóng.

## 21. Bổ sung (chờ duyệt): tỉnh lộ được phép cụt

### 21.1 Sửa nguyên tắc của mục 14
Người dùng khảo sát thực tế (2026-10-20) và thấy hai điều:
- đường lớn có thể cụt thật ở chỗ nó hạ xuống dưới tỉnh lộ;
- tỉnh lộ là cấp thấp nhất của mạng A, nên điểm cuối của nó là điểm cụt hợp lệ.

Nguyên tắc mới:
- **Quốc lộ và cao tốc** vẫn không được cụt. Điểm cụt ở đó coi là nghi lỗi dữ liệu và vẫn được nối bù như mục 14 và 16.
- **Tỉnh lộ** được phép cụt. Chỉ nối bù khi đoạn hở là lỗ thủng của **chính tuyến đó**: hai đầu đoạn nối cùng số hiệu tỉnh lộ (`ref` giống nhau sau khi chuẩn hóa) và đoạn hở ngắn hơn `bridge-km` (2 km). Ví dụ là OSM thiếu số hiệu trên vài trăm mét.

### 21.2 Hiện trạng (đo trên `big_roads.geojson` hiện tại)
- Đoạn nối bù đang gắn vào tỉnh lộ dài khoảng 349 km. Trong đó 282 km đi qua đường B/C, 55 km qua đường nhỏ và 12 km qua phà.
- Gắn vào quốc lộ có 140 km, vào cao tốc 72 km.
- Còn 779 điểm cụt. 171 điểm nằm trên tuyến chính (quốc lộ hoặc cao tốc), 608 điểm còn lại chủ yếu là tỉnh lộ và đường có tên.

### 21.3 Thay đổi
1. **Builder:** từ điểm cụt của tỉnh lộ, bỏ cả bước nối bù offline (mục 14) và bước hỏi OSRM (mục 16), trừ trường hợp cùng số hiệu ở 21.1. Phà nằm trên chính tuyến tỉnh lộ vẫn giữ.
2. **Lý do điểm cụt:** thêm giá trị `province_end` cho điểm cụt tỉnh lộ không được nối. `meta.dead_end_reasons` có thêm khóa này.
3. **Giao diện:**
   - Điểm cụt `province_end` không vào danh sách duyệt, kể cả khi bật "Tất cả".
   - Trên bản đồ, chúng chỉ hiện từ mức phóng 11, vẽ vòng tròn xám `#888`.
   - Tooltip ghi "Cuối tỉnh lộ".
4. **Kéo theo:**
   - dựng lại `big_road_edges.sqlite` và file cụm;
   - chạy lại thí nghiệm gate (lượt 3) trên mạng mới, vì mạng A ngắn đi;
   - `meta` thêm `bridge_km_by_group`.

### 21.4 Kiểm thử (TDD)
- Tỉnh lộ dừng ở đường `residential` cách tỉnh lộ khác 1 km: không có đoạn nối, có điểm cụt `province_end`.
- Tỉnh lộ ĐT.295 hở 300 m (đoạn giữa chỉ ghi `secondary`, không số hiệu), hai đầu cùng ĐT.295: vẫn nối như cũ.
- Quốc lộ cụt cách quốc lộ khác 1 km theo đường `secondary`: vẫn nối như mục 14.
- Bước OSRM (mục 16) không gọi OSRM cho điểm cụt tỉnh lộ. Kiểm tra bằng router giả, đếm số lần gọi.
- Frontend:
  - `reviewItems` bỏ `province_end`, kể cả khi bật `all`;
  - điểm `province_end` không hiện dưới mức phóng 11;
  - tooltip ghi "Cuối tỉnh lộ".
- Chạy thật và ghi lại số km nối bù theo nhóm, cùng số điểm cụt theo lý do, trước và sau.

## 22. Bổ sung (đã duyệt 2026-10-20): bỏ hẳn phân cụm spectral, chỉ giữ Tỉnh và Sông

### 22.1 Lý do
Người dùng chốt hai điều:
- **Ma trận thời gian:** gọi thẳng `/table` của OSRM. Một bảng 2000×2000 mất 17,6 s và đúng tuyệt đối.
- **Chia bài toán VRP:** dùng vùng theo sông chính.

Phân cụm spectral (NJW với k = 8, 16, 32, 64, cùng `natural_spectral` và các gate đường nhỏ của bước 4a) không còn dùng vào đâu. Các mục 18.14 và 18.15 (phần spectral) cùng bước 3 và bước 4 của thí nghiệm gate chuyển thành lịch sử, không còn hiệu lực.

### 22.2 Thay đổi
1. **Đổi tên** `model/spectral_clusters.py` thành `model/clusters.py`. File mới giữ lại:
   - đồ thị nút giao của mạng A;
   - phân vùng theo tỉnh (`province`) và theo sông (`natural`), kèm `merge_components`, `node_labels`, `boundary`, `cuts` và `extra_gates`.

   Bỏ hẳn:
   - NJW, tùy chọn `--ks` và `--target`;
   - `allocate_k`, `natural_spectral_partition`;
   - phân vùng `spectral` và `natural_spectral`.

   Trường `method` ở đầu `big_road_clusters.json` đổi thành `"regions"`.
2. **Xóa** `model/full_network_gates.py` và `tests/test_full_network_gates.py`. Script này chỉ phục vụ ranh giới spectral. Nếu `runtime/full_network_nodes.idx` còn thì xóa luôn.
3. **`model/gate_matrix_experiment.py`:**
   - chỉ chạy `province` và `natural`;
   - bỏ nhánh `spectral_k`, phần kề nhau riêng của `natural_spectral` và nhãn `boundary_kind = spectral`;
   - file cụm còn phân vùng lạ thì bỏ qua và in cảnh báo, không dừng.
4. **`model/osrm_mld_boundary.py`:** bỏ các con số tham chiếu của spectral (1.832, 590). Chỉ giữ 350 của `natural`, và đổi chữ "spectral" trong chú thích.
5. **`app.py`:** `clusters_outdated` và bước `setup` gọi `model/clusters.py`. File cụm cũ còn phân vùng spectral thì coi là lỗi thời và dựng lại.
6. **Frontend:**
   - bỏ các nút k, `availableKs`, `isSpectral`, nút "Sông + spectral" và mọi chuỗi "spectral";
   - lớp Cụm chỉ còn hai nút "Sông (N)" và "Tỉnh (34)". Mặc định chọn Sông khi file có, nếu không thì chọn Tỉnh;
   - các nút bị bỏ thì xóa luôn CSS của chúng.
7. **Dựng lại** `osrm/data/big_road_clusters.json` rồi build frontend.
8. **Báo cáo cũ** trong `reports/` (`spectral_spike/`, `full_network_gates.json` và các bản gate cũ) giữ nguyên làm lịch sử, không xóa.
9. **Chạy lại lượt 1 và lượt 2** của thí nghiệm gate với hai phân vùng, để báo cáo khớp với file cụm mới.

### 22.3 Kiểm thử (TDD)
- Thêm một test tìm chữ `spectral` (không phân biệt hoa thường) trong `model/`, `download/`, `backend/`, `app.py`, `osrm/manage.py`, `frontend/src/` và `tests/`. Kết quả phải là 0, chỉ trừ chính file test này. Test phải hỏng trước khi xóa.
- File cụm dựng lại chỉ có hai phân vùng, `province` và `natural`, đủ các trường như trước.
- Thí nghiệm gặp file có thêm một phân vùng lạ thì bỏ qua nó và chạy tiếp.
- Frontend:
  - lớp Cụm chỉ có hai nút, mặc định là Sông;
  - khi file không có `natural` thì mặc định là Tỉnh.
- Toàn bộ test Python và vitest đều qua, và build thành công.
