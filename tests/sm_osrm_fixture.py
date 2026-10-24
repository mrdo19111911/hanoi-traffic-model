"""Tiny OSRM v26.10.0 datasets in the real tar format, plus tiny .osm files, for the speedmatrix tests (not a test).

``write_dataset`` cuts ways into geometries, builds the edge-based nodes and turns the way OSRM does (the layouts of
``speedmatrix/osrmfiles.py``) and keeps every identity of the real files true, so a reader test can break exactly one
thing through the keyword options.
"""
import io
import json
import math
from pathlib import Path
import tarfile

import numpy as np

FINGERPRINT = b"OSRP\x1a\n\x00\x02\x01\x08\xfa"
INVALID = (1 << 22) - 1
KMH = {"residential": 25, "primary": 65, "track": 10, "secondary": 50, "tertiary": 35}

# (lon, lat) of the fixture town; way ids are 1-based in TOWN_WAYS order.
TOWN_COORDS = {101: (105.8000, 21.0000), 107: (105.8005, 21.0000), 102: (105.8010, 21.0000),
               103: (105.8020, 21.0000), 104: (105.8010, 21.0010), 105: (105.8010, 20.9990),
               106: (105.8020, 20.9990)}
TOWN_WAYS = [(1, [101, 107, 102, 103], {"highway": "residential", "name": "Phố Một"}),
             (2, [104, 102, 105], {"highway": "primary", "name": "Phố Hai"}),
             (3, [105, 106], {"highway": "residential", "oneway": "yes", "name": "Phố Ba"}),
             (4, [103, 106], {"highway": "track", "name": "Đường Bốn"})]
TOWN_OBSTACLES = {(107, "forward", 1): 20}
TOWN_BANNED = {(104, 102, 103)}


def _tar(path, blocks, fingerprint=FINGERPRINT):
    """blocks: {member name: bytes}; the fingerprint member comes first, like OSRM writes it."""
    with tarfile.open(path, "w") as tar:
        for name, data in [("osrm_fingerprint.meta", fingerprint), *blocks.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _meta(count):
    return np.array([count], dtype="<u8").tobytes()


def _plain(name, array, count=None):
    array = np.ascontiguousarray(array)
    return {name + ".meta": _meta(len(array) if count is None else count), name: array.tobytes()}


def pack(values, bits):
    """OSRM PackedVector words: value i at bit i * bits, LSB first, ceil(n / 64) * bits words."""
    words = -(-len(values) // 64) * bits
    big = 0
    for i, v in enumerate(values):
        big |= int(v) << (i * bits)
    return np.frombuffer(big.to_bytes(words * 8, "little"), dtype="<u8")


def _packed_blocks(name, values, bits, short=False):
    words = pack(values, bits)
    if short:
        words = words[:-1]
    return {name + "/number_of_elements.meta": _meta(len(values)), name + "/packed.meta": _meta(len(words)),
            name + "/packed": words.tobytes()}


def _nbg_blocks(coords, ids, short=False):
    blocks = _plain("/common/nbn_data/coordinates", np.array(coords, dtype="<i4").reshape(-1, 2))
    blocks.update(_packed_blocks("/common/nbn_data/osm_node_ids", ids, 40, short))
    return blocks


def write_nbg_nodes(base, coords, ids):
    """Only ``<base>.nbg_nodes``: coords are (lon_e6, lat_e6) pairs, ids the OSM node ids; returns the path."""
    path = Path(str(base) + ".nbg_nodes")
    _tar(path, _nbg_blocks(coords, ids))
    return path


def _haversine_m(a, b):
    (lon1, lat1), (lon2, lat2) = [(x / 1e6, y / 1e6) for x, y in (a, b)]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def _drop(blocks, drop_blocks):
    return {k: v for k, v in blocks.items()
            if not any(k == d or k == d + ".meta" or k.startswith(d + "/") for d in drop_blocks)}


def write_dataset(root, coords, ways, obstacles={}, banned=set(), turn_ds=20, uturn_ds=200, pbf_sha256="fixture", *,
                  drop_blocks=frozenset(), backward_edge=False, fingerprint_of={}, enw_delta={}, weight_delta={},
                  short_packed=frozenset()):
    """Write ``<root>/map.osrm.*`` and ``<root>/build_matrix.json``; returns the base path ``<root>/map.osrm``.

    coords {osm id: (lon, lat)}; ways [(way id, [node ids], tags)]. obstacles {(node id, "forward"|"reverse", way id):
    ds} are signals on the zero-length node move; banned {(from, via, to)} are node triples without a turn. The
    keyword options break the output on purpose: drop_blocks (block names left out), backward_edge (backward=1 on the
    first EBG edge), fingerprint_of ({suffix: 11-byte fingerprint}), enw_delta / weight_delta ({id: ds} added to the
    .enw duration of an EBN / the weight of an EBG edge), short_packed (packed blocks written one word short).
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    base = root / "map.osrm"
    ids = list(coords)
    index_of = {i: k for k, i in enumerate(ids)}
    e6 = [(round(coords[i][0] * 1e6), round(coords[i][1] * 1e6)) for i in ids]
    uses = {}
    for _, refs, _ in ways:
        for n in set(refs):
            uses[n] = uses.get(n, 0) + 1
    geos = []                                   # (way id, [node ids], one-way)
    for way_id, refs, tags in ways:
        cut = [0] + [k for k in range(1, len(refs) - 1) if uses[refs[k]] > 1] + [len(refs) - 1]
        for a, b in zip(cut, cut[1:]):
            geos.append((way_id, refs[a:b + 1], tags.get("oneway") == "yes"))
    index, nodes, fwd, rev = [0], [], [], []
    ebn_fwd_dur, ebn_rev_dur = [], []
    highway = {w: t["highway"] for w, _, t in ways}
    for way_id, refs, _oneway in geos:
        kmh = KMH[highway[way_id]]
        m = len(refs) - 1
        f, r = [INVALID], []
        n = []
        for j in range(m):
            n += [index_of[refs[j]], index_of[refs[j + 1]]]
        seg = [max(1, round(36 * _haversine_m(e6[index_of[refs[j]]], e6[index_of[refs[j + 1]]]) / kmh)) for j in range(m)]
        for j in range(m):
            r.append(seg[j])                    # rd[2j]: reverse of real segment j
            f.append(seg[j])                    # fd[2j+1]
            if j < m - 1:
                node = refs[j + 1]
                r.append(obstacles.get((node, "reverse", way_id), 0))     # rd[2j+1]: node move j
                f.append(obstacles.get((node, "forward", way_id), 0))     # fd[2j+2]
        r.append(INVALID)
        nodes += n
        fwd += f
        rev += r
        index.append(index[-1] + len(n))
        b, e = index[-2], index[-1]
        ebn_fwd_dur.append(sum(fwd[b + 1:e]))
        ebn_rev_dur.append(sum(rev[b:e - 1]))
    # EBNs: every geometry forward, then the reverse of every two-way geometry.
    ebn = [(g, True) for g in range(len(geos))] + [(g, False) for g in range(len(geos)) if not geos[g][2]]
    enw = [ebn_fwd_dur[g] if fw else ebn_rev_dur[g] for g, fw in ebn]

    def start(e):
        g, fw = ebn[e]
        return geos[g][1][0 if fw else -1]

    def end(e):
        g, fw = ebn[e]
        return geos[g][1][-1 if fw else 0]

    def before_end(e):
        g, fw = ebn[e]
        return geos[g][1][-2 if fw else 1]

    def after_start(e):
        g, fw = ebn[e]
        return geos[g][1][1 if fw else -2]

    turns = []
    for a in range(len(ebn)):
        for b in range(len(ebn)):
            if start(b) != end(a) or (before_end(a), end(a), after_start(b)) in banned:
                continue
            same = ebn[a][0] == ebn[b][0] and ebn[a][1] != ebn[b][1]
            turns.append((a, b, uturn_ds if same else turn_ds))
    edges = np.zeros((len(turns), 6), dtype="<u4")
    for t, (a, b, ds) in enumerate(turns):
        duration = enw[a] + ds
        weight = duration + weight_delta.get(t, 0)
        edges[t] = (a, b, t, np.int32(weight).view(np.uint32), np.float32(0).view(np.uint32), duration | (1 << 30))
    if backward_edge and len(edges):
        edges[0, 5] |= 1 << 31
    ebn_rows = np.array([(g | (int(fw) << 31), 0, 0) for g, fw in ebn], dtype="<u4").reshape(-1, 3)
    enw_file = [d + enw_delta.get(i, 0) for i, d in enumerate(enw)]
    short = set(short_packed)
    files = {
        ".nbg_nodes": _nbg_blocks(e6, ids, "/common/nbn_data/osm_node_ids" in short),
        ".geometry": {**_plain("/common/segment_data/index", np.array(index, dtype="<u4")),
                      **_plain("/common/segment_data/nodes", np.array(nodes, dtype="<u4")),
                      **_packed_blocks("/common/segment_data/forward_durations", fwd, 22,
                                       "/common/segment_data/forward_durations" in short),
                      **_packed_blocks("/common/segment_data/reverse_durations", rev, 22,
                                       "/common/segment_data/reverse_durations" in short),
                      **_plain("/common/segment_data/forward_data_sources", np.zeros(len(nodes), dtype=np.uint8)),
                      **_plain("/common/segment_data/reverse_data_sources", np.zeros(len(nodes), dtype=np.uint8))},
        ".ebg_nodes": _plain("/common/ebg_node_data/nodes", ebn_rows),
        ".ebg": {**_plain("/common/number_of_edge_based_nodes", np.array([len(ebn)], dtype="<u4")),
                 **_plain("/common/edge_based_edge_list", edges)},
        ".turn_duration_penalties": _plain("/common/turn_penalty/duration",
                                           np.array([ds for _, _, ds in turns], dtype="<i2")),
        ".enw": _plain("/extractor/edge_based_node_durations", np.array(enw_file, dtype="<u4")),
    }
    for suffix, blocks in files.items():
        _tar(str(base) + suffix, _drop(blocks, drop_blocks), fingerprint_of.get(suffix, FINGERPRINT))
    (root / "build_matrix.json").write_text(
        json.dumps({"input": {"pbf_sha256": pbf_sha256, "osrm_version": "v26.10.0"}}), encoding="utf-8")
    return base


def grid(n, step_deg=0.001, primary_every=4):
    """An n x n street grid: ({node id: (lon, lat)}, ways), every row and column one way, every
    ``primary_every``-th one primary, the rest residential."""
    coords = {r * n + c + 1: (105.8 + c * step_deg, 21.0 + r * step_deg) for r in range(n) for c in range(n)}
    ways = []
    for k in range(n):
        for label, refs in (("Row", [k * n + c + 1 for c in range(n)]), ("Col", [r * n + k + 1 for r in range(n)])):
            ways.append((len(ways) + 1, refs, {"highway": "primary" if k % primary_every == 0 else "residential",
                                               "name": f"{label} {k}"}))
    return coords, ways


def write_osm(path, coords, ways):
    """A tiny .osm XML file (same layout as tests/test_sm_build.py); coords {id: (lon, lat)}, ways with tag dicts."""
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6'>"]
    for i, (lon, lat) in coords.items():
        lines.append(f"<node id='{i}' version='1' lat='{lat}' lon='{lon}'></node>")
    for way_id, refs, tags in ways:
        lines.append(f"<way id='{way_id}' version='1'>")
        lines += [f"<nd ref='{n}'/>" for n in refs]
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</way>")
    lines.append("</osm>")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
