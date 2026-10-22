# Miền Bắc (trừ Hà Nội): quy định hạn chế xe tải, bản 2 (tra cứu ngày 21/10/2026)

Bản này bổ sung cho [research_north.md](research_north.md). Các quy tắc mã hóa được nằm trong [rules_north.json](rules_north.json): 16 quy tắc và 14 mục `unresolved`.

**Độ tin cậy:**
- **Cao:** đã đọc toàn văn.
- **TB:** tóm tắt chính thức, hoặc tin báo dẫn văn bản.
- **Thấp:** chỉ có tin báo, hoặc không rõ còn hiệu lực.

Tỉnh, thành dùng theo địa giới sau sắp xếp 7/2025.

## Ba câu hỏi mở của bản 1

### 1. Hải Phòng, Điều 6 QĐ 11/2026: đã đọc lại toàn văn, vẫn mơ hồ

Độ tin cậy: Cao (về câu chữ).

- **Nguồn:** bản scan có dấu, ký 10/2/2026, hiệu lực 23/2/2026, thay QĐ 05/2025 của Hải Dương cũ.
  - https://cdn.haiphong.gov.vn/gov-hpg/6326/tintuc/2026/2/2026-02-10-14-.signed639064815851805463.pdf
  - Trang dẫn: https://sokhcn.haiphong.gov.vn/van-ban-quy-pham-phap-luat/quyet-dinh-so-11-2026-qd-ubnd-ngay-10-2-2026-cua-uy-ban-nhan-dan-thanh-pho-hai-phong-ban-hanh-qu-858589
- **Câu chữ:**
  - Điều 3.5 định nghĩa "hạn chế lưu thông" là "một số tuyến đường hạn chế về thời gian, tải trọng đối với một số loại phương tiện".
  - Điều 6.1.a: xe chở hàng "hạn chế lưu thông trên đường đô thị" vào giờ cao điểm (6h–8h, 11h30–12h30, 16h30–18h30) và giờ ban đêm (22h–6h), trừ điểm b.
  - Điều 6.1.b: xe chở hàng có khối lượng chuyên chở đến 1,5 t "chỉ được phép hoạt động trên các tuyến đường giới hạn trong khu vực trung tâm thành phố từ 20 giờ 00 phút đến 06 giờ 00 phút sáng hôm sau".
  - Điều 5.2: xe chở VLXD, phế thải rời chỉ chạy trong đô thị vào giờ ban đêm.
- **Kết luận:** văn bản là khung dẫn chiếu biển báo. Nó không liệt kê tuyến và không nói xe trên 1,5 t ở khu trung tâm chạy thế nào.
  - Trong JSON: ≤1,5 t cấm 6h–20h trong toàn bộ KV1 và KV2. Đây là cách đọc chặt hơn, để mức TB.
  - Xe trên 1,5 t: áp cùng khung cấm, nhưng mức Thấp (suy luận), mặc định tắt.
- **Bối cảnh:** từ 2017, Hải Phòng đã cắm biển cấm xe 0,5–1,5 t từ 6h đến 20h trên phố nội thành. Xe tải lớn chỉ được đi một danh sách trục, có ngưỡng 3,5 t, 7,5 t hoặc không giới hạn (Nguyễn Văn Linh, Lê Thánh Tông, Ngô Quyền, Trường Chinh, Lạch Tray ≤7,5 t…). Độ tin cậy: Thấp, vì chưa xác nhận danh sách còn áp dụng. https://logistics.gov.vn/chinh-sach/hai-phong-quy-dinh-moi-ve-phan-luong-xe-tai-xe-khach
- **Vẫn nên hỏi Sở Xây dựng Hải Phòng.**

### 2. Bắc Ninh, QĐ 06/2026: không có danh sách tuyến

Độ tin cậy: Cao.

- **Nguồn:** đã đọc toàn văn, ký 12/1/2026, hiệu lực 1/2/2026, bãi bỏ QĐ 50/2024. https://bacninh.gov.vn/documents/37632/85377703/06-2026-QĐ-UBND.pdf
- **Điều 10.2:**
  - a) xe tải (trừ xe ben, đầu kéo, container) chạy 24/24, trừ tuyến có biển cấm;
  - b) "cấm các loại xe ben, xe đầu kéo, xe chở container hoạt động vào giờ cao điểm trên một số tuyến đường".
- **Không có danh sách tuyến.** Giờ cao điểm cũng chỉ được định nghĩa chung chung (Điều 3.6). Điều 13 giao Sở Xây dựng khảo sát giờ cao điểm và cắm biển cấm theo giờ.
- **Hệ quả:** danh sách tuyến chỉ có trên biển báo (OSM). Ghi vào `unresolved`.
- **"Đô thị"** gồm toàn bộ các phường và khu trung tâm các xã.
- **QĐ 09/2026** (VSMT, VLXD, phế thải), đã đọc. Độ tin cậy: Cao. https://luatvietnam.vn/van-ban/xem-truoc-file-6e76a273-13fb-0700-2b77-959b9856783b
  - Xe phế thải rời cấm 6h–8h và 16h30–18h30.
  - Giờ cho xe VLXD do UBND xã quyết định.
  - VSMT chạy 22h–5h.
  - Văn bản này thay QĐ 13/2025.

### 3. Tuyên Quang: QĐ 19/2026 đã bãi bỏ toàn bộ QĐ 52/2024

Độ tin cậy: Cao.

- **Nguồn:** đã đọc toàn văn trên Công báo số 18 (30/3/2026). Ký 25/3/2026, hiệu lực 6/4/2026. http://congbao.tuyenquang.gov.vn/media/files/2026/signed-so-18-6-9.pdf
- **Điều 5.2.a:** "Bãi bỏ toàn bộ Quyết định số 52/2024/QĐ-UBND".
- **Ngưỡng 1,5 t không được giữ lại.** QĐ 52/2024 có quy định này (Điều 7.5, đã đọc lại: https://static.luatvietnam.vn/xem-noi-dung-file-quyet-dinh-52-2024-qd-ubnd-tuyen-quang-thoi-gian-hoat-dong-xe-ve-sinh-xe-cho-vlxd-phe-thai-383472-d2/uploaded/vietlawfile/2025/1/52_2024_qd_ubnd_030125102518.pdf.aspx):
  - xe dưới 1,5 t chạy 24/24;
  - xe từ 1,5 t chỉ chạy ngoài 6h–7h30 và 16h30–18h.

  QĐ 19/2026 chỉ còn:
  - xe VLXD, phế thải rời: không chạy trong đô thị 6h–7h30 và 16h30–18h;
  - xe VSMT: 19h–6h.
- **Từ 6/4/2026, Tuyên Quang không còn khung giờ chung cho xe tải.** Bản 1 ghi "Cao, có thể đã bị thay": nay đã xác nhận bị bãi bỏ. Không đưa vào JSON.

## Theo tỉnh

### Hải Phòng (gồm Hải Dương cũ)

- **QĐ 11/2026:** xem mục 1 ở trên. JSON: `hp_kv1_*`, `hp_kv2_*`.
  - Trong OSM, sông Bắc Hưng Hải ở KV2 mang tên "Sông Sặt".
  - Cầu Lộ Cương chưa có tên trong OSM.
- **ĐT.390 (Thanh Hà cũ):** xe tải từ 4 trục cấm 6h–8h và 17h–19h hằng ngày, từ 15/10/2026.
  - Chỉ chiều QL5 → cầu Quang Thanh, Km15+100–Km35+280.
  - Do Sở Xây dựng ban hành; bài báo không nêu số văn bản. Độ tin cậy: TB.
  - JSON mã hóa cả hai chiều.
  - https://baohaiphong.vn/han-che-xe-tai-tu-4-truc-tro-len-tren-duong-390-tu-ngay-15-10-555724.html
- **Cầu Niệm:** cấm xe sơ mi rơ moóc từ 20h ngày 14/10/2026, trong thời gian kiểm định sau sự cố ngày 13/10. Chưa có ngày kết thúc. Độ tin cậy: TB.
  - https://baohaiphong.vn/hai-phong-cam-xe-so-mi-ro-mooc-qua-cau-niem-tu-toi-14-10-555789.html
- **Bùi Thị Từ Nhiên:** TB 417/TB-SXD ngày 9/10/2025, hiệu lực 13/10/2025. Độ tin cậy: TB (trang tổng hợp).
  - Xe chở trên 2,5 t cấm cả hai chiều.
  - Chiều Phủ Thượng Đoạn → Phương Lưu: cấm mọi ô tô 6h–8h và 16h30–18h30.
  - https://thuviennhadat.vn/bat-dong-san/khung-gio-cam-tai-cam-oto-duong-bui-thi-tu-nhien-phuong-dong-hai-hai-phong-tu-ngay-13-10-2025-25251.html
- **Đã hết hạn, không mã hóa:** các lệnh cấm sự kiện (Hoa Phượng Đỏ 8/5/2026; 6 tuyến sáng 30/8/2026).
- **Cảng, khu công nghiệp:** không tìm thấy khung giờ ra vào cảng (Đình Vũ, Lạch Huyện) hay khu công nghiệp bằng văn bản. Bài 7/2026 chỉ nói chung về xe container "đi vào đường cấm theo giờ" trên Nguyễn Bỉnh Khiêm, Đình Vũ, Bùi Viện, Tôn Đức Thắng. https://baohaiphong.vn/kiem-soat-xe-trong-tai-lon-de-phong-chong-tai-nan-giao-thong-547399.html

### Quảng Ninh

- **Đường bao biển Hạ Long – Cẩm Phả và Trần Quốc Nghiễn:** cấm xe "tải trọng trên 3,5 tấn" từ 6/2023. Căn cứ là công văn của Văn phòng UBND tỉnh chấp thuận phương án của Sở GTVT; bài không nêu số. Độ tin cậy: TB.
  - https://thuonghieucongluan.com.vn/quang-ninh-cam-xe-tai-tren-3-5-tan-luu-thong-o-duong-bao-bien-a195821.html
- **Quảng Yên (khu Hà An):** có biển cấm xe trên 10 t. Bài không rõ năm; chỉ là biển báo, không mã hóa. Độ tin cậy: Thấp. Chỉ thấy qua đoạn trích kết quả tìm kiếm, chưa mở trang: https://banduong.vn/Quang-Yen--Quang-Ninh-Sau-phan-anh-xe-container-vuot-bien-cam-luc-luong-chuc-nang-vao-cuoc-chan-chinh-vi-pham-d17275.html
- **Hành chính:** Chính phủ đã thông qua hồ sơ đề án thành lập TP Quảng Ninh trực thuộc Trung ương (NQ 202/NQ-CP, 27/7/2026; theo đoạn trích tìm kiếm: https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/7/202_nq-cp_27072026_1-signed.signed.pdf). Chưa xác nhận Quốc hội đã thông qua. Một trang Wikipedia nói hiệu lực từ 1/9/2026, chưa kiểm chứng.
- **Không tìm thấy:** quy định riêng cho Móng Cái hay cửa khẩu, cầu Bãi Cháy, Cái Lân. QĐ 33/2026 (bản 1) chỉ quy định giờ cho VSMT và phế thải.

### Bắc Ninh (gồm Bắc Giang cũ)

- **QĐ 06/2026 và QĐ 09/2026:** xem mục 2 ở trên.
- **Khu TP Bắc Giang cũ: QĐ 516/2014, sửa bởi QĐ 38/2021.** Độ tin cậy: TB.
  - Caselaw ghi "Đang có hiệu lực". Không có trong danh mục văn bản hết hiệu lực từ 1/6/2026 (QĐ 763/QĐ-UBND; đã kiểm tra: https://bacgiang.bacninh.gov.vn/documents/42816/122230708/Q__763_-_phu_luc.pdf).
  - Phụ lục I: xe tải trên 2,5 t cấm 6h–8h và 16h30–19h30 trên khoảng 34 tuyến. QĐ 38/2021 bổ sung 8 tuyến.
  - Phụ lục II: xe tải trên 2,5 t cấm hẳn trên 11 tuyến.
  - Đê sông Thương: xe tổng tải trọng không quá 12 t.
  - Quy định 7,5 t / 15 t theo bề rộng mặt đường.
  - Văn bản 516: https://caselaw.vn/van-ban-phap-luat/162605-quyet-dinh-so-516-2014-qd-ubnd-ngay-15-08-2014-ve-quan-ly-trat-tu-giao-thong-tren-dia-ban-thanh-pho-bac-giang-tinh-bac-giang
  - Văn bản 38/2021: https://caselaw.vn/van-ban-phap-luat/398454-quyet-dinh-so-38-2021-qd-ubnd-ngay-17-08-2021-cua-tinh-bac-giang-sua-doi-bo-sung-mot-so-dieu-cua-quy-dinh-quan-ly-trat-tu-giao-thong-tren-dia-ban-thanh-pho-bac-giang-va-phu-luc-i-kem-theo-quyet-dinh-516-2014-qd-ubnd
  - **Rủi ro:** văn bản cấp tỉnh cũ cho một thành phố đã giải thể; có thể đã được thay mà chưa cập nhật.
- **Không tìm thấy:** thông báo nào của Sở Xây dựng Bắc Ninh liệt kê tuyến cấm xe ben, đầu kéo, container.

### Hưng Yên (gồm Thái Bình cũ)

- **QĐ 46/2026** (ký 28/8/2026): xe từ 1,5 t chỉ bị cấm trên tuyến có biển. Không có khung giờ chung. Độ tin cậy: TB.
  - Bản 1 ghi đã đọc. Lần này trang thuvienphapluat trả 403 nên chưa đọc lại.
- **Không tìm thấy:** quy định riêng cho Hưng Yên, Thái Bình cũ, cầu yếu hay khu công nghiệp.

### Ninh Bình (gồm Nam Định, Hà Nam cũ)

- **Cầu Đò Quan:** tạm cấm mọi xe cơ giới (trừ xe máy 2 bánh, xe ưu tiên) từ 10h ngày 21/5/2026, sau khi tàu HD-5839 va vào dầm cầu, "đến khi có thông báo thay thế". Chưa tìm thấy thông báo mở lại. Độ tin cậy: TB.
  - https://baoninhbinh.org.vn/phuong-tien-thuy-va-cham-cau-do-quan-han-che-phuong-tien-qua-cau-bat-dau-tu-10h-260521104552426.html
- **Cầu phao Ninh Cường** (QL.37B Km74+391): TB 2262/TB-SXD ngày 20/3/2026. Tạm cấm ô tô khách và ô tô tải từ 21/3/2026 đến khi có thông báo thay thế. Độ tin cậy: TB.
  - https://nbtv.vn/news/401/53657/tu-08h00-ngay-21-3-tam-cam-xe-o-to-khach-va-xe-o-to-tai-qua-cau-phao-ninh-cuong
- **QĐ 124/2026** (VLXD, bản 1): ghi vào `unresolved`.
- **QĐ 94/2026:** không tìm thấy văn bản. Số văn bản này trong bản 1 chưa được kiểm chứng.
- **Không tìm thấy:** lệnh cấm xe tải nội thành ở Nam Định cũ hay Phủ Lý.

### Thái Nguyên (gồm Bắc Kạn cũ)

- **Công văn 1426/SXD-QLKCHTGT ngày 26/8/2025:** đã đọc toàn văn. Độ tin cậy: Cao.
  - Có 28 cầu hạn chế tải trọng (khối lượng toàn bộ 3,5–30 t; bộ ba xe tải - đầu kéo - rơ moóc) và 6 vị trí hạn chế chiều cao trên các QL và ĐT.
  - Không cầu nào có tên trong OSM, nên toàn bộ ghi vào `unresolved` kèm lý trình.
  - https://drvn.gov.vn/upload/2006538/20250912/VB_1426_Thai_Nguyen_3e105.pdf
- **TP Thái Nguyên cũ, 12/2021:** xe tải trên 5 t cấm trên Hùng Vương, Nguyễn Du, Nha Trang, phố 19-8. Đội Cấn cấm 6h30–8h30 và 16h–19h. Không có số văn bản; chưa rõ còn áp dụng. Độ tin cậy: Thấp.
  - https://baothainguyen.vn/thoi-su-trong-tinh/202112/tp-thai-nguyen-cam-xe-o-to-tai-luu-thong-tai-mot-so-tuyen-duong-36784C3/
- **Đã hết hạn:** các lệnh cấm đường trong năm 2026 (15/5, 22/9) đều theo sự kiện.

### Phú Thọ (gồm Vĩnh Phúc, Hòa Bình cũ)

- **Không tìm thấy:**
  - lệnh cấm xe tải thường trực ở Việt Trì, Vĩnh Yên, Hòa Bình;
  - quy định cho Tam Đảo: theo đoạn trích tìm kiếm (chưa mở trang), chỉ có lệnh tạm cấm xe khách từ 30 chỗ lên núi dịp 29/4–30/5/2026, đã hết hạn;
  - văn bản năm 2026 thay QĐ 51/2025.
- Phân luồng Giỗ Tổ 2026 (xe hàng từ 3,5 t trên QL2 và cao tốc Tuyên Quang – Phú Thọ) là tạm thời. Chỉ thấy qua đoạn trích tìm kiếm: https://phobienphapluat.vn/phan-luong-giao-thong-den-hung-2026-huong-dan-lo-trinh-chi-tiet-a4517.html

### Tuyên Quang (gồm Hà Giang cũ)

- Xem mục 3 ở trên: không còn khung giờ chung cho xe tải.
- **Không tìm thấy:** lệnh cấm xe tải trọng lớn trên đèo Mã Pì Lèng, QL4C, hay cửa khẩu Thanh Thủy.

### Lào Cai (gồm Yên Bái cũ)

- **Nội thị Sa Pa: QĐ 2288/QĐ-UBND (1/8/2022) của thị xã Sa Pa.** Độ tin cậy: Thấp.
  - Xe tải chuyên chở trên 10 t cấm trên 31 tuyến: Chủ nhật đến thứ Năm 6h–23h; thứ Sáu, thứ Bảy cả ngày.
  - Thị xã đã giải thể (7/2025); chưa rõ còn áp dụng.
  - https://baolaocai.vn/tu-218-thi-xa-sa-pa-cam-xe-khach-xe-tai-luu-thong-tren-31-tuyen-duong-post359590.html
- **QL4D (Trạm Tôn – Lào Cai, Km89–136):** lệnh cấm xe từ 4 trục vào thứ Sáu đến Chủ nhật giờ cao điểm (có từ 6/2018) đã được dỡ bỏ. Văn bản 7341/UBND-XD cho mọi phương tiện chạy bình thường từ 20/12/2024. Độ tin cậy: TB. Không mã hóa.
  - https://baolaocai.vn/phan-luong-giao-thong-tren-quoc-lo-4d-tu-deo-tram-ton-thi-xa-sa-pa-di-thanh-pho-lao-cai-post394987.html
- **Cửa khẩu Kim Thành:** chỉ có lộ trình bắt buộc cho xe hàng nhập khẩu (phương án 51, 2/2024). Đề xuất tổ chức lại nút cuối cao tốc (9/2026) chưa được duyệt. Ghi vào `unresolved`.
  - https://logistics.gov.vn/dich-vu-logistics/cang-bien/lao-cai-to-chuc-phan-luong-van-chuyen-hang-hoa-qua-cua-khau-duong-bo-kim-thanh
  - https://baolaocai.vn/go-nut-that-giao-thong-tai-cua-khau-kim-thanh-post909737.html
- **QĐ 04/2026** (VLXD, bản 1): ghi vào `unresolved`.
- **Không tìm thấy:** lệnh cấm trên đèo Khau Phạ, Ô Quy Hồ.

### Lạng Sơn

**Không tìm thấy.** Không có lệnh cấm xe tải nội thị hay phân luồng bắt buộc ở cửa khẩu Hữu Nghị, Tân Thanh bằng văn bản. Báo cáo xuất nhập khẩu hằng ngày chỉ nói về đường chuyên dụng và giờ làm việc đến 20h. Chỉ thấy qua đoạn trích tìm kiếm: https://congthuong.vn/cap-nhat-tin-xuat-nhap-khau-qua-cac-cua-khau-tinh-lang-son-ngay-10-2-2026-442626.html

### Cao Bằng

**Không tìm thấy.**

### Điện Biên

**Không tìm thấy** quy định mới. Bản 1 ghi: xe VLXD ở Điện Biên Phủ chỉ chạy 20h–5h.

### Lai Châu

**Không tìm thấy.**

### Sơn La

**Không tìm thấy lệnh thường trực.** Hai lệnh đã hết hạn (chỉ thấy qua đoạn trích tìm kiếm, chưa mở trang):
- QL6 qua Mộc Châu: xe tải trên 3,5 t bị hạn chế 18h ngày 1/9 đến 3h ngày 2/9/2026. https://lsvn.vn/son-la-han-che-phuong-tien-tren-quoc-lo-6a-phuc-vu-tuan-van-hoa-du-lich-moc-chau-nam-2026-a178458.html
- cấm theo giờ ở đèo Mộc Châu: kết thúc 7/10/2026. https://congly.vn/deo-moc-chau-chinh-thuc-thong-tuyen-542409.html

## Những điểm chưa chắc chắn nhất

1. **Hải Phòng:** cách đọc Điều 6 QĐ 11/2026, và xe trên 1,5 t có bị cấm ban ngày trong KV1, KV2 không. Seed và chuỗi đường bao của KV2 cũng cần kiểm tra khi dựng đa giác.
2. **Bắc Giang cũ:** hiệu lực của QĐ 516/2014 sau sắp xếp.
3. **Bắc Ninh:** danh sách tuyến cấm xe ben, đầu kéo, container chỉ có trên biển báo.
4. **Lệnh tạm thời chưa có ngày kết thúc** (cầu Niệm, cầu Đò Quan, cầu phao Ninh Cường): cần kiểm tra lại định kỳ.
5. **28 cầu của Thái Nguyên:** cần tọa độ.
