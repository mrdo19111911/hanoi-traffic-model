"""Travel-time API vocabulary and the spatial sample inventory.

Spec:
- ``ROAD_CLASSES``, ``METHODS``, ``Method`` and ``ESTIMATE_ERRORS`` are the public contract of
  ``/api/options`` and ``/api/estimate``. ``ESTIMATE_ERRORS`` is keyed by estimator exception
  *class name* (renaming an exception in ``model/estimate_spatial.py`` breaks the mapping).
- Request models reject unknown fields and non-finite numbers.
- ``data_status(root)`` counts the valid rows of ``data/spatial_samples.jsonl`` per class and slot,
  never raises, and reports corruption as ``quality.invalid_rows`` plus a warning.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROAD_CLASSES = [
    {"id": "caotoc", "label": "Cao tốc", "color": "#dc3e48"},
    {"id": "quoclo", "label": "Quốc lộ", "color": "#e89b36"},
    {"id": "chinh", "label": "Đường chính", "color": "#3974d8"},
    {"id": "noido", "label": "Đường nội đô", "color": "#2a9d8f"},
    {"id": "nho", "label": "Đường nhỏ", "color": "#8c64bc"},
]
CLASS_IDS = {c["id"] for c in ROAD_CLASSES}
METHODS = [
    {"id": "mean", "label": "Trung bình cùng loại đường"},
    {"id": "nearest", "label": "Điểm cùng loại gần nhất"},
    {"id": "idw1", "label": "Nghịch đảo khoảng cách (IDW 1)"},
    {"id": "idw2", "label": "Nghịch đảo bình phương khoảng cách (IDW 2)"},
    {"id": "linear_idw1", "label": "Tam giác tuyến tính + IDW 1"},
    {"id": "linear_idw2", "label": "Tam giác tuyến tính + IDW 2"},
]
Method = Literal["mean", "nearest", "idw1", "idw2", "linear_idw1", "linear_idw2"]
DEFAULT_METHOD = "idw1"
# Estimator exception class name -> user-facing message (renaming the exceptions breaks this map).
ESTIMATE_ERRORS = {
    "MissingRoadData": "Thiếu dữ liệu cho đúng loại đường, ngày hoặc khung giờ của tuyến. Hãy thu thập thêm dữ liệu.",
    "RoadClassError": "Chưa xác định được đúng loại đường trên tuyến. Cần kiểm tra chỉ mục đường OSM.",
    "RouteAlignmentError": "Tuyến OSRM chưa khớp chỉ mục đường OSM. Cần kiểm tra dữ liệu bản đồ.",
    "SpatialDataError": "Dữ liệu không gian chưa sẵn sàng hoặc chưa hợp lệ. Hãy thu thập thêm dữ liệu.",
}
SLOT_RE = re.compile(r"(?:[01][0-9]|2[0-3])[0-5][0-9]")


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
    method: Method = DEFAULT_METHOD


class CrawlRequest(InputModel):
    date: date
    slots: list[str] = Field(min_length=1, max_length=48)
    max_calls: int = Field(ge=1, le=60, strict=True)

    @field_validator("slots")
    @classmethod
    def valid_slots(cls, slots):
        if any(not SLOT_RE.fullmatch(slot) for slot in slots):
            raise ValueError("Khung giờ phải có dạng HHMM, từ 0000 đến 2359.")
        return list(dict.fromkeys(slots))


def _valid_sample(row):
    cls, slot = row["road_class"], row["slot"]
    lat, lon, speed = float(row["lat"]), float(row["lon"]), float(row["speed_kmh"])
    return (cls in CLASS_IDS and isinstance(slot, str) and SLOT_RE.fullmatch(slot) is not None
            and all(math.isfinite(x) for x in (lat, lon, speed))
            and -90 <= lat <= 90 and -180 <= lon <= 180 and speed > 0
            and type(row.get("day")) is int and 0 <= row["day"] <= 6)


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
                        if not _valid_sample(row):
                            raise ValueError("invalid sample")
                        counts[row["road_class"]] += 1
                        slots[row["slot"]] += 1
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
