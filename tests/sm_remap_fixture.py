"""Two tiny maps of the same place for re-resolving rules: OLD, and NEW after an update.

NEW differs from OLD by: way 1 "Phố A" split into 91 [11, 12] and 92 [12, 13]; "Vành đai D" renamed
"Đường Mới"; "Phố C" deleted; 30 extra segments of "Phố B".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sm_osrm_fixture import write_osm  # noqa: E402

from speedmatrix.segments import build_segments  # noqa: E402

BBOX = [105.79, 20.99, 105.83, 21.03]
SEED = [21.009, 105.8097]
RING = ["Vành đai A", "Vành đai B", "Vành đai C", "Vành đai D"]
ZONE = {"boundary_names": RING, "seed": SEED, "area_km2": [2, 6]}

COORDS = {11: (105.800, 20.995), 12: (105.801, 20.995), 13: (105.802, 20.995),
          21: (105.800, 20.993), 22: (105.801, 20.993), 23: (105.802, 20.993),
          51: (105.800, 20.991), 52: (105.801, 20.991),
          101: (105.800, 21.000), 102: (105.8193, 21.000), 103: (105.8193, 21.018), 104: (105.800, 21.018)}
EXTRA_B = {200 + i: (105.800 + 0.0002 * i, 20.992) for i in range(31)}


def _tag(name, highway="secondary"):
    return {"highway": highway, "name": name}


def _ring(d_name):
    return [(10, [101, 102], _tag(RING[0])), (11, [102, 103], _tag(RING[1])),
            (12, [103, 104], _tag(RING[2])), (13, [104, 101], _tag(d_name))]


OLD_WAYS = [(1, [11, 12, 13], _tag("Phố A")), (2, [21, 22, 23], _tag("Phố B")), (5, [51, 52], _tag("Phố C"))] \
    + _ring(RING[3])
NEW_WAYS = [(91, [11, 12], _tag("Phố A")), (92, [12, 13], _tag("Phố A")), (2, [21, 22, 23], _tag("Phố B")),
            (6, sorted(EXTRA_B), _tag("Phố B"))] + _ring("Đường Mới")


def build_maps(base):
    """(old SegmentTable, new SegmentTable) written under ``base``."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    write_osm(base / "old.osm", COORDS, OLD_WAYS)
    write_osm(base / "new.osm", {**COORDS, **EXTRA_B}, NEW_WAYS)
    return build_segments(base / "old.osm", None), build_segments(base / "new.osm", None)
