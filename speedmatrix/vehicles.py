"""Vehicle groups and the condition language used by rules."""
import operator
from dataclasses import dataclass, fields

KINDS = ("normal", "tipper", "tractor", "container", "pickup", "van")
OPS = ("<", "<=", ">", ">=", "==", "in", "not_in")

_CMP = {"<": operator.lt, "<=": operator.le, ">": operator.gt,
        ">=": operator.ge, "==": operator.eq}


@dataclass(frozen=True)
class VehicleGroup:
    name: str
    gross_t: float
    payload_t: float
    axles: int
    kind: str
    height_m: float
    length_m: float
    is_base: bool = False


ATTRS = tuple(f.name for f in fields(VehicleGroup) if f.name != "is_base")


def cond_pairs(spec) -> list:
    """Normalise an attribute condition to a list of [op, value] pairs."""
    if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], str):
        return [list(spec)]
    return [list(p) for p in spec]


def _test(op: str, have, want) -> bool:
    if op == "in":
        return have in want
    if op == "not_in":
        return have not in want
    return _CMP[op](have, want)


def matches(cond: dict, group: VehicleGroup) -> bool:
    """True when the group satisfies every attribute condition (AND)."""
    for attr, spec in (cond or {}).items():
        have = getattr(group, attr)
        for op, want in cond_pairs(spec):
            if not _test(op, have, want):
                return False
    return True
