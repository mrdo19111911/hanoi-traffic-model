"""Cut every footprint back from roads and alleys: in Vietnam no building stands on a carriageway or a ngõ.

Overlaps come from footprint offsets (Open Buildings is often 1-2 m off), from OSM drawing, and from the
merge keeping footprints that are less than 40 % on a road. Rule, applied to OSM and Open Buildings alike:
  footprint := footprint - union(carriageway polygons it touches), keep the largest remaining part;
  drop the building when what remains is < 50 % of the original or < 6 m2 (it was mostly on the road).
Carriageway half-widths are deliberately narrow, because OSM gives no width for alleys:
  lanes x 1.5 m when tagged, else motorway/trunk/primary 5, secondary 4, tertiary 3, links 2.5,
  residential/unclassified/living_street 1.5, service 1.0, footway/path/pedestrian/steps/cycleway 0.75.
Not cut by: bridges, tunnels, covered or indoor ways, area=yes (squares drawn as outlines),
and highway=construction/proposed/platform/corridor/elevator/bus_stop.
The cut uses half-width + 0.1 m, so storing coordinates at 1e-6 degrees (about 5 cm) cannot push an edge back
onto the carriageway. road_gap_m records how far each footprint stays from the nearest carriageway (up to 5 m),
which lets compact_layer.py use a rectangle only where the rectangle cannot reach the road either.

Input  data/buildings/vn_buildings_merged.parquet (ids from merge_buildings.py), osm_roads.parquet.
Output data/buildings/vn_buildings_clean.parquet: same columns and ids, clipped geometry, dropped rows removed,
plus clip_m2 (area cut off) and road_gap_m (metres to the nearest carriageway after the cut, NULL when over 5 m).
Summary in vn_buildings_clean.meta.json.

    .venv/Scripts/python.exe buildings/clip_to_roads.py [--out data/buildings] [--bbox W S E N]
"""
import argparse, json, os, time
import duckdb

CELL = 0.25
MARGIN = 0.002
UTM = "'EPSG:4326', 'EPSG:32648', always_xy := true"
BACK = "'EPSG:32648', 'EPSG:4326', always_xy := true"
KEEP_SHARE, MIN_M2 = 0.5, 6.0
ROUND_M = 0.1                                    # margin for coordinate rounding downstream
GAP_M = 5.0                                      # road_gap_m is recorded up to this distance
HALF = """coalesce(try_cast(lanes AS DOUBLE) * 1.5, CASE
    WHEN hw IN ('motorway','trunk','primary') THEN 5 WHEN hw = 'secondary' THEN 4 WHEN hw = 'tertiary' THEN 3
    WHEN hw LIKE '%_link' THEN 2.5 WHEN hw IN ('residential','unclassified','living_street') THEN 1.5
    WHEN hw = 'service' THEN 1.0 WHEN hw IN ('footway','path','pedestrian','steps','cycleway') THEN 0.75 END)"""
SKIP = """coalesce(bridge, 'no') = 'no' AND coalesce(tunnel, 'no') = 'no' AND coalesce(covered, 'no') = 'no'
    AND coalesce(indoor, 'no') = 'no' AND coalesce(area, 'no') = 'no'
    AND hw NOT IN ('construction','proposed','platform','corridor','elevator','bus_stop')"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--bbox', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), help='limit to a lon/lat box (testing)')
    ap.add_argument('--dst', default='vn_buildings_clean.parquet')
    a = ap.parse_args()
    t0 = time.time()
    p = lambda n: os.path.join(a.out, n).replace('\\', '/')
    con = duckdb.connect(p('_clip.duckdb'))
    con.execute(f"INSTALL spatial; LOAD spatial; SET enable_progress_bar = false; SET memory_limit = '20GB'; SET temp_directory = '{p('_tmp')}';")
    where = f"WHERE cx BETWEEN {a.bbox[0]} AND {a.bbox[2]} AND cy BETWEEN {a.bbox[1]} AND {a.bbox[3]}" if a.bbox else ''
    con.execute(f"""CREATE OR REPLACE TABLE b AS SELECT * FROM (
        SELECT id, ST_X(ST_Centroid(geometry)) cx, ST_Y(ST_Centroid(geometry)) cy, ST_XMin(geometry) x0, ST_YMin(geometry) y0,
               ST_XMax(geometry) x1, ST_YMax(geometry) y1, geometry FROM '{p('vn_buildings_merged.parquet')}') {where}
        ORDER BY floor(cy / {CELL}), floor(cx / {CELL})""")
    con.execute(f"""CREATE OR REPLACE TABLE r AS SELECT ST_XMin(geometry) x0, ST_YMin(geometry) y0, ST_XMax(geometry) x1, ST_YMax(geometry) y1,
        ST_Buffer(ST_Transform(geometry, {UTM}), {HALF} + {ROUND_M}) g FROM '{p('osm_roads.parquet')}' WHERE {HALF} IS NOT NULL AND {SKIP}""")
    cells = con.execute(f"SELECT DISTINCT floor(cx / {CELL}) * {CELL}, floor(cy / {CELL}) * {CELL} FROM b ORDER BY 2, 1").fetchall()
    print('buildings', con.execute("SELECT count(*) FROM b").fetchone()[0], '| road polygons', con.execute("SELECT count(*) FROM r").fetchone()[0],
          '| cells', len(cells), f'| {time.time() - t0:.0f}s', flush=True)
    # fix holds every footprint within GAP_M of a carriageway: cut ones (gap 0) get g = the largest remaining part.
    con.execute("CREATE OR REPLACE TABLE fix (id UINTEGER, g GEOMETRY, a0 DOUBLE, a1 DOUBLE, gap DOUBLE)")
    for k, (x, y) in enumerate(cells):
        tc = time.time()
        con.execute(f"""CREATE OR REPLACE TEMP TABLE cb AS SELECT id, ST_MakeValid(ST_Transform(geometry, {UTM})) g FROM b
                        WHERE cx >= {x} AND cx < {x + CELL} AND cy >= {y} AND cy < {y + CELL}""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE cr AS SELECT g FROM r
                        WHERE x1 >= {x - MARGIN} AND x0 <= {x + CELL + MARGIN} AND y1 >= {y - MARGIN} AND y0 <= {y + CELL + MARGIN}""")
        con.execute(f"""CREATE OR REPLACE TEMP TABLE pr AS SELECT cb.id, cb.g, cr.g rg, ST_Distance(cb.g, cr.g) dist
                        FROM cb JOIN cr ON ST_DWithin(cb.g, cr.g, {GAP_M})""")
        con.execute("""INSERT INTO fix
            SELECT id, g1, a0, CASE WHEN g1 IS NULL THEN a0 ELSE ST_Area(g1) END, gap FROM (
                SELECT id, a0, gap, CASE WHEN d IS NULL THEN NULL WHEN ST_IsEmpty(d) THEN d
                       ELSE (SELECT g FROM (SELECT unnest(ST_Dump(d)).geom g) ORDER BY ST_Area(g) DESC LIMIT 1) END g1
                FROM (SELECT id, any_value(ST_Area(g)) a0, min(dist) gap,
                             CASE WHEN min(dist) = 0 THEN ST_Difference(any_value(g), ST_Union_Agg(rg) FILTER (WHERE dist = 0)) END d
                      FROM pr GROUP BY id))""")
        if (k + 1) % 25 == 0 or k + 1 == len(cells):
            n = con.execute("SELECT count(*) FILTER (WHERE gap = 0), count(*) FILTER (WHERE gap = 0 AND a1 < greatest(a0 * 0.5, 6.0)) FROM fix").fetchone()
            print(f'[{k + 1}/{len(cells)}] touched {n[0]} dropped {n[1]} | cell {time.time() - tc:.1f}s | {time.time() - t0:.0f}s', flush=True)
    dst = p(a.dst)
    con.execute(f"""COPY (
        SELECT m.* EXCLUDE (geometry),
               CASE WHEN f.g IS NULL THEN m.geometry ELSE ST_Transform(f.g, {BACK}) END geometry,
               coalesce(f.a0 - f.a1, 0)::FLOAT clip_m2, f.gap::FLOAT road_gap_m
        FROM '{p('vn_buildings_merged.parquet')}' m LEFT JOIN fix f USING (id)
        WHERE {'m.id IN (SELECT id FROM b) AND ' if a.bbox else ''}(f.id IS NULL OR f.gap > 0 OR f.a1 >= greatest(f.a0 * {KEEP_SHARE}, {MIN_M2}))
        ORDER BY m.id) TO '{dst}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 200000)""")
    s = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE a1 < greatest(a0 * {KEEP_SHARE}, {MIN_M2})),
                               sum(a0 - a1) FILTER (WHERE a1 >= greatest(a0 * {KEEP_SHARE}, {MIN_M2})), median(a0 - a1) FROM fix WHERE gap = 0""").fetchone()
    by_src = con.execute(f"SELECT src, count(*) FROM '{dst}' GROUP BY 1 ORDER BY 2 DESC").fetchall()
    meta = {'rule': 'footprint minus carriageway union, keep largest part; drop if remaining < 50 % or < 6 m2',
            'rounding_margin_m': ROUND_M, 'road_gap_recorded_up_to_m': GAP_M,
            'half_width_sql': ' '.join(HALF.split()), 'not_cut_by': ' '.join(SKIP.split()),
            'touched': s[0], 'dropped': s[1], 'clipped_m2_total': s[2], 'clipped_m2_median': s[3], 'rows_by_source': dict(by_src)}
    json.dump(meta, open(p(a.dst.replace('.parquet', '.meta.json')), 'w'), indent=1)
    print(f'wrote {dst} | touched {s[0]} | dropped {s[1]} | median cut {s[3]:.1f} m2 | {by_src} | {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()
