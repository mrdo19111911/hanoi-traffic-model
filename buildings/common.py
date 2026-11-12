"""Constants and small helpers shared by the buildings pipeline scripts (spec).

Every value here replaced a copy that lived in several scripts; each replacement is proven bit-for-bit equal to the
expression it replaced by tests/test_buildings_common.py, so nothing published changes. Contents:

- Paths: REPO, BY_AREA (buildings/by_area), INDEX_JSON (its index.json), COMMUNE_GROUPS (buildings/commune_groups.json),
  CALIB_DIR (data/buildings/_calib) and RAW_DIR (its raw_by_area, the raw 2.5D layer of commit 703e019);
  load_index(path=None) and load_commune_groups(path=None) read those JSON files (a falsy path = the default).
  CLI defaults that are relative strings ('buildings/by_area', 'buildings/commune_groups.json') stay in the scripts.
- Metres per degree, equirectangular: M_PER_DEG_LON = 111320 (at the equator, times cos(lat)), M_PER_DEG_LAT = 110574.
  metres_per_deg(lat) is the scalar math form compact_layer.py has always used (the frontend's metresPerDeg mirrors it
  and a committed fixture cross-checks decoding: its results must never change); metres_per_deg_np(lat) is the numpy
  form (kx array or np.float64, ky = 110574.0). Sites that multiply the constants in their own order keep that order.
  Deliberately not shared: alpha_vs_photos.py uses 110540 (a recorded measurement cited by the README) and
  extra_heights.py the km form 111.32 x 110.574 x cos (a different unit, not bit-equal to the metre form).
- Floors: LEVEL_M = 3.3 m per floor, FLOOR_BASE_M = 3.9; floors_of(h) = 1 + round((h - 3.9) / 3.3) rounding half away
  from zero, clipped to 1-255, uint8 (the SQL rule of heights_25d.attrs_query). part_heights.part_height and
  calibrate_heights.truth_frame keep their own half-up variant inline (same floors on positive heights, see the test).
- UTM 48N: UTM_EPSG = 32648, and the DuckDB ST_Transform argument fragments UTM_SQL (lon/lat -> UTM) and UTM_BACK_SQL.
- DuckDB: spatial_connection(path=None, *, install=False, progress_bar=False) opens a connection (in memory when path is
  None) and runs '[INSTALL spatial; ]LOAD spatial;[ SET enable_progress_bar = false;]'. duckdb is imported and
  duckdb.connect looked up at call time, so build_areas.limit_threads' patch of duckdb.connect still applies.
- Parquet: ZSTD_LEVEL = 19, ROW_GROUP = 500_000 and PARQUET_KW, the pq.write_table keywords of every published file.
"""
import json, math, os

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BY_AREA = os.path.join(REPO, 'buildings', 'by_area')
INDEX_JSON = os.path.join(BY_AREA, 'index.json')
COMMUNE_GROUPS = os.path.join(REPO, 'buildings', 'commune_groups.json')
CALIB_DIR = os.path.join(REPO, 'data', 'buildings', '_calib')
RAW_DIR = os.path.join(CALIB_DIR, 'raw_by_area')

M_PER_DEG_LON = 111320             # metres per degree of longitude at the equator (times cos(lat))
M_PER_DEG_LAT = 110574             # metres per degree of latitude

LEVEL_M = 3.3                      # metres per floor
FLOOR_BASE_M = 3.9                 # floors = 1 + round((h - FLOOR_BASE_M) / LEVEL_M)

UTM_EPSG = 32648                   # UTM 48N
UTM_SQL = f"'EPSG:4326', 'EPSG:{UTM_EPSG}', always_xy := true"
UTM_BACK_SQL = f"'EPSG:{UTM_EPSG}', 'EPSG:4326', always_xy := true"

ZSTD_LEVEL = 19
ROW_GROUP = 500_000
PARQUET_KW = {'compression': 'zstd', 'compression_level': ZSTD_LEVEL, 'row_group_size': ROW_GROUP}


def load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_index(path=None):
    """by_area/index.json (or the index file at `path`)."""
    return load_json(path or INDEX_JSON)


def load_commune_groups(path=None):
    """commune_groups.json (or the groups file at `path`)."""
    return load_json(path or COMMUNE_GROUPS)


def metres_per_deg(lat):
    """Metres per degree (east, north) at a scalar latitude; math-based, results must never change."""
    return M_PER_DEG_LON * math.cos(math.radians(lat)), M_PER_DEG_LAT


def metres_per_deg_np(lat):
    """Metres per degree (east as array or np.float64, north as float 110574.0) at numpy latitudes."""
    return M_PER_DEG_LON * np.cos(np.radians(lat)), float(M_PER_DEG_LAT)


def floors_of(h):
    """Floors of heights in metres: 1 + round((h - 3.9) / 3.3) (half away from zero), clipped to 1-255, uint8."""
    x = (np.asarray(h, float) - FLOOR_BASE_M) / LEVEL_M
    return np.clip(1 + np.sign(x) * np.floor(np.abs(x) + 0.5), 1, 255).astype(np.uint8)


def spatial_connection(path=None, *, install=False, progress_bar=False):
    """DuckDB connection (in memory when `path` is None) with the spatial extension loaded; INSTALL first when
    `install`, and the progress bar switched off unless `progress_bar`. Site-specific SETs stay with the caller."""
    import duckdb
    con = duckdb.connect() if path is None else duckdb.connect(path)
    con.execute(('INSTALL spatial; ' if install else '') + 'LOAD spatial;'
                + ('' if progress_bar else ' SET enable_progress_bar = false;'))
    return con
