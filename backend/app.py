"""Local-only HTTP API, credential handling, and bounded background jobs.

Run with ``python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000``.
The API deliberately keeps the TomTom key in the server process environment.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import sqlite3
import tempfile
import threading
from typing import Annotated, Callable, Literal
from urllib.error import HTTPError
from urllib.request import urlopen
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from model.osm_classes import BIG_ROAD_POLICY_VERSION


ROOT = Path(__file__).resolve().parents[1]
ROAD_CLASSES = [
    {"id": "caotoc", "label": "Cao tốc", "color": "#dc3e48"},
    {"id": "quoclo", "label": "Quốc lộ", "color": "#e89b36"},
    {"id": "chinh", "label": "Đường chính", "color": "#3974d8"},
    {"id": "noido", "label": "Đường nội đô", "color": "#2a9d8f"},
    {"id": "nho", "label": "Đường nhỏ", "color": "#8c64bc"},
]
METHODS = [
    {"id": "mean", "label": "Trung bình cùng loại đường"},
    {"id": "nearest", "label": "Điểm cùng loại gần nhất"},
    {"id": "idw1", "label": "Nghịch đảo khoảng cách (IDW 1)"},
    {"id": "idw2", "label": "Nghịch đảo bình phương khoảng cách (IDW 2)"},
    {"id": "linear_idw1", "label": "Tam giác tuyến tính + IDW 1"},
    {"id": "linear_idw2", "label": "Tam giác tuyến tính + IDW 2"},
]
Method = Literal["mean", "nearest", "idw1", "idw2", "linear_idw1", "linear_idw2"]
# Estimator exception class name -> user-facing message (renaming the exceptions breaks this map).
ESTIMATE_ERRORS = {
    "MissingRoadData": "Thiếu dữ liệu cho đúng loại đường, ngày hoặc khung giờ của tuyến. Hãy thu thập thêm dữ liệu.",
    "RoadClassError": "Chưa xác định được đúng loại đường trên tuyến. Cần kiểm tra chỉ mục đường OSM.",
    "RouteAlignmentError": "Tuyến OSRM chưa khớp chỉ mục đường OSM. Cần kiểm tra dữ liệu bản đồ.",
    "SpatialDataError": "Dữ liệu không gian chưa sẵn sàng hoặc chưa hợp lệ. Hãy thu thập thêm dữ liệu.",
}


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Point(InputModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class EstimateRequest(InputModel):
    origin: Point
    destination: Point
    day: int = Field(ge=0, le=6, strict=True)
    hour: float = Field(ge=0, le=24)
    method: Method = "idw1"


Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class BigRouteRequest(InputModel):
    origin: tuple[Latitude, Longitude]
    destination: tuple[Latitude, Longitude]


REVIEW_STATUSES = ("unseen", "seen", "skipped")  # chưa xem / đã xem / bỏ qua


class ReviewRequest(InputModel):
    status: Literal["unseen", "seen", "skipped"]
    note: str = Field(default="", max_length=500)


class CrawlRequest(InputModel):
    date: date
    slots: list[str] = Field(min_length=1, max_length=48)
    max_calls: int = Field(ge=1, le=60, strict=True)

    @field_validator("slots")
    @classmethod
    def valid_slots(cls, slots):
        if any(not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", slot) for slot in slots):
            raise ValueError("Khung giờ phải có dạng HHMM, từ 0000 đến 2359.")
        return list(dict.fromkeys(slots))


def read_api_key():
    """Read process then Windows User configuration without exposing the value."""
    key = os.environ.get("TOMTOM_API_KEY", "").strip()
    if key:
        return key
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, "TOMTOM_API_KEY")
                return str(value).strip() or None
        except OSError:
            pass
    return None


def probe_osrm():
    try:
        with urlopen("http://127.0.0.1:5000/nearest/v1/driving/105.85,21.03?number=1", timeout=1.5) as response:
            return json.load(response).get("code") == "Ok"
    except Exception:
        return False


def _now():
    return datetime.now(timezone.utc).isoformat()


BIG_ROAD_FILES = {"roads": "big_roads.geojson", "junctions": "big_road_junctions.geojson", "meta": "big_roads.meta.json",
                  "land": "vietnam_land.geojson", "boundary": "vietnam_boundary.geojson", "provinces": "vietnam_provinces.geojson",
                  "edges": "big_road_edges.sqlite"}
BIG_ROAD_MISSING = "Chưa dựng bản đồ đường lớn. Chạy python model/build_big_roads.py."
BIG_ROAD_UNREADABLE = "Bản đồ đường lớn chưa hợp lệ. Chạy lại python model/build_big_roads.py."
REVIEW_KEY = re.compile(r"(?:ferry:)?[0-9]+")
REVIEW_UNREADABLE = "Không đọc được tệp data/big_road_review.json. Kiểm tra hoặc xóa tệp này rồi thử lại."
OSRM_DOWN = "OSRM chưa chạy. Hãy khởi động bằng python app.py start."
NO_ROUTE = "Không tìm được tuyến giữa hai điểm."


def osrm_big_route(lat1, lon1, lat2, lon2):
    """OSRM /route with per-node annotations; NoRoute answers (HTTP 400) are returned, not raised."""
    base = os.environ.get("OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
    url = (f"{base}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
           "?overview=full&geometries=geojson&annotations=nodes,distance,duration")
    try:
        with urlopen(url, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        with exc:
            return json.load(exc)


def route_pairs(response):
    """Return (coords, nodes, distances, durations) of the first route; coords align with nodes.

    Raises ValueError when the response is incomplete or misaligned.
    """
    try:
        route = response["routes"][0]
        geometry = route["geometry"]
        if geometry.get("type") != "LineString":
            raise ValueError("geometry must be a GeoJSON LineString")
        coords = [[float(lon), float(lat)] for lon, lat in geometry["coordinates"]]
        nodes, distances, durations = [], [], []
        for leg in route["legs"]:
            annotation = leg["annotation"]
            leg_nodes, leg_distances, leg_durations = annotation["nodes"], annotation["distance"], annotation["duration"]
            if not len(leg_nodes) == len(leg_distances) + 1 == len(leg_durations) + 1:
                raise ValueError("annotation lengths do not align")
            if nodes and nodes[-1] != leg_nodes[0]:
                raise ValueError("leg endpoints do not align")
            nodes.extend(leg_nodes[1:] if nodes else leg_nodes)
            distances.extend(float(d) for d in leg_distances)
            durations.extend(float(d) for d in leg_durations)
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("incomplete OSRM route") from exc
    if len(coords) != len(nodes) or len(nodes) < 2:
        raise ValueError("geometry and annotation nodes do not align")
    if not all(math.isfinite(x) for point in coords for x in point):
        raise ValueError("invalid coordinate")
    if any(not math.isfinite(x) or x < 0 for x in distances + durations):
        raise ValueError("invalid distance or duration")
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in nodes):
        raise ValueError("invalid node id")
    return coords, nodes, distances, durations


def open_big_road_edges(root):
    """Read-only connection to the network-A node-pair index, verified against the OSRM map.

    Missing file -> 404, unreadable -> 503, other map (or unverifiable build) -> 422.
    """
    path = root / "osrm/data" / BIG_ROAD_FILES["edges"]
    if not path.is_file():
        raise HTTPException(404, BIG_ROAD_MISSING)
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        row = conn.execute("SELECT value FROM metadata WHERE key = 'pbf_sha256'").fetchone()
        conn.execute("SELECT a, b FROM edges LIMIT 1").fetchall()
        if not row or not isinstance(row[0], str) or not row[0]:
            raise ValueError("missing pbf_sha256")
    except (sqlite3.Error, ValueError):
        if conn is not None:
            conn.close()
        raise HTTPException(503, BIG_ROAD_UNREADABLE) from None
    try:
        expected = json.loads((root / "osrm/build.json").read_text(encoding="utf-8"))["input"]["pbf_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        expected = None
    if expected != row[0]:
        conn.close()
        raise HTTPException(422, ESTIMATE_ERRORS["SpatialDataError"])
    return conn


def big_roads_current(root):
    """True when the big-road build matches the OSRM map and the current policy."""
    try:
        meta = json.loads((root / "osrm/data" / BIG_ROAD_FILES["meta"]).read_text(encoding="utf-8"))
        build = json.loads((root / "osrm/build.json").read_text(encoding="utf-8"))
        return meta["pbf_sha256"] == build["input"]["pbf_sha256"] and meta["policy_version"] == BIG_ROAD_POLICY_VERSION
    except (OSError, ValueError, KeyError, TypeError):
        return False


def data_status(root):
    path = root / "data/spatial_samples.jsonl"
    counts, slots, invalid = Counter(), Counter(), 0
    if path.is_file():
        try:
            with path.open(encoding="utf-8-sig") as source:
                for line in source:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        cls, slot = row["road_class"], row["slot"]
                        lat, lon, speed = float(row["lat"]), float(row["lon"]), float(row["speed_kmh"])
                        if (cls not in {c["id"] for c in ROAD_CLASSES}
                                or not isinstance(slot, str)
                                or not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", slot)
                                or not all(math.isfinite(x) for x in (lat, lon, speed))
                                or not (-90 <= lat <= 90 and -180 <= lon <= 180 and speed > 0)
                                or type(row.get("day")) is not int or not 0 <= row["day"] <= 6):
                            raise ValueError("invalid sample")
                        counts[cls] += 1
                        slots[slot] += 1
                    except (ValueError, TypeError, KeyError, AttributeError):
                        invalid += 1
        except OSError:
            invalid += 1
    count = sum(counts.values())
    missing = [c["id"] for c in ROAD_CLASSES if counts[c["id"]] == 0]
    warnings = []
    if not count:
        warnings.append("Chưa có mẫu không gian hợp lệ. Hãy thu thập dữ liệu trước khi ước lượng.")
    elif missing:
        warnings.append("Một số loại đường chưa có dữ liệu; không dùng vận tốc loại đường khác để thay thế.")
    if invalid:
        warnings.append("Có dòng dữ liệu không hợp lệ đã được bỏ qua.")
    return {
        "sample_count": count,
        "by_class": {c["id"]: counts[c["id"]] for c in ROAD_CLASSES},
        "by_slot": dict(sorted(slots.items())),
        "last_updated": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if path.is_file() else None,
        "quality": {"status": "missing" if not count else "partial" if missing or invalid else "ready",
                    "invalid_rows": invalid, "missing_classes": missing, "warnings": warnings},
    }


class JobManager:
    def __init__(self, runner):
        self.runner = runner
        self.lock = threading.Lock()
        self.jobs = {}

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise HTTPException(404, "Không tìm thấy tác vụ.")
            return dict(job)

    def submit(self, kind, payload):
        with self.lock:
            if any(job["kind"] == kind and job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise HTTPException(409, "Tác vụ cùng loại đang chạy. Vui lòng đợi hoàn tất.")
            # Retain recent jobs without allowing an unbounded process history.
            if len(self.jobs) >= 100:
                oldest = next((key for key, job in self.jobs.items() if job["status"] in ("completed", "failed")), None)
                if oldest:
                    del self.jobs[oldest]
            job = {"id": uuid.uuid4().hex, "kind": kind, "status": "queued", "created_at": _now(),
                   "started_at": None, "finished_at": None, "result": None, "error": None}
            self.jobs[job["id"]] = job
            snapshot = dict(job)
        threading.Thread(target=self._run, args=(job["id"], kind, payload), daemon=True).start()
        return snapshot

    def _run(self, job_id, kind, payload):
        with self.lock:
            self.jobs[job_id].update(status="running", started_at=_now())
        try:
            result = self.runner(kind, payload)
            with self.lock:
                self.jobs[job_id].update(status="completed", result=result, finished_at=_now())
        except Exception:
            # Subprocess/HTTP exception strings can include API keys and URLs.
            with self.lock:
                self.jobs[job_id].update(status="failed", finished_at=_now(),
                    error="Tác vụ chưa hoàn tất. Kiểm tra dữ liệu, cấu hình API và kết nối dịch vụ trên máy chủ.")


def create_app(*, root=None, estimator_factory=None, osrm_probe=None, key_reader=None, job_runner=None,
               big_route_osrm=None):
    root = Path(root or ROOT).resolve()
    key_reader = key_reader or read_api_key
    osrm_probe = osrm_probe or probe_osrm
    big_route_osrm = big_route_osrm or osrm_big_route
    review_path = root / "data/big_road_review.json"
    review_lock = threading.Lock()

    def make_estimator():
        if estimator_factory is not None:
            return estimator_factory()
        from model.estimate_spatial import SpatialEstimator
        return SpatialEstimator(root=root)

    def run_job(kind, payload):
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        if kind == "crawl":
            key = key_reader()
            if not key:
                raise RuntimeError("Missing API configuration")
            environment["TOMTOM_API_KEY"] = key
            command = [sys.executable, str(root / "download/crawl_spatial.py"), "--date", payload["date"],
                       "--slots", *payload["slots"], "--max-calls", str(payload["max_calls"])]
        else:
            command = [sys.executable, str(root / "model/compare_spatial.py")]
        completed = subprocess.run(command, cwd=root, env=environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=1800, check=False)
        if completed.returncode:
            raise RuntimeError("Local worker failed")
        return data_status(root) if kind == "crawl" else {"report_available": (root / "reports/spatial_comparison.json").is_file()}

    jobs = JobManager(job_runner or run_job)
    app = FastAPI(title="Hanoi traffic model", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.jobs = jobs
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": "Thông số không hợp lệ. Kiểm tra tọa độ, ngày, giờ, phương pháp và giới hạn API.",
            "errors": [{"field": ".".join(str(x) for x in error["loc"]), "type": error["type"]} for error in exc.errors()]})

    @app.get("/api/health")
    def health():
        return {"status": "ok", "osrm_ready": bool(osrm_probe()), "api_key_configured": bool(key_reader()),
                "samples_ready": data_status(root)["sample_count"] > 0, "big_roads_current": big_roads_current(root)}

    @app.get("/api/options")
    def options():
        return {"road_classes": ROAD_CLASSES, "methods": METHODS, "default_method": "idw1"}

    @app.get("/api/data/status")
    def samples_status():
        return data_status(root)

    @app.get("/api/experiments")
    def experiments():
        path = root / "reports/spatial_comparison.json"
        if not path.is_file():
            raise HTTPException(404, "Chưa có báo cáo so sánh. Hãy chạy kiểm nghiệm.")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            raise HTTPException(503, "Báo cáo chưa sẵn sàng. Hãy chạy lại kiểm nghiệm.") from None

    @app.post("/api/estimate")
    def estimate(body: EstimateRequest):
        try:
            return make_estimator().estimate(body.origin.lat, body.origin.lon, body.destination.lat,
                                            body.destination.lon, body.day, body.hour, body.method)
        except ValueError as exc:
            raise HTTPException(422, ESTIMATE_ERRORS.get(type(exc).__name__, "Không thể ước lượng với tuyến và dữ liệu đã chọn.")) from None
        except Exception:
            raise HTTPException(503, "Chưa thể ước lượng. Kiểm tra OSRM, chỉ mục đường OSM và dữ liệu tốc độ trên máy chủ.") from None

    @app.post("/api/crawl", status_code=202)
    def crawl(body: CrawlRequest):
        if not key_reader():
            raise HTTPException(503, "Máy chủ chưa cấu hình TOMTOM_API_KEY.")
        return jobs.submit("crawl", body.model_dump(mode="json"))

    @app.post("/api/experiments/run", status_code=202)
    def run_experiments():
        return jobs.submit("experiments", {})

    def big_road_file(kind):
        path = root / "osrm/data" / BIG_ROAD_FILES[kind]
        if not path.is_file():
            raise HTTPException(404, BIG_ROAD_MISSING)
        return FileResponse(path, media_type="application/json" if kind == "meta" else "application/geo+json")

    def read_review():
        """Stored review items; caller holds review_lock."""
        if not review_path.is_file():
            return {}
        try:
            items = json.loads(review_path.read_text(encoding="utf-8"))["items"]
            if not isinstance(items, dict):
                raise TypeError("items must be an object")
            return items
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, REVIEW_UNREADABLE) from None

    def write_review(items):
        """Write via a temp file in the same directory, then os.replace; caller holds review_lock."""
        review_path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=review_path.parent,
                                             prefix=".big_road_review.", suffix=".tmp", delete=False)
        try:
            with handle:
                json.dump({"items": items}, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, review_path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise

    @app.get("/api/big-roads/boundary")
    def big_road_boundary():
        return big_road_file("boundary")

    @app.get("/api/big-roads/provinces")
    def big_road_provinces():
        return big_road_file("provinces")

    @app.get("/api/big-roads/clusters")
    def big_road_clusters():
        path = root / "osrm/data/big_road_clusters.json"
        if not path.is_file():
            raise HTTPException(404, "Chưa có dữ liệu cụm.")
        return FileResponse(path, media_type="application/json")

    @app.get("/api/big-roads/review")
    def big_road_review():
        with review_lock:
            return {"items": read_review()}

    @app.put("/api/big-roads/review/{key}")
    def put_big_road_review(key: str, body: ReviewRequest):
        if not REVIEW_KEY.fullmatch(key):
            raise HTTPException(422, "Khóa mục rà không hợp lệ. Dùng id node OSM hoặc ferry:<way_id>.")
        with review_lock:
            items = read_review()
            item = {"status": body.status, "note": body.note, "updated_at": _now()}
            items[key] = item
            try:
                write_review(items)
            except OSError:
                raise HTTPException(503, "Chưa ghi được trạng thái rà soát. Kiểm tra quyền ghi thư mục data/.") from None
            return item

    @app.post("/api/big-roads/route")
    def big_road_route(body: BigRouteRequest):
        conn = open_big_road_edges(root)  # verified before OSRM is called
        try:
            try:
                response = big_route_osrm(body.origin[0], body.origin[1], body.destination[0], body.destination[1])
            except Exception:
                # Exception text may contain URLs; never return it.
                raise HTTPException(503, OSRM_DOWN) from None
            if not isinstance(response, dict) or response.get("code") != "Ok" or not response.get("routes"):
                raise HTTPException(422, NO_ROUTE)
            try:
                coords, nodes, distances, durations = route_pairs(response)
            except ValueError:
                raise HTTPException(422, ESTIMATE_ERRORS["RouteAlignmentError"]) from None
            pair_on_a, segments = {}, []
            for i, (distance, duration) in enumerate(zip(distances, durations)):
                if distance == 0 and segments:
                    on_a = segments[-1]["on_a"]  # zero-length joins carry no class evidence
                else:
                    pair = tuple(sorted((nodes[i], nodes[i + 1])))  # the index stores pairs sorted
                    if pair not in pair_on_a:
                        # Any listed pair (A way, bridge or ferry) is on A; an absent pair is off A, never guessed.
                        pair_on_a[pair] = conn.execute("SELECT 1 FROM edges WHERE a=? AND b=? LIMIT 1", pair).fetchone() is not None
                    on_a = pair_on_a[pair]
                if not segments or segments[-1]["on_a"] != on_a:
                    segments.append({"on_a": on_a, "coords": [coords[i]], "m": 0., "s": 0.})
                segment = segments[-1]
                segment["coords"].append(coords[i + 1])
                segment["m"] += distance
                segment["s"] += duration
        except sqlite3.Error:
            raise HTTPException(503, BIG_ROAD_UNREADABLE) from None
        finally:
            conn.close()
        on_m = sum(s["m"] for s in segments if s["on_a"])
        off_m = sum(s["m"] for s in segments if not s["on_a"])
        return {"segments": [{"on_a": s["on_a"], "coords": s["coords"], "km": round(s["m"] / 1000, 3),
                              "seconds": round(s["s"], 1)} for s in segments],
                "km": round((on_m + off_m) / 1000, 3), "minutes": round(sum(durations) / 60, 2),
                "km_on_a": round(on_m / 1000, 3), "km_off_a": round(off_m / 1000, 3)}

    @app.get("/api/big-roads")
    def big_roads():
        return big_road_file("roads")

    @app.get("/api/big-roads/junctions")
    def big_road_junctions():
        return big_road_file("junctions")

    @app.get("/api/big-roads/meta")
    def big_road_meta():
        return big_road_file("meta")

    @app.get("/api/big-roads/land")
    def big_road_land():
        return big_road_file("land")

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str):
        return jobs.get(job_id)

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
