from __future__ import annotations
import random
from collections.abc import Sequence
from optifreight.models import (
    CargoClass, CityNode, EventKind, MAX_DRIVE_AXLE_KG, MAX_WEIGHT_KG,
    RoadEdge, RouteStep, ShipmentOffer, TachoState, TrailerType, TruckSpec, TruckState,
)
from optifreight.graph import RoadGraph
from optifreight.physics import (
    axle_ok, backhaul_ev, capacity_ok, compatible, drive_axle_kg,
    ldm_for, leg_cost, profit_ok, tacho_dock, tacho_drive,
)
from optifreight.optimizer import _advance, unwind_steps, Solution

def verify_solution(
    sol: Solution, graph: RoadGraph, truck: TruckSpec,
    offers: Sequence[ShipmentOffer], start_node: str, t0: int = 0,
) -> None:
    if sol is None: return
    offers_by_id = {o.id: o for o in offers}
    assert len(sol.steps) <= 2 * len(offers)
    curr_node, curr_tacho = start_node, TachoState(t0, 0, 0, t0)
    onboard: list[ShipmentOffer] = []
    picked, delivered, adr_tainted = set(), [], False
    calc_cost, calc_dist, calc_gross = 0, 0, 0
    for step in sol.steps:
        offer = offers_by_id[step.offer_id]
        tgt = offer.origin_id if step.kind is EventKind.PICKUP else offer.dest_id
        assert step.city_id == tgt
        leg = graph.get_leg(curr_node, tgt); assert leg is not None and leg.dist_m == step.leg_dist_m
        cargo_kg = sum(o.weight_kg for o in onboard); assert cargo_kg <= MAX_WEIGHT_KG
        t_drv = tacho_drive(TachoState(curr_tacho.clock, curr_tacho.cont_drive, curr_tacho.day_drive, curr_tacho.shift_start), leg.time_min); assert t_drv.clock == step.arrival_min
        c_leg = leg_cost(leg, cargo_kg, truck.base_cpkm, truck.rate_cptkm)
        assert c_leg == step.leg_cost_cents; calc_cost += c_leg; calc_dist += leg.dist_m
        earliest = offer.earliest_pickup_min if step.kind is EventKind.PICKUP else offer.earliest_delivery_min
        latest = offer.latest_pickup_min if step.kind is EventKind.PICKUP else offer.latest_delivery_min
        dock = tacho_dock(t_drv, earliest, latest, offer.dock_dwell_min); assert dock is not None
        t_dk, s_min = dock
        assert s_min == step.start_min and t_dk.clock == step.end_min
        assert t_dk.breaks_taken == step.breaks_on_leg and t_dk.rests_taken == step.rests_on_leg
        if step.kind is EventKind.PICKUP:
            assert offer.id not in picked and compatible(tuple(onboard), offer, truck.trailer, adr_tainted)
            picked.add(offer.id); onboard.append(offer)
            if offer.cargo_class is CargoClass.ADR_HAZMAT: adr_tainted = True
            assert capacity_ok(sum(o.weight_kg for o in onboard), sum(ldm_for(o, truck.trailer) or 0 for o in onboard))
        else:
            assert offer.id in picked and offer.id not in delivered and len(onboard) > 0 and onboard[-1].id == offer.id
            onboard.pop(); delivered.append(offer.id); calc_gross += offer.revenue_cents
        stack = tuple((o.weight_kg, ldm_for(o, truck.trailer) or 0) for o in onboard)
        ax = drive_axle_kg(stack); assert ax <= MAX_DRIVE_AXLE_KG and ax == step.drive_axle_kg_after
        assert step.used_kg_after == sum(o.weight_kg for o in onboard) and step.used_ldm_after == sum(ldm_for(o, truck.trailer) or 0 for o in onboard) and step.onboard_after == tuple(o.id for o in onboard)
        curr_node, curr_tacho = tgt, t_dk
    assert len(onboard) == 0 and curr_node == sol.final_node and curr_tacho.clock == sol.final_clock
    assert calc_gross == sol.gross_revenue_cents and calc_cost == sol.total_cost_cents and calc_dist == sol.total_dist_m
    ev = backhaul_ev(graph.nodes[curr_node]); assert ev == sol.backhaul_ev_cents
    net = calc_gross - calc_cost + ev
    assert net == sol.net_profit_cents and profit_ok(net, calc_dist, truck.min_net_profit_cents, truck.min_cents_per_km)
    assert tuple(delivered) == sol.delivered_ids

def brute_force_solve(
    graph: RoadGraph, truck: TruckSpec, offers: Sequence[ShipmentOffer],
    start_node: str, t0: int = 0,
) -> tuple[int, tuple[tuple[str, str], ...]] | None:
    offers_by_id = {o.id: o for o in sorted(offers, key=lambda x: x.id)}
    init_state = TruckState(start_node, t0, (), frozenset(), frozenset(), 0, 0, 0, 0, t0, False, 0, 0, 0, None)
    best_net, best_seq, stack = None, None, [init_state]
    while stack:
        curr = stack.pop()
        if not curr.onboard and curr.delivered:
            ev = backhaul_ev(graph.nodes[curr.node]); net = curr.revenue - curr.cost + ev
            if profit_ok(net, curr.dist_m, truck.min_net_profit_cents, truck.min_cents_per_km):
                steps = unwind_steps(curr.last_step)
                ev_seq = tuple((st.kind.value, st.offer_id) for st in steps)
                if best_net is None or net > best_net or (net == best_net and (best_seq is None or ev_seq < best_seq)):
                    best_net, best_seq = net, ev_seq
        succs: list[TruckState] = []
        if curr.onboard:
            oid = curr.onboard[-1]; o = offers_by_id[oid]; rem_ob = curr.onboard[:-1]
            items = tuple((offers_by_id[x].weight_kg, ldm_for(offers_by_id[x], truck.trailer) or 0) for x in rem_ob)
            axle, leg = drive_axle_kg(items), graph.get_leg(curr.node, o.dest_id)
            if axle <= MAX_DRIVE_AXLE_KG and leg is not None:
                o_ldm = ldm_for(o, truck.trailer) or 0
                nxt = _advance(EventKind.DELIVER, oid, o.dest_id, curr, leg, (o.earliest_delivery_min, o.latest_delivery_min, o.dock_dwell_min), rem_ob, -o.weight_kg, -o_ldm, axle, o.revenue_cents, curr.adr_tainted, frozenset({oid}), frozenset(), truck)
                if nxt is not None: succs.append(nxt)
        onboard_objs = tuple(offers_by_id[x] for x in curr.onboard)
        for oid in sorted(offers_by_id):
            if oid in curr.picked: continue
            o = offers_by_id[oid]; o_ldm = ldm_for(o, truck.trailer)
            if o_ldm is None or not compatible(onboard_objs, o, truck.trailer, curr.adr_tainted) or not capacity_ok(curr.used_kg + o.weight_kg, curr.used_ldm + o_ldm): continue
            new_objs = onboard_objs + (o,)
            items = tuple((x.weight_kg, ldm_for(x, truck.trailer) or 0) for x in new_objs)
            axle, leg = drive_axle_kg(items), graph.get_leg(curr.node, o.origin_id)
            if axle <= MAX_DRIVE_AXLE_KG and leg is not None:  # no lifo_forward_ok: that is a solver prune, not a rule
                tnt = curr.adr_tainted or (o.cargo_class is CargoClass.ADR_HAZMAT)
                nxt = _advance(EventKind.PICKUP, oid, o.origin_id, curr, leg, (o.earliest_pickup_min, o.latest_pickup_min, o.dock_dwell_min), curr.onboard + (oid,), o.weight_kg, o_ldm, axle, 0, tnt, frozenset(), frozenset({oid}), truck)
                if nxt is not None: succs.append(nxt)
        for s in reversed(succs): stack.append(s)
    return None if best_net is None else (best_net, best_seq)

def generate_random_instance(rng: random.Random) -> tuple[RoadGraph, TruckSpec, tuple[ShipmentOffer, ...], str]:
    c_ids = [f"C{i}" for i in range(rng.choice([4, 5]))]
    cities = tuple(CityNode(c, f"City_{c}", rng.randint(0, 150) * 100, rng.randint(0, 500)) for c in c_ids)
    pairs = set((c_ids[i], c_ids[(i + 1) % len(c_ids)]) for i in range(len(c_ids))) | {(u, v) for u in c_ids for v in c_ids if u != v and rng.randint(0, 1) == 1}
    edges = [RoadEdge(u, v, d := rng.randint(40, 700) * 1000, d // 1200, rng.randint(0, 20) * 100) for u, v in sorted(pairs)]
    tr = rng.choice([TrailerType.STANDARD_TILT, TrailerType.MEGA, TrailerType.REEFER]); truck = TruckSpec("TRK", tr, 120, 3, 0, 0 if rng.randint(0, 2) != 0 else rng.randint(10, 40))
    offers = []
    for i in range(rng.randint(1, 4)):
        orig = rng.choice(c_ids); dest = rng.choice([c for c in c_ids if c != orig])
        ccls = rng.choice([CargoClass.GENERAL, CargoClass.ADR_HAZMAT, CargoClass.FOOD_PHARMA, CargoClass.FRAGILE_ELECTRONICS])
        req_tr = tr if rng.randint(0, 2) != 0 else rng.choice(list(TrailerType))
        if ccls is CargoClass.FOOD_PHARMA: req_tr = TrailerType.REEFER
        elif ccls is CargoClass.ADR_HAZMAT and req_tr is TrailerType.REEFER: req_tr = TrailerType.STANDARD_TILT
        p_e = rng.randint(0, 600); p_l = p_e + rng.randint(60, 900); d_e = p_e + rng.randint(0, 600); d_l = d_e + rng.randint(120, 2400)
        offers.append(ShipmentOffer(f"O{i}", orig, dest, rng.randint(1, 10) * 1000, rng.randint(2, 10), 1200, rng.choice([2, 3]), rng.choice([1000, 1200, 1800]), rng.randint(0, 1) == 1, req_tr, ccls, p_e, p_l, d_e, d_l, rng.randint(300, 1500) * 100, rng.choice([30, 45, 60])))
    return RoadGraph(cities, tuple(edges)), truck, tuple(offers), rng.choice(c_ids)
