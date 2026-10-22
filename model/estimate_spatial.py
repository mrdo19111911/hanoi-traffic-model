"""Route travel times using exact OSM road classes and spatial speed fields.

Every positive length OSRM annotation edge must have one unambiguous class in
the index built from the same PBF. A speed field never borrows another class.
Interpolation methods are candidates, not claims of validated accuracy.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import urllib.error
import urllib.request

try:
    from .spatial import MissingRoadData, ROAD_CLASSES, SpeedField, project
except ImportError:
    from spatial import MissingRoadData, ROAD_CLASSES, SpeedField, project


class SpatialDataError(ValueError):
    """Missing or incompatible data prevents a defensible estimate."""


class RoadClassError(SpatialDataError):
    pass


class RouteAlignmentError(SpatialDataError):
    pass


# Existing measured weekday congestion multipliers, relative to Tuesday.
DAY_MULT = {0: (.98, .96), 1: (1., 1.), 2: (.94, 1.03),
            3: (.95, 1.02), 4: (.95, 1.20), 5: (.60, .79), 6: (.36, .74)}


def _day_multiplier(day, hour):
    morning, evening = DAY_MULT[day]
    points = [(0, 1.), (8, morning), (17.5, evening), (24, 1.)]
    for (h0, v0), (h1, v1) in zip(points, points[1:]):
        if h0 <= hour < h1:
            return v0 + (v1 - v0) * (hour - h0) / (h1 - h0)
    return 1.


def _finite(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _slot_hour(slot):
    if not isinstance(slot, str) or len(slot) != 4 or not slot.isdigit():
        raise SpatialDataError(f"Invalid sample slot {slot!r}; use HHMM")
    hour, minute = int(slot[:2]), int(slot[2:])
    if hour >= 24 or minute >= 60:
        raise SpatialDataError(f"Invalid sample slot {slot!r}; use HHMM")
    return hour + minute / 60


class SpatialEstimator:
    def __init__(self, root=None, sample_path=None, index_path=None,
                 samples=None, route_provider=None):
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.sample_path = Path(sample_path) if sample_path else self.root / "data/spatial_samples.jsonl"
        self.index_path = Path(index_path) if index_path else self.root / "osrm/data/road_classes.sqlite"
        self.route_provider = route_provider or self._osrm_route
        self._injected = samples is not None
        self._sample_stamp = None
        self._fields = {}
        self._base_warnings = []
        self._rows = []
        self._lock = threading.RLock()
        if samples is not None:
            self._set_samples(list(samples), "injected")

    def _set_samples(self, rows, source):
        groups = defaultdict(list)
        for original in rows:
            row = dict(original)
            cls = row.get("road_class")
            if cls not in ROAD_CLASSES:
                raise SpatialDataError(f"Unknown sample road class {cls!r}")
            day = row.get("day", 1)
            if isinstance(day, bool) or not isinstance(day, int) or day not in DAY_MULT:
                raise SpatialDataError(f"Invalid sample day {day!r}; expected Monday=0 through Sunday=6")
            slot = _slot_hour(row.get("slot"))
            lat, lon = _finite(row.get("lat"), "sample latitude"), _finite(row.get("lon"), "sample longitude")
            row["xy"] = project(lat, lon)
            row["speed_kmh"] = _finite(row.get("speed_kmh"), "sample speed")
            if row["speed_kmh"] <= 0:
                raise SpatialDataError("Sample speeds must be positive")
            groups[(cls, day, slot)].append(row)
        if not groups:
            raise MissingRoadData("No spatial speed samples; crawl same-class observations first")
        self._rows = rows
        self._groups = dict(groups)
        self._fields = {}
        self._slots = defaultdict(list)
        for cls, day, slot in self._groups:
            self._slots[(cls, day)].append(slot)
        for slots in self._slots.values():
            slots.sort()
        self._source = source

    def _load_samples(self):
        if self._injected:
            return
        if self.sample_path.exists():
            stat = self.sample_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._sample_stamp:
                return
            try:
                rows = [json.loads(line) for line in self.sample_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except (OSError, ValueError) as exc:
                raise SpatialDataError(f"Cannot read spatial samples: {self.sample_path.name}") from exc
            self._set_samples(rows, "spatial_samples")
            self._sample_stamp = stamp
            self._base_warnings = []
            return
        if self._sample_stamp == "legacy":
            return
        try:
            try:
                from .compare_spatial import legacy_samples
            except ImportError:
                from compare_spatial import legacy_samples
            rows, _ = legacy_samples()
        except (OSError, ValueError, ImportError) as exc:
            raise MissingRoadData(f"Missing {self.sample_path.name}; crawl spatial speed samples first") from exc
        # Old positions were reconstructed from route proportions, not observed.
        for row in rows:
            row["lat"] = 21.05 + row["y"] / 111320
            row["lon"] = 105.85 + row["x"] / (111320 * math.cos(math.radians(21.05)))
            row["day"] = 1
        self._set_samples(rows, "legacy_exploratory")
        self._sample_stamp = "legacy"
        self._base_warnings = ["Exploratory legacy samples: approximate route-proportional locations and name-based classes; accuracy is unverified."]

    def _osrm_route(self, lat1, lon1, lat2, lon2):
        base = os.environ.get("OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
        url = (f"{base}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
               "?overview=full&geometries=geojson&steps=true&annotations=nodes,distance,duration")
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RouteAlignmentError("OSRM is unavailable; start the local OSRM server and retry") from exc

    def _index_connection(self):
        if not self.index_path.is_file():
            raise SpatialDataError("Missing OSM road-class index; run model/build_road_index.py")
        # A new read-only connection per estimate is safe across backend threads.
        conn = sqlite3.connect(self.index_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            metadata = [json.loads(row[0]) for row in conn.execute("SELECT value FROM metadata")]
            manifest = next(row for row in metadata if isinstance(row, dict) and row.get("pbf_sha256"))
            build = json.loads((self.root / "osrm/build.json").read_text(encoding="utf-8"))
            expected = build["input"]["pbf_sha256"]
            if expected != manifest["pbf_sha256"]:
                raise SpatialDataError("OSRM/index PBF fingerprint mismatch; rebuild the road-class index from the OSRM map")
            return conn, manifest
        except SpatialDataError:
            conn.close()
            raise
        except (OSError, ValueError, KeyError, StopIteration, sqlite3.Error) as exc:
            conn.close()
            raise SpatialDataError("Cannot verify OSRM/index PBF fingerprint; rebuild local routing data and road-class index") from exc

    @staticmethod
    def _route_edges(response):
        try:
            if "routes" in response:
                if response.get("code", "Ok") != "Ok" or not response["routes"]:
                    raise RouteAlignmentError("OSRM found no route between the selected endpoints")
                route = response["routes"][0]
            else:
                route = response
            geometry = route["geometry"]
            if geometry.get("type") != "LineString":
                raise RouteAlignmentError("OSRM route geometry must be a full GeoJSON LineString")
            coords = geometry["coordinates"]
            nodes, distances = [], []
            for leg in route["legs"]:
                annotation = leg["annotation"]
                leg_nodes = annotation["nodes"]
                leg_distances = annotation["distance"]
                if len(leg_nodes) != len(leg_distances) + 1:
                    raise RouteAlignmentError("OSRM annotation nodes/distance counts do not align")
                if nodes:
                    if nodes[-1] != leg_nodes[0]:
                        raise RouteAlignmentError("OSRM leg annotation endpoints do not align")
                    nodes.extend(leg_nodes[1:])
                else:
                    nodes.extend(leg_nodes)
                distances.extend(leg_distances)
            if len(coords) != len(nodes) or len(nodes) < 2:
                raise RouteAlignmentError("OSRM full geometry and annotation nodes do not align; exact edge classes cannot be assigned")
            for point in coords:
                if len(point) != 2:
                    raise RouteAlignmentError("Invalid OSRM coordinate")
                lon, lat = _finite(point[0], "OSRM longitude"), _finite(point[1], "OSRM latitude")
                project(lat, lon)
            distances = [_finite(d, "OSRM edge distance") for d in distances]
            if any(d < 0 for d in distances):
                raise RouteAlignmentError("OSRM returned negative edge distance")
            if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in nodes):
                raise RouteAlignmentError("OSRM returned invalid annotation node identifiers")
            return geometry, nodes, distances
        except RouteAlignmentError:
            raise
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RouteAlignmentError("Incomplete or invalid OSRM route annotations; request nodes,distance with full GeoJSON geometry") from exc

    def _field_speed(self, cls, day, slot, lat, lon, method):
        key = (method, cls, day, slot)
        if key not in self._fields:
            rows = self._groups[(cls, day, slot)]
            self._fields[key] = SpeedField([r["xy"] for r in rows], [r["speed_kmh"] for r in rows], method)
        return float(self._fields[key].predict([project(lat, lon)])[0])

    def _time_speed(self, cls, day, hour, lat, lon, method, warnings):
        source_day = day
        if (cls, source_day) not in self._slots:
            if (cls, 1) not in self._slots:
                raise MissingRoadData(f"No samples for road class {cls}, day {day}; crawl this class/day or a Tuesday baseline")
            source_day = 1
            warnings.add(f"Road class {cls}: reusing Tuesday samples with the legacy weekday congestion multiplier for day {day}.")
        slots = self._slots[(cls, source_day)]
        if len(slots) == 1:
            speed = self._field_speed(cls, source_day, slots[0], lat, lon, method)
            warnings.add(f"Road class {cls}, day {source_day}: only one observed time slot; speed held constant across hours.")
        else:
            right = bisect_right(slots, hour) % len(slots)
            left = (right - 1) % len(slots)
            low, high = slots[left], slots[right]
            span = (high - low) % 24
            fraction = ((hour - low) % 24) / span
            v0 = self._field_speed(cls, source_day, low, lat, lon, method)
            v1 = self._field_speed(cls, source_day, high, lat, lon, method)
            speed = v0 + fraction * (v1 - v0)
        if source_day != day:
            night_slots = [slot for slot in slots if slot < 6 or slot >= 23]
            if night_slots:
                night = self._field_speed(cls, source_day, min(night_slots), lat, lon, method)
                congestion = max(0., night / speed - 1)
                speed = night / (1 + congestion * _day_multiplier(day, hour))
            else:
                warnings.add(f"Road class {cls}: no night baseline; Tuesday speed reused without a weekday correction.")
        return speed

    def estimate(self, lat1, lon1, lat2, lon2, day, hour, method="idw1"):
        # Refreshing a sample file swaps all groups and field caches together.
        # Serialize on one estimator instance so concurrent HTTP requests never
        # see a mixture of two versions; SQLite connections remain local.
        with self._lock:
            return self._estimate(lat1, lon1, lat2, lon2, day, hour, method)

    def _estimate(self, lat1, lon1, lat2, lon2, day, hour, method):
        lat1, lon1, lat2, lon2 = [_finite(v, label) for v, label in [(lat1,"lat1"),(lon1,"lon1"),(lat2,"lat2"),(lon2,"lon2")]]
        project(lat1, lon1)
        project(lat2, lon2)
        if isinstance(day, bool) or not isinstance(day, int) or day not in DAY_MULT:
            raise ValueError("day must be an integer Monday=0 through Sunday=6")
        hour = _finite(hour, "hour")
        if not 0 <= hour < 24:
            raise ValueError("hour must be in [0, 24)")
        if method not in SpeedField.METHODS:
            raise ValueError(f"Unknown interpolation method: {method}")
        self._load_samples()
        geometry, nodes, distances = self._route_edges(self.route_provider(lat1, lon1, lat2, lon2))
        conn, manifest = self._index_connection()
        warnings = set(self._base_warnings)
        segments, by_class = [], defaultdict(lambda: {"km": 0., "minutes": 0.})
        total_minutes, total_m = 0., 0.
        edge_classes = {}
        try:
            for i, distance in enumerate(distances):
                if distance == 0:
                    continue
                pair = tuple(sorted((nodes[i], nodes[i + 1])))
                if pair not in edge_classes:
                    classes = {row[0] for row in conn.execute("SELECT DISTINCT road_class FROM edges WHERE a=? AND b=?", pair)}
                    if not classes:
                        raise RoadClassError(f"Unknown OSM edge {pair[0]}–{pair[1]}; rebuild the index or inspect the source OSM tags")
                    if len(classes) != 1:
                        raise RoadClassError(f"Ambiguous road classes for OSM edge {pair[0]}–{pair[1]}: {sorted(map(str, classes))}")
                    cls = next(iter(classes))
                    if cls not in ROAD_CLASSES:
                        raise RoadClassError(f"Unsupported road class {cls!r} for OSM edge {pair[0]}–{pair[1]}; inspect OSM tags")
                    edge_classes[pair] = cls
                cls = edge_classes[pair]
                start, end = geometry["coordinates"][i:i + 2]
                lon, lat = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
                arrival_hour = hour + total_minutes / 60
                edge_day = (day + int(arrival_hour // 24)) % 7
                speed = self._time_speed(cls, edge_day, arrival_hour % 24, lat, lon, method, warnings)
                minutes = distance / 1000 / speed * 60
                total_minutes += minutes
                total_m += distance
                by_class[cls]["km"] += distance / 1000
                by_class[cls]["minutes"] += minutes
                segments.append({"geometry": {"type": "LineString", "coordinates": [start, end]},
                                 "road_class": cls, "speed_kmh": speed, "distance_m": distance, "minutes": minutes})
        except sqlite3.Error as exc:
            raise SpatialDataError("Road-class index cannot be queried; rebuild model/build_road_index.py") from exc
        finally:
            conn.close()
        warnings.add("Spatial interpolation accuracy has not been independently validated; compare candidate formulas on held-out data.")
        return {"minutes": total_minutes, "km": total_m / 1000, "geometry": geometry,
                "segments": segments, "by_class": dict(by_class), "warnings": sorted(warnings),
                "method": method, "data_quality": {
                    "sample_source": self._source, "sample_count": len(self._rows),
                    "samples_by_class": dict(Counter(r["road_class"] for r in self._rows)),
                    "location_quality": sorted({r.get("location_quality", "unspecified") for r in self._rows}),
                    "sample_class_quality": sorted({r.get("class_quality", "unspecified") for r in self._rows}),
                    "route_class_quality": manifest.get("class_quality", "osm_tag_policy"),
                    "pbf_sha256": manifest["pbf_sha256"], "day_convention": "Monday=0, Sunday=6",
                    "time_policy": "cyclic linear interpolation of same-class/day spatial speed fields at edge entry time",
                    "validation_status": "unvalidated"}}
