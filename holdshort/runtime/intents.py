"""Operational intents: where an approved route will be, and when.

ASTM F3548-21 calls this an operational intent — a set of 4D volumes (a footprint, an
altitude band, a time window) that an operator shares before flying. Two intents conflict
if and only if a volume of each intersects in space and time (3.2.8); anything more than a
centimetre apart is clear. Strategic deconfliction is refusing the second filing while it
is still a filing, which is much cheaper than separating two aircraft that are already
airborne.

The operator never states the volumes. The runtime derives them from the filed legs and the
performance the operator declared for the aircraft, so a window cannot be understated to
hide a crossing. The runtime still only verifies: it does not move the route, it says which
aircraft, where and until when.
"""

import math
import uuid
from dataclasses import dataclass, field

from holdshort.core.config import Performance
from holdshort.core.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    TRAFFIC_VERTICAL_M,
    _point_segment_m,
    ground_clamped,
)

# 시간 여유. 승인 확인·적재가 예정보다 몇 초 밀리거나 당겨질 수 있어서 창을 양쪽으로 넓힙니다.
# 30틱 = 24초 = 순항으로 530m. 창이 넓을수록 안전하고, 그만큼 같은 공역을 못 나눠 씁니다.
TIME_PAD_TICKS = 30
# 새 신청의 경로를 이 간격으로 찍어 남의 회랑에 들어가는 첫 점을 찾습니다. 30m 회랑을 5m 로
# 찍으면 놓치는 것은 회랑 가장자리를 5m 이하로 스치는 경우뿐이고, 그건 29.9m 떨어진 것입니다.
SAMPLE_M = 5.0
# F3548 3.2.8: 1cm 보다 떨어져 있으면 겹치지 않은 것입니다.
CLEAR_M = 0.01
# 끝을 모르는 부피의 이탈 틱. 회수돼 떠 있는 기체는 언제 내릴지 모릅니다 — 내리거나 새 승인이
# 대신할 때까지 그 자리를 덮습니다.
OPEN_ENDED_TICK = 10 ** 9
# 링크가 끊긴 기체의 예약을 명목 착지 뒤로 얼마나 더 붙잡나. 여기에 부피의 여유 TIME_PAD_TICKS 가
# 한 번 더 붙습니다(명목 착지 + 60틱). 텔레메트리가 없으니 늦게 뜬 기체가 명목보다 몇 틱 늦게
# 내리는 것까지 덮어야 합니다. 그 뒤로는 기체가 신고한 대로 내렸다고 보고 길을 풀지만, 착륙장은
# 링크가 돌아올 때까지 그 기체의 것입니다(의도가 살아 있으므로 landing_conflict 가 막음).
LOST_LINK_MARGIN_TICKS = TIME_PAD_TICKS

ACCEPTED = "accepted"     # 승인됐고 아직 안 떴습니다
ACTIVATED = "activated"   # 텔레메트리가 떠 있다고 합니다
ENDED = "ended"           # 내렸거나 회수됐거나 새 승인이 대신했습니다

# 의도의 종류. 승인한 경로(route), 경로를 잃고 떠 있는 기체가 서 있을 자리(contingency),
# 의도 없이 떠 있는 기체의 지금 자리(presence — 텔레메트리에서 그때그때 만들고 등록부에는 없음).
ROUTE = "route"
CONTINGENCY = "contingency"
PRESENCE = "presence"


@dataclass
class Volume4D:
    """회랑 한 토막. a→b 선분 둘레 lateral_m, 고도 띠 [floor, ceiling], 틱 [from, to)."""

    leg: int                          # 신청서의 구간 번호(blocked_leg 와 같은 셈)
    a: tuple[float, float]
    b: tuple[float, float]
    alt_lo: float                     # 명목 고도 띠. 순항 구간은 lo == hi
    alt_hi: float
    t_enter: int                      # 명목 진입·이탈 틱 (여유 전)
    t_exit: int
    lateral_m: float = TRAFFIC_LATERAL_M
    vertical_m: float = TRAFFIC_VERTICAL_M
    pad_ticks: int = TIME_PAD_TICKS

    @property
    def floor_m(self) -> float:
        return ground_clamped(self.alt_lo - self.vertical_m)

    @property
    def ceiling_m(self) -> float:
        return self.alt_hi + self.vertical_m

    @property
    def from_tick(self) -> int:
        return self.t_enter - self.pad_ticks

    @property
    def to_tick(self) -> int:
        return self.t_exit + self.pad_ticks

    @property
    def is_column(self) -> bool:
        return self.a == self.b

    def contains(self, lat: float, lon: float, alt_m: float, tick: int) -> bool:
        """이 점이 이 부피 안인가. 시간 → 고도 → 거리 순으로, 싼 것부터 봅니다."""
        if not (self.from_tick <= tick < self.to_tick):
            return False
        return self.covers(lat, lon, alt_m)

    def covers(self, lat: float, lon: float, alt_m: float) -> bool:
        """시간을 빼고 공간만. 링크가 끊긴 기체는 언제 어디 있을지가 아니라 어디에 있을 수
        있는지가 문제라, 두절의 예약과 복구 뒤 순응 검사가 이것을 씁니다."""
        if not (self.floor_m - CLEAR_M <= alt_m <= self.ceiling_m + CLEAR_M):
            return False
        return _point_segment_m(lat, lon, self.a, self.b)[0] <= self.lateral_m + CLEAR_M

    def polygon(self) -> list[tuple[float, float]]:
        """선분을 lateral_m 만큼 부풀린 직사각형. 화면용이고 판정은 contains 가 합니다."""
        north = (self.b[0] - self.a[0]) * METRES_PER_DEG_LAT
        east = (self.b[1] - self.a[1]) * METRES_PER_DEG_LON
        length = math.hypot(north, east)
        if length < 1e-9:
            north, east, length = 1.0, 0.0, 1.0
        ux, uy = north / length, east / length          # 진행 방향 단위벡터 (북, 동)
        px, py = -uy, ux                                  # 왼쪽 수직
        r = self.lateral_m

        def at(point, along, side):
            return (point[0] + (ux * along + px * side) / METRES_PER_DEG_LAT,
                    point[1] + (uy * along + py * side) / METRES_PER_DEG_LON)

        return [at(self.a, -r, r), at(self.b, r, r), at(self.b, r, -r), at(self.a, -r, -r)]

    def to_dict(self) -> dict:
        return {
            "leg": self.leg, "polygon": [[lat, lon] for lat, lon in self.polygon()],
            "floor_m": round(self.floor_m, 1), "ceiling_m": round(self.ceiling_m, 1),
            "from_tick": self.from_tick, "to_tick": self.to_tick,
        }


@dataclass
class Intent:
    asset: str
    proposal_id: str
    volumes: list[Volume4D]
    start: tuple[float, float]
    landing: tuple[float, float]
    depart_tick: int
    arrive_tick: int                  # 명목 착지 틱
    filed_tick: int
    state: str = ACCEPTED
    ended_reason: str = ""
    kind: str = ROUTE
    # 승인할 때 판정한 통신 두절 대비 행동(configs/fleet.yaml performance.lost_link).
    # continue_and_land 의 대비 부피는 승인 경로 + 착륙 기둥 — 곧 이 volumes 그대로입니다.
    contingency: str = ""
    # 링크가 끊겨 예약을 늘린 뒤면 새 텔레메트리가 끊긴 첫 틱. 끊기지 않았으면 None.
    dark_since: int | None = None
    id: str = field(default_factory=lambda: f"i_{uuid.uuid4().hex[:10]}")
    # 늘리기 전의 창 [(t_enter, t_exit)]. 링크가 돌아오면 이것으로 되돌립니다.
    _windows: list | None = field(default=None, repr=False, compare=False)

    @property
    def from_tick(self) -> int:
        return min(v.from_tick for v in self.volumes)

    @property
    def to_tick(self) -> int:
        return max(v.to_tick for v in self.volumes)

    @property
    def live(self) -> bool:
        return self.state in (ACCEPTED, ACTIVATED)

    def covers(self, lat: float, lon: float, alt_m: float) -> bool:
        """이 자리가 승인한 부피 어디엔가 드나(시간은 보지 않음). 링크 복구 뒤 순응 검사입니다."""
        return any(volume.covers(lat, lon, alt_m) for volume in self.volumes)

    def reserve_dark(self, last_seen_tick: int, at: tuple[float, float, float] | None,
                     margin_ticks: int = LOST_LINK_MARGIN_TICKS) -> int:
        """링크가 끊겼습니다. 남은 경로 전부를 마지막으로 본 틱부터 명목 착지 + 여유까지 막습니다.

        텔레메트리가 없으니 기체가 남은 경로의 어디쯤인지 모릅니다. 신고한 대비 행동
        (continue_and_land)은 승인 경로를 그대로 날아 목적지에 내리는 것이라, 남은 부피 전부가
        그 사이 어느 틱에든 쓰일 수 있습니다. 이미 지난 부피(마지막으로 본 자리보다 앞이고
        일정상으로도 나온 것)는 그대로 둡니다 — 떠난 이륙 기둥까지 다시 막으면 옆 자리 기체가 한
        대도 못 뜹니다. 예약이 풀리는 틱을 돌려줍니다.
        """
        if self._windows is None:
            self._windows = [(volume.t_enter, volume.t_exit) for volume in self.volumes]
        start = self._remaining_from(last_seen_tick, at)
        landing = last_seen_tick if self.arrive_tick >= OPEN_ENDED_TICK else self.arrive_tick
        until = max(landing, last_seen_tick) + margin_ticks
        for volume in self.volumes[start:]:
            volume.t_enter = min(volume.t_enter, last_seen_tick)
            if volume.t_exit < OPEN_ENDED_TICK:
                volume.t_exit = max(volume.t_exit, until)
        self.dark_since = last_seen_tick + 1
        return max(volume.to_tick for volume in self.volumes[start:])

    def release_dark(self) -> None:
        """링크가 돌아왔습니다. 늘렸던 창을 승인 때의 창으로 되돌립니다 — 이제 다시 보이니까요."""
        if self._windows is not None:
            for volume, (enter, leave) in zip(self.volumes, self._windows, strict=True):
                volume.t_enter, volume.t_exit = enter, leave
        self._windows = None
        self.dark_since = None

    def _remaining_from(self, tick: int, at: tuple[float, float, float] | None) -> int:
        """남은 경로가 시작되는 부피 번호. 일정상 아직 안 나온 첫 부피와 마지막으로 본 자리를 덮는
        첫 부피 중 앞선 것 — 일정보다 늦은 기체는 자리가, 빠른 기체는 일정이 더 앞을 가리킵니다."""
        by_time = next((index for index, volume in enumerate(self.volumes)
                        if volume.to_tick > tick), len(self.volumes) - 1)
        if at is None:
            return by_time
        by_place = next((index for index, volume in enumerate(self.volumes)
                         if volume.covers(*at)), None)
        return by_time if by_place is None else min(by_time, by_place)

    def reanchor(self, depart_tick: int) -> int:
        """실제 출발 틱으로 시간 창을 옮깁니다. 옮긴 틱 수(음수면 일찍 뜬 것)를 돌려줍니다.

        승인한 창보다 일찍 뜬 기체는 승인한 창에 없습니다. 의도를 그대로 두면 판정이 빈 하늘을
        막고 실제 기체는 아무 창에도 안 잡힙니다 — 있는 그대로를 등록하는 것이 먼저입니다.
        """
        shift = depart_tick - self.depart_tick
        for volume in self.volumes:
            volume.t_enter += shift
            if volume.t_exit < OPEN_ENDED_TICK:
                volume.t_exit += shift
        self.depart_tick += shift
        if self.arrive_tick < OPEN_ENDED_TICK:
            self.arrive_tick += shift
        return shift

    def to_dict(self) -> dict:
        return {"asset": self.asset, "state": self.state, "id": self.id, "kind": self.kind,
                "from_tick": self.from_tick, "to_tick": self.to_tick,
                "depart_tick": self.depart_tick, "arrive_tick": self.arrive_tick,
                "proposal_id": self.proposal_id, "ended": self.ended_reason or None,
                "contingency": self.contingency or None, "dark_since": self.dark_since}

    def detailed(self) -> dict:
        return {**self.to_dict(), "volumes": [v.to_dict() for v in self.volumes]}


@dataclass
class Conflict:
    asset: str                        # 상대 기체
    intent_id: str | None             # 의도 없이 서 있는 기체(텔레메트리)면 None
    kind: str                         # traffic | landing
    at: tuple[float, float]           # 새 경로가 상대 부피에 처음 들어가는 점
    leg: int
    until_tick: int                   # 상대 부피가 비는 틱. 이때부터 떠도 됩니다
    alt_m: float = 0.0
    tick: int = 0


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def corridor_widths(performance: Performance) -> tuple[float, float]:
    """상대 쪽 회랑의 반폭 (옆, 위아래).

    분리 최소치에 운영사가 신고한 항법 오차를 더합니다. 최소치만으로 그리면 31m 옆의 두 승인이
    한쪽이 1m 만 벗어나도 분리를 잃고, 시뮬레이터의 경유점 반경(6m, 모서리를 자름)으로 충분히
    그렇게 됩니다. 위아래는 한 틱의 승강 폭만큼 — 꼭짓점에서 고도를 맞추는 동안의 오차입니다.
    """
    step = max(performance.climb_mps, performance.descent_mps) * performance.seconds_per_tick
    return TRAFFIC_LATERAL_M + performance.nav_tolerance_m, TRAFFIC_VERTICAL_M + step


def schedule(legs: list[dict], depart_tick: int, start_alt_m: float,
             performance: Performance) -> tuple[list[Volume4D], int]:
    """신고 성능으로 구간마다 언제 들어가고 나오는지. (부피 목록, 명목 착지 틱).

    출발점에서 첫 구간 고도까지 제자리 상승, 구간은 순항 속도, 꼭짓점에서 제자리 승강, 끝점에서
    지면까지 하강 — 시뮬레이터가 실제로 나는 순서 그대로입니다(World._advance). 수직 구간은
    길이 0 인 부피로 따로 둡니다. 40m 에서 118m 로 오르는 동안 기체는 두 순항 띠 어느 쪽에도
    없어서, 띠 두 개만 두면 그 사이를 지나는 다른 기체를 못 봅니다.
    """
    step_m = performance.cruise_mps * performance.seconds_per_tick
    climb_m = performance.climb_mps * performance.seconds_per_tick
    descent_m = performance.descent_mps * performance.seconds_per_tick
    lateral, vertical = corridor_widths(performance)

    def change_ticks(from_m: float, to_m: float) -> int:
        rate = climb_m if to_m > from_m else descent_m
        return int(math.ceil(abs(to_m - from_m) / rate)) if rate > 0 else 0

    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs]
    altitudes = [ground_clamped(float(leg.get("alt_m") or 0.0)) for leg in legs]
    volumes: list[Volume4D] = []
    tick = depart_tick
    height = ground_clamped(start_alt_m)
    for index in range(len(points) - 1):
        target = altitudes[index + 1]
        # 꼭짓점(첫 점이면 이륙 기둥)에서 제자리 승강
        rising = change_ticks(height, target)
        if rising or index == 0:
            volumes.append(Volume4D(index + 1, points[index], points[index],
                                    min(height, target), max(height, target), tick, tick + rising,
                                    lateral, vertical))
            tick += rising
        height = target
        # 구간 순항
        moving = int(math.ceil(_distance_m(points[index], points[index + 1]) / step_m))
        volumes.append(Volume4D(index + 1, points[index], points[index + 1],
                                height, height, tick, tick + moving, lateral, vertical))
        tick += moving
    arrive = tick + change_ticks(height, 0.0)
    volumes.append(Volume4D(len(points) - 1, points[-1], points[-1], 0.0, height, tick, arrive,
                            lateral, vertical))
    return volumes, arrive


def hold(asset: str, at: tuple[float, float], alt_m: float, now: int, performance: Performance,
         exit_point: tuple[float, float] | None = None, kind: str = CONTINGENCY,
         proposal_id: str = "") -> Intent:
    """경로 없이 떠 있는 기체가 덮는 자리. 끝나는 틱이 없습니다.

    회수·물림·반려로 의도가 끝난 기체는 그 자리에 떠서 기다립니다(구역 안이었으면 가장 가까운
    바깥까지 날아가서). 의도가 없다고 판정에서 빠지면 그 자리를 지나는 다음 신청이 그대로 승인됩니다
    — 떠 있는 기체는 언제나 어딘가에 있고, 그 자리는 전략적 비충돌이 봐야 합니다. 내리거나(observe:
    arrived) 새 경로가 승인되면(accept: replaced) 끝납니다.
    """
    lateral, vertical = corridor_widths(performance)
    volumes: list[Volume4D] = []
    tick, here = now, at
    if exit_point is not None and _distance_m(at, exit_point) > CLEAR_M:
        step_m = performance.cruise_mps * performance.seconds_per_tick
        moving = int(math.ceil(_distance_m(at, exit_point) / step_m))
        volumes.append(Volume4D(1, at, exit_point, alt_m, alt_m, now, now + moving,
                                lateral, vertical))
        tick, here = now + moving, exit_point
    volumes.append(Volume4D(len(volumes) + 1, here, here, alt_m, alt_m, tick, OPEN_ENDED_TICK,
                            lateral, vertical))
    return Intent(asset=asset, proposal_id=proposal_id, volumes=volumes, start=at, landing=here,
                  depart_tick=now, arrive_tick=OPEN_ENDED_TICK, filed_tick=now, state=ACTIVATED,
                  kind=kind)


def samples(volumes: list[Volume4D]):
    """새 경로가 지나는 점들을 (lat, lon, alt, tick, leg) 로, 나는 순서대로."""
    for volume in volumes:
        span = max(1, volume.t_exit - volume.t_enter)
        if volume.is_column:
            height = volume.alt_hi - volume.alt_lo
            count = max(1, int(math.ceil(height / SAMPLE_M)))
            for k in range(count + 1):
                fraction = k / count
                yield (volume.a[0], volume.a[1], volume.alt_lo + height * fraction,
                       volume.t_enter + int(span * fraction), volume.leg)
            continue
        length = _distance_m(volume.a, volume.b)
        count = max(1, int(math.ceil(length / SAMPLE_M)))
        for k in range(count + 1):
            fraction = k / count
            yield (volume.a[0] + (volume.b[0] - volume.a[0]) * fraction,
                   volume.a[1] + (volume.b[1] - volume.a[1]) * fraction,
                   volume.alt_lo, volume.t_enter + int(span * fraction), volume.leg)


def first_conflict(volumes: list[Volume4D], others: list[Intent]) -> Conflict | None:
    """새 경로가 남의 부피에 처음 들어가는 점. 없으면 None.

    새 경로 쪽은 부풀리지 않은 중심선의 점을, 상대 쪽은 부풀린 부피를 씁니다. 양쪽을 다
    부풀리면 30m 옆으로 비킨 경로도 겹친다고 나와서 '고도 +30m' 같은 해결이 영영 안 됩니다.
    """
    live = [(intent, intent.volumes) for intent in others if intent.live]
    if not live:
        return None
    for lat, lon, alt, tick, leg in samples(volumes):
        for intent, theirs in live:
            for volume in theirs:
                if volume.contains(lat, lon, alt, tick):
                    # 자리(presence)는 등록된 의도가 아니라 id 가 없습니다
                    return Conflict(intent.asset, None if intent.kind == PRESENCE else intent.id,
                                    "traffic", (lat, lon), leg, volume.to_tick, alt, tick)
    return None


def ground_conflict(landing: tuple[float, float], arrive_tick: int, last_leg: int,
                    occupants: list[tuple[str, tuple[float, float], Intent | None]],
                    now: int) -> Conflict | None:
    """내려앉을 자리에 지금 다른 기체가 서 있는가 — 의도가 아니라 텔레메트리로.

    내린 기체의 의도는 끝나서 의도만 보면 그 자리가 빈 것으로 나오고, 다음 신청을 낼 때까지의
    틈은 몇 틱이 아니라 얼마든지 길어질 수 있습니다(거절·재시도·창고 자리). 서 있는 기체가 승인된
    출발(accepted)을 갖고 있고 새 도착이 그 뒤면 그때는 떠나고 없으니 됩니다.
    occupants 는 (기체, 자리, 살아 있는 의도 또는 None).
    """
    radius = TRAFFIC_LATERAL_M + CLEAR_M
    for asset, at, intent in occupants:
        if _distance_m(landing, at) > radius:
            continue
        leaving = intent is not None and intent.state == ACCEPTED
        if leaving and arrive_tick >= intent.depart_tick + TIME_PAD_TICKS:
            continue
        until = intent.depart_tick + TIME_PAD_TICKS if leaving else now + TIME_PAD_TICKS
        return Conflict(asset, intent.id if intent is not None else None, "landing", landing,
                        last_leg, until, 0.0, arrive_tick)
    return None


def landing_conflict(landing: tuple[float, float], arrive_tick: int, last_leg: int,
                     others: list[Intent], now: int) -> Conflict | None:
    """내려앉을 자리를 다른 의도가 쓰는가. 착륙장 하나에 두 대는 없습니다.

    상대의 착륙점과, 아직 안 뜬 상대의 출발점(뜰 때까지 거기 서 있음) 둘 다 봅니다. 내린 기체는
    다음 승인이 대신할 때까지 거기 서 있으므로 착륙점은 상대가 내린 뒤로 끝이 없습니다 — 착륙
    기둥이 끝나는 틱까지만 막으면 그 뒤에 도착하는 신청이 서 있는 기체 위로 내립니다. 반대로
    내가 먼저 내리는 자리에 상대가 뒤에 내리는 것도 같은 일이라, 같은 자리에 내리는 살아 있는 의도
    둘은 앞뒤 없이 겹칩니다. 회랑 부피는 출발 30틱 전부터만 자리를 덮어서, 그 전에 옆에 내리는
    것은 출발점 규칙이 잡습니다.
    """
    radius = TRAFFIC_LATERAL_M + CLEAR_M
    for intent in others:
        if not intent.live:
            continue
        if intent.kind == ROUTE and _distance_m(landing, intent.landing) <= radius:
            # 비는 틱은 모릅니다(상대가 다음 경로를 승인받아야 압니다). 가장 이른 가능성만 말합니다.
            return Conflict(intent.asset, intent.id, "landing", landing, last_leg,
                            max(intent.to_tick, now + TIME_PAD_TICKS), 0.0, arrive_tick)
        if (intent.state == ACCEPTED and _distance_m(landing, intent.start) <= radius
                and now <= arrive_tick < intent.depart_tick + TIME_PAD_TICKS):
            return Conflict(intent.asset, intent.id, "landing", landing, last_leg,
                            intent.depart_tick + TIME_PAD_TICKS, 0.0, arrive_tick)
    return None


class IntentRegistry:
    """기체마다 최신 의도 하나. 상태는 accepted → activated → ended 로만 갑니다."""

    def __init__(self):
        self._latest: dict[str, Intent] = {}
        # 승인한 창보다 일찍 뜬 기체. (의도, 승인했던 출발 틱). 런타임이 꺼내서 원장에 남깁니다.
        self.nonconforming: list[tuple[Intent, int]] = []

    def get(self, asset: str) -> Intent | None:
        return self._latest.get(asset)

    def others(self, asset: str) -> list[Intent]:
        """다른 기체의 살아 있는 의도. 자기 것은 새 승인이 대신하므로 안 봅니다."""
        return [i for a, i in self._latest.items() if a != asset and i.live]

    def live(self) -> list[Intent]:
        return [i for i in self._latest.values() if i.live]

    def accept(self, intent: Intent) -> Intent | None:
        """새 승인. 같은 기체의 앞 의도는 끝납니다. 끝난 앞 의도를 돌려줍니다."""
        previous = self._latest.get(intent.asset)
        if previous is not None and previous.live:
            previous.state, previous.ended_reason = ENDED, "replaced"
        self._latest[intent.asset] = intent
        return previous

    def end(self, asset: str, reason: str) -> Intent | None:
        intent = self._latest.get(asset)
        if intent is None or not intent.live:
            return None
        intent.state, intent.ended_reason = ENDED, reason
        return intent

    def observe(self, telemetry: dict, tick: int) -> list[tuple[str, str, str]]:
        """텔레메트리로 상태를 옮깁니다. (기체, 이전, 이후) 목록.

        떠 있으면 activated, 떠 있다가 내려앉았으면 ended(arrived). 창이 다 지나도록 안 떴으면
        ended(expired) — 안 그러면 죽은 의도가 남의 신청을 영영 막습니다.
        승인한 창(출발 - 여유)보다 일찍 떴으면 순응하지 않은 것입니다. 의도를 실제 출발 틱으로
        옮겨 등록하고 nonconforming 에 남깁니다 — 조종장치가 미룬 출발을 안 지킨 것을 판정이
        빈 하늘을 막는 채로 두면 안 됩니다.
        """
        changes = []
        for asset, intent in self._latest.items():
            if not intent.live:
                continue
            state = telemetry.get(asset) or {}
            airborne = float(state.get("alt_m") or 0.0) > 1.0
            was = intent.state
            if intent.state == ACCEPTED and airborne:
                if tick < intent.depart_tick - TIME_PAD_TICKS:
                    planned = intent.depart_tick
                    intent.reanchor(tick)
                    self.nonconforming.append((intent, planned))
                intent.state = ACTIVATED
            elif intent.state == ACTIVATED and not airborne and state:
                intent.state, intent.ended_reason = ENDED, "arrived"
            elif intent.state == ACCEPTED and tick >= intent.to_tick:
                intent.state, intent.ended_reason = ENDED, "expired"
            if intent.state != was:
                changes.append((asset, was, intent.state))
        return changes

    def drain_nonconforming(self) -> list[tuple[Intent, int]]:
        found, self.nonconforming = self.nonconforming, []
        return found

    def clear(self) -> None:
        self._latest.clear()
        self.nonconforming.clear()

    def snapshot(self) -> list[dict]:
        return [intent.to_dict() for intent in self._latest.values()]


# ---------- 텔레메트리 심장박동 ----------

LINK_OK = "ok"
LINK_LOST = "lost"


@dataclass
class Link:
    status: str = LINK_OK
    since_tick: int = 0               # 지금 상태가 시작된 틱. lost 면 새 기록이 끊긴 첫 틱
    last_seen_tick: int = 0           # 마지막으로 새 기록이 온 틱(텔레메트리의 틱 도장)
    declared_tick: int | None = None  # 런타임이 두절로 판정한 틱(lost 일 때만)

    def to_dict(self) -> dict:
        return {"status": self.status, "since_tick": self.since_tick,
                "last_seen_tick": self.last_seen_tick, "declared_tick": self.declared_tick}


@dataclass
class LinkEvent:
    asset: str
    kind: str                         # lost | restored
    since_tick: int                   # 새 기록이 끊긴 첫 틱
    last_seen_tick: int               # 끊기기 전 마지막 새 기록의 틱
    tick: int                         # 두절로 판정한 틱, 또는 새 기록이 다시 온 틱


class LinkWatch:
    """기체마다 텔레메트리가 언제 마지막으로 새로워졌나. 판정은 이것 하나입니다.

    떠 있는 기체의 기록이 timeout_ticks 동안 새로워지지 않으면 두절, 다시 새로워지면 복구. 새로움은
    기록의 틱 도장(telemetry_tick)으로 셉니다 — 위치가 그대로인지로 세면 제자리에 떠 있는 기체가
    두절로 보입니다. 도장이 없는 기록(도장을 안 주는 어댑터)은 심장박동을 모르는 것이고, 모르는 것은
    끊긴 것이 아닙니다(끝점 검사가 자리를 모르면 비교하지 않는 것과 같은 원칙). 땅에 있는 기체는
    두절로 판정하지 않습니다 — 잡아 둘 하늘이 없습니다.
    """

    def __init__(self, timeout_ticks: int):
        self.timeout_ticks = max(1, int(timeout_ticks))
        self.links: dict[str, Link] = {}

    def lost(self, asset: str) -> bool:
        link = self.links.get(asset)
        return link is not None and link.status == LINK_LOST

    def observe(self, telemetry: dict, tick: int) -> list[LinkEvent]:
        """이번 폴링의 텔레메트리로 상태를 옮깁니다. 바뀐 것만 돌려줍니다."""
        events = []
        for asset, state in telemetry.items():
            seen = _stamp(state, tick)
            link = self.links.get(asset)
            if link is None:
                link = self.links[asset] = Link(since_tick=seen, last_seen_tick=seen)
            if link.status == LINK_LOST:
                if seen > link.last_seen_tick:
                    events.append(LinkEvent(asset, "restored", link.since_tick,
                                            link.last_seen_tick, seen))
                    link.status, link.since_tick, link.last_seen_tick = LINK_OK, seen, seen
                    link.declared_tick = None
                continue
            link.last_seen_tick = max(link.last_seen_tick, seen)
            airborne = float(state.get("alt_m") or 0.0) > 1.0
            if airborne and tick - link.last_seen_tick >= self.timeout_ticks:
                link.status, link.since_tick = LINK_LOST, link.last_seen_tick + 1
                link.declared_tick = tick
                events.append(LinkEvent(asset, "lost", link.since_tick, link.last_seen_tick, tick))
        return events

    def clear(self) -> None:
        self.links.clear()

    def snapshot(self) -> dict:
        return {asset: link.to_dict() for asset, link in self.links.items()}


def _stamp(state: dict, tick: int) -> int:
    """기록의 틱 도장. 없거나 수가 아니면 지금 틱 — 모르는 것은 새로운 것으로 봅니다."""
    try:
        return tick if state.get("telemetry_tick") is None else int(state["telemetry_tick"])
    except (TypeError, ValueError):
        return tick
