import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from speedmatrix.rules import (
    BAN_LAYERS, Rule, RuleError, load_seed_file, rule_set_sha, validate_rule,
)
from speedmatrix.vehicles import VehicleGroup, matches

BB = [105.7, 20.9, 106.0, 21.1]
BAN_BODY = {"vehicle": {}, "action": "close", "scope": {"names": ["A"], "bbox": BB}}


def ban(**over):
    body = json.loads(json.dumps(BAN_BODY))
    body.update(over)
    return Rule("r", "ban", "official", body)


class RuleTests(unittest.TestCase):
    def test_vehicle_conditions(self):
        g = VehicleGroup("tu2t", gross_t=8, payload_t=5, axles=2, kind="normal", height_m=3.2, length_m=7.5)
        self.assertTrue(matches({"gross_t": [">=", 2]}, g))
        self.assertFalse(matches({"gross_t": ["<", 2]}, g))
        self.assertTrue(matches({"kind": ["not_in", ["pickup"]], "gross_t": [[">", 3.5], ["<=", 10]]}, g))
        self.assertTrue(matches({}, g))
        self.assertFalse(matches({"kind": ["in", ["pickup", "van"]]}, g))
        self.assertTrue(matches({"axles": ["==", 2]}, g))

    def test_validation_names_the_field(self):
        with self.assertRaisesRegex(RuleError, "scope"):
            validate_rule(Rule("r1", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"names": ["A"], "polygon": {}}}))
        with self.assertRaisesRegex(RuleError, "pct"):
            validate_rule(Rule("r2", "speed_pct", "edit", {"scope": {"names": ["A"]}, "pct": -100}))
        with self.assertRaisesRegex(RuleError, "when"):
            validate_rule(Rule("r3", "speed_set", "edit", {"scope": {"names": ["A"]}, "kmh": 20, "when": {"times": [["6", "9"]]}}))
        with self.assertRaisesRegex(RuleError, "when"):
            validate_rule(Rule("r3", "speed_set", "edit", {"scope": {"names": ["A"]}, "kmh": 20, "when": {"daytypes": ["monday"]}}))

    def test_rule_set_sha_ignores_inactive_and_order(self):
        a = Rule("a", "speed_set", "edit", {"scope": {"names": ["X"]}, "kmh": 20})
        b = Rule("b", "speed_set", "edit", {"scope": {"names": ["Y"]}, "kmh": 30}, active=False)
        c = Rule("c", "speed_set", "edit", {"scope": {"names": ["Z"]}, "kmh": 30})
        self.assertEqual(rule_set_sha([a, b]), rule_set_sha([a]))
        self.assertEqual(rule_set_sha([a, c]), rule_set_sha([c, a]))
        self.assertNotEqual(rule_set_sha([a, c]), rule_set_sha([a]))

    def test_sha_depends_on_validity_dates(self):
        a = Rule("a", "ban", "official", BAN_BODY)
        b = Rule("a", "ban", "official", BAN_BODY, valid_to=date(2026, 2, 1))
        self.assertNotEqual(rule_set_sha([a]), rule_set_sha([b]))

    def test_seed_low_confidence_imports_inactive(self):
        rules = [
            {"id": "low", "kind": "ban", "layer": "official", "confidence": "thap", "body": BAN_BODY},
            {"id": "low_on", "kind": "ban", "layer": "official", "confidence": "thap", "active": True, "body": BAN_BODY},
            {"id": "hi", "kind": "ban", "layer": "official", "confidence": "cao", "body": BAN_BODY,
             "valid_from": "2026-01-15", "valid_to": None, "document": "QD 1", "note": "n", "source": "u"},
        ]
        doc = {"area": "x", "researched_at": "2026-10-21", "comment": "c", "rules": rules,
               "unresolved": [{"place": "p"}],
               "groups": [{"name": "g", "gross_t": 8, "payload_t": 5, "axles": 2, "kind": "normal",
                           "height_m": 3.2, "length_m": 7.5, "is_base": True}],
               "holidays": [["2026-09-02", "Quoc khanh"]]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rules_x.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            seed = load_seed_file(path)
        self.assertEqual({r.id: r.active for r in seed.rules}, {"low": False, "low_on": True, "hi": True})
        self.assertEqual(seed.rules[2].valid_from, date(2026, 1, 15))
        self.assertIsNone(seed.rules[2].valid_to)
        self.assertEqual(seed.unresolved, [{"place": "p"}])
        self.assertEqual(seed.groups[0].name, "g")
        self.assertEqual(seed.holidays, [(date(2026, 9, 2), "Quoc khanh")])

    def test_validity_dates(self):
        r = Rule("t", "ban", "official", BAN_BODY, valid_from=date(2026, 1, 15), valid_to=date(2026, 2, 28))
        self.assertTrue(r.in_force(date(2026, 1, 15)))
        self.assertFalse(r.in_force(date(2026, 3, 1)))
        with self.assertRaisesRegex(RuleError, "valid"):
            validate_rule(Rule("u", "ban", "official", BAN_BODY, valid_from=date(2026, 3, 1), valid_to=date(2026, 2, 1)))

    def test_scope_kinds_need_bbox_and_known_vehicle_fields(self):
        with self.assertRaisesRegex(RuleError, "bbox"):
            validate_rule(Rule("r", "ban", "official", {"vehicle": {}, "action": "close", "scope": {"routes": [{"name": "A"}]}}))
        with self.assertRaisesRegex(RuleError, "weight"):
            validate_rule(Rule("r", "ban", "official", {"vehicle": {"weight": [">", 1]}, "action": "close", "scope": {"names": ["A"], "bbox": BB}}))
        with self.assertRaisesRegex(RuleError, "kind"):
            validate_rule(Rule("r", "ban", "official", {"vehicle": {"kind": ["in", ["bus"]]}, "action": "close", "scope": {"names": ["A"], "bbox": BB}}))

    def test_accepts_rich_scopes(self):
        zone = {"boundary_names": ["Vanh dai 3", {"name": "A", "from": "B", "to": "C"}, {"ref": "QL1"}],
                "seed": [21.0, 105.8], "area_km2": [10, 50]}
        for scope in (
            {"zone": zone, "bbox": BB},
            {"routes": [{"name": "A", "from": "B"}, {"ref": "QL13"}], "bbox": BB, "classes": ["chinh"]},
            {"admin": {"province": "Ha Tinh", "units": ["P1"], "except_refs": ["QL"]}},
            {"segments": [[1, 2, 3]]},
            {"polygon": {"type": "Polygon", "coordinates": []}},
        ):
            validate_rule(ban(scope=scope, exempt={"routes": [{"name": "E"}]},
                              when={"daytypes": ["sat"], "times": [["22:00", "06:00"]]}))

    def test_rejects_bad_bodies(self):
        bad = [
            ban(scope={"names": ["A"], "bbox": BB}, exempt={"names": ["x"]}),
            ban(action="shut"),
            ban(scope={"names": ["A"], "bbox": BB, "classes": ["pha"]}),
            ban(scope={"zone": {"boundary_names": [], "seed": [1, 2], "area_km2": [1, 2]}, "bbox": BB}),
        ]
        for r in bad:
            with self.assertRaises(RuleError):
                validate_rule(r)
        with self.assertRaisesRegex(RuleError, "road_classes"):
            validate_rule(Rule("c", "cap", "official", {"vehicle": {}, "road_classes": {"pha": 5}}))
        with self.assertRaisesRegex(RuleError, "kmh"):
            validate_rule(Rule("c", "speed_set", "edit", {"scope": {"names": ["A"]}, "kmh": 0}))
        with self.assertRaisesRegex(RuleError, "kind"):
            validate_rule(Rule("c", "zap", "edit", {}))
        validate_rule(Rule("c", "cap", "official", {"vehicle": {"gross_t": [">", 3.5]}, "road_classes": {"caotoc": 90}}))

    def _seed(self, rule_over=None, drop=()):
        raw = {"id": "x", "kind": "ban", "layer": "official", "body": BAN_BODY}
        raw.update(rule_over or {})
        for k in drop:
            raw.pop(k)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rules_x.json"
            path.write_text(json.dumps({"rules": [raw]}), encoding="utf-8")
            return load_seed_file(path)

    def test_seed_rejects_unknown_rule_key(self):
        with self.assertRaisesRegex(RuleError, "valid_form"):
            self._seed({"valid_form": "2026-01-01"})

    def test_seed_rejects_string_active_and_missing_fields(self):
        with self.assertRaisesRegex(RuleError, "active"):
            self._seed({"active": "false"})
        for k in ("id", "kind", "body"):
            with self.assertRaisesRegex(RuleError, k):
                self._seed(drop=(k,))

    def test_ban_layer_must_be_one_that_closures_apply(self):
        # group_closures applies only the BAN_LAYERS; an "edit" ban would be stored and silently ignored
        self.assertEqual(BAN_LAYERS, ("official", "company"))
        with self.assertRaisesRegex(RuleError, "layer"):
            validate_rule(Rule("r", "ban", "edit", json.loads(json.dumps(BAN_BODY))))
        for layer in BAN_LAYERS:
            validate_rule(Rule("r", "ban", layer, json.loads(json.dumps(BAN_BODY))))
        validate_rule(Rule("c", "cap", "edit", {"vehicle": {}, "road_classes": {"caotoc": 90}}))

    def _groups_seed(self, *groups):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "groups.json"
            path.write_text(json.dumps({"groups": list(groups)}), encoding="utf-8")
            return load_seed_file(path)

    def test_group_values_are_validated(self):
        good = {"name": "tu2t", "gross_t": 8.0, "payload_t": 5.0, "axles": 2, "kind": "normal",
                "height_m": 3.4, "length_m": 7.5, "is_base": False}
        self.assertEqual(self._groups_seed(good).groups[0].name, "tu2t")
        bad = [({"kind": "Container"}, "kind"), ({"gross_t": 0}, "gross_t"), ({"gross_t": "8"}, "gross_t"),
               ({"payload_t": -1}, "payload_t"), ({"payload_t": 9.0}, "payload_t"), ({"axles": 0}, "axles"),
               ({"axles": 2.5}, "axles"), ({"height_m": 0}, "height_m"), ({"length_m": None}, "length_m"),
               ({"is_base": "yes"}, "is_base"), ({"name": "tu 2t"}, "name"), ({"name": ""}, "name"),
               ({"gross_t": True}, "gross_t")]
        for over, field in bad:
            with self.subTest(over=over), self.assertRaisesRegex(RuleError, field):
                self._groups_seed(dict(good, **over))

    def test_group_file_has_at_most_one_base(self):
        a = {"name": "a", "gross_t": 1.9, "payload_t": 1.0, "axles": 2, "kind": "normal",
             "height_m": 2.6, "length_m": 5.0, "is_base": True}
        with self.assertRaisesRegex(RuleError, "base"):
            self._groups_seed(a, dict(a, name="b"))
        with self.assertRaisesRegex(RuleError, "duplicate"):
            self._groups_seed(a, dict(a, is_base=False))

    def test_unknown_keys_in_zone_and_admin(self):
        zone = {"boundary_names": ["A"], "seed": [21.0, 105.8], "area_km2": [1, 5], "area_km": 3}
        with self.assertRaisesRegex(RuleError, "area_km"):
            validate_rule(ban(scope={"zone": zone, "bbox": BB}))
        with self.assertRaisesRegex(RuleError, "except_ref"):
            validate_rule(ban(scope={"admin": {"province": "P", "units": ["U"], "except_ref": ["QL"]}}))


if __name__ == "__main__":
    unittest.main()
