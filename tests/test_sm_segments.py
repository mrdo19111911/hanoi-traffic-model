"""Offline tests for the speedmatrix segment table, admin areas and water lines."""
from pathlib import Path
import tempfile
import unittest

import numpy as np

from speedmatrix.geo import haversine_m, points_in_geometry
from speedmatrix.segments import (HGV_VALUES, ROAD_CLASSES, build_admin, build_segments, build_water_lines,
                                  find_segments, load_admin, load_segments, load_water_lines, parse_metres,
                                  parse_tonnes, save_admin, save_segments, save_water_lines)

LAT_IN, LON_IN = 21.0015, 105.8015
NODES = {11: (21.001, 105.801), 12: (21.001, 105.802), 13: (21.001, 105.803),
         21: (21.002, 105.801), 22: (21.002, 105.802),
         31: (21.003, 105.801), 32: (21.003, 105.802),
         41: (21.004, 105.801), 42: (21.004, 105.802),
         51: (21.005, 105.801), 52: (21.005, 105.802),
         61: (22.5, 107.5), 62: (22.5, 107.501),
         91: (21.0, 105.8), 92: (21.0, 105.803), 93: (21.003, 105.803), 94: (21.003, 105.8)}
WAYS = [(1, [11, 12, 13], {"highway": "primary", "oneway": "yes", "maxweight": "10 t", "name": "Phố Huế"}),
        (2, [21, 22], {"highway": "residential", "maxweight": "8000 kg", "maxheight": "4.2 m", "hgv": "no",
                       "hgv:conditional": "yes @ (21:00-06:00)"}),
        (3, [31, 32], {"highway": "footway"}),
        (4, [41, 42], {"route": "ferry"}),
        (5, [51, 52], {"highway": "service", "maxweight": "default"}),
        (6, [61, 62], {"highway": "primary"}),
        (90, [91, 92, 93, 94, 91], {"boundary": "administrative"})]
RELATIONS = [(1, [90], {"type": "boundary", "boundary": "administrative", "admin_level": "6",
                        "name": "Phường Thử"})]
BBOX = (105.7, 20.9, 106.0, 21.1)


def write_osm(path, nodes, ways, relations=()):
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<osm version='0.6'>"]
    for i, (lat, lon) in nodes.items():
        lines.append(f"<node id='{i}' version='1' lat='{lat}' lon='{lon}'></node>")
    for way_id, refs, tags in ways:
        lines.append(f"<way id='{way_id}' version='1'>")
        lines += [f"<nd ref='{n}'/>" for n in refs]
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</way>")
    for relation_id, members, tags in relations:
        lines.append(f"<relation id='{relation_id}' version='1'>")
        lines += [f"<member type='way' ref='{m}' role='outer'/>" for m in members]
        lines += [f"<tag k='{k}' v='{v}'/>" for k, v in tags.items()]
        lines.append("</relation>")
    lines.append("</osm>")
    path.write_text("\n".join(lines), encoding="utf-8")


class SegmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.osm = cls.root / "tiny.osm"
        write_osm(cls.osm, NODES, WAYS, RELATIONS)
        cls.table = build_segments(cls.osm, BBOX)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_kept_ways_classes_and_ferry(self):
        t = self.table
        self.assertEqual(sorted(t.way_id.tolist()), [1, 2, 4, 5])
        cls = {int(w): ROAD_CLASSES[c] for w, c in zip(t.way_id, t.road_class)}
        self.assertEqual(cls, {1: "noido", 2: "noido", 4: "pha", 5: "nho"})
        self.assertEqual(t.n, 5)  # way 1 has two segments, the others one each

    def test_one_row_per_adjacent_pair_with_length(self):
        i = find_segments(self.table, np.array([[1, 11, 12], [1, 12, 11], [1, 11, 13]]))
        self.assertEqual(i[0], i[1])
        self.assertEqual(i[2], -1)
        expected = float(haversine_m(*NODES[11], *NODES[12]))
        self.assertAlmostEqual(float(self.table.length_m[i[0]]), expected, delta=0.5)

    def test_weight_and_height_parsing(self):
        self.assertEqual(parse_tonnes("10"), 10.0)
        self.assertEqual(parse_tonnes("10 t"), 10.0)
        self.assertEqual(parse_tonnes("5.5t"), 5.5)
        self.assertEqual(parse_tonnes("8000 kg"), 8.0)
        for bad in ("default", "", "0", "-3", "10 st"):
            self.assertIsNone(parse_tonnes(bad))
        self.assertEqual(parse_metres("4.2 m"), 4.2)
        self.assertIsNone(parse_metres("4'2\""))
        self.assertEqual(self.table.meta["unparsed"]["maxweight"], 1)

    def test_oneway_and_tags_kept(self):
        t = self.table
        w = {int(x): k for k, x in enumerate(t.way_id)}
        self.assertEqual(int(t.oneway[w[1]]), 1)
        self.assertEqual(int(t.oneway[w[2]]), 0)
        self.assertEqual(HGV_VALUES[t.hgv[w[2]]], "no")
        self.assertEqual(t.strings[t.cond_hgv[w[2]]], "yes @ (21:00-06:00)")
        self.assertTrue(np.isnan(t.maxweight_t[w[5]]))
        self.assertEqual(float(t.maxweight_t[w[1]]), 10.0)
        self.assertAlmostEqual(float(t.maxheight_m[w[2]]), 4.2, places=5)
        self.assertEqual(t.strings[t.name[w[1]]], "Phố Huế")
        self.assertEqual(int(t.ref[w[1]]), -1)
        self.assertEqual(t.strings[t.highway[w[4]]], "ferry")
        self.assertEqual(len(t.segment_class), t.n)

    def test_admin_areas_from_relations(self):
        feats = build_admin(self.osm)
        self.assertEqual([(f["properties"]["name"], f["properties"]["admin_level"]) for f in feats],
                         [("Phường Thử", 6)])
        inside = points_in_geometry(np.array([LAT_IN]), np.array([LON_IN]), feats[0]["geometry"])
        self.assertEqual(inside.tolist(), [True])
        save_admin(feats, self.root / "adm")
        self.assertEqual(load_admin(self.root / "adm"), feats)

    def test_bbox_and_round_trip(self):
        self.assertNotIn(6, self.table.way_id.tolist())
        save_segments(self.table, self.root / "seg")
        again = load_segments(self.root / "seg")
        self.assertEqual(again.sha256, self.table.sha256)
        np.testing.assert_array_equal(again.a, self.table.a)
        self.assertEqual(again.strings, self.table.strings)
        self.assertEqual(again.meta["counts"], self.table.meta["counts"])
        self.assertFalse(any(p.name.endswith(".tmp") for p in self.root.iterdir()))
        full = build_segments(self.osm, None)
        self.assertIn(6, full.way_id.tolist())


class WaterTests(unittest.TestCase):
    def test_water_lines_named_rivers_and_coastline(self):
        nodes = {1: (21.0, 105.8), 2: (21.0, 105.81), 3: (21.1, 105.8), 4: (21.1, 105.81), 5: (21.2, 105.8),
                 6: (21.2, 105.81), 7: (22.5, 107.5), 8: (22.5, 107.51)}
        ways = [(1, [1, 2], {"waterway": "river", "name": "Sông Thử"}),
                (2, [3, 4], {"natural": "coastline"}),
                (3, [5, 6], {"waterway": "river"}),
                (4, [7, 8], {"waterway": "canal", "name": "Kênh Xa"})]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_osm(root / "w.osm", nodes, ways)
            lines = build_water_lines(root / "w.osm", (105.7, 20.9, 106.0, 21.5))
            names = sorted(lines.strings[i] for i in lines.name)
            self.assertEqual(names, ["Sông Thử", "coastline"])
            self.assertEqual(len(lines.a_lat), 2)
            save_water_lines(lines, root / "out")
            again = load_water_lines(root / "out")
            self.assertEqual(again.strings, lines.strings)
            np.testing.assert_array_equal(again.a_lat, lines.a_lat)
            np.testing.assert_array_equal(again.name, lines.name)
            self.assertFalse(any(p.name.endswith(".tmp") for p in root.iterdir()))


if __name__ == "__main__":
    unittest.main()
