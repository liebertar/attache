"""The eight nouns. Shared by agents and runtime; neither side may add fields at will."""

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum

# 영향 범위 순위. 중재자가 규칙으로 되돌아갈 때 이 순서로 고릅니다.
BLAST_RANK = {"none": 0, "schedule": 1, "cargo": 2, "passenger": 3, "public": 4}


class Verdict(str, Enum):
    AUTO = "auto"        # 한도 안. 실행됩니다
    QUEUED = "queued"    # 자원 배정을 기다리는 중. 아직 아무 일도 안 일어났습니다
    HUMAN = "human"      # 사람이 봐야 합니다
    DENIED = "denied"


@dataclass
class Proposal:
    """에이전트가 낼 수 있는 유일한 것. 실행 능력은 없습니다."""

    asset_id: str
    action: str
    cost_usd: float
    blast_radius: str
    rationale: str
    params: dict = field(default_factory=dict)
    resource: str | None = None
    author: str = "rules"
    world: str = "guarded"
    id: str = field(default_factory=lambda: f"p_{uuid.uuid4().hex[:10]}")
    filed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Proposal":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in allowed})

    def validate(self) -> list[str]:
        problems = []
        if not self.asset_id or not self.action:
            problems.append("asset_id and action are required")
        if self.blast_radius not in BLAST_RANK:
            problems.append(f"unknown blast_radius: {self.blast_radius}")
        if self.cost_usd < 0:
            problems.append("cost_usd must not be negative")
        return problems


@dataclass
class Decision:
    proposal_id: str
    verdict: Verdict
    reason: str
    policy_hit: str | None = None
    forbids: str | None = None      # 금지된 것 자체. 행동 이름이거나 자원 이름
    authority_hit: str | None = None
    arbiter: str | None = None
    approved_by: str | None = None
    ledger_id: str | None = None
    committed: bool = False
    # 사람이 읽는 문장은 reason 에 그대로 둡니다. 화면은 code 와 detail 로 자기 말을
    # 만듭니다 — 문구를 고칠 때마다 화면이 조용히 깨지지 않게.
    code: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["verdict"] = self.verdict.value
        return out


@dataclass
class LedgerEntry:
    """기록이 먼저 남고 그 다음에 실행됩니다. 순서가 뒤집히면 실행이 안 됩니다."""

    proposal: dict
    decision: dict
    id: str = field(default_factory=lambda: f"l_{uuid.uuid4().hex[:12]}")
    at: float = field(default_factory=time.time)
    outcome: str = "pending"
    # 판정 맥락. 어느 틱에, 어느 공역 판본으로, 어느 정책이 걸린 채, 어떤 검사를 어느 순서로 했나.
    # {tick, airspace_revision, policies: [id], intent_id, checks_run: [이름]}. "그때 왜 그렇게
    # 판정했나" 를 원장만 보고 답할 수 있어야 합니다 — 지금 상태로는 그때 상태를 모릅니다.
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
