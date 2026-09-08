"""What makes the runtime binding rather than advisory.

Network isolation and separate images stop an agent that plays by the rules. The thing
that stops one that does not is the actuator refusing to move without an authorization
receipt. The ledger id is that receipt: the runtime writes the entry before it acts, and
the entry id travels with the command.
"""

import unittest

from sim.world import Simulation


class ReceiptTest(unittest.TestCase):
    def test_an_open_actuator_obeys_anyone(self):
        world = Simulation(lock_actuator=False).worlds["direct"]
        result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, None, "schedule",
                           None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.unrecorded_actions, 1)

    def test_a_locked_actuator_refuses_a_command_with_no_receipt(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, None, "schedule",
                           None, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 1)
        self.assertEqual(world.vehicles["taxi-a"].assigned_pad, None)

    def test_a_locked_actuator_still_obeys_the_runtime(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, "l_abc123",
                           "schedule", None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 0)
        self.assertEqual(world.score.unrecorded_actions, 0)

    def test_locking_costs_nothing_when_everyone_already_goes_through(self):
        """런타임을 거치는 쪽은 잠그든 안 잠그든 결과가 같습니다."""
        for locked in (False, True):
            world = Simulation(lock_actuator=locked).worlds["guarded"]
            result = world.act("taxi-a", "reserve_pad", {"pad": "pad:P1"}, "l_1",
                               "schedule", None, 1)
            self.assertTrue(result["ok"], f"locked={locked}")


if __name__ == "__main__":
    unittest.main()


class LedgerTruthTest(unittest.TestCase):
    """원장이 거짓을 적으면 원장이 아닙니다."""

    def test_the_closing_entry_reports_what_actually_happened(self):
        import tempfile

        from attache.core.models import Decision, Proposal, Verdict
        from attache.runtime.authority import AuthorityCheck
        from attache.runtime.commit import Committer
        from attache.runtime.ledger import Ledger
        from attache.runtime.locks import LockTable
        from attache.runtime.policy import PolicyBook
        from attache.core.config import Authority

        simulation = Simulation()
        world = simulation.worlds["guarded"]

        class LocalAdapter:
            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                return world.act(asset_id, action, params, ledger_id, blast, approved_by, 1)

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            ledger = Ledger(handle.name)
        authority = AuthorityCheck(Authority(200, 500), PolicyBook())
        committer = Committer(LocalAdapter(), LockTable(["pad:P1"]), ledger, authority)

        proposal = Proposal(asset_id="taxi-a", action="reserve_pad", cost_usd=28.0,
                            blast_radius="schedule", rationale="", resource="pad:P1",
                            params={"pad": "pad:P1"})
        committer.commit(proposal, Decision(proposal.id, Verdict.AUTO, "한도 안"))

        closed = [e for e in ledger.tail(10) if e["outcome"] != "pending"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["outcome"], "done")
        self.assertTrue(closed[0]["decision"]["committed"],
                        "결과는 done 인데 실행 안 했다고 적혀 있습니다")
        self.assertIsNotNone(closed[0]["decision"]["ledger_id"])
