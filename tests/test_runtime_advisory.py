"""A tower advisory is information after repeated refusals. The runtime writes it; nothing changes.

Three refusals in a row (or a decline after refusals) make the runtime list the options the
code can see and check each one with the same deterministic judge. A model may pick one id
out of that list and phrase the summary; anything else it says is dropped and a rule picks.
"""

import json
import tempfile
import unittest

from holdshort.core.geo import Volume, box
from holdshort.core.models import Proposal, Verdict
from holdshort.llm.client import LlmReply, TieredLlm
from holdshort.runtime.advisory import (
    ADVISORY_AFTER,
    CLIMB_M,
    AdvisoryDesk,
    Option,
    Refusal,
    build_options,
    parse_advice,
    rule_pick,
)
from holdshort.runtime.service import Runtime
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
ZONE_ITEM = {"id": "nofly-t", "kind": "notam", "name": "시험 회랑", "text": sim_world.ZONE_TEXT,
             "published_tick": 525, "until_tick": 900}
HERE = (40.7100, -73.9855)


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


class StubSuper(TieredLlm):
    """정해진 문장으로 답하는 Super 티어."""

    def __init__(self, text: str):
        super().__init__(base_url="http://stub", models={"super": "stub-super"},
                         timeout_s=0.0, request_extra={}, record_dir="")
        self.text = text
        self.asked = []

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        self.asked.append((tier.value, user))
        reply = LlmReply(text=self.text, model="stub-super")
        self.account(tier, reply, 0)
        return reply


def make_runtime(llm=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.advisory_async = False       # 시험은 권고가 원장에 적힌 다음을 봅니다
    runtime.notice_async = False
    if llm is not None:
        runtime.llm = llm
        runtime.notices.llm = llm
        runtime.advisor.llm = llm
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.telemetry = {"drone-01": {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0}}
    runtime.tick = 600
    return runtime, adapter


def ledger_lines(runtime):
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


def advisories(runtime):
    return [e for e in ledger_lines(runtime) if e["proposal"]["action"] == "advisory"]


def legs(points, alt_m):
    return [{"lat": lat, "lon": lon, "alt_m": alt_m} for lat, lon in points]


THROUGH_ZONE = legs([HERE, (40.7225, -73.9855), (40.7350, -73.9855)], 60)
AWAY = legs([HERE, (40.7000, -73.9855)], 60)


def file_route(runtime, route, asset="drone-01", **params):
    return runtime.file(Proposal(asset_id=asset, action="fly_route", cost_usd=12.0,
                                 blast_radius="schedule", rationale="",
                                 params={"legs": route, **params}).to_dict())


class TriggerTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.absorb([ZONE_ITEM])

    def test_exactly_three_consecutive_refusals_and_a_success_resets(self):
        for _ in range(ADVISORY_AFTER - 1):
            self.assertIs(file_route(self.runtime, THROUGH_ZONE).verdict, Verdict.DENIED)
        self.assertEqual(advisories(self.runtime), [], "두 번은 재작성 사다리 안입니다")
        self.assertEqual(self.runtime.snapshot()["advisories"], [])
        # 승인이 하나 나가면 연속은 끊깁니다
        self.assertTrue(file_route(self.runtime, AWAY).committed)
        self.runtime.tick += 20         # 중복 방지 창 밖
        for _ in range(ADVISORY_AFTER - 1):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(advisories(self.runtime), [])
        file_route(self.runtime, THROUGH_ZONE)
        noted = advisories(self.runtime)
        self.assertEqual(len(noted), 1)
        entry = noted[0]
        self.assertEqual((entry["proposal"]["author"], entry["decision"]["verdict"],
                          entry["decision"]["code"], entry["outcome"]),
                         ("runtime", "auto", "advisory", "noted"))
        self.assertEqual(entry["context"]["checks_run"], ["advisory"])
        params = entry["proposal"]["params"]
        self.assertEqual(params["trigger"], "refusals")
        self.assertEqual(len(params["refusals"]), ADVISORY_AFTER, "승인 뒤의 거절만 셉니다")
        self.assertEqual({r["code"] for r in params["refusals"]}, {"airspace"})
        self.assertEqual({r["blocked_volume"] for r in params["refusals"]}, {"nofly-t"})
        shown = self.runtime.snapshot()["advisories"]
        self.assertEqual([(a["asset"], a["ledger_id"], a["chosen"]) for a in shown],
                         [("drone-01", entry["id"], params["chosen"])])
        # 네 번째 거절에는 새 권고가 없고, 실행된 것도 없습니다 — 권고는 아무것도 바꾸지 않습니다
        file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(advisories(self.runtime)), 1)
        self.assertEqual([s[1] for s in self.adapter.sent], ["fly_route"])

    def test_the_zone_offers_the_notice_window_and_the_lifted_route_is_still_illegal(self):
        for _ in range(ADVISORY_AFTER):
            file_route(self.runtime, THROUGH_ZONE)
        params = advisories(self.runtime)[0]["proposal"]["params"]
        by_id = {o["id"]: o for o in params["options"]}
        self.assertEqual(list(by_id), ["climb", "notice_window", "decline", "escalate"])
        self.assertFalse(by_id["climb"]["legal"], "구역은 400ft 까지라 30m 올려도 안입니다")
        self.assertIn("시험 회랑", by_id["climb"]["why"])
        self.assertEqual((by_id["notice_window"]["legal"], by_id["notice_window"]["until_tick"]),
                         (True, 900))
        self.assertTrue(by_id["decline"]["legal"] and by_id["escalate"]["legal"])
        self.assertEqual((params["chosen"], params["source"], params["model"]),
                         ("notice_window", "rules", ""))
        self.assertIn("3 times in a row", params["summary"])
        self.assertIn("notice window", params["summary"])

    def test_a_decline_after_refusals_is_an_advisory_and_a_plain_decline_is_not(self):
        decline = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                           blast_radius="none", rationale="규정상 경로 없음")
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        self.assertEqual(advisories(self.runtime), [], "거절이 없었으면 권고도 없습니다")
        file_route(self.runtime, THROUGH_ZONE)
        self.runtime.tick += 20
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        noted = advisories(self.runtime)
        self.assertEqual(len(noted), 1)
        params = noted[0]["proposal"]["params"]
        self.assertEqual((params["trigger"], len(params["refusals"])),
                         ("decline_after_refusals", 1))
        self.assertIn("declined the order after 1 refusals", params["summary"])
        self.assertEqual(self.runtime.snapshot()["advisories"][0]["trigger"],
                         "decline_after_refusals")

    def test_the_same_block_is_not_advised_again_and_a_new_block_is(self):
        """실주행: 남이 착륙대를 쓰는 동안 몇 틱마다 재신청 → 세 번째마다 같은 '틱 X 까지 대기'.
        같은 막힘이 이어지는 동안은 한 번이고, 막힘이 바뀌어 다시 세 번 쌓이면 또 씁니다."""
        for _ in range(ADVISORY_AFTER * 3):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(advisories(self.runtime)), 1)
        # 다른 것에 막힙니다(구역 대신 건물) → 세 번 쌓이면 새 권고
        self.runtime.airspace.add(Volume(id="bldg-new", name="새 건물",
                                         polygon=box(40.7040, -73.9860, 40.7050, -73.9850),
                                         floor_m=0.0, ceiling_m=130.0, clearance_m=50.0,
                                         rule="forbidden"))
        south = legs([HERE, (40.7045, -73.9855), (40.7000, -73.9855)], 60)
        for index in range(ADVISORY_AFTER):
            self.assertEqual(file_route(self.runtime, south).forbids, "bldg-new")
            self.assertEqual(len(advisories(self.runtime)), 1 if index < ADVISORY_AFTER - 1 else 2)
        self.assertEqual({r["blocked_volume"] for r in
                          advisories(self.runtime)[1]["proposal"]["params"]["refusals"]},
                         {"nofly-t", "bldg-new"})

    def test_a_committed_decline_resets_the_streak(self):
        decline = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                           blast_radius="none", rationale="규정상 경로 없음")
        file_route(self.runtime, THROUGH_ZONE)
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        self.assertEqual(len(advisories(self.runtime)), 1)
        self.assertEqual(self.runtime.advisor.streaks, {}, "주문이 사라졌으니 연속도 끊깁니다")
        self.runtime.tick += 20
        for _ in range(ADVISORY_AFTER - 1):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(advisories(self.runtime)), 1, "새 주문의 거절은 새로 셉니다")
        file_route(self.runtime, THROUGH_ZONE)
        noted = advisories(self.runtime)
        self.assertEqual(len(noted), 2)
        self.assertEqual(len(noted[1]["proposal"]["params"]["refusals"]), ADVISORY_AFTER)

    def test_a_duplicate_decline_writes_no_second_advisory(self):
        decline = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                           blast_radius="none", rationale="규정상 경로 없음")
        file_route(self.runtime, THROUGH_ZONE)
        self.assertTrue(self.runtime.file(decline.to_dict()).committed)
        again = self.runtime.file(Proposal(asset_id="drone-01", action="decline_job",
                                           cost_usd=0.0, blast_radius="none",
                                           rationale="규정상 경로 없음").to_dict())
        self.assertEqual((again.verdict, again.code), (Verdict.DENIED, "duplicate"))
        self.assertEqual(len(advisories(self.runtime)), 1)
        self.assertEqual([s[1] for s in self.adapter.sent], ["decline_job"])

    def test_a_model_summary_that_arrives_after_the_round_changed_is_dropped(self):
        import threading
        import time

        class SlowSuper(StubSuper):
            def ask(self, *args, **kwargs):
                time.sleep(0.2)
                return super().ask(*args, **kwargs)

        runtime, _ = make_runtime(SlowSuper(json.dumps({"choice": "decline", "summary": "x"})))
        runtime.advisory_async = True
        runtime._round = 1
        runtime.absorb([ZONE_ITEM])
        for _ in range(ADVISORY_AFTER):
            file_route(runtime, THROUGH_ZONE)
        runtime._follow_round(2)
        for thread in threading.enumerate():
            if thread.name.startswith("advisory-"):
                thread.join(2.0)
        self.assertEqual(runtime.snapshot()["advisories"], [])
        self.assertEqual(advisories(runtime), [])

    def test_a_new_round_clears_the_streak_and_the_card(self):
        for _ in range(ADVISORY_AFTER):
            file_route(self.runtime, THROUGH_ZONE)
        self.assertEqual(len(self.runtime.snapshot()["advisories"]), 1)
        self.runtime._follow_round(7)
        self.assertEqual(self.runtime.snapshot()["advisories"], [])
        self.assertEqual(self.runtime.advisor.streaks, {})


class JudgeTest(unittest.TestCase):
    """선택지의 합법 여부는 같은 판정 함수가 말합니다."""

    def roof(self, ceiling_m: float):
        runtime, adapter = make_runtime()
        runtime.airspace.add(Volume(id="bldg-roof", name=f"roof {ceiling_m:.0f}",
                                    polygon=box(40.7190, -73.9860, 40.7200, -73.9850),
                                    floor_m=0.0, ceiling_m=ceiling_m, clearance_m=50.0,
                                    rule="forbidden"))
        return runtime, adapter

    def test_a_lifted_route_over_a_130_m_roof_is_illegal(self):
        runtime, _ = self.roof(130.0)
        route = legs([HERE, (40.7195, -73.9855), (40.7350, -73.9855)], 60)
        for _ in range(ADVISORY_AFTER):
            decision = file_route(runtime, route)
            self.assertEqual(decision.forbids, "bldg-roof")
        params = advisories(runtime)[0]["proposal"]["params"]
        climb = next(o for o in params["options"] if o["id"] == "climb")
        self.assertEqual((climb["legal"], climb["shift_m"]), (False, CLIMB_M))
        self.assertIn("roof 130", climb["why"])
        self.assertEqual([o["id"] for o in params["options"]], ["climb", "decline", "escalate"])
        self.assertEqual(params["chosen"], "decline")

    def test_a_lifted_route_over_a_30_m_roof_is_legal_and_chosen(self):
        # 옥상 30m + 이격 50m = 80m 까지 막힘. 60m 는 거절, 90m 는 통과합니다.
        runtime, adapter = self.roof(30.0)
        route = legs([HERE, (40.7195, -73.9855), (40.7350, -73.9855)], 60)
        for _ in range(ADVISORY_AFTER):
            decision = file_route(runtime, route)
            self.assertEqual(decision.forbids, "bldg-roof")
        params = advisories(runtime)[0]["proposal"]["params"]
        climb = next(o for o in params["options"] if o["id"] == "climb")
        self.assertTrue(climb["legal"], climb["why"])
        self.assertEqual(params["chosen"], "climb")
        self.assertEqual(adapter.sent, [], "권고는 평가만 하고 실행하지 않습니다")

    def test_a_pad_filed_by_resource_alone_is_judged_against_that_pad(self):
        """reserve_pad 는 resource 만 싣고 params.pad 가 없을 수 있습니다. 끝점 검사의 목적지는
        resource 입니다 — 안 넘기면 권고가 끝점 차이를 못 보고 '올라가면 된다' 고 합니다."""
        runtime, _ = make_runtime()
        runtime.pad_coords = {"pad:launch": (40.7200, -73.9800)}
        runtime.airspace.add(Volume(id="bldg-roof", name="roof 30",
                                    polygon=box(40.7140, -73.9860, 40.7150, -73.9850),
                                    floor_m=0.0, ceiling_m=30.0, clearance_m=50.0,
                                    rule="forbidden"))
        astray = legs([HERE, (40.7145, -73.9855), (40.7200, -73.9855)], 60)   # 끝이 착륙대에서 460m
        for _ in range(ADVISORY_AFTER):
            decision = runtime.file(Proposal(asset_id="drone-01", action="reserve_pad",
                                             cost_usd=28.0, blast_radius="schedule",
                                             rationale="", resource="pad:launch",
                                             params={"legs": astray}).to_dict())
            self.assertIs(decision.verdict, Verdict.DENIED)
            self.assertIn("끝점", decision.reason)
        params = advisories(runtime)[0]["proposal"]["params"]
        climb = next(o for o in params["options"] if o["id"] == "climb")
        self.assertFalse(climb["legal"], climb["why"])
        self.assertIn("끝점", climb["why"])

    def test_a_crossing_offers_holding_until_the_other_corridor_clears(self):
        runtime, adapter = make_runtime()
        runtime.telemetry["drone-02"] = {"lat": 40.7100, "lon": -73.9800, "alt_m": 0.0}
        self.assertTrue(file_route(runtime, legs([(40.7100, -73.9800), (40.7400, -73.9800)], 60),
                                   asset="drone-02").committed)
        runtime.telemetry["drone-01"] = {"lat": 40.7200, "lon": -73.9900, "alt_m": 0.0}
        crossing = legs([(40.7200, -73.9900), (40.7200, -73.9700)], 60)
        for _ in range(ADVISORY_AFTER):
            decision = file_route(runtime, crossing)
            self.assertEqual(decision.policy_hit, "traffic")
        params = advisories(runtime)[0]["proposal"]["params"]
        by_id = {o["id"]: o for o in params["options"]}
        self.assertEqual(list(by_id), ["hold", "climb", "decline", "escalate"])
        until = decision.detail["blocked_until_tick"]
        self.assertEqual((by_id["hold"]["legal"], by_id["hold"]["until_tick"]), (True, until))
        self.assertIn("drone-02", by_id["hold"]["why"])
        self.assertTrue(by_id["climb"]["legal"], "상대 회랑은 위아래 ±27m 라 30m 위는 비켜 갑니다")
        self.assertEqual(params["chosen"], "hold")
        self.assertEqual({r["blocked_asset"] for r in params["refusals"]}, {"drone-02"})


class ModelTest(unittest.TestCase):
    """모델은 목록에서 하나를 고르고 요약을 씁니다. 목록 밖이면 규칙이 고르고 문구도 틀로 씁니다."""

    def setUp(self):
        self.refusals = [Refusal("drone-01", 600 + i, "airspace", "traffic", "traffic", None,
                                 "drone-02", 700, f"p{i}", "fly_route", legs=THROUGH_ZONE)
                         for i in range(3)]
        self.options = build_options(self.refusals, lambda refusal, route: None,
                                     lambda volume_id: None, airborne=False)

    def test_options_are_ordered_and_the_rule_takes_the_first_legal_one(self):
        self.assertEqual([o.id for o in self.options], ["hold", "climb", "decline", "escalate"])
        self.assertEqual(rule_pick(self.options), "hold")
        self.assertEqual(rule_pick([Option("hold", "", False, ""), Option("climb", "", True, "")]),
                         "climb")
        airborne = build_options(self.refusals, lambda r, route: "roof", lambda v: None, True)
        self.assertFalse(airborne[0].legal, "떠 있는 기체는 지상 대기를 할 수 없습니다")
        self.assertFalse(airborne[1].legal)
        self.assertEqual(rule_pick(airborne), "decline")

    def test_a_choice_in_the_list_is_kept_with_the_models_summary(self):
        llm = StubSuper(json.dumps({"choice": "climb", "summary": "Two crossings with drone-02. "
                                    "Climb thirty metres and refile."}))
        desk = AdvisoryDesk(llm)
        params = desk.compose("drone-01", "refusals", self.refusals, self.options, False)
        self.assertEqual((params["chosen"], params["source"], params["model"]),
                         ("climb", "super", "stub-super"))
        self.assertTrue(params["summary"].startswith("Two crossings"))
        self.assertIn("[hold] hold on the ground until tick 700 — legal", llm.asked[0][1])

    def test_a_choice_outside_the_list_is_ignored_and_the_rule_picks(self):
        for text in (json.dumps({"choice": "teleport", "summary": "Teleport past drone-02."}),
                     "just climb", ""):
            llm = StubSuper(text)
            desk = AdvisoryDesk(llm)
            params = desk.compose("drone-01", "refusals", self.refusals, self.options, False)
            self.assertEqual((params["chosen"], params["source"]), ("hold", "rules"), text)
            self.assertIn("The rules suggest: hold on the ground until tick 700",
                          params["summary"])
            self.assertNotIn("Teleport", params["summary"])
            self.assertEqual(llm.stats["super"].fallback, 1)

    def test_a_choice_echoed_with_the_brackets_of_the_list_is_read(self):
        """실주행: 권고 160건 중 155건이 "[hold]" 로 답해 규칙 선택으로 떨어졌습니다."""
        for text in ('{"choice": "[hold]", "summary": "Wait it out."}',
                     '{"choice": " [HOLD] ", "summary": "Wait it out."}',
                     '{"choice": "\'hold\'", "summary": "Wait it out."}'):
            self.assertEqual(parse_advice(text, self.options), ("hold", "Wait it out."), text)
        self.assertEqual(parse_advice('{"choice": "[teleport]"}', self.options), (None, ""))

    def test_an_illegal_pick_counts_as_outside_the_list(self):
        options = [Option("hold", "hold", False, "airborne"),
                   Option("decline", "decline", True, "")]
        self.assertEqual(parse_advice(json.dumps({"choice": "hold", "summary": "x"}), options),
                         (None, "x"))
        self.assertEqual(parse_advice(json.dumps({"choice": "decline"}), options),
                         ("decline", ""))

    def test_no_model_means_a_template_and_no_call(self):
        desk = AdvisoryDesk(TieredLlm(base_url="", models={}))
        self.assertFalse(desk.has_model)
        params = desk.compose("drone-01", "refusals", self.refusals, self.options, False)
        self.assertEqual((params["chosen"], params["source"], params["model"]),
                         ("hold", "rules", ""))
        self.assertEqual(params["summary"],
                         "drone-01 was refused 3 times in a row (traffic). "
                         "The rules suggest: hold on the ground until tick 700.")

    def test_the_runtime_records_the_model_word_on_the_ledger(self):
        llm = StubSuper(json.dumps({"choice": "decline", "summary": "The zone is closed until "
                                    "tick 900. Decline this order."}))
        runtime, _ = make_runtime(llm)
        runtime.absorb([ZONE_ITEM])
        for _ in range(ADVISORY_AFTER):
            file_route(runtime, THROUGH_ZONE)
        entry = advisories(runtime)[0]
        self.assertEqual(entry["decision"]["detail"],
                         {"resource": "drone-01", "chosen": "decline", "trigger": "refusals",
                          "source": "super"})
        self.assertEqual(entry["proposal"]["params"]["model"], "stub-super")
        self.assertEqual(runtime.snapshot()["advisories"][0]["summary"],
                         "The zone is closed until tick 900. Decline this order.")
        self.assertEqual([tier for tier, _ in llm.asked], ["super"])


if __name__ == "__main__":
    unittest.main()
