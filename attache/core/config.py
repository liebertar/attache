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
class FleetConfig:
    name: str
    resources: list[str]
    authority: Authority
    escalation: Escalation
    policies: list[Policy] = field(default_factory=list)


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
    )
