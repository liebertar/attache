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

    def matches(self, action: str, resource: str | None, asset: dict, tick: int) -> bool:
        if tick < self.active_from_tick:
            return False
        if self.active_until_tick is not None and tick > self.active_until_tick:
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

    per_asset_usd: float
    fleet_usd: float
    human_required_blast: list[str] = field(default_factory=list)
    human_required_actions: list[str] = field(default_factory=list)


@dataclass
class Escalation:
    nano: str
    super: str
    ultra: str


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


@dataclass
class FleetConfig:
    name: str
    resources: list[str]
    authority: Authority
    escalation: Escalation
    policies: list[Policy] = field(default_factory=list)
    performance: Performance = field(default_factory=Performance)


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
        performance=Performance(**(raw.get("performance") or {})),
    )
