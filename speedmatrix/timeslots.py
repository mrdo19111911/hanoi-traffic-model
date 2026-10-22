"""Time slots (48 x 30 min), day types and OSM conditional-restriction parsing."""
import datetime
import re

import numpy as np

DAYTYPES = ("weekday", "sat", "sun")
SLOTS = 48
SLOT_S = 1800

_DAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
_TIME = r"\d{1,2}:\d{2}"
_RANGE_RE = re.compile(rf"^({_TIME})\s*-\s*({_TIME})$")
_COND_RE = re.compile(r"^\s*([^@]*?)\s*@\s*\((.*)\)\s*$", re.S)


class ConditionalParseError(ValueError):
    """Raised for a conditional value this module does not support."""


def slot_index(hhmm: str) -> int:
    """Map an "HHMM" string on a 30-minute boundary to a slot index 0..47."""
    if not isinstance(hhmm, str) or not re.fullmatch(r"\d{4}", hhmm):
        raise ValueError(f"bad slot {hhmm!r}")
    h, m = int(hhmm[:2]), int(hhmm[2:])
    if h > 23 or m not in (0, 30):
        raise ValueError(f"slot {hhmm!r} is not on a 30-minute boundary")
    return h * 2 + m // 30


def daytype_of(day: datetime.date, holidays) -> str:
    """Day type of a date; public holidays count as Sunday."""
    if day in holidays:
        return "sun"
    wd = day.weekday()
    return "weekday" if wd < 5 else ("sat" if wd == 5 else "sun")


def _minutes(text: str, allow_2400: bool = True) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        raise ValueError(f"bad time {text!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if mi > 59 or h > 24 or (h == 24 and (mi or not allow_2400)):
        raise ValueError(f"bad time {text!r}")
    return h * 60 + mi


def times_mask(intervals) -> np.ndarray:
    """Boolean (48,) mask of slots overlapped by any [start, end) interval.

    Times are "HH:MM"; end "24:00" is allowed; end <= start wraps past midnight.
    """
    mask = np.zeros(SLOTS, dtype=bool)
    for start, end in intervals:
        s, e = _minutes(start), _minutes(end)
        parts = [(s, e)] if e > s else [(s, 1440), (0, e)]
        for a, b in parts:
            if b <= a:
                continue
            mask[a // 30:(b + 29) // 30] = True
    return mask


def when_mask(when) -> np.ndarray:
    """Boolean (3, 48) mask for a {"daytypes": [...], "times": [[s, e], ...]} dict.

    A missing key means all; None means always. Raises ValueError for an
    unknown key or an unknown daytype name.
    """
    mask = np.zeros((len(DAYTYPES), SLOTS), dtype=bool)
    when = when or {}
    unknown = set(when) - {"daytypes", "times"}
    if unknown:
        raise ValueError(f"unknown key in when: {sorted(unknown)}")
    daytypes = when.get("daytypes")
    if daytypes is not None:
        bad = [d for d in daytypes if d not in DAYTYPES]
        if bad:
            raise ValueError(f"unknown daytype {bad}; allowed {list(DAYTYPES)}")
    times = when.get("times")
    tmask = np.ones(SLOTS, dtype=bool) if times is None else times_mask([tuple(t) for t in times])
    for i, dt in enumerate(DAYTYPES):
        if daytypes is None or dt in daytypes:
            mask[i] = tmask
    return mask


def _day_index(token: str) -> int:
    if token not in _DAYS:
        raise ConditionalParseError(f"unsupported day {token!r}")
    return _DAYS.index(token)


def _daytypes_of_list(text: str) -> set:
    covered = set()
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        if token == "PH":
            covered.add("sun")
            continue
        names = [t.strip() for t in token.split("-")]
        if len(names) == 1:
            days = [_day_index(names[0])]
        elif len(names) == 2:
            a, b = _day_index(names[0]), _day_index(names[1])
            days = [(a + k) % 7 for k in range((b - a) % 7 + 1)]
        else:
            raise ConditionalParseError(f"unsupported day list {text!r}")
        for d in days:
            covered.add("weekday" if d < 5 else ("sat" if d == 5 else "sun"))
    return covered


def _parse_spec(spec: str) -> np.ndarray:
    spec = spec.strip()
    first = re.search(_TIME, spec)
    if not first:
        raise ConditionalParseError(f"no time range in {spec!r}")
    day_text, time_text = spec[:first.start()].strip(), spec[first.start():]
    intervals = []
    for piece in time_text.split(","):
        m = _RANGE_RE.match(piece.strip())
        if not m:
            raise ConditionalParseError(f"unsupported time range {piece!r}")
        try:
            _minutes(m.group(1)), _minutes(m.group(2))
        except ValueError as exc:
            raise ConditionalParseError(str(exc)) from None
        intervals.append((m.group(1), m.group(2)))
    daytypes = list(_daytypes_of_list(day_text)) if day_text else None
    if daytypes is not None and not daytypes:
        raise ConditionalParseError(f"empty day list in {spec!r}")
    return when_mask({"daytypes": daytypes, "times": intervals})


def parse_conditional(value: str):
    """Parse an OSM `*:conditional` value into [(value, mask (3, 48)), ...].

    A day list covers weekday if it names any Mo-Fr, sat if it names Sa,
    sun if it names Su or PH (conservative for trucks).
    """
    result = []
    for part in value.split(";"):
        m = _COND_RE.match(part)
        if not m or not m.group(1):
            raise ConditionalParseError(f"unsupported conditional {part!r}")
        result.append((m.group(1), _parse_spec(m.group(2))))
    return result
