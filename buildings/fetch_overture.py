"""Fetch the Vietnam part of the Overture Maps buildings theme (building and building_part).

Source: the latest release in the Overture STAC catalog (https://stac.overturemaps.org/catalog.json), GeoParquet on
the public overturemaps-us-west-2 bucket over HTTPS, licence ODbL. Only the files whose STAC bbox overlaps Vietnam are
read (19 of 512 for buildings), and DuckDB reads only the row groups and columns it needs, not whole files.
A footprint is kept when its centroid lies in one of the 34 provinces of data/buildings/vn_admin.parquet.

Per row: Overture id, province iso, the height attributes (height, min_height, num_floors, num_floors_underground,
min_floor, roof_*), facade and class attributes, `src` = dataset of the footprint (OpenStreetMap, Microsoft ML
Buildings, Google Open Buildings, Esri Community Maps, ...), `height_src` = dataset of the height or floor count
(Overture records per property where a value came from), the OSM record id when there is one, and the geometry as WKB.

Output data/overture/vn_<type>.parquet (+ .meta.json). Re-runnable: each source file is written to
data/overture/_parts/<type>_<NNNNN>.parquet first and skipped when present.

    .venv/Scripts/python.exe buildings/fetch_overture.py [--release 2026-10-21.0] [--out data/overture] [--threads N]
"""
import argparse, json, os, sys, time, urllib.request

import duckdb

STAC = "https://stac.overturemaps.org"
UA = {"User-Agent": "hanoi-traffic-model/buildings (urllib)"}
VN_BBOX = (102.1, 8.1, 109.6, 23.4)        # lon/lat, mainland + coastal islands; the province test does the real cut
TYPES = ('building', 'building_part')


def get_json(url):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120))


def overlapping_files(release, typ):
    """(index, https href) of the files of one type whose STAC item bbox overlaps VN_BBOX."""
    coll = get_json(f"{STAC}/{release}/buildings/{typ}/collection.json")
    items = [l['href'] for l in coll['links'] if l['rel'] == 'item']
    boxes = coll['extent']['spatial']['bbox'][1:]           # [0] is the whole collection, then one per item in order
    assert len(items) == len(boxes), (len(items), len(boxes))
    x0, y0, x1, y1 = VN_BBOX
    out = []
    for i, (href, b) in enumerate(zip(items, boxes)):
        if b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0:
            item = get_json(href)
            assert item['bbox'] == b, href
            out.append((i, item['assets']['aws']['href']))
    return out


def fetch_file(con, href, dst):
    x0, y0, x1, y1 = VN_BBOX
    con.execute(f"""
        COPY (
          WITH raw AS (
            SELECT id, sources, subtype, class, height, min_height, num_floors, num_floors_underground, min_floor,
                   roof_shape, roof_height, roof_material, roof_color, roof_direction, roof_orientation,
                   facade_material, facade_color, is_underground, names.primary AS name, geometry,
                   {'has_parts' if 'type=building/' in href else 'building_id'} AS link
            FROM read_parquet('{href}')
            WHERE bbox.xmax >= {x0} AND bbox.xmin <= {x1} AND bbox.ymax >= {y0} AND bbox.ymin <= {y1}
          )
          SELECT r.* EXCLUDE (sources, geometry),
                 p.iso AS province,
                 list_filter(r.sources, s -> coalesce(s.property, '') = '')[1].dataset AS src,
                 list_filter(r.sources, s -> s.property LIKE '%height%' OR s.property LIKE '%floor%')[1].dataset
                     AS height_src,
                 list_filter(r.sources, s -> s.dataset = 'OpenStreetMap')[1].record_id AS osm_record,
                 ST_AsWKB(r.geometry) AS wkb
          FROM raw r JOIN prov p ON ST_Contains(p.geom, ST_Centroid(ST_GeomFromWKB(ST_AsWKB(r.geometry))))
        ) TO '{dst}.tmp' (FORMAT parquet, COMPRESSION zstd)""")
    os.replace(dst + '.tmp', dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--release', help='Overture release, default the latest in the STAC catalog')
    ap.add_argument('--out', default='data/overture')
    ap.add_argument('--admin', default='data/buildings/vn_admin.parquet')
    ap.add_argument('--threads', type=int, default=round(0.6 * (os.cpu_count() or 1)))
    a = ap.parse_args()
    release = a.release or get_json(f"{STAC}/catalog.json")['latest']
    parts = os.path.join(a.out, '_parts')
    os.makedirs(parts, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET threads={a.threads}")
    con.execute(f"CREATE TABLE prov AS SELECT iso, ST_GeomFromWKB(wkb) geom FROM '{a.admin}' WHERE kind = 'province'")
    print('release', release, '|', con.execute("SELECT count(*) FROM prov").fetchone()[0], 'provinces', flush=True)
    for typ in TYPES:
        files = overlapping_files(release, typ)
        print(f"{typ}: {len(files)} files overlap Vietnam", flush=True)
        t = time.time()
        for n, (i, href) in enumerate(files, 1):
            dst = os.path.join(parts, f"{typ}_{i:05d}.parquet")
            st = 'skip'
            if not os.path.exists(dst):
                fetch_file(con, href, dst)
                st = 'ok'
            rows = con.execute(f"SELECT count(*) FROM '{dst}'").fetchone()[0]
            print(f"  {st:4s} {n:2d}/{len(files)} file {i:05d}: {rows:>9,} rows | {time.time() - t:6.0f} s", flush=True)
        out = os.path.join(a.out, f"vn_{typ}.parquet")
        con.execute(f"COPY (SELECT * FROM read_parquet('{parts}/{typ}_*.parquet') ORDER BY province, id) "
                    f"TO '{out}.tmp' (FORMAT parquet, COMPRESSION zstd)")
        os.replace(out + '.tmp', out)
        q = lambda sql: con.execute(sql.format(t=f"'{out}'")).fetchall()
        meta = {'release': release, 'type': typ, 'licence': 'ODbL-1.0 (Overture Maps Foundation)',
                'source': f"{STAC}/{release}/buildings/{typ}/collection.json", 'files': [i for i, _ in files],
                'rows': q("SELECT count(*) FROM {t}")[0][0],
                'with_height': q("SELECT count(height) FROM {t}")[0][0],
                'with_num_floors': q("SELECT count(num_floors) FROM {t}")[0][0],
                'by_src': dict(q("SELECT coalesce(src, '?'), count(*) FROM {t} GROUP BY 1 ORDER BY 2 DESC")),
                'height_or_floors_by_src': dict(q("SELECT coalesce(height_src, src, '?'), count(*) FROM {t} "
                                                  "WHERE height IS NOT NULL OR num_floors IS NOT NULL "
                                                  "GROUP BY 1 ORDER BY 2 DESC"))}
        with open(out.replace('.parquet', '.meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        print(json.dumps(meta, ensure_ascii=False, indent=1), flush=True)


if __name__ == '__main__':
    sys.exit(main())
