"""Segment table from the OSM PBF: one row per adjacent node pair of a kept way.

The table is the segment identity used by every later speedmatrix step. It is
built with array buffers (no per-segment Python objects) so the nationwide PBF
fits in memory.
"""
from array import array
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shutil

import numpy as np
import osmium
from osmium.geom import GeoJSONFactory

from model.osm_classes import DEFAULT_SPEEDS_KMH, POLICY_VERSION, classify_osm, parse_maxspeed
from speedmatrix.geo import haversine_m

ROAD_CLASSES = ("caotoc", "quoclo", "chinh", "noido", "nho", "pha")
HGV_VALUES = ("", "no", "yes", "designated", "destination", "delivery", "other")
SEGMENTS_FORMAT = 1
_TYPECODES = {"q": np.int64, "i": np.int32, "b": np.int8, "f": np.float32}
_FERRY = ROAD_CLASSES.index("pha")
_TONNES = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*(t|kg)?\s*", re.I)
_METRES = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*(m)?\s*", re.I)


def parse_tonnes(value):
    """A positive weight in tonnes from "10", "10 t", "5.5t" or "8000 kg"; None for anything else."""
    match = _TONNES.fullmatch(value or "")
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    if (match.group(2) or "").lower() == "kg":
        number /= 1000.0
    return number if number > 0 else None


def parse_metres(value):
    """A positive length in metres from "4.2" or "4.2 m"; None for feet/inches and other forms."""
    match = _METRES.fullmatch(value or "")
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    return number if number > 0 else None


@dataclass
class SegmentTable:
    a: np.ndarray
    b: np.ndarray
    way: np.ndarray
    a_lat: np.ndarray
    a_lon: np.ndarray
    b_lat: np.ndarray
    b_lon: np.ndarray
    length_m: np.ndarray
    way_id: np.ndarray
    highway: np.ndarray
    road_class: np.ndarray
    oneway: np.ndarray
    name: np.ndarray
    ref: np.ndarray
    maxspeed_kmh: np.ndarray
    maxweight_t: np.ndarray
    maxheight_m: np.ndarray
    hgv: np.ndarray
    cond_hgv: np.ndarray
    cond_maxweight: np.ndarray
    strings: list
    meta: dict
    _index: object = field(default=None, repr=False, compare=False)

    @property
    def n(self) -> int:
        return len(self.a)

    @property
    def mid_lat(self) -> np.ndarray:
        return (self.a_lat.astype(np.float64) + self.b_lat) / 2

    @property
    def mid_lon(self) -> np.ndarray:
        return (self.a_lon.astype(np.float64) + self.b_lon) / 2

    @property
    def segment_class(self) -> np.ndarray:
        return self.road_class[self.way]

    @property
    def sha256(self) -> str:
        payload = json.dumps([self.meta.get("pbf_sha256"), self.meta.get("bbox"), SEGMENTS_FORMAT, POLICY_VERSION])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_ARRAYS_SEGMENT = ("a", "b", "way", "a_lat", "a_lon", "b_lat", "b_lon", "length_m")
_ARRAYS_WAY = ("way_id", "highway", "road_class", "oneway", "name", "ref", "maxspeed_kmh", "maxweight_t",
               "maxheight_m", "hgv", "cond_hgv", "cond_maxweight")


class _Strings:
    def __init__(self):
        self.items = []
        self.lookup = {}

    def index(self, text):
        if not text:
            return -1
        found = self.lookup.get(text)
        if found is None:
            found = self.lookup[text] = len(self.items)
            self.items.append(text)
        return found


def _inside(bbox, lat, lon):
    return bbox is None or (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3])


def _oneway(tags, highway):
    value = tags.get("oneway", "")
    if value == "-1":
        return -1
    if value in ("yes", "1", "true"):
        return 1
    if tags.get("junction") == "roundabout":
        return 1
    if highway in ("motorway", "motorway_link") and value != "no":
        return 1
    return 0


class _SegmentHandler(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.strings = _Strings()
        self.seg = {k: array("q" if k in ("a", "b") else "i" if k == "way" else "f") for k in _ARRAYS_SEGMENT}
        self.ways = {"way_id": array("q"), "highway": array("i"), "road_class": array("b"), "oneway": array("b"),
                     "name": array("i"), "ref": array("i"), "maxspeed_kmh": array("f"), "maxweight_t": array("f"),
                     "maxheight_m": array("f"), "hgv": array("b"), "cond_hgv": array("i"),
                     "cond_maxweight": array("i")}
        self.counts = {c: 0 for c in ROAD_CLASSES}
        self.unparsed = {"maxweight": 0, "maxheight": 0, "maxspeed": 0}

    def way(self, w):
        tags = w.tags
        highway = tags.get("highway", "")
        if highway in DEFAULT_SPEEDS_KMH and tags.get("area") != "yes":
            road_class = ROAD_CLASSES.index(classify_osm(highway, tags.get("ref", ""), tags.get("name", ""),
                                                         tags.get("service", ""))) \
                if classify_osm(highway, tags.get("ref", ""), tags.get("name", ""),
                                tags.get("service", "")) in ROAD_CLASSES else None
        elif tags.get("route") == "ferry":
            highway, road_class = "ferry", _FERRY
        else:
            return
        if road_class is None:
            return
        pts = [(n.ref, n.lat, n.lon) for n in w.nodes if n.location.valid()]
        if len(pts) < 2 or not any(_inside(self.bbox, lat, lon) for _, lat, lon in pts):
            return
        way_index = len(self.ways["way_id"])
        maxweight_raw, maxheight_raw, maxspeed_raw = tags.get("maxweight", ""), tags.get("maxheight", ""), tags.get("maxspeed", "")
        weight, height, speed = parse_tonnes(maxweight_raw), parse_metres(maxheight_raw), parse_maxspeed(maxspeed_raw)
        for key, raw, parsed in (("maxweight", maxweight_raw, weight), ("maxheight", maxheight_raw, height),
                                 ("maxspeed", maxspeed_raw, speed)):
            if raw and parsed is None:
                self.unparsed[key] += 1
        hgv = tags.get("hgv", "")
        hgv_code = HGV_VALUES.index(hgv) if hgv in HGV_VALUES else HGV_VALUES.index("other")
        nan = float("nan")
        s, ws = self.strings, self.ways
        ws["way_id"].append(w.id)
        ws["highway"].append(s.index(highway))
        ws["road_class"].append(road_class)
        ws["oneway"].append(_oneway(tags, highway))
        ws["name"].append(s.index(tags.get("name", "")))
        ws["ref"].append(s.index(tags.get("ref", "")))
        ws["maxspeed_kmh"].append(nan if speed is None else speed)
        ws["maxweight_t"].append(nan if weight is None else weight)
        ws["maxheight_m"].append(nan if height is None else height)
        ws["hgv"].append(hgv_code)
        ws["cond_hgv"].append(s.index(tags.get("hgv:conditional", "")))
        ws["cond_maxweight"].append(s.index(tags.get("maxweight:conditional", "")))
        seg = self.seg
        for (ra, la, oa), (rb, lb, ob) in zip(pts, pts[1:]):
            seg["a"].append(ra)
            seg["b"].append(rb)
            seg["way"].append(way_index)
            seg["a_lat"].append(la)
            seg["a_lon"].append(oa)
            seg["b_lat"].append(lb)
            seg["b_lon"].append(ob)
            self.counts[ROAD_CLASSES[road_class]] += 1


def _sha256_file(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def build_segments(pbf, bbox) -> SegmentTable:
    """Read the PBF once and return the segment table (ways touching ``bbox``, or all with None)."""
    pbf = Path(pbf)
    bbox = None if bbox is None else [float(v) for v in bbox]
    handler = _SegmentHandler(bbox)
    handler.apply_file(str(pbf), locations=True, idx="flex_mem")
    seg, ways = handler.seg, handler.ways
    dtypes = {"a": np.int64, "b": np.int64, "way": np.int32, "way_id": np.int64, "highway": np.int32,
              "road_class": np.int8, "oneway": np.int8, "name": np.int32, "ref": np.int32, "hgv": np.int8,
              "cond_hgv": np.int32, "cond_maxweight": np.int32}
    arrays = {}
    for source in (seg, ways):
        for key, buffer in source.items():
            arrays[key] = np.frombuffer(buffer, dtype=_TYPECODES[buffer.typecode]).astype(
                dtypes.get(key, np.float32))
    arrays["length_m"] = haversine_m(arrays["a_lat"].astype(np.float64), arrays["a_lon"].astype(np.float64),
                                     arrays["b_lat"].astype(np.float64), arrays["b_lon"].astype(np.float64)
                                     ).astype(np.float32)
    meta = {"pbf_sha256": _sha256_file(pbf), "bbox": bbox, "format": SEGMENTS_FORMAT,
            "policy_version": POLICY_VERSION, "counts": handler.counts, "unparsed": handler.unparsed}
    return SegmentTable(strings=list(handler.strings.items), meta=meta, **arrays)


def _swap_dir(tmp: Path, out_dir: Path):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    tmp.rename(out_dir)


def save_segments(table: SegmentTable, out_dir) -> None:
    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_dir.with_name(out_dir.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    np.savez(tmp / "segments.npz", **{k: getattr(table, k) for k in _ARRAYS_SEGMENT + _ARRAYS_WAY})
    (tmp / "strings.json").write_text(json.dumps(table.strings, ensure_ascii=False), encoding="utf-8")
    (tmp / "meta.json").write_text(json.dumps(table.meta, ensure_ascii=False), encoding="utf-8")
    _swap_dir(tmp, out_dir)


def load_segments(out_dir) -> SegmentTable:
    out_dir = Path(out_dir)
    with np.load(out_dir / "segments.npz") as data:
        arrays = {k: data[k] for k in _ARRAYS_SEGMENT + _ARRAYS_WAY}
    strings = json.loads((out_dir / "strings.json").read_text(encoding="utf-8"))
    meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    return SegmentTable(strings=strings, meta=meta, **arrays)


_KEY_DTYPE = np.dtype([("w", np.int64), ("lo", np.int64), ("hi", np.int64)])


def find_segments(table: SegmentTable, keys) -> np.ndarray:
    """Row index per ``[way_id, a, b]`` key (either node order); -1 when missing."""
    keys = np.asarray(keys, dtype=np.int64).reshape(-1, 3)
    if table._index is None:
        rows = np.empty(table.n, dtype=_KEY_DTYPE)
        rows["w"] = table.way_id[table.way]
        rows["lo"] = np.minimum(table.a, table.b)
        rows["hi"] = np.maximum(table.a, table.b)
        order = np.argsort(rows, order=("w", "lo", "hi"), kind="stable")
        table._index = (rows[order], order)
    sorted_rows, order = table._index
    query = np.empty(len(keys), dtype=_KEY_DTYPE)
    query["w"], query["lo"], query["hi"] = keys[:, 0], np.minimum(keys[:, 1], keys[:, 2]), np.maximum(keys[:, 1], keys[:, 2])
    pos = np.searchsorted(sorted_rows, query)
    pos_c = np.minimum(pos, max(len(sorted_rows) - 1, 0))
    if len(sorted_rows) == 0:
        return np.full(len(keys), -1, dtype=np.int64)
    hit = (pos < len(sorted_rows)) & (sorted_rows[pos_c] == query)
    return np.where(hit, order[pos_c], -1).astype(np.int64)


# ---- administrative areas -------------------------------------------------

def build_admin(pbf, levels=(4, 6, 8)) -> list:
    """GeoJSON features of boundary=administrative relations at ``levels``, assembled as multipolygons."""
    factory = GeoJSONFactory()
    wanted = {str(v) for v in levels}
    features = []
    processor = osmium.FileProcessor(str(pbf)).with_locations().with_areas()
    for obj in processor:
        if not obj.is_area() or obj.from_way():
            continue
        tags = obj.tags
        if tags.get("boundary") != "administrative" or tags.get("admin_level") not in wanted:
            continue
        features.append({"type": "Feature", "geometry": json.loads(factory.create_multipolygon(obj)),
                         "properties": {"name": tags.get("name", ""), "admin_level": int(tags["admin_level"]),
                                        "osm_id": obj.orig_id()}})
    return features


def _write_text_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_admin(features, out_dir) -> None:
    _write_text_atomic(Path(out_dir) / "admin.json", json.dumps(features, ensure_ascii=False))


def load_admin(out_dir) -> list:
    return json.loads((Path(out_dir) / "admin.json").read_text(encoding="utf-8"))


# ---- water lines ----------------------------------------------------------

@dataclass
class WaterLines:
    a_lat: np.ndarray
    a_lon: np.ndarray
    b_lat: np.ndarray
    b_lon: np.ndarray
    name: np.ndarray
    strings: list


class _WaterHandler(osmium.SimpleHandler):
    def __init__(self, bbox):
        super().__init__()
        self.bbox = bbox
        self.strings = _Strings()
        self.cols = {k: array("f") for k in ("a_lat", "a_lon", "b_lat", "b_lon")}
        self.name = array("i")

    def way(self, w):
        tags = w.tags
        if tags.get("natural") == "coastline":
            label = "coastline"
        elif tags.get("waterway") in ("river", "canal") and tags.get("name"):
            label = tags["name"]
        else:
            return
        pts = [(n.lat, n.lon) for n in w.nodes if n.location.valid()]
        if len(pts) < 2 or not any(_inside(self.bbox, lat, lon) for lat, lon in pts):
            return
        code = self.strings.index(label)
        for (la, oa), (lb, ob) in zip(pts, pts[1:]):
            self.cols["a_lat"].append(la)
            self.cols["a_lon"].append(oa)
            self.cols["b_lat"].append(lb)
            self.cols["b_lon"].append(ob)
            self.name.append(code)


def build_water_lines(pbf, bbox) -> WaterLines:
    bbox = None if bbox is None else [float(v) for v in bbox]
    handler = _WaterHandler(bbox)
    handler.apply_file(str(pbf), locations=True, idx="flex_mem")
    cols = {k: np.frombuffer(v, dtype=np.float32).copy() for k, v in handler.cols.items()}
    return WaterLines(name=np.frombuffer(handler.name, dtype=np.int32).copy(), strings=list(handler.strings.items), **cols)


def save_water_lines(lines: WaterLines, out_dir) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "water.npz.tmp"
    with tmp.open("wb") as sink:
        np.savez(sink, a_lat=lines.a_lat, a_lon=lines.a_lon, b_lat=lines.b_lat, b_lon=lines.b_lon,
                 name=lines.name, strings=np.array(json.dumps(lines.strings, ensure_ascii=False)))
    tmp.replace(out_dir / "water.npz")


def load_water_lines(out_dir) -> WaterLines:
    with np.load(Path(out_dir) / "water.npz") as data:
        return WaterLines(a_lat=data["a_lat"], a_lon=data["a_lon"], b_lat=data["b_lat"], b_lon=data["b_lon"],
                          name=data["name"], strings=json.loads(str(data["strings"])))
