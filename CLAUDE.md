# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Hanoi travel-time estimator: OSRM routes a trip, then travel time is computed from TomTom-derived speed samples interpolated per road class, weekday and time slot. Local Windows app: FastAPI backend + React/Leaflet frontend. User-facing strings (API errors, UI) are Vietnamese; new code and docstrings are English. Git repository, published publicly on GitHub (mrdo19111911/hanoi-traffic-model): TomTom-derived data (`data/*.json*`, `reports/spatial_crawl.json`) and large generated data are gitignored and must stay out of it.

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

## Conventions

- Day: 0 = Monday … 6 = Sunday. Slot: `HHMM` string. Hour: decimal (8.5 = 08:30). Times are local UTC+7 (`+07:00` in TomTom `departAt`).
- Generated and large paths are gitignored: `osrm/tools|data|downloads|logs`, `osrm/build.json`, `osrm/server.json`, `runtime/`, `data/api_raw/`.
- TomTom free tier is limited (README: ~1600/20000 calls used, Oct 2026); crawls are capped at 60 calls per job.
