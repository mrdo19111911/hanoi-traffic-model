"""Offline tests for assembling province (admin_level=4, ISO3166-2 VN-*) boundaries from an OSM file."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_big_roads import write_osm  # noqa: E402

# VN-01: a 0.1 degree square from four ways (502 reversed) with a 0.02 degree hole (inner way 505).
# VN-02: one closed way. VN-03: only an open ring. VN-04: a closed ring plus an open chain and a member way missing
# from the file (like Khanh Hoa's Truong Sa ways in the extract). LA-XA: a Lao province, ignored.
NODES = {1: (21.0, 105.0), 2: (21.0, 105.1), 3: (21.1, 105.1), 4: (21.1, 105.0),
         5: (21.04, 105.04), 6: (21.04, 105.06), 7: (21.06, 105.06), 8: (21.06, 105.04),
         21: (21.2, 105.0), 22: (21.2, 105.1), 23: (21.3, 105.1), 24: (21.3, 105.0),
         31: (21.4, 105.0), 32: (21.4, 105.1), 33: (21.5, 105.1),
         41: (21.6, 105.0), 42: (21.6, 105.1), 43: (21.7, 105.1),
         51: (21.8, 105.0), 52: (21.8, 105.1), 53: (21.9, 105.1), 54: (21.8, 106.0), 55: (21.9, 106.0)}
ADMIN = {"boundary": "administrative", "admin_level": "4"}
WAYS = [(501, [1, 2], ADMIN), (502, [3, 2], ADMIN), (503, [3, 4], ADMIN), (504, [4, 1], ADMIN), (505, [5, 6, 7, 8, 5], {}),
        (511, [21, 22, 23, 24, 21], ADMIN), (521, [31, 32], ADMIN), (522, [32, 33], ADMIN), (531, [41, 42, 43, 41], ADMIN),
        (541, [51, 52, 53, 51], ADMIN), (542, [54, 55], ADMIN)]


def relation(relation_id, members, iso, name, admin_level="4"):
    return (relation_id, members, {"type": "boundary", "boundary": "administrative", "admin_level": admin_level,
                                   "ISO3166-2": iso, "name": name})


RELATIONS = [relation(1, [501, 502, (503, "outer"), 504, (505, "inner"), (21, "admin_centre", "node")], "VN-01", "Thành phố Thử"),
             relation(2, [511], "VN-02", "Tỉnh Hai"),
             relation(3, [521, 522], "VN-03", "Tỉnh Hở"),
             relation(4, [531], "LA-XA", "Xaignabouli"),
             relation(5, [541, 542, 999], "VN-04", "Tỉnh Đảo")]


class BuildProvincesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from build_provinces import build
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        write_osm(root / "provinces.osm", NODES, WAYS, RELATIONS)
        (root / "out").mkdir()
        cls.result = build(root / "provinces.osm", root / "out/vietnam_provinces.geojson")
        cls.data = json.loads((root / "out/vietnam_provinces.geojson").read_text(encoding="utf-8"))
        cls.by_iso = {f["properties"]["iso"]: f for f in cls.data["features"]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_only_vietnamese_provinces_are_kept(self):
        self.assertEqual(self.data["type"], "FeatureCollection")
        self.assertEqual(sorted(self.by_iso), ["VN-01", "VN-02", "VN-04"])
        self.assertEqual(self.data, self.result)

    def test_ring_is_joined_from_reversed_ways_with_the_inner_ring_as_a_hole(self):
        feature = self.by_iso["VN-01"]
        self.assertEqual(feature["geometry"]["type"], "MultiPolygon")
        (outer, hole), = feature["geometry"]["coordinates"]
        self.assertEqual({tuple(p) for p in outer}, {(105.0, 21.0), (105.1, 21.0), (105.1, 21.1), (105.0, 21.1)})
        self.assertEqual({tuple(p) for p in hole}, {(105.04, 21.04), (105.06, 21.04), (105.06, 21.06), (105.04, 21.06)})
        self.assertEqual((outer[0], hole[0]), (outer[-1], hole[-1]))
        props = feature["properties"]
        self.assertEqual((props["name"], props["relation_id"], props["points"]), ("Thành phố Thử", 1, 10))
        # 10.39 km x 11.13 km minus a 2.08 km x 2.23 km hole.
        self.assertAlmostEqual(props["area_km2"], 115.6 - 4.6, delta=1.0)

    def test_province_with_an_open_ring_is_reported_and_the_others_written(self):
        failed, = self.data["failed"]
        self.assertEqual((failed["iso"], failed["name"]), ("VN-03", "Tỉnh Hở"))
        self.assertTrue(failed["reason"])
        self.assertEqual(len(self.by_iso["VN-02"]["geometry"]["coordinates"]), 1)

    def test_open_chains_beside_a_closed_ring_are_dropped_and_reported(self):
        feature = self.by_iso["VN-04"]
        (ring,), = feature["geometry"]["coordinates"]
        self.assertEqual(len(ring), 4)
        partial, = self.data["partial"]
        self.assertEqual((partial["iso"], partial["name"]), ("VN-04", "Tỉnh Đảo"))
        self.assertIn("1 open", partial["reason"])
        self.assertIn("1 member ways missing", partial["reason"])
        self.assertNotIn("VN-04", [item["iso"] for item in self.data["failed"]])

    def test_file_records_the_map_and_policy(self):
        from osm_classes import BIG_ROAD_POLICY_VERSION
        self.assertEqual(len(self.data["pbf_sha256"]), 64)
        self.assertEqual(self.data["policy_version"], BIG_ROAD_POLICY_VERSION)
        self.assertEqual(sorted(p.name for p in (Path(self.temp.name) / "out").iterdir()), ["vietnam_provinces.geojson"])

    def test_no_assembled_province_writes_nothing(self):
        from build_provinces import build
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_osm(root / "open.osm", NODES, WAYS, [RELATIONS[2]])
            (root / "vietnam_provinces.geojson").write_text('{"old": true}', encoding="utf-8")
            with self.assertRaises(ValueError):
                build(root / "open.osm", root / "vietnam_provinces.geojson")
            self.assertEqual(json.loads((root / "vietnam_provinces.geojson").read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["open.osm", "vietnam_provinces.geojson"])


if __name__ == "__main__":
    unittest.main()
