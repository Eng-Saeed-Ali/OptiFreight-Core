from __future__ import annotations

import os
import subprocess
import sys
import pytest

from optifreight.models import (
    CityNode,
    RoadEdge,
    ShipmentOffer,
    TruckSpec,
    TrailerType,
    CargoClass,
    EventKind,
)
from optifreight.graph import RoadGraph
from optifreight.optimizer import solve, unwind_steps, lifo_forward_ok, dominates
from optifreight.cli_visualizer import (
    format_eur,
    render_bed_ascii,
    build_demo_scenario,
    render_solution_report,
    main,
)


def _build_test_graph() -> RoadGraph:
    nodes = (
        CityNode("BER", "Berlin", backhaul_ev_cents=5_000, return_prob_permille=200),
        CityNode("LEI", "Leipzig", backhaul_ev_cents=5_000, return_prob_permille=200),
        CityNode("NUR", "Nuremberg", backhaul_ev_cents=5_000, return_prob_permille=200),
        CityNode("MUC", "Munich", backhaul_ev_cents=10_000, return_prob_permille=500),
    )
    edges = (
        RoadEdge("BER", "LEI", distance_m=160_000, drive_min=120),
        RoadEdge("LEI", "NUR", distance_m=250_000, drive_min=180),
        RoadEdge("NUR", "MUC", distance_m=170_000, drive_min=120),
    )
    return RoadGraph(nodes, edges)


def test_multistop_ltl_consolidation() -> None:
    graph = _build_test_graph()
    truck = TruckSpec("T1", TrailerType.STANDARD_TILT, base_cpkm=100, rate_cptkm=2, min_net_profit_cents=0, min_cents_per_km=0)

    # Offer A: BER -> MUC (long leg)
    # 15 non-stackable pallets (6000 mm LDM), 10_000 kg, 1500 EUR revenue
    offer_a = ShipmentOffer(
        id="A",
        origin_id="BER",
        dest_id="MUC",
        weight_kg=10_000,
        pallet_count=15,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=400,
        latest_delivery_min=1200,
        revenue_cents=150_000,
        dock_dwell_min=45,
    )

    # Offer B: LEI -> NUR (intermediate stop)
    # 15 non-stackable pallets (6000 mm LDM), 8_000 kg, 900 EUR revenue
    offer_b = ShipmentOffer(
        id="B",
        origin_id="LEI",
        dest_id="NUR",
        weight_kg=8_000,
        pallet_count=15,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=100,
        latest_pickup_min=300,
        earliest_delivery_min=250,
        latest_delivery_min=600,
        revenue_cents=90_000,
        dock_dwell_min=45,
    )

    # 1. Single offer A alone
    sol_a = solve(graph, truck, (offer_a,), "BER")
    assert sol_a is not None
    assert sol_a.delivered_ids == ("A",)
    single_profit = sol_a.net_profit_cents

    # 2. Both offers together -> Multi-stop consolidation
    sol_both = solve(graph, truck, (offer_a, offer_b), "BER")
    assert sol_both is not None
    # Strict LIFO door stack: B is loaded after A, so B must be delivered first!
    assert sol_both.delivered_ids == ("B", "A")
    assert sol_both.gross_revenue_cents == 240_000
    # Consolidated multi-stop route earns significantly more than single offer alone!
    assert sol_both.net_profit_cents > single_profit
    assert sol_both.net_profit_cents == 171_400


def test_f4_lifo_violating_sequence_rejection() -> None:
    graph = _build_test_graph()
    truck = TruckSpec("T1", TrailerType.STANDARD_TILT, base_cpkm=100, rate_cptkm=2)

    # Offer A: BER -> NUR (early deadline at NUR)
    offer_a = ShipmentOffer(
        id="A",
        origin_id="BER",
        dest_id="NUR",
        weight_kg=8_000,
        pallet_count=15,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=300,
        latest_delivery_min=450,  # Closes early!
        revenue_cents=100_000,
        dock_dwell_min=45,
    )

    # Offer B: LEI -> MUC (MUC is past NUR)
    offer_b = ShipmentOffer(
        id="B",
        origin_id="LEI",
        dest_id="MUC",
        weight_kg=8_000,
        pallet_count=15,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=100,
        latest_pickup_min=300,
        earliest_delivery_min=400,
        latest_delivery_min=800,
        revenue_cents=120_000,
        dock_dwell_min=45,
    )

    # If truck picks up A at BER, then picks up B at LEI:
    # B is at the door, but B's dest is MUC. To deliver A at NUR first is illegal under LIFO!
    # And to deliver B at MUC first would make truck miss A's 450 min deadline at NUR!
    # Thus both cannot be co-loaded. The optimizer must choose the more profitable single offer (B).
    sol = solve(graph, truck, (offer_a, offer_b), "BER")
    assert sol is not None
    assert sol.delivered_ids == ("B",)
    assert "A" not in sol.delivered_ids


def test_ec_b_diminishing_load_axle_rejection() -> None:
    graph = _build_test_graph()
    truck = TruckSpec("T1", TrailerType.STANDARD_TILT, base_cpkm=100, rate_cptkm=2)

    # Front-heavy cargo: 16_000 kg, 9_000 mm LDM, BER -> MUC
    offer_front = ShipmentOffer(
        id="FRONT",
        origin_id="BER",
        dest_id="MUC",
        weight_kg=16_000,
        pallet_count=21,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=400,
        latest_delivery_min=1200,
        revenue_cents=200_000,
        dock_dwell_min=45,
    )

    # Rear counterbalance: 8_000 kg, 4_600 mm LDM, BER -> NUR
    # Destined for NUR (intermediate stop before MUC)
    offer_rear = ShipmentOffer(
        id="REAR",
        origin_id="BER",
        dest_id="NUR",
        weight_kg=8_000,
        pallet_count=11,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=200,
        latest_delivery_min=600,
        revenue_cents=100_000,
        dock_dwell_min=45,
    )

    # If both loaded: axle ok. But if REAR is delivered at NUR, FRONT alone has drive axle 11_567 kg > 11_500 kg!
    # Therefore, dropping REAR at NUR while FRONT remains onboard must be rejected!
    sol = solve(graph, truck, (offer_front, offer_rear), "BER")
    assert sol is not None
    # Cannot deliver both with REAR dropped at NUR! Only REAR alone is legally deliverable.
    assert sol.delivered_ids == ("REAR",)


def test_ec_c_adr_hazmat_tainting() -> None:
    graph = _build_test_graph()
    truck = TruckSpec("T1", TrailerType.STANDARD_TILT, base_cpkm=100, rate_cptkm=2)

    # Offer 1: ADR HAZMAT, BER -> LEI
    offer_hazmat = ShipmentOffer(
        id="ADR",
        origin_id="BER",
        dest_id="LEI",
        weight_kg=5_000,
        pallet_count=6,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.ADR_HAZMAT,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=100,
        latest_delivery_min=300,
        revenue_cents=250_000,
        dock_dwell_min=45,
    )

    # Offer 2: FOOD_PHARMA, LEI -> MUC (pickup available after HAZMAT is unloaded at LEI!)
    offer_food = ShipmentOffer(
        id="FOOD",
        origin_id="LEI",
        dest_id="MUC",
        weight_kg=5_000,
        pallet_count=6,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.FOOD_PHARMA,
        earliest_pickup_min=180,
        latest_pickup_min=300,
        earliest_delivery_min=400,
        latest_delivery_min=800,
        revenue_cents=150_000,
        dock_dwell_min=45,
    )

    sol = solve(graph, truck, (offer_hazmat, offer_food), "BER")
    assert sol is not None
    # Once ADR has been on the trailer, adr_tainted is True and FOOD can never be loaded
    assert "FOOD" not in sol.delivered_ids
    assert sol.delivered_ids == ("ADR",)


def test_f11_profit_floor_rejection() -> None:
    graph = _build_test_graph()
    # Require 5000 EUR min profit on a scenario where max profit is ~800 EUR
    truck_strict = TruckSpec(
        "T_STRICT",
        TrailerType.STANDARD_TILT,
        base_cpkm=100,
        rate_cptkm=2,
        min_net_profit_cents=500_000,
        min_cents_per_km=500,
    )
    offer = ShipmentOffer(
        id="O1",
        origin_id="BER",
        dest_id="LEI",
        weight_kg=5_000,
        pallet_count=6,
        pallet_len_mm=1_200,
        pallets_per_row=3,
        pallet_height_mm=1_200,
        is_stackable=False,
        required_trailer=TrailerType.STANDARD_TILT,
        cargo_class=CargoClass.GENERAL,
        earliest_pickup_min=0,
        latest_pickup_min=100,
        earliest_delivery_min=100,
        latest_delivery_min=300,
        revenue_cents=50_000,
        dock_dwell_min=45,
    )
    sol = solve(graph, truck_strict, (offer,), "BER")
    assert sol is None


def test_cross_process_pythonhashseed_determinism() -> None:
    script = """
import os, sys
from optifreight.models import CityNode, RoadEdge, ShipmentOffer, TruckSpec, TrailerType, CargoClass
from optifreight.graph import RoadGraph
from optifreight.optimizer import solve

nodes = (
    CityNode("BER", "Berlin", 5000, 200),
    CityNode("LEI", "Leipzig", 5000, 200),
    CityNode("NUR", "Nuremberg", 5000, 200),
    CityNode("MUC", "Munich", 10000, 500),
)
edges = (
    RoadEdge("BER", "LEI", 160000, 120),
    RoadEdge("LEI", "NUR", 250000, 180),
    RoadEdge("NUR", "MUC", 170000, 120),
)
graph = RoadGraph(nodes, edges)
truck = TruckSpec("T1", TrailerType.STANDARD_TILT, 100, 2, 0, 0)
o_a = ShipmentOffer("A", "BER", "MUC", 10000, 15, 1200, 3, 1200, False, TrailerType.STANDARD_TILT, CargoClass.GENERAL, 0, 100, 400, 1200, 150000, 45)
o_b = ShipmentOffer("B", "LEI", "NUR", 8000, 15, 1200, 3, 1200, False, TrailerType.STANDARD_TILT, CargoClass.GENERAL, 100, 300, 250, 600, 90000, 45)

sol = solve(graph, truck, (o_a, o_b), "BER")
if sol:
    ev_seq = ",".join(f"{st.kind.value}:{st.offer_id}:{st.arrival_min}:{st.end_min}" for st in sol.steps)
    print(f"NET={sol.net_profit_cents};EXP={sol.expansions};SEQ={ev_seq}")
"""
    env1 = os.environ.copy()
    env1["PYTHONPATH"] = "src"
    env1["PYTHONHASHSEED"] = "1"
    res1 = subprocess.run([sys.executable, "-c", script], env=env1, capture_output=True, text=True, check=True)

    env2 = os.environ.copy()
    env2["PYTHONPATH"] = "src"
    env2["PYTHONHASHSEED"] = "999"
    res2 = subprocess.run([sys.executable, "-c", script], env=env2, capture_output=True, text=True, check=True)

    assert res1.stdout == res2.stdout
    assert "NET=171400" in res1.stdout


def test_format_eur() -> None:
    assert format_eur(145_050) == "€ 1 450,50"
    assert format_eur(-32_005) == "-€ 320,05"
    assert format_eur(0) == "€ 0,00"
    assert format_eur(99) == "€ 0,99"
    assert format_eur(-99) == "-€ 0,99"
    assert format_eur(100) == "€ 1,00"
    assert format_eur(1_000_000_00) == "€ 1 000 000,00"


def test_render_bed_ascii() -> None:
    empty = render_bed_ascii(())
    assert len(empty) == 81
    assert empty == "[NOSE|" + "." * 68 + "|DOOR>]"

    single = render_bed_ascii([("A", 2_400)])
    assert len(single) == 81
    assert single.startswith("[NOSE|") and single.endswith("|DOOR>]")
    assert "A" in single
    assert single.endswith("." * 56 + "|DOOR>]")

    two = render_bed_ascii([("FRONT", 4_800), ("REAR", 2_400)])
    assert len(two) == 81
    assert two.index("FRONT") < two.index("REAR")
    assert two.endswith("." * 32 + "|DOOR>]")


def test_routestep_predock_breaks_and_rests() -> None:
    # Verify that RouteStep accurately records pre-dock wait rests triggered in tacho_dock
    graph = RoadGraph(
        (CityNode("A", "Origin"), CityNode("B", "Destination")),
        (RoadEdge("A", "B", 100_000, 100),),
    )
    truck = TruckSpec("T", TrailerType.STANDARD_TILT, 100, 2, 0, 0)
    # Arrive at 100 min; delivery earliest is 800 min -> 700 min pre-dock wait triggers 1 rest
    offer = (
        ShipmentOffer(
            "O1", "A", "B", 1_000, 2, 1_200, 2, 1_000, False,
            TrailerType.STANDARD_TILT, CargoClass.GENERAL,
            0, 100, 800, 1_200, 50_000, 30,
        ),
    )
    sol = solve(graph, truck, offer, "A")
    assert sol is not None
    assert len(sol.steps) == 2
    step_deliver = sol.steps[1]
    assert step_deliver.rests_on_leg == 1


def test_cli_main_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])
    assert code == 0
    captured = capsys.readouterr().out
    assert "OPTIFREIGHT-CORE DISPATCH & ROUTE REPORT" in captured
    assert "CHRONOLOGICAL ROUTE SCHEDULE:" in captured
    assert "FINANCIAL & PHYSICAL LEDGER:" in captured
    assert "Gross Revenue:      € 9 000,00" in captured
    assert "Net Profit:         € 7 202,20" in captured
    assert "Optimality:         PROVEN" in captured
    assert "[NOSE|" in captured
    assert "|DOOR>]" in captured


def test_a1_upper_bound_admissibility_probe() -> None:
    graph = RoadGraph(
        (CityNode("A", "City A"), CityNode("B", "City B"), CityNode("C", "City C")),
        (RoadEdge("A", "B", 100_000, 60), RoadEdge("B", "C", 100_000, 60)),
    )
    truck = TruckSpec("T", TrailerType.STANDARD_TILT, base_cpkm=100, rate_cptkm=2)
    offer_h = ShipmentOffer("H", "A", "B", 2_000, 3, 1_200, 3, 1_000, False, TrailerType.STANDARD_TILT, CargoClass.ADR_HAZMAT, 0, 200, 0, 2_000, 60_000, 30)
    offer_f = ShipmentOffer("F", "B", "C", 2_000, 3, 1_200, 3, 1_000, False, TrailerType.STANDARD_TILT, CargoClass.FRAGILE_ELECTRONICS, 0, 2_000, 0, 2_000, 100_000, 30)
    sol = solve(graph, truck, (offer_h, offer_f), "A")
    assert sol is not None
    assert sol.delivered_ids == ("H", "F")
    assert sol.net_profit_cents == 139_200


def test_a4_proven_optimal_flag() -> None:
    graph = RoadGraph(
        (CityNode("A", "City A"), CityNode("B", "City B")),
        (RoadEdge("A", "B", 100_000, 100),),
    )
    truck = TruckSpec("T", TrailerType.STANDARD_TILT, 100, 2, 0, 0)
    offer = (
        ShipmentOffer("O1", "A", "B", 1_000, 2, 1_200, 2, 1_000, False, TrailerType.STANDARD_TILT, CargoClass.GENERAL, 0, 100, 100, 300, 50_000, 30),
    )
    sol_early = solve(graph, truck, offer, "A", max_expansions=1)
    if sol_early is not None:
        assert sol_early.proven_optimal is False
    sol_full = solve(graph, truck, offer, "A", max_expansions=50_000)
    assert sol_full is not None
    assert sol_full.proven_optimal is True


def test_a6_routestep_deep_recursion_hygiene() -> None:
    from optifreight.models import RouteStep
    step = RouteStep(EventKind.PICKUP, "O0", "C", 0, 0, 10, 100, 10, 0, 0, 100, 100, 2500, ("O0",), None)
    for i in range(1, 1_500):
        step = RouteStep(EventKind.PICKUP, f"O{i}", "C", i, i, i + 10, 100, 10, 0, 0, 100, 100, 2500, (f"O{i}",), step)
    assert repr(step) is not None
    step2 = RouteStep(EventKind.PICKUP, "O1499", "C", 1499, 1499, 1509, 100, 10, 0, 0, 100, 100, 2500, ("O1499",), None)
    assert step == step2


def test_a7_cumulative_bed_rendering() -> None:
    bed = render_bed_ascii([("A", 4_533), ("B", 4_533), ("C", 4_534)])
    assert len(bed) == 81
    assert "." not in bed[6:-7]
    bed_tiny = render_bed_ascii([("T", 100)])
    assert "T" in bed_tiny
    assert len(bed_tiny) == 81




def test_a10_dominance_respects_r05_lexicographic_tie_break() -> None:
    """A10 (Phase 2-B audit): equal profit + shorter distance must NOT prune a label whose event prefix sorts first."""
    from optifreight.models import RouteStep, TruckState

    def _chain(*ids: str) -> RouteStep | None:
        prev = None
        for oid in ids:
            prev = RouteStep(EventKind.PICKUP, oid, "X", 0, 0, 0, 1, 1, 0, 0, 0, 0, 0, (), prev)
        return prev

    def _st(dist: int, last: RouteStep | None, revenue: int = 1_000) -> TruckState:
        return TruckState("X", 500, ("A", "B"), frozenset(), frozenset({"A", "B"}), 0, 0, 0, 0, 0, False, revenue, 100, dist, last)

    shorter_but_later = _st(1_000, _chain("B", "A"))  # ('PICKUP','B') sorts after ('PICKUP','A')
    longer_but_first = _st(2_000, _chain("A", "B"))
    assert dominates(shorter_but_later, longer_but_first) is False  # b keeps its R0.5 claim
    assert dominates(longer_but_first, shorter_but_later) is False  # longer distance never dominates
    assert dominates(_st(1_000, _chain("A", "B")), _st(2_000, _chain("B", "A"))) is True
    assert dominates(_st(3_000, _chain("B", "A"), revenue=1_001), longer_but_first) is False  # dist worse
    assert dominates(_st(1_000, _chain("B", "A"), revenue=1_001), longer_but_first) is True  # strictly more profit
