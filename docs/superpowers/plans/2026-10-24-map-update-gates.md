# Map Update and Gates Implementation Plan (sub-project 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly, unattended map update for the `speedmatrix` package. Each run:
- takes a Vietnam PBF from Geofabrik;
- builds the matrix OSRM dataset;
- reads OSRM's directed turn graph;
- carries the frozen 22 regions and about 1,000 subregions over to the new map, and re-cuts the about 14,000 level-3 cells with min-cut only where the graph changed;
- derives the gates of every level;
- re-resolves every rule and reports orphans;
- rebuilds the speed layer and group views;
- checks the graph against OSRM;
- puts the release into use only when every blocking check passes.

**Architecture:**
- New modules in the existing `speedmatrix/` package, one responsibility each:
  - `osrmfiles`: reads the OSRM tar blocks;
  - `matrix_osrm`: the patched matrix profile build;
  - `graph`: the directed turn graph;
  - `staticroute`: a single-slot reference router;
  - `mincut`: the cell splitter;
  - `partition`: labels and partition rules;
  - `gates`;
  - `rulecheck`: rule re-resolution;
  - `verify_osrm`: spec §12.1;
  - `update`: the release pipeline;
  - `sysres`: workers, memory, disk.
- Heavy steps run as subprocesses of `python -m speedmatrix.update`. Each step writes into `runtime/matrix/releases/<id>/`. `current.json` moves only after the checks pass.
- The spike code in `reports/gate_compose_spike/` (gitignored, not in the public repo) is ported, not imported: `build_matrix_osrm.py`, `matrix_graph.py`, `refine_mincut.py`, `hier_split.py`, `hier_split_l3.py`, `apply_overrides.py` and `update_matrix.py`.

**Tech Stack:** Python 3.14 (`.venv`), numpy, scipy (`sparse.csgraph`, `cluster.vq`, `spatial.cKDTree`), osmium, sqlite3, ctypes (Windows process and memory calls), unittest. OSRM v26.10.0 binaries in `osrm/tools/`.

**Spec:** `docs/superpowers/specs/2026-10-21-speed-matrix-system-design.md`.
- This plan covers §6 (sub-project 1) and §12.1.
- It also covers §5.1 (release layout), §3 (segments, gates, cells), the §13 errors it can raise and the §14 tests of reading, partitioning and rules.
- Sub-project 0's plan, `docs/superpowers/plans/2026-10-21-speed-layer-vehicle-rules.md`, is built. Its results are in `reports/speedmatrix_build.md`.

## Global Constraints

- **Git:**
  - the repo is public on GitHub (mrdo19111911/hanoi-traffic-model);
  - every task ends with the whole suite green (`.venv/Scripts/python.exe -m unittest discover -s tests`), then a commit and a push to `origin/main`;
  - stage files by explicit path, never `git add -A`;
  - before the commit, `git diff --cached --name-only` must list no `data/*.json*`, `reports/spatial_crawl.json`, `reports/gate_compose_spike/` or `runtime/` path;
  - commit messages end with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (use a second `-m`);
  - if the push is rejected, run `git pull --rebase --autostash origin main` and push again. Never stage, restore or commit files this task did not create or edit (other sessions edit `buildings/` without committing). If the autostash pop conflicts, stop and ask.
- **Network:** the only allowed network calls go to `https://download.geofabrik.de/asia/vietnam-latest.osm.pbf` and its `.md5` (`osrm/manage.py` `MAP_URL`). Any other host raises `ValueError`. No TomTom call, ever. Exception text from `urllib` or `OSError` is never stored, printed or logged (spec §13); a job records the exception class and a fixed message instead.
- **Ports:**
  - never start, stop, query or kill anything on 5000 (production OSRM) or 8000 (web app); `PROTECTED_PORTS = (5000, 8000)` makes every routed start and every client raise `ValueError`;
  - defaults: 5058 for the build/verify routed, 5059 for the served matrix OSRM, 5061 for the real-data smoke test;
  - a port already in use raises; nothing is ever killed that this run did not start (check process identity with `osrm/manage.py` `process_identity`).
- **OSRM version:** pinned to `v26.10.0`. `osrmfiles.check_build_json` refuses any other `input.osrm_version`.
- **No LLM** and no manual step in the update. Every map fix (labels, gates, regions, polygons) is a rule in code, re-run on every map. The frozen polygon file and seed tables are inputs and are never edited by hand.
- **Core invariant (spec §7.2, §13):**
  - missing or ambiguous data raises, or is resolved by a stated rule and counted;
  - nothing is silently substituted;
  - speeds are never borrowed across road classes.
- **Dependencies:** numpy, scipy and osmium plus the standard library. No shapely, pandas, networkx or psutil.
- **Language:**
  - code, docstrings, CLI output and logs are in English;
  - user-facing text is in Vietnamese: reports in `reports/`, future API/UI strings.
- **Parallelism and memory:**
  - workers = `max(1, min(round(0.6*os.cpu_count()), floor(0.6 * available_GB / per_worker_GB)))`, which is 14 on this machine when memory allows (`sysres.default_workers`);
  - `--workers N` overrides it;
  - each parallel step states its `per_worker_GB`;
  - the machine has 31.9 GB shared with other sessions (5.1 GB free on 2026-10-24); a step whose measured peak goes above 24 GB stops the run;
  - the OSRM build starts only when available memory is at least the last recorded `osrm-extract` peak plus 1 GB (13.1 + 1 = 14.1 GB from the 2026-10-21 build when no earlier `build_matrix.json` exists); otherwise the run fails with exit 2 before building (Decision 29);
  - each worker carries one unit end to end; no pools are stacked on pools.
- **Disk:**
  - large outputs go under the gitignored `runtime/matrix/`;
  - `update run` prunes first, then refuses to start when free space on the root's drive is below 1.5 × the measured size of the current release (its release dir plus the segments, speeds and groups dirs it references), or 1.5 × 12 GB = 18 GB when there is no current release; `--min-free-gb` overrides it (E: had 16 GB free on 2026-10-24, before the 6.7 GB spike release is pruned);
  - on Windows a memory-mapped `.npy` cannot be deleted while it is mapped: code drops its maps before deleting, tests clean up with `ignore_errors=True` (or `TemporaryDirectory(ignore_cleanup_errors=True)`), and `prune` skips a locked dir with a warning instead of failing the run;
  - every output directory is written to a temp sibling and renamed into place.
- **Traceability:** `release.json` records:
  - the PBF md5 and sha256;
  - `profiles_sha256`;
  - `osrm_version`;
  - the graph sha;
  - the segments, speeds and groups dirs;
  - `code_version` (`git rev-parse HEAD` plus `+dirty` when `git status --porcelain -- speedmatrix` is not empty);
  - every check with `value`, `ok` and `blocking`.
- **A failed run never changes `current.json`, the served release or `state.sqlite` rules.**
- **Tests:**
  - offline: no OSRM, network or API key;
  - OSRM datasets are tiny tars written by `tests/sm_osrm_fixture.py`, whose format matches the real one;
  - OSM inputs are tiny `.osm` XML files written by `sm_osrm_fixture.write_osm`; new tests import that one, and the older copies in `tests/test_sm_segments.py` and `tests/test_sm_build.py` stay as they are;
  - real-data tests run only when `SM_REAL_DATA=1` and their files exist;
  - a test that needs the fixture helper adds its own directory to `sys.path` first: `sys.path.insert(0, str(Path(__file__).resolve().parent))`.
- **Imports:**
  - new modules follow the existing pattern: a relative import first, a flat import on `ImportError`;
  - `from model.clusters import _RiverIndex`; `osrm/manage.py` is imported by adding `osrm/` to `sys.path`, as the spike did;
  - `speedmatrix/update.py` also runs as a script (scheduled task), so it puts the repo root on `sys.path` like `build.py`.
- **Day types and slots:** as sub-project 0. `("weekday", "sat", "sun")`, 48 slots of 1800 s, local time UTC+7, timestamps ISO-8601 with `+07:00`.

## Review Focus

1. **An OSM way split between two maps** (same nodes, new way ids; the most common weekly edit). Expected:
   - labels carry over unchanged, because they are keyed by node id;
   - no L2 cell counts as changed;
   - a segment-anchored rule still finds its segment through the node pair and is not reported as an orphan.

   Tested in Task 7 (`test_carry_over_ignores_way_ids`) and Task 9 (`test_way_split_anchor_found_by_node_pair`).
2. **A Geofabrik download that is cut off, or whose md5 does not match.** Expected:
   - the run fails with exit 2;
   - the `.part` file is removed and no PBF remains under `releases/`;
   - `current.json` and the served release are untouched;
   - the job row holds the error class and message.

   Tested in Task 11 (`test_md5_mismatch_keeps_current_and_removes_part`, `test_network_error_is_recorded_without_its_text`).
3. **An edit that cuts a cell in two** (a bridge removed, a road deleted). Expected: the stray piece joins the sibling cell it shares the most edges with, the contiguity check passes and the L1 count is unchanged. An island with no link at all is not a violation. Tested in Task 7 (`test_removed_bridge_fragment_joins_most_connected_sibling`, `test_island_without_link_is_not_a_violation`).
4. **A trusted zone rule that breaks on the new map** (a boundary road renamed or removed → `ZoneError`). Expected: the run stops right after the rules step with exit 1 (`failed_checks`), the release is not promoted, the rule report names the rule, and the running release keeps working. Tested in Task 9 (`test_zone_error_is_blocking`) and Task 11 (`test_rule_error_blocks_promotion_and_stops_the_run`).
5. **A crash during a run leaves `update.lock` behind, or the scheduled run starts while a manual run is going.** Expected:
   - a lock whose process is gone (or has another identity) is taken over;
   - a live lock makes the second run exit 2 without touching anything: no release dir, no job row, and `update.lock` byte-identical.

   Tested in Task 11 (`test_stale_lock_is_taken_over`, `test_live_lock_refuses_without_touching_anything`).

## Decisions taken in this plan

Each line gives the decision and what it costs if it is wrong.

1. **Graph granularity:** the turn graph is stored at OSRM edge-based-node (EBN) level. Each EBN carries a CSR slice of its directed segments in travel order. Moving along a geometry is an implicit arc with cost 0. The graph is a directory of `.npy` files (memory-mappable by workers), not one `graph.npz`. Spec §5.1 is amended to match. *Cost:* sub-project 2 derives segment-level adjacency in O(n) if it needs it.
2. **OSRM v26 node entries:** each geometry stores every interior node twice, and the zero-length "node entry" between the copies carries that node's obstacle duration (traffic signal, stop, barrier). Each node entry is folded into the real segment travelled just before it (`seg_fixed_ds`). *Cost:* if OSRM charged them elsewhere, the per-EBN identity test and §12.1 fail loudly, and only `graph.py` changes.
3. **Road class and segment identity:** they come from the nationwide `SegmentTable` (sub-project 0), whose `pbf_sha256` must equal the matrix build's, not from `osrm/data/road_classes.sqlite`, which covers the Hà Nội bbox only and serves the web app. Spec §6.4 is amended to match. *Cost:* none for the web app; a stale table stops the graph build.
4. **`SEGMENTS_FORMAT` 2:** the table also keeps the highway values OSRM routes but the speed table lacks: `track`, `path`, `footway`, `pedestrian`, `cycleway`, `bridleway`, `steps`, `busway`, `bus_guideway`, `corridor` and `road`. They take class `nho` and car.lua `default_speed` (10 km/h); the 2026-10-21 graph had about 148k such pairs. *Cost:* a slightly larger table, and every speed version changes once.
5. **Node pairs found on several ways:** the graph takes the row whose free-flow time is closest to OSRM's own segment duration, ties going to the lowest way id, and counts them. *Cost:* a rare wrong class on duplicated ways.
6. **Frozen L1/L2 as node tables:**
   - L1 and L2 labels are carried over by OSM node id from the current release;
   - the first release starts from the v3.2 seed (`node_region_v3.2.npz`, `node_region_h32all.npz` and `labels_l3.npy` in the spike scratch);
   - new nodes take their L1 label from the frozen v3.2 polygons and their L2 label by network propagation inside their L1 cell;
   - spec §6.5 said "re-label by polygon" and is amended to match: polygons disagree with the min-cut labels on 0.1% of nodes (spike §14), and level 2 has no polygons at all.

   *Cost:* borders follow the min cut, not the drawn polygons, which are for display only.
7. **The frozen L1 polygons are committed** as `data/partition/regions_l1_v3.2.geojson` (1.7 MB, OSM-derived). The raw seed tables are copied out of the session `%TEMP%` scratch into the gitignored `runtime/matrix/partition/seed_v3.2_raw/` in Task 1 (Step 0), so a temp cleanup cannot lose them, and are imported into `runtime/matrix/partition/seed_v3.2.npz` as soon as Task 7 lands. Without a seed, a run falls back to polygons for L1 and fresh min-cut splits for L2 and L3. *Cost:* if the scratch is already gone at Task 1, gates move away from the v3.2 positions.
8. **Contiguity:**
   - contiguity is weak connectivity per weak component of the whole graph;
   - every piece but the largest joins the sibling cell (same parent) it shares the most edges with;
   - so region 6's western Nghệ An piece joins a neighbour and L1 stays at 22.

   *Cost:* if the user wants region 6 split into two regions instead, L1 becomes 23 once and the "L1 unchanged" check is re-baselined.
9. **Nesting by split-then-merge:** a cell spanning two parents is split by parent (the largest part keeps the code, the other parts get new codes), and contiguity then merges the small parts. *Cost:* none known.
10. **L3 re-split trigger and method:**
    - an L2 cell is re-split when its hash changes; the hash is sha256 over its sorted node ids and its sorted inner edges `(min id, max id, capacity)`;
    - the re-split is warm-started from the old L3 labels, with min-cut refinement in a 1 km band;
    - a fresh k-means split runs only when more than 20% of the cell's nodes are new or its node count changed by more than 20%;
    - the v3.2 seed carries no L2 hashes, so the first run warm-splits every L2 cell (about 1,009 jobs, never fresh, since node counts match); Task 12 budgets about 80 minutes for it with 14 workers. Later runs re-split only changed cells.

    *Cost:* cuts slightly less optimal than a fresh split, in exchange for stable gates; one slow first run.
11. **Ferry unit:** in each connected ferry component, the "home" landing is the one with the most road edges (ties: lowest OSM id). The non-landing ferry nodes and every other landing take the home landing's full label vector, so gates sit on the road edges at the other landings. *Cost:* a crossing's time is counted inside one cell.
12. **River rule:** segments that cross `osrm/data/main_rivers.geojson` stay in the graph with min-cut capacity 0, whatever PBF the run uses (rivers rarely change). This is a deliberate change from v3.2: the spike's `matrix_graph.py` removed river-crossing edges from the graph altogether, which also changed contiguity and the Voronoi growth. Keeping the bridges makes contiguity follow the real network while the cut still prefers rivers. *Cost:* cut placement near a river that moved, and fresh splits that differ from v3.2 near bridges.
13. **Expressway rule:** expressway-only nodes (all edges `motorway`/`motorway_link`) take the label of the nearest non-expressway node with the same parent. Runs shorter than 2 km between two crossings into one cell are absorbed. This replaces the spike's land raster. *Cost:* gates near borders sit slightly differently from v3.2.
14. **Label storage:** labels are `uint16` dense indices per segment (`labels_l{1,2,3}.npy`), mapped to stable int64 cell codes in `partition.json`. More than 65,535 cells at a level raises. *Cost:* a finer k would need a format change.
15. **Gate definition:** a gate is every arc between two segments of different cells, including the cost-0 continuation inside a geometry (a cut in the middle of a road). `gates.npz` holds each level's arcs and each cell's entry and exit segments as CSR. *Cost:* none.
16. **Blocking versus warning:**
    - blocking: the §6.6 checks, §12.1, the graph identities, rule `ZoneError`/`RuleError`, and any orphan of an active trusted rule (confidence `cao` or `tb`), exactly as sub-project 0's `rules-check` fails them;
    - warnings: an L2 count change, orphans of other rules, a rule whose matched segments drift by more than 20%, and gate drift (Decision 19);
    - the run stops at the first step whose blocking check fails, with status `failed_checks` and exit 1; later steps do not run.

    *Cost:* a trusted ban whose road was deleted from OSM stops the weekly update until the rule is changed through `import-rules` (a rule edit, not a map edit); part of a low-confidence ban can be lost for a week if nobody reads the report.
17. **Segment anchors:** a segment anchor resolves by `(way, a, b)`, then by the node pair on any way. No rule row is rewritten. An anchor is an orphan only when its node pair is gone. *Cost:* a node pair reused by a different physical road is accepted silently, which is practically never.
18. **§12.1 sample:**
    - the speeds are those of the base layer at weekday slot 16 (08:00), without caps or closures;
    - 500 segment midpoints, seed 12, excluding motorway segments, segments with folded obstacles, segments under 20 m, tiny components and duplicated geometries;
    - pairs on the same geometry are dropped;
    - two per-segment checks are added: every graph direction is updated by the speed file, and at least 99.9% of segments agree within max(1 ds, 1%).

    *Cost:* errors specific to motorway phantoms go unseen.
19. **Gate drift instead of a gate snap check:** gates are arcs of the OSRM graph, so `/nearest` at a gate's via node answers about 0 m by construction and could never fail; spec §6.6's snap criterion (≤ 20 m L1, ≤ 5 m L2) therefore holds by construction. What can drift is a carried-over gate: for every L1 and L2 gate via node of the previous release that still exists in the new graph (by OSM id), its coordinates must lie within 5 m of the old ones; via nodes that are gone are counted too. Both are warnings, because a mapper moving a node is a legitimate edit and blocking it would need a manual step. *Cost:* a moved gate is promoted with only a report line.
20. **Out of scope here:** level-3 pieces, clique and escape tables (§9) and §12.2–12.5 belong to sub-project 2. Sub-project 1 promotes on the map checks, §12.1, rules and a successful speeds/groups build. The spike's OSRM-composition accuracy checks are replaced by §12.1 here and §12.2 in sub-project 2. Spec §1 and §6.8 are amended to say that each sub-project's build is promoted after the checks of what it builds. *Cost:* sub-project 2 must add its steps before promotion.
21. **Job state:** it goes in `state.sqlite` schema 2 (`jobs` table), with logs as files. The spike's `history.jsonl` is no longer written. *Cost:* none.
22. **Disk:**
    - before building, every release except the current (and served) one is pruned;
    - after a promotion, 2 promoted releases (the current one included) and the newest failed one are kept;
    - pruning also removes `runtime/matrix/segments/<sha>` and `runtime/matrix/speeds/<version>` dirs that no kept release references, including the format-1 segments dir left by sub-project 0;
    - the disk floor is 1.5 × the measured size of the current release (18 GB with none), checked after pruning; a fixed 16 GB would block the Geofabrik run on E:;
    - the §12.1 dataset copy (4.3 GB) is a full copy, deleted after use.

    *Cost:* only one older release is kept for rollback; a run can still fill the disk near the end if a release grows by more than half.
23. **Measured subprocesses:** heavy steps are subprocesses. Their peak memory is the largest summed private bytes of the process tree, sampled every 0.5 s (the venv `python.exe` is a launcher with a child process). *Cost:* a peak shorter than 0.5 s can be missed.
24. **Scheduled task:** it reuses the spike's task name `HanoiTrafficMatrixUpdate` and lock file `runtime/matrix/update.lock`, so the two can never run together. Registering it creates persistent system configuration, so it is done only after the user says yes. *Cost:* none.
25. **Bootstrap run:** the first real run uses the PBF that v3.2 was built from (`osrm/data/vietnam-latest.osm.pbf`, sha `d408722f…`), as a regression against the v3.2 counts. Then a Geofabrik run exercises the carry-over path. The run never deletes a `--pbf` input; it deletes only a PBF it downloaded itself. *Cost:* about one extra hour.
26. **No gate on a blocked road, by construction and checked:** the matrix profile drops access-blocked ways from the graph and applies no restricted-access penalty, so every EBG edge has weight equal to duration. `build_graph` counts edges whose weight differs (`restricted_edges`) and raises when any exists, which is the blocking form of spec §6.6's first check. *Cost:* a profile change that reintroduces penalties stops the build until the profile is fixed.
27. **Step exit codes:** every step CLI (`partition run`, `rulecheck`, `verify_osrm`) exits 0 whenever it wrote its JSON, whatever its checks say; any other exit code, or a missing JSON, is an error. `RealSteps` reads the checks from the JSON. *Cost:* a step's exit code alone no longer tells pass from fail.
28. **Group views live in the release:** `RealSteps.groups` writes into `<release>/groups/` through a new `--out` option of `speedmatrix.build groups`, so a failed `--force` run never changes the served release's views. *Cost:* sub-project 0's `status` command does not list these views.
29. **Memory gate before the OSRM build:** the build starts only when available memory is at least the last recorded `osrm-extract` peak plus 1 GB (14.1 GB by default). *Cost:* on a busy machine the run fails with exit 2 instead of paging; the next weekly run retries.
30. **§12.1 queries OSRM first:** `verify_release` gets the OSRM `/table` and `/nearest` answers, stops `osrm-routed`, and only then runs its own router workers, so the two never hold memory together. *Cost:* none.

---

### Task 1: OSRM dataset reader and the tiny-dataset fixture

**Files:**
- Create: `speedmatrix/osrmfiles.py`
- Create: `tests/sm_osrm_fixture.py` (helper module, not a test)
- Create (gitignored, never committed): `runtime/matrix/partition/seed_v3.2_raw/` (Step 0)
- Test: `tests/test_sm_osrmfiles.py`

**Interfaces:**
- Produces (`osrmfiles.py`):
  - constants `OSRM_VERSION = "v26.10.0"`, `INVALID_DURATION = (1 << 22) - 1`, `ID_BITS = 40`, `DURATION_BITS = 22`, `PACK_BLOCK = 64`;
  - `class OsrmFormatError(ValueError)`;
  - `read_block(path, name: str, dtype) -> np.ndarray`: `np.fromfile` at the tar member offset, the count from `<name>.meta` (uint64). It uses the same approach as `model/osrm_mld_boundary._read`. A missing block raises `OsrmFormatError` naming the file and the block; so does a size that is not `count × itemsize`;
  - `read_packed(path, name: str, bits: int) -> np.ndarray[uint64]`:
    - reads `<name>/packed` and `<name>/number_of_elements.meta`;
    - the word count must equal `ceil(count / 64) * bits`, else `OsrmFormatError`;
    - element i sits at bit `i * bits`, LSB first, across little-endian 64-bit words (port `unpack` of `matrix_graph.py`);
  - `@dataclass OsrmDataset`:
    - nodes: `nbg_osm: int64 (N,)`, `nbg_lon_e6`, `nbg_lat_e6: int32 (N,)`;
    - geometries: `geo_index: int64 (G+1,)`, `geo_nodes: int32 (entries,)`, `geo_fwd_ds`, `geo_rev_ds: uint32 (entries,)`;
    - edge-based nodes: `ebn_geometry: int32 (E,)`, `ebn_forward: bool (E,)`, `ebn_tiny: bool (E,)`, `ebn_duration_ds: uint32 (E,)`;
    - turns: `turn_src`, `turn_dst: int32 (T,)`, `turn_ds: uint16 (T,)`, `turn_total_ds: uint32 (T,)`;
    - `restricted_edges: int`: EBG edges whose weight differs from their duration (0 on a matrix-profile build; Decision 26);
    - `fingerprint: bytes`;
  - `read_dataset(base) -> OsrmDataset`. `base` is the path without extension (`.../vietnam-latest.osrm`). It raises `OsrmFormatError` when:
    - the files' `osrm_fingerprint.meta` differ;
    - any EBG edge lacks forward=1 or has backward=1;
    - `turn_id` is not `0..T-1` in order;
    - a turn duration is negative;
    - `/common/number_of_edge_based_nodes` differs from the EBN count;
  - `read_datasources(base) -> tuple[np.ndarray[uint8], np.ndarray[uint8]]`: forward and reverse data sources per geometry entry;
  - `check_build_json(path) -> dict`: returns the parsed file; raises `OsrmFormatError` unless `input.osrm_version == OSRM_VERSION`.
- Produces (`tests/sm_osrm_fixture.py`):
  - `TOWN_COORDS`, `TOWN_WAYS`, `TOWN_OBSTACLES`, `TOWN_BANNED` (below);
  - `write_dataset(root: Path, coords, ways, obstacles={}, banned=set(), turn_ds=20, uturn_ds=200, pbf_sha256="fixture", *, drop_blocks=frozenset(), backward_edge=False, fingerprint_of={}, enw_delta={}, weight_delta={}, short_packed=frozenset()) -> Path` (returns the base path). The keyword options break the output on purpose, for the error tests: `drop_blocks` leaves out the named blocks; `backward_edge=True` sets backward=1 on the first EBG edge; `fingerprint_of` maps a file suffix to another fingerprint; `enw_delta` adds ds to the `.enw` duration of the given EBNs; `weight_delta` adds to the weight of the given EBG edges; `short_packed` writes the named packed blocks one word short;
  - `write_nbg_nodes(base: Path, coords: list[tuple[int, int]], ids: list[int]) -> Path`: writes only `<base>.nbg_nodes` (used by `write_dataset` and by the packed-vector test) and returns its path;
  - `grid(n: int, step_deg=0.001, primary_every=4) -> (coords, ways)`;
  - `write_osm(path, coords, ways)`: same XML layout as `tests/test_sm_build.py`, with way tags `highway`, `oneway=yes` when set, and `name`.

Block names and layouts were checked on 2026-10-24 on `runtime/matrix/releases/20261021-1400/osrm/data/vietnam-latest.osrm.*`, the matrix profile build of 2026-10-21. The reader must use exactly these:

| File | Block | Layout |
|---|---|---|
| `.nbg_nodes` | `/common/nbn_data/coordinates` | int32 (N, 2): lon, lat × 1e6 |
| `.nbg_nodes` | `/common/nbn_data/osm_node_ids` | packed, 40 bits |
| `.geometry` | `/common/segment_data/index` | uint32 (G + 1) |
| `.geometry` | `/common/segment_data/nodes` | uint32, node-based-graph node index |
| `.geometry` | `/common/segment_data/forward_durations`, `reverse_durations` | packed, 22 bits, deci-seconds |
| `.geometry` | `/common/segment_data/forward_data_sources`, `reverse_data_sources` | uint8 |
| `.ebg_nodes` | `/common/ebg_node_data/nodes` | 3 × uint32: `geometry:31 \| forward:1`, `component:31 \| tiny:1`, `annotation:31 \| segregated:1` |
| `.ebg` | `/common/edge_based_edge_list` | 6 × uint32: source, target, turn_id, weight (int32), distance (float32), `duration:30 \| forward:1 \| backward:1` |
| `.ebg` | `/common/number_of_edge_based_nodes` | uint32, one value |
| `.turn_duration_penalties` | `/common/turn_penalty/duration` | int16, deci-seconds |
| `.enw` | `/extractor/edge_based_node_durations` | uint32, deci-seconds |

**Geometry layout in v26.10.0:**
- A geometry's entries are `n0, n1, n1, n2, n2, …, nm`, always an even count.
- Real segment j is the move from entry 2j to entry 2j+1. Node entry j is the zero-length move from 2j+1 to 2j+2.
- The forward duration of the move from entry i to i+1 is `fd[i+1]`; its reverse duration is `rd[i]`.
- `fd[first]` and `rd[last]` hold `INVALID_DURATION`.

**Facts on that dataset**, which the real-data test asserts on whatever dataset it finds:
- **turn total:** EBG edge duration equals `enw[source] + turn_ds[turn]` for 100% of 19,254,604 edges;
- **EBN total:** for 100% of the 10,120,903 EBNs, the EBN duration equals the sum of its geometry entries;
  - forward EBN: `fd[b+1..e-1]`;
  - reverse EBN: `rd[b..e-2]`;
- **counts:**
  - N = 25,870,452 nodes;
  - G = 5,164,605 geometries;
  - 53,178,332 entries;
  - E = 10,120,903 EBNs;
  - T = 19,254,604 turns;
  - 52,605,793 directed real segments;
  - 26,589,166 undirected ones;
- **weights:** weight equals duration on every edge (matrix profile).

**The fixture town (TOWN).** Coordinates are (lon, lat):
- 101 (105.8000, 21.0000);
- 107 (105.8005, 21.0000);
- 102 (105.8010, 21.0000);
- 103 (105.8020, 21.0000);
- 104 (105.8010, 21.0010);
- 105 (105.8010, 20.9990);
- 106 (105.8020, 20.9990).

Ways:
1. `[101, 107, 102, 103]`, residential, 25 km/h;
2. `[104, 102, 105]`, primary, 65 km/h;
3. `[105, 106]`, residential, 25 km/h, one-way;
4. `[103, 106]`, track, 10 km/h.

`TOWN_OBSTACLES = {(107, "forward", 1): 20}` puts a 20 ds signal at node 107 for travel along way 1 in its own direction. `TOWN_BANNED = {(104, 102, 103)}` bans the turn from 104 via 102 to 103.

`write_dataset`:
- cuts ways into geometries at every node shared by two ways and at way ends;
- writes geometries in way order, then EBNs: every geometry forward first (ids 0..G-1), then the reverse of every two-way geometry;
- sets each real segment's duration to `max(1, round(36 · haversine_m / kmh))`, with node entries 0 unless listed in `obstacles`;
- adds a turn from every EBN ending at a node to every EBN starting there:
  - `uturn_ds` onto the reverse of the same geometry;
  - `turn_ds` otherwise;
  - none for a banned (from, via, to) triple;
- writes `.nbg_nodes`, `.geometry`, `.ebg_nodes`, `.ebg`, `.turn_duration_penalties` and `.enw` in the layouts above, with the identities true, the same 11-byte fingerprint in every file (as in `tests/test_osrm_mld_boundary.py` `write_tar`), all-zero data sources, and `build_matrix.json` = `{"input": {"pbf_sha256": pbf_sha256, "osrm_version": "v26.10.0"}}`.

TOWN therefore has 7 nodes, 6 geometries (g0 = 101-107-102, g1 = 102-103, g2 = 104-102, g3 = 102-105, g4 = 105-106, g5 = 103-106), 11 EBNs and 13 directed real segments.

- [ ] **Step 0: Copy the v3.2 seed out of `%TEMP%`**

The v3.2 seed exists only in a session scratch folder that Windows temp cleanup or Storage Sense may delete. Copy `node_region_v3.2.npz`, `node_region_h32all.npz` and `labels_l3.npy` from `C:\Users\Tyzen\AppData\Local\Temp\claude\E--Work-hanoi-traffic-model-hanoi-traffic-model\6ee1752a-7c0a-47f6-bb00-69183ffc8a50\scratchpad\v32\mincut\` into `runtime/matrix/partition/seed_v3.2_raw/`, and write a `SHA256SUMS` file there. `runtime/` is gitignored; nothing here is committed. If the folder is already gone, write `MISSING.txt` there with the date, so Task 12 records it and uses the Decision 7 fallback.

- [ ] **Step 1: Write the failing tests**

The error fixtures come from `write_dataset` options: `self.base2` (`drop_blocks={"/extractor/edge_based_node_durations"}`), `bad` (`short_packed={"/common/segment_data/forward_durations"}`, then the `.geometry` path), `self.base_backward` (`backward_edge=True`) and `self.base_mixed` (`fingerprint_of={".enw": b"OSRN" + bytes(7)}`).

```python
def test_reads_town(self):
    d = read_dataset(self.base)
    self.assertEqual(sorted(d.nbg_osm.tolist()), [101, 102, 103, 104, 105, 106, 107])
    self.assertEqual((len(d.geo_index) - 1, len(d.ebn_geometry)), (6, 11))
    self.assertEqual(int(d.ebn_forward.sum()), 6)
    self.assertTrue((np.diff(d.geo_index) % 2 == 0).all())

def test_identities_hold(self):
    d = read_dataset(self.base)
    np.testing.assert_array_equal(d.turn_total_ds, d.ebn_duration_ds[d.turn_src] + d.turn_ds)
    self.assertEqual(int(d.geo_fwd_ds[d.geo_index[0]]), INVALID_DURATION)
    self.assertEqual(d.restricted_edges, 0)
    self.assertEqual(read_dataset(self.base_weight).restricted_edges, 1)        # weight_delta={0: 5}

def test_packed_ids_round_trip_over_a_block_boundary(self):
    # 70 ids > 64 forces two blocks: words = ceil(70/64) * 40 = 80
    ids = [10**10 + 7 * i for i in range(70)]                 # > 2^32, so all 40 bits are used
    p = write_nbg_nodes(self.root / "big", coords=[(105_800_000, 21_000_000)] * 70, ids=ids)   # sm_osrm_fixture helper
    self.assertEqual(read_packed(p, "/common/nbn_data/osm_node_ids", 40).tolist(), ids)

def test_missing_block_is_named(self):
    # rewrite .enw without its duration block
    with self.assertRaisesRegex(OsrmFormatError, "edge_based_node_durations"): read_dataset(self.base2)

def test_wrong_packed_size_raises(self):
    with self.assertRaisesRegex(OsrmFormatError, "packed"): read_packed(bad, "/common/segment_data/forward_durations", 22)

def test_backward_edge_flag_and_fingerprint_mismatch_raise(self):
    with self.assertRaisesRegex(OsrmFormatError, "backward"): read_dataset(self.base_backward)
    with self.assertRaisesRegex(OsrmFormatError, "fingerprint"): read_dataset(self.base_mixed)

def test_version_pinned(self):
    with self.assertRaisesRegex(OsrmFormatError, "v26.10.0"): check_build_json(write({"input": {"osrm_version": "v26.11.0"}}))

@unittest.skipUnless(os.environ.get("SM_REAL_DATA") == "1", "real data only")
def test_real_dataset_identities(self):
    # base = <current release>/osrm/data/vietnam-latest.osrm from runtime/matrix/current.json, else
    # runtime/matrix/releases/20261021-1400/osrm/data/..., else osrm/data/vietnam-latest.osrm; skip if none exists
    d = read_dataset(base)
    self.assertTrue(np.array_equal(d.turn_total_ds, d.ebn_duration_ds[d.turn_src] + d.turn_ds))
    self.assertEqual(d.restricted_edges, 0)
    fd = np.where(d.geo_fwd_ds == INVALID_DURATION, 0, d.geo_fwd_ds).astype(np.int64)
    rd = np.where(d.geo_rev_ds == INVALID_DURATION, 0, d.geo_rev_ds).astype(np.int64)
    cf, cr = np.r_[0, np.cumsum(fd)], np.r_[0, np.cumsum(rd)]
    b, e = d.geo_index[d.ebn_geometry], d.geo_index[d.ebn_geometry + 1]
    fwd_sum = cf[e] - cf[b + 1]                                                  # fd[b+1 .. e-1]
    rev_sum = cr[e - 1] - cr[b]                                                  # rd[b .. e-2]
    np.testing.assert_array_equal(np.where(d.ebn_forward, fwd_sum, rev_sum), d.ebn_duration_ds.astype(np.int64))
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_osrmfiles -v`. Expected: `ModuleNotFoundError: speedmatrix.osrmfiles`.

- [ ] **Step 3: Implement `osrmfiles.py` and `tests/sm_osrm_fixture.py`**

`read_dataset` reads only the blocks in the table, `/common/number_of_edge_based_nodes` included.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command (`OK`), then `$env:SM_REAL_DATA = "1"; .venv/Scripts/python.exe -m unittest tests.test_sm_osrmfiles.OsrmFilesTests.test_real_dataset_identities -v`. Expected: `OK`, under about 2 minutes.

- [ ] **Step 5: Run the whole suite, commit and push**

```
git add speedmatrix/osrmfiles.py tests/sm_osrm_fixture.py tests/test_sm_osrmfiles.py
git commit -m "speedmatrix: read the OSRM v26.10.0 turn graph blocks" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 2: Machine resources and the matrix OSRM build

**Files:**
- Create: `speedmatrix/sysres.py`, `speedmatrix/matrix_osrm.py`
- Test: `tests/test_sm_sysres.py`, `tests/test_sm_matrix_osrm.py`

**Interfaces:**
- Produces (`sysres.py`):
  - `cpu_workers(cpu_count: int | None = None) -> int`: `max(1, round(0.6 * cpu))`;
  - `available_memory_gb() -> float`: `GlobalMemoryStatusEx` via ctypes on Windows, `os.sysconf("SC_AVPHYS_PAGES") * SC_PAGE_SIZE` elsewhere;
  - `default_workers(per_worker_gb: float, requested: int | None = None, *, cpu_count=None, available_gb=None) -> int`: `requested` if given, else `max(1, min(cpu_workers(cpu_count), floor(0.6 * available_gb / per_worker_gb)))`;
  - `free_disk_gb(path) -> float` (`shutil.disk_usage`);
  - `@dataclass StepResult(code: int, seconds: float, peak_bytes: int | None, last_line: str)`;
  - `run_measured(cmd: list, log_path: Path, cwd: Path, env: dict | None = None) -> StepResult`:
    - stdout and stderr are appended to `log_path` and the last stdout line is kept;
    - on Windows a thread samples the process tree every 0.5 s (`CreateToolhelp32Snapshot` for descendants, `GetProcessMemoryInfo` `PrivateUsage`) and keeps the largest sum;
    - elsewhere it uses `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`;
    - if sampling fails, `peak_bytes` is None and the step still runs.
- Produces (`matrix_osrm.py`):
  - `PATCHES`: the four `(file, old, new)` tuples of `reports/gate_compose_spike/build_matrix_osrm.py`, copied verbatim (weight = duration; the restricted list emptied; `construction_ignored_on_open_highway`; the `way_handlers.lua` construction test);
  - `class ProfilePatchError(RuntimeError)`;
  - `patch_profiles(src: Path, dst: Path) -> list[dict]`: copies the tree, then applies each patch, which must match exactly once; otherwise `ProfilePatchError` naming the file and the count;
  - `profiles_sha256(dir: Path) -> str`: sha256 over the sorted relative paths and bytes of the files;
  - `build_matrix_osrm(pbf: Path, out: Path, threads: int, tools: Path = manage.TOOLS, runner=run_measured) -> dict`:
    - runs `osrm-extract --version` through `runner` first; its last line must contain `26.10.0` (`osrmfiles.OSRM_VERSION` without the `v`), else `RuntimeError("osrm-extract reports <line>, expected v26.10.0")`. `osrm_version` in the output is `OSRM_VERSION`; nothing reads the gitignored `osrm/build.json`;
    - copies the PBF to `out/data/<name>` and returns that path as `info["pbf_copy"]` (later steps read this copy);
    - runs `osrm-extract -p <out>/profiles/car.lua -t <threads> <pbf copy>`, then `osrm-partition -t <threads> <base>`, then `osrm-customize -t <threads> <base>`;
    - writes `out/build_matrix.json`: `{"input": {"pbf_sha256", "osrm_version"}, "pbf_md5", "profiles_sha256", "patches", "steps": {"extract"|"partition"|"customize": {"seconds", "peak_bytes"}}}`;
    - a non-zero exit raises `RuntimeError("osrm-<step> failed with exit <code>, see <log>")` and writes no `build_matrix.json`;
  - CLI: `python -m speedmatrix.matrix_osrm --pbf PATH --out DIR [--threads N]` (default `cpu_workers()`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sm_sysres.py
def test_worker_formula(self):
    self.assertEqual(cpu_workers(24), 14); self.assertEqual(cpu_workers(1), 1)
    self.assertEqual(default_workers(0.5, cpu_count=24, available_gb=10), 12)
    self.assertEqual(default_workers(2.0, cpu_count=24, available_gb=10), 3)
    self.assertEqual(default_workers(8.0, cpu_count=24, available_gb=4), 1)
    self.assertEqual(default_workers(0.5, requested=5, cpu_count=24, available_gb=1), 5)

def test_run_measured_logs_and_peak(self):
    r = run_measured([sys.executable, "-c", "import time; x = bytearray(200_000_000); print('done'); time.sleep(1.5)"], self.log, ROOT)
    self.assertEqual((r.code, r.last_line), (0, "done")); self.assertIn("done", self.log.read_text(encoding="utf-8"))
    if os.name == "nt": self.assertGreaterEqual(r.peak_bytes, 150_000_000)

# tests/test_sm_matrix_osrm.py
def test_patches_apply_once(self):
    # fixture profile dir: car.lua and lib/way_handlers.lua holding each `old` string exactly once
    applied = patch_profiles(self.src, self.dst)
    self.assertEqual(len(applied), 4); self.assertIn("weight_name                     = 'duration'", (self.dst / "car.lua").read_text(encoding="utf-8"))

def test_patch_matching_twice_raises(self):
    with self.assertRaisesRegex(ProfilePatchError, "car.lua.*2"): patch_profiles(self.src_twice, self.dst2)

@unittest.skipUnless((ROOT / "osrm/tools/profiles/car.lua").exists(), "profiles absent")
def test_stock_profiles_patch_cleanly(self):
    applied = patch_profiles(ROOT / "osrm/tools/profiles", self.tmp / "p")
    self.assertEqual(len(applied), 4)
    self.assertIn("weight_name                     = 'duration'", (self.tmp / "p" / "car.lua").read_text(encoding="utf-8"))

def fake_runner(calls, code=0, version="v26.10.0"):
    def run(cmd, log, cwd, env=None):
        calls.append([Path(cmd[0]).name] + [str(c) for c in cmd[1:]])
        if "--version" in cmd: return StepResult(0, 0.1, None, version)
        return StepResult(code, 1.0, 10, "")
    return run

def test_build_runs_three_steps_and_records(self):
    calls = []
    info = build_matrix_osrm(self.pbf, self.out, 14, tools=self.tools, runner=fake_runner(calls))
    self.assertEqual([c[0] for c in calls], ["osrm-extract.exe", "osrm-extract.exe", "osrm-partition.exe", "osrm-customize.exe"])
    self.assertEqual(calls[0][1:], ["--version"]); self.assertEqual(calls[1][calls[1].index("-t") + 1], "14")
    doc = json.loads((self.out / "build_matrix.json").read_text())
    self.assertEqual((doc["input"]["pbf_sha256"], doc["input"]["osrm_version"]), (sha256_of(self.pbf), "v26.10.0"))
    self.assertEqual(info["pbf_copy"], self.out / "data" / self.pbf.name)

def test_wrong_binary_version_raises(self):
    with self.assertRaisesRegex(RuntimeError, "v26.11.0"): build_matrix_osrm(self.pbf, self.out, 2, tools=self.tools, runner=fake_runner([], version="v26.11.0"))

def test_failed_step_leaves_no_build_json(self):
    with self.assertRaisesRegex(RuntimeError, "osrm-extract failed"): build_matrix_osrm(self.pbf, self.out, 2, tools=self.tools, runner=fake_runner([], code=1))
    self.assertFalse((self.out / "build_matrix.json").exists())
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_sysres tests.test_sm_matrix_osrm -v`. Expected: ImportError.

- [ ] **Step 3: Implement `sysres.py` and `matrix_osrm.py`**

`check_build_json` (Task 1) is applied to the written file. Pass `str(threads)` in the command lines.

- [ ] **Step 4: Run the tests and confirm they pass**

Run the same command. Expected: `OK`.

- [ ] **Step 5: Run the whole suite, commit and push** (the files above)

---

### Task 3: Segment table format 2, node-pair lookup and the OSRM default speed

**Files:**
- Modify: `speedmatrix/segments.py`, `speedmatrix/speeds.py`
- Test: `tests/test_sm_segments.py`, `tests/test_sm_speeds.py` (update and extend; `tests/test_sm_speeds.py` `build_table` gains a track way)

**Interfaces:**
- Produces (`segments.py`):
  - `EXTRA_HIGHWAYS = ("track", "path", "footway", "pedestrian", "cycleway", "bridleway", "steps", "busway", "bus_guideway", "corridor", "road")`: these ways are kept with class `"nho"` (whatever `classify_osm` says), unless `area=yes`;
  - `SEGMENTS_FORMAT = 2`;
  - `load_segments` raises `ValueError("segments format 1 in <dir>; rebuild with this code (format 2)")` when `meta["format"] != SEGMENTS_FORMAT`;
  - `pair_lookup(table, a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray[int64] start, np.ndarray[int32] count]`: unordered node pairs → a slice of `table.pair_order` (rows sorted by `(min(a,b), max(a,b), way_id)`); `count == 0` when absent. The sorted rows and their keys are cached in a new dataclass field `pair_order: object = field(default=None, repr=False, compare=False)` (beside `_index`), built on first use and never written by `save_segments`.
- Produces (`speeds.py`):
  - `OSRM_DEFAULT_KMH = 10` (car.lua `default_speed`; a test keeps them equal, like `FERRY_KMH`);
  - `way_free_flow_kmh(table) -> np.ndarray[float32] (w,)`: the per-way speed `FreeFlowSource` already computes, with `OSRM_DEFAULT_KMH` for `EXTRA_HIGHWAYS`;
  - `FreeFlowSource.id = "profile_free_flow:v2"`, built on `way_free_flow_kmh`.

- [ ] **Step 1: Write the failing tests**

In the existing fixture of `tests/test_sm_segments.py`, way 3 (`footway`) is now kept. Add way 7 `highway=track`, way 8 `highway=path`, way 9 `highway=proposed`, and way 10 = a second `residential` way over the node pair (11, 12).

```python
def test_kept_ways_classes_and_ferry(self):            # updated
    kept = sorted(self.table.way_id.tolist()); self.assertEqual(kept, [1, 2, 3, 4, 5, 7, 8, 10])
    cls = {int(w): ROAD_CLASSES[c] for w, c in zip(self.table.way_id, self.table.road_class)}
    self.assertEqual((cls[3], cls[7], cls[8]), ("nho", "nho", "nho"))

def test_old_format_refused(self):
    meta = json.loads((self.root / "seg/meta.json").read_text()); meta["format"] = 1; write(meta)
    with self.assertRaisesRegex(ValueError, "format 1"): load_segments(self.root / "seg")

def test_pair_lookup_both_orders_and_duplicates(self):
    start, count = pair_lookup(self.table, np.array([11, 12, 11]), np.array([12, 11, 99]))
    self.assertEqual(count.tolist(), [2, 2, 0])
    rows = self.table.pair_order[start[0]:start[0] + count[0]]
    self.assertEqual(sorted(int(self.table.way_id[self.table.way[r]]) for r in rows), [1, 10])

# tests/test_sm_speeds.py: build_table gains a ("Đường mòn", "track", "nho", nan) way and its strings;
# self.track is the index of its segment
def test_extra_highways_get_osrm_default_speed(self):
    L = build_speed_layer(self.t, FreeFlowSource(), [], ScopeResolver(self.t))
    self.assertEqual(int(L.at("weekday", 0)[self.track]), OSRM_DEFAULT_KMH)

def test_default_speed_matches_profile(self):
    # read `default_speed = <n>` in osrm/tools/profiles/car.lua; skip if absent
    self.assertEqual(n, OSRM_DEFAULT_KMH)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_segments tests.test_sm_speeds -v`.

- [ ] **Step 3: Implement**

OSM node ids reach 14,297,527,150 (34 bits) in the current table, so `(lo, hi)` cannot be packed into one int64. Map node ids to dense ranks first (`np.unique` over `a` and `b`: at most 26.7M ranks, 25 bits each), pack `rank_lo << 25 | rank_hi`, sort rows with `np.lexsort((way_id_of_row, packed))`, and answer queries with two `np.searchsorted` calls; a query id absent from the rank table gives count 0. Every sub-project 0 test must still pass; update only the expectations this format changes.

- [ ] **Step 4: Run the tests and confirm they pass, then run the whole suite**

- [ ] **Step 5: Commit and push** (the four files)

---

### Task 4: The directed turn graph

**Files:**
- Create: `speedmatrix/graph.py`
- Test: `tests/test_sm_graph.py`

**Interfaces:**
- Consumes:
  - `read_dataset`, `check_build_json`, `OsrmDataset` (Task 1);
  - `SegmentTable`, `pair_lookup`, `ROAD_CLASSES` (Task 3);
  - `way_free_flow_kmh` (Task 3);
  - `speedmatrix.geo.haversine_m`.
- Produces:
  - `GRAPH_FORMAT = 1`;
  - `class GraphError(ValueError)`;
  - `@dataclass TurnGraph`:
    - **nodes** (sorted by OSM id):
      - `node_osm: int64 (N,)`;
      - `node_lon_e6`, `node_lat_e6: int32 (N,)`;
    - **directed real segments**, grouped by EBN in travel order:
      - `seg_tail`, `seg_head: int32 (S,)` (node indices);
      - `seg_len_m: float32`;
      - `seg_fixed_ds: uint16` (folded node entries);
      - `seg_osrm_ds: uint32` (OSRM's own free-flow duration);
      - `seg_row: int32` (SegmentTable row);
      - `seg_class: int8`;
      - `seg_highway: int16` (index into `meta["highways"]`);
    - **EBNs**:
      - `ebn_offsets: int64 (E+1,)`;
      - `ebn_geometry: int32`;
      - `ebn_forward: bool`;
      - `ebn_tiny: bool`;
    - **turns**, sorted by source EBN:
      - `turn_offsets: int64 (E+1,)`;
      - `turn_dst: int32 (T,)`;
      - `turn_ds: uint16 (T,)`;
    - `meta: dict` (`format`, `pbf_sha256`, `osrm_version`, `segments_sha256`, `highways`, `counts`, `ambiguous_pairs`, `obstacle_entries`, `restricted_edges`);
    - properties:
      - `n_nodes`, `n_segments`, `n_ebn`, `n_turns`;
      - `node_lat`, `node_lon` (float64);
      - `seg_ebn` (np.repeat over the offsets);
      - `sha256`: of the canonical meta;
  - `build_graph(base, build_json: Path, table: SegmentTable) -> TurnGraph`;
  - `save_graph(g, out_dir)` and `load_graph(out_dir, mmap: bool = False) -> TurnGraph`: one `.npy` per array plus `meta.json`, written to a temp sibling and renamed;
  - `segment_ds(g, seg_kmh: np.ndarray) -> np.ndarray[float64] (S,)`: `36 · seg_len_m / seg_kmh + seg_fixed_ds`;
  - CLI: `python -m speedmatrix.graph --osrm-base BASE --build-json FILE --segments DIR --out DIR` prints the out dir.

**Build order:**
1. `check_build_json`. Then `table.meta["pbf_sha256"]` must equal `input.pbf_sha256`, else `GraphError("segment table built from another PBF")`. Then `restricted_edges` must be 0, else `GraphError("k restricted-access edges: the matrix profile must block them")` (Decision 26; spec §6.6 "no gate on a blocked road").
2. Expand each EBN into its real segments (Task 1 layout).
   - Forward: segment j runs from entry 2j to 2j+1, with duration `fd[b+2j+1]`, and absorbs node entry j's `fd[b+2j+2]` for j ≤ m−2.
   - Reverse: segments run j = m−1 … 0, each from entry 2j+1 to 2j, with duration `rd[b+2j]`. Segment j absorbs node entry j−1's `rd[b+2j−1]` for j ≥ 1.
   - Vectorise with `np.repeat` over the EBN lengths.
3. Check the EBN identity: `Σ (seg_osrm_ds + seg_fixed_ds)` per EBN equals `ebn_duration_ds`. Any mismatch raises `GraphError("enw identity: k EBNs differ, e.g. …")`.
4. Sort the turns stably by source. Check that `turn_total_ds == ebn_duration_ds[src] + turn_ds`, and that the head of the source's last segment is the tail of the target's first segment, for every turn. Any mismatch raises `GraphError` naming the identity.
5. Build the node table from the nodes used, and compute `seg_len_m` by haversine on the e6 coordinates.
6. Map each segment to a row with `pair_lookup`:
   - `count == 0` → `GraphError` with the count and up to 5 `(osm_a, osm_b)` examples;
   - `count > 1` → take the row minimising `|36·len/way_kmh − seg_osrm_ds|`, ties to the lowest way id, and count it in `meta["ambiguous_pairs"]`.

   `seg_class` and `seg_highway` come from the row's way.

- [ ] **Step 1: Write the failing tests**

The fixture comes from `sm_osrm_fixture`:
- `write_osm` + `build_segments` on TOWN gives `self.table`;
- `write_dataset(…, pbf_sha256=_sha256_file(osm))` gives `self.base`.

```python
def test_counts_and_obstacle_fold(self):
    g = build_graph(self.base, self.bj, self.table)
    self.assertEqual((g.n_nodes, g.n_ebn, g.n_segments), (7, 11, 13))
    s = slice(g.ebn_offsets[0], g.ebn_offsets[1])                      # g0 forward: 101-107-102
    self.assertEqual(g.node_osm[g.seg_tail[s]].tolist(), [101, 107]); self.assertEqual(g.seg_fixed_ds[s].tolist(), [20, 0])
    r = g.ebn_offsets[6]                                                # g0 reverse
    self.assertEqual(g.node_osm[g.seg_tail[r:r + 2]].tolist(), [102, 107]); self.assertEqual(g.seg_fixed_ds[r:r + 2].tolist(), [0, 0])

def test_turns_connect_and_banned_turn_absent(self):
    g = build_graph(self.base, self.bj, self.table)
    last = g.ebn_offsets[1:] - 1; first = g.ebn_offsets[:-1]; src = np.repeat(np.arange(g.n_ebn), np.diff(g.turn_offsets))
    np.testing.assert_array_equal(g.seg_head[last[src]], g.seg_tail[first[g.turn_dst]])
    triples = {(int(g.node_osm[g.seg_tail[last[u]]]), int(g.node_osm[g.seg_head[last[u]]]), int(g.node_osm[g.seg_head[first[v]]]))
               for u, v in zip(src, g.turn_dst)}
    self.assertNotIn((104, 102, 103), triples); self.assertIn((107, 102, 103), triples)

def test_rows_classes_and_one_way(self):
    g = build_graph(self.base, self.bj, self.table)
    self.assertTrue((g.seg_row >= 0).all())
    self.assertEqual(ROAD_CLASSES[g.seg_class[g.ebn_offsets[5]]], "nho")      # g5 = way 4, track
    self.assertEqual(int((g.ebn_geometry == 4).sum()), 1)                      # way 3 one-way: forward only

def test_identity_failure_raises(self):
    # rewrite .enw with one EBN duration + 1
    with self.assertRaisesRegex(GraphError, "enw identity"): build_graph(self.base_bad, self.bj, self.table)

def test_unmapped_pair_raises_with_example(self):
    # table built from an .osm where way 4 is highway=proposed (not kept)
    with self.assertRaisesRegex(GraphError, r"2 segments.*\(103, 106\)"): build_graph(self.base, self.bj, self.table_no4)

def test_pbf_hash_mismatch_raises(self):
    with self.assertRaisesRegex(GraphError, "another PBF"): build_graph(self.base_other_sha, self.bj_other, self.table)

def test_restricted_edge_raises(self):
    # write_dataset(..., weight_delta={0: 5})
    with self.assertRaisesRegex(GraphError, "1 restricted-access edges"): build_graph(self.base_weight, self.bj, self.table)

def test_ambiguous_pair_takes_the_closest_free_flow(self):
    # .osm adds way 5 [102, 103] highway=primary; OSRM durations stay those of way 1 (25 km/h)
    g = build_graph(self.base, self.bj, self.table_dup)
    i = g.ebn_offsets[1]                                               # g1 forward
    self.assertEqual(int(self.table_dup.way_id[self.table_dup.way[g.seg_row[i]]]), 1); self.assertEqual(g.meta["ambiguous_pairs"], 2)

def test_round_trip_with_mmap(self):
    g = build_graph(self.base, self.bj, self.table); save_graph(g, self.root / "graph")
    h = load_graph(self.root / "graph", mmap=True); self.assertEqual(h.sha256, g.sha256)
    np.testing.assert_array_equal(h.turn_dst, g.turn_dst); self.assertFalse(any(p.name.endswith(".tmp") for p in self.root.iterdir()))
    del h                                    # Windows cannot delete a mapped file; setUp uses TemporaryDirectory(ignore_cleanup_errors=True)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_graph -v`.

- [ ] **Step 3: Implement `graph.py`**

Keep every per-segment array in int32/uint32/float32 while expanding. The nationwide build must stay under 10 GB peak (52.6M segments). Free `OsrmDataset` arrays as soon as they are used.

- [ ] **Step 4: Run the tests and confirm they pass**

- [ ] **Step 5: Run the whole suite, commit and push**

---

### Task 5: Single-slot reference router

**Files:**
- Create: `speedmatrix/staticroute.py`
- Test: `tests/test_sm_staticroute.py`

**Interfaces:**
- Consumes: `TurnGraph`, `segment_ds` (Task 4); `default_workers` (Task 2).
- Produces:
  - `ebn_weights(g, seg_ds) -> np.ndarray[float64] (E,)`;
  - `class StaticRouter(g: TurnGraph, seg_ds: np.ndarray)`:
    - `twin(seg: int) -> int`: the same physical segment in the other direction (same geometry, opposite `ebn_forward`, lowest EBN id), or −1;
    - `table(src: np.ndarray, dst: np.ndarray) -> np.ndarray[float64] (P, Q)` in seconds. Points are segment midpoints. The value is `inf` when unreachable and `NaN` when both points lie on one geometry or on the diagonal;
  - `parallel_table(graph_dir: Path, seg_ds_file: Path, src, dst, workers: int) -> np.ndarray`: the same as `table`, split into source chunks over a `multiprocessing` pool. Workers open the graph with `load_graph(mmap=True)`; `per_worker_GB = 0.8`.

**Method:**
- Run scipy `dijkstra` at EBN level on a CSR with one virtual node per source.
- `dist(v)` is the time at the end of EBN v. Arc u→w costs `turn_ds + W(w)`.
- Each virtual source p links to the EBN of its segment and to the EBN of its twin. Each link costs the time from the midpoint to the end of that EBN: half the segment plus the segments after it.
- The arrival at target t through EBN v is `dist(v) − (half of t + the segments after t in v)`; the minimum is taken over v ∈ {EBN of t, EBN of its twin}.
- Run one source per `dijkstra` call and keep only the target columns. A dense `(P, E)` result would need 40 GB for 500 sources.
- Units: deci-seconds inside, seconds out.

- [ ] **Step 1: Write the failing tests**

```python
def brute(g, seg_ds, s, t):
    """Segment-level digraph: half-segment source/target, chain arcs inside EBNs, turn arcs; scipy dijkstra."""

def test_matches_bruteforce(self):
    R = StaticRouter(self.g, self.ds); segs = np.arange(self.g.n_segments)
    T = R.table(segs, segs)
    for i, s in enumerate(segs):
        for j, t in enumerate(segs):
            if np.isnan(T[i, j]): continue
            self.assertAlmostEqual(T[i, j], brute(self.g, self.ds, s, t) / 10, places=9)

def test_twin_and_one_way(self):
    R = StaticRouter(self.g, self.ds); fwd = self.g.ebn_offsets[4]                  # way 3, one-way
    self.assertEqual(R.twin(fwd), -1); self.assertEqual(R.twin(R.twin(self.g.ebn_offsets[1])), self.g.ebn_offsets[1])

def test_banned_turn_costs_a_detour(self):
    a, b = self.mid_104_102, self.mid_102_103
    banned = StaticRouter(self.g, self.ds).table(np.array([a]), np.array([b]))[0, 0]
    free = StaticRouter(self.g_unbanned, self.ds_unbanned).table(np.array([a]), np.array([b]))[0, 0]
    self.assertGreater(banned, free + 15)          # the ban forces a 200 ds U-turn or a loop

def test_unreachable_and_same_geometry(self):
    T = StaticRouter(self.g_island, self.ds_island).table(np.array([0, self.island_seg]), np.array([self.island_seg, 1]))
    self.assertTrue(np.isinf(T[0, 0])); self.assertTrue(np.isnan(StaticRouter(self.g, self.ds).table(np.array([0]), np.array([1]))[0, 0]))

def test_parallel_equals_serial(self):
    save_graph(self.g, self.root / "g"); np.save(self.root / "ds.npy", self.ds)
    np.testing.assert_allclose(parallel_table(self.root / "g", self.root / "ds.npy", self.src, self.dst, 2),
                               StaticRouter(self.g, self.ds).table(self.src, self.dst), equal_nan=True)
```

Fixture variants:
- `g_unbanned`: TOWN with `banned=set()`;
- `g_island`: TOWN plus way 5 `[201, 202]`, 1 km away, unconnected.

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_staticroute -v`.

- [ ] **Step 3: Implement `staticroute.py`**

- [ ] **Step 4: Run the tests and confirm they pass**

- [ ] **Step 5: Run the whole suite, commit and push**

---

### Task 6: The min-cut cell splitter

**Files:**
- Create: `speedmatrix/mincut.py`
- Test: `tests/test_sm_mincut.py`

**Interfaces:**
- Consumes: `default_workers` (Task 2).
- Produces:
  - constants:
    - `CAPACITY = {"motorway": 6, "motorway_link": 6, "trunk": 6, "trunk_link": 6, "primary": 7, "primary_link": 7, "secondary": 8, "secondary_link": 8, "tertiary": 9, "tertiary_link": 9}`, `DEFAULT_CAPACITY = 10` (the weights of the spike's `refine_mincut.py`); a river-crossing edge stays in the graph with capacity 0 (Decision 12; the spike's `matrix_graph.py` removed such edges instead);
    - `MIN_RUN_KM = 2.0`; `SEED = 17`;
  - `@dataclass CellGraph`:
    - `node: int64 (n,)` (global node index);
    - `osm: int64 (n,)`;
    - `x_km`, `y_km: float64 (n,)` (local projection: `x = (lon − lon0)·111.32·cos(lat0)`, `y = (lat − lat0)·111.32`, with lat0/lon0 = the cell's means);
    - `lo`, `hi: int32 (m,)` (local, `lo < hi`);
    - `cap: int8 (m,)`;
    - `length_m: float32 (m,)`;
    - `expressway: bool (m,)`;
  - `CellGraph.from_edges(osm, lon, lat, lo, hi, highway: np.ndarray[int16], names: list[str], river: np.ndarray[bool], length_m=None, node=None) -> CellGraph`: `highway` holds codes into `names` (no per-edge Python strings, so the nationwide graph stays compact); `cap` and `expressway` come from `names[highway]`;
  - `weighted_cut(lo, hi, cap, side: np.ndarray[int8], n) -> tuple[int, np.ndarray[bool], np.ndarray[bool]]`: a port of `refine_mincut.weighted_cut`;
  - `refine_pairs(g, labels, band_km) -> tuple[np.ndarray, list[dict]]`: for each pair of cells sharing edges, largest cut first (ties by codes):
    - nodes farther than `band_km` from the pair's current border are fixed;
    - the min cut decides the rest;
  - `merge_fragments(g, labels) -> tuple[np.ndarray, int]`: for each label, every connected piece but the largest joins the label it shares the most edges with. A piece with no outside edge is left as it is;
  - `expressway_rule(g, labels, parent: np.ndarray | None = None) -> tuple[np.ndarray, dict]`: Decision 13. Only nodes whose label may change are touched (`labels >= 0` and, when `parent` is given, the same parent);
  - `split_k(nodes: int, junctions: int, nodes_per_cell: int, min_k: int = 2) -> int`: `min(round(nodes / nodes_per_cell), junctions // 4)`; values below `min_k` give 1;
  - `split_cell(g, k, band_km, seed=SEED) -> tuple[np.ndarray, dict]`:
    - `scipy.cluster.vq.kmeans2(minit="++", seed=seed)` on the km coordinates of junctions (degree ≥ 3);
    - seeds snap to their nearest junction;
    - a network Voronoi by length assigns every node; unreached nodes take the nearest reached node;
    - then `refine_pairs`, `merge_fragments` and `expressway_rule`;
    - labels are 0..k'−1;
  - `warm_split(g, old: np.ndarray, band_km) -> tuple[np.ndarray, dict]`:
    - nodes with `old == -1` take the label of the nearest labelled node by length, or by Euclidean distance when unreached;
    - then `refine_pairs`, `merge_fragments` and `expressway_rule`;
    - labels keep the old codes;
  - `cell_hash(g) -> str`: sha256 over `np.sort(g.osm)` and the inner edges as sorted `(min osm, max osm, cap)` rows;
  - `split_jobs(jobs: list[dict], workers: int) -> dict[int, np.ndarray]`:
    - a job is `{"code", "graph", "old" (or None), "k", "band_km", "mode": "warm" | "fresh"}`;
    - jobs run largest first in a `multiprocessing` pool, sending only the job's arrays;
    - `per_worker_GB = 0.5` for L3 jobs, 3.0 for L2 jobs.

- [ ] **Step 1: Write the failing tests**

```python
def test_weighted_cut_finds_minimum(self):
    # two 10-node cliques (cap 10) joined by two edges of cap 10
    value, a, b = weighted_cut(lo, hi, cap, side_first_and_last, 20)
    self.assertEqual(value, 20); self.assertTrue(a[:10].all() and not a[10:].any())

def test_big_road_preferred_when_counts_tie(self):
    # cliques A - M - B; A-M two residential edges (cap 10), M-B two primary edges (cap 7); A fixed side 1, B side 2
    value, a, _ = weighted_cut(lo, hi, cap, side, 30)
    self.assertEqual(value, 14); self.assertTrue(a[10:20].all())

def test_river_crossing_is_free(self):
    osm, lon, lat, lo, hi = self.two_banks                    # two 3x3 grids on either bank joined by one bridge edge (the last)
    only_bridge_mask = np.zeros(len(lo), bool); only_bridge_mask[-1] = True
    g = CellGraph.from_edges(osm, lon, lat, lo, hi, highway=np.zeros(len(lo), np.int16), names=["primary"], river=only_bridge_mask)
    self.assertEqual(int(g.cap[only_bridge_mask][0]), 0)

def test_fresh_split_two_clusters(self):
    labels, info = split_cell(self.two_grids, k=2, band_km=1.0)          # two 6x6 grids 2 km apart, joined by 2 edges
    self.assertEqual(len(set(labels[:36])), 1); self.assertEqual(len(set(labels[36:])), 1); self.assertNotEqual(labels[0], labels[-1])
    self.assertEqual(info["final_cut_edges"], 2)

def test_merge_fragments_joins_most_connected(self):
    labels, moved = merge_fragments(self.g3, self.labels_with_piece)   # piece of label 1: 3 edges to label 2, 1 edge to label 3
    self.assertTrue((labels[self.piece] == 2).all()); self.assertEqual(moved, 1)

def test_expressway_takes_nearest_ordinary_node_and_absorbs_short_run(self):
    labels, info = expressway_rule(self.gx, self.lx)
    self.assertTrue((labels[self.motorway_in_land_a] == 0).all()); self.assertEqual(info["short_runs_absorbed"], 1)

def test_warm_split_keeps_a_min_cut_and_labels_new_nodes(self):
    old = self.two_grid_labels.copy(); self.assertTrue(np.array_equal(warm_split(self.two_grids, old, 1.0)[0], old))
    old2 = np.r_[old, [-1, -1, -1]]                                   # three new nodes hanging off grid 1
    self.assertTrue((warm_split(self.two_grids_plus, old2, 1.0)[0][-3:] == old[0]).all())

def test_cell_hash(self):
    self.assertEqual(cell_hash(self.g), cell_hash(self.g_permuted)); self.assertNotEqual(cell_hash(self.g), cell_hash(self.g_primary))

def test_split_k(self):
    self.assertEqual(split_k(5400, 1000, 1800), 3); self.assertEqual(split_k(5400, 8, 1800), 2); self.assertEqual(split_k(2000, 1000, 1800), 1)

def test_split_jobs_parallel_equals_serial(self):
    a, b = split_jobs(self.jobs, 1), split_jobs(self.jobs, 2)
    self.assertEqual(a.keys(), b.keys()); self.assertTrue(all(np.array_equal(a[k], b[k]) for k in a))
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_mincut -v`.

- [ ] **Step 3: Implement `mincut.py`**

Port `weighted_cut`, the pair loop of `refine`, `merge_fragments` and `absorb_short_runs` from the spike, replacing the raster land rule with Decision 13. The functions are pure: no file or OSRM access.

- [ ] **Step 4: Run the tests and confirm they pass**

- [ ] **Step 5: Run the whole suite, commit and push**

---

### Task 7: Partition labels: carry-over, rules and the L3 re-split

**Files:**
- Create: `speedmatrix/partition.py`
- Create: `data/partition/regions_l1_v3.2.geojson`, a byte copy of `reports/gate_compose_spike/regions_v3.2.geojson`. Its sha256 goes into `partition.json` and the task's commit message.
- Modify: `tests/sm_osrm_fixture.py` (adds `make_graphs`)
- Test: `tests/test_sm_partition.py`

**Interfaces:**
- Consumes: `TurnGraph` (Task 4); `CellGraph`, `split_cell`, `warm_split`, `split_k`, `cell_hash`, `split_jobs`, `merge_fragments`, `expressway_rule` (Task 6); `points_in_geometry` (sub-project 0); `_RiverIndex` (`model/clusters.py`).
- Produces:
  - `class PartitionError(ValueError)`; `MAX_CELLS = 65535`;
  - `@dataclass PartitionConfig(frozen: int = 2, l2_nodes: int = 25000, l2_min_k: int = 4, l2_band_km: float = 3.0, split_nodes: tuple = (1800,), split_band_km: tuple = (1.0,), fresh_frac: float = 0.2)`; `k = frozen + len(split_nodes)`;
  - `@dataclass NodeGraph`:
    - nodes: `osm`, `lon`, `lat (N,)`, in exactly the order of `g.node_osm`, so node index i is the same in both graphs;
    - undirected edges: `lo`, `hi: int32 (M,)`, `highway: int16 (M,)` (codes into `highway_names`, the graph's `meta["highways"]`), `length_m`, `river`, `ferry`, `expressway: bool (M,)`;
    - `component: int32 (N,)` (weak component of the whole graph);
  - `node_graph(g: TurnGraph, rivers: dict) -> NodeGraph`;
  - `cell_graph(ng: NodeGraph, mask: np.ndarray[bool] | None = None) -> CellGraph`: the sub-graph on the masked nodes (all when None), so `merge_fragments` and `expressway_rule` run on L1 and L2 cells too;
  - `sm_osrm_fixture.make_graphs(root, coords, ways, rivers=None, obstacles={}) -> (SegmentTable, TurnGraph, NodeGraph)`: `write_osm` → `build_segments` → `write_dataset` → `build_graph` → `node_graph`; every partition and gate fixture is built this way;
  - `@dataclass Previous(ids: int64 (n,) sorted, codes: int64 (n, k), l2_hash: dict[int, str] | None, source: str)`;
  - `import_seed(scratch: Path, out: Path) -> dict`:
    - reads `node_region_v3.2.npz` (L1), `node_region_h32all.npz` (L2 codes `100000 + 1000·R + s`) and `labels_l3.npy` (L3 codes aligned with the same `ids`);
    - drops negative ids (barrier copies);
    - keeps −1;
    - writes `ids` and `codes` with `l2_hash` None;
  - `load_seed(path) -> Previous`; `load_previous(release_dir) -> Previous` (from `node_labels.npz` and `partition.json`; JSON turns the `l2_hash` keys into strings, so `load_previous` converts them back to int);
  - `@dataclass Partition`:
    - `codes: int64 (N, k)` per node;
    - `cells: list[np.ndarray[int64]]` (stable codes per level, in dense-index order);
    - `seg_labels: list[np.ndarray[uint16] (S,)]`;
    - `l2_hash: dict[int, str]`;
    - `info: dict`;
  - `build_partition(g: TurnGraph, ng: NodeGraph, previous: Previous | None, polygons: dict, config: PartitionConfig, workers: int) -> Partition`;
  - `check_partition(ng, p, previous: Previous | None, expected_l1: int | None) -> dict[str, dict]`: checks named `labels_complete`, `nesting`, `contiguity_l{1..k}`, `l1_cells_unchanged` (blocking) and `l2_cells_changed` (warning);
  - `save_partition(p, release_dir)` writes:
    - `labels_l{ℓ}.npy` (uint16 per segment);
    - `node_labels.npz` (`ids`, `codes`);
    - `partition.json` (`format`, `k`, `config`, `cells`, `parents`, `l2_hash`, `resplit` counts, `rules` info, `checks`, `stats`, `seed`/`previous`, `polygons_sha256`, `rivers_sha256`);
  - `load_partition(release_dir) -> Partition`;
  - CLI `python -m speedmatrix.partition`:
    - `import-seed --scratch DIR --out FILE`;
    - `run --release DIR [--previous DIR | --seed FILE] [--polygons FILE] [--rivers FILE] [--workers N]`: Task 8 adds gates and their checks to `run`.

**Order inside `build_partition`.** Every step is a rule, re-run on every map:
1. **Carry-over:** codes by node id from `previous` (−1 for new nodes).
2. **New L1 labels:** polygon (`properties.region`). Nodes outside every polygon take the label reached by network propagation, then by Euclidean nearest.
3. **New L2 labels:** network propagation (multi-source Dijkstra by length, over edges whose two ends share the L1 label), then Euclidean nearest within the same L1. With no seed and no previous release, L2 is a fresh `split_cell` per L1 cell instead (`k = max(l2_min_k, round(nodes / l2_nodes))`, band `l2_band_km`).
4. **Expressway rule** for new expressway-only nodes, levels 1–2.
5. **Ferry rule** (Decision 11) on levels 1–2.
6. **L1 contiguity** (Decision 8). It can move whole pieces, such as region 6's western part, into another L1 cell, so it runs before nesting.
7. **L2 nesting:** split-then-merge (Decision 9).
8. **L2 contiguity** (Decision 8), restricted to siblings.
9. **L2 hashes:** `cell_hash` per L2 cell.
10. **Level 3 (and deeper):**
    - an unchanged hash keeps the old codes;
    - a changed hash gets `warm_split`, or `split_cell` when more than `fresh_frac` of its nodes are new or its node count changed by more than `fresh_frac`;
    - when `previous.l2_hash` is None (the v3.2 seed), every L2 cell counts as changed and gets `warm_split`, never `split_cell`: the seed's node counts match, and a fresh split would throw away the v3.2 cells;
    - new cell codes come from `max(existing) + 1`, in order of each cell's smallest OSM id;
    - all of it goes through `split_jobs`.
11. **Ferry rule and contiguity again** at the split levels, then nesting is re-checked at every level; a violation here raises `PartitionError` (it would be a bug in the rules above).
12. **Dense indices** per level. More than `MAX_CELLS` cells raises `PartitionError`.

Then each segment takes the labels of its tail node.

- [ ] **Step 1: Write the failing tests**

Fixtures: `make_graphs` on small grids (`grid(n)`) and their edited copies; `self.g_split` is the same map with way 1 split into ways 91 and 92 over the same nodes, so only way ids differ. Polygons: `{"type": "FeatureCollection", "features": [region 0 = west half, region 1 = east half]}`.

```python
def test_carry_over_ignores_way_ids(self):                         # Review Focus 1
    p1 = build_partition(self.g, self.ng, None, POLY, CFG, 1); prev = previous_of(p1, self.ng)
    p2 = build_partition(self.g_split, self.ng_split, prev, POLY, CFG, 1)   # way 1 split into ways 91 and 92
    np.testing.assert_array_equal(p2.codes, p1.codes); self.assertEqual(p2.info["resplit"], {"unchanged": len(p1.cells[1]), "warm": 0, "fresh": 0})

def test_saved_hashes_survive_the_json_round_trip(self):
    p1 = build_partition(self.g, self.ng, None, POLY, CFG3, 1); save_partition(p1, self.rel)
    prev = load_previous(self.rel); self.assertTrue(all(isinstance(c, int) for c in prev.l2_hash))
    p2 = build_partition(self.g, self.ng, prev, POLY, CFG3, 1)
    self.assertEqual(p2.info["resplit"], {"unchanged": len(p1.cells[1]), "warm": 0, "fresh": 0})

def test_seed_without_hashes_warm_splits_every_l2_cell(self):
    p = build_partition(self.g, self.ng, replace(previous_of(self.p3, self.ng), l2_hash=None), POLY, CFG3, 1)
    self.assertEqual(p.info["resplit"], {"unchanged": 0, "warm": len(self.p3.cells[1]), "fresh": 0})

def test_new_nodes_l1_by_polygon_l2_by_network(self):
    p = build_partition(self.g_spur, self.ng_spur, self.prev, POLY, CFG, 1)
    self.assertEqual(p.codes[self.spur, 0].tolist(), [1, 1, 1]); self.assertTrue((p.codes[self.spur, 1] == p.codes[self.attach, 1]).all())

def test_removed_bridge_fragment_joins_most_connected_sibling(self):   # Review Focus 3
    p = build_partition(self.g_nobridge, self.ng_nobridge, self.prev_x_spans_bridge, POLY, CFG, 1)
    self.assertTrue((p.codes[self.half2, 1] == self.code_y).all())     # 3 edges to Y, 1 to Z
    self.assertTrue(check_partition(self.ng_nobridge, p, self.prev_x_spans_bridge, 2)["contiguity_l2"]["ok"])

def test_island_without_link_is_not_a_violation(self):
    p = build_partition(self.g_island, self.ng_island, self.prev, POLY, CFG, 1)
    self.assertTrue(all(v["ok"] for k, v in check_partition(self.ng_island, p, self.prev, 2).items() if v["blocking"]))

def test_region_split_in_two_merges_the_small_piece(self):
    # previous L1 cell 0 = west half + a detached 5-node piece inside the east half, linked to it by 2 edges
    p = build_partition(self.g, self.ng, self.prev_detached, POLY, CFG, 1)
    self.assertTrue((p.codes[self.piece, 0] == 1).all()); self.assertEqual(len(p.cells[0]), 2)

def test_ferry_unit_takes_home_landing_labels(self):
    p = build_partition(self.g_ferry, self.ng_ferry, None, POLY, CFG, 1)   # landing W: 3 road edges, landing E: 1
    self.assertTrue((p.codes[self.ferry_inner] == p.codes[self.landing_w]).all())
    self.assertTrue((p.codes[self.landing_e] == p.codes[self.landing_w]).all())

def test_nesting_split_then_merge(self):
    p = build_partition(self.g, self.ng, self.prev_l2_across_l1, POLY, CFG, 1)
    l1_of_l2 = {}; [l1_of_l2.setdefault(c2, set()).add(c1) for c1, c2 in p.codes[:, :2].tolist()]
    self.assertTrue(all(len(s) == 1 for s in l1_of_l2.values()))

def test_only_changed_l2_cells_are_resplit_and_grown_ones_fresh(self):
    p = build_partition(self.g_two_changes, self.ng_two_changes, self.prev, POLY, CFG3, 1)
    self.assertEqual(p.info["resplit"], {"unchanged": self.n_l2 - 2, "warm": 1, "fresh": 1})   # one +2 nodes, one +40%

def test_k2_and_k3_levels(self):
    self.assertEqual(len(build_partition(self.g, self.ng, None, POLY, PartitionConfig(split_nodes=(), l2_nodes=40), 1).seg_labels), 2)
    p3 = build_partition(self.g, self.ng, None, POLY, PartitionConfig(split_nodes=(12,), l2_nodes=40), 1)
    self.assertEqual(len(p3.seg_labels), 3); self.assertEqual(p3.seg_labels[2].dtype, np.uint16)

def test_seed_import(self):
    # fake scratch: ids [-1, 5, 6, 7], L1 int8 [3, 3, 3, -1], L2 [103001, 103001, 103002, -1], L3 [103001000, 103001001, 103002000, -1]
    info = import_seed(self.scratch, self.out); s = load_seed(self.out)
    self.assertEqual(s.ids.tolist(), [5, 6, 7]); self.assertEqual(s.codes[2].tolist(), [-1, -1, -1]); self.assertIsNone(s.l2_hash)

def test_too_many_cells_raises(self):
    with mock.patch.object(partition, "MAX_CELLS", 3):
        with self.assertRaisesRegex(PartitionError, "cells"): build_partition(self.g, self.ng, None, POLY, CFG3, 1)
```

`previous_of(p, ng)` is a test helper building `Previous(ng.osm, p.codes, p.l2_hash, "test")`; `self.p3` is a fresh k = 3 partition of `self.g`.

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_partition -v`.

- [ ] **Step 3: Implement `partition.py` and copy the polygon file**

`node_graph` builds the undirected unique node pairs from the directed segments: highway from `seg_highway`, ferry when `seg_class == "pha"`, river from `_RiverIndex(features).crosses` on candidate edges only (a grid prefilter, as `matrix_graph.py` did), and components from `connected_components`.

- [ ] **Step 4: Run the tests and confirm they pass**

- [ ] **Step 5: Run the whole suite, commit and push**

```
git add speedmatrix/partition.py data/partition/regions_l1_v3.2.geojson tests/sm_osrm_fixture.py tests/test_sm_partition.py
```

`data/partition/` is not gitignored (`/data/*.json` matches only top-level files); check that the geojson is in the staged list.

- [ ] **Step 6: Import the v3.2 seed into `runtime/`**

```
.venv/Scripts/python.exe -m speedmatrix.partition import-seed --scratch runtime/matrix/partition/seed_v3.2_raw --out runtime/matrix/partition/seed_v3.2.npz
```

Expected: 25,870,961 ids read, 4,550 negative ids dropped; L1 22 codes, 579,913 nodes without a label; L2 1,009 codes; L3 13,793 codes. Skip when Task 1 Step 0 wrote `MISSING.txt`. Nothing to commit.

---

### Task 8: Gates of every level, gate drift, and the partition CLI end to end

**Files:**
- Create: `speedmatrix/gates.py`
- Modify: `speedmatrix/partition.py` (CLI `run` writes gates and adds their checks)
- Test: `tests/test_sm_gates.py`

**Interfaces:**
- Consumes: `TurnGraph` (Task 4); `Partition`, `NodeGraph` (Task 7).
- Produces:
  - `@dataclass LevelGates`:
    - per gate arc: `src_seg`, `dst_seg: int32 (A,)`, `kind: int8` (0 = continuation inside a geometry, 1 = turn), `via: int32` (node index), `via_osm: int64`, `via_lon_e6`, `via_lat_e6: int32` (so a later release can compare positions without the old graph), `cell_out`, `cell_in: uint16`;
    - per cell: `entry_offsets: int64 (C+1,)`, `entries: int32`, `exit_offsets: int64 (C+1,)`, `exits: int32` (unique segments, sorted);
  - `level_gates(g, seg_labels_level: np.ndarray[uint16], n_cells: int) -> LevelGates`;
  - `build_gates(g, p: Partition) -> list[LevelGates]`;
  - `check_gates(g, ng: NodeGraph, gates) -> dict`: `gates_nested` (the arcs of level ℓ are a subset of those of level ℓ+1) and `ferry_gates_at_landings` (a gate touching a ferry segment has a landing as its via node). Both blocking;
  - `gate_stats(g, gates) -> dict`, per level:
    - `cells`;
    - `gate_arcs`;
    - `via_nodes` (unique);
    - `entries_per_cell` and `exits_per_cell` as `{p50, p95, max}`;
    - `piece_cells = Σ entries × exits`;
  - `gate_drift(old: list[LevelGates], g: TurnGraph, levels=(0, 1), tol_m: float = 5.0) -> dict` (Decision 19): for each level, every unique `via_osm` of the old gates is looked up in `g.node_osm`. Returns per level `{"checked", "moved", "gone", "examples": [≤ 20 × {osm, old, new, metres}]}`, where `moved` counts nodes more than `tol_m` from their old coordinates;
  - `save_gates(gates, path)` / `load_gates(path) -> list[LevelGates]` (`gates.npz`, keys `l{ℓ}_<field>`).
- `partition run` writes `labels_l*.npy`, `node_labels.npz`, `gates.npz` and `partition.json` (checks + `gate_stats`). With `--previous DIR` it also adds the warnings `gate_drift_l1` and `gate_drift_l2` (value = `moved`, ok when 0, `blocking: false`; `gone` is reported beside it). It prints the release dir and exits 0 whenever it wrote `partition.json`, whatever the checks say; a crash exits non-zero (Decision 27).
- `gates.py` imports `Partition` and `NodeGraph` only under `typing.TYPE_CHECKING`, and the `partition` CLI imports `gates` inside the `run` command, so the two modules never import each other at load time.

- [ ] **Step 1: Write the failing tests**

```python
def test_chain_gate_inside_a_geometry_and_turn_gate(self):
    # TOWN, one level: node 101 in cell 0, every other node in cell 1
    lg = level_gates(self.g, self.labels_101_alone, 2)
    got = {(int(k), int(self.g.node_osm[v])) for k, v in zip(lg.kind, lg.via)}
    self.assertEqual(got, {(0, 107), (1, 101)})                      # 101->107 then 107->102 leaves cell 0; U-turn at 101 enters it
    entries = lg.entries[lg.entry_offsets[0]:lg.entry_offsets[1]]; exits = lg.exits[lg.exit_offsets[0]:lg.exit_offsets[1]]
    self.assertEqual(entries.tolist(), exits.tolist())                  # both are the single segment 101->107

def test_coarse_gates_are_fine_gates(self):
    gates = build_gates(self.gg, self.pgrid); self.assertTrue(check_gates(self.gg, self.nggrid, gates)["gates_nested"]["ok"])

def test_ferry_gates_only_at_landings(self):
    gates = build_gates(self.gf, self.pferry); self.assertTrue(check_gates(self.gf, self.ngf, gates)["ferry_gates_at_landings"]["ok"])
    bad = build_gates(self.gf, self.pferry_without_rule)              # labels written by hand: the inner ferry node in the other cell
    self.assertFalse(check_gates(self.gf, self.ngf, bad)["ferry_gates_at_landings"]["ok"])

def test_stats_and_round_trip(self):
    gates = build_gates(self.gg, self.pgrid); s = gate_stats(self.gg, gates)
    self.assertEqual(s["l3"]["piece_cells"], sum(len(e) * len(x) for e, x in self.entries_exits_l3))
    save_gates(gates, self.root / "gates.npz"); again = load_gates(self.root / "gates.npz")
    np.testing.assert_array_equal(again[2].dst_seg, gates[2].dst_seg)

def test_partition_cli_end_to_end(self):
    # grid(12) dataset + its segment table -> graph dir in <release>; polygons = two halves; k = 3, l2_nodes=40, split 12
    code, out = run_cli(["run", "--release", R, "--polygons", POLY_FILE, "--rivers", EMPTY_RIVERS, "--workers", "2"])
    self.assertEqual(code, 0); doc = json.loads((R / "partition.json").read_text())
    self.assertTrue(all(v["ok"] for v in doc["checks"].values() if v["blocking"]))
    self.assertEqual(np.load(R / "labels_l3.npy").dtype, np.uint16); self.assertTrue((R / "gates.npz").is_file())

def test_cli_exits_0_with_a_failing_check(self):
    # same release, polygons that cut one L1 cell in two -> contiguity_l1 fails; the JSON says so and the exit code is 0
    code, _ = run_cli(["run", "--release", R2, "--polygons", BAD_POLY_FILE, "--rivers", EMPTY_RIVERS, "--workers", "1"])
    self.assertEqual(code, 0); self.assertFalse(json.loads((R2 / "partition.json").read_text())["checks"]["contiguity_l1"]["ok"])

def test_gate_drift_counts_moved_and_gone(self):
    old = build_gates(self.gg, self.pgrid)
    moved_g = self.gg_moved                            # same map, one L1 gate via node shifted 8 m, one L2 gate via node deleted
    d = gate_drift(old, moved_g)
    self.assertEqual((d["l1"]["moved"], d["l2"]["gone"]), (1, 1)); self.assertEqual(gate_drift(old, self.gg)["l1"]["moved"], 0)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_gates -v`.

- [ ] **Step 3: Implement `gates.py` and the CLI wiring**

Arcs come from two vectorised sets:
- **continuations:** `(s, s+1)` where `s+1` belongs to the same EBN;
- **turns:** `(last(u), first(v))` over the turn CSR.

A gate is an arc whose two labels differ.

- [ ] **Step 4: Run the tests and confirm they pass**

- [ ] **Step 5: Run the whole suite, commit and push**

---

### Task 9: Re-resolving rules on a new map, and the orphan report

**Files:**
- Modify: `speedmatrix/scopes.py` (segment anchors fall back to the node pair)
- Create: `speedmatrix/rulecheck.py`
- Test: `tests/test_sm_scopes.py` (extend), `tests/test_sm_rulecheck.py`

**Interfaces:**
- Consumes: `ScopeResolver`, `ZoneError`, `RuleError`, `pair_lookup`, `load_segments`, `load_admin`, `load_water_lines`, `open_state` (sub-project 0 and Task 3).
- Produces:
  - `ScopeResult.moved: list[dict]`, default empty: `{"anchor": [w, a, b], "way_ids": [...]}`, one per anchor found through its node pair on other ways;
  - `rules_report(new: ScopeResolver, rules: list[Rule], old: ScopeResolver | None = None) -> dict`:
    - `{"rules": {id: {...}}, "summary": {...}, "blocking": [ids]}`;
    - each rule entry: `{kind, layer, confidence, active, segments_new, segments_old, orphans_new, orphans_old, moved, error, status}`;
    - `status` is one of `ok`, `moved`, `changed` (`|new − old| > 20%` of old and more than 10 segments), `new_orphans` (an orphan not present on the old map), `error`; when several apply, the first of `error`, `new_orphans`, `changed`, `moved`, `ok` wins; a rule without a scope gets `no_scope` and never blocks;
    - orphan anchors carry `"was": [[lat_a, lon_a], [lat_b, lon_b]]` from the old table when it has them;
    - `blocking` lists the active rules with `status == "error"` and the active rules of confidence `cao` or `tb` with any orphan on the new map, exactly as `speedmatrix.build rules-check` fails them (Decision 16); inactive rules never block;
  - CLI: `python -m speedmatrix.rulecheck --segments DIR [--previous DIR] [--as-of YYYY-MM-DD] --out FILE [--root ROOT]`:
    - opens `<root>/state.sqlite` with `sqlite3.connect("file:<path>?mode=ro", uri=True)`, so it never runs DDL, inserts or upgrades the schema, and reads the active rules in force;
    - writes the report;
    - exits 0 whenever it wrote the report, whatever `blocking` holds; a crash exits non-zero (Decision 27).

- [ ] **Step 1: Write the failing tests**

There are two maps, written with `write_osm` and `build_segments`:
- the old map has ways 1 "Phố A" `[11, 12, 13]`, 2 "Phố B", the four ring roads "Vành đai A–D" of a 2 km zone, and 5 "Phố C" `[51, 52]`;
- the rules (`RULES`) are: `anchor_rule` (segments `[[1, 11, 12]]`, `cao`), `names_a`, `names_b` (names, `cao`), `names_c` (names "Phố C", confidence `thap` but `active=True`), and `zone_rule` (the zone bounded by "Vành đai A–D", `cao`); `ORPHAN_ANCHOR_RULE` is `orphan_anchor` (segments `[[5, 51, 52]]`, `thap`, active); `MOVED_AND_CHANGED_RULE` is `moved_and_changed` (segments `[[1, 11, 12]]` plus names "Phố B", `thap`, active: its anchor moves and its count grows by 30); `NO_SCOPE_RULE` is `no_scope`, a rule whose body has no scope (the case `rules-check` prints as `no scope`);
- the new map has:
  - way 1 split into 91 `[11, 12]` and 92 `[12, 13]`, both named "Phố A";
  - "Vành đai D" renamed "Đường Mới";
  - "Phố C" deleted;
  - 30 extra segments of "Phố B".

```python
# tests/test_sm_scopes.py
def test_way_split_anchor_found_by_node_pair(self):                  # Review Focus 1
    r = ScopeResolver(self.new).resolve({"segments": [[1, 11, 12]]})
    self.assertEqual(len(r.indices), 1); self.assertEqual(r.orphans, []); self.assertEqual(r.moved, [{"anchor": [1, 11, 12], "way_ids": [91]}])

def test_gone_pair_is_an_orphan(self):
    r = ScopeResolver(self.new).resolve({"segments": [[5, 51, 52]]}); self.assertEqual(r.orphans, [{"anchor": [5, 51, 52]}])

# tests/test_sm_rulecheck.py
def test_statuses(self):
    rep = rules_report(ScopeResolver(self.new), RULES, ScopeResolver(self.old))
    st = {k: v["status"] for k, v in rep["rules"].items()}
    self.assertEqual(st, {"anchor_rule": "moved", "names_a": "ok", "names_b": "changed", "names_c": "new_orphans", "zone_rule": "error"})

def test_zone_error_is_blocking(self):                                # Review Focus 4
    rep = rules_report(ScopeResolver(self.new), RULES, ScopeResolver(self.old))
    self.assertEqual(rep["blocking"], ["zone_rule"]); self.assertIn("Vành đai D", rep["rules"]["zone_rule"]["error"])

def test_inactive_rule_errors_do_not_block(self):
    rep = rules_report(ScopeResolver(self.new), [replace(ZONE_RULE, active=False)]); self.assertEqual(rep["blocking"], [])

def test_orphan_keeps_its_old_geometry(self):
    rep = rules_report(ScopeResolver(self.new), [ORPHAN_ANCHOR_RULE], ScopeResolver(self.old))
    self.assertEqual(rep["rules"]["orphan_anchor"]["orphans_new"][0]["was"], [[LAT_51, LON_51], [LAT_52, LON_52]])

def test_trusted_orphan_blocks(self):
    rep = rules_report(ScopeResolver(self.new), [replace(NAMES_C, confidence="cao")], ScopeResolver(self.old))
    self.assertEqual(rep["blocking"], ["names_c"])

def test_precedence_and_no_scope(self):
    rep = rules_report(ScopeResolver(self.new), [MOVED_AND_CHANGED_RULE, NO_SCOPE_RULE], ScopeResolver(self.old))
    self.assertEqual({k: v["status"] for k, v in rep["rules"].items()}, {"moved_and_changed": "changed", "no_scope": "no_scope"})

def test_cli_writes_report_and_exits_0(self):
    before = (R / "state.sqlite").read_bytes()
    self.assertEqual(main(["--root", R, "--segments", NEW, "--previous", OLD, "--out", OUT]), 0)
    doc = json.loads(OUT.read_text()); self.assertEqual(doc["summary"]["error"], 1); self.assertEqual(doc["blocking"], ["zone_rule"])
    self.assertEqual((R / "state.sqlite").read_bytes(), before)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_scopes tests.test_sm_rulecheck -v`.

- [ ] **Step 3: Implement**

In `ScopeResolver._resolve`, after `find_segments`, the anchors still missing go through `pair_lookup`. A hit adds all rows of the pair and a `moved` entry; a miss stays an orphan. `rulecheck` builds one resolver per table, with admin and water loaded from each segments dir.

- [ ] **Step 4: Run the tests and confirm they pass, then the whole suite**

`tests/test_sm_build.py` must stay green: an anchor found by pair is no longer an orphan, so check its seed.

- [ ] **Step 5: Commit and push**

---

### Task 10: Checking the graph against OSRM (spec §12.1)

**Files:**
- Create: `speedmatrix/verify_osrm.py`
- Test: `tests/test_sm_verify.py`

**Interfaces:**
- Consumes:
  - `TurnGraph`, `load_graph`, `segment_ds` (Task 4);
  - `StaticRouter`, `parallel_table` (Task 5);
  - `read_datasources`, `read_dataset` (Task 1);
  - `load_layer`, `ReleaseMismatch` (sub-project 0);
  - `run_measured`, `default_workers`, `free_disk_gb` (Task 2).
- Produces:
  - `PROTECTED_PORTS = (5000, 8000)`; `SAMPLE_SLOT = ("weekday", 16)`; `SAMPLE_POINTS = 500`; `SAMPLE_SEED = 12`; `TABLE_ABS_S = 2.0`; `TABLE_REL = 0.002`; `SEGMENT_TOL = (1, 0.01)`; `SEGMENT_MIN_SHARE = 0.999`;
  - `seg_kmh_from_layer(g, layer, speeds_report: dict) -> np.ndarray[float32] (S,)`: `layer.at(*SAMPLE_SLOT)[g.seg_row]`. `speeds_report` is `<speeds>/report.json`; the layer itself carries no segments sha. It raises `ReleaseMismatch` when `speeds_report["segments_sha256"]` differs from `g.meta["segments_sha256"]` or `layer.n` differs from the table's row count;
  - `write_speed_file(g, seg_kmh, path) -> int`: one line `tail_osm,head_osm,kmh` per unique directed pair, sorted, integer km/h, written in chunks of 1M rows; returns the row count;
  - `copy_dataset(src_data_dir, dst_dir) -> None`: a full file copy. It raises `RuntimeError` when free space is below the dataset size + 2 GB;
  - `customize_copy(tools, base_copy, speed_file, threads) -> StepResult`: `osrm-customize --segment-speed-file <file> -t <threads> <base>`;
  - `class Routed(base, port, tools)` context manager:
    - refuses `PROTECTED_PORTS` (`ValueError`) and a port in use (`RuntimeError`, nothing touched);
    - runs `osrm-routed --algorithm mld --ip 127.0.0.1 --port P --threads 4 --max-table-size 2500 <base>`;
    - waits until `/nearest` answers;
    - stops only the process it started (checked by identity);
  - `class OsrmClient(port)`: `table(lonlat: np.ndarray) -> (seconds (P, P), locations (P, 2))` and `nearest(lon, lat, number=2) -> list[(lon, lat, distance_m)]`. It also refuses `PROTECTED_PORTS`;
  - `sample_points(g, n, seed, mask: np.ndarray[bool] | None = None) -> np.ndarray[int64]`: segment ids taken from a seeded permutation (`default_rng(seed).permutation`) of the eligible segments, first `min(n, eligible)` of them, so a small graph never raises. Eligible segments:
    - are not motorway or motorway_link;
    - have `seg_fixed_ds == 0`;
    - are at least 20 m long;
    - are not tiny;
    - lie in an EBN whose (geometry, direction) appears only once;
    - lie in the largest weak component;
    - are in `mask` when one is given (the smoke test passes "L1 label is 15");
  - `compare_tables(ours, theirs) -> dict`: over pairs where both are finite and ours is not NaN, with tolerance `max(TABLE_ABS_S, TABLE_REL · theirs)`. Returns `{"pairs", "over", "max_abs_s", "max_rel", "unreachable_mismatch", "examples": [≤ 20 × {i, j, ours, osrm}]}`;
  - `datasource_coverage(g, fwd_src, rev_src) -> dict`: for every graph segment, the source of its real-segment entry must be 1 (the speed file). Returns `{"segments", "not_updated", "examples"}`;
  - `segment_agreement(g, seg_kmh, base_copy) -> dict`: compares `36·len/kmh` rounded with the customized `.geometry` durations of the same entries. Returns `{"share_within", "p99_abs_ds", "max_abs_ds"}`;
  - `verify_release(release_dir, speeds_dir, tools, port, workers, points: int = SAMPLE_POINTS, mask: np.ndarray[bool] | None = None) -> dict[str, dict]`. Its checks are all blocking:
    - `osrm_table_over_tolerance == 0`;
    - `osrm_table_pairs >= 0.9 × planned pairs`, where planned pairs = `p·(p−1)` for the `p` points actually drawn (the shortfall against `points` is recorded beside it);
    - `osrm_datasource_not_updated == 0`;
    - `osrm_segment_agreement >= 0.999`.

    It writes `verify.json` and removes `verify/` (the copy and the CSV) in a `finally`;
  - CLI: `python -m speedmatrix.verify_osrm --release DIR --speeds DIR [--port 5058] [--workers N] [--points 500]`. Exits 0 whenever it wrote `verify.json`, whatever the checks say; a crash exits non-zero (Decision 27).

**Order inside `verify_release`** (Decision 30): write the speed file, copy the dataset, customize the copy, read its data sources and durations; start `Routed`, get `/table` and the `/nearest` answers, stop `Routed`; only then run `parallel_table` for our side. `osrm-routed` (about 4–5 GB on the nationwide copy) and the router workers never run at the same time.

Points whose OSRM `sources[i].location` lies more than 1 m from the midpoint, or whose `/nearest` with `number=2` returns two candidates within 1 m, are dropped and replaced from the same permutation. At most 2 × `points` candidates are tried, and the drop count is recorded.

The gate snap check of spec §6.6 is not here: it holds by construction, and gate drift (Task 8) replaces it (Decision 19).

- [ ] **Step 1: Write the failing tests**

```python
def test_speed_file_rows(self):
    n = write_speed_file(self.g, self.kmh, self.root / "s.csv"); lines = (self.root / "s.csv").read_text().splitlines()
    self.assertEqual(n, 13); self.assertEqual(lines[0], "101,107,25"); self.assertEqual(len(set(lines)), 13)

def test_protected_and_busy_ports_refused(self):
    for port in (5000, 8000):
        with self.assertRaises(ValueError): Routed(self.base, port, self.tools).__enter__()
        with self.assertRaises(ValueError): OsrmClient(port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen(); busy = s.getsockname()[1]
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not start")):
            with self.assertRaisesRegex(RuntimeError, "in use"): Routed(self.base, busy, self.tools).__enter__()

def test_sample_points_rules(self):
    pts = sample_points(self.g_mixed, 50, 12)     # TOWN + a motorway way, a 15 m segment, a tiny EBN, a duplicated EBN
    self.assertTrue(set(pts.tolist()).isdisjoint(self.excluded)); np.testing.assert_array_equal(pts, sample_points(self.g_mixed, 50, 12))
    self.assertEqual(len(pts), self.n_eligible)    # fewer eligible segments than asked: no error, all of them

def test_sample_points_mask(self):
    mask = np.zeros(self.g_mixed.n_segments, bool); mask[self.ebn0_segments] = True
    self.assertTrue(set(sample_points(self.g_mixed, 50, 12, mask).tolist()) <= set(self.ebn0_segments))

def test_seg_kmh_refuses_another_segment_table(self):
    with self.assertRaises(ReleaseMismatch): seg_kmh_from_layer(self.g, self.layer, {"segments_sha256": "other"})

def test_compare_tolerance(self):
    r = compare_tables(np.array([[501.9, 3607.1]]), np.array([[500.0, 3600.0]])); self.assertEqual(r["over"], 0)
    r = compare_tables(np.array([[502.1, 3607.3]]), np.array([[500.0, 3600.0]])); self.assertEqual(r["over"], 2)

def test_datasource_coverage_counts_graph_directions_only(self):
    fwd, rev = self.sources_all_updated_but_one_way_reverse
    self.assertEqual(datasource_coverage(self.g, fwd, rev)["not_updated"], 0)
    fwd[self.entry_of_101_107] = 0; self.assertEqual(datasource_coverage(self.g, fwd, rev)["not_updated"], 1)

def test_verify_flow_with_fakes(self):
    # Routed/OsrmClient/customize replaced by fakes; the fake table = StaticRouter result + 1 s, one pair + 30 s.
    # The fake Routed records enter/exit; the fake parallel_table asserts that Routed has exited (Decision 30).
    checks = verify_release(self.release, self.speeds, self.tools, 5058, 1)
    self.assertFalse(checks["osrm_table_over_tolerance"]["ok"]); self.assertEqual(checks["osrm_table_over_tolerance"]["value"], 1)
    self.assertEqual(self.events, ["routed_enter", "routed_exit", "parallel_table"])
    self.assertFalse((self.release / "verify").exists())

def test_cli_exits_0_on_failed_checks(self):
    self.assertEqual(main_with_fakes(["--release", str(self.release), "--speeds", str(self.speeds)]), 0)
    self.assertFalse(json.loads((self.release / "verify.json").read_text())["osrm_table_over_tolerance"]["ok"])
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_verify -v`.

- [ ] **Step 3: Implement `verify_osrm.py`**

`parallel_table` uses `default_workers(0.8, workers)`. The OSRM table for 500 points is one `/table` call.

- [ ] **Step 4: Run the tests and confirm they pass**

- [ ] **Step 5: Run the whole suite, commit and push**

---

### Task 11: Job state and the release pipeline

**Files:**
- Modify: `speedmatrix/state.py` (schema 2, `jobs`), `tests/test_sm_state.py` (the newer-schema test uses `'3'`)
- Modify: `speedmatrix/build.py` (`groups --out DIR`, Decision 28), `tests/test_sm_build.py` (one test)
- Create: `speedmatrix/update.py`
- Modify: `CLAUDE.md`. Add a "Speed matrix" section with the `speedmatrix.build` and `speedmatrix.update` commands, the release layout and the Decisions 6, 16 and 24 in one line each. Note that it replaces `reports/gate_compose_spike/update_matrix.py`.
- Test: `tests/test_sm_update.py`

**Interfaces:**
- Produces (`state.py`):
  - `SCHEMA = 2`;
  - a schema-1 file is upgraded in place: the `jobs` table is created and `meta.schema` is set to `2`; schema 3 or newer raises;
  - `jobs(id TEXT PK, kind TEXT, status TEXT, started_at TEXT, finished_at TEXT, info TEXT, error TEXT, log_path TEXT)`;
  - `State.start_job(id, kind, log_path)`, `State.finish_job(id, status, info: dict, error: str | None = None)`, `State.jobs(kind=None, limit=20) -> list[dict]`.
- Produces (`build.py`): `groups ... [--out DIR]` writes the views into `DIR` instead of `<speeds>/groups/<as_of>` and prints `DIR`. Without `--out` nothing changes.
- Produces (`update.py`):
  - constants `TASK_NAME = "HanoiTrafficMatrixUpdate"`, `BUILD_PORT = 5058`, `SERVE_PORT = 5059`, `KEEP = 2`, `MAX_STEP_PEAK_GB = 24`, `DISK_FACTOR = 1.5`, `BOOTSTRAP_RELEASE_GB = 12`, `EXTRACT_PEAK_DEFAULT_GB = 13.1`, `GEOFABRIK_PBF = manage.MAP_URL`, `GEOFABRIK_MD5 = GEOFABRIK_PBF + ".md5"`;
  - `class UpdateError(RuntimeError)`;
  - `@dataclass Check(value, ok: bool, blocking: bool)`;
  - `check_url(url) -> str`: returns `url` when it starts with `https://download.geofabrik.de/`, else raises `ValueError`;
  - `download_pbf(url, dest: Path, md5: str, opener=urllib.request.urlopen) -> None`: `check_url`; streams into `<dest>.part`; computes the md5 of the `.part`; on a mismatch deletes the `.part` and raises `UpdateError("downloaded PBF does not match its md5")`; on a match renames it to `dest`. A `urllib.error.URLError` or `OSError` deletes the `.part` and raises `UpdateError("<Class>: download from Geofabrik failed")`, never the exception text (spec §13);
  - `release_size_gb(root, current: dict) -> float`: the measured size of the current release dir plus the segments, speeds and groups dirs it references;
  - `class Steps(Protocol)`. Each method returns a dict that may hold `"checks": {name: Check-dict}`:
    - `remote_md5() -> str`;
    - `download(dest: Path, md5: str) -> None`;
    - `build_osrm(pbf, rel) -> dict` (holds `pbf_copy` and the `osrm-extract` peak);
    - `segments(pbf_copy) -> Path`;
    - `graph(rel, segments_dir) -> dict`;
    - `partition(rel, previous_release: Path | None, seed: Path | None) -> dict`;
    - `rules(rel, segments_dir, previous_segments: Path | None, as_of) -> dict`;
    - `speeds(segments_dir, as_of) -> Path`;
    - `groups(segments_dir, speeds_dir, out_dir: Path, as_of) -> Path`;
    - `verify(rel, speeds_dir) -> dict`;
  - `class RealSteps(root, workers, build_port, log_path, opener=urllib.request.urlopen)`:
    - `fetch(url) -> bytes`: `check_url`, then one GET through `opener`; `remote_md5` is `fetch(GEOFABRIK_MD5)`'s first token; `download` is `download_pbf(GEOFABRIK_PBF, dest, md5, self.opener)`;
    - every heavy step is a `run_measured` subprocess of `sys.executable -m speedmatrix.<module> …` with `cwd` = repo root and `PYTHONIOENCODING=utf-8`;
    - `segments`, `speeds` and `groups` call `speedmatrix.build` and take the printed path (`StepResult.last_line`); `groups` passes `--out <rel>/groups`;
    - `partition`, `rules` and `verify` read their checks from `partition.json`, `rules.json` and `verify.json` (`rules.json` gives `rules_blocking` = the `blocking` list, ok when empty, blocking; `rules_new_orphans` and `rules_changed` as warnings). Any non-zero exit, or a missing JSON, raises `UpdateError("<step> failed with exit <code>, see <log>")` (Decision 27);
    - each step's `seconds` and `peak_bytes` go into `release.json` `steps`; a peak above `MAX_STEP_PEAK_GB` raises `UpdateError`;
  - `run_update(root, steps, *, pbf=None, force=False, keep=KEEP, min_free_gb=None, as_of=None, seed=None, clock=datetime.now, memory=available_memory_gb, disk=free_disk_gb) -> int`: exits 0 when promoted or unchanged, 1 on failed checks, 2 on an error or a held lock;
  - `prune(root, keep, protect: set[str], before_build: bool) -> list[str]`:
    - before a build, every release but the protected ones is removed;
    - after a build, only the newest `keep` promoted releases and the newest failed one are kept;
    - then `runtime/matrix/segments/<sha>` and `runtime/matrix/speeds/<version>` dirs that no kept release references are removed, the format-1 segments dir included;
    - a dir that cannot be removed (a mapped `.npy`, an open file) is skipped with a warning in the log and the job info, never an error;
    - a path outside `<root>/releases`, `<root>/segments` or `<root>/speeds` raises;
  - `lock(root)` context manager: `update.lock` holds `{pid, identity}`. A live lock raises `UpdateError("another update is running (pid N)")`; a lock whose process is gone or has another identity is taken over;
  - `serve(root, port=SERVE_PORT) -> dict`, `stop(root) -> dict | None`, `status(root) -> dict`;
  - `schedule(root, day="SUN", time="03:00", runner=subprocess.run)` and `unschedule(runner=subprocess.run)`;
  - CLI: `python -m speedmatrix.update run [--pbf PATH] [--force] [--root DIR] [--keep 2] [--workers N] [--build-port 5058] [--min-free-gb GB] [--seed FILE] [--as-of DATE] | serve [--port 5059] | stop | status | schedule [--day SUN] [--time 03:00] | unschedule`.

**`run_update` order:**
1. Take the lock (a held live lock returns 2 here, before anything is written). Read `current.json`. Pick the release id `YYYYMMDD-HHMMSS` from `clock()`, with `-2`, `-3`, … appended when that dir already exists. Start the job row under that id (kind `update`).
2. Get the md5: the remote `.md5` (or that of `--pbf`). If it equals `current.pbf_md5` and `--force` is not given, finish the job as `unchanged` and return 0; no release dir is created.
3. `prune(before_build=True)`. Then the disk floor: `min_free_gb` if given, else `DISK_FACTOR × release_size_gb(current)`, or `DISK_FACTOR × BOOTSTRAP_RELEASE_GB` (18 GB) without a current release. `disk(root) < floor` raises `UpdateError("only X GB free on <drive>, need Y")`.
4. Create `releases/<id>/`. Write `release.json` (`status: running`).
5. Run the steps in order. After each step, merge its checks into `release.json`; when any blocking check is not ok, stop there (Decision 16):
   - `download(<rel>/<name>.osm.pbf, md5)` unless `--pbf` was given;
   - memory gate (Decision 29): `memory()` must be at least the `osrm-extract` peak recorded in the current release's `build_matrix.json` (else `EXTRACT_PEAK_DEFAULT_GB`) plus 1 GB, else `UpdateError("only X GB memory available, the OSRM build needs Y")`;
   - `build_osrm`; then delete the PBF this run downloaded (the OSRM build keeps its own copy, `pbf_copy`). A `--pbf` input is never deleted;
   - `segments(pbf_copy)`;
   - `graph`;
   - `partition`: previous = the current release, else the seed;
   - `rules`: previous segments = `current.segments_dir`;
   - `speeds`;
   - `groups` into `<rel>/groups/`;
   - `verify`.
6. A blocking check that is not ok gives status `failed_checks`, return 1; `current.json` stays as it was.
7. Promote:
   - set `release.json` status to `promoted`;
   - write `current.json` (`id`, `release`, `pbf_md5`, `pbf_sha256`, `graph_sha256`, `segments_dir`, `speeds_dir`, `groups_dir`, `summary`, `finished`) atomically;
   - restart the matrix server if `server.json` shows a live one;
   - `prune(before_build=False)`.
8. Any exception gives status `error` and records `"<Class>: <message>"`, where the message of an `UpdateError` is the fixed text above; return 2. The job row and (when it exists) `release.json` are always finished in an outer `finally`.

- [ ] **Step 1: Write the failing tests**

`FakeSteps(**overrides)` records each called step in `called`, writes the minimum files and returns the checks given per step name (`checks={"rules": {name: Check}}`); its `download` calls the real `download_pbf(GEOFABRIK_PBF, dest, md5, opener=lambda url: io.BytesIO(self.pbf_bytes))`, so the md5 handling is the real code. `mkcurrent(root, md5)` writes a promoted release and `current.json`. Every test passes `memory=lambda: 64.0`, `disk=lambda root: 500.0` unless it tests them, and a `clock` that advances one second per call.

```python
# tests/test_sm_state.py
def test_schema_1_upgrades(self):
    # create a schema-1 file with the old DDL, then open_state
    s = open_state(self.db); self.assertEqual(s.jobs(), []); self.assertEqual(meta_schema(self.db), "2")

# tests/test_sm_build.py
def test_groups_out_dir(self):
    code, out = run_cli(["groups", "--segments", SEG, "--speeds", SPD, "--out", str(self.tmp / "g")])
    self.assertEqual((code, out.strip().splitlines()[-1]), (0, str(self.tmp / "g"))); self.assertFalse((Path(SPD) / "groups").exists())

# tests/test_sm_update.py
def test_unchanged_md5_is_a_no_op(self):
    mkcurrent(R, "abc"); f = FakeSteps(md5="abc")
    self.assertEqual(run_update(R, f, **ENV), 0); self.assertEqual(f.called, ["remote_md5"]); self.assertEqual(len(list((R / "releases").iterdir())), 1)
    self.assertEqual(open_state(R / "state.sqlite").jobs()[0]["status"], "unchanged")

def test_md5_mismatch_keeps_current_and_removes_part(self):                  # Review Focus 2
    mkcurrent(R, "old"); before = (R / "current.json").read_bytes()
    self.assertEqual(run_update(R, FakeSteps(md5="new", pbf_bytes=b"not the PBF"), **ENV), 2)
    self.assertEqual((R / "current.json").read_bytes(), before)
    self.assertEqual(list(R.rglob("*.part")), []); self.assertEqual(list((R / "releases").rglob("*.pbf")), [])
    self.assertIn("does not match its md5", open_state(R / "state.sqlite").jobs()[0]["error"])

def test_network_error_is_recorded_without_its_text(self):
    self.assertEqual(run_update(R, FakeSteps(md5_error=urllib.error.URLError("https://x?key=secret timed out")), **ENV), 2)
    err = open_state(R / "state.sqlite").jobs()[0]["error"]
    self.assertTrue(err.startswith("URLError")); self.assertNotIn("secret", err)

def test_failed_check_keeps_current(self):
    mkcurrent(R, "old"); self.assertEqual(run_update(R, FakeSteps(md5="new", checks={"partition": {"contiguity_l3": Check(2, False, True)}}), **ENV), 1)
    self.assertEqual(json.loads((R / "current.json").read_text())["pbf_md5"], "old")

def test_rule_error_blocks_promotion_and_stops_the_run(self):                 # Review Focus 4
    f = FakeSteps(md5="new", checks={"rules": {"rules_blocking": Check(["zone_rule"], False, True)}})
    self.assertEqual(run_update(R, f, **ENV), 1); self.assertNotIn("speeds", f.called)

def test_warnings_do_not_block(self):
    self.assertEqual(run_update(R, FakeSteps(md5="new", checks={"rules": {"rules_new_orphans": Check(3, False, False)}}), **ENV), 0)

def test_promotion_and_prune_with_unique_ids(self):
    for md5 in ("a", "b", "c"): run_update(R, FakeSteps(md5=md5), **ENV)         # three runs within one minute
    rel = sorted(p.name for p in (R / "releases").iterdir()); self.assertEqual(len(rel), 2); self.assertEqual(len(set(rel)), 2)
    self.assertEqual(json.loads((R / "current.json").read_text())["pbf_md5"], "c")

def test_prune_removes_unreferenced_segments_and_speeds(self):
    mkcurrent(R, "a", segments="seg_a", speeds="spd_a"); mkdirs(R / "segments/seg_old", R / "speeds/spd_old")
    prune(R, 2, set(), before_build=False)
    self.assertEqual(sorted(p.name for p in (R / "segments").iterdir()), ["seg_a"]); self.assertFalse((R / "speeds/spd_old").exists())

def test_prune_skips_a_locked_dir(self):
    with mock.patch("shutil.rmtree", side_effect=PermissionError("in use")):
        self.assertEqual(prune(R, 2, set(), before_build=True), [])              # no exception

def test_pbf_input_is_never_deleted(self):
    src = R / "input.osm.pbf"; src.write_bytes(b"pbf")
    run_update(R, FakeSteps(), pbf=src, **ENV); self.assertTrue(src.exists())

def test_live_lock_refuses_without_touching_anything(self):                       # Review Focus 5
    write_lock(R, os.getpid(), process_identity(os.getpid())); before = (R / "update.lock").read_bytes()
    self.assertEqual(run_update(R, FakeSteps(md5="x"), **ENV), 2)
    self.assertFalse((R / "releases").exists()); self.assertEqual((R / "update.lock").read_bytes(), before)
    self.assertFalse((R / "state.sqlite").exists())

def test_stale_lock_is_taken_over(self):
    write_lock(R, DEAD_PID, {"executable": "x", "created": 1}); self.assertEqual(run_update(R, FakeSteps(md5="x"), **ENV), 0)

def test_disk_floor_from_current_release(self):
    mkcurrent(R, "a", size_gb=10.0)                                              # fake release_size_gb -> 10
    self.assertEqual(run_update(R, FakeSteps(md5="b"), **dict(ENV, disk=lambda root: 14.0)), 2)   # needs 15
    self.assertEqual(run_update(R, FakeSteps(md5="b"), **dict(ENV, disk=lambda root: 16.0)), 0)

def test_memory_gate(self):
    f = FakeSteps(md5="x")
    self.assertEqual(run_update(R, f, **dict(ENV, memory=lambda: 10.0)), 2); self.assertNotIn("build_osrm", f.called)
    self.assertIn("memory", open_state(R / "state.sqlite").jobs()[0]["error"])

def test_only_geofabrik(self):
    with self.assertRaises(ValueError): RealSteps(R, 1, 5058, R / "l.log").fetch("https://example.com/x.md5")
    with self.assertRaises(ValueError): download_pbf("https://example.com/x.pbf", R / "x.pbf", "0" * 32)

def test_release_json_records_versions_steps_and_checks(self):
    run_update(R, FakeSteps(md5="x"), **ENV); doc = json.loads(next((R / "releases").glob("*/release.json")).read_text())
    self.assertTrue({"code_version", "steps", "checks", "pbf"} <= doc.keys()); self.assertIn("blocking", next(iter(doc["checks"].values())))

def test_schedule_command_line(self):
    seen = []; schedule(R, runner=lambda argv, **kw: seen.append(argv) or SimpleNamespace(returncode=0))
    argv = seen[0]; self.assertEqual(argv[:4], ["schtasks", "/Create", "/TN", "HanoiTrafficMatrixUpdate"])
    self.assertIn("speedmatrix\\update.py\" run --root", argv[5]); self.assertEqual(argv[6:], ["/SC", "WEEKLY", "/D", "SUN", "/ST", "03:00", "/F"])

def test_serve_refuses_protected_port(self):
    with self.assertRaises(ValueError): serve(R, 5000)
```

`RealSteps` reads checks from the step's JSON and treats a crash as an error:

```python
def test_real_steps_read_checks_and_treat_crash_as_error(self):
    rel = R / "releases/r"; rel.mkdir(parents=True); steps = RealSteps(R, 1, 5058, R / "l.log")
    def ok(cmd, log, cwd, env=None):
        (rel / "partition.json").write_text(json.dumps({"checks": {"contiguity_l3": {"value": 1, "ok": False, "blocking": True}}}))
        return StepResult(0, 1.0, 10, str(rel))
    with mock.patch("speedmatrix.update.run_measured", ok):
        self.assertFalse(steps.partition(rel, None, None)["checks"]["contiguity_l3"]["ok"])
    (rel / "partition.json").unlink()
    with mock.patch("speedmatrix.update.run_measured", lambda *a, **k: StepResult(1, 1.0, 10, "")):
        with self.assertRaisesRegex(UpdateError, "partition failed with exit 1"): steps.partition(rel, None, None)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run `.venv/Scripts/python.exe -m unittest tests.test_sm_state tests.test_sm_build tests.test_sm_update -v`.

- [ ] **Step 3: Implement `state.py` schema 2, `build.py` `--out`, `update.py` and the `CLAUDE.md` section**

`schedule` uses `/TR "\"<pythonw.exe>\" \"<repo>\speedmatrix\update.py\" run --root \"<root>\""`, with `pythonw.exe` next to `sys.executable`. On Windows, the lock and the server use `osrm/manage.py` `process_identity`. Elsewhere, liveness is `os.kill(pid, 0)`. `update.py` binds `free_disk_gb` and `available_memory_gb` at module level (`from .sysres import ...`) and uses them only as the defaults of `disk` and `memory`.

- [ ] **Step 4: Run the tests and confirm they pass, then the whole suite**

- [ ] **Step 5: Commit and push** (`speedmatrix/state.py`, `speedmatrix/build.py`, `speedmatrix/update.py`, `CLAUDE.md`, `tests/test_sm_state.py`, `tests/test_sm_build.py`, `tests/test_sm_update.py`)

---

### Task 12: Real nationwide runs and the Vietnamese report

**Files:**
- Create: `tests/test_sm_real_smoke.py` (runs only with `SM_REAL_DATA=1`)
- Create: `reports/map_update_gates.md` (in Vietnamese)
- Modify: only when a run exposes a defect. Then fix it in the owning module with a new failing test first, run the whole suite, and record the defect in the report.

**Interfaces:**
- Consumes: everything above.

`tests/test_sm_real_smoke.py` (spec §14) does two things:
- reloads the current release's partition and gates, and re-runs `check_partition` (with `previous=None`) and `check_gates` (all blocking checks ok);
- runs §12.1 on region 15 (Hà Nội) only, on port 5061: `verify_release(release, speeds, tools, 5061, workers, points=200, mask=<segments whose L1 label is region 15>)`, with the same tolerance.

It skips when `current.json` is absent.

- [ ] **Step 1: Check the seed**

`runtime/matrix/partition/seed_v3.2.npz` should exist (Task 7 Step 6). If it does not and `runtime/matrix/partition/seed_v3.2_raw/` holds the raw tables, run the Task 7 Step 6 command now. If Task 1 Step 0 wrote `MISSING.txt`, record that and run Step 3 without `--seed` (Decision 7 fallback).

- [ ] **Step 2: Check resources**

Record:
- free space on E:. With no current release, `update` refuses below 18 GB (1.5 × 12 GB). The spike release `runtime/matrix/releases/20261021-1400` (6.7 GB) is pruned first; after the bootstrap the floor becomes 1.5 × the measured release size;
- available memory.

The OSRM build needs at least 14.1 GB available (Decision 29). If less is available, wait for other sessions' jobs rather than lowering the limits.

- [ ] **Step 3: Bootstrap run on the PBF of v3.2 (regression)**

Run in the background:
```
.venv/Scripts/python.exe -m speedmatrix.update run --pbf osrm/data/vietnam-latest.osm.pbf --force --seed runtime/matrix/partition/seed_v3.2.npz
```

Budget about 3 hours:
- the build, graph, speeds, groups and §12.1 steps took about 1 h 20 min in the spike;
- the seed carries no L2 hashes, so the partition step warm-splits every one of the about 1,009 L2 cells (Decision 10). The spike's fresh L3 split took 80 minutes with 6 workers; expect about 80 minutes with 14 workers, since warm splits refine a 1 km band but there are more jobs per worker.

Expected: exit 0, status `promoted`. The run never deletes `osrm/data/vietnam-latest.osm.pbf` (a `--pbf` input). Against v3.2:
- L1 = 22;
- L2 = 1,009 ± 1;
- L3 within 5% of 13,793.

Record per step:
- wall time and peak memory (`release.json` `steps`);
- every check;
- `gate_stats` per level;
- the rules report summary (expected: 0 blocking, as `rules-check` on 2026-10-22 was 0 failing);
- §12.1 numbers: pairs, max error, drops, datasource coverage, segment agreement.

If a blocking check fails:
- find the cause;
- fix it with a test in its module;
- commit and push;
- re-run with `--force`.

Never edit labels, gates or rules by hand.

- [ ] **Step 4: Real Geofabrik run (carry-over path)**

Run in the background `.venv/Scripts/python.exe -m speedmatrix.update run`. This downloads the newest PBF, the only network call; the download took about 30 minutes on 2026-10-21.

Expected:
- exit 0;
- `partition.json` `resplit` shows how many L2 cells changed;
- L1 and L2 counts unchanged.

Record the same numbers as Step 3, plus:
- the rule statuses (`moved`, `changed`, `new_orphans`) with rule ids;
- the orphan list;
- gate drift (`gate_drift_l1`, `gate_drift_l2`: moved and gone).

- [ ] **Step 5: No-op run**

Run `.venv/Scripts/python.exe -m speedmatrix.update run` again. Expected: exit 0, job `unchanged`, no new release dir. Only the `.md5` is fetched.

- [ ] **Step 6: Smoke tests**

Run `$env:SM_REAL_DATA = "1"; .venv/Scripts/python.exe -m unittest tests.test_sm_real_smoke tests.test_sm_osrmfiles -v`. Expected: `OK`.

- [ ] **Step 7: Write `reports/map_update_gates.md` in Vietnamese**

The report holds:
- the commands;
- the machine state: free disk and memory;
- a table per run with wall time and peak per step;
- the §16 numbers re-measured on the directed turn graph, as a table in the layout of spec §16:
  - cells per level;
  - gate arcs and via nodes per level;
  - entries and exits per L3 cell (median, p95, max) for core Hà Nội (within 10 km of 105.8522, 21.0287) and nationwide;
  - Σ entries × exits (the piece-size proxy for sub-project 2);
- every check with its value;
- §12.1 results;
- gate drift between the two runs;
- the rule report: orphans by rule, with their old coordinates;
- L3 re-split counts;
- the defects found and fixed;
- the decisions of this plan that the runs confirmed or contradicted;
- open items for the user:
  - registering the weekly task (Step 8);
  - region 6 (Decision 8);
  - the spec §17 questions, which are unchanged.

No TomTom-derived number appears in it.

- [ ] **Step 8: Ask before scheduling**

`update schedule` registers a Windows scheduled task, which is persistent system configuration. Ask the user. Run `.venv/Scripts/python.exe -m speedmatrix.update schedule` only after a yes, and record the answer in the report.

- [ ] **Step 9: Run the whole suite, commit and push**

```
git add tests/test_sm_real_smoke.py reports/map_update_gates.md
git commit -m "Map update and gates: nationwide runs and report" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin main
```
