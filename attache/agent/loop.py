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
from attache.agent.planner import OperatorPlanner
from attache.agent.propose import Proposer
from attache.core.http import get_json, post_json
from attache.llm.client import TieredLlm

FALLBACK_PADS = ["bay:A", "bay:B"]


class GuardedAgent:
    """신청서를 냅니다. 그게 전부입니다."""

    def __init__(self, asset_id: str, runtime_url: str, proposer: Proposer):
        self.asset_id = asset_id
        self.runtime_url = runtime_url.rstrip("/")
        self.proposer = proposer
        self.pad_index = 0
        self.pads: dict = {}
        self.banned: set[str] = set()
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self.denial_s = float(os.getenv("DENIAL_COOLDOWN_S", "6"))
        self.planner = OperatorPlanner()
        self.preferred_alt_m = float(os.getenv("CRUISE_ALT_M", "110"))

    def _open_pad(self) -> str:
        names = sorted(self.pads) or FALLBACK_PADS
        open_pads = [pad for pad in names if pad not in self.banned] or names
        return open_pads[self.pad_index % len(open_pads)]

    def telemetry(self) -> dict:
        return get_json(f"{self.runtime_url}/telemetry/{self.asset_id}") or {}

    def _destination(self, telemetry: dict, proposal) -> tuple | None:
        if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
            return (telemetry["job_lat"], telemetry["job_lon"])
        if proposal.action == "reserve_pad" and proposal.resource:
            pads = self.pads or {}
            at = pads.get(proposal.resource)
            return (at["lat"], at["lon"]) if at else None
        return None

    def _file_with_route(self, proposal, telemetry: dict):
        """일단 최단 직선으로 냅니다. 규정에 안 맞으면 런타임이 어디가 문제인지
        알려주고, 그때 다시 그립니다. 승인은 우리가 하는 게 아닙니다."""
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = self._destination(telemetry, proposal)
        if here[0] is None or goal is None:
            return post_json(f"{self.runtime_url}/proposals", proposal.to_dict())

        proposal.params = {**proposal.params,
                           "legs": self.planner.straight(here, goal, self.preferred_alt_m)}
        decision = post_json(f"{self.runtime_url}/proposals", proposal.to_dict())
        if not decision or decision.get("policy_hit") != "airspace":
            return decision

        # 다시 그리라고 했습니다. 우리 공역 사본으로 우회로를 그립니다.
        self.planner.note_refusal(decision.get("forbids"))
        legs = self.planner.draw(here, goal)
        if not legs:
            # 규정을 지키면서 갈 수 있는 길이 없습니다. 이 주문은 드론이 못 합니다.
            return post_json(f"{self.runtime_url}/proposals",
                             {**proposal.to_dict(), "action": "decline_job",
                              "cost_usd": 0.0, "blast_radius": "none", "params": {},
                              "resource": None,
                              "rationale": f"{proposal.rationale} · 규정상 경로 없음"})
        proposal.params = {**proposal.params, "legs": legs}
        proposal.rationale += f" · 재작성 {len(legs)}구간"
        return post_json(f"{self.runtime_url}/proposals", proposal.to_dict())

    def step(self) -> None:
        telemetry = self.telemetry()
        if not telemetry:
            return
        if not self.planner.airspace.all():
            world = get_json(f"{self.runtime_url}/airspace") or {}
            self.planner.load(world.get("volumes", []))
            self.pads = world.get("pads", {})
        concern = detect(telemetry)
        if concern is None:
            return
        proposal = self.proposer.write(
            concern, telemetry, self._open_pad(), frozenset(self.banned)
        )
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # 방금 거절당한 걸 계속 들이밀지 않습니다
        self.cooldown[proposal.action] = time.time() + self.repeat_s
        decision = self._file_with_route(proposal, telemetry)
        if decision and decision.get("verdict") in ("denied", "human", "queued"):
            self.cooldown[proposal.action] = time.time() + self.denial_s
        if decision and decision.get("verdict") == "denied":
            if decision.get("policy_hit"):
                # 강제점이 있으면 무엇이 금지됐는지 그 자리에서 알게 됩니다.
                # 자원이 막힌 것을 행동이 막힌 것으로 잘못 배우면 영영 신청을 못 합니다.
                self.banned.add(decision.get("forbids") or proposal.action)
            elif proposal.resource:
                self.pad_index += 1
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
