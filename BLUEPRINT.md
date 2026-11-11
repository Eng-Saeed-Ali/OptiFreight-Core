# OptiFreight-Core — BLUEPRINT (Phase 0-A, Tier 1)

Pure Python 3.14, standard library only (`dataclasses`, `enum`, `heapq`, `itertools`, `argparse`).
Phase 0-B (SPEC.md / HANDOFF.md) must treat every equation here as **normative**.

---

## 0. Global Numeric Contract

| Quantity | Unit | Type | Hard bound |
|---|---|---|---|
| Mass | kg | `int` | `MAX_WEIGHT_KG = 24_000` |
| Floor length (LDM) | mm | `int` | `MAX_LDM_MM = 13_600` |
| Height | mm | `int` | `2_700` STANDARD_TILT / REEFER, `3_000` MEGA |
| Distance | **meters** | `int` | `> 0` per edge |
| Money | euro-cents | `int` | signed |
| Time | minutes since planning-horizon start `t0` | `int` | `>= 0` |
| Probabilities / shares | permille (‰) | `int` | `0..1000` |

**Rules**
- **R0.1 No `float` anywhere.** No `/`. Use `//` for floor division, plus `ceil_div(n, d) = -(-n // d)`.
- **R0.2 Round in the conservative direction:** costs, loads and times round **up** (`ceil_div`); revenues and EV round **down** (`//`).
- **R0.3 Round once per atomic unit (each edge), then add the integers.** Integer addition is associative, so a path cost does not depend on summation order.
- **R0.4 Compare ratios by cross-multiplying, never by dividing:** `a/b >= c/d  ⇔  a*d >= c*b` (with `b, d > 0`).
- **R0.5 Determinism:** never iterate over a `set`/`frozenset` of `str` without `sorted()`, because `PYTHONHASHSEED` randomizes the order. Heap entries are `(key..., seq:int, payload)` with a monotonic `itertools.count()` so the heap never compares dataclasses.

---

## 1. Filter Equations (11 baseline filters)

### F1 — Boundary hygiene (parse, don't validate)
These checks run in `__post_init__` and raise `ValueError` before any object is registered.
- IDs match `^[A-Z0-9_-]{1,32}$`; any `\x00` in any `str` field is rejected.
- `weight_kg, ldm inputs, height_mm, distance_m, drive_min > 0`; `toll_cents >= 0`; `revenue_cents > 0`.
- `origin_id != dest_id`; `earliest <= latest` for both windows; `earliest_delivery >= earliest_pickup`.
- Statically infeasible offers are rejected: `weight_kg > 24_000`, `ldm_mm > 13_600`, `height_mm > clearance(MEGA)`.

### F2 — Dual capacity (checked after every load event)
```
Σ w_i ≤ 24_000   ∧   Σ ldm_i ≤ 13_600      (i ∈ onboard stack)
```

### F3 — Stackability → per-shipment LDM (computed per truck, because clearance depends on trailer)
Each shipment has: `pallet_count`, `pallet_len_mm` (along the bed), `pallets_per_row` (across a 2_400 mm width; Euro 1200×800 lengthwise → 3), `pallet_height_mm`, `is_stackable`.
```
if pallet_height_mm > clearance_mm(trailer):     REJECT
tiers      = (clearance_mm // pallet_height_mm) if is_stackable else 1      # ≥ 1
per_row    = pallets_per_row * tiers
rows       = ceil_div(pallet_count, per_row)
ldm_mm     = rows * pallet_len_mm
```
**EC-A (cross-shipment stacking is forbidden):** pallets of different shipments never share a floor footprint. A top pallet bound for another stop would break LIFO and the axle model, so stacking happens only within a shipment.

### F4 — LIFO door stack
`onboard` is a `tuple[str, ...]` ordered by load sequence (index 0 = nose, `[-1]` = door).
- A delivery is legal **only** for `onboard[-1]`.
- Several deliveries at the same city are a sequence of pops at that node with zero travel between them, and each pop pays its own `dock_dwell_min`.
- **Invariant (falls out of LIFO):** occupied floor is always the contiguous prefix `[0, Σldm)`, so cargo positions are prefix sums. No gaps, and no repacking needed.

### F5 — Axle load (lever rule, doubled coordinates to stay integer)
Constants (configurable, in `models.py`): `KINGPIN_X_MM = 1_600`, `TRAILER_AXLE_X_MM = 10_300` (measured from the front wall), `FIFTH_WHEEL_DRIVE_SHARE_PERMILLE = 850`, `TRACTOR_DRIVE_TARE_KG = 2_500`, `TRAILER_TARE_KINGPIN_KG = 2_000`, `MAX_DRIVE_AXLE_KG = 11_500`.
```
offset_i   = Σ_{j<i} ldm_j                       # prefix sum in stack order
X2_i       = 2*offset_i + ldm_i                  # 2 × centre-of-mass position
SPAN2      = 2*(TRAILER_AXLE_X_MM - KINGPIN_X_MM)
moment     = Σ w_i * (2*TRAILER_AXLE_X_MM - X2_i)          # signed: cargo behind the axle lifts the kingpin
kingpin_kg = TRAILER_TARE_KINGPIN_KG + ceil_div(max(moment, 0), SPAN2)
drive_kg   = TRACTOR_DRIVE_TARE_KG + ceil_div(kingpin_kg * 850, 1000)
REQUIRE drive_kg ≤ 11_500
```
**EC-B (diminishing load):** run the check **after every pop as well as every push**. Under LIFO, a pop always removes the rearmost cargo, which moves the centre of mass forward. A route that is legal at full load can therefore become illegal halfway through. Sanity check: 20 t packed into the first 5 m gives `drive ≈ 17.7 t`, which fails. A uniform 24 t load gives `≈ 10.7 t`, which passes.

### F6 — Equipment and ADR segregation
```
SERVES = { STANDARD_TILT: {STANDARD_TILT},
           MEGA:          {STANDARD_TILT, MEGA},
           REEFER:        {REEFER, STANDARD_TILT} }
REQUIRE offer.required_trailer ∈ SERVES[truck.trailer]
INCOMPATIBLE = { {ADR_HAZMAT, FOOD_PHARMA}, {ADR_HAZMAT, FRAGILE_ELECTRONICS} }   # frozenset pairs
REQUIRE ∀ a ∈ onboard: frozenset({a.cls, new.cls}) ∉ INCOMPATIBLE
```
**EC-C (residue taint):** the state carries `adr_tainted: bool`, which becomes `True` once any `ADR_HAZMAT` has been loaded and stays `True` for the rest of the route. While it is set, `FOOD_PHARMA` cannot be loaded, even after the hazmat has been unloaded, because there is no trailer wash in the model.

### F7 — EU 561/2006 tachograph (deterministic state machine)
Tacho state: `cont_drive`, `day_drive`, `shift_start` (minutes). Derived: `shift_elapsed = clock - shift_start`.
Constants: `CONT=270, BREAK=45, DAY=540, REST=660, AMP=900`.

**`drive(d)`** splits driving inside an edge (**EC-D:** an edge longer than the remaining budget is cut mid-edge; we assume a lay-by is available):
```
while d > 0:
    room = min(CONT - cont_drive, DAY - day_drive, AMP - shift_elapsed)
    if room == 0:
        if day_drive == DAY or shift_elapsed + BREAK >= AMP: take_rest()
        else:                                                take_break()
        continue
    c = min(d, room); clock += c; cont_drive += c; day_drive += c; d -= c
take_break(): clock += 45;  cont_drive = 0                              # counts toward AMP
take_rest():  clock += 660; cont_drive = day_drive = 0; shift_start = clock
```
**`dock(arrival, earliest, latest, dwell)`:**
```
start = max(arrival, earliest); wait = start - arrival
if shift_elapsed(arrival) + wait + dwell > AMP and wait < REST:
     take_rest() at arrival; start = max(clock, earliest)
elif wait >= REST: reset as rest (shift_start = start, counters = 0)
elif wait >= BREAK: cont_drive = 0          # policy: idle wait ≥45 counts as a break
end = start + dwell                          # dwell is OTHER WORK: consumes AMP, does NOT reset cont_drive
REQUIRE end ≤ latest  ∧  end - shift_start ≤ AMP
```
Out of scope: weekly and fortnightly limits, the 10 h drive extension, and split breaks. Document these as `# NOT MODELLED`.

### F8 — Time windows
Service must **start ≥ earliest** and **end ≤ latest**. This is already enforced inside `dock()` (above) for pickups at origin and deliveries at destination. Windows are absolute horizon minutes, so multi-day windows are native.

### F9 — Weight-dependent edge cost (one rounding per edge)
`w` = onboard weight while traversing the edge. It is constant within a leg and equal to `0` when deadheading.
```
edge_cost = ceil_div(distance_m * (base_cpkm*1000 + rate_cptkm * w), 1_000_000) + toll_cents
leg_cost  = Σ edge_cost over the edges of the leg's path
```
(Units: `base` is ¢/km and `rate` is ¢/(t·km), so the formula reduces exactly to `km·(base + rate·t)`.)

### F10 — Backhaul EV
```
ev_cents = backhaul_ev_cents(final_node) * return_prob_permille(final_node) // 1000
```

### F11 — Profit floor (cross-multiplied, no division)
```
net = Σ revenue_delivered - Σ leg_cost + ev_cents
REQUIRE net ≥ min_net_profit_cents
REQUIRE net * 1000 ≥ min_cents_per_km * total_distance_m
```
A solution is only evaluated when `onboard == ()`, i.e. every picked offer has been delivered.

---

## 2. Algorithm — Best-First Branch-and-Bound over Shipment Events

**EC-E (no cycles by construction):** the search tree branches over **events** (`PICKUP(s)` / `DELIVER(s)`), not over raw graph nodes. Travel between two event cities is a precomputed leg. Each event fires at most once and `DELIVER(s)` requires `PICKUP(s)`, so depth is ≤ `2n` and city-level loops cannot occur.

**Leg routing (graph.py):** run Dijkstra from each node twice:
1. **Time-shortest** (tie-break: `distance_m`, then lexicographic node path). This is the leg actually driven, because tacho and windows are time-critical. Store the edge tuple so F9 can be costed for any `w`.
2. **Distance-shortest** `D_min[u][v]`, used **only** for admissible lower bounds.

(Documented simplification: the cost-optimal path depends on `w`. We drive time-optimal legs.)

**State (`TruckState`, frozen + slots):** `node, clock, onboard: tuple, delivered: frozenset, picked: frozenset, used_kg, used_ldm, cont_drive, day_drive, shift_start, adr_tainted, revenue, cost, dist_m, last_step: RouteStep | None`.
**EC-F (persistent linked list):** `RouteStep` holds a `prev` reference. Branches share their common history at O(1) cost per expansion, with no O(depth) tuple copying.

**Expansion order:** first `DELIVER(onboard[-1])`, then `PICKUP(s)` for unpicked `s` in sorted-ID order. Each successor runs F2–F8; any failure discards that child.

**Admissible upper bound (prune if `UB ≤ best_net`, strict improvement only):**
```
R_open  = Σ revenue(onboard) + Σ revenue(s unpicked, F6-compatible,
           latest_pickup(s) ≥ clock + T_min(node, origin(s)))
C_lb    = ceil_div(base_cpkm * max_{s∈onboard} D_min[node][dest(s)], 1000)   # must reach the farthest drop; w≥0, tolls≥0
UB      = revenue - cost + R_open + EV_MAX - C_lb
```

**LIFO forward-check (cheap prune at pickup):** for consecutive stack entries `lower`, `upper`:
`earliest_delivery(upper) + T_min(dest(upper), dest(lower)) + dwell > latest_delivery(lower)` ⇒ prune. The lower item can no longer be reached in time.

**Dominance:** `key = (node, onboard, delivered)`. Label A dominates B if
`A.clock ≤ B.clock ∧ A.revenue-A.cost ≥ B.revenue-B.cost ∧ A.cont_drive ≤ … ∧ A.day_drive ≤ … ∧ A.shift_start ≥ B.shift_start ∧ A.adr_tainted ≤ B.adr_tainted`. Dominated labels are dropped, and each key keeps a small Pareto list.

**Heap:** `heappush(pq, (-UB, depth, next(seq), state))`. A `max_expansions` budget makes the search anytime: it returns the best solution found so far. Ties in the final net are broken by the lexicographically smallest event sequence.

---

## 3. Module Layout and 5-Step Build Plan (each step ≤ 100 lines)

| Step | File | Contents |
|---|---|---|
| 1 | `models.py` | `Enum`s (`TrailerType`, `CargoClass`, `EventKind`), all constants from §0/§1, `ceil_div`, frozen+slots dataclasses `CityNode`, `RoadEdge`, `ShipmentOffer`, `TruckSpec`, `TruckState`, `RouteStep`, all F1 validation in `__post_init__`. |
| 2 | `graph.py` | `RoadGraph` (adjacency as `dict[str, tuple[RoadEdge, ...]]`), deterministic Dijkstra (time and distance variants), `Leg(time_min, dist_m, edges)` cache, `T_min`/`D_min` lookups. |
| 3 | `physics.py` | Pure functions: `ldm_for(offer, trailer)`, `capacity_ok`, `drive_axle_kg(stack)`, `compatible(onboard, offer, tainted)`, `edge_cost(edge, w)`, `leg_cost`, `tacho_drive(state, d)`, `tacho_dock(...)`, `backhaul_ev`, `profit_ok`. No I/O. |
| 4 | `optimizer.py` | `solve(graph, truck, offers, start_node, t0, params, max_expansions) -> Solution`: successor generation, UB, LIFO forward-check, dominance table, heap loop. |
| 5 | `cli_visualizer.py` | `argparse` demo with a built-in 6-city / 6-offer scenario, ASCII trailer-bed diagram (13.6 m scaled to 68 chars, nose→door), timeline including breaks and rests, and a cost/profit ledger (cents formatted as `€ 1 450,50` at print time only). |

**Acceptance tests for Phase 0-B to specify:** an axle overload after an intermediate drop (EC-B); a food-after-hazmat rejection (EC-C); a 600-min edge forcing break, rest, then break mid-edge (EC-D); a LIFO-violating sequence being unreachable; a byte-identical result across two runs with different `PYTHONHASHSEED`.

---

## 4. Phase 1-A — Adversarial Audit of Steps 1–5 and Phase 2 Architecture (Tier 1)

> **Precedence:** where §4 contradicts §0–§3, §4 wins. Phase 1-B (Argon) must fold these changes into `SPEC.md`.

### 4.1 Audit Findings (ranked)

| ID | Sev | Module | Finding | Evidence |
|---|---|---|---|---|
| **A1** | **P0** | `optimizer.upper_bound` | **The UB is not admissible.** `r_open` drops unpicked offers that are incompatible with the *current* `onboard`, but `onboard` is temporary. Once ADR is delivered, `FRAGILE_ELECTRONICS` becomes loadable again, because the taint only blocks FOOD. The optimal branch is pruned. | Probe: A→B `H`(ADR, €600) and B→C `F`(FRAGILE, €1 000). `solve` returns `('F',)` net **79 600**, but `H→F` is feasible with net **139 200**. |
| **A2** | **P0** | `optimizer.dominates` | **Dominance is unsound in two ways.** (a) `dist_m` is never compared, but F11 checks `net*1000 ≥ min_cpkm*dist_m`, so a "dominating" label with a longer distance can fail the floor that the pruned label would pass. (b) Tacho monotonicity is false under the greedy `tacho_dock` policy. A forced rest starts **at arrival**, so an earlier `clock` or later `shift_start` can mean less usable amplitude later. Conversely, a label that is closer to AMP can be forced into a reset rest and come out *ahead*. Neither "earlier clock" nor "more amplitude" is a safe partial order. | By construction from F7/F11. Example for (b): label `b` at AMP−600 rests at arrival during a 600-min wait (costs +60 min) and leaves fresh. Label `a`, which "dominates" it, works on and must rest 660 min on the next leg. |
| **A3** | P1 | `models`/`physics` F5 | `TRAILER_TARE_KINGPIN_KG` is a dead constant, and §1 F5 contradicts itself. Applying the §1 formula literally gives 24 t uniform → 12 408 kg (FAIL), yet §1 claims it is ≈10.7 t (PASS). This is a Phase 0-A error and the fault is mine. | `2_500 + ceil((2_000+9_656)·850/1000) = 12_408`. |
| **A4** | P1 | `optimizer.solve` | Anytime cut-off is silent. When `max_expansions` is hit, the result is returned as if it were optimal. | `while pq and expansions < max_expansions` has no flag. |
| **A5** | P2 | `optimizer.dominates` | Reflexive dominance (all fields equal ⇒ prune) keeps the label that was **explored first**, not the lexicographically smallest event sequence. This breaks the R0.5 tie-break contract (output stays deterministic but violates the contract). | `any(dominates(e, succ))` is true for `e == succ`. |
| **A6** | P2 | `models.RouteStep` | `prev` takes part in `__eq__` and `__repr__`. Comparison and repr therefore recurse O(depth), with a `RecursionError` risk past about 1 000 steps. The Step 4 failure trace was already a multi-KB repr. | Dataclass default `compare=True, repr=True`. |
| **A7** | P2 | `cli.render_bed_ascii` | Each segment is floored independently, so drift accumulates. Three 4 533 mm loads (a full 13.6 m) draw 66/68 chars and show a free gap that does not exist. Any segment under 200 mm disappears. `13_600` is hard-coded instead of using `MAX_LDM_MM`. | `(4533·68)//13600 = 22`; `3·22 = 66`. |
| **A8** | P3 | `optimizer.lifo_forward_ok` | The check is sound but weak: it leaves out the upper item's `dock_dwell_min` and only looks at adjacent pairs, never cumulative chains. | Necessary condition only, never over-prunes. |
| **A9** | P3 | `models.py` | Statements packed with semicolons go against the later "readable formatting" rule. `Solution.expansions` records the count at discovery, not the total. | Style / telemetry. |

**Verified correct (no action):** per-leg `breaks_on_leg/rests_on_leg`. `_advance` builds a fresh `TachoState(..., breaks_taken=0, rests_taken=0)` for every leg, and `t_dk` accumulates the drive and dock counters on top of it, so the Gate-6 fix is right. Also correct: revenue is credited at DELIVER and matches `r_open`; F9 is costed at pre-pop weight; the heap tuple never compares `TruckState`; all set iteration is sorted; `tacho_drive` always terminates (after a break, `elapsed+45 < 900` ⇒ `room > 0`); clamping `max(moment, 0)` matches physics, since cargo behind the axle only unloads the kingpin.

### 4.2 Step 6.0 — Mandatory Fix Pack (lands BEFORE Steps 6–7; Step 7's oracle WILL fail without it)

| Fix | Exact change |
|---|---|
| **A1** | In `upper_bound`, replace `compatible(onboard_objs, o, ...)` with `compatible((), o, truck.trailer, state.adr_tainted)`. Only *permanent* exclusions remain (equipment `SERVES` and the EC-C taint), which keeps the relaxation admissible. Regression test: the probe above must return `delivered_ids == ("H","F")`, `net == 139_200`. |
| **A2** | Replace `dominates` with **sound-by-construction** dominance: `a.clock == b.clock ∧ a.cont_drive == b.cont_drive ∧ a.day_drive == b.day_drive ∧ a.shift_start == b.shift_start ∧ (a.revenue−a.cost) ≥ (b.revenue−b.cost) ∧ a.dist_m ≤ b.dist_m ∧ a.adr_tainted ≤ b.adr_tainted`. Combined with the key `(node, onboard, delivered)`, the future feasible sets are identical and the only differences are profit, distance, and taint, which is monotone. Relaxing this again requires a written proof plus a clean Step 7 fuzz run. |
| **A5** | Also in `dominates`: if `a` and `b` tie on profit, `dist_m`, and taint, return `False` (keep both labels). R0.5 lexicographic tie-breaking then happens at solution-compare time. |
| **A3** | Keep the §1 formula **verbatim** (`kingpin_kg = TRAILER_TARE_KINGPIN_KG + ceil_div(max(moment,0), SPAN2)`). Set **`TRACTOR_DRIVE_TARE_KG = 800`**, redefined as the tractor's own drive-axle tare. The empty combination is then `800 + 2_000·850/1000 = 2_500 kg`. Because `2_000·850` is divisible by 1 000, `ceil(( 2000+k)·850/1000) ≡ 1_700 + ceil(k·850/1000)` for every integer `k`. Every existing number stays **bit-identical** (10 708 / 17 743 / 10 786 / 11 567 / empty 2 500 / demo ledger) and every constant is used. Add an assertion test for this identity. |
| **A4** | Add a final field `proven_optimal: bool` to `Solution` (= heap exhausted, not budget-stopped). The CLI prints `Optimality: PROVEN` or `BUDGET-LIMITED`. |
| **A6** | `prev: RouteStep \| None = field(default=None, compare=False, repr=False)`. Apply the same to `TruckState.last_step`. |
| **A7** | Use cumulative rounding: `a = cum*W // MAX_LDM_MM`, `b = (cum+ldm)*W // MAX_LDM_MM`, `w = max(b−a, 1)` if `ldm > 0`. Clamp the total to `W`. A full trailer renders 0 dots. |
| A8/A9 | Optional. Tighten with `+ up.dock_dwell_min`. De-pack `models.py` if the line budget allows. |

### 4.3 Step 6 — `src/optifreight/intake_gate.py` (Imperative Shell Boundary, ≤ 100 lines)

**Role:** this is the *only* place untrusted bytes (Trans.eu/TruKKer webhooks, LLM-extracted JSON) become domain objects. The rule is "parse, don't validate": once data passes the gate, the core never re-checks provenance. Allowed imports: `json`, `re`, `dataclasses`, `enum`, `collections.abc`, and `optifreight.{models,graph}`.

**Constants:** `MAX_PAYLOAD_BYTES = 4_194_304`, `MAX_JSON_DEPTH = 8`, `MAX_CITIES = 256`, `MAX_EDGES = 4_096`, `MAX_OFFERS = 64` (B&B is exponential, so this is a guard, not a throughput target), `MAX_DECIMAL_CHARS = 24`, `MAX_INT_DIGITS = 15`.

**Pipeline (ordered; every stage is O(n) and fails fast):**
1. **Type and size:** require `bytes | str`. For `str`, encode to UTF-8 first and measure in **bytes**. If the size is over `MAX_PAYLOAD_BYTES`, raise `IntakeError("PAYLOAD_TOO_LARGE")`. Decode bytes with strict `utf-8`. Reject `\x00` anywhere (`NUL_BYTE`).
2. **Depth pre-scan (before `json.loads`):** a single-pass character scanner. Track `in_string` and handle `\\` escapes correctly. Count `[`/`{` minus `]`/`}`. Exceeding `MAX_JSON_DEPTH` raises `DEPTH_EXCEEDED`. This blocks recursion bombs before the C parser allocates anything.
3. **Hardened parse:** `json.loads(text, parse_float=str, parse_int=_bounded_int, parse_constant=_reject_constant, object_pairs_hook=_no_dup_keys)`.
   - `parse_float=str` means **no `float` object ever exists**. Numeric literals such as `1450.50` arrive as their raw source string and are routed to the decimal parser.
   - `_bounded_int` rejects literals longer than `MAX_INT_DIGITS` (int-DoS).
   - `_reject_constant` kills `NaN`/`Infinity`.
   - `_no_dup_keys` rejects duplicate keys, which defeats parser-differential smuggling.
4. **Schema `optifreight.bundle/v1`:** top-level keys must be exactly `{schema, start_node, truck, cities, edges, offers}`. Unknown keys are rejected everywhere (closed world).

**`parse_decimal_to_scaled_int(s: str, scale: int) -> int`:**
- `scale ∈ {1, 10, 100, 1000}`; `k = len(str(scale)) − 1` (number of fractional digits allowed).
- Require `type(s) is str`, `len(s) ≤ MAX_DECIMAL_CHARS`, no `\x00`, then a full match on `^(0|[1-9][0-9]{0,14})(?:\.([0-9]{1,k}))?\Z`. When `k == 0` the fraction group is disallowed. This rejects `-`, `+`, `e/E` (sci-notation bombs), whitespace, `.5`, `01`, `1.`, `1_000`, and Unicode digits (use `[0-9]`, not `\d`).
- The result is `int(whole) * scale + int(frac.ljust(k, "0"))`.
- **Extra precision is REJECTED, not rounded** (`"1.2345"` at scale 1000 raises `ValueError`). Silent rounding at the boundary is a money bug.
- Examples: `("1450.50",100) → 145_050`; `("24.0",1000) → 24_000` (t→kg); `("1.20",1000) → 1_200` (m→mm); `("350",1000) → 350_000` (km→m).

**Wire-unit map (the boundary owns every unit conversion):**
- `*_eur` is a decimal parsed at scale 100 (→ cents).
- `weight_t` is a decimal parsed at scale 1000 (→ kg).
- `pallet_len_m` and `pallet_height_m` are decimals parsed at scale 1000 (→ mm).
- `distance_km` is a decimal parsed at scale 1000 (→ m).
- `*_min`, `*_permille`, `*_cpkm`, `*_cptkm`, and counts are JSON ints. `bool` is rejected wherever an int is expected (`type(v) is int`).
- Enums are matched exactly by `.value`.
- `min_net_profit_eur` is the only field allowed a leading `-`. Strip the sign, parse the rest, then negate.

**Error model (two tiers):**
- **Envelope-fatal**: size, depth, bad JSON, bad schema, bad truck, cities, edges, or `start_node`, or `RoadGraph` construction failure. These raise `IntakeError(ValueError)` with a closed `code: str` and a `detail` truncated to 64 chars. Untrusted text is never echoed unbounded.
- **Per-offer**: a bad field, an unknown origin/dest node, a duplicate offer id (the first one wins), or a `ShipmentOffer.__post_init__` `ValueError` produce an `OfferRejection(index: int, offer_id: str | None, code: str, detail: str)` (frozen+slots). The offer is skipped and the batch continues.

**Signatures:**
```python
def parse_decimal_to_scaled_int(s: str, scale: int) -> int: ...
def ingest_json_bundle_report(raw_json: str | bytes) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str, tuple[OfferRejection, ...]]: ...
def ingest_json_bundle(raw_json: str | bytes) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str]: ...  # = report[:4]
```
Offers come back sorted by `id` (R0.5). `ingest_json_bundle` keeps the exact 4-tuple from the directive, and rejections are exposed through `_report`.

**Step 6 acceptance tests (`tests/test_intake_gate.py`):**
- 4 MiB+1 byte is rejected and exactly 4 MiB is accepted. Measure in bytes: a `str` made of `€` characters crosses the limit sooner.
- Depth 9 is rejected, depth 8 is accepted, and `"[[[["` *inside a string* does not count toward depth.
- `1e999999`, `NaN`, `-1.00`, `"1.234"`@100, `"٣"` (Arabic-Indic digit), and `"1\x00"` are all rejected.
- Duplicate keys are rejected.
- A bare JSON number `1450.50` comes back as exactly `145_050`.
- A bundle of 3 good and 2 bad offers yields 3 offers and 2 rejections with the correct codes.
- Round-trip: the demo scenario serialized to v1 JSON → `ingest_json_bundle` → `solve` must give a ledger byte-identical to `main([])`.

### 4.4 Step 7 — `tests/test_architecture_fitness.py` (Fitness Functions and Oracle Fuzzing, ≤ 100 lines + helper)

Discover files with `pathlib.Path.glob` and `.joinpath`. **Never use the `/` path operator**: it is an `ast.Div` and would trip the rule it enforces.

1. **FF-1 Zero-float:** for every `.py` under `src/optifreight` and `tests`, count each of the following and require the total to be 0: `ast.Constant` holding a `float` or `complex`, `ast.Div` (which also covers `/=`), and `ast.Call` where `func` is `Name(id ∈ {"float","complex"})` or `Attribute(attr == "fromhex")`. Report `file:line` for every offence. Ban `import math`, `decimal`, `fractions`, and `statistics` in `src/`.
2. **FF-2 Functional-core isolation:** an exact per-module import allowlist, checked against `ast.Import`/`ast.ImportFrom` **including nested or function-level imports** (use `ast.walk`, not just `module.body`):
   - `models`: `{__future__, re, dataclasses, enum}`
   - `graph`: `{__future__, collections.abc, dataclasses, heapq, itertools, optifreight.models}`
   - `physics`: `{__future__, collections.abc, optifreight.models, optifreight.graph}`
   - `optimizer`: `{__future__, collections.abc, dataclasses, heapq, itertools, optifreight.models, optifreight.graph, optifreight.physics}`
   - `intake_gate` and `cli_visualizer` are the shell. They additionally allow `json` and `argparse` respectively, and nothing else (no `os`, `sys`, `socket`, `subprocess`, `pickle`, `importlib`, `builtins`).
   - Also ban calls to `eval`, `exec`, `compile`, `open`, `__import__`, and `input` anywhere in `src/`.
3. **FF-3 Immutable slots:** every `ClassDef` decorated with `dataclass` must use the **call** form with keywords `slots=True` and `frozen=True` as `ast.Constant(True)`. A bare `@dataclass` fails. Enumerate the classes found and assert the expected set (≥ 10: 7 models, `Leg`, `Solution`, `OfferRejection`), so that a parser regression cannot pass vacuously.
4. **FF-4 Oracle fuzzing (`random.Random(42)`, ints only via `randint`/`choice`):** run 50 instances. Each has 4–5 cities on a random strongly connected digraph, 1–4 offers, and ADR/FRAGILE/FOOD/REEFER mixed in with probability ≥ 30 %, so A1-style paths actually occur. `min_cents_per_km > 0` is set in about a third of the instances (exercises A2a). Windows are tight enough that about 25 % of instances force breaks or rests.
   - **Replay verifier** `verify_solution(...)`: rebuild the trajectory from `sol.steps` using only the public physics functions and assert every rule, independently of the optimizer:
     - each offer is PICKed ≤ 1×, and DELIVER follows its PICKUP;
     - every DELIVER pops `onboard[-1]` (F4);
     - F2 and F5 hold after every push **and** every pop (EC-B);
     - F6 and the taint hold, with no FOOD after any ADR (EC-C);
     - recomputed tacho values equal the recorded `arrival/start/end/breaks/rests` (F7, F8, EC-D);
     - Σ `leg_cost_cents` == `total_cost_cents` (F9);
     - the EV is correct (F10) and `profit_ok` holds (F11);
     - Σ delivered revenue == `gross_revenue_cents`;
     - the final onboard is empty, and `len(steps) ≤ 2n` (EC-E).
   - **Brute-force oracle:** an exhaustive DFS over all event sequences that uses the *same* successor rules, with **no UB and no dominance**. Assert `(sol is None) == (best is None)` and `sol.net_profit_cents == best_net`. Also assert `sol.proven_optimal`.
   - **Determinism:** two `solve` calls give identical `steps` tuples.
   - On failure, print the seed index and a minimal JSON repro, which the Step 6 v1 schema can then reload.

**Ordering constraint:** Step 6.0 → Step 6 → Step 7. If FF-4 finds any optimality gap after the fix pack, open an `unknown` in HANDOFF, then fall back to **UB-only search (dominance disabled)** while the investigation runs. Never weaken the oracle.

### 4.5 Updated Build Plan

| Step | Files | Budget |
|---|---|---|
| 6.0 | `optimizer.py`, `models.py`, `physics.py` (constant only), `cli_visualizer.py`, plus regression tests in `tests/test_optimizer.py` | Existing budgets |
| 6 | `src/optifreight/intake_gate.py`, `tests/test_intake_gate.py` | ≤ 100 lines src |
| 7 | `tests/test_architecture_fitness.py` (+ `tests/_oracle.py` helper if it exceeds 100 lines) | ≤ 100 + ≤ 100 |
