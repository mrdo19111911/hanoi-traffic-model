import unittest

import numpy as np

from speedmatrix.geo import haversine_m, points_in_geometry


class GeoTests(unittest.TestCase):
    def test_haversine_one_degree_lat(self):
        self.assertAlmostEqual(float(haversine_m(21.0, 105.8, 22.0, 105.8)), 111195, delta=50)

    def test_polygon_with_hole_and_multipolygon(self):
        square = [[[105.0, 21.0], [106.0, 21.0], [106.0, 22.0], [105.0, 22.0], [105.0, 21.0]]]
        hole = [[105.4, 21.4], [105.6, 21.4], [105.6, 21.6], [105.4, 21.6], [105.4, 21.4]]
        lat, lon = np.array([21.5, 21.2, 23.0]), np.array([105.5, 105.2, 105.5])
        self.assertEqual(points_in_geometry(lat, lon, {"type": "Polygon", "coordinates": square + [hole]}).tolist(),
                         [False, True, False])
        second = [[[105.0, 22.5], [106.0, 22.5], [106.0, 23.5], [105.0, 23.5], [105.0, 22.5]]]
        multi = {"type": "MultiPolygon", "coordinates": [square, second]}
        self.assertEqual(points_in_geometry(lat, lon, multi).tolist(), [True, True, True])

    def test_other_geometry_type_raises(self):
        with self.assertRaises(ValueError):
            points_in_geometry(np.array([1.0]), np.array([1.0]), {"type": "Point", "coordinates": [1, 1]})


if __name__ == "__main__":
    unittest.main()
