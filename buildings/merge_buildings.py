"""Merge OSM and Open Buildings footprints for Vietnam, cell by cell.

Rules (same as the Tiny Map description, thresholds fixed here):
- Every OSM building is kept and takes priority.
- An Open Buildings footprint is dropped when
  low_conf: Google confidence < 0.7 (Microsoft has no confidence and is kept),
  osm_dup:  > 20 % of its area is covered by OSM buildings,
  water:    > 30 % of its area lies on OSM water areas,
  road:     > 40 % of its area lies on an OSM carriageway (bridges and tunnels excluded).
  Carriageway half-width: lanes x 1.75 m when tagged, else by class
  (motorway/trunk/primary 7, secondary 5, tertiary 4, residential/unclassified/living_street 2.5, service 1.5, links 3.5).
Overlap fractions use the union of the overlapping features, so crossings are not counted twice.

Work is split into 0.25 degree cells; a footprint belongs to the cell holding its bbox centre.
Each cell is written to data/buildings/_parts/<cell>.parquet, so an interrupted run resumes where it stopped.
At the end the parts and the OSM buildings are assembled into data/buildings/vn_buildings_merged.parquet.

    .venv/Scripts/python.exe buildings/merge_buildings.py [--ob data/open_buildings/vnm] [--out data/buildings]
"""
import argparse, json, math, os, time
import duckdb

CELL = 0.25
MARGIN = 0.002                                   # degrees; features straddling a cell edge
UTM = "'EPSG:4326', 'EPSG:32648', always_xy := true"
RULES = {'low_conf': 0.7, 'osm_dup': 0.2, 'water': 0.3, 'road': 0.4}
HALF_WIDTH = """coalesce(try_cast(lanes AS DOUBLE) * 1.75, CASE
    WHEN hw IN ('motorway','trunk','primary') THEN 7 WHEN hw = 'secondary' THEN 5 WHEN hw = 'tertiary' THEN 4
    WHEN hw IN ('residential','unclassified','living_street') THEN 2.5 WHEN hw = 'service' THEN 1.5
    WHEN hw LIKE '%_link' THEN 3.5 END)"""


def connect(out):
    con = duckdb.connect(os.path.join(out, '_merge.duckdb'))
    con.execute("INSTALL spatial; LOAD spatial; SET enable_progress_bar = false;")
    con.execute(f"SET temp_directory = '{os.path.join(out, '_tmp').replace(chr(92), '/')}'; SET memory_limit = '20GB';")
    return con


def prepare_osm(con, out):
    """Load OSM inputs once, in metres (UTM 48N), with lon/lat bboxes for cell filtering."""
    have = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    p = lambda n: os.path.join(out, n).replace('\\', '/')
    if 'osm_b' not in have:
        con.execute(f"""CREATE TABLE osm_b AS SELECT osm_id, ST_XMin(geometry) x0, ST_YMin(geometry) y0, ST_XMax(geometry) x1, ST_YMax(geometry) y1,
                        ST_Transform(geometry, {UTM}) g FROM '{p('osm_buildings.parquet')}'""")
    if 'osm_r' not in have:
        con.execute(f"""CREATE TABLE osm_r AS SELECT ST_XMin(geometry) x0, ST_YMin(geometry) y0, ST_XMax(geometry) x1, ST_YMax(geometry) y1,
                        ST_Buffer(ST_Transform(geometry, {UTM}), {HALF_WIDTH}) g FROM '{p('osm_roads.parquet')}'
                        WHERE {HALF_WIDTH} IS NOT NULL AND coalesce(bridge, 'no') = 'no' AND coalesce(tunnel, 'no') = 'no'""")
    if 'osm_w' not in have:
        con.execute(f"""CREATE TABLE osm_w AS SELECT ST_XMin(geometry) x0, ST_YMin(geometry) y0, ST_XMax(geometry) x1, ST_YMax(geometry) y1,
                        ST_MakeValid(ST_Transform(geometry, {UTM})) g FROM '{p('osm_water.parquet')}'""")
    for t in ('osm_b', 'osm_r', 'osm_w'):
        print(t, con.execute(f"SELECT count(*) FROM {t}").fetchone()[0], flush=True)


def frac(con, other, label):
    """Share of each candidate footprint covered by the union of the `other` features it touches."""
    con.execute(f"""CREATE OR REPLACE TEMP TABLE f_{label} AS
        SELECT c.rid, ST_Area(ST_Union_Agg(ST_Intersection(c.g, o.g))) / any_value(c.a) f
        FROM cand c JOIN {other} o ON ST_Intersects(c.g, o.g) GROUP BY c.rid""")


def do_cell(con, src, x, y, part):
    x0, y0, x1, y1 = x, y, x + CELL, y + CELL
    near = f"x1 >= {x0 - MARGIN} AND x0 <= {x1 + MARGIN} AND y1 >= {y0 - MARGIN} AND y0 <= {y1 + MARGIN}"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ob AS
        SELECT row_number() OVER () rid, bf_source src, confidence conf, geometry
        FROM read_parquet('{src}')
        WHERE bbox.xmin < {x1} AND bbox.xmax >= {x0} AND bbox.ymin < {y1} AND bbox.ymax >= {y0}  -- lets row-group stats skip data
          AND (bbox.xmin + bbox.xmax) / 2 >= {x0} AND (bbox.xmin + bbox.xmax) / 2 < {x1}
          AND (bbox.ymin + bbox.ymax) / 2 >= {y0} AND (bbox.ymin + bbox.ymax) / 2 < {y1}""")
    n = con.execute("SELECT count(*) FROM ob").fetchone()[0]
    if n == 0:
        return None
    con.execute(f"""CREATE OR REPLACE TEMP TABLE cand AS SELECT rid, g, ST_Area(g) a FROM
        (SELECT rid, ST_MakeValid(ST_Transform(geometry, {UTM})) g FROM ob WHERE conf IS NULL OR conf >= {RULES['low_conf']}) WHERE ST_Area(g) > 0""")
    for t, lab in (('osm_b', 'osm'), ('osm_r', 'road'), ('osm_w', 'water')):
        con.execute(f"CREATE OR REPLACE TEMP TABLE near_{lab} AS SELECT g FROM {t} WHERE {near}")
        frac(con, f'near_{lab}', lab)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE res AS SELECT ob.*, CASE
          WHEN ob.conf IS NOT NULL AND ob.conf < {RULES['low_conf']} THEN 'low_conf'
          WHEN cand.rid IS NULL THEN 'invalid'
          WHEN coalesce(fo.f, 0) > {RULES['osm_dup']} THEN 'osm_dup'
          WHEN coalesce(fw.f, 0) > {RULES['water']} THEN 'water'
          WHEN coalesce(fr.f, 0) > {RULES['road']} THEN 'road' END reason, cand.a area_m2
        FROM ob LEFT JOIN cand USING (rid) LEFT JOIN f_osm fo USING (rid) LEFT JOIN f_water fw USING (rid) LEFT JOIN f_road fr USING (rid)""")
    stats = dict(con.execute("SELECT coalesce(reason, 'kept'), count(*) FROM res GROUP BY 1").fetchall())
    con.execute(f"""COPY (SELECT src, conf, area_m2, geometry FROM res WHERE reason IS NULL)
                    TO '{part}' (FORMAT parquet, COMPRESSION zstd)""")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ob', default='data/open_buildings/vnm')
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--cells', nargs='*', help='only these cells, e.g. 105.75_20.75 (testing; nothing is assembled)')
    ap.add_argument('--prepare-only', action='store_true', help='load the OSM inputs into the work database and stop')
    a = ap.parse_args()
    parts = os.path.join(a.out, '_parts'); os.makedirs(parts, exist_ok=True)
    t = time.time()
    con = connect(a.out)
    prepare_osm(con, a.out)
    if a.prepare_only:
        return
    src = os.path.join(a.ob, '*.parquet').replace('\\', '/')
    # One pass over the bbox columns finds the cells that hold footprints, so empty cells cost nothing.
    occ = con.execute(f"""SELECT floor((bbox.xmin + bbox.xmax) / 2 / {CELL}) i, floor((bbox.ymin + bbox.ymax) / 2 / {CELL}) j, count(*)
                          FROM read_parquet('{src}') GROUP BY 1, 2 ORDER BY 2, 1""").fetchall()
    print('Open Buildings', sum(r[2] for r in occ), 'footprints in', len(occ), 'cells', flush=True)
    stats_path = os.path.join(a.out, '_parts', 'stats.json')
    stats = json.load(open(stats_path)) if os.path.exists(stats_path) else {}
    stats = {k: v for k, v in stats.items() if v}      # earlier runs may have recorded empty cells
    cells = [(i * CELL, j * CELL) for i, j, _ in occ]
    if a.cells:
        cells = [tuple(map(float, c.split('_'))) for c in a.cells]
    for k, (x, y) in enumerate(cells):
        key = f"{x:.2f}_{y:.2f}"
        if key in stats and not a.cells:
            continue
        tc = time.time()
        s = do_cell(con, src, x, y, os.path.join(parts, key + '.parquet').replace('\\', '/'))
        stats[key] = s or {}
        json.dump(stats, open(stats_path, 'w'))
        if s:
            print(f"[{k + 1}/{len(cells)}] {key} {s} {time.time() - tc:.1f}s total {time.time() - t:.0f}s", flush=True)
    if a.cells:
        return
    tot = {}
    for s in stats.values():
        for r, c in s.items():
            tot[r] = tot.get(r, 0) + c
    print('Open Buildings outcome', tot, flush=True)
    osm = os.path.join(a.out, 'osm_buildings.parquet').replace('\\', '/')
    kept = os.path.join(parts, '*.parquet').replace('\\', '/')
    dst = os.path.join(a.out, 'vn_buildings_merged.parquet').replace('\\', '/')
    # id is assigned once here and stored, so later steps never depend on read order.
    # OSM comes tagged OGC:CRS84 and Open Buildings EPSG:4326 (same axes in practice); drop the tag so they union.
    plain = "ST_GeomFromWKB(ST_AsWKB(geometry))"
    con.execute(f"""CREATE OR REPLACE TABLE merged AS
        SELECT 'osm' src, osm_id, NULL::DOUBLE conf, ST_Area(ST_Transform(geometry, {UTM})) area_m2,
               building_levels, height, name, addr_housenumber, addr_street, {plain} geometry FROM '{osm}'
        UNION ALL
        SELECT src, NULL, conf, area_m2, NULL, NULL, NULL, NULL, NULL, {plain} FROM read_parquet('{kept}')""")
    con.execute(f"""COPY (SELECT (row_number() OVER (ORDER BY ST_Y(ST_Centroid(geometry)) // 0.1, ST_X(ST_Centroid(geometry)) // 0.1) - 1)::UINTEGER id, *
                    FROM merged ORDER BY id) TO '{dst}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)""")
    con.execute("DROP TABLE merged")
    n = con.execute(f"SELECT src, count(*) FROM '{dst}' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    meta = {'rules': RULES, 'half_width_sql': ' '.join(HALF_WIDTH.split()), 'cell_deg': CELL, 'outcome': tot, 'rows_by_source': dict(n),
            'sources': {'osm': 'osrm/data/vietnam-latest.osm.pbf', 'open_buildings': 'data.source.coop/vida/google-microsoft-open-buildings (VNM)'}}
    json.dump(meta, open(os.path.join(a.out, 'vn_buildings_merged.meta.json'), 'w'), indent=1)
    print('wrote', dst, n, round(time.time() - t), 's')


if __name__ == '__main__':
    main()
