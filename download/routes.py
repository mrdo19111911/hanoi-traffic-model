"""Fixed TomTom probe routes for the spatial crawl.

Each entry is (route_id, "lat,lon" origin, "lat,lon" destination). The route id
names the cached snapshot (``data/api_raw/<id>_<YYYYMMDD>_<HHMM>.json``), so an
id must never be renamed once a snapshot exists for it.

R01-R15 are the original Hanoi routes. EXP_probe_1..3 are three short stretches
of the Hanoi-Hai Phong expressway (the only ``caotoc`` samples); they used to be
derived at run time from a legacy geometry file that is not in git, so the
coordinates are pinned here instead (derived 2026-11-12 from that file at 20-26 %,
45-51 % and 70-76 % of the route length).
"""

ROUTES = [
    ("R01_hoguom_noibai", "21.0285,105.8525", "21.2142,105.8000"),
    ("R02_royal_noibai", "20.9990,105.8130", "21.2142,105.8000"),
    ("R03_times_bigc", "20.9930,105.8670", "21.0090,105.7990"),
    ("R04_hadong_noibai", "20.9800,105.7850", "21.2142,105.8000"),
    ("R05_giapbat_ngatu", "20.9860,105.8410", "21.0080,105.8150"),
    ("R06_mydinh_hoguom", "21.0280,105.7710", "21.0285,105.8525"),
    ("R07_longbien_hadong", "21.0350,105.8750", "20.9800,105.7850"),
    ("R08_caugiay_times", "21.0350,105.7950", "20.9930,105.8670"),
    ("R09_dongxuan_vanmieu", "21.0380,105.8460", "21.0278,105.8350"),
    ("R10_chohom_bachkhoa", "21.0140,105.8560", "21.0050,105.8430"),
    ("R11_nghiatan_caugiay", "21.0350,105.7950", "21.0280,105.8000"),
    ("R12_hotay_hoguom", "21.0550,105.8300", "21.0285,105.8525"),
    ("R13_bigc_hotay", "21.0090,105.7990", "21.0550,105.8300"),
    ("R14_times_vanmieu", "20.9930,105.8670", "21.0278,105.8350"),
    ("R15_giapbat_hoguom", "20.9860,105.8410", "21.0285,105.8525"),
    ("EXP_probe_1", "20.9089328,105.9840602", "20.8587378,106.0254079"),
    ("EXP_probe_2", "20.8614818,106.2351726", "20.8582873,106.3027771"),
    ("EXP_probe_3", "20.8090192,106.5097053", "20.7801063,106.5701408"),
]
