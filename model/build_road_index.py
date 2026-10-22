"""Index exact adjacent OSM node pairs, for matching OSRM route annotations."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import osmium

from osm_classes import classify_osm, POLICY_VERSION

ROOT = Path(__file__).resolve().parents[1]


class RegionNodes(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.ids = set()

    def node(self, node):
        west, south, east, north = self.bbox
        if west <= node.location.lon <= east and south <= node.location.lat <= north:
            self.ids.add(node.id)


class RoadEdges(osmium.SimpleHandler):
    def __init__(self, node_ids, database):
        super().__init__()
        self.ids = node_ids
        self.database = database
        self.batch = []
        self.counts = Counter()

    def flush(self):
        self.database.executemany("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?,?,?)", self.batch)
        self.database.commit()
        self.batch.clear()

    def way(self, way):
        highway = way.tags.get("highway", "")
        if not highway:
            return
        nodes = [node.ref for node in way.nodes]
        if not any(node in self.ids for node in nodes):
            return
        name, ref = way.tags.get("name", ""), way.tags.get("ref", "")
        cls = classify_osm(highway, ref, name, way.tags.get("service", ""))
        for first, second in zip(nodes, nodes[1:]):
            a, b = sorted((first, second))
            self.batch.append((a, b, way.id, cls, highway, name, ref))
            self.counts[cls] += 1
        if len(self.batch) >= 10000:
            self.flush()


def build(pbf, target, bbox):
    if not pbf.exists():
        raise FileNotFoundError("Run python osrm/manage.py setup first")
    print("Collecting nodes in region", bbox, flush=True)
    region = RegionNodes(bbox)
    region.apply_file(str(pbf))
    print("Regional nodes:", len(region.ids), flush=True)
    if not region.ids:
        raise ValueError("No nodes in bounding box")
    temp = target.with_suffix(".building.sqlite")
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(temp)) as db:
        db.execute("DROP TABLE IF EXISTS edges")
        db.execute("DROP TABLE IF EXISTS metadata")
        db.execute("CREATE TABLE edges (a INTEGER, b INTEGER, way_id INTEGER, road_class TEXT, highway TEXT, name TEXT, ref TEXT, PRIMARY KEY(a,b,way_id)) WITHOUT ROWID")
        handler = RoadEdges(region.ids, db)
        print("Indexing tagged road edges", flush=True)
        handler.apply_file(str(pbf))
        handler.flush()
        with pbf.open("rb") as source:
            sha = hashlib.file_digest(source, "sha256").hexdigest()
        metadata = {"pbf_sha256": sha, "policy_version": POLICY_VERSION, "bbox": bbox,
                    "edge_counts": dict(handler.counts), "class_quality": "osm_tag_policy"}
        db.execute("CREATE TABLE metadata (value TEXT)")
        db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata),))
        db.commit()
    temp.replace(target)
    print("Index ready:", target, "counts:", dict(handler.counts), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pbf", type=Path, default=ROOT / "osrm/data/vietnam-latest.osm.pbf")
    parser.add_argument("--output", type=Path, default=ROOT / "osrm/data/road_classes.sqlite")
    parser.add_argument("--bbox", type=float, nargs=4, default=[105.3, 20.5, 107.1, 21.8],
                        metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    args = parser.parse_args()
    west, south, east, north = args.bbox
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        parser.error("Invalid bounding box")
    build(args.pbf, args.output, args.bbox)


if __name__ == "__main__":
    main()
