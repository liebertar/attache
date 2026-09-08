"""The runtime process. Holds locks, limits, the arbiter, the single commit path, the ledger."""

import os
import threading
import time

from attache.adapters import build as build_adapter
from attache.core import config as config_module
from attache.core.http import JsonServer, get_json
from attache.core.models import Decision, Proposal, Verdict
from attache.llm.client import TieredLlm
from attache.runtime.arbiter import Arbiter
from attache.runtime.authority import AuthorityCheck
from attache.runtime.commit import Committer
from attache.runtime.ledger import Ledger
from attache.runtime.locks import LockTable
from attache.runtime.policy import PolicyBook


class Runtime:
    def __init__(self, config_path: str, sim_url: str, ledger_path: str, window_s: float = 1.5):
        self.config = config_module.load(config_path)
        self.policies = PolicyBook(self.config.policies)
        self.authority = AuthorityCheck(self.config.authority, self.policies)
        self.locks = LockTable(self.config.resources)
        self.ledger = Ledger(ledger_path)
        self.llm = TieredLlm(models=vars(self.config.escalation))
        self.arbiter = Arbiter(self.llm)
        self.adapter = build_adapter(
            os.getenv("ADAPTER", "sim"), sim_url=sim_url, world="guarded"
        )
        self.committer = Committer(self.adapter, self.locks, self.ledger, self.authority)

        self.sim_url = sim_url
        self.window_s = window_s
        self.tick = 0
        self.telemetry: dict = {}
        self._contended: dict[str, list[tuple[Proposal, Decision, float]]] = {}
        self._awaiting_human: dict[str, Proposal] = {}
        self._decisions: dict[str, Decision] = {}
        # 벽시계가 아니라 세계의 시계로 셉니다. 그래야 재현이 됩니다.
        self._recent_commits: dict[tuple[str, str], int] = {}
        self.dedupe_ticks = int(os.getenv("DEDUPE_TICKS", "15"))
        self._guard = threading.Lock()

    # ---------- 신청 접수 ----------

    def file(self, raw: dict) -> Decision:
        proposal = Proposal.from_dict({**raw, "world": "guarded"})
        asset = self.telemetry.get(proposal.asset_id, {})

        seen_at = self._recent_commits.get((proposal.asset_id, proposal.action))
        if seen_at is not None and self.tick - seen_at < self.dedupe_ticks:
            # 같은 신청이 연달아 오면 한 번만 나갑니다. 아니면 중복 청구가 됩니다.
            decision = Decision(proposal.id, Verdict.DENIED, "직전에 같은 신청이 실행됐습니다")
            self._decisions[proposal.id] = decision
            return decision

        decision = self.authority.evaluate(proposal, asset, self.tick)
        self._decisions[proposal.id] = decision

        if decision.verdict is Verdict.DENIED:
            self.ledger.close_entry(self.ledger.open_entry(proposal, decision), "denied")
            return decision
        if decision.verdict is Verdict.HUMAN:
            with self._guard:
                existing = next(
                    (
                        waiting
                        for waiting in self._awaiting_human.values()
                        if waiting.asset_id == proposal.asset_id
                        and waiting.action == proposal.action
                    ),
                    None,
                )
                if existing is not None:
                    # 승인 화면에 같은 카드를 쌓지 않습니다
                    return self._decisions[existing.id]
                self._awaiting_human[proposal.id] = proposal
            return decision
        return self._queue_or_commit(proposal, decision)

    def _queue_or_commit(self, proposal: Proposal, decision: Decision) -> Decision:
        if not proposal.resource:
            committed = self.committer.commit(proposal, decision)
            if committed.committed:
                self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
            return committed
        with self._guard:
            self._contended.setdefault(proposal.resource, []).append(
                (proposal, decision, time.time())
            )
        decision.reason = f"{proposal.resource} 배정 대기"
        return decision

    def approve(self, proposal_id: str, actor: str, allow: bool) -> Decision | None:
        with self._guard:
            proposal = self._awaiting_human.pop(proposal_id, None)
        if proposal is None:
            return None
        decision = self._decisions[proposal_id]
        decision.approved_by = actor
        if not allow:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} 가 거부했습니다"
            self.ledger.close_entry(self.ledger.open_entry(proposal, decision), "denied")
            return decision
        decision.verdict = Verdict.AUTO
        decision.reason = f"{actor} 가 승인했습니다"
        return self._queue_or_commit(proposal, decision)

    # ---------- 자원 중재 ----------

    def _settle_contended(self) -> None:
        now = time.time()
        with self._guard:
            ready = [
                resource
                for resource, waiting in self._contended.items()
                if waiting and now - waiting[0][2] >= self.window_s
            ]
            batches = {resource: self._contended.pop(resource) for resource in ready}

        for resource, waiting in batches.items():
            held = self.locks.holder(resource)
            candidates = [item[0] for item in waiting]
            if held and held.asset_id not in {p.asset_id for p in candidates}:
                for proposal, decision, _ in waiting:
                    decision.verdict = Verdict.DENIED
                    decision.reason = f"{resource} 는 {held.asset_id} 가 쓰는 중입니다"
                    self.ledger.close_entry(self.ledger.open_entry(proposal, decision), "denied")
                continue

            winner, how = self.arbiter.choose(candidates, self.telemetry)
            for proposal, decision, _ in waiting:
                if proposal.id == winner.id:
                    decision.arbiter = how if len(candidates) > 1 else None
                    self.committer.commit(proposal, decision)
                    if decision.committed:
                        self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
                else:
                    decision.verdict = Verdict.DENIED
                    decision.arbiter = how
                    decision.reason = f"{winner.asset_id} 가 {resource} 를 받았습니다"
                    self.ledger.close_entry(self.ledger.open_entry(proposal, decision), "denied")

    # ---------- 바깥에서 오는 소식 ----------

    def _pull_world(self) -> None:
        state = self.adapter.telemetry()
        if state:
            self.tick = state.get("tick", self.tick)
            self.telemetry = state.get("assets", {})

        bulletins = get_json(f"{self.sim_url}/bulletins?world=guarded") or {}
        known = {p.id for p in self.policies.all()}
        for item in bulletins.get("bulletins", []):
            if item.get("kind") not in ("recall", "zone") or item["id"] in known:
                continue
            # 제한하는 정책은 즉시 걸립니다. 푸는 정책만 사람이 풉니다.
            policy = config_module.Policy(
                id=item["id"],
                reason=item.get("reason", item["kind"]),
                forbid_action=item.get("forbid_action"),
                forbid_resource=item.get("forbid_resource"),
                applies_to=item.get("applies_to", {}),
                active_from_tick=0,
            )
            self.policies.add(policy)
            self.revoke_under(policy)

    def background(self) -> None:
        while True:
            self._pull_world()
            self._settle_contended()
            time.sleep(0.25)

    def revoke_under(self, policy) -> Decision | None:
        """금지가 도착했는데 이미 그 자원을 잡고 있으면 뺏고 회항시킵니다.

        거절만 하는 것과 이게 다릅니다. 강제점이 있으면 이미 벌어진 일도 되돌립니다.
        회항은 한도를 보지 않고 나갑니다. 닫힌 구역에서 빠져나오는 건 예산 문제가
        아닙니다. 대신 누가 왜 시켰는지는 원장에 그대로 남습니다.
        """
        if not policy.forbid_resource:
            return None
        hold = self.locks.holder(policy.forbid_resource)
        if hold is None:
            return None

        self.locks.release(policy.forbid_resource, hold.asset_id)
        retreat = Proposal(
            asset_id=hold.asset_id,
            action="divert_ground",
            cost_usd=35.0,
            blast_radius="cargo",
            rationale=f"{policy.reason} ({policy.id})",
            author="runtime",
        )
        decision = Decision(
            retreat.id, Verdict.AUTO, f"{policy.id} 로 {policy.forbid_resource} 회수",
            policy_hit=policy.id,
        )
        self._decisions[retreat.id] = decision
        return self.committer.commit(retreat, decision)

    # ---------- 화면에 보여줄 것 ----------

    def snapshot(self) -> dict:
        with self._guard:
            pending = [p.to_dict() for p in self._awaiting_human.values()]
            waiting = {r: len(v) for r, v in self._contended.items()}
        return {
            "tick": self.tick,
            "config": self.config.name,
            "llm": {"enabled": self.llm.enabled, "models": self.llm.models},
            "locks": self.locks.snapshot(),
            "contended": waiting,
            "awaiting_human": pending,
            "policies": [vars(p) for p in self.policies.all()],
            "spend": {
                "fleet": self.authority.fleet_spend,
                "fleet_limit": self.config.authority.fleet_usd,
                "per_asset_limit": self.config.authority.per_asset_usd,
                "by_asset": {
                    asset: self.authority.asset_spend(asset) for asset in self.telemetry
                },
            },
            "ledger": self.ledger.tail(25),
        }


def main() -> None:
    runtime = Runtime(
        config_path=os.getenv("CONFIG", "configs/fleet.yaml"),
        sim_url=os.getenv("SIM_URL", "http://sim:8100"),
        ledger_path=os.getenv("LEDGER_PATH", "ledger.jsonl"),
        window_s=float(os.getenv("ARBITRATION_WINDOW_S", "1.5")),
    )
    threading.Thread(target=runtime.background, daemon=True).start()

    server = JsonServer(int(os.getenv("PORT", "8000")))
    server.add("POST", "/proposals", lambda body, query: (200, runtime.file(body).to_dict()))
    server.add(
        "POST",
        "/approve",
        lambda body, query: _approval(runtime, body, allow=True),
    )
    server.add("POST", "/deny", lambda body, query: _approval(runtime, body, allow=False))
    server.add("GET", "/state", lambda body, query: (200, runtime.snapshot()))
    server.add(
        "GET",
        "/telemetry/{asset}",
        lambda body, query, asset: (200, runtime.telemetry.get(asset, {})),
    )
    server.add("GET", "/health", lambda body, query: (200, {"ok": True, "tick": runtime.tick}))
    print(f"runtime listening on :{os.getenv('PORT', '8000')}", flush=True)
    server.serve_forever()


def _approval(runtime: Runtime, body: dict, allow: bool):
    decision = runtime.approve(
        body.get("proposal_id", ""), body.get("actor", "관제사"), allow=allow
    )
    if decision is None:
        return 404, {"error": "그런 신청서가 없습니다"}
    return 200, decision.to_dict()


if __name__ == "__main__":
    main()
