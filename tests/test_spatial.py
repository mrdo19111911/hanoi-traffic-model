import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
from spatial import SpeedField, RoadSpeedModel, MissingRoadData


class SpatialTests(unittest.TestCase):
    def test_one_sample_is_constant_everywhere(self):
        for method in SpeedField.METHODS:
            field = SpeedField([(0, 0)], [40], method)
            self.assertEqual(field.predict([(10000, -10000)])[0], 40)

    def test_two_samples_distance_ratio(self):
        field = SpeedField([(0, 0), (100, 0)], [20, 60], "idw1")
        self.assertAlmostEqual(field.predict([(25, 0)])[0], 30)
        self.assertEqual(list(field.predict([(0, 0), (100, 0)])), [20, 60])

    def test_linear_triangle_recovers_affine_field(self):
        field = SpeedField([(0, 0), (100, 0), (0, 100)], [20, 40, 60], "linear_idw1")
        self.assertAlmostEqual(field.predict([(25, 25)])[0], 35)

    def test_outside_triangle_uses_idw_without_negative_speeds(self):
        points = [(0, 0), (100, 0), (0, 100)]
        values = [20, 40, 60]
        linear = SpeedField(points, values, "linear_idw2")
        idw = SpeedField(points, values, "idw2")
        self.assertAlmostEqual(linear.predict([(1000, 1000)])[0], idw.predict([(1000, 1000)])[0])

    def test_collinear_and_duplicate_samples(self):
        field = SpeedField([(0, 0), (0, 0), (100, 0)], [20, 40, 60], "linear_idw1")
        self.assertEqual(field.predict([(0, 0)])[0], 30)
        self.assertAlmostEqual(field.predict([(50, 0)])[0], 45)

    def test_road_classes_never_mix_even_at_identical_coordinates(self):
        model = RoadSpeedModel([
            {"lat": 21, "lon": 105.8, "road_class": "caotoc", "slot": "0800", "speed_kmh": 100},
            {"lat": 21, "lon": 105.8, "road_class": "nho", "slot": "0800", "speed_kmh": 10},
        ], "idw1")
        self.assertEqual(model.speed(21, 105.8, "caotoc", "0800"), 100)
        self.assertEqual(model.speed(21, 105.8, "nho", "0800"), 10)
        with self.assertRaises(MissingRoadData):
            model.speed(21, 105.8, "quoclo", "0800")

    def test_time_slots_never_mix_implicitly(self):
        model = RoadSpeedModel([
            {"lat": 21, "lon": 105.8, "road_class": "chinh", "slot": "0800", "speed_kmh": 20},
            {"lat": 21, "lon": 105.8, "road_class": "chinh", "slot": "NIGHT", "speed_kmh": 40},
        ], "idw1")
        self.assertEqual(model.speed(21, 105.8, "chinh", "0800"), 20)
        with self.assertRaises(MissingRoadData):
            model.speed(21, 105.8, "chinh", "1730")

    def test_invalid_samples_are_rejected(self):
        for speed in [0, -1, math.nan, math.inf]:
            with self.assertRaises(ValueError):
                SpeedField([(0, 0)], [speed], "idw1")


if __name__ == "__main__":
    unittest.main()
