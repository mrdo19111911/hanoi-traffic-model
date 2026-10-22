import unittest
from pathlib import Path

import numpy as np

from speedmatrix.rules import load_seed_file, validate_rule
from speedmatrix.timeslots import when_mask
from speedmatrix.vehicles import matches

ROOT = Path(__file__).resolve().parent.parent


class SeedTests(unittest.TestCase):
    def test_seed_files_load_and_validate(self):
        ids = []
        for path in sorted((ROOT / "data/rules").rglob("*.json")):
            seed = load_seed_file(path)
            for r in seed.rules:
                validate_rule(r)
                ids.append(r.id)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 116)

    def test_city_rules_hit_the_right_groups(self):
        rules = {r.id: r for p in (ROOT / "data/rules/official").glob("*.json") for r in load_seed_file(p).rules}
        groups = {g.name: g for g in load_seed_file(ROOT / "data/rules/groups.json").groups}
        hit = lambda rid: sorted(n for n, g in groups.items() if matches(rules[rid].body["vehicle"], g))
        self.assertEqual(hit("hn_zone_lt2t_peak"), ["duoi2t"])
        self.assertEqual(hit("hn_zone_ge2t_day"), ["container", "tu2t"])
        self.assertEqual(hit("hcm_qd23_xe_tai_nhe"), ["duoi2t"])
        self.assertEqual(hit("hcm_qd23_xe_tai_nang"), ["container", "tu2t"])
        self.assertEqual(hit("hcm_qd23_dau_keo_so_mi"), ["container"])
        self.assertEqual(np.flatnonzero(when_mask(rules["hn_zone_ge2t_day"].body["when"])[0]).tolist(), list(range(12, 42)))
        self.assertFalse(rules["hcm_du_thao_2026_tren_1t"].active)

    def test_every_group_gets_exactly_one_national_cap(self):
        caps = [r for r in load_seed_file(ROOT / "data/rules/official/national.json").rules if r.kind == "cap"]
        for g in load_seed_file(ROOT / "data/rules/groups.json").groups:
            self.assertEqual(sum(matches(r.body["vehicle"], g) for r in caps), 1, g.name)

    def test_exactly_one_base_group(self):
        self.assertEqual(sum(g.is_base for g in load_seed_file(ROOT / "data/rules/groups.json").groups), 1)

    def test_holidays_load(self):
        h = load_seed_file(ROOT / "data/rules/holidays.json").holidays
        self.assertEqual([d.isoformat() for d, _ in h],
                         ["2026-01-01", "2026-04-30", "2026-05-01", "2026-09-02"])


if __name__ == "__main__":
    unittest.main()
