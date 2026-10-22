# Miền Nam (sau sáp nhập 7/2025): hạn chế xe tải, bản 2 (tra cứu 21/10/2026)

Một agent nghiên cứu tổng hợp từ web, mở rộng [research_hcm.md](research_hcm.md) và [research_south.md](research_south.md). Quy tắc máy đọc nằm trong [rules_south.json](rules_south.json): 50 quy tắc, 17 mục `unresolved`.

**Độ tin cậy** (giống README):
- **Cao:** đã đọc toàn văn.
- **TB:** tóm tắt chính thức, hoặc tin báo dẫn văn bản hay cơ quan ban hành.
- **Thấp:** chỉ có tin báo, hoặc không rõ còn hiệu lực.

Tên đường trong JSON đã được đối chiếu với OSM (`osrm/data/vietnam-nobuildings.osm.pbf`) trong bbox của từng quy tắc. Chỉ 3 mốc không khớp: ref `ĐT743`, "Bến Đò", "Miếu Ông Cù" (khu Bình Dương cũ). Lưu ý: `osrm/data/road_classes.sqlite` hiện chỉ phủ vùng Hà Nội (bbox 105.3–107.1, 20.5–21.8), nên ước lượng tốc độ cho miền Nam cần dựng lại chỉ mục.

## 1. TP.HCM: QĐ 23/2018/QĐ-UBND, sửa bởi QĐ 23/2019/QĐ-UBND (Cao)

Toàn văn đọc qua caselaw.vn:
- QĐ 23/2018: https://caselaw.vn/van-ban-phap-luat/323635-quyet-dinh-23-2018-qd-ubnd-quy-dinh-ve-han-che-va-cap-phep-o-to-cho-hang-o-to-tai-luu-thong-trong-khu-vuc-noi-do-thanh-pho-ho-chi-minh
- QĐ 23/2019: https://caselaw.vn/van-ban-phap-luat/330617-quyet-dinh-so-23-2019-qd-ubnd-ngay-16-09-2019-sua-doi-bo-sung-quyet-dinh-23-2018-qd-ubnd-quy-dinh-ve-han-che-va-cap-phep-o-to-cho-hang-o-to-tai-luu-thong-trong-khu-vuc-noi-do-thanh-pho-ho-chi-minh

Tình trạng hiệu lực:
- Tháng 3/2026, Sở Xây dựng xác nhận văn bản vẫn áp dụng (nguồn trong research_hcm.md).
- Đến 21/10/2026 chưa thấy quyết định thay thế nào được ký.

**Phân loại xe (Điều 2)**, tính theo khối lượng chuyên chở ghi trong giấy kiểm định:

| Loại | Gồm |
|---|---|
| Xe tải nhẹ (khoản 9) | ô tô chở hàng dưới 1.500 kg; ô tô tải 1.500–2.500 kg; xe thí điểm |
| Xe tải nặng (khoản 10) | ô tô tải trên 2.500 kg; máy kéo; xe máy chuyên dùng; rơ moóc, sơ mi rơ moóc do ô tô kéo |

QĐ 23/2019 (Điều 1.2) loại khỏi phạm vi điều chỉnh: xe bán tải (không nêu ngưỡng) và xe tải van có khối lượng chuyên chở dưới 950 kg. QĐ 23/2019 không sửa Điều 3, Điều 4 hay Phụ lục 1; nó bãi bỏ Phụ lục 2–7 (biểu mẫu).

**Giờ cấm (Điều 3)**, áp dụng hằng ngày:

| Loại | Giờ cấm |
|---|---|
| Xe tải nhẹ | 6h–9h và 16h–20h |
| Xe tải nặng | 6h–22h, trừ các hành lang ở Phụ lục 1 |

**Ranh giới (Điều 4)** theo thứ tự: QL1 (Xa lộ Hà Nội → Nguyễn Văn Linh) → Xa lộ Hà Nội (QL1 → nút Cát Lái) → Mai Chí Thọ, Đồng Văn Cống → Võ Chí Công → cầu Phú Mỹ → đường trên cao → nút Khu A Nam Sài Gòn → Nguyễn Văn Linh → QL1.

Trên OSM:
- QL1 nay mang tên Đỗ Mười, Lê Đức Anh, Lê Khả Phiêu (ref QL1).
- Đoạn Ngã tư Thủ Đức – Cát Lái của Xa lộ Hà Nội nay là Võ Nguyên Giáp.

Điều 4.2: xe tải được chạy không hạn chế giờ trên chính các tuyến ranh.

**Phụ lục 1:** hành lang cho xe tải nặng. Mỗi hành lang là một quy tắc `open` `hcm_hl_*`.

| Giờ được chạy | Hành lang (từ → đến, qua) | Trong JSON |
|---|---|---|
| Không giới hạn | cảng Phúc Long: Xa lộ Hà Nội – Ngã tư Tây Hòa – Nguyễn Văn Bá – đường số 2 | có |
| Không giới hạn | các cảng dọc Lưu Trọng Lư. Hướng 1: Nguyễn Văn Linh – Huỳnh Tấn Phát – Lưu Trọng Lư (cả Liên cảng A5, Bến Nghé). Hướng 2: Lưu Trọng Lư – Huỳnh Tấn Phát – Trần Xuân Soạn – Tân Thuận 4 – đường nối – Nguyễn Văn Linh | có, trừ đoạn Tân Thuận 4 (không có trên OSM) |
| Không giới hạn | cảng ICD, đường số 1 (Thủ Đức): Nguyễn Văn Bá → nhánh sông Sài Gòn | có |
| Không giới hạn | chợ Bình Điền: Nguyễn Văn Linh → chợ | unresolved (đường không tên) |
| Không giới hạn | cảng Nhà Rồng, hướng ra: Trương Đình Hợi – Tôn Thất Thuyết – cầu Tân Thuận 2 – Nguyễn Văn Linh | có |
| 9h–16h | Mai Chí Thọ (Nguyễn Cơ Thạch → Đồng Văn Cống) | có |
| 9h–16h | Trần Xuân Soạn (Huỳnh Tấn Phát → Lê Văn Lương) | có |
| 9h–16h | Phạm Thế Hiển (Trịnh Quang Nghị → đường nối Phạm Thế Hiển – QL50) | có |
| 9h–16h | QL50 (Nguyễn Văn Linh → Phạm Thế Hiển) | có |
| 9h–16h | đăng kiểm 50.01S: QL1 – Kinh Dương Vương – trung tâm | unresolved (không biết vị trí trung tâm) |
| 9h–16h | đăng kiểm 50.03V: QL1 – Phú Châu | có |
| 9h–16h | đăng kiểm 50.03S: QL1 – Phạm Văn Đồng – QL13 – trung tâm | chỉ đoạn Phạm Văn Đồng |
| 9h–16h, 21h–22h | Lê Trọng Tấn (QL1 → KCN Tân Bình); đường nội bộ KCN Tân Bình (D7 – M1) | có; D7 không có trên OSM |
| 9h–16h, 21h–22h | đường số 14 Thủ Đức (QL1 → đường số 13) | có |
| 9h–16h, 21h–22h | nhà máy sữa: Xa lộ Hà Nội – Võ Văn Ngân – Thống Nhất – Đặng Văn Bi – đường số 6 | có |
| 9h–16h, 21h–22h | cảng Nhà Rồng, hướng vào: cầu Tân Thuận 1 – Nguyễn Tất Thành – cổng kho 5 | có, trừ chính cây cầu (xem dưới) |
| 9h–16h, 21h–22h | cảng Tân Thuận 2: Nguyễn Văn Linh – Huỳnh Tấn Phát – Bùi Văn Ba | có |
| 9h–16h, 21h–22h | cảng Lotus: Nguyễn Văn Linh – Nguyễn Thị Thập – Nguyễn Văn Quỳ | có |
| 8h–16h, 18h–22h | cảng Phú Định: QL1 – Võ Văn Kiệt – Hồ Học Lãm | có |

Cách mã hóa:
- Các hành lang chỉ mở cho xe có `payload_t` > 2,5. Phụ lục 1 không miễn giờ cao điểm cho xe tải nhẹ.
- Trên OSM, cả đường Võ Nguyên Giáp và Mai Chí Thọ đều mang tên của tuyến ranh. Vì vậy hai đoạn sau, dù nằm trong vùng, bị coi là ranh:
  - Võ Nguyên Giáp, đoạn cầu Sài Gòn → Cát Lái;
  - Mai Chí Thọ, đoạn hầm sông Sài Gòn → Đồng Văn Cống.

  Hai đoạn này được cấm bổ sung bằng các quy tắc `*_tuyen_bo_sung`.

## 2. TP.HCM: các cấm riêng

| Nội dung | Tin cậy | Nguồn |
|---|---|---|
| **Võ Văn Kiệt:** không có quy định riêng. Tuyến nằm trong nội đô, chịu giờ cấm chung; riêng đoạn QL1 – Hồ Học Lãm thuộc hành lang Phú Định. Đây là lời Sở GTVT năm 2019. | TB | https://tuoitre.vn/plo/so-giao-thong-noi-ve-gio-cam-xe-tai-tren-tuyen-vo-van-kiet-post526241.html |
| **Cầu Tân Thuận 1**, từ 6/12/2025 tới khi xây cầu mới: cấm xe tải trên 1,5 t (VTC ghi khối lượng chuyên chở), xe khách trên 16 chỗ, xe có trọng tải toàn bộ trên 6 t; chiều cao tối đa 2,5 m. Do Phòng CSGT thông báo ngày 4/12/2025. Lệnh này xung đột với hành lang Nhà Rồng hướng vào: xe đi vòng qua cầu Tân Thuận 2. | TB | https://tuoitre.vn/saigontimes/cam-xe-tai-cau-tan-thuan-1-luu-thong-on-dinh-gio-cao-diem/ ; https://vtcnews.vn/tp-hcm-dieu-chinh-giao-thong-ham-song-sai-gon-va-cau-tan-thuan-1-ar991047.html |
| **Hầm Thủ Thiêm** (hầm sông Sài Gòn): không thấy quy định thường trực riêng cho xe tải. Chỉ có lệnh đóng hầm một đêm (19–20/12/2025). Hầm nằm trong vùng nội đô, nên áp giờ cấm chung (suy luận). | Thấp | https://vtcnews.vn/tp-hcm-dieu-chinh-giao-thong-ham-song-sai-gon-va-cau-tan-thuan-1-ar991047.html |
| **Cầu Phú Mỹ:** không thấy lệnh cấm xe tải theo giờ. Cầu là tuyến ranh, được đi mọi giờ. | không thấy | tìm kiếm 21/10/2026 |
| **Cầu Rạch Chiếc:** không thấy lệnh cấm hiện hành. Chỉ có tin cũ về cầu xuống cấp và phân luồng xe dưới 3,5 t khi thông xe cầu mới. | không thấy | tìm kiếm 21/10/2026 |
| **Nguyễn Văn Linh**, làn hỗn hợp đoạn Huỳnh Tấn Phát – QL1: xe chở trên 2,5 t cấm 6h–8h, 11h–13h, 17h–19h, từ 9/5/2020. Chưa rõ còn áp dụng. | Thấp | https://tuoitre.vn/plo/cam-xe-tai-tren-25-tan-luu-thong-tren-duong-nguyen-van-linh-post573005.html |
| **Tây Lân** (Bình Chánh), chiều QL1 → Liên Ấp 1-2-3: cấm ô tô tải 6h–9h và 16h–19h, từ 12/12/2020. | Thấp | https://tuoitre.vn/plo/5-tuyen-duong-o-binh-tan-va-binh-chanh-se-cam-xe-theo-gio-post604768.html |
| **Khu công nghệ cao** (Thủ Đức cũ): xe tải trên 3,5 t cấm 6h–22h từ 1/6/2017; xe phục vụ nhà máy trong khu được cấp phép. Không có số văn bản. | Thấp | https://tuoitre.vn/plo/dieu-tra-mua-duong-cho-xe-di-vao-gio-cam-bai-1-xe-container-ram-rap-xuyen-qua-khu-cong-nghe-cao-post736547.html |
| **Cầu vượt thép ngã tư Thủ Đức:** cấm container và xe trên 5 t từ 27/7/2024, tạm thời trong lúc thi công metro. Chưa rõ đã dỡ chưa; để vào unresolved. | Thấp | https://tuoitre.vn/plo/tu-27-7-cam-container-va-xe-tai-tren-5-tan-luu-thong-qua-cau-vuot-thep-nga-tu-thu-duc-post801580.html |
| **Cửa ngõ sân bay Tân Sơn Nhất** (Trường Sơn, nhánh Bạch Đằng, nhánh Hồng Hà): chỉ là đề xuất năm 2024. Để vào unresolved. | Thấp | https://lsvn.vn/tp-hcm-du-kien-cam-xe-tai-chay-o-cua-ngo-san-bay-tan-son-nhat-theo-gio-1704888958-a139809.html |
| Các lệnh tạm thời đã hết hạn, không mã hóa: cấm xe từ 5 t trên cửa ngõ dịp 20–23/2/2025; cầu Bình Triệu 26/8–30/11/2025. | — | https://tienphong.vn/han-che-xe-tai-tren-5-tan-o-mot-so-tuyen-duong-cua-ngo-tphcm-post1718639.tpo |

## 3. TP.HCM: dự thảo thay thế năm 2026 (`active: false`, Thấp)

Diễn tiến:
- **5/2026:** Sở Xây dựng lấy ý kiến dự thảo.
- **8/2026:** Sở hoàn thiện dự thảo, đề xuất ban hành vào tháng 9/2026.
- **14/10/2026:** Thanh Niên vẫn gọi đây là đề xuất.

Nguồn:
- https://vnexpress.net/xe-tai-tren-mot-tan-co-the-bi-cam-vao-noi-do-tp-hcm-tu-6h-den-22h-5073586.html
- https://tienphong.vn/tphcm-du-kien-cam-o-to-tai-tren-1-tan-xe-khach-tren-29-cho-vao-noi-do-tu-6-22h-post1843158.tpo
- https://tuoitre.vn/tphcm-de-xuat-cam-xe-tai-tren-1-tan-vao-noi-thanh-tu-6h-den-22h-100260825165139228.htm
- https://thanhnien.vn/tphcm-tinh-han-che-xe-xang-dau-vao-trung-tam-tu-2028-185261014153500292.htm

Nội dung dự thảo:
- **Xe chở trên 1 t:** cấm 6h–22h trong vùng.
- **Xe chở đến 1 t:** cấm 6h–9h và 15h–20h. Bản tháng 5 ghi 16h–20h.
- **Vành đai** (giống ranh hiện hành): Đỗ Mười, Lê Đức Anh, Lê Khả Phiêu, Nguyễn Văn Linh, đường dẫn và cầu Phú Mỹ, Võ Chí Công, Đồng Văn Cống, Mai Chí Thọ, Võ Nguyên Giáp, Xa lộ Hà Nội.
- **Hành lang cho xe trên 1 t:** giữ các cảng và khu như hiện nay. Giờ chạy là 9h–15h và 20h–6h, hoặc 9h–15h và 22h–6h; chưa có danh sách chi tiết.
- **Khu Bình Dương cũ và Bà Rịa – Vũng Tàu cũ:** không dùng vành đai. Thay vào đó hạn chế theo tuyến (QL13, QL1K, Mỹ Phước – Tân Vạn...); chưa có chi tiết.
- **Từ 2028:** dự kiến có vùng phát thải thấp.

## 4. Khu Bình Dương cũ (TP.HCM)

| Nội dung | Tin cậy | Nguồn |
|---|---|---|
| **6 tuyến**, áp dụng từ 1/8/2022: QL13 (ngã 5 Phước Kiến – cầu Vĩnh Bình); Mỹ Phước – Tân Vạn (Phú Lợi – ĐT743A); ĐT743 (Phú Lợi – ĐT743B/Độc Lập); Nguyễn Thị Minh Khai (QL13 – Phú Lợi); Huỳnh Văn Cù (ngã 5 Phước Kiến – ngã ba Bến Đò); ĐT747 (Miếu Ông Cù – cầu Ông Tiếp). Xe kéo rơ moóc, sơ mi rơ moóc cấm 6h–8h và 16h30–18h30. Xe chở trên 10 t cấm 11h–13h. Do Sở GTVT Bình Dương ban hành, không có số văn bản. Chưa xác nhận còn áp dụng sau sáp nhập. | TB | https://bnews.vn/tu-1-8-binh-duong-bat-dau-cam-xe-theo-gio-tai-tuyen-duong-trong-diem/253249.html |
| **Thuận An**, từ 28/4/2025, theo chỉ đạo của Sở Xây dựng Bình Dương: (1) Nguyễn Du, đoạn ĐT743C – Hữu Nghị: xe trên 5 t cấm 6h–22h ở một chiều; chiều ngược lại cấm 6h–8h, 11h–13h, 16h30–18h30. (2) Nguyễn Du, đoạn Hữu Nghị – ĐT743B: đầu kéo cấm 6h–8h và 16h30–18h30; xe trên 10 t cấm 11h–13h. (3) Bùi Hữu Nghĩa: xe trên 5 t cấm 6h–8h, 11h–13h, 16h30–18h30. | TB | https://dantri.com.vn/thoi-su/chinh-thuc-cam-xe-tren-hai-tuyen-duong-ma-dan-tri-phan-anh-o-binh-duong-20250424132258004.htm |
| **Phường Đông Hòa**, xử phạt từ 15/9/2025: (1) ĐT743A (QL1K → Mỹ Phước – Tân Vạn) và Mỹ Phước – Tân Vạn nối dài (→ QL1A): xe trên 10 t (trừ đầu kéo) cấm 11h–13h; đầu kéo cấm 6h–8h và 16h30–18h30. (2) Đường Thống Nhất (QL1K → QL1A): cấm xe trên 10 t, bài không nêu giờ. | TB (Thống Nhất: Thấp) | https://voh.com.vn/giao-thong/tphcm-tang-cuong-xu-ly-xe-tai-chay-trong-gio-cao-diem-tu-159-619066.html |

Trên OSM không có ref ĐT743 (chỉ có ĐT.743A, ĐT.743B, ĐT.743C). Mốc "ngã 5 Phước Kiến" được hiểu là giao lộ QL13 – Huỳnh Văn Cù; đây là suy luận.

## 5. Khu Bà Rịa – Vũng Tàu cũ (TP.HCM)

**Không tìm thấy** văn bản hay thông báo đang hiệu lực nào về:
- giờ cấm xe tải ở Vũng Tàu, Bà Rịa, Phú Mỹ;
- khung giờ cho xe container vào cảng Cái Mép – Thị Vải.

Kết quả tìm kiếm chỉ có ba loại tin:
- văn bản chia khung giờ thời COVID năm 2021, đã thu hồi: https://plo.vn/vung-tau-thu-hoi-van-ban-chia-khung-gio-luu-thong-gay-kho-xe-luong-xanh-post647355.html;
- biển cấm đậu xe;
- tin về hạ tầng (đường 991, 991B).

Dự thảo 2026 nói sẽ hạn chế xe tải theo tuyến ở khu này, nhưng chưa có danh sách tuyến. Vì vậy JSON không có quy tắc `vt_` nào; mục này nằm trong `unresolved`.

## 6. Đồng Nai (gồm Bình Phước cũ)

| Nội dung | Tin cậy | Nguồn |
|---|---|---|
| **QĐ 20/2026/QĐ-UBND** (ký 24/3/2026, hiệu lực 10/4/2026). Giờ cao điểm: 5h–8h, 11h–13h, 16h–19h. Có hạn chế xe tải và xe khách trong đô thị theo khung giờ và phạm vi do Sở Xây dựng và UBND xã công bố. Khoản 3 Điều 6 cho cấp phép vào giờ cấm. Chưa đọc được toàn văn. | TB | https://voh.com.vn/giao-thong/dong-nai-ap-dung-quy-dinh-moi-ve-to-chuc-giao-thong-duong-bo-tu-1042026-650475.html ; https://baoxaydung.vn/dong-nai-nhieu-phuong-an-mo-loi-cho-xe-cua-doanh-nghiep-doc-tuyen-dong-khoi-192260909180526378.htm |
| **Đồng Khởi**, đoạn ngã tư Amata (QL1) – Nguyễn Văn Tiên (cây xăng 26), từ 25/8/2026: xe chở trên 2 t cấm 6h–8h30, 11h–13h30, 16h–21h; xe chở trên 5 t cấm 5h–22h. Phân làn mới từ 2/10/2026 không đổi giờ cấm. Xe của doanh nghiệp có cơ sở trên tuyến được đi nếu Công an TP Đồng Nai chấp thuận. | TB | https://tuoitre.vn/vu-cam-xe-tai-tu-sang-den-toi-o-dong-nai-tai-xe-chu-y-phan-lan-moi-tu-ngay-2-10-100261001155609232.htm |
| **Biên Hòa**, biển báo lắp từ 5/2020, theo bài trên cổng tỉnh năm 2021: (1) Nguyễn Ái Quốc, Phạm Văn Thuận: xe trên 5 t cấm 6h–22h; xe trên 2 t cấm 6h–8h, 11h–13h, 16h–22h. (2) Bùi Văn Hòa: xe trên 2 t cấm 6h–8h, 16h–19h. (3) Phan Trung, Trương Định, Lý Văn Sâm, Lê Quý Đôn, Đặng Văn Trơn, Nguyễn Phúc Chu: xe trên 5 t cấm 6h–22h. Chưa xác nhận còn đúng năm 2026. | Thấp | https://dongnai.gov.vn/en/news/Socio-Economic-information/large-trucks-prohibited-from-entering-inner-of-bien-hoa-city-4247.html |
| **Vành đai 3**, dự án thành phần 1A (Tân Vạn – Nhơn Trạch, gồm cầu Nhơn Trạch), từ 20/8/2025: cấm xe tải trên 1,5 t và xe khách trên 16 chỗ trong giai đoạn 1. Giai đoạn 2 dự kiến cuối 2026. | TB | https://vneconomy.vn/sau-ngay-198-xe-nao-duoc-luu-thong-qua-cau-nhon-trach.htm |
| **Bùi Hữu Nghĩa** (Biên Hòa), "từ 5 t cấm 5h–22h từ 1/12/2025" (theo research_south.md): **không tìm lại được nguồn**. Có thể đã nhầm với Bùi Hữu Nghĩa ở Thuận An. | unresolved | — |
| Bình Phước cũ: **không tìm thấy** quy định riêng. | — | — |

## 7. Tây Ninh (gồm Long An cũ)

- QĐ 60/2026/QĐ-UBND: theo research_south.md, văn bản không có giờ cấm hay vùng cấm xe tải.
- Tân An, TP Tây Ninh cũ: **không tìm thấy** thông báo cấm xe tải (tìm kiếm 21/10/2026).

## 8. Cần Thơ (gồm Sóc Trăng, Hậu Giang cũ)

| Nội dung | Tin cậy | Nguồn |
|---|---|---|
| **5 trục nội ô.** Xe chở hàng có khối lượng toàn bộ trên 3,5 t cấm 6h–7h30 và 16h30–18h30 trên: 30 Tháng 4 – Hòa Bình (Nguyễn Văn Linh → Nguyễn Trãi); 3 Tháng 2 – Trần Hưng Đạo – Hùng Vương (Nguyễn Văn Linh → CMT8); Nguyễn Văn Cừ (Nguyễn Văn Linh → CMT8); CMT8 (Nguyễn Văn Cừ → Nguyễn Trãi); Mậu Thân (Nguyễn Văn Cừ → 30 Tháng 4). Thí điểm 13/2–30/6/2026; BNEWS cho biết UBND TP ban hành văn bản cấm chính thức tháng 6/2026 (không rõ số hiệu). | TB | https://tuoitre.vn/plo/tu-13-2-can-tho-cam-xe-tai-tren-35-tan-luu-thong-tren-5-tuyen-duong-vao-gio-cao-diem-post895655.html ; https://bnews.vn/can-tho-quy-hoach-4-bai-do-xe-cong-cong-giam-un-tac-noi-do/439942.html |
| **Nút IC3:** sơ mi rơ moóc trên QL1 hướng TP.HCM → Cà Mau cấm rẽ phải vào IC3 lúc 6h–8h, 11h–13h30, 16h30–18h30. Đây là cấm rẽ, định dạng không biểu diễn được, nên để vào unresolved. | TB | như trên |
| **Cầu 30 Tháng 4**, sông Thốt Nốt (phường Thuận Hưng): tải trọng cho phép 8 t; sập ngày 28/2/2026 do xe 8,2 t, chưa thông xe đường bộ. Để vào unresolved. | TB | https://voh.com.vn/giao-thong/sap-cau-30-thang-4-o-can-tho-do-xe-qua-tai-xu-ly-tai-xe-va-doanh-nghiep-lien-quan-647938.html |
| Sóc Trăng, Vị Thanh: **không tìm thấy** quy định cấm xe tải nội ô. | — | — |

## 9. An Giang (gồm Kiên Giang cũ)

- Long Xuyên: **không tìm thấy** lệnh cấm. Theo tin báo, xe lớn qua nội ô đã giảm nhờ tuyến tránh: https://nhandan.vn/som-xu-ly-mot-so-ton-tai-de-bao-dam-an-toan-tren-tuyen-tranh-long-xuyen-post814937.html
- Châu Đốc, Rạch Giá: **không tìm thấy**.

## 10. Đồng Tháp (gồm Tiền Giang cũ) và Vĩnh Long (gồm Bến Tre, Trà Vinh cũ)

| Nội dung | Tin cậy | Nguồn |
|---|---|---|
| **Cầu Rạch Miễu** (QL60, nối Đồng Tháp và Vĩnh Long), từ 10/6/2024: xe tải từ 3 trục cấm hằng ngày; hướng Tiền Giang → Bến Tre 9h–11h và 15h–19h, hướng ngược lại 15h–19h. Theo kết quả tìm kiếm (chưa mở bài), cầu Rạch Miễu 2 đã thông xe tháng 8/2025, nên lệnh này rất có thể đã thôi áp dụng. | Thấp | https://voh.com.vn/giao-thong/cau-rach-mieu-tu-106-cam-xe-3-truc-qua-cau-gio-cao-diem-529694.html |
| Đồng Tháp ban hành **QĐ 76/2026/QĐ-UBND** (7/7/2026) về thời gian, phạm vi hoạt động của xe thô sơ, xe chở hàng bốn bánh có gắn động cơ và vận tải trong đô thị. Chỉ đọc được tiêu đề. | unresolved | https://thuviennhadat.vn/van-ban-phap-luat-viet-nam/quyet-dinh-76-2026-qd-ubnd-thoi-gian-pham-vi-hoat-dong-cua-xe-tho-so-dong-thap-713726.html |
| Mỹ Tho: chỉ có đường một chiều (5/2025), **không thấy** cấm xe tải. | — | https://tuoitre.vn/duong-hai-ben-gieng-nuoc-my-tho-thanh-duong-mot-chieu-tu-hom-nay-10-5-20250510081731872.htm |
| Vĩnh Long: **QĐ 51/2026/QĐ-UBND** chỉ quy định giờ cho xe vệ sinh môi trường và xe chở vật liệu xây dựng (chưa đọc toàn văn). QĐ 28/2017 của Bến Tre đã hết hiệu lực. | TB | https://luatvietnam.vn/xay-dung/quyet-dinh-51-2026-qd-ubnd-vinh-long-quy-dinh-thoi-gian-hoat-dong-xe-ve-sinh-moi-truong-xe-cho-vat-lieu-xay-dung-436523-d2.html |
| Trà Vinh, Bến Tre, Cao Lãnh, Sa Đéc: **không tìm thấy**. | — | — |

## 11. Cà Mau (gồm Bạc Liêu cũ)

| Nội dung | Tin cậy | Nguồn |
|---|---|---|
| **Bạc Liêu, QĐ 09/2012/QĐ-UBND** (hiệu lực 24/8/2012; caselaw ghi "đang có hiệu lực"). Ô tô tải có trọng tải từ 3,5 t trở lên và máy kéo cấm 6h–8h, 10h30–12h, 16h30–18h trên Võ Thị Sáu, Cách Mạng (Lê Duẩn – cầu Xáng), Lộc Ninh, Nguyễn Thị Minh Khai (cầu Ông Bổn – Cao Văn Lầu). Văn bản còn có cấm đậu và cấm bốc dỡ hàng trên nhiều đường, nhưng phần đó không mã hóa. Chỉ đọc được bản tóm tắt. | TB | https://caselaw.vn/van-ban-phap-luat/181701-quyet-dinh-so-09-2012-qd-ubnd-ngay-14-08-2012-ve-quy-dinh-to-chuc-giao-thong-hoat-dong-van-tai-duong-bo-tren-dia-ban-noi-o-thanh-pho-bac-lieu-do-uy-ban-nhan-dan-tinh-bac-lieu-ban-hanh |
| Cà Mau: QĐ 35/2026 (xe vệ sinh môi trường, xe chở vật liệu xây dựng; thay QĐ 63/2024) và QĐ 37/2026 (xe thô sơ, xe bốn bánh có gắn động cơ). **Không thấy** cấm ô tô tải trong TP Cà Mau cũ. | Thấp | https://vbpl.moj.gov.vn/FileData/TW/Lists/vbpq/Attachments/187760/VanBanGoc_37.2026.QD.UBND.pdf |

## 12. Cầu yếu và phà ở đồng bằng sông Cửu Long

**Không tìm thấy** danh mục tập trung nào về cầu giới hạn tải trọng hay giới hạn tải trọng phà. Văn bản về phà chỉ quy định giá vé, ví dụ Đồng Tháp QĐ 250/QĐ-UBND-HC năm 2025: https://luatvietnam.vn/thue/quyet-dinh-250-qd-ubnd-hc-dong-thap-2025-quy-dinh-khung-gia-dich-vu-su-dung-pha-396291-d2.html

Nguồn cho lớp này phải là thẻ `maxweight` và `hgv` trên OSM.

## Những điểm chưa chắc chắn lớn nhất

1. **Dự thảo TP.HCM 2026.** Nếu được ký, dự thảo thay toàn bộ nhóm `hcm_qd23_*` và `hcm_hl_*`. Ngưỡng giảm từ 2,5 t xuống 1 t, khung giờ của hành lang đổi theo, khu Bình Dương và Vũng Tàu cũ có danh sách tuyến mới.
2. **Khu Bình Dương cũ.** Các quy định chỉ có trong tin báo, không có số văn bản. Chưa rõ còn áp dụng sau sáp nhập. Một số mốc không khớp OSM: ĐT743, Bến Đò, Miếu Ông Cù.
3. **Đồng Nai, QĐ 20/2026.** Chưa đọc được toàn văn. Các tuyến cấm ở Biên Hòa chỉ có từ biển báo năm 2020.
4. **Cơ sở ngưỡng.** Nhiều tin chỉ ghi "trên X tấn" mà không rõ khối lượng toàn bộ hay chuyên chở. Cách chọn được ghi trong `note` của từng quy tắc.
