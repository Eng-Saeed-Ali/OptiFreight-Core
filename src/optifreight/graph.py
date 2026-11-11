from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import heapq
import itertools

from optifreight.models import CityNode, RoadEdge


@dataclass(slots=True, frozen=True)
class Leg:
    origin_id: str
    dest_id: str
    time_min: int
    dist_m: int
    edges: tuple[RoadEdge, ...]


class RoadGraph:
    nodes: dict[str, CityNode]
    adj: dict[str, tuple[RoadEdge, ...]]

    def __init__(self, nodes: Iterable[CityNode], edges: Iterable[RoadEdge]) -> None:
        self.nodes = {}
        for n in nodes:
            if n.id in self.nodes:
                raise ValueError(f"Duplicate node id: {n.id!r}")
            self.nodes[n.id] = n
        if not self.nodes:
            raise ValueError("Empty node set")

        adj_map: dict[str, list[RoadEdge]] = {nid: [] for nid in self.nodes}
        for e in edges:
            if e.origin_id not in self.nodes:
                raise ValueError(f"Unknown edge origin: {e.origin_id!r}")
            if e.dest_id not in self.nodes:
                raise ValueError(f"Unknown edge dest: {e.dest_id!r}")
            adj_map[e.origin_id].append(e)

        self.adj = {
            nid: tuple(sorted(el, key=lambda x: (x.dest_id, x.drive_min, x.distance_m, x.toll_cents)))
            for nid, el in adj_map.items()
        }

        self._legs: dict[tuple[str, str], Leg] = {}
        self._d_mins: dict[tuple[str, str], int] = {}
        self._precompute()

    def _precompute(self) -> None:
        counter = itertools.count()
        for u in self.nodes:
            # Time-shortest Dijkstra (tie-break by dist_m, then node path)
            t_pq = [(0, 0, (u,), next(counter), u, ())]
            t_best: dict[str, tuple[int, int, tuple[str, ...]]] = {}
            while t_pq:
                t, d, path, _, curr, edges = heapq.heappop(t_pq)
                if curr in t_best:
                    continue
                t_best[curr] = (t, d, path)
                self._legs[(u, curr)] = Leg(u, curr, t, d, edges)
                for edge in self.adj[curr]:
                    nxt = edge.dest_id
                    if nxt not in t_best:
                        heapq.heappush(
                            t_pq,
                            (t + edge.drive_min, d + edge.distance_m, path + (nxt,), next(counter), nxt, edges + (edge,)),
                        )

            # Distance-shortest Dijkstra (used for admissible lower bound)
            d_pq = [(0, (u,), next(counter), u)]
            d_best: dict[str, int] = {}
            while d_pq:
                d, path, _, curr = heapq.heappop(d_pq)
                if curr in d_best:
                    continue
                d_best[curr] = d
                self._d_mins[(u, curr)] = d
                for edge in self.adj[curr]:
                    nxt = edge.dest_id
                    if nxt not in d_best:
                        heapq.heappush(d_pq, (d + edge.distance_m, path + (nxt,), next(counter), nxt))

    def get_leg(self, u: str, v: str) -> Leg | None:
        if u not in self.nodes or v not in self.nodes:
            return None
        return self._legs.get((u, v))

    def t_min(self, u: str, v: str) -> int | None:
        leg = self.get_leg(u, v)
        return leg.time_min if leg is not None else None

    def d_min(self, u: str, v: str) -> int | None:
        if u not in self.nodes or v not in self.nodes:
            return None
        return self._d_mins.get((u, v))

