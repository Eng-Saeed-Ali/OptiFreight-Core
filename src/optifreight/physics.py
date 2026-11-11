from __future__ import annotations

from collections.abc import Sequence

from optifreight.models import (
    MAX_WEIGHT_KG, MAX_LDM_MM, KINGPIN_X_MM, TRAILER_AXLE_X_MM,
    FIFTH_WHEEL_DRIVE_SHARE_PERMILLE, TRACTOR_DRIVE_TARE_KG, TRAILER_TARE_KINGPIN_KG, MAX_DRIVE_AXLE_KG,
    TACHO_CONT_MIN, TACHO_BREAK_MIN, TACHO_DAY_MIN, TACHO_REST_MIN, TACHO_AMP_MIN,
    TrailerType, CargoClass, CityNode, RoadEdge, ShipmentOffer, TachoState,
    ceil_div, clearance_mm, ldm_for,
)
from optifreight.graph import Leg

SERVES: dict[TrailerType, frozenset[TrailerType]] = {
    TrailerType.STANDARD_TILT: frozenset({TrailerType.STANDARD_TILT}),
    TrailerType.MEGA: frozenset({TrailerType.STANDARD_TILT, TrailerType.MEGA}),
    TrailerType.REEFER: frozenset({TrailerType.REEFER, TrailerType.STANDARD_TILT}),
}

INCOMPATIBLE: frozenset[frozenset[CargoClass]] = frozenset({
    frozenset({CargoClass.ADR_HAZMAT, CargoClass.FOOD_PHARMA}),
    frozenset({CargoClass.ADR_HAZMAT, CargoClass.FRAGILE_ELECTRONICS}),
})

def capacity_ok(used_kg: int, used_ldm: int) -> bool:
    return used_kg <= MAX_WEIGHT_KG and used_ldm <= MAX_LDM_MM

def drive_axle_kg(stack_items: Sequence[tuple[int, int]]) -> int:
    span2 = 2 * (TRAILER_AXLE_X_MM - KINGPIN_X_MM)
    offset = 0
    moment = 0
    for w_i, ldm_i in stack_items:
        x2_i = 2 * offset + ldm_i
        moment += w_i * (2 * TRAILER_AXLE_X_MM - x2_i)
        offset += ldm_i
    kingpin_kg = TRAILER_TARE_KINGPIN_KG + ceil_div(max(moment, 0), span2)
    return TRACTOR_DRIVE_TARE_KG + ceil_div(kingpin_kg * FIFTH_WHEEL_DRIVE_SHARE_PERMILLE, 1000)

def axle_ok(stack_items: Sequence[tuple[int, int]]) -> bool:
    return drive_axle_kg(stack_items) <= MAX_DRIVE_AXLE_KG

def compatible(
    onboard_offers: Sequence[ShipmentOffer],
    new_offer: ShipmentOffer,
    trailer: TrailerType,
    adr_tainted: bool,
) -> bool:
    if new_offer.required_trailer not in SERVES[trailer]:
        return False
    if adr_tainted and new_offer.cargo_class is CargoClass.FOOD_PHARMA:
        return False
    return all(frozenset({a.cargo_class, new_offer.cargo_class}) not in INCOMPATIBLE for a in onboard_offers)

def edge_cost(edge: RoadEdge, weight_kg: int, base_cpkm: int, rate_cptkm: int) -> int:
    return ceil_div(edge.distance_m * (base_cpkm * 1000 + rate_cptkm * weight_kg), 1_000_000) + edge.toll_cents

def leg_cost(leg: Leg, weight_kg: int, base_cpkm: int, rate_cptkm: int) -> int:
    return sum(edge_cost(e, weight_kg, base_cpkm, rate_cptkm) for e in leg.edges)

def tacho_drive(tacho: TachoState, drive_min: int) -> TachoState:
    clock = tacho.clock
    cont, day, shift = tacho.cont_drive, tacho.day_drive, tacho.shift_start
    brk, rst = tacho.breaks_taken, tacho.rests_taken
    d = drive_min
    while d > 0:
        elapsed = clock - shift
        room = min(TACHO_CONT_MIN - cont, TACHO_DAY_MIN - day, TACHO_AMP_MIN - elapsed)
        if room <= 0:
            if day == TACHO_DAY_MIN or elapsed + TACHO_BREAK_MIN >= TACHO_AMP_MIN:
                clock += TACHO_REST_MIN
                cont, day, shift, rst = 0, 0, clock, rst + 1
            else:
                clock += TACHO_BREAK_MIN
                cont, brk = 0, brk + 1
            continue
        c = min(d, room)
        clock += c
        cont += c
        day += c
        d -= c
    return TachoState(clock, cont, day, shift, brk, rst)

def tacho_dock(
    tacho: TachoState, earliest_min: int, latest_min: int, dwell_min: int
) -> tuple[TachoState, int] | None:
    clock = tacho.clock
    cont, day, shift = tacho.cont_drive, tacho.day_drive, tacho.shift_start
    brk, rst = tacho.breaks_taken, tacho.rests_taken
    arrival = clock
    start = max(arrival, earliest_min)
    wait = start - arrival
    elapsed = arrival - shift
    if elapsed + wait + dwell_min > TACHO_AMP_MIN and wait < TACHO_REST_MIN:
        clock = arrival + TACHO_REST_MIN
        start = max(clock, earliest_min)
        shift, cont, day, rst = clock, 0, 0, rst + 1
    elif wait >= TACHO_REST_MIN:
        shift, cont, day, rst = start, 0, 0, rst + 1
    elif wait >= TACHO_BREAK_MIN:
        cont, brk = 0, brk + 1
    end = start + dwell_min
    if end > latest_min or (end - shift) > TACHO_AMP_MIN:
        return None
    return TachoState(end, cont, day, shift, brk, rst), start

def backhaul_ev(city: CityNode) -> int:
    return (city.backhaul_ev_cents * city.return_prob_permille) // 1000

def profit_ok(
    net_cents: int, total_dist_m: int, min_net_profit_cents: int, min_cents_per_km: int
) -> bool:
    return net_cents >= min_net_profit_cents and net_cents * 1000 >= min_cents_per_km * total_dist_m
