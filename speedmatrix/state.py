"""SQLite state store: rules with history, vehicle groups, holidays, speed versions and jobs.

Schema 2 adds the ``jobs`` table (one row per pipeline run, Decision 21); a schema-1 file is upgraded in place.
"""
import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from .rules import Rule, rule_from_dict, rule_to_dict, validate_rule
    from .vehicles import VehicleGroup
except ImportError:  # pragma: no cover - flat import
    from rules import Rule, rule_from_dict, rule_to_dict, validate_rule
    from vehicles import VehicleGroup

SCHEMA = 2
_TZ = timezone(timedelta(hours=7))

_DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS rules(
  id TEXT PRIMARY KEY, kind TEXT, layer TEXT, body TEXT, confidence TEXT,
  active INTEGER, source TEXT, document TEXT, note TEXT,
  valid_from TEXT, valid_to TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS unresolved(area TEXT, item TEXT);
CREATE TABLE IF NOT EXISTS rule_history(
  hid INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT, at TEXT, author TEXT,
  reason TEXT, action TEXT, snapshot TEXT);
CREATE INDEX IF NOT EXISTS rule_history_rule ON rule_history(rule_id);
CREATE TABLE IF NOT EXISTS vehicle_groups(name TEXT PRIMARY KEY, attrs TEXT, is_base INTEGER);
CREATE TABLE IF NOT EXISTS holidays(day TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS speed_versions(version TEXT PRIMARY KEY, created_at TEXT, info TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT, status TEXT, started_at TEXT, finished_at TEXT,
  info TEXT, error TEXT, log_path TEXT);
"""


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


class State:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    # ---- rules
    def _row_to_rule(self, row) -> Rule:
        d = {k: row[k] for k in row.keys() if k != "updated_at"}
        d["body"] = json.loads(d["body"])
        d["active"] = bool(d["active"])
        return rule_from_dict(d)

    def _get(self, rule_id: str):
        row = self._db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return self._row_to_rule(row) if row else None

    def _write(self, rule: Rule, author: str, reason: str, action: str) -> None:
        d = rule_to_dict(rule)
        now = _now()
        self._db.execute(
            "INSERT OR REPLACE INTO rules(id, kind, layer, body, confidence, active, source, document,"
            " note, valid_from, valid_to, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (rule.id, rule.kind, rule.layer, _dump(rule.body), rule.confidence, int(rule.active),
             rule.source, rule.document, rule.note, d["valid_from"], d["valid_to"], now))
        self._db.execute(
            "INSERT INTO rule_history(rule_id, at, author, reason, action, snapshot) VALUES(?,?,?,?,?,?)",
            (rule.id, now, author, reason, action, _dump(d)))
        self._db.commit()

    def upsert_rule(self, rule: Rule, author: str, reason: str) -> None:
        validate_rule(rule)
        old = self._get(rule.id)
        if old is not None and _dump(rule_to_dict(old)) == _dump(rule_to_dict(rule)):
            return
        self._write(rule, author, reason, "update" if old else "create")

    def set_active(self, rule_id: str, active: bool, author: str, reason: str) -> None:
        old = self._get(rule_id)
        if old is None:
            raise KeyError(rule_id)
        if old.active == bool(active):
            return
        old.active = bool(active)
        self._write(old, author, reason, "activate" if active else "deactivate")

    def rules(self, active_only: bool = True, kinds=None, as_of: date | None = None) -> list:
        sql, args = "SELECT * FROM rules", []
        where = []
        if active_only:
            where.append("active=1")
        if kinds:
            where.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            args += list(kinds)
        if where:
            sql += " WHERE " + " AND ".join(where)
        out = [self._row_to_rule(r) for r in self._db.execute(sql + " ORDER BY id", args)]
        if as_of is not None:
            out = [r for r in out if r.in_force(as_of)]
        return out

    def history(self, rule_id: str) -> list:
        rows = self._db.execute(
            "SELECT * FROM rule_history WHERE rule_id=? ORDER BY hid", (rule_id,)).fetchall()
        return [{"hid": r["hid"], "rule_id": r["rule_id"], "at": r["at"], "author": r["author"],
                 "reason": r["reason"], "action": r["action"], "snapshot": json.loads(r["snapshot"])}
                for r in rows]

    def revert(self, rule_id: str, history_id: int, author: str) -> Rule:
        row = self._db.execute(
            "SELECT snapshot FROM rule_history WHERE hid=? AND rule_id=?", (history_id, rule_id)).fetchone()
        if row is None:
            raise KeyError(f"history {history_id} of rule {rule_id}")
        rule = rule_from_dict(json.loads(row["snapshot"]))
        validate_rule(rule)
        self._write(rule, author, f"revert to history {history_id}", "revert")
        return rule

    # ---- unresolved
    def add_unresolved(self, area: str, items: list) -> None:
        self._db.execute("DELETE FROM unresolved WHERE area=?", (area,))
        self._db.executemany("INSERT INTO unresolved(area, item) VALUES(?,?)",
                             [(area, _dump(i)) for i in items])
        self._db.commit()

    def unresolved(self) -> list:
        out = []
        for r in self._db.execute("SELECT rowid, area, item FROM unresolved ORDER BY rowid"):
            item = json.loads(r["item"])
            item.setdefault("area", r["area"])
            out.append(item)
        return out

    # ---- vehicle groups
    def upsert_group(self, group: VehicleGroup) -> None:
        attrs = asdict(group)
        attrs.pop("name")
        attrs.pop("is_base")
        if group.is_base:
            self._db.execute("UPDATE vehicle_groups SET is_base=0")
        self._db.execute("INSERT OR REPLACE INTO vehicle_groups(name, attrs, is_base) VALUES(?,?,?)",
                         (group.name, _dump(attrs), int(group.is_base)))
        self._db.commit()

    def groups(self) -> list:
        return [VehicleGroup(name=r["name"], is_base=bool(r["is_base"]), **json.loads(r["attrs"]))
                for r in self._db.execute("SELECT * FROM vehicle_groups ORDER BY name")]

    # ---- holidays
    def add_holiday(self, day: date, name: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO holidays(day, name) VALUES(?,?)", (day.isoformat(), name))
        self._db.commit()

    def holidays(self) -> set:
        return {date.fromisoformat(r["day"]) for r in self._db.execute("SELECT day FROM holidays")}

    # ---- speed versions
    def record_speed_version(self, version: str, info: dict) -> None:
        self._db.execute("INSERT OR REPLACE INTO speed_versions(version, created_at, info) VALUES(?,?,?)",
                         (version, _now(), _dump(info)))
        self._db.commit()

    def speed_versions(self) -> list:
        return [{"version": r["version"], "created_at": r["created_at"], "info": json.loads(r["info"])}
                for r in self._db.execute("SELECT * FROM speed_versions ORDER BY created_at, version")]

    # ---- jobs
    def start_job(self, id: str, kind: str, log_path) -> None:
        """A new job row with status ``running``; an existing id raises sqlite3.IntegrityError."""
        self._db.execute("INSERT INTO jobs(id, kind, status, started_at, log_path) VALUES(?,?,?,?,?)",
                         (id, kind, "running", _now(), None if log_path is None else str(log_path)))
        self._db.commit()

    def finish_job(self, id: str, status: str, info: dict, error: str | None = None) -> None:
        cur = self._db.execute("UPDATE jobs SET status=?, finished_at=?, info=?, error=? WHERE id=?",
                               (status, _now(), _dump(info), error, id))
        self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(f"job {id}")

    @staticmethod
    def _job(row) -> dict:
        d = dict(row)
        d["info"] = json.loads(d["info"]) if d["info"] is not None else None
        return d

    def job(self, id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM jobs WHERE id=?", (id,)).fetchone()
        return self._job(row) if row else None

    def jobs(self, kind: str | None = None, limit: int = 20) -> list:
        """The newest ``limit`` jobs (of ``kind`` when given), newest first."""
        sql, args = "SELECT * FROM jobs", []
        if kind is not None:
            sql += " WHERE kind=?"
            args.append(kind)
        args.append(int(limit))
        return [self._job(r) for r in self._db.execute(sql + " ORDER BY started_at DESC, rowid DESC LIMIT ?", args)]

    def close(self) -> None:
        self._db.close()


def open_state(path: Path) -> State:
    """Open (creating if needed) the state DB; upgrade an older schema in place, refuse a newer one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        has_meta = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if has_meta:
            row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row and int(row["value"]) > SCHEMA:
                raise RuntimeError(f"state schema {row['value']} is newer than supported {SCHEMA}")
        db.executescript(_DDL)                  # CREATE ... IF NOT EXISTS: adds the tables a schema-1 file lacks
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA),))
        db.commit()
    except Exception:
        db.close()
        raise
    return State(db)
