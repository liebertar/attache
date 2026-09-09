"""The runtime process. Holds locks, limits, the arbiter, the single commit path, the ledger."""

import os
import threading
import time

from attache.adapters import build as build_adapter
from attache.core import config as config_module
from attache.core.geo import Airspace, Volume, first_breach, nearest_exit
from attache.core.route import Router
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
        self.pad_coords: dict[str, tuple[float, float]] = {}
        self.window_s = window_s
        self.tick = 0
        self.telemetry: dict = {}
        self.airspace = Airspace()
        self.router = Router(self.airspace)
        self.zone_volumes: set[str] = set()   # 공지로 들어온 구역. 끝나면 빼야 합니다
        self._round = None                    # 시뮬레이터가 판을 새로 시작하면 따라갑니다
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
            decision = Decision(proposal.id, Verdict.DENIED, "직전에 같은 신청이 실행됐습니다",
                                code="duplicate")
            self._decisions[proposal.id] = decision
            return decision

        blocked = self.check_route(proposal)
        if blocked:
            decision = Decision(proposal.id, Verdict.DENIED, blocked, policy_hit="airspace",
                                forbids=proposal.params.get("blocked_volume"),
                                code="airspace")
            self._decisions[proposal.id] = decision
            self.ledger.close_entry(self.ledger.open_entry(proposal, decision), "denied")
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

    def check_route(self, proposal: Proposal) -> str | None:
        """받은 경로가 규정에 맞나. 경로를 그리는 건 우리 일이 아닙니다.

        운영사가 자기 기체와 자기 일정을 알고 길을 그립니다. 우리가 하는 건 그 길이
        허용되는지 답하는 것뿐이고, 안 되면 어느 구간의 어느 구역 때문인지 말해줍니다.
        길을 대신 그려주면 그 순간 우리가 운영사가 되고, 잘못된 길의 책임도 우리 것이
        됩니다. 권한과 실행은 나뉘어 있어야 합니다.
        """
        if proposal.action not in ("reserve_pad", "fly_route"):
            return None
        legs = proposal.params.get("legs")
        if not legs:
            return None if not self.airspace.all() else "경로를 같이 내야 합니다"

        found = first_breach(self.airspace, legs)
        if found is not None:
            segment, volume, why, at = found
            # 무엇이 왜 막혔는지를 값으로 남깁니다. 화면이 문장을 다시 뜯으면
            # 문구를 고칠 때마다 화면이 조용히 깨집니다.
            proposal.params = {
                **proposal.params,
                "blocked_volume": volume.id,
                "blocked_leg": segment,
                "blocked_name": volume.name,
                "blocked_floor_m": volume.floor_m,
                "blocked_kind": volume.rule,
                "blocked_at": {"lat": round(at[0], 6), "lon": round(at[1], 6)},
                "blocked_polygon": [[lat, lon] for lat, lon in volume.polygon],
                "blocked_ceiling_m": volume.ceiling_m,
            }
            return f"{segment}번 구간이 규정을 어깁니다 — {why}"
        # 경로의 끝은 내려앉는 자리입니다. 옆으로 지나갈 수 있는 길과 수직으로 내려올 수 있는 자리는
        # 다른 기준이라, 끝점 둘레(LANDING_SEPARATION_M)에 건물·금지 구역이 없는지 따로 봅니다.
        last = legs[-1]
        landing = self.airspace.landing_breach(float(last["lat"]), float(last["lon"]))
        if landing is not None:
            volume, gap = landing
            proposal.params = {
                **proposal.params,
                "blocked_volume": volume.id, "blocked_leg": len(legs) - 1,
                "blocked_name": volume.name, "blocked_floor_m": volume.floor_m,
                "blocked_kind": "landing",
                "blocked_at": {"lat": round(float(last["lat"]), 6),
                               "lon": round(float(last["lon"]), 6)},
                "blocked_polygon": [[lat, lon] for lat, lon in volume.polygon],
                "blocked_ceiling_m": volume.ceiling_m,
            }
            return f"착륙 지점 둘레에 {volume.name} ({gap:.0f}m) — 내려앉을 수 없습니다"
        return None

    def _queue_or_commit(self, proposal: Proposal, decision: Decision) -> Decision:
        if not proposal.resource:
            committed = self.committer.commit(proposal, decision)
            if committed.committed:
                self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
            return committed

        with self._guard:
            waiting = self._contended.setdefault(proposal.resource, [])
            standing = next(
                (d for p, d, _ in waiting if p.asset_id == proposal.asset_id), None
            )
            if standing is not None:
                # 이미 줄을 서 있습니다. 같은 기체가 같은 자원으로 두 번 서지 않습니다.
                return standing
            waiting.append((proposal, decision, time.time()))

        decision.verdict = Verdict.QUEUED
        decision.reason = f"{proposal.resource} 배정을 기다리는 중"
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
                    decision.code = "resource_held"
                    decision.detail = {"resource": resource, "holder": held.asset_id}
                    self.ledger.close_entry(self.ledger.open_entry(proposal, decision), "denied")
                continue

            winner, how = self.arbiter.choose(candidates, self.telemetry)
            for proposal, decision, _ in waiting:
                if proposal.id == winner.id:
                    decision.arbiter = how if len(candidates) > 1 else None
                    decision.verdict = Verdict.AUTO
                    decision.reason = f"{resource} 배정됨"
                    decision.code = "resource_granted"
                    decision.detail = {"resource": resource}
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
            self._follow_round(state.get("round"))

        if not self.airspace.all():
            world = get_json(f"{self.sim_url}/state?world=guarded&volumes=1") or {}
            for raw in world.get("volumes", []):
                self.airspace.add(Volume.from_dict(raw))
            self.pad_coords = {
                name: (at["lat"], at["lon"])
                for name, at in (world.get("pad_coords") or {}).items()
            }

        bulletins = get_json(f"{self.sim_url}/bulletins?world=guarded") or {}
        self.absorb(bulletins.get("bulletins", []))

    def _follow_round(self, round_number) -> None:
        """판이 바뀌면 한 판짜리 상태를 비웁니다 — 예산, 잠금, 중복 방지.

        원장은 남습니다. 그건 역사이고, 판이 바뀐다고 없던 일이 되지 않습니다.
        공지 정책도 비웁니다. 같은 id 로 다시 게시되면 그때 다시 걸립니다.
        """
        if round_number is None or round_number == self._round:
            return
        self._round = round_number
        self.authority.new_round()
        for asset_id in list(self.telemetry):
            self.locks.release_all(asset_id)
        self._recent_commits.clear()
        for volume_id in self.zone_volumes:
            self.airspace.remove(volume_id)
        self.zone_volumes.clear()
        self.policies.clear()

    def absorb(self, bulletins: list[dict]) -> None:
        """공지를 규칙으로 받습니다. 구역 공지는 공역에도 넣습니다.

        자원만 막고 공역을 그대로 두면, 닫힌 구역을 지나는 경로가 계속 승인됩니다.
        기지 위에 구역이 닫혔는데 거기로 날아가는 경로가 통과하던 게 그래서였습니다.
        유효기간이 끝나 공지에서 빠지면 판정 기준에서도 빠집니다.
        """
        items = [i for i in bulletins if i.get("kind") in ("recall", "zone")]
        known = {p.id for p in self.policies.all()}
        for item in items:
            if item["id"] in known:
                continue
            if item.get("polygon"):
                volume = Volume.from_dict(item)
                self.airspace.add(volume)
                self.zone_volumes.add(item["id"])
                self.recall_flights(volume)
            # 제한하는 정책은 즉시 걸립니다. 푸는 정책만 사람이 풉니다.
            policy = config_module.Policy(
                id=item["id"],
                reason=item.get("reason", item["kind"]),
                forbid_action=item.get("forbid_action"),
                forbid_resource=item.get("forbid_resource"),
                applies_to=item.get("applies_to", {}),
                active_from_tick=0,
                active_until_tick=item.get("until_tick"),
            )
            self.policies.add(policy)
            self.revoke_under(policy)
        for expired in self.zone_volumes - {i["id"] for i in items}:
            self.airspace.remove(expired)
            self.zone_volumes.discard(expired)

    def background(self) -> None:
        while True:
            # 한 번 실패해도 다음 주기에 다시 봅니다. 이 스레드가 죽으면 런타임은 옛 세계를
            # 보면서 판정하게 되고, 그건 조용히 틀리는 최악의 상태입니다.
            try:
                self._pull_world()
                self._settle_contended()
            except Exception as error:  # noqa: BLE001 — 살아남는 것이 먼저입니다
                print(f"runtime background: {error!r}", flush=True)
            time.sleep(0.25)

    def recall_flights(self, volume) -> list[Decision]:
        """이미 승인해서 날고 있는 경로를 새 구역으로 다시 판정합니다.

        거절만으로는 부족합니다. 규칙이 도착하기 전에 승인한 비행은 그 규칙을 모르고
        계속 날아갑니다. 강제점이 있다는 말은 이미 벌어진 일도 되돌린다는 뜻입니다.
        """
        pulled = []
        for asset_id, telemetry in self.telemetry.items():
            legs = [{"lat": telemetry.get("lat"), "lon": telemetry.get("lon"),
                     "alt_m": telemetry.get("alt_m", 0.0)}]
            legs += [{"lat": leg["lat"], "lon": leg["lon"], "alt_m": leg.get("alt_m", 0.0)}
                     for leg in (telemetry.get("route") or [])]
            if len(legs) < 2 or legs[0]["lat"] is None:
                continue
            if first_breach(Airspace([volume], default_ceiling_m=None), legs) is None:
                continue
            # 안에 있던 기체는 가장 가까운 바깥으로 내보냅니다. 제자리에 세워두면 닫힌 구역
            # 안에 머무는 것이고, 거기서는 어떤 경로도 출발점부터 금지라 다시 그릴 수 없습니다.
            exit_point = nearest_exit(volume, legs[0]["lat"], legs[0]["lon"])
            params = ({"exit": {"lat": exit_point[0], "lon": exit_point[1]}, "volume": volume.id}
                      if exit_point else {"volume": volume.id})
            retreat = Proposal(
                asset_id=asset_id, action="divert_ground", cost_usd=35.0,
                blast_radius="cargo", rationale=f"{volume.name} ({volume.id})",
                author="runtime", params=params,
            )
            decision = Decision(
                retreat.id, Verdict.AUTO,
                f"{volume.id} 로 비행 중이던 경로를 회수",
                policy_hit=volume.id, code="recalled",
                detail={"resource": asset_id, "policy": volume.id},
            )
            # 조종장치에는 원장 번호(문자열)가 갑니다. 원장 항목 객체를 그대로 넘겼더니 HTTP
            # 어댑터가 JSON 으로 못 만들어 배경 스레드가 죽었고, 그 뒤로 런타임이 옛 위치를
            # 계속 내보내서 모든 신청이 엉뚱한 자리에서 시작됐습니다. 로컬 어댑터만 쓰는
            # 시험은 못 잡았습니다.
            entry = self.ledger.open_entry(retreat, decision)
            decision.ledger_id = entry.id
            result = self.adapter.execute(asset_id, "divert_ground", params, entry.id,
                                          blast=retreat.blast_radius)
            ok = bool(result.get("ok"))
            self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}",
                                    decision)
            pulled.append(decision)
        return pulled

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
            policy_hit=policy.id, code="recalled",
            detail={"resource": policy.forbid_resource, "policy": policy.id},
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
    # 공역 판본을 같이 보냅니다. 구역이 새로 닫히면 운영사가 사본을 갱신하고 처음부터
    # 피해서 그리게 — 안 그러면 닫힌 구역으로 직선을 내고 거절당한 뒤에야 압니다.
    server.add(
        "GET",
        "/telemetry/{asset}",
        lambda body, query, asset: (200, {**runtime.telemetry.get(asset, {}),
                                          "airspace_revision": runtime.airspace.revision}
                                    if asset in runtime.telemetry else {}),
    )
    server.add("GET", "/airspace", lambda body, query: (200, {
        "volumes": [v.to_dict() for v in runtime.airspace.all()],
        "pads": {n: {"lat": a[0], "lon": a[1]} for n, a in runtime.pad_coords.items()},
    }))
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
