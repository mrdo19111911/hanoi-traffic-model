"""Real heights beyond OSM tags for the published layer (buildings/by_area): Wikidata (CC0) heights / floors matched to
footprints, and sibling copies inside a complex. The result is a small committed file, buildings/height_sources.json,
that calibrate_heights.py apply reads (precedence osm / osm_part > wikidata > sibling > g25cal > est).

    .venv/Scripts/python.exe buildings/extra_heights.py fetch       # Wikidata SPARQL -> data/buildings/_wikidata/ (cached, never refetched)
    .venv/Scripts/python.exe buildings/extra_heights.py names       # OSM id / name of every OSM footprint -> data/buildings/_extra/names.parquet
    .venv/Scripts/python.exe buildings/extra_heights.py complexes   # OSM complex areas and site relations (PBF) -> data/buildings/_extra/
    .venv/Scripts/python.exe buildings/extra_heights.py build       # match + siblings + validation -> buildings/height_sources.json

All inputs of `build` are the raw layer (data/buildings/_calib/raw_by_area, commit 703e019), the Wikidata cache, the
OSM names / complexes extracted from the PBF, and the calibration parameters (for the hold-out "before" values).
Every matching or acceptance decision is a fixed rule of this file (see the constants below and the 'rules' block of
the output); nothing is decided per item by hand.
"""
import argparse, datetime, glob, hashlib, json, math, os, re, sys, time, unicodedata
import urllib.parse, urllib.request
import numpy as np, pyarrow as pa, pyarrow.parquet as pq, shapely

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WD_DIR = os.path.join(REPO, 'data', 'buildings', '_wikidata')
EXTRA = os.path.join(REPO, 'data', 'buildings', '_extra')
RAW_DIR = os.path.join(REPO, 'data', 'buildings', '_calib', 'raw_by_area')
OUT_JSON = os.path.join(REPO, 'buildings', 'height_sources.json')
PBF = os.path.join(REPO, 'osrm', 'data', 'vietnam-latest.osm.pbf')
UA = 'hanoi-traffic-model/1.0 (https://github.com/mrdo19111911/hanoi-traffic-model; building heights; contact via GitHub issues)'
SPARQL = 'https://query.wikidata.org/sparql'
BOX = (102.1, 8.2, 109.6, 23.5)            # lon/lat box around Vietnam (items outside Vietnam find no footprint)


# ---------------------------------------------------------------------------------------------------- fetch

def sparql_cached(name, query):
    """Result of a SPARQL query, from data/buildings/_wikidata/<name>-<sha1 of the query>.json if present, else fetched
    once (POST, polite User-Agent naming the repository) and saved. Never refetched."""
    os.makedirs(WD_DIR, exist_ok=True)
    key = hashlib.sha1(query.encode('utf-8')).hexdigest()[:16]
    path = os.path.join(WD_DIR, f'{name}-{key}.json')
    if os.path.exists(path):
        return json.load(open(path, encoding='utf-8'))
    req = urllib.request.Request(SPARQL, data=urllib.parse.urlencode({'query': query, 'format': 'json'}).encode(),
                                 headers={'User-Agent': UA, 'Accept': 'application/sparql-results+json'})
    for attempt in range(4):
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=300).read())
            break
        except Exception as e:                       # 429 / 5xx / timeouts: back off, then give up
            if attempt == 3:
                raise
            print(f'  {name}: {type(e).__name__}, retry in {10 * (attempt + 1)} s', flush=True)
            time.sleep(10 * (attempt + 1))
    out = {'query': query, 'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
           'endpoint': SPARQL, 'result': data}
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(out, fh, ensure_ascii=False)
    time.sleep(2)
    return out


Q_LIST_COUNTRY = '''SELECT DISTINCT ?item WHERE {
  ?item wdt:P17 wd:Q881 ; wdt:P625 ?c .
  { ?item p:P2048 ?s } UNION { ?item p:P1101 ?s } }'''
Q_LIST_BOX = '''SELECT DISTINCT ?item WHERE {
  SERVICE wikibase:box { ?item wdt:P625 ?c .
    bd:serviceParam wikibase:cornerSouthWest "Point(%g %g)"^^geo:wktLiteral ;
                    wikibase:cornerNorthEast "Point(%g %g)"^^geo:wktLiteral . }
  { ?item p:P2048 ?s } UNION { ?item p:P1101 ?s } }''' % BOX
Q_COORD = '''SELECT ?item ?c ?rank WHERE { VALUES ?item { %s }
  ?item p:P625 ?st . ?st ps:P625 ?c ; wikibase:rank ?rank . }'''
Q_HEIGHT = '''SELECT ?item ?st ?amount ?unit ?norm ?rank ?part WHERE { VALUES ?item { %s }
  ?item p:P2048 ?st . ?st wikibase:rank ?rank ; psv:P2048 ?v . ?v wikibase:quantityAmount ?amount ; wikibase:quantityUnit ?unit .
  OPTIONAL { ?st psn:P2048/wikibase:quantityAmount ?norm . }
  OPTIONAL { ?st pq:P518 ?part . } }'''
Q_FLOORS = '''SELECT ?item ?st ?floors ?rank WHERE { VALUES ?item { %s }
  ?item p:P1101 ?st . ?st ps:P1101 ?floors ; wikibase:rank ?rank . }'''
Q_INFO = '''SELECT ?item ?vi ?en ?type ?typeEn ?isBuilding WHERE { VALUES ?item { %s }
  OPTIONAL { ?item rdfs:label ?vi . FILTER(LANG(?vi) = "vi") }
  OPTIONAL { ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }
  OPTIONAL { ?item wdt:P31 ?type . OPTIONAL { ?type rdfs:label ?typeEn . FILTER(LANG(?typeEn) = "en") }
             BIND(EXISTS { ?type wdt:P279* wd:Q41176 } AS ?isBuilding) } }'''
Q_STATE = '''SELECT ?item ?prec ?state ?stateEn ?inception ?opened ?demolished WHERE { VALUES ?item { %s }
  OPTIONAL { ?item p:P625/psv:P625/wikibase:geoPrecision ?prec . }
  OPTIONAL { ?item wdt:P5817 ?state . OPTIONAL { ?state rdfs:label ?stateEn . FILTER(LANG(?stateEn) = "en") } }
  OPTIONAL { ?item wdt:P571 ?inception . }
  OPTIONAL { ?item wdt:P1619 ?opened . }
  OPTIONAL { ?item wdt:P576 ?demolished . } }'''
Q_CLASS = '''SELECT DISTINCT ?item ?anc ?ancEn WHERE { VALUES ?item { %s }
  ?item wdt:P31/wdt:P279* ?anc . ?anc rdfs:label ?ancEn . FILTER(LANG(?ancEn) = "en") }'''
Q_EVENT = '''SELECT ?item ?ev ?evEn WHERE { VALUES ?item { %s }
  ?item wdt:P793 ?ev . OPTIONAL { ?ev rdfs:label ?evEn . FILTER(LANG(?evEn) = "en") } }'''
Q_ALIAS = '''SELECT ?item ?alias WHERE { VALUES ?item { %s }
  ?item skos:altLabel ?alias . FILTER(LANG(?alias) IN ("vi", "en")) }'''
BATCH = 150


def _val(b, k):
    return b[k]['value'] if k in b else None


def _qid(uri):
    return uri.rsplit('/', 1)[-1] if uri else None


def wikidata_items():
    """Every cached Wikidata item (fetching what is missing): qid -> {coords, heights, floors, labels, types}."""
    ids = set()
    for name, q in (('list_country', Q_LIST_COUNTRY), ('list_box', Q_LIST_BOX)):
        r = sparql_cached(name, q)
        ids |= {_qid(_val(b, 'item')) for b in r['result']['results']['bindings']}
    ids = sorted(ids, key=lambda s: int(s[1:]))
    items = {q: {'qid': q, 'coords': [], 'heights': [], 'floors': [], 'vi': None, 'en': None, 'aliases': [],
                 'types': {}, 'is_building': False,
                 'geo_precision': [], 'state': {}, 'inception': [], 'opened': [], 'demolished': [],
                 'classes': {}, 'events': {}} for q in ids}
    for b0 in range(0, len(ids), BATCH):
        vals = ' '.join(f'wd:{q}' for q in ids[b0:b0 + BATCH])
        tag = f'{b0 // BATCH:03d}'
        for b in sparql_cached(f'coord_{tag}', Q_COORD % vals)['result']['results']['bindings']:
            m = re.match(r'Point\(([-\d.eE]+) ([-\d.eE]+)\)', _val(b, 'c') or '')
            if m and '<http' not in (_val(b, 'c') or ''):   # Earth only (other globes carry a URI prefix)
                items[_qid(_val(b, 'item'))]['coords'].append((float(m.group(1)), float(m.group(2)), _qid(_val(b, 'rank'))))
        seen = set()
        for b in sparql_cached(f'height_{tag}', Q_HEIGHT % vals)['result']['results']['bindings']:
            st = _val(b, 'st')
            rec = {'st': st, 'amount': float(_val(b, 'amount')), 'unit': _qid(_val(b, 'unit')),
                   'norm': float(_val(b, 'norm')) if _val(b, 'norm') else None, 'rank': _qid(_val(b, 'rank')),
                   'part': _qid(_val(b, 'part'))}
            key = (st, rec['part'])
            if key not in seen:
                seen.add(key); items[_qid(_val(b, 'item'))]['heights'].append(rec)
        for b in sparql_cached(f'floors_{tag}', Q_FLOORS % vals)['result']['results']['bindings']:
            items[_qid(_val(b, 'item'))]['floors'].append({'st': _val(b, 'st'), 'value': float(_val(b, 'floors')),
                                                          'rank': _qid(_val(b, 'rank'))})
        for b in sparql_cached(f'info_{tag}', Q_INFO % vals)['result']['results']['bindings']:
            it = items[_qid(_val(b, 'item'))]
            it['vi'] = it['vi'] or _val(b, 'vi'); it['en'] = it['en'] or _val(b, 'en')
            if _val(b, 'type'):
                it['types'][_qid(_val(b, 'type'))] = _val(b, 'typeEn')
                it['is_building'] |= _val(b, 'isBuilding') == 'true'
        for b in sparql_cached(f'alias_{tag}', Q_ALIAS % vals)['result']['results']['bindings']:
            it = items[_qid(_val(b, 'item'))]
            if _val(b, 'alias') not in it['aliases']:
                it['aliases'].append(_val(b, 'alias'))
        for b in sparql_cached(f'state_{tag}', Q_STATE % vals)['result']['results']['bindings']:
            it = items[_qid(_val(b, 'item'))]
            if _val(b, 'prec') and float(_val(b, 'prec')) not in it['geo_precision']:
                it['geo_precision'].append(float(_val(b, 'prec')))
            if _val(b, 'state'):
                it['state'][_qid(_val(b, 'state'))] = _val(b, 'stateEn')
            for k in ('inception', 'opened', 'demolished'):
                v = _val(b, k)
                if v and v[:10] not in it[k]:
                    it[k].append(v[:10])
        for b in sparql_cached(f'class_{tag}', Q_CLASS % vals)['result']['results']['bindings']:
            items[_qid(_val(b, 'item'))]['classes'][_qid(_val(b, 'anc'))] = _val(b, 'ancEn')
        for b in sparql_cached(f'event_{tag}', Q_EVENT % vals)['result']['results']['bindings']:
            items[_qid(_val(b, 'item'))]['events'][_qid(_val(b, 'ev'))] = _val(b, 'evEn')
    for it in items.values():                        # deterministic order inside each item
        it['coords'].sort(); it['heights'].sort(key=lambda r: (r['st'], r['part'] or '')); it['floors'].sort(key=lambda r: r['st'])
        it['aliases'].sort(); it['geo_precision'].sort()
        for k in ('inception', 'opened', 'demolished'):
            it[k].sort()
    return items


def cmd_fetch(a):
    items = wikidata_items()
    n = len(items)
    print(f'items {n} | with coordinates {sum(1 for i in items.values() if i["coords"])} | with height '
          f'{sum(1 for i in items.values() if i["heights"])} | with floors {sum(1 for i in items.values() if i["floors"])} | '
          f'building class {sum(1 for i in items.values() if i["is_building"])}')
    units = {}
    for i in items.values():
        for h in i['heights']:
            units[h['unit']] = units.get(h['unit'], 0) + 1
    print('height units', units)


# ---------------------------------------------------------------------------------------------------- OSM inputs

def cmd_names(a):
    """id, osm_id, name of every OSM footprint of the clean layer (ids are kept by every later step)."""
    import duckdb
    os.makedirs(EXTRA, exist_ok=True)
    src = os.path.join(REPO, 'data', 'buildings', 'vn_buildings_clean.parquet').replace('\\', '/')
    dst = os.path.join(EXTRA, 'names.parquet').replace('\\', '/')
    duckdb.connect().execute(f"COPY (SELECT id, osm_id, name FROM '{src}' WHERE osm_id IS NOT NULL ORDER BY id) TO '{dst}' "
                             f"(FORMAT parquet, COMPRESSION zstd)")
    t = pq.read_table(dst)
    print(f'wrote {dst}: {t.num_rows} rows, named {t.num_rows - t["name"].null_count}')


COMPLEX_LANDUSE = ('residential', 'commercial', 'retail')
COMPLEX_AMENITY = ('university', 'college', 'hospital', 'school')
COMPLEX_MAX_KM2 = 0.5                    # larger areas are neighbourhoods or districts, not one complex


def cmd_complexes(a):
    """Complex areas (closed ways / multipolygons with landuse residential / commercial / retail, any residential=*, or
    amenity university / college / hospital / school, at most COMPLEX_MAX_KM2) and type=site relations (member ids)
    from the PBF -> data/buildings/_extra/complexes.parquet, site_members.parquet."""
    import osmium
    os.makedirs(EXTRA, exist_ok=True)
    t0 = time.time()

    class H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.wkb = osmium.geom.WKBFactory()
            self.rows = []
            self.sites = []

        def relation(self, r):
            if r.tags.get('type') == 'site':
                for m in r.members:
                    if m.type in ('w', 'r'):
                        self.sites.append((f'r{r.id}', f'{m.type}{m.ref}', r.tags.get('name')))

        def area(self, ar):
            t = ar.tags
            kind = None
            if t.get('landuse') in COMPLEX_LANDUSE:
                kind = 'landuse=' + t.get('landuse')
            elif 'residential' in t:
                kind = 'residential=' + t.get('residential')
            elif t.get('amenity') in COMPLEX_AMENITY:
                kind = 'amenity=' + t.get('amenity')
            if kind is None or ('building' in t and t.get('building') != 'no'):
                return
            try:
                g = shapely.from_wkb(bytes.fromhex(self.wkb.create_multipolygon(ar)))
            except (osmium.InvalidLocationError, RuntimeError, shapely.errors.GEOSException):
                return
            lat = shapely.centroid(g).y
            km2 = shapely.area(g) * 111.32 * 110.574 * math.cos(math.radians(lat))
            if km2 <= COMPLEX_MAX_KM2:
                self.rows.append({'cid': ('w' if ar.from_way() else 'r') + str(ar.orig_id()), 'kind': kind,
                                  'name': t.get('name'), 'km2': km2, 'wkb': shapely.to_wkb(g)})

    h = H()
    h.apply_file(PBF, locations=True, idx='flex_mem')
    h.rows.sort(key=lambda r: r['cid'])
    pq.write_table(pa.Table.from_pylist(h.rows), os.path.join(EXTRA, 'complexes.parquet'), compression='zstd')
    pq.write_table(pa.table({'site': [s[0] for s in h.sites], 'member': [s[1] for s in h.sites],
                             'name': [s[2] for s in h.sites]}), os.path.join(EXTRA, 'site_members.parquet'))
    print(f'complex areas {len(h.rows)} | site relation members {len(h.sites)} | {time.time() - t0:.0f}s')


# ---------------------------------------------------------------------------------------------------- scan

WD_RADIUS_M = 50.0                       # footprints within this distance of a Wikidata point are candidates
SIB_H25_MIN, SIB_AREA_MIN = 25.0, 400.0  # sibling targets: raw h25 >= this or footprint >= this (m2)


def item_points(it):
    """Coordinates of an item used for matching: every non-deprecated P625 value of the best rank present."""
    rank = {'ontology#PreferredRank': 0, 'ontology#NormalRank': 1}
    cs = [c for c in it['coords'] if c[2] in rank]
    if not cs:
        return []
    best = min(rank[c[2]] for c in cs)
    return sorted({(c[0], c[1]) for c in cs if rank[c[2]] == best})


def _scan_file(args):
    """One raw file -> (sibling-target rows, Wikidata candidate rows). Targets: rows without a real height (not osm /
    osm_part) with raw h25 >= SIB_H25_MIN or area >= SIB_AREA_MIN. Candidates: every footprint within WD_RADIUS_M of a
    Wikidata point (distance to the footprint, 0 when it contains the point)."""
    path, pts = args
    from scipy.spatial import cKDTree
    from calibrate_heights import features, ring_geoms, src_strings, xyz, TRUTH_SRC
    tbl = pq.read_table(path)
    name = os.path.basename(path)
    src = src_strings(tbl)
    f = features(tbl)
    hdm = tbl['height_dm'].combine_chunks()
    hv = hdm.fill_null(0).to_numpy().astype(float) / 10
    h25 = np.where(src == 'g25', hv, np.nan)
    ids = tbl['id'].to_numpy()
    real = np.isin(src, TRUTH_SRC)
    el = np.flatnonzero(~real & ((np.nan_to_num(h25) >= SIB_H25_MIN) | (f['area'] >= SIB_AREA_MIN)))
    tg = {'file': [name] * len(el), 'id': ids[el], 'lon': f['lon'][el], 'lat': f['lat'][el], 'area': f['area'][el],
          'length': f['length'][el], 'width': f['width'][el], 'h25': h25[el],
          'src': [s if s is not None else '' for s in src[el]]}
    cand = []
    if pts:
        geoms, kx, ky = ring_geoms(tbl)
        tree = cKDTree(xyz(f['lon'], f['lat']))
        fl = tbl['floors'].combine_chunks().fill_null(0).to_numpy()
        lon_e6, lat_e6 = tbl['lon_e6'].to_numpy(), tbl['lat_e6'].to_numpy()
        for qid, plon, plat in pts:
            for i in tree.query_ball_point(xyz(np.array([plon]), np.array([plat]))[0], 600.0):
                px, py = (plon * 1e6 - lon_e6[i]) * kx[i], (plat * 1e6 - lat_e6[i]) * ky[i]
                d = float(shapely.distance(geoms[i], shapely.Point(px, py)))
                if d <= WD_RADIUS_M:
                    cand.append({'qid': qid, 'plon': plon, 'plat': plat, 'file': name, 'id': int(ids[i]), 'd': round(d, 2),
                                 'lon': float(f['lon'][i]), 'lat': float(f['lat'][i]), 'area': float(f['area'][i]),
                                 'length': float(f['length'][i]), 'width': float(f['width'][i]),
                                 'src': src[i] or '', 'height': float(hv[i]) if src[i] else None,
                                 'floors': int(fl[i])})
    return tg, cand


def cmd_scan(a):
    """Sibling targets and Wikidata candidates of every raw file -> data/buildings/_extra/{targets,wd_candidates}.parquet."""
    from concurrent.futures import ProcessPoolExecutor
    t0 = time.time()
    os.makedirs(EXTRA, exist_ok=True)
    items = wikidata_items()
    idx = json.load(open(os.path.join(a.raw_dir, 'index.json'), encoding='utf-8'))
    pts = [(q, lo, la) for q, it in items.items() for lo, la in item_points(it)]
    jobs = []
    for e in idx['files']:
        lo0, la0, lo1, la1 = e['bbox']
        m = 0.01
        jobs.append((os.path.join(a.raw_dir, e['file']),
                     [p for p in pts if lo0 - m <= p[1] <= lo1 + m and la0 - m <= p[2] <= la1 + m]))
    jobs.sort(key=lambda j: -os.path.getsize(j[0]))
    T, C = [], []
    with ProcessPoolExecutor(min(a.workers, 6)) as ex:
        for k, (tg, cand) in enumerate(ex.map(_scan_file, jobs)):
            T.append(pa.table(tg)); C.extend(cand)
            if (k + 1) % 50 == 0:
                print(f'{k + 1}/{len(jobs)} files | {time.time() - t0:.0f}s', flush=True)
    tt = pa.concat_tables(T).sort_by([('file', 'ascending'), ('id', 'ascending')])
    pq.write_table(tt, os.path.join(EXTRA, 'targets.parquet'), compression='zstd')
    C.sort(key=lambda r: (r['qid'], r['file'], r['id'], r['plon'], r['plat']))
    pq.write_table(pa.Table.from_pylist(C), os.path.join(EXTRA, 'wd_candidates.parquet'), compression='zstd')
    print(f'targets {tt.num_rows} | wikidata candidates {len(C)} (items {len({c["qid"] for c in C})}) | {time.time() - t0:.0f}s')

# ---------------------------------------------------------------------------------------------------- names

GENERIC = {'khach', 'san', 'hotel', 'toa', 'nha', 'resort', 'spa', 'the', 'and', 'apartment', 'apartments', 'hostel',
           'homestay', 'nghi', 'boutique', 'villa', 'inn', 'by', 'of'}
NAME_MATCH = 0.75                        # name similarity counted as the same name
INDEX_RE = re.compile(r'([a-z]*)(\d{1,3})([a-z]?)')


def norm_name(s):
    """Lower case, no diacritics (d for đ), runs of anything but a-z / 0-9 -> one space."""
    if not s:
        return ''
    s = s.replace('đ', 'd').replace('Đ', 'D')
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^0-9a-z]+', ' ', s.lower()).strip()


def _core(n):
    t = [w for w in n.split() if w not in GENERIC]
    return ' '.join(t) if t else n


def name_sim(a, b):
    """Similarity of two normalised names in [0, 1]: difflib ratio of the names without generic words (hotel, toa,
    nha, ...), raised to 0.9 when the shorter (>= 5 characters) appears as whole words in the longer."""
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    a, b = _core(a), _core(b)
    r = SequenceMatcher(None, a, b).ratio()
    s, l = sorted((a, b), key=len)
    if len(s) >= 5 and re.search(r'(^| )' + re.escape(s) + r'( |$)', l):
        r = max(r, 0.9)
    return r


STEM_GENERIC = GENERIC | {'lo', 'khu', 'day', 'block', 'blk', 'tower', 'building', 'bldg', 'khoi', 'thap', 'nha', 'toa',
                         'so', 'no', 'cu', 'chung', 'can', 'ho', 'tang', 'unit', 'zone', 'phan'}


def name_core(n):
    """A normalised name without the generic words of STEM_GENERIC (toa, lo, nha, khu, day, block, chung cu, ...)."""
    return ' '.join(w for w in n.split() if w not in STEM_GENERIC) if n else ''


def name_stem(n):
    """(stem, indexed) of a normalised name: a last word like CT1 / A2 / 12 / 3B is an index (its letters stay in the
    stem: CT1 -> 'ct'), a last single letter too; generic words (STEM_GENERIC: toa, lo, nha, khu, day, block, ...) are
    dropped from the stem, so 'Toa F' / 'Toa G' have no stem and are not a name link; else the whole name (without
    generic words) with indexed False."""
    if not n:
        return None, False
    w = n.split()
    m = INDEX_RE.fullmatch(w[-1])
    if m:
        stem = ' '.join([x for x in w[:-1] if x not in STEM_GENERIC] + ([m.group(1)] if m.group(1) else []))
    elif len(w[-1]) == 1 and len(w) > 1:
        stem = name_core(' '.join(w[:-1]))
    else:
        c = name_core(n)
        return (c, False) if len(c) >= 2 else (None, False)
    return (stem, True) if len(stem) >= 2 else (None, False)


def load_names():
    t = pq.read_table(os.path.join(EXTRA, 'names.parquet'))
    return dict(zip(t['id'].to_numpy().tolist(), t['name'].to_pylist())), dict(zip(t['id'].to_numpy().tolist(), t['osm_id'].to_pylist()))


# ---------------------------------------------------------------------------------------------------- Wikidata rules

PBF_DATE = '2026-10-20'                  # items opened / built after the map extract cannot be on it
WD_H_RANGE = (3.0, 600.0)                # accepted Wikidata heights (m)
WD_FLOOR_M, WD_FLOOR_BASE = 3.3, 4.0     # height from floors: floors x 3.3 + 4 m
WD_PREC_MAX = 0.001                      # coordinate precision coarser than this (degrees, ~110 m): rejected
WD_STATE_WORDS = ('proposed', 'planned', 'construction', 'demolish', 'destroyed', 'former', 'abandoned', 'cancel',
                  'not in use', 'unfinished', 'unbuilt', 'disused')
# P31/P279* ancestor classes (English labels) marking an unbuilt or vanished building; whole phrases, so the generic
# 'fixed construction' / 'planned act' ancestors of every building do not match.
WD_CLASS_PHRASES = ('proposed building', 'proposed structure', 'planned building', 'planned structure', 'under construction',
                    'unbuilt', 'unfinished building', 'unfinished structure', 'cancelled', 'canceled', 'never built',
                    'demolished building', 'demolished structure', 'destroyed building', 'former building', 'lost building', 'ruins')
WD_ORG_WORDS = ('company', 'organization', 'organisation', 'enterprise', 'business', 'brand', 'chain')
WD_VESSEL_WORDS = ('cruise', 'cruises', 'boat', 'ship', 'yacht', 'du thuyen')   # whole words ('tau' alone is Vung Tau)
WD_EVENT_WORDS = ('demolition', 'destruction', 'cancel', 'abandon')   # P793 significant events that end a building
WD_RATIO_CAP = (0.5, 4.0)                # Wikidata height / raw h25 (4 = tall-stage alpha clamp); h25 >= 94 m: lower bound only
WD_SAT_H25 = 94.0
NAME_CONFLICT_REJECT = 0.5               # best footprint has an OSM name with similarity below this: item rejected
WD_W = {'name': 3.0, 'name_conflict': -1.0, 'contains': 1.0, 'size': 1.0, 'tall': 1.0, 'per_m': 1 / 25}
NAME_CONFLICT = 0.3                      # a footprint whose OSM name has a similarity below this is named something else
WD_AMBIG_REJECT, WD_AMBIG_LIST = 0.25, 0.75


def _lower_median(v):
    v = sorted(v)
    return v[(len(v) - 1) // 2]


def wd_value(it):
    """(height m or None, floors or None, how, reject reason or None) of an item. Height: P2048 statements of the best
    non-deprecated rank without an 'applies to part' qualifier, normalised to metres (psn; metre amounts as they are),
    lower median. Floors: P1101 likewise. Then height = P2048, else floors x 3.3 + 4; floors = P1101, else
    round(height / 3.3) (>= 1)."""
    rank = {'ontology#PreferredRank': 0, 'ontology#NormalRank': 1}

    def best(recs):
        recs = [r for r in recs if r['rank'] in rank]
        if not recs:
            return []
        b = min(rank[r['rank']] for r in recs)
        return [r for r in recs if rank[r['rank']] == b]
    hs = [r for r in best(it['heights']) if r['part'] is None]
    hm = [r['norm'] if r['norm'] is not None else (r['amount'] if r['unit'] == 'Q11573' else None) for r in hs]
    hm = [v for v in hm if v is not None]
    fl = [r['value'] for r in best(it['floors']) if r['value'] >= 1]
    H = _lower_median(hm) if hm else None
    F = int(round(_lower_median(fl))) if fl else None
    if H is None and F is None:
        return None, None, None, 'no usable height or floors (part-only, deprecated or unknown unit)'
    how = 'P2048' if H is not None else 'P1101 x 3.3 + 4'
    if H is None:
        H = F * WD_FLOOR_M + WD_FLOOR_BASE
    if F is None:
        F = max(1, int(round(H / WD_FLOOR_M)))
    return round(H, 2), F, how, None


def wd_item_reject(it, H):
    """Item-level rejection (before matching), first rule that applies."""
    if not it['is_building']:
        return 'not a building class (P31/P279* Q41176): ' + ', '.join(sorted(v or k for k, v in it['types'].items()))
    for k, v in sorted(it['types'].items()):           # a direct P31 company / organisation is not a building
        if v and any(w in v.lower() for w in WD_ORG_WORDS):
            return f'instance of {v}'
    labels = ' ' + ' | '.join(norm_name(x) for x in [it['vi'], it['en'], *it['aliases']] if x) + ' '
    if any(f' {w} ' in labels for w in WD_VESSEL_WORDS):
        return 'a vessel (label names a cruise / boat / ship), not a building'
    if not item_points(it):
        return 'no usable coordinates'
    if it['geo_precision'] and min(it['geo_precision']) > WD_PREC_MAX:
        return f'coordinate precision coarser than {WD_PREC_MAX} deg'
    for k, v in sorted(it['state'].items()):        # P5817 state of use whose English label names one of the words
        if any(w in (v or '').lower() for w in WD_STATE_WORDS):
            return f'state of use: {v}'
    for k, v in sorted(it['classes'].items()):
        if v and any(p in v.lower() for p in WD_CLASS_PHRASES):
            return f'class marks it unbuilt / gone: {v}'
    for k, v in sorted(it['events'].items()):
        if v and any(w in v.lower() for w in WD_EVENT_WORDS):
            return f'significant event: {v}'
    for k in ('opened', 'inception'):
        if it[k] and min(it[k]) > PBF_DATE:
            return f'{k} {min(it[k])} after the map extract'
    if it['demolished']:
        return 'demolished / dissolved (P576)'
    if not (WD_H_RANGE[0] <= H <= WD_H_RANGE[1]):
        return f'height {H} m outside {WD_H_RANGE}'
    return None


def wd_score(c, H, sim, named):
    """Explicit score of a candidate footprint: 3 x name match (similarity >= NAME_MATCH with its OSM name) - 1 if it
    has an OSM name with similarity < NAME_CONFLICT (named something else) + 1 if it contains the point + size (0 at
    <= 100 m2 .. 1 at >= 2,000 m2, log scale) + 1 if its own raw height (2.5D or OSM) is at least min(H / 2, 30 m)
    - distance / 25 m."""
    size = min(1.0, max(0.0, math.log10(max(c['area'], 1) / 100) / math.log10(20)))
    own = c['height'] if c['height'] is not None else 0.0
    tall = 1.0 if own >= min(H / 2, 30.0) else 0.0
    return (WD_W['name'] * (sim >= NAME_MATCH) + WD_W['name_conflict'] * (named and sim < NAME_CONFLICT)
            + WD_W['contains'] * (c['d'] == 0) + WD_W['size'] * size
            + WD_W['tall'] * tall - WD_W['per_m'] * c['d'])


def match_wikidata(items, cands, names):
    """Wikidata items -> (outcome of every item, ambiguous cases). Rules, in order: item value and item rules
    (wd_value, wd_item_reject); candidates = footprints within WD_RADIUS_M of any of the item's points (nearest
    distance per footprint); the highest wd_score wins (ties: nearer, then file / id); no candidate or a best score
    < 0: rejected; runner-up within WD_AMBIG_REJECT of the best and the best has no name match: rejected as
    ambiguous; runner-up within WD_AMBIG_LIST: listed as ambiguous, resolved by the score; two items on one
    footprint: the item with a P2048 height keeps it, then the higher score (ties: lower QID), the other is rejected."""
    by_q = {}
    for c in cands:
        k = (c['file'], c['id'])
        d = by_q.setdefault(c['qid'], {})
        if k not in d or c['d'] < d[k]['d']:
            d[k] = c
    out, ambiguous = {}, []
    for q, it in items.items():
        H, F, how, why = wd_value(it)
        rec = {'qid': q, 'label': it['vi'] or it['en'] or (it['aliases'][0] if it['aliases'] else ''), 'height_m': H,
               'floors': F, 'how': how, 'types': sorted(v or k for k, v in it['types'].items())}
        why = why or wd_item_reject(it, H)
        if why:
            out[q] = {**rec, 'status': 'rejected', 'reason': why}
            continue
        labels = sorted({norm_name(x) for x in [it['vi'], it['en'], *it['aliases']] if x} - {''})
        cs = []
        for k, c in sorted(by_q.get(q, {}).items()):
            nn = norm_name(names.get(c['id']))
            sim = max((name_sim(l, nn) for l in labels), default=0.0) if nn else 0.0
            cs.append({**c, 'osm_name': names.get(c['id']), 'sim': round(sim, 3), 'score': round(wd_score(c, H, sim, bool(nn)), 3)})
        cs.sort(key=lambda c: (-c['score'], c['d'], c['file'], c['id']))
        if not cs:
            out[q] = {**rec, 'status': 'rejected', 'reason': f'no footprint within {WD_RADIUS_M:g} m'}
            continue
        b = cs[0]
        rec.update({'file': b['file'], 'id': b['id'], 'score': b['score'], 'd': b['d'], 'sim': b['sim'], 'osm_name': b['osm_name'],
                    'area': round(b['area'], 1), 'cand_src': b['src'], 'cand_height': b['height'], 'n_candidates': len(cs),
                    'lon': round(b['lon'], 6), 'lat': round(b['lat'], 6), 'length': b['length'], 'width': b['width']})
        if b['score'] < 0:
            out[q] = {**rec, 'status': 'rejected', 'reason': 'no plausible footprint (best score < 0)'}
            continue
        if b['osm_name'] and b['sim'] < NAME_CONFLICT_REJECT:
            out[q] = {**rec, 'status': 'rejected', 'reason': f'best footprint carries another OSM name (similarity < {NAME_CONFLICT_REJECT})'}
            continue
        margin = b['score'] - cs[1]['score'] if len(cs) > 1 else math.inf
        if margin < WD_AMBIG_LIST:
            amb = {'qid': q, 'label': rec['label'],
                   'best': {'file': b['file'], 'id': b['id'], 'score': b['score'], 'd_m': b['d'], 'area_m2': round(b['area']), 'name_sim': b['sim']},
                   'runner_up': {'file': cs[1]['file'], 'id': cs[1]['id'], 'score': cs[1]['score'], 'd_m': cs[1]['d'],
                                 'area_m2': round(cs[1]['area']), 'name_sim': cs[1]['sim']},
                   'margin': round(margin, 3)}
            if margin < WD_AMBIG_REJECT and b['sim'] < NAME_MATCH:
                amb['resolution'] = f'rejected: margin < {WD_AMBIG_REJECT} and no name match'
                ambiguous.append(amb)
                out[q] = {**rec, 'status': 'rejected', 'reason': 'ambiguous footprint'}
                continue
            amb['resolution'] = 'best score kept' + (' (name match)' if b['sim'] >= NAME_MATCH else '')
            ambiguous.append(amb)
        rec['rule'] = 'name' if b['sim'] >= NAME_MATCH else ('contains' if b['d'] == 0 else 'nearest')
        out[q] = {**rec, 'status': 'matched'}
    # One item per footprint: an item with a P2048 height (the building's own measured height) first, then the
    # higher score, then the lower QID (a hotel inside a tower carries the hotel's floors, not the tower's).
    taken = {}
    for q in sorted((q for q in out if out[q]['status'] == 'matched'),
                    key=lambda q: (out[q]['how'] != 'P2048', -out[q]['score'], int(q[1:]))):
        k = (out[q]['file'], out[q]['id'])
        if k in taken:
            out[q].update({'status': 'rejected', 'reason': f'duplicate: same footprint as {taken[k]} (P2048 first, then higher score)'})
        else:
            taken[k] = q
    for a in ambiguous:                               # an ambiguous item later dropped as a duplicate says so
        if out[a['qid']]['status'] == 'rejected' and out[a['qid']]['reason'].startswith('duplicate'):
            a['resolution'] += '; then ' + out[a['qid']]['reason']
    return out, ambiguous


# ---------------------------------------------------------------------------------------------------- siblings

SIB_RADIUS_M = 300.0
SIB_AREA_RATIO = (0.75, 1.25)            # area of the sibling / area of the target
SIB_SHAPE_RATIO = (0.8, 1.25)            # long side and elongation (long / short side), sibling / target


def complex_ids(lon, lat):
    """Smallest complex area (cmd_complexes) containing each point, as an index into complexes.parquet (-1: none)."""
    t = pq.read_table(os.path.join(EXTRA, 'complexes.parquet'))
    polys = shapely.from_wkb(t['wkb'].to_pylist())
    km2 = t['km2'].to_numpy()
    pi, gi = shapely.STRtree(polys).query(shapely.points(lon, lat), predicate='within')
    out = np.full(len(lon), -1, np.int64)
    o = np.lexsort((-gi, -km2[gi]))                  # largest first (ties: higher index first): the smallest is written last
    out[pi[o]] = gi[o]
    return out


def site_ids(osm_ids):
    t = pq.read_table(os.path.join(EXTRA, 'site_members.parquet'))
    m = {}
    for mem, site in sorted(zip(t['member'].to_pylist(), t['site'].to_pylist())):
        m.setdefault(mem, site)
    return np.array([m.get(o, '') if o else '' for o in osm_ids], object)


def sib_frame(F, names, osm):
    """Adds the link keys (normalised name, indexed name stem, complex area, site relation) and points to a frame."""
    from calibrate_heights import xyz
    ids = F['id'].tolist()
    nn = [norm_name(names.get(i)) for i in ids]
    st = [name_stem(n) for n in nn]
    F['nname'] = np.array([name_core(n) if len(name_core(n)) >= 2 else '' for n in nn], object)   # same-name link: not generic-only
    F['stem'] = np.array([s if ix else '' for s, ix in st], object)
    F['complex'] = complex_ids(F['lon'], F['lat'])
    F['site'] = site_ids([osm.get(i) for i in ids])
    F['xyz'] = xyz(F['lon'], F['lat'])
    F['elong'] = F['length'] / np.maximum(F['width'], 0.1)
    return F


def find_siblings(Tg, Sr, rows=None, src_ok=None):
    """Qualifying siblings of targets (rows of Tg, default all): sources (real heights) within SIB_RADIUS_M, area
    ratio in SIB_AREA_RATIO, long side and elongation ratios in SIB_SHAPE_RATIO, not the target itself, and linked
    by the same indexed name stem (CT1 / CT2, toa A / toa B), the same full OSM name, the same complex area or the same
    site relation (src_ok: only these sources). Returns {target row: [(source index, link kind), ...]} for targets
    with at least one."""
    from scipy.spatial import cKDTree
    rows = np.arange(len(Tg['id'])) if rows is None else np.asarray(rows)
    tree = cKDTree(Sr['xyz'])
    out = {}
    for i, idx in zip(rows.tolist(), tree.query_ball_point(Tg['xyz'][rows], SIB_RADIUS_M)):
        if not idx:
            continue
        idx = np.asarray(sorted(idx))
        idx = idx[(Sr['file'][idx] != Tg['file'][i]) | (Sr['id'][idx] != Tg['id'][i])]
        if src_ok is not None:
            idx = idx[src_ok[idx]]
        ar = Sr['area'][idx] / Tg['area'][i]
        lr = Sr['length'][idx] / max(Tg['length'][i], 0.1)
        er = Sr['elong'][idx] / Tg['elong'][i]
        idx = idx[(ar >= SIB_AREA_RATIO[0]) & (ar <= SIB_AREA_RATIO[1]) & (lr >= SIB_SHAPE_RATIO[0]) & (lr <= SIB_SHAPE_RATIO[1])
                  & (er >= SIB_SHAPE_RATIO[0]) & (er <= SIB_SHAPE_RATIO[1])]
        sib = []
        for j in idx.tolist():
            k = []
            if Tg['stem'][i] and Tg['stem'][i] == Sr['stem'][j]:
                k.append('name_stem')
            elif Tg['nname'][i] and Tg['nname'][i] == Sr['nname'][j]:
                k.append('same_name')
            if Tg['complex'][i] >= 0 and Tg['complex'][i] == Sr['complex'][j]:
                k.append('complex')
            if Tg['site'][i] and Tg['site'][i] == Sr['site'][j]:
                k.append('site')
            if k:
                sib.append((j, '+'.join(k)))
        if sib:
            out[i] = sib
    return out


SIB_COMPLEX_H25_MIN = 25.0               # a link by complex area / site relation only needs the target's raw h25 >= this
SIB_SAT_SIMILAR = 94.0                   # raw h25 of both >= this (saturated): similar


SRC_RATIO = (0.5, 4.0)                   # a source votes only if its own real height / raw h25 is in here (h25 >= 94 m: lower bound)


def source_ok(height, h25):
    """A sibling source may vote: no raw h25 (< 2 m), or real / h25 within SRC_RATIO (saturated h25: lower bound only),
    as the Wikidata cap; a tag of 3.3 m on a building 2.5D sees at 32 m is not copied."""
    if not np.isfinite(h25) or h25 < 2:
        return True
    r = height / h25
    return r >= SRC_RATIO[0] and (h25 >= SIB_SAT_SIMILAR or r <= SRC_RATIO[1])


def _similar(a, b, factor):
    return (a >= SIB_SAT_SIMILAR and b >= SIB_SAT_SIMILAR) or 1 / factor <= a / b <= factor


def sibling_votes(th, sibs, Sr, factor):
    """Siblings (source index, link) of a target with raw h25 `th` that may vote. The source must pass source_ok.
    Target with a raw h25 (>= 2 m): the sibling's raw h25, or its real height when it has no raw h25, within
    [1 / factor, factor] of th (both >= SIB_SAT_SIMILAR: similar); a link by complex area / site only (no name) also
    needs th >= SIB_COMPLEX_H25_MIN. Target without a raw h25: only siblings linked by name (name stem / same name)."""
    out = []
    has_t = np.isfinite(th) and th >= 2
    for j, kind in sibs:
        if not Sr['vote_ok'][j]:
            continue
        named = 'name_stem' in kind or 'same_name' in kind
        if not has_t:
            if named:
                out.append((j, kind))
            continue
        if not named and th < SIB_COMPLEX_H25_MIN:
            continue
        sh = Sr['h25'][j]
        ref = sh if (np.isfinite(sh) and sh >= 2) else Sr['height'][j]
        if _similar(th, ref, factor):
            out.append((j, kind))
    return out


def sibling_value(heights, rule):
    """(height, None) from the voting siblings' heights under a rule {'min_n', 'agree', 'factor'}, or (None, reason):
    fewer than min_n siblings; max / min above agree (siblings disagree). Height = median."""
    if len(heights) < rule['min_n']:
        return None, 'too few voting siblings'
    h = np.asarray(heights, float)
    if rule['agree'] and h.max() / h.min() > rule['agree']:
        return None, 'siblings disagree'
    return float(np.median(h)), None


# ---------------------------------------------------------------------------------------------------- current values

def current_oos(T, P):
    """Out-of-sample published-model value (main + tall stage) of every calibration row (clean osm truth rows with a
    2.5D value): training rows by the inner 5-fold block CV, held-out rows (calibrate_heights' 2 km block hold-out)
    from the training rows, as calibrate_heights fit / tall compute them, with the settings of the parameter file.
    Returns (pred, C, hold, train, test); pred is NaN outside C."""
    import calibrate_heights as CH
    real, h25 = T['real'], T['h25']
    C = T['clean'] & T['has25']; E = T['clean'] & ~T['has25']
    hold = CH.holdout_blocks(T, C)
    train, test = C & ~hold, C & hold
    fold = CH.fold_of(T, train | (E & ~hold))
    spec = CH.CANDIDATES[P['grouping']]
    R, k = P['local']['radius_m'], P['local']['shrink_k']
    T['group'] = CH.assign(T, spec)
    on = np.array([q['on'] for q in P['groups']])
    base = np.full(len(real), np.nan)
    for f in range(CH.INNER_FOLDS):
        tr, te = train & (fold != f), train & (fold == f)
        base[te], _ = CH.predict_cal(T, tr, te, spec, R, k)
    groups = CH.fit_groups(T, train, spec)
    for q, o in zip(groups, on):
        q['on'] = bool(o)
    base[test], _ = CH.predict_cal(T, train, test, spec, R, k, groups)
    st = P['tall']['settings']
    pool = C & ((h25 >= CH.TALL_ANCHOR_H25) | (real >= CH.TALL_ANCHOR_REAL))
    if st['osm_part_anchors']:
        raise NotImplementedError('osm_part tall anchors')
    bw = CH.block_weights(T['block'])
    prov = np.full(len(real), '', object)
    tgt = on[T['group']] & (h25 >= st['threshold_t_m'])
    pred = base.copy()
    radii = tuple(st['radii_m'])
    for anc_m, q_m in [(pool & train & (fold != f), train & (fold == f) & tgt) for f in range(CH.INNER_FOLDS)] + [(pool & train, test & tgt)]:
        ai, qi = np.flatnonzero(anc_m), np.flatnonzero(q_m)
        A = CH.tall_anchors(T['lon'][ai], T['lat'][ai], h25[ai], real[ai], prov[ai], bw[ai])
        al, _, tier, _ = CH.tall_alpha(A, CH.tall_bands(A), T['xyz'][qi], h25[qi], prov[qi], st['min_anchors_n'], st['weights'],
                                       st['saturation_h25_m'], radii, window=st['window'], d0=st['distance_d0_m'],
                                       sim_floor=st['similarity_floor'], alpha_clamp=tuple(st['alpha_clamp']))
        pred[qi] = CH.tall_height(base[qi], h25[qi], al, tier, len(radii), st['lam'], st['fallback'])
    return pred, C, hold, train, test


# ---------------------------------------------------------------------------------------------------- build

WD_RATIO_PCT = 1.0                       # Wikidata height / raw 2.5D outside the [p, 100 - p] percentiles of real / h25 ...
WD_RATIO_BANDS = (0.0, 10.0, 30.0, 60.0, 150.0)   # ... of clean OSM truth rows whose real height is in the band of the claim
WD_NOH25_PCT = 99.0                      # no 2.5D value: claims above this percentile of real heights of clean no-2.5D rows
WD_SLENDER_PCT = 99.5                    # height / sqrt(footprint area) above this percentile of clean truth rows >= 30 m
SIB_FACTOR_GRID = (1.25, 1.5, 1.67, 2.0)   # raw h25 similarity factor tried (capped at 2: a podium is not a tower)
SIB_RULE_GRID = [{'min_n': n, 'agree': ag, 'factor': fa} for n in (1, 2) for ag in (None, 1.5, 1.25) for fa in SIB_FACTOR_GRID]
OSM_WRONG = 0.5                          # OSM looks wrong: OSM height < this x raw h25 and Wikidata is closer to h25


def wd_limits(T, train_side):
    """Plausibility limits of a Wikidata height, from the clean OSM truth rows outside the held-out blocks
    (data-driven, recorded in the output)."""
    C = T['clean'] & T['has25'] & train_side; E = T['clean'] & ~T['has25'] & train_side
    r = T['real'][C] / T['h25'][C]
    edges = list(WD_RATIO_BANDS) + [math.inf]
    bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        s = (T['real'][C] >= lo) & (T['real'][C] < hi)
        bands.append({'real_from_m': lo, 'ratio_min': round(float(np.percentile(r[s], WD_RATIO_PCT)), 3),
                      'ratio_max': round(float(np.percentile(r[s], 100 - WD_RATIO_PCT)), 3), 'n': int(s.sum())})
    tall = T['clean'] & (T['real'] >= 30) & train_side
    return {'ratio_bands': bands, 'no_h25_max_m': round(float(np.percentile(T['real'][E], WD_NOH25_PCT)), 1),
            'slender_max': round(float(np.percentile(T['real'][tall] / np.sqrt(T['area'][tall]), WD_SLENDER_PCT)), 2)}


def wd_plausible(H, h25, area, L, rule):
    """None if a Wikidata height is plausible for the footprint it matched, else the reason (first that applies)."""
    if H / math.sqrt(max(area, 1.0)) > L['slender_max']:
        return f"footprint too small for the height (height / sqrt(area) > {L['slender_max']})"
    if not np.isfinite(h25) or h25 < 2:
        if rule != 'name':
            return 'no raw 2.5D value to check a match without a name'
        if H > L['no_h25_max_m']:
            return f"no 2.5D value and height > {L['no_h25_max_m']} m"
        return None
    r = H / h25
    if r < WD_RATIO_CAP[0] or (h25 < WD_SAT_H25 and r > WD_RATIO_CAP[1]):
        return f"height / raw 2.5D {r:.2f} outside the cap [{WD_RATIO_CAP[0]}, {WD_RATIO_CAP[1]}] (upper bound off for h25 >= {WD_SAT_H25:g} m)"
    b = [x for x in L['ratio_bands'] if H >= x['real_from_m']][-1]
    if not (b['ratio_min'] <= H / h25 <= b['ratio_max']):
        return f"height / raw 2.5D {H / h25:.2f} outside [{b['ratio_min']}, {b['ratio_max']}] (OSM truth, p{WD_RATIO_PCT:g}-p{100 - WD_RATIO_PCT:g})"
    return None


def band_key(lo, hi):
    return f'{lo}-{hi:g} m' if hi < 1e9 else f'>{lo} m'


def by_band(real, before, after, covered):
    """Per real-height band and overall: n, covered, MAE / bias before and after (all rows), and on covered rows."""
    import calibrate_heights as CH
    out = {}
    for lo, hi in [(None, None)] + list(CH.BANDS):
        s = np.ones(len(real), bool) if lo is None else ((real > lo) & (real <= hi) if lo else real <= hi)
        c = s & covered
        out['overall' if lo is None else band_key(lo, hi)] = {
            'n': int(s.sum()), 'covered': int(c.sum()), 'before': CH.metrics(real[s], before[s]), 'after': CH.metrics(real[s], after[s]),
            'covered_before': CH.metrics(real[c], before[c]), 'covered_after': CH.metrics(real[c], after[c])}
    return out


def cmd_build(a):
    import calibrate_heights as CH
    t0 = time.time()
    P = json.load(open(os.path.join(REPO, 'buildings', 'height_calibration.json'), encoding='utf-8'))
    items = wikidata_items()
    names, osm = load_names()
    cands = pq.read_table(os.path.join(EXTRA, 'wd_candidates.parquet')).to_pylist()
    T = CH.truth_frame()
    cur, C, hold, train, test = current_oos(T, P)
    tkey = {(f, int(i)): k for k, (f, i) in enumerate(zip(T['file'].tolist(), T['id'].tolist()))}
    print(f'inputs | {time.time() - t0:.0f}s', flush=True)

    # 1. Wikidata.
    hold_blocks = set(np.unique(T['block'][hold]).tolist())
    L = wd_limits(T, ~np.isin(T['block'], list(hold_blocks)))
    out, amb = match_wikidata(items, cands, names)
    for q, o in out.items():
        if o['status'] != 'matched':
            continue
        k = tkey.get((o['file'], o['id']))
        o['h25'] = (float(T['h25'][k]) if k is not None else np.nan) if o['cand_src'] in CH.TRUTH_SRC else \
            (o['cand_height'] if o['cand_src'] == 'g25' else np.nan)
        why = wd_plausible(o['height_m'], o['h25'], o['area'], L, o['rule'])
        if why:
            o.update({'status': 'rejected', 'reason': 'implausible: ' + why})
    acc = {q: o for q, o in out.items() if o['status'] == 'matched'}
    applied = {q: o for q, o in acc.items() if o['cand_src'] not in CH.TRUTH_SRC}
    # Agreement with OSM where both exist (accepted matches, and every match before the plausibility rule).
    agree = {}
    for nm, sel in (('accepted', acc), ('before_plausibility_rule', {q: o for q, o in out.items() if 'h25' in o})):
        ro = [o for o in sel.values() if o['cand_src'] in CH.TRUTH_SRC]
        wd = np.array([o['height_m'] for o in ro]); rl = np.array([o['cand_height'] for o in ro]); hh = np.array([o['h25'] for o in ro])
        wrong = (rl < OSM_WRONG * hh) & (np.abs(np.log(wd / hh)) < np.abs(np.log(rl / hh)))
        ks = [tkey.get((o['file'], o['id'])) for o in ro]
        inC = np.array([k is not None and bool(C[k]) for k in ks])
        cv = np.array([cur[k] if k is not None else np.nan for k in ks])
        ok = ~wrong
        agree[nm] = {'n': len(ro), 'osm_looks_wrong': int(wrong.sum()), 'wikidata_vs_osm': CH.metrics(rl[ok], wd[ok]),
                     'calibration_rows': {'n': int((ok & inC).sum()), 'wikidata_vs_osm': CH.metrics(rl[ok & inC], wd[ok & inC]),
                                          'current_model_vs_osm': CH.metrics(rl[ok & inC], cv[ok & inC])},
                     'outliers_over_1.5x': [{'qid': o['qid'], 'label': o['label'], 'wikidata_m': o['height_m'], 'osm_m': o['cand_height'],
                                             'osm_src': o['cand_src'], 'raw_h25_m': round(float(o['h25']), 1), 'how': o['how'],
                                             'osm_looks_wrong': bool(w)}
                                            for o, w in zip(ro, wrong) if abs(math.log(o['height_m'] / o['cand_height'])) > math.log(1.5)]}
        if nm == 'accepted':
            osm_wrong = [{'qid': o['qid'], 'label': o['label'], 'file': o['file'], 'id': o['id'], 'osm_m': o['cand_height'],
                          'osm_src': o['cand_src'], 'wikidata_m': o['height_m'], 'raw_h25_m': round(float(o['h25']), 1)}
                         for o, w in zip(ro, wrong) if w]
    print(f'wikidata: matched {len(acc)}, applied {len(applied)} | {time.time() - t0:.0f}s', flush=True)

    # 2. Siblings: sources are clean-or-part truth rows (not junk, not in a mass-tagged block) and applied Wikidata rows.
    srcm = ~T['junk'] & ~T['uniform']
    si = np.flatnonzero(srcm)
    wl = sorted(applied.values(), key=lambda o: (o['file'], o['id']))
    S = {'file': np.r_[T['file'][si].astype(object), np.array([o['file'] for o in wl], object)],
         'id': np.r_[T['id'][si].astype(np.int64), np.array([o['id'] for o in wl], np.int64)],
         'lon': np.r_[T['lon'][si], [o['lon'] for o in wl]], 'lat': np.r_[T['lat'][si], [o['lat'] for o in wl]],
         'area': np.r_[T['area'][si], [o['area'] for o in wl]], 'length': np.r_[T['length'][si], [o['length'] for o in wl]],
         'width': np.r_[T['width'][si], [o['width'] for o in wl]], 'height': np.r_[T['real'][si], [o['height_m'] for o in wl]],
         'floors': np.r_[T['floors'][si], [o['floors'] for o in wl]].astype(float),
         'h25': np.r_[T['h25'][si], [o['h25'] for o in wl]].astype(float),
         'kind': np.r_[T['src'][si].astype(object), np.array(['wikidata'] * len(wl), object)],
         'truth': np.r_[si, np.full(len(wl), -1)]}
    S = sib_frame(S, names, osm)
    S['train_side'] = np.array([CH.block_keys(np.array([lo]), np.array([la]))[0] not in hold_blocks
                                for lo, la in zip(S['lon'], S['lat'])]) | (S['kind'] == 'wikidata')
    S['vote_ok'] = np.array([source_ok(h, x) for h, x in zip(S['height'], S['h25'])])
    print(f'sibling sources {len(S["id"])} (may vote {int(S["vote_ok"].sum())}) | {time.time() - t0:.0f}s', flush=True)
    # Eligible truth rows (validation targets): calibration rows with raw h25 >= SIB_H25_MIN or area >= SIB_AREA_MIN.
    tr_of = {int(t): j for j, t in enumerate(S['truth'].tolist()) if t >= 0}
    elig = C & ((T['h25'] >= SIB_H25_MIN) | (T['area'] >= SIB_AREA_MIN))
    ei = np.flatnonzero(elig)
    erow = np.array([tr_of[int(k)] for k in ei])
    loo = find_siblings(S, S, erow)
    real = T['real']

    def predict(sib_map, rows, rule):
        v = np.full(len(rows), np.nan); why = {}
        for n, r in enumerate(rows.tolist()):
            if r in sib_map:
                vs = sibling_votes(S['h25'][r], sib_map[r], S, rule['factor'])
                h, reason = sibling_value([S['height'][j] for j, _ in vs], rule)
                if h is not None:
                    v[n] = h
                else:
                    why[reason] = why.get(reason, 0) + 1
        return v, why
    sel = {}
    tr_e = train[ei]
    base_mae = float(np.abs(cur[ei][tr_e] - real[ei][tr_e]).mean())
    best = None
    loo_sel = find_siblings(S, S, erow, src_ok=S['train_side'])     # selection: training-side sources only
    for rule in SIB_RULE_GRID:
        v, _ = predict(loo_sel, erow, rule)
        p = np.where(np.isfinite(v), v, cur[ei])
        mae = float(np.abs(p[tr_e] - real[ei][tr_e]).mean())
        key = f"min_n={rule['min_n']} agree={rule['agree']} factor={rule['factor']}"
        sel[key] = {'mae_m': round(mae, 4), 'covered': int(np.isfinite(v)[tr_e].sum())}
        if best is None or mae < best[0]:
            best = (mae, rule, key)
    sib_on = best[0] < base_mae
    rule = best[1]
    print(f'sibling rule {best[2]}: LOO train MAE {best[0]:.3f} vs current {base_mae:.3f} | {time.time() - t0:.0f}s', flush=True)
    v, why_loo = predict(loo, erow, rule)
    cov = np.isfinite(v)
    loo_val = by_band(real[ei], cur[ei], np.where(cov, v, cur[ei]), cov)
    links = {}
    for n, r in enumerate(erow.tolist()):
        if cov[n]:
            for _, k in loo[r]:
                links[k] = links.get(k, 0) + 1
    # Hold-out: held-out calibration rows; Wikidata (independent of OSM) first, then siblings from training-side sources.
    hi_ = np.flatnonzero(test)
    wd_by_key = {(o['file'], o['id']): o for o in acc.values()}
    after = cur[hi_].copy(); how = np.full(len(hi_), '', object)
    for n, k in enumerate(hi_.tolist()):
        o = wd_by_key.get((T['file'][k], int(T['id'][k])))
        if o is not None:
            after[n] = o['height_m']; how[n] = 'wikidata'
    he = np.array([k for k in hi_.tolist() if elig[k]])
    hsib = find_siblings(S, S, np.array([tr_of[int(k)] for k in he]), src_ok=S['train_side'])
    hv, _ = predict(hsib, np.array([tr_of[int(k)] for k in he]), rule) if sib_on else (np.full(len(he), np.nan), {})
    pos = {k: n for n, k in enumerate(hi_.tolist())}
    for k, h in zip(he.tolist(), hv.tolist()):
        if np.isfinite(h) and how[pos[k]] == '':
            after[pos[k]] = h; how[pos[k]] = 'sibling'
    hold_val = {'all': by_band(real[hi_], cur[hi_], after, how != ''),
                'wikidata_rows': by_band(real[hi_], cur[hi_], after, how == 'wikidata'),
                'sibling_rows': by_band(real[hi_], cur[hi_], after, how == 'sibling'),
                'note': 'before = the published model (main + tall stage) on the 2 km block hold-out of calibrate_heights; after = '
                        'Wikidata where an accepted item matches the row, else a sibling copy from training-side real heights '
                        '(osm / osm_part rows outside the held-out blocks, applied Wikidata rows), else before'}
    print(f'hold-out: wikidata {int((how == "wikidata").sum())}, sibling {int((how == "sibling").sum())} of {len(hi_)} | {time.time() - t0:.0f}s', flush=True)

    # 3. Published sibling rows: every target (raw layer, no real height, not an applied Wikidata row).
    tg = pq.read_table(os.path.join(EXTRA, 'targets.parquet')).to_pydict()
    wkeys = {(o['file'], o['id']) for o in applied.values()}
    keep = np.array([(f, i) not in wkeys for f, i in zip(tg['file'], tg['id'])])
    Tg = {k: np.asarray(v, object if k in ('file', 'src') else None)[keep] for k, v in tg.items()}
    Tg['id'] = Tg['id'].astype(np.int64)
    Tg = sib_frame(Tg, names, osm)
    tsib = find_siblings(Tg, S) if sib_on else {}
    srows, rej = [], {}
    for i in sorted(tsib):
        vs = sibling_votes(Tg['h25'][i], tsib[i], S, rule['factor'])
        h, reason = sibling_value([S['height'][j] for j, _ in vs], rule)
        if h is None:
            rej[reason] = rej.get(reason, 0) + 1
            continue
        fl = [int(S['floors'][j]) for j, _ in vs if S['floors'][j] >= 1]
        F = _lower_median(fl) if fl else int(CH.floors_of([h])[0])
        js = sorted(vs, key=lambda t: (S['file'][t[0]], S['id'][t[0]]))
        srows.append({'file': Tg['file'][i], 'id': int(Tg['id'][i]), 'height_dm': int(CH.dm_of([h])[0]), 'floors': int(F),
                      'n': len(js), 'link': sorted({k for _, k in js}), 'siblings': [[S['file'][j], int(S['id'][j]), S['kind'][j]] for j, _ in js],
                      'raw_src': Tg['src'][i] or None, 'raw_h25_m': None if not np.isfinite(Tg['h25'][i]) else round(float(Tg['h25'][i]), 1),
                      'area_m2': round(float(Tg['area'][i]), 1), 'lon': round(float(Tg['lon'][i]), 6), 'lat': round(float(Tg['lat'][i]), 6)})
    print(f'sibling rows {len(srows)} (targets with siblings {len(tsib)}, rejected {rej}) | {time.time() - t0:.0f}s', flush=True)

    # 4. Output.
    wrows = [{'file': o['file'], 'id': o['id'], 'qid': o['qid'], 'height_dm': int(CH.dm_of([o['height_m']])[0]), 'floors': int(o['floors']),
              'how': o['how'], 'rule': o['rule'], 'label': o['label'], 'raw_src': o['cand_src'] or None,
              'raw_h25_m': None if not np.isfinite(o['h25']) else round(float(o['h25']), 1), 'area_m2': o['area'],
              'lon': o['lon'], 'lat': o['lat']} for o in sorted(applied.values(), key=lambda o: (o['file'], o['id']))]
    reasons = {}
    for o in out.values():
        if o['status'] == 'rejected':
            r = o['reason']
            r = ('implausible: ' + re.split(r' -?\d+\.\d+ outside| \(| >', r[len('implausible: '):])[0].strip()
                 if r.startswith('implausible') else re.split(r'[:(]', r)[0].strip())
            reasons[r] = reasons.get(r, 0) + 1
    fetched = sorted(glob.glob(os.path.join(WD_DIR, '*.json')))
    small = lambda n: sum(1 for w in wrows if (w['raw_h25_m'] or 0) < SIB_H25_MIN and w['area_m2'] < SIB_AREA_MIN)
    R = {
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'precedence': ['osm', 'osm_part', 'wikidata', 'sibling', 'g25cal', 'est'],
        'apply': 'calibrate_heights.py apply reads the rows below (file, id): after the calibration of a raw file, a row '
                 'listed under wikidata whose raw height_src is not osm / osm_part gets the Wikidata height and floors '
                 '(height_src wikidata); a row listed under sibling (never osm / osm_part / wikidata) gets the sibling '
                 'height and floors (height_src sibling). Rebuild this file with buildings/extra_heights.py build.',
        'wikidata': {
            'source': 'Wikidata (CC0 1.0), https://query.wikidata.org/sparql, cached raw results in data/buildings/_wikidata/',
            'fetched_at': min(json.load(open(f, encoding='utf-8'))['fetched_at'] for f in fetched) if fetched else None,
            'queries': {'items': [Q_LIST_COUNTRY, Q_LIST_BOX], 'details': [Q_COORD, Q_HEIGHT, Q_FLOORS, Q_INFO, Q_STATE, Q_CLASS, Q_EVENT, Q_ALIAS]},
            'rules': {
                'value': wd_value.__doc__.split('\n', 1)[1].strip().replace('\n    ', ' '),
                'item': ['not a building class (P31/P279* Q41176), with or without floors',
                         f'a direct P31 class naming {list(WD_ORG_WORDS)}', f'a label word in {list(WD_VESSEL_WORDS)} (floating hotels)',
                         'no coordinates',
                         f'coordinate precision coarser than {WD_PREC_MAX} deg',
                         f'P5817 state of use naming {"/".join(WD_STATE_WORDS)}', f'opened / inception after the map extract ({PBF_DATE})',
                         'P576 dissolved / demolished', f'height outside {list(WD_H_RANGE)} m'],
                'candidates': f'footprints of the raw layer within {WD_RADIUS_M:g} m of the item point (distance to the footprint, 0 inside)',
                'score': f"{WD_W['name']:g} x name match (similarity >= {NAME_MATCH} with the footprint's OSM name; name_sim: difflib ratio "
                    f"without generic words, 0.9 for whole-word containment) {WD_W['name_conflict']:+g} if the footprint has an OSM name "
                    f"with similarity < {NAME_CONFLICT} + {WD_W['contains']:g} if it contains the point + {WD_W['size']:g} x size "
                    f"(log scale, 0 at 100 m2 .. 1 at 2000 m2) + {WD_W['tall']:g} if its own raw height >= min(H / 2, 30 m) - distance / 25 m",
                'pick': f'highest score (ties: nearer, file, id); rejected if no candidate or best score < 0; runner-up within '
                        f'{WD_AMBIG_REJECT} and no name match: rejected as ambiguous; runner-up within {WD_AMBIG_LIST}: listed, best kept; '
                        'two items on one footprint: the one with P2048 first, then higher score, then lower QID',
                'plausibility': {'rule': f'height / sqrt(area) <= slender_max (p{WD_SLENDER_PCT:g} of clean OSM rows >= 30 m); '
                                         f'with a raw 2.5D value: height / h25 within the cap {list(WD_RATIO_CAP)} (tall-stage alpha clamp; '
                                         f'upper bound off for h25 >= {WD_SAT_H25:g} m) and within the p{WD_RATIO_PCT:g}-p{100 - WD_RATIO_PCT:g} range of '
                                         'real / h25 of clean OSM rows in the real-height band of the claim; without a raw 2.5D value: only '
                                         f'name matches, height <= p{WD_NOH25_PCT:g} of real heights of clean no-2.5D OSM rows', 'limits': L,
                                 'why_cap': 'the percentile band alone let 363 m on h25 42.6 m (8.5x) through: the >= 150 m band holds only '
                                            '64 tagged towers, which 2.5D under-reads up to 13.8x (p99), so the band limit was 13.77'},
                'name_conflict': f'the best footprint has an OSM name with similarity < {NAME_CONFLICT_REJECT} to every vi / en label or alias: rejected',
                'unbuilt': f'P31/P279* ancestor label containing {list(WD_CLASS_PHRASES)}; P5817 label containing {list(WD_STATE_WORDS)}; '
                           f'P793 event containing {list(WD_EVENT_WORDS)}; P576 present',
                'never_overrides': 'osm, osm_part (disagreements are reported, not changed)'},
            'counts': {'items_fetched': len(items), 'with_height_p2048': sum(1 for i in items.values() if i['heights']),
                       'with_floors_p1101': sum(1 for i in items.values() if i['floors']),
                       'matched_accepted': len(acc), 'matched_on_osm_rows': len(acc) - len(applied), 'applied_rows': len(applied),
                       'applied_on_small_rows (raw h25 < 25 m and area < 400 m2)': small(0),
                       'by_rule': {r: sum(1 for o in acc.values() if o['rule'] == r) for r in ('name', 'contains', 'nearest')},
                       'ambiguous_listed': len(amb), 'rejected': sum(1 for o in out.values() if o['status'] == 'rejected'),
                       'rejected_by_reason': dict(sorted(reasons.items(), key=lambda t: -t[1]))},
            'osm_agreement': agree, 'osm_looks_wrong': osm_wrong,
            'ambiguous': amb,
            'rejected': [{'qid': o['qid'], 'label': o['label'], 'reason': o['reason']} for o in sorted(out.values(), key=lambda o: int(o['qid'][1:]))
                         if o['status'] == 'rejected'],
        },
        'sibling': {
            'rules': {'targets': f'rows without a real height (not osm / osm_part / wikidata) with raw h25 >= {SIB_H25_MIN:g} m or footprint >= {SIB_AREA_MIN:g} m2',
                      'sources': 'osm / osm_part rows that are not junk and not in a mass-tagged block (calibrate_heights truth filters), and applied Wikidata rows',
                      'candidate': f'within {SIB_RADIUS_M:g} m (centroids), area ratio in {list(SIB_AREA_RATIO)}, long side and elongation ratios in {list(SIB_SHAPE_RATIO)}',
                      'link': 'same indexed name stem (CT1/CT2, Toa A/Toa B, Block 3/Block 4; the letters of the index stay in the stem), the same full '
                              f"OSM name, the same smallest OSM complex area (landuse {'/'.join(COMPLEX_LANDUSE)}, residential=*, amenity "
                              f"{'/'.join(COMPLEX_AMENITY)}, <= {COMPLEX_MAX_KM2} km2, holding the centroid) or the same type=site relation",
                      'vote': ' '.join(sibling_votes.__doc__.split()),
                      'value': 'median of the qualifying siblings\' heights; floors: lower median of their floors',
                      'chosen': rule, 'on': bool(sib_on),
                      'selection': {'criterion': 'LOO MAE over eligible training-block calibration rows, siblings from training-side sources only '
                                                 '(rows without a sibling keep the current value)',
                                    'current_mae_m': round(base_mae, 4), 'grid': sel}},
            'counts': {'targets_scanned': int(len(tg['id'])), 'targets_with_siblings': len(tsib), 'rows': len(srows),
                       'rejected_by_rule': rej, 'by_link': {k: sum(1 for r in srows if k in '+'.join(r['link'])) for k in ('name_stem', 'same_name', 'complex', 'site')},
                       'by_source_kind': {k: sum(1 for r in srows if any(x[2] == k for x in r['siblings'])) for k in ('osm', 'osm_part', 'wikidata')}},
            'leave_one_out': {'rows': 'calibration rows (clean OSM, with a 2.5D value) meeting the target rule, each predicted from its siblings without itself',
                              'eligible': int(len(ei)), 'covered': int(cov.sum()), 'not_covered_by_reason': why_loo, 'links': links,
                              'current': 'out-of-sample value of the published model (inner block CV for training rows, hold-out for held-out rows)',
                              'per_band': loo_val},
        },
        'holdout': hold_val,
    }
    write_sources(a.out, R, wrows, srows)
    print(f'wrote {a.out}: wikidata rows {len(wrows)}, sibling rows {len(srows)} | {time.time() - t0:.0f}s')
    print(json.dumps({'wikidata': R['wikidata']['counts'], 'agree': {k: {kk: vv for kk, vv in v.items() if kk != 'outliers_over_1.5x'} for k, v in agree.items()},
                      'sibling': R['sibling']['counts'], 'loo': loo_val, 'holdout': hold_val['all']}, indent=1, ensure_ascii=False))


ROW_COLS = {'wikidata': ['file', 'id', 'qid', 'height_dm', 'floors', 'how', 'rule', 'label', 'raw_src', 'raw_h25_m', 'area_m2', 'lon', 'lat'],
            'sibling': ['file', 'id', 'height_dm', 'floors', 'n', 'link', 'raw_src', 'raw_h25_m', 'area_m2', 'lon', 'lat', 'siblings']}


def write_sources(path, R, wrows, srows):
    """height_sources.json: the report, then the rows as column lists + one array per row (one line each)."""
    R = dict(R)
    R['rows'] = '__ROWS__'
    txt = json.dumps(R, ensure_ascii=False, indent=1)
    parts = []
    for nm, rows in (('wikidata', wrows), ('sibling', srows)):
        cols = ROW_COLS[nm]
        body = ',\n'.join('   ' + json.dumps([r[c] for c in cols], ensure_ascii=False, separators=(',', ':')) for r in rows)
        parts.append(f'  "{nm}": {{\n   "columns": {json.dumps(cols)},\n   "data": [\n{body}\n   ]\n  }}')
    txt = txt.replace('"__ROWS__"', '{\n' + ',\n'.join(parts) + '\n }')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(txt + '\n')


def load_sources(path):
    """{file: {'wikidata': {id: (height_dm, floors)}, 'sibling': {id: (height_dm, floors)}}} from height_sources.json."""
    R = json.load(open(path, encoding='utf-8'))
    out = {}
    for nm in ('wikidata', 'sibling'):
        cols = R['rows'][nm]['columns']
        fi, ii, hi, fl = (cols.index(c) for c in ('file', 'id', 'height_dm', 'floors'))
        for r in R['rows'][nm]['data']:
            out.setdefault(r[fi], {'wikidata': {}, 'sibling': {}})[nm][int(r[ii])] = (int(r[hi]), int(r[fl]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('fetch'); sub.add_parser('names'); sub.add_parser('complexes')
    p = sub.add_parser('scan')
    p.add_argument('--raw-dir', default=RAW_DIR)
    p.add_argument('--workers', type=int, default=6)
    p = sub.add_parser('build')
    p.add_argument('--raw-dir', default=RAW_DIR)
    p.add_argument('--out', default=OUT_JSON)
    p.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()
    {'fetch': cmd_fetch, 'names': cmd_names, 'complexes': cmd_complexes, 'scan': cmd_scan, 'build': cmd_build}[a.cmd](a)


if __name__ == '__main__':
    main()
