# OptiFreight-Core — Normative Technical Specification (`SPEC.md`)

**Phase:** 0-B & 1-B (Tier 2 Systems Specification — Gemini Argon)  
**Upstream Authority:** `BLUEPRINT.md` (§0–§4 — Claude Opus 5.5)  
**Downstream Executor:** Gemini 3.8 Flash High (Steps 1–7)

Every rule (`R0.1`–`R0.5`), filter (`F1`–`F11`), edge case (`EC-A`–`EC-F`), audit fix (`A1`–`A9`), and fitness function (`FF-1`–`FF-4`) from `BLUEPRINT.md` is locked below into concrete module layouts, dataclass schemas, and pure function signatures. Where `BLUEPRINT.md` §4 updates §0–§3, §4 takes strict precedence.

---

## 1. Directory & Module Layout

```text
c:/projects/OptiFreight_Core/
├── BLUEPRINT.md
├── SPEC.md
├── HANDOFF.md
├── ADR.md
├── src/
│   └── optifreight/
│       ├── __init__.py
│       ├── models.py                      # Step 1 + Step 6.0 fixes (<= 100 lines)
│       ├── graph.py                       # Step 2 (<= 100 lines)
│       ├── physics.py                     # Step 3 + Step 6.0 fixes (<= 115 lines)
│       ├── optimizer.py                   # Step 4 + Step 6.0 fixes (<= 135 lines)
│       ├── cli_visualizer.py              # Step 5 + Step 6.0 fixes (<= 115 lines)
│       └── intake_gate.py                 # Step 6 (<= 100 lines)
└── tests/
    ├── test_models.py                     # Step 1 tests
    ├── test_graph.py                      # Step 2 tests
    ├── test_physics.py                    # Step 3 tests
    ├── test_optimizer.py                  # Step 4, Step 5 & Step 6.0 tests
    ├── test_intake_gate.py                # Step 6 boundary & security tests
    ├── _oracle.py                         # Step 7 replay verifier & brute-force DFS oracle (<= 100 lines)
    └── test_architecture_fitness.py       # Step 7 AST fitness functions FF-1..FF-4 (<= 100 lines)
```

---

## 2. Global Invariants & Error-Handling Contract

1. **Zero Floating-Point (`R0.1` / `FF-1`):** No `float` or `complex` literals, no `float()` / `complex()` / `.fromhex()` calls, no `/` or `/=` division operators, and no `math`, `decimal`, `fractions`, or `statistics` imports in `src/`. Division uses `//` (floor) and `ceil_div(n, d) = -(-n // d)`. In tests, file discovery MUST use `pathlib.Path.glob` and `.joinpath(...)`—never the `/` path operator.
2. **Error Handling With Values:**
   - **Boundary hygiene (`F1` & `Step 6`):** Dataclass `__post_init__` hooks in `models.py` raise `ValueError` on malformed inputs; `intake_gate.py` raises `IntakeError(ValueError)` on envelope faults and records `OfferRejection` values for per-offer faults.
   - **Hot-loop constraint checks (`F2`–`F11`):** Pure functions in `physics.py` and `optimizer.py` **never raise exceptions** for infeasible routes; they return `bool`, `int`, or `None` so Branch-and-Bound expansion avoids stack-trace allocation overhead.
3. **Deterministic Ordering (`R0.5`):** Always iterate sets/frozensets via `sorted(...)`. Heap tuples always include a monotonic tie-breaker `seq: int` from `itertools.count()` before any dataclass payload.

---

## 3. Module-by-Module Signatures & Contracts

### 3.1 `src/optifreight/models.py` (Step 1 + Step 6.0 Audit Fixes `A3`, `A6`, `A9`)

#### Constants
```python
MAX_WEIGHT_KG: int = 24_000
MAX_LDM_MM: int = 13_600
CLEARANCE_STANDARD_MM: int = 2_700
CLEARANCE_MEGA_MM: int = 3_000
KINGPIN_X_MM: int = 1_600
TRAILER_AXLE_X_MM: int = 10_300
FIFTH_WHEEL_DRIVE_SHARE_PERMILLE: int = 850
TRACTOR_DRIVE_TARE_KG: int = 800
TRAILER_TARE_KINGPIN_KG: int = 2_000
MAX_DRIVE_AXLE_KG: int = 11_500
TACHO_CONT_MIN: int = 270
TACHO_BREAK_MIN: int = 45
TACHO_DAY_MIN: int = 540
TACHO_REST_MIN: int = 660
TACHO_AMP_MIN: int = 900
```
- **Audit Fix `A3`:** `TRACTOR_DRIVE_TARE_KG = 800` represents the bare tractor drive-axle tare; combined with `TRAILER_TARE_KINGPIN_KG = 2_000` at `850‰` fifth-wheel share, empty coupled drive-axle load is `800 + (2_000 * 850) // 1000 = 2_500 kg`.

#### Enums & Helpers
```python
class TrailerType(Enum):
    STANDARD_TILT = "STANDARD_TILT"
    MEGA = "MEGA"
    REEFER = "REEFER"

class CargoClass(Enum):
    GENERAL = "GENERAL"
    ADR_HAZMAT = "ADR_HAZMAT"
    FOOD_PHARMA = "FOOD_PHARMA"
    FRAGILE_ELECTRONICS = "FRAGILE_ELECTRONICS"

class EventKind(Enum):
    PICKUP = "PICKUP"
    DELIVER = "DELIVER"

def ceil_div(n: int, d: int) -> int: ...
def clearance_mm(trailer: TrailerType) -> int: ...
def ldm_for(offer: "ShipmentOffer", trailer: TrailerType) -> int | None: ...
```
- `clearance_mm(trailer)` returns `CLEARANCE_MEGA_MM` (`3_000`) if `trailer is TrailerType.MEGA` else `CLEARANCE_STANDARD_MM` (`2_700`).
- `ldm_for(offer, trailer)` implements **F3 + EC-A**:
  - If `offer.pallet_height_mm > clearance_mm(trailer)`: return `None`.
  - `tiers = (clearance_mm(trailer) // offer.pallet_height_mm) if offer.is_stackable else 1`
  - `rows = ceil_div(offer.pallet_count, offer.pallets_per_row * tiers)`
  - Return `rows * offer.pallet_len_mm`.

#### Immutable Slot Dataclasses (`@dataclass(slots=True, frozen=True)`)
- **`CityNode`**:
  - `id: str`, `name: str`, `backhaul_ev_cents: int = 0`, `return_prob_permille: int = 0`
  - `__post_init__` (`F1`): `id` matches `^[A-Z0-9_-]{1,32}\Z`; `name` non-empty and `"\x00" not in name`; `backhaul_ev_cents >= 0`; `0 <= return_prob_permille <= 1000`.
- **`RoadEdge`**:
  - `origin_id: str`, `dest_id: str`, `distance_m: int`, `drive_min: int`, `toll_cents: int = 0`
  - `__post_init__` (`F1`): valid IDs, `origin_id != dest_id`, `distance_m > 0`, `drive_min > 0`, `toll_cents >= 0`.
- **`ShipmentOffer`**:
  - `id: str`, `origin_id: str`, `dest_id: str`, `weight_kg: int`, `pallet_count: int`, `pallet_len_mm: int`, `pallets_per_row: int`, `pallet_height_mm: int`, `is_stackable: bool`, `required_trailer: TrailerType`, `cargo_class: CargoClass`, `earliest_pickup_min: int`, `latest_pickup_min: int`, `earliest_delivery_min: int`, `latest_delivery_min: int`, `revenue_cents: int`, `dock_dwell_min: int = 45`
  - `__post_init__` (`F1`):
    - Valid IDs (`id`, `origin_id`, `dest_id`) and `origin_id != dest_id`.
    - `0 < weight_kg <= MAX_WEIGHT_KG`; `pallet_count > 0`, `pallet_len_mm > 0`, `pallets_per_row > 0`, `0 < pallet_height_mm <= CLEARANCE_MEGA_MM`.
    - `0 <= earliest_pickup_min <= latest_pickup_min`, `earliest_delivery_min <= latest_delivery_min`, `earliest_delivery_min >= earliest_pickup_min`.
    - `revenue_cents > 0`, `dock_dwell_min > 0`.
    - Static feasibility check: `req_ldm = ldm_for(self, self.required_trailer)` and `mega_ldm = ldm_for(self, TrailerType.MEGA)`; if `req_ldm is None or req_ldm > MAX_LDM_MM or mega_ldm is None or mega_ldm > MAX_LDM_MM`: raise `ValueError`.
- **`TruckSpec`**:
  - `id: str`, `trailer: TrailerType`, `base_cpkm: int`, `rate_cptkm: int`, `min_net_profit_cents: int = 0`, `min_cents_per_km: int = 0`
  - `__post_init__` (`F1`): valid `id`, `base_cpkm >= 0`, `rate_cptkm >= 0`, `min_cents_per_km >= 0`.
- **`TachoState`**:
  - `clock: int`, `cont_drive: int`, `day_drive: int`, `shift_start: int`, `breaks_taken: int = 0`, `rests_taken: int = 0`
- **`RouteStep`** (`EC-F` persistent linked list + **Audit Fix `A6`**):
  - `kind: EventKind`, `offer_id: str`, `city_id: str`, `arrival_min: int`, `start_min: int`, `end_min: int`, `leg_dist_m: int`, `leg_cost_cents: int`, `breaks_on_leg: int`, `rests_on_leg: int`, `used_kg_after: int`, `used_ldm_after: int`, `drive_axle_kg_after: int`, `onboard_after: tuple[str, ...]`, `prev: RouteStep | None = field(default=None, compare=False, repr=False)`
- **`TruckState`** (**Audit Fix `A6`**):
  - `node: str`, `clock: int`, `onboard: tuple[str, ...]`, `delivered: frozenset[str]`, `picked: frozenset[str]`, `used_kg: int`, `used_ldm: int`, `cont_drive: int`, `day_drive: int`, `shift_start: int`, `adr_tainted: bool`, `revenue: int`, `cost: int`, `dist_m: int`, `last_step: RouteStep | None = field(default=None, compare=False, repr=False)`

---

### 3.2 `src/optifreight/graph.py` (Step 2)

```python
@dataclass(slots=True, frozen=True)
class Leg:
    origin_id: str
    dest_id: str
    time_min: int
    dist_m: int
    edges: tuple[RoadEdge, ...]

class RoadGraph:
    nodes: dict[str, CityNode]
    adj: dict[str, tuple[RoadEdge, ...]]

    def __init__(self, nodes: Iterable[CityNode], edges: Iterable[RoadEdge]) -> None: ...
    def get_leg(self, u: str, v: str) -> Leg | None: ...
    def t_min(self, u: str, v: str) -> int | None: ...
    def d_min(self, u: str, v: str) -> int | None: ...
```
- `__init__` verifies every edge's `origin_id` and `dest_id` exist in `nodes` (else raises `ValueError`), stores `adj[u]` sorted by `(e.dest_id, e.drive_min, e.distance_m, e.toll_cents)`, and precomputes all-pairs `Leg`s and distance-shortest `d_min` via Dijkstra.
- **Time-shortest Dijkstra (`get_leg`, `t_min`):** Priority key `(time_min, dist_m, node_path_tuple)`. For `u == v`, returns `Leg(u, u, 0, 0, ())`.
- **Distance-shortest (`d_min`):** Minimum total `distance_m` from `u` to `v` (`0` for `u == v`, `None` if unreachable).

---

### 3.3 `src/optifreight/physics.py` (Step 3 + Step 6.0 Audit Fix `A3`)

```python
SERVES: dict[TrailerType, frozenset[TrailerType]] = {
    TrailerType.STANDARD_TILT: frozenset({TrailerType.STANDARD_TILT}),
    TrailerType.MEGA: frozenset({TrailerType.STANDARD_TILT, TrailerType.MEGA}),
    TrailerType.REEFER: frozenset({TrailerType.REEFER, TrailerType.STANDARD_TILT}),
}
INCOMPATIBLE: frozenset[frozenset[CargoClass]] = frozenset({
    frozenset({CargoClass.ADR_HAZMAT, CargoClass.FOOD_PHARMA}),
    frozenset({CargoClass.ADR_HAZMAT, CargoClass.FRAGILE_ELECTRONICS}),
})

def capacity_ok(used_kg: int, used_ldm: int) -> bool: ...
def drive_axle_kg(stack_items: Sequence[tuple[int, int]]) -> int: ...
def axle_ok(stack_items: Sequence[tuple[int, int]]) -> bool: ...
def compatible(
    onboard_offers: Sequence[ShipmentOffer],
    new_offer: ShipmentOffer,
    trailer: TrailerType,
    adr_tainted: bool,
) -> bool: ...
def edge_cost(edge: RoadEdge, weight_kg: int, base_cpkm: int, rate_cptkm: int) -> int: ...
def leg_cost(leg: Leg, weight_kg: int, base_cpkm: int, rate_cptkm: int) -> int: ...
def tacho_drive(tacho: TachoState, drive_min: int) -> TachoState: ...
def tacho_dock(
    tacho: TachoState, earliest_min: int, latest_min: int, dwell_min: int
) -> tuple[TachoState, int] | None: ...
def backhaul_ev(city: CityNode) -> int: ...
def profit_ok(
    net_cents: int, total_dist_m: int, min_net_profit_cents: int, min_cents_per_km: int
) -> bool: ...
```
- Also re-export `ldm_for` and `clearance_mm` from `models.py`.
- **`drive_axle_kg(stack_items)` (`F5 + EC-B + Audit Fix A3`):** `stack_items` is a sequence of `(weight_kg, ldm_mm)` ordered from nose (`0`) to door (`-1`).
  - `span2 = 2 * (TRAILER_AXLE_X_MM - KINGPIN_X_MM)` (`17_400`)
  - `offset = 0`, `moment = 0`
  - For `(w_i, ldm_i)` in `stack_items`:
    `x2_i = 2 * offset + ldm_i`; `moment += w_i * (2 * TRAILER_AXLE_X_MM - x2_i)`; `offset += ldm_i`
  - `kingpin_kg = TRAILER_TARE_KINGPIN_KG + ceil_div(max(moment, 0), span2)`
  - `return TRACTOR_DRIVE_TARE_KG + ceil_div(kingpin_kg * FIFTH_WHEEL_DRIVE_SHARE_PERMILLE, 1000)`

---

### 3.4 `src/optifreight/optimizer.py` (Step 4 + Step 6.0 Audit Fixes `A1`, `A2`, `A4`, `A5`, `A8`, `A9`)

```python
@dataclass(slots=True, frozen=True)
class Solution:
    steps: tuple[RouteStep, ...]
    delivered_ids: tuple[str, ...]
    final_node: str
    final_clock: int
    gross_revenue_cents: int
    total_cost_cents: int
    backhaul_ev_cents: int
    net_profit_cents: int
    total_dist_m: int
    expansions: int
    proven_optimal: bool = True

def unwind_steps(last_step: RouteStep | None) -> tuple[RouteStep, ...]: ...
def lifo_forward_ok(graph: RoadGraph, stack_offers: Sequence[ShipmentOffer]) -> bool: ...
def upper_bound(
    state: TruckState,
    graph: RoadGraph,
    truck: TruckSpec,
    offers_by_id: dict[str, ShipmentOffer],
    ev_max: int,
) -> int: ...
def dominates(a: TruckState, b: TruckState) -> bool: ...
def solve(
    graph: RoadGraph,
    truck: TruckSpec,
    offers: Sequence[ShipmentOffer],
    start_node: str,
    t0: int = 0,
    max_expansions: int = 50_000,
) -> Solution | None: ...
```
- **`lifo_forward_ok(graph, stack_offers)` (`A8`):**
  For each consecutive pair `low = stack_offers[i]`, `up = stack_offers[i + 1]`:
  `t = graph.t_min(up.dest_id, low.dest_id)`
  If `t is None` or `up.earliest_delivery_min + up.dock_dwell_min + t + low.dock_dwell_min > low.latest_delivery_min`: return `False`.
- **Admissible Upper Bound (`upper_bound` — Audit Fix `A1`):**
  - `r_open = sum(offers_by_id[oid].revenue_cents for oid in state.onboard)` plus `sum(o.revenue_cents for oid, o in offers_by_id.items() if oid not in state.picked and compatible((), o, truck.trailer, state.adr_tainted) and graph.t_min(state.node, o.origin_id) is not None and o.latest_pickup_min >= state.clock + graph.t_min(state.node, o.origin_id))`
  - Notice `compatible((), o, truck.trailer, state.adr_tainted)` checks against an empty onboard tuple so temporary co-loading conflicts (such as `ADR_HAZMAT` currently onboard vs unpicked `FRAGILE_ELECTRONICS`) never make `upper_bound` underestimate the true multi-leg profit after `onboard` is delivered.
  - `max_d = max((graph.d_min(state.node, offers_by_id[oid].dest_id) or 0 for oid in state.onboard), default=0)`
  - `return state.revenue - state.cost + r_open + ev_max - ceil_div(truck.base_cpkm * max_d, 1000)`
- **Sound Exact-Tacho Dominance (`dominates(a, b)` — Audit Fixes `A2`, `A5`, & `A10`):**
  - Requires identical tachograph and clock coordinates: `a.clock == b.clock and a.cont_drive == b.cont_drive and a.day_drive == b.day_drive and a.shift_start == b.shift_start`.
  - Requires weak dominance on all three monotone metrics (`pa = a.revenue - a.cost >= pb = b.revenue - b.cost`, `a.dist_m <= b.dist_m`, `int(a.adr_tainted) <= int(b.adr_tainted)`) AND **strict** dominance on at least one (`pa > pb or a.dist_m < b.dist_m or int(a.adr_tainted) < int(b.adr_tainted)`).
  - Additionally, when `pa == pb` (`A10`), requires `_ev_seq(a.last_step) <= _ev_seq(b.last_step)` so a shorter-distance or untainted label never prunes an equal-profit label whose event prefix sorts earlier under `R0.5`.
- **Termination & Proven Optimality (`A4` & `A9`):**
  - When `solve` exits the `while pq and expansions < max_expansions` loop, if a best solution was found, return `Solution(..., expansions=expansions, proven_optimal=(not pq))` where `not pq` is `True` iff the heap was exhausted (or all remaining heap entries have `-neg_ub < best_net`).

---

### 3.5 `src/optifreight/cli_visualizer.py` (Step 5 + Step 6.0 Audit Fixes `A4`, `A7`)

```python
def format_eur(cents: int) -> str: ...
def render_bed_ascii(stack_items: Sequence[tuple[str, int]], width_chars: int = 68) -> str: ...
def build_demo_scenario() -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str]: ...
def render_solution_report(
    sol: Solution, offers_by_id: dict[str, ShipmentOffer], trailer: TrailerType
) -> str: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```
- `format_eur(145_050)` returns `"€ 1 450,50"` using integer division and modulo (`abs(cents) // 100`, `abs(cents) % 100`).
- **`render_bed_ascii` (`A7` cumulative rounding):**
  Uses `MAX_LDM_MM` (`13_600`) and cumulative boundaries `a = (cum * width_chars) // MAX_LDM_MM`, `b = ((cum + ldm) * width_chars) // MAX_LDM_MM`, `w = min(max(b - a, 1), max(0, width_chars - used))` for each item with `ldm > 0`, filling any remaining `width_chars - used` cells with `.`. A full `13_600 mm` load always renders `0` dots.
- **`render_solution_report` (`A4`):**
  Includes `f"Optimality:         {'PROVEN' if sol.proven_optimal else 'BUDGET-LIMITED'}"` in the ledger section.

---

### 3.6 `src/optifreight/intake_gate.py` (Step 6 — Imperative Boundary Gate, `<= 100 lines`)

#### Allowed Imports
`__future__`, `collections.abc`, `dataclasses`, `enum`, `json`, `re`, and `optifreight.{models, graph}`.

#### Constants & Types
```python
MAX_PAYLOAD_BYTES: int = 4_194_304
MAX_JSON_DEPTH: int = 8
MAX_CITIES: int = 256
MAX_EDGES: int = 4_096
MAX_OFFERS: int = 64
MAX_DECIMAL_CHARS: int = 24
MAX_INT_DIGITS: int = 15

class IntakeError(ValueError):
    code: str
    detail: str
    def __init__(self, code: str, detail: str = "") -> None: ...

@dataclass(slots=True, frozen=True)
class OfferRejection:
    index: int
    offer_id: str | None
    code: str
    detail: str
```
- `IntakeError` truncates `detail` to at most 64 characters and initializes `super().__init__(f"{code}: {self.detail}")`.

#### Pure Boundary Functions
```python
def parse_decimal_to_scaled_int(s: str, scale: int) -> int: ...
def ingest_json_bundle_report(
    raw_json: str | bytes,
) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str, tuple[OfferRejection, ...]]: ...
def ingest_json_bundle(
    raw_json: str | bytes,
) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str]: ...
```
1. **`parse_decimal_to_scaled_int(s: str, scale: int) -> int`:**
   - Requires `scale in (1, 10, 100, 1000)`; else raises `ValueError("Invalid scale")`. Let `k = len(str(scale)) - 1`.
   - Requires `type(s) is str`, `1 <= len(s) <= MAX_DECIMAL_CHARS`, and `"\x00" not in s`.
   - Matches strict ASCII regex `^(0|[1-9][0-9]{0,14})(?:\.([0-9]+))?\Z`. If not matched, or if a fractional part is present when `k == 0` or `len(frac) > k`, raises `ValueError` (extra decimal places are rejected, never rounded; scientific notation `e`/`E`, signs `+`/`-`, leading zeros `01`, and non-ASCII digits are rejected).
   - Returns `int(whole) * scale + (int(frac.ljust(k, "0")) if frac else 0)`.
2. **Ordered Ingestion Pipeline (`ingest_json_bundle_report` & `ingest_json_bundle`):**
   - **Stage 1 (Type, UTF-8 Byte Length & NUL Check):** Must be `str | bytes` (else raise `IntakeError("INVALID_TYPE")`). Encode `str` to UTF-8 catching `UnicodeEncodeError` (lone surrogates raise `IntakeError("INVALID_UTF8")`, `A11`). Measure UTF-8 byte length (`len(b)`). If `> MAX_PAYLOAD_BYTES`, raise `IntakeError("PAYLOAD_TOO_LARGE")`. Decode `bytes` with strict UTF-8 (else `IntakeError("INVALID_UTF8")`). If `"\x00" in text`, raise `IntakeError("NUL_BYTE")`.
   - **Stage 2 (Single-Pass Depth Pre-Scan):** Scan `text` tracking string literals (`"` and `\` escapes) and nesting depth of `{`/`[` vs `}`/`]`. If depth exceeds `MAX_JSON_DEPTH`, raise `IntakeError("DEPTH_EXCEEDED")`.
   - **Stage 3 (Zero-Float Hardened `json.loads`):**
     Call `json.loads(text, parse_float=str, parse_int=_bounded_int, parse_constant=_reject_constant, object_pairs_hook=_no_dup_keys)`:
     - `parse_float=str` keeps JSON decimal numbers as raw `str` tokens so **no Python `float` is ever instantiated**.
     - `_bounded_int(s)` rejects integer literals with `len(s.lstrip("-")) > MAX_INT_DIGITS` (`IntakeError("INT_OVERFLOW")`).
     - `_reject_constant(s)` raises `IntakeError("NON_FINITE_NUMBER")` on `NaN`, `Infinity`, `-Infinity`.
     - `_no_dup_keys(pairs)` raises `IntakeError("DUPLICATE_KEY")` if any JSON object contains duplicate keys.
   - **Stage 4 (`optifreight.bundle/v1` Closed-World Schema):**
     - Top-level object must have exact keys `{"schema", "start_node", "truck", "cities", "edges", "offers"}` with `schema == "optifreight.bundle/v1"`.
     - Collection caps: `1 <= len(cities) <= MAX_CITIES`, `len(edges) <= MAX_EDGES`, `len(offers) <= MAX_OFFERS`.
     - **City object keys:** `{"id", "name", "backhaul_ev_eur", "return_prob_permille"}` (`backhaul_ev_eur` optional default `"0.00"` scaled by `100`; `return_prob_permille` optional default `0` int).
     - **Edge object keys:** `{"origin_id", "dest_id", "distance_km", "drive_min", "toll_eur"}` (`distance_km` scaled by `1000` -> `distance_m`; `toll_eur` optional default `"0.00"` scaled by `100` -> `toll_cents`).
     - **Truck object keys:** `{"id", "trailer", "base_cpkm", "rate_cptkm", "min_net_profit_eur", "min_cents_per_km"}` (`min_net_profit_eur` optional default `"0.00"`, only field permitting an optional leading `-` sign before decimal parsing at scale `100`; `min_cents_per_km` optional default `0`).
     - **Offer object keys:** `{"id", "origin_id", "dest_id", "weight_t", "pallet_count", "pallet_len_m", "pallets_per_row", "pallet_height_m", "is_stackable", "required_trailer", "cargo_class", "earliest_pickup_min", "latest_pickup_min", "earliest_delivery_min", "latest_delivery_min", "revenue_eur", "dock_dwell_min"}` (`dock_dwell_min` optional default `45`; `weight_t`, `pallet_len_m`, `pallet_height_m` scaled by `1000`; `revenue_eur` scaled by `100`).
     - Any malformed offer (unknown key, bad decimal, unknown `origin_id`/`dest_id` not in `graph.nodes`, duplicate offer `id` where first wins, or `ValueError` from `ShipmentOffer`) appends an `OfferRejection(index, offer_id, code, detail[:64])` and skips that offer without aborting the bundle.
     - Valid offers are returned sorted by `o.id` (`R0.5`). `ingest_json_bundle(raw_json)` returns `ingest_json_bundle_report(raw_json)[:4]`.

---

### 3.7 `tests/test_architecture_fitness.py` & `tests/_oracle.py` (Step 7 — Fitness Functions & Oracle Fuzzing)

1. **`FF-1` (AST Zero-Float & Zero-Division Fitness Function):**
   Walks the AST of every `.py` file in `src/optifreight` and `tests` (discovered via `Path.glob` / `.joinpath`) asserting:
   - `0` `ast.Constant` nodes with `isinstance(n.value, (float, complex))`.
   - `0` `ast.Div` nodes (covers `/` and `/=`).
   - `0` `ast.Call` nodes invoking `float`, `complex`, or `.fromhex`.
   - `0` imports of `math`, `decimal`, `fractions`, or `statistics` in `src/optifreight`.
2. **`FF-2` (AST Functional-Core Isolation Fitness Function):**
   Verifies via `ast.walk` that every `Import` and `ImportFrom` (including function-local imports) in `src/optifreight/*.py` obeys the strict per-module allowlist:
   - `models.py`: `{"__future__", "re", "dataclasses", "enum"}`
   - `graph.py`: `{"__future__", "collections.abc", "dataclasses", "heapq", "itertools", "optifreight.models"}`
   - `physics.py`: `{"__future__", "collections.abc", "optifreight.models", "optifreight.graph"}`
   - `optimizer.py`: `{"__future__", "collections.abc", "dataclasses", "heapq", "itertools", "optifreight.models", "optifreight.graph", "optifreight.physics"}`
   - `intake_gate.py`: `{"__future__", "collections.abc", "dataclasses", "enum", "json", "re", "optifreight.models", "optifreight.graph"}`
   - `cli_visualizer.py`: `{"__future__", "argparse", "collections.abc", "optifreight.models", "optifreight.graph", "optifreight.optimizer"}`
   - Also bans calls to `eval`, `exec`, `compile`, `open`, `__import__`, and `input` across `src/optifreight/*.py`.
3. **`FF-3` (AST Immutable-Slot Dataclass Fitness Function):**
   Verifies that every `@dataclass` class in `src/optifreight/*.py` specifies `slots=True` and `frozen=True` as `ast.Constant(True)`, and asserts the discovered set contains all 10 dataclasses (`CityNode`, `RoadEdge`, `ShipmentOffer`, `TruckSpec`, `TachoState`, `RouteStep`, `TruckState`, `Leg`, `Solution`, `OfferRejection`).
4. **`FF-4` & `FF-4b` (50-Instance + 1,000-Instance Property Fuzzer & Brute-Force DFS Oracle in `tests/_oracle.py` — `A12` & `A13`):**
   - `verify_solution(sol, graph, truck, offers, start_node, t0=0) -> None`: Independently replays `sol.steps` from scratch using only `physics.py` functions, verifying `F1`–`F11` and `EC-A`–`EC-F`.
   - `brute_force_solve(graph, truck, offers, start_node, t0=0) -> tuple[int, tuple[tuple[str, str], ...]] | None`: Exhaustive event-sequence DFS with **no upper-bound pruning, no dominance pruning, and no `lifo_forward_ok` solver lookahead pruning (`A12`)**.
   - Runs 50 seeded instances (`Random(42)`) and 1,000 deep instances (`Random(2026)`) with non-vacuous coverage floors (`feasible >= 300`, `multi >= 50`, `breaks >= 300`, `rests >= 100`), asserting `verify_solution` passes, `sol.proven_optimal is True`, and `(sol.net_profit_cents, ev_seq) == brute_force_solve(...)`.

---

## 4. The Phase 1 & Phase 2 Micro-Stepping Execution Plan

| Step | Target File(s) | Target Test File(s) | Required Acceptance Tests |
|---|---|---|---|
| **1–5** | `models.py`, `graph.py`, `physics.py`, `optimizer.py`, `cli_visualizer.py` | `test_models.py`, `test_graph.py`, `test_physics.py`, `test_optimizer.py` | Baseline 24 unit tests (✅ COMPLETE). |
| **6.0** | `models.py`, `physics.py`, `optimizer.py`, `cli_visualizer.py` | `tests/test_optimizer.py`, `tests/test_physics.py` | Audit Fixes `A1`–`A9`: `H(ADR)->F(FRAGILE)` admissibility regression (`net == 139_200`), exact-tacho dominance (`dist_m` & strict tie retention), `TRACTOR_DRIVE_TARE_KG = 800` + `TRAILER_TARE_KINGPIN_KG = 2_000` identity check, `Solution.proven_optimal`, `RouteStep.prev` non-recursive `repr`/`==`, cumulative `render_bed_ascii` zero-dot full trailer check. |
| **6** | `src/optifreight/intake_gate.py`, `src/optifreight/__init__.py` | `tests/test_intake_gate.py` | `4 MiB` byte-cap, depth-8 scanner (ignoring brackets inside strings), `parse_decimal_to_scaled_int` (rejecting extra precision, sci-notation, `NaN`, non-ASCII digits, NUL bytes), duplicate JSON key rejection, partial offer rejection resilience, demo round-trip equivalence. |
| **7** | `tests/_oracle.py`, `tests/test_architecture_fitness.py` | `tests/test_architecture_fitness.py` | `FF-1` (zero-float/div AST), `FF-2` (import allowlist AST), `FF-3` (10 frozen+slots dataclasses AST), `FF-4` (50-seed `Random(42)` fuzzer + independent replay verifier + brute-force DFS optimality oracle). |
