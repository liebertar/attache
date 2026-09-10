"""Information the tower takes in: a weather report, an incident, a restriction — as text.

What arrives is a sentence (a METAR-like line from the simulator, a search snippet, a line a
person typed). The runtime needs numbers with units and a place on the map. A grammar reads
the regular dialects deterministically: the same sentence always gives the same numbers, and
a sentence it cannot read is refused rather than guessed at. Prose the grammar cannot read
goes to a model that fills the same schema; code validates that answer against the ranges
below and the gazetteer, and what a model read is held for a person before it applies.
"""

import re
from dataclasses import dataclass, field

from attache.core.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON
from attache.core.notam import (
    Clock,
    Notice,
    _window,
    circle,
    from_model_form,
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
