"""A notice arrives as a sentence. What the runtime does with it depends on who read it.

The grammar reads the FAA dialect and the rule applies the tick it lands. Prose the grammar
cannot read goes to a model, is validated hard, and waits for a person. Neither path lets
the simulator hand the runtime a polygon: the bulletin feed is text.
"""

import json
import tempfile
import unittest

from attache.core.geo import box
from attache.core.models import Verdict
from attache.core.notam import (
    MAX_AREA_M2,
    Clock,
    Notice,
    area_m2,
    format_dms,
    parse_dms,
    parse_notice,
    validate,
)
from attache.llm.client import LlmReply, TieredLlm
from attache.runtime.service import Runtime
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"


class GrammarTest(unittest.TestCase):
    def test_dms_round_trips_to_the_second(self):
        for lat, lon in ((40.71950, -73.98900), (40.72550, -73.98200), (-33.8688, 151.2093)):
            token = format_dms(lat, lon)
            back = parse_dms(token)
            self.assertAlmostEqual(back[0], lat, delta=0.5 / 3600)
            self.assertAlmostEqual(back[1], lon, delta=0.5 / 3600)
        self.assertEqual(format_dms(40.71950, -73.98900), "404310N0735920W")
        self.assertEqual(parse_dms("404310N0735920W"), (40 + 43 / 60 + 10 / 3600,
                                                        -(73 + 59 / 60 + 20 / 3600)))
        with self.assertRaises(ValueError):
            parse_dms("404370N0735920W")     # 70초는 없습니다

    def test_the_simulators_notice_parses_to_its_own_polygon_and_window(self):
        notice = parse_notice(sim_world.ZONE_TEXT, sim_world.CLOCK)
        self.assertIsNotNone(notice)
        self.assertEqual([[lat, lon] for lat, lon in notice.polygon], sim_world.ZONE["polygon"])
        self.assertEqual((notice.from_tick, notice.until_tick),
                         (sim_world.ZONE_TICK, sim_world.ZONE_UNTIL))
        self.assertEqual((notice.from_tick, notice.until_tick), (525, 900))
        self.assertAlmostEqual(notice.ceiling_m, 121.92)
        self.assertEqual(notice.floor_m, 0.0)
        self.assertEqual(notice.reference, "AGL")
        self.assertTrue(sim_world.ZONE_VOLUME.covers(40.7225, -73.9855))

    def test_radius_tick_window_and_a_name(self):
        notice = parse_notice("HOSPITAL PAD: 0.5NM RADIUS OF 404310N0735920W SFC-UNL TICK 10-20")
        self.assertEqual(notice.name, "HOSPITAL PAD")
        self.assertEqual(len(notice.polygon), 16)
        self.assertIsNone(notice.ceiling_m)
        self.assertEqual((notice.from_tick, notice.until_tick), (10, 20))
        self.assertAlmostEqual(area_m2(notice.polygon) / 1e6, 3.14 * (0.5 * 1.852) ** 2, delta=0.1)

    def test_floor_and_ceiling_in_feet(self):
        notice = parse_notice("AREA BOUNDED BY 404310N0735920W 404310N0735855W 404332N0735855W "
                              "200FT-1000FT AMSL 0930-1030Z")
        self.assertAlmostEqual(notice.floor_m, 60.96)
        self.assertAlmostEqual(notice.ceiling_m, 304.8)
        self.assertEqual(notice.reference, "AMSL")
        self.assertEqual((notice.from_tick, notice.until_tick), (2250, 6750))

    def test_prose_is_not_read(self):
        for text in ("Emergency helicopter operations over the East Village hospital until noon",
                     "AREA BOUNDED BY 404310N0735920W 404310N0735855W",   # 두 점은 면이 아닙니다
                     "", "   "):
            self.assertIsNone(parse_notice(text), text)

    def test_the_clock_maps_zulu_to_ticks_both_ways(self):
        clock = Clock("0900", 0.8)
        self.assertEqual(clock.tick_of("0907"), 525)
        self.assertEqual(clock.tick_of("0912"), 900)
        self.assertEqual(clock.zulu_of(525), "0907")
        self.assertEqual(Clock("2350", 0.8).tick_of("0010"), 20 * 60 / 0.8)
        with self.assertRaises(ValueError):
            clock.tick_of("2560")


class ValidationTest(unittest.TestCase):
    """모델이 지어낸 것에 거는 검사. 하나라도 걸리면 보류도 안 합니다."""

    def setUp(self):
        self.bbox = (40.68, -74.03, 40.83, -73.93)
        self.good = Notice(polygon=box(40.7195, -73.989, 40.7255, -73.982), ceiling_m=121.9)

    def test_a_reasonable_notice_passes(self):
        self.assertEqual(validate(self.good, self.bbox), [])

    def test_each_gate(self):
        outside = Notice(polygon=box(41.0, -73.0, 41.01, -72.99))
        self.assertTrue(any("밖" in p for p in validate(outside, self.bbox)))
        huge = Notice(polygon=box(40.70, -74.02, 40.75, -73.95))
        self.assertGreater(area_m2(huge.polygon), MAX_AREA_M2)
        self.assertTrue(any("km²" in p for p in validate(huge, self.bbox)))
        self.assertTrue(validate(Notice(polygon=self.good.polygon, floor_m=200.0), self.bbox))
        self.assertTrue(validate(Notice(polygon=self.good.polygon, ceiling_m=2000.0), self.bbox))
        self.assertTrue(validate(Notice(polygon=self.good.polygon, from_tick=9, until_tick=8),
                                 self.bbox))
        self.assertTrue(validate(Notice(polygon=self.good.polygon[:2]), self.bbox))
        many = Notice(polygon=[(40.72 + i * 1e-5, -73.985) for i in range(33)])
        self.assertTrue(validate(many, self.bbox))
        # 상자를 모르면(착륙장 목록 전) 상자 검사만 건너뜁니다
        self.assertEqual(validate(outside, None), [])


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime(llm=None):
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    if llm is not None:
        runtime.llm = llm
        runtime.notices.llm = llm
    runtime.landing_areas = sim_world.LANDING_AREAS
    return runtime, adapter


def ledger_lines(runtime):
    """닫힌 항목만. 원장은 열 때 한 줄, 닫을 때 한 줄이라 같은 id 가 두 번 나옵니다."""
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


class GrammarNoticeEnforcementTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.item = {"id": "nofly-t", "kind": "notam", "name": "시험 회랑",
                     "text": sim_world.ZONE_TEXT, "published_tick": 525, "until_tick": 900}

    def test_it_applies_the_tick_it_lands_and_pulls_a_crossing_flight(self):
        inside = (40.7225, -73.9855)
        self.runtime.telemetry = {"drone-01": {"lat": 40.7150, "lon": -73.9855, "alt_m": 60.0,
                                               "route": [{"lat": 40.7300, "lon": -73.9855,
                                                          "alt_m": 60.0}]}}
        self.runtime.tick = 524
        self.runtime.absorb([self.item])
        self.assertEqual(self.runtime.snapshot()["notices"][0]["applied"], False,
                         "창이 열리기 전에는 예정만 됩니다")
        self.assertIsNone(self.runtime.airspace.breach(*inside, 60.0))
        self.runtime.tick = 525
        self.runtime.absorb([self.item])
        volume = self.runtime.airspace.breach(*inside, 60.0)
        self.assertIsNotNone(volume)
        self.assertEqual(volume.id, "nofly-t")
        self.assertEqual((volume.from_tick, volume.until_tick), (525, 900))
        self.assertEqual(volume.source, "grammar")
        self.assertEqual([s[:2] for s in self.adapter.sent], [("drone-01", "divert_ground")])
        notices = self.runtime.snapshot()["notices"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(set(notices[0]) >= {"id", "name", "kind", "from_tick", "until_tick",
                                             "source", "polygon"}, True)
        self.assertEqual(notices[0]["source"], "grammar")
        self.assertEqual(notices[0]["polygon"], sim_world.ZONE["polygon"])
        self.assertIn("nofly-t", [p["id"] for p in self.runtime.snapshot()["policies"]])

    def test_it_lapses_when_the_window_closes_or_the_feed_drops_it(self):
        self.runtime.tick = 600
        self.runtime.absorb([self.item])
        self.assertIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.runtime.tick = 901
        self.runtime.absorb([self.item])
        self.assertNotIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.runtime.tick = 700
        self.runtime.absorb([self.item])
        self.assertIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.runtime.absorb([])
        self.assertNotIn("nofly-t", {v.id for v in self.runtime.airspace.all()})
        self.assertEqual(self.runtime.snapshot()["notices"], [])

    def test_the_bulletin_feed_is_text_only(self):
        simulation = sim_world.Simulation()
        for _ in range(sim_world.ZONE_TICK):
            simulation.step()
        zone = next(b for b in simulation.bulletins() if b["kind"] == "notam")
        self.assertEqual(zone["text"], sim_world.ZONE_TEXT)
        self.assertNotIn("polygon", zone)
        self.assertEqual((zone["published_tick"], zone["until_tick"]), (525, 900))

    def test_a_route_through_the_notice_is_refused_while_it_holds(self):
        from attache.core.models import Proposal

        self.runtime.tick = 600
        self.runtime.telemetry = {"drone-01": {"lat": 40.7100, "lon": -73.9855, "alt_m": 0.0}}
        self.runtime.absorb([self.item])
        legs = [{"lat": 40.7100, "lon": -73.9855, "alt_m": 60}, {"lat": 40.7225, "lon": -73.9855,
                                                                  "alt_m": 60},
                {"lat": 40.7350, "lon": -73.9855, "alt_m": 60}]
        decision = self.runtime.file(Proposal(asset_id="drone-01", action="fly_route",
                                              cost_usd=12.0, blast_radius="schedule",
                                              rationale="", params={"legs": legs}).to_dict())
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.forbids, "nofly-t")
        self.assertEqual(ledger_lines(self.runtime)[-1]["context"]["policies"], ["nofly-t"])


class ProseNoticeTest(unittest.TestCase):
    PROSE = ("Emergency helicopter operations at the East Village hospital helipad. "
             "Uncrewed aircraft keep clear of the block bounded by E 14th, Ave A, E 10th "
             "and 1st Ave, surface to 400 ft, from 0907Z to 0912Z.")

    def item(self):
        return {"id": "nofly-prose", "kind": "notam", "name": "응급헬기", "text": self.PROSE,
                "published_tick": 525, "until_tick": 900}

    def test_without_a_model_it_is_recorded_as_unreadable_and_never_applies(self):
        runtime, adapter = make_runtime()
        runtime.tick = 600
        runtime.absorb([self.item()])
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        self.assertNotIn("nofly-prose", {v.id for v in runtime.airspace.all()})
        unread = [e for e in ledger_lines(runtime)
                  if e["decision"].get("code") == "notice_unreadable"]
        self.assertEqual(len(unread), 1, "한 번만 기록하고 매 폴링마다 다시 묻지 않습니다")
        self.assertEqual(unread[0]["proposal"]["action"], "publish_notice")
        self.assertEqual(unread[0]["context"]["checks_run"], ["notice:grammar", "notice:model"])
        self.assertEqual(adapter.sent, [])

    def _model(self, polygon, **extra):
        form = {"name": "East Village helipad", "polygon": polygon, "floor_m": 0,
                "ceiling_m": 121.9, "from": "0907", "until": "0912", **extra}

        class StubSuper(TieredLlm):
            def __init__(self):
                super().__init__(base_url="http://stub", models={"super": "stub-super"},
                                 timeout_s=0.0, request_extra={}, record_dir="")
                self.asked = []

            def ask(self, tier, system, user, max_tokens=400, json_object=False,
                    timeout_s=None):
                self.asked.append((tier.value, user))
                return LlmReply(text=json.dumps(form), model="stub-super")

        return StubSuper()

    def test_a_model_compiled_notice_is_held_until_a_person_confirms_it(self):
        llm = self._model([[40.7195, -73.989], [40.7195, -73.982], [40.7255, -73.982],
                           [40.7255, -73.989]])
        runtime, adapter = make_runtime(llm)
        runtime.tick = 600
        inside = (40.7225, -73.9855)
        runtime.absorb([self.item()])
        self.assertEqual([tier for tier, _ in llm.asked], ["super"])
        # 보류: 공역에 없고, 배너에도 없고, 승인 목록에 있습니다
        self.assertIsNone(runtime.airspace.breach(*inside, 60.0))
        self.assertEqual(runtime.snapshot()["notices"], [])
        pending = runtime.snapshot()["awaiting_human"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action"], "publish_notice")
        self.assertEqual(pending[0]["author"], "model:stub-super")
        self.assertEqual(pending[0]["params"]["notice"]["source"], "model:stub-super")
        self.assertEqual(pending[0]["params"]["notice"]["from_tick"], 525)
        self.assertEqual(pending[0]["cost_usd"], 0.0)
        runtime.absorb([self.item()])
        self.assertEqual(len(llm.asked), 1, "같은 공지를 다시 묻지 않습니다")
        # 사람이 확인하면 그때부터 걸립니다 — 사람의 말로
        decision = runtime.approve(pending[0]["id"], "관제사", allow=True)
        self.assertIs(decision.verdict, Verdict.AUTO)
        self.assertEqual(decision.code, "notice_published")
        volume = runtime.airspace.breach(*inside, 60.0)
        self.assertIsNotNone(volume)
        self.assertEqual(volume.source, "human")
        notices = runtime.snapshot()["notices"]
        self.assertEqual((notices[0]["source"], notices[0]["confirmed_by"]), ("human", "관제사"))
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])

    def test_a_person_can_refuse_it_and_nothing_applies(self):
        llm = self._model([[40.7195, -73.989], [40.7195, -73.982], [40.7255, -73.982]])
        runtime, _ = make_runtime(llm)
        runtime.tick = 600
        runtime.absorb([self.item()])
        pending = runtime.snapshot()["awaiting_human"][0]
        decision = runtime.approve(pending["id"], "관제사", allow=False)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.code, "notice_refused")
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertNotIn("nofly-prose", {v.id for v in runtime.airspace.all()})
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [],
                         "거부한 공지를 다시 올리지 않습니다")

    def test_a_model_polygon_outside_the_service_box_is_discarded_not_held(self):
        llm = self._model([[41.5, -72.0], [41.5, -71.99], [41.51, -71.99]])
        runtime, _ = make_runtime(llm)
        runtime.tick = 600
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(runtime.snapshot()["notices"], [])
        unread = [e for e in ledger_lines(runtime)
                  if e["decision"].get("code") == "notice_unreadable"]
        self.assertEqual(len(unread), 1)
        self.assertIn("밖", unread[0]["decision"]["reason"])

    def test_a_model_answer_that_is_not_the_schema_is_discarded(self):
        llm = self._model("north of the hospital")
        runtime, _ = make_runtime(llm)
        runtime.tick = 600
        runtime.absorb([self.item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(llm.stats["super"].fallback, 1)


if __name__ == "__main__":
    unittest.main()
