"""Offline tests for launcher decisions that do not start processes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("launcher", ROOT / "app.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def launcher_policy():
    spec = importlib.util.spec_from_file_location("osm_classes", ROOT / "model/osm_classes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BIG_ROAD_POLICY_VERSION


class BigRoadRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "osrm/data").mkdir(parents=True)
        (self.root / "osrm/build.json").write_text(json.dumps({"input": {"pbf_sha256": "map-a"}}), encoding="utf-8")

    def write_meta(self, sha="map-a", policy=None):
        policy = launcher_policy() if policy is None else policy
        (self.root / "osrm/data/big_roads.meta.json").write_text(json.dumps({"pbf_sha256": sha, "policy_version": policy}), encoding="utf-8")

    def test_missing_build_needs_rebuild(self):
        self.assertTrue(launcher.big_roads_outdated(self.root, "map-a"))

    def test_matching_build_is_kept(self):
        self.write_meta()
        self.assertFalse(launcher.big_roads_outdated(self.root, "map-a"))

    def test_new_map_or_policy_needs_rebuild(self):
        self.write_meta(sha="map-old")
        self.assertTrue(launcher.big_roads_outdated(self.root, "map-a"))
        self.write_meta(policy=launcher_policy() - 1)
        self.assertTrue(launcher.big_roads_outdated(self.root, "map-a"))

    def test_corrupt_meta_needs_rebuild(self):
        (self.root / "osrm/data/big_roads.meta.json").write_text("{not json", encoding="utf-8")
        self.assertTrue(launcher.big_roads_outdated(self.root, "map-a"))


class ClusterRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "osrm/data").mkdir(parents=True)
        (self.root / "osrm/data/big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")

    def write_clusters(self, sha="map-a", policy=6, method="regions", partitions=("province", "natural")):
        (self.root / "osrm/data/big_road_clusters.json").write_text(
            json.dumps({"method": method, "pbf_sha256": sha, "policy_version": policy,
                        "partitions": [{"method": m, "k": 2} for m in partitions]}), encoding="utf-8")

    def test_missing_clusters_need_a_run(self):
        self.assertTrue(launcher.clusters_outdated(self.root))

    def test_clusters_from_the_current_big_road_build_are_kept(self):
        self.write_clusters()
        self.assertFalse(launcher.clusters_outdated(self.root))

    def test_clusters_from_another_map_or_policy_need_a_run(self):
        self.write_clusters(sha="map-old")
        self.assertTrue(launcher.clusters_outdated(self.root))
        self.write_clusters(policy=5)
        self.assertTrue(launcher.clusters_outdated(self.root))

    def test_clusters_in_the_old_format_or_with_other_partitions_need_a_run(self):
        self.write_clusters(method="njw_inv_time")
        self.assertTrue(launcher.clusters_outdated(self.root))
        self.write_clusters(partitions=("kmeans", "province", "natural"))
        self.assertTrue(launcher.clusters_outdated(self.root))
        self.write_clusters(partitions=("province",))
        self.assertFalse(launcher.clusters_outdated(self.root))

    def test_corrupt_clusters_or_missing_meta_need_a_run(self):
        (self.root / "osrm/data/big_road_clusters.json").write_text("{not json", encoding="utf-8")
        self.assertTrue(launcher.clusters_outdated(self.root))
        self.write_clusters()
        (self.root / "osrm/data/big_roads.meta.json").unlink()
        self.assertTrue(launcher.clusters_outdated(self.root))


class ProvinceRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "osrm/data").mkdir(parents=True)
        (self.root / "osrm/data/big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")

    def write_provinces(self, sha="map-a", policy=6):
        (self.root / "osrm/data/vietnam_provinces.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": policy, "failed": [], "features": []}), encoding="utf-8")

    def test_missing_provinces_need_a_build(self):
        self.assertTrue(launcher.provinces_outdated(self.root))

    def test_provinces_from_the_current_map_are_kept(self):
        self.write_provinces()
        self.assertFalse(launcher.provinces_outdated(self.root))

    def test_provinces_from_another_map_or_policy_need_a_build(self):
        self.write_provinces(sha="map-old")
        self.assertTrue(launcher.provinces_outdated(self.root))
        self.write_provinces(policy=5)
        self.assertTrue(launcher.provinces_outdated(self.root))

    def test_corrupt_provinces_need_a_build(self):
        (self.root / "osrm/data/vietnam_provinces.geojson").write_text("{not json", encoding="utf-8")
        self.assertTrue(launcher.provinces_outdated(self.root))


class MainRiverRebuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "osrm/data").mkdir(parents=True)
        (self.root / "osrm/data/big_roads.meta.json").write_text(json.dumps({"pbf_sha256": "map-a", "policy_version": 6}), encoding="utf-8")

    def write_rivers(self, sha="map-a", policy=6):
        (self.root / "osrm/data/main_rivers.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "pbf_sha256": sha, "policy_version": policy, "features": [], "crossings": []}), encoding="utf-8")

    def test_missing_main_rivers_need_a_survey(self):
        self.assertTrue(launcher.rivers_outdated(self.root))

    def test_main_rivers_from_the_current_build_are_kept(self):
        self.write_rivers()
        self.assertFalse(launcher.rivers_outdated(self.root))

    def test_main_rivers_from_another_map_or_policy_need_a_survey(self):
        self.write_rivers(sha="map-old")
        self.assertTrue(launcher.rivers_outdated(self.root))
        self.write_rivers(policy=5)
        self.assertTrue(launcher.rivers_outdated(self.root))


if __name__ == "__main__":
    unittest.main()
