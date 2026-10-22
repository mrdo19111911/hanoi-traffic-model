# Spike: phân cụm phổ (spectral clustering) mạng đường lớn A

Thử nghiệm dùng một lần, chỉ để báo cáo, không phải code production. Không sửa file nào ngoài thư mục này, không gọi mạng, không gọi OSRM, không dùng TomTom.

- Script: `spectral_spike.py` (chạy từ gốc repo: `.venv/Scripts/python.exe reports/spectral_spike/spectral_spike.py`, mất khoảng 1 phút)
- Kết quả đầy đủ: `summary.json` (có số liệu từng cụm), log chạy: `run.log`
- Bản đồ: `clusters_k16.geojson`, gồm các nút giao của k=16 với thuộc tính `cluster`, `color` và `boundary` (nút biên)

## Cách làm

1. Đọc `osrm/data/big_roads.geojson`, bỏ 78 đoạn `fragment` và 1.021 đoạn `isolated`. Còn 66.775 đoạn.
2. Cắt đoạn tại đầu mút và đỉnh dùng chung (toạ độ làm tròn 1e-6), được 69.968 nút và 82.813 đoạn. Gộp chuỗi nút bậc 2 lại thì được đồ thị nút giao. Thành phần liên thông chính có **24.349 nút, 35.747 cạnh, 65.333 km**; phần bị bỏ ngoài thành phần chính chỉ 80 km.
3. Trọng số chính là `1/thời gian` (cao tốc 80, quốc lộ 50, tỉnh lộ 40, phà 15 km/h; thời gian tối thiểu 30 s). Bản phụ dùng `exp(-km/10)`.
4. Ma trận Laplace chuẩn hoá `I - D^-1/2 W D^-1/2`, lấy 64 vector riêng nhỏ nhất bằng `eigsh` shift-invert. Sau đó chạy NJW: chuẩn hoá từng hàng, rồi `kmeans2 ++` với 10 lần khởi tạo.
5. Đối chứng: k-means trên toạ độ chiếu (km) của các nút giao. Không làm đối chứng theo 63 tỉnh vì không có ranh giới tỉnh offline.

Thời gian chạy: đọc và cắt 2,0 s, gộp chuỗi 0,2 s, `eigsh` 64 vector 0,8 s. k-means 10 lần khởi tạo mất 1,2 / 2,4 / 7,0 / 29 s cho k = 8 / 16 / 32 / 64.

## Kết quả (trọng số 1/thời gian, so với k-means toạ độ)

| k | phương pháp | km mỗi cụm (min / trung vị / max) | số cạnh bị cắt | km bị cắt (% mạng) | conductance TB / max | số cụm liền mạch | số nút biên (TB mỗi cụm) | số tuyến cần tính, N=2000 |
|---|---|---|---|---|---|---|---|---|
| 8 | **spectral** | 3.480 / 8.475 / 10.831 | **80** | **1.203 (1,8%)** | 0,0029 / 0,0069 | **7/8** | **150** (17,5) | 0,63 tr |
| 8 | k-means xy | 4.306 / 7.642 / 13.848 | 120 | 1.733 (2,7%) | 0,0038 / 0,0068 | 3/8 | 214 (24,5) | 0,74 tr |
| 16 | **spectral** | 2.388 / 3.189 / 7.185 | **164** | **2.642 (4,0%)** | 0,0048 / 0,0070 | **15/16** | **313** (16,5) | **0,46 tr** |
| 16 | k-means xy | 2.318 / 3.447 / 7.133 | 256 | 3.564 (5,5%) | 0,0078 / 0,0117 | 1/16 | 459 (24,5) | 0,61 tr |
| 32 | **spectral** | 735 / 1.793 / 3.887 | **257** | **4.072 (6,2%)** | 0,0086 / 0,0175 | **31/32** | **482** (12) | **0,44 tr** |
| 32 | k-means xy | 593 / 1.844 / 3.002 | 402 | 6.595 (10,1%) | 0,0144 / 0,0654 | 11/32 | 725 (17) | 0,76 tr |
| 64 | **spectral** | 144 / 857 / 3.270 | **488** | **7.441 (11,4%)** | 0,0149 / 0,0323 | **61/64** | **907** (13) | 0,97 tr |
| 64 | k-means xy | 197 / 800 / 1.715 | 692 | 10.813 (16,6%) | 0,0258 / 0,0980 | 20/64 | 1.241 (16) | 1,69 tr |

Ghi chú về các cột:

- Conductance tính trên số cạnh, không có trọng số. Bản có trọng số nằm trong `summary.json`.
- "Liền mạch" nghĩa là cụm đó liên thông khi chỉ đi trên đường lớn bên trong cụm.
- "Số tuyến cần tính" lấy 2.000 khách hàng giả định, chia cho các cụm theo số km đường. Số này bằng tổng ba phần: cặp trong cùng cụm, cặp từ điểm tới nút biên của cụm mình, và cặp nút biên với nút biên. Để so sánh: ma trận đầy đủ là 4,0 triệu cặp.
- Trọng số `exp(-km/10)` cho kết quả gần như bằng trọng số chính và kém hơn một chút (ví dụ k=16: 184 cạnh cắt, 331 nút biên).

**Kết luận về chất lượng phân cụm:** spectral làm tốt trên mạng này. Hình chữ S hẹp của Việt Nam tạo ra những "cổ chai" tự nhiên. So với cắt theo toạ độ, spectral cắt ít hơn khoảng 30–36% số cạnh và có ít hơn khoảng 27–33% số nút biên. Gần như mọi cụm đều liền mạch, còn k-means theo toạ độ cắt ngang đường và để lại những mẩu rời. Vùng hợp lý là k=16–32: khoảng 300–500 nút biên cho cả nước, 12–16 nút biên mỗi cụm.

## Có giúp tính ma trận thời gian toàn quốc không?

Trên giấy thì có giảm: với 2.000 điểm, cách "tầng trung chuyển qua nút biên" chỉ cần khoảng 0,44–0,46 triệu tuyến thay vì 4 triệu, tức ít hơn khoảng 9 lần. Ma trận nút biên với nút biên (khoảng 0,1–0,23 triệu cặp mỗi khung giờ) còn dùng lại được cho mọi bài toán và lưu được vào DB. Nhưng có ba điểm làm cách này không đáng làm:

1. **OSRM MLD đã làm đúng việc này rồi.** `/table` không tính N² tuyến riêng lẻ. Nó chạy many-to-many trên các ô phân vùng nhiều tầng, cũng dùng nút biên và lớp phủ, nhưng phân vùng tốt hơn và viết bằng C++. Nếu tự làm tầng trung chuyển bằng Python thì vừa làm lại MLD, vừa phải thêm khoảng 1 tỷ phép min-plus để ghép các cặp khác cụm.
2. **Ranh giới chỉ nằm trên mạng A.** Khách hàng nằm trên đường nhỏ, và đường nhỏ vẫn băng qua ranh giới cụm mà không đi qua "nút biên" nào. Vì vậy phép ghép `điểm → biên → biên → điểm` không còn chính xác trên toàn mạng, mà ép mọi chuyến liên cụm phải đi trên mạng A.
3. **Phụ thuộc thời gian.** Phép ghép chỉ đúng khi cả chuyến dùng tốc độ của một khung giờ khởi hành. Muốn đúng theo thời điểm thực sự đi qua nút biên thì phải ghép các hàm thời gian (FIFO), phức tạp hơn nhiều.

Phần tốn kém thật sự hiện nay là mô hình tốc độ. `SpatialEstimator` gọi `/route` cho từng cặp rồi tính lại thời gian theo từng cạnh, nên chi phí tăng theo N². Phân cụm chỉ giảm được hằng số ở bước này, còn bản chất bài toán vẫn vậy.

## Khuyến nghị

**Không dùng spectral clustering để dựng ma trận thời gian.** Hãy để OSRM MLD xử lý `/table`, và tập trung vào việc đưa mô hình tốc độ theo khung giờ vào chính OSRM:

- Với mỗi khung giờ, xuất tốc độ theo từng cặp nút OSM của các cạnh (đã có trong `road_classes.sqlite`) thành file `--segment-speed-file`.
- Chạy `osrm-customize`. MLD được thiết kế cho đúng việc này: chỉ tính lại metric, không phân vùng lại.
- Gọi `/table` cho từng khung giờ, rồi lưu ma trận vào DB theo khoá (điểm, điểm, ngày, khung giờ).

Cách này cho ma trận chính xác trên toàn mạng, không phải xấp xỉ qua nút biên. Spike này chưa đo thời gian `osrm-customize` và `/table` 2000×2000 trên máy này, nên đó là việc cần đo tiếp theo.

Spectral clustering (k≈16–32, trọng số 1/thời gian) chỉ nên giữ như công cụ phụ cho phía VRP, nếu sau này cần: chia nhỏ bài toán (cluster-first), lọc bớt những cặp quá xa không bao giờ cùng một xe, hoặc làm vùng thô để tra nhanh. Không cần nó để tính ma trận.
