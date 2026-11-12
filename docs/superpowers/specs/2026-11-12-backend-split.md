# Tách `backend/app.py` thành module (2026-11-12)

## Mục tiêu

`backend/app.py` 560 dòng với `create_app` 233 dòng chứa toàn bộ route, model, job, OSRM client và credential. Tách theo trách nhiệm để mỗi file ≤ 300 dòng, mỗi hàm ≤ 100 dòng, **không đổi hành vi API**.

## Đầu vào / đầu ra giữ nguyên

- `create_app(*, root=, estimator_factory=, osrm_probe=, key_reader=, job_runner=, big_route_osrm=)` — chữ ký, giá trị mặc định và thứ tự route không đổi.
- Điểm vào `backend.app:app`; `backend.app` vẫn xuất `ESTIMATE_ERRORS`, `create_app`.
- Bảng route (method, path) đóng băng trong `tests/test_backend_layout.py::EXPECTED_ROUTES`; hai catch-all (`/api/{path}` 404 JSON, `/{path}` SPA) vẫn đăng ký sau cùng.
- Mọi thông điệp lỗi tiếng Việt, mã trạng thái, gzip, handler lỗi validate giữ nguyên.

## Bố cục

| Module | Trách nhiệm | Phụ thuộc trong gói |
|---|---|---|
| `credentials.py` | `read_api_key` (env → `HKCU\Environment`) | – |
| `osrm_client.py` | `osrm_base`, `probe_osrm`, `osrm_big_route`, `route_pairs` | – |
| `samples.py` | `ROAD_CLASSES`, `METHODS`, `ESTIMATE_ERRORS`, request model, `data_status` | – |
| `jobs.py` | `JobManager`, `subprocess_runner(root, key_reader)`, `now()` | `samples` |
| `estimate_api.py` | router: health, options, data/status, experiments, estimate, crawl, experiments/run, jobs/{id} | `samples`, `big_roads` (chỉ `big_roads_current`) |
| `big_roads.py` | router: file đã dựng, `ReviewStore`, `/route` (`open_big_road_edges`, `trace_segments`, `route_summary`) | `samples`, `osrm_client`, `jobs.now` |
| `app.py` | `create_app`: deps mặc định, `JobManager`, FastAPI, handler validate, `include_router` ×2, hai catch-all | tất cả |

Không có vòng import: `estimate_api → big_roads → {samples, osrm_client, jobs}`; `jobs → samples`.

## Tiêu chí xong

1. `tests/test_backend.py` (characterization sẵn có, 46 test) xanh không sửa gì ngoài chỗ patch `urlopen` chuyển sang `backend.osrm_client`.
2. `tests/test_backend_layout.py`: bảng route không đổi, catch-all cuối, mọi `backend/*.py` ≤ 300 dòng, không hàm > 100 dòng.
3. `tests/test_backend_jobs.py`: nhánh `subprocess_runner` (trước đây không có test) — lệnh, key chỉ qua env, thiếu key không chạy tiến trình, exit ≠ 0 → `RuntimeError` không kèm output.
4. Toàn bộ bộ test offline xanh.
