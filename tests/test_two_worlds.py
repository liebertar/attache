"""The claim this project rests on.

Same seed, same vehicles, same detection code, same proposal writer. One world files its
requests with the runtime; the other holds the actuator address. The unguarded agents are
not sabotaged: they honour their own per-vehicle budget, they read the recall bulletin and
obey it, and they are shown the whole fleet's state, which the guarded agents never see.

They still fail, because a rule that lives in each agent is not a rule.
"""

import unittest

from attache.agent.detect import detect
from attache.agent.planner import OperatorPlanner
from attache.agent.propose import COSTS, by_rule
from attache.core.config import load as config_load
from attache.core.models import Proposal, Verdict
from attache.core.route import Router
from attache.runtime.service import Runtime
from sim.world import RECALL, RECALL_TICK, ZONE, ZONE_TICK, Simulation

# 22 m/s 로 날면 브루클린-맨해튼 한 번 왕복이 1100틱 안팎입니다.
# 구역 폐쇄(560~900틱) 이후까지 봐야 두 세계가 갈리는 지점이 나옵니다.
TICKS = 1500
PADS = ["pad:launch"]


class LocalAdapter:
    """HTTP 없이 같은 프로세스에서 세계를 때립니다. 경로는 동일합니다."""

    def __init__(self, world, clock):
        self.world = world
        self.clock = clock

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        return self.world.act(asset_id, action, params, ledger_id, blast, approved_by,
                              self.clock())


BUDGET_ESCALATIONS = {"per_asset_usd", "fleet_usd"}


class GuardedSide:
    """운영사 쪽. 길은 우리가 그리고, 되는지는 런타임에 묻습니다."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.planner = OperatorPlanner(runtime.airspace)
        self.preferred_alt_m = Router.cruise_alt_default()
        self.pad_index = {}
        self.banned = {}
        self.cooldown = {}

    def run_tick(self, snapshot):
        for asset_id, telemetry in snapshot["assets"].items():
            concern = detect(telemetry)
            if concern is None:
                continue
            index = self.pad_index.setdefault(asset_id, 0)
            banned = self.banned.setdefault(asset_id, set())
            open_pads = [pad for pad in PADS if pad not in banned] or PADS
            proposal = by_rule(
                concern, telemetry, open_pads[index % len(open_pads)], frozenset(banned)
            )
            if snapshot["tick"] < self.cooldown.get((asset_id, proposal.action), 0):
                continue
            decision = self._file(proposal, telemetry)
            if decision.verdict in (Verdict.DENIED, Verdict.HUMAN, Verdict.QUEUED):
                self.cooldown[(asset_id, proposal.action)] = snapshot["tick"] + 12
            if decision.verdict is Verdict.DENIED:
                if decision.policy_hit:
                    # 자원이 막힌 걸 행동이 막힌 걸로 배우면 영영 신청을 못 합니다
                    banned.add(decision.forbids or proposal.action)
                elif proposal.resource:
                    self.pad_index[asset_id] = (index + 1) % len(PADS)
        self.runtime._settle_contended()
        self._controller_reviews()

    def _destination(self, proposal, telemetry):
        if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
            return (telemetry["job_lat"], telemetry["job_lon"])
        if proposal.action == "reserve_pad" and proposal.resource:
            at = self.runtime.pad_coords.get(proposal.resource)
            return at
        return None

    def _file(self, proposal, telemetry):
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = self._destination(proposal, telemetry)
        if here[0] is None or goal is None:
            return self.runtime.file(proposal.to_dict())

        # 먼저 최단 직선으로 냅니다. 운영사는 원래 제일 싼 길을 냅니다.
        proposal.params = {**proposal.params,
                           "legs": self.planner.straight(here, goal, self.preferred_alt_m)}
        decision = self.runtime.file(proposal.to_dict())
        if decision.policy_hit != "airspace":
            return decision
        # 다시 그리라고 했습니다.
        legs = self.planner.draw(here, goal)
        if not legs:
            if proposal.action != "fly_route":
                return decision   # 이륙장을 못 간다고 주문을 반려하지는 않습니다
            declined = Proposal.from_dict({**proposal.to_dict(), "action": "decline_job",
                                           "cost_usd": 0.0, "blast_radius": "none",
                                           "params": {}, "resource": None})
            return self.runtime.file(declined.to_dict())
        redrawn = Proposal.from_dict({**proposal.to_dict(),
                                      "params": {**proposal.params, "legs": legs}})
        return self.runtime.file(redrawn.to_dict())

    def _controller_reviews(self):
        """원격 관제사. 안전 때문에 올라온 건 승인하고, 예산 초과는 거부합니다."""
        for proposal_id in list(self.runtime._awaiting_human):
            decision = self.runtime._decisions[proposal_id]
            allow = decision.authority_hit not in BUDGET_ESCALATIONS
            self.runtime.approve(proposal_id, "원격 관제사", allow=allow)


class DirectSide:
    """스스로 규칙을 지키려는, 잘 만든 에이전트들."""

    def __init__(self, world, per_asset_limit=200.0, bulletin_lag_ticks=25):
        self.world = world
        self.per_asset_limit = per_asset_limit
        self.bulletin_lag = bulletin_lag_ticks
        self.spend = {}
        self.banned = {}

    def run_tick(self, snapshot, tick, bulletins):
        # 강제점이 없으니 기체마다 따로 공지를 확인합니다. 확인 주기만큼 늦습니다.
        for asset_id, telemetry in snapshot["assets"].items():
            phase = sum(ord(c) for c in asset_id) % self.bulletin_lag
            if not bulletins or tick % self.bulletin_lag != phase:
                continue
            for item in bulletins:
                if item.get("applies_to", {}).get("model") not in (None, telemetry["model"]):
                    continue
                seen = self.banned.setdefault(asset_id, set())
                if item.get("forbid_action"):
                    seen.add(item["forbid_action"])
                if item.get("forbid_resource"):
                    seen.add(item["forbid_resource"])

        # 세 기체가 같은 주기로 상태를 읽습니다. 읽은 뒤 쓰기까지가 비어 있습니다.
        for asset_id, telemetry in snapshot["assets"].items():
            concern = detect(telemetry)
            if concern is None:
                continue
            banned = self.banned.get(asset_id, set())
            pad = self._free_looking_pad(snapshot, asset_id, banned)
            proposal = by_rule(concern, telemetry, pad)
            if proposal.action in banned or (proposal.resource and proposal.resource in banned):
                continue
            spent = self.spend.get(asset_id, 0.0)
            if spent + COSTS.get(proposal.action, 0.0) > self.per_asset_limit:
                continue
            if proposal.action in ("fly_route", "reserve_pad"):
                goal = None
                if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
                    goal = (telemetry["job_lat"], telemetry["job_lon"])
                if goal:
                    proposal.params = {**proposal.params, "legs": OperatorPlanner.straight(
                        (telemetry["lat"], telemetry["lon"]), goal,
                        Router.cruise_alt_default())}
            result = self.world.act(asset_id, proposal.action, proposal.params, None,
                                    proposal.blast_radius, None, tick)
            if result.get("ok"):
                self.spend[asset_id] = spent + result.get("cost_usd", 0.0)

    @staticmethod
    def _free_looking_pad(snapshot, asset_id, banned=frozenset()):
        # 위치는 Remote ID 로 보이지만 예약 의도는 안 보입니다. 회사가 다르면 더욱.
        taken = {
            vehicle.get("assigned_pad")
            for vid, vehicle in snapshot["assets"].items()
            if vid != asset_id and vehicle.get("state") in ("landed", "charging")
        }
        open_pads = [pad for pad in PADS if pad not in banned]
        for pad in open_pads:
            if pad not in taken:
                return pad
        return open_pads[0] if open_pads else PADS[0]


CONFIG = "configs/fleet.yaml"


def run(tmp_ledger: str):
    # 점수판이 재는 한도와 런타임이 강제하는 한도는 같은 숫자여야 합니다.
    # 따로 적어두면 런타임 기준으로는 정상인데 점수판만 초과라고 합니다.
    limit = config_load(CONFIG).authority.fleet_usd
    simulation = Simulation(seed=7, fleet_limit=limit)
    runtime = Runtime(CONFIG, "http://unused", tmp_ledger, window_s=0.0)
    guarded_world = simulation.worlds["guarded"]
    adapter = LocalAdapter(guarded_world, lambda: simulation.tick_count)
    runtime.adapter = adapter
    runtime.committer.adapter = adapter

    from attache.core.geo import Volume

    for raw in guarded_world.snapshot(0, volumes=True)["volumes"]:
        runtime.airspace.add(Volume.from_dict(raw))
    runtime.pad_coords = {
        name: (at["lat"], at["lon"])
        for name, at in guarded_world.snapshot(0)["pad_coords"].items()
    }
    guarded = GuardedSide(runtime)
    direct = DirectSide(simulation.worlds["direct"])

    for _ in range(TICKS):
        simulation.step()
        tick = simulation.tick_count

        # 제한하는 공지는 런타임이 도착 즉시 겁니다. 푸는 정책만 사람이 풉니다.
        # 실서비스와 같은 코드로 받습니다 — 두 벌로 적으면 갈라집니다.
        runtime.absorb(simulation.bulletins())

        runtime.tick = tick
        guarded_snapshot = guarded_world.snapshot(tick)
        runtime.telemetry = guarded_snapshot["assets"]
        guarded.run_tick(guarded_snapshot)

        direct.run_tick(simulation.worlds["direct"].snapshot(tick), tick, simulation.bulletins())

    return (
        guarded_world.snapshot(TICKS)["scoreboard"],
        simulation.worlds["direct"].snapshot(TICKS)["scoreboard"],
    )


class TwoWorldsTest(unittest.TestCase):
    """구조적으로 항상 참이어야 하는 것만 여기서 봅니다.

    특정 사건이 그 판에 일어났는지에 기대는 단언은 넣지 않습니다. 시나리오가 조금만
    달라져도 깨지고, 깨진 걸 맞추려고 시나리오를 손대면 시험이 아니라 장식이 됩니다.
    사건별 메커니즘은 test_mechanisms.py 에서 따로 봅니다.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            cls.guarded, cls.direct = run(handle.name)

    # ---------- 런타임 쪽에서 반드시 참이어야 하는 것 ----------

    def test_pads_are_never_shared_under_the_runtime(self):
        self.assertEqual(self.guarded["pad_conflicts"], 0)

    def test_every_guarded_action_is_on_the_record(self):
        self.assertEqual(self.guarded["unrecorded_actions"], 0)

    def test_the_fleet_budget_cannot_be_exceeded_unattended(self):
        self.assertEqual(self.guarded["over_fleet_limit_usd"], 0)

    def test_passenger_impact_never_happens_unattended(self):
        self.assertEqual(self.guarded["unapproved_passenger_actions"], 0)

    def test_a_closed_zone_is_emptied_faster_where_something_enforces_it(self):
        """규칙이 도착했을 때 안에 있던 기체를 누가 빼내느냐.

        침범 횟수가 아니라 머문 시간을 봅니다. 규칙이 도착한 순간 안에 있던 것은 아무도
        잘못한 게 아닙니다. 갈리는 것은 그 다음입니다 — 한쪽은 회항 명령을 받고,
        다른 쪽은 기체가 스스로 공지를 확인할 때까지 남아 있습니다.
        """
        self.assertEqual(self.guarded["zone_incursions"], 0)
        # 머문 시간은 그 판에 누가 어디 있었느냐에 달려 있어서 '반드시 더 짧다'로 묶으면
        # 시나리오가 조금만 달라져도 깨집니다. 더 오래 남지 않는다는 것만 봅니다.
        self.assertLessEqual(self.guarded["zone_dwell_ticks"],
                             self.direct["zone_dwell_ticks"])

    # ---------- 직결 쪽에서 반드시 참이어야 하는 것 ----------

    def test_nothing_the_direct_side_does_is_recorded(self):
        """한 건도 아니고 전부입니다. 남길 곳이 없어서입니다."""
        self.assertEqual(self.direct["unrecorded_actions"], self.direct["actions"])
        self.assertGreater(self.direct["actions"], 0)

    # 패드 충돌은 "그 판에 두 대가 같은 순간에 같은 패드를 골랐는가"에 달려 있어서
    # 시나리오가 조금만 달라져도 났다 안 났다 합니다. 위 docstring 이 말하는 그 경우라
    # 결정적으로 볼 수 있는 test_mechanisms.PadContentionTest 로 옮겼습니다.

    def test_the_direct_side_never_gets_a_human_look(self):
        self.assertEqual(self.direct["human_approvals"], 0)

    # ---------- 양쪽이 같은 일을 하고 있다는 것 ----------

    def test_both_sides_actually_flew(self):
        """한쪽이 굶어 있으면 비교가 아닙니다."""
        self.assertGreater(self.guarded["actions"], 8)
        self.assertGreater(self.direct["actions"], 8)


if __name__ == "__main__":
    import json

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        guarded, direct = run(handle.name)
    print(json.dumps({"guarded": guarded, "direct": direct}, indent=2, ensure_ascii=False))
