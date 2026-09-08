"""Find a way there that stays inside the rules.

This is not obstacle avoidance — the autopilot does that, and it must, because it is the
layer that has to work when everything else is down. This is the other kind of routing:
given a map of who is allowed where and how high, hand back a path that a regulator would
sign off on, or say plainly that there is none.

Refusing is easy and useless. An operator told only "no" flies anyway or stops delivering.
The useful answer is "not that way, this way, and stay under 61 m for the middle leg".
"""

import heapq
import math
from dataclasses import dataclass

from attache.core.geo import Airspace, first_breach


@dataclass
class Leg:
    lat: float
    lon: float
    alt_m: float

    def to_dict(self) -> dict:
        return {"lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "alt_m": round(self.alt_m, 1)}


@dataclass
class Route:
    legs: list[Leg]
    detoured: bool
    reason: str = ""

    @property
    def lowest_ceiling(self) -> float:
        return min((leg.alt_m for leg in self.legs), default=0.0)

    def to_dict(self) -> dict:
        return {
            "legs": [leg.to_dict() for leg in self.legs],
            "detoured": self.detoured,
            "reason": self.reason,
        }


class Router:
    def __init__(self, airspace: Airspace, cell_deg: float = 0.0018,
                 min_alt_m: float = 20.0):
        """cell_deg 0.0018 은 위도로 약 200 m. FAA 격자(약 0.008°)보다 촘촘합니다."""
        self.airspace = airspace
        self.cell = cell_deg
        self.min_alt_m = min_alt_m

    # ---------- 격자 ----------

    def _node(self, lat: float, lon: float) -> tuple[int, int]:
        return (round(lat / self.cell), round(lon / self.cell))

    def _coords(self, node: tuple[int, int]) -> tuple[float, float]:
        return (node[0] * self.cell, node[1] * self.cell)

    def _blocked(self, node: tuple[int, int]) -> bool:
        lat, lon = self._coords(node)
        return any(
            volume.rule == "forbidden" and volume.covers(lat, lon)
            for volume in self.airspace.all()
        )

    def _ceiling(self, node: tuple[int, int]) -> float | None:
        return self.airspace.ceiling_at(*self._coords(node))

    # ---------- 길찾기 ----------

    def plan(self, start: tuple[float, float], goal: tuple[float, float]) -> Route | None:
        start_node, goal_node = self._node(*start), self._node(*goal)
        if self._blocked(goal_node):
            return None  # 목적지 자체가 금지 구역이면 우회로가 없습니다

        direct = self._straight(start_node, goal_node)
        if direct is not None:
            legs = self._to_legs([start_node, goal_node])
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=False)

        path = self._search(start_node, goal_node)
        if path is None:
            return None
        # 꺾이는 점만 남기면 보기 좋지만, 남긴 두 점 사이를 직선으로 이으면 모서리를
        # 잘라먹어 금지 구역을 스칠 수 있습니다. 잘라낸 결과를 다시 검사해서
        # 통과할 때만 씁니다.
        for nodes in (self._simplify(path), path):
            legs = self._to_legs(nodes)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=True, reason="금지 구역을 피해 우회")
        return None

    def _legal_chain(self, nodes: list[tuple[int, int]]) -> bool:
        for a, b in zip(nodes, nodes[1:]):
            steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1) * 3
            for index in range(steps + 1):
                fraction = index / steps
                node = (round(a[0] + (b[0] - a[0]) * fraction),
                        round(a[1] + (b[1] - a[1]) * fraction))
                if self._blocked(node):
                    return False
        return True

    def _straight(self, a: tuple[int, int], b: tuple[int, int]) -> bool | None:
        steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1) * 2
        for index in range(steps + 1):
            fraction = index / steps
            node = (round(a[0] + (b[0] - a[0]) * fraction),
                    round(a[1] + (b[1] - a[1]) * fraction))
            if self._blocked(node):
                return None
        return True

    def _search(self, start, goal, budget: int = 40_000):
        def heuristic(node):
            return math.dist(node, goal)

        open_set = [(heuristic(start), 0.0, start)]
        came_from: dict = {}
        best = {start: 0.0}
        seen = 0
        while open_set:
            _, cost, node = heapq.heappop(open_set)
            if node == goal:
                path = [node]
                while node in came_from:
                    node = came_from[node]
                    path.append(node)
                return list(reversed(path))
            seen += 1
            if seen > budget:
                return None
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1),
                           (1, 1), (1, -1), (-1, 1), (-1, -1)):
                nxt = (node[0] + di, node[1] + dj)
                if abs(nxt[0] - goal[0]) > 200 or abs(nxt[1] - goal[1]) > 200:
                    continue
                if self._blocked(nxt):
                    continue
                step = math.hypot(di, dj)
                fresh = cost + step
                if fresh < best.get(nxt, float("inf")):
                    best[nxt] = fresh
                    came_from[nxt] = node
                    heapq.heappush(open_set, (fresh + heuristic(nxt), fresh, nxt))
        return None

    @staticmethod
    def _simplify(path: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """같은 방향으로 이어지는 칸은 하나로 묶습니다. 꺾이는 지점만 남습니다."""
        if len(path) < 3:
            return path
        kept = [path[0]]
        for previous, node, following in zip(path, path[1:], path[2:]):
            before = (node[0] - previous[0], node[1] - previous[1])
            after = (following[0] - node[0], following[1] - node[1])
            if before != after:
                kept.append(node)
        kept.append(path[-1])
        return kept

    def _to_legs(self, nodes: list[tuple[int, int]]) -> list[Leg]:
        """구간마다 그 구간에서 허용되는 가장 높은 고도를 붙입니다.

        구간 하나에 천장이 여러 개면 가장 낮은 것을 따릅니다. 한 구간 안에서 오르내리는
        비행은 실제로도 안 합니다.
        """
        legs = []
        for index, node in enumerate(nodes):
            lat, lon = self._coords(node)
            span = [node] if index == 0 else self._between(nodes[index - 1], node)
            ceilings = [c for c in (self._ceiling(n) for n in span) if c is not None]
            altitude = min(ceilings) if ceilings else self.min_alt_m
            legs.append(Leg(lat, lon, max(self.min_alt_m, altitude - 1.0)))
        return legs

    @staticmethod
    def _between(a, b):
        steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]), 1) * 3
        return [(round(a[0] + (b[0] - a[0]) * i / steps),
                 round(a[1] + (b[1] - a[1]) * i / steps)) for i in range(steps + 1)]
