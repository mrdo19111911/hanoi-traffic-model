"""Geometry helpers: haversine distance and point-in-polygon tests."""
import numpy as np

EARTH_RADIUS_M = 6371008.8


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
