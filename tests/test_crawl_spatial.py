"""Offline proof that crawl samples preserve geometry and road classes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from download.crawl_spatial import crawl, extract_snapshot, rebuild_samples, select_trace_indices


def snapshot():
    return {"request": {"departAt": "2026-10-20T08:00:00+07:00", "travelMode": "car", "traffic": "false", "computeTravelTimeFor": "all", "instructionsType": "text", "language": "en-GB"},
            "origin": [21., 105.], "destination": [21.001, 105.001], "fetched_at": "2026-10-19T00:00:00Z",
            "response": {"routes": [{"legs": [{"points": [{"latitude": 21., "longitude": 105.}, {"latitude": 21.001, "longitude": 105.}, {"latitude": 21.001, "longitude": 105.001}]}],
            "guidance": {"instructions": [{"pointIndex": 0, "routeOffsetInMeters": 0, "travelTimeInSeconds": 0, "street": "A"}, {"pointIndex": 2, "routeOffsetInMeters": 200, "travelTimeInSeconds": 20, "street": "B"}]}}]}}


def matching():
    return {"code": "Ok", "tracepoints": [{"matchings_index": 0, "waypoint_index": i, "location": point} for i, point in enumerate([[105., 21.], [105., 21.001], [105.001, 21.001]])],
            "matchings": [{"confidence": .99, "legs": [{"annotation": {"nodes": [10, 11], "distance": [100]}}, {"annotation": {"nodes": [11, 12], "distance": [100]}}]}]}


class CrawlSpatialTests(unittest.TestCase):
    def test_geometry_midpoint_uses_bend_and_provenance(self):
        rows, excluded = extract_snapshot(snapshot(), "R01", "R01_20261020_0800.json", lambda points: matching(), lambda a, b: {"noido"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["road_class"], "noido")
        self.assertEqual(rows[0]["speed_kmh"], 36)
        self.assertAlmostEqual(rows[0]["lon"], 105., places=4)
        self.assertAlmostEqual(rows[0]["lat"], 21.001, places=4)
        self.assertEqual(rows[0]["class_quality"], "osm_map_match_node_pairs")
        self.assertEqual(rows[0]["speed_quality"], "tomtom_instruction_segment_mean")
        self.assertEqual(rows[0]["day"], 1)
        self.assertFalse(excluded)

    def test_mixed_classes_are_excluded_instead_of_sharing_speed(self):
        rows, excluded = extract_snapshot(snapshot(), "R01", "test.json", lambda points: matching(), lambda a, b: {"caotoc" if a == 10 else "noido"})
        self.assertEqual(rows, [])
        self.assertEqual(excluded["mixed_road_classes"], 1)

    def test_unknown_or_ambiguous_node_pairs_are_excluded(self):
        for classes, reason in [(set(), "unknown_road_edge"), ({"unknown"}, "unknown_road_edge"), ({"noido", "caotoc"}, "ambiguous_road_edge")]:
            rows, excluded = extract_snapshot(snapshot(), "R01", "test.json", lambda points: matching(), lambda a, b: classes)
            self.assertFalse(rows)
            self.assertEqual(excluded[reason], 1)

    def test_null_interior_tracepoint_or_split_match_is_rejected(self):
        for mutate in [lambda match: match["tracepoints"].__setitem__(1, None), lambda match: match["tracepoints"][2].update(matchings_index=1)]:
            match = matching()
            mutate(match)
            rows, excluded = extract_snapshot(snapshot(), "R01", "test.json", lambda points: match, lambda a, b: {"noido"})
            self.assertFalse(rows)
            self.assertEqual(excluded["unmatched_instruction_interval"], 1)

    def test_geometry_distance_mismatch_is_rejected(self):
        match = matching()
        match["matchings"][0]["legs"][0]["annotation"]["distance"] = [1000]
        rows, excluded = extract_snapshot(snapshot(), "R01", "test.json", lambda points: match, lambda a, b: {"noido"})
        self.assertFalse(rows)
        self.assertEqual(excluded["matched_length_mismatch"], 1)

    def test_mandatory_instruction_points_are_never_downsampled_away(self):
        points = [[21 + i * .0001, 105.] for i in range(500)]
        mandatory = [0, 7, 17, 499]
        selected = select_trace_indices(points, mandatory)
        self.assertTrue(set(mandatory).issubset(selected))
        self.assertLessEqual(len(selected), 100)
        with self.assertRaises(ValueError):
            select_trace_indices(points, list(range(101)))

    def test_rebuild_is_atomic_resumable_and_uses_all_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "data/api_raw"
            raw.mkdir(parents=True)
            for i in [1, 2]:
                (raw / f"R0{i}_20261020_0800.json").write_text(json.dumps(snapshot()), encoding="utf-8")
            summary = rebuild_samples(root=root, match_provider=lambda points: matching(), class_lookup=lambda a, b: {"noido"})
            self.assertEqual(summary["sample_count"], 2)
            text = (root / "data/spatial_samples.jsonl").read_text(encoding="utf-8")
            summary = rebuild_samples(root=root, match_provider=lambda points: matching(), class_lookup=lambda a, b: {"noido"})
            self.assertEqual((root / "data/spatial_samples.jsonl").read_text(encoding="utf-8"), text)
            self.assertEqual(summary["snapshots"], 2)

    def test_crawl_reuses_valid_cache_and_enforces_new_call_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "data/api_raw"
            raw.mkdir(parents=True)
            (raw / "R01_20261020_0800.json").write_text(json.dumps(snapshot()), encoding="utf-8")
            calls = []
            def fetch(origin, destination, request, key):
                calls.append(request["departAt"])
                return snapshot()["response"]
            summary = crawl(root, "2026-10-20", ["0800", "1730", "0300"], 1, fetcher=fetch,
                            match_provider=lambda points: matching(), class_lookup=lambda a, b: {"noido"},
                            routes=[("R01_test", "21,105", "21.001,105.001")], key_reader=lambda: "private-key")
            self.assertEqual(len(calls), 1)
            self.assertEqual(summary["new_requests"], 1)
            self.assertEqual(summary["cached_requests"], 1)
            self.assertNotIn("private-key", "".join(p.read_text(encoding="utf-8") for p in raw.glob("*.json")))


if __name__ == "__main__":
    unittest.main()
