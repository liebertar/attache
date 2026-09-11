"""The operator lets a small model sketch the route. The runtime still decides.

This is the strongest claim the project makes: who draws the line changes nothing about
the guarantee. A route drafted by Nemotron goes through exactly the same judge as one
from A*, and gets refused for exactly the same reasons. So the model may be wrong, slow,
or absent, and nothing it produces is executed until `first_breach` and the runtime have
said yes.

What the model gets: origin, goal, the rules in one paragraph, and a reading of the map —
what the straight line hits and at what distance, which of those cannot be crossed at any
legal altitude (go around, and which side is open), where the ceiling drops, and the
reason the last attempt was refused. What it returns: a list of legs, or nothing. What
this file does with it: schema check, bounds check, snap the ends, length check, the
operator's own altitude rule per leg (the same one the straight line uses), then the
operator's own pre-judgement with the same `first_breach` the runtime uses. Fails once →
one more ask that names every breach: the obstacle, its roof, the altitude that would
have been needed, whether that is above the limit (then it must be flown around), and
which side is clear. Fails twice → the caller draws with A*.
"""

import json
import math
import os
import time

from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    Volume,
    first_breach,
    leg_breaches,
)
from shared.llm.client import LlmTier, TieredLlm, parse_json_object

MAX_LEGS = 12
# 모델이 적는 고도의 허용 범위. 최저 순항(FLOOR_ALT_M 70 m)보다 낮게 적어도 버리지 않고
# _apply_altitude_rule 이 운영사 규칙으로 올립니다 — 어디로 갈지는 모델, 얼마나 높이는 규칙.
ALT_MIN_M = 40.0
ALT_MAX_M = 120.0
# 직선의 이만큼까지만 우회로로 봅니다. 그보다 길면 모델이 헤맨 것이고, A* 가 더 잘 그립니다.
MAX_STRETCH = 2.5
# 착륙장 모음의 경계 상자에서 이만큼(약 2km) 바깥까지가 서비스 영역입니다.
BBOX_MARGIN_DEG = 0.02
# 직선 위에서 모델에게 알려줄 장애물 개수. 첫 번째만 알려주면 그 뒤에 있는 것에 또 걸립니다.
OBSTACLE_LIMIT = 5
# 어떤 고도로도 못 넘는 것(돌아가야 하는 것)은 전부 알려줍니다. 하나라도 빠지면 거기에 걸립니다.
GO_AROUND_LIMIT = 12
# 장애물 옆으로 얼마나 비켜야 열리는지 재 보는 거리(m). 첫 번째 열린 자리를 알려줍니다.
SIDE_PROBES_M = (80, 150, 250, 400, 600, 900, 1300, 2000, 3000)
MAX_ASKS = 2
# 초안 한 번의 예산. 신청서(수백 토큰, 6초)와 다릅니다 — 지도 읽기 1,200 토큰을 넣고 7구간을 받는 데
# 한가한 Ollama 에서도 8.7~8.9초, 녹음된 통과 답은 5~19초였습니다. 기체 공통 타임아웃(6초)으로는
# 라이브에서 초안 15건이 전부 잘려 nano 가 그린 경로가 하나도 없었습니다. 기체는 지상에서
# 기다리는 중이라 이 시간은 화면에서 '거절 뒤 다시 그리는 중' 으로 보입니다.
# 실주행(Ollama, 드론 4대): 초안이 한 슬롯에 줄을 서서 19~28초, 30초로는 9건 중 7건이 잘렸습니다.
DRAFT_TIMEOUT_S = 60.0
# 서버가 방금 답을 못 줬으면 이만큼은 초안을 묻지 않고 A* 로 갑니다. 타임아웃 뒤에 5.6초를
# 기다렸다 같은 서버에 또 30초를 걸면, 끝나지 못할 호출 뒤에서 기체가 그만큼 더 섭니다.
DRAFT_BACKOFF_S = 30.0
# 마감까지 이보다 적게 남았으면 묻지 않습니다. 지도 읽기를 넣고 첫 토큰을 받는 데만 이만큼 걸립니다.
MIN_ASK_S = 2.0
# 건물을 넘으려면 옥상 + 이격(50m) 위여야 합니다. 딱 그 높이는 아직 구역 안이라(닫힌 구간) 0.5m 더 —
# 운영사 계획기(route.Router.leg_altitude)와 같은 셈입니다. 두 곳이 다르면 모델이 계획기와 다른
# 숫자를 듣습니다.
OVER_ROOF_MARGIN_M = 0.5

SYSTEM = (
    "You draft a flight route for one uncrewed delivery aircraft. You do not fly it and you "
    "do not approve it: a runtime judges every route against the rules below and refuses "
    "anything that breaks them, so draw carefully rather than optimistically. "
    "Reply with one JSON object and nothing else: "
    '{"legs":[{"lat":<deg>,"lon":<deg>,"alt_m":<m>}, ...]}. '
    "A leg is a waypoint; alt_m is the cruise altitude of the segment that ends at that "
    "waypoint. Rules: cruise between 70 and 120 m AGL. To cross a building you must be 50 m "
    "above its roof; if roof + 50 m is above 120 m you must go around it. Stay 10 m laterally "
    "clear of buildings 20 m or taller and 40 m clear of no-fly cells (they are forbidden at "
    "every altitude, so always go around them). Some cells cap the altitude; never plan above "
    "the cap there. The last waypoint is the landing point and needs 50 m clear of buildings "
    "and no-fly cells around it. Use at most 12 legs. The first leg must be the origin and "
    "the last leg the goal, both exactly as given. Every lat/lon must stay inside the service "
    "box. Use decimal degrees with 5 decimals. Open water and parks are the safe corridors "
    "in this city: prefer them, give every obstacle a wide berth (100 m or more, not 15 m), "
    "and detour early rather than clipping the corner of an obstacle."
)


def service_bbox(points: list[tuple[float, float]],
                 margin_deg: float = BBOX_MARGIN_DEG) -> tuple[float, float, float, float] | None:
    """(lat_min, lon_min, lat_max, lon_max). 점이 없으면 None."""
    if not points:
        return None
    lats = [float(p[0]) for p in points]
    lons = [float(p[1]) for p in points]
    return (min(lats) - margin_deg, min(lons) - margin_deg,
            max(lats) + margin_deg, max(lons) + margin_deg)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _offset(point: tuple[float, float], north_m: float, east_m: float) -> tuple[float, float]:
    return (point[0] + north_m / METRES_PER_DEG_LAT, point[1] + east_m / METRES_PER_DEG_LON)


def _heading(start: tuple[float, float], goal: tuple[float, float]) -> float:
    """진행 방향(라디안, 북 0, 동 +)."""
    north = (goal[0] - start[0]) * METRES_PER_DEG_LAT
    east = (goal[1] - start[1]) * METRES_PER_DEG_LON
    return math.atan2(east, north)


def _footprint(volume: Volume) -> str:
    lats = [p[0] for p in volume.polygon]
    lons = [p[1] for p in volume.polygon]
    if not lats:
        return "everywhere"
    return f"lat {min(lats):.5f}..{max(lats):.5f}, lon {min(lons):.5f}..{max(lons):.5f}"


# 모델이 고도 키를 잘못 적는 방식들. 실주행에서 4B 는 열 답 중 넷을 "alt_ma" 로 적었고(재시도에서도
# 같은 오타), 그 초안은 전부 버려졌습니다. 키 이름은 양식이지 규칙이 아니라 — 값은 같은 범위 검사와
# 같은 판정을 받습니다 — 읽어 줍니다.
ALTITUDE_KEYS = ("alt_m", "alt_ma", "altitude_m", "altitude", "alt")


def _altitude_field(leg: dict):
    for key in ALTITUDE_KEYS:
        if key in leg:
            return leg[key]
    raise KeyError("alt_m")


def is_building(volume: Volume) -> bool:
    return volume.id.startswith("bldg-") and volume.ceiling_m is not None


def needed_over(volume: Volume) -> int | None:
    """건물을 넘으려면 필요한 고도(m, 올림). 건물이 아니면 None."""
    if not is_building(volume):
        return None
    return math.ceil(volume.ceiling_m + volume.clearance_m + OVER_ROOF_MARGIN_M)


def breach_words(volume: Volume) -> str:
    """사전 판정에 걸린 것을 짧게. 화면 카드용입니다. 예: "crossed bldg-t02452, roof 114 m"."""
    if is_building(volume):
        return f"crossed {volume.id}, roof {volume.ceiling_m:.0f} m"
    if volume.rule == "ceiling" and volume.ceiling_m is not None:
        return f"above the {volume.ceiling_m:.0f} m cap in {volume.id}"
    return f"entered {volume.id}"


def describe(volume: Volume, allowed_m: float = ALT_MAX_M) -> str:
    """모델이 읽을 장애물 한 줄. 판정 근거가 아니라 지도 읽기입니다.

    allowed_m 은 그 자리에서 올라갈 수 있는 최대(천장 칸 아래면 120 보다 낮습니다). 필요한 고도가
    그보다 높으면 넘을 방법이 없으니 '돌아가라' 고 씁니다.
    """
    needed = needed_over(volume)
    if needed is not None:
        how = (f"cross it at {needed} m or higher" if needed <= allowed_m
               else f"would need {needed} m, above the {allowed_m:.0f} m limit — go around it")
        return (f"building {volume.id} ({volume.name}, roof {volume.ceiling_m:.0f} m; {how}), "
                f"footprint {_footprint(volume)}")
    if volume.rule == "forbidden":
        band = "at every altitude" if volume.ceiling_m is None else f"up to {volume.top_m:.0f} m"
        return (f"no-fly cell {volume.id} ({volume.name}, forbidden {band}, keep 40 m away, "
                f"go around it), footprint {_footprint(volume)}")
    if volume.rule == "ceiling" and volume.ceiling_m is not None:
        return (f"altitude cap {volume.name}: at most {volume.ceiling_m:.0f} m, "
                f"footprint {_footprint(volume)}")
    return f"{volume.name}, footprint {_footprint(volume)}"


class ModelDrafter:
    def __init__(self, llm: TieredLlm, planner, tier: LlmTier = LlmTier.NANO,
                 bbox: tuple[float, float, float, float] | None = None,
                 timeout_s: float | None = None, backoff_s: float | None = None):
        self.llm = llm
        self.planner = planner          # 운영사의 공역 사본과 직선 계획을 씁니다
        self.tier = tier
        self.bbox = bbox
        self.model = llm.model_for(tier)
        self.timeout_s = (float(os.getenv("DRAFT_TIMEOUT_S", str(DRAFT_TIMEOUT_S)))
                          if timeout_s is None else float(timeout_s))
        self.backoff_s = (float(os.getenv("DRAFT_BACKOFF_S", str(DRAFT_BACKOFF_S)))
                          if backoff_s is None else float(backoff_s))
        # 초안 호출이 잘린 뒤 이 시각까지는 묻지 않습니다. 신청서(6초) 호출이 잘린 것과는 별개입니다
        # — 같은 서버 상태를 공유했더니 바쁜 Ollama 에서 신청서가 한 번 잘릴 때마다 초안을 30초씩
        # 건너뛰어, 실주행에서 nano 초안이 한 건도 없었습니다.
        self.skip_until = 0.0
        self.last_attempts = 0
        self.last_failures: list[str] = []
        self.last_raised = 0            # 운영사의 고도 규칙이 올린 구간 수
        # 화면 카드(model_trace.route.draft)용: 마지막 초안이 무엇에 걸렸나, 모델이 쓴 시간(합).
        self.last_breach: str | None = None
        self.last_latency_ms = 0
        self._volumes_by_id: dict[str, Volume] = {}
        self._volumes_for = -1

    @property
    def enabled(self) -> bool:
        return bool(self.llm.enabled and self.model)

    @property
    def name(self) -> str:
        return f"{self.tier.value}:{self.model}"

    # ---------- 그리기 ----------

    def draft(self, start: tuple[float, float], goal: tuple[float, float],
              context: dict | None = None, deadline: float | None = None) -> list[dict] | None:
        """모델에게 두 번까지 묻고, 통과한 초안만 돌려줍니다. 못 하면 None — A* 차례입니다.

        deadline 은 monotonic 시각. 있으면 두 질문을 합쳐 그때까지만 묻습니다 — 부르는 쪽이 거절
        순간부터 초안 예산 하나(timeout_s)만 기다리고 A* 로 가므로, 그 뒤에 서버에 걸린 질문은
        답이 와도 쓸 데가 없습니다. 없으면 질문마다 예산 하나씩입니다.
        """
        self.last_attempts = 0
        self.last_failures = []
        self.last_raised = 0
        self.last_breach = None
        self.last_latency_ms = 0
        if not self.enabled:
            return None
        if time.monotonic() < self.skip_until:
            # 초안 호출이 방금 잘렸습니다. 또 물으면 또 기다릴 뿐이라 이번은 A* 차례입니다.
            self.last_failures.append("server unreachable a moment ago, skipped")
            return None
        airspace = self.planner.airspace
        if airspace.landing_breach(goal[0], goal[1]) is not None:
            return None      # 내려앉을 수 없는 자리. 어떤 초안도 소용없고 A* 도 같은 답입니다

        bbox = self.bbox or service_bbox([start, goal])
        brief = self._brief(start, goal, bbox, context)
        user = brief
        took_s = 0.0          # 직전 질문이 걸린 시간. 재시도는 그보다 짧게 끝날 리 없습니다
        for _ in range(MAX_ASKS):
            budget = self._budget(deadline)
            if budget < MIN_ASK_S:
                self.last_failures.append("draft budget exhausted, not asked")
                return None
            if deadline is not None and budget < took_s:
                # 첫 답이 남은 예산보다 오래 걸렸습니다. 같은 서버에 같은 크기의 질문을 또 걸면
                # 마감 뒤에 오는 답을 기다리는 것뿐입니다(실주행: 잘린 재시도 11건, 통과 0건).
                self.last_failures.append(
                    f"retry skipped: {budget:.0f} s left, the first ask took {took_s:.0f} s")
                return None
            self.last_attempts += 1
            reply = self.llm.ask(self.tier, SYSTEM, user, max_tokens=700, json_object=True,
                                 timeout_s=budget)
            if reply is None:
                self.last_failures.append("no reply")
                self.skip_until = time.monotonic() + self.backoff_s
                return None   # 서버가 없거나 느립니다. 또 물어봐야 또 기다립니다
            took_s = reply.latency_ms / 1000.0
            self.last_latency_ms += reply.latency_ms
            form = parse_json_object(reply.text)
            legs, problem = self.validate(form, start, goal, bbox)
            if legs is None:
                self.llm.discard(self.tier)
                self.last_failures.append(problem)
                self.last_breach = problem
                user = self._retry_brief(brief, reply.text[:1500], [problem])
                continue
            legs = self._apply_altitude_rule(legs)
            breaches = self.breaches_along(legs)
            if not breaches:
                return legs
            self.llm.discard(self.tier)
            self.last_breach = breach_words(breaches[0][1])
            lines = self.feedback_lines(legs, breaches)
            self.last_failures.append("; ".join(lines))
            user = self._retry_brief(brief, json.dumps({"legs": legs}), lines)
        return None

    def _budget(self, deadline: float | None) -> float:
        """이번 질문에 줄 시간. 마감이 있으면 남은 시간과 예산 중 작은 쪽."""
        if deadline is None:
            return self.timeout_s
        return min(self.timeout_s, deadline - time.monotonic())

    # ---------- 양식 검사 (코드가 합니다, 모델은 못 바꿉니다) ----------

    @staticmethod
    def validate(form, start: tuple[float, float], goal: tuple[float, float],
                 bbox: tuple[float, float, float, float]) -> tuple[list[dict] | None, str | None]:
        """양식, 개수, 상자, 고도, 양 끝, 길이. 하나라도 어기면 (None, 이유)."""
        if not isinstance(form, dict) or not isinstance(form.get("legs"), list):
            return None, "not a {\"legs\": [...]} object"
        raw = form["legs"]
        if len(raw) < 2:
            return None, f"{len(raw)} legs, need at least 2"
        if len(raw) > MAX_LEGS:
            return None, f"{len(raw)} legs, at most {MAX_LEGS}"
        legs = []
        for index, leg in enumerate(raw, start=1):
            if not isinstance(leg, dict):
                return None, f"leg {index} is not an object"
            try:
                lat, lon = float(leg["lat"]), float(leg["lon"])
                alt = float(_altitude_field(leg))
            except (KeyError, TypeError, ValueError):
                return None, f"leg {index} lacks numeric lat/lon/alt_m"
            if not all(math.isfinite(v) for v in (lat, lon, alt)):
                return None, f"leg {index} is not finite"
            if not ALT_MIN_M <= alt <= ALT_MAX_M:
                return None, f"leg {index} alt_m {alt:.0f} outside {ALT_MIN_M:.0f}..{ALT_MAX_M:.0f}"
            if not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                return None, f"leg {index} ({lat:.5f},{lon:.5f}) outside the service box"
            legs.append({"lat": round(lat, 6), "lon": round(lon, 6), "alt_m": round(alt, 1)})
        # 양 끝은 우리가 압니다. 모델이 조금 빗나가게 적어도 출발점과 목적지는 사실이 이깁니다.
        legs[0] = {**legs[0], "lat": round(start[0], 6), "lon": round(start[1], 6)}
        legs[-1] = {**legs[-1], "lat": round(goal[0], 6), "lon": round(goal[1], 6)}
        # 같은 자리를 두 번 적으면 길이 0 인 구간이 생깁니다. 판정은 되지만 뜻이 없습니다.
        distinct = [legs[0]]
        for leg in legs[1:]:
            if distance_m((distinct[-1]["lat"], distinct[-1]["lon"]),
                          (leg["lat"], leg["lon"])) >= 1.0:
                distinct.append(leg)
        if len(distinct) < 2:
            return None, "all legs at the same place"
        straight = distance_m(start, goal)
        total = sum(distance_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                    for a, b in zip(distinct, distinct[1:], strict=False))
        if total > MAX_STRETCH * straight + 50.0:
            return None, (f"route {total:.0f} m is longer than {MAX_STRETCH}x the straight "
                          f"{straight:.0f} m")
        return distinct, None

    def _apply_altitude_rule(self, legs: list[dict]) -> list[dict]:
        """모델이 적은 고도가 그 구간에서 안 되면 운영사의 규칙(가장 낮은 안전 고도)으로 바꿉니다.

        어디로 갈지는 모델이 그렸고, 얼마나 높이 갈지는 직선을 낼 때와 같은 규칙입니다
        (planner.straight 도 leg_altitude 로 고도를 정합니다). 그 구간을 어떤 고도로도 못 지나면
        모델의 값을 그대로 두고 판정이 이유를 말하게 합니다.
        """
        router = self.planner.router
        fixed = [dict(legs[0])]
        for here, nxt in zip(legs, legs[1:], strict=False):
            a, b = (here["lat"], here["lon"]), (nxt["lat"], nxt["lon"])
            segment = [{"lat": a[0], "lon": a[1], "alt_m": nxt["alt_m"]},
                       {"lat": b[0], "lon": b[1], "alt_m": nxt["alt_m"]}]
            altitude = nxt["alt_m"]
            floor = min(router.floor_alt_m, ALT_MAX_M)
            if altitude < floor:
                # 판정 자료에 없는 낮은 건물(20 m 미만) 위로도 50 m 가 남아야 합니다. 모델의 값이
                # 그보다 낮으면 규칙의 최저로 올립니다. 천장 칸이 그보다 낮으면 아래서 판정이
                # 말합니다.
                segment = [{**segment[0], "alt_m": floor}, {**segment[1], "alt_m": floor}]
                altitude = floor
                self.last_raised += 1
            if first_breach(self.planner.airspace, segment) is not None:
                safe = router.leg_altitude(a, b)
                if safe is not None and ALT_MIN_M <= safe <= ALT_MAX_M:
                    altitude = round(safe, 1)
                    self.last_raised += 1
            fixed.append({**nxt, "alt_m": altitude})
        return fixed

    # ---------- 모델에게 보여줄 것 ----------

    def _volume(self, volume_id: str | None) -> Volume | None:
        airspace = self.planner.airspace
        if self._volumes_for != airspace.revision:
            self._volumes_by_id = {v.id: v for v in airspace.all()}
            self._volumes_for = airspace.revision
        return self._volumes_by_id.get(volume_id or "")

    def breaches_along(self, legs: list[dict], limit: int = OBSTACLE_LIMIT) -> list:
        """경로가 차례로 부딪히는 것들 [(구간, 구역, 이유, 자리)].

        구간마다 부딪히는 것을 전부(진행 순서대로) 모읍니다(geo.leg_breaches). 예전에는 첫 번째를
        넘긴 자리부터 다시 물었는데, 짧은 구간 끝의 낮은 건물 하나 뒤에서 멈춰 그 뒤의 '돌아야
        하는' 건물이 목록에서 빠졌습니다. 같은 구역은 한 번만.
        """
        found = []
        seen: set[str] = set()
        for index in range(len(legs) - 1):
            for _fraction, volume, why, at in leg_breaches(self.planner.airspace, legs[index],
                                                           legs[index + 1]):
                if volume.id in seen:
                    continue
                seen.add(volume.id)
                found.append((index + 1, volume, why, at))
                if len(found) >= limit:
                    return found
        return found

    def obstacles(self, start: tuple[float, float], goal: tuple[float, float],
                  alt_m: float, limit: int = OBSTACLE_LIMIT) -> list:
        """직선이 차례로 부딪히는 것들 [(구간, 구역, 이유, 자리)]."""
        return self.breaches_along([{"lat": start[0], "lon": start[1], "alt_m": alt_m},
                                    {"lat": goal[0], "lon": goal[1], "alt_m": alt_m}], limit)

    def go_arounds(self, start: tuple[float, float], goal: tuple[float, float]) -> list:
        """직선 위에서 어떤 합법 고도로도 못 넘는 것들 — 돌아가야 하는 것 [(구역, 자리)].

        최대 고도(120m)로 직선을 재면 그보다 낮게 넘을 수 있는 건물은 안 걸리고, 옥상 + 50m 가
        120m 를 넘는 건물·모든 고도에서 금지인 칸·옆 이격만 남습니다. 천장 칸 아래에서는 그
        천장이 한계라, 그 칸을 지나는 동안은 천장 바로 아래 고도로 다시 잽니다 — 120m 스캔은
        칸 자체에 걸려 칸의 발자국을 통째로 건너뛰므로, 안에 있는 101m 건물(90m 칸에서 152m
        필요)이 빠졌고 모델은 그 건물을 좌우로 지그재그하며 관통했습니다.
        """
        found: dict[str, tuple[Volume, tuple[float, float]]] = {}
        self._collect_go_arounds(start, goal, ALT_MAX_M, found, depth=0)
        ordered = sorted(found.values(), key=lambda pair: distance_m(start, pair[1]))
        return ordered[:GO_AROUND_LIMIT]

    def _collect_go_arounds(self, cursor: tuple[float, float], goal: tuple[float, float],
                            alt_m: float, found: dict, depth: int) -> None:
        """cursor→goal 을 alt_m 로 재서 돌아가야 하는 것을 found 에 모읍니다.

        천장 칸에 걸리면 그 칸의 발자국 구간을 (천장 - 1m) 로 한 번 더 잽니다(재귀, 셋까지 —
        칸 안의 더 낮은 칸). 그 밖의 것은 must_go_around 로 거릅니다.
        """
        airspace = self.planner.airspace
        while len(found) < GO_AROUND_LIMIT and distance_m(cursor, goal) >= 1.0:
            legs = [{"lat": cursor[0], "lon": cursor[1], "alt_m": alt_m},
                    {"lat": goal[0], "lon": goal[1], "alt_m": alt_m}]
            breach = first_breach(airspace, legs)
            if breach is None:
                return
            _, volume, _, at = breach
            beyond = self._past(volume, cursor, goal, at)
            if volume.rule == "ceiling" and volume.ceiling_m is not None and depth < 3:
                cap = volume.ceiling_m - 1.0
                if cap < alt_m:
                    self._collect_go_arounds(at, beyond or goal, cap, found, depth + 1)
            elif volume.id not in found and self.must_go_around(volume, at):
                found[volume.id] = (volume, at)
            if beyond is None:
                return
            cursor = beyond

    def allowed_over(self, at: tuple[float, float]) -> float:
        """그 자리에서 올라갈 수 있는 최대 고도(천장 - 1m, 최대 120m)."""
        ceiling = self.planner.airspace.ceiling_at(at[0], at[1])
        return ALT_MAX_M if ceiling is None else min(ALT_MAX_M, ceiling - 1.0)

    def must_go_around(self, volume: Volume, at: tuple[float, float]) -> bool:
        """이 장애물을 그 자리에서 넘을 합법 고도가 없는가."""
        needed = needed_over(volume)
        if needed is None:
            return volume.rule == "forbidden"     # 금지 칸은 고도와 무관합니다
        return needed > self.allowed_over(at)

    @staticmethod
    def _past(volume: Volume, cursor, goal, at) -> tuple[float, float] | None:
        lats = [p[0] for p in volume.polygon] or [at[0]]
        lons = [p[1] for p in volume.polygon] or [at[1]]
        length = distance_m(cursor, goal)
        if length < 1.0:
            return None
        # 구역 경계 상자의 대각선만큼 앞으로 갑니다. 그 안에 있는 자국은 전부 지나칩니다.
        span = distance_m((min(lats), min(lons)), (max(lats), max(lons))) + 30.0
        fraction = min(1.0, (distance_m(cursor, at) + span) / length)
        if fraction >= 1.0:
            return None
        return (cursor[0] + (goal[0] - cursor[0]) * fraction,
                cursor[1] + (goal[1] - cursor[1]) * fraction)

    @staticmethod
    def _compass(north: float, east: float) -> str:
        names = ("north", "north-east", "east", "south-east", "south", "south-west", "west",
                 "north-west")
        return names[int((math.degrees(math.atan2(east, north)) % 360 + 22.5) // 45) % 8]

    def openings(self, at: tuple[float, float], heading_rad: float) -> dict[str, dict]:
        """장애물 자리에서 좌우로 얼마나 비키면 열리는지. 그 자리의 허용 고도로 잽니다.

        {"left": {"compass", "metres", "point"}, "right": {...}}. 막혀 있으면 metres 가 None.
        """
        airspace = self.planner.airspace
        router = self.planner.router
        found = {}
        for label, sign in (("left", 1.0), ("right", -1.0)):
            # 진행 방향(북 cos h, 동 sin h)의 왼쪽은 90도 반시계로 돌린 (북 sin h, 동 -cos h).
            # 처음에 부호를 거꾸로 적어 북쪽으로 가는 선의 '왼쪽'이 동쪽으로 나왔습니다.
            north = math.sin(heading_rad) * sign
            east = -math.cos(heading_rad) * sign
            opening = {"compass": self._compass(north, east), "metres": None, "point": None}
            for metres in SIDE_PROBES_M:
                point = _offset(at, north * metres, east * metres)
                if not airspace.too_close(point[0], point[1], router._altitude_at(*point)):
                    opening.update(metres=metres, point=point)
                    break
            found[label] = opening
        return found

    def _clear_sides(self, at: tuple[float, float], heading_rad: float) -> str:
        words = []
        for label, opening in self.openings(at, heading_rad).items():
            side = f"{label} ({opening['compass']})"
            if opening["metres"] is None:
                words.append(f"blocked for {SIDE_PROBES_M[-1]} m to the {side}")
            else:
                point = opening["point"]
                words.append(f"clear {opening['metres']} m to the {side} "
                             f"at {point[0]:.5f},{point[1]:.5f}")
        return "; ".join(words)

    @staticmethod
    def pick_side(openings: dict[str, dict], previous: str | None) -> str | None:
        """어느 쪽으로 돌지 하나만 고릅니다. 열린 쪽이 없으면 None.

        가까운 쪽이 기본이지만, 바로 앞 장애물에서 고른 쪽이 두 배 안쪽으로 열려 있으면 그쪽을
        유지합니다. 녹음된 초안에서 모델이 '왼쪽 80m 열림, 오른쪽 80m 열림' 을 번갈아 경유점으로
        삼아 건물 사이를 지그재그로 관통했습니다 — 좌우 정보를 둘 다 주면 둘 다 씁니다.
        """
        open_sides = {label: o for label, o in openings.items() if o["metres"] is not None}
        if not open_sides:
            return None
        nearest = min(open_sides, key=lambda label: open_sides[label]["metres"])
        if previous in open_sides and \
                open_sides[previous]["metres"] <= 2 * open_sides[nearest]["metres"]:
            return previous
        return nearest

    def side_advice(self, at: tuple[float, float], heading_rad: float,
                    previous: str | None = None) -> tuple[str, str | None]:
        """(모델에게 줄 문장, 고른 쪽). '남쪽으로 지나라, 예를 들어 이 점을 거쳐' 꼴입니다."""
        openings = self.openings(at, heading_rad)
        chosen = self.pick_side(openings, previous)
        if chosen is None:
            return (f"both sides blocked for {SIDE_PROBES_M[-1]} m — detour far earlier", None)
        opening = openings[chosen]
        point = opening["point"]
        words = (f"pass {opening['compass'].upper()} of it ({chosen}), e.g. via "
                 f"{point[0]:.5f},{point[1]:.5f} ({opening['metres']} m off the line)")
        other = "right" if chosen == "left" else "left"
        elsewhere = openings[other]
        if elsewhere["metres"] is not None:
            words += f"; {elsewhere['compass']} is also clear at {elsewhere['metres']} m"
        return words, chosen

    def _ceilings_along(self, start, goal, step_m: float = 100.0) -> list[str]:
        """직선 위에서 천장이 120m 아래로 내려가는 구간들. 그 위로 그리면 거절입니다."""
        airspace = self.planner.airspace
        length = distance_m(start, goal)
        samples = max(1, int(length / step_m))
        spans: list[list] = []      # [cap, from_km, to_km]
        for step in range(samples + 1):
            fraction = step / samples
            point = (start[0] + (goal[0] - start[0]) * fraction,
                     start[1] + (goal[1] - start[1]) * fraction)
            ceiling = airspace.ceiling_at(*point)
            cap = None if ceiling is None or ceiling >= ALT_MAX_M else round(ceiling - 1.0)
            km = fraction * length / 1000.0
            if cap is not None and spans and spans[-1][0] == cap and spans[-1][2] >= km - 0.15:
                spans[-1][2] = km
            elif cap is not None:
                spans.append([cap, km, km])
        return [f"altitude capped at {cap} m from {a:.1f} km to {b:.1f} km along the line"
                for cap, a, b in spans]

    def _brief(self, start, goal, bbox, context: dict | None) -> str:
        context = context or {}
        straight = self.planner.straight(start, goal)
        alt_m = float(straight[-1]["alt_m"]) if straight else ALT_MIN_M
        heading = _heading(start, goal)
        length = distance_m(start, goal)
        lines = [
            f"origin {start[0]:.5f},{start[1]:.5f} -> goal {goal[0]:.5f},{goal[1]:.5f} "
            f"(straight {length / 1000:.1f} km, heading {math.degrees(heading) % 360:.0f} deg).",
            f"service box lat {bbox[0]:.4f}..{bbox[2]:.4f}, lon {bbox[1]:.4f}..{bbox[3]:.4f}.",
        ]
        # 먼저, 어떤 고도로도 못 넘는 것 전부. 이건 높이로는 못 고치고 옆으로 비켜야 합니다.
        around = self.go_arounds(start, goal)
        if around:
            lines.append("GO AROUND — no legal altitude over these on the straight line "
                         "(roof + 50 m clearance is above the limit there, or forbidden at "
                         "every altitude); climbing cannot help, pass beside them. Pick ONE side "
                         "for a run of neighbouring obstacles and stay on it — never alternate "
                         "between left and right points, that crosses the obstacle:")
            side = None
            for volume, at in around:
                km = distance_m(start, at) / 1000.0
                advice, side = self.side_advice(at, heading, side)
                lines.append(f"- {self._go_around_words(volume, at)} at {km:.1f} km: {advice}")
        hits = self.obstacles(start, goal, alt_m)
        if hits:
            lines.append(f"The straight line at {alt_m:.0f} m is refused. Along it, in order:")
            for _, volume, _, at in hits:
                km = distance_m(start, at) / 1000.0
                # 천장 칸은 옆으로 비키는 게 아니라 낮게 지나면 됩니다. 좌우는 금지 구역만 잽니다.
                sides = f"; {self._clear_sides(at, heading)}" if volume.rule == "forbidden" else ""
                lines.append(f"- at {km:.1f} km: {describe(volume, self.allowed_over(at))}{sides}")
        lines += ["- " + line for line in self._ceilings_along(start, goal)]
        refused = self._volume(context.get("forbids"))
        reason = context.get("reason")
        if reason:
            listed = {volume.id for _, volume, _, _ in hits} | {volume.id for volume, _ in around}
            extra = "" if refused is None or refused.id in listed else f" ({describe(refused)})"
            lines.append(f"The runtime's refusal of the straight line: {reason}{extra}")
        lines.append("Draft a route that avoids all of that, with room to spare. JSON only.")
        return "\n".join(lines)

    def _go_around_words(self, volume: Volume, at: tuple[float, float]) -> str:
        needed = needed_over(volume)
        if needed is None:
            return f"no-fly cell {volume.id} ({volume.name}, forbidden at every altitude)"
        return (f"building {volume.id} ({volume.name}, roof {volume.ceiling_m:.0f} m, would need "
                f"{needed} m, limit there {self.allowed_over(at):.0f} m)")

    def feedback_lines(self, legs: list[dict], breaches: list) -> list[str]:
        """걸린 것마다 한 줄. 돌 쪽은 앞 줄과 같은 쪽을 유지합니다(pick_side)."""
        lines, side = [], None
        for breach in breaches:
            line, side = self.feedback_line(legs, breach, side)
            lines.append(line)
        return lines

    def feedback_line(self, legs: list[dict], breach,
                      previous_side: str | None = None) -> tuple[str, str | None]:
        """재시도 때 모델에게 줄, 걸린 것 하나에 대한 구체적인 한 줄과 고른 쪽.

        무엇에(id·이름) 어디서 걸렸고, 옥상이 몇 m 라 몇 m 가 필요했는지, 그게 그 자리의 한계를
        넘어 돌아가야 하는지, 어느 쪽이 열려 있는지. '고쳐라' 만 들으면 모델은 같은 선을 조금
        흔들어 다시 냅니다. 무엇을 어느 쪽으로 얼마나 옮겨야 하는지를 숫자로 들어야 합니다.
        """
        segment, volume, why, at = breach
        here, nxt = legs[segment - 1], legs[segment]
        heading = _heading((here["lat"], here["lon"]), (nxt["lat"], nxt["lon"]))
        flown = float(nxt["alt_m"])
        where = (f"leg {segment} ({here['lat']:.5f},{here['lon']:.5f} -> "
                 f"{nxt['lat']:.5f},{nxt['lon']:.5f} at {flown:.0f} m)")
        needed = needed_over(volume)
        allowed = self.allowed_over(at)
        if needed is None and volume.rule == "forbidden":
            fix = (f"no-fly cell {volume.id} ({volume.name}) is forbidden at every altitude, "
                   f"so you MUST go around it")
        elif needed is None:
            fix = f"{volume.name}: stay at or below the cap there"
        elif needed > allowed:
            fix = (f"building {volume.id} ({volume.name}): roof {volume.ceiling_m:.0f} m, you "
                   f"would need {needed} m over it, which is above the {allowed:.0f} m limit "
                   f"there, so you MUST fly around it, not over it")
        else:
            fix = (f"building {volume.id} ({volume.name}): roof {volume.ceiling_m:.0f} m, you "
                   f"need {needed} m or higher over it (you flew {flown:.0f} m); climb to "
                   f"{needed} m or fly around it")
        sides, side = "", previous_side
        if volume.rule == "forbidden":
            advice, side = self.side_advice(at, heading, previous_side)
            sides = f"; {advice}"
        return (f"{where} hits {fix} at {at[0]:.5f},{at[1]:.5f}{sides} (judge said: {why})",
                side)

    @staticmethod
    def _retry_brief(brief: str, previous: str, failures: list[str]) -> str:
        listed = "\n".join(f"- {line}" for line in failures)
        return (brief
                + "\n\nYour previous draft was refused by the operator's own pre-check:\n"
                + listed
                + f"\nPrevious draft: {previous}\n"
                "Where it says MUST go around, climbing cannot fix it: move those waypoints to "
                "the clear side named above, by at least that distance, and detour early. "
                "Elsewhere move the legs well away from what they hit (100 m or more). Keep the "
                "rest, and reply with the full JSON object again.")
