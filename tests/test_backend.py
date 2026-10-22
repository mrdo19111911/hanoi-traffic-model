"""Offline contract and safety tests for the local API."""
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from backend.app import ESTIMATE_ERRORS, create_app
from model.osm_classes import BIG_ROAD_POLICY_VERSION


class FakeEstimator:
    def estimate(self, lat1, lon1, lat2, lon2, day, hour, method):
        return {"minutes": 12.0, "km": 5.0, "geometry": {"type": "LineString", "coordinates": [[lon1, lat1], [lon2, lat2]]}, "segments": [], "by_class": {}, "warnings": [], "method": method, "data_quality": {}}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data").mkdir()
        self.app = create_app(root=self.root, estimator_factory=lambda: FakeEstimator(), osrm_probe=lambda: True, key_reader=lambda: "test-key")
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def request_body(self):
        return {"origin": {"lat": 21.02, "lon": 105.85}, "destination": {"lat": 21.22, "lon": 105.81}, "day": 1, "hour": 8, "method": "idw1"}

    def test_health_never_exposes_key(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["api_key_configured"])
        self.assertTrue(response.json()["osrm_ready"])
        self.assertNotIn("test-key", response.text)

    def test_options_and_exact_estimate_contract(self):
        self.assertEqual(len(self.client.get("/api/options").json()["road_classes"]), 5)
        response = self.client.post("/api/estimate", json=self.request_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["method"], "idw1")
        self.assertEqual(response.json()["geometry"]["coordinates"][0], [105.85, 21.02])

    def test_invalid_requests_do_not_run_estimator(self):
        for field, value in [("day", 7), ("hour", 24.1), ("method", "invented")]:
            body = self.request_body()
            body[field] = value
            self.assertEqual(self.client.post("/api/estimate", json=body).status_code, 422)
        body = self.request_body()
        body["origin"]["lat"] = 99
        self.assertEqual(self.client.post("/api/estimate", json=body).status_code, 422)

    def test_data_counts_only_valid_samples_and_reports_corruption(self):
        rows = [{"lat": 21.0, "lon": 105.8, "speed_kmh": 40, "road_class": "caotoc", "day": 1, "slot": "0800"}, {"lat": 21.1, "lon": 105.9, "speed_kmh": 20, "road_class": "noido", "day": 1, "slot": "0800"}]
        (self.root / "data/spatial_samples.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n{broken\n", encoding="utf-8")
        status = self.client.get("/api/data/status").json()
        self.assertEqual(status["sample_count"], 2)
        self.assertEqual(status["by_class"]["caotoc"], 1)
        self.assertEqual(status["by_slot"]["0800"], 2)
        self.assertEqual(status["quality"]["invalid_rows"], 1)
        self.assertTrue(self.client.get("/api/health").json()["samples_ready"])

    def test_missing_report_and_unknown_api_routes_are_json(self):
        response = self.client.get("/api/experiments")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/unknown").headers["content-type"], "application/json")

    def test_report_is_served(self):
        (self.root / "reports").mkdir()
        (self.root / "reports/spatial_comparison.json").write_text('{"status":"exploratory_only"}', encoding="utf-8")
        self.assertEqual(self.client.get("/api/experiments").json()["status"], "exploratory_only")

    def test_crawl_validation_and_missing_key(self):
        body = {"date": "2026-10-20", "slots": ["0800"], "max_calls": 2}
        for updates in [{"slots": ["2460"]}, {"slots": []}, {"max_calls": 61}, {"date": "bad"}]:
            self.assertEqual(self.client.post("/api/crawl", json={**body, **updates}).status_code, 422)
        client = TestClient(create_app(root=self.root, key_reader=lambda: None))
        self.assertEqual(client.post("/api/crawl", json=body).status_code, 503)
        client.close()

    def test_one_crawl_at_a_time_and_job_completion(self):
        started, release = threading.Event(), threading.Event()
        def runner(kind, payload):
            started.set()
            release.wait(3)
            return {"sample_count": 3}
        client = TestClient(create_app(root=self.root, key_reader=lambda: "secret", job_runner=runner))
        try:
            body = {"date": "2026-10-20", "slots": ["0800"], "max_calls": 2}
            response = client.post("/api/crawl", json=body)
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["id"]
            self.assertTrue(started.wait(1))
            self.assertEqual(client.post("/api/crawl", json=body).status_code, 409)
            release.set()
            for _ in range(100):
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(.01)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["result"]["sample_count"], 3)
            self.assertEqual(client.get("/api/jobs/no-such-job").status_code, 404)
        finally:
            release.set()
            client.close()

    def test_estimator_exception_secrets_do_not_escape(self):
        class Broken:
            def estimate(self, *args):
                raise RuntimeError("https://example.com/?key=very-secret")
        client = TestClient(create_app(root=self.root, estimator_factory=Broken))
        response = client.post("/api/estimate", json=self.request_body())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("very-secret", response.text)
        client.close()

    def test_background_failure_never_returns_worker_exception(self):
        def broken(kind, payload):
            raise RuntimeError("TomTom URL?key=very-secret")
        client = TestClient(create_app(root=self.root, job_runner=broken))
        try:
            response = client.post("/api/experiments/run")
            self.assertEqual(response.status_code, 202)
            for _ in range(100):
                job = client.get("/api/jobs/" + response.json()["id"])
                if job.json()["status"] == "failed":
                    break
                time.sleep(.01)
            self.assertEqual(job.json()["status"], "failed")
            self.assertNotIn("very-secret", job.text)
        finally:
            client.close()

    def test_estimator_missing_road_class_has_actionable_error(self):
        class MissingRoadData(ValueError):
            pass
        class Missing:
            def estimate(self, *args):
                raise MissingRoadData("untrusted details")
        client = TestClient(create_app(root=self.root, estimator_factory=Missing))
        response = client.post("/api/estimate", json=self.request_body())
        self.assertEqual(response.status_code, 422)
        self.assertIn("đúng loại đường", response.json()["detail"])
        client.close()

    def test_frontend_and_api_fallback_are_separate(self):
        dist = self.root / "frontend/dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html>Hanoi</html>", encoding="utf-8")
        client = TestClient(create_app(root=self.root))
        self.assertIn("Hanoi", client.get("/").text)
        self.assertEqual(client.get("/api/missing").status_code, 404)
        self.assertIn("application/json", client.get("/api/missing").headers["content-type"])
        self.assertNotIn("Hanoi", client.get("/../../README.md").text)
        client.close()


class BigRoadApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "osrm/data"
        self.data.mkdir(parents=True)
        (self.root / "osrm/build.json").write_text(json.dumps({"input": {"pbf_sha256": "map-a"}}), encoding="utf-8")
        self.client = TestClient(create_app(root=self.root, osrm_probe=lambda: True, key_reader=lambda: None))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def write_build(self, sha="map-a", policy=BIG_ROAD_POLICY_VERSION):
        roads = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "LineString",
                 "coordinates": [[105.8 + i / 1000, 21.0], [105.801 + i / 1000, 21.0]]}, "properties": {"way_id": i}} for i in range(50)]}
        (self.data / "big_roads.geojson").write_text(json.dumps(roads), encoding="utf-8")
        (self.data / "big_road_junctions.geojson").write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        (self.data / "big_roads.meta.json").write_text(json.dumps({"pbf_sha256": sha, "policy_version": policy, "ways": 50}), encoding="utf-8")
        return roads

    def test_big_road_files_are_served_with_gzip(self):
        roads = self.write_build()
        response = self.client.get("/api/big-roads", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("content-encoding"), "gzip")
        self.assertEqual(response.json(), roads)
        self.assertEqual(self.client.get("/api/big-roads/junctions").json()["features"], [])
        self.assertEqual(self.client.get("/api/big-roads/meta").json()["ways"], 50)

    def test_land_polygon_is_served_when_built(self):
        self.assertEqual(self.client.get("/api/big-roads/land").status_code, 404)
        self.assertIn("Chưa dựng bản đồ đường lớn", self.client.get("/api/big-roads/land").json()["detail"])
        (self.data / "vietnam_land.geojson").write_text('{"type":"Feature","geometry":{"type":"Polygon","coordinates":[]},"properties":{"points":0}}', encoding="utf-8")
        self.assertEqual(self.client.get("/api/big-roads/land").json()["geometry"]["type"], "Polygon")

    def test_missing_build_has_actionable_vietnamese_error(self):
        for path in ["/api/big-roads", "/api/big-roads/junctions", "/api/big-roads/meta"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertIn("Chưa dựng bản đồ đường lớn", response.json()["detail"])

    def test_health_reports_whether_big_roads_match_the_map(self):
        self.assertFalse(self.client.get("/api/health").json()["big_roads_current"])
        self.write_build()
        self.assertTrue(self.client.get("/api/health").json()["big_roads_current"])
        self.write_build(sha="map-old")
        self.assertFalse(self.client.get("/api/health").json()["big_roads_current"])
        self.write_build(policy=BIG_ROAD_POLICY_VERSION - 1)
        self.assertFalse(self.client.get("/api/health").json()["big_roads_current"])

    def test_boundary_polygon_is_served_when_built(self):
        response = self.client.get("/api/big-roads/boundary")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Chưa dựng bản đồ đường lớn", response.json()["detail"])
        boundary = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []},
                    "properties": {"source": "osm_admin_level_2", "tolerance_m": 100, "points": 0}}
        (self.data / "vietnam_boundary.geojson").write_text(json.dumps(boundary), encoding="utf-8")
        response = self.client.get("/api/big-roads/boundary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), boundary)

    def test_provinces_are_served_when_built(self):
        response = self.client.get("/api/big-roads/provinces")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Chưa dựng bản đồ đường lớn", response.json()["detail"])
        provinces = {"type": "FeatureCollection", "pbf_sha256": "map-a", "policy_version": BIG_ROAD_POLICY_VERSION, "failed": [],
                     "features": [{"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []},
                                   "properties": {"iso": "VN-HN", "name": "Thành phố Hà Nội", "relation_id": 1, "area_km2": 0, "points": 0}}]}
        (self.data / "vietnam_provinces.geojson").write_text(json.dumps(provinces, ensure_ascii=False), encoding="utf-8")
        response = self.client.get("/api/big-roads/provinces")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), provinces)

    def test_clusters_are_served_when_present(self):
        response = self.client.get("/api/big-roads/clusters")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Chưa có dữ liệu cụm.")
        clusters = {"method": "regions", "created_at": "2026-10-20T00:00:00+00:00", "partitions": [{"method": "natural", "k": 2, "ways": {"1": 0, "2": 1}}]}
        (self.data / "big_road_clusters.json").write_text(json.dumps(clusters), encoding="utf-8")
        response = self.client.get("/api/big-roads/clusters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), clusters)


class BigRoadReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = TestClient(create_app(root=self.root, osrm_probe=lambda: True, key_reader=lambda: None))
        self.path = self.root / "data/big_road_review.json"

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_review_is_empty_without_a_file(self):
        response = self.client.get("/api/big-roads/review")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": {}})
        self.assertFalse(self.path.exists())

    def test_put_stores_item_and_get_returns_it(self):
        response = self.client.put("/api/big-roads/review/123456", json={"status": "seen", "note": "Cầu đang xây"})
        self.assertEqual(response.status_code, 200)
        item = response.json()
        self.assertEqual(item["status"], "seen")
        self.assertEqual(item["note"], "Cầu đang xây")
        stamp = datetime.fromisoformat(item["updated_at"])
        self.assertEqual(stamp.utcoffset(), timedelta(0))
        self.client.put("/api/big-roads/review/ferry:42", json={"status": "skipped"})
        items = self.client.get("/api/big-roads/review").json()["items"]
        self.assertEqual(items["123456"], item)
        self.assertEqual(items["ferry:42"]["status"], "skipped")
        self.assertEqual(items["ferry:42"]["note"], "")
        # Overwriting a key replaces only that entry.
        self.client.put("/api/big-roads/review/123456", json={"status": "unseen", "note": ""})
        items = self.client.get("/api/big-roads/review").json()["items"]
        self.assertEqual(items["123456"]["status"], "unseen")
        self.assertEqual(set(items), {"123456", "ferry:42"})

    def test_put_rejects_invalid_status_note_and_key(self):
        for key, body in [("1", {"status": "done"}), ("1", {"status": "đã xem"}), ("1", {}),
                          ("1", {"status": "seen", "note": "x" * 501}), ("1", {"status": "seen", "note": 5}),
                          ("abc", {"status": "seen"}), ("ferry:", {"status": "seen"}), ("way:1", {"status": "seen"}),
                          ("-1", {"status": "seen"}), ("1", {"status": "seen", "extra": 1})]:
            with self.subTest(key=key, body=body):
                self.assertEqual(self.client.put(f"/api/big-roads/review/{key}", json=body).status_code, 422)
        self.assertEqual(self.client.put("/api/big-roads/review/1", json={"status": "seen", "note": "x" * 500}).status_code, 200)
        self.assertEqual(set(self.client.get("/api/big-roads/review").json()["items"]), {"1"})

    def test_put_writes_atomically_and_leaves_no_temp_file(self):
        for index in range(5):
            self.assertEqual(self.client.put(f"/api/big-roads/review/{index}", json={"status": "seen"}).status_code, 200)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["big_road_review.json"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["items"]), {"0", "1", "2", "3", "4"})

    def test_corrupt_review_file_is_reported_and_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.client.get("/api/big-roads/review").status_code, 503)
        self.assertEqual(self.client.put("/api/big-roads/review/1", json={"status": "seen"}).status_code, 503)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_concurrent_puts_keep_every_item(self):
        def put(index):
            return self.client.put(f"/api/big-roads/review/{index}", json={"status": "seen"}).status_code
        threads = [threading.Thread(target=put, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.client.get("/api/big-roads/review").json()["items"]), 20)


class FakeOsrm:
    """Records calls and returns a canned OSRM /route response (or raises)."""

    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def __call__(self, lat1, lon1, lat2, lon2):
        self.calls.append((lat1, lon1, lat2, lon2))
        if self.error:
            raise self.error
        return self.response


class BigRoadRouteTests(unittest.TestCase):
    NODES = [10, 2, 3, 4, 5, 6]
    COORDS = [[105.80, 21.00], [105.81, 21.00], [105.82, 21.00], [105.83, 21.00], [105.84, 21.00], [105.85, 21.00]]
    DISTANCES = [1000.0, 500.0, 2000.0, 250.0, 1250.0]
    DURATIONS = [60.0, 30.0, 120.0, 30.0, 60.0]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "osrm/data"
        self.data.mkdir(parents=True)
        (self.root / "osrm/build.json").write_text(json.dumps({"input": {"pbf_sha256": "map-a"}}), encoding="utf-8")
        # Pairs are stored sorted (a < b); (10, 2) is traversed reversed. (3, 4) and (4, 5) are not
        # network A. (5, 6) is a ferry, which is part of network A. Neither road_classes.sqlite nor
        # big_roads.geojson exists: the endpoint must not need them.
        self.write_edges("map-a", [(2, 10, 100, "a"), (2, 3, 100, "a"), (5, 6, 900, "ferry")])
        self.osrm = FakeOsrm(self.route_response())

    def tearDown(self):
        self.temp.cleanup()

    def write_edges(self, sha, edges):
        path = self.data / "big_road_edges.sqlite"
        path.unlink(missing_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE edges (a INTEGER, b INTEGER, way_id INTEGER, kind TEXT, PRIMARY KEY(a,b,way_id))")
            db.executemany("INSERT INTO edges VALUES (?,?,?,?)", edges)
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO metadata VALUES (?,?)", [("pbf_sha256", sha), ("policy_version", "6")])
            db.commit()

    def route_response(self):
        return {"code": "Ok", "routes": [{"distance": sum(self.DISTANCES), "duration": sum(self.DURATIONS),
                "geometry": {"type": "LineString", "coordinates": self.COORDS},
                "legs": [{"annotation": {"nodes": self.NODES, "distance": self.DISTANCES, "duration": self.DURATIONS}}]}]}

    def post(self, body=None, osrm=None):
        client = TestClient(create_app(root=self.root, osrm_probe=lambda: True, key_reader=lambda: None,
                                       big_route_osrm=osrm or self.osrm))
        try:
            return client.post("/api/big-roads/route", json=body or {"origin": [21.0, 105.8], "destination": [21.0, 105.85]})
        finally:
            client.close()

    def test_route_splits_segments_by_membership_in_network_a(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.osrm.calls, [(21.0, 105.8, 21.0, 105.85)])
        body = response.json()
        self.assertEqual(set(body), {"segments", "km", "minutes", "km_on_a", "km_off_a"})
        self.assertEqual([s["on_a"] for s in body["segments"]], [True, False, True])
        self.assertEqual([s["coords"] for s in body["segments"]],
                         [self.COORDS[0:3], self.COORDS[2:5], self.COORDS[4:6]])
        self.assertEqual([s["km"] for s in body["segments"]], [1.5, 2.25, 1.25])
        self.assertEqual([s["seconds"] for s in body["segments"]], [90.0, 150.0, 60.0])
        self.assertAlmostEqual(body["km"], 5.0)
        self.assertAlmostEqual(body["minutes"], 5.0)
        self.assertAlmostEqual(body["km_on_a"], 2.75)
        self.assertAlmostEqual(body["km_off_a"], 2.25)

    def test_ferry_pair_is_on_a(self):
        self.write_edges("map-a", [(5, 6, 900, "ferry")])
        body = self.post().json()
        self.assertEqual([s["on_a"] for s in body["segments"]], [False, True])
        self.assertEqual(body["segments"][1]["coords"], self.COORDS[4:6])
        self.assertAlmostEqual(body["km_on_a"], 1.25)

    def test_pair_missing_from_index_is_off_a(self):
        self.write_edges("map-a", [(2, 10, 100, "a"), (2, 3, 100, "a"), (3, 4, 300, "road"), (5, 6, 900, "ferry")])
        body = self.post().json()
        self.assertEqual([s["on_a"] for s in body["segments"]], [True, False, True])
        self.assertEqual([s["coords"] for s in body["segments"]], [self.COORDS[0:4], self.COORDS[3:5], self.COORDS[4:6]])
        self.assertAlmostEqual(body["km_off_a"], 0.25)

    def test_no_route_is_422(self):
        response = self.post(osrm=FakeOsrm({"code": "NoRoute", "message": "Impossible route between points", "routes": []}))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Không tìm được tuyến giữa hai điểm.")

    def test_osrm_down_is_503_without_exception_text(self):
        response = self.post(osrm=FakeOsrm(error=OSError("http://127.0.0.1:5000/?key=very-secret refused")))
        self.assertEqual(response.status_code, 503)
        self.assertIn("OSRM chưa chạy", response.json()["detail"])
        self.assertNotIn("very-secret", response.text)

    def test_edges_from_another_map_are_422_like_estimate(self):
        self.write_edges("map-old", [(2, 3, 100, "a")])
        response = self.post()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], ESTIMATE_ERRORS["SpatialDataError"])
        self.assertEqual(self.osrm.calls, [])

    def test_missing_edge_index_is_404(self):
        (self.data / "big_road_edges.sqlite").unlink()
        response = self.post()
        self.assertEqual(response.status_code, 404)
        self.assertIn("Chưa dựng bản đồ đường lớn", response.json()["detail"])
        self.assertEqual(self.osrm.calls, [])

    def test_unreadable_edge_index_is_503(self):
        (self.data / "big_road_edges.sqlite").write_bytes(b"not a database at all, just bytes" * 10)
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertIn("Chạy lại python model/build_big_roads.py", response.json()["detail"])
        self.assertEqual(self.osrm.calls, [])

    def test_invalid_coordinates_are_422(self):
        for body in [{"origin": [91, 105.8], "destination": [21.0, 105.85]},
                     {"origin": [21.0, 181], "destination": [21.0, 105.85]},
                     {"origin": [21.0], "destination": [21.0, 105.85]},
                     {"origin": [21.0, 105.8, 3], "destination": [21.0, 105.85]},
                     {"origin": ["a", 105.8], "destination": [21.0, 105.85]},
                     {"origin": [21.0, 105.8]}]:
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 422)
        self.assertEqual(self.osrm.calls, [])

    def test_malformed_osrm_response_is_not_a_crash(self):
        broken = self.route_response()
        broken["routes"][0]["geometry"]["coordinates"] = self.COORDS[:-1]
        response = self.post(osrm=FakeOsrm(broken))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], ESTIMATE_ERRORS["RouteAlignmentError"])


if __name__ == "__main__":
    unittest.main()
