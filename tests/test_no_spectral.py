"""Spec 22.3: spectral clustering is gone from the code; only the province and natural (river) regions remain."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ["model", "download", "backend", "app.py", "osrm/manage.py", "frontend/src", "tests"]
SUFFIXES = {".py", ".ts", ".tsx", ".css", ".js", ".json", ".html"}
WORD = re.compile("spec" + "tral", re.IGNORECASE)  # split so this file never matches itself either


class NoSpectralTests(unittest.TestCase):
    def test_no_source_file_mentions_it(self):
        hits = []
        for entry in SCANNED:
            path = ROOT / entry
            files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file() and p.suffix in SUFFIXES
                                                   and "__pycache__" not in p.parts]
            for file in files:
                if file.resolve() == Path(__file__).resolve():
                    continue
                text = file.read_text(encoding="utf-8", errors="replace")
                hits += [f"{file.relative_to(ROOT)}:{n}" for n, line in enumerate(text.splitlines(), 1) if WORD.search(line)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
