"""Big-road map (network A) routes: built files, the review store and route classification against network A.

Spec: ``big_roads_router(root, big_route_osrm)`` returns an ``APIRouter``.
- Built files under ``osrm/data`` are served as they are; a missing build is a 404 with a rebuild hint.
- ``big_roads_current(root)`` is True only when ``big_roads.meta.json`` matches the OSRM map (``pbf_sha256``)
  and the current ``BIG_ROAD_POLICY_VERSION``.
- Review items live in ``data/big_road_review.json`` behind one lock; writes are atomic (temp file + replace)
  and a corrupt file is reported (503), never overwritten.
- ``/api/big-roads/route``: the edge index is verified against the OSRM map *before* OSRM is called
  (404 missing, 503 unreadable, 422 other map); every node pair of the route is looked up in the index, a
  listed pair is on A, an absent pair is off A, never guessed; zero-length joins inherit the previous segment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import Field

from model.osm_classes import BIG_ROAD_POLICY_VERSION

from .jobs import now
from .osrm_client import route_pairs
from .samples import ESTIMATE_ERRORS, InputModel

BIG_ROAD_FILES = {"roads": "big_roads.geojson", "junctions": "big_road_junctions.geojson", "meta": "big_roads.meta.json",
                  "land": "vietnam_land.geojson", "boundary": "vietnam_boundary.geojson", "provinces": "vietnam_provinces.geojson",
                  "edges": "big_road_edges.sqlite"}
BIG_ROAD_MISSING = "Chưa dựng bản đồ đường lớn. Chạy python model/build_big_roads.py."
BIG_ROAD_UNREADABLE = "Bản đồ đường lớn chưa hợp lệ. Chạy lại python model/build_big_roads.py."
REVIEW_KEY = re.compile(r"(?:ferry:)?[0-9]+")
REVIEW_UNREADABLE = "Không đọc được tệp data/big_road_review.json. Kiểm tra hoặc xóa tệp này rồi thử lại."
REVIEW_UNWRITABLE = "Chưa ghi được trạng thái rà soát. Kiểm tra quyền ghi thư mục data/."
OSRM_DOWN = "OSRM chưa chạy. Hãy khởi động bằng python app.py start."
NO_ROUTE = "Không tìm được tuyến giữa hai điểm."

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class BigRouteRequest(InputModel):
    origin: tuple[Latitude, Longitude]
    destination: tuple[Latitude, Longitude]


class ReviewRequest(InputModel):
    status: Literal["unseen", "seen", "skipped"]  # chưa xem / đã xem / bỏ qua
    note: str = Field(default="", max_length=500)


def big_roads_current(root):
    """True when the big-road build matches the OSRM map and the current policy."""
    try:
        meta = json.loads((root / "osrm/data" / BIG_ROAD_FILES["meta"]).read_text(encoding="utf-8"))
        build = json.loads((root / "osrm/build.json").read_text(encoding="utf-8"))
        return meta["pbf_sha256"] == build["input"]["pbf_sha256"] and meta["policy_version"] == BIG_ROAD_POLICY_VERSION
    except (OSError, ValueError, KeyError, TypeError):
        return False


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


def trace_segments(conn, coords, nodes, distances, durations):
    """Split the route into runs of consecutive edges that are on / off network A."""
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
    return segments


def route_summary(segments, durations):
    on_m = sum(s["m"] for s in segments if s["on_a"])
    off_m = sum(s["m"] for s in segments if not s["on_a"])
    return {"segments": [{"on_a": s["on_a"], "coords": s["coords"], "km": round(s["m"] / 1000, 3),
                          "seconds": round(s["s"], 1)} for s in segments],
            "km": round((on_m + off_m) / 1000, 3), "minutes": round(sum(durations) / 60, 2),
            "km_on_a": round(on_m / 1000, 3), "km_off_a": round(off_m / 1000, 3)}


class ReviewStore:
    """Review items in data/big_road_review.json; every read/write happens under ``lock``."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def read(self):
        if not self.path.is_file():
            return {}
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))["items"]
            if not isinstance(items, dict):
                raise TypeError("items must be an object")
            return items
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, REVIEW_UNREADABLE) from None

    def write(self, items):
        """Write via a temp file in the same directory, then os.replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".big_road_review.", suffix=".tmp", delete=False)
        try:
            with handle:
                json.dump({"items": items}, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise


def big_roads_router(root, big_route_osrm):
    router = APIRouter()
    review = ReviewStore(root / "data/big_road_review.json")

    def big_road_file(kind):
        path = root / "osrm/data" / BIG_ROAD_FILES[kind]
        if not path.is_file():
            raise HTTPException(404, BIG_ROAD_MISSING)
        return FileResponse(path, media_type="application/json" if kind == "meta" else "application/geo+json")

    for kind, suffix in [("roads", ""), ("junctions", "/junctions"), ("meta", "/meta"), ("land", "/land"),
                         ("boundary", "/boundary"), ("provinces", "/provinces")]:
        router.add_api_route("/api/big-roads" + suffix, (lambda k: lambda: big_road_file(k))(kind),
                             methods=["GET"], name=f"big_road_{kind}")

    @router.get("/api/big-roads/clusters")
    def big_road_clusters():
        path = root / "osrm/data/big_road_clusters.json"
        if not path.is_file():
            raise HTTPException(404, "Chưa có dữ liệu cụm.")
        return FileResponse(path, media_type="application/json")

    @router.get("/api/big-roads/review")
    def big_road_review():
        with review.lock:
            return {"items": review.read()}

    @router.put("/api/big-roads/review/{key}")
    def put_big_road_review(key: str, body: ReviewRequest):
        if not REVIEW_KEY.fullmatch(key):
            raise HTTPException(422, "Khóa mục rà không hợp lệ. Dùng id node OSM hoặc ferry:<way_id>.")
        with review.lock:
            items = review.read()
            item = {"status": body.status, "note": body.note, "updated_at": now()}
            items[key] = item
            try:
                review.write(items)
            except OSError:
                raise HTTPException(503, REVIEW_UNWRITABLE) from None
            return item

    @router.post("/api/big-roads/route")
    def big_road_route(body: BigRouteRequest):
        conn = open_big_road_edges(root)  # verified before OSRM is called
        try:
            try:
                response = big_route_osrm(body.origin[0], body.origin[1], body.destination[0], body.destination[1])
            except Exception:
                raise HTTPException(503, OSRM_DOWN) from None  # exception text may contain URLs
            if not isinstance(response, dict) or response.get("code") != "Ok" or not response.get("routes"):
                raise HTTPException(422, NO_ROUTE)
            try:
                coords, nodes, distances, durations = route_pairs(response)
            except ValueError:
                raise HTTPException(422, ESTIMATE_ERRORS["RouteAlignmentError"]) from None
            segments = trace_segments(conn, coords, nodes, distances, durations)
        except sqlite3.Error:
            raise HTTPException(503, BIG_ROAD_UNREADABLE) from None
        finally:
            conn.close()
        return route_summary(segments, durations)

    return router
