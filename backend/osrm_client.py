"""Calls to the local OSRM server and parsing of its route answers.

Spec:
- ``osrm_base()`` is ``OSRM_BASE_URL`` (default ``http://127.0.0.1:5000``) without a trailing slash;
  every OSRM URL in the backend goes through it.
- ``probe_osrm()`` -> bool, never raises.
- ``osrm_big_route()`` returns the parsed ``/route`` JSON; an HTTP 400 (NoRoute) body is returned, not raised.
- ``route_pairs(response)`` -> (coords, nodes, distances, durations) of the first route, coords aligned with
  nodes; raises ``ValueError`` on anything incomplete or misaligned.
"""
from __future__ import annotations

import json
import math
import os
from urllib.error import HTTPError
from urllib.request import urlopen


def osrm_base():
    return os.environ.get("OSRM_BASE_URL", "http://127.0.0.1:5000").rstrip("/")


def probe_osrm():
    try:
        with urlopen(osrm_base() + "/nearest/v1/driving/105.85,21.03?number=1", timeout=1.5) as response:
            return json.load(response).get("code") == "Ok"
    except Exception:
        return False


def osrm_big_route(lat1, lon1, lat2, lon2):
    """OSRM /route with per-node annotations; NoRoute answers (HTTP 400) are returned, not raised."""
    url = (f"{osrm_base()}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
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
