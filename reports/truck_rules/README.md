# Quy định hạn chế xe tải trong đô thị: tổng hợp toàn quốc (tra cứu 21/10/2026)

Đây là đầu vào cho lớp "quy tắc cấm" của hệ thống tốc độ và ma trận dành cho xe tải nhiều cỡ.

Chi tiết và nguồn nằm trong các file sau:
- [research_north.md](research_north.md): miền Bắc
- [research_central.md](research_central.md): miền Trung và Tây Nguyên
- [research_hcm.md](research_hcm.md): TP.HCM
- [research_south.md](research_south.md): miền Nam

Phần Hà Nội có ở cuối file này.

**Độ tin cậy:**
- **Cao:** đã đọc toàn văn văn bản.
- **TB** (trung bình): bản tóm tắt chính thức hoặc tin báo dẫn văn bản.
- **Thấp:** chỉ có tin báo; hoặc không rõ văn bản còn hiệu lực hay không.

## 1. Các nơi có vùng cấm hoặc khung giờ xe tải

| Địa phương | Văn bản | Ngưỡng tính theo | Quy tắc chính | Phạm vi | Tin cậy |
|---|---|---|---|---|---|
| **Hà Nội** | QĐ 01/2026, hiệu lực 15/1/2026 (sửa bởi QĐ 52/2026) | khối lượng toàn bộ | dưới 2 t: ngoài giờ cao điểm (6h–9h, 16h–19h30). Từ 2 t: chỉ 21h–6h | vùng trong vòng Phạm Văn Đồng … Vành đai 3 … Võ Văn Kiệt; một số tuyến được đi | TB |
| **TP.HCM** | QĐ 23/2018 (sửa bởi QĐ 23/2019) | khối lượng chuyên chở | ≤2,5 t: cấm 6h–9h, 16h–20h. >2,5 t: cấm 6h–22h, trừ hành lang | nội đô, trong vòng QL1 – Xa lộ Hà Nội – Mai Chí Thọ – Võ Chí Công – Nguyễn Văn Linh | Cao |
| TP.HCM, khu Bình Dương cũ | phương án phân luồng 2022 | chuyên chở | đầu kéo: cấm 6h–8h, 16h30–18h30. >10 t: cấm 11h–13h | 6 tuyến (QL13, Mỹ Phước – Tân Vạn, ĐT743…) | TB |
| **Hải Phòng** | QĐ 11/2026 | chuyên chở | ≤1,5 t: chỉ 20h–6h trong trung tâm. Xe lớn hơn: câu chữ mơ hồ | KV1 (Hải Phòng cũ), KV2 (Hải Dương cũ) | Cao, nhưng cần hỏi lại cách hiểu |
| **Đà Nẵng** | phương án theo công văn 2019 | tải trọng | 1,5–2,5 t: cấm 6h30–8h30, 11h–12h, 16h–19h. >2,5 t: cấm 6h–22h | vòng Nguyễn Tất Thành – Điện Biên Phủ – … – 3 Tháng 2 | TB |
| **Hà Tĩnh** | QĐ 53/2025 | chuyên chở | <1,5 t: 24/24. 1,5–3,5 t: ngoài giờ cao điểm (6h30–8h30, 16h30–18h30). >3,5 t: chỉ 22h–6h | mọi đường trong các phường, trừ quốc lộ và đường tỉnh | Cao |
| **Huế** | TB 93/TB-UBND (4/2026) | ≥3,5 t | cấm 6h–8h, 16h30–18h30; xe đi vành đai | trung tâm (Phú Xuân, Thuận Hóa cũ) | TB |
| **Cần Thơ** | thí điểm 2/2026, chính thức 6/2026 | khối lượng toàn bộ >3,5 t | cấm 6h–7h30, 16h30–18h30 | 5 trục nội ô | TB |
| **Đồng Nai** | QĐ 20/2026 cộng cấm theo tuyến | chuyên chở | Đồng Khởi: >2 t cấm theo 3 khung giờ; >5 t cấm 5h–22h. Bùi Hữu Nghĩa: ≥5 t cấm 5h–22h | theo tuyến | TB |
| **Tuyên Quang** | QĐ 52/2024 | chuyên chở | ≥1,5 t: cấm 6h–7h30, 16h30–18h | đường đô thị | Cao, nhưng có thể đã bị QĐ 19/2026 thay |
| **Nghệ An (Vinh cũ)** | QĐ 70/2016 (sửa 2022) | chuyên chở | ≥1,8 t: cấm 6h–22h trên 53 tuyến. ≥4 t: cấm 6h–22h trên 24 tuyến. Xe đi qua ≥4 t: cấm 5h–24h | theo tuyến | Cao (caselaw), chưa rõ hiệu lực sau sáp nhập |
| **Quảng Ngãi** | TB 482, 485/TB-SXD (8/2026) | tải trọng | ≥5, 7, 10 t: cấm 6h–18h hoặc cả ngày, tùy tuyến | khoảng 30 tuyến ở Nghĩa Lộ, Cẩm Thành, Trương Quang Trọng | Cao |
| Quảng Trị (Đồng Hới) | phân luồng 2019 cộng biển | chuyên chở | >10 t: đi tránh 24/24. >3,5 t: cấm giờ trên 4 tuyến | theo tuyến | Thấp |
| Lâm Đồng (Đà Lạt) | Sở Xây dựng 2026 | ≥1,5 t / ≥3,5 t | ≥1,5 t: cấm 6h–8h, 10h–12h, 16h–18h. ≥3,5 t: cấm 6h–21h trên 15 tuyến (có thể chỉ tạm thời) | 4 phường trung tâm | Thấp |
| Gia Lai (Quy Nhơn, Pleiku) | công văn, phân luồng | 3,5 t / 10 t | Quy Nhơn: >3,5 t cấm giờ cao điểm trên 1 trục. Pleiku: ≥10 t chạy qua phải đi tránh | theo tuyến | Thấp |
| Đắk Lắk (Buôn Ma Thuột) | biển báo | 3,5 t / 10 t | ≥10 t: cấm 6h–22h trên 3 trục; >3,5 t: cấm trên Thủ Khoa Huân | theo tuyến | Thấp |
| Bắc Ninh | QĐ 06/2026 | không rõ | xe ben, đầu kéo, container: cấm giờ cao điểm trên "một số tuyến" | chưa có danh sách | Thấp |

## 2. Không có lệnh cấm xe tải chung bằng văn bản

Các quyết định ở những tỉnh dưới đây chỉ quy định giờ cho xe chở vật liệu xây dựng, phế thải hoặc vệ sinh môi trường; xe tải chạy theo biển báo:
- Quảng Ninh, Hưng Yên, Ninh Bình, Thái Nguyên, Phú Thọ, Lào Cai, Lạng Sơn, Cao Bằng, Điện Biên, Lai Châu, Sơn La;
- Thanh Hóa, Khánh Hòa (văn bản năm 2012 đã hết hiệu lực);
- Tây Ninh, An Giang, Đồng Tháp, Vĩnh Long (quy định cũ của Bến Tre đã hết hiệu lực), Cà Mau.

## 3. Quy định toàn quốc

- **NĐ 241/2026**, hiệu lực 1/7/2026: xe pickup cabin kép và xe van có khối lượng toàn bộ ≤3,5 t được chạy như ô tô con.
- **Biển báo theo từng tuyến:** cấm xe tải, giới hạn tải trọng cầu, cấm xe ≥3 trục, cấm theo giờ. Đây là nguồn chính ở mọi nơi không có vùng cấm. Biển báo không có dữ liệu tập trung, chỉ có trong thẻ OSM:
  - `hgv=no`: 2.404 way;
  - `maxweight`: 1.613 way;
  - `hgv:conditional`: 464 way;
  - `maxweight:conditional`: 375 way;
  - `maxheight`: 1.251 way.

## 4. Hệ quả cho thiết kế

1. **Mỗi xe cần ít nhất hai thông số khối lượng: khối lượng toàn bộ và khối lượng chuyên chở cho phép.** Ngưỡng ở mỗi nơi tính khác nhau: Hà Nội và Cần Thơ dùng khối lượng toàn bộ, phần lớn nơi khác dùng khối lượng chuyên chở. Thêm số trục (Lào Cai, Hải Dương cũ) và loại xe (đầu kéo, xe ben, container). Vì vậy không thể định nghĩa cỡ xe chung cho cả nước. Mỗi quy tắc phải tự kiểm tra trên các thông số của từng xe.
2. **Có ba dạng quy tắc:**
   - (a) vùng cấm: một đa giác dựng tự động từ chuỗi đường bao, kèm danh sách tuyến được đi;
   - (b) danh sách tuyến cấm;
   - (c) toàn bộ đường đô thị trong các phường (Hà Tĩnh), trừ quốc lộ và đường tỉnh.

   Mỗi quy tắc gồm: điều kiện về xe, các khung giờ, dạng quy tắc, văn bản và nguồn, hiệu lực, độ tin cậy.
3. **Thẻ OSM là lớp nền cho các tuyến có biển báo.** Quy tắc từ văn bản phủ lên trên. Quy tắc do công ty tự thêm có ưu tiên cao nhất.
4. **Nhiều mục chưa chắc chắn** (độ tin cậy Thấp, văn bản có thể đã hết hiệu lực, câu chữ mơ hồ). Hệ thống nên cho bật hoặc tắt từng quy tắc và lưu lý do. Mặc định nên bật các quy tắc "Cao" và "TB". Quy tắc "Thấp" để người dùng quyết định.

## Hà Nội: QĐ 01/2026/QĐ-UBND

- Ban hành 4/1/2026, hiệu lực 15/1/2026. Sau đó QĐ 52/2026 sửa: xe pickup được chạy 24/24.
- **Vùng hạn chế:** bên trong vòng Phạm Văn Đồng → Hồ Tùng Mậu → Xuân Phương → Đường 70 → đường gom Đại lộ Thăng Long → Hoàng Tùng (Vành đai 3,5) → Lê Trọng Tấn → Văn Khê → Phúc La → cầu Bươu → Phan Trọng Tuệ – Ngọc Hồi → Vành đai 3 → Nguyễn Văn Linh → Lý Sơn → cầu Đông Trù → Trường Sa → Hoàng Sa → Võ Văn Kiệt → cầu Thăng Long.
- **Tuyến trong vùng vẫn được đi:** Nguyễn Văn Cừ, Ngọc Lâm, Ngô Gia Khảm, Bắc Thăng Long – Vực Dê, Phương Trạch, Cổ Linh, Đàm Quang Trung, Huỳnh Tấn Phát, Tam Trinh (đến Vành đai 3), Đại lộ Thăng Long (Phạm Hùng → Đường 70).
- **Nguồn:**
  - https://xaydungchinhsach.chinhphu.vn/quyet-dinh-01-2026-qd-ubnd-quy-dinh-hoat-dong-cua-phuong-tien-giao-thong-duong-bo-tren-dia-ban-ha-noi-119260104191512937.htm
  - https://vietnamnet.vn/cam-xe-tai-tu-2-tan-vao-ha-noi-ban-ngay-nhung-dieu-tai-xe-can-biet-2482421.html
  - https://www.sggp.org.vn/ha-noi-xe-ban-tai-duoc-luu-thong-2424-gio-post850609.html

**Đã chuyển:** các file `rules_*.json` nay nằm ở `data/rules/official/{hanoi,north,central,south,national}.json`.
