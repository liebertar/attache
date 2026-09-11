"""A route the model sketches is a proposal, never a route.

Every check here is code: shape, count, box, altitude band, snapped ends, length, and
then the same judge the runtime uses. The model can be garbage, adversarial, or right —
what leaves `draft()` is either something the judge passed or nothing.
"""

import json
import pathlib
import unittest

from holdshort.agent.drafter import (
    ALT_MAX_M,
    DRAFT_TIMEOUT_S,
    MAX_LEGS,
    ModelDrafter,
    describe,
    service_bbox,
)
from holdshort.agent.planner import OperatorPlanner
from holdshort.core.geo import Volume, first_breach
from holdshort.llm.client import LlmReply, TieredLlm
from sim.world import LANDING_AREAS, Simulation, seat_of, to_latlon
from tests.fixture_llm import FIXTURE_DIR, FixtureLlm, load_fixtures

RUNTIME_DIR = pathlib.Path(__file__).resolve().parent.parent / "holdshort" / "runtime"


class ScriptedLlm(TieredLlm):
    """답을 차례로 냅니다. 몇 번 물었는지도 셉니다."""

    def __init__(self, *texts):
        super().__init__(base_url="http://scripted", models={"nano": "scripted-nano"},
                         timeout_s=1.0, request_extra={}, record_dir="")
        self.texts = list(texts)
        self.prompts = []
        self.budgets = []

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        self.prompts.append(user)
        self.budgets.append(timeout_s)
        if not self.texts:
            return None
        text = self.texts.pop(0)
        return None if text is None else LlmReply(text=text, model="scripted-nano")


def _fleet_planner() -> OperatorPlanner:
    planner = OperatorPlanner()
    planner.load(Simulation(seed=7).worlds["guarded"].snapshot(0, volumes=True)["volumes"])
    return planner


def _bbox():
    return service_bbox([(a["lat"], a["lon"]) for a in LANDING_AREAS])


# 이륙장 옆 마당 첫 자리 → 브루클린 브리지 파크. 강 위로 짧게 가는 길이라 손으로 그릴 수 있습니다.
START = tuple(round(v, 6) for v in to_latlon(*seat_of(0)))
GOAL = (40.70200, -73.99650)


def _hand_drawn():
    """강 위로 도는 3구간. 시험이 스스로 판정해서 통과를 확인한 뒤 씁니다.

    마당을 나와 서쪽으로 비니거힐·덤보의 저층(최고 63m) 위를 114m 로 건너 공원에 내립니다.
    강 위로만 돌면 덤보 강변의 83m 건물이 마지막 구간에 걸립니다."""
    return {"legs": [
        {"lat": START[0], "lon": START[1], "alt_m": 70},
        {"lat": 40.70115, "lon": -73.97055, "alt_m": 90},
        {"lat": 40.70160, "lon": -73.99575, "alt_m": 114},
        {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 90},
    ]}


class ValidationTest(unittest.TestCase):
    """모델이 무엇을 보내든 코드가 거릅니다. 판정보다 먼저, 판정과 무관하게."""

    def setUp(self):
        self.bbox = _bbox()

    def check(self, form):
        return ModelDrafter.validate(form, START, GOAL, self.bbox)

    def test_garbage_shapes_are_rejected(self):
        for form in (None, {}, {"legs": "north"}, {"legs": [1, 2]}, {"legs": [{"lat": 1}]},
                     {"legs": [{"lat": "a", "lon": "b", "alt_m": "c"},
                               {"lat": 1, "lon": 2, "alt_m": 3}]},
                     {"legs": [{"lat": 40.7, "lon": -73.9, "alt_m": 60}]}):
            with self.subTest(form=form):
                legs, why = self.check(form)
                self.assertIsNone(legs)
                self.assertTrue(why)

    def test_misspelt_altitude_keys_are_read_as_alt_m(self):
        """실주행의 4B 는 열 답 중 넷을 alt_ma 로 적었습니다. 키 이름은 양식이지 규칙이 아닙니다."""
        for key in ("alt_ma", "altitude_m", "altitude", "alt"):
            with self.subTest(key=key):
                legs, why = self.check({"legs": [{"lat": START[0], "lon": START[1], key: 60},
                                                 {"lat": GOAL[0], "lon": GOAL[1], key: 200}]})
                self.assertIsNone(legs)
                self.assertIn("outside", why, "값은 같은 범위 검사를 받습니다")
                legs, why = self.check({"legs": [{"lat": START[0], "lon": START[1], key: 60},
                                                 {"lat": GOAL[0], "lon": GOAL[1], key: 90}]})
                self.assertIsNone(why)
                self.assertEqual([leg["alt_m"] for leg in legs], [60.0, 90.0])
                self.assertNotIn(key if key != "alt_m" else "x", legs[0])

    def test_too_many_legs(self):
        legs = [{"lat": 40.70 + i * 0.0005, "lon": -73.98, "alt_m": 60}
                for i in range(MAX_LEGS + 1)]
        self.assertIsNone(self.check({"legs": legs})[0])
        self.assertIsNotNone(self.check({"legs": legs[:MAX_LEGS]})[0])

    def test_out_of_bbox_and_absurd_altitude(self):
        good = _hand_drawn()
        far = json.loads(json.dumps(good))
        far["legs"][1]["lat"] = 41.5            # 코네티컷
        self.assertIn("service box", self.check(far)[1])
        for altitude in (-5, 0, 39.9, 120.1, 5000, float("nan"), float("inf")):
            bad = json.loads(json.dumps(good, allow_nan=True), parse_constant=float)
            bad["legs"][2]["alt_m"] = altitude
            with self.subTest(altitude=altitude):
                self.assertIsNone(self.check(bad)[0])

    def test_ends_are_snapped_and_a_wander_is_too_long(self):
        form = _hand_drawn()
        form["legs"][0]["lat"] += 0.001          # 모델이 출발점을 110m 빗나가게 적음
        form["legs"][-1]["lon"] -= 0.001
        legs, why = self.check(form)
        self.assertIsNone(why)
        self.assertEqual((legs[0]["lat"], legs[0]["lon"]), START)
        self.assertEqual((legs[-1]["lat"], legs[-1]["lon"]), GOAL)
        wander = _hand_drawn()
        wander["legs"].insert(1, {"lat": 40.80, "lon": -73.95, "alt_m": 60})   # 할렘까지 갔다 옴
        self.assertIn("longer than", self.check(wander)[1])

    def test_repeated_points_collapse(self):
        form = {"legs": [{"lat": START[0], "lon": START[1], "alt_m": 60},
                         {"lat": START[0], "lon": START[1], "alt_m": 60},
                         {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 60}]}
        legs, _ = self.check(form)
        self.assertEqual(len(legs), 2)


class DraftFlowTest(unittest.TestCase):
    """묻고, 거르고, 판정하고, 한 번 더 묻고, 그래도 안 되면 손을 뗍니다."""

    @classmethod
    def setUpClass(cls):
        cls.planner = _fleet_planner()
        cls.bbox = _bbox()
        # 손으로 그린 길이 실제 공역에서 통과하는지 시험이 먼저 확인합니다
        legs, why = ModelDrafter.validate(_hand_drawn(), START, GOAL, cls.bbox)
        assert why is None, why
        assert first_breach(cls.planner.airspace, legs) is None, "손 경로가 판정을 못 넘습니다"

    def drafter(self, *texts):
        return ModelDrafter(ScriptedLlm(*texts), self.planner, bbox=self.bbox)

    def test_a_passing_draft_is_returned_with_its_ends_snapped(self):
        drafter = self.drafter(json.dumps(_hand_drawn()))
        legs = drafter.draft(START, GOAL, {"reason": "x", "forbids": None})
        self.assertIsNotNone(legs)
        self.assertEqual(drafter.last_attempts, 1)
        self.assertEqual((legs[0]["lat"], legs[0]["lon"]), START)
        self.assertIsNone(first_breach(self.planner.airspace, legs))
        self.assertEqual(drafter.name, "nano:scripted-nano")

    def test_json_only_inside_think_is_garbage_then_a_second_ask(self):
        drafter = self.drafter("<think>" + json.dumps(_hand_drawn()) + "</think>",
                               json.dumps(_hand_drawn()))
        legs = drafter.draft(START, GOAL)
        self.assertIsNotNone(legs)
        self.assertEqual(drafter.last_attempts, 2)
        self.assertIn("not a", drafter.last_failures[0])
        self.assertIn("previous draft was refused", drafter.llm.prompts[1])

    def test_a_draft_through_a_building_is_retried_with_the_exact_failure(self):
        """직선(창고 옆 건물 관통)을 두 번 보내면 두 번 다 거절되고 None 입니다."""
        through = {"legs": [{"lat": START[0], "lon": START[1], "alt_m": 60},
                            {"lat": 40.7985, "lon": -73.955, "alt_m": 60}]}
        goal = (40.7985, -73.955)
        drafter = self.drafter(json.dumps(through), json.dumps(through))
        self.assertIsNone(drafter.draft(START, goal))
        self.assertEqual(drafter.last_attempts, 2)
        self.assertEqual(len(drafter.last_failures), 2)
        self.assertIn("hits", drafter.last_failures[0])
        retry = drafter.llm.prompts[1]
        self.assertIn("previous draft was refused", retry)
        self.assertIn("judge said", retry)

    def test_no_reply_means_no_second_ask(self):
        drafter = self.drafter(None, json.dumps(_hand_drawn()))
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 1)

    def test_the_draft_call_brings_its_own_budget(self):
        """초안은 6초짜리 신청서 질문이 아닙니다. 한가한 Ollama 에서도 9초, 통과 답은 19초까지."""
        drafter = self.drafter(json.dumps(_hand_drawn()))
        drafter.draft(START, GOAL)
        self.assertEqual(drafter.llm.budgets, [DRAFT_TIMEOUT_S])
        custom = ModelDrafter(ScriptedLlm(json.dumps(_hand_drawn())), self.planner, bbox=self.bbox,
                              timeout_s=12.5)
        custom.draft(START, GOAL)
        self.assertEqual(custom.llm.budgets, [12.5])

    def test_a_server_that_just_timed_out_is_not_asked_again_for_a_while(self):
        """타임아웃 뒤 5.6초 기다렸다 같은 서버에 또 30초를 거는 대신 이번은 A* 차례입니다."""
        import time

        drafter = self.drafter(json.dumps(_hand_drawn()), json.dumps(_hand_drawn()))
        # 초안 호출이 잘린 직후(skip_until). 신청서 호출이 잘린 것(llm.unreachable_at)은 초안과
        # 무관합니다.
        drafter.skip_until = time.monotonic() + drafter.backoff_s
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 0)
        self.assertIn("skipped", drafter.last_failures[0])
        drafter.skip_until = time.monotonic() - 1
        drafter.llm.unreachable_at = time.monotonic()      # 신청서가 방금 잘렸어도 초안은 묻습니다
        self.assertIsNotNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 1)

    def test_disabled_model_never_asks(self):
        llm = TieredLlm(base_url="", models={}, timeout_s=1, request_extra={}, record_dir="")
        drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
        self.assertFalse(drafter.enabled)
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 0)

    def test_the_operator_altitude_rule_lifts_a_leg_that_is_too_low(self):
        """모델이 40m 로 적은 강 위 구간은 그대로, 건물 위 구간은 가장 낮은 안전 고도로."""
        form = _hand_drawn()
        for leg in form["legs"]:
            leg["alt_m"] = 40
        drafter = self.drafter(json.dumps(form))
        legs = drafter.draft(START, GOAL)
        self.assertIsNotNone(legs)
        self.assertTrue(all(40 <= leg["alt_m"] <= ALT_MAX_M for leg in legs))
        self.assertIsNone(first_breach(self.planner.airspace, legs))

    def test_the_brief_reads_the_map_from_the_judge(self):
        drafter = self.drafter()
        goal = (40.7985, -73.955)
        brief = drafter._brief(START, goal, self.bbox, {"reason": "1번 구간이 규정을 어깁니다",
                                                       "forbids": None})
        self.assertIn(f"origin {START[0]:.5f},{START[1]:.5f} -> goal 40.79850,-73.95500", brief)
        self.assertIn("no-fly cell", brief)          # 미드타운 KLGA 0ft 띠
        self.assertIn("clear", brief)                # 어느 쪽이 열려 있는지
        self.assertIn("altitude capped", brief)      # 300ft 칸
        self.assertIn("runtime's refusal", brief)
        hits = drafter.obstacles(START, goal, 120.0)
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(len({v.id for _, v, _, _ in hits}), len(hits))   # 같은 것을 두 번 안 셈

    def test_describe_words(self):
        tall = Volume(id="bldg-1", name="건물 157m", polygon=[(40.71, -73.97), (40.71, -73.969),
                      (40.711, -73.969), (40.711, -73.97)], ceiling_m=157.0, clearance_m=50.0)
        self.assertIn("go around", describe(tall))
        self.assertIn("would need 208 m", describe(tall))       # 157 + 50 + 0.5, 올림
        low = Volume(id="bldg-2", name="건물 44m", polygon=tall.polygon, ceiling_m=44.0,
                     clearance_m=50.0)
        self.assertIn("95 m or higher", describe(low))
        self.assertIn("go around", describe(low, allowed_m=60.0))   # 천장 칸 아래서는 못 넘습니다
        cell = Volume(id="klga-0", name="KLGA 격자 0ft", polygon=tall.polygon, rule="forbidden")
        self.assertIn("every altitude", describe(cell))

    def test_a_deadline_clamps_each_ask_to_what_is_left(self):
        """마감이 있으면 두 질문을 합쳐 그때까지만. 남은 게 2초 미만이면 묻지도 않습니다."""
        import time

        drafter = self.drafter(json.dumps(_hand_drawn()))
        drafter.draft(START, GOAL, deadline=time.monotonic() + 10.0)
        self.assertLessEqual(drafter.llm.budgets[0], 10.0)
        self.assertGreater(drafter.llm.budgets[0], 9.0)
        spent = self.drafter(json.dumps(_hand_drawn()))
        self.assertIsNone(spent.draft(START, GOAL, deadline=time.monotonic() + 0.5))
        self.assertEqual(spent.last_attempts, 0)
        self.assertEqual(spent.llm.budgets, [])
        self.assertIn("budget exhausted", spent.last_failures[0])

    def test_a_retry_is_skipped_when_the_first_ask_took_longer_than_what_is_left(self):
        """실주행에서 잘린 재시도는 11건, 통과는 0건. 같은 크기의 질문을 남은 시간보다 길게 걸면
        마감 뒤에 오는 답을 기다릴 뿐입니다."""
        import time

        class Slow(ScriptedLlm):
            def ask(self, *args, **kwargs):
                reply = super().ask(*args, **kwargs)
                return None if reply is None else LlmReply(text=reply.text, model=reply.model,
                                                           latency_ms=7_000)

        drafter = ModelDrafter(Slow(json.dumps({"legs": "garbage"}),
                                    json.dumps(_hand_drawn())), self.planner, bbox=self.bbox)
        # 마감까지 5초, 첫 답(양식 아님)이 7초 걸렸다고 보고합니다 → 같은 질문을 또 걸어봐야
        # 마감 뒤에 옵니다. 다시 묻지 않습니다
        drafter.draft(START, GOAL, deadline=time.monotonic() + 5.0)
        self.assertEqual(drafter.last_attempts, 1)
        self.assertEqual(len(drafter.llm.prompts), 1)
        self.assertIn("retry skipped", drafter.last_failures[-1])
        # 마감 없이(질문마다 예산 하나) 물으면 그대로 두 번 묻습니다
        again = ModelDrafter(Slow(json.dumps({"legs": "garbage"}), json.dumps(_hand_drawn())),
                             self.planner, bbox=self.bbox)
        self.assertIsNotNone(again.draft(START, GOAL))
        self.assertEqual(again.last_attempts, 2)


class GoAroundUnderACeilingCellTest(unittest.TestCase):
    """천장 칸 안의 건물. 120m 스캔은 칸 자체에 걸려 칸을 통째로 건너뛰므로 따로 봐야 합니다.

    실주행(맥캐런 공원, 90m 칸 uasfm-171132 안의 101m 건물 bldg-t03281): GO AROUND 목록이 비어
    좌우 두 점을 다 받은 4B 가 건물 사이를 지그재그로 관통했습니다.
    """

    START, GOAL = (40.72060, -73.95200), (40.71819, -73.97575)

    def setUp(self):
        from holdshort.core.geo import box

        self.planner = OperatorPlanner()
        along = (40.71922, -73.96392)         # 직선 위, 출발점에서 약 1.0 km
        self.planner.airspace.add(Volume(
            id="cell-90", name="KLGA 300ft", rule="ceiling", ceiling_m=91.4,
            polygon=box(40.7150, -73.9700, 40.7260, -73.9520)))
        self.planner.airspace.add(Volume(
            id="bldg-101", name="건물 101m", rule="forbidden", ceiling_m=101.0, clearance_m=50.0,
            polygon=box(along[0] - 0.00017, along[1] - 0.00022,
                        along[0] + 0.00017, along[1] + 0.00022)))
        self.planner.airspace.add(Volume(
            id="bldg-60", name="건물 60m", rule="forbidden", ceiling_m=60.0, clearance_m=50.0,
            polygon=box(40.71880, -73.95700, 40.71910, -73.95660)))
        self.drafter = ModelDrafter(FixtureLlm(records=[]), self.planner, bbox=_bbox())

    def test_the_building_inside_the_cell_is_a_go_around_and_gets_one_side(self):
        around = self.drafter.go_arounds(self.START, self.GOAL)
        self.assertEqual([v.id for v, _ in around], ["bldg-101"],
                         "60m 건물은 111m 로 넘을 수 있어 돌아갈 것이 아닙니다")
        self.assertTrue(self.drafter.must_go_around(*around[0]))
        # 120m 만으로 재면 칸만 보이고 건물은 빠집니다 — 그래서 따로 재는 것입니다
        # 120 m 직선 스캔에도 건물이 나옵니다 — 예전에는 칸 진입에서 멈춰 그 안의 건물이 빠졌는데,
        # leg_breaches 가 구간이 어기는 것을 전부 모으므로 칸과 건물이 둘 다 목록에 있습니다.
        self.assertIn("bldg-101", {v.id for _, v, _, _ in
                                   self.drafter.obstacles(self.START, self.GOAL, ALT_MAX_M)})
        brief = self.drafter._brief(self.START, self.GOAL, _bbox(), {})
        head, _, _ = brief.partition("The straight line at")
        self.assertIn("GO AROUND", head)
        self.assertIn("bldg-101", head)
        self.assertIn("would need 152 m, limit there 90 m", head)
        self.assertEqual(len([line for line in head.splitlines() if " of it (" in line]), 1,
                         "돌 쪽은 하나만 말합니다")
        self.assertRegex(head, r"pass (NORTH|SOUTH)(-[A-Z]+)? of it \((left|right)\), e\.g\. via")


class FeedbackTest(unittest.TestCase):
    """두 번째 질문은 무엇에 걸렸고 어느 쪽으로 얼마나 비켜야 하는지를 숫자로 듭니다.

    녹음(drafts_nano.json, 브루클린브리지파크): 진짜 nano 가 덤보 강변의 77m 건물(bldg-t02419)을
    10m 로 스쳤습니다. 77 + 50 + 0.5 = 128 m 가 필요하고 한계는 120 m 라 돌아가야 합니다.
    """

    @classmethod
    def setUpClass(cls):
        cls.planner = _fleet_planner()
        cls.bbox = _bbox()
        records = [r for r in load_fixtures() if r.get("kind") == "draft"
                   and r.get("area") == "Brooklyn Bridge Park" and r.get("expect") == "fail"]
        if not records:
            raise unittest.SkipTest("브루클린브리지파크 거절 녹음이 없습니다")
        cls.record = records[0]
        cls.start = tuple(cls.record["start"])
        cls.goal = tuple(cls.record["goal"])
        # 두 번째 답으로 쓸, 강 위로 도는 손 경로(seat 1 에서도 판정을 넘는지 시험이 먼저 봅니다)
        cls.clearing = {"tier": "nano", "model": "nemotron-3-nano",
                        "needle": "Your previous draft was refused",
                        "text": json.dumps(_hand_drawn()), "source": "hand-drawn"}
        legs, why = ModelDrafter.validate(_hand_drawn(), cls.start, cls.goal, cls.bbox)
        assert why is None, why
        assert first_breach(cls.planner.airspace, legs) is None

    def test_the_brief_lists_go_arounds_up_front_with_one_side_to_pass(self):
        drafter = ModelDrafter(FixtureLlm(records=[]), self.planner, bbox=self.bbox)
        brief = drafter._brief(self.start, self.goal, self.bbox, {})
        head, _, rest = brief.partition("The straight line at")
        self.assertIn("GO AROUND", head)
        self.assertIn("bldg-t02419", head)
        self.assertIn("would need 128 m", head)
        self.assertIn("pass SOUTH of it (left), e.g. via", head)
        self.assertIn("never alternate between left and right", head)
        self.assertIn("bldg-t02419", rest)             # 순서대로 읽는 목록에도 있습니다
        around = drafter.go_arounds(self.start, self.goal)
        self.assertTrue(all(drafter.must_go_around(v, at) for v, at in around))
        self.assertIn("bldg-t02419", {v.id for v, _ in around})

    def test_the_second_ask_names_the_building_its_roof_and_the_side_to_pass(self):
        llm = FixtureLlm(records=[self.record])          # 첫 질문에만 답합니다
        drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
        self.assertIsNone(drafter.draft(self.start, self.goal, {}))
        self.assertEqual(drafter.last_attempts, 2)
        self.assertEqual(len(llm.asked), 2)
        retry = llm.asked[1][1]
        feedback = retry.split("previous draft was refused")[1]
        self.assertIn("bldg-t02419", feedback)
        self.assertIn("roof 77 m", feedback)
        self.assertIn("would need 128 m", feedback)
        self.assertIn("above the 120 m limit", feedback)
        self.assertIn("MUST fly around it", feedback)
        self.assertRegex(feedback, r"pass (NORTH|SOUTH|EAST|WEST)(-[A-Z]+)? of it")
        self.assertIn("climbing cannot fix it", feedback)

    def test_a_draft_that_clears_after_feedback_is_returned_with_two_attempts(self):
        # 재시도 녹음을 앞에 둡니다: 첫 질문에는 그 바늘이 없어 첫 녹음이 답하고, 두 번째에는
        # 두 프롬프트가 다 맞는데 앞의 것이 이깁니다.
        llm = FixtureLlm(records=[self.clearing, self.record])
        drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
        legs = drafter.draft(self.start, self.goal, {})
        self.assertIsNotNone(legs, drafter.last_failures)
        self.assertEqual(drafter.last_attempts, 2)
        self.assertEqual(llm.served, [self.record["_file"], "Your previous draft was refused"])
        self.assertIsNone(first_breach(self.planner.airspace, legs))
        self.assertEqual((legs[0]["lat"], legs[0]["lon"]), self.start)
        self.assertEqual((legs[-1]["lat"], legs[-1]["lon"]), self.goal)

    def test_pick_side_sticks_to_the_previous_side_when_it_is_open(self):
        near_left = {"left": {"compass": "south", "metres": 80, "point": (0, 0)},
                     "right": {"compass": "north", "metres": 80, "point": (0, 0)}}
        self.assertEqual(ModelDrafter.pick_side(near_left, None), "left")
        self.assertEqual(ModelDrafter.pick_side(near_left, "right"), "right")
        far_right = {"left": {"compass": "south", "metres": 80, "point": (0, 0)},
                     "right": {"compass": "north", "metres": 400, "point": (0, 0)}}
        # 두 배 넘게 멀면 바꿉니다
        self.assertEqual(ModelDrafter.pick_side(far_right, "right"), "left")
        blocked = {"left": {"compass": "south", "metres": None, "point": None},
                   "right": {"compass": "north", "metres": None, "point": None}}
        self.assertIsNone(ModelDrafter.pick_side(blocked, "left"))


class RecordedRepliesTest(unittest.TestCase):
    """녹음된 실제 Nemotron 답. 양식은 통과해야 하고, 판정은 통과할 수도 안 할 수도 있습니다.

    모델이 그린 길이 판정을 못 넘는 것은 고장이 아니라 이 설계가 예상한 일입니다. 시험이
    보는 것은 그 답이 코드의 검사를 지나 판정에 닿는지, 그리고 통과한 것이 정말 통과인지입니다.
    """

    def setUp(self):
        self.records = [r for r in load_fixtures() if r.get("kind") == "draft"]
        if not self.records:
            self.skipTest(f"녹음된 초안이 없습니다 ({FIXTURE_DIR})")
        self.planner = _fleet_planner()
        self.bbox = _bbox()

    def test_every_recorded_draft_parses_as_a_form(self):
        from holdshort.llm.client import parse_json_object

        for record in self.records:
            with self.subTest(file=record["_file"]):
                form = parse_json_object(record["text"])
                self.assertIsInstance(form, dict)
                self.assertIsInstance(form.get("legs"), list)

    def test_accepted_recordings_really_pass_the_judge_and_refused_ones_really_fail(self):
        seen_pass = False
        for record in self.records:
            start, goal = tuple(record["start"]), tuple(record["goal"])
            llm = FixtureLlm(records=[record])
            drafter = ModelDrafter(llm, self.planner, bbox=self.bbox)
            legs = drafter.draft(start, goal, {})
            with self.subTest(file=record["_file"], expect=record.get("expect")):
                if record.get("expect") == "pass":
                    self.assertIsNotNone(legs, drafter.last_failures)
                    self.assertIsNone(first_breach(self.planner.airspace, legs))
                    seen_pass = True
                else:
                    self.assertIsNone(legs)
                    self.assertTrue(drafter.last_failures)
        self.assertTrue(seen_pass, "통과하는 녹음이 하나는 있어야 fixture 시험이 뜻이 있습니다")

    def test_the_fixture_says_whether_the_straight_line_was_refused(self):
        """직선이 통과하는 자리의 초안은 실제 흐름에서 쓰이지 않습니다. 기록이 그걸 말해야
        합니다."""
        for record in self.records:
            straight = self.planner.straight(tuple(record["start"]), tuple(record["goal"]))
            refused = first_breach(self.planner.airspace, straight) is not None
            with self.subTest(file=record["_file"], area=record.get("area")):
                self.assertEqual(bool(record.get("straight_refused")), refused)


class RuntimeNeverReadsTheDrafterTest(unittest.TestCase):
    def test_no_runtime_file_mentions_the_drafter(self):
        """누가 그렸는지는 원장에만 남고 판정에는 안 들어갑니다. grep 으로 못박습니다."""
        offenders = []
        for path in RUNTIME_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for needle in ("drafter", "draft_attempts"):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
