"""Config loading. The runtime knows nothing about motors or refunds; this file is the domain."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Policy:
    """전 기체 금지 규칙. 한도보다 먼저 봅니다.

    행동을 막거나(리콜: 급속충전 금지) 자원을 막습니다(비행금지 구역: 그 패드 금지).
    둘 다 비어 있으면 아무것도 막지 않습니다.
    """

    id: str
    reason: str
    forbid_action: str | None = None
    forbid_resource: str | None = None
    applies_to: dict = field(default_factory=dict)
    active_from_tick: int = 0
    active_until_tick: int | None = None   # ED-269 구역도 유효기간을 갖습니다
    # 땅에 있는 기체에만 거는 금지. 기상 대기(WEATHER HOLD)는 이륙을 막는 것이지 떠 있는 기체를
    # 세우는 것이 아닙니다 — 떠 있는 기체의 재신청(회수 뒤 내려올 길)은 그대로 판정받아야 합니다.
    ground_only: bool = False

    def matches(self, action: str, resource: str | None, asset: dict, tick: int) -> bool:
        if tick < self.active_from_tick:
            return False
        if self.active_until_tick is not None and tick > self.active_until_tick:
            return False
        if self.ground_only and float(asset.get("alt_m") or 0.0) > 1.0:
            return False
        if self.forbid_action and action != self.forbid_action:
            return False
        if self.forbid_resource and resource != self.forbid_resource:
            return False
        if not self.forbid_action and not self.forbid_resource:
            return False
        return all(asset.get(key) == value for key, value in self.applies_to.items())


@dataclass
class Authority:
    """한도, 그리고 사람이 꼭 봐야 하는 행동 목록."""

    per_asset_usd: float | None      # null 이면 돈을 판정하지 않습니다(예산은 운영사 몫)
    fleet_usd: float | None
    human_required_blast: list[str] = field(default_factory=list)
    human_required_actions: list[str] = field(default_factory=list)


@dataclass
class Escalation:
    nano: str
    super: str
    ultra: str


# 모델 id → 사람이 읽는 이름. 화면 헤더와 기체 라벨이 씁니다. 여기 없는 id 는 그대로 보여 주고,
# 빈 id 는 "rules"(모델 없이 규칙만) 입니다. Ollama 태그(:4b, :latest)와 Nebius id 를 같이 둡니다 —
# 두 서버의 같은 계열이 화면에서 같은 이름으로 읽혀야 합니다.
MODEL_DISPLAY = {
    "nemotron-3-nano:4b": "Nemotron Nano 4B",
    "nemotron-3-nano": "Nemotron Nano 30B",
    "nemotron-3-nano:latest": "Nemotron Nano 30B",
    "nvidia/nemotron-3_5-lightning": "Nemotron 3.5 Lightning",
    "nvidia/nemotron-3-super-120b-a12b": "Nemotron Super 120B",
    "nvidia/nemotron-3-ultra-550b-a55b": "Nemotron Ultra 550B",
}
RULES_DISPLAY = "rules"


def model_display(model_id: str | None) -> str:
    """모델 id 의 표시 이름. 모르는 id 는 지어내지 않고 그대로 돌려줍니다."""
    key = (model_id or "").strip()
    if not key:
        return RULES_DISPLAY
    return MODEL_DISPLAY.get(key.lower(), key)


# 통신 두절 대비 행동. 조종장치가 링크를 잃으면 무엇을 하는지 운영사가 신고합니다.
# 런타임은 승인할 때 그 행동이 만드는 부피(continue_and_land: 승인 경로 + 착륙 기둥)까지 판정하고,
# 두절 중에는 그 부피를 예약된 채로 둡니다. 모르는 행동은 판정할 수 없으니 그 신청은 거절합니다.
CONTINUE_AND_LAND = "continue_and_land"
KNOWN_LOST_LINK_BEHAVIOURS = (CONTINUE_AND_LAND,)


@dataclass
class LostLink:
    behaviour: str = CONTINUE_AND_LAND
    timeout_ticks: int = 15          # 떠 있는 기체의 텔레메트리가 이만큼 안 새로워지면 두절

    @property
    def known(self) -> bool:
        return self.behaviour in KNOWN_LOST_LINK_BEHAVIOURS


@dataclass
class Performance:
    """운영사가 신고한 기체 성능과 이 판의 시계. 런타임이 의도(4D)의 시간 창을 여기서 셈합니다.

    운영사는 경로만 냅니다. 언제 어디에 있을지는 런타임이 신고 성능으로 계산합니다 — 운영사가
    시간 창을 직접 적으면 좁게 적어 충돌을 숨길 수 있습니다. 시뮬레이터(sim/world.py)의 상수와
    같아야 하고, tests/test_intents.py 가 둘을 대조합니다.
    """

    cruise_mps: float = 22.0
    climb_mps: float = 2.0
    descent_mps: float = 1.75
    seconds_per_tick: float = 0.8
    clearance_ticks: int = 25        # 지상 승인 확인 시간. 그다음 틱에 뜹니다
    clock_epoch_z: str = "0900"      # 틱 0 의 Zulu 시각. NOTAM 의 시간 창을 틱으로 옮길 때 씁니다
    # 항법 오차. 기체가 승인된 선에서 이만큼은 벗어날 수 있다고 운영사가 신고하는 값이고, 의도(4D)
    # 회랑은 분리 최소치에 이것을 더한 폭입니다(F3548 은 의도 부피에 운영사의 순응 오차가 들어
    # 있기를 기대합니다). 시뮬레이터의 경유점 반경(ARRIVAL_RADIUS_M, 모서리를 자르는 만큼)보다
    # 커야 합니다.
    nav_tolerance_m: float = 10.0
    # 통신 두절 대비. 운영사 신고값이고, 런타임은 이것으로 두절을 판정하고 대비 부피를 봅니다.
    lost_link: LostLink = field(default_factory=LostLink)


@dataclass
class WeatherLimits:
    """이 기단이 뜰 수 있는 날씨. 보고서의 숫자가 이 밖이면 런타임이 이륙을 세웁니다(WEATHER HOLD).

    숫자는 운영사 규격입니다 — 소형 배달 멀티로터의 제조사 한계(돌풍 12 m/s 안팎)에 맞춘 값이고,
    Part 107 는 시정 3 SM(약 4.8 km)을 요구하지만 도심 저고도 BVLOS 운항 규격은 대개 더 짧은
    거리를 씁니다. 창이 없는 보고서는 hold_default_ticks 만큼 세웁니다.
    """

    max_wind_mps: float = 10.0
    max_gust_mps: float = 12.0
    min_visibility_m: float = 1500.0
    hold_default_ticks: int = 500


@dataclass
class IntakeConfig:
    """정보 수집. Tavily 로 물을 질문 목록(키가 있을 때만 돕니다)과 METAR 관측소(키 없이 돕니다)."""

    queries: list[str] = field(default_factory=lambda: list(DEFAULT_INTAKE_QUERIES))
    metar_stations: list[str] = field(default_factory=lambda: list(DEFAULT_METAR_STATIONS))


DEFAULT_INTAKE_QUERIES = (
    "New York City wind gust forecast today",
    "NYC temporary flight restriction drones today",
    "Manhattan building fire today",
)
# 센트럴파크(KNYC)와 라과디아(KLGA). 서비스 영역의 두 관측소입니다. 환경 변수 METAR_STATIONS 가
# 있으면 그것이 이깁니다(쉼표·공백으로 나눔).
DEFAULT_METAR_STATIONS = ("KNYC", "KLGA")


def _performance(raw: dict | None) -> Performance:
    """performance 절. lost_link 는 한 단계 안의 표라 따로 접습니다."""
    fields = dict(raw or {})
    lost_link = fields.pop("lost_link", None) or {}
    return Performance(**fields, lost_link=LostLink(**lost_link))


def _intake(raw: dict | None) -> IntakeConfig:
    """intake 절. 비어 있는 METAR_STATIONS 는 없는 값입니다 — compose 는 값이 없어도 빈 문자열을
    넘기고, 그걸 '관측소 없음' 으로 읽으면 METAR 가 조용히 꺼집니다. 끄는 것은 METAR=off 입니다."""
    fields = dict(raw or {})
    stations = (os.getenv("METAR_STATIONS") or "").replace(",", " ").split()
    if stations:
        fields["metar_stations"] = stations
    return IntakeConfig(**fields)


@dataclass
class FleetConfig:
    name: str
    resources: list[str]
    authority: Authority
    escalation: Escalation
    policies: list[Policy] = field(default_factory=list)
    performance: Performance = field(default_factory=Performance)
    weather: WeatherLimits = field(default_factory=WeatherLimits)
    intake: IntakeConfig = field(default_factory=IntakeConfig)


def load(path: str | Path) -> FleetConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    models = raw.get("models", {})
    return FleetConfig(
        name=raw.get("name", "unnamed"),
        resources=list(raw.get("resources", [])),
        authority=Authority(**raw["authority"]),
        escalation=Escalation(
            nano=os.getenv("MODEL_NANO", models.get("nano", "")),
            super=os.getenv("MODEL_SUPER", models.get("super", "")),
            ultra=os.getenv("MODEL_ULTRA", models.get("ultra", "")),
        ),
        policies=[Policy(**p) for p in raw.get("policies", [])],
        performance=_performance(raw.get("performance")),
        weather=WeatherLimits(**(raw.get("weather") or {})),
        intake=_intake(raw.get("intake")),
    )
