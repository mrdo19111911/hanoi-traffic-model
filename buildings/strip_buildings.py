"""Write a copy of the PBF without buildings, for routing (OSRM never uses buildings).

Dropped: ways and relations tagged building=* (not 'no') or building:part=*, untagged ways used only as
members of dropped relations, and untagged nodes used only by dropped ways. Everything else is kept as is;
relations that pointed at a dropped building keep their other members. Buildings, with their names,
addresses and heights, live in the separate building layer (data/buildings/vn_buildings*.parquet).

    .venv/Scripts/python.exe buildings/strip_buildings.py [--pbf osrm/data/vietnam-latest.osm.pbf] [--out osrm/data/vietnam-nobuildings.osm.pbf]
"""
import argparse, os, time
import osmium


def is_building(tags):
    b = tags.get('building')
    return (b is not None and b != 'no') or 'building:part' in tags


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pbf', default='osrm/data/vietnam-latest.osm.pbf')
    ap.add_argument('--out', default='osrm/data/vietnam-nobuildings.osm.pbf')
    a = ap.parse_args()
    t = time.time()

    # Pass 1: building relations and the ways they use; building ways and their nodes.
    drop_rel, rel_ways, drop_way, cand_nodes = set(), set(), set(), set()

    class P1(osmium.SimpleHandler):
        def way(s, w):
            if is_building(w.tags):
                drop_way.add(w.id); cand_nodes.update(n.ref for n in w.nodes)

        def relation(s, r):
            if is_building(r.tags):
                drop_rel.add(r.id); rel_ways.update(m.ref for m in r.members if m.type == 'w')
    P1().apply_file(a.pbf)

    # Pass 2: anything still in use by kept objects stays.
    used_ways, used_nodes, untagged_rel_ways = set(), set(), set()

    class P2(osmium.SimpleHandler):
        def way(s, w):
            if w.id in rel_ways and w.id not in drop_way and len(w.tags) == 0:
                untagged_rel_ways.add(w.id); cand_nodes.update(n.ref for n in w.nodes)

        def relation(s, r):
            if r.id in drop_rel:
                return
            for m in r.members:
                if m.type == 'w':
                    used_ways.add(m.ref)
                elif m.type == 'n':
                    used_nodes.add(m.ref)
    P2().apply_file(a.pbf)
    drop_way |= (untagged_rel_ways - used_ways)

    class P3(osmium.SimpleHandler):
        def way(s, w):
            if w.id not in drop_way:
                for n in w.nodes:
                    if n.ref in cand_nodes:
                        used_nodes.add(n.ref)
    P3().apply_file(a.pbf)
    print(f'drop: {len(drop_way)} ways, {len(drop_rel)} relations; candidate nodes {len(cand_nodes)} | {time.time() - t:.0f}s', flush=True)

    wr = osmium.SimpleWriter(a.out + '.tmp.pbf')
    kept = {'n': 0, 'w': 0, 'r': 0}
    dropped_nodes = [0]

    class W(osmium.SimpleHandler):
        def node(s, n):
            if n.id in cand_nodes and n.id not in used_nodes and len(n.tags) == 0:
                dropped_nodes[0] += 1
                return
            wr.add_node(n); kept['n'] += 1

        def way(s, w):
            if w.id not in drop_way:
                wr.add_way(w); kept['w'] += 1

        def relation(s, r):
            if r.id not in drop_rel:
                wr.add_relation(r); kept['r'] += 1
    W().apply_file(a.pbf)
    wr.close()
    os.replace(a.out + '.tmp.pbf', a.out)
    s0, s1 = os.path.getsize(a.pbf), os.path.getsize(a.out)
    print(f'kept {kept}, dropped nodes {dropped_nodes[0]}')
    print(f'{a.pbf}: {s0 / 1e6:.0f} MB -> {a.out}: {s1 / 1e6:.0f} MB ({100 * (1 - s1 / s0):.0f}% smaller) | {time.time() - t:.0f}s')


if __name__ == '__main__':
    main()
