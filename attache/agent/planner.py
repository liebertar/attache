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

    @staticmethod
    def straight(start: tuple[float, float], goal: tuple[float, float],
                 alt_m: float) -> list[dict]:
        """공역을 안 보는 계획. 데이터가 없거나 안 볼 때 나오는 그 경로입니다."""
        return [
            {"lat": start[0], "lon": start[1], "alt_m": alt_m},
            {"lat": goal[0], "lon": goal[1], "alt_m": alt_m},
        ]
