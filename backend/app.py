"""Local-only HTTP API: wiring of the routers, validation errors, the API 404 and the built frontend.

Run with ``python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000`` (one worker: job state is
in-process). ``create_app`` takes every external dependency as a keyword so tests run with a temp root
and fakes; the TomTom key stays in the server process (see ``credentials.py``).

Modules: ``samples`` (API vocabulary, request models, sample inventory), ``estimate_api`` (estimator and
job routes), ``big_roads`` (network-A routes), ``jobs`` (JobManager + local worker), ``osrm_client``,
``credentials``.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .big_roads import big_roads_router
from .credentials import read_api_key
from .estimate_api import estimate_router
from .jobs import JobManager, subprocess_runner
from .osrm_client import osrm_big_route, probe_osrm
from .samples import ESTIMATE_ERRORS  # noqa: F401  (public: tests and callers import it from here)

ROOT = Path(__file__).resolve().parents[1]
INVALID_REQUEST = "Thông số không hợp lệ. Kiểm tra tọa độ, ngày, giờ, phương pháp và giới hạn API."


def create_app(*, root=None, estimator_factory=None, osrm_probe=None, key_reader=None, job_runner=None,
               big_route_osrm=None):
    root = Path(root or ROOT).resolve()
    key_reader = key_reader or read_api_key

    def make_estimator():
        if estimator_factory is not None:
            return estimator_factory()
        from model.estimate_spatial import SpatialEstimator
        return SpatialEstimator(root=root)

    jobs = JobManager(job_runner or subprocess_runner(root, key_reader))
    app = FastAPI(title="Hanoi traffic model", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.jobs = jobs
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": INVALID_REQUEST,
            "errors": [{"field": ".".join(str(x) for x in error["loc"]), "type": error["type"]} for error in exc.errors()]})

    app.include_router(estimate_router(root, make_estimator, key_reader, osrm_probe or probe_osrm, jobs))
    app.include_router(big_roads_router(root, big_route_osrm or osrm_big_route))

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    def unknown_api(path: str):
        raise HTTPException(404, "Không tìm thấy API.")

    @app.get("/{path:path}")
    def frontend(path: str):
        dist = (root / "frontend/dist").resolve()
        requested = (dist / path).resolve()
        if not requested.is_relative_to(dist):
            raise HTTPException(404, "Không tìm thấy tệp.")
        if requested.is_file():
            return FileResponse(requested)
        index = dist / "index.html"
        if index.is_file() and (not path or not Path(path).suffix):
            return FileResponse(index)
        raise HTTPException(404, "Giao diện chưa được build. Chạy bước cài đặt giao diện trước.")

    return app


app = create_app()
