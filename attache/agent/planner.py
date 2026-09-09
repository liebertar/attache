"""The drone operator's own route planner.

Drawing the route is the operator's job. They know their aircraft, their schedule and
their customers. What they cannot do is decide whether the route is allowed — that is
somebody else's call, and this file never makes it.

So the planner draws the cheapest line it can, submits it, and if the runtime says no it
draws another one that avoids what it was told about. The operator's own copy of the
airspace may be stale or read differently; the disagreement is resolved by asking, not by
assuming.
"""

from attache.core.geo import Airspace, Volume
from attache.core.route import Router


class OperatorPlanner:
    def __init__(self, airspace: Airspace | None = None):
        # 우리 회사가 가진 공역 사본. 최신이 아닐 수도 있습니다.
        self.airspace = airspace or Airspace()
        self.router = Router(self.airspace)
        self.learned: set[str] = set()   # 런타임이 알려준 것들

    def load(self, raw_volumes: list[dict]) -> None:
        for raw in raw_volumes:
            self.airspace.add(Volume.from_dict(raw))

    def note_refusal(self, volume_id: str | None) -> None:
        """거절 사유에 나온 구역을 우리 지도에도 반영합니다."""
        if volume_id:
            self.learned.add(volume_id)

    def draw(self, start: tuple[float, float], goal: tuple[float, float]) -> list[dict] | None:
        """직선이 되면 직선으로, 안 되면 우회로로. 안 되면 None."""
        route = self.router.plan(start, goal)
        if route is None:
            return None
        return [leg.to_dict() for leg in route.legs]

    def start_blocked(self, start: tuple[float, float], telemetry: dict | None = None) -> bool:
        """출발점 자체가 금지 구역 안(또는 이격 거리 안)인가. 그러면 목적지 문제가 아닙니다."""
        altitude = float((telemetry or {}).get("alt_m") or self.router.cruise_alt_m)
        return self.airspace.too_close(start[0], start[1], altitude)

    def straight(self, start: tuple[float, float], goal: tuple[float, float],
                 alt_m: float | None = None) -> list[dict]:
        """제일 싼 길: 직선 하나. 고도는 우리 사본 기준의 가장 낮은 안전 고도이고, 그런 고도가
        없으면(옥상 + 이격이 천장을 넘음) 천장 아래 최대로 내서 런타임이 왜 안 되는지 말하게 둡니다."""
        if alt_m is None:
            alt_m = self.router.leg_altitude(start, goal)
            if alt_m is None:
                alt_m = min(self.router._altitude_at(*start), self.router._altitude_at(*goal))
        return self.straight_at(start, goal, alt_m)

    @staticmethod
    def straight_at(start: tuple[float, float], goal: tuple[float, float],
                    alt_m: float) -> list[dict]:
        """공역을 안 보는 계획. 직결 세계가 내는 그 경로입니다."""
        return [
            {"lat": start[0], "lon": start[1], "alt_m": alt_m},
            {"lat": goal[0], "lon": goal[1], "alt_m": alt_m},
        ]
