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

from attache.core.geo import Airspace, _leg_samples, first_breach

# 실제 배달 드론이 다니는 높이입니다. Wing 이 약 45m 로 납니다.
# 허용 천장까지 최대한 올라가면 도시의 건물이 전부 그 아래에 깔려서,
# 경로가 건물을 아예 안 보게 됩니다.
CRUISE_ALT_M = 55.0


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
                 min_alt_m: float = 20.0, cruise_alt_m: float = 0.0):
        """cell_deg 0.0018 은 위도로 약 200 m. FAA 격자(약 0.008°)보다 촘촘합니다.

        cruise_alt_m 은 허용 천장이 아니라 이 기체가 다니고 싶은 높이입니다. 실제
        배달 드론은 40~60m 로 납니다(Wing 이 약 45m). 천장까지 최대한 올라가면
        도시의 건물은 대부분 그 아래에 깔려서, 경로가 건물을 아예 안 봅니다.
        """
        self.airspace = airspace
        self.cell = cell_deg
        self.min_alt_m = min_alt_m
        self.cruise_alt_m = cruise_alt_m or CRUISE_ALT_M

    @staticmethod
    def cruise_alt_default() -> float:
        """기체가 다니고 싶은 기본 높이. 이 숫자를 두 곳에 적으면 반드시 갈라집니다."""
        return CRUISE_ALT_M

    # ---------- 격자 ----------

    def _node(self, lat: float, lon: float) -> tuple[int, int]:
        return (round(lat / self.cell), round(lon / self.cell))

    def _coords(self, node: tuple[int, int]) -> tuple[float, float]:
        return (node[0] * self.cell, node[1] * self.cell)

    def _forbidden_at(self, lat: float, lon: float) -> bool:
        """공역의 색인을 그대로 씁니다. 여기에 따로 색인을 두면 또 갈라집니다."""
        return any(
            volume.rule == "forbidden" and volume.covers(lat, lon)
            for volume in self.airspace.near(lat, lon)
        )

    def _blocked(self, node: tuple[int, int]) -> bool:
        return self._forbidden_at(*self._coords(node))

    def _crosses(self, a: tuple[int, int], b: tuple[int, int], samples: int = 0) -> bool:
        """두 격자점을 잇는 선분이 금지 구역을 지나는가.

        격자점만 보면 모서리를 잘라먹습니다. 대각선 한 칸이 폴리곤 귀퉁이를 관통해도
        양 끝은 바깥이라 통과로 보이고, 그 경로는 first_breach 에서 반드시 떨어집니다.
        판정자가 선분을 보므로 계획기도 선분을 봐야 합니다.
        """
        lat_a, lon_a = self._coords(a)
        lat_b, lon_b = self._coords(b)
        if not samples:
            # 판정자와 같은 간격으로 봅니다. 성기게 보면 건물 사이로 빠져나갑니다.
            samples = _leg_samples({"lat": lat_a, "lon": lon_a}, {"lat": lat_b, "lon": lon_b})
        for step in range(1, samples):
            fraction = step / samples
            if self._forbidden_at(lat_a + (lat_b - lat_a) * fraction,
                                  lon_a + (lon_b - lon_a) * fraction):
                return True
        return False

    def _ceiling_between(self, a: tuple[int, int], b: tuple[int, int],
                         samples: int = 48) -> float | None:
        """구간 위의 가장 낮은 천장. 판정자(40 표본)보다 촘촘하게 봅니다."""
        lat_a, lon_a = self._coords(a)
        lat_b, lon_b = self._coords(b)
        ceilings = []
        for step in range(samples + 1):
            fraction = step / samples
            ceiling = self.airspace.ceiling_at(lat_a + (lat_b - lat_a) * fraction,
                                               lon_a + (lon_b - lon_a) * fraction)
            if ceiling is not None:
                ceilings.append(ceiling)
        return min(ceilings) if ceilings else None

    # ---------- 길찾기 ----------

    def plan(self, start: tuple[float, float], goal: tuple[float, float]) -> Route | None:
        start_node, goal_node = self._node(*start), self._node(*goal)
        if self._blocked(goal_node):
            return None  # 목적지 자체가 금지 구역이면 우회로가 없습니다

        direct = self._straight(start_node, goal_node)
        if direct is not None:
            legs = self._pin(self._to_legs([start_node, goal_node]), start, goal)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=False)

        path = self._search(start_node, goal_node)
        if path is None:
            return None
        # 꺾이는 점만 남기면 보기 좋지만, 남긴 두 점 사이를 직선으로 이으면 모서리를
        # 잘라먹어 금지 구역을 스칠 수 있습니다. 잘라낸 결과를 다시 검사해서
        # 통과할 때만 씁니다.
        for nodes in (self._simplify(path), path):
            if not self._legal_chain(nodes):
                continue
            legs = self._pin(self._to_legs(nodes), start, goal)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=True, reason="금지 구역을 피해 우회")
        return None

    @staticmethod
    def _pin(legs: list[Leg], start: tuple[float, float],
             goal: tuple[float, float]) -> list[Leg]:
        """경로의 양 끝을 실제 지점에 맞춥니다.

        탐색은 격자점 위에서 하므로 마지막 구간이 목적지에서 최대 100m 떨어진 곳에서
        끝납니다. 기체는 경유점을 다 쓰고 남은 100m 를 승인 없이 날아갑니다 —
        그 구간이 어디를 지나는지는 아무도 판정한 적이 없습니다.
        """
        if not legs:
            return legs
        legs[0] = Leg(start[0], start[1], legs[0].alt_m)
        legs[-1] = Leg(goal[0], goal[1], legs[-1].alt_m)
        return legs

    def _legal_chain(self, nodes: list[tuple[int, int]]) -> bool:
        """이어 붙인 구간이 금지 구역을 안 지나는가. 격자점이 아니라 선분을 봅니다."""
        return all(
            not self._blocked(a) and not self._blocked(b)
            and not self._crosses(a, b)
            for a, b in zip(nodes, nodes[1:])
        )

    def _straight(self, a: tuple[int, int], b: tuple[int, int]) -> bool | None:
        return True if self._legal_chain([a, b]) else None

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
                if self._blocked(nxt) or self._crosses(node, nxt):
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
            ceiling = (self.airspace.ceiling_at(lat, lon) if index == 0
                       else self._ceiling_between(nodes[index - 1], node))
            allowed = self.cruise_alt_m if ceiling is None else ceiling - 1.0
            altitude = max(self.min_alt_m, min(self.cruise_alt_m, allowed))
            legs.append(Leg(lat, lon, altitude))
        return legs
