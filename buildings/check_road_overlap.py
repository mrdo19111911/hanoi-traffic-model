"""Check the two footprint rules on the finished layers, inside a lon/lat box:
no building on a carriageway or alley, and no two buildings overlapping.

Carriageways are the polygons fit_footprints.py works with (half-widths HALF, exclusions SKIP, no rounding margin).
For the clean footprints and for the compact layer as decoded by compact_layer.decode(), it prints how many
buildings overlap a carriageway, and how many pairs of buildings overlap each other, by more than 0.05, 0.5 and
2 m2 (touching walls do not count; the 0.5 and 2 m2 counts must be 0), and neighbours that stand 2 cm to 1 m apart
without touching, with the area of gaps under 1 m wide.

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


_T = Transformer.from_crs(4326, 32648, always_xy=True)


def utm(geoms):
    """lon/lat footprints -> valid UTM 48N polygons."""
    g = np.empty(len(geoms), object); g[:] = list(geoms)
    return shapely.make_valid(shapely.transform(g, lambda c: np.column_stack(_T.transform(c[:, 0], c[:, 1]))))


def carriageways(con, roads_parquet, bbox):
    """Carriageway polygons (UTM, half-width HALF, no rounding margin) of roads meeting a lon/lat box, and their tree."""
    env = 'ST_MakeEnvelope({}, {}, {}, {})'.format(*bbox)
    roads = con.execute(f"""SELECT ST_AsWKB(ST_Buffer(ST_Transform(geometry, 'EPSG:4326', 'EPSG:32648', always_xy := true), {HALF}))
        FROM '{roads_parquet}' WHERE {HALF} IS NOT NULL AND {SKIP} AND ST_Intersects(geometry, {env})""").fetchall()
    R = [shapely.make_valid(shapely.from_wkb(bytes(w))) for (w,) in roads]
    return R, STRtree(R)


def on_road_m2(geoms, R, tree, projected=False):
    """Area (m2) of each lon/lat footprint (UTM polygons from utm() when projected) on the carriageways R
    (UTM polygons, tree = STRtree(R))."""
    out = []
    for g in (geoms if projected else utm(geoms)):
        hit = tree.query(g, predicate='intersects')
        out.append(shapely.union_all([R[j] for j in hit]).intersection(g).area if len(hit) else 0.0)
    return np.array(out)


def pair_overlaps(geoms, projected=False, pairs=False):
    """Overlap area (m2) of every pair of intersecting lon/lat footprints (UTM polygons from utm() when projected);
    with pairs, (i, j, area) with i < j the positions of the two footprints."""
    g = np.asarray(geoms, object) if projected else utm(geoms)
    i, j = STRtree(g).query(g, predicate='intersects')
    m = i < j
    a = np.array([shapely.intersection(g[x], g[y]).area for x, y in zip(i[m], j[m])])
    return (i[m], j[m], a) if pairs else a


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
    R, tree = carriageways(con, p('osm_roads.parquet'), a.bbox)
    overlap = lambda geoms: on_road_m2(geoms, R, tree)

    def report(name, v, what='on a road'):
        print(f'{name:24s} n={len(v):8d}  {what} over 0.05 m2: {(v > 0.05).sum():6d}  over 0.5 m2: {(v > 0.5).sum():6d}  '
              f'over 2 m2: {(v > 2).sum():6d}  max {v.max(initial=0):.2f} m2')

    def narrow_gaps(name, geoms):
        """Neighbours that do not touch but stand less than 1 m apart, and the area of such gaps (closing at 0.5 m)."""
        g = utm(geoms)
        i, j = STRtree(g).query(g, predicate='dwithin', distance=1.0)
        m = i < j
        d = shapely.distance(g[i[m]], g[j[m]])
        u = shapely.union_all(g)
        c = shapely.buffer(shapely.buffer(u, 0.5, join_style='mitre'), -0.5, join_style='mitre')
        gap = shapely.difference(c, u)
        hit = tree.query(gap)
        if len(hit):
            gap = shapely.difference(gap, shapely.union_all([R[k] for k in hit]))
        print(f'{name:24s} pairs 0.02-1 m apart: {((d > 0.02) & (d < 1.0)).sum():6d}  touching: {(d <= 0.02).sum():6d}  '
              f'gap area under 1 m wide: {gap.area:8.0f} m2')

    clean = [shapely.from_wkb(bytes(w)) for (w,) in
             con.execute(f"SELECT ST_AsWKB(geometry) FROM '{p(a.clean)}' WHERE ST_Intersects(geometry, {env})").fetchall()]
    report('clean footprints', overlap(clean))
    report('clean footprints', pair_overlaps(clean), 'pairs overlapping')
    narrow_gaps('clean footprints', clean)
    if os.path.exists(p(a.compact)):
        rows = pq.read_table(p(a.compact), filters=[('lon_e6', '>=', int(W * 1e6)), ('lon_e6', '<=', int(E * 1e6)),
                                                    ('lat_e6', '>=', int(S * 1e6)), ('lat_e6', '<=', int(N * 1e6))]).to_pylist()
        is_rect = np.array([r['ring'] is None for r in rows], bool)
        v = overlap([decode(r) for r in rows])
        report('compact, decoded', v); report('  rectangles', v[is_rect]); report('  rings', v[~is_rect])
        report('compact, decoded', pair_overlaps([decode(r) for r in rows]), 'pairs overlapping')
        narrow_gaps('compact, decoded', [decode(r) for r in rows])


if __name__ == '__main__':
    main()
