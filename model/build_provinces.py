"""Build Vietnam's province boundaries (admin_level=4, ISO3166-2 VN-*) from the OSM file.

Writes vietnam_provinces.geojson: a FeatureCollection of MultiPolygons (inner rings as holes), simplified for
display, with the map's pbf_sha256 and the big-road policy version at the top level. Provinces without a closed
outer ring are left out and listed under "failed"; those kept without some open chains are listed under "partial".
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

import osmium

from build_big_roads import METRES_PER_DEGREE, _located, _write_json_atomic, inside, merge_chains, simplify
from osm_classes import BIG_ROAD_POLICY_VERSION

ROOT = Path(__file__).resolve().parents[1]


def province_relations(pbf):
    """Relation-only pass: [{relation_id, iso, name, members: [(way_id, role)]}] for VN-* admin_level=4, by iso."""
    found = []
    for relation in osmium.FileProcessor(str(pbf), osmium.osm.RELATION):
        tags = relation.tags
        if tags.get("boundary") == "administrative" and tags.get("admin_level") == "4" and tags.get("ISO3166-2", "").startswith("VN-"):
            found.append({"relation_id": relation.id, "iso": tags.get("ISO3166-2"), "name": tags.get("name", ""),
                          "members": [(m.ref, m.role or "outer") for m in relation.members if m.type == "w"]})
    return sorted(found, key=lambda item: item["iso"])


class MemberWays(osmium.SimpleHandler):
    """Located node lists of the given ways."""

    def __init__(self, way_ids):
        super().__init__()
        self.way_ids, self.ways = set(way_ids), {}

    def way(self, way):
        if way.id in self.way_ids:
            self.ways[way.id] = _located(way)


def _signed_area_km2(ring):
    scale = math.cos(math.radians(sum(lat for _, lat in ring) / len(ring))) * (METRES_PER_DEGREE / 1000) ** 2
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2 * scale


def _rings(ways, members, role):
    """(closed rings as (lon, lat) lists, open chain count) for the members with this role."""
    chains = merge_chains([ways[way_id] for way_id, r in members if r == role and way_id in ways], key=lambda point: point[0])
    closed = [[loc for _, loc in chain] for chain in chains if len(chain) > 3 and chain[0][0] == chain[-1][0]]
    return closed, len(chains) - len(closed)


def assemble_province(ways, members, tolerance_m):
    """(polygons [[outer, *holes]] or None, reason, note). Outer rings run counter-clockwise, holes clockwise.

    Open chains are dropped as long as one outer ring closes (the extract can lack far-off island ways); the note
    then says what was dropped, otherwise it is None.
    """
    missing = sum(1 for way_id, _ in members if way_id not in ways)
    outers, open_outer = _rings(ways, members, "outer")
    inners, open_inner = _rings(ways, members, "inner")
    detail = f"{open_outer} open outer and {open_inner} open inner chains ({missing} member ways missing from the file)"
    if not outers:
        return None, f"failed: no closed outer ring; {detail}", None
    note = f"dropped {detail}" if open_outer or open_inner else None
    outers = [ring for ring in (simplify(ring, [], tolerance_m) for ring in outers) if len(ring) >= 4]
    inners = [ring for ring in (simplify(ring, [], tolerance_m) for ring in inners) if len(ring) >= 4]
    if not outers:
        return None, "failed: outer rings collapse when simplified", None
    polygons = [[ring if _signed_area_km2(ring) > 0 else ring[::-1]] for ring in outers]
    for ring in inners:
        # A hole belongs to the smallest outer ring holding its first vertex.
        holders = [i for i, polygon in enumerate(polygons) if inside(polygon[0], [ring[0]])[0]]
        if not holders:
            return None, "failed: inner ring outside every outer ring", None
        best = min(holders, key=lambda i: _signed_area_km2(polygons[i][0]))
        polygons[best].append(ring if _signed_area_km2(ring) < 0 else ring[::-1])
    return polygons, "ok", note


def build(pbf, out, tolerance_m=100.0):
    pbf, out = Path(pbf), Path(out)
    if not pbf.is_file():
        raise FileNotFoundError(f"Missing OSM file {pbf}; run python osrm/manage.py setup first")
    relations = province_relations(pbf)
    handler = MemberWays(way_id for item in relations for way_id, _ in item["members"])
    handler.apply_file(str(pbf), locations=True, idx="flex_mem")
    features, failed, partial = [], [], []
    for item in relations:
        polygons, reason, note = assemble_province(handler.ways, item["members"], tolerance_m)
        if polygons is None:
            failed.append({"iso": item["iso"], "name": item["name"], "reason": reason})
            continue
        if note:
            partial.append({"iso": item["iso"], "name": item["name"], "reason": note})
        area = sum(_signed_area_km2(ring) for polygon in polygons for ring in polygon)
        coords = [[[[round(lon, 6), round(lat, 6)] for lon, lat in ring] for ring in polygon] for polygon in polygons]
        features.append({"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": coords},
                         "properties": {"iso": item["iso"], "name": item["name"], "relation_id": item["relation_id"],
                                        "area_km2": round(area, 1), "points": sum(len(r) for p in coords for r in p)}})
    if not features:
        raise ValueError(f"No VN- province could be assembled ({len(relations)} relations, {len(failed)} failed)")
    with pbf.open("rb") as source:
        sha = hashlib.file_digest(source, "sha256").hexdigest()
    result = {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": BIG_ROAD_POLICY_VERSION,
              "failed": failed, "partial": partial, "features": features}
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(out, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=ROOT / "osrm/data/vietnam-latest.osm.pbf")
    parser.add_argument("--out", type=Path, default=ROOT / "osrm/data/vietnam_provinces.geojson")
    parser.add_argument("--tolerance-m", type=float, default=100.0)
    args = parser.parse_args()
    if args.tolerance_m < 0:
        parser.error("Tolerance must be non-negative")
    result = build(args.pbf, args.out, args.tolerance_m)
    print(json.dumps({"provinces": len(result["features"]), "points": sum(f["properties"]["points"] for f in result["features"]),
                      "failed": result["failed"], "partial": result["partial"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
