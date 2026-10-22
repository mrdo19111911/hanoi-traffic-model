"""Deterministic estimator tests: no network services or API credentials."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))
from estimate_spatial import (SpatialEstimator, MissingRoadData, RoadClassError,
                              RouteAlignmentError, SpatialDataError)


class EstimatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "osrm/data").mkdir(parents=True)
        (self.root / "osrm/build.json").write_text(json.dumps({"input": {"pbf_sha256": "map-a"}}))
        self.index = self.root / "osrm/data/road_classes.sqlite"
        with self.db() as conn:
            conn.executescript("CREATE TABLE edges(a INTEGER,b INTEGER,way_id INTEGER,road_class TEXT,highway TEXT,name TEXT,ref TEXT, PRIMARY KEY(a,b,way_id)); CREATE TABLE metadata(value TEXT);")
            conn.execute("INSERT INTO metadata VALUES (?)", (json.dumps({"pbf_sha256": "map-a", "class_quality": "osm_tag_policy"}),))
            conn.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?)", [(10,20,1,"caotoc","motorway","bridge",None), (20,30,2,"nho","residential","underpass",None)])
        self.route = {"code": "Ok", "routes": [{"geometry": {"type": "LineString", "coordinates": [[105.8,21],[105.81,21],[105.82,21]]}, "legs": [{"annotation": {"nodes": [10,20,30], "distance": [1000,1000], "duration": [1,1]}}]}]}
        self.samples = [self.sample("caotoc",100), self.sample("nho",10)]

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.index)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def sample(cls, speed, slot="0800", day=1):
        return {"lat": 21, "lon": 105.8, "road_class": cls, "speed_kmh": speed,
                "slot": slot, "day": day, "location_quality": "route_point",
                "class_quality": "osm_tag_policy"}

    def estimator(self, samples=None, route=None):
        return SpatialEstimator(root=self.root, samples=self.samples if samples is None else samples,
                                route_provider=lambda *args: self.route if route is None else route)

    def estimate(self, estimator=None, **kwargs):
        params = dict(lat1=21,lon1=105.8,lat2=21,lon2=105.82,day=1,hour=8,method="idw1")
        params.update(kwargs)
        return (estimator or self.estimator()).estimate(**params)

    def test_crossing_road_classes_stay_separate_and_time_uses_units(self):
        result = self.estimate()
        self.assertEqual([r["road_class"] for r in result["segments"]], ["caotoc","nho"])
        self.assertEqual([r["speed_kmh"] for r in result["segments"]], [100,10])
        self.assertEqual(result["km"], 2)
        self.assertAlmostEqual(result["minutes"], 6.6)
        self.assertAlmostEqual(sum(r["minutes"] for r in result["segments"]), result["minutes"])
        self.assertAlmostEqual(result["by_class"]["caotoc"]["minutes"], .6)

    def test_missing_class_does_not_use_nearby_other_class(self):
        with self.assertRaisesRegex(MissingRoadData,"nho"):
            self.estimate(self.estimator(samples=self.samples[:1]))

    def test_exact_osm_node_pair_works_in_reverse_direction(self):
        route = copy.deepcopy(self.route)
        route["routes"][0]["legs"][0]["annotation"]["nodes"] = [30,20,10]
        self.assertEqual([s["road_class"] for s in self.estimate(self.estimator(route=route))["segments"]], ["nho","caotoc"])

    def test_unknown_osm_edge_is_actionable_error(self):
        route = copy.deepcopy(self.route)
        route["routes"][0]["legs"][0]["annotation"]["nodes"][0] = 999
        with self.assertRaisesRegex(RoadClassError,"999"):
            self.estimate(self.estimator(route=route))

    def test_ambiguous_class_pair_is_rejected(self):
        with self.db() as conn:
            conn.execute("INSERT INTO edges VALUES (10,20,99,'nho','residential','conflict',NULL)")
        with self.assertRaisesRegex(RoadClassError,"Ambiguous"):
            self.estimate()

    def test_parallel_ways_with_same_class_are_acceptable(self):
        with self.db() as conn:
            conn.execute("INSERT INTO edges VALUES (10,20,99,'caotoc','motorway','parallel',NULL)")
        self.assertEqual(self.estimate()["segments"][0]["road_class"], "caotoc")

    def test_mismatched_geometry_annotations_fail_instead_of_guessing(self):
        route = copy.deepcopy(self.route)
        route["routes"][0]["geometry"]["coordinates"].insert(1,[105.805,21])
        with self.assertRaises(RouteAlignmentError):
            self.estimate(self.estimator(route=route))

    def test_pbf_fingerprint_must_match(self):
        (self.root / "osrm/build.json").write_text(json.dumps({"input": {"pbf_sha256": "different-map"}}))
        with self.assertRaisesRegex(SpatialDataError,"fingerprint"):
            self.estimate()

    def test_time_interpolation_and_midnight_wrap(self):
        samples = [self.sample(c,v,slot) for c in ["caotoc","nho"] for slot,v in [("2300",20),("0100",40)]]
        result = self.estimate(self.estimator(samples=samples),hour=0)
        self.assertAlmostEqual(result["segments"][0]["speed_kmh"],30)

    def test_other_day_reuse_is_explicit_and_class_preserving(self):
        samples = [self.sample(c,v,slot) for c in ["caotoc","nho"] for slot,v in [("0300",60),("0800",30)]]
        result = self.estimate(self.estimator(samples=samples),day=6)
        self.assertAlmostEqual(result["segments"][0]["speed_kmh"],60/1.36)
        self.assertTrue(any("Tuesday" in warning for warning in result["warnings"]))

    def test_missing_day_without_tuesday_has_no_silent_fallback(self):
        with self.assertRaises(MissingRoadData):
            self.estimate(self.estimator(samples=[self.sample("caotoc",40,day=2)]),day=0)

    def test_invalid_request_rejected_before_route_provider(self):
        for changes in [{"day":7},{"day":True},{"hour":24},{"hour":float("nan")},{"lat1":float("nan")},{"lon1":181},{"method":"magic"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.estimate(**changes)

    def test_empty_osrm_response_is_actionable(self):
        with self.assertRaisesRegex(RouteAlignmentError,"route"):
            self.estimate(self.estimator(route={"code":"NoRoute","routes":[]}))

    def test_zero_distance_edges_do_not_require_speed_or_class(self):
        route = copy.deepcopy(self.route)
        annotation = route["routes"][0]["legs"][0]["annotation"]
        annotation["nodes"][0] = 999
        annotation["distance"][0] = 0
        self.assertAlmostEqual(self.estimate(self.estimator(route=route))["minutes"],6)

    def test_route_entry_time_moves_to_next_day_after_midnight(self):
        samples = [self.sample("caotoc",10,day=1), self.sample("nho",10,day=1),
                   self.sample("nho",20,day=2)]
        result = self.estimate(self.estimator(samples=samples),hour=23.95)
        self.assertEqual([s["speed_kmh"] for s in result["segments"]],[10,20])
        self.assertAlmostEqual(result["minutes"],9)

    def test_multi_leg_routes_join_one_shared_annotation_node(self):
        route = copy.deepcopy(self.route)
        route["routes"][0]["legs"] = [
            {"annotation":{"nodes":[10,20],"distance":[1000]}},
            {"annotation":{"nodes":[20,30],"distance":[1000]}}]
        self.assertAlmostEqual(self.estimate(self.estimator(route=route))["minutes"],6.6)

    def test_disconnected_leg_annotations_are_rejected(self):
        route = copy.deepcopy(self.route)
        route["routes"][0]["legs"] = [
            {"annotation":{"nodes":[10,20],"distance":[1000]}},
            {"annotation":{"nodes":[40,30],"distance":[1000]}}]
        with self.assertRaises(RouteAlignmentError):
            self.estimate(self.estimator(route=route))

    def test_method_reaches_triangle_spatial_interpolation(self):
        samples = [self.sample("caotoc",20), self.sample("caotoc",60),
                   self.sample("caotoc",100),self.sample("nho",10)]
        samples[1]["lon"] = 105.82
        samples[2]["lat"] = 21.02
        result = self.estimate(self.estimator(samples=samples),method="linear_idw1")
        self.assertAlmostEqual(result["segments"][0]["speed_kmh"],30)

    def test_missing_night_baseline_exposes_uncorrected_day_assumption(self):
        result = self.estimate(day=6)
        self.assertTrue(any("without a weekday correction" in message for message in result["warnings"]))

    def test_sample_file_refreshes_when_new_observations_arrive(self):
        path = self.root / "data/spatial_samples.jsonl"
        path.parent.mkdir()
        path.write_text("\n".join(json.dumps(r) for r in self.samples))
        estimator = SpatialEstimator(root=self.root,route_provider=lambda *args:self.route)
        self.assertAlmostEqual(self.estimate(estimator)["minutes"],6.6)
        more = self.samples + [self.sample("nho",30)]
        path.write_text("\n".join(json.dumps(r) for r in more))
        self.assertAlmostEqual(self.estimate(estimator)["minutes"],3.6)

    def test_same_estimator_can_serve_different_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        estimator = self.estimator()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _:self.estimate(estimator),range(6)))
        self.assertEqual([r["minutes"] for r in results],[6.6]*6)


class PackageImportTests(unittest.TestCase):
    def test_backend_style_import_falls_back_to_legacy_samples(self):
        # A fresh interpreter keeps model/ off sys.path, as when the backend imports model.estimate_spatial.
        import subprocess
        code = ("import sys; from model.estimate_spatial import SpatialEstimator; "
                "e = SpatialEstimator(root=sys.argv[1]); e._load_samples(); print(e._source, len(e._rows))")
        with tempfile.TemporaryDirectory() as empty_root:
            completed = subprocess.run([sys.executable, "-c", code, empty_root], cwd=Path(__file__).resolve().parents[1],
                                       capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        source, count = completed.stdout.split()
        self.assertEqual(source, "legacy_exploratory")
        self.assertGreater(int(count), 0)


if __name__ == "__main__":
    unittest.main()
