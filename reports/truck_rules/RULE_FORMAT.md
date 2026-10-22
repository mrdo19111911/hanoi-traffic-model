# Rule file format for truck restrictions (seed files)

Every `rules_<area>.json` file in this folder is a seed file for `speedmatrix` (plan `docs/superpowers/plans/2026-10-21-speed-layer-vehicle-rules.md`, Task 4). `python -m speedmatrix.build import-rules` loads these files. Write valid UTF-8 JSON, without comments.

```json
{
  "area": "south",
  "researched_at": "2026-10-21",
  "rules": [ <rule>, ... ],
  "unresolved": [ {"place": "...", "what": "...", "why": "...", "source": "..."} ]
}
```

## Rule

| Key | Value |
|---|---|
| `id` | Lowercase ASCII snake_case, unique across the repo. The prefix is the place (`hn_`, `hcm_`, `hp_`, `dn_`, `vn_` for national rules, ...). |
| `kind` | `"ban"` or `"cap"` |
| `layer` | `"official"` |
| `confidence` | `"cao"`: full text read. `"tb"`: official summary, or a news article citing the decision. `"thap"`: news only, or unclear whether the decision is still in force. |
| `active` | Optional. When omitted, `"thap"` rules import inactive. Set `false` for drafts or rules not yet in force. |
| `document` | Number and name of the legal document(s), e.g. `"QĐ 23/2018/QĐ-UBND, sửa bởi QĐ 23/2019/QĐ-UBND"` |
| `source` | URL or URLs, space-separated, of pages actually read. Never invent a URL. |
| `valid_from`, `valid_to` | `"YYYY-MM-DD"` or `null`. Temporary bans (Tết, construction) set `valid_to`. |
| `note` | In Vietnamese: ambiguities, how the rule was read, and anything that could not be encoded exactly. |
| `body` | See below. |

## `body` of a `ban` rule

```json
{
  "vehicle": {"payload_t": ["<=", 2.5], "kind": ["not_in", ["pickup", "van"]]},
  "action": "close",
  "when": {"daytypes": ["weekday", "sat", "sun"], "times": [["06:00", "09:00"], ["16:00", "20:00"]]},
  "scope": { ... },
  "exempt": {"routes": [ ... ]}
}
```

**`vehicle`** maps an attribute to `[op, value]`, or to a list of such pairs; everything is AND-ed. `{}` matches every truck.
- Attributes:
  - `gross_t`: khối lượng toàn bộ;
  - `payload_t`: khối lượng chuyên chở cho phép;
  - `axles`;
  - `kind`: one of `normal`, `tipper` (xe ben), `tractor` (đầu kéo / sơ mi rơ moóc), `container`, `pickup` (bán tải), `van`;
  - `height_m`;
  - `length_m`.
- Ops: `<`, `<=`, `>`, `>=`, `==`, `in`, `not_in`.
- Take the threshold basis (gross or payload) exactly from the document. If the document is ambiguous, pick the stricter reading and explain it in `note`.

**`action`** is `"close"` (banned) or `"open"`. Use `open` for corridors or exemptions that run during a time window inside a closed zone. Within one layer, open beats close.

**`when`:**
- `times` are `"HH:MM"` local time. `"24:00"` is allowed. An interval that ends earlier than it starts wraps past midnight.
- An omitted `when` means always.
- Day types: `weekday` (Mon–Fri), `sat`, `sun` (Sunday and public holidays).
- If a rule applies on some weekdays only, list `weekday` (conservative) and say so in `note`.

**`scope`** holds exactly **one** of the following keys, plus an optional `bbox` `[west, south, east, north]`. `bbox` is **required** with `routes`, `names` and `zone`, and should be a tight box around the town, so that common street names do not match in other cities.

- `"zone"`: an area enclosed by boundary roads, as in "the area inside the ring A → B → C".

  ```json
  {"boundary_names": ["Phạm Văn Đồng", "Vành đai 3", ...], "seed": [lat, lon], "area_km2": [min, max]}
  ```
  - `boundary_names`: the ring, in order. Each entry is one of:
    - a street name; bridges count as names ("Cầu Phú Mỹ");
    - a road ref (`"QL1"`);
    - a named river or canal ("Sông Sặt"), or `"coastline"`;
    - a route piece `{"name": ..., "from": ..., "to": ...}`, when only part of a long road bounds the zone. A road named whole that also runs through the zone would split it, and the build rejects such a zone.
  - `seed`: a point well inside the zone, e.g. the central market or the lake.
  - `area_km2`: generous bounds on the expected area, used to detect leaks.
  - Roads that lie on the boundary are **not** in the zone.
- `"routes"`: a list of route pieces.

  ```json
  [{"name": "Mai Chí Thọ", "from": "Nguyễn Cơ Thạch", "to": "Đồng Văn Cống"}, {"ref": "QL13", "from": "Ngã 5 Phước Kiến", "to": "Cầu Vĩnh Bình"}, {"name": "Đồng Khởi"}]
  ```
  - `name` is the street name, or `ref` a road number (`QL13`, `ĐT743`). One of the two is required.
  - `from` and `to` name the crossing streets, bridges or landmarks that bound the piece. Omit both for the whole street inside `bbox`.
- `"admin"`: every road inside the administrative units, e.g. Hà Tĩnh "mọi đường trong các phường".

  ```json
  {"province": "Hà Tĩnh", "units": ["Phường Thành Sen", ...], "except_refs": ["QL", "ĐT"]}
  ```
  - Use the unit names in force after the July 2025 reorganisation, and say so in `note`.
  - `except_refs` excludes roads whose `ref` starts with those prefixes.
- `"names"`: a plain list of street names, each meaning the whole street inside `bbox`.

Optional filters inside `scope`: `classes` and `exclude_classes`. Values are taken from `caotoc`, `quoclo`, `chinh`, `noido`, `nho`.

**`exempt`** uses the same `routes` form as `scope.routes`. It removes those pieces from the rule.

## `body` of a `cap` rule (speed limit for a vehicle type)

```json
{"vehicle": {"gross_t": [">", 3.5]}, "road_classes": {"caotoc": 90, "quoclo": 80, "chinh": 70, "noido": 50, "nho": 40}}
```

Only the 5 classes are allowed. The law speaks of road types (urban or not, dual carriageway or not), which these classes do not encode. Explain the chosen mapping in `note`.

## Not encoded (record in `unresolved` instead)

- Hazardous goods, passengers, motorbikes.
- Rules without a locatable place.
- Anything only visible on a sign photo.
