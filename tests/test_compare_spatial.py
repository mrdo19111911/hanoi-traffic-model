import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

import compare_spatial as cs  # noqa: E402


def row(route, slot="0800", cls="noido", lat=21.03, lon=105.85, m=200.0, s=30.0, **extra):
    base = {"lat": lat, "lon": lon, "road_class": cls, "day": 1, "slot": slot, "speed_kmh": m / s * 3.6,
            "m": m, "s": s, "street": f"Phố {route}", "route": route,
            "snapshot": f"{route}_20261020_{slot}.json", "pbf_sha256": "abc"}
    base.update(extra)
    return base


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def grid_rows(routes, cls="noido", slot="0800"):
    out = []
    for i, route in enumerate(routes):
        for k in range(3):
            out.append(row(route, slot=slot, cls=cls, lat=21.0 + 0.01 * i, lon=105.8 + 0.01 * k, m=200 + 10 * k, s=30 + i))
    return out


class SpatialSamplesTests(unittest.TestCase):
    def test_reads_fields_and_projects_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, [row("R01")])
            samples, excluded = cs.spatial_samples(path)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        x, y = cs.project(21.03, 105.85)
        self.assertAlmostEqual(sample["x"], x)
        self.assertAlmostEqual(sample["y"], y)
        for key in ["road_class", "slot", "street", "route", "snapshot", "m", "s"]:
            self.assertIn(key, sample)
        self.assertAlmostEqual(sample["speed_kmh"], 200 / 30 * 3.6)
        self.assertEqual(dict(excluded), {})

    def test_invalid_rows_are_dropped_and_counted(self):
        bad_class = row("R02", cls="xyz")
        missing = row("R03")
        del missing["lat"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, [row("R01"), row("R04", m=0), bad_class, missing])
            samples, excluded = cs.spatial_samples(path)
        self.assertEqual([s["route"] for s in samples], ["R01"])
        self.assertEqual(dict(excluded), {"nonpositive_length_or_time": 1, "unknown_road_class": 1, "missing_field": 1})


class SourceSelectionTests(unittest.TestCase):
    def legacy(self):
        return [{"legacy": True}], {"unknown_road_class": 2}

    def test_auto_prefers_spatial_samples_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, [row("R01")])
            samples, _, source = cs.load_samples("auto", path, legacy=self.legacy)
        self.assertEqual(source, "spatial_samples")
        self.assertEqual(samples[0]["route"], "R01")

    def test_auto_falls_back_to_legacy_when_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "none.jsonl"
            empty = Path(temp) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            for path in [missing, empty]:
                samples, excluded, source = cs.load_samples("auto", path, legacy=self.legacy)
                self.assertEqual(source, "legacy")
                self.assertEqual(samples, [{"legacy": True}])

    def test_spatial_source_requires_samples(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                cs.load_samples("spatial", Path(temp) / "none.jsonl", legacy=self.legacy)


class HoldoutTests(unittest.TestCase):
    def fold_keys(self, samples, holdout):
        groups = {}
        for s in samples:
            groups.setdefault(cs.holdout_fold(s, holdout), set()).add(s[holdout])
        return groups

    def test_route_holdout_never_shares_a_route(self):
        samples = [s for r in grid_rows([f"R{i:02d}" for i in range(1, 11)]) for s in [r]]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, samples)
            loaded, _ = cs.spatial_samples(path)
        groups = self.fold_keys(loaded, "route")
        routes = [k for keys in groups.values() for k in keys]
        self.assertEqual(len(routes), len(set(routes)))
        scores, skipped = cs.evaluate(loaded, holdout="route")
        self.assertIn("noido", scores)
        self.assertEqual(scores["noido"]["mean"]["n_held_out"], len(loaded) - sum(skipped.values()))

    def test_snapshot_holdout_never_shares_a_snapshot(self):
        rows = grid_rows(["R01", "R02", "R03"], slot="0800") + grid_rows(["R01", "R02", "R03"], slot="1730")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, rows)
            loaded, _ = cs.spatial_samples(path)
        groups = self.fold_keys(loaded, "snapshot")
        snaps = [k for keys in groups.values() for k in keys]
        self.assertEqual(len(snaps), len(set(snaps)))
        cs.evaluate(loaded, holdout="snapshot")

    def test_class_without_same_class_training_is_unscorable(self):
        rows = grid_rows([f"R{i:02d}" for i in range(1, 8)]) + [row("R01", cls="caotoc", lat=21.2, lon=105.9)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, rows)
            loaded, _ = cs.spatial_samples(path)
        scores, skipped = cs.evaluate(loaded, holdout="route")
        self.assertEqual(skipped["caotoc"], 1)
        self.assertNotIn("caotoc", scores)


class ReportTests(unittest.TestCase):
    def test_report_records_source_and_holdout(self):
        rows = grid_rows([f"R{i:02d}" for i in range(1, 8)])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "s.jsonl"
            write_jsonl(path, rows)
            samples, excluded = cs.spatial_samples(path)
            report = cs.build_report(samples, excluded, "spatial_samples", "route", path)
        self.assertEqual(report["source"], "spatial_samples")
        self.assertEqual(report["holdout"], "route")
        self.assertEqual(report["pbf_sha256"], "abc")
        self.assertIn("scores", report)
        self.assertTrue(report["limitations"])
        self.assertTrue(any("TomTom" in item for item in report["limitations"]))

    def test_legacy_report_keeps_street_holdout(self):
        report = cs.build_report([], {}, "legacy", "street", Path("x"))
        self.assertEqual(report["source"], "legacy")
        self.assertNotIn("pbf_sha256", report)
        self.assertTrue(any("Legacy" in item for item in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
