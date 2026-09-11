"""The draft starts the moment the refusal arrives, not after the screen has shown it.

The screen shows a refusal for 5.6 s. The model used to be asked only after that, so the
aircraft stood for 5.6 s plus the whole draft. Now the ask goes to a worker thread at the
refusal, the display delay still elapses, and the result is collected afterwards — within
one draft budget counted from the refusal, never more. Nothing about provenance changes:
params.drafter still says who drew the line, and the runtime still judges it.
"""

import threading
import time
import unittest
from unittest import mock

from holdshort.agent import loop as loop_module
from holdshort.agent.drafter import ModelDrafter
from holdshort.agent.loop import GuardedAgent
from holdshort.agent.planner import OperatorPlanner
from holdshort.agent.propose import Proposer
from holdshort.core.models import Proposal
from holdshort.llm.client import TieredLlm
from tests.fixture_llm import FixtureLlm

HERE = (40.70178, -73.96920)
GOAL = (40.70600, -73.98000)
REFUSAL = {"verdict": "denied", "policy_hit": "airspace", "code": "airspace",
           "reason": "1번 구간이 규정을 어깁니다", "forbids": "bldg-x", "detail": {}}
APPROVAL = {"verdict": "auto", "policy_hit": None, "reason": "", "detail": {}}
DRAWN = [{"lat": HERE[0], "lon": HERE[1], "alt_m": 60.0},
         {"lat": 40.70400, "lon": -73.97500, "alt_m": 60.0},
         {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 60.0}]


class SleepingDrafter:
    """모델 대신 잠만 자는 초안기. 언제 불렸고 마감을 얼마로 받았는지 적습니다."""

    name = "nano:sleeper"

    def __init__(self, sleep_s: float, timeout_s: float = 5.0, legs=None):
        self.sleep_s = sleep_s
        self.timeout_s = timeout_s
        self.legs = DRAWN if legs is None else legs
        self.calls = 0
        self.started_at: float | None = None
        self.deadlines: list = []
        self.finished = threading.Event()
        self.last_attempts = 0

    def draft(self, start, goal, context=None, deadline=None):
        self.calls += 1
        self.started_at = time.monotonic()
        self.deadlines.append(deadline)
        self.last_attempts = 1
        time.sleep(self.sleep_s)
        self.finished.set()
        return self.legs


class FakeRuntime:
    """post_json 대역. 답을 차례로 내고, 신청이 언제 왔는지 적습니다."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.filings: list[tuple[float, dict]] = []

    def __call__(self, url, payload, timeout=20.0, headers=None):
        self.filings.append((time.monotonic(), payload))
        return self.answers.pop(0) if self.answers else APPROVAL


def _agent(drafter, redraw_s: float) -> GuardedAgent:
    llm = TieredLlm(base_url="", models={}, timeout_s=1.0, request_extra={}, record_dir="")
    agent = GuardedAgent("drone-t", "http://runtime.test", Proposer(llm), llm)
    agent.planner = OperatorPlanner()       # 빈 공역: A* 는 직선을 밀리초에 그립니다
    agent.drafter = drafter
    agent.redraw_s = redraw_s
    return agent


def _proposal() -> Proposal:
    return Proposal(asset_id="drone-t", action="fly_route", cost_usd=12.0,
                    blast_radius="schedule", rationale="시험", params={})


TELEMETRY = {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0, "job_lat": GOAL[0],
             "job_lon": GOAL[1]}


def _file(agent: GuardedAgent, runtime: FakeRuntime, telemetry: dict | None = None):
    """post_json 은 대역 런타임으로, 초안 뒤의 자리 확인(get_json)은 주어진 텔레메트리로."""
    fresh = dict(TELEMETRY if telemetry is None else telemetry)
    with mock.patch.object(loop_module, "post_json", runtime), \
            mock.patch.object(loop_module, "get_json", lambda url, **kwargs: fresh):
        return agent._file_with_route(_proposal(), dict(TELEMETRY))


class DraftAtRefusalTimeTest(unittest.TestCase):
    def test_the_draft_starts_at_the_refusal_and_the_display_delay_still_elapses(self):
        drafter = SleepingDrafter(sleep_s=0.05, timeout_s=5.0)
        agent = _agent(drafter, redraw_s=0.4)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime)
        self.assertEqual(decision["verdict"], "auto")
        refused_at, straight = runtime.filings[0]
        redrawn_at, redrawn = runtime.filings[1]
        self.assertEqual(straight["params"]["drafter"], "straight")
        # 초안은 거절이 온 직후에 시작했습니다 — 5.6초(여기서는 0.4초)를 기다린 뒤가 아니라
        self.assertLess(drafter.started_at - refused_at, 0.1)
        # 그래도 화면이 거절을 보여주는 시간은 그대로 흘렀습니다
        self.assertGreaterEqual(redrawn_at - refused_at, 0.4)
        self.assertEqual(redrawn["params"]["drafter"], "nano:sleeper")
        self.assertEqual(redrawn["params"]["draft_attempts"], 1)
        self.assertEqual(redrawn["params"]["legs"], DRAWN)
        # 마감은 거절 시각 + 초안 예산 하나
        self.assertAlmostEqual(drafter.deadlines[0], refused_at + 5.0, delta=0.1)
        self.assertFalse(agent.draft_in_flight)

    def test_a_slow_draft_that_finishes_inside_the_budget_is_used(self):
        drafter = SleepingDrafter(sleep_s=0.5, timeout_s=2.0)
        agent = _agent(drafter, redraw_s=0.1)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, runtime)
        refused_at, _ = runtime.filings[0]
        redrawn_at, redrawn = runtime.filings[1]
        self.assertGreaterEqual(redrawn_at - refused_at, 0.5)   # 초안이 끝날 때까지 기다렸습니다
        self.assertLess(redrawn_at - refused_at, 1.5)
        self.assertEqual(redrawn["params"]["drafter"], "nano:sleeper")
        self.assertEqual(redrawn["params"]["draft_attempts"], 1)
        self.assertFalse(agent.draft_in_flight)

    def test_a_draft_slower_than_the_budget_falls_back_to_a_star(self):
        drafter = SleepingDrafter(sleep_s=1.0, timeout_s=0.4)
        agent = _agent(drafter, redraw_s=0.1)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, runtime)
        refused_at, _ = runtime.filings[0]
        redrawn_at, redrawn = runtime.filings[1]
        # 예산(0.4초)까지만 기다리고 A* 로 냈습니다. 초안이 끝나는 1초까지 기다리지 않습니다.
        self.assertGreaterEqual(redrawn_at - refused_at, 0.4)
        self.assertLess(redrawn_at - refused_at, 0.9)
        self.assertEqual(redrawn["params"]["drafter"], "astar")
        self.assertEqual(redrawn["params"]["draft_attempts"], 1)   # 물어보긴 했습니다
        self.assertNotEqual(redrawn["params"]["legs"], DRAWN)
        self.assertTrue(agent.draft_in_flight)                     # 서버에는 아직 걸려 있고
        self.assertTrue(drafter.finished.wait(2.0))
        agent._draft.future.result(timeout=1.0)
        self.assertFalse(agent.draft_in_flight)                    # 끝나면 비어 있습니다

    def test_only_one_draft_is_ever_in_flight_and_the_pool_does_not_grow(self):
        drafter = SleepingDrafter(sleep_s=0.6, timeout_s=0.2)
        agent = _agent(drafter, redraw_s=0.05)
        first = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, first)
        self.assertEqual(first.filings[1][1]["params"]["drafter"], "astar")
        self.assertTrue(agent.draft_in_flight)
        # 지난 초안이 아직 걸려 있는 동안의 새 거절: 또 묻지 않고 A* 로, 물은 횟수 0
        second = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, second)
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(second.filings[1][1]["params"]["drafter"], "astar")
        self.assertEqual(second.filings[1][1]["params"]["draft_attempts"], 0)
        self.assertTrue(drafter.finished.wait(2.0))
        agent._draft.future.result(timeout=1.0)
        # 끝난 뒤의 거절은 다시 묻습니다. 스레드는 하나뿐이고 새로 생기지 않습니다.
        drafter.sleep_s = 0.01
        third = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, third)
        self.assertEqual(drafter.calls, 2)
        self.assertEqual(third.filings[1][1]["params"]["drafter"], "nano:sleeper")
        self.assertFalse(agent.draft_in_flight)
        # 초안 스레드는 데몬이고 끝난 것은 남지 않습니다 — Ctrl-C 가 서버에 걸린 초안을 안 기다리게
        alive = [th for th in threading.enumerate() if th.name == "draft-drone-t"]
        self.assertLessEqual(len(alive), 1)
        self.assertTrue(all(th.daemon for th in alive))
        self.assertTrue(all(th.daemon for th in threading.enumerate()
                            if th.name.startswith("draft-")))

    def test_a_traffic_refusal_never_starts_a_draft(self):
        """교차 거절 뒤에는 모델에게 묻지 않습니다 — 모델은 다른 기체를 모릅니다."""
        drafter = SleepingDrafter(sleep_s=0.01)
        agent = _agent(drafter, redraw_s=0.05)
        traffic = {**REFUSAL, "policy_hit": "traffic", "code": "traffic",
                   "detail": {"blocked_asset": "drone-02", "blocked_until_tick": 900}}
        # 직선 → 교차, +30m → 교차, 지연 → 교차(같은 틱이라 사다리 끝) → 다시 그리기(A*) → 승인
        runtime = FakeRuntime(traffic, traffic, traffic, APPROVAL)
        _file(agent, runtime)
        self.assertEqual(drafter.calls, 0)
        self.assertEqual(runtime.filings[-1][1]["params"]["drafter"], "astar")
        self.assertFalse(agent.draft_in_flight)

    def test_a_crashing_drafter_does_not_take_the_agent_down(self):
        class Crashing(SleepingDrafter):
            def draft(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("boom")

        agent = _agent(Crashing(sleep_s=0.0), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime)
        self.assertEqual(decision["verdict"], "auto")
        self.assertEqual(runtime.filings[1][1]["params"]["drafter"], "astar")
        self.assertFalse(agent.draft_in_flight)


class MovedWhileDraftingTest(unittest.TestCase):
    """초안을 기다리는 사이 기체가 움직였습니다. 런타임은 첫 점이 자리에서 멀면 거절합니다."""

    def test_a_small_move_re_anchors_the_first_leg_and_keeps_the_draft(self):
        drafter = SleepingDrafter(sleep_s=0.05)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        nudged = {**TELEMETRY, "lat": HERE[0] + 0.0002}           # 22 m 옆, 회랑 반폭 안
        _file(agent, runtime, telemetry=nudged)
        redrawn = runtime.filings[1][1]
        self.assertEqual(redrawn["params"]["drafter"], "nano:sleeper")
        self.assertEqual((redrawn["params"]["legs"][0]["lat"], redrawn["params"]["legs"][0]["lon"]),
                         (round(nudged["lat"], 6), HERE[1]))
        self.assertEqual(redrawn["params"]["legs"][1:], DRAWN[1:])

    def test_taking_off_meanwhile_drops_the_ground_draft(self):
        drafter = SleepingDrafter(sleep_s=0.05)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime, telemetry={**TELEMETRY, "alt_m": 40.0})
        self.assertEqual(decision["verdict"], "denied",
                         "이번 차례는 접고 다음 차례에 공중에서 냅니다")
        self.assertEqual(len(runtime.filings), 1)

    def test_a_big_move_redraws_from_the_new_spot(self):
        drafter = SleepingDrafter(sleep_s=0.05)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        far = {**TELEMETRY, "lat": HERE[0] + 0.002}                # 220 m 옆
        _file(agent, runtime, telemetry=far)
        redrawn = runtime.filings[1][1]
        self.assertEqual(redrawn["params"]["legs"][0]["lat"], round(far["lat"], 6))
        self.assertNotEqual(redrawn["params"]["legs"][1:], DRAWN[1:])


class BackoffStillWorksTest(unittest.TestCase):
    """서버가 답을 못 주면 한동안 묻지 않습니다 — 작업 스레드에서 물어도 같습니다."""

    def test_a_no_reply_sets_the_backoff_and_the_next_refusal_is_not_asked(self):
        llm = FixtureLlm(records=[])                       # 무엇을 물어도 None
        planner = OperatorPlanner()
        drafter = ModelDrafter(llm, planner, timeout_s=5.0, backoff_s=30.0)
        self.assertTrue(drafter.enabled)
        agent = _agent(drafter, redraw_s=0.05)
        agent.planner = planner
        first = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, first)
        self.assertEqual(first.filings[1][1]["params"]["drafter"], "astar")
        self.assertEqual(first.filings[1][1]["params"]["draft_attempts"], 1)
        self.assertEqual(len(llm.asked), 1)
        self.assertGreater(drafter.skip_until, time.monotonic())
        self.assertFalse(agent.draft_in_flight)
        second = FakeRuntime(REFUSAL, APPROVAL)
        _file(agent, second)
        self.assertEqual(len(llm.asked), 1)                # 물러섰습니다
        self.assertEqual(second.filings[1][1]["params"]["draft_attempts"], 0)
        self.assertIn("skipped", drafter.last_failures[0])


if __name__ == "__main__":
    unittest.main()
