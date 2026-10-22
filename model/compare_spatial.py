"""Compare fixed interpolation candidates on held-out whole street names.

Legacy sample locations are approximate: the original JSONL does not contain
coordinates or offsets. Results are exploratory, not a final accuracy claim.
"""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

try:
    from .classify5 import classify5
    from .spatial import SpeedField, project
except ImportError:
    from classify5 import classify5
    from spatial import SpeedField, project

ROOT = Path(__file__).resolve().parents[1]


def legacy_samples():
    geometries = json.loads((ROOT / "data/geometries_full.json").read_text(encoding="utf-8"))
    groups = defaultdict(list)
    excluded = Counter()
    for filename in ["segments_raw.jsonl", "segments_exp.jsonl"]:
        for line in (ROOT / "data" / filename).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            row["source_file"] = filename
            groups[(row["route"], row["slot"])].append(row)
    samples = []
    for (route, slot), rows in groups.items():
        if route not in geometries:
            excluded["missing_route_geometry"] += len(rows)
            continue
        poly = geometries[route]["poly"]
        xy = np.asarray([project(lat, lon) for lat, lon in poly])
        cumulative = np.r_[0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
        length = sum(row["m"] for row in rows)
        offset = 0
        for row in rows:
            midpoint = (offset + row["m"] / 2) / max(1, length) * cumulative[-1]
            offset += row["m"]
            cls = "caotoc" if row["cls"] == "motorway" else classify5(row["street"])
            if cls == "unknown":
                excluded["unknown_road_class"] += 1
                continue
            if row["m"] <= 0 or row["s"] <= 0:
                excluded["nonpositive_length_or_time"] += 1
                continue
            # Preserve the actual hour rather than treating the old 02:00
            # requests labelled '2230' as observations at 22:30.
            departure = datetime.fromisoformat(row["depart"])
            actual_slot = departure.strftime("%H%M")
            x, y = [np.interp(midpoint, cumulative, xy[:, axis]) for axis in [0, 1]]
            samples.append({
                "x": float(x), "y": float(y), "road_class": cls,
                "slot": actual_slot, "street": row["street"], "route": route,
                "speed_kmh": row["m"] / row["s"] * 3.6,
                "m": row["m"], "s": row["s"],
                "location_quality": "proportional_route_approximation",
                "class_quality": "legacy_name_heuristic_or_explicit_motorway",
            })
    return samples, excluded


CLASSES = {"caotoc", "quoclo", "chinh", "noido", "nho"}
SPATIAL_FIELDS = ["lat", "lon", "road_class", "slot", "street", "route", "snapshot", "m", "s"]


def spatial_samples(path):
    """Read data/spatial_samples.jsonl rows (real TomTom geometry, OSM-matched classes) into samples."""
    samples, excluded, pbf = [], Counter(), None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if any(row.get(key) is None for key in SPATIAL_FIELDS):
            excluded["missing_field"] += 1
            continue
        if row["road_class"] not in CLASSES:
            excluded["unknown_road_class"] += 1
            continue
        if row["m"] <= 0 or row["s"] <= 0:
            excluded["nonpositive_length_or_time"] += 1
            continue
        x, y = project(row["lat"], row["lon"])
        pbf = pbf or row.get("pbf_sha256")
        samples.append({
            "x": float(x), "y": float(y), "road_class": row["road_class"], "slot": row["slot"],
            "street": row["street"], "route": row["route"], "snapshot": row["snapshot"],
            "speed_kmh": row["m"] / row["s"] * 3.6, "m": row["m"], "s": row["s"], "pbf_sha256": row.get("pbf_sha256"),
        })
    return samples, excluded


def load_samples(source, path, legacy=None):
    """Pick the sample source: 'auto' prefers the spatial file when it has samples, else legacy."""
    legacy = legacy or legacy_samples
    if source in ("auto", "spatial"):
        samples, excluded = spatial_samples(path) if Path(path).is_file() else ([], Counter())
        if samples:
            return samples, excluded, "spatial_samples"
        if source == "spatial":
            raise ValueError(f"no usable samples in {path}")
    samples, excluded = legacy()
    return samples, excluded, "legacy"


def street_fold(street):
    normalized = re.sub(r"\s+", " ", street.strip().casefold())
    return int(hashlib.sha256(normalized.encode("utf-8")).hexdigest(), 16) % 5


def holdout_fold(sample, holdout):
    """Fold 0-4 for a sample; every sample sharing the holdout key lands in the same fold."""
    if holdout == "street":
        return street_fold(sample["street"])
    return int(hashlib.sha256(str(sample[holdout]).encode("utf-8")).hexdigest(), 16) % 5


def evaluate(samples, holdout="street"):
    groups = defaultdict(list)
    for row in samples:
        groups[(row["road_class"], row["slot"])].append(row)
    scores = defaultdict(lambda: defaultdict(list))
    skipped = Counter()
    for (cls, slot), rows in groups.items():
        for fold in range(5):
            train = [r for r in rows if holdout_fold(r, holdout) != fold]
            test = [r for r in rows if holdout_fold(r, holdout) == fold]
            if not test:
                continue
            if not train:
                skipped[cls] += len(test)
                continue
            # Every held-out key (street, route or snapshot) is absent from train,
            # including copies on other routes at this time slot.
            assert not ({r[holdout] for r in train} & {r[holdout] for r in test})
            query = [(r["x"], r["y"]) for r in test]
            truth = np.asarray([r["speed_kmh"] for r in test])
            metres = np.asarray([r["m"] for r in test])
            seconds = np.asarray([r["s"] for r in test])
            for method in SpeedField.METHODS:
                field = SpeedField([(r["x"], r["y"]) for r in train],
                                   [r["speed_kmh"] for r in train], method)
                prediction = field.predict(query)
                assert np.isfinite(prediction).all() and (prediction > 0).all()
                inside = (field.triangulation.find_simplex(query) >= 0) if field.triangulation else np.zeros(len(test), dtype=bool)
                for i in range(len(test)):
                    scores[cls][method].append({
                        "speed_error": float(abs(prediction[i] - truth[i])),
                        "time_error_s": float(abs(metres[i] / prediction[i] * 3.6 - seconds[i])),
                        "metres": float(metres[i]), "seconds": float(seconds[i]),
                        "inside_hull": bool(inside[i]),
                    })
    result = {}
    for cls, methods in sorted(scores.items()):
        result[cls] = {}
        for method, rows in methods.items():
            length = sum(r["metres"] for r in rows)
            result[cls][method] = {
                "n_held_out": len(rows),
                "mae_kmh": sum(r["speed_error"] for r in rows) / len(rows),
                "length_weighted_mae_kmh": sum(r["speed_error"] * r["metres"] for r in rows) / length,
                "travel_time_wape_pct": 100 * sum(r["time_error_s"] for r in rows) / sum(r["seconds"] for r in rows),
                "inside_triangle_pct": 100 * sum(r["inside_hull"] for r in rows) / len(rows),
            }
    return result, skipped


LEGACY_LIMITATIONS = [
    "Legacy segment coordinates are reconstructed approximately along a single saved route geometry.",
    "Legacy road classes use name heuristics; no verified TomTom-to-OSM class alignment yet.",
    "Speeds are TomTom routing outputs, not independent physical speed measurements.",
    "Fixed five-fold split holds out whole normalized street names; nearby streets can still be correlated.",
    "Highway data has only one named spatial source and cannot establish interpolation accuracy.",
    "Formula comparisons are cross-validation results, without a separate untouched final test dataset.",
]
SPATIAL_LIMITATIONS = [
    "Speeds are TomTom instruction-segment means, not independently measured physical point speeds.",
    "The sample is small; scores per road class and slot rest on few held-out routes.",
    "Five-fold split holds out whole {holdout}s; nearby routes can still be correlated.",
    "Formula comparisons are cross-validation results, without a separate untouched final test dataset.",
]


def build_report(samples, excluded, source, holdout, path):
    scores, skipped = evaluate(samples, holdout=holdout)
    report = {
        "status": "exploratory_only", "source": source, "holdout": holdout,
        "limitations": LEGACY_LIMITATIONS if source == "legacy" else [s.format(holdout=holdout) for s in SPATIAL_LIMITATIONS],
        "candidates": list(SpeedField.METHODS),
        "samples_by_class": dict(Counter(r["road_class"] for r in samples)),
        "excluded": dict(excluded), "unscorable_without_same_class_training": dict(skipped),
        "scores": scores,
    }
    if source != "legacy":
        report["samples_path"] = str(path)
        report["pbf_sha256"] = next((s["pbf_sha256"] for s in samples if s.get("pbf_sha256")), None)
    return report


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["auto", "spatial", "legacy"], default="auto")
    parser.add_argument("--holdout", choices=["route", "snapshot", "street"])
    parser.add_argument("--samples", type=Path, default=ROOT / "data/spatial_samples.jsonl")
    args = parser.parse_args(argv)
    samples, excluded, source = load_samples(args.source, args.samples)
    holdout = args.holdout or ("street" if source == "legacy" else "route")
    report = build_report(samples, excluded, source, holdout, args.samples)
    scores, skipped = report["scores"], report["unscorable_without_same_class_training"]
    output = ROOT / "reports/spatial_comparison.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EXPLORATORY: source={source}, holdout={holdout}, samples={len(samples)}")
    print("class  method          n     MAE km/h    length-weighted MAE   time WAPE %")
    for cls, methods in scores.items():
        for method, score in methods.items():
            print(f"{cls:6} {method:14} {score['n_held_out']:4} {score['mae_kmh']:10.2f} {score['length_weighted_mae_kmh']:20.2f} {score['travel_time_wape_pct']:13.2f}")
    print("Unscorable:", dict(skipped))
    print("Report:", output)


if __name__ == "__main__":
    main()
