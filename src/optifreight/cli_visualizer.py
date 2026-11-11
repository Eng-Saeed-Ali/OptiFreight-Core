from __future__ import annotations
import argparse
from collections.abc import Sequence
from pathlib import Path
from optifreight.models import (
    CargoClass, CityNode, MAX_LDM_MM, RoadEdge, ShipmentOffer, TrailerType, TruckSpec, ldm_for,
)
from optifreight.graph import RoadGraph
from optifreight.intake_gate import IntakeError, ingest_json_bundle
from optifreight.optimizer import Solution, solve

def format_eur(cents: int) -> str:
    sign = "-€ " if cents < 0 else "€ "
    c_abs = abs(cents)
    eur, rem = c_abs // 100, c_abs % 100
    s_eur = f"{eur:,}".replace(",", " ")
    return f"{sign}{s_eur},{rem:02d}"

def render_bed_ascii(stack_items: Sequence[tuple[str, int]], width_chars: int = 68) -> str:
    bed: list[str] = []
    cum, used = 0, 0
    for oid, ldm in stack_items:
        if ldm > 0 and used < width_chars:
            a = (cum * width_chars) // MAX_LDM_MM
            b = ((cum + ldm) * width_chars) // MAX_LDM_MM
            w = min(max(b - a, 1), width_chars - used)
            bed.append(oid[:w].center(w, "#"))
            used += w
        cum += ldm
    bed.append("." * max(0, width_chars - used))
    return f"[NOSE|{''.join(bed)}|DOOR>]"

def build_demo_scenario() -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str]:
    nodes = (
        CityNode("BER", "Berlin", 0, 0), CityNode("PRG", "Prague", 5_000, 1_000),
        CityNode("VIE", "Vienna", 8_000, 1_000), CityNode("MUC", "Munich", 10_000, 1_000),
        CityNode("WAW", "Warsaw", 4_000, 1_000), CityNode("MIL", "Milan", 25_000, 1_000),
    )
    edges = (
        RoadEdge("BER", "PRG", 350_000, 270, 5_000), RoadEdge("PRG", "BER", 350_000, 270, 5_000),
        RoadEdge("BER", "WAW", 570_000, 400, 8_000), RoadEdge("WAW", "BER", 570_000, 400, 8_000),
        RoadEdge("PRG", "VIE", 330_000, 240, 4_500), RoadEdge("VIE", "PRG", 330_000, 240, 4_500),
        RoadEdge("PRG", "MUC", 380_000, 260, 5_500), RoadEdge("MUC", "PRG", 380_000, 260, 5_500),
        RoadEdge("VIE", "MUC", 430_000, 300, 6_000), RoadEdge("MUC", "VIE", 430_000, 300, 6_000),
        RoadEdge("VIE", "MIL", 850_000, 600, 12_000), RoadEdge("MIL", "VIE", 850_000, 600, 12_000),
        RoadEdge("MUC", "MIL", 500_000, 360, 9_000), RoadEdge("MIL", "MUC", 500_000, 360, 9_000),
    )
    truck = TruckSpec("T_DEMO", TrailerType.STANDARD_TILT, base_cpkm=100, rate_cptkm=2)
    offers = (
        ShipmentOffer("OFFER_A", "BER", "PRG", 6_000, 8, 1_200, 3, 1_200, False, TrailerType.STANDARD_TILT, CargoClass.GENERAL, 0, 120, 300, 600, 180_000, 30),
        ShipmentOffer("OFFER_B", "BER", "VIE", 8_000, 12, 1_200, 3, 1_200, False, TrailerType.STANDARD_TILT, CargoClass.GENERAL, 0, 120, 600, 1500, 260_000, 30),
        ShipmentOffer("OFFER_C", "PRG", "VIE", 5_000, 6, 1_200, 3, 1_200, False, TrailerType.STANDARD_TILT, CargoClass.GENERAL, 350, 700, 700, 1800, 140_000, 30),
        ShipmentOffer("OFFER_D", "VIE", "MIL", 7_000, 9, 1_200, 3, 1_200, False, TrailerType.STANDARD_TILT, CargoClass.FRAGILE_ELECTRONICS, 700, 1800, 1800, 3500, 320_000, 45),
        ShipmentOffer("OFFER_E", "BER", "WAW", 10_000, 12, 1_200, 3, 1_200, False, TrailerType.STANDARD_TILT, CargoClass.ADR_HAZMAT, 0, 120, 450, 1000, 150_000, 45),
        ShipmentOffer("OFFER_F", "PRG", "MUC", 4_000, 6, 1_200, 3, 1_200, False, TrailerType.REEFER, CargoClass.FOOD_PHARMA, 300, 600, 600, 1200, 200_000, 30),
    )
    return RoadGraph(nodes, edges), truck, offers, "BER"

def render_solution_report(sol: Solution, offers_by_id: dict[str, ShipmentOffer], trailer: TrailerType) -> str:
    lines = [
        "=" * 80,
        " " * 20 + "OPTIFREIGHT-CORE DISPATCH & ROUTE REPORT",
        "=" * 80,
        "CHRONOLOGICAL ROUTE SCHEDULE:",
        "-" * 80,
    ]
    for i, st in enumerate(sol.steps, 1):
        lines.append(
            f"Step {i:2d} | {st.kind.value:7s} {st.offer_id:8s} at {st.city_id} | "
            f"Arr: {st.arrival_min}m | Dock: {st.start_min}m..{st.end_min}m | "
            f"Breaks: {st.breaks_on_leg} | Rests: {st.rests_on_leg}"
        )
        lines.append(
            f"        Payload: {st.used_kg_after:,} kg | Floor: {st.used_ldm_after} mm | "
            f"Drive Axle: {st.drive_axle_kg_after:,} kg".replace(",", " ")
        )
        items = tuple((x, ldm_for(offers_by_id[x], trailer) or 0) for x in st.onboard_after)
        lines.append(f"        Bed: {render_bed_ascii(items)}")
        lines.append("-" * 80)
    lines.extend([
        "=" * 80,
        "FINANCIAL & PHYSICAL LEDGER:",
        "-" * 80,
        f"Gross Revenue:      {format_eur(sol.gross_revenue_cents)}",
        f"Total Leg Costs:    {format_eur(sol.total_cost_cents)}",
        f"Backhaul EV:        {format_eur(sol.backhaul_ev_cents)}",
        "-" * 80,
        f"Net Profit:         {format_eur(sol.net_profit_cents)}",
        f"Total Distance:     {sol.total_dist_m // 1000} km ({sol.total_dist_m} m)",
        f"Final Clock:        {sol.final_clock} min ({sol.final_clock // 60}h {sol.final_clock % 60:02d}m)",
        f"Delivered Offers:   {', '.join(sol.delivered_ids)}",
        f"Search Expansions:  {sol.expansions} nodes",
        f"Optimality:         {'PROVEN' if sol.proven_optimal else 'BUDGET-LIMITED'}",
        "=" * 80,
    ])
    return "\n".join(lines)

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OptiFreight-Core Route Optimizer CLI")
    parser.add_argument("bundle", nargs="?", help="Optional path to optifreight.bundle/v1 JSON file")
    parser.add_argument("--max-expansions", type=int, default=50_000, help="Max B&B expansions")
    args = parser.parse_args(argv)
    if args.bundle:
        try:
            graph, truck, offers, start = ingest_json_bundle(Path(args.bundle).read_bytes())
        except (OSError, IntakeError) as exc:
            print(f"Intake error: {exc}")
            return 2
    else:
        graph, truck, offers, start = build_demo_scenario()
    sol = solve(graph, truck, offers, start, max_expansions=args.max_expansions)
    if sol is None:
        print("No feasible route found.")
        return 1
    print(render_solution_report(sol, {o.id: o for o in offers}, truck.trailer))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

