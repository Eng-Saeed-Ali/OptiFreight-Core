from __future__ import annotations

import pytest
from optifreight.models import (
    MAX_WEIGHT_KG,
    MAX_LDM_MM,
    TrailerType,
    CargoClass,
    CityNode,
    RoadEdge,
    ShipmentOffer,
    TachoState,
)
from optifreight.graph import Leg
from optifreight.physics import (
    SERVES,
    INCOMPATIBLE,
    capacity_ok,
    drive_axle_kg,
    axle_ok,
    compatible,
    edge_cost,
    leg_cost,
    tacho_drive,
    tacho_dock,
    backhaul_ev,
    profit_ok,
)


def _make_offer(
    offer_id: str,
    weight_kg: int,
    required_trailer: TrailerType = TrailerType.STANDARD_TILT,
    cargo_class: CargoClass = CargoClass.GENERAL,
) -> ShipmentOffer:
    return ShipmentOffer(
        id=offer_id,
        origin_id="ORIG",
        dest_id="DEST",
        weight_kg=weight_kg,
        pallet_count=6,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=True,
        required_trailer=required_trailer,
        cargo_class=cargo_class,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=200,
        latest_delivery_min=400,
        revenue_cents=50_000,
        dock_dwell_min=45,
    )


def test_capacity_ok_boundary() -> None:
    assert capacity_ok(0, 0) is True
    assert capacity_ok(MAX_WEIGHT_KG, MAX_LDM_MM) is True
    assert capacity_ok(MAX_WEIGHT_KG + 1, MAX_LDM_MM) is False
    assert capacity_ok(MAX_WEIGHT_KG, MAX_LDM_MM + 1) is False
    assert capacity_ok(25_000, 14_000) is False


def test_drive_axle_and_diminishing_load_trap() -> None:
    # Empty bed: tare only
    assert drive_axle_kg(()) == 2_500
    assert axle_ok(()) is True


def test_a3_tare_identity() -> None:
    from optifreight.models import TRACTOR_DRIVE_TARE_KG, TRAILER_TARE_KINGPIN_KG, FIFTH_WHEEL_DRIVE_SHARE_PERMILLE
    assert TRACTOR_DRIVE_TARE_KG == 800
    assert TRAILER_TARE_KINGPIN_KG == 2_000
    assert drive_axle_kg(()) == 2_500
    for k in (0, 1, 100, 10_000, 100_000):
        v1 = TRACTOR_DRIVE_TARE_KG + (-(-(TRAILER_TARE_KINGPIN_KG + k) * FIFTH_WHEEL_DRIVE_SHARE_PERMILLE) // 1000)
        v2 = 2_500 + (-(-k * FIFTH_WHEEL_DRIVE_SHARE_PERMILLE) // 1000)
        assert v1 == v2

    # Sanity check 1: 20_000 kg packed into front 5_000 mm fails drive axle limit (11_500 kg)
    # Yields 17_743 kg (~17.7 t > 11_500 kg)
    axle_front_heavy = drive_axle_kg([(20_000, 5_000)])
    assert axle_front_heavy == 17_743
    assert axle_ok([(20_000, 5_000)]) is False

    # Sanity check 2: Uniform 24_000 kg across full 13_600 mm passes drive axle limit
    # Yields 10_708 kg (~10.7 t <= 11_500 kg)
    axle_uniform = drive_axle_kg([(24_000, 13_600)])
    assert axle_uniform == 10_708
    assert axle_ok([(24_000, 13_600)]) is True

    # EC-B Diminishing-Load Trap:
    # front_cargo: 16_000 kg on the first 9_000 mm
    # rear_counterbalance: 8_000 kg on the remaining 4_600 mm (sitting behind trailer axle at 10_300 mm)
    # Total payload: 24_000 kg, total length: 13_600 mm
    front_cargo = (16_000, 9_000)
    rear_counterbalance = (8_000, 4_600)

    # When both are on board, rear cargo counterbalances nose -> drive axle load is 10_786 kg <= 11_500 kg
    assert drive_axle_kg([front_cargo, rear_counterbalance]) == 10_786
    assert axle_ok([front_cargo, rear_counterbalance]) is True

    # When rear cargo is unloaded first (LIFO pop), remaining front cargo produces 11_567 kg > 11_500 kg!
    assert drive_axle_kg([front_cargo]) == 11_567
    assert axle_ok([front_cargo]) is False


def test_compatibility_and_adr_tainting() -> None:
    offer_general = _make_offer("O_GEN", 5_000, TrailerType.STANDARD_TILT, CargoClass.GENERAL)
    offer_mega = _make_offer("O_MEGA", 5_000, TrailerType.MEGA, CargoClass.GENERAL)
    offer_hazmat = _make_offer("O_ADR", 5_000, TrailerType.STANDARD_TILT, CargoClass.ADR_HAZMAT)
    offer_food = _make_offer("O_FOOD", 5_000, TrailerType.STANDARD_TILT, CargoClass.FOOD_PHARMA)
    offer_elec = _make_offer("O_ELEC", 5_000, TrailerType.STANDARD_TILT, CargoClass.FRAGILE_ELECTRONICS)

    # 1. Trailer service compatibility
    # STANDARD_TILT truck cannot serve MEGA offer
    assert compatible((), offer_mega, TrailerType.STANDARD_TILT, False) is False
    # MEGA truck can serve both STANDARD_TILT and MEGA
    assert compatible((), offer_general, TrailerType.MEGA, False) is False or True
    assert compatible((), offer_mega, TrailerType.MEGA, False) is True
    assert compatible((), offer_general, TrailerType.MEGA, False) is True

    # 2. Incompatible cargo co-loading
    # HAZMAT cannot co-load with FOOD_PHARMA
    assert compatible((offer_hazmat,), offer_food, TrailerType.STANDARD_TILT, False) is False
    assert compatible((offer_food,), offer_hazmat, TrailerType.STANDARD_TILT, False) is False

    # HAZMAT cannot co-load with FRAGILE_ELECTRONICS
    assert compatible((offer_hazmat,), offer_elec, TrailerType.STANDARD_TILT, False) is False
    assert compatible((offer_elec,), offer_hazmat, TrailerType.STANDARD_TILT, False) is False

    # GENERAL can co-load with anything
    assert compatible((offer_general,), offer_hazmat, TrailerType.STANDARD_TILT, False) is True
    assert compatible((offer_general,), offer_food, TrailerType.STANDARD_TILT, False) is True

    # 3. EC-C Permanent ADR Residue Taint
    # Once adr_tainted is True, FOOD_PHARMA is rejected even on an empty truck!
    assert compatible((), offer_food, TrailerType.STANDARD_TILT, adr_tainted=True) is False
    # But GENERAL or another HAZMAT can still be loaded
    assert compatible((), offer_general, TrailerType.STANDARD_TILT, adr_tainted=True) is True
    assert compatible((), offer_hazmat, TrailerType.STANDARD_TILT, adr_tainted=True) is True


def test_tacho_drive_mid_edge_splitting() -> None:
    # 600 min drive on a single edge starting from fresh shift:
    # Step 1: drive 270 min (hits 4.5h continuous limit)
    # Step 2: take mandatory 45 min break (cont_drive = 0)
    # Step 3: drive 270 min (hits 9.0h daily limit: 270 + 270 = 540 min)
    # Step 4: take mandatory 660 min (11.0h) overnight rest (shift resets)
    # Step 5: drive remaining 60 min (600 - 270 - 270 = 60 min)
    # Total elapsed clock = 270 + 45 + 270 + 660 + 60 = 1305 min
    t0 = TachoState(clock=0, cont_drive=0, day_drive=0, shift_start=0, breaks_taken=0, rests_taken=0)
    t_after = tacho_drive(t0, 600)

    assert t_after.clock == 1_305
    assert t_after.cont_drive == 60
    assert t_after.day_drive == 60
    assert t_after.shift_start == 1_245
    assert t_after.breaks_taken == 1
    assert t_after.rests_taken == 1


def test_tacho_dock_waiting_and_amplitude() -> None:
    # 1. Early wait < 45 min does NOT reset cont_drive
    t1 = TachoState(clock=100, cont_drive=100, day_drive=100, shift_start=0)
    res1 = tacho_dock(t1, earliest_min=130, latest_min=300, dwell_min=45)
    assert res1 is not None
    st1, start1 = res1
    assert start1 == 130
    assert st1.clock == 175  # 130 + 45
    assert st1.cont_drive == 100  # not reset because wait was only 30 min
    assert st1.breaks_taken == 0

    # 2. Early wait >= 45 min resets cont_drive = 0 (counts as break)
    t2 = TachoState(clock=100, cont_drive=100, day_drive=100, shift_start=0)
    res2 = tacho_dock(t2, earliest_min=150, latest_min=300, dwell_min=45)
    assert res2 is not None
    st2, start2 = res2
    assert start2 == 150
    assert st2.clock == 195  # 150 + 45
    assert st2.cont_drive == 0  # reset because wait was 50 min >= 45 min
    assert st2.breaks_taken == 1

    # 3. Early wait >= 660 min resets full shift (counts as daily rest)
    t3 = TachoState(clock=100, cont_drive=100, day_drive=100, shift_start=0)
    res3 = tacho_dock(t3, earliest_min=800, latest_min=1000, dwell_min=45)
    assert res3 is not None
    st3, start3 = res3
    assert start3 == 800
    assert st3.clock == 845
    assert st3.cont_drive == 0
    assert st3.day_drive == 0
    assert st3.shift_start == 800
    assert st3.rests_taken == 1

    # 4. Shift amplitude overflow triggering pre-dock rest
    # Truck arrives at 850 min (shift elapsed = 850). Dwell is 60 min. 850 + 60 = 910 > 900 AMP!
    # Forces overnight rest at arrival: clock = 850 + 660 = 1510.
    t4 = TachoState(clock=850, cont_drive=100, day_drive=400, shift_start=0)
    res4 = tacho_dock(t4, earliest_min=0, latest_min=2000, dwell_min=60)
    assert res4 is not None
    st4, start4 = res4
    assert start4 == 1_510
    assert st4.clock == 1_570
    assert st4.shift_start == 1_510
    assert st4.cont_drive == 0
    assert st4.day_drive == 0
    assert st4.rests_taken == 1

    # 5. Missing latest_min window returns None
    t5 = TachoState(clock=200, cont_drive=0, day_drive=100, shift_start=100)
    assert tacho_dock(t5, earliest_min=200, latest_min=230, dwell_min=45) is None


def test_costs_backhaul_and_profit() -> None:
    # Edge cost: 100 km (100_000 m), base 100 c/km, rate 2 c/(t*km), toll 500 c
    # Carried weight = 10_000 kg (10 t)
    # Cost = ceil_div(100_000 * (100*1000 + 2*10000), 1_000_000) + 500
    #      = ceil_div(100_000 * (100_000 + 20_000), 1_000_000) + 500
    #      = ceil_div(100_000 * 120_000, 1_000_000) + 500
    #      = 12_000 + 500 = 12_500 cents
    e = RoadEdge(origin_id="A", dest_id="B", distance_m=100_000, drive_min=60, toll_cents=500)
    assert edge_cost(e, weight_kg=10_000, base_cpkm=100, rate_cptkm=2) == 12_500

    # Deadhead cost (weight = 0):
    # Cost = ceil_div(100_000 * 100_000, 1_000_000) + 500 = 10_000 + 500 = 10_500 cents
    assert edge_cost(e, weight_kg=0, base_cpkm=100, rate_cptkm=2) == 10_500

    leg = Leg(origin_id="A", dest_id="B", time_min=60, dist_m=100_000, edges=(e, e))
    assert leg_cost(leg, weight_kg=10_000, base_cpkm=100, rate_cptkm=2) == 25_000

    # Backhaul EV: 20_000 cents * 400 permille // 1000 = 8_000 cents
    city = CityNode(id="MUC", name="Munich", backhaul_ev_cents=20_000, return_prob_permille=400)
    assert backhaul_ev(city) == 8_000

    # Profit floor (F11 cross-multiplied)
    # Total dist = 200_000 m (200 km)
    # Case 1: net 30_000 cents, min_net = 20_000, min_cpkm = 120 (requires 200 * 120 = 24_000 cents) -> True
    assert profit_ok(net_cents=30_000, total_dist_m=200_000, min_net_profit_cents=20_000, min_cents_per_km=120) is True

    # Case 2: net 22_000 cents -> passes min_net (20_000), but 22_000 < 24_000 -> False
    assert profit_ok(net_cents=22_000, total_dist_m=200_000, min_net_profit_cents=20_000, min_cents_per_km=120) is False

    # Case 3: net 15_000 cents -> fails min_net -> False
    assert profit_ok(net_cents=15_000, total_dist_m=200_000, min_net_profit_cents=20_000, min_cents_per_km=120) is False

