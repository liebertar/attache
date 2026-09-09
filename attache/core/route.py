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

from attache.core.geo import Airspace, first_breach, required_top_along

# 구간 고도는 "그 구간에서 가장 낮은 안전 고도"입니다: 아래 가장 높은 옥상 + 이격 50m, 최소
# FLOOR. 그게 그 자리의 천장(FAA 격자, 최대 CRUISE)을 넘으면 그 구간은 못 지나고 옆으로
# 돕니다. 그래서 강 위에서는 40m, 저층 위에서는 70m, 탑 옆에서는 돌아가는 식으로 고도가
# 구간마다 달라집니다 — 한 높이로만 다니면 고도를 판정한다는 게 화면에 안 보입니다.
CRUISE_ALT_M = 120.0      # 올라갈 수 있는 최대. FAA 기본 상한 400ft(121.9m) 바로 아래
# 판정 자료에 없는 건물(20 m 미만) 위로도 50 m 가 남아야 합니다. 40 m 로 두었더니 33~37 m 건물을
# 40 m 로 지나는 경로가 승인됐고, 화면에서 회랑이 그 건물을 뚫고 갔습니다. 천장이 70 m 미만인
# FAA 격자 칸(15/30/61 m)은 이 값으로 지나갈 수 없게 됩니다 — 그 칸을 도는 것이 맞습니다.
FLOOR_ALT_M = 70.0        # 아무것도 없는 곳(강·공원)의 순항 최소 = 자료 문턱 20 m + 이격 50 m

# 목적지에서 이만큼 벗어난 곳까지는 우회로로 봅니다. 도시 한 구역을 크게
# 돌아가는 경로가 나올 수 있어야 합니다.
SEARCH_REACH_M = 12_000.0


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
    def __init__(self, airspace: Airspace, cell_deg: float = 0.00045,
                 min_alt_m: float = 20.0, cruise_alt_m: float = 0.0):
        """cell_deg 0.00045 는 약 50 m. 건물 하나 크기입니다.

        200m 로 잡던 시절에는 FAA 격자(약 900m)만 피하면 됐습니다. 건물이 들어오면
        간선 하나가 200m 라 맨해튼에서는 거의 모든 간선이 건물을 스쳐 그래프가 끊깁니다.
        50m 로 내리면 골목이 열리고, 100m·28m 보다도 빠릅니다 — 전자는 간선이 자주
        막혀 탐색이 헤매고, 후자는 같은 거리를 더 잘게 나눠 걷기 때문입니다.

        cruise_alt_m 은 허용 천장이 아니라 이 기체가 다니고 싶은 높이입니다. 실제
        배달 드론은 40~60m 로 납니다(Wing 이 약 45m). 천장까지 최대한 올라가면
        도시의 건물은 대부분 그 아래에 깔려서, 경로가 건물을 아예 안 봅니다.
        """
        self.airspace = airspace
        self.cell = cell_deg
        self.min_alt_m = min_alt_m
        self.cruise_alt_m = cruise_alt_m or CRUISE_ALT_M
        self.floor_alt_m = min(FLOOR_ALT_M, self.cruise_alt_m)
        self._blocked_memo: dict[tuple[int, int], bool] = {}
        self._edge_memo: dict[tuple, float | None] = {}
        self._route_memo: dict[tuple, Route | None] = {}
        self._memo_for = -1

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
        """여기를 우리가 날 고도로 지날 수 있는가.

        그 고도는 순항 높이와 그 자리의 천장 중 낮은 쪽입니다 — _to_legs 가 구간마다
        붙이는 값과 같은 규칙입니다. 두 곳이 다른 높이를 쓰면 계획기가 통과라고 본
        경로를 판정자가 거절합니다.
        """
        return self.airspace.too_close(lat, lon, self._altitude_at(lat, lon))

    def _altitude_at(self, lat: float, lon: float) -> float:
        ceiling = self.airspace.ceiling_at(lat, lon)
        allowed = self.cruise_alt_m if ceiling is None else ceiling - 1.0
        return max(self.min_alt_m, min(self.cruise_alt_m, allowed))

    def _blocked(self, node: tuple[int, int]) -> bool:
        """격자점 하나의 답은 안 바뀝니다. 탐색 중에 같은 점을 수십 번 다시 봅니다."""
        known = self._blocked_memo.get(node)
        if known is None:
            known = self._forbidden_at(*self._coords(node))
            self._blocked_memo[node] = known
        return known

    def leg_altitude(self, start: tuple[float, float], goal: tuple[float, float]) -> float | None:
        """이 선분을 지날 수 있는 가장 낮은 안전 고도. 못 지나면 None.

        아래 가장 높은 옥상 + 이격(50m), 최소 FLOOR. 그게 선분 위 가장 낮은 천장(-1m, 최대
        CRUISE)을 넘으면 옆으로 돌아야 하는 구간입니다. 마지막에 판정자와 같은 first_breach 로
        한 번 더 봅니다 — 옆 이격·격자·구역은 거기서 잡힙니다.
        """
        here = {"lat": start[0], "lon": start[1]}
        nxt = {"lat": goal[0], "lon": goal[1]}
        allowed = min(self._altitude_at(*start), self._altitude_at(*goal),
                      self._ceiling_allowance(start, goal))
        top = required_top_along(self.airspace, here, nxt)
        # 최저 순항(70 m)은 천장이 허락하는 곳에서만입니다. 천장이 낮은 칸(61 m)에서는 천장 - 1.
        # 건물 Volume 은 옥상 + 이격까지를 막습니다(닫힌 구간). 딱 그 높이는 아직 안이라 0.5m 더.
        floor_here = min(self.floor_alt_m, allowed)
        needed = max(floor_here, self.min_alt_m, top + 0.5 if top > 0 else 0.0)
        if needed > allowed:
            return None
        legs = [{**here, "alt_m": needed}, {**nxt, "alt_m": needed}]
        return None if first_breach(self.airspace, legs) is not None else needed

    def _ceiling_allowance(self, start, goal, samples: int = 24) -> float:
        """선분 위에서 가장 낮은 천장 - 1m. 천장이 없으면 최대 순항."""
        lowest = self.cruise_alt_m
        for step in range(samples + 1):
            fraction = step / samples
            ceiling = self.airspace.ceiling_at(start[0] + (goal[0] - start[0]) * fraction,
                                               start[1] + (goal[1] - start[1]) * fraction)
            if ceiling is not None:
                lowest = min(lowest, ceiling - 1.0)
        return max(self.min_alt_m, lowest)

    def _edge(self, a: tuple[int, int], b: tuple[int, int]) -> float | None:
        """격자 간선의 고도(못 지나면 None). 같은 간선을 탐색 중 수십 번 다시 봅니다."""
        key = (a, b) if a <= b else (b, a)
        if key in self._edge_memo:
            return self._edge_memo[key]
        altitude = self.leg_altitude(self._coords(a), self._coords(b))
        self._edge_memo[key] = altitude
        return altitude

    def _crosses(self, a: tuple[int, int], b: tuple[int, int], samples: int = 0) -> bool:
        """두 격자점을 잇는 선분을 어느 고도로도 못 지나는가.

        격자점만 보면 모서리를 잘라먹습니다. 판정자가 선분을 보므로 계획기도 선분을 봅니다.
        """
        return self._edge(a, b) is None

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
        if self._memo_for != self.airspace.revision:
            self._blocked_memo.clear()          # 공역이 바뀌면 기억한 답도 버립니다
            self._edge_memo.clear()
            self._route_memo.clear()
            self._memo_for = self.airspace.revision
        # 같은 자리에서 같은 착륙장으로는 판마다 다시 갑니다. 건물 3만 4천 동에 천장 낮은 칸을 도는
        # 탐색은 한 번에 1~3분이라, 같은 공역 판본 안에서는 답을 기억합니다(20 m 안은 같은 자리).
        key = (round(start[0], 4), round(start[1], 4), round(goal[0], 4), round(goal[1], 4))
        if key in self._route_memo:
            return self._route_memo[key]
        route = self._plan(start, goal)
        self._route_memo[key] = route
        return route

    def _plan(self, start: tuple[float, float], goal: tuple[float, float]) -> Route | None:
        if self.airspace.landing_breach(*goal) is not None:
            return None  # 내려앉을 수 없는 자리입니다. 길이 있어도 소용없습니다
        starts = self._free_nodes_near(start)
        goals = self._free_nodes_near(goal)
        if not starts or not goals:
            return None  # 출발점이나 목적지 둘레에 열린 격자점이 없습니다

        direct = self._straight(starts[0], goals[0])
        if direct is not None:
            legs = self._attach(self._to_legs([starts[0], goals[0]]), start, goal)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=False)

        path = self._search(starts, goals)
        if path is None:
            return None
        # 줄을 당겨 곧게 편 것부터 씁니다. 안 되면 꺾인 점만 남긴 것, 그것도 안 되면
        # 격자를 한 칸씩 밟은 원본. 어느 쪽이든 마지막에 판정자가 다시 봅니다.
        for nodes in (self._pull(path), self._simplify(path), path):
            if not self._legal_chain(nodes):
                continue
            legs = self._attach(self._to_legs(nodes), start, goal)
            if first_breach(self.airspace, [leg.to_dict() for leg in legs]) is None:
                return Route(legs, detoured=True, reason="금지 구역을 피해 우회")
        return None

    def _free_nodes_near(self, point: tuple[float, float]) -> list[tuple[int, int]]:
        """이 지점에서 이어 붙일 수 있는 열린 격자점들, 가까운 순.

        격자점은 실제 지점에서 최대 35m 벗어납니다. 주소는 건물 옆이고, 회수돼 떠 있는 자리는
        구역 경계 바로 밖이라, 딱 떨어지는 격자점이 건물 안이거나 이격 거리 안인 일이 흔합니다.
        그러면 목적지가 멀쩡한데도 '길이 없다'가 나왔습니다. 둘레 두 칸 안에서 지점까지의
        직선이 통과하는 점들을 전부 씁니다 — 가장 가까운 점 하나가 건물 사이 막힌 주머니에
        갇혀 있을 수 있어서(센트럴파크 북쪽에서 돌아오는 길이 그래서 안 났습니다) 탐색은
        여럿에서 동시에 시작하고 어느 하나에 닿으면 끝납니다. _pin 이 실제 지점으로 잇습니다.
        """
        centre = self._node(*point)
        candidates = sorted(
            ((centre[0] + di, centre[1] + dj) for di in range(-2, 3) for dj in range(-2, 3)),
            key=lambda node: math.dist(self._coords(node), point),
        )
        open_nodes = []
        for node in candidates:
            if self._blocked(node):
                continue
            if self.leg_altitude(point, self._coords(node)) is not None:
                open_nodes.append(node)
        return open_nodes

    def _free_node_near(self, point: tuple[float, float]) -> tuple[int, int] | None:
        nodes = self._free_nodes_near(point)
        return nodes[0] if nodes else None

    def _attach(self, legs: list[Leg], start: tuple[float, float],
                goal: tuple[float, float]) -> list[Leg]:
        """실제 출발점·목적지를 격자 경로의 양 끝에 잇습니다.

        탐색은 격자점 위에서 하므로 경로가 목적지에서 최대 35m 떨어진 곳에서 끝납니다.
        예전에는 끝 격자점을 실제 지점으로 바꿔치기했는데, 그러면 마지막 구간이 판정한 적 없는
        새 선분이 되어 건물 모서리를 스치고 통째로 거절되는 일이 있었습니다(유니언스퀘어).
        격자점은 그대로 두고 실제 지점까지 짧은 구간을 하나 덧붙입니다 — 그 구간은
        _free_nodes_near 가 이미 통과를 확인한 선분입니다. 고도는 양쪽 중 낮은 쪽.
        """
        if not legs:
            return legs
        head_alt = self.leg_altitude(start, (legs[0].lat, legs[0].lon))
        tail_alt = self.leg_altitude((legs[-1].lat, legs[-1].lon), goal)
        if head_alt is None or tail_alt is None:
            return legs      # _free_nodes_near 가 이미 확인한 구간이라 여기 올 일은 없습니다
        legs[0] = Leg(legs[0].lat, legs[0].lon, head_alt)
        return ([Leg(start[0], start[1], head_alt)] + legs
                + [Leg(goal[0], goal[1], tail_alt)])

    def _pull(self, path: list[tuple[int, int]], window: int = 80) -> list[tuple[int, int]]:
        """줄을 당깁니다. 막는 게 없는 구간은 곧게 펴집니다.

        A* 는 같은 길이면 어느 쪽으로 꺾든 값이 같아서, 뻥 뚫린 강 위에서도 격자를
        한 칸씩 밟은 계단이 나옵니다. 꺾인 점만 남기는 방식은 한 군데라도 걸리면
        경로 전체를 원본으로 되돌려서, 장애물이 없는 구간까지 같이 계단이 됐습니다.
        여기서는 갈 수 있는 데까지 곧게 가고, 막히는 자리에서만 꺾습니다.
        """
        if len(path) < 3:
            return path
        kept = [path[0]]
        index = 0
        while index < len(path) - 1:
            furthest = index + 1
            for candidate in range(min(len(path) - 1, index + window), index + 1, -1):
                if self._legal_chain([path[index], path[candidate]]):
                    furthest = candidate
                    break
            kept.append(path[furthest])
            index = furthest
        return kept

    def _legal_chain(self, nodes: list[tuple[int, int]]) -> bool:
        """이어 붙인 구간이 금지 구역을 안 지나는가. 격자점이 아니라 선분을 봅니다."""
        return all(
            not self._blocked(a) and not self._blocked(b)
            and not self._crosses(a, b)
            for a, b in zip(nodes, nodes[1:], strict=False)
        )

    def _straight(self, a: tuple[int, int], b: tuple[int, int]) -> bool | None:
        return True if self._legal_chain([a, b]) else None

    def _search(self, starts, goals, budget: int = 120_000):
        """여러 출발 격자점에서 동시에 시작해 목적지 격자점 중 아무 데나 닿으면 끝납니다."""
        starts = [starts] if isinstance(starts, tuple) else list(starts)
        goals = {goals} if isinstance(goals, tuple) else set(goals)
        goal = min(goals, key=lambda node: math.dist(node, starts[0]))
        origin = starts[0]
        reach = max(40, int(SEARCH_REACH_M / (self.cell * 110_570.0)))

        def heuristic(node):
            return math.dist(node, goal)

        open_set = [(heuristic(start), 0.0, start) for start in starts]
        heapq.heapify(open_set)
        came_from: dict = {}
        best = {start: 0.0 for start in starts}
        seen = 0
        while open_set:
            _, cost, node = heapq.heappop(open_set)
            if node in goals:
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
                # 탐색 범위는 거리로 잡습니다. 노드 수로 잡으면 격자를 촘촘하게 할수록
                # 볼 수 있는 범위가 같이 좁아져서, 멀리 있는 목적지를 아예 못 찾습니다.
                # 목적지 둘레만 보면 갈 때는 되는데 올 때는 안 되는 길이 생깁니다 — 갈 때
                # 목적지 둘레 안에서 멀리 돌아간 길이 올 때는 창 밖이라서. 양쪽 둘레를 다 봅니다.
                if ((abs(nxt[0] - goal[0]) > reach or abs(nxt[1] - goal[1]) > reach)
                        and (abs(nxt[0] - origin[0]) > reach or abs(nxt[1] - origin[1]) > reach)):
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
        for previous, node, following in zip(path, path[1:], path[2:], strict=False):
            before = (node[0] - previous[0], node[1] - previous[1])
            after = (following[0] - node[0], following[1] - node[1])
            if before != after:
                kept.append(node)
        kept.append(path[-1])
        return kept

    def _to_legs(self, nodes: list[tuple[int, int]]) -> list[Leg]:
        """구간마다 그 구간의 가장 낮은 안전 고도(_edge)를 붙입니다. 한 구간 안에서는 한 고도."""
        legs = []
        for index, node in enumerate(nodes):
            lat, lon = self._coords(node)
            if index == 0:
                altitude = self._edge(node, nodes[1]) if len(nodes) > 1 else self.floor_alt_m
            else:
                altitude = self._edge(nodes[index - 1], node)
            legs.append(Leg(lat, lon, self.floor_alt_m if altitude is None else altitude))
        return legs
