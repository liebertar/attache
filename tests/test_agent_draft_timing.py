"""The choice starts the moment the refusal arrives; the model draft is the last resort.

The screen shows a refusal for 5.6 s. The planner's candidates and the model's choice run on a
worker thread from the refusal, the display delay still elapses, and the result is collected
afterwards. The model draft used to start at the refusal; now it waits until every candidate has
been refused — each drone has one model slot, and a 60 s draft queued in front of a 2 s choice
would make the aircraft stand for the draft. Within its own budget, one draft in flight at a
time, never taking the agent down. Nothing about provenance changes: params.drafter still says
who drew the line, and the runtime still judges it.
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
        self.last_latency_ms = 0
        self.last_breach = None

    def draft(self, start, goal, context=None, deadline=None):
        self.calls += 1
        self.started_at = time.monotonic()
        self.deadlines.append(deadline)
        self.last_attempts = 1
        time.sleep(self.sleep_s)
        self.finished.set()
        return self.legs


class TimedPlanner(OperatorPlanner):
    """후보를 언제 그리기 시작했는지 적는 계획기(빈 공역 — 밀리초에 답합니다)."""

    def __init__(self):
        super().__init__()
        self.asked_at: list[float] = []

    def candidates(self, start, goal, context=None, budget_s=None):
        self.asked_at.append(time.monotonic())
        return super().candidates(start, goal, context, budget_s)


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
    agent.planner = TimedPlanner()          # 빈 공역: 후보는 직선 하나(최단)입니다
    agent.drafter = drafter
    agent.redraw_s = redraw_s
    return agent


def _proposal() -> Proposal:
    return Proposal(asset_id="drone-t", action="fly_route", cost_usd=12.0,
                    blast_radius="schedule", rationale="시험", params={})


TELEMETRY = {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0, "job_lat": GOAL[0],
             "job_lon": GOAL[1]}


def _file(agent: GuardedAgent, runtime: FakeRuntime, telemetry: dict | None = None):
    """post_json 은 대역 런타임으로, /state 는 빈 상태로, 뒤의 자리 확인은 주어진 텔레메트리로."""
    fresh = dict(TELEMETRY if telemetry is None else telemetry)

    def get(url, **kwargs):
        return {} if url.endswith("/state") else fresh

    with mock.patch.object(loop_module, "post_json", runtime), \
            mock.patch.object(loop_module, "get_json", get):
        return agent._file_with_route(_proposal(), dict(TELEMETRY))


def _shortest(agent: GuardedAgent) -> list[dict]:
    return agent.planner.candidates(HERE, GOAL)[0]["legs"]


class ChoiceAtRefusalTimeTest(unittest.TestCase):
    def test_the_choice_starts_at_the_refusal_and_the_display_delay_still_elapses(self):
        drafter = SleepingDrafter(sleep_s=0.05, timeout_s=5.0)
        agent = _agent(drafter, redraw_s=0.4)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime)
        self.assertEqual(decision["verdict"], "auto")
        refused_at, straight = runtime.filings[0]
        redrawn_at, redrawn = runtime.filings[1]
        self.assertEqual(straight["params"]["drafter"], "straight")
        # 후보는 거절이 온 직후에 그리기 시작했습니다 — 5.6초(여기서는 0.4초)를 기다린 뒤가 아니라
        self.assertLess(agent.planner.asked_at[0] - refused_at, 0.1)
        # 그래도 화면이 거절을 보여주는 시간은 그대로 흘렀습니다
        self.assertGreaterEqual(redrawn_at - refused_at, 0.4)
        self.assertEqual(redrawn["params"]["drafter"], "astar")      # 모델이 없어 규칙이 골랐습니다
        self.assertEqual(redrawn["params"]["route_choice"]["path"], "rules")
        self.assertEqual(redrawn["params"]["legs"], _shortest(agent))
        self.assertEqual(drafter.calls, 0, "후보가 통하면 초안은 묻지 않습니다")
        self.assertFalse(agent.draft_in_flight)

    def test_the_draft_waits_until_every_candidate_is_refused(self):
        drafter = SleepingDrafter(sleep_s=0.05, timeout_s=5.0)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        decision = _file(agent, runtime)
        self.assertEqual(decision["verdict"], "auto")
        candidate_at, _ = runtime.filings[1]
        drafted_at, drafted = runtime.filings[2]
        self.assertGreaterEqual(drafter.started_at, candidate_at, "후보가 거절된 뒤에야 묻습니다")
        self.assertEqual(drafted["params"]["drafter"], "nano:sleeper")
        self.assertEqual(drafted["params"]["draft_attempts"], 1)
        self.assertEqual(drafted["params"]["legs"], DRAWN)
        # 마감은 초안을 시작한 시각 + 초안 예산 하나
        self.assertAlmostEqual(drafter.deadlines[0], drafter.started_at + 5.0, delta=0.1)
        self.assertFalse(agent.draft_in_flight)

    def test_a_slow_draft_that_finishes_inside_the_budget_is_used(self):
        drafter = SleepingDrafter(sleep_s=0.5, timeout_s=2.0)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        _file(agent, runtime)
        candidate_at, _ = runtime.filings[1]
        drafted_at, drafted = runtime.filings[2]
        self.assertGreaterEqual(drafted_at - candidate_at, 0.5)    # 초안이 끝날 때까지 기다렸습니다
        self.assertLess(drafted_at - candidate_at, 1.5)
        self.assertEqual(drafted["params"]["drafter"], "nano:sleeper")
        self.assertFalse(agent.draft_in_flight)

    def test_a_draft_slower_than_its_budget_is_dropped_and_the_turn_ends(self):
        drafter = SleepingDrafter(sleep_s=1.0, timeout_s=0.4)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL)
        started = time.monotonic()
        decision = _file(agent, runtime)
        # 예산(0.4초)까지만 기다렸고 초안이 끝나는 1초까지 기다리지 않았습니다.
        # 후보는 이미 거절됐으니
        # 이번 차례는 그 거절로 끝나고, 다음 차례에 처음부터 다시 냅니다.
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertEqual(decision["verdict"], "denied")
        self.assertEqual(len(runtime.filings), 2)
        self.assertTrue(agent.draft_in_flight)                     # 서버에는 아직 걸려 있고
        self.assertTrue(drafter.finished.wait(2.0))
        agent._draft.future.result(timeout=1.0)
        self.assertFalse(agent.draft_in_flight)                    # 끝나면 비어 있습니다

    def test_only_one_draft_is_ever_in_flight_and_the_pool_does_not_grow(self):
        drafter = SleepingDrafter(sleep_s=0.6, timeout_s=0.2)
        agent = _agent(drafter, redraw_s=0.05)
        _file(agent, FakeRuntime(REFUSAL, REFUSAL))
        self.assertTrue(agent.draft_in_flight)
        # 지난 초안이 아직 걸려 있는 동안의 새 거절: 또 묻지 않습니다
        second = FakeRuntime(REFUSAL, REFUSAL)
        _file(agent, second)
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(len(second.filings), 2)
        self.assertTrue(drafter.finished.wait(2.0))
        agent._draft.future.result(timeout=1.0)
        # 끝난 뒤의 거절은 다시 묻습니다. 스레드는 하나뿐이고 새로 생기지 않습니다.
        drafter.sleep_s = 0.01
        third = FakeRuntime(REFUSAL, REFUSAL, APPROVAL)
        _file(agent, third)
        self.assertEqual(drafter.calls, 2)
        self.assertEqual(third.filings[2][1]["params"]["drafter"], "nano:sleeper")
        self.assertFalse(agent.draft_in_flight)
        # 작업 스레드는 데몬이고 끝난 것은 남지 않습니다 — Ctrl-C 가 서버에 걸린 초안을 안 기다리게
        alive = [th for th in threading.enumerate() if th.name == "draft-drone-t"]
        self.assertLessEqual(len(alive), 1)
        self.assertTrue(all(th.daemon for th in threading.enumerate()
                            if th.name.startswith("draft-")))

    def test_a_traffic_refusal_never_starts_a_draft(self):
        """교차 거절 뒤에는 모델에게 묻지 않습니다 — 모델은 다른 기체를 모릅니다."""
        drafter = SleepingDrafter(sleep_s=0.01)
        agent = _agent(drafter, redraw_s=0.05)
        traffic = {**REFUSAL, "policy_hit": "traffic", "code": "traffic",
                   "detail": {"blocked_asset": "drone-02", "blocked_until_tick": 900}}
        # 직선 → 교차, +30m → 교차, 지연 → 교차(같은 틱이라 사다리 끝) → 후보(A*) → 승인
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

        drafter = Crashing(sleep_s=0.0)
        agent = _agent(drafter, redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, REFUSAL)
        decision = _file(agent, runtime)
        self.assertEqual(drafter.calls, 1)
        self.assertEqual(decision["verdict"], "denied")
        self.assertEqual(len(runtime.filings), 2)
        self.assertFalse(agent.draft_in_flight)


class MovedWhileChoosingTest(unittest.TestCase):
    """후보를 고르는 사이 기체가 움직였습니다. 런타임은 첫 점이 자리에서 멀면 거절합니다."""

    def test_a_small_move_re_anchors_the_first_leg_and_keeps_the_candidate(self):
        agent = _agent(SleepingDrafter(sleep_s=0.05), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        nudged = {**TELEMETRY, "lat": HERE[0] + 0.0002}           # 22 m 옆, 회랑 반폭 안
        _file(agent, runtime, telemetry=nudged)
        redrawn = runtime.filings[1][1]
        self.assertEqual(redrawn["params"]["drafter"], "astar")
        self.assertEqual((redrawn["params"]["legs"][0]["lat"], redrawn["params"]["legs"][0]["lon"]),
                         (round(nudged["lat"], 6), HERE[1]))
        self.assertEqual(redrawn["params"]["legs"][1:], _shortest(agent)[1:])

    def test_taking_off_meanwhile_drops_the_ground_route(self):
        agent = _agent(SleepingDrafter(sleep_s=0.05), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        decision = _file(agent, runtime, telemetry={**TELEMETRY, "alt_m": 40.0})
        self.assertEqual(decision["verdict"], "denied",
                         "이번 차례는 접고 다음 차례에 공중에서 냅니다")
        self.assertEqual(len(runtime.filings), 1)

    def test_a_big_move_redraws_from_the_new_spot(self):
        agent = _agent(SleepingDrafter(sleep_s=0.05), redraw_s=0.05)
        runtime = FakeRuntime(REFUSAL, APPROVAL)
        far = {**TELEMETRY, "lat": HERE[0] + 0.002}                # 220 m 옆
        _file(agent, runtime, telemetry=far)
        redrawn = runtime.filings[1][1]
        self.assertEqual(redrawn["params"]["legs"][0]["lat"], round(far["lat"], 6))
        self.assertNotEqual(redrawn["params"]["legs"][1:], _shortest(agent)[1:])
        self.assertNotIn("route_choice", redrawn["params"], "옛 자리의 후보는 버렸습니다")


class BackoffStillWorksTest(unittest.TestCase):
    """서버가 답을 못 주면 한동안 초안을 묻지 않습니다 — 마지막 수단이 되어도 같습니다."""

    def test_a_no_reply_sets_the_backoff_and_the_next_refusal_is_not_asked(self):
        llm = FixtureLlm(records=[])                       # 무엇을 물어도 None
        planner = TimedPlanner()
        drafter = ModelDrafter(llm, planner, timeout_s=5.0, backoff_s=30.0)
        self.assertTrue(drafter.enabled)
        agent = _agent(drafter, redraw_s=0.05)
        agent.planner = planner
        first = FakeRuntime(REFUSAL, REFUSAL)
        _file(agent, first)
        self.assertEqual(len(first.filings), 2)            # 직선, 후보. 초안은 답이 없었습니다
        self.assertEqual(len(llm.asked), 1)
        self.assertGreater(drafter.skip_until, time.monotonic())
        self.assertFalse(agent.draft_in_flight)
        second = FakeRuntime(REFUSAL, REFUSAL)
        _file(agent, second)
        self.assertEqual(len(llm.asked), 1)                # 물러섰습니다
        self.assertIn("skipped", drafter.last_failures[0])


if __name__ == "__main__":
    unittest.main()
