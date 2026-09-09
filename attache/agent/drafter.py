"""The operator lets a small model sketch the route. The runtime still decides.

This is the strongest claim the project makes: who draws the line changes nothing about
the guarantee. A route drafted by Nemotron goes through exactly the same judge as one
from A*, and gets refused for exactly the same reasons. So the model may be wrong, slow,
or absent, and nothing it produces is executed until `first_breach` and the runtime have
said yes.

What the model gets: origin, goal, the rules in one paragraph, and a reading of the map —
what the straight line hits and at what distance, which side of each obstacle is clear,
where the ceiling drops, and the reason the last attempt was refused. What it returns: a
list of legs, or nothing. What this file does with it: schema check, bounds check, snap
the ends, length check, the operator's own altitude rule per leg (the same one the
straight line uses), then the operator's own pre-judgement with the same `first_breach`
the runtime uses. Fails once → one more ask with every breach listed. Fails twice → the
caller draws with A*.
"""

import json
import math
import os
import time

from attache.core.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, Volume, first_breach
from attache.llm.client import LlmTier, TieredLlm, parse_json_object

MAX_LEGS = 12
ALT_MIN_M = 40.0
ALT_MAX_M = 120.0
# 직선의 이만큼까지만 우회로로 봅니다. 그보다 길면 모델이 헤맨 것이고, A* 가 더 잘 그립니다.
MAX_STRETCH = 2.5
# 착륙장 모음의 경계 상자에서 이만큼(약 2km) 바깥까지가 서비스 영역입니다.
BBOX_MARGIN_DEG = 0.02
# 직선 위에서 모델에게 알려줄 장애물 개수. 첫 번째만 알려주면 그 뒤에 있는 것에 또 걸립니다.
OBSTACLE_LIMIT = 5
# 장애물 옆으로 얼마나 비켜야 열리는지 재 보는 거리(m). 첫 번째 열린 자리를 알려줍니다.
SIDE_PROBES_M = (80, 150, 250, 400, 600, 900, 1300, 2000, 3000)
MAX_ASKS = 2
# 초안 한 번의 예산. 신청서(수백 토큰, 6초)와 다릅니다 — 지도 읽기 1,200 토큰을 넣고 7구간을 받는 데
# 한가한 Ollama 에서도 8.7~8.9초, 녹음된 통과 답은 5~19초였습니다. 기체 공통 타임아웃(6초)으로는
# 라이브에서 초안 15건이 전부 잘려 nano 가 그린 경로가 하나도 없었습니다. 기체는 지상에서
# 기다리는 중이라 이 시간은 화면에서 '거절 뒤 다시 그리는 중' 으로 보입니다.
DRAFT_TIMEOUT_S = 30.0
# 서버가 방금 답을 못 줬으면 이만큼은 초안을 묻지 않고 A* 로 갑니다. 타임아웃 뒤에 5.6초를
# 기다렸다 같은 서버에 또 30초를 걸면, 끝나지 못할 호출 뒤에서 기체가 그만큼 더 섭니다.
DRAFT_BACKOFF_S = 30.0

SYSTEM = (
    "You draft a flight route for one uncrewed delivery aircraft. You do not fly it and you "
    "do not approve it: a runtime judges every route against the rules below and refuses "
    "anything that breaks them, so draw carefully rather than optimistically. "
    "Reply with one JSON object and nothing else: "
    '{"legs":[{"lat":<deg>,"lon":<deg>,"alt_m":<m>}, ...]}. '
    "A leg is a waypoint; alt_m is the cruise altitude of the segment that ends at that "
    "waypoint. Rules: cruise between 40 and 120 m AGL. To cross a building you must be 50 m "
    "above its roof; if roof + 50 m is above 120 m you must go around it. Stay 10 m laterally "
    "clear of buildings 40 m or taller and 40 m clear of no-fly cells (they are forbidden at "
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


def _footprint(volume: Volume) -> str:
    lats = [p[0] for p in volume.polygon]
    lons = [p[1] for p in volume.polygon]
    if not lats:
        return "everywhere"
    return f"lat {min(lats):.5f}..{max(lats):.5f}, lon {min(lons):.5f}..{max(lons):.5f}"


def describe(volume: Volume) -> str:
    """모델이 읽을 장애물 한 줄. 판정 근거가 아니라 지도 읽기입니다."""
    if volume.id.startswith("bldg-") and volume.ceiling_m is not None:
        needed = volume.ceiling_m + volume.clearance_m + 1.0
        how = (f"cross it at {needed:.0f} m or higher" if needed <= ALT_MAX_M
               else "too tall to cross, go around it")
        return (f"building {volume.name} (roof {volume.ceiling_m:.0f} m; {how}), "
                f"footprint {_footprint(volume)}")
    if volume.rule == "forbidden":
        band = "at every altitude" if volume.ceiling_m is None else f"up to {volume.top_m:.0f} m"
        return (f"no-fly cell {volume.name} (forbidden {band}, keep 40 m away), "
                f"footprint {_footprint(volume)}")
    if volume.rule == "ceiling" and volume.ceiling_m is not None:
        return (f"altitude cap {volume.name}: at most {volume.ceiling_m:.0f} m, "
                f"footprint {_footprint(volume)}")
    return f"{volume.name}, footprint {_footprint(volume)}"


def _failure_line(breach) -> str:
    segment, volume, why, at = breach
    return (f"leg {segment} hits {describe(volume)} at {at[0]:.5f},{at[1]:.5f} "
            f"(judge said: {why})")


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
        # 초안 호출이 잘린 뒤 이 시각까지는 묻지 않습니다. 신청서(6초) 호출이 잘린 것과는 별개입니다 —
        # 같은 서버 상태를 공유했더니 바쁜 Ollama 에서 신청서가 한 번 잘릴 때마다 초안을 30초씩
        # 건너뛰어, 실주행에서 nano 초안이 한 건도 없었습니다.
        self.skip_until = 0.0
        self.last_attempts = 0
        self.last_failures: list[str] = []
        self.last_raised = 0            # 운영사의 고도 규칙이 올린 구간 수
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
              context: dict | None = None) -> list[dict] | None:
        """모델에게 두 번까지 묻고, 통과한 초안만 돌려줍니다. 못 하면 None — A* 차례입니다."""
        self.last_attempts = 0
        self.last_failures = []
        self.last_raised = 0
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
        for _ in range(MAX_ASKS):
            self.last_attempts += 1
            reply = self.llm.ask(self.tier, SYSTEM, user, max_tokens=700, json_object=True,
                                 timeout_s=self.timeout_s)
            if reply is None:
                self.last_failures.append("no reply")
                self.skip_until = time.monotonic() + self.backoff_s
                return None   # 서버가 없거나 느립니다. 또 물어봐야 또 기다립니다
            form = parse_json_object(reply.text)
            legs, problem = self.validate(form, start, goal, bbox)
            if legs is None:
                self.llm.discard(self.tier)
                self.last_failures.append(problem)
                user = self._retry_brief(brief, reply.text[:1500], [problem])
                continue
            legs = self._apply_altitude_rule(legs)
            breaches = self.breaches_along(legs)
            if not breaches:
                return legs
            self.llm.discard(self.tier)
            lines = [_failure_line(breach) for breach in breaches]
            self.last_failures.append("; ".join(lines))
            user = self._retry_brief(brief, json.dumps({"legs": legs}), lines)
        return None

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
                lat, lon, alt = float(leg["lat"]), float(leg["lon"]), float(leg["alt_m"])
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
        """경로가 차례로 부딪히는 것들. 판정 함수로 읽되, 하나 넘기고 그다음을 또 봅니다.

        first_breach 는 첫 번째만 답합니다. 재시도 때 첫 번째만 알려주면 모델은 그것만 고치고
        두 번째에 또 걸립니다. 걸린 구역의 발자국을 지나친 자리부터 다시 물어 목록을 만듭니다.
        """
        found = []
        seen: set[str] = set()
        remaining = [dict(leg) for leg in legs]
        offset = 0     # remaining[0] 이 원래 몇 번째 구간의 출발점인지
        while remaining and len(remaining) >= 2 and len(found) < limit:
            breach = first_breach(self.planner.airspace, remaining)
            if breach is None:
                break
            segment, volume, why, at = breach
            if volume.id not in seen:
                seen.add(volume.id)
                found.append((segment + offset, volume, why, at))
            here, nxt = remaining[segment - 1], remaining[segment]
            beyond = self._past(volume, (here["lat"], here["lon"]), (nxt["lat"], nxt["lon"]), at)
            if beyond is None:
                # 이 구간의 끝까지 지나쳤습니다. 다음 구간부터 봅니다.
                remaining = remaining[segment:]
                offset += segment
                continue
            remaining = ([{"lat": beyond[0], "lon": beyond[1], "alt_m": nxt["alt_m"]}]
                         + remaining[segment:])
            offset += segment - 1
        return found

    def obstacles(self, start: tuple[float, float], goal: tuple[float, float],
                  alt_m: float) -> list:
        """직선이 차례로 부딪히는 것들 [(구간, 구역, 이유, 자리)]."""
        return self.breaches_along([{"lat": start[0], "lon": start[1], "alt_m": alt_m},
                                    {"lat": goal[0], "lon": goal[1], "alt_m": alt_m}])

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

    def _clear_sides(self, at: tuple[float, float], heading_rad: float) -> str:
        """장애물 자리에서 좌우로 얼마나 비키면 열리는지. 그 자리의 허용 고도로 잽니다."""
        airspace = self.planner.airspace
        router = self.planner.router
        words = []
        for label, sign in (("left", 1.0), ("right", -1.0)):
            # 진행 방향(북 cos h, 동 sin h)의 왼쪽은 90도 반시계로 돌린 (북 sin h, 동 -cos h).
            # 처음에 부호를 거꾸로 적어 북쪽으로 가는 선의 '왼쪽'이 동쪽으로 나왔습니다.
            north = math.sin(heading_rad) * sign
            east = -math.cos(heading_rad) * sign
            label = f"{label} ({self._compass(north, east)})"
            for metres in SIDE_PROBES_M:
                point = _offset(at, north * metres, east * metres)
                if not airspace.too_close(point[0], point[1], router._altitude_at(*point)):
                    words.append(f"clear {metres} m to the {label} "
                                 f"at {point[0]:.5f},{point[1]:.5f}")
                    break
            else:
                words.append(f"blocked for {SIDE_PROBES_M[-1]} m to the {label}")
        return "; ".join(words)

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
        north = (goal[0] - start[0]) * METRES_PER_DEG_LAT
        east = (goal[1] - start[1]) * METRES_PER_DEG_LON
        heading = math.atan2(east, north)
        length = distance_m(start, goal)
        lines = [
            f"origin {start[0]:.5f},{start[1]:.5f} -> goal {goal[0]:.5f},{goal[1]:.5f} "
            f"(straight {length / 1000:.1f} km, heading {math.degrees(heading) % 360:.0f} deg).",
            f"service box lat {bbox[0]:.4f}..{bbox[2]:.4f}, lon {bbox[1]:.4f}..{bbox[3]:.4f}.",
        ]
        hits = self.obstacles(start, goal, alt_m)
        if hits:
            lines.append(f"The straight line at {alt_m:.0f} m is refused. Along it, in order:")
            for _, volume, _, at in hits:
                km = distance_m(start, at) / 1000.0
                # 천장 칸은 옆으로 비키는 게 아니라 낮게 지나면 됩니다. 좌우는 금지 구역만 잽니다.
                sides = f"; {self._clear_sides(at, heading)}" if volume.rule == "forbidden" else ""
                lines.append(f"- at {km:.1f} km: {describe(volume)}{sides}")
        lines += ["- " + line for line in self._ceilings_along(start, goal)]
        refused = self._volume(context.get("forbids"))
        reason = context.get("reason")
        if reason:
            listed = {volume.id for _, volume, _, _ in hits}
            extra = "" if refused is None or refused.id in listed else f" ({describe(refused)})"
            lines.append(f"The runtime's refusal of the straight line: {reason}{extra}")
        lines.append("Draft a route that avoids all of that, with room to spare. JSON only.")
        return "\n".join(lines)

    @staticmethod
    def _retry_brief(brief: str, previous: str, failures: list[str]) -> str:
        listed = "\n".join(f"- {line}" for line in failures)
        return (brief
                + "\n\nYour previous draft was refused by the operator's own pre-check:\n"
                + listed
                + f"\nPrevious draft: {previous}\n"
                "Move those legs well away from what they hit (100 m or more), keep the rest, "
                "and reply with the full JSON object again.")
