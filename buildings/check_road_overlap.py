"""Check the "no building on a carriageway or alley" rule on the finished layers, inside a lon/lat box.

Carriageways are the same polygons clip_to_roads.py cuts with (half-widths HALF, exclusions SKIP, no rounding
margin). For the clean footprints and for the compact layer as decoded by compact_layer.decode(), it prints how
many buildings overlap a carriageway by more than 0.5 m2 and 2 m2. Both counts should be 0.

    .venv/Scripts/python.exe buildings/check_road_overlap.py --bbox W S E N [--out data/buildings]
        [--clean vn_buildings_clean.parquet] [--compact vn_buildings_compact.parquet]
"""
import argparse, os, sys
import duckdb, numpy as np, pyarrow.parquet as pq, shapely
from pyproj import Transformer
from shapely.strtree import STRtree

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clip_to_roads import HALF, SKIP
from compact_layer import decode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bbox', type=float, nargs=4, metavar=('W', 'S', 'E', 'N'), required=True)
    ap.add_argument('--out', default='data/buildings')
    ap.add_argument('--clean', default='vn_buildings_clean.parquet')
    ap.add_argument('--compact', default='vn_buildings_compact.parquet')
    a = ap.parse_args()
    W, S, E, N = a.bbox
    p = lambda n: os.path.join(a.out, n).replace('\\', '/')
    env = f'ST_MakeEnvelope({W}, {S}, {E}, {N})'
    con = duckdb.connect(); con.execute('LOAD spatial')
    roads = con.execute(f"""SELECT ST_AsWKB(ST_Buffer(ST_Transform(geometry, 'EPSG:4326', 'EPSG:32648', always_xy := true), {HALF}))
        FROM '{p('osm_roads.parquet')}' WHERE {HALF} IS NOT NULL AND {SKIP} AND ST_Intersects(geometry, {env})""").fetchall()
    R = [shapely.make_valid(shapely.from_wkb(bytes(w))) for (w,) in roads]
    tree = STRtree(R)
    T = Transformer.from_crs(4326, 32648, always_xy=True)

    def overlap(geoms):
        out = []
        for g in geoms:
            g = shapely.make_valid(shapely.transform(g, lambda c: np.column_stack(T.transform(c[:, 0], c[:, 1]))))
            hit = tree.query(g, predicate='intersects')
            out.append(shapely.union_all([R[j] for j in hit]).intersection(g).area if len(hit) else 0.0)
        return np.array(out)

    def report(name, v):
        print(f'{name:24s} n={len(v):8d}  over 0.5 m2: {(v > 0.5).sum():6d}  over 2 m2: {(v > 2).sum():6d}  max {v.max(initial=0):.2f} m2')

    clean = [shapely.from_wkb(bytes(w)) for (w,) in
             con.execute(f"SELECT ST_AsWKB(geometry) FROM '{p(a.clean)}' WHERE ST_Intersects(geometry, {env})").fetchall()]
    report('clean footprints', overlap(clean))
    if os.path.exists(p(a.compact)):
        rows = pq.read_table(p(a.compact), filters=[('lon_e6', '>=', int(W * 1e6)), ('lon_e6', '<=', int(E * 1e6)),
                                                    ('lat_e6', '>=', int(S * 1e6)), ('lat_e6', '<=', int(N * 1e6))]).to_pylist()
        is_rect = np.array([r['ring'] is None for r in rows], bool)
        v = overlap([decode(r) for r in rows])
        report('compact, decoded', v); report('  rectangles', v[is_rect]); report('  rings', v[~is_rect])


if __name__ == '__main__':
    main()
