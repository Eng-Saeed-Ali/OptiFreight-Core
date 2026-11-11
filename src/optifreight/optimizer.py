from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass
import heapq, itertools
from optifreight.models import (
    CargoClass, EventKind, MAX_DRIVE_AXLE_KG, RouteStep, ShipmentOffer,
    TachoState, TruckSpec, TruckState, ceil_div, ldm_for,
)
from optifreight.graph import Leg, RoadGraph
from optifreight.physics import (
    backhaul_ev, capacity_ok, compatible, drive_axle_kg, leg_cost, profit_ok, tacho_dock, tacho_drive,
)

@dataclass(slots=True, frozen=True)
class Solution:
    steps: tuple[RouteStep, ...]
    delivered_ids: tuple[str, ...]
    final_node: str; final_clock: int
    gross_revenue_cents: int; total_cost_cents: int
    backhaul_ev_cents: int; net_profit_cents: int
    total_dist_m: int; expansions: int
    proven_optimal: bool = True

def unwind_steps(last_step: RouteStep | None) -> tuple[RouteStep, ...]:
    steps: list[RouteStep] = []
    while last_step is not None:
        steps.append(last_step)
        last_step = last_step.prev
    return tuple(reversed(steps))

def lifo_forward_ok(graph: RoadGraph, stack_offers: Sequence[ShipmentOffer]) -> bool:
    for i in range(len(stack_offers) - 1):
        low, up = stack_offers[i], stack_offers[i + 1]
        t = graph.t_min(up.dest_id, low.dest_id)
        if t is None or up.earliest_delivery_min + up.dock_dwell_min + t + low.dock_dwell_min > low.latest_delivery_min:
            return False
    return True

def upper_bound(
    state: TruckState, graph: RoadGraph, truck: TruckSpec,
    offers_by_id: dict[str, ShipmentOffer], ev_max: int,
) -> int:
    r_open = sum(offers_by_id[x].revenue_cents for x in state.onboard)
    for oid, o in offers_by_id.items():
        if oid not in state.picked:
            t = graph.t_min(state.node, o.origin_id)
            if t is not None and o.latest_pickup_min >= state.clock + t and compatible((), o, truck.trailer, state.adr_tainted):
                r_open += o.revenue_cents
    max_d = max((graph.d_min(state.node, offers_by_id[x].dest_id) or 0 for x in state.onboard), default=0)
    return state.revenue - state.cost + r_open + ev_max - ceil_div(truck.base_cpkm * max_d, 1000)

def dominates(a: TruckState, b: TruckState) -> bool:
    if a.clock != b.clock or a.cont_drive != b.cont_drive or a.day_drive != b.day_drive or a.shift_start != b.shift_start:
        return False
    pa, pb, ta, tb = a.revenue - a.cost, b.revenue - b.cost, int(a.adr_tainted), int(b.adr_tainted)
    if not ((pa >= pb and a.dist_m <= b.dist_m and ta <= tb) and (pa > pb or a.dist_m < b.dist_m or ta < tb)):
        return False
    return pa > pb or _ev_seq(a.last_step) <= _ev_seq(b.last_step)

def _ev_seq(last: RouteStep | None) -> tuple[tuple[str, str], ...]:
    return tuple((st.kind.value, st.offer_id) for st in unwind_steps(last))

def _advance(
    kind: EventKind, oid: str, city: str, curr: TruckState, leg: Leg,
    twin: tuple[int, int, int], new_ob: tuple[str, ...], dw: int, dldm: int,
    axle: int, dr: int, tainted: bool, d_add: frozenset[str], p_add: frozenset[str], tr: TruckSpec,
) -> TruckState | None:
    t_drv = tacho_drive(TachoState(curr.clock, curr.cont_drive, curr.day_drive, curr.shift_start), leg.time_min)
    dock = tacho_dock(t_drv, twin[0], twin[1], twin[2])
    if dock is None: return None
    t_dk, s_min = dock
    c, u_kg, u_ldm = leg_cost(leg, curr.used_kg, tr.base_cpkm, tr.rate_cptkm), curr.used_kg + dw, curr.used_ldm + dldm
    st = RouteStep(kind, oid, city, t_drv.clock, s_min, t_dk.clock, leg.dist_m, c, t_dk.breaks_taken, t_dk.rests_taken, u_kg, u_ldm, axle, new_ob, curr.last_step)
    return TruckState(city, t_dk.clock, new_ob, curr.delivered | d_add, curr.picked | p_add, u_kg, u_ldm, t_dk.cont_drive, t_dk.day_drive, t_dk.shift_start, tainted, curr.revenue + dr, curr.cost + c, curr.dist_m + leg.dist_m, st)

def solve(
    graph: RoadGraph, truck: TruckSpec, offers: Sequence[ShipmentOffer],
    start_node: str, t0: int = 0, max_expansions: int = 50_000,
) -> Solution | None:
    offers_by_id = {o.id: o for o in sorted(offers, key=lambda x: x.id)}
    ev_max = max((backhaul_ev(n) for n in graph.nodes.values()), default=0)
    init_state = TruckState(start_node, t0, (), frozenset(), frozenset(), 0, 0, 0, 0, t0, False, 0, 0, 0, None)
    seq = itertools.count()
    pq = [(-upper_bound(init_state, graph, truck, offers_by_id, ev_max), 0, next(seq), init_state)]
    dom: dict[tuple[str, tuple[str, ...], frozenset[str]], list[TruckState]] = {(start_node, (), frozenset()): [init_state]}
    best_sol, best_net, best_seq, expansions = None, None, None, 0
    while pq and expansions < max_expansions:
        neg_ub, _, _, curr = heapq.heappop(pq)
        if best_net is not None and -neg_ub < best_net: continue
        expansions += 1
        if not curr.onboard and curr.delivered:
            ev = backhaul_ev(graph.nodes[curr.node])
            net = curr.revenue - curr.cost + ev
            if profit_ok(net, curr.dist_m, truck.min_net_profit_cents, truck.min_cents_per_km):
                steps = unwind_steps(curr.last_step)
                ev_seq = tuple((st.kind.value, st.offer_id) for st in steps)
                if best_net is None or net > best_net or (net == best_net and (best_seq is None or ev_seq < best_seq)):
                    best_net, best_seq = net, ev_seq
                    best_sol = (steps, tuple(st.offer_id for st in steps if st.kind is EventKind.DELIVER), curr.node, curr.clock, curr.revenue, curr.cost, ev, net, curr.dist_m)
        succs: list[TruckState] = []
        if curr.onboard:
            oid = curr.onboard[-1]
            o = offers_by_id[oid]
            rem_ob = curr.onboard[:-1]
            items = tuple((offers_by_id[x].weight_kg, ldm_for(offers_by_id[x], truck.trailer) or 0) for x in rem_ob)
            axle, leg = drive_axle_kg(items), graph.get_leg(curr.node, o.dest_id)
            if axle <= MAX_DRIVE_AXLE_KG and leg is not None:
                o_ldm = ldm_for(o, truck.trailer) or 0
                nxt = _advance(EventKind.DELIVER, oid, o.dest_id, curr, leg, (o.earliest_delivery_min, o.latest_delivery_min, o.dock_dwell_min), rem_ob, -o.weight_kg, -o_ldm, axle, o.revenue_cents, curr.adr_tainted, frozenset({oid}), frozenset(), truck)
                if nxt is not None: succs.append(nxt)
        onboard_objs = tuple(offers_by_id[x] for x in curr.onboard)
        for oid in sorted(offers_by_id):
            if oid in curr.picked: continue
            o = offers_by_id[oid]
            o_ldm = ldm_for(o, truck.trailer)
            if o_ldm is None or not compatible(onboard_objs, o, truck.trailer, curr.adr_tainted) or not capacity_ok(curr.used_kg + o.weight_kg, curr.used_ldm + o_ldm):
                continue
            new_objs = onboard_objs + (o,)
            items = tuple((x.weight_kg, ldm_for(x, truck.trailer) or 0) for x in new_objs)
            axle, leg = drive_axle_kg(items), graph.get_leg(curr.node, o.origin_id)
            if axle <= MAX_DRIVE_AXLE_KG and lifo_forward_ok(graph, new_objs) and leg is not None:
                tnt = curr.adr_tainted or (o.cargo_class is CargoClass.ADR_HAZMAT)
                nxt = _advance(EventKind.PICKUP, oid, o.origin_id, curr, leg, (o.earliest_pickup_min, o.latest_pickup_min, o.dock_dwell_min), curr.onboard + (oid,), o.weight_kg, o_ldm, axle, 0, tnt, frozenset(), frozenset({oid}), truck)
                if nxt is not None: succs.append(nxt)
        for succ in succs:
            s_ub = upper_bound(succ, graph, truck, offers_by_id, ev_max)
            if best_net is not None and s_ub < best_net: continue
            k = (succ.node, succ.onboard, succ.delivered)
            lst = dom.setdefault(k, [])
            if any(dominates(e, succ) for e in lst): continue
            dom[k] = [e for e in lst if not dominates(succ, e)] + [succ]
            heapq.heappush(pq, (-s_ub, -(len(succ.picked) + len(succ.delivered)), next(seq), succ))
    if best_sol is None: return None
    return Solution(*best_sol, expansions, not pq or (best_net is not None and -pq[0][0] < best_net))
