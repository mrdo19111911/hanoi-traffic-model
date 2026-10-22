"""Download the Vietnam partitions of the VIDA Google-Microsoft Open Buildings GeoParquet.

Source: https://data.source.coop/vida/google-microsoft-open-buildings/ (by_country_s2, country_iso=VNM).
Re-runnable: files already complete (same size as the listing) are skipped, partial files are resumed.

    .venv/Scripts/python.exe buildings/fetch_open_buildings.py [--out data/open_buildings/vnm] [--workers 4]
"""
import argparse, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

BUCKET = "https://data.source.coop/vida"
PREFIX = "google-microsoft-open-buildings/geoparquet/by_country_s2/country_iso=VNM/"
UA = {"User-Agent": "hanoi-traffic-model/buildings (urllib)"}


def listing():
    xml = urllib.request.urlopen(urllib.request.Request(f"{BUCKET}?list-type=2&prefix={PREFIX}", headers=UA), timeout=60).read().decode()
    return [(k, int(s)) for k, s in re.findall(r"<Key>([^<]+\.parquet)</Key>.*?<Size>(\d+)</Size>", xml)]


def fetch(key, size, out, attempts=10):
    path = os.path.join(out, os.path.basename(key))
    for n in range(attempts):
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have == size:
            return key, size, 'skip' if n == 0 else 'ok'
        req = urllib.request.Request(f"{BUCKET}/{key}", headers={**UA, **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(path, 'ab' if have else 'wb') as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        except (OSError, urllib.error.URLError) as e:   # network drop: resume from what is on disk
            print(f"retry {n + 1} {os.path.basename(key)} at {have / 1e6:.0f} MB: {type(e).__name__}", flush=True)
            time.sleep(min(60, 5 * (n + 1)))
    got = os.path.getsize(path) if os.path.exists(path) else 0
    raise RuntimeError(f"{key}: got {got} bytes, expected {size}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/open_buildings/vnm')
    ap.add_argument('--workers', type=int, default=4)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    files = listing()
    total = sum(s for _, s in files)
    print(f"{len(files)} files, {total / 1e9:.2f} GB", flush=True)
    t, done = time.time(), 0
    with ThreadPoolExecutor(a.workers) as ex:
        for key, size, st in ex.map(lambda ks: fetch(*ks, a.out), sorted(files, key=lambda x: -x[1])):
            done += size
            print(f"{st:4s} {os.path.basename(key)} {size / 1e6:8.1f} MB | {done / total:6.1%} | {time.time() - t:6.0f} s", flush=True)
    print('done', flush=True)


if __name__ == '__main__':
    sys.exit(main())
