"""A route the model sketches is a proposal, never a route.

Every check here is code: shape, count, box, altitude band, snapped ends, length, and
then the same judge the runtime uses. The model can be garbage, adversarial, or right —
what leaves `draft()` is either something the judge passed or nothing.
"""

import json
import pathlib
import unittest

from attache.agent.drafter import (
    ALT_MAX_M,
    DRAFT_TIMEOUT_S,
    MAX_LEGS,
    ModelDrafter,
    describe,
    service_bbox,
)
from attache.agent.planner import OperatorPlanner
from attache.core.geo import Volume, first_breach
from attache.llm.client import LlmReply, TieredLlm
from sim.world import LANDING_AREAS, Simulation, seat_of, to_latlon
from tests.fixture_llm import FIXTURE_DIR, FixtureLlm, load_fixtures

RUNTIME_DIR = pathlib.Path(__file__).resolve().parent.parent / "attache" / "runtime"


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
        {"lat": START[0], "lon": START[1], "alt_m": 60},
        {"lat": 40.70115, "lon": -73.97055, "alt_m": 40},
        {"lat": 40.70160, "lon": -73.99575, "alt_m": 114},
        {"lat": GOAL[0], "lon": GOAL[1], "alt_m": 40},
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
        drafter.llm.unreachable_at = time.monotonic()
        self.assertIsNone(drafter.draft(START, GOAL))
        self.assertEqual(drafter.last_attempts, 0)
        self.assertIn("skipped", drafter.last_failures[0])
        drafter.llm.unreachable_at = time.monotonic() - drafter.backoff_s - 1
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
        low = Volume(id="bldg-2", name="건물 44m", polygon=tall.polygon, ceiling_m=44.0,
                     clearance_m=50.0)
        self.assertIn("95 m or higher", describe(low))
        cell = Volume(id="klga-0", name="KLGA 격자 0ft", polygon=tall.polygon, rule="forbidden")
        self.assertIn("every altitude", describe(cell))


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
        from attache.llm.client import parse_json_object

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
        """직선이 통과하는 자리의 초안은 실제 흐름에서 쓰이지 않습니다. 기록이 그걸 말해야 합니다."""
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
