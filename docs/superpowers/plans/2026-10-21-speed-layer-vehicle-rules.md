# Speed Layer, Versioning, Vehicle Groups and Ban Rules Implementation Plan (sub-project 0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `speedmatrix` package that builds, from the Vietnam PBF:
- a nationwide segment table;
- a versioned base speed layer (3 day types × 48 slots);
- per-vehicle-group views (speed caps and closures).

The rules behind all of these live in a SQLite state store, and every rule is re-runnable. The store is seeded with the 116 nationwide truck rules researched on 2026-10-21.

**Architecture:**
- Each component is a module with plain numpy arrays at its interfaces:
  - segments (one row per adjacent OSM node pair of a drivable way);
  - rules and vehicle groups in SQLite;
  - scope resolution (segment anchors, polygon, street names, a zone sealed by its boundary roads);
  - closures (OSM tags, then official, then company rules);
  - the speed layer (source → % rules → set rules → clamp);
  - group views (min(base, cap), plus closures).
- A CLI, `python -m speedmatrix.build`, writes versioned outputs under `runtime/matrix/`.
- The base source is the OSRM car-profile free-flow speed. Sample-based interpolation is sub-project 5 and will plug into the same source interface.

**Tech Stack:** Python 3.14 (`.venv`), numpy, scipy (`ndimage`), osmium, sqlite3, unittest.

**Spec:** `docs/superpowers/specs/2026-10-21-speed-matrix-system-design.md`. This plan covers sections 5.2–5.4, 7.3, 7.4 (segment level only) and 8. It also creates the segment identity that sub-project 1 maps OSRM segments onto.

## Global Constraints

- **Not a git repository:** there are no commit steps. After each task, run the whole suite: `.venv/Scripts/python.exe -m unittest discover -s tests`. It must stay green.
- **Dependencies:** only those in `requirements-spatial.txt` (numpy, scipy, osmium) and the standard library. No shapely, no pandas.
- **Language:** code, docstrings and CLI output are in English. User-facing API/UI strings (later sub-projects) are in Vietnamese.
- **Core invariant:**
  - speeds are never borrowed across road classes;
  - missing data raises (`MissingRoadData`, `RuleError`, `ZoneError`) and is never silently substituted.
- **Day types:** `("weekday", "sat", "sun")`. Public holidays use `"sun"` via the holiday calendar.
- **Slots:** 48 slots of 1800 s; slot i starts at 1800·i s after 00:00 local time (UTC+7).
- **Ban windows:** a ban interval closes every slot it overlaps by more than 0 s. Intervals may cross midnight (`21:00–06:00`) and may end at `24:00`.
- **Re-runnable rules:**
  - every edit is a rule row;
  - no task edits an output file by hand;
  - a rule whose target cannot be found is reported as an **orphan** and stays stored.
- **Low-confidence rules:** rules with confidence `"thap"` (Thấp) import inactive unless their file says `"active": true`.
- **Rule files:** the seed format is `reports/truck_rules/RULE_FORMAT.md`; that file is the contract for every rule body.
- **Dated rules:** a rule with `valid_from`/`valid_to` counts in a build only when the build's `--as-of` date (default: today) falls inside its range.
- **Outputs:**
  - outputs go under `runtime/matrix/` (gitignored `runtime/`);
  - every output directory is written to a temp sibling and renamed into place;
  - every output carries the hashes of its inputs.
- **Offline tests:**
  - tests run offline, with no OSRM, network or API key;
  - OSM fixtures are tiny `.osm` XML files written by the test, like `write_osm` in `tests/test_big_roads.py`;
  - do not touch OSRM on port 5000 or the web app on port 8000.
- **Imports:**
  - the package is `speedmatrix/` at the repo root, with an `__init__.py`;
  - tests import `from speedmatrix.<module> import ...` (repo root is on `sys.path` under `python -m unittest`);
  - `model/` modules are imported as `from model.osm_classes import ...` and `from model.spatial import MissingRoadData` (`model` is a namespace package).

## Review Focus

1. **A ban interval that crosses midnight or ends at 24:00** (Hà Nội ≥ 2 t: closed 06:00–21:00; a sign "22:00-06:00"). Expected: exactly the overlapped slots close, with nothing dropped at the wrap. Tested in Task 1 (`test_interval_crossing_midnight_wraps`, `test_end_at_2400`).
2. **A boundary road of a zone whose name does not match OSM.** Expected: the zone leaks to the grid edge, which must raise `ZoneError` naming the missing names. It must not ban half the country. Tested in Task 5 (`test_gap_in_boundary_raises_instead_of_leaking`, `test_unknown_boundary_name_raises`).
3. **A rule anchored to segments that vanish after a map update.** Expected: the rule is reported as an orphan, stays active for its remaining targets, and the build does not fail. Tested in Task 5 (`test_missing_anchor_is_an_orphan`) and Task 8 (`test_orphans_are_listed_in_build_report`).
4. **Garbage or unusual `maxweight`/`maxheight`/conditional values** (`"10 t"`, `"8000 kg"`, `"default"`, `"no @ (sunrise-sunset)"`). Expected: parsed when the meaning is clear, otherwise counted as unparsed. Never crash. Never read as 0 (0 would close the road to every truck). Tested in Task 2 (`test_weight_and_height_parsing`) and Task 6 (`test_unparsed_conditional_is_reported_not_applied`).
5. **A boundary road that also runs through its own zone** (a long road named whole in `boundary_names`). Expected: the zone splits, and the build must raise `ZoneError` ("not adjacent") instead of quietly banning only half the city. Tested in Task 5 (`test_boundary_road_crossing_the_zone_is_caught`).
6. **A % rule that drives a speed to ≤ 0 or above the class limit.** Expected: the speed is clamped to the class limits and the clamp is counted in the layer stats. A speed of 0 must never appear in the base layer, because 0 means "closed" only in group views. Tested in Task 7 (`test_clamp_is_counted_and_zero_never_appears`).

---

### Task 1: Time slots, day types and the geometry helpers

**Files:**
- Create: `speedmatrix/__init__.py` (empty), `speedmatrix/timeslots.py`, `speedmatrix/geo.py`
- Test: `tests/test_sm_timeslots.py`, `tests/test_sm_geo.py`

**Interfaces:**
- Produces (`timeslots.py`):
  - constants `DAYTYPES = ("weekday", "sat", "sun")`, `SLOTS = 48`, `SLOT_S = 1800`;
  - `slot_index(hhmm: str) -> int`: `"0830"` → 17; `ValueError` if not on a 30-minute boundary;
  - `daytype_of(day: datetime.date, holidays: set[datetime.date]) -> str`;
  - `times_mask(intervals: list[tuple[str, str]]) -> np.ndarray[bool] (48,)`: `"HH:MM"` strings, end `"24:00"` allowed, end < start wraps past midnight;
  - `when_mask(when: dict | None) -> np.ndarray[bool] (3, 48)`: `{"daytypes": [...], "times": [[start, end], ...]}`; a missing key means all; `None` means always;
  - `class ConditionalParseError(ValueError)`;
  - `parse_conditional(value: str) -> list[tuple[str, np.ndarray[bool] (3, 48)]]`: OSM `*:conditional` values.
- Produces (`geo.py`):
  - `haversine_m(lat1, lon1, lat2, lon2) -> np.ndarray` (vectorized);
  - `points_in_geometry(lat: np.ndarray, lon: np.ndarray, geometry: dict) -> np.ndarray[bool]`: GeoJSON `Polygon`/`MultiPolygon` with holes, even-odd rule; `ValueError` for other types.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sm_timeslots.py
def test_slot_index_and_bad_slot(self):
    self.assertEqual(slot_index("0000"), 0); self.assertEqual(slot_index("0830"), 17); self.assertEqual(slot_index("2330"), 47)
    with self.assertRaises(ValueError): slot_index("0815")

def test_daytype_with_holiday(self):
    hol = {date(2026, 9, 2)}
    self.assertEqual(daytype_of(date(2026, 10, 21), hol), "weekday")   # Wednesday
    self.assertEqual(daytype_of(date(2026, 10, 24), hol), "sat")
    self.assertEqual(daytype_of(date(2026, 10, 25), hol), "sun")
    self.assertEqual(daytype_of(date(2026, 9, 2), hol), "sun")          # holiday on a Wednesday

def test_overlap_closes_partial_slots(self):
    m = times_mask([("06:00", "09:00"), ("16:00", "19:30")])
    self.assertEqual(np.flatnonzero(m).tolist(), list(range(12, 18)) + list(range(32, 39)))
    self.assertEqual(np.flatnonzero(times_mask([("06:10", "06:20")])).tolist(), [12])

def test_interval_crossing_midnight_wraps(self):
    self.assertEqual(np.flatnonzero(times_mask([("21:00", "06:00")])).tolist(), list(range(0, 12)) + list(range(42, 48)))

def test_end_at_2400(self):
    self.assertEqual(np.flatnonzero(times_mask([("22:00", "24:00")])).tolist(), [44, 45, 46, 47])

def test_when_mask_defaults_and_daytypes(self):
    self.assertTrue(when_mask(None).all())
    m = when_mask({"daytypes": ["sat"], "times": [["06:00", "07:00"]]})
    self.assertEqual(m.sum(), 2); self.assertTrue(m[1, 12] and m[1, 13])

def test_parse_conditional_forms(self):
    (value, m), = parse_conditional("no @ (06:00-09:00,16:00-19:30)")
    self.assertEqual(value, "no"); self.assertTrue(m.all(axis=0)[12] and not m[:, 11].any())
    (value, m), = parse_conditional("10 @ (Mo-Fr 06:00-22:00)")
    self.assertEqual(value, "10"); self.assertTrue(m[0, 12]); self.assertFalse(m[1].any() or m[2].any())
    parts = parse_conditional("no @ (Sa,Su,PH 08:00-12:00); 5 @ (22:00-06:00)")
    self.assertEqual([p[0] for p in parts], ["no", "5"]); self.assertTrue(parts[0][1][2, 16])

def test_unsupported_conditional_raises(self):
    for bad in ("no @ (sunrise-sunset)", "no @ (weight>7)", "no @ (Jan-Mar 08:00-09:00)", "no"):
        with self.assertRaises(ConditionalParseError): parse_conditional(bad)

# tests/test_sm_geo.py
def test_haversine_one_degree_lat(self):
    self.assertAlmostEqual(float(haversine_m(21.0, 105.8, 22.0, 105.8)), 111195, delta=50)

def test_polygon_with_hole_and_multipolygon(self):
    square = [[[105.0, 21.0], [106.0, 21.0], [106.0, 22.0], [105.0, 22.0], [105.0, 21.0]]]
    hole = [[105.4, 21.4], [105.6, 21.4], [105.6, 21.6], [105.4, 21.6], [105.4, 21.4]]
    lat, lon = np.array([21.5, 21.2, 23.0]), np.array([105.5, 105.2, 105.5])
    self.assertEqual(points_in_geometry(lat, lon, {"type": "Polygon", "coordinates": square + [hole]}).tolist(), [False, True, False])
    multi = {"type": "MultiPolygon", "coordinates": [square, [[[105.0, 22.5], [106.0, 22.5], [106.0, 23.5], [105.0, 22.5]]]]}
    self.assertEqual(points_in_geometry(lat, lon, multi).tolist(), [True, True, True])
```

Supported conditional syntax:
- `<value> @ (<spec>)` parts joined by `;`;
- `<spec>` is an optional day list (`Mo`…`Su`, ranges `Mo-Fr`, `PH`) followed by comma-separated `HH:MM-HH:MM` ranges;
- no day list means every day type.

Mapping days to day types:
- a day list covers `weekday` if it names any of Mo–Fr;
- it covers `sat` if it names Sa;
- it covers `sun` if it names Su or PH.

This is conservative for trucks: one listed weekday closes the whole weekday type.

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_timeslots tests.test_sm_geo -v`. Expected: `ModuleNotFoundError: speedmatrix`.

- [ ] **Step 3: Implement `timeslots.py` and `geo.py`**

- `times_mask` works in minutes: an interval [s, e) closes slot i when it overlaps [30i, 30i + 30) by more than 0 minutes. When e ≤ s, the interval wraps past midnight and is split into [s, 1440) and [0, e).
- `points_in_geometry` uses vectorized ray casting per ring with the even-odd rule. All rings of a polygon count; the parts of a MultiPolygon are OR-ed.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: all OK.

---

### Task 2: Segment table from the PBF

**Files:**
- Create: `speedmatrix/segments.py`
- Test: `tests/test_sm_segments.py` (copy `write_osm` from `tests/test_big_roads.py` into the test file as a helper)

**Interfaces:**
- Consumes: `model.osm_classes.classify_osm`, `free_flow_speed`, `DEFAULT_SPEEDS_KMH`, `POLICY_VERSION`; `geo.haversine_m`.
- Produces:
  - constants:
    - `ROAD_CLASSES = ("caotoc", "quoclo", "chinh", "noido", "nho", "pha")`: code = index; `pha` is a ferry, which is not a speed-sample class;
    - `HGV_VALUES = ("", "no", "yes", "designated", "destination", "delivery", "other")`;
    - `SEGMENTS_FORMAT = 1`.
  - `@dataclass SegmentTable`. All arrays are numpy.
    - Per segment (length n):
      - `a`, `b` (int64 OSM node ids, in way order);
      - `way` (int32 index into the way arrays);
      - `a_lat`, `a_lon`, `b_lat`, `b_lon` (float32);
      - `length_m` (float32).
    - Per way (length w):
      - `way_id` (int64);
      - `highway` (int32 string index);
      - `road_class` (int8);
      - `oneway` (int8: 1 forward, −1 reverse, 0 both);
      - `name`, `ref` (int32 string index, −1 none);
      - `maxspeed_kmh`, `maxweight_t`, `maxheight_m` (float32, NaN = none or unparsed);
      - `hgv` (int8 index into `HGV_VALUES`);
      - `cond_hgv`, `cond_maxweight` (int32 string index, −1 none).
    - `strings: list[str]`; `meta: dict`.
    - Properties: `n`, `mid_lat`, `mid_lon`, `segment_class` (= `road_class[way]`), `sha256` (hash of `meta["pbf_sha256"]`, `bbox`, `SEGMENTS_FORMAT`, `POLICY_VERSION`).
  - `parse_tonnes(value: str) -> float | None` and `parse_metres(value: str) -> float | None`.
  - `build_segments(pbf: Path, bbox: tuple[float, float, float, float] | None) -> SegmentTable`.
  - `save_segments(table: SegmentTable, out_dir: Path) -> None` and `load_segments(out_dir: Path) -> SegmentTable`. The directory holds `segments.npz`, `strings.json` and `meta.json`, written to a temp sibling and renamed.
  - `find_segments(table: SegmentTable, keys: np.ndarray (k, 3) [way_id, a, b]) -> np.ndarray[int64] (k,)`: −1 when missing; matches either node order.
  - `build_admin(pbf: Path, levels=(4, 6, 8)) -> list[dict]`: GeoJSON features with `properties` `{name, admin_level, osm_id}`, from `boundary=administrative` relations assembled with `osmium.area.AreaManager`.
  - `save_admin(features, out_dir)` writes `admin.json` next to `segments.npz`; `load_admin(out_dir) -> list[dict]`. The `segments` CLI command (Task 8) writes both.
  - `@dataclass WaterLines(a_lat, a_lon, b_lat, b_lon: np.ndarray[float32], name: np.ndarray[int32], strings: list[str])`: one row per segment of a **named** `waterway=river|canal` way or of a `natural=coastline` way (name `"coastline"`), same bbox rule. Zones whose boundary is a river or the sea (e.g. Hải Phòng KV2 "Sông Sặt") use these lines.
  - `build_water_lines(pbf, bbox) -> WaterLines`, `save_water_lines(lines, out_dir)` (`water.npz`) and `load_water_lines(out_dir)`. The `segments` CLI command writes `water.npz` too.

Which ways are kept:
- ways whose `highway` is a key of `DEFAULT_SPEEDS_KMH` and that are not `area=yes`;
- ways with `route=ferry` (class `pha`, highway string `"ferry"`).

With a bbox, a way is kept when any of its nodes lies inside the bbox.

`oneway` is 1 when:
- the tag is `oneway=yes|1|true`;
- or the way is `junction=roundabout`;
- or the highway is `motorway`/`motorway_link` without `oneway=no`.

`oneway=-1` gives −1. Everything else gives 0.

`meta` holds:
- `pbf_sha256`, `bbox`, `format`, `policy_version`;
- `counts` per class;
- `unparsed`: counts per tag for unparseable `maxweight`, `maxheight` and `maxspeed` values.

- [ ] **Step 1: Write the failing tests**

The fixture has:
- way 1: `primary`, `oneway=yes`, `maxweight=10 t`, `name=Phố Huế`;
- way 2: `residential`, `maxweight=8000 kg`, `maxheight=4.2 m`, `hgv=no`, `hgv:conditional=yes @ (21:00-06:00)`;
- way 3: `footway`, not kept;
- way 4: `route=ferry`;
- way 5: `service`, `maxweight=default`;
- way 6: lies outside the test bbox.

```python
def test_kept_ways_classes_and_ferry(self):
    t = self.table
    kept = sorted(t.way_id.tolist()); self.assertEqual(kept, [1, 2, 4, 5])
    cls = {int(w): ROAD_CLASSES[c] for w, c in zip(t.way_id, t.road_class)}
    self.assertEqual(cls, {1: "noido", 2: "noido", 4: "pha", 5: "nho"})  # "Phố" → noido per classify_osm

def test_one_row_per_adjacent_pair_with_length(self):
    i = find_segments(self.table, np.array([[1, 11, 12], [1, 12, 11], [1, 11, 13]]))
    self.assertEqual(i[0], i[1]); self.assertEqual(i[2], -1)
    self.assertAlmostEqual(float(self.table.length_m[i[0]]), <haversine of nodes 11,12>, delta=0.5)

def test_weight_and_height_parsing(self):
    self.assertEqual(parse_tonnes("10"), 10.0); self.assertEqual(parse_tonnes("10 t"), 10.0)
    self.assertEqual(parse_tonnes("5.5t"), 5.5); self.assertEqual(parse_tonnes("8000 kg"), 8.0)
    for bad in ("default", "", "0", "-3", "10 st"): self.assertIsNone(parse_tonnes(bad))
    self.assertEqual(parse_metres("4.2 m"), 4.2); self.assertIsNone(parse_metres("4'2\""))
    self.assertEqual(self.table.meta["unparsed"]["maxweight"], 1)   # way 5 "default"

def test_oneway_and_tags_kept(self):
    t = self.table; w = {int(x): k for k, x in enumerate(t.way_id)}
    self.assertEqual(int(t.oneway[w[1]]), 1); self.assertEqual(HGV_VALUES[t.hgv[w[2]]], "no")
    self.assertEqual(t.strings[t.cond_hgv[w[2]]], "yes @ (21:00-06:00)")
    self.assertTrue(np.isnan(t.maxweight_t[w[5]]))

def test_admin_areas_from_relations(self):
    # fixture: one admin_level=6 relation "Phường Thử" built from a closed way
    feats = build_admin(self.osm)
    self.assertEqual([(f["properties"]["name"], f["properties"]["admin_level"]) for f in feats], [("Phường Thử", 6)])
    self.assertEqual(points_in_geometry(np.array([LAT_IN]), np.array([LON_IN]), feats[0]["geometry"]).tolist(), [True])

def test_bbox_and_round_trip(self):
    self.assertNotIn(6, self.table.way_id.tolist())
    save_segments(self.table, self.root / "seg"); again = load_segments(self.root / "seg")
    self.assertEqual(again.sha256, self.table.sha256); np.testing.assert_array_equal(again.a, self.table.a)
    self.assertFalse(any(p.name.endswith(".tmp") for p in self.root.iterdir()))
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_segments -v`. Expected: ImportError.

- [ ] **Step 3: Implement `segments.py`**

- Use one `osmium.SimpleHandler` pass with `apply_file(..., locations=True, idx="flex_mem")`.
- Append to Python `array('q')`/`array('f')` buffers and convert to numpy at the end. This keeps memory near the final array size on the nationwide PBF.
- `find_segments` builds a sorted key per row on first use, `(way_id, min(a, b), max(a, b))` via `np.lexsort`, cached on the instance, and searches it with `np.searchsorted`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: OK.

---

### Task 3: Rules, vehicle groups and the SQLite state store

**Files:**
- Create: `speedmatrix/rules.py`, `speedmatrix/vehicles.py`, `speedmatrix/state.py`
- Test: `tests/test_sm_rules.py`, `tests/test_sm_state.py`

**Interfaces:**
- Produces (`vehicles.py`):
  - `KINDS = ("normal", "tipper", "tractor", "container", "pickup", "van")`;
  - `@dataclass(frozen=True) VehicleGroup(name: str, gross_t: float, payload_t: float, axles: int, kind: str, height_m: float, length_m: float, is_base: bool = False)`;
  - `matches(cond: dict, group: VehicleGroup) -> bool`.
    - `cond` maps an attribute to `[op, value]` or to a list of such pairs, all AND-ed.
    - Ops are `<`, `<=`, `>`, `>=`, `==`, `in`, `not_in`.
    - An empty `cond` matches every group.
- Produces (`rules.py`):
  - `RULE_KINDS = ("speed_set", "speed_pct", "cap", "ban")`, `LAYERS = ("official", "company", "edit")`, `CONFIDENCE = ("cao", "tb", "thap")`;
  - `class RuleError(ValueError)`;
  - `@dataclass Rule(id: str, kind: str, layer: str, body: dict, confidence: str = "cao", active: bool = True, source: str = "", document: str = "", note: str = "", valid_from: date | None = None, valid_to: date | None = None)`;
  - `Rule.in_force(as_of: date) -> bool`;
  - `validate_rule(rule: Rule) -> None`: raises `RuleError` with a message that names the bad field;
  - `rule_set_sha(rules: list[Rule]) -> str`: sha256 of the canonical JSON (`sort_keys`, separators `(",", ":")`) of the **active** rules sorted by id. Every field takes part, so changing the validity dates changes the hash;
  - `load_seed_file(path: Path) -> SeedFile`;
  - `@dataclass SeedFile(rules: list[Rule], groups: list[VehicleGroup], holidays: list[tuple[date, str]], unresolved: list[dict])`.
    - The seed format is `reports/truck_rules/RULE_FORMAT.md`, plus optional top-level `groups` and `holidays` keys.
    - Unknown top-level keys (`area`, `researched_at`, `comment`) are ignored.
- Produces (`state.py`):
  - `open_state(path: Path) -> State`: creates schema version 1. A newer schema raises `RuntimeError`.
  - `State` methods:
    - `upsert_rule(rule, author, reason)`;
    - `set_active(rule_id, active, author, reason)`;
    - `rules(active_only=True, kinds=None, as_of: date | None = None) -> list[Rule]`: with `as_of`, only rules in force on that date;
    - `add_unresolved(area: str, items: list[dict])` / `unresolved() -> list[dict]`: kept so that the UI can list what the research could not encode;
    - `history(rule_id) -> list[dict]`;
    - `revert(rule_id, history_id, author) -> Rule`;
    - `upsert_group(group)`;
    - `groups() -> list[VehicleGroup]`;
    - `add_holiday(day, name)`;
    - `holidays() -> set[date]`;
    - `record_speed_version(version: str, info: dict)`;
    - `speed_versions() -> list[dict]`;
    - `close()`.

**Rule bodies.** `validate_rule` checks exactly these:

| Kind | Required keys | Optional keys |
|---|---|---|
| `speed_set` | `scope`, `kmh` (> 0) | `when` |
| `speed_pct` | `scope`, `pct` (> −100) | `when`, `classes` |
| `cap` | `vehicle` (cond), `road_classes` ({class: kmh > 0}, classes from `ROAD_CLASSES[:5]`) | – |
| `ban` | `vehicle`, `scope`, `action` (`"close"` \| `"open"`) | `when`, `exempt` (`{"names": [...]}`) |

`scope` has **exactly one** of these keys:
- `segments`: `[[way_id, a, b], ...]`, used by UI edits;
- `polygon`: a GeoJSON geometry;
- `names`: a list of street names;
- `routes`: route pieces `{name | ref, from?, to?}`;
- `zone`: `{boundary_names, seed, area_km2}`;
- `admin`: `{province, units, except_refs?}`.

The meaning of each scope is defined in RULE_FORMAT.md and resolved in Task 5. Further checks:
- `bbox` is required with `names`, `routes` and `zone`;
- the scope may also have `classes` and `exclude_classes`;
- `exempt` may hold only `routes`;
- vehicle attributes must be fields of `VehicleGroup`, and `kind` values must be in `KINDS`;
- `when` must pass `when_mask` without error;
- `valid_from` ≤ `valid_to`.

**Schema** (`meta(key, value)` holds `schema=1`):
- `rules(id PK, kind, layer, body JSON, confidence, active INT, source, document, note, valid_from, valid_to, updated_at)`;
- `unresolved(area, item JSON)`: replaced per area on each import;
- `rule_history(hid INTEGER PK, rule_id, at, author, reason, action, snapshot JSON)`:
  - every upsert and activation change appends a full snapshot;
  - an upsert identical to the stored rule is a no-op and adds no row, which keeps `import-rules` re-runnable;
  - `revert` re-applies a snapshot as a new history row;
- `vehicle_groups(name PK, attrs JSON, is_base INT)`: there is exactly one base group; `upsert_group` with `is_base=True` clears the flag on the others;
- `holidays(day PK, name)`;
- `speed_versions(version PK, created_at, info JSON)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sm_rules.py
def test_vehicle_conditions(self):
    g = VehicleGroup("tu2t", gross_t=8, payload_t=5, axles=2, kind="normal", height_m=3.2, length_m=7.5)
    self.assertTrue(matches({"gross_t": [">=", 2]}, g)); self.assertFalse(matches({"gross_t": ["<", 2]}, g))
    self.assertTrue(matches({"kind": ["not_in", ["pickup"]], "gross_t": [[">", 3.5], ["<=", 10]]}, g))
    self.assertTrue(matches({}, g))

def test_validation_names_the_field(self):
    with self.assertRaisesRegex(RuleError, "scope"):
        validate_rule(Rule("r1", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"names": ["A"], "polygon": {}}}))
    with self.assertRaisesRegex(RuleError, "pct"):
        validate_rule(Rule("r2", "speed_pct", "edit", {"scope": {"names": ["A"]}, "pct": -100}))
    with self.assertRaisesRegex(RuleError, "when"):
        validate_rule(Rule("r3", "speed_set", "edit", {"scope": {"names": ["A"]}, "kmh": 20, "when": {"times": [["6", "9"]]}}))

def test_rule_set_sha_ignores_inactive_and_order(self):
    a = Rule("a", "speed_set", "edit", {"scope": {"names": ["X"]}, "kmh": 20})
    b = Rule("b", "speed_set", "edit", {"scope": {"names": ["Y"]}, "kmh": 30}, active=False)
    self.assertEqual(rule_set_sha([a, b]), rule_set_sha([a])); self.assertEqual(rule_set_sha([a]), rule_set_sha([a]))

def test_seed_low_confidence_imports_inactive(self):
    # seed with one "thap" rule without "active" and one "thap" with "active": true
    seed = load_seed_file(path)
    self.assertEqual({r.id: r.active for r in seed.rules}, {"low": False, "low_on": True})

def test_validity_dates(self):
    r = Rule("t", "ban", "official", BAN_BODY, valid_from=date(2026, 1, 15), valid_to=date(2026, 2, 28))
    self.assertTrue(r.in_force(date(2026, 1, 15))); self.assertFalse(r.in_force(date(2026, 3, 1)))
    with self.assertRaisesRegex(RuleError, "valid"):
        validate_rule(Rule("u", "ban", "official", BAN_BODY, valid_from=date(2026, 3, 1), valid_to=date(2026, 2, 1)))

def test_scope_kinds_need_bbox_and_known_vehicle_fields(self):
    with self.assertRaisesRegex(RuleError, "bbox"):
        validate_rule(Rule("r", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"routes": [{"name": "A"}]}}))
    with self.assertRaisesRegex(RuleError, "weight"):
        validate_rule(Rule("r", "ban", "official", {"vehicle": {"weight": [">", 1]}, "action": "close", "scope": {"names": ["A"], "bbox": BB}}))
    with self.assertRaisesRegex(RuleError, "kind"):
        validate_rule(Rule("r", "ban", "official", {"vehicle": {"kind": ["in", ["bus"]]}, "action": "close", "scope": {"names": ["A"], "bbox": BB}}))

# tests/test_sm_state.py
def test_history_and_revert(self):
    s = open_state(self.db)
    s.upsert_rule(Rule("r", "speed_set", "edit", {"scope": {"names": ["X"]}, "kmh": 20}), "an", "first")
    s.upsert_rule(Rule("r", "speed_set", "edit", {"scope": {"names": ["X"]}, "kmh": 35}), "an", "second")
    s.set_active("r", False, "an", "off")
    first = s.history("r")[0]["hid"]
    back = s.revert("r", first, "an")
    self.assertEqual(back.body["kmh"], 20); self.assertTrue(back.active); self.assertEqual(len(s.history("r")), 4)

def test_single_base_group_and_holidays(self):
    s = open_state(self.db)
    s.upsert_group(VehicleGroup("a", 1.9, 1, 2, "normal", 2.5, 5.0, is_base=True))
    s.upsert_group(VehicleGroup("b", 8, 5, 2, "normal", 3.2, 7.5, is_base=True))
    self.assertEqual([g.name for g in s.groups() if g.is_base], ["b"])
    s.add_holiday(date(2026, 9, 2), "Quốc khánh"); self.assertEqual(s.holidays(), {date(2026, 9, 2)})

def test_newer_schema_refuses(self):
    s = open_state(self.db); s._db.execute("UPDATE meta SET value='2' WHERE key='schema'"); s._db.commit(); s.close()
    with self.assertRaises(RuntimeError): open_state(self.db)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_rules tests.test_sm_state -v`. Expected: ImportError.

- [ ] **Step 3: Implement `vehicles.py`, `rules.py` and `state.py`**

- `validate_rule` is called by `State.upsert_rule` before writing.
- Timestamps are ISO-8601 with a `+07:00` offset.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: OK.

---

### Task 4: Seed files: vehicle groups, holidays and the nationwide rules

**Files:**
- Create: `data/rules/groups.json`, `data/rules/holidays.json`.
- Move: `reports/truck_rules/rules_{hanoi,north,central,south,national}.json` → `data/rules/official/{hanoi,north,central,south,national}.json`.
  - Leave a line in `reports/truck_rules/README.md` saying where they went.
  - The research notes (`research_*.md`) and `RULE_FORMAT.md` stay in `reports/truck_rules/`.
- Test: `tests/test_sm_seeds.py`

**Interfaces:**
- Consumes: `load_seed_file`, `validate_rule`, `matches` (Task 3); `when_mask` (Task 1).
- Produces the seed data that Tasks 8 and 9 import.

The nationwide rules were researched on 2026-10-21 into the five files above: **116 rules and 69 `unresolved` items**. All of them pass the format check (no duplicate ids). Breakdown by file:

| File | cao | tb | thap (inactive) | Main content |
|---|---|---|---|---|
| hanoi | 2 | 7 | 2 | QĐ 01/2026 + 52/2026 zone, 17 allowed pieces; bridges and works with dates |
| north | 0 | 10 | 6 | Hải Phòng KV1/KV2, ĐT.390 axles, Quảng Ninh, Bắc Giang streets, Sa Pa, Thái Nguyên |
| central | 16 | 10 | 6 | Vinh route lists, Hà Tĩnh wards (`admin`), Huế, Đà Nẵng, Quảng Ngãi, Lâm Đồng/Đà Lạt, Khánh Lê |
| south | 24 | 16 | 10 | HCM QĐ 23/2018 zone + 16 corridors, the 2026 draft (inactive), old Bình Dương, Đồng Nai, Cần Thơ, Bạc Liêu |
| national | 5 | 1 | 1 | three TT 38/2024 `cap` rules; Bến Thủy 1, Trường Tiền, CT.01 height, Cam Lộ–La Sơn axles |

Content of the two new files:

1. **`groups.json`**, the three groups around Hà Nội. Each `groups` entry gives `name`, `gross_t`, `payload_t`, `axles`, `kind`, `height_m`, `length_m` and `is_base`. These values are **examples for the user to confirm** (spec, open question 3):
   - `duoi2t`: 1.9 / 0.99 / 2 / normal / 2.6 / 5.0, base;
   - `tu2t`: 8.0 / 5.0 / 2 / normal / 3.4 / 7.5;
   - `container`: 40.0 / 27.0 / 5 / container / 4.2 / 16.5.

   Caps come from `national.json` (`vn_cap_*`), so this file has no cap rules.
2. **`holidays.json`**: `holidays` entries `["2026-01-01", "Tết Dương lịch"]`, `["2026-04-30", ...]`, `["2026-05-01", ...]`, `["2026-09-02", ...]`. The user adds each year's lunar holidays through `State.add_holiday`. A `comment` field says so.

- [ ] **Step 1: Write the failing test**

```python
def test_seed_files_load_and_validate(self):
    ids = []
    for path in sorted((ROOT / "data/rules").rglob("*.json")):
        seed = load_seed_file(path)
        for r in seed.rules: validate_rule(r); ids.append(r.id)
    self.assertEqual(len(ids), len(set(ids))); self.assertGreaterEqual(len(ids), 116)

def test_city_rules_hit_the_right_groups(self):
    rules = {r.id: r for p in (ROOT / "data/rules/official").glob("*.json") for r in load_seed_file(p).rules}
    groups = {g.name: g for g in load_seed_file(ROOT / "data/rules/groups.json").groups}
    hit = lambda rid: sorted(n for n, g in groups.items() if matches(rules[rid].body["vehicle"], g))
    self.assertEqual(hit("hn_zone_lt2t_peak"), ["duoi2t"]); self.assertEqual(hit("hn_zone_ge2t_day"), ["container", "tu2t"])
    self.assertEqual(hit("hcm_qd23_xe_tai_nhe"), ["duoi2t"]); self.assertEqual(hit("hcm_qd23_xe_tai_nang"), ["container", "tu2t"])
    self.assertEqual(hit("hcm_qd23_dau_keo_so_mi"), ["container"])
    self.assertEqual(np.flatnonzero(when_mask(rules["hn_zone_ge2t_day"].body["when"])[0]).tolist(), list(range(12, 42)))
    self.assertFalse(rules["hcm_du_thao_2026_tren_1t"].active)

def test_every_group_gets_exactly_one_national_cap(self):
    caps = [r for r in load_seed_file(ROOT / "data/rules/official/national.json").rules if r.kind == "cap"]
    for g in load_seed_file(ROOT / "data/rules/groups.json").groups:
        self.assertEqual(sum(matches(r.body["vehicle"], g) for r in caps), 1, g.name)

def test_exactly_one_base_group(self):
    self.assertEqual(sum(g.is_base for g in load_seed_file(ROOT / "data/rules/groups.json").groups), 1)
```

- [ ] **Step 2: Run the test and confirm it fails** (the files are missing)

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_seeds -v`.

- [ ] **Step 3: Write `groups.json` and `holidays.json`, then move the five rule files**

Do not edit the rule contents in this task. Task 9 fixes names against OSM.

- [ ] **Step 4: Run the test and confirm it passes**

Run the same command. Expected: OK.

---

### Task 5: Scope resolution, including zones sealed by their boundary roads

**Files:**
- Create: `speedmatrix/scopes.py`
- Test: `tests/test_sm_scopes.py`

**Interfaces:**
- Consumes: `SegmentTable`, `find_segments` (Task 2); `points_in_geometry` (Task 1); `RuleError` (Task 3).
- Produces:
  - `class ZoneError(ValueError)`;
  - `normalize_name(s: str) -> str`: NFC, casefold, collapse whitespace, strip one leading `"đường "` or `"phố "`;
  - `@dataclass ScopeResult(indices: np.ndarray[int64], orphans: list[dict])`: `indices` is sorted and unique;
  - `@dataclass ZoneResult(indices: np.ndarray[int64], area_km2: float, missing_names: list[str])`;
  - `zone_segments(table, boundary_names, seed, bbox, area_km2, cell_m=20.0, seal_m=60.0, water: WaterLines | None = None) -> ZoneResult`.
    - Each boundary entry is either a string or a route piece `{name | ref, from?, to?}`.
    - A string matches road segments by normalized name **or** by normalized ref (`"QL1"`), and water lines by normalized name.
    - A route piece is resolved with `route_piece`. Use a piece when only part of a long road bounds the zone, e.g. Đại lộ Thăng Long.
  - `normalize_ref(s: str) -> str`: `"QL.13"`, `"QL 13"` and `"ql13"` all give `"QL13"`; `"ĐT743"` and `"DT 743"` both give `"DT743"` (Đ is folded to D); an optional letter suffix is kept (`"QL1A"`).
  - `route_piece(table, piece: dict, bbox) -> tuple[np.ndarray[int64], dict | None]`: the segments of one route piece, plus an orphan dict or None.
  - `class ScopeResolver(table, admin: list[dict] | None = None, water: WaterLines | None = None)` with `resolve(scope: dict, exempt: dict | None = None) -> ScopeResult`. Results are cached by the canonical JSON of `(scope, exempt)`, so a zone shared by two rules is computed once.

**Scope semantics:**
- `segments`: anchors not found become orphans `{"anchor": [way_id, a, b]}`. The found ones are kept.
- `names`: ways whose normalized `name` or `ref` **equals** one of the normalized names, optionally limited by `bbox`. A name that matches nothing is an orphan `{"name": ...}`.
- `polygon`: segment midpoints inside the polygon.
- `zone`: `zone_segments(table, bbox=scope["bbox"], **scope["zone"])`. A `ZoneError` is **not** caught; it propagates.
- `routes`: the union of `route_piece` over the pieces. Each orphan is `{"route": piece, "missing": "name" | "from" | "to"}`.
- `admin`:
  - midpoints inside any feature whose `name` is in `units`, among features inside the province feature named `province` (admin_level 4);
  - then drop ways whose `normalize_ref(ref)` starts with any `normalize_ref(prefix)` in `except_refs`;
  - a unit not found is an orphan `{"unit": name}`;
  - with no `admin` data loaded, raise `RuleError("admin areas not loaded")`.
- After any of the above:
  - keep only `classes`, if given;
  - drop `exclude_classes`;
  - drop the segments of `exempt["routes"]`, resolved like `routes`. Orphans of exempt pieces are reported too.

**Route piece algorithm.** The piece is the named road between two crossings. Both carriageways of a dual road are kept.

```
H = segments in bbox whose way matches the piece (normalize_name(name) == or normalize_ref(ref) ==); none -> orphan "name"
no from/to -> all of H
F = nodes of H shared with a way in bbox whose name/ref matches `from` (same for T with `to`); empty -> orphan "from"/"to"
d_F, d_T = multi-source Dijkstra on H (undirected, length_m) from F and from T
D = min over T of d_F
keep the segments of H whose two ends both satisfy d_F + d_T <= 1.1 * D + 200 m
```

When only one of `from`/`to` is given, the piece runs from that crossing to the end of H farther from it.

**Zone algorithm.** The zone is the region the boundary roads enclose around the seed. Rasterizing the boundary lines makes the zone robust to roads that pass under or over the boundary without sharing a node.

```
project the bbox to a local metric plane (x = (lon-lon0)*111320*cos(lat0), y = (lat-lat0)*111320), grid of cell_m
blocked = cells touched by any boundary segment (sample each segment every cell_m/2)
blocked = binary_dilation(blocked, iterations=round(seal_m / cell_m))          # seals gaps up to ~2*seal_m
labels = scipy.ndimage.label(~blocked) (4-connectivity)
seed cell blocked                       -> ZoneError("seed lies on the boundary")
missing_names (no segment matches)      -> ZoneError(listing them)
region touches the grid border          -> ZoneError("zone leaks to the bbox edge: ...")
area = cells * cell_m^2 / 1e6 outside [min, max] -> ZoneError(area)
a boundary entry with no blocked cell 4-adjacent to the region -> ZoneError("boundary not adjacent: ...")
    (catches a boundary road that also runs through the zone and splits it, leaving part of the ring outside the region)
indices = segments whose midpoint cell is in the region (boundary roads themselves are blocked, so stay open)
```

- [ ] **Step 1: Write the failing tests**

The fixture is a synthetic `SegmentTable` built in the test from a helper, without a PBF:
- a 4 km × 4 km square ring of four named roads ("Vành đai A", "Vành đai B", "Vành đai C", "Vành đai D");
- a grid of inner streets, one named "Phố Miễn";
- one street outside the ring;
- one street that crosses under road B without sharing a node;
- a dual carriageway "Đại lộ X" crossing "Phố 1", "Phố 2" and "Phố 3";
- a road with `ref` "QL.99" that crosses "Phố 3";
- an `ADMIN_FIXTURE` with a province "Tỉnh T" (level 4) and a ward "Phường P" (level 6) covering part of the grid and part of QL.99.

```python
def test_zone_contains_inner_streets_only(self):
    z = zone_segments(self.t, ["Vành đai A", "Vành đai B", "Vành đai C", "Vành đai D"], SEED, BBOX, (5, 30))
    self.assertEqual(set(z.indices.tolist()), self.inner)          # outside street and ring roads excluded
    self.assertAlmostEqual(z.area_km2, 16.0, delta=1.5)

def test_underpass_does_not_leak(self):
    z = zone_segments(self.t, RING, SEED, BBOX, (5, 30))
    self.assertNotIn(self.outside_part_of_underpass, z.indices.tolist())

def test_gap_in_boundary_raises_instead_of_leaking(self):
    with self.assertRaisesRegex(ZoneError, "leaks"):
        zone_segments(self.t, RING[:3], SEED, BBOX, (5, 30))        # road D left out -> open side

def test_unknown_boundary_name_raises(self):
    with self.assertRaisesRegex(ZoneError, "Vành đai Z"):
        zone_segments(self.t, RING + ["Vành đai Z"], SEED, BBOX, (5, 30))

def test_river_side_and_ref_boundaries(self):
    # variant fixture: side D is a WaterLines river "Sông Thử" and side C carries only ref "QL.9"
    z = zone_segments(self.t2, ["Vành đai A", "Vành đai B", "QL9", "Sông Thử"], SEED, BBOX, (5, 30), water=self.water)
    self.assertEqual(set(z.indices.tolist()), self.inner2)

def test_boundary_road_crossing_the_zone_is_caught(self):
    # "Phố Giữa" runs straight through the middle; naming it as a boundary splits the zone
    with self.assertRaisesRegex(ZoneError, "not adjacent"):
        zone_segments(self.t, RING + ["Phố Giữa"], SEED, BBOX, (1, 30))

def test_area_bounds(self):
    with self.assertRaisesRegex(ZoneError, "area"):
        zone_segments(self.t, RING, SEED, BBOX, (20, 30))

def test_exempt_and_class_filters(self):
    r = ScopeResolver(self.t).resolve({"zone": ZONE, "bbox": BBOX, "exclude_classes": ["nho"]}, exempt={"routes": [{"name": "Phố Miễn"}]})
    self.assertTrue(set(r.indices.tolist()).isdisjoint(self.mien | self.nho)); self.assertEqual(r.orphans, [])

def test_route_piece_between_crossings_keeps_both_carriageways(self):
    # "Đại lộ X" is a dual carriageway crossing "Phố 1", "Phố 2", "Phố 3" in that order
    r = ScopeResolver(self.t).resolve({"routes": [{"name": "Đại lộ X", "from": "Phố 1", "to": "Phố 2"}], "bbox": BBOX})
    self.assertEqual(set(r.indices.tolist()), self.x_between_1_and_2_both_ways)

def test_route_piece_by_ref_and_missing_crossing(self):
    self.assertEqual(normalize_ref("QL.13"), "QL13"); self.assertEqual(normalize_ref("ĐT 743"), "DT743")
    r = ScopeResolver(self.t).resolve({"routes": [{"ref": "ql 99", "to": "Phố 3"}, {"name": "Đại lộ X", "from": "Phố Không"}], "bbox": BBOX})
    self.assertEqual(set(r.indices.tolist()), self.ql99_up_to_pho3)
    self.assertEqual(r.orphans, [{"route": {"name": "Đại lộ X", "from": "Phố Không"}, "missing": "from"}])

def test_admin_units_except_national_roads(self):
    res = ScopeResolver(self.t, admin=ADMIN_FIXTURE)   # province "Tỉnh T" containing "Phường P"
    r = res.resolve({"admin": {"province": "Tỉnh T", "units": ["Phường P", "Phường Q"], "except_refs": ["QL"]}})
    self.assertEqual(set(r.indices.tolist()), self.in_ward_p - self.ql99); self.assertEqual(r.orphans, [{"unit": "Phường Q"}])
    with self.assertRaisesRegex(RuleError, "admin"):
        ScopeResolver(self.t).resolve({"admin": {"province": "Tỉnh T", "units": ["Phường P"]}})

def test_missing_anchor_is_an_orphan(self):
    r = ScopeResolver(self.t).resolve({"segments": [[self.w, self.a, self.b], [999, 1, 2]]})
    self.assertEqual(len(r.indices), 1); self.assertEqual(r.orphans, [{"anchor": [999, 1, 2]}])

def test_names_match_after_normalisation(self):
    self.assertEqual(normalize_name("  Đường  Vành đai A "), normalize_name("vành đai a"))
    r = ScopeResolver(self.t).resolve({"names": ["vành đai a", "Không Có"]})
    self.assertEqual(r.orphans, [{"name": "Không Có"}]); self.assertGreater(len(r.indices), 0)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_scopes -v`.

- [ ] **Step 3: Implement `scopes.py`**

The name index is built once per resolver: normalized string → way indices, then mapped to segment indices with `np.isin(table.way, ways)`.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: OK.

---

### Task 6: Closures for one vehicle group

**Files:**
- Create: `speedmatrix/bans.py`
- Test: `tests/test_sm_bans.py`

**Interfaces:**
- Consumes: `SegmentTable`, `HGV_VALUES` (Task 2); `parse_conditional`, `when_mask` (Task 1); `VehicleGroup`, `matches`, `Rule` (Task 3); `ScopeResolver` (Task 5).
- Produces:
  - `HGV_GROSS_T = 3.5`: OSM `hgv` means vehicles above 3.5 t gross;
  - `@dataclass ClosureSet(idx: np.ndarray[int64] (k,), mask: np.ndarray[bool] (k, 3, 48))`: sorted, rows with an empty mask dropped;
  - `ClosureSet.closed_at(daytype: int, slot: int) -> np.ndarray[int64]`;
  - `osm_closures(table, group) -> tuple[ClosureSet, dict]`: the dict is a report `{"unparsed_conditional": int, "examples": [...] (≤ 20)}`;
  - `group_closures(table, group, rules: list[Rule], resolver: ScopeResolver) -> tuple[ClosureSet, dict]`: the report adds `"orphans": {rule_id: [...]}` and `"rules_applied": [rule_id, ...]`.

**Layers, in order.** Each layer overwrites the mask of the segments its rules touch.

1. **OSM**, applied to a group unless `group.kind == "pickup"` (NĐ 241/2026: pickups drive as cars):
   - `hgv=no` closes always when `gross_t > HGV_GROSS_T`. `hgv:conditional` parts then set the mask in their windows: `no` closes, `yes`/`designated`/`destination`/`delivery` open.
   - `maxweight_t < gross_t` closes always. Each `maxweight:conditional` part `X @ (...)` closes in its window when `gross_t > X`.
   - `maxheight_m < height_m` closes always. This applies to pickups too.
   - `hgv=destination|delivery` is treated as open.
   - A conditional that raises `ConditionalParseError` is counted and listed in the report, and **not applied**.
2. **Official** `ban` rules whose `vehicle` cond matches the group, sorted by id. All `close` rules first, then all `open` rules, so an exemption wins within its own layer.
3. **Company** `ban` rules, in the same way.

Rules of other kinds and inactive rules are ignored.

- [ ] **Step 1: Write the failing tests**

The fixture table has ways with `hgv=no`, with `maxweight=10`, with `maxheight=3.5`, with `maxweight:conditional=5 @ (06:00-09:00)` and with an unparseable conditional.

```python
def test_light_truck_ignores_hgv_and_weight_limits_it_meets(self):
    c, _ = osm_closures(self.t, DUOI2T)                      # 1.9 t, 2.6 m
    self.assertEqual(c.idx.size, 0)

def test_heavy_truck_closed_by_hgv_weight_height(self):
    c, _ = osm_closures(self.t, CONTAINER)                   # 40 t, 4.2 m
    self.assertTrue({self.hgv_no, self.mw10, self.mh35} <= set(c.idx.tolist()))
    self.assertTrue(c.mask[c.idx.tolist().index(self.mw10)].all())

def test_conditional_weight_only_in_its_window(self):
    c, _ = osm_closures(self.t, TU2T)                        # 8 t
    m = c.mask[c.idx.tolist().index(self.mwcond5)]
    self.assertEqual(np.flatnonzero(m[0]).tolist(), [12, 13, 14, 15, 16, 17])

def test_unparsed_conditional_is_reported_not_applied(self):
    c, rep = osm_closures(self.t, CONTAINER)
    self.assertEqual(rep["unparsed_conditional"], 1); self.assertNotIn(self.badcond, c.idx.tolist())

def test_pickup_keeps_height_limit_only(self):
    c, _ = osm_closures(self.t, PICKUP_4T)                   # 4 t gross, kind pickup, 2.0 m
    self.assertEqual(c.idx.tolist(), [])

def test_official_close_then_company_open_wins(self):
    rules = [Rule("o1", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"names": ["Phố A"]}, "when": {"times": [["06:00", "09:00"]]}}),
             Rule("c1", "ban", "company", {"vehicle": {}, "action": "open", "scope": {"names": ["Phố A"]}, "when": {"times": [["08:00", "09:00"]]}})]
    c, rep = group_closures(self.t, TU2T, rules, ScopeResolver(self.t))
    row = c.mask[c.idx.tolist().index(self.pho_a)][0]
    self.assertEqual(np.flatnonzero(row).tolist(), [12, 13, 14, 15]); self.assertEqual(rep["rules_applied"], ["o1", "c1"])

def test_open_wins_inside_one_layer_and_vehicle_cond_filters(self):
    rules = [Rule("a", "ban", "official", {"vehicle": {"gross_t": [">=", 2]}, "action": "close", "scope": {"names": ["Phố A"]}}),
             Rule("b", "ban", "official", {"vehicle": {}, "action": "open", "scope": {"names": ["Phố A"]}, "when": {"times": [["00:00", "06:00"]]}})]
    c, _ = group_closures(self.t, DUOI2T, rules, ScopeResolver(self.t)); self.assertNotIn(self.pho_a, c.idx.tolist())
    c, _ = group_closures(self.t, TU2T, rules, ScopeResolver(self.t))
    self.assertFalse(c.mask[c.idx.tolist().index(self.pho_a)][:, :12].any())
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_bans -v`.

- [ ] **Step 3: Implement `bans.py`**

- Work on a dict `segment index → (3, 48) bool` for the touched segments only, then pack it into a `ClosureSet`.
- Parse each distinct conditional string once and cache it by string index.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: OK.

---

### Task 7: The base speed layer and its version

**Files:**
- Create: `speedmatrix/speeds.py`
- Test: `tests/test_sm_speeds.py`

**Interfaces:**
- Consumes: `SegmentTable`, `ROAD_CLASSES` (Task 2); `when_mask`, `DAYTYPES` (Task 1); `Rule`, `rule_set_sha` (Task 3); `ScopeResolver` (Task 5); `model.osm_classes.free_flow_speed`; `model.spatial.MissingRoadData`.
- Produces:
  - `SPEED_CODE_VERSION = 1`; `FERRY_KMH = 5` (car.lua `speeds.route.ferry`; a test keeps them equal);
  - `CLASS_LIMITS_KMH = {"caotoc": (20, 120), "quoclo": (10, 100), "chinh": (5, 90), "noido": (5, 70), "nho": (3, 50), "pha": (3, 30)}`;
  - `class BaseSource(Protocol)`: `id: str`; `speeds(table: SegmentTable, daytype: str) -> np.ndarray[float32] (n, 48)`. NaN means no data;
  - `class FreeFlowSource`: `id = "profile_free_flow:v1"`. It returns the same `free_flow_speed(highway, maxspeed)` value in every slot, and `FERRY_KMH` for ferries. `free_flow_speed` takes the way's maxspeed string, which comes from `maxspeed_kmh` formatted back as `"%g"`, or `""` when NaN;
  - `@dataclass SpeedLayer`:
    - `version: str`, `source_id: str`, `n: int`;
    - `base: dict[str, np.ndarray[uint8] (n,)]`;
    - `dyn_idx: dict[str, np.ndarray[int64] (m,)]`, `dyn: dict[str, np.ndarray[uint8] (m, 48)]`;
    - `rule_offsets: np.ndarray[int64] (n+1,)`, `rule_ids: list[str]` (a CSR of the speed rules applied to each segment);
    - `stats: dict` with `clamped` per class, `dynamic_segments` per daytype, `orphans` per rule;
  - `SpeedLayer.at(daytype: str, slot: int) -> np.ndarray[uint8] (n,)` and `SpeedLayer.rows(daytype: str, idx: np.ndarray) -> np.ndarray[uint8] (k, 48)`;
  - `speed_version(table, source, rules: list[Rule]) -> str`: sha256 over `table.sha256`, `source.id`, `rule_set_sha` of the `speed_set`/`speed_pct` rules, the canonical JSON of `CLASS_LIMITS_KMH`, and `SPEED_CODE_VERSION`;
  - `build_speed_layer(table, source, rules, resolver) -> SpeedLayer`;
  - `save_layer(layer, out_dir)` / `load_layer(out_dir) -> SpeedLayer`.

**Build order** for each daytype:
1. `v = source.speeds(table, daytype)`. Any NaN raises `MissingRoadData` with the count and up to 5 `(way_id, a, b)` examples.
2. `speed_pct` rules, sorted by id: inside the rule's scope (and `classes` filter) and `when_mask[daytype]` slots, multiply by `(1 + pct/100)`. Rules compound.
3. `speed_set` rules, sorted by id: inside their scope and slots, set `v = kmh`.
4. Clamp each segment to `CLASS_LIMITS_KMH[class]`, counting clamped `(segment, slot)` cells per class.
5. Round to uint8.
6. Rows whose 48 values are not all equal go to `dyn_idx`/`dyn`; `base` holds column 0 for every segment.

Inactive rules and other kinds are ignored. Orphans from the resolver go to `stats["orphans"]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_free_flow_layer_is_static_and_uses_maxspeed(self):
    L = build_speed_layer(self.t, FreeFlowSource(), [], ScopeResolver(self.t))
    self.assertEqual(L.dyn_idx["weekday"].size, 0)
    self.assertEqual(int(L.at("weekday", 17)[self.primary]), 65); self.assertEqual(int(L.at("sun", 0)[self.max60]), 48)
    self.assertEqual(int(L.at("sat", 3)[self.ferry]), FERRY_KMH)

def test_pct_then_set_only_in_their_slots_and_classes(self):
    rules = [Rule("p", "speed_pct", "edit", {"scope": {"polygon": self.box}, "classes": ["chinh"], "pct": -50, "when": {"daytypes": ["weekday"], "times": [["07:00", "08:00"]]}}),
             Rule("s", "speed_set", "edit", {"scope": {"names": ["Phố A"]}, "kmh": 12})]
    L = build_speed_layer(self.t, FreeFlowSource(), rules, ScopeResolver(self.t))
    row = L.rows("weekday", np.array([self.primary]))[0]
    self.assertEqual(int(row[14]), 32); self.assertEqual(int(row[13]), 65); self.assertEqual(int(L.at("sat", 14)[self.primary]), 65)
    self.assertTrue((L.rows("sun", np.array([self.pho_a]))[0] == 12).all())
    self.assertEqual(L.rule_ids[L.rule_offsets[self.primary]:L.rule_offsets[self.primary + 1]], ["p"])

def test_clamp_is_counted_and_zero_never_appears(self):
    rules = [Rule("z", "speed_pct", "edit", {"scope": {"names": ["Ngõ B"]}, "pct": -99.9})]
    L = build_speed_layer(self.t, FreeFlowSource(), rules, ScopeResolver(self.t))
    self.assertEqual(int(L.at("weekday", 0)[self.ngo_b]), 3); self.assertGreater(L.stats["clamped"]["nho"], 0)
    self.assertTrue(all((L.base[d] > 0).all() for d in DAYTYPES))

def test_missing_speed_raises(self):
    class Holey(FreeFlowSource):
        id = "holey"
        def speeds(self, table, daytype):
            v = super().speeds(table, daytype); v[0, 5] = np.nan; return v
    with self.assertRaises(MissingRoadData): build_speed_layer(self.t, Holey(), [], ScopeResolver(self.t))

def test_version_changes_with_active_speed_rules_only(self):
    v0 = speed_version(self.t, FreeFlowSource(), [])
    ban = Rule("b", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"names": ["Phố A"]}})
    self.assertEqual(speed_version(self.t, FreeFlowSource(), [ban]), v0)
    s = Rule("s", "speed_set", "edit", {"scope": {"names": ["Phố A"]}, "kmh": 12})
    self.assertNotEqual(speed_version(self.t, FreeFlowSource(), [s]), v0)

def test_round_trip_and_ferry_speed_matches_profile(self):
    L = build_speed_layer(self.t, FreeFlowSource(), [], ScopeResolver(self.t)); save_layer(L, self.root / "L")
    M = load_layer(self.root / "L"); self.assertEqual(M.version, L.version); np.testing.assert_array_equal(M.base["sat"], L.base["sat"])
    # read `ferry = <n>` in the route block of osrm/tools/profiles/car.lua; skip if absent; assertEqual(n, FERRY_KMH)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_speeds -v`.

- [ ] **Step 3: Implement `speeds.py`**

`build_speed_layer` sets `layer.version = speed_version(...)`. Saving writes `layer.npz` and `layer.json` (version, source_id, stats, rule_ids).

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: OK.

---

### Task 8: Group views and the build CLI

**Files:**
- Create: `speedmatrix/groups.py`, `speedmatrix/build.py`
- Test: `tests/test_sm_groups.py`, `tests/test_sm_build.py`

**Interfaces:**
- Consumes: everything above.
- Produces (`groups.py`):
  - `@dataclass GroupView`:
    - `group: VehicleGroup`, `speed_version: str`;
    - `caps: dict[str, float]`: the minimum over the matching `cap` rules, per class;
    - `closures: ClosureSet`;
    - `capped_idx: np.ndarray[int64]`: segments where the cap is below the base speed in some slot of some daytype;
    - `affected_idx: np.ndarray[int64]`: `capped_idx` ∪ `closures.idx`;
    - `report: dict`.
  - `build_group_view(table, layer, group, rules, resolver) -> GroupView`.
  - `group_speeds(view, layer, table, daytype: str, slot: int) -> np.ndarray[uint8] (n,)`: `min(base, cap)`, with 0 on segments closed in that slot.
  - `save_group_view(view, path: Path)` / `load_group_view(path) -> GroupView`.
- Produces (`build.py`, run as `python -m speedmatrix.build <command>`):
  - the root is `--root`, default `runtime/matrix`; the state file is `<root>/state.sqlite`;
  - `segments --pbf PATH [--bbox W S E N]` writes `<root>/segments/<sha256[:12]>/` (skipped if present), including `admin.json`, and prints its path;
  - `import-rules FILE...` upserts the rules, groups and holidays of the seed files and replaces the file's `unresolved` items, with author `"seed"` and reason = the file name. It is re-runnable, and an unchanged rule adds no history row;
  - `speeds --segments DIR [--as-of YYYY-MM-DD]` builds the layer with `FreeFlowSource` and the active state rules in force on `--as-of` (default today), writes `<root>/speeds/<version[:12]>/` (skipped if present), records it with `record_speed_version`, and writes `report.json` (stats, orphans, as_of);
  - `groups --segments DIR --speeds DIR [--as-of YYYY-MM-DD]` writes `<speeds>/groups/<as_of>/<name>.npz` for every group in the state, plus `report.json` with closures per rule, orphans per rule and the OSM report;
  - `rules-check --segments DIR [--as-of ...]` resolves every active rule's scope and prints one line per rule: id, segments matched, orphans, `ZoneError` if any. It exits 2 if an active `cao`/`tb` rule has an orphan or a zone error. This is the command Task 9 iterates on;
  - `status` prints the segment builds, the speed versions with dates, the groups and the count of active rules per layer.
  - Exit code 0 on success. A `MissingRoadData`, `RuleError` or `ZoneError` prints its message and exits 2, leaving no partial output directory.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sm_groups.py
def test_cap_binds_only_where_base_is_faster(self):
    v = build_group_view(self.t, self.L, CONTAINER, self.rules, ScopeResolver(self.t))
    self.assertEqual(v.caps["caotoc"], 80)
    self.assertIn(self.motorway, v.capped_idx.tolist()); self.assertNotIn(self.residential, v.capped_idx.tolist())
    self.assertEqual(int(group_speeds(v, self.L, self.t, "weekday", 10)[self.motorway]), 80)

def test_closed_slot_is_zero_and_base_group_untouched(self):
    v = build_group_view(self.t, self.L, TU2T, self.rules_with_zone_ban, ScopeResolver(self.t))
    s = group_speeds(v, self.L, self.t, "weekday", 14)
    self.assertEqual(int(s[self.inner_street]), 0); self.assertGreater(int(group_speeds(v, self.L, self.t, "weekday", 44)[self.inner_street]), 0)
    b = build_group_view(self.t, self.L, DUOI2T, self.rules, ScopeResolver(self.t))
    self.assertEqual(b.affected_idx.size, 0)

def test_round_trip(self):
    v = build_group_view(self.t, self.L, CONTAINER, self.rules, ScopeResolver(self.t)); save_group_view(v, self.root / "c.npz")
    w = load_group_view(self.root / "c.npz"); np.testing.assert_array_equal(w.affected_idx, v.affected_idx); self.assertEqual(w.caps, v.caps)

# tests/test_sm_build.py (runs main(argv) in-process on the tiny .osm fixture)
def test_end_to_end_and_idempotent(self):
    self.assertEqual(main(["--root", R, "segments", "--pbf", OSM]), 0)
    self.assertEqual(main(["--root", R, "import-rules", SEED]), 0); self.assertEqual(main(["--root", R, "import-rules", SEED]), 0)
    self.assertEqual(len(open_state(R / "state.sqlite").history("hn_test")), 1)
    self.assertEqual(main(["--root", R, "speeds", "--segments", SEG]), 0)
    self.assertEqual(main(["--root", R, "groups", "--segments", SEG, "--speeds", SPD]), 0)
    self.assertTrue((SPD / "groups" / TODAY / "container.npz").is_file())

def test_as_of_excludes_rules_out_of_force(self):
    # seed holds a ban valid 2026-01-01..2026-01-31 on the fixture's only primary road
    main(["--root", R, "groups", "--segments", SEG, "--speeds", SPD, "--as-of", "2026-01-15"])
    main(["--root", R, "groups", "--segments", SEG, "--speeds", SPD, "--as-of", "2026-02-15"])
    jan, feb = (load_group_view(SPD / "groups" / d / "tu2t.npz") for d in ("2026-01-15", "2026-02-15"))
    self.assertIn(PRIMARY, jan.closures.idx.tolist()); self.assertNotIn(PRIMARY, feb.closures.idx.tolist())

def test_rules_check_fails_on_orphans_of_trusted_rules(self):
    self.assertEqual(main(["--root", R, "rules-check", "--segments", SEG]), 2)   # orphan_rule is "tb"

def test_orphans_are_listed_in_build_report(self):
    # seed holds a speed_set rule anchored on [999, 1, 2]
    report = json.loads((SPD / "report.json").read_text(encoding="utf-8"))
    self.assertEqual(report["orphans"]["orphan_rule"], [{"anchor": [999, 1, 2]}])

def test_zone_error_exits_2_without_partial_output(self):
    # seed holds a ban whose zone names a road absent from the fixture
    self.assertEqual(main(["--root", R2, "groups", "--segments", SEG2, "--speeds", SPD2]), 2)
    self.assertFalse((SPD2 / "groups").exists())
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_groups tests.test_sm_build -v`.

- [ ] **Step 3: Implement `groups.py` and `build.py`**

- The CLI shares one `ScopeResolver` per run, so a zone is computed once for all groups.
- The `groups` output is written to `<speeds>/groups.tmp/` and renamed at the end.

- [ ] **Step 4: Run the tests and confirm they pass, then run the whole suite**

Run `.venv/Scripts/python.exe -m unittest discover -s tests`. Expected: OK. No earlier test may break.

---

### Task 9: Run on the real map and make every trusted rule resolve

**Files:**
- Modify: `data/rules/official/*.json`. Only names, refs, from/to pieces and zone boundary entries may change, to match OSM. Thresholds, times and vehicle conditions stay as the research found them.
- Create: `reports/speedmatrix_build.md` (results, in Vietnamese like the other reports)

**Interfaces:**
- Consumes: the CLI of Task 8 and the seed files of Task 4.

- [ ] **Step 1: Build the nationwide segments and import every seed**

Run in the background:
```
.venv/Scripts/python.exe -m speedmatrix.build segments --pbf osrm/data/vietnam-latest.osm.pbf
.venv/Scripts/python.exe -m speedmatrix.build import-rules data/rules/groups.json data/rules/holidays.json data/rules/official/hanoi.json data/rules/official/north.json data/rules/official/central.json data/rules/official/south.json data/rules/official/national.json
```

Expected:
- both exit 0;
- `status` shows 116 or more rules;
- record the wall time and peak memory of `segments`.

If memory goes above 32 GB, stop and record where; do not tune blindly.

- [ ] **Step 2: Iterate `rules-check` until every active `cao`/`tb` rule resolves**

Run `.venv/Scripts/python.exe -m speedmatrix.build rules-check --segments runtime/matrix/segments/<id>`.

For each orphan or `ZoneError`:
1. Look up the OSM spelling or ref inside the rule's bbox, from the segment table (a short scratchpad script over `strings`).
2. Fix the seed file: change the spelling, swap a name for a ref, turn a long boundary road into a route piece, or give a landmark its crossing street.
3. Re-run `import-rules` and `rules-check`.

Repeat until it exits 0.

A piece with no OSM counterpart at all (an unnamed road, a landmark with no street) gets two changes:
- a `note` in the rule;
- the rule is lowered to `thap`, which makes it inactive.

Do not guess geometry. Log every change, with its reason, in the report.

Known items to resolve, from the research:
- **Hà Nội:**
  - Hoàng Tùng is also called Vành đai 3,5;
  - Đường 70;
  - the Đại lộ Thăng Long service roads;
  - two streets are named Lê Trọng Tấn; only the Hà Đông one is a boundary, so use a route piece;
  - Thanh Lâm and the CN3–CN5 pieces.
- **TP.HCM:** the boundary entry "QL1" (a ref).
- **Old Bình Dương:** `ĐT743`, "Bến Đò", "Miếu Ông Cù".
- **Hải Phòng KV2:** the river side "Sông Sặt" (water lines); the seed point is estimated.
- **Đà Nẵng:** street names carry a "Đường " prefix in OSM, which `normalize_name` strips.
- **Central refs** are written "QL.49" in OSM, which `normalize_ref` folds.

- [ ] **Step 3: Spot-check the two big zones**

From the `tu2t` group view (built in Step 4), check:
- **Hà Nội:**
  - weekday slot 16 (08:00) is closed on Phố Huế;
  - it is open on Vành đai 3 itself and on QL1A south of Ngọc Hồi;
  - slot 44 (22:00) is open on Phố Huế.
- **TP.HCM:**
  - weekday slot 16 is closed on Nguyễn Huệ;
  - it is open on Trần Xuân Soạn between Huỳnh Tấn Phát and Lê Văn Lương at slot 20 (10:00, corridor `hcm_hl_tran_xuan_soan`, 9h–16h), but closed there at slot 16;
  - it is open on Mai Chí Thọ at slot 16, because Mai Chí Thọ is a boundary road and the corridor rule only repeats that.

Use a short script run from the scratchpad. Add the results to the report.

- [ ] **Step 4: Speeds and group views, nationwide**

Run in the background:
- `speeds --segments <id>`;
- `groups --segments <id> --speeds <v>` for `--as-of` today and for `--as-of 2026-10-01`. Rules such as the Cầu Thăng Long works make the two dates differ.

Record:
- wall time and peak memory per step;
- output sizes;
- per-group affected-segment counts;
- the OSM report: unparsed conditionals and examples;
- the segments closed per rule for the ten largest rules.

- [ ] **Step 5: Write `reports/speedmatrix_build.md`**

The report holds:
- the commands;
- the numbers from Steps 1–4;
- every seed change made in Step 2;
- the 69 research `unresolved` items, grouped by area: they are still not encoded;
- the open items waiting for the user: group attribute values, lunar holidays, and the rules whose reading was chosen as the stricter one:
  - Hải Phòng over 1.5 t;
  - tractors in Hà Nội;
  - vans under NĐ 241/2026;
  - the Đà Lạt street pairs.
