"""Collect TomTom instruction means with topology-based OSM road classes.

Each speed is an instruction-interval mean, represented at the midpoint of
the actual TomTom sub-polyline. It is not a physical point-speed observation.
Mixed, unknown and ambiguous OSM intervals are excluded instead of assigning
one average speed to different road classes. Credentials are never cached.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from download.download_day import ROUTES

CLASSES = {"caotoc", "quoclo", "chinh", "noido", "nho"}
REQUEST_OPTIONS = {"travelMode": "car", "traffic": "false", "computeTravelTimeFor": "all",
                   "instructionsType": "text", "language": "en-GB"}
WARNINGS = [
    "Speeds are TomTom instruction-segment means, not independently measured physical point speeds.",
    "Locations are geometric midpoints of actual TomTom instruction sub-polylines.",
    "A sample is accepted only when every positive-length matched OSM edge has the same unambiguous road class.",
    "Time slots describe route departure time; instruction intervals can occur later along the trip.",
    "Road classes follow the project's explicit OSM-tag policy, not an official legal road classification.",
]


class CrawlError(ValueError):
    """A safe diagnostic which never contains credentials or a request URL."""


def read_api_key():
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


def _distance(first, second):
    """Haversine metres for latitude/longitude pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*first, *second))
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1., math.sqrt(value)))


def _cumulative(points):
    lengths = [0.]
    for first, second in zip(points, points[1:]):
        lengths.append(lengths[-1] + _distance(first, second))
    return lengths


def point_at_fraction(points, fraction):
    lengths = _cumulative(points)
    if not lengths or lengths[-1] <= 0:
        raise CrawlError("Degenerate route geometry")
    target = lengths[-1] * fraction
    right = min(max(1, bisect_left(lengths, target)), len(points) - 1)
    fraction = (target - lengths[right - 1]) / max(1e-12, lengths[right] - lengths[right - 1])
    return [points[right - 1][i] + fraction * (points[right][i] - points[right - 1][i]) for i in range(2)]


def select_trace_indices(points, mandatory, max_points=100):
    """Keep all instruction boundaries and fill by geometric arclength."""
    if len(points) < 2:
        raise CrawlError("Too few geometry points")
    selected = set(mandatory) | {0, len(points) - 1}
    if any(type(index) is not int or index < 0 or index >= len(points) for index in selected):
        raise CrawlError("Invalid instruction pointIndex")
    if len(selected) > max_points:
        raise CrawlError("More than 100 mandatory instruction points; snapshot explicitly excluded")
    if len(points) <= max_points:
        return list(range(len(points)))
    lengths = _cumulative(points)
    # A dense uniform grid supplies extra bend support without dropping anchors.
    for i in range(1, max_points * 4):
        if len(selected) >= max_points:
            break
        target = lengths[-1] * i / (max_points * 4)
        index = min(len(points) - 1, bisect_left(lengths, target))
        selected.add(index)
    # Fill gaps with their arclength midpoint, so extras cover the whole route.
    while len(selected) < max_points:
        ordered = sorted(selected)
        candidates = [(lengths[b] - lengths[a], a, b) for a, b in zip(ordered, ordered[1:]) if b - a > 1]
        if not candidates:
            break
        _, left, right = max(candidates)
        index = min(right - 1, max(left + 1, bisect_left(lengths, (lengths[left] + lengths[right]) / 2)))
        selected.add(index)
    return sorted(selected)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise CrawlError("Redirect refused")


def fetch_tomtom(origin, destination, request, key):
    coordinates = ":".join(",".join(f"{number:.7f}" for number in point) for point in (origin, destination))
    query = urlencode({**request, "key": key})
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{coordinates}/json?{query}"
    try:
        with build_opener(NoRedirect()).open(Request(url), timeout=45) as response:
            result = json.load(response)
        if not result.get("routes"):
            raise CrawlError("TomTom returned no usable route")
        return result
    except HTTPError as exc:
        raise CrawlError(f"TomTom HTTP {exc.code}; request not cached") from None
    except CrawlError:
        raise
    except Exception:
        raise CrawlError("TomTom connection or response failed; request not cached") from None


def match_osrm(points):
    coordinates = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in points)
    query = urlencode({"annotations": "nodes,distance", "geometries": "geojson", "overview": "full",
                       "gaps": "ignore", "tidy": "false", "steps": "false",
                       "radiuses": ";".join("20" for _ in points)})
    try:
        with urlopen(f"http://127.0.0.1:5000/match/v1/driving/{coordinates}?{query}", timeout=45) as response:
            return json.load(response)
    except Exception:
        raise CrawlError("Local OSRM map matching unavailable") from None


def _read_geometry(snapshot):
    route = snapshot["response"]["routes"][0]
    if len(route["legs"]) != 1:
        raise CrawlError("Multi-leg TomTom geometry requires explicit index handling")
    points = [[float(point["latitude"]), float(point["longitude"])] for point in route["legs"][0]["points"]]
    if any(not all(math.isfinite(v) for v in point) or not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180) for point in points):
        raise CrawlError("Invalid TomTom coordinates")
    instructions = route["guidance"]["instructions"]
    if len(instructions) < 2:
        raise CrawlError("No instruction intervals")
    return points, instructions


def extract_snapshot(snapshot, route_id, snapshot_name, match_provider, class_lookup):
    """Return valid same-class instruction means and explicit exclusion counts.

    Injection: match_provider(list[[lat,lon]]) returns OSRM /match JSON;
    class_lookup(a,b) returns the set of classes for sorted OSM node IDs.
    """
    excluded = Counter()
    try:
        points, instructions = _read_geometry(snapshot)
        indices = select_trace_indices(points, [instruction["pointIndex"] for instruction in instructions])
        departure = datetime.fromisoformat(snapshot["request"]["departAt"])
        slot = departure.strftime("%H%M")
    except (KeyError, IndexError, TypeError, ValueError):
        return [], Counter({"invalid_snapshot_or_too_many_instruction_points": 1})
    try:
        match = match_provider([points[index] for index in indices])
        if match.get("code") != "Ok" or len(match["tracepoints"]) != len(indices):
            return [], Counter({"map_match_failed": len(instructions) - 1})
    except Exception:
        return [], Counter({"map_match_failed": len(instructions) - 1})
    index_positions = {index: position for position, index in enumerate(indices)}
    rows = []
    for number, (first, second) in enumerate(zip(instructions, instructions[1:])):
        try:
            start, end = first["pointIndex"], second["pointIndex"]
            metres = float(second["routeOffsetInMeters"]) - float(first["routeOffsetInMeters"])
            seconds = float(second["travelTimeInSeconds"]) - float(first["travelTimeInSeconds"])
            if end <= start or not all(math.isfinite(value) and value > 0 for value in (metres, seconds)):
                excluded["nonpositive_instruction_interval"] += 1
                continue
            for instruction in (first, second):
                if "point" in instruction:
                    actual = instruction["point"]
                    if _distance(points[instruction["pointIndex"]], [actual["latitude"], actual["longitude"]]) > 30:
                        raise CrawlError("Instruction pointIndex disagrees with point coordinate")
            trace = match["tracepoints"][index_positions[start]:index_positions[end] + 1]
            if any(point is None for point in trace) or len({point["matchings_index"] for point in trace}) != 1:
                excluded["unmatched_instruction_interval"] += 1
                continue
            matching_index = trace[0]["matchings_index"]
            matching = match["matchings"][matching_index]
            waypoints = [point["waypoint_index"] for point in trace]
            if any(type(index) is not int or index < 0 for index in waypoints) or any(a >= b for a, b in zip(waypoints, waypoints[1:])):
                excluded["unmatched_instruction_interval"] += 1
                continue
            if any(_distance(points[indices[index_positions[start] + i]], [point["location"][1], point["location"][0]]) > 40 for i, point in enumerate(trace)):
                excluded["excessive_snap_distance"] += 1
                continue
            legs = matching["legs"][waypoints[0]:waypoints[-1]]
            if len(legs) != waypoints[-1] - waypoints[0] or not legs:
                excluded["annotation_mismatch"] += 1
                continue
            interval_classes, matched_metres, reason = set(), 0., None
            for leg in legs:
                annotation = leg["annotation"]
                nodes, distances = annotation["nodes"], annotation["distance"]
                if len(nodes) != len(distances) + 1:
                    reason = "annotation_mismatch"
                    break
                for a, b, distance in zip(nodes, nodes[1:], distances):
                    distance = float(distance)
                    if not math.isfinite(distance) or distance < 0:
                        reason = "annotation_mismatch"
                        break
                    if distance == 0:
                        continue
                    if type(a) is not int or type(b) is not int or a <= 0 or b <= 0:
                        reason = "unknown_road_edge"
                        break
                    classes = set(class_lookup(*sorted((a, b))))
                    if not classes or not classes.issubset(CLASSES):
                        reason = "unknown_road_edge"
                        break
                    if len(classes) != 1:
                        reason = "ambiguous_road_edge"
                        break
                    interval_classes.update(classes)
                    matched_metres += distance
                if reason:
                    break
            if reason:
                excluded[reason] += 1
                continue
            if len(interval_classes) != 1:
                excluded["mixed_road_classes" if interval_classes else "empty_matched_interval"] += 1
                continue
            if abs(matched_metres - metres) > max(30, .25 * metres):
                excluded["matched_length_mismatch"] += 1
                continue
            lat, lon = point_at_fraction(points[start:end + 1], .5)
            rows.append({"lat": lat, "lon": lon, "road_class": next(iter(interval_classes)),
                         "day": departure.weekday(), "slot": slot, "speed_kmh": metres / seconds * 3.6,
                         "m": metres, "s": seconds, "street": first.get("street", ""),
                         "route": snapshot.get("route", route_id), "snapshot": snapshot_name,
                         "instruction_interval": number, "start_point_index": start, "end_point_index": end,
                         "matched_m": matched_metres, "location_quality": "tomtom_instruction_geometry",
                         "class_quality": "osm_map_match_node_pairs", "speed_quality": "tomtom_instruction_segment_mean",
                         "time_quality": "route_departure_slot", "fetched_at": snapshot.get("fetched_at"),
                         "map_match_confidence": matching.get("confidence"), "warnings": WARNINGS})
        except (KeyError, IndexError, TypeError, ValueError, sqlite3.Error):
            excluded["invalid_instruction_or_annotation"] += 1
    return rows, excluded


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def _open_index(root):
    path = root / "osrm/data/road_classes.sqlite"
    if not path.is_file():
        raise CrawlError("Missing OSM road-class index; build it before collecting spatial samples")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        manifest = json.loads(connection.execute("SELECT value FROM metadata").fetchone()[0])
        build = json.loads((root / "osrm/build.json").read_text(encoding="utf-8"))
        if manifest["pbf_sha256"] != build["input"]["pbf_sha256"]:
            raise CrawlError("OSRM and road-class index use different OSM map fingerprints")
    except Exception:
        connection.close()
        raise CrawlError("Cannot verify OSM road-class index and OSRM map fingerprint") from None
    cache = {}
    def lookup(a, b):
        pair = (a, b)
        if pair not in cache:
            cache[pair] = {row[0] for row in connection.execute("SELECT DISTINCT road_class FROM edges WHERE a=? AND b=?", pair)}
        return cache[pair]
    return connection, lookup, manifest


def rebuild_samples(root=None, match_provider=None, class_lookup=None):
    """Atomically regenerate samples from every cached full TomTom snapshot."""
    root = Path(root or ROOT)
    connection, manifest = None, {}
    if class_lookup is None:
        connection, class_lookup, manifest = _open_index(root)
    match_provider = match_provider or match_osrm
    rows, excluded, snapshots = [], Counter(), 0
    try:
        for path in sorted((root / "data/api_raw").glob("*.json")):
            try:
                snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
                route_id = path.stem.rsplit("_", 2)[0]
                found, rejected = extract_snapshot(snapshot, route_id, path.name, match_provider, class_lookup)
                for row in found:
                    row["pbf_sha256"] = manifest.get("pbf_sha256", "injected_test_index")
                rows.extend(found)
                excluded.update(rejected)
                snapshots += 1
            except (OSError, ValueError):
                excluded["unreadable_snapshot"] += 1
    finally:
        if connection:
            connection.close()
    # On complete match failure preserve the previous useful dataset.
    if not rows and (root / "data/spatial_samples.jsonl").is_file():
        raise CrawlError("No valid same-class intervals recovered; existing samples preserved")
    _atomic_text(root / "data/spatial_samples.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    summary = {"sample_count": len(rows), "by_class": dict(Counter(row["road_class"] for row in rows)),
               "snapshots": snapshots, "excluded": dict(excluded), "warnings": WARNINGS,
               "pbf_sha256": manifest.get("pbf_sha256"), "updated_at": datetime.now(timezone.utc).isoformat()}
    _atomic_text(root / "reports/spatial_crawl.json", json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def _all_routes(root):
    routes = list(ROUTES)
    path = root / "data/geometries_full.json"
    if path.is_file():
        geometry = json.loads(path.read_text(encoding="utf-8"))
        points = geometry.get("EXP_hn_hp", {}).get("poly", [])
        if len(points) > 1:
            for index, (start, end) in enumerate([(.20, .26), (.45, .51), (.70, .76)], 1):
                origin, destination = point_at_fraction(points, start), point_at_fraction(points, end)
                routes.append((f"EXP_probe_{index}", ",".join(map(str, origin)), ",".join(map(str, destination))))
    return routes


def _same_snapshot(snapshot, origin, destination, request):
    try:
        return (snapshot["request"] == request and bool(snapshot["response"]["routes"])
                and all(abs(float(a) - float(b)) < 1e-6 for a, b in zip(snapshot["origin"], origin))
                and all(abs(float(a) - float(b)) < 1e-6 for a, b in zip(snapshot["destination"], destination)))
    except (KeyError, TypeError, ValueError):
        return False


def crawl(root, day_date, slots, max_calls, *, fetcher=None, match_provider=None, class_lookup=None, routes=None, key_reader=None):
    root = Path(root)
    try:
        day = date.fromisoformat(day_date)
    except ValueError:
        raise CrawlError("Date must be YYYY-MM-DD") from None
    if type(max_calls) is not int or not 1 <= max_calls <= 60:
        raise CrawlError("max_calls must be between 1 and 60")
    if not slots or any(not isinstance(slot, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3])[0-5][0-9]", slot) for slot in slots):
        raise CrawlError("Slots must be HHMM between 0000 and 2359")
    slots = list(dict.fromkeys(slots))
    fetcher, key_reader = fetcher or fetch_tomtom, key_reader or read_api_key
    raw = root / "data/api_raw"
    raw.mkdir(parents=True, exist_ok=True)
    calls, cached, failures, key = 0, 0, Counter(), None
    for route_id, start, end in (routes if routes is not None else _all_routes(root)):
        origin, destination = [float(x) for x in start.split(",")], [float(x) for x in end.split(",")]
        prefix = route_id.split("_", 1)[0] if re.match(r"^R\d{2}_", route_id) else route_id
        for slot in slots:
            request = {**REQUEST_OPTIONS, "departAt": f"{day.isoformat()}T{slot[:2]}:{slot[2:]}:00+07:00"}
            path = raw / f"{prefix}_{day:%Y%m%d}_{slot}.json"
            if path.is_file():
                try:
                    previous = json.loads(path.read_text(encoding="utf-8-sig"))
                    if _same_snapshot(previous, origin, destination, request):
                        cached += 1
                        continue
                except (OSError, ValueError):
                    pass
                # Preserve old snapshots if request options or endpoints change.
                fingerprint = hashlib.sha256(json.dumps([origin, destination, request], sort_keys=True).encode()).hexdigest()[:10]
                path = path.with_name(path.stem + "_" + fingerprint + path.suffix)
                if path.is_file():
                    try:
                        if _same_snapshot(json.loads(path.read_text(encoding="utf-8-sig")), origin, destination, request):
                            cached += 1
                            continue
                    except (OSError, ValueError):
                        pass
                    failures["existing_cache_conflict"] += 1
                    continue
            if calls >= max_calls:
                continue
            key = key or key_reader()
            if not key:
                raise CrawlError("TOMTOM_API_KEY is not configured on the server")
            calls += 1
            try:
                response = fetcher(origin, destination, request, key)
                if not response.get("routes"):
                    failures["no_route"] += 1
                    continue
                snapshot = {"route": route_id, "request": request, "origin": origin, "destination": destination,
                            "fetched_at": datetime.now(timezone.utc).isoformat(), "response": response}
                text = json.dumps(snapshot, ensure_ascii=False).replace(key, "[redacted]")
                _atomic_text(path, text)
            except Exception:
                # Never interpolate exception text: urllib exceptions contain URLs.
                failures["request_failed"] += 1
            if fetcher is fetch_tomtom:
                time.sleep(.25)
    summary = rebuild_samples(root=root, match_provider=match_provider, class_lookup=class_lookup)
    summary.update(new_requests=calls, cached_requests=cached, request_failures=dict(failures))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--slots", nargs="+", default=["0800", "1730", "0300"])
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args()
    try:
        result = crawl(ROOT, args.date, args.slots, args.max_calls)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["sample_count"] else 1
    except Exception as exc:
        print(str(exc) if isinstance(exc, CrawlError) else "Crawl failed; cached snapshots preserved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
