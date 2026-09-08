"""The claim this project rests on.

Same seed, same vehicles, same detection code, same proposal writer. One world files its
requests with the runtime; the other holds the actuator address. The unguarded agents are
not sabotaged: they honour their own per-vehicle budget, they read the recall bulletin and
obey it, and they are shown the whole fleet's state, which the guarded agents never see.

They still fail, because a rule that lives in each agent is not a rule.
"""

import unittest

from attache.agent.detect import detect
from attache.agent.propose import COSTS, by_rule
from attache.core.config import Policy
from attache.core.models import Verdict
from attache.runtime.service import Runtime
from sim.world import RECALL, RECALL_TICK, ZONE, ZONE_TICK, Simulation

TICKS = 420
PADS = ["pad:P1", "pad:P2"]


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
    def __init__(self, runtime):
        self.runtime = runtime
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
            decision = self.runtime.file(proposal.to_dict())
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


def run(tmp_ledger: str):
    simulation = Simulation(seed=7, fleet_limit=500.0)
    runtime = Runtime("configs/fleet.yaml", "http://unused", tmp_ledger, window_s=0.0)
    guarded_world = simulation.worlds["guarded"]
    adapter = LocalAdapter(guarded_world, lambda: simulation.tick_count)
    runtime.adapter = adapter
    runtime.committer.adapter = adapter

    from attache.core.geo import Volume

    for raw in guarded_world.snapshot(0)["volumes"]:
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
        known = {p.id for p in runtime.policies.all()}
        for item in simulation.bulletins():
            if item["id"] in known:
                continue
            policy = Policy(
                id=item["id"],
                reason=item["reason"],
                forbid_action=item.get("forbid_action"),
                forbid_resource=item.get("forbid_resource"),
                applies_to=item.get("applies_to", {}),
            )
            runtime.policies.add(policy)
            runtime.revoke_under(policy)

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
    @classmethod
    def setUpClass(cls):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            cls.guarded, cls.direct = run(handle.name)

    def test_pads_are_never_shared_under_the_runtime(self):
        self.assertEqual(self.guarded["pad_conflicts"], 0)

    def test_pads_are_shared_without_it(self):
        self.assertGreater(self.direct["pad_conflicts"], 0)

    def test_fleet_budget_holds_only_where_something_holds_it(self):
        self.assertLessEqual(self.guarded["over_fleet_limit_usd"], 0)
        self.assertGreater(self.direct["spend_usd"], self.guarded["spend_usd"])

    def test_recall_binds_immediately_on_one_side_only(self):
        self.assertEqual(self.guarded["post_recall_violations"], 0)
        self.assertGreater(self.direct["post_recall_violations"], 0)

    def test_every_guarded_action_is_on_the_record(self):
        self.assertEqual(self.guarded["unrecorded_actions"], 0)
        self.assertEqual(self.direct["unrecorded_actions"], self.direct["actions"])

    def test_a_closed_zone_is_emptied_only_where_something_enforces_it(self):
        self.assertEqual(self.guarded["zone_incursions"], 0)
        self.assertGreater(self.direct["zone_incursions"], 0)

    def test_passenger_impact_never_happens_unattended(self):
        self.assertEqual(self.guarded["unapproved_passenger_actions"], 0)
        self.assertGreater(self.direct["unapproved_passenger_actions"], 0)


if __name__ == "__main__":
    import json

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        guarded, direct = run(handle.name)
    print(json.dumps({"guarded": guarded, "direct": direct}, indent=2, ensure_ascii=False))
