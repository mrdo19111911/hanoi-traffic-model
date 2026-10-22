"""One explicit, inspectable classification policy for OSM ways.

These five project categories are a modelling policy, not an official legal
road classification. Original OSM tags are retained in the spatial index.
"""
import re

POLICY_VERSION = 1


def classify_osm(highway, ref="", name="", service=""):
    if highway in ("motorway", "motorway_link"):
        return "caotoc"
    if re.search(r"(?:^|[;\s/])QL\s*\.?\s*\d", ref.upper()) or highway in ("trunk", "trunk_link"):
        return "quoclo"
    if any(word in name.casefold() for word in ("ngõ", "ngách", "hẻm")):
        return "nho"
    if highway in ("service", "living_street", "track"):
        return "nho"
    if "phố" in name.casefold():
        return "noido"
    if highway in ("primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link"):
        return "chinh"
    if highway in ("residential", "unclassified"):
        return "noido"
    return "unknown"


# Big-road (A) network for intercity routing. OSM tags a national road by how
# each stretch looks locally, so QL1 through a town is often only "primary";
# the ref keeps such stretches in the network. Provincial roads (đường tỉnh: ĐT, also written DT
# or TL) count too; district roads (ĐH) do not.
BIG_ROAD_POLICY_VERSION = 6
BIG_ROAD_TAGS = {"motorway": "caotoc", "motorway_link": "caotoc", "trunk": "quoclo", "trunk_link": "quoclo"}
DRIVABLE_TAGS = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link",
                 "unclassified", "residential", "living_street", "service", "road"}
_EXPRESSWAY_REF = re.compile(r"(?:^|[;\s/])CT\s*\.?\s*\d", re.I)
_NATIONAL_REF = re.compile(r"(?:^|[;\s/])(?:QL|AH)\s*\.?\s*\d", re.I)
_PROVINCIAL_REF = re.compile(r"(?:^|[;\s/])(?:ĐT|DT|TL)\s*\.?\s*\d", re.I)


def big_road_group(highway, ref=""):
    """Return "caotoc", "quoclo" or "tinhlo" for an A-network way, otherwise None."""
    if highway in BIG_ROAD_TAGS:
        return BIG_ROAD_TAGS[highway]
    if highway not in DRIVABLE_TAGS:
        return None
    if _EXPRESSWAY_REF.search(ref):
        return "caotoc"
    if _NATIONAL_REF.search(ref):
        return "quoclo"
    if _PROVINCIAL_REF.search(ref):
        return "tinhlo"
    return None


def is_big_road(highway, ref=""):
    return big_road_group(highway, ref) is not None


# Free-flow speeds as the local OSRM car profile (osrm/tools/profiles/car.lua) drives them: a numeric km/h
# maxspeed times speed_reduction, otherwise the profile's default for the highway tag. A test keeps this in step.
SPEED_REDUCTION = 0.8
DEFAULT_SPEEDS_KMH = {"motorway": 90, "motorway_link": 45, "trunk": 85, "trunk_link": 40, "primary": 65, "primary_link": 30,
                      "secondary": 55, "secondary_link": 25, "tertiary": 40, "tertiary_link": 20, "unclassified": 25,
                      "residential": 25, "living_street": 10, "service": 15}
_MAXSPEED_KMH = re.compile(r"\s*(\d+(?:\.\d+)?)\s*(?:km/h)?\s*")


def parse_maxspeed(value):
    """A positive maxspeed in km/h ("60", "60 km/h"), or None for mph, "none", zones and other forms."""
    match = _MAXSPEED_KMH.fullmatch(value or "")
    speed = float(match.group(1)) if match else 0.0
    return speed if speed > 0 else None


def free_flow_speed(highway, maxspeed=""):
    """(km/h, source) for a road; (None, "unknown") when the profile has no speed for its highway tag."""
    if highway not in DEFAULT_SPEEDS_KMH:
        return None, "unknown"
    limit = parse_maxspeed(maxspeed)
    if limit is not None:
        return round(limit * SPEED_REDUCTION, 1), "osm_maxspeed"
    return DEFAULT_SPEEDS_KMH[highway], "profile_default"
