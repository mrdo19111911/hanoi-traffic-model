"""Reader for the OSRM v26.10.0 dataset files the matrix graph is built from.

OSRM writes each ``<base>.<suffix>`` as a tar of raw blocks: ``<name>`` holds the elements and ``<name>.meta`` an
8-byte element count. Packed vectors (``<name>/packed``, ``<name>/number_of_elements.meta``) store ``bits`` per
element, LSB first across little-endian 64-bit words, in blocks of 64 elements. Block names and layouts are the
ones of the 2026-10-21 matrix-profile build; nothing here guesses them.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import tarfile

import numpy as np

OSRM_VERSION = "v26.10.0"
INVALID_DURATION = (1 << 22) - 1
ID_BITS = 40
DURATION_BITS = 22
PACK_BLOCK = 64
_UNPACK_CHUNK = 1 << 22
_DURATION_MASK = (1 << 30) - 1
_FINGERPRINT_FILES = (".nbg_nodes", ".geometry", ".ebg_nodes", ".ebg", ".turn_duration_penalties", ".enw")


class OsrmFormatError(ValueError):
    """An OSRM file is missing a block, has the wrong size or breaks one of the identities the graph relies on."""


@dataclass
class OsrmDataset:
    nbg_osm: np.ndarray            # int64 (N,)
    nbg_lon_e6: np.ndarray         # int32 (N,)
    nbg_lat_e6: np.ndarray         # int32 (N,)
    geo_index: np.ndarray          # int64 (G+1,)
    geo_nodes: np.ndarray          # int32 (entries,)
    geo_fwd_ds: np.ndarray         # uint32 (entries,)
    geo_rev_ds: np.ndarray         # uint32 (entries,)
    ebn_geometry: np.ndarray       # int32 (E,)
    ebn_forward: np.ndarray        # bool (E,)
    ebn_tiny: np.ndarray           # bool (E,)
    ebn_duration_ds: np.ndarray    # uint32 (E,)
    turn_src: np.ndarray           # int32 (T,)
    turn_dst: np.ndarray           # int32 (T,)
    turn_ds: np.ndarray            # uint16 (T,)
    turn_total_ds: np.ndarray      # uint32 (T,)
    restricted_edges: int          # EBG edges whose weight differs from their duration (0 on a matrix-profile build)
    fingerprint: bytes


def _members(path):
    with tarfile.open(path) as tar:
        return {m.name: (m.offset_data, m.size) for m in tar.getmembers()}


def _count(path, name, members):
    if name + ".meta" not in members:
        raise OsrmFormatError(f"{Path(path).name} has no block {name}.meta")
    return int(np.fromfile(path, dtype="<u8", count=1, offset=members[name + ".meta"][0])[0])


def _block(path, name, dtype, members):
    if name not in members:
        raise OsrmFormatError(f"{Path(path).name} has no block {name}")
    count = _count(path, name, members)
    offset, size = members[name]
    dtype = np.dtype(dtype)
    if size != count * dtype.itemsize:
        raise OsrmFormatError(f"{Path(path).name}: block {name} holds {size} bytes, not {count} x {dtype} "
                              f"({count * dtype.itemsize})")
    return np.fromfile(path, dtype=dtype, count=count, offset=offset)


def read_block(path, name, dtype):
    """A block as an array: ``np.fromfile`` at the tar member offset, the count from ``<name>.meta`` (uint64)."""
    return _block(path, name, dtype, _members(path))


def _packed(path, name, bits, members):
    count = _count(path, name + "/number_of_elements", members)
    words = _block(path, name + "/packed", "<u8", members)
    expected = -(-count // PACK_BLOCK) * bits
    if len(words) != expected:
        raise OsrmFormatError(f"{Path(path).name}: packed block {name} holds {len(words)} words, expected {expected} "
                              f"for {count} elements of {bits} bits")
    out = np.empty(count, dtype=np.uint64)
    mask = np.uint64((1 << bits) - 1)
    for start in range(0, count, _UNPACK_CHUNK):      # chunked: a 53M-element block would need GBs of temporaries
        idx = np.arange(start, min(start + _UNPACK_CHUNK, count), dtype=np.uint64) * np.uint64(bits)
        w = (idx >> np.uint64(6)).astype(np.int64)
        o = idx & np.uint64(63)
        lo = words[w] >> o
        hi = np.where(o + np.uint64(bits) > 64,
                      words[np.minimum(w + 1, len(words) - 1)] << ((np.uint64(64) - o) & np.uint64(63)), np.uint64(0))
        out[start:start + len(idx)] = (lo | hi) & mask
    return out


def read_packed(path, name, bits):
    """The elements of an OSRM PackedVector as uint64: element i sits at bit ``i * bits``, LSB first."""
    return _packed(path, name, bits, _members(path))


def _fingerprint(path, members):
    if "osrm_fingerprint.meta" not in members:
        raise OsrmFormatError(f"{Path(path).name} has no osrm_fingerprint.meta")
    offset, size = members["osrm_fingerprint.meta"]
    return bytes(np.fromfile(path, dtype=np.uint8, count=size, offset=offset))


def read_dataset(base):
    """All blocks of the turn graph from ``<base>.<suffix>`` (base = path without extension, ``....osrm``)."""
    paths = {s: str(base) + s for s in _FINGERPRINT_FILES}
    members = {s: _members(p) for s, p in paths.items()}
    prints = {s: _fingerprint(paths[s], members[s]) for s in _FINGERPRINT_FILES}
    if len(set(prints.values())) != 1:
        raise OsrmFormatError("osrm_fingerprint.meta differs between files: "
                              + ", ".join(f"{s}={v.hex()}" for s, v in prints.items()))

    def block(suffix, name, dtype):
        return _block(paths[suffix], name, dtype, members[suffix])

    def packed(suffix, name, bits):
        return _packed(paths[suffix], name, bits, members[suffix])

    coords = block(".nbg_nodes", "/common/nbn_data/coordinates", (np.int32, 2))
    nbg_osm = packed(".nbg_nodes", "/common/nbn_data/osm_node_ids", ID_BITS).astype(np.int64)
    if len(nbg_osm) != len(coords):
        raise OsrmFormatError(f".nbg_nodes: {len(coords)} coordinates but {len(nbg_osm)} osm node ids")
    geo_index = block(".geometry", "/common/segment_data/index", "<u4").astype(np.int64)
    geo_nodes = block(".geometry", "/common/segment_data/nodes", "<u4").astype(np.int32)
    geo_fwd = packed(".geometry", "/common/segment_data/forward_durations", DURATION_BITS).astype(np.uint32)
    geo_rev = packed(".geometry", "/common/segment_data/reverse_durations", DURATION_BITS).astype(np.uint32)
    for label, arr in (("nodes", geo_nodes), ("forward_durations", geo_fwd), ("reverse_durations", geo_rev)):
        if len(arr) != int(geo_index[-1]):
            raise OsrmFormatError(f".geometry: {label} has {len(arr)} entries, the index ends at {int(geo_index[-1])}")
    ebn = block(".ebg_nodes", "/common/ebg_node_data/nodes", (np.uint32, 3))
    ebn_geometry = (ebn[:, 0] & np.uint32(0x7FFFFFFF)).astype(np.int32)
    ebn_forward = (ebn[:, 0] >> np.uint32(31)).astype(bool)
    ebn_tiny = (ebn[:, 1] >> np.uint32(31)).astype(bool)
    del ebn
    declared = int(block(".ebg", "/common/number_of_edge_based_nodes", "<u4")[0])
    if declared != len(ebn_geometry):
        raise OsrmFormatError(f".ebg: number_of_edge_based_nodes is {declared}, .ebg_nodes holds {len(ebn_geometry)}")
    ebn_duration = block(".enw", "/extractor/edge_based_node_durations", "<u4")
    if len(ebn_duration) != len(ebn_geometry):
        raise OsrmFormatError(f".enw: {len(ebn_duration)} durations for {len(ebn_geometry)} edge-based nodes")
    edges = block(".ebg", "/common/edge_based_edge_list", (np.uint32, 6))
    flags = edges[:, 5]
    bad = (((flags >> np.uint32(30)) & np.uint32(1)) != 1) | ((flags >> np.uint32(31)) != 0)
    if bad.any():
        raise OsrmFormatError(f".ebg: {int(bad.sum())} edges lack forward=1 or have backward=1 "
                              f"(first at {int(np.argmax(bad))})")
    if not np.array_equal(edges[:, 2], np.arange(len(edges), dtype=np.uint32)):
        raise OsrmFormatError(".ebg: turn_id is not 0..T-1 in order")
    turn_raw = block(".turn_duration_penalties", "/common/turn_penalty/duration", "<i2")
    if len(turn_raw) != len(edges):
        raise OsrmFormatError(f".turn_duration_penalties: {len(turn_raw)} turns for {len(edges)} edges")
    if len(turn_raw) and int(turn_raw.min()) < 0:
        raise OsrmFormatError(f".turn_duration_penalties: negative turn duration {int(turn_raw.min())}")
    duration = flags & np.uint32(_DURATION_MASK)
    restricted = int((edges[:, 3].view(np.int32).astype(np.int64) != duration.astype(np.int64)).sum())
    return OsrmDataset(
        nbg_osm=nbg_osm, nbg_lon_e6=np.ascontiguousarray(coords[:, 0]), nbg_lat_e6=np.ascontiguousarray(coords[:, 1]),
        geo_index=geo_index, geo_nodes=geo_nodes, geo_fwd_ds=geo_fwd, geo_rev_ds=geo_rev,
        ebn_geometry=ebn_geometry, ebn_forward=ebn_forward, ebn_tiny=ebn_tiny, ebn_duration_ds=ebn_duration,
        turn_src=edges[:, 0].astype(np.int32), turn_dst=edges[:, 1].astype(np.int32),
        turn_ds=turn_raw.astype(np.uint16), turn_total_ds=duration.astype(np.uint32),
        restricted_edges=restricted, fingerprint=next(iter(prints.values())))


def read_datasources(base):
    """Forward and reverse data source id (uint8) of every geometry entry."""
    path = str(base) + ".geometry"
    members = _members(path)
    return (_block(path, "/common/segment_data/forward_data_sources", np.uint8, members),
            _block(path, "/common/segment_data/reverse_data_sources", np.uint8, members))


def check_build_json(path):
    """The parsed build json; OsrmFormatError unless it was built by OSRM_VERSION."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    found = (doc.get("input") or {}).get("osrm_version")
    if found != OSRM_VERSION:
        raise OsrmFormatError(f"{Path(path).name}: osrm_version is {found!r}, only {OSRM_VERSION} is supported")
    return doc
