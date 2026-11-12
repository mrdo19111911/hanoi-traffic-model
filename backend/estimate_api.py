"""Routes of the travel-time estimator: health, options, sample status, estimate, crawl and experiment jobs.

Spec: ``estimate_router(root, make_estimator, key_reader, osrm_probe, jobs)`` returns an ``APIRouter``.
- ``/api/health`` exposes only booleans about the key and services.
- ``/api/estimate`` maps estimator ``ValueError`` subclasses to 422 through ``ESTIMATE_ERRORS`` and any other
  failure to a fixed 503; exception text never reaches the client.
- ``/api/crawl`` (202) needs a configured key (503 otherwise); ``/api/experiments/run`` (202); both go
  through the shared ``JobManager``.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .big_roads import big_roads_current
from .samples import CrawlRequest, DEFAULT_METHOD, ESTIMATE_ERRORS, EstimateRequest, METHODS, ROAD_CLASSES, data_status


def estimate_router(root, make_estimator, key_reader, osrm_probe, jobs):
    router = APIRouter()

    @router.get("/api/health")
    def health():
        return {"status": "ok", "osrm_ready": bool(osrm_probe()), "api_key_configured": bool(key_reader()),
                "samples_ready": data_status(root)["sample_count"] > 0, "big_roads_current": big_roads_current(root)}

    @router.get("/api/options")
    def options():
        return {"road_classes": ROAD_CLASSES, "methods": METHODS, "default_method": DEFAULT_METHOD}

    @router.get("/api/data/status")
    def samples_status():
        return data_status(root)

    @router.get("/api/experiments")
    def experiments():
        path = root / "reports/spatial_comparison.json"
        if not path.is_file():
            raise HTTPException(404, "Chưa có báo cáo so sánh. Hãy chạy kiểm nghiệm.")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            raise HTTPException(503, "Báo cáo chưa sẵn sàng. Hãy chạy lại kiểm nghiệm.") from None

    @router.post("/api/estimate")
    def estimate(body: EstimateRequest):
        try:
            return make_estimator().estimate(body.origin.lat, body.origin.lon, body.destination.lat,
                                            body.destination.lon, body.day, body.hour, body.method)
        except ValueError as exc:
            raise HTTPException(422, ESTIMATE_ERRORS.get(type(exc).__name__, "Không thể ước lượng với tuyến và dữ liệu đã chọn.")) from None
        except Exception:
            raise HTTPException(503, "Chưa thể ước lượng. Kiểm tra OSRM, chỉ mục đường OSM và dữ liệu tốc độ trên máy chủ.") from None

    @router.post("/api/crawl", status_code=202)
    def crawl(body: CrawlRequest):
        if not key_reader():
            raise HTTPException(503, "Máy chủ chưa cấu hình TOMTOM_API_KEY.")
        return jobs.submit("crawl", body.model_dump(mode="json"))

    @router.post("/api/experiments/run", status_code=202)
    def run_experiments():
        return jobs.submit("experiments", {})

    @router.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        return jobs.get(job_id)

    return router
