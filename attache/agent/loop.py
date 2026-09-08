"""One process per vehicle. It files requests. That is the whole of it.

The only address this process knows is the runtime's. It cannot reach an actuator: there is
no actuator client in this package, none in its container image, and in compose it is not
even on the network the vehicles live on.

The other wiring — an agent holding the actuator address — lives in the `direct_agent`
package, which is built into a different image.
"""

import os
import time

from attache.agent.detect import detect
from attache.agent.propose import Proposer
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


def _report(asset_id: str, mode: str, proposal, outcome) -> None:
    verdict = (outcome or {}).get("verdict") or ("ok" if (outcome or {}).get("ok") else "?")
    print(f"[{mode}:{asset_id}] {proposal.action} ${proposal.cost_usd:.0f} -> {verdict}",
          flush=True)


def build_llm() -> TieredLlm:
    return TieredLlm(models={
        "nano": os.getenv("MODEL_NANO", ""),
        "super": os.getenv("MODEL_SUPER", ""),
        "ultra": os.getenv("MODEL_ULTRA", ""),
    })


def main() -> None:
    asset_id = os.environ["ASSET_ID"]
    period = float(os.getenv("AGENT_PERIOD_S", "0.6"))
    llm = build_llm()
    agent = GuardedAgent(
        asset_id, os.getenv("RUNTIME_URL", "http://runtime:8000"), Proposer(llm)
    )
    print(f"agent {asset_id} up, files to runtime (llm={'on' if llm.enabled else 'off'})",
          flush=True)
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
