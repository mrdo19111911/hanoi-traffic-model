"""Rule model, validation and seed-file loading."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

try:
    from .segments import ROAD_CLASSES
    from .timeslots import when_mask
    from .vehicles import ATTRS, KINDS, OPS, VehicleGroup, cond_pairs
except ImportError:  # pragma: no cover - flat import
    from segments import ROAD_CLASSES
    from timeslots import when_mask
    from vehicles import ATTRS, KINDS, OPS, VehicleGroup, cond_pairs

RULE_KINDS = ("speed_set", "speed_pct", "cap", "ban")
LAYERS = ("official", "company", "edit")
BAN_LAYERS = ("official", "company")     # the rule layers group_closures applies, after the OSM tags
CONFIDENCE = ("cao", "tb", "thap")
CAP_CLASSES = ROAD_CLASSES[:5]
_RULE_KEYS = {"id", "kind", "layer", "confidence", "active", "source", "document",
              "note", "valid_from", "valid_to", "body"}
SCOPE_KEYS = ("segments", "polygon", "names", "routes", "zone", "admin")
GROUP_NAME = re.compile(r"[A-Za-z0-9_-]+")


class RuleError(ValueError):
    """Raised when a rule is malformed; the message names the bad field."""


@dataclass
class Rule:
    id: str
    kind: str
    layer: str
    body: dict
    confidence: str = "cao"
    active: bool = True
    source: str = ""
    document: str = ""
    note: str = ""
    valid_from: date | None = None
    valid_to: date | None = None

    def in_force(self, as_of: date) -> bool:
        if self.valid_from is not None and as_of < self.valid_from:
            return False
        if self.valid_to is not None and as_of > self.valid_to:
            return False
        return True


@dataclass
class SeedFile:
    rules: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    holidays: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)


def rule_to_dict(rule: Rule) -> dict:
    d = asdict(rule)
    d["valid_from"] = rule.valid_from.isoformat() if rule.valid_from else None
    d["valid_to"] = rule.valid_to.isoformat() if rule.valid_to else None
    return d


def rule_from_dict(d: dict) -> Rule:
    d = dict(d)
    for k in ("valid_from", "valid_to"):
        v = d.get(k)
        d[k] = date.fromisoformat(v) if isinstance(v, str) and v else None
    return Rule(**d)


def rule_set_sha(rules: list) -> str:
    """sha256 of the canonical JSON of the active rules, sorted by id."""
    active = sorted((r for r in rules if r.active), key=lambda r: r.id)
    text = json.dumps([rule_to_dict(r) for r in active], sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- validation

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _str_list(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(s, str) and s for s in v)


def _check_vehicle(cond, where="vehicle") -> None:
    if not isinstance(cond, dict):
        raise RuleError(f"{where}: must be an object")
    for attr, spec in cond.items():
        if attr not in ATTRS:
            raise RuleError(f"{where}: unknown vehicle attribute {attr!r}")
        try:
            pairs = cond_pairs(spec)
        except TypeError:
            raise RuleError(f"{where}.{attr}: must be [op, value] or a list of pairs") from None
        if not pairs:
            raise RuleError(f"{where}.{attr}: empty condition")
        for pair in pairs:
            if len(pair) != 2 or pair[0] not in OPS:
                raise RuleError(f"{where}.{attr}: bad [op, value] {pair!r}; ops {list(OPS)}")
            op, value = pair
            if op in ("in", "not_in"):
                if not isinstance(value, list) or not value:
                    raise RuleError(f"{where}.{attr}: {op} needs a non-empty list")
                values = value
            else:
                values = [value]
            for v in values:
                if attr == "kind":
                    if v not in KINDS:
                        raise RuleError(f"{where}.kind: unknown kind {v!r}; allowed {list(KINDS)}")
                elif attr == "name":
                    if not isinstance(v, str):
                        raise RuleError(f"{where}.name: must be a string")
                elif not _num(v):
                    raise RuleError(f"{where}.{attr}: value must be a number, got {v!r}")


def _check_when(when) -> None:
    if when is None:
        return
    if not isinstance(when, dict):
        raise RuleError("when: must be an object")
    try:
        when_mask(when)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuleError(f"when: {exc}") from None


def _check_bbox(bbox) -> None:
    if (not isinstance(bbox, list) or len(bbox) != 4 or not all(_num(v) for v in bbox)
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise RuleError("scope.bbox: must be [west, south, east, north]")


def _check_pieces(pieces, where) -> None:
    if not isinstance(pieces, list) or not pieces:
        raise RuleError(f"{where}: must be a non-empty list of route pieces")
    for p in pieces:
        if not isinstance(p, dict):
            raise RuleError(f"{where}: piece must be an object, got {p!r}")
        extra = set(p) - {"name", "ref", "from", "to"}
        if extra:
            raise RuleError(f"{where}: unknown piece keys {sorted(extra)}")
        if not (isinstance(p.get("name"), str) and p["name"]) and not (isinstance(p.get("ref"), str) and p["ref"]):
            raise RuleError(f"{where}: piece needs name or ref")
        for k in ("from", "to"):
            if k in p and p[k] is not None and not isinstance(p[k], str):
                raise RuleError(f"{where}.{k}: must be a string")


def _check_scope(scope, need_bbox: bool = True) -> None:
    """Check a scope; need_bbox is False for speed rules (UI edits), True for bans."""
    if not isinstance(scope, dict):
        raise RuleError("scope: must be an object")
    present = [k for k in SCOPE_KEYS if k in scope]
    if len(present) != 1:
        raise RuleError(f"scope: needs exactly one of {list(SCOPE_KEYS)}, got {present}")
    extra = set(scope) - set(SCOPE_KEYS) - {"bbox", "classes", "exclude_classes"}
    if extra:
        raise RuleError(f"scope: unknown keys {sorted(extra)}")
    key = present[0]
    value = scope[key]
    if key in ("names", "routes", "zone"):
        if need_bbox and "bbox" not in scope:
            raise RuleError(f"scope.bbox: required with {key}")
    if "bbox" in scope:
        _check_bbox(scope["bbox"])
    for k in ("classes", "exclude_classes"):
        if k in scope:
            v = scope[k]
            if not isinstance(v, list) or any(c not in CAP_CLASSES for c in v):
                raise RuleError(f"scope.{k}: values must be from {list(CAP_CLASSES)}")
    if key == "segments":
        if (not isinstance(value, list) or not value
                or not all(isinstance(s, (list, tuple)) and len(s) == 3 and all(isinstance(x, int) for x in s)
                           for s in value)):
            raise RuleError("scope.segments: must be [[way_id, a, b], ...]")
    elif key == "polygon":
        if not isinstance(value, dict) or "type" not in value:
            raise RuleError("scope.polygon: must be a GeoJSON geometry")
    elif key == "names":
        if not _str_list(value):
            raise RuleError("scope.names: must be a non-empty list of strings")
    elif key == "routes":
        _check_pieces(value, "scope.routes")
    elif key == "zone":
        if not isinstance(value, dict):
            raise RuleError("scope.zone: must be an object")
        extra = set(value) - {"boundary_names", "seed", "area_km2"}
        if extra:
            raise RuleError(f"scope.zone: unknown keys {sorted(extra)}")
        bn = value.get("boundary_names")
        if not isinstance(bn, list) or not bn:
            raise RuleError("scope.zone.boundary_names: must be a non-empty list")
        for entry in bn:
            if isinstance(entry, str):
                if not entry:
                    raise RuleError("scope.zone.boundary_names: empty name")
            else:
                _check_pieces([entry], "scope.zone.boundary_names")
        seed = value.get("seed")
        if not isinstance(seed, list) or len(seed) != 2 or not all(_num(v) for v in seed):
            raise RuleError("scope.zone.seed: must be [lat, lon]")
        area = value.get("area_km2")
        if (not isinstance(area, list) or len(area) != 2 or not all(_num(v) for v in area)
                or not 0 < area[0] <= area[1]):
            raise RuleError("scope.zone.area_km2: must be [min, max] with 0 < min <= max")
    elif key == "admin":
        if not isinstance(value, dict):
            raise RuleError("scope.admin: must be an object")
        extra = set(value) - {"province", "units", "except_refs"}
        if extra:
            raise RuleError(f"scope.admin: unknown keys {sorted(extra)}")
        if not isinstance(value.get("province"), str) or not value["province"]:
            raise RuleError("scope.admin.province: must be a string")
        if not _str_list(value.get("units")):
            raise RuleError("scope.admin.units: must be a non-empty list of strings")
        if "except_refs" in value and not (isinstance(value["except_refs"], list)
                                           and all(isinstance(s, str) for s in value["except_refs"])):
            raise RuleError("scope.admin.except_refs: must be a list of strings")


def _need(body: dict, required, optional) -> None:
    for k in required:
        if k not in body:
            raise RuleError(f"body.{k}: required")
    extra = set(body) - set(required) - set(optional)
    if extra:
        raise RuleError(f"body: unknown keys {sorted(extra)}")


def validate_rule(rule: Rule) -> None:
    """Raise RuleError (naming the bad field) unless the rule is well formed."""
    if not isinstance(rule.id, str) or not rule.id:
        raise RuleError("id: must be a non-empty string")
    if rule.kind not in RULE_KINDS:
        raise RuleError(f"kind: {rule.kind!r} not in {list(RULE_KINDS)}")
    if rule.layer not in LAYERS:
        raise RuleError(f"layer: {rule.layer!r} not in {list(LAYERS)}")
    if rule.confidence not in CONFIDENCE:
        raise RuleError(f"confidence: {rule.confidence!r} not in {list(CONFIDENCE)}")
    if (rule.valid_from and rule.valid_to) and rule.valid_from > rule.valid_to:
        raise RuleError("valid_from must not be after valid_to")
    body = rule.body
    if not isinstance(body, dict):
        raise RuleError("body: must be an object")
    kind = rule.kind
    if kind == "speed_set":
        _need(body, ("scope", "kmh"), ("when",))
        if not _num(body["kmh"]) or body["kmh"] <= 0:
            raise RuleError("kmh: must be a number > 0")
        _check_when(body.get("when"))
        _check_scope(body["scope"], need_bbox=False)
    elif kind == "speed_pct":
        _need(body, ("scope", "pct"), ("when", "classes"))
        if not _num(body["pct"]) or body["pct"] <= -100:
            raise RuleError("pct: must be a number > -100")
        if "classes" in body:
            c = body["classes"]
            if not isinstance(c, list) or any(x not in CAP_CLASSES for x in c):
                raise RuleError(f"classes: values must be from {list(CAP_CLASSES)}")
        _check_when(body.get("when"))
        _check_scope(body["scope"], need_bbox=False)
    elif kind == "cap":
        _need(body, ("vehicle", "road_classes"), ())
        _check_vehicle(body["vehicle"])
        rc = body["road_classes"]
        if not isinstance(rc, dict) or not rc:
            raise RuleError("road_classes: must be a non-empty object")
        for cls, kmh in rc.items():
            if cls not in CAP_CLASSES:
                raise RuleError(f"road_classes: class {cls!r} not in {list(CAP_CLASSES)}")
            if not _num(kmh) or kmh <= 0:
                raise RuleError(f"road_classes.{cls}: must be a number > 0")
    else:  # ban
        if rule.layer not in BAN_LAYERS:
            raise RuleError(f"layer: a ban must be in one of {list(BAN_LAYERS)}, got {rule.layer!r}")
        _need(body, ("vehicle", "scope", "action"), ("when", "exempt"))
        _check_vehicle(body["vehicle"])
        _check_scope(body["scope"])
        if body["action"] not in ("close", "open"):
            raise RuleError("action: must be 'close' or 'open'")
        _check_when(body.get("when"))
        if "exempt" in body:
            ex = body["exempt"]
            if not isinstance(ex, dict) or set(ex) != {"routes"}:
                raise RuleError("exempt: may hold only 'routes'")
            _check_pieces(ex["routes"], "exempt.routes")


def validate_group(group: VehicleGroup) -> None:
    """Raise RuleError (naming the bad field) unless the vehicle group's values are usable.

    A typo in ``kind`` or a non-positive size would be stored silently and make rules stop matching.
    """
    if not isinstance(group.name, str) or not GROUP_NAME.fullmatch(group.name):
        raise RuleError(f"group name: {group.name!r} must match {GROUP_NAME.pattern} (it names a file)")
    where = f"group {group.name!r}"
    for attr in ("gross_t", "payload_t", "height_m", "length_m"):
        v = getattr(group, attr)
        if not _num(v) or not v > 0:
            raise RuleError(f"{where}.{attr}: must be a number > 0, got {v!r}")
    if group.payload_t > group.gross_t:
        raise RuleError(f"{where}.payload_t: {group.payload_t} exceeds gross_t {group.gross_t}")
    if not isinstance(group.axles, int) or isinstance(group.axles, bool) or group.axles < 1:
        raise RuleError(f"{where}.axles: must be an integer >= 1, got {group.axles!r}")
    if group.kind not in KINDS:
        raise RuleError(f"{where}.kind: unknown kind {group.kind!r}; allowed {list(KINDS)}")
    if not isinstance(group.is_base, bool):
        raise RuleError(f"{where}.is_base: must be true or false, got {group.is_base!r}")


# ---------------------------------------------------------------- seed files

def _parse_date(v, where):
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        raise RuleError(f"{where}: bad date {v!r}") from None


def load_seed_file(path: Path) -> SeedFile:
    """Load a seed file (RULE_FORMAT.md plus optional groups and holidays)."""
    doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rules = []
    for raw in doc.get("rules", []):
        if not isinstance(raw, dict):
            raise RuleError("rule: must be an object")
        for k in ("id", "kind", "body"):
            if k not in raw:
                raise RuleError(f"{k}: required in rule {raw.get('id', '?')!r}")
        extra = set(raw) - _RULE_KEYS
        if extra:
            raise RuleError(f"rule {raw['id']!r}: unknown keys {sorted(extra)}")
        confidence = raw.get("confidence", "cao")
        if "active" in raw and not isinstance(raw["active"], bool):
            raise RuleError(f"active: must be true or false in rule {raw['id']!r}")
        active = raw["active"] if "active" in raw else confidence != "thap"
        rules.append(Rule(
            id=raw["id"], kind=raw["kind"], layer=raw.get("layer", "official"),
            body=raw["body"], confidence=confidence, active=active,
            source=raw.get("source", ""), document=raw.get("document", ""),
            note=raw.get("note", ""),
            valid_from=_parse_date(raw.get("valid_from"), f"{raw['id']}.valid_from"),
            valid_to=_parse_date(raw.get("valid_to"), f"{raw['id']}.valid_to"),
        ))
    groups = [VehicleGroup(**g) for g in doc.get("groups", [])]
    for group in groups:
        validate_group(group)
    names = [g.name for g in groups]
    if len(set(names)) != len(names):
        raise RuleError(f"groups: duplicate names {sorted({n for n in names if names.count(n) > 1})}")
    if sum(g.is_base for g in groups) > 1:
        raise RuleError(f"groups: more than one base group {[g.name for g in groups if g.is_base]}")
    holidays = []
    for h in doc.get("holidays", []):
        day, name = (h["day"], h.get("name", "")) if isinstance(h, dict) else (h[0], h[1])
        holidays.append((_parse_date(day, "holidays"), name))
    return SeedFile(rules, groups, holidays, list(doc.get("unresolved", [])))
