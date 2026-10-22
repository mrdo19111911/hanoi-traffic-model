"""Spatial speed interpolation, isolated by road class, day and time slot.

Coordinates passed to SpeedField are planar metres. RoadSpeedModel projects
latitude/longitude to a local metric plane around Hanoi before interpolation.
Missing groups raise an error instead of borrowing another road class.
"""
from collections import defaultdict
import math

import numpy as np
from scipy.spatial import Delaunay, QhullError

ROAD_CLASSES = ("caotoc", "quoclo", "chinh", "noido", "nho")


class MissingRoadData(ValueError):
    pass


def project(lat, lon):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid latitude/longitude")
    return ((lon - 105.85) * 111320 * math.cos(math.radians(21.05)),
            (lat - 21.05) * 111320)


class SpeedField:
    METHODS = ("mean", "nearest", "idw1", "idw2", "linear_idw1", "linear_idw2")

    def __init__(self, points, values, method="idw1"):
        if method not in self.METHODS:
            raise ValueError(f"Unknown method: {method}")
        points = np.asarray(points, dtype=float)
        values = np.asarray(values, dtype=float)
        if (points.ndim != 2 or points.shape[1] != 2 or len(points) == 0
                or values.shape != (len(points),) or not np.isfinite(points).all()
                or not np.isfinite(values).all() or (values <= 0).any()):
            raise ValueError("Expected finite 2D points and positive finite speeds")
        # Co-located observations represent one spatial support point.
        self.points, inverse = np.unique(points, axis=0, return_inverse=True)
        self.values = np.bincount(inverse, weights=values) / np.bincount(inverse)
        self.method = method
        self.triangulation = None
        if (method.startswith("linear_") and len(self.points) >= 3
                and np.linalg.matrix_rank(self.points - self.points[0]) == 2):
            try:
                self.triangulation = Delaunay(self.points)
            except QhullError:
                # Degenerate geometry still has a well-defined IDW fallback.
                pass

    def _distance_prediction(self, query):
        distances = np.linalg.norm(query[:, None, :] - self.points[None, :, :], axis=2)
        nearest = distances.argmin(axis=1)
        if self.method == "nearest":
            return self.values[nearest].copy()
        exact = distances[np.arange(len(query)), nearest] <= 1e-7
        result = self.values[nearest].copy()
        if (~exact).any():
            d = distances[~exact]
            power = 2 if self.method.endswith("2") else 1
            weights = (d.min(axis=1, keepdims=True) / d) ** power
            result[~exact] = (weights @ self.values) / weights.sum(axis=1)
        return result

    def predict(self, queries):
        query = np.asarray(queries, dtype=float)
        if query.size == 0:
            return np.empty(0)
        if query.ndim != 2 or query.shape[1] != 2 or not np.isfinite(query).all():
            raise ValueError("Expected finite query points with shape (n, 2)")
        if len(self.points) == 1:
            return np.full(len(query), self.values[0])
        if self.method == "mean":
            return np.full(len(query), self.values.mean())
        result = np.empty(len(query))
        for start in range(0, len(query), 256):
            batch = query[start:start + 256]
            result[start:start + len(batch)] = self._distance_prediction(batch)
        if self.triangulation is not None:
            tri = self.triangulation
            simplex = tri.find_simplex(query, tol=1e-10)
            inside = simplex >= 0
            selected = simplex[inside]
            transforms = tri.transform[selected]
            uv = np.einsum("nij,nj->ni", transforms[:, :2, :], query[inside] - transforms[:, 2, :])
            weights = np.column_stack((uv, 1 - uv.sum(axis=1)))
            weights = np.maximum(weights, 0)
            weights /= weights.sum(axis=1, keepdims=True)
            result[inside] = (self.values[tri.simplices[selected]] * weights).sum(axis=1)
        return result


class RoadSpeedModel:
    def __init__(self, samples, method="idw1"):
        groups = defaultdict(list)
        for sample in samples:
            road_class = sample["road_class"]
            if road_class not in ROAD_CLASSES:
                raise ValueError(f"Unknown road class: {road_class}")
            groups[(road_class, sample.get("day"), sample["slot"])].append(sample)
        self.fields = {}
        for key, rows in groups.items():
            self.fields[key] = SpeedField(
                [project(row["lat"], row["lon"]) for row in rows],
                [row["speed_kmh"] for row in rows], method)

    def speed(self, lat, lon, road_class, slot, day=None):
        key = (road_class, day, slot)
        if key not in self.fields:
            raise MissingRoadData(f"No samples for road class/day/slot {key}")
        return float(self.fields[key].predict([project(lat, lon)])[0])
