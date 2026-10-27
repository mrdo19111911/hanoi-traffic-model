# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Hanoi travel-time estimator: OSRM routes a trip, then travel time is computed from TomTom-derived speed samples interpolated per road class, weekday and time slot. Local Windows app: FastAPI backend + React/Leaflet frontend. User-facing strings (API errors, UI) are Vietnamese; new code and docstrings are English. Git repository, published publicly on GitHub (mrdo19111911/hanoi-traffic-model): TomTom-derived data (`data/*.json*`, `reports/spatial_crawl.json`) is gitignored and must stay out of it. Intermediate generated data (`data/buildings/`, `data/open_buildings/`, OSRM files) stays gitignored too. The published building layer is committed: one file per commune group in `buildings/by_area/`, each kept under GitHub's 100 MB file limit.

## Commands

All Python runs from the repo root with the project venv (`.venv/Scripts/python.exe`, Python 3.14). Dependencies: `requirements-api.txt` (fastapi, uvicorn, httpx) and `requirements-spatial.txt` (numpy, scipy, osmium).

```powershell
python app.py setup     # venv + pip, OSRM setup, road index build (if PBF changed), npm ci + build
python app.py start     # starts OSRM (port 5000) then web server (port 8000), detached; logs in runtime/web.log
python app.py status | stop
python app.py serve     # foreground uvicorn backend.app:app on 127.0.0.1:8000

python osrm/manage.py setup|start|stop|status [--threads N] [--port P] [--rebuild]

# Python tests (unittest, offline: no OSRM, network or API key needed)
.venv/Scripts/python.exe -m unittest discover -s tests
.venv/Scripts/python.exe -m unittest tests.test_backend.BackendTests.test_health_never_exposes_key

# Frontend (in frontend/); dev server proxies /api to 127.0.0.1:8000
npm run dev
npm run build           # tsc -b && vite build -> frontend/dist (served by the backend)
npm test                # vitest run (jsdom)
npx vitest run src/App.test.tsx -t "<test name>"

# Spatial data pipeline
.venv/Scripts/python.exe model/build_road_index.py                       # PBF -> osrm/data/road_classes.sqlite
.venv/Scripts/python.exe download/crawl_spatial.py --date 2026-10-20 --slots 0800 1730 --max-calls 12
.venv/Scripts/python.exe model/compare_spatial.py                        # -> reports/spatial_comparison.json
```

`app.py` and `osrm/manage.py` are Windows-only (Win32 process identity checks, DETACHED_PROCESS). OSRM and the web server bind to 127.0.0.1 only; `OSRM_BASE_URL` overrides the OSRM address.

## Architecture

Two generations of model coexist. Know which one you are touching.

**Legacy (README examples, CLI only):** `model/estimate.py`, `congestion.py`, `classify5.py`, `build_zone_factors.py`, `estimate_tomtom.py`, `download/download_day.py` + `crawl_segments.py`. Classifies roads by street-name keywords, uses an 8x8 zone grid (`data/zone_factors.json`) and the `data/segments_*.jsonl` crawls. Not used by the backend except as a fallback sample source.

**Spatial (current; what the backend uses):**

1. `model/osm_classes.py` — the single OSM-tag policy mapping ways to the five classes `caotoc / quoclo / chinh / noido / nho` (`POLICY_VERSION`).
2. `model/build_road_index.py` — reads the Vietnam PBF with osmium and writes `osrm/data/road_classes.sqlite`: table `edges(a, b, way_id, road_class, ...)` keyed by adjacent OSM node pairs, plus a `metadata` row containing `pbf_sha256`. That hash must equal `osrm/build.json` `input.pbf_sha256`; the crawler and estimator refuse to run on a mismatch, so rebuild the index whenever the OSRM map is rebuilt.
3. `download/crawl_spatial.py` — fetches TomTom routes (snapshots cached in `data/api_raw/<Rxx>_<YYYYMMDD>_<HHMM>.json`, key redacted, never refetched), map-matches each instruction interval through OSRM `/match`, looks up the node pairs in the index, and rebuilds `data/spatial_samples.jsonl` from all cached snapshots. An interval whose edges are mixed, unknown or ambiguous classes is excluded, not averaged. Routes come from `download/download_day.py` `ROUTES`.
4. `model/spatial.py` — `SpeedField` interpolation (mean, nearest, IDW1/2, Delaunay linear + IDW fallback) on a local metric projection around Hanoi, grouped by `(road_class, day, slot)`.
5. `model/estimate_spatial.py` — `SpatialEstimator`: OSRM `/route` with `annotations=nodes`, every positive-length edge must resolve to exactly one class in the index, then each edge gets the speed field of its own class. Raises `MissingRoadData`, `RoadClassError`, `RouteAlignmentError`, `SpatialDataError` (all `ValueError` subclasses). If `data/spatial_samples.jsonl` is absent (current state of `data/`), it falls back to `compare_spatial.legacy_samples()` with an "exploratory" warning.
6. `model/compare_spatial.py` — held-out comparison of interpolation methods, written to `reports/spatial_comparison.json`.

**Core invariant across the spatial code:** speeds are never borrowed across road classes, and missing data raises instead of silently substituting. Preserve this when changing interpolation, crawling or estimation.

**Backend (`backend/app.py`):** `create_app(root=, estimator_factory=, osrm_probe=, key_reader=, job_runner=)` takes injectable dependencies; tests use a temp root and fakes. `/api/estimate` maps estimator exception *class names* to Vietnamese 422 messages, so renaming those exceptions breaks the mapping. Crawl and experiment jobs run `crawl_spatial.py` / `compare_spatial.py` as subprocesses through an in-memory `JobManager` (one job per kind at a time; state lost on restart, hence the single uvicorn worker). Non-`/api` paths serve `frontend/dist` with SPA fallback.

**Credential handling:** `TOMTOM_API_KEY` is read from the process env, then the Windows `HKCU\Environment` registry value. It must never appear in responses, job errors, logs or cached snapshots; error paths deliberately swallow exception text because urllib messages contain the keyed URL. Tests assert this.

**Import layout:** `model/` and `download/` have no `__init__.py`. Scripts in `model/` import siblings flatly (`from spatial import ...`) and are run as scripts; Modules the backend reaches (`estimate_spatial.py` and, through the legacy fallback, `compare_spatial.py`) try a relative import first and fall back to the flat one, so they work both as `model.<name>` and as scripts; keep that pattern for any `model/` module they start importing. `PackageImportTests` checks this in a fresh interpreter. `crawl_spatial.py` adds the repo root to `sys.path`. `tests/test_estimate_spatial.py` puts `model/` on `sys.path`.

## Big-road map (network A)

National review map of the big roads, separate from the five-class travel-time model (spec `docs/superpowers/specs/2026-10-19-big-road-map-design.md`). `model/build_big_roads.py` (PBF -> `osrm/data/big_roads*.geojson`, `big_road_edges.sqlite`, `vietnam_land/boundary.geojson`; free-flow speeds as the local OSRM car profile drives) and `model/build_provinces.py` write files stamped with `pbf_sha256` and `BIG_ROAD_POLICY_VERSION` (from `osm_classes.py`); `app.py setup` rebuilds them when either is stale. `model/natural_barriers.py` writes `reports/natural_barriers.json` and `osrm/data/main_rivers.geojson`, then `model/clusters.py` writes `big_road_clusters.json` with only the `province` and `natural` partitions (`app.py` treats any other content as outdated). Backend serves them under `/api/big-roads/*`; UI in `frontend/src/BigRoadPanels.tsx` + `bigRoads.ts`. `gate_matrix_experiment.py` and `osrm_mld_boundary.py` are measurement-only experiments writing to `reports/`.

## Buildings layer (`buildings/`)

Nationwide footprints + heights, not used by the backend. Full pipeline and rules in `buildings/README.md`; deps in `requirements-buildings.txt` (duckdb spatial, rasterio, shapely, pyproj, pyarrow). Key points:

- Invariants every published file must pass (`check_road_overlap.py`): 0 buildings on a carriageway or alley, 0 overlaps, file under 95 MB. Fixes are code rules (`fit_footprints.py` shift/cut/shared walls), never hand edits or deletions.
- `buildings/build_areas.py` builds area by area (one area = one entry of `commune_groups.json`) on 60 % of the CPUs and **commits and pushes each finished `by_area/*.parquet` itself**; use `--no-push`/`--no-git` and `--bbox ... --dst-dir <tmp>` for trial runs.
- Building ids are assigned once by `merge_buildings.py`; later steps keep them. Heights: Google 2.5D calibrated (`calibrate_heights.py` -> `height_calibration.json`), OSM `building:part` (`part_heights.py`), Wikidata/sibling towers (`extra_heights.py` -> `height_sources.json`), front-row rule (`frontrow_heights.py`).
- The frontend reads published files straight from `raw.githubusercontent.com/.../buildings/by_area/` at zoom 16+ (`frontend/src/buildings.ts`, browser cache). Its decoder mirrors `compact_layer.py` `SCHEMA`/`decode`; after changing the encoding regenerate the cross-check fixture with `frontend/src/test/buildings_fixture.py [--keep-ids]`.

## Demo pages server

`python tools/nav_server.py --port 8767 --directory PATH` serves the 3D/tree/house demo pages (they live outside this repo) and injects a page menu grouped by the filename rules in `GROUPS`; files on disk are never modified.

## Speed matrix (`speedmatrix/`)

Nationwide speed layer, vehicle-group views and the gate partition for the VRP matrix (spec `docs/superpowers/specs/2026-10-21-speed-matrix-system-design.md`). Outputs live under the gitignored `runtime/matrix/` (the default `--root`). Modules import relatively first, then flat; `build.py` and `update.py` put the repo root on `sys.path`.

```powershell
.venv/Scripts/python.exe -m speedmatrix.build import-rules FILE...          # seed rules, vehicle groups, holidays -> state.sqlite
.venv/Scripts/python.exe -m speedmatrix.build segments --pbf PBF | speeds --segments DIR | groups --segments DIR --speeds DIR [--out DIR] | rules-check --segments DIR | status
.venv/Scripts/python.exe -m speedmatrix.update run [--pbf PATH] [--force] [--seed FILE] [--min-free-gb GB]   # weekly release pipeline
.venv/Scripts/python.exe -m speedmatrix.update serve [--port 5059] | stop | status | schedule [--day SUN] [--time 03:00] | unschedule
```

`speedmatrix.update` replaces `reports/gate_compose_spike/update_matrix.py`. Release layout under `runtime/matrix/`: `releases/<YYYYMMDD-HHMMSS>/` (`release.json`, `osrm/`, `graph/`, `labels_l*.npy`, `node_labels.npz`, `gates.npz`, `partition.json`, `rules.json`, `groups/`, `verify.json`, `logs/`), shared `segments/<sha>/` and `speeds/<version>/`, plus `current.json` (moved only by a promotion), `state.sqlite` (rules, groups, `jobs`), `server.json`, `update.lock`, `logs/`. Ports: 5058 build/verify, 5059 served matrix; 5000 and 8000 are refused. The only network call is the Geofabrik PBF and its `.md5`.

- Decision 6: L1/L2 labels are frozen and carried over by OSM node id (v3.2 seed the first time); only new nodes are labelled, by the v3.2 polygons (L1) and network propagation (L2).
- Decision 16: blocking checks (§6.6, §12.1, graph identities, rule errors, orphans of active `cao`/`tb` rules) stop the run at that step with `failed_checks` (exit 1) and no promotion; the rest are warnings.
- Decision 24: the scheduled task `HanoiTrafficMatrixUpdate` and lock `runtime/matrix/update.lock` are the spike's; registering the task (`update schedule`) needs the user's explicit yes.

## Conventions

- Day: 0 = Monday … 6 = Sunday. Slot: `HHMM` string. Hour: decimal (8.5 = 08:30). Times are local UTC+7 (`+07:00` in TomTom `departAt`).
- Generated and large paths are gitignored: `osrm/tools|data|downloads|logs`, `osrm/build.json`, `osrm/server.json`, `runtime/`, `data/api_raw/`.
- **Code, never an LLM (absolute rule):** anything that can be done by code (counting, measuring, checks, tests, tables, matching, accept/reject decisions, data conversion, running and monitoring jobs) is done by a script. Never by an LLM or subagent, not even "to be quick". Subagents are only for work that truly needs judgement, and a data decision is always a re-runnable code rule, never per-item LLM judgement.
- **Heavy jobs run on the 3090 PC, not locally:** every job that costs a lot of CPU, GPU or data (builds, training, bulk downloads, tile fetches, dataset pulls) runs on DESKTOP-L6QJ2RK (`ssh Mrdo@100.123.93.72`, Tailscale; work dirs `G:\vn_dl`, `G:\esri_z19`, `G:\stage`). The local PC is for light edits and checks only. A job already running locally finishes there; new ones go to the 3090. Esri/TomTom data may be copied to it but never goes to GitHub.
- TomTom free tier is limited (README: ~1600/20000 calls used, Oct 2026); crawls are capped at 60 calls per job.
