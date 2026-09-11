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
import time
from dataclasses import dataclass

from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    Airspace,
    _point_segment_m,
    first_breach,
    required_top_along,
)

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

# ---------- 후보 경로 (candidates) ----------
# 계획기는 규정 안의 길을 셋까지 내놓고, 고르는 것은 운영사의 모델입니다
# (drone/agent/chooser.py).
# 모델에게 좌표를 쓰게 했더니 맨해튼을 길게 가로지르는 초안은 판정을 거의 못 넘었습니다 — 기하는
# 탐색 문제입니다. 고르기는 판단 문제라 모델이 맡고, 무엇을 고르든 런타임이 같은 규칙으로
# 판정합니다.
CANDIDATE_LABELS = {"a": "shortest", "b": "lowest altitude", "c": "clear of traffic"}
VARIANT_TAGS = {"b": "lowest-altitude", "c": "clear-of-traffic"}
# 두 경로의 모든 점이 서로 이만큼 안이면 같은 길로 칩니다. 이름만 다른 같은 길 셋을 주면 모델은
# 고르는 것이 아니라 제비를 뽑습니다.
DISTINCT_M = 100.0
# (b) 고도 벌점. 구간 순항 고도가 최저(70 m)에서 최대(120 m)로 오르면 그 구간 비용이 (1 + 이 값)배.
# 1.0 이면 120 m 로 1 km 가 70 m 로 2 km 와 같습니다 — 강·공원 위로 돌아 낮게 가는 길이 나오되,
# 한 블록 넘자고 두 배를 돌지는 않습니다.
ALTITUDE_WEIGHT = 1.0
# (c) 이격 벌점. 다른 기체의 승인 회랑·사고 원·공지 구역에서 이 거리 안의 격자점은 가까울수록
# 비쌉니다(맞닿으면 1 + CLEAR_WEIGHT 배). 회랑 반폭(30 m + 항법 10 m)의 여섯 배쯤입니다 — 교차
# 거절은 시각까지 맞아야 나지만, '겹칠 걱정 없는 길' 을 고를 수 있으려면 공간으로 떨어진 길이
# 하나는 있어야 합니다. 구역 자체는 이미 금지(40 m 이격)라 이 값은 그 위의 여유입니다.
CLEAR_MARGIN_M = 250.0
CLEAR_WEIGHT = 4.0
# (b)(c) 탐색 하나의 시간 한도(초). (a)는 오늘의 A* 그대로라 한도가 없습니다(없으면 갈 길이
# 없는 것). 나머지는 선택지일 뿐이라 늦으면 뺍니다. 간선 기억(_edge_memo)을 같이 쓰므로 (a) 뒤에는
# 대개 한도보다 훨씬 빨리 끝납니다. 탐색 도중 이만큼마다 시계를 봅니다(매번 보면 그것도 비용).
VARIANT_BUDGET_S = 12.0
DEADLINE_CHECK_EVERY = 512
# 벌점 탐색의 휴리스틱 배율(가중 A*). 거리 휴리스틱은 벌점이 붙은 비용에 비해 너무 작아서, 그대로
# 두면 탐색이 거의 다익스트라가 되어 할렘까지 12초 안에 못 끝냅니다(실측: 제한 시간 초과로 후보가
# (a) 하나만 남았습니다). 휴리스틱을 실제 비용 배율 쯤으로 부풀리면 최단이라는 보장은 잃고 속도를
# 얻습니다 — 여기서 필요한 것은 최적해가 아니라 '규정 안의 다른 길' 이고, 합법은 first_breach 가
# 따로 봅니다.
HEURISTIC_SCALE = {"b": 1.0 + ALTITUDE_WEIGHT / 2.0, "c": 1.2}
# 벌점 후보의 줄 당기기 창(격자 칸, 약 1.2 km). (a) 는 80칸이지만, (b) 는 지름길마다 그 선분의 안전
# 고도를 새로 셈해야 해서(긴 선분일수록 비쌉니다) 80칸이면 할렘 한 판에 37초가 걸렸습니다(실측).
VARIANT_PULL_WINDOW = 24
# 탐색이 한도를 거의 다 쓰고 길을 찾았을 때, 그 길을 버리지 않도록 줄 당기기에 더 주는 시간(초).
PULL_GRACE_S = 2.0
# 벌점 탐색은 (a) 둘레 이만큼(m) 안에서만 합니다. 대안은 최단의 이웃이지 도시 반대편이 아닙니다 —
# 그리고 할렘처럼 0ft 격자를 크게 도는 여정에서 창(12 km) 전체를 벌점 탐색하면 노드 한도(12만)를
# 4초 만에 다 쓰고도 못 찾았습니다(실측: (b)(c) 없음, 후보 (a) 하나). 폭은 (c) 가 회랑에서 벌점
# 거리(250 m)만큼 비킬 자리가 넉넉히 남도록 잡습니다. 한도는 시간(deadline)이 따로 쥡니다.
VARIANT_TUBE_M = 1200.0
VARIANT_NODE_BUDGET = 400_000
# (a) 보다 이만큼 넘게 긴 후보는 뺍니다. 두 배 가까이 도는 길은 고를 거리가 아니라
# 배터리 문제입니다.
MAX_VARIANT_STRETCH = 1.8
# 경로를 점으로 펼칠 때의 간격(m). 같은 길인지, 무엇 옆을 지나는지를 이 점들로 잽니다.
SAMPLE_STEP_M = 25.0


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
        # (b)(c) 후보의 기억. 찾은 것만 기억합니다 — 한도에 걸려 못 찾은 것은 다음에 기억이 더 찬
        # 채로 다시 찾으면 나올 수 있습니다.
        self._variant_memo: dict[tuple, list[dict]] = {}
        self._memo_for = -1
        # 마지막 candidates() 의 후보별 소요 시간(초). 측정과 로그용입니다.
        self.last_timings: dict[str, float] = {}

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
            self._variant_memo.clear()
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

    def _pull(self, path: list[tuple[int, int]], window: int = 80,
              keep=None, deadline: float | None = None) -> list[tuple[int, int]]:
        """줄을 당깁니다. 막는 게 없는 구간은 곧게 펴집니다.

        A* 는 같은 길이면 어느 쪽으로 꺾든 값이 같아서, 뻥 뚫린 강 위에서도 격자를
        한 칸씩 밟은 계단이 나옵니다. 꺾인 점만 남기는 방식은 한 군데라도 걸리면
        경로 전체를 원본으로 되돌려서, 장애물이 없는 구간까지 같이 계단이 됐습니다.
        여기서는 갈 수 있는 데까지 곧게 가고, 막히는 자리에서만 꺾습니다.

        keep(i, j) 가 있으면 i→j 지름길이 그 후보의 성격(낮게·떨어져)을 지킬 때만 당깁니다. 안
        그러면 낮게 돌던 길이 곧게 펴지며 탑 위로 올라가고, 비켜 가던 길이 회랑을 가로지릅니다.
        deadline 이 지나면 남은 구간은 당기지 않고 꺾인 점만 남겨 붙입니다((a) 는 넘기지 않음).
        """
        if len(path) < 3:
            return path
        kept = [path[0]]
        index = 0
        while index < len(path) - 1:
            if deadline is not None and time.monotonic() > deadline:
                kept.extend(self._simplify(path[index:])[1:])
                return kept
            furthest = index + 1
            for candidate in range(min(len(path) - 1, index + window), index + 1, -1):
                if keep is not None and not keep(index, candidate):
                    continue
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

    def _search(self, starts, goals, budget: int = 120_000, step_cost=None,
                deadline: float | None = None, heuristic_scale: float = 1.0, allowed=None):
        """여러 출발 격자점에서 동시에 시작해 목적지 격자점 중 아무 데나 닿으면 끝납니다.

        step_cost(node, nxt, step) 가 있으면 간선 비용을 그것으로 셉니다(후보 (b)(c) 의 벌점).
        heuristic_scale 은 그 벌점에 맞춰 휴리스틱을 부풀리는 배율(가중 A*)이고, deadline
        (monotonic)이 지나면 None, allowed(node) 가 False 인 격자점은 밟지 않습니다(대안의 관).
        (a) 는 넷 다 없이 부르므로 오늘과 한 칸도 다르지 않습니다.
        """
        starts = [starts] if isinstance(starts, tuple) else list(starts)
        goals = {goals} if isinstance(goals, tuple) else set(goals)
        goal = min(goals, key=lambda node: math.dist(node, starts[0]))
        origin = starts[0]
        reach = max(40, int(SEARCH_REACH_M / (self.cell * 110_570.0)))

        def heuristic(node):
            return math.dist(node, goal) * heuristic_scale

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
            if (deadline is not None and seen % DEADLINE_CHECK_EVERY == 0
                    and time.monotonic() > deadline):
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
                if allowed is not None and not allowed(nxt):
                    continue
                if self._blocked(nxt) or self._crosses(node, nxt):
                    continue
                step = math.hypot(di, dj)
                fresh = cost + (step if step_cost is None else step_cost(node, nxt, step))
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

    # ---------- 후보 ----------

    def candidates(self, start: tuple[float, float], goal: tuple[float, float],
                   context: dict | None = None, budget_s: float | None = None) -> list[dict]:
        """규정 안의 길을 셋까지: (a) 최단 (b) 가장 낮게 (c) 다른 기체·사고 구역에서 떨어져.

        셋 다 우리 사본의 first_breach 를 통과한 것만 나옵니다 — 합법은 고른 뒤가 아니라 만들 때
        정해집니다. 거의 같은 길(DISTINCT_M 안)은 하나로 칩니다. context 는 비켜 갈 것
        (keep_clear_shapes 의 입력)이고, 비켜 갈 것이 없으면 (c) 는 (a) 와 같은 길이라 만들지
        않습니다.
        이 파일은 고르지 않습니다 — 고르는 것은 운영사의 모델이고, 무엇을 고르든 런타임이
        판정합니다.
        후보 하나: {id, label, legs, length_m, max_alt_m, min_alt_m, reason_tags}.
        """
        began = time.monotonic()
        shortest = self.plan(start, goal)
        self.last_timings = {"a": round(time.monotonic() - began, 3)}
        if shortest is None:
            return []   # (a) 가 없으면 갈 길이 없는 것입니다. 벌점을 붙인 탐색도 같은 벽에 막힙니다
        shapes = keep_clear_shapes(context)
        budget = VARIANT_BUDGET_S if budget_s is None else max(0.0, float(budget_s))
        found = [self._candidate("a", [leg.to_dict() for leg in shortest.legs], shapes,
                                 ["detour" if shortest.detoured else "straight"])]
        if budget <= 0.0:
            return found
        for variant in ("b", "c"):
            if variant == "c" and not shapes:
                continue
            began = time.monotonic()
            legs = self._variant(variant, start, goal, shapes, budget, found[0]["legs"])
            self.last_timings[variant] = round(time.monotonic() - began, 3)
            if legs is None:
                continue
            candidate = self._candidate(variant, legs, shapes, [VARIANT_TAGS[variant]])
            if candidate["length_m"] > MAX_VARIANT_STRETCH * max(1, found[0]["length_m"]):
                continue
            if any(same_route(candidate["legs"], other["legs"]) for other in found):
                continue
            found.append(candidate)
        return found

    def _variant(self, variant: str, start: tuple[float, float], goal: tuple[float, float],
                 shapes: list["KeepClear"], budget_s: float,
                 around: list[dict]) -> list[dict] | None:
        """벌점을 붙인 A* 로 (b) 또는 (c) 를 찾습니다. 통과한 legs 또는 None.

        그 뒤는 (a) 와 같습니다: 줄 당기기(성격을 지킬 때만) → 꺾인 점만 → 원본, 마지막에
        first_breach.
        """
        key = (variant, round(start[0], 4), round(start[1], 4), round(goal[0], 4),
               round(goal[1], 4), tuple(shape.key for shape in shapes) if variant == "c" else ())
        if key in self._variant_memo:
            return self._variant_memo[key]
        starts = self._free_nodes_near(start)
        goals = self._free_nodes_near(goal)
        if not starts or not goals:
            return None
        deadline = time.monotonic() + budget_s
        tube = self._tube(around, VARIANT_TUBE_M)
        if variant == "b":
            path = self._search(starts, goals, VARIANT_NODE_BUDGET, self._altitude_cost,
                                deadline, HEURISTIC_SCALE["b"], tube)
            keep = None if path is None else self._keeps_low(path)
        else:
            penalty = self._penalty_for(shapes)

            def clearance_cost(node, nxt, step):
                return step * (1.0 + penalty(nxt))

            path = self._search(starts, goals, VARIANT_NODE_BUDGET, clearance_cost, deadline,
                                HEURISTIC_SCALE["c"], tube)
            keep = None if path is None else self._keeps_clear(path, penalty, shapes)
        if path is None:
            # 한도 안에 못 찾았습니다. 기억하지 않습니다 — 다음엔 간선 기억이 더 차 있습니다
            return None
        pull_by = max(deadline, time.monotonic() + PULL_GRACE_S)
        pulled = self._pull(path, window=VARIANT_PULL_WINDOW, keep=keep, deadline=pull_by)
        for nodes in (pulled, self._simplify(path), path):
            if not self._legal_chain(nodes):
                continue
            legs = [leg.to_dict() for leg in self._attach(self._to_legs(nodes), start, goal)]
            if first_breach(self.airspace, legs) is None:
                self._variant_memo[key] = legs
                return legs
        return None

    def _tube(self, legs: list[dict], width_m: float):
        """격자점이 legs 에서 width_m 안인가. 탐색 중 같은 점을 여러 번 보므로
        이번 탐색 동안만 기억합니다."""
        points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
        segments = list(zip(points, points[1:], strict=False))
        known: dict[tuple[int, int], bool] = {}

        def inside(node: tuple[int, int]) -> bool:
            value = known.get(node)
            if value is None:
                lat, lon = self._coords(node)
                value = min(_point_segment_m(lat, lon, a, b)[0] for a, b in segments) <= width_m
                known[node] = value
            return value

        return inside

    def _altitude_cost(self, node, nxt, step: float) -> float:
        """(b) 의 간선 비용. 그 간선의 가장 낮은 안전 고도가 높을수록 비쌉니다(_edge 는 기억)."""
        altitude = self._edge(node, nxt)
        if altitude is None:
            return step        # 막힌 간선은 _search 가 먼저 거릅니다. 여기 올 일은 없습니다
        span = max(1.0, self.cruise_alt_m - self.floor_alt_m)
        return step * (1.0 + ALTITUDE_WEIGHT * max(0.0, altitude - self.floor_alt_m) / span)

    def _penalty_for(self, shapes: list["KeepClear"]):
        """(c) 의 격자점 벌점. 같은 점을 탐색 중에 여러 번 보므로 이 탐색 동안만 기억합니다."""
        known: dict[tuple[int, int], float] = {}

        def penalty(node: tuple[int, int]) -> float:
            value = known.get(node)
            if value is None:
                value = clear_penalty(*self._coords(node), shapes)
                known[node] = value
            return value

        return penalty

    def _keeps_low(self, path: list[tuple[int, int]]):
        """지름길이 그 사이 원래 길의 가장 높은 구간보다 높아지지 않을 때만 당깁니다."""
        altitudes = [self._edge(a, b) for a, b in zip(path, path[1:], strict=False)]
        altitudes = [self.cruise_alt_m if alt is None else alt for alt in altitudes]

        def keep(index: int, candidate: int) -> bool:
            shortcut = self._edge(path[index], path[candidate])
            return shortcut is not None and shortcut <= max(altitudes[index:candidate]) + 0.5

        return keep

    def _keeps_clear(self, path: list[tuple[int, int]], penalty, shapes: list["KeepClear"]):
        """지름길 위 어느 점도 그 사이 원래 길보다 비킬 것에 가깝지 않을 때만 당깁니다."""
        along = [penalty(node) for node in path]

        def keep(index: int, candidate: int) -> bool:
            limit = max(along[index:candidate + 1]) + 0.05
            a, b = self._coords(path[index]), self._coords(path[candidate])
            steps = max(1, int(_flat_m(a, b) / SAMPLE_STEP_M))
            return all(clear_penalty(a[0] + (b[0] - a[0]) * k / steps,
                                     a[1] + (b[1] - a[1]) * k / steps, shapes) <= limit
                       for k in range(steps + 1))

        return keep

    def _candidate(self, variant: str, legs: list[dict], shapes: list["KeepClear"],
                   tags: list[str]) -> dict:
        """후보 한 줄. 무엇 옆(CLEAR_MARGIN_M 안)을 지나는지 태그로 붙입니다 — 모델과 규칙이
        읽습니다."""
        altitudes = [float(leg["alt_m"]) for leg in legs]
        near = [f"near-{kind}:{name}" for kind, name in exposure(legs, shapes)]
        return {"id": variant, "label": CANDIDATE_LABELS[variant], "legs": legs,
                "length_m": round(route_length_m(legs)),
                "max_alt_m": round(max(altitudes), 1), "min_alt_m": round(min(altitudes), 1),
                "reason_tags": list(tags) + near}


# ---------- 비켜 갈 것 (후보 (c)) ----------


@dataclass(frozen=True)
class KeepClear:
    """(c) 후보가 비켜 가는 것 하나: 다른 기체의 승인 회랑(선분들) 또는 원(사고·공지 구역)."""

    id: str
    kind: str                                  # traffic | keepout
    segments: tuple = ()                       # (((lat, lon), (lat, lon)), ...)
    centre: tuple | None = None                # 원의 중심 (lat, lon)
    radius_m: float = 0.0
    box: tuple = (0.0, 0.0, 0.0, 0.0)          # (south, north, west, east) — 벌점 거리만큼 넓힘

    def in_box(self, lat: float, lon: float) -> bool:
        south, north, west, east = self.box
        return south <= lat <= north and west <= lon <= east

    def distance_m(self, lat: float, lon: float) -> float:
        if self.centre is not None:
            return max(0.0, _flat_m((lat, lon), self.centre) - self.radius_m)
        return min((_point_segment_m(lat, lon, a, b)[0] for a, b in self.segments),
                   default=math.inf)

    @property
    def key(self) -> tuple:
        """같은 모양이면 같은 값(좌표 약 10 m 로 반올림). (c) 기억의 열쇠입니다."""
        centre = tuple(round(value, 4) for value in (self.centre or ()))
        segments = tuple((round(a[0], 4), round(a[1], 4), round(b[0], 4), round(b[1], 4))
                         for a, b in self.segments)
        return (self.id, round(self.radius_m), centre, segments)


def keep_clear_shapes(context: dict | None) -> list[KeepClear]:
    """비켜 갈 것들. context = {"traffic": [{"id", "legs": [{"lat", "lon"}, ...]}],
    "keepouts": [{"id", "lat", "lon", "radius_m"} 또는 {"id", "polygon": [[lat, lon], ...]}]}.

    다각형은 무게중심과 가장 먼 꼭짓점까지의 원으로 잽니다. 판정이 아니라 벌점이라 넉넉한 쪽이
    맞습니다 — 구역 자체는 이미 금지 부피로 공역 사본에 있고 계획기는 그걸 절대 안 지납니다.
    못 읽는 항목은 건너뜁니다(런타임 /state 의 모양이 바뀌어도 후보 (c) 만 빠질 뿐입니다).
    """
    context = context or {}
    shapes: list[KeepClear] = []
    for item in context.get("traffic") or []:
        points = _points_of(item.get("legs") if isinstance(item, dict) else None)
        if not points:
            continue
        if len(points) == 1:
            points = points * 2
        shapes.append(KeepClear(str(item.get("id") or "traffic"), "traffic",
                                segments=tuple(zip(points, points[1:], strict=False)),
                                box=_box(points, 0.0)))
    for item in context.get("keepouts") or []:
        centre, radius = _circle_of(item if isinstance(item, dict) else {})
        if centre is None:
            continue
        shapes.append(KeepClear(str(item.get("id") or "keepout"), "keepout", centre=centre,
                                radius_m=radius, box=_box([centre], radius)))
    return shapes


def clear_penalty(lat: float, lon: float, shapes: list[KeepClear]) -> float:
    """가장 가까운 비킬 것까지의 거리로 셈한 벌점. 벌점 거리 밖이면 0, 맞닿으면 CLEAR_WEIGHT."""
    nearest = math.inf
    for shape in shapes:
        if shape.in_box(lat, lon):
            nearest = min(nearest, shape.distance_m(lat, lon))
    if nearest >= CLEAR_MARGIN_M:
        return 0.0
    return CLEAR_WEIGHT * (1.0 - nearest / CLEAR_MARGIN_M)


def exposure(legs: list[dict], shapes: list[KeepClear]) -> list[tuple[str, str]]:
    """경로가 벌점 거리 안으로 지나는 것들 [(kind, id)]."""
    points = route_samples(legs)
    near = []
    for shape in shapes:
        if any(shape.in_box(lat, lon) and shape.distance_m(lat, lon) < CLEAR_MARGIN_M
               for lat, lon in points):
            near.append((shape.kind, shape.id))
    return near


def route_samples(legs: list[dict], step_m: float = SAMPLE_STEP_M) -> list[tuple[float, float]]:
    """경로를 step_m 간격의 점으로 펼칩니다(끝점 포함)."""
    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
    samples: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:], strict=False):
        steps = max(1, int(_flat_m(a, b) / step_m))
        samples.extend((a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)
                       for k in range(steps))
    if points:
        samples.append(points[-1])
    return samples


def route_length_m(legs: list[dict]) -> float:
    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
    return sum(_flat_m(a, b) for a, b in zip(points, points[1:], strict=False))


def same_route(first: list[dict], second: list[dict], limit_m: float = DISTINCT_M) -> bool:
    """두 경로의 모든 점이 서로 limit_m 안인가(양방향 하우스도르프 거리). 그러면 같은 길입니다."""
    return _within(first, second, limit_m) and _within(second, first, limit_m)


def _within(first: list[dict], second: list[dict], limit_m: float) -> bool:
    segments = [((float(a["lat"]), float(a["lon"])), (float(b["lat"]), float(b["lon"])))
                for a, b in zip(second, second[1:], strict=False)]
    if not segments:
        return False
    for lat, lon in route_samples(first, 2 * SAMPLE_STEP_M):
        if min(_point_segment_m(lat, lon, a, b)[0] for a, b in segments) > limit_m:
            return False
    return True


def _flat_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _points_of(raw) -> list[tuple[float, float]]:
    points = []
    for item in raw or []:
        try:
            lat, lon = (float(item["lat"]), float(item["lon"])) if isinstance(item, dict) \
                else (float(item[0]), float(item[1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon):
            points.append((lat, lon))
    return points


def _circle_of(item: dict) -> tuple[tuple[float, float] | None, float]:
    """원 하나로. 중심·반경이 있으면 그대로, 다각형이면 무게중심 + 가장 먼 꼭짓점."""
    try:
        if item.get("lat") is not None and item.get("lon") is not None:
            return (float(item["lat"]), float(item["lon"])), max(0.0, float(item.get("radius_m")
                                                                             or 0.0))
    except (TypeError, ValueError):
        return None, 0.0
    polygon = _points_of(item.get("polygon"))
    if not polygon:
        return None, 0.0
    centre = (sum(p[0] for p in polygon) / len(polygon), sum(p[1] for p in polygon) / len(polygon))
    return centre, max(_flat_m(centre, point) for point in polygon)


def _box(points: list[tuple[float, float]], radius_m: float) -> tuple[float, float, float, float]:
    reach = radius_m + CLEAR_MARGIN_M
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) - reach / METRES_PER_DEG_LAT, max(lats) + reach / METRES_PER_DEG_LAT,
            min(lons) - reach / METRES_PER_DEG_LON, max(lons) + reach / METRES_PER_DEG_LON)
