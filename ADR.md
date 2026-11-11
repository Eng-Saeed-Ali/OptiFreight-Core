# OptiFreight-Core — Architecture Decision Records (`ADR.md`)

**Authority:** Tier 1 Chief Architect (Claude Opus 5.5) & Tier 2 Contract Engineer (Gemini Argon)  
**Methodology:** Prof. Dr. Wael Badawy's Architectural Governance Triad (*Governing Constraints → Engineering Decision → Rejected Alternatives → Operational Evidence & Review Trigger*)

---

## ADR-001: Fixed-Point Integer Scaling & Zero-Float Law (`R0.1`)

- **Status:** Accepted & Enforced (`FF-1`)
- **Governing Constraints:**
  - IEEE-754 binary floating-point arithmetic is non-associative (`(a + b) + c ≠ a + (b + c)`) and cannot represent base-10 currency (`0.10` EUR) or metric fractions (`0.2` LDM) exactly.
  - In a Best-First Branch-and-Bound (`heapq`) optimizer with dominance pruning, a `1 ULP` (`10⁻¹⁶`) rounding discrepancy between two equivalent paths flips priority-queue ordering and invalidates dominance comparisons across CPU architectures or Python builds.
- **Engineering Decision:**
  - Ban all `float` literals, `float()` casts, and `/` division operators across `src/` and `tests/`.
  - Represent every physical, temporal, and financial quantity as a 64-bit hardware integer (`RAX`/`RCX` registers):
    - Mass in **kilograms (`int`, `1..24_000 kg`)**
    - Length and height in **millimeters (`int`, `1..13_600 mm` bed, `2_700`/`3_000 mm` clearance)**
    - Road distance in **meters (`int`)**
    - Time in **integer minutes (`int`)**
    - Currency in **euro-cents (`int`)** and rates in **permille (`‰`, `0..1000`)**
  - All rational divisions use exact integer floor `//` or ceiling division `ceil_div(n, d) = -(-n // d)`.
  - Cross-multiply profit-per-kilometer floors (`net_cents * 1000 >= min_cents_per_km * total_dist_m`, `F11`) to avoid division altogether.
- **Rejected Alternatives:**
  - *IEEE-754 `float` with `math.isclose` tolerances:* Rejected because epsilon-tolerance breaks transitivity in Pareto dominance tables (`a ~ b` and `b ~ c` does not imply `a ~ c`).
  - *`decimal.Decimal` in hot loops:* Rejected due to `15–40×` object allocation and software-emulated arithmetic overhead compared to native integer instructions during Branch-and-Bound state expansion.
- **Operational Evidence & Review Trigger:**
  - Verified by `FF-1` AST inspection (`0` floats, `0` `/` operators across all modules) and subprocess determinism tests (`PYTHONHASHSEED=1` vs `999`).
  - *Review Trigger:* Re-evaluate only if sub-millimeter or sub-cent fractional pricing derivatives require scale factors exceeding `10¹²` where 64-bit integer products risk overflow (Python `int` is arbitrary-precision, but keeping values within `< 2⁶³` preserves single-word fast paths).

---

## ADR-002: Contiguous LIFO Stack & Doubled-Coordinate Lever-Arm Axle Physics (`F4`, `F5`, `EC-B`, `A3`)

- **Status:** Accepted & Enforced (`Step 3` & `Step 6.0`)
- **Governing Constraints:**
  - Rear-loading European box, reefer, and standard tilt trailers enforce strict Last-In, First-Out (`LIFO`) physical access: unloading a front pallet group without first unloading rear cargo requires a double-handling repack at a cross-dock.
  - EU Council Directive 96/53/EC restricts the tractor drive axle to `11_500 kg` (`MAX_DRIVE_AXLE_KG`). Removing rear cargo during an intermediate delivery shifts the remaining center of mass toward the kingpin (`EC-B` diminishing-load trap), which can overload the tractor drive axle mid-route.
- **Engineering Decision:**
  - Model `onboard` as an ordered tuple of offer IDs from nose (`index 0`) to rear door (`index -1`). Under LIFO (`F4`), occupied floor space is always a contiguous prefix `[0, ∑ ldmᵢ)` with zero interior gaps.
  - Compute lever-arm center-of-mass coordinates in **doubled millimeters** (`X2_i = 2 * offset_i + ldm_i` and `SPAN2 = 2 * (TRAILER_AXLE_X_MM - KINGPIN_X_MM) = 17_400`) so the midpoint of any odd-millimeter LDM remains an exact integer without division by `2`.
  - Reconcile empty tare constants (`A3`): `TRACTOR_DRIVE_TARE_KG = 800` (tractor drive-axle tare) and `TRAILER_TARE_KINGPIN_KG = 2_000` (empty trailer kingpin reaction) with `FIFTH_WHEEL_DRIVE_SHARE_PERMILLE = 850`, yielding an empty coupled drive-axle load of `800 + (2000 × 850) // 1000 = 2500 kg` and `10_708 kg` under a uniform `24_000 kg` full load (`<= 11_500 kg`).
  - Enforce `axle_ok` on **both** `PICKUP` (push) and `DELIVER` (pop).
- **Rejected Alternatives:**
  - *Full 3D Bin-Packing MILP inside Branch-and-Bound:* Rejected because 3D guillotine packing is strongly NP-hard per node expansion and unnecessary when standard Euro-pallet rows (`pallets_per_row`) and vertical stackability (`F3`) reduce trailer bed allocation to 1D LIFO prefix intervals.
  - *Checking axle weight only at pickup:* Rejected because `EC-B` proves a legal 24 t two-stop load (`10_786 kg` drive axle) becomes illegal (`11_567 kg > 11_500 kg`) the moment the rear counterbalance is unloaded.
- **Operational Evidence & Review Trigger:**
  - Verified by `test_drive_axle_and_diminishing_load_trap` and `test_ec_b_diminishing_load_axle_rejection`.
  - *Review Trigger:* If side-curtain loading (non-LIFO side access) is introduced for specific `STANDARD_TILT` contracts, add an explicit trailer capability flag and gap-tracking interval list.

---

## ADR-003: Event-Space Branch-and-Bound, Admissible Relaxation & Exact-Tacho Dominance (`A1`, `A2`, `A5`, `A10`, `EC-E`, `EC-F`)

- **Status:** Accepted & Enforced (`Step 4`, `Step 6.0` & `Phase 2-B`)
- **Governing Constraints:**
  - Searching over raw road-graph edges allows cyclic deadhead loops and blows up the branching factor on dense highway networks.
  - Phase 1-A and Phase 2-B adversarial audits (`BLUEPRINT.md` §4.1) uncovered subtle optimality bugs in naive pruning:
    1. **Non-Admissible Upper Bound (`A1`):** Filtering unpicked offers in `upper_bound` against *currently onboard* cargo prunes optimal multi-leg routes where a temporary conflict (`ADR_HAZMAT` onboard vs unpicked `FRAGILE_ELECTRONICS`) disappears as soon as the onboard cargo is delivered.
    2. **Unsound Tachograph, Distance & `R0.5` Dominance (`A2`, `A5`, `A10`):** Under EU 561/2006 (`F7`), `tacho_dock` triggers forced rests starting at `arrival`; a label arriving slightly later or closer to the 15h shift amplitude limit can be forced into a reset rest during dock wait and emerge with a *fresh* shift, outperforming an earlier label on subsequent legs. Furthermore, omitting `dist_m` from dominance can prune a shorter path that passes the `min_cents_per_km` floor (`F11`), and pruning an equal-profit path with a lexicographically smaller event prefix violates `R0.5`.
- **Engineering Decision:**
  - **Event-Space Branching (`EC-E`):** Branch strictly over `DELIVER(onboard[-1])` and `PICKUP(s)` events across precomputed time-shortest `Leg`s, bounding tree depth to `≤ 2n` and eliminating graph cycles by construction.
  - **Persistent History (`EC-F`):** Store route history as an immutable singly-linked list (`RouteStep.prev` with `compare=False, repr=False` per `A6`) for `O(1)` branching allocations and non-recursive equality/repr.
  - **Admissible Upper Bound (`A1`):** In `upper_bound`, test unpicked offers with `compatible((), o, truck.trailer, state.adr_tainted)` so only *permanent* state exclusions (equipment mismatch and irreversible `EC-C` hazmat food taint) are excluded from `r_open`.
  - **Sound Exact-Tacho + `R0.5` Dominance (`A2`, `A5`, `A10`):** Restrict dominance at key `(node, onboard, delivered)` to states with identical `(clock, cont_drive, day_drive, shift_start)`, requiring weak dominance on `(profit, -dist_m, -adr_tainted)`, **strict** dominance on at least one of those three, and `_ev_seq(a.last_step) <= _ev_seq(b.last_step)` whenever `pa == pb`.
- **Rejected Alternatives:**
  - *Component-wise inequality dominance on tachograph counters (`cont_drive <=`, `shift_start >=`):* Rejected by counterexample (`A2b`) because non-linear rest resets invert partial orders across dock waiting windows.
- **Operational Evidence & Review Trigger:**
  - Verified by the `H(ADR) -> F(FRAGILE)` regression probe (`net == 139_200`), `test_a10_dominance_respects_r05_lexicographic_tie_break`, and 1,050-instance brute-force DFS oracle fuzzing (`FF-4` & `FF-4b`).
  - *Review Trigger:* If problem instances scale beyond `n > 15` concurrent LTL offers per single-truck tour, introduce time-bucketed dominance classes accompanied by a formal monotonicity proof verified against the `FF-4` oracle.

---

## ADR-004: Zero-Float Imperative JSON Boundary Gate (`Step 6`)

- **Status:** Accepted & Enforced (`src/optifreight/intake_gate.py`)
- **Governing Constraints:**
  - External freight webhooks (`Trans.eu`, `TruKKer`, LLM tool calls) transmit decimal strings or JSON numbers (`"1450.50"`, `24.0`) and untrusted payloads that may contain memory-exhaustion bombs, deep nesting recursion bombs, `NaN`/`Infinity`, duplicate keys, or NUL bytes.
  - Calling Python's default `json.loads` instantiates IEEE-754 `float` objects for JSON fractional numbers, violating `R0.1` before boundary validation even runs.
- **Engineering Decision:**
  - Isolate all untrusted JSON ingestion inside `src/optifreight/intake_gate.py`:
    1. Enforce a strict `4 MiB` UTF-8 byte ceiling (`MAX_PAYLOAD_BYTES = 4_194_304`), NUL-byte rejection, and a single-pass string-aware bracket nesting pre-scan (`MAX_JSON_DEPTH = 8`) *before* invoking `json.loads`.
    2. Call `json.loads` with `parse_float=str`, `parse_int=_bounded_int` (`<= 15` digits), `parse_constant=_reject_constant`, and `object_pairs_hook=_no_dup_keys`.
    3. Convert decimal strings via `parse_decimal_to_scaled_int(s, scale)` using pure ASCII regex and integer string slicing (`int(whole) * scale + int(frac.ljust(k, "0"))`), rejecting excess fractional precision rather than rounding silently.
    4. Separate **envelope-fatal faults** (raising `IntakeError(ValueError)` with bounded 64-char detail) from **per-offer faults** (collected as immutable `OfferRejection` records while keeping valid offers).
- **Rejected Alternatives:**
  - *Parsing JSON floats then calling `round(val * 100)`:* Rejected because `R0.1` forbids floats and binary float parsing introduces representation artifacts prior to rounding.
  - *Aborting the entire batch on a single malformed spot offer:* Rejected because a single bad spot-market listing from an external exchange must not take down dispatch optimization for the remaining valid offers.
- **Operational Evidence & Review Trigger:**
  - Verified by `tests/test_intake_gate.py` and `FF-1`/`FF-2`.
  - *Review Trigger:* When schema `optifreight.bundle/v2` adds multi-currency support, require explicit integer exchange-rate permille tables inside the bundle envelope.

---

## ADR-005: AST Architectural Fitness Functions & Brute-Force Oracle Fuzzing (`Step 7`)

- **Status:** Accepted & Enforced (`tests/test_architecture_fitness.py` & `tests/_oracle.py`)
- **Governing Constraints:**
  - Manual code review alone cannot prevent architectural drift (accidental `/` path operators, unapproved stdlib imports in the functional core, missing `slots=True` on new dataclasses, or subtle heuristic over-pruning).
- **Engineering Decision:**
  - Embed four automated fitness functions (`FF-1` through `FF-4`) directly in the `pytest` suite:
    - `FF-1`: AST scanner enforcing zero `float`/`complex` literals, zero `float()` calls, and zero `/` operators across `src/` and `tests/`.
    - `FF-2`: AST import allowlist enforcing strict Functional Core / Imperative Shell separation across all 6 source modules, plus banning `eval`, `exec`, `compile`, `open`, `__import__`, and `input`.
    - `FF-3`: AST decorator check verifying all 10 dataclasses explicitly set `slots=True` and `frozen=True`.
    - `FF-4`: Deterministic `random.Random(42)` property fuzzer generating 50 multi-offer problem instances, validating every solution via an independent physics replay verifier (`verify_solution`) AND asserting exact optimality parity against an unpruned brute-force DFS oracle (`brute_force_solve`).
- **Rejected Alternatives:**
  - *External linter plugins (flake8/ruff custom rules):* Rejected due to the Zero-External-Packages constraint (`NO pip install`); Python's built-in `ast` module executes in `< 50 ms` inside `pytest` with zero external dependencies.
- **Operational Evidence & Review Trigger:**
  - Executed automatically on every `pytest` run.
  - *Review Trigger:* Update `FF-2` allowlists and `FF-3` dataclass count whenever a new module or domain dataclass is approved in `SPEC.md`.

