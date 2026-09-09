"""Each mechanism on its own, without depending on a 420-tick scenario going a certain way.

A full run is good for the structural guarantees. It is a bad place to assert that a
particular event happened, because the moment the scenario shifts the test starts failing
for reasons that have nothing to do with the code being wrong.
"""

import tempfile
import unittest

from attache.core.config import Authority, Policy
from attache.core.models import Proposal, Verdict
from attache.runtime.authority import AuthorityCheck
from attache.runtime.policy import PolicyBook
from attache.runtime.service import Runtime
from sim.world import RECALL, RECALL_TICK, Simulation


def proposal(**kwargs) -> Proposal:
    base = dict(asset_id="drone-01", action="fast_charge", cost_usd=60.0,
                blast_radius="none", rationale="배터리 12%")
    return Proposal(**{**base, **kwargs})


class RecallTest(unittest.TestCase):
    """공지가 도착한 순간부터 막느냐, 각자 확인할 때까지 기다리느냐."""

    def setUp(self):
        self.policies = PolicyBook()
        self.check = AuthorityCheck(Authority(320, 720), self.policies)
        self.recall = Policy(RECALL["id"], RECALL["reason"],
                             forbid_action=RECALL["forbid_action"],
                             applies_to=RECALL["applies_to"])
        self.asset = {"model": "dv-x500"}

    def test_before_the_recall_it_clears(self):
        self.assertIs(self.check.evaluate(proposal(), self.asset, 0).verdict, Verdict.AUTO)

    def test_the_tick_the_recall_lands_it_stops(self):
        self.policies.add(self.recall)
        decision = self.check.evaluate(proposal(), self.asset, 0)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, RECALL["id"])
        self.assertEqual(decision.forbids, "fast_charge")

    def test_an_agent_polling_on_its_own_has_a_window(self):
        """강제점이 없으면 공지와 이행 사이가 빕니다. 그 창이 위험의 크기입니다."""
        # 공지 시점은 sim 이 정합니다. 여기에 숫자를 박아두면 시나리오를 조정할 때
        # 시험이 조용히 다른 순간을 보게 됩니다.
        published_at, poll_every = RECALL_TICK, 25
        simulation = Simulation()
        world = simulation.worlds["direct"]
        for _ in range(published_at + 5):
            simulation.step()
        # 공지는 나갔고, 기체는 아직 다음 폴링 전입니다
        self.assertTrue(any(b["id"] == RECALL["id"] for b in simulation.bulletins()))
        world.vehicles["drone-01"].state = "landed"   # 충전은 패드 위에서만 됩니다
        result = world.act("drone-01", "fast_charge", {}, None, "none", None,
                           simulation.tick_count)
        self.assertGreater(world.score.post_recall_violations, 0,
                           "강제점이 없으면 이 창에서 금지 행동이 실제로 나갑니다")
        del result, poll_every


class PassengerGateTest(unittest.TestCase):
    def setUp(self):
        self.check = AuthorityCheck(
            Authority(320, 720, human_required_blast=["passenger", "public"],
                      human_required_actions=["disengage_autonomy"]),
            PolicyBook(),
        )

    def test_a_free_action_still_needs_a_human_when_passengers_are_aboard(self):
        decision = self.check.evaluate(
            proposal(action="disengage_autonomy", cost_usd=0.0, blast_radius="passenger"),
            {}, 0,
        )
        self.assertIs(decision.verdict, Verdict.HUMAN)

    def test_the_actuator_records_it_as_unapproved_when_nobody_looked(self):
        world = Simulation().worlds["direct"]
        world.act("drone-01", "disengage_autonomy", {}, None, "passenger", None, 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 1)

    def test_and_not_when_someone_did(self):
        world = Simulation().worlds["guarded"]
        world.act("drone-01", "disengage_autonomy", {}, "l_1", "passenger", "관제사", 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 0)
        self.assertEqual(world.score.human_approvals, 1)


class RevocationTest(unittest.TestCase):
    """거절하는 것과 이미 벌어진 일을 되돌리는 것은 다릅니다."""

    def test_a_ban_takes_back_a_resource_already_held(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

        class LocalAdapter:
            def __init__(self):
                self.sent = []

            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                self.sent.append((asset_id, action))
                return {"ok": True}

            def telemetry(self):
                return {}

        adapter = LocalAdapter()
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        runtime.locks.acquire("pad:P1", "drone-01", "p_1")

        decision = runtime.revoke_under(
            Policy("nofly-x", "응급헬기", forbid_resource="pad:P1")
        )
        self.assertIsNotNone(decision)
        self.assertIn(("drone-01", "divert_ground"), adapter.sent)
        self.assertIsNone(runtime.locks.holder("pad:P1"))
        self.assertEqual(decision.policy_hit, "nofly-x")

    def test_it_does_nothing_when_nobody_holds_it(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
        self.assertIsNone(
            runtime.revoke_under(Policy("nofly-y", "x", forbid_resource="pad:P9"))
        )


if __name__ == "__main__":
    unittest.main()


class PadContentionTest(unittest.TestCase):
    """착륙 패드는 한 번에 한 대입니다. 그걸 지키는 게 무엇인가.

    직결 배선에는 지킬 것이 없습니다. 두 기체가 같은 순간에 같은 패드를 고르면
    둘 다 갑니다 — 서로의 예약 의도를 볼 방법이 없어서입니다. 두 세계를 오래 돌려서
    이 순간이 우연히 오기를 기다리는 대신, 여기서 그 순간을 직접 만듭니다.
    """

    def test_without_a_lock_table_two_vehicles_take_the_same_pad(self):
        from sim.world import Simulation

        world = Simulation(lock_actuator=False).worlds["direct"]
        for asset in ("drone-01", "drone-02"):
            vehicle = world.vehicles[asset]
            vehicle.battery = 40.0
            result = world.act(asset, "reserve_pad", {"pad": "bay:A"}, None, "schedule",
                               None, 1)
            self.assertTrue(result["ok"], "조종장치가 아무나 받습니다")
            vehicle.state = "landed"
            vehicle.assigned_pad = "bay:A"

        world._detect_pad_conflicts(2)
        self.assertGreater(world.score.pad_conflicts, 0)

    def test_a_lock_table_hands_the_pad_to_one_of_them(self):
        from attache.runtime.locks import LockTable

        locks = LockTable(["bay:A", "bay:B"])
        self.assertTrue(locks.acquire("bay:A", "drone-01", "p1"))
        self.assertFalse(locks.acquire("bay:A", "drone-02", "p2"))
        self.assertEqual(locks.holder("bay:A").asset_id, "drone-01")


class RoundResetTest(unittest.TestCase):
    """판이 바뀌면 한 판짜리 상태도 새로 시작해야 합니다.

    화면을 켜두면 시뮬레이터가 알아서 다음 판을 시작합니다. 그때 런타임의 예산이
    누적으로 남아 있으면 두 번째 판부터는 한도가 다 차서 아무것도 승인되지 않습니다.
    가드 쪽만 멈추고 직결 쪽은 계속 날아서, 데모가 정반대를 말하게 됩니다.
    """

    def _runtime(self):
        import tempfile

        from attache.runtime.service import Runtime

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            return Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

    def test_a_new_round_hands_the_budget_back(self):
        from attache.core.models import Proposal

        runtime = self._runtime()
        runtime._follow_round(0)
        limit = runtime.authority.authority.fleet_usd
        while runtime.authority.fleet_spend < limit:
            runtime.authority.record_spend(
                Proposal(asset_id="drone-01", action="charge", cost_usd=22.0,
                         blast_radius="none", rationale="")
            )
        self.assertGreaterEqual(runtime.authority.fleet_spend, limit)

        runtime._follow_round(1)
        self.assertEqual(runtime.authority.fleet_spend, 0.0)
        self.assertEqual(runtime.authority.asset_spend("drone-01"), 0.0)

    def test_a_new_round_lets_go_of_pads_and_stale_bans(self):
        from attache.core.config import Policy

        runtime = self._runtime()
        runtime.telemetry = {"drone-01": {}}
        runtime._follow_round(0)
        runtime.locks.acquire("bay:A", "drone-01", "p_1")
        runtime.policies.add(Policy("nofly-x", "지난 판의 공지", forbid_resource="bay:A"))

        runtime._follow_round(1)
        self.assertIsNone(runtime.locks.holder("bay:A"),
                          "기체가 새로 세워졌는데 지난 판의 예약이 남으면 아무도 못 씁니다")
        self.assertEqual(runtime.policies.all(), [])

    def test_the_same_round_is_not_a_reset(self):
        from attache.core.models import Proposal

        runtime = self._runtime()
        runtime._follow_round(3)
        runtime.authority.record_spend(
            Proposal(asset_id="drone-01", action="charge", cost_usd=22.0,
                     blast_radius="none", rationale="")
        )
        runtime._follow_round(3)
        self.assertEqual(runtime.authority.fleet_spend, 22.0)
