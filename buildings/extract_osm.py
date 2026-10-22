"""Extract buildings, carriageways and water polygons from the Vietnam PBF into GeoParquet (EPSG:4326, WKB).

These are the OSM inputs of the building merge (merge_buildings.py):
- osm_buildings.parquet: every closed way or multipolygon tagged building=* (not building=no),
  with the tags the merge and the height step need (levels, height, name, address).
- osm_roads.parquet: highway=* ways with class, lanes, bridge, tunnel, layer, covered, indoor and area.
- osm_water.parquet: water areas (natural=water, water=*, landuse=reservoir/basin, waterway=riverbank).

    .venv/Scripts/python.exe buildings/extract_osm.py [--pbf osrm/data/vietnam-latest.osm.pbf] [--out data/buildings]
"""
import argparse, gzip, json, os, time
import duckdb, osmium

B_TAGS = ('building', 'building:levels', 'height', 'name', 'addr:housenumber', 'addr:street', 'amenity', 'shop')


class Handler(osmium.SimpleHandler):
    def __init__(self, out):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.f = {k: gzip.open(os.path.join(out, f'_osm_{k}.ndjson.gz'), 'wt', encoding='utf-8') for k in ('buildings', 'roads', 'water')}
        self.n = dict.fromkeys(self.f, 0)

    def put(self, kind, row):
        self.f[kind].write(json.dumps(row, ensure_ascii=False) + '\n')
        self.n[kind] += 1

    def way(self, w):
        t = w.tags
        hw = t.get('highway')
        if not hw:
            return
        try:
            g = self.wkb.create_linestring(w)
        except (osmium.InvalidLocationError, RuntimeError):
            return
        self.put('roads', {'id': w.id, 'hw': hw, 'lanes': t.get('lanes'), 'bridge': t.get('bridge'),
                           'tunnel': t.get('tunnel'), 'layer': t.get('layer'), 'covered': t.get('covered'),
                           'indoor': t.get('indoor'), 'area': t.get('area'), 'name': t.get('name'), 'g': g})

    def area(self, a):
        t = a.tags
        is_b = 'building' in t and t.get('building') != 'no'
        is_w = (t.get('natural') == 'water' or 'water' in t or t.get('landuse') in ('reservoir', 'basin')
                or t.get('waterway') == 'riverbank')
        if not (is_b or is_w):
            return
        try:
            g = self.wkb.create_multipolygon(a)
        except (osmium.InvalidLocationError, RuntimeError):
            return
        oid = ('w' if a.from_way() else 'r') + str(a.orig_id())
        if is_b:
            self.put('buildings', {'osm_id': oid, **{k.replace(':', '_'): t.get(k) for k in B_TAGS}, 'g': g})
        else:
            self.put('water', {'osm_id': oid, 'g': g})

    def close(self):
        for f in self.f.values():
            f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    ap.add_argument('--out', default='data/buildings')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    t = time.time()
    h = Handler(a.out)
    h.apply_file(a.pbf, locations=True, idx='flex_mem')
    h.close()
    print('read', h.n, round(time.time() - t), 's', flush=True)
    con = duckdb.connect()
    con.execute('INSTALL spatial; LOAD spatial;')
    for k in h.f:
        src = os.path.join(a.out, f'_osm_{k}.ndjson.gz').replace('\\', '/')
        dst = os.path.join(a.out, f'osm_{k}.parquet').replace('\\', '/')
        con.execute(f"""COPY (SELECT * EXCLUDE (g), ST_GeomFromHEXWKB(g) AS geometry FROM read_json('{src}', format='newline_delimited',
                        sample_size=-1)) TO '{dst}' (FORMAT parquet, COMPRESSION zstd)""")
        os.remove(src)
        print(k, '->', dst, os.path.getsize(dst) // 1_000_000, 'MB', flush=True)
    print('done', round(time.time() - t), 's')


if __name__ == '__main__':
    main()
