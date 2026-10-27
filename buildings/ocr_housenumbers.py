"""Measurement only: OCR (RapidOCR, PaddleOCR models on onnxruntime) of street images -> text boxes. Run on the 3090.

    python buildings/ocr_housenumbers.py --images DIR --out FILE.json
"""
import argparse, glob, json, os, time

from rapidocr_onnxruntime import RapidOCR

ap = argparse.ArgumentParser(); ap.add_argument('--images', required=True); ap.add_argument('--out', required=True)
a = ap.parse_args()
eng = RapidOCR()
res = {}
t0 = time.time()
for p in sorted(glob.glob(os.path.join(a.images, '*.jpg'))):
    r, _ = eng(p)
    res[os.path.basename(p)[:-4]] = [dict(box=[[float(x), float(y)] for x, y in b], text=t, conf=float(c)) for b, t, c in (r or [])]
json.dump(res, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
print('images', len(res), 'boxes', sum(len(v) for v in res.values()), 'sec', round(time.time() - t0))
