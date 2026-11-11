from __future__ import annotations
from dataclasses import FrozenInstanceError
import pytest
from optifreight.models import (
    MAX_WEIGHT_KG,
    MAX_LDM_MM,
    CLEARANCE_STANDARD_MM,
    CLEARANCE_MEGA_MM,
    KINGPIN_X_MM,
    TRAILER_AXLE_X_MM,
    FIFTH_WHEEL_DRIVE_SHARE_PERMILLE,
    TRACTOR_DRIVE_TARE_KG,
    TRAILER_TARE_KINGPIN_KG,
    MAX_DRIVE_AXLE_KG,
    TACHO_CONT_MIN,
    TACHO_BREAK_MIN,
    TACHO_DAY_MIN,
    TACHO_REST_MIN,
    TACHO_AMP_MIN,
    TrailerType,
    CargoClass,
    EventKind,
    ceil_div,
    clearance_mm,
    ldm_for,
    CityNode,
    RoadEdge,
    ShipmentOffer,
    TruckSpec,
    TachoState,
    RouteStep,
    TruckState,
)

def test_constants_and_helpers() -> None:
    assert MAX_WEIGHT_KG == 24_000
    assert MAX_LDM_MM == 13_600
    assert CLEARANCE_STANDARD_MM == 2_700
    assert CLEARANCE_MEGA_MM == 3_000
    assert KINGPIN_X_MM == 1_600
    assert TRAILER_AXLE_X_MM == 10_300
    assert FIFTH_WHEEL_DRIVE_SHARE_PERMILLE == 850
    assert TRACTOR_DRIVE_TARE_KG == 800
    assert TRAILER_TARE_KINGPIN_KG == 2_000
    assert MAX_DRIVE_AXLE_KG == 11_500
    assert TACHO_CONT_MIN == 270
    assert TACHO_BREAK_MIN == 45
    assert TACHO_DAY_MIN == 540
    assert TACHO_REST_MIN == 660
    assert TACHO_AMP_MIN == 900

    assert ceil_div(0, 5) == 0
    assert ceil_div(1, 5) == 1
    assert ceil_div(5, 5) == 1
    assert ceil_div(6, 5) == 2
    assert ceil_div(10, 3) == 4

    assert clearance_mm(TrailerType.STANDARD_TILT) == 2_700
    assert clearance_mm(TrailerType.REEFER) == 2_700
    assert clearance_mm(TrailerType.MEGA) == 3_000

def test_dataclass_instantiation_and_immutability() -> None:
    node = CityNode(id="BER", name="Berlin", backhaul_ev_cents=10_000, return_prob_permille=500)
    assert node.id == "BER"
    assert node.name == "Berlin"
    assert not hasattr(node, "__dict__")
    with pytest.raises(FrozenInstanceError):
        node.name = "Munich"  # type: ignore[misc]

    edge = RoadEdge(origin_id="BER", dest_id="MUC", distance_m=585_000, drive_min=360, toll_cents=4_500)
    assert edge.origin_id == "BER"
    assert not hasattr(edge, "__dict__")
    with pytest.raises(FrozenInstanceError):
        edge.toll_cents = 0  # type: ignore[misc]

    offer = ShipmentOffer(
        id="OFFER_01",
        origin_id="BER",
        dest_id="MUC",
        weight_kg=12_000,
        pallet_count=15,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_300,
        is_stackable=True,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=60,
        latest_pickup_min=180,
        earliest_delivery_min=420,
        latest_delivery_min=720,
        revenue_cents=120_000,
        dock_dwell_min=45,
    )
    assert offer.id == "OFFER_01"
    assert not hasattr(offer, "__dict__")
    with pytest.raises(FrozenInstanceError):
        offer.weight_kg = 10_000  # type: ignore[misc]

    truck = TruckSpec(
        id="TRUCK_01",
        trailer=TrailerType.MEGA,
        base_cpkm=110,
        rate_cptkm=2,
        min_net_profit_cents=25_000,
        min_cents_per_km=90,
    )
    assert truck.id == "TRUCK_01"
    assert not hasattr(truck, "__dict__")

    tacho = TachoState(clock=120, cont_drive=120, day_drive=120, shift_start=0, breaks_taken=0, rests_taken=0)
    assert tacho.clock == 120
    assert not hasattr(tacho, "__dict__")

    step = RouteStep(
        kind=EventKind.PICKUP,
        offer_id="OFFER_01",
        city_id="BER",
        arrival_min=60,
        start_min=60,
        end_min=105,
        leg_dist_m=0,
        leg_cost_cents=0,
        breaks_on_leg=0,
        rests_on_leg=0,
        used_kg_after=12_000,
        used_ldm_after=3_600,
        drive_axle_kg_after=7_500,
        onboard_after=("OFFER_01",),
        prev=None,
    )
    assert step.kind == EventKind.PICKUP
    assert not hasattr(step, "__dict__")

    state = TruckState(
        node="BER",
        clock=105,
        onboard=("OFFER_01",),
        delivered=frozenset(),
        picked=frozenset({"OFFER_01"}),
        used_kg=12_000,
        used_ldm=3_600,
        cont_drive=0,
        day_drive=0,
        shift_start=60,
        adr_tainted=False,
        revenue=120_000,
        cost=0,
        dist_m=0,
        last_step=step,
    )
    assert state.node == "BER"
    assert not hasattr(state, "__dict__")

def test_stackability_and_ldm_calculation() -> None:
    # 6 Euro pallets (1200 mm lengthwise, 3 per row)
    # Non-stackable: 6 pallets // 3 per row = 2 rows * 1200 = 2400 mm
    o_nonstack = ShipmentOffer(
        id="O_NS",
        origin_id="BER",
        dest_id="HAM",
        weight_kg=3_000,
        pallet_count=6,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_400,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=200,
        latest_delivery_min=400,
        revenue_cents=45_000,
    )
    assert ldm_for(o_nonstack, TrailerType.STANDARD_TILT) == 2_400
    assert ldm_for(o_nonstack, TrailerType.MEGA) == 2_400

    # Stackable with pallet_height = 1400 mm
    # Standard clearance = 2700: 2700 // 1400 = 1 tier -> rows = ceil(6 / 3) = 2 -> 2400 mm
    # Mega clearance = 3000: 3000 // 1400 = 2 tiers -> rows = ceil(6 / (3 * 2)) = 1 -> 1200 mm
    o_stack = ShipmentOffer(
        id="O_ST",
        origin_id="BER",
        dest_id="HAM",
        weight_kg=3_000,
        pallet_count=6,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_400,
        is_stackable=True,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=200,
        latest_delivery_min=400,
        revenue_cents=45_000,
    )
    assert ldm_for(o_stack, TrailerType.STANDARD_TILT) == 2_400
    assert ldm_for(o_stack, TrailerType.MEGA) == 1_200

    # Pallet height 2800 mm: fits in MEGA (3000 mm) but exceeds STANDARD_TILT (2700 mm)
    o_mega_only = ShipmentOffer(
        id="O_MEGA",
        origin_id="BER",
        dest_id="HAM",
        weight_kg=2_000,
        pallet_count=3,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=2_800,
        is_stackable=False,
        required_trailer=TrailerType.MEGA,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=200,
        latest_delivery_min=400,
        revenue_cents=60_000,
    )
    assert ldm_for(o_mega_only, TrailerType.STANDARD_TILT) is None
    assert ldm_for(o_mega_only, TrailerType.MEGA) == 1_200

def test_boundary_hygiene_f1_rejections() -> None:
    # ID regex and NUL byte rejections
    with pytest.raises(ValueError):
        CityNode(id="berlin", name="Berlin")  # lowercase not matching regex
    with pytest.raises(ValueError):
        CityNode(id="BER\x00LIN", name="Berlin")  # NUL in id
    with pytest.raises(ValueError):
        CityNode(id="BER", name="Berlin\x00")  # NUL in name
    with pytest.raises(ValueError):
        CityNode(id="BER", name="")  # empty name
    with pytest.raises(ValueError):
        CityNode(id="BER", name="Berlin", backhaul_ev_cents=-1)  # negative EV
    with pytest.raises(ValueError):
        CityNode(id="BER", name="Berlin", return_prob_permille=1001)  # permille > 1000

    # RoadEdge validation
    with pytest.raises(ValueError):
        RoadEdge(origin_id="BER", dest_id="BER", distance_m=10_000, drive_min=10)  # origin == dest
    with pytest.raises(ValueError):
        RoadEdge(origin_id="BER", dest_id="MUC", distance_m=0, drive_min=10)  # distance <= 0
    with pytest.raises(ValueError):
        RoadEdge(origin_id="BER", dest_id="MUC", distance_m=10_000, drive_min=0)  # drive_min <= 0
    with pytest.raises(ValueError):
        RoadEdge(origin_id="BER", dest_id="MUC", distance_m=10_000, drive_min=10, toll_cents=-5)

    # ShipmentOffer boundary validations
    base_kwargs = dict(
        id="O_01",
        origin_id="BER",
        dest_id="MUC",
        weight_kg=10_000,
        pallet_count=10,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=True,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=100,
        latest_pickup_min=200,
        earliest_delivery_min=300,
        latest_delivery_min=400,
        revenue_cents=50_000,
        dock_dwell_min=45,
    )

    # origin == dest
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"dest_id": "BER"}))  # type: ignore[arg-type]

    # weight <= 0 or > 24_000
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"weight_kg": 0}))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"weight_kg": 24_001}))  # type: ignore[arg-type]

    # Pallet height > CLEARANCE_MEGA_MM (3000)
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"pallet_height_mm": 3_001}))  # type: ignore[arg-type]

    # Inverted time windows
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"earliest_pickup_min": 250, "latest_pickup_min": 200}))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"earliest_delivery_min": 450, "latest_delivery_min": 400}))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"earliest_pickup_min": 350, "earliest_delivery_min": 300}))  # type: ignore[arg-type]

    # Revenue <= 0
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"revenue_cents": 0}))  # type: ignore[arg-type]

    # Static LDM overflow (> 13_600 mm)
    # 40 pallets * 1200 mm // 3 = 14 rows * 1200 = 16_800 mm > 13_600 mm
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"pallet_count": 40, "is_stackable": False}))  # type: ignore[arg-type]

    # Pallet height 2800 mm on STANDARD_TILT (clearance 2700) -> static infeasibility
    with pytest.raises(ValueError):
        ShipmentOffer(**(base_kwargs | {"pallet_height_mm": 2_800, "required_trailer": TrailerType.STANDARD_TILT}))  # type: ignore[arg-type]

    # TruckSpec validation
    with pytest.raises(ValueError):
        TruckSpec(id="T_01", trailer=TrailerType.MEGA, base_cpkm=-1, rate_cptkm=2)
    with pytest.raises(ValueError):
        TruckSpec(id="T_01", trailer=TrailerType.MEGA, base_cpkm=100, rate_cptkm=2, min_cents_per_km=-5)

