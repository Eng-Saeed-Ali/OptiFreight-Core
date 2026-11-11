from __future__ import annotations

import pytest
from optifreight.models import CityNode, RoadEdge
from optifreight.graph import Leg, RoadGraph


def test_graph_construction_and_validation() -> None:
    n1 = CityNode(id="A", name="City A")
    n2 = CityNode(id="B", name="City B")

    # Empty node set
    with pytest.raises(ValueError, match="Empty node set"):
        RoadGraph(nodes=(), edges=())

    # Duplicate node id
    with pytest.raises(ValueError, match="Duplicate node id"):
        RoadGraph(nodes=(n1, CityNode(id="A", name="City A duplicate")), edges=())

    # Unknown edge origin
    e_bad_orig = RoadEdge(origin_id="X", dest_id="B", distance_m=10_000, drive_min=10)
    with pytest.raises(ValueError, match="Unknown edge origin"):
        RoadGraph(nodes=(n1, n2), edges=(e_bad_orig,))

    # Unknown edge dest
    e_bad_dest = RoadEdge(origin_id="A", dest_id="Y", distance_m=10_000, drive_min=10)
    with pytest.raises(ValueError, match="Unknown edge dest"):
        RoadGraph(nodes=(n1, n2), edges=(e_bad_dest,))


def test_self_legs_unreachable_and_unknown() -> None:
    n1 = CityNode(id="A", name="City A")
    n2 = CityNode(id="B", name="City B")
    n3 = CityNode(id="C", name="City C")

    # One directed edge: A -> B
    e1 = RoadEdge(origin_id="A", dest_id="B", distance_m=50_000, drive_min=45, toll_cents=200)
    graph = RoadGraph(nodes=(n1, n2, n3), edges=(e1,))

    # Self leg for A
    leg_a = graph.get_leg("A", "A")
    assert leg_a == Leg(origin_id="A", dest_id="A", time_min=0, dist_m=0, edges=())
    assert graph.t_min("A", "A") == 0
    assert graph.d_min("A", "A") == 0

    # Reachable A -> B
    leg_ab = graph.get_leg("A", "B")
    assert leg_ab is not None
    assert leg_ab.origin_id == "A"
    assert leg_ab.dest_id == "B"
    assert leg_ab.time_min == 45
    assert leg_ab.dist_m == 50_000
    assert leg_ab.edges == (e1,)
    assert graph.t_min("A", "B") == 45
    assert graph.d_min("A", "B") == 50_000

    # Unreachable B -> A
    assert graph.get_leg("B", "A") is None
    assert graph.t_min("B", "A") is None
    assert graph.d_min("B", "A") is None

    # Unreachable A -> C
    assert graph.get_leg("A", "C") is None
    assert graph.t_min("A", "C") is None
    assert graph.d_min("A", "C") is None

    # Unknown node
    assert graph.get_leg("UNKNOWN", "A") is None
    assert graph.t_min("UNKNOWN", "A") is None
    assert graph.d_min("UNKNOWN", "A") is None


def test_divergent_time_vs_distance_shortest_paths() -> None:
    # Diamond graph:
    # START -> FAST -> END:  time = 50 + 50 = 100 min, dist = 75_000 + 75_000 = 150_000 m
    # START -> SHORT -> END: time = 65 + 65 = 130 min, dist = 55_000 + 55_000 = 110_000 m
    n_start = CityNode(id="START", name="Start")
    n_fast = CityNode(id="FAST", name="Fast")
    n_short = CityNode(id="SHORT", name="Short")
    n_end = CityNode(id="END", name="End")

    e_f1 = RoadEdge(origin_id="START", dest_id="FAST", distance_m=75_000, drive_min=50)
    e_f2 = RoadEdge(origin_id="FAST", dest_id="END", distance_m=75_000, drive_min=50)

    e_s1 = RoadEdge(origin_id="START", dest_id="SHORT", distance_m=55_000, drive_min=65)
    e_s2 = RoadEdge(origin_id="SHORT", dest_id="END", distance_m=55_000, drive_min=65)

    graph = RoadGraph(
        nodes=(n_start, n_fast, n_short, n_end),
        edges=(e_f1, e_f2, e_s1, e_s2),
    )

    # Time-shortest must choose the FAST path
    leg = graph.get_leg("START", "END")
    assert leg is not None
    assert leg.time_min == 100
    assert leg.dist_m == 150_000
    assert leg.edges == (e_f1, e_f2)
    assert graph.t_min("START", "END") == 100

    # Distance-shortest must reflect the SHORT path for admissible lower-bound costing
    assert graph.d_min("START", "END") == 110_000


def test_tie_breaking_determinism() -> None:
    # Two intermediate paths between S and E:
    # Path 1: S -> MID1 -> E
    # Path 2: S -> MID2 -> E
    ns = CityNode(id="S", name="Start")
    ne = CityNode(id="E", name="End")
    m1 = CityNode(id="MID1", name="Mid 1")
    m2 = CityNode(id="MID2", name="Mid 2")

    # 1. Equal time_min (100 min), but MID1 has longer distance (120 km) while MID2 has shorter distance (110 km)
    # Tie broken by dist_m -> MID2 must be chosen
    e1_1 = RoadEdge(origin_id="S", dest_id="MID1", distance_m=60_000, drive_min=50)
    e1_2 = RoadEdge(origin_id="MID1", dest_id="E", distance_m=60_000, drive_min=50)

    e2_1 = RoadEdge(origin_id="S", dest_id="MID2", distance_m=55_000, drive_min=50)
    e2_2 = RoadEdge(origin_id="MID2", dest_id="E", distance_m=55_000, drive_min=50)

    g1 = RoadGraph(nodes=(ns, ne, m1, m2), edges=(e1_1, e1_2, e2_1, e2_2))
    leg1 = g1.get_leg("S", "E")
    assert leg1 is not None
    assert leg1.time_min == 100
    assert leg1.dist_m == 110_000
    assert leg1.edges == (e2_1, e2_2)

    # 2. Equal time_min (100 min) AND equal dist_m (110 km)
    # Tie broken by lexicographical node path: ('S', 'MID1', 'E') < ('S', 'MID2', 'E')
    # Even if edges for MID2 are provided first in the list
    e1_1_eq = RoadEdge(origin_id="S", dest_id="MID1", distance_m=55_000, drive_min=50)
    e1_2_eq = RoadEdge(origin_id="MID1", dest_id="E", distance_m=55_000, drive_min=50)

    # Supply MID2 edges first
    g2 = RoadGraph(nodes=(ns, ne, m1, m2), edges=(e2_1, e2_2, e1_1_eq, e1_2_eq))
    leg2 = g2.get_leg("S", "E")
    assert leg2 is not None
    assert leg2.time_min == 100
    assert leg2.dist_m == 110_000
    assert leg2.edges == (e1_1_eq, e1_2_eq)

