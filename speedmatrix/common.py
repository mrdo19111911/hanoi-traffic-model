"""Shared by every speedmatrix module: repo paths, small file helpers and the ``osrm/manage`` import.

Spec:
- ``REPO`` is the checkout root, ``DEFAULT_ROOT`` the default ``--root`` (``runtime/matrix``).
- ``import_osrm_manage()`` returns ``osrm/manage.py`` as the module ``manage`` (one import, cached in
  ``sys.modules``) without leaving ``osrm/`` on ``sys.path``.
- ``write_json`` writes ``indent=1``, ``ensure_ascii=False``, LF, UTF-8 through a ``.tmp`` sibling and
  ``os.replace``: the old file survives a failed dump. ``default`` is opt-in (the update pipeline passes ``str``).
- ``read_json`` raises on a missing file; ``read_json_or_none`` returns ``None``.
- ``sha256_file`` hashes a file; ``open_sqlite_ro`` opens an existing SQLite file read-only with ``sqlite3.Row``.

Modules are run as ``python -m speedmatrix.<module>`` from the repo root; only ``update.py`` also runs as a
script (the scheduled task) and bootstraps itself.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO / "runtime" / "matrix"


def import_osrm_manage():
    """``osrm/manage.py`` as the module ``manage`` (process identity, OSRM paths, Geofabrik URL)."""
    if "manage" in sys.modules:
        return sys.modules["manage"]
    osrm_dir = str(REPO / "osrm")
    sys.path.insert(0, osrm_dir)
    try:
        return importlib.import_module("manage")
    finally:
        sys.path.remove(osrm_dir)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_json_or_none(path):
    path = Path(path)
    return read_json(path) if path.is_file() else None


def write_json(path, doc, *, default=None) -> None:
    """Write to a temp sibling and rename it into place."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=default), encoding="utf-8", newline="\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def sha256_file(path) -> str:
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def open_sqlite_ro(path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db
