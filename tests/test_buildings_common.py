"""Equivalence tests for buildings/common.py: every shared constant or helper must give bit-for-bit what the
expression it replaced gave. The old expressions are copied below as literals; floats compare with ==, arrays with
np.array_equal, never approx. FINGERPRINTS were taken from the pipeline functions before the refactor (sha256 of
their raw output bytes on deterministic synthetic inputs), so the refactored functions must reproduce them exactly."""
import hashlib, json, math, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

B = Path(__file__).resolve().parents[1] / "buildings"
sys.path.insert(0, str(B))

LATS = [8.5, 10.8, 16.0, 21.03, 23.4, 0.0, 21.0, 8.0, 23.5, 105.0 / 7]
LAT_ARR = np.r_[np.array(LATS), np.linspace(8.0, 23.5, 3101)]
# Heights 0..100 m in 0.05 m steps, plus each exact .5 boundary of (h - 3.9) / 3.3 and its float neighbours.
_EDGE = 3.9 + 3.3 * (np.arange(-2, 31) + 0.5)
H_GRID = np.r_[np.round(np.arange(2001) * 0.05, 2), _EDGE, np.nextafter(_EDGE, -np.inf), np.nextafter(_EDGE, np.inf)]


def _sha(*parts):
    h = hashlib.sha256()
    for x in parts:
        h.update(x if isinstance(x, bytes) else np.ascontiguousarray(x).tobytes())
    return h.hexdigest()


def _table():
    """A deterministic compact table (rects and rings) over Vietnam's latitudes."""
    import shapely
    from compact_layer import encode_rows
    rng = np.random.default_rng(7)
    geoms = []
    for lat in (8.5, 10.8, 16.0, 21.03, 23.4):
        for k in range(12):
            x0, y0 = 105.8 + k * 1e-3 + rng.random() * 1e-4, lat + rng.random() * 1e-4
            w, h = 3e-5 + rng.random() * 1e-4, 3e-5 + rng.random() * 1e-4
            pts = [(0, 0), (w, 0), (w, h), (0, h)] if k % 3 else [(0, 0), (w, 0), (w, h / 3), (w / 3, h / 3), (w / 3, h), (0, h)]
            geoms.append(shapely.affinity.rotate(shapely.Polygon([(x0 + a, y0 + b) for a, b in pts]), 17 * k, origin='centroid'))
    n = len(geoms)
    tbl, _, _ = encode_rows(list(range(n)), ['osm'] * n, shapely.to_wkb(geoms), [100] * n, ['g25'] * n, [3] * n, [None] * n)
    return tbl


def fingerprints():
    """sha256 of the outputs of every pipeline function whose constants the refactor touches."""
    import shapely
    import calibrate_heights as ch, check_road_overlap as cro, compact_layer as cl, fit_footprints as ff
    import frontrow_heights as fr, heights_25d as h25, housenumber_interp as hn, part_heights as ph
    tbl = _table()
    rng = np.random.default_rng(11)
    lon, lat = rng.uniform(102.1, 109.6, 5000), rng.uniform(8.2, 23.5, 5000)
    g, kx, ky = ch.ring_geoms(tbl)
    fg, frame = fr.common_frame(tbl)

    class Rec:
        def execute(self, q):
            self.q = q
            return self

        def fetchall(self):
            return []
    rec = Rec()
    cro.carriageways(rec, 'r.parquet', (105.8, 21.0, 105.9, 21.1))
    parts = [ph.part_height(f'{h:.2f}', lv, roof) for h in H_GRID for lv, roof in ((None, None), ('3', '1.5'))]
    parts += [ph.part_height(None, f'{lv / 4:g}', roof) for lv in range(1, 400) for roof in (None, '2', '150')]
    return {
        'metres_per_deg': _sha(repr([cl.metres_per_deg(x) for x in LATS]).encode()),
        'ring_geoms': _sha(b''.join(shapely.to_wkb(g)), kx, ky),
        'common_frame': _sha(b''.join(shapely.to_wkb(fg)), repr(frame).encode()),
        'footprint_bounds': _sha(*ph.footprint_bounds(tbl)),
        'cell_keys': _sha(ch.cell_keys(lon, lat)),
        'block_keys': _sha(ch.block_keys(lon, lat), ch.block_keys(lon, lat, 500.0)),
        'metres_per_unit': _sha(*ff.metres_per_unit(LAT_ARR), repr(ff.metres_per_unit(21.03)).encode()),
        'floors_of': _sha(ch.floors_of(H_GRID), repr(ch.LEVEL_M).encode()),
        'part_height': _sha(repr(parts).encode()),
        'housenumber_k': _sha(repr((hn.KX, hn.KY)).encode()),
        'attrs_query': _sha(h25.attrs_query("'src.parquet'", 'hh', '').encode(), h25.attrs_query('m0', 'hh', 'WHERE x').encode()),
        'carriageways_sql': _sha(rec.q.encode()),
        'frontrow_params': _sha(json.dumps(fr.PARAMS, ensure_ascii=False).encode()),
    }


FINGERPRINTS = {  # taken from the pre-refactor code (commit d43426b)
    'metres_per_deg': '6134716e99a0ccac25516dcfd3bb5696bf73d91bc219384476986d5b2d6a0bc0',
    'ring_geoms': '47f8685c1c2a24fde143f75f2750fc14dd5458e5a3bd8ae5fcfd1c56a7cd1f01',
    'common_frame': '5ccd4b235d82b33865761be1366d77efc29fddbc2b7d8e8fe31b3d716e820578',
    'footprint_bounds': '2f678bb620d756441a84de9b86107bc97a769be0ca2b17ee4c122c89040f50c2',
    'cell_keys': '1439ab22c4f8df19f4481d2dbb33301eaf456a32526afb62cc70a9c7cb1c3aaf',
    'block_keys': 'c1aa3936ab18c3ddb0c283f10d7733e56eeec86684cf6a3aaeff38c48540d208',
    'metres_per_unit': '67a70b35aa9ac8d9063180eb3ca9980c24e3dab35ef347393f603ba38a7bd6d9',
    'floors_of': 'ae3c38e99520342ac3f608f812c4a6cae011c32ffb2df7d08dede1d928ade0eb',
    'part_height': 'e52c55e9e3dfb6c694e5e9b02d1b6802579e769470d74b95f08552378c5c959d',
    'housenumber_k': '22e8eab22326403d0d3edfcdd227fbae1f4a4f7a67d9e4067c06c38e6437a433',
    'attrs_query': '225c9ffe0d1d70c1ab2fcf6637ddc4f336f4b98bb94761a6521f448a32423746',
    'carriageways_sql': '3071be3132204bb24a5443e94e3de0b43bdc6bd4780b78a4150b8320c2701f43',
    'frontrow_params': '8c1a21ee65bddfc6f6ae3712d18addcf6da37eba7e466529af763f26d62154ac',
}


class FingerprintTests(unittest.TestCase):
    def test_pipeline_outputs_unchanged(self):
        got = fingerprints()
        for k, v in FINGERPRINTS.items():
            self.assertEqual(got[k], v, k)
        self.assertEqual(set(got), set(FINGERPRINTS))


class MetresPerDegTests(unittest.TestCase):
    def test_scalar_matches_compact_layer_formula(self):
        import common, compact_layer
        for lat in LATS:
            old = (111320 * math.cos(math.radians(lat)), 110574)
            for new in (common.metres_per_deg(lat), compact_layer.metres_per_deg(lat)):
                self.assertEqual(new, old)
                self.assertIs(type(new[0]), float)
                self.assertIs(type(new[1]), int)

    def test_array_matches_every_old_site(self):
        from common import M_PER_DEG_LAT, M_PER_DEG_LON, metres_per_deg_np
        lat, n = LAT_ARR, len(LAT_ARR)
        kx, ky = metres_per_deg_np(lat)
        self.assertTrue(np.array_equal(kx, 111320 * np.cos(np.radians(lat))))
        self.assertTrue(np.array_equal(kx, 111320.0 * np.cos(np.radians(lat))))
        self.assertIs(type(ky), float)
        self.assertEqual(ky, 110574.0)
        # calibrate_heights.ring_geoms
        self.assertTrue(np.array_equal(kx / 1e6, 111320 * np.cos(np.radians(lat)) / 1e6))
        self.assertTrue(np.array_equal(np.full(n, M_PER_DEG_LAT / 1e6), np.full(n, 110574 / 1e6)))
        # calibrate_heights.cell_keys / block_keys: only the constants change, the order of operations stays
        lon = np.linspace(102.0, 110.0, n)
        self.assertTrue(np.array_equal((lon - 100.0) * M_PER_DEG_LON * np.cos(np.radians(lat)),
                                       (lon - 100.0) * 111320.0 * np.cos(np.radians(lat))))
        self.assertTrue(np.array_equal((lat - 8.0) * float(M_PER_DEG_LAT), (lat - 8.0) * 110574.0))
        # fit_footprints.metres_per_unit
        mx, my = metres_per_deg_np(lat)
        self.assertTrue(np.array_equal(mx * 1e-6, 111320 * np.cos(np.radians(lat)) * 1e-6))
        self.assertEqual(my * 1e-6, 110574 * 1e-6)
        # frontrow_heights.common_frame and housenumber_interp (scalar latitude through numpy)
        for x in LATS:
            self.assertEqual(metres_per_deg_np(x), (111320 * np.cos(np.radians(x)), 110574.0))
        # part_heights.footprint_bounds
        half = np.linspace(0.5, 60.0, n)
        self.assertTrue(np.array_equal(half / metres_per_deg_np(lat)[0], half / (111320 * np.cos(np.radians(lat)))))
        self.assertTrue(np.array_equal(half / M_PER_DEG_LAT, half / 110574))


def _old_floors_of(h):          # calibrate_heights.floors_of before the refactor
    x = (np.asarray(h, float) - 3.9) / 3.3
    return np.clip(1 + np.sign(x) * np.floor(np.abs(x) + 0.5), 1, 255).astype(np.uint8)


class FloorsTests(unittest.TestCase):
    def test_floors_of_equals_old_calibrate_formula(self):
        import calibrate_heights, common
        old = _old_floors_of(H_GRID)
        for f in (common.floors_of, calibrate_heights.floors_of):
            new = f(H_GRID)
            self.assertEqual(new.dtype, np.uint8)
            self.assertTrue(np.array_equal(new, old))
        self.assertEqual((common.LEVEL_M, common.FLOOR_BASE_M), (3.3, 3.9))

    def test_inline_half_up_variants_keep_their_constants(self):
        """part_heights.part_height and calibrate_heights.truth_frame keep their own half-up rule (not unified with
        floors_of, which rounds half away from zero); only the constants are shared."""
        from common import FLOOR_BASE_M, LEVEL_M
        r = np.r_[H_GRID, 150.0, 900.0, np.nan]
        self.assertTrue(np.array_equal(np.maximum(1, 1 + np.floor((r - FLOOR_BASE_M) / LEVEL_M + 0.5)),
                                       np.maximum(1, 1 + np.floor((r - 3.9) / 3.3 + 0.5)), equal_nan=True))
        for h in r[:-1]:
            self.assertEqual(max(1, 1 + int(np.floor((h - FLOOR_BASE_M) / LEVEL_M + 0.5))),
                             max(1, 1 + int(np.floor((h - 3.9) / 3.3 + 0.5))))

    def test_half_up_and_half_away_agree_after_the_clamp(self):
        """Why the two rules give the same floors on positive heights: they differ only at negative .5 points,
        where both clamp to 1 (recorded, not relied on)."""
        h = np.r_[H_GRID[H_GRID > 0], np.linspace(100.0, 900.0, 8001)]
        part = np.array([min(max(max(1, 1 + int(np.floor((x - 3.9) / 3.3 + 0.5))), 1), 255) for x in h])
        self.assertTrue(np.array_equal(part, _old_floors_of(h).astype(int)))

    def test_floors_of_matches_the_sql_rule_of_heights_25d(self):
        import duckdb
        q = """SELECT CAST(least(greatest(1, 1 + round((hm - 3.9) / 3.3)), 255) AS UTINYINT) f
               FROM (SELECT unnest(?::DOUBLE[]) hm, generate_subscripts(?::DOUBLE[], 1) i) ORDER BY i"""
        sql = duckdb.connect().execute(q, [H_GRID.tolist()] * 2).fetchnumpy()['f']
        self.assertTrue(np.array_equal(sql, _old_floors_of(H_GRID)))


class UtmTests(unittest.TestCase):
    def test_epsg_and_sql_fragments(self):
        import clip_to_roads, common, fit_footprints, merge_buildings
        self.assertEqual(common.UTM_EPSG, 32648)
        utm, back = "'EPSG:4326', 'EPSG:32648', always_xy := true", "'EPSG:32648', 'EPSG:4326', always_xy := true"
        self.assertEqual((common.UTM_SQL, common.UTM_BACK_SQL), (utm, back))
        self.assertEqual((clip_to_roads.UTM, clip_to_roads.BACK, merge_buildings.UTM, fit_footprints.UTM), (utm, back, utm, utm))


class _FakeCon:
    def __init__(self):
        self.sql = []

    def execute(self, q, *a):
        self.sql.append(q)
        return self


class DuckdbTests(unittest.TestCase):
    def test_in_memory_spatial(self):
        from common import spatial_connection
        con = spatial_connection()
        self.assertEqual(con.execute('SELECT ST_AsText(ST_Point(1, 2))').fetchone()[0], 'POINT (1 2)')
        self.assertFalse(con.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0])

    def test_statements_and_path(self):
        import duckdb
        from common import spatial_connection
        cases = [((), {}, "LOAD spatial; SET enable_progress_bar = false;"),
                 (('x.duckdb',), {'install': True}, "INSTALL spatial; LOAD spatial; SET enable_progress_bar = false;"),
                 ((), {'progress_bar': True}, "LOAD spatial;"),
                 ((), {'install': True, 'progress_bar': True}, "INSTALL spatial; LOAD spatial;")]
        for args, kw, stmt in cases:
            fake = _FakeCon()
            with mock.patch.object(duckdb, 'connect', return_value=fake) as m:
                self.assertIs(spatial_connection(*args, **kw), fake)
            m.assert_called_once_with(*args)
            self.assertEqual(fake.sql, [stmt])

    def test_build_areas_thread_patch_still_applies(self):
        import duckdb, pyarrow as pa
        import build_areas
        from common import spatial_connection
        cpu = pa.cpu_count()
        fake = _FakeCon()
        try:
            with mock.patch.object(duckdb, 'connect', return_value=fake) as m:
                build_areas.limit_threads(2)
                spatial_connection()
            m.assert_called_once_with(config={'threads': 2})
        finally:
            pa.set_cpu_count(cpu)


class ParquetTests(unittest.TestCase):
    def test_values_and_identical_bytes(self):
        import pyarrow as pa, pyarrow.parquet as pq
        from common import PARQUET_KW, ROW_GROUP, ZSTD_LEVEL
        self.assertEqual((ZSTD_LEVEL, ROW_GROUP), (19, 500_000))
        self.assertEqual(PARQUET_KW, {'compression': 'zstd', 'compression_level': 19, 'row_group_size': 500_000})
        t = pa.table({'a': np.arange(1000), 'b': np.linspace(0, 1, 1000)})
        with tempfile.TemporaryDirectory() as d:
            old, new = os.path.join(d, 'old.parquet'), os.path.join(d, 'new.parquet')
            pq.write_table(t, old, compression='zstd', compression_level=19, row_group_size=500_000)
            pq.write_table(t, new, **PARQUET_KW)
            self.assertEqual(Path(old).read_bytes(), Path(new).read_bytes())


class PathTests(unittest.TestCase):
    def test_constants_equal_the_old_expressions(self):
        import alpha_frontrow, calibrate_heights, common, extra_heights, frontrow_heights, housenumber_interp
        repo = os.path.dirname(os.path.dirname(os.path.abspath(str(B / 'calibrate_heights.py'))))
        self.assertEqual(common.REPO, repo)
        by_area, work = os.path.join(repo, 'buildings', 'by_area'), os.path.join(repo, 'data', 'buildings', '_calib')
        raw = os.path.join(repo, 'data', 'buildings', '_calib', 'raw_by_area')
        self.assertEqual(raw, os.path.join(work, 'raw_by_area'))
        self.assertEqual((common.BY_AREA, common.CALIB_DIR, common.RAW_DIR), (by_area, work, raw))
        self.assertEqual(common.INDEX_JSON, os.path.join(by_area, 'index.json'))
        self.assertEqual(common.COMMUNE_GROUPS, os.path.join(repo, 'buildings', 'commune_groups.json'))
        self.assertEqual((calibrate_heights.REPO, calibrate_heights.BY_AREA, calibrate_heights.WORK, calibrate_heights.RAW_DIR),
                         (repo, by_area, work, raw))
        self.assertEqual((alpha_frontrow.REPO, alpha_frontrow.PUB_DIR, alpha_frontrow.RAW_DIR, alpha_frontrow.GROUPS),
                         (repo, by_area, raw, common.COMMUNE_GROUPS))
        self.assertEqual((frontrow_heights.REPO, frontrow_heights.RAW_DIR), (repo, raw))
        self.assertEqual((housenumber_interp.REPO, housenumber_interp.PUB_DIR), (repo, by_area))
        self.assertEqual((extra_heights.REPO, extra_heights.RAW_DIR), (repo, raw))

    def test_files_exist_and_loaders(self):
        import common
        self.assertTrue(os.path.isfile(common.COMMUNE_GROUPS))
        self.assertTrue(os.path.isfile(common.INDEX_JSON))
        self.assertEqual(common.load_index(), json.load(open(common.INDEX_JSON, encoding='utf-8')))
        self.assertEqual(common.load_index(''), common.load_index())
        self.assertEqual(common.load_commune_groups(), json.load(open(common.COMMUNE_GROUPS, encoding='utf-8')))
        self.assertEqual(common.load_commune_groups(common.COMMUNE_GROUPS)['groups'][0], common.load_commune_groups()['groups'][0])


if __name__ == '__main__':
    unittest.main()
