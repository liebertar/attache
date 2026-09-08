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
from sim.world import RECALL, Simulation


def proposal(**kwargs) -> Proposal:
    base = dict(asset_id="taxi-a", action="fast_charge", cost_usd=60.0,
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
        self.asset = {"model": "robotaxi-v3"}

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
        published_at, poll_every = 30, 25
        simulation = Simulation()
        world = simulation.worlds["direct"]
        for _ in range(published_at + 5):
            simulation.step()
        # 공지는 나갔고, 기체는 아직 다음 폴링 전입니다
        self.assertTrue(any(b["id"] == RECALL["id"] for b in simulation.bulletins()))
        world.vehicles["taxi-a"].state = "landed"   # 충전은 패드 위에서만 됩니다
        result = world.act("taxi-a", "fast_charge", {}, None, "none", None,
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
        world.act("taxi-a", "disengage_autonomy", {}, None, "passenger", None, 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 1)

    def test_and_not_when_someone_did(self):
        world = Simulation().worlds["guarded"]
        world.act("taxi-a", "disengage_autonomy", {}, "l_1", "passenger", "관제사", 1)
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
        runtime.locks.acquire("pad:P1", "taxi-a", "p_1")

        decision = runtime.revoke_under(
            Policy("nofly-x", "응급헬기", forbid_resource="pad:P1")
        )
        self.assertIsNotNone(decision)
        self.assertIn(("taxi-a", "divert_ground"), adapter.sent)
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
