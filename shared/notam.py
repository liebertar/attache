"""A notice to airmen is a sentence. The runtime needs a volume with a clock on it.

Real airspace closures arrive as text in a fixed dialect — an FAA NOTAM reads
"AREA BOUNDED BY 404310N0735920W ... SFC-400FT AGL 0907-0912Z". Most of that dialect is
regular enough for a grammar, and a grammar is deterministic: the same sentence always
gives the same polygon, and a sentence the grammar cannot read is refused rather than
guessed at. Prose the grammar cannot read is handed to a model to compile into the same
schema, and what the model produces never applies until a person has confirmed it.
"""

import math
import re
from dataclasses import dataclass, field

from shared.geo import (
    DEFAULT_CEILING_M,
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
)

FEET_TO_M = 0.3048
NM_TO_M = 1852.0
# 반지름 공지는 다각형으로 옮깁니다. 판정은 다각형만 알고, 16각형이면 반지름 오차가 2% 입니다.
RADIUS_SIDES = 16

# 모델이 구조화한 공지에 거는 검사. 문법으로 읽은 공지에는 안 겁니다 — 규제기관이 큰 구역을
# 닫으면 그건 닫힌 것입니다. 모델이 지어낸 것은 이 상자 안, 이 넓이 안이어야 하고 사람이 봅니다.
MIN_VERTICES = 3
MAX_VERTICES = 32
MAX_AREA_M2 = 4_000_000.0          # 4 km²
MAX_FLOOR_M = DEFAULT_CEILING_M    # 바닥이 Part 107 상한보다 높으면 드론과 무관한 공지입니다
MAX_CEILING_M = 1524.0             # 5,000 ft

COORD = re.compile(r"(\d{6}(?:\.\d+)?)([NS])(\d{7}(?:\.\d+)?)([EW])")
VERTICAL = re.compile(
    r"\b(SFC|GND|(\d+)\s*FT)\s*-\s*(UNL|(\d+)\s*FT)\s*(AGL|AMSL|MSL)?\b", re.IGNORECASE
)
WINDOW_ZULU = re.compile(r"\b(\d{4})Z?\s*-\s*(\d{4})Z?\b")
WINDOW_TICK = re.compile(r"\bTICKS?\s+(\d+)\s*-\s*(\d+)\b", re.IGNORECASE)
RADIUS = re.compile(r"\b(\d+(?:\.\d+)?)\s*NM\s+RADIUS\s+OF\s+" + COORD.pattern, re.IGNORECASE)
BOUNDED = re.compile(r"\bAREA\s+BOUNDED\s+BY\b", re.IGNORECASE)


@dataclass
class Clock:
    """틱과 Zulu 시각 사이. 판마다 틱 0 이 어느 시각인지 정해져 있어야 창을 옮길 수 있습니다."""

    epoch_z: str = "0900"
    seconds_per_tick: float = 0.8

    def tick_of(self, hhmm: str) -> int:
        minutes = _minutes(hhmm) - _minutes(self.epoch_z)
        if minutes < 0:
            minutes += 24 * 60      # 자정을 넘긴 창
        return int(round(minutes * 60.0 / self.seconds_per_tick))

    def zulu_of(self, tick: int) -> str:
        minutes = _minutes(self.epoch_z) + int(round(tick * self.seconds_per_tick / 60.0))
        minutes %= 24 * 60
        return f"{minutes // 60:02d}{minutes % 60:02d}"


def _minutes(hhmm: str) -> int:
    if not re.fullmatch(r"\d{4}", hhmm) or int(hhmm[:2]) > 23 or int(hhmm[2:]) > 59:
        raise ValueError(f"시각이 HHMM 이 아닙니다: {hhmm!r}")
    return int(hhmm[:2]) * 60 + int(hhmm[2:])


@dataclass
class Notice:
    """문법이 읽어 낸 것. Volume 하나와 시간 창입니다."""

    polygon: list[tuple[float, float]]
    floor_m: float = 0.0
    ceiling_m: float | None = None
    reference: str = "AGL"
    from_tick: int | None = None
    until_tick: int | None = None
    name: str = ""
    text: str = ""
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "polygon": [[lat, lon] for lat, lon in self.polygon],
            "floor_m": self.floor_m, "ceiling_m": self.ceiling_m, "reference": self.reference,
            "from_tick": self.from_tick, "until_tick": self.until_tick,
            "name": self.name, "text": self.text,
        }


def parse_dms(token: str) -> tuple[float, float]:
    """DDMMSS[NS]DDDMMSS[EW] → (lat, lon). 초는 소수도 받습니다."""
    match = COORD.fullmatch(token.strip())
    if match is None:
        raise ValueError(f"좌표가 아닙니다: {token!r}")
    lat = _dms(match.group(1), 2) * (1 if match.group(2) == "N" else -1)
    lon = _dms(match.group(3), 3) * (1 if match.group(4) == "E" else -1)
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        raise ValueError(f"지구 위 좌표가 아닙니다: {token!r}")
    return lat, lon


def _dms(digits: str, degree_width: int) -> float:
    degrees = int(digits[:degree_width])
    minutes = int(digits[degree_width:degree_width + 2])
    seconds = float(digits[degree_width + 2:])
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"분·초가 60 을 넘습니다: {digits!r}")
    return degrees + minutes / 60.0 + seconds / 3600.0


def format_dms(lat: float, lon: float) -> str:
    """(lat, lon) → DDMMSS[NS]DDDMMSS[EW]. 초 단위로 반올림합니다 — 공지의 해상도가 그렇습니다."""

    def part(value: float, width: int, positive: str, negative: str) -> str:
        hemisphere = positive if value >= 0 else negative
        total = int(round(abs(value) * 3600.0))
        degrees, rest = divmod(total, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{degrees:0{width}d}{minutes:02d}{seconds:02d}{hemisphere}"

    return part(lat, 2, "N", "S") + part(lon, 3, "E", "W")


def parse_notice(text: str, clock: Clock | None = None) -> Notice | None:
    """문법으로 읽습니다. 못 읽으면 None — 추측하지 않습니다.

    읽는 것: 'AREA BOUNDED BY <좌표>...' 또는 '<r>NM RADIUS OF <좌표>', 'SFC-400FT AGL' 같은
    고도 띠, '0907-0912Z' 같은 Zulu 창(시계로 틱으로) 또는 'TICK 525-900'. 앞에 '이름:' 이 오면
    이름입니다. 그 밖의 문장은 이 함수의 몫이 아닙니다.
    """
    if not text or not text.strip():
        return None
    clock = clock or Clock()
    body = " ".join(text.split())
    name = ""
    if ":" in body and not COORD.search(body.split(":", 1)[0]):
        head, body = body.split(":", 1)
        name, body = head.strip(), body.strip()

    polygon = _shape(body)
    if polygon is None:
        return None
    floor_m, ceiling_m, reference = _vertical(body)
    from_tick, until_tick = _window(body, clock)
    problems = shape_problems(polygon)
    if problems:
        return None
    return Notice(polygon=polygon, floor_m=floor_m, ceiling_m=ceiling_m, reference=reference,
                  from_tick=from_tick, until_tick=until_tick, name=name, text=text.strip())


def _shape(body: str) -> list[tuple[float, float]] | None:
    radius = RADIUS.search(body)
    if radius is not None:
        centre = parse_dms(radius.group(0).split()[-1])
        return circle(centre, float(radius.group(1)) * NM_TO_M)
    if BOUNDED.search(body) is None:
        return None
    tail = body[BOUNDED.search(body).end():]
    points = [parse_dms(m.group(0)) for m in COORD.finditer(tail)]
    return points if len(points) >= MIN_VERTICES else None


def _vertical(body: str) -> tuple[float, float | None, str]:
    match = VERTICAL.search(body)
    if match is None:
        return 0.0, None, "AGL"
    floor_m = 0.0 if match.group(2) is None else float(match.group(2)) * FEET_TO_M
    ceiling_m = None if match.group(4) is None else float(match.group(4)) * FEET_TO_M
    reference = (match.group(5) or "AGL").upper().replace("MSL", "AMSL").replace("AAMSL", "AMSL")
    return floor_m, ceiling_m, reference


def _window(body: str, clock: Clock) -> tuple[int | None, int | None]:
    ticks = WINDOW_TICK.search(body)
    if ticks is not None:
        return int(ticks.group(1)), int(ticks.group(2))
    zulu = WINDOW_ZULU.search(body)
    if zulu is None:
        return None, None
    return clock.tick_of(zulu.group(1)), clock.tick_of(zulu.group(2))


def circle(centre: tuple[float, float], radius_m: float,
           sides: int = RADIUS_SIDES) -> list[tuple[float, float]]:
    return [
        (centre[0] + radius_m * math.cos(2 * math.pi * k / sides) / METRES_PER_DEG_LAT,
         centre[1] + radius_m * math.sin(2 * math.pi * k / sides) / METRES_PER_DEG_LON)
        for k in range(sides)
    ]


def area_m2(polygon: list[tuple[float, float]]) -> float:
    """신발끈 공식. 구역이 작아서 평면으로 봐도 됩니다."""
    total = 0.0
    for index, (lat_a, lon_a) in enumerate(polygon):
        lat_b, lon_b = polygon[(index + 1) % len(polygon)]
        total += (lon_a * METRES_PER_DEG_LON) * (lat_b * METRES_PER_DEG_LAT) \
            - (lon_b * METRES_PER_DEG_LON) * (lat_a * METRES_PER_DEG_LAT)
    return abs(total) / 2.0


def shape_problems(polygon) -> list[str]:
    problems = []
    if not isinstance(polygon, list) or not (MIN_VERTICES <= len(polygon) <= MAX_VERTICES):
        return [f"꼭짓점이 {MIN_VERTICES}~{MAX_VERTICES}개가 아님"]
    for point in polygon:
        try:
            lat, lon = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            return ["꼭짓점이 (lat, lon) 이 아님"]
        if not (math.isfinite(lat) and math.isfinite(lon)
                and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            problems.append("지구 위 좌표가 아닌 꼭짓점")
            break
    return problems


def validate(notice: Notice, bbox: tuple[float, float, float, float] | None) -> list[str]:
    """모델이 구조화한 공지에 거는 검사 전부. 하나라도 걸리면 적용도 보류도 안 합니다.

    bbox 는 (lat_min, lon_min, lat_max, lon_max) — 서비스 영역. 모델이 그린 다각형은 이 안에
    있어야 합니다. 없으면(착륙장 목록이 아직 없으면) 상자 검사는 못 하고 나머지만 봅니다.
    """
    problems = shape_problems(notice.polygon)
    if problems:
        return problems
    if bbox is not None:
        lat_min, lon_min, lat_max, lon_max = bbox
        if any(not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max)
               for lat, lon in notice.polygon):
            problems.append("서비스 영역 밖 꼭짓점")
    area = area_m2(notice.polygon)
    if area > MAX_AREA_M2:
        problems.append(f"넓이 {area / 1e6:.1f} km² > {MAX_AREA_M2 / 1e6:.0f} km²")
    if not (0.0 <= notice.floor_m <= MAX_FLOOR_M):
        problems.append(f"바닥 {notice.floor_m:.0f}m 가 0~{MAX_FLOOR_M:.1f}m 밖")
    if notice.ceiling_m is not None and not (notice.floor_m <= notice.ceiling_m <= MAX_CEILING_M):
        problems.append(f"천장 {notice.ceiling_m:.0f}m 가 바닥~{MAX_CEILING_M:.0f}m 밖")
    if (notice.from_tick is not None and notice.until_tick is not None
            and notice.until_tick <= notice.from_tick):
        problems.append("끝나는 틱이 시작 틱보다 앞")
    return problems


# ---------- 모델이 읽는 쪽 ----------

COMPILE_SYSTEM = (
    "You turn one airspace notice written in prose into a structured restriction. You do not "
    "decide whether it applies: a person will confirm it. Reply with one JSON object and "
    'nothing else: {"name": "<short name>", "polygon": [[lat, lon], ...] (3 to 32 vertices, '
    'decimal degrees, 5 decimals), "floor_m": <metres AGL, 0 for the surface>, '
    '"ceiling_m": <metres AGL or null for unlimited>, "from": "HHMM" Zulu or null, '
    '"until": "HHMM" Zulu or null}. If the notice gives a radius, draw a polygon of 16 '
    "vertices around the centre. If it does not say, floor is 0 and ceiling is null. Never "
    "invent coordinates that are not in the notice."
)


def from_model_form(form: dict, clock: Clock, text: str) -> Notice | None:
    """모델의 JSON 을 같은 Notice 로. 양식이 아니면 None. 검사(validate)는 따로 겁니다."""
    if not isinstance(form, dict) or not isinstance(form.get("polygon"), list):
        return None
    try:
        polygon = [(float(p[0]), float(p[1])) for p in form["polygon"]]
        floor_m = float(form.get("floor_m") or 0.0)
        ceiling_m = None if form.get("ceiling_m") is None else float(form["ceiling_m"])
        from_tick = _tick_field(form.get("from"), clock)
        until_tick = _tick_field(form.get("until"), clock)
    except (TypeError, ValueError, IndexError):
        return None
    # 이름은 화면(승인 카드·배너)에 그대로 오릅니다. 모델이 지은 문자열이라 표시 문자는 여기서
    # 뺍니다 — 화면도 이스케이프하지만, 어느 화면에 오르든 모델 출력이 마크업이 돼서는 안 됩니다.
    name = "".join(ch for ch in str(form.get("name") or "") if ch not in "<>&\"'`")
    return Notice(polygon=polygon, floor_m=floor_m, ceiling_m=ceiling_m, reference="AGL",
                  from_tick=from_tick, until_tick=until_tick,
                  name=" ".join(name.split())[:80], text=text)


def _tick_field(value, clock: Clock) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return clock.tick_of(str(value).strip().rstrip("Zz"))
