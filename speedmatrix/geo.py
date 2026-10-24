"""Geometry helpers: haversine distance, OSRM's segment length and point-in-polygon tests."""
import numpy as np

EARTH_RADIUS_M = 6371008.8
# OSRM v26 util::coordinate_calculation::greatCircleDistance: mapbox cheap ruler (WGS84) with 1800 rulers, one per
# 0.05 degree latitude band (src/util/coordinate_calculation.cpp, include/util/cheap_ruler.hpp).
_WGS84_RE_KM = 6378.137
_WGS84_FE = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_FE * (2 - _WGS84_FE)
_RULER_STEP_E6 = 50_000                  # int(90.0 * 1e6 / 1800)


def haversine_m(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Great-circle distance in metres (vectorized, degrees in)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2) - np.radians(lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def _in_ring(lat: np.ndarray, lon: np.ndarray, ring) -> np.ndarray:
    """Even-odd ray casting against one ring of [lon, lat] vertices."""
    inside = np.zeros(lat.shape, dtype=bool)
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if y1 == y2:
            continue
        crosses = (y1 > lat) != (y2 > lat)
        x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (lon < x_at)
    return inside


def points_in_geometry(lat: np.ndarray, lon: np.ndarray, geometry: dict) -> np.ndarray:
    """Boolean mask of points inside a GeoJSON Polygon or MultiPolygon.

    All rings of a polygon count (even-odd, so holes work); MultiPolygon
    parts are OR-ed.
    """
    kind = geometry.get("type")
    if kind == "Polygon":
        polygons = [geometry["coordinates"]]
    elif kind == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValueError(f"unsupported geometry type {kind!r}")
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    result = np.zeros(lat.shape, dtype=bool)
    for rings in polygons:
        poly = np.zeros(lat.shape, dtype=bool)
        for ring in rings:
            poly ^= _in_ring(lat, lon, ring)
        result |= poly
    return result


def osrm_length_m(lon1_e6, lat1_e6, lon2_e6, lat2_e6) -> np.ndarray:
    """Metres between fixed-point (1e-6 degree, int) coordinates exactly as OSRM v26 measures a segment: the cheap
    ruler of the 0.05 degree band holding the truncated mean latitude, then sqrt(dx^2 + dy^2)."""
    lat1 = np.asarray(lat1_e6, dtype=np.int64)
    lat2 = np.asarray(lat2_e6, dtype=np.int64)
    total = lat1 + lat2
    mean = np.sign(total) * (np.abs(total) // 2)                 # C++ integer division truncates toward zero
    shifted = np.abs(mean) - 1
    band = np.where(shifted < 0, 0, shifted // _RULER_STEP_E6)   # (|lat| - 1) / step, truncated
    ruler_lat = _RULER_STEP_E6 * (band + 0.5) / 1e6
    rad = np.pi / 180.0
    mul = rad * _WGS84_RE_KM * 1000
    coslat = np.cos(ruler_lat * rad)
    w2 = 1 / (1 - _WGS84_E2 * (1 - coslat * coslat))
    w = np.sqrt(w2)
    kx = mul * w * coslat
    ky = mul * w * w2 * (1 - _WGS84_E2)
    dlon = np.asarray(lon1_e6, dtype=np.int64) / 1e6 - np.asarray(lon2_e6, dtype=np.int64) / 1e6
    dlon = dlon - 360.0 * np.round(dlon / 360.0)                 # std::remainder(a - b, 360)
    dx = dlon * kx
    dy = (lat1 / 1e6 - lat2 / 1e6) * ky
    return np.sqrt(dx * dx + dy * dy)
