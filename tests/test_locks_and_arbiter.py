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
        self.assertTrue(locks.acquire("pad:P1", "drone-01", "p1"))
        self.assertFalse(locks.acquire("pad:P1", "drone-02", "p2"))
        self.assertTrue(locks.acquire("pad:P1", "drone-01", "p3"))

    def test_release_only_by_holder(self):
        locks = LockTable(["pad:P1"])
        locks.acquire("pad:P1", "drone-01", "p1")
        self.assertFalse(locks.release("pad:P1", "drone-02"))
        self.assertTrue(locks.release("pad:P1", "drone-01"))
        self.assertTrue(locks.acquire("pad:P1", "drone-02", "p2"))


class ArbiterTest(unittest.TestCase):
    def setUp(self):
        self.telemetry = {"drone-01": {"battery": 40.0}, "drone-02": {"battery": 9.0}}

    def test_rule_prefers_larger_blast_radius(self):
        winner, _ = by_rule([make("drone-02", "cargo"), make("drone-01", "passenger")],
                            self.telemetry)
        self.assertEqual(winner.asset_id, "drone-01")

    def test_rule_breaks_ties_on_battery(self):
        winner, _ = by_rule([make("drone-01", "cargo"), make("drone-02", "cargo")],
                            self.telemetry)
        self.assertEqual(winner.asset_id, "drone-02")

    def test_model_choice_is_used_when_in_range(self):
        arbiter = Arbiter(StubLlm("2"))
        candidates = [make("drone-01", "passenger"), make("drone-02", "cargo")]
        winner, how = arbiter.choose(candidates, self.telemetry)
        self.assertEqual(winner.asset_id, "drone-02")
        self.assertTrue(how.startswith("ultra:"))

    def test_out_of_range_answer_is_discarded(self):
        arbiter = Arbiter(StubLlm("저는 7번이 좋다고 생각합니다"))
        candidates = [make("drone-01", "passenger"), make("drone-02", "cargo")]
        winner, how = arbiter.choose(candidates, self.telemetry)
        self.assertEqual(winner.asset_id, "drone-01")  # 규칙으로 되돌아갑니다
        self.assertTrue(how.startswith("rule:"))

    def test_prose_answer_is_discarded(self):
        arbiter = Arbiter(StubLlm("둘 다 착륙시키고 새 패드를 하나 더 지으세요"))
        candidates = [make("drone-01", "passenger"), make("drone-02", "cargo")]
        _, how = arbiter.choose(candidates, self.telemetry)
        self.assertTrue(how.startswith("rule:"))


if __name__ == "__main__":
    unittest.main()
