# Spec: kiểm định nội suy trên mẫu không gian mới (đã duyệt 2026-10-20)

## Vấn đề
`model/compare_spatial.py` chỉ đọc mẫu cũ trong `data/segments_*.jsonl`. Mẫu cũ có tọa độ ước lượng dọc tuyến và loại đường đoán theo tên đường. Trong khi đó `download/crawl_spatial.py` đã ghi `data/spatial_samples.jsonl`: mỗi mẫu có tọa độ thật từ TomTom và loại đường khớp OSM (101 mẫu sau lượt thu thập ngày 2026-10-20). Bảng kiểm định vì vậy chưa phản ánh dữ liệu mới.

## Thay đổi
1. **Hàm mới `spatial_samples(path)`** đọc file JSONL.
   - Mỗi dòng cho: `x, y = project(lat, lon)`, `road_class`, `slot`, `street`, `route`, `snapshot`, `speed_kmh`, `m`, `s`.
   - Bỏ dòng có `m ≤ 0` hoặc `s ≤ 0` (đếm vào `nonpositive_length_or_time`).
   - Bỏ dòng thiếu trường bắt buộc (đếm vào `missing_field`).
   - Bỏ dòng có loại đường ngoài 5 loại (đếm vào `unknown_road_class`).
2. **Chọn nguồn mẫu** bằng `--source auto|spatial|legacy`, mặc định `auto`:
   - `auto` dùng `data/spatial_samples.jsonl` khi file có ít nhất một mẫu hợp lệ; nếu không thì dùng mẫu cũ;
   - `spatial` dùng file mới, và báo lỗi nếu file thiếu hoặc rỗng;
   - `legacy` dùng mẫu cũ như hiện nay.
3. **Cách giữ lại** bằng `--holdout route|snapshot|street`. Mặc định là `route` cho mẫu mới và `street` cho mẫu cũ.
   - Fold của một mẫu bằng `sha256(khóa) mod 5`, với khóa là tuyến, snapshot hoặc tên đường đã chuẩn hóa.
   - Mọi mẫu cùng khóa vào cùng một fold, nên tập train và tập test không bao giờ chung khóa. Có `assert` kiểm tra điều này.
   - Vẫn nhóm theo (loại đường, khung giờ). Không mượn mẫu khác loại đường: nhóm nào không có mẫu train cùng loại thì bị đếm vào `unscorable_without_same_class_training`.
4. **Báo cáo** `reports/spatial_comparison.json` giữ nguyên các khóa cũ và thêm:
   - `source` (`"spatial_samples"` hoặc `"legacy"`), `holdout`, `samples_path`;
   - `pbf_sha256`, lấy từ dòng đầu của file mẫu, chỉ có với nguồn mới.

   Danh sách `limitations` đổi theo nguồn. Với nguồn mới, danh sách ghi rõ:
   - vận tốc là trung bình đoạn chỉ dẫn của TomTom;
   - số mẫu ít;
   - các tuyến gần nhau vẫn có thể tương quan;
   - không có tập kiểm tra cuối riêng.

## Kiểm thử (TDD, `tests/test_compare_spatial.py`)
- `spatial_samples`: đọc đúng các trường và chiếu tọa độ; bỏ và đếm đúng các dòng không hợp lệ.
- `auto` chọn file mới khi file có mẫu, và quay về mẫu cũ khi file thiếu hoặc rỗng (dùng thư mục tạm, hàm đọc mẫu cũ là giả).
- `holdout=route`: không fold nào có tuyến chung giữa train và test; `holdout=snapshot` cũng vậy với snapshot.
- Mẫu cao tốc không có train cùng loại thì bị đếm là không chấm được, không lấy mẫu loại khác bù vào.
- Báo cáo có `source`, `holdout`, và giữ nguyên `scores` cùng `limitations`.
