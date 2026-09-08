"""One process per vehicle.

MODE=guarded  → the only address it knows is the runtime. It cannot reach an actuator;
                in compose it is not even on the network the simulator lives on.
MODE=direct   → it holds the actuator address itself. This is how most fleets are wired
                today, and it is given MORE information than the guarded agent, not less.
"""

import os
import time

from attache.agent.detect import detect
from attache.agent.propose import COSTS, Proposer
from attache.core.http import get_json, post_json
from attache.llm.client import TieredLlm

PADS = ["pad:P1", "pad:P2"]


class GuardedAgent:
    """신청서를 냅니다. 그게 전부입니다."""

    def __init__(self, asset_id: str, runtime_url: str, proposer: Proposer):
        self.asset_id = asset_id
        self.runtime_url = runtime_url.rstrip("/")
        self.proposer = proposer
        self.pad_index = 0
        self.banned: set[str] = set()
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self.denial_s = float(os.getenv("DENIAL_COOLDOWN_S", "6"))

    def telemetry(self) -> dict:
        return get_json(f"{self.runtime_url}/telemetry/{self.asset_id}") or {}

    def step(self) -> None:
        telemetry = self.telemetry()
        if not telemetry:
            return
        concern = detect(telemetry)
        if concern is None:
            return
        proposal = self.proposer.write(
            concern, telemetry, PADS[self.pad_index], frozenset(self.banned)
        )
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # 방금 거절당한 걸 계속 들이밀지 않습니다
        self.cooldown[proposal.action] = time.time() + self.repeat_s
        decision = post_json(f"{self.runtime_url}/proposals", proposal.to_dict())
        if decision and decision.get("verdict") in ("denied", "human"):
            self.cooldown[proposal.action] = time.time() + self.denial_s
        if decision and decision.get("verdict") == "denied":
            if decision.get("policy_hit"):
                # 강제점이 있으면 금지 사실을 그 자리에서 알게 됩니다
                self.banned.add(proposal.action)
            elif proposal.resource:
                self.pad_index = (self.pad_index + 1) % len(PADS)
        _report(self.asset_id, "guarded", proposal, decision)


class DirectAgent:
    """조종장치 주소를 직접 들고 있습니다. 스스로 규칙을 지키려고 합니다."""

    def __init__(self, asset_id: str, sim_url: str, proposer: Proposer,
                 per_asset_limit: float, bulletin_period_s: float):
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self.asset_id = asset_id
        self.sim_url = sim_url.rstrip("/")
        self.proposer = proposer
        self.per_asset_limit = per_asset_limit
        self.bulletin_period_s = bulletin_period_s
        self.spend = 0.0
        self.banned_actions: set[str] = set()
        self._last_bulletin_check = 0.0

    def _refresh_bulletins(self, model: str) -> None:
        now = time.time()
        if now - self._last_bulletin_check < self.bulletin_period_s:
            return
        self._last_bulletin_check = now
        payload = get_json(f"{self.sim_url}/bulletins") or {}
        for item in payload.get("bulletins", []):
            applies = item.get("applies_to", {})
            if applies.get("model") in (None, model):
                self.banned_actions.add(item["forbid_action"])

    def step(self) -> None:
        state = get_json(f"{self.sim_url}/state?world=direct") or {}
        telemetry = (state.get("assets") or {}).get(self.asset_id)
        if not telemetry:
            return
        self._refresh_bulletins(telemetry.get("model", ""))

        concern = detect(telemetry)
        if concern is None:
            return

        proposal = self.proposer.write(
            concern, telemetry, self._free_looking_pad(state), frozenset(self.banned_actions)
        )
        if proposal.action in self.banned_actions:
            return  # 공지를 본 뒤에는 스스로 지킵니다
        if self.spend + COSTS.get(proposal.action, 0.0) > self.per_asset_limit:
            return  # 자기 한도는 스스로 지킵니다. 기단 합계는 알 방법이 없습니다
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # 방금 낸 명령을 또 보내지 않습니다
        self.cooldown[proposal.action] = time.time() + self.repeat_s

        result = post_json(
            f"{self.sim_url}/act",
            {
                "world": "direct",
                "asset": self.asset_id,
                "action": proposal.action,
                "params": proposal.params,
                "blast": proposal.blast_radius,
            },
        )
        if result and result.get("ok"):
            self.spend += result.get("cost_usd", 0.0)
        _report(self.asset_id, "direct", proposal, result)

    def _free_looking_pad(state: dict) -> str:
        """다른 기체가 실제로 내려앉아 있는 패드만 피할 수 있습니다.

        위치는 Remote ID 로 공개되지만 '내가 저 패드를 잡아뒀다'는 의도는 공개되지
        않습니다. 회사가 다르면 서로의 예약을 볼 방법이 아예 없습니다. 하늘길은
        ASTM F3548 이 이 문제를 풀어놨는데, 땅 위 패드는 아무도 안 풀었습니다.
        """
        taken = {
            vehicle.get("assigned_pad")
            for vid, vehicle in (state.get("assets") or {}).items()
            if vid != self.asset_id and vehicle.get("state") in ("landed", "charging")
        }
        for pad in PADS:
            if pad not in taken:
                return pad
        return PADS[0]


def _report(asset_id: str, mode: str, proposal, outcome) -> None:
    verdict = (outcome or {}).get("verdict") or ("ok" if (outcome or {}).get("ok") else "?")
    print(f"[{mode}:{asset_id}] {proposal.action} ${proposal.cost_usd:.0f} -> {verdict}",
          flush=True)


def main() -> None:
    asset_id = os.environ["ASSET_ID"]
    mode = os.getenv("MODE", "guarded")
    period = float(os.getenv("AGENT_PERIOD_S", "0.6"))
    llm = TieredLlm(models={
        "nano": os.getenv("MODEL_NANO", ""),
        "super": os.getenv("MODEL_SUPER", ""),
        "ultra": os.getenv("MODEL_ULTRA", ""),
    })
    proposer = Proposer(llm)

    if mode == "guarded":
        agent = GuardedAgent(asset_id, os.getenv("RUNTIME_URL", "http://runtime:8000"), proposer)
    else:
        agent = DirectAgent(
            asset_id,
            os.getenv("SIM_URL", "http://sim:8100"),
            proposer,
            per_asset_limit=float(os.getenv("PER_ASSET_LIMIT_USD", "200")),
            bulletin_period_s=float(os.getenv("BULLETIN_PERIOD_S", "5")),
        )

    print(f"agent {asset_id} up in {mode} mode (llm={'on' if llm.enabled else 'off'})", flush=True)
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
