"""Information the tower takes in: a weather report, an incident, a restriction — as text.

What arrives is a sentence (a METAR-like line from the simulator, a search snippet, a line a
person typed). The runtime needs numbers with units and a place on the map. A grammar reads
the regular dialects deterministically: the same sentence always gives the same numbers, and
a sentence it cannot read is refused rather than guessed at. Prose the grammar cannot read
goes to a model that fills the same schema; code validates that answer against the ranges
below and the gazetteer, and what a model read is held for a person before it applies.
"""

import datetime
import re
from dataclasses import dataclass, field

from holdshort.core.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON
from holdshort.core.notam import (
    COORD,
    Clock,
    Notice,
    _window,
    circle,
    from_model_form,
    parse_dms,
)

KT_TO_MPS = 0.514444
MPH_TO_MPS = 0.44704
KMH_TO_MPS = 1.0 / 3.6
SM_TO_M = 1609.34

# 사고 반경. 모델이 지어낸 값은 이 안이어야 하고, 문장에 없으면 기본값입니다.
DEFAULT_INCIDENT_RADIUS_M = 150.0
MIN_INCIDENT_RADIUS_M = 50.0
MAX_INCIDENT_RADIUS_M = 500.0
# 모델이 구조화한 날씨에 거는 범위. 허리케인도 60 m/s 안이고, 시정 50 km 는 맑은 날의 끝입니다.
MAX_WIND_MPS = 60.0
MAX_GUST_MPS = 80.0
MAX_VISIBILITY_M = 50_000.0

INCIDENT_KINDS = ("fire", "collapse", "explosion", "police", "gas_leak")


class UnknownPlace(ValueError):
    """모델이 말한 자리가 지명 사전에 없습니다. 양식은 맞았지만 그 곳은 없는 곳입니다."""

# ---------- 날씨 문법 ----------
# "WIND 240 AT 18 GUST 28 KT" (풀어 쓴 METAR), "24018G28KT" (METAR),
# "winds 25 mph gusting to 40 mph" (예보 산문). 단위가 있어야 숫자입니다 — 단위 없는 숫자는
# 바람이 아닙니다.
WIND_SPELLED = re.compile(
    r"\bWIND\s+(?:\d{3}|VRB)\s+AT\s+(\d{1,3})(?:\s*(KTS?|KNOTS?|MPS|M/S))?"
    r"(?:\s+GUST(?:S|ING)?\s+(?:TO\s+)?(\d{1,3})(?:\s*(KTS?|KNOTS?|MPS|M/S))?)?\b",
    re.IGNORECASE)
WIND_METAR = re.compile(r"\b(?:\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b")
WIND_PROSE = re.compile(
    r"\bwinds?\s+(?:of\s+|at\s+|around\s+|near\s+|up\s+to\s+)?(\d{1,3})\s*"
    r"(kts?|knots?|mph|m/s|mps|km/h)\b", re.IGNORECASE)
GUST_PROSE = re.compile(
    r"\bgust(?:s|ing)?\s+(?:to|of|up\s+to|near|around|as\s+high\s+as)?\s*(\d{1,3})\s*"
    r"(kts?|knots?|mph|m/s|mps|km/h)\b", re.IGNORECASE)
VIS_SPELLED = re.compile(
    r"\bVIS(?:IBILITY)?\s+(?:OF\s+|AROUND\s+|BELOW\s+|UNDER\s+)?(\d+(?:/\d+)?(?:\.\d+)?)\s*"
    r"(SM|KM|MI|MILES?|M|METRES?|METERS?)\b", re.IGNORECASE)
# METAR 의 4자리 시정(m)은 바람 그룹 뒤에 홀로 섭니다. 뒤에 "-" 나 "Z" 가 붙으면 그것은 시각 창
# ("KT 0930-0940Z")이지 시정이 아닙니다 — 그렇게 읽으면 없는 시정 위반으로 이륙이 섭니다.
VIS_METAR = re.compile(r"\b(\d+(?:/\d+)?)SM\b|(?:KT|MPS)\s+(\d{4})(?=\s|$)")
PRECIP_WORDS = re.compile(
    r"(?<![A-Z])([+-]?(?:SH|TS|FZ)?(?:RA|SN|DZ|GR|GS|PL|SG|FG|BR|TS))(?![A-Z])"
    r"|\b(rain|snow|thunderstorms?|fog|sleet|hail|drizzle|blizzard)\b", re.IGNORECASE)


def _mps(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in ("kt", "kts", "knot", "knots"):
        return value * KT_TO_MPS
    if unit == "mph":
        return value * MPH_TO_MPS
    if unit == "km/h":
        return value * KMH_TO_MPS
    return value          # mps, m/s


def _metres(value: str, unit: str) -> float:
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        number = float(numerator) / float(denominator)
    else:
        number = float(value)
    unit = unit.lower()
    if unit in ("sm", "mi", "mile", "miles"):
        return number * SM_TO_M
    if unit == "km":
        return number * 1000.0
    return number


@dataclass
class WeatherReport:
    """문법(또는 모델)이 읽어 낸 날씨. 전부 m/s 와 m 입니다."""

    wind_mps: float | None = None
    gust_mps: float | None = None
    visibility_m: float | None = None
    precipitation: str | None = None
    from_tick: int | None = None
    until_tick: int | None = None
    text: str = ""

    def to_dict(self) -> dict:
        return {"wind_mps": _rounded(self.wind_mps), "gust_mps": _rounded(self.gust_mps),
                "visibility_m": _rounded(self.visibility_m, 0),
                "precipitation": self.precipitation,
                "from_tick": self.from_tick, "until_tick": self.until_tick, "text": self.text}


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def parse_weather(text: str, clock: Clock | None = None) -> WeatherReport | None:
    """날씨 문장을 숫자로. 바람이나 시정 중 하나는 읽혀야 보고서입니다. 못 읽으면 None."""
    if not text or not text.strip():
        return None
    clock = clock or Clock()
    body = " ".join(text.split())
    wind = gust = visibility = None
    spelled = WIND_SPELLED.search(body)
    metar = WIND_METAR.search(body)
    # 단위는 바람 뒤("AT 10 M/S GUST 13")나 돌풍 뒤("AT 18 GUST 28 KT") 어느 쪽에 와도 됩니다.
    if spelled is not None and not (spelled.group(2) or spelled.group(4)):
        spelled = None
    if spelled is not None:
        unit = spelled.group(4) or spelled.group(2)
        wind = _mps(float(spelled.group(1)), spelled.group(2) or unit)
        gust = _mps(float(spelled.group(3)), unit) if spelled.group(3) else None
    elif metar is not None:
        wind = _mps(float(metar.group(1)), metar.group(3))
        gust = _mps(float(metar.group(2)), metar.group(3)) if metar.group(2) else None
    else:
        prose = WIND_PROSE.search(body)
        if prose is not None:
            wind = _mps(float(prose.group(1)), prose.group(2))
        gusting = GUST_PROSE.search(body)
        if gusting is not None:
            gust = _mps(float(gusting.group(1)), gusting.group(2))
    seen = VIS_SPELLED.search(body)
    if seen is not None:
        visibility = _metres(seen.group(1), seen.group(2))
    else:
        short = VIS_METAR.search(body)
        if short is not None:
            visibility = (_metres(short.group(1), "SM") if short.group(1)
                          else float(short.group(2)))
    if wind is None and gust is None and visibility is None:
        return None
    precipitation = None
    found = PRECIP_WORDS.search(body)
    if found is not None:
        precipitation = (found.group(1) or found.group(2)).upper()
    from_tick, until_tick = _window(body, clock)
    return WeatherReport(wind_mps=wind, gust_mps=gust, visibility_m=visibility,
                         precipitation=precipitation, from_tick=from_tick, until_tick=until_tick,
                         text=text.strip())


# ---------- 사고 문법 ----------
INCIDENT_WORDS = re.compile(
    r"\b(fire|blaze|collapse[d]?|explosion|explod\w*|police|gas\s+leak)\b", re.IGNORECASE)
INCIDENT_NAMES = {"blaze": "fire", "collapsed": "collapse", "explod": "explosion",
                  "gas leak": "gas_leak"}
# 주소는 "AT <번지> <거리>" 로 옵니다. 그 뒤의 마침표·쉼표·반경·시각 앞에서 끊습니다.
ADDRESS_PHRASE = re.compile(
    r"\b(?:AT|ON)\s+(\d{1,5}[A-Za-z]?\s+[A-Za-z0-9.'\- ]+?)"
    r"(?=\s*[.,;:()]|\s+(?:RADIUS|KEEP|CLEAR|WITHIN|TICKS?|FROM|UNTIL|IN\s+MANHATTAN|MANHATTAN|"
    r"NEW\s+YORK|NYC|\d{4}Z)\b|\s*$)", re.IGNORECASE)
BUILDING_ID = re.compile(r"\b(bldg-t\d{3,7})\b")
RADIUS_PHRASE = re.compile(
    r"\b(\d{2,4})\s*(?:M|METRES?|METERS?)\s+RADIUS\b|\bRADIUS\s+(?:OF\s+)?(\d{2,4})\s*"
    r"(?:M|METRES?|METERS?)\b|\bWITHIN\s+(\d{2,4})\s*(?:M|METRES?|METERS?)\b", re.IGNORECASE)

# 주소 정규화. "250 W 47th St" 와 "250 West 47th Street" 는 같은 곳입니다.
ABBREVIATIONS = {
    "st": "street", "ave": "avenue", "av": "avenue", "blvd": "boulevard", "rd": "road",
    "pl": "place", "sq": "square", "dr": "drive", "ln": "lane", "pkwy": "parkway",
    "ct": "court", "ter": "terrace", "w": "west", "e": "east", "n": "north", "s": "south",
}


def normalise_address(label: str) -> str:
    words = re.sub(r"[.,;:#]", " ", str(label or "")).lower().split()
    return " ".join(ABBREVIATIONS.get(word, word) for word in words)


@dataclass
class Gazetteer:
    """주소 → 좌표, 건물 id → 중심. 모델이 말한 곳은 여기 있어야 곳입니다."""

    addresses: list[dict] = field(default_factory=list)
    buildings: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # 건물을 따로 싣지 않고 공역(런타임이 시뮬레이터에서 받은 3만 동)에 물을 때.
    # id → 다각형 또는 None.
    lookup: object = None

    def __post_init__(self):
        self._by_label = {normalise_address(a.get("label", "")): a for a in self.addresses
                          if a.get("lat") is not None and a.get("lon") is not None}

    def address(self, label: str) -> dict | None:
        key = normalise_address(label)
        found = self._by_label.get(key)
        if found is None:
            # 뒤에 붙는 동네 이름은 주소가 아닙니다.
            for tail in (" manhattan", " new york", " ny", " nyc"):
                if key.endswith(tail):
                    found = self._by_label.get(key[: -len(tail)].strip())
                    break
        return found

    def building(self, building_id: str) -> tuple[float, float] | None:
        polygon = self.buildings.get(building_id)
        if not polygon and self.lookup is not None:
            polygon = self.lookup(building_id)
        if not polygon:
            return None
        count = len(polygon)
        return (sum(p[0] for p in polygon) / count, sum(p[1] for p in polygon) / count)

    def add_building(self, building_id: str, polygon: list[tuple[float, float]]) -> None:
        self.buildings[building_id] = list(polygon)


@dataclass
class IncidentReport:
    """읽어 낸 사고 하나: 무엇이, 어디에(중심), 얼마나 넓게, 언제까지."""

    kind: str
    centre: tuple[float, float]
    radius_m: float = DEFAULT_INCIDENT_RADIUS_M
    place: str = ""                 # 사람이 읽는 자리 이름(주소 또는 건물 이름)
    building_id: str | None = None
    from_tick: int | None = None
    until_tick: int | None = None
    text: str = ""

    @property
    def name(self) -> str:
        """화면·거절 사유에 오르는 이름. "FIRE · 250 West 47th Street"."""
        return f"{self.kind.replace('_', ' ').upper()} · {self.place}"

    def polygon(self) -> list[tuple[float, float]]:
        return circle(self.centre, self.radius_m)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "centre": [round(self.centre[0], 6), round(self.centre[1], 6)],
                "radius_m": round(self.radius_m, 1), "place": self.place,
                "building_id": self.building_id, "from_tick": self.from_tick,
                "until_tick": self.until_tick, "text": self.text}


def _incident_kind(word: str) -> str:
    lowered = " ".join(word.lower().split())
    for prefix, name in INCIDENT_NAMES.items():
        if lowered.startswith(prefix):
            return name
    return lowered


def parse_incident(text: str, gazetteer: Gazetteer, clock: Clock | None = None,
                   hints: dict | None = None) -> IncidentReport | None:
    """사고 문장을 자리로. 종류 + (주소 또는 건물 id)가 있어야 하고, 그 자리가 지명 사전에 있어야
    합니다. hints 는 문장과 같이 온 구조화 값(address·building_id·radius_m) — 문장에 없을 때만
    봅니다.
    """
    if not text or not text.strip():
        return None
    clock = clock or Clock()
    hints = hints or {}
    body = " ".join(text.split())
    word = INCIDENT_WORDS.search(body)
    if word is None:
        return None
    kind = _incident_kind(word.group(1))
    centre, place, building_id = None, "", None
    building = BUILDING_ID.search(body)
    building_key = building.group(1) if building else hints.get("building_id")
    if building_key:
        centre = gazetteer.building(str(building_key))
        if centre is not None:
            building_id, place = str(building_key), str(hints.get("name") or building_key)
    if centre is None:
        phrase = ADDRESS_PHRASE.search(body)
        candidates = [phrase.group(1)] if phrase else []
        if hints.get("address"):
            candidates.append(str(hints["address"]))
        for candidate in candidates:
            found = gazetteer.address(candidate)
            if found is not None:
                centre, place = (float(found["lat"]), float(found["lon"])), found["label"]
                break
    if centre is None:
        return None
    radius = _radius_from(body)
    if radius is None:
        radius = hint_number(hints.get("radius_m"), DEFAULT_INCIDENT_RADIUS_M)
    from_tick, until_tick = _window(body, clock)
    return IncidentReport(kind=kind, centre=centre, radius_m=radius, place=place,
                          building_id=building_id, from_tick=from_tick, until_tick=until_tick,
                          text=text.strip())


def _radius_from(body: str) -> float | None:
    match = RADIUS_PHRASE.search(body)
    if match is None:
        return None
    return float(next(g for g in match.groups() if g))


def hint_number(value, default: float) -> float:
    """문장과 같이 온 구조화 값 하나. 수가 아니면 기본값 — 힌트 하나가 항목을 깨뜨리지 않습니다."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if number != number else number


# ---------- 검사 ----------

def weather_problems(report: WeatherReport) -> list[str]:
    """날씨 보고서에 거는 범위 검사. 누가 읽었든 겁니다 — 문법이 "WIND 240 AT 900 KT" 를 읽어 냈다면
    그것은 관측이 아니라 오독이고, 오독으로 이륙을 세우면 안 됩니다."""
    problems = []
    if report.wind_mps is None and report.gust_mps is None and report.visibility_m is None:
        problems.append("바람도 시정도 없음")
    if report.wind_mps is not None and not (0.0 <= report.wind_mps <= MAX_WIND_MPS):
        problems.append(f"바람 {report.wind_mps:.0f} m/s 가 0~{MAX_WIND_MPS:.0f} 밖")
    if report.gust_mps is not None and not (0.0 <= report.gust_mps <= MAX_GUST_MPS):
        problems.append(f"돌풍 {report.gust_mps:.0f} m/s 가 0~{MAX_GUST_MPS:.0f} 밖")
    if (report.wind_mps is not None and report.gust_mps is not None
            and report.gust_mps < report.wind_mps):
        problems.append("돌풍이 바람보다 약함")
    if report.visibility_m is not None and not (0.0 <= report.visibility_m <= MAX_VISIBILITY_M):
        problems.append(f"시정 {report.visibility_m:.0f} m 가 0~{MAX_VISIBILITY_M:.0f} 밖")
    problems += _window_problems(report.from_tick, report.until_tick)
    return problems


def incident_problems(report: IncidentReport,
                      bbox: tuple[float, float, float, float] | None) -> list[str]:
    """사고에 거는 범위 검사. 자리는 지명 사전이 이미 보증했고, 여기서는 종류·반경·상자·창만.
    문법이 읽은 것에도 겁니다 — "KEEP CLEAR 9999 M RADIUS" 는 맨해튼을 닫는 문장입니다."""
    problems = []
    if report.kind not in INCIDENT_KINDS:
        problems.append(f"모르는 사고 종류 {report.kind!r}")
    if not (MIN_INCIDENT_RADIUS_M <= report.radius_m <= MAX_INCIDENT_RADIUS_M):
        problems.append(f"반경 {report.radius_m:.0f} m 가 {MIN_INCIDENT_RADIUS_M:.0f}~"
                        f"{MAX_INCIDENT_RADIUS_M:.0f} 밖")
    if bbox is not None:
        lat_min, lon_min, lat_max, lon_max = bbox
        lat, lon = report.centre
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            problems.append("서비스 영역 밖 자리")
    problems += _window_problems(report.from_tick, report.until_tick)
    return problems


def _window_problems(from_tick: int | None, until_tick: int | None) -> list[str]:
    if from_tick is not None and until_tick is not None and until_tick <= from_tick:
        return ["끝나는 틱이 시작 틱보다 앞"]
    return []


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    north = (b[0] - a[0]) * METRES_PER_DEG_LAT
    east = (b[1] - a[1]) * METRES_PER_DEG_LON
    return (north * north + east * east) ** 0.5


# ---------- 모델이 읽는 쪽 ----------

INTAKE_SYSTEM = (
    "You read one piece of text gathered for a drone tower in New York City: a weather "
    "report or forecast, an incident (fire, collapse, explosion, police activity, gas leak) at "
    "an address, or an airspace restriction. You do not decide anything: code checks your "
    "numbers and a person confirms before anything applies. Reply with one JSON object and "
    'nothing else. If the text is none of these, reply {"kind": "none"}. For weather: '
    '{"kind": "weather", "wind_mps": <number or null>, "gust_mps": <number or null>, '
    '"visibility_m": <metres or null>, "precipitation": "<word or null>", "from": "HHMM" Zulu '
    'or null, "until": "HHMM" Zulu or null}. For an incident: {"kind": "incident", "incident": '
    '"fire"|"collapse"|"explosion"|"police"|"gas_leak", "address": "<street address exactly as '
    'written, e.g. 250 West 47th Street>" or null, "building_id": "bldg-t12345" or null, '
    '"radius_m": <50..500 or null>, "from": "HHMM" or null, "until": "HHMM" or null}. For a '
    'restriction: {"kind": "notice", "name": "<short name>", "polygon": [[lat, lon], ...] (3 to '
    '32 vertices), "floor_m": <metres AGL>, "ceiling_m": <metres AGL or null>, "from": "HHMM" '
    'or null, "until": "HHMM" or null}. Convert knots and mph to m/s and miles to metres. '
    "Never invent an address, a number or a coordinate that is not in the text."
)


@dataclass
class Compiled:
    """모델의 답을 코드가 읽은 결과. kind 는 weather | incident | notice | none."""

    kind: str
    weather: WeatherReport | None = None
    incident: IncidentReport | None = None
    notice: Notice | None = None


def from_intake_form(form: dict, gazetteer: Gazetteer, clock: Clock, text: str,
                     hints: dict | None = None) -> Compiled | None:
    """모델의 JSON 을 같은 보고서 양식으로. 양식이 아니면 None. 범위 검사는 따로 겁니다."""
    if not isinstance(form, dict):
        return None
    kind = str(form.get("kind") or "").strip().lower()
    try:
        if kind == "none":
            return Compiled("none")
        if kind == "weather":
            return Compiled("weather", weather=WeatherReport(
                wind_mps=_number(form.get("wind_mps")), gust_mps=_number(form.get("gust_mps")),
                visibility_m=_number(form.get("visibility_m")),
                precipitation=_clean(form.get("precipitation")) or None,
                from_tick=_tick(form.get("from"), clock),
                until_tick=_tick(form.get("until"), clock),
                text=text))
        if kind == "incident":
            report = _incident_from(form, gazetteer, clock, text, hints or {})
            return None if report is None else Compiled("incident", incident=report)
        if kind == "notice":
            notice = from_model_form(form, clock, text)
            return None if notice is None else Compiled("notice", notice=notice)
    except UnknownPlace:
        raise
    except (TypeError, ValueError):
        return None
    return None


def _incident_from(form: dict, gazetteer: Gazetteer, clock: Clock, text: str,
                   hints: dict) -> IncidentReport | None:
    kind = _clean(form.get("incident")).lower().replace(" ", "_")
    centre, place, building_id = None, "", None
    if form.get("building_id"):
        building_id = _clean(form.get("building_id"))
        centre = gazetteer.building(building_id)
        place = str(hints.get("name") or building_id)
        if centre is None:
            raise UnknownPlace(f"모르는 건물 {building_id}")
    elif form.get("address"):
        found = gazetteer.address(_clean(form.get("address")))
        if found is None:
            raise UnknownPlace(f"지명 사전에 없는 주소 {_clean(form.get('address'))!r}")
        centre, place = (float(found["lat"]), float(found["lon"])), found["label"]
    if centre is None:
        return None
    radius = _number(form.get("radius_m"))
    return IncidentReport(kind=kind, centre=centre,
                          radius_m=DEFAULT_INCIDENT_RADIUS_M if radius is None else radius,
                          place=place, building_id=building_id,
                          from_tick=_tick(form.get("from"), clock),
                          until_tick=_tick(form.get("until"), clock), text=text)


def _number(value) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("유한한 수가 아님")
    return number


def _tick(value, clock: Clock) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return clock.tick_of(str(value).strip().rstrip("Zz"))


def _clean(value) -> str:
    """모델이 지은 문자열에서 표시 문자를 뗍니다 — 화면에 오르는 값입니다."""
    text = "".join(ch for ch in str(value or "") if ch not in "<>&\"'`")
    return " ".join(text.split())[:120]


# ---------- 사전 브리핑: 그날 그 자리의 위험을 읽는 문법 ----------
# 관제탑이 Tavily 로 받아 오는 쪽(공식 공지·기사)에서 읽어 내는 것은 넷입니다: 타워크레인(높이 +
# 주소), 행사(장소 + 시간 창), 공원 폐쇄(착륙장 이름 + 시간 창), 비행 제한(반경 + 중심 + 시간 창).
# 다섯째(기상 주의보)는 규칙이 되지 않고 정보로만 실립니다 — 이륙 정지는 관제탑 피드(METAR)의
# 일입니다. 문법이 못 읽는 산문은 Super 가 같은 양식을 채우고, 어느 쪽이 읽었든 코드가 범위를
# 검사합니다. 여기서 나오는 것은 판정이 아니라 '읽은 것' 입니다.

MIN_CRANE_M = 10.0
MAX_CRANE_M = 400.0            # 세계에서 가장 높은 타워크레인도 이 안입니다
MIN_BRIEF_RADIUS_M = 50.0
MAX_BRIEF_RADIUS_M = 5000.0    # 이보다 큰 원은 서비스 영역 전체라 코드가 그리지 않습니다
DEFAULT_EVENT_RADIUS_M = 300.0
FT_TO_M = 0.3048
NM_TO_M_BRIEF = 1852.0
SM_TO_M_BRIEF = 1609.34
# 화면·원장에 남기는 근거 문장 길이.
EVIDENCE_CHARS = 240

BRIEFING_KINDS = ("crane", "event", "closure", "restriction", "weather", "none")
# 이 말들이 하나도 없으면 모델에게 물을 것도 없습니다 — 기단과 무관한 글입니다.
BRIEFING_WORDS = re.compile(
    r"\b(crane|closed|closure|closing|parade|marathon|race|festival|street\s+fair|rally|march|"
    r"protest|concert|fireworks|motorcade|game|ceremony|vigil|tfr|flight\s+restriction|notam|"
    r"no[- ]fly|drone|uas|advisory|warning|watch|wind|storm|gust|assembly|summit)\b", re.IGNORECASE)

CRANE_WORDS = re.compile(r"\b(tower\s+crane|crawler\s+crane|mobile\s+crane|crane)\b", re.IGNORECASE)
CLOSURE_WORDS = re.compile(
    r"\b(closed|closure[s]?|will\s+close|is\s+closing|shut(?:\s+down)?|off[- ]limits|"
    r"no\s+public\s+access|not\s+accessible)\b", re.IGNORECASE)
EVENT_WORDS = re.compile(
    r"\b(parade|marathon|half[- ]marathon|race|concert|festival|street\s+fair|rally|"
    r"march(?:ers?|es)?|demonstration|protest(?:ers?)?|fireworks|motorcade|ball\s*game|game|"
    r"ceremony|vigil|block\s+party|procession|gathering)\b", re.IGNORECASE)
RESTRICTION_WORDS = re.compile(
    r"\b(tfr|temporary\s+flight\s+restriction[s]?|flight\s+restriction[s]?|notam|"
    r"no[- ]fly\s+zone|no[- ]drone\s+zone|uas\s+operations?\s+(?:are|is)\s+prohibited|"
    r"vip\s+movement)\b", re.IGNORECASE)
ADVISORY_WORDS = re.compile(
    r"\b(wind\s+advisory|high\s+wind\s+(?:warning|watch)|severe\s+thunderstorm\s+(?:warning|watch)"
    r"|special\s+weather\s+statement|dense\s+fog\s+advisory|gale\s+warning|"
    r"tornado\s+(?:warning|watch)|winter\s+storm\s+(?:warning|watch)|flood\s+warning)\b",
    re.IGNORECASE)

# 주소는 문장 아무 데나 있습니다("A tower crane at 2701 Broadway will…"). 지명 사전이 아는
# 것만 자리가 됩니다 — 읽어 낸 문자열이 아니라 사전이 준 좌표가 규칙의 중심입니다.
STREET_TAIL = (r"(?:Street|St|Avenue|Ave|Av|Boulevard|Blvd|Parkway|Pkwy|Place|Pl|Drive|Dr|"
               r"Road|Rd|Plaza|Square|Sq|Lane|Ln|Terrace|Ter|Court|Ct|Broadway|Bowery)")
ADDRESS_ANY = re.compile(
    r"\b(\d{1,5}[A-Za-z]?(?:-\d{1,3})?\s+(?:[A-Za-z0-9.'’]+\s+){0,3}" + STREET_TAIL + r")\b\.?",
    re.IGNORECASE)
HEIGHT_LED = re.compile(
    r"(?:height|tall|high|reach(?:es|ing)?|rises?(?:\s+to)?|up\s+to|maximum\s+height\s+of|"
    r"height\s+of|elevation\s+of|top\s+(?:out|height)\s*(?:at|of)?)[^.\d]{0,30}"
    r"(\d{1,4}(?:,\d{3})?(?:\.\d+)?)\s*(feet|foot|ft|meters|metres|m)\b", re.IGNORECASE)
HEIGHT_TRAILING = re.compile(
    r"\b(\d{1,4}(?:,\d{3})?(?:\.\d+)?)\s*(feet|foot|ft|meters|metres|m)\b[\s-]*"
    r"(?:tall|high|in\s+height|above\s+(?:grade|street\s+level|ground))", re.IGNORECASE)
RADIUS_UNITS = (r"(nautical\s+miles?|nmr?|statute\s+miles?|miles?|mi|feet|ft|meters|metres|m|"
                r"kilomet(?:er|re)s?|km)")
RADIUS_PATTERNS = (
    re.compile(r"(\d+(?:\.\d+)?)\s*" + RADIUS_UNITS + r"[\s-]*radius", re.IGNORECASE),
    re.compile(r"radius\s*[:\-]?\s*(?:of\s+)?(\d+(?:\.\d+)?)\s*" + RADIUS_UNITS,
               re.IGNORECASE),
    re.compile(r"within\s+(?:an?\s+)?(\d+(?:\.\d+)?)\s*" + RADIUS_UNITS + r"\s+(?:radius\s+)?of",
               re.IGNORECASE),
    re.compile(r"\b(\d+(?:\.\d+)?)\s*(nmr)\b", re.IGNORECASE),
)
CEILING_FT = re.compile(
    r"(?:up\s+to\s+(?:and\s+including\s+)?|below\s+|surface\s+to\s+|sfc\s*[-–]\s*)"
    r"(\d{2,5})\s*(?:ft|feet)\b", re.IGNORECASE)
# 좌표. FAA 본문의 DDMMSS(기존 COORD), 도·분·초 기호, 십진수 세 가지가 실제로 옵니다.
COORD_DMS = re.compile(
    r"(\d{1,3})\s*[°:]\s*(\d{1,2})\s*['′:]\s*(\d{1,2}(?:\.\d+)?)\s*[\"″]?\s*([NSEW])",
    re.IGNORECASE)
COORD_DMS_SPACED = re.compile(
    r"\b(\d{2,3})\s+(\d{1,2})\s+(\d{1,2}(?:\.\d+)?)\s*([NSEW])\b")
COORD_DECIMAL = re.compile(
    r"lat(?:itude)?[:\s]+(-?\d{1,3}\.\d{3,7})[,;\s]+lon(?:gitude)?[:\s]+(-?\d{1,3}\.\d{3,7})",
    re.IGNORECASE)
COORD_PAIR = re.compile(r"\(?\s*(-?\d{2}\.\d{3,7})\s*,\s*(-?\d{2,3}\.\d{3,7})\s*\)?")

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "nov": 11, "dec": 12}
MONTH_NAME = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
              r"aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE_SPAN = re.compile(
    MONTH_NAME + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*(?:–|—|-|to|through|thru|until)\s*"
    r"(?:" + MONTH_NAME + r"\.?\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", re.IGNORECASE)
DATE_ONE = re.compile(
    MONTH_NAME + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", re.IGNORECASE)
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")
TIME_ZULU = re.compile(r"\b(\d{1,2}):?(\d{2})\s*(?:z|utc|zulu)\b", re.IGNORECASE)
TIME_LOCAL = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.IGNORECASE)
OPEN_ENDED = re.compile(r"\buntil\s+further\s+notice\b", re.IGNORECASE)
SENTENCES = re.compile(
    r"(?<=[.!?])(?<!\bSt\.)(?<!\bAve\.)(?<!\bDr\.)(?<!\bMt\.)(?<!\bJr\.)(?<!\bNo\.)"
    r"(?<!\ba\.m\.)(?<!\bp\.m\.)\s+|\n+")


class UnknownWindow(ValueError):
    """시간 창이 없거나 말이 안 됩니다. 창이 없는 규칙은 영원한 규칙이라 걸 수 없습니다."""


# ---------- 뉴욕 지방시 ----------

def eastern_offset_hours(when) -> int:
    """그날 뉴욕의 UTC 차(-4 여름, -5 겨울). tzdata 가 없는 이미지에서도 돌아야 해서 규칙으로.

    미국 동부: 3월 둘째 일요일 02:00 지방시부터 11월 첫째 일요일 02:00 까지가 여름시각입니다.
    """
    day = when.date() if isinstance(when, datetime.datetime) else when
    march = datetime.date(day.year, 3, 8)
    start = march + datetime.timedelta(days=(6 - march.weekday()) % 7)      # 둘째 일요일
    november = datetime.date(day.year, 11, 1)
    end = november + datetime.timedelta(days=(6 - november.weekday()) % 7)  # 첫째 일요일
    return -4 if start <= day < end else -5


def eastern_to_utc(day: datetime.date, hour: int, minute: int) -> datetime.datetime:
    naive = datetime.datetime(day.year, day.month, day.day, hour % 24, minute)
    return (naive - datetime.timedelta(hours=eastern_offset_hours(day))).replace(
        tzinfo=datetime.UTC)


def utc_to_eastern(when: datetime.datetime) -> datetime.datetime:
    return when + datetime.timedelta(hours=eastern_offset_hours(when))


def _iso(when: datetime.datetime | None) -> str | None:
    return None if when is None else when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(value) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=datetime.UTC)


# ---------- 시간 창 ----------

@dataclass
class Window:
    """언제부터 언제까지. UTC 입니다. end 가 없으면 '해제 전까지'."""

    start: datetime.datetime | None = None
    end: datetime.datetime | None = None
    daily: bool = False          # 날짜 범위 + 하루 시간대(공사 시간 등)
    text: str = ""

    def to_dict(self) -> dict:
        return {"start": _iso(self.start), "end": _iso(self.end), "daily": self.daily,
                "text": self.text}


def _month_of(word: str) -> int:
    return MONTHS[word[:3].lower()]


def _dates_in(text: str, year: int) -> list[datetime.date]:
    """본문이 말한 날짜들, 나온 순서대로. 연도가 없으면 브리핑하는 해로 읽습니다."""
    found: list[datetime.date] = []
    span = DATE_SPAN.search(text)
    if span is not None:
        first_month, first_day, second_month, second_day, span_year = span.groups()
        chosen_year = int(span_year) if span_year else year
        try:
            found.append(datetime.date(chosen_year, _month_of(first_month), int(first_day)))
            found.append(datetime.date(chosen_year,
                                       _month_of(second_month or first_month), int(second_day)))
        except ValueError:
            found = []
        if found:
            return found
    for match in DATE_ONE.finditer(text):
        month, day, given = match.groups()
        try:
            found.append(datetime.date(int(given) if given else year, _month_of(month), int(day)))
        except ValueError:
            continue
    for match in DATE_ISO.finditer(text):
        try:
            found.append(datetime.date(*(int(part) for part in match.groups())))
        except ValueError:
            continue
    for match in DATE_US.finditer(text):
        month, day, given = match.groups()
        stated = int(given)
        try:
            found.append(datetime.date(stated + 2000 if stated < 100 else stated,
                                       int(month), int(day)))
        except ValueError:
            continue
    return found


def _times_in(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(Zulu 시각들, 지방시 시각들). 둘 다 나온 순서대로."""
    zulu = [(int(match.group(1)) % 24, int(match.group(2)))
            for match in TIME_ZULU.finditer(text)]
    local = []
    for match in TIME_LOCAL.finditer(text):
        hour = int(match.group(1)) % 12
        if match.group(3).lower().startswith("p"):
            hour += 12
        local.append((hour, int(match.group(2) or 0)))
    if re.search(r"\bnoon\b", text, re.IGNORECASE):
        local.append((12, 0))
    return zulu, local


def read_window(text: str, day: datetime.date) -> Window | None:
    """본문의 시간 창을 UTC 로. 못 읽으면 None — 창 없는 규칙은 만들지 않습니다.

    FAA 쪽처럼 '날짜 + HHMM UTC' 가 짝으로 오면 그 사이가 통째로 창입니다. 날짜 범위와 하루
    시간대가 따로 오면(공사 7 AM~6 PM, 9월 14일부터 11월 30일까지) 브리핑하는 날의 그 시간대가
    창입니다. 날짜만 있으면 그 날 하루, 시간만 있으면 오늘 그 시간입니다.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return None
    stamps = _utc_stamps(body, day.year)
    if len(stamps) >= 2:
        return Window(stamps[0], stamps[1], text=body[:EVIDENCE_CHARS])
    dates = _dates_in(body, day.year)
    zulu, local = _times_in(body)
    open_ended = OPEN_ENDED.search(body) is not None
    if not dates and not zulu and not local and not open_ended:
        return None
    first, last = (dates[0], dates[-1]) if dates else (day, day)
    if len(dates) == 1:
        last = first
    inside = first <= day <= last
    base = day if inside else first
    if len(zulu) >= 2:
        start = datetime.datetime(base.year, base.month, base.day, *zulu[0],
                                  tzinfo=datetime.UTC)
        end = datetime.datetime(base.year, base.month, base.day, *zulu[1],
                                tzinfo=datetime.UTC)
    elif len(local) >= 2:
        start = eastern_to_utc(base, *local[0])
        end = eastern_to_utc(base, *local[1])
    elif open_ended and dates:
        return Window(eastern_to_utc(first, 0, 0), None, text=body[:EVIDENCE_CHARS])
    elif dates:
        return Window(eastern_to_utc(first, 0, 0), eastern_to_utc(last, 23, 59),
                      daily=last > first, text=body[:EVIDENCE_CHARS])
    elif open_ended:
        return Window(eastern_to_utc(day, 0, 0), None, text=body[:EVIDENCE_CHARS])
    else:
        return None
    if end <= start:
        end += datetime.timedelta(days=1)      # 자정을 넘긴 창
    return Window(start, end, daily=bool(dates) and last > first, text=body[:EVIDENCE_CHARS])


def _utc_stamps(body: str, year: int) -> list[datetime.datetime]:
    """'<날짜> … HHMM UTC' 짝들. FAA 의 시작·종료 줄이 이 모양으로 옵니다."""
    pattern = re.compile(
        r"(" + MONTH_NAME + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*\d{4})?|\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}/\d{1,2}/\d{2,4})[^,;.\n]{0,24}?\b(\d{1,2}:?\d{2})\s*(?:z|utc|zulu)\b",
        re.IGNORECASE)
    stamps = []
    for match in pattern.finditer(body):
        dates = _dates_in(match.group(1), year)
        digits = match.group(3).replace(":", "")
        if not dates or len(digits) < 3:
            continue
        hour, minute = int(digits[:-2]), int(digits[-2:])
        if hour > 23 or minute > 59:
            continue
        stamps.append(datetime.datetime(dates[0].year, dates[0].month, dates[0].day, hour,
                                        minute, tzinfo=datetime.UTC))
    return stamps


# ---------- 읽은 것 ----------

@dataclass
class Hazard:
    """브리핑이 읽어 낸 위험 하나. 자리는 지명 사전·착륙장 목록이 준 좌표입니다."""

    kind: str
    place: str = ""
    centre: tuple[float, float] | None = None
    radius_m: float | None = None
    height_m: float | None = None
    ceiling_m: float | None = None
    landing_area: str | None = None       # 폐쇄된 착륙장 id
    address: str = ""                     # 지명 사전이 준 표준 주소
    window: Window | None = None
    detail: str = ""                      # 한 줄 요약. 코드가 짓습니다
    evidence: str = ""                    # 근거 문장
    note: str = ""                        # 코드가 덧붙이는 말(그리지 못한 큰 링 등)
    numbers: dict = field(default_factory=dict)   # 기상 주의보의 숫자

    @property
    def rule_kind(self) -> str | None:
        """이 위험이 만드는 규칙의 종류. 없으면 정보일 뿐입니다."""
        return self.kind if self.kind in ("crane", "event", "closure", "restriction") else None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "place": self.place,
                "centre": None if self.centre is None else [round(self.centre[0], 6),
                                                            round(self.centre[1], 6)],
                "radius_m": None if self.radius_m is None else round(self.radius_m, 1),
                "height_m": None if self.height_m is None else round(self.height_m, 1),
                "ceiling_m": None if self.ceiling_m is None else round(self.ceiling_m, 1),
                "landing_area": self.landing_area, "address": self.address,
                "window": None if self.window is None else self.window.to_dict(),
                "detail": self.detail, "evidence": self.evidence[:EVIDENCE_CHARS],
                "note": self.note, "numbers": dict(self.numbers)}

    @classmethod
    def from_dict(cls, raw: dict) -> "Hazard":
        window = raw.get("window") or None
        centre = raw.get("centre")
        return cls(
            kind=str(raw.get("kind") or "none"), place=str(raw.get("place") or ""),
            centre=(float(centre[0]), float(centre[1])) if centre else None,
            radius_m=raw.get("radius_m"), height_m=raw.get("height_m"),
            ceiling_m=raw.get("ceiling_m"), landing_area=raw.get("landing_area"),
            address=str(raw.get("address") or ""),
            window=None if not window else Window(_from_iso(window.get("start")),
                                                  _from_iso(window.get("end")),
                                                  bool(window.get("daily")),
                                                  str(window.get("text") or "")),
            detail=str(raw.get("detail") or ""), evidence=str(raw.get("evidence") or ""),
            note=str(raw.get("note") or ""), numbers=dict(raw.get("numbers") or {}))


# 착륙장 이름의 다른 표기. 공지는 공식 이름을 쓰고 우리 목록은 짧은 이름을 씁니다.
LANDING_ALIASES = {
    "la-tompkins": ["Tompkins Square Park"],
    "la-washington": ["Washington Square Park"],
    "la-union": ["Union Square Park"],
    "la-stuytown": ["Stuyvesant Town Oval", "Stuyvesant Oval"],
    "la-seaport": ["Pier 17"],
    "la-pier25": ["Pier 25"], "la-pier45": ["Pier 45"], "la-pier62": ["Pier 62"],
    "la-pier76": ["Pier 76"], "la-pier84": ["Pier 84"],
    "la-eastmeadow": ["East Meadow"], "la-northmeadow": ["North Meadow"],
    "la-cpnorth": ["Central Park North"],
    "la-hunters": ["Hunter's Point South Park", "Hunters Point South Park"],
    "la-gantry": ["Gantry Plaza State Park"],
    "la-sara": ["Sara Delano Roosevelt Park"],
    "la-stnicholas": ["Saint Nicholas Park"],
    "la-stuyvesant": ["Stuyvesant Cove Park"],
    "la-battery": ["The Battery"],
    "la-morningside": ["Morningside Park"],
}
# 이름 뒤에 이 말이 붙으면 그 공원 얘기가 아닙니다("Battery Park City" 는 동네입니다).
NOT_THE_PARK = re.compile(r"\s+cit(?:y|ies)\b", re.IGNORECASE)


def landing_names(area: dict) -> list[str]:
    """이 착륙장을 부르는 이름들. 긴 것부터 — 짧은 이름이 긴 이름을 가로채면 안 됩니다."""
    name = str(area.get("name") or "")
    names = {name, *LANDING_ALIASES.get(str(area.get("id") or ""), [])}
    if name and not name.lower().endswith("park"):
        names.add(f"{name} Park")
    return sorted((n for n in names if n), key=len, reverse=True)


def find_landing_area(text: str, landing_areas: list[dict]) -> tuple[dict, str] | None:
    """본문이 이름을 댄 착륙장. 가장 긴 이름이 이깁니다."""
    body = " ".join(str(text or "").split())
    best = None
    for area in landing_areas or []:
        for name in landing_names(area):
            found = re.search(re.escape(name) + r"\b", body, re.IGNORECASE)
            if found is None:
                continue
            if NOT_THE_PARK.match(body[found.end():]):
                continue
            if best is None or len(name) > len(best[1]):
                best = (area, name)
    return best


def _sentences(text: str) -> list[str]:
    return [part for part in SENTENCES.split(" ".join(str(text or "").split())) if part]


def _sentence_with(text: str, pattern: re.Pattern) -> str:
    for sentence in _sentences(text):
        if pattern.search(sentence):
            return sentence
    return ""


def _address_in(text: str, gazetteer: Gazetteer) -> dict | None:
    """본문의 주소 중 지명 사전이 아는 첫 번째. 사전에 없는 주소는 자리가 아닙니다."""
    for match in ADDRESS_ANY.finditer(text or ""):
        found = gazetteer.address(match.group(1))
        if found is not None:
            return found
    return None


def _height_m(text: str) -> float | None:
    for pattern in (HEIGHT_LED, HEIGHT_TRAILING):
        match = pattern.search(text or "")
        if match is None:
            continue
        number = float(match.group(1).replace(",", ""))
        unit = match.group(2).lower()
        return number * FT_TO_M if unit in ("feet", "foot", "ft") else number
    return None


def _radius_m_all(text: str) -> list[float]:
    """본문이 말한 반경 전부(m). TFR 은 안쪽 핵과 바깥 링을 같이 적기도 합니다."""
    found = []
    for pattern in RADIUS_PATTERNS:
        for match in pattern.finditer(text or ""):
            number = float(match.group(1))
            unit = match.group(2).lower().replace(" ", "")
            if unit.startswith("nautical") or unit in ("nm", "nmr"):
                found.append(number * NM_TO_M_BRIEF)
            elif unit.startswith("statute") or unit in ("mile", "miles", "mi"):
                found.append(number * SM_TO_M_BRIEF)
            elif unit in ("feet", "ft"):
                found.append(number * FT_TO_M)
            elif unit in ("km", "kilometer", "kilometers", "kilometre", "kilometres"):
                found.append(number * 1000.0)
            else:
                found.append(number)
    return sorted(found)


def _centre_in(text: str) -> tuple[float, float] | None:
    """본문의 좌표. FAA 의 DDMMSS, 도분초 기호, 십진수 순으로 봅니다."""
    body = text or ""
    found = COORD.search(body)
    if found is not None:
        return parse_dms(found.group(0))
    dms = COORD_DMS.findall(body) or COORD_DMS_SPACED.findall(body)
    if len(dms) >= 2:
        values = {}
        for degrees, minutes, seconds, hemisphere in dms[:2]:
            value = float(degrees) + float(minutes) / 60.0 + float(seconds) / 3600.0
            letter = hemisphere.upper()
            values[letter] = -value if letter in ("S", "W") else value
        if ("N" in values or "S" in values) and ("E" in values or "W" in values):
            return (values.get("N", values.get("S")), values.get("W", values.get("E")))
    decimal = COORD_DECIMAL.search(body)
    if decimal is not None:
        return (float(decimal.group(1)), float(decimal.group(2)))
    pair = COORD_PAIR.search(body)
    if pair is not None:
        return (float(pair.group(1)), float(pair.group(2)))
    return None


def _ceiling_m(text: str) -> float | None:
    match = CEILING_FT.search(text or "")
    return None if match is None else float(match.group(1)) * FT_TO_M


# ---------- 문법 넷 ----------

def read_crane(text: str, gazetteer: Gazetteer, day: datetime.date) -> Hazard | None:
    """타워크레인: 높이 + 주소. 크레인은 세워져 있는 동안 늘 거기 있으므로 하루 시간대는 안 봅니다.

    크레인이 필요한 이유는 자료에 없기 때문입니다 — 심사자가 쓰는 건물 자료(OSM)에도, FAA 격자에도
    어제 세운 크레인은 없습니다. 공지에는 있습니다.
    """
    if CRANE_WORDS.search(text or "") is None:
        return None
    sentence = _sentence_with(text, CRANE_WORDS)
    height = _height_m(sentence) or _height_m(text)
    if height is None:
        return None
    found = _address_in(sentence, gazetteer) or _address_in(text, gazetteer)
    if found is None:
        return None
    window = read_window(text, day)
    dates = _dates_in(text, day.year)
    if window is not None and dates:
        # 작업 시간은 크레인이 서 있는 시간이 아닙니다. 세워진 날부터 내리는 날까지입니다.
        window = Window(eastern_to_utc(dates[0], 0, 0), eastern_to_utc(dates[-1], 23, 59),
                        text=window.text)
    return Hazard(
        kind="crane", place=str(found["label"]), address=str(found["label"]),
        centre=(float(found["lat"]), float(found["lon"])), height_m=height, window=window,
        detail=f"tower crane {height:.0f} m · {found['label']}",
        evidence=(sentence or text)[:EVIDENCE_CHARS])


def read_closure(text: str, landing_areas: list[dict], day: datetime.date) -> Hazard | None:
    """공원 폐쇄: 우리 착륙장 이름 + 시간 창. 착륙장은 공원과 부두라 폐쇄가 곧 착륙 불가입니다."""
    if CLOSURE_WORDS.search(text or "") is None:
        return None
    sentence = _sentence_with(text, CLOSURE_WORDS)
    named = find_landing_area(sentence, landing_areas) or find_landing_area(text, landing_areas)
    if named is None:
        return None
    area, name = named
    window = read_window(text, day)
    if window is None:
        return None
    return Hazard(
        kind="closure", place=str(area.get("name") or name), landing_area=str(area.get("id")),
        centre=(float(area["lat"]), float(area["lon"])), window=window,
        detail=f"{area.get('name')} closed", evidence=(sentence or text)[:EVIDENCE_CHARS])


def read_event(text: str, gazetteer: Gazetteer, landing_areas: list[dict],
               day: datetime.date) -> Hazard | None:
    """행사: 장소 + 시간 창. 사람이 모이는 자리 위로는 날지 않습니다(Part 107.39 와 같은 이유)."""
    if EVENT_WORDS.search(text or "") is None:
        return None
    sentence = _sentence_with(text, EVENT_WORDS)
    place, centre = "", None
    named = find_landing_area(sentence, landing_areas) or find_landing_area(text, landing_areas)
    if named is not None:
        area, _ = named
        place, centre = str(area.get("name")), (float(area["lat"]), float(area["lon"]))
    else:
        found = _address_in(sentence, gazetteer) or _address_in(text, gazetteer)
        if found is not None:
            place, centre = str(found["label"]), (float(found["lat"]), float(found["lon"]))
    if centre is None:
        return None
    window = read_window(text, day)
    if window is None:
        return None
    radii = [r for r in _radius_m_all(text) if MIN_BRIEF_RADIUS_M <= r <= MAX_BRIEF_RADIUS_M]
    radius = radii[0] if radii else DEFAULT_EVENT_RADIUS_M
    return Hazard(
        kind="event", place=place, centre=centre, radius_m=radius, window=window,
        detail=f"event · {place} · {radius:.0f} m radius",
        evidence=(sentence or text)[:EVIDENCE_CHARS])


def read_restriction(text: str, day: datetime.date) -> Hazard | None:
    """비행 제한: 반경 + 중심 + 시간 창. FAA 의 TFR 상세 쪽이 이 모양으로 옵니다."""
    if RESTRICTION_WORDS.search(text or "") is None:
        return None
    centre = _centre_in(text)
    if centre is None:
        return None
    radii = _radius_m_all(text)
    if not radii:
        return None
    window = read_window(text, day)
    if window is None:
        return None
    radius = radii[0]
    note = ""
    bigger = [r for r in radii if r > MAX_BRIEF_RADIUS_M]
    if bigger:
        note = (f"바깥 링 {max(bigger) / NM_TO_M_BRIEF:.0f} NM 은 코드가 그리기에 너무 큽니다 — "
                "사람이 봐야 합니다")
    return Hazard(
        kind="restriction", place="TFR", centre=centre, radius_m=radius,
        ceiling_m=_ceiling_m(text), window=window, note=note,
        detail=f"TFR · {radius:.0f} m radius",
        evidence=(_sentence_with(text, RESTRICTION_WORDS) or text)[:EVIDENCE_CHARS])


def read_advisory(text: str, day: datetime.date) -> Hazard | None:
    """기상 주의보. 규칙은 만들지 않습니다 — 이륙 정지는 관측(METAR)이 하는 일입니다."""
    if ADVISORY_WORDS.search(text or "") is None:
        return None
    report = parse_weather(text)
    kind = ADVISORY_WORDS.search(text).group(0)
    window = read_window(text, day)
    numbers = {} if report is None else {key: value for key, value in report.to_dict().items()
                                         if value is not None and key != "text"}
    detail = kind.title()
    if report is not None and report.gust_mps is not None:
        detail += f" · gusts {report.gust_mps:.0f} m/s"
    elif report is not None and report.wind_mps is not None:
        detail += f" · wind {report.wind_mps:.0f} m/s"
    return Hazard(kind="weather", place=kind.title(), window=window, numbers=numbers,
                  detail=detail,
                  evidence=(_sentence_with(text, ADVISORY_WORDS) or text)[:EVIDENCE_CHARS])


def read_hazard(text: str, gazetteer: Gazetteer, landing_areas: list[dict],
                day: datetime.date) -> Hazard | None:
    """문법으로 읽어 봅니다: 제한 → 크레인 → 폐쇄 → 행사 → 기상. 못 읽으면 None."""
    body = " ".join(str(text or "").split())
    if not body:
        return None
    for reader in (
        lambda: read_restriction(body, day),
        lambda: read_crane(body, gazetteer, day),
        lambda: read_closure(body, landing_areas, day),
        lambda: read_event(body, gazetteer, landing_areas, day),
        lambda: read_advisory(body, day),
    ):
        hazard = reader()
        if hazard is not None:
            return hazard
    return None


def worth_a_model(text: str) -> bool:
    """문법이 못 읽은 글을 Super 에게 물을 가치가 있나. 낱말 하나도 안 걸리면 남의 얘기입니다."""
    return BRIEFING_WORDS.search(text or "") is not None


# ---------- 검사(코드가 판정합니다) ----------

def hazard_problems(hazard: Hazard, bbox: tuple[float, float, float, float] | None) -> list[str]:
    """읽어 낸 위험에 거는 범위 검사 전부. 누가 읽었든 겁니다."""
    problems = []
    if hazard.kind not in BRIEFING_KINDS:
        problems.append(f"모르는 종류 {hazard.kind!r}")
    if hazard.kind == "crane":
        if hazard.height_m is None or not (MIN_CRANE_M <= hazard.height_m <= MAX_CRANE_M):
            problems.append(f"크레인 높이가 {MIN_CRANE_M:.0f}~{MAX_CRANE_M:.0f} m 밖")
        if hazard.centre is None:
            problems.append("자리를 못 찾음")
    if hazard.kind in ("event", "restriction"):
        radius = hazard.radius_m
        if radius is None or not (MIN_BRIEF_RADIUS_M <= radius <= MAX_BRIEF_RADIUS_M):
            problems.append(f"반경이 {MIN_BRIEF_RADIUS_M:.0f}~{MAX_BRIEF_RADIUS_M:.0f} m 밖")
        if hazard.centre is None:
            problems.append("중심을 못 찾음")
    if hazard.kind == "closure" and not hazard.landing_area:
        problems.append("우리 착륙장이 아님")
    if hazard.rule_kind is not None and hazard.window is None:
        problems.append("시간 창이 없음")
    window = hazard.window
    if window is not None and window.start is not None and window.end is not None \
            and window.end <= window.start:
        problems.append("끝이 시작보다 앞")
    if bbox is not None and hazard.centre is not None:
        lat_min, lon_min, lat_max, lon_max = bbox
        lat, lon = hazard.centre
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            problems.append("서비스 영역 밖 자리")
    return problems


# ---------- 모델이 읽는 쪽 ----------

BRIEFING_SYSTEM = (
    "You read one page a drone tower gathered for its pre-flight briefing over New York City "
    "and put it in a form. You do not decide anything: code checks your numbers against the "
    "tower's own gazetteer and a person confirms anything you read. Reply with one JSON object "
    'and nothing else. If the page is not about any of these, reply {"kind": "none"}. '
    'Kinds: {"kind": "crane", "address": "<street address exactly as written>", '
    '"height_ft": <number or null>, "height_m": <number or null>}; '
    '{"kind": "closure", "park": "<park name exactly as written>"}; '
    '{"kind": "event", "venue": "<park name or street address as written>", '
    '"radius_m": <50..5000 or null>}; '
    '{"kind": "restriction", "lat": <number>, "lon": <number>, "radius_m": <50..5000>, '
    '"ceiling_ft": <number or null>}; {"kind": "weather"}. '
    'Every kind may carry a window: "start" and "end" as "YYYY-MM-DDTHH:MM" plus '
    '"timezone": "UTC" or "local", and "summary": one short sentence. '
    "Never invent an address, a park, a coordinate or a number that is not on the page."
)


def from_briefing_form(form: dict, gazetteer: Gazetteer, landing_areas: list[dict],
                       day: datetime.date, text: str = "") -> Hazard | None:
    """모델(또는 Tavily 의 research)이 채운 양식을 같은 Hazard 로. 양식이 아니면 None.

    자리는 모델의 말이 아니라 지명 사전·착륙장 목록에서 찾습니다 — 모델이 좌표를 지어내도
    사전에 없는 곳은 곳이 아닙니다. 좌표를 직접 받는 것은 비행 제한뿐이고, 그것도 서비스 영역
    상자 검사를 지나야 합니다.
    """
    if not isinstance(form, dict):
        return None
    kind = str(form.get("kind") or "").strip().lower()
    if kind not in BRIEFING_KINDS:
        return None
    window = _form_window(form, day) or read_window(text, day)
    summary = _clean(form.get("summary"))
    if kind == "none":
        return Hazard(kind="none", detail=summary, evidence=text[:EVIDENCE_CHARS])
    if kind == "weather":
        return Hazard(kind="weather", place="weather advisory", window=window, detail=summary,
                      evidence=text[:EVIDENCE_CHARS])
    if kind == "crane":
        found = gazetteer.address(_clean(form.get("address")))
        if found is None:
            raise UnknownPlace(f"지명 사전에 없는 주소 {_clean(form.get('address'))!r}")
        height = _number(form.get("height_m"))
        if height is None and form.get("height_ft") is not None:
            height = (_number(form.get("height_ft")) or 0.0) * FT_TO_M
        return Hazard(kind="crane", place=str(found["label"]), address=str(found["label"]),
                      centre=(float(found["lat"]), float(found["lon"])), height_m=height,
                      window=window, detail=summary or f"tower crane · {found['label']}",
                      evidence=text[:EVIDENCE_CHARS])
    if kind == "closure":
        named = find_landing_area(_clean(form.get("park")), landing_areas)
        if named is None:
            raise UnknownPlace(f"우리 착륙장이 아님 {_clean(form.get('park'))!r}")
        area, _ = named
        return Hazard(kind="closure", place=str(area.get("name")),
                      landing_area=str(area.get("id")),
                      centre=(float(area["lat"]), float(area["lon"])), window=window,
                      detail=summary or f"{area.get('name')} closed",
                      evidence=text[:EVIDENCE_CHARS])
    if kind == "event":
        venue = _clean(form.get("venue"))
        named = find_landing_area(venue, landing_areas)
        if named is not None:
            area, _ = named
            place, centre = str(area.get("name")), (float(area["lat"]), float(area["lon"]))
        else:
            found = gazetteer.address(venue)
            if found is None:
                raise UnknownPlace(f"모르는 행사 장소 {venue!r}")
            place, centre = str(found["label"]), (float(found["lat"]), float(found["lon"]))
        radius = _number(form.get("radius_m")) or DEFAULT_EVENT_RADIUS_M
        return Hazard(kind="event", place=place, centre=centre, radius_m=radius, window=window,
                      detail=summary or f"event · {place}", evidence=text[:EVIDENCE_CHARS])
    latitude, longitude = _number(form.get("lat")), _number(form.get("lon"))
    if latitude is None or longitude is None:
        return None
    ceiling = _number(form.get("ceiling_ft"))
    return Hazard(kind="restriction", place=_clean(form.get("venue")) or "TFR",
                  centre=(latitude, longitude), radius_m=_number(form.get("radius_m")),
                  ceiling_m=None if ceiling is None else ceiling * FT_TO_M, window=window,
                  detail=summary or "TFR", evidence=text[:EVIDENCE_CHARS])


def _form_window(form: dict, day: datetime.date) -> Window | None:
    """양식의 start·end. timezone 이 local 이면 뉴욕 지방시로 읽습니다."""
    local = str(form.get("timezone") or "").strip().lower() == "local"
    start, end = _form_time(form.get("start"), day, local), _form_time(form.get("end"), day, local)
    if start is None and end is None:
        return None
    return Window(start, end, text=f"{form.get('start')} ~ {form.get('end')}")


def _form_time(value, day: datetime.date, local: bool) -> datetime.datetime | None:
    raw = str(value or "").strip().rstrip("Zz")
    if not raw:
        return None
    for shape in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.datetime.strptime(raw, shape)
        except ValueError:
            continue
        if local:
            return eastern_to_utc(parsed.date(), parsed.hour, parsed.minute)
        return parsed.replace(tzinfo=datetime.UTC)
    match = TIME_LOCAL.search(raw) or TIME_ZULU.search(raw)
    if match is None:
        return None
    zulu, spoken = _times_in(raw)
    if zulu:
        return datetime.datetime(day.year, day.month, day.day, *zulu[0],
                                 tzinfo=datetime.UTC)
    if spoken:
        return eastern_to_utc(day, *spoken[0])
    return None
