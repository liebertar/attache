import tempfile
import time
import unittest
from unittest import mock

from holdshort.core.models import Proposal, Verdict
from holdshort.llm.client import LlmReply, TieredLlm
from holdshort.runtime import service as service_module
from holdshort.runtime.arbiter import SYSTEM, Arbiter, by_rule, parse_verdict
from holdshort.runtime.locks import LockTable
from holdshort.runtime.service import Runtime


class StubLlm(TieredLlm):
    def __init__(self, text, delay_s=0.0):
        super().__init__(base_url="http://stub", models={"ultra": "stub-ultra"},
                         timeout_s=1.0, request_extra={}, record_dir="")
        self.text = text
        self.delay_s = delay_s
        self.prompts = []

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        self.prompts.append((system, user, max_tokens, json_object))
        if self.delay_s:
            time.sleep(self.delay_s)
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

    def test_prose_with_a_number_inside_is_still_prose(self):
        arbiter = Arbiter(StubLlm("1번은 안 되고 2번이 낫겠습니다"))
        candidates = [make("drone-01", "passenger"), make("drone-02", "cargo")]
        _, how = arbiter.choose(candidates, self.telemetry)
        self.assertTrue(how.startswith("rule:"))

    def test_json_choice_with_reason_is_used_and_the_reason_is_kept(self):
        llm = StubLlm('{"choice": 2, "reason": "battery is at 9%, it lands first"}')
        arbiter = Arbiter(llm)
        candidates = [make("drone-01", "passenger"), make("drone-02", "cargo")]
        choice = arbiter.pick(candidates, self.telemetry)
        self.assertEqual(choice.proposal.asset_id, "drone-02")
        self.assertEqual(choice.how, "ultra:stub-ultra")
        self.assertEqual(choice.reason, "battery is at 9%, it lands first")
        system, user, max_tokens, json_object = llm.prompts[0]
        self.assertTrue(json_object)
        self.assertEqual(max_tokens, 160)
        for word in ("budget", "cost", "$", "usd"):
            self.assertNotIn(word, (system + user).lower(), f"중재 프롬프트에 돈 이야기: {word}")

    def test_a_long_reason_is_cut_to_140(self):
        arbiter = Arbiter(StubLlm('{"choice": 1, "reason": "' + "x" * 500 + '"}'))
        choice = arbiter.pick([make("drone-01", "passenger"), make("drone-02", "cargo")],
                              self.telemetry)
        self.assertEqual(len(choice.reason), 140)

    def test_json_choice_out_of_range_or_not_a_number_falls_back(self):
        for text in ('{"choice": 5, "reason": "x"}', '{"choice": "two"}', '{"choice": 0}',
                     '{"reason": "no choice at all"}'):
            with self.subTest(text=text):
                choice = Arbiter(StubLlm(text)).pick(
                    [make("drone-01", "passenger"), make("drone-02", "cargo")], self.telemetry)
                self.assertTrue(choice.how.startswith("rule:"))
                self.assertEqual(choice.reason, "")

    def test_parse_verdict_shapes(self):
        self.assertEqual(parse_verdict("2", 3), (1, ""))
        self.assertEqual(parse_verdict('{"choice": 3, "reason": "r"}', 3), (2, "r"))
        self.assertEqual(parse_verdict('<think>1 or 3?</think>{"choice": 1}', 3), (0, ""))
        self.assertIsNone(parse_verdict("2 or 3", 3))
        self.assertIsNone(parse_verdict("", 3))
        self.assertIn("safety", SYSTEM)


class TickingAdapter:
    """세계의 시계만 흉내 냅니다. 부를 때마다 한 틱."""

    def __init__(self):
        self.n = 0
        self.executed = []

    def telemetry(self):
        self.n += 1
        return {"tick": self.n, "assets": {"drone-01": {"battery": 40.0},
                                            "drone-02": {"battery": 9.0}}}

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        self.executed.append((asset_id, action))
        return {"ok": True}


class SlowArbiterDoesNotStallTheWorldTest(unittest.TestCase):
    """Ultra 가 3초 생각하는 동안에도 틱은 흘러야 합니다.

    중재와 세계 갱신이 한 스레드에 있던 때는 모델이 답할 때까지 런타임이 옛 위치로
    판정했습니다. 중재는 자기 스레드에서 돌고, 그동안 _pull_world 는 계속 돕니다.
    """

    def test_a_three_second_arbiter_leaves_the_tick_running(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, window_s=0.0)
        adapter = TickingAdapter()
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        runtime.llm = StubLlm('{"choice": 2, "reason": "lower battery lands first"}', delay_s=3.0)
        runtime.arbiter = Arbiter(runtime.llm)
        runtime.telemetry = adapter.telemetry()["assets"]

        first = runtime.file(make("drone-01", "cargo").to_dict())
        second = runtime.file(make("drone-02", "cargo").to_dict())
        self.assertIs(first.verdict, Verdict.QUEUED)
        self.assertIs(second.verdict, Verdict.QUEUED)

        # 시뮬레이터 HTTP 는 없습니다. 공역·공지 조회는 빈 답으로 막고 어댑터의 시계만 씁니다.
        with mock.patch.object(service_module, "get_json", lambda *a, **k: {}):
            runtime.start_background()
            time.sleep(1.5)
            ticks_while_thinking = runtime.tick
            time.sleep(2.5)
        # 0.25초마다 한 틱이면 1.5초에 대여섯 틱. 중재 뒤에 묶여 있었다면 0~1틱입니다.
        self.assertGreaterEqual(ticks_while_thinking, 4,
                                "중재가 끝날 때까지 세계 갱신이 멈춰 있었습니다")
        self.assertEqual(adapter.executed, [("drone-02", "reserve_pad")])
        self.assertEqual(second.detail.get("arbiter_reason"), "lower battery lands first")
        self.assertEqual(second.arbiter, "ultra:stub-ultra")
        self.assertIs(first.verdict, Verdict.DENIED)
        self.assertEqual(first.detail.get("arbiter_reason"), "lower battery lands first")


if __name__ == "__main__":
    unittest.main()
