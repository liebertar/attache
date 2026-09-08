import unittest

from attache.core.models import Proposal
from attache.llm.client import TieredLlm
from attache.runtime.arbiter import Arbiter, by_rule
from attache.runtime.locks import LockTable


class StubLlm(TieredLlm):
    def __init__(self, text):
        super().__init__(base_url="http://stub", models={"ultra": "stub-ultra"})
        self.text = text

    def ask(self, tier, system, user, max_tokens=400):
        from attache.llm.client import LlmReply

        return LlmReply(text=self.text, model="stub-ultra")


def make(asset_id, blast, cost=18.0):
    return Proposal(asset_id=asset_id, action="reserve_pad", cost_usd=cost,
                    blast_radius=blast, rationale="", resource="pad:P1")


class LockTest(unittest.TestCase):
    def test_one_holder_only(self):
        locks = LockTable(["pad:P1"])
        self.assertTrue(locks.acquire("pad:P1", "taxi-a", "p1"))
        self.assertFalse(locks.acquire("pad:P1", "drone-b", "p2"))
        self.assertTrue(locks.acquire("pad:P1", "taxi-a", "p3"))

    def test_release_only_by_holder(self):
        locks = LockTable(["pad:P1"])
        locks.acquire("pad:P1", "taxi-a", "p1")
        self.assertFalse(locks.release("pad:P1", "drone-b"))
        self.assertTrue(locks.release("pad:P1", "taxi-a"))
        self.assertTrue(locks.acquire("pad:P1", "drone-b", "p2"))


class ArbiterTest(unittest.TestCase):
    def setUp(self):
        self.telemetry = {"taxi-a": {"battery": 40.0}, "drone-b": {"battery": 9.0}}

    def test_rule_prefers_larger_blast_radius(self):
        winner, _ = by_rule([make("drone-b", "cargo"), make("taxi-a", "passenger")],
                            self.telemetry)
        self.assertEqual(winner.asset_id, "taxi-a")

    def test_rule_breaks_ties_on_battery(self):
        winner, _ = by_rule([make("taxi-a", "cargo"), make("drone-b", "cargo")],
                            self.telemetry)
        self.assertEqual(winner.asset_id, "drone-b")

    def test_model_choice_is_used_when_in_range(self):
        arbiter = Arbiter(StubLlm("2"))
        candidates = [make("taxi-a", "passenger"), make("drone-b", "cargo")]
        winner, how = arbiter.choose(candidates, self.telemetry)
        self.assertEqual(winner.asset_id, "drone-b")
        self.assertTrue(how.startswith("ultra:"))

    def test_out_of_range_answer_is_discarded(self):
        arbiter = Arbiter(StubLlm("저는 7번이 좋다고 생각합니다"))
        candidates = [make("taxi-a", "passenger"), make("drone-b", "cargo")]
        winner, how = arbiter.choose(candidates, self.telemetry)
        self.assertEqual(winner.asset_id, "taxi-a")  # 규칙으로 되돌아갑니다
        self.assertTrue(how.startswith("rule:"))

    def test_prose_answer_is_discarded(self):
        arbiter = Arbiter(StubLlm("둘 다 착륙시키고 새 패드를 하나 더 지으세요"))
        candidates = [make("taxi-a", "passenger"), make("drone-b", "cargo")]
        _, how = arbiter.choose(candidates, self.telemetry)
        self.assertTrue(how.startswith("rule:"))


if __name__ == "__main__":
    unittest.main()
