# Vietnam buildings: footprints + heights

One row per building for all of Vietnam: OSM footprints first, Open Buildings (Google + Microsoft) footprints where OSM has none, cut back so that no building stands on a road or alley, and a height per building from Google Open Buildings 2.5D Temporal (2023).

Not used by the travel-time backend. Possible uses on the map side: snapping delivery points to a building, telling apartment towers from tube houses, and building density per area as an input for speed interpolation.

The OSM map itself can then be shipped without buildings (`strip_buildings.py`). The building layer replaces them.

## Run

Dependencies: `requirements-buildings.txt` (duckdb with the spatial extension, rasterio, shapely, pyproj, numpy, pyarrow), plus osmium from `requirements-spatial.txt`.

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-buildings.txt
.venv/Scripts/python.exe buildings/fetch_open_buildings.py     # 37 GeoParquet files, 8.69 GB -> data/open_buildings/vnm
.venv/Scripts/python.exe buildings/extract_osm.py              # PBF -> data/buildings/osm_{buildings,roads,water}.parquet
.venv/Scripts/python.exe buildings/merge_buildings.py          # -> vn_buildings_merged.parquet (+ .meta.json)
.venv/Scripts/python.exe buildings/clip_to_roads.py            # -> vn_buildings_clean.parquet (+ .meta.json)
.venv/Scripts/python.exe buildings/heights_25d.py              # -> vn_buildings.parquet (attributes and heights)
.venv/Scripts/python.exe buildings/compact_layer.py            # -> vn_buildings_compact.parquet (footprint + height, small)
.venv/Scripts/python.exe buildings/check_road_overlap.py --bbox 105.81 20.98 105.83 21.00   # both counts must be 0
.venv/Scripts/python.exe buildings/group_communes.py          # -> buildings/commune_groups.{json,md} (table of contents)
.venv/Scripts/python.exe buildings/split_by_area.py            # -> by_area/<province>_<NN>.parquet + index.json
.venv/Scripts/python.exe buildings/strip_buildings.py          # -> osrm/data/vietnam-nobuildings.osm.pbf
```

Every step can be re-run. The download resumes partial files. The merge works in 0.25 degree cells and skips cells already in `data/buildings/_parts/stats.json`; delete `_parts/` and `_merge.duckdb` to redo it, for example after a new PBF. `extract_osm.py` must be re-run whenever the PBF changes. Ids are assigned once by the merge; every later file keeps them.

## Merge rules (`merge_buildings.py`)

- Every OSM building (`building=*`, not `no`) is kept.
- An Open Buildings footprint is dropped when:
  - **low_conf:** Google confidence is below 0.7. Microsoft footprints have no confidence and are kept.
  - **osm_dup:** more than 20 % of its area is covered by OSM buildings.
  - **water:** more than 30 % of its area lies on OSM water areas.
  - **road:** more than 40 % of its area lies on an OSM carriageway. Bridges and tunnels are excluded.
- Carriageway half-width is lanes × 1.75 m when tagged. Otherwise it goes by class: motorway, trunk and primary 7 m; secondary 5 m; tertiary 4 m; residential, unclassified and living_street 2.5 m; service 1.5 m; links 3.5 m.
- Overlaps are measured against the union of the touching features, in UTM 48N.
- The thresholds and the outcome counts are written to `vn_buildings_merged.meta.json`.

## No building on a road or alley (`clip_to_roads.py`)

No house in Vietnam sticks out into a street or a ngõ. Footprints that do are errors: Open Buildings is often 1–2 m off, and the merge keeps footprints that are less than 40 % on a road. The rule applies to OSM and Open Buildings alike:

- footprint := footprint − union of the carriageways it touches; keep the largest remaining part.
- Drop the building when the remainder is under 50 % of the original or under 6 m² (it was mostly on the road).
- Carriageway half-widths are narrow on purpose, because OSM has no width for alleys: lanes × 1.5 m when tagged, else motorway/trunk/primary 5 m, secondary 4 m, tertiary 3 m, links 2.5 m, residential/unclassified/living_street 1.5 m, service 1.0 m, footway/path/pedestrian/steps/cycleway 0.75 m.
- Not cut by bridges, tunnels, `covered`, `indoor`, `area=yes`, or `highway=construction/proposed/platform/corridor/elevator/bus_stop`.
- The cut adds 0.1 m to each half-width, so storing coordinates at 1e-6 degrees later cannot push an edge back onto the road.
- `road_gap_m` records how far each footprint stays from the nearest carriageway, up to 5 m.
- Bùi Xương Trạch test (11,600 footprints): 3,239 cut, 90 dropped, median cut 3.9 m².
- Nationwide (October 2026): 55,201,810 footprints in, 4,269,462 cut, 50,216 dropped, median cut 5.2 m², 28 min. `check_road_overlap.py` then finds 0 footprints over a road, before and after compression.

## Heights (`heights_25d.py`)

- Source: `gs://open-buildings-temporal-data` (public, no account), band `building_height`, year 2023, licence CC BY 4.0 or ODbL.
- Each tile is read at 1 m through the file's built-in 2× overview: height band only, about 1 km windows, only where buildings are. Nothing is stored except the result.
- On Bùi Xương Trạch (9,692 buildings), 1 m reads stay within 1 m of the full 0.5 m read for 98.6 % of buildings, and within 0.5 m for 93.2 %. At 2 m the figure is 93.4 % within 1 m.
- Height of a building is the median of the pixels inside its (clipped) footprint. A footprint smaller than a pixel takes the pixel under its centroid. 2.5D values under 2 m are treated as "no building".
- Tiles of neighbouring S2 cells overlap, and one of the two is nodata there. A building keeps the first valid value.
- The nationwide run takes hours. Each finished tile is saved to `data/buildings/_25d_done/`. After an interruption (Windows Update restarted the machine during the first run), re-running resumes from those files. Delete `_25d_done/` when the footprints change.
- Floors: the OSM `building:levels` tag when present. Otherwise `1 + round((height − 3.9) / 3.3)`, at least 1: ground floor 3.9 m, upper floors 3.3 m.
- The 2.5D height is a model estimate from Sentinel-2, effective resolution 4 m. Against the three OSM-tagged buildings in Bùi Xương Trạch, it matched one (7 floors, 25.5 m) and missed two. It tends to under-read narrow tube houses.

## Output `data/buildings/vn_buildings.parquet`

| column | type | meaning |
|---|---|---|
| id | uint32 | row key, same as `vn_buildings_clean.parquet` (which keeps the geometry) |
| src | text | `osm`, `google`, `microsoft` |
| lon_e6, lat_e6 | int32 | centroid in 1e-6 degrees (about 0.1 m) |
| area_m2 | float32 | footprint area |
| conf_pct | uint8 | Google confidence × 100 |
| h25_dm | uint16 | 2.5D median height, decimetres |
| osm_h_dm | uint16 | OSM `height`, or `building:levels` × 3.3 m, decimetres |
| height_dm, height_src | uint16, text | chosen height: OSM tag first, then 2.5D (`osm` / `g25`) |
| floors | uint8 | see above |
| osm_id, name, addr | text | OSM only, mostly empty |

## Compact layer `data/buildings/vn_buildings_compact.parquet` (`compact_layer.py`)

Footprint plus height in about 19 bytes per building (Bùi Xương Trạch test: 18.9 B), zstd level 19. `decode()` in `compact_layer.py` turns a row back into a polygon.

- **Rectangle**, when the footprint fills ≥ 95 % of its rotated bounding rectangle, has no hole, and the rectangle cannot reach a road: its largest bulge past the footprint plus 0.15 m is less than `road_gap_m`. Stored as centre (`lon_e6`, `lat_e6`), `length_dm`, `width_dm` and `angle_cdeg` (long side, clockwise from north). Round-trip IoU median 0.97.
- **Ring** otherwise, including every footprint that was cut: first vertex in `lon_e6`/`lat_e6`, then int16 (dx, dy) steps in 1e-6 degrees. Holes are dropped. Round-trip IoU median 0.99.
- Plus `id`, `src`, `height_dm`, `height_src`, `floors`.

## Download files by area (`group_communes.py`, `split_by_area.py`)

A client downloads and opens only the area it needs: `data/buildings/by_area/<province>_<NN>.parquet`, same schema as the compact layer, plus `by_area/index.json`.

- **Boundaries** come from OSM, two-level system since July 2025: 34 provinces (`admin_level=4`, `ISO3166-2` VN-*) and 3,318 communes (`admin_level=6` named Xã / Phường / Đặc khu), assembled once into `data/buildings/vn_admin.parquet`. The extract cuts off part of the offshore boundary of Trường Sa, so osmium cannot close Khánh Hòa or Trường Sa; for any unit it cannot assemble, the script polygonizes the member ways present and keeps the closed faces (Khánh Hòa: 46 of 61 ways, mainland and islands kept).
- **Groups** (`group_communes.py`): neighbouring units of one province and one kind, up to 3 urban wards (Phường) or up to 10 communes (Xã, Đặc khu) per file, at most about 15 MB. Small groups merge into a neighbour of the same kind. Result: 551 files, median 1.5 MB, largest about 9.4 MB, about 1 GB in total (estimates).
- **Table of contents**, committed: [`commune_groups.md`](commune_groups.md) (readable, per province) and `commune_groups.json` (machine-readable). Grouping depends only on boundaries and rough counts, so it does not change when footprints are refined.
- **Assignment** (`split_by_area.py`): a building goes to the commune that contains its centroid, so each building is in exactly one file and keeps its id. Centroids outside every commune go to the nearest one (nationwide: 64,432, 0.12 %).
- `by_area/index.json` lists per file its province, communes with exact building counts, bytes and lon/lat bbox. A client picks files by bbox or by commune name.
- Bùi Xương Trạch test: 11,510 buildings in 3 files (`ha_noi_15` Khương Đình, `ha_noi_16` Phương Liệt, `ha_noi_19` Định Công), totals match the compact layer.

## OSM without buildings (`strip_buildings.py`)

Writes `osrm/data/vietnam-nobuildings.osm.pbf`: drops `building` / `building:part` ways and relations, their untagged member ways, and untagged nodes used only by dropped ways. For the October 2026 extract: 331 MB → 297 MB. Building ways are about 10 % of the file, so the map does not get much lighter; the building layer is where the volume is. OSRM keeps reading `vietnam-latest.osm.pbf`.
