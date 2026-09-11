"""A notice arrives as a sentence. What the runtime does with it depends on who read it.

The grammar reads the FAA dialect and the rule applies the tick it lands. Prose the grammar
cannot read goes to a model, is validated hard, and waits for a person. Neither path lets
the simulator hand the runtime a polygon: the bulletin feed is text.
"""

import json
import tempfile
import unittest

from attache.core.geo import box
from attache.core.models import Proposal, Verdict
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
from tests.fixture_llm import FixtureLlm, load_fixtures

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
    runtime.notice_async = False        # 시험은 absorb 가 돌아온 다음을 봅니다(스레드는 아래 따로)
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
        # 보류: 공역에 없고, 배너에는 '사람 대기' 로(held), 승인 목록에 있습니다
        self.assertIsNone(runtime.airspace.breach(*inside, 60.0))
        shown = runtime.snapshot()["notices"]
        self.assertEqual([(n["id"], n["held"], n["applied"]) for n in shown],
                         [("nofly-prose", True, False)])
        self.assertEqual([n["id"] for n in runtime.notices.pending()], ["nofly-prose"])
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
        self.assertEqual((notices[0]["held"], notices[0]["applied"]), (False, True))
        self.assertEqual(runtime.notices.pending(), [])
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


def medevac_item() -> dict:
    """시뮬레이터의 두 번째 공지, 공지 목록에 실리는 모양 그대로(문장뿐)."""
    return {"id": sim_world.MEDEVAC["id"], "kind": "notam", "name": sim_world.MEDEVAC["name"],
            "text": sim_world.MEDEVAC_TEXT, "published_tick": sim_world.MEDEVAC_TICK,
            "until_tick": sim_world.MEDEVAC_UNTIL}


def fixture_super(label: str) -> FixtureLlm:
    """tests/fixtures/llm/notices_super.json 의 기록 하나로 답하는 Super 티어."""
    records = [r for r in load_fixtures() if r.get("label") == label]
    assert records, label
    return FixtureLlm(records, model=records[0]["model"], tiers=("super",))


class SecondNoticeTest(unittest.TestCase):
    """두 번째 공지는 문법 밖의 자유 문장입니다. 첫 구역이 걷힌 뒤 문장으로만 옵니다."""

    def test_the_simulator_publishes_prose_the_grammar_cannot_read_once_the_first_zone_lapses(self):
        self.assertIsNone(parse_notice(sim_world.MEDEVAC_TEXT, sim_world.CLOCK))
        self.assertEqual((sim_world.MEDEVAC_TICK, sim_world.MEDEVAC_UNTIL), (1350, 2100))
        self.assertEqual(sim_world.CLOCK.zulu_of(sim_world.MEDEVAC_TICK), "0918")
        self.assertGreater(sim_world.MEDEVAC_TICK, sim_world.ZONE_UNTIL)
        simulation = sim_world.Simulation()
        simulation.tick_count = sim_world.ZONE_UNTIL + 1
        self.assertEqual([b["kind"] for b in simulation.bulletins()], [])
        simulation.tick_count = sim_world.MEDEVAC_TICK
        notams = [b for b in simulation.bulletins() if b["kind"] == "notam"]
        self.assertEqual([b["id"] for b in notams], ["nofly-2026-09-medevac"])
        self.assertEqual(notams[0]["text"], sim_world.MEDEVAC_TEXT)
        self.assertNotIn("polygon", notams[0])
        self.assertEqual((notams[0]["published_tick"], notams[0]["until_tick"]), (1350, 2100))
        simulation.tick_count = sim_world.MEDEVAC_UNTIL + 1
        self.assertNotIn("nofly-2026-09-medevac", [b["id"] for b in simulation.bulletins()])

    def test_without_a_model_it_is_ledgered_unreadable_and_stays_out_of_the_notices(self):
        """배너는 /state.notices 에 없는 공지를 원문 그대로 '런타임이 아직 못 읽음' 으로 씁니다."""
        runtime, adapter = make_runtime()
        runtime.tick = 1400
        runtime.absorb([medevac_item()])
        runtime.absorb([medevac_item()])
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [])
        unread = [e for e in ledger_lines(runtime)
                  if e["decision"].get("code") == "notice_unreadable"]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["decision"]["detail"]["notice"], "nofly-2026-09-medevac")
        self.assertIn("모델이 없음", unread[0]["decision"]["reason"])
        self.assertEqual(adapter.sent, [])


class HeldNoticeTest(unittest.TestCase):
    """문장 → 모델 양식 → 보류 → 사람 확인 → 적용. 확인 전에는 판정에 들어가지 않습니다."""

    CENTRE = (40.81444, -73.93972)        # 404852N0735623W, 할렘 병원

    def setUp(self):
        self.llm = fixture_super("reference")
        self.runtime, self.adapter = make_runtime(self.llm)
        self.runtime.tick = 1400
        # 병원 상공을 지나는 승인 경로를 날고 있는 기체 하나(회수 대상)와 땅에 선 기체 하나.
        self.runtime.telemetry = {
            "drone-02": {"lat": 40.8050, "lon": -73.93972, "alt_m": 60.0,
                         "route": [{"lat": 40.8250, "lon": -73.93972, "alt_m": 60.0}]},
            "drone-01": {"lat": 40.8050, "lon": -73.9450, "alt_m": 0.0},
        }

    def through(self) -> list[dict]:
        return [{"lat": 40.8050, "lon": -73.9450, "alt_m": 60},
                {"lat": 40.8144, "lon": -73.9397, "alt_m": 60},
                {"lat": 40.8250, "lon": -73.9350, "alt_m": 60}]

    def file_through(self) -> Verdict:
        decision = self.runtime.file(Proposal(
            asset_id="drone-01", action="fly_route", cost_usd=12.0, blast_radius="schedule",
            rationale="", params={"legs": self.through()}).to_dict())
        return decision

    def test_it_is_held_shown_kept_out_of_judging_and_applied_only_after_a_person_confirms(self):
        self.runtime.absorb([medevac_item()])
        self.assertEqual(self.llm.served, ["notices_super.json"])
        shown = self.runtime.snapshot()["notices"]
        self.assertEqual([(n["id"], n["held"], n["applied"], n["source"]) for n in shown],
                         [("nofly-2026-09-medevac", True, False,
                           "model:nemotron-3-super-reference")])
        self.assertEqual((shown[0]["from_tick"], shown[0]["until_tick"]), (1350, 2100))
        self.assertEqual(len(shown[0]["polygon"]), 16)
        pending = self.runtime.snapshot()["awaiting_human"]
        self.assertEqual([p["action"] for p in pending], ["publish_notice"])
        # 보류 중에는 판정에 없습니다. 병원 상공의 새 경로가 승인되고, 날던 기체도 회수되지 않음.
        self.assertIsNone(self.runtime.airspace.breach(*self.CENTRE, 60.0))
        self.assertIs(self.file_through().verdict, Verdict.AUTO)
        self.assertEqual([s[1] for s in self.adapter.sent], ["fly_route"])
        self.assertNotIn("nofly-2026-09-medevac",
                         [p["id"] for p in self.runtime.snapshot()["policies"]])
        # 사람이 확인하면 그때부터: 공역에 들어가고, 지나던 경로는 회수되고, 새 경로는 거절됩니다.
        decision = self.runtime.approve(pending[0]["id"], "관제사", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.AUTO, "notice_published"))
        volume = self.runtime.airspace.breach(*self.CENTRE, 60.0)
        self.assertEqual((volume.id, volume.source), ("nofly-2026-09-medevac", "human"))
        recalled = [s for s in self.adapter.sent if s[1] == "divert_ground"]
        self.assertEqual([s[0] for s in recalled], ["drone-02"])
        self.assertEqual(recalled[0][2]["volume"], "nofly-2026-09-medevac")
        self.runtime.tick = 1420       # 중복 방지 창(15틱) 밖에서 다시 냅니다
        decision = self.file_through()
        self.assertEqual((decision.verdict, decision.forbids),
                         (Verdict.DENIED, "nofly-2026-09-medevac"))
        shown = self.runtime.snapshot()["notices"][0]
        self.assertEqual((shown["held"], shown["applied"], shown["confirmed_by"]),
                         (False, True, "관제사"))
        self.assertIn("nofly-2026-09-medevac",
                      [p["id"] for p in self.runtime.snapshot()["policies"]])
        published = [e for e in ledger_lines(self.runtime)
                     if e["decision"].get("code") == "notice_published"]
        self.assertEqual(published[0]["context"]["checks_run"],
                         ["notice:grammar", "notice:model", "notice:human"])
        self.assertEqual(published[0]["outcome"], "done")
        # 보류할 때 연 항목이 사람의 답으로 닫힙니다 — 같은 원장 번호, 열린 채 남는 줄 없음
        self.assertEqual(published[0]["id"], self._card_id(pending[0]["id"]))
        self.assertEqual(self._open_ids(), set())
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])

    def _lines(self):
        with open(self.runtime.ledger.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def _card_id(self, proposal_id):
        return next(e["id"] for e in self._lines() if e["proposal"]["id"] == proposal_id)

    def _open_ids(self):
        """열렸는데 닫히지 않은 원장 번호."""
        opened = {e["id"] for e in self._lines() if e["outcome"] == "pending"}
        closed = {e["id"] for e in self._lines() if e["outcome"] != "pending"}
        return opened - closed

    def test_a_refusal_closes_the_held_entry_instead_of_opening_another(self):
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime.approve(pending["id"], "관제사", allow=False)
        rows = [(e["id"], e["outcome"], e["decision"]["code"]) for e in self._lines()
                if e["proposal"]["id"] == pending["id"]]
        self.assertEqual(rows, [(rows[0][0], "pending", "human_notice"),
                                (rows[0][0], "denied", "notice_refused")])
        self.assertEqual(self._open_ids(), set())

    def test_a_round_change_closes_a_held_card_as_lapsed(self):
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime._follow_round(2)
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        rows = [(e["outcome"], e["decision"]["code"]) for e in self._lines()
                if e["proposal"]["id"] == pending["id"]]
        self.assertEqual(rows, [("pending", "human_notice"), ("lapsed", "notice_lapsed")])
        self.assertEqual(self._open_ids(), set())

    def test_confirming_after_the_window_closed_applies_nothing_and_says_so(self):
        """폴링 한 번 사이의 경주. 창이 닫힌 뒤의 승인은 '걸렸다' 가 아니라 '지나갔다' 입니다."""
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime.tick = 2101
        decision = self.runtime.approve(pending["id"], "관제사", allow=True)
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "notice_lapsed"))
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        self.assertEqual(self.runtime.airspace.all(), [])
        self.assertNotIn("nofly-2026-09-medevac",
                         [p["id"] for p in self.runtime.snapshot()["policies"]])
        self.assertEqual([e["outcome"] for e in self._lines()
                          if e["proposal"]["id"] == pending["id"]], ["pending", "lapsed"])
        self.runtime.absorb([medevac_item()])
        self.assertEqual(len(self.llm.asked), 1, "닫힌 창의 공지를 다시 묻지 않습니다")

    def test_confirmed_before_the_window_opens_it_is_shown_as_confirmed_then_applies(self):
        self.runtime.tick = 1300
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"][0]
        self.runtime.approve(pending["id"], "관제사", allow=True)
        shown = self.runtime.snapshot()["notices"][0]
        self.assertEqual((shown["held"], shown["applied"], shown["source"]),
                         (False, False, "human"))
        self.runtime.tick = 1350
        self.runtime.absorb([medevac_item()])
        self.assertTrue(self.runtime.snapshot()["notices"][0]["applied"])
        # 확인됐지만 걸리기 전에 목록에서 빠지면 기록도 갑니다
        other, _ = make_runtime(fixture_super("reference"))
        other.tick = 1300
        other.absorb([medevac_item()])
        other.approve(other.snapshot()["awaiting_human"][0]["id"], "관제사", allow=True)
        other.absorb([])
        self.assertEqual(other.snapshot()["notices"], [])

    def test_the_model_reads_off_the_world_thread_and_the_next_poll_collects_it(self):
        """실서비스 배선. 읽기 스레드가 도는 동안 absorb 는 바로 돌아오고, 답은 다음 폴링이 적음."""
        import threading

        self.runtime.notice_async = True
        self.runtime.absorb([medevac_item()])
        self.assertIn("nofly-2026-09-medevac", self.runtime._reading)
        for thread in threading.enumerate():
            if thread.name.startswith("notice-"):
                self.assertTrue(thread.daemon)
                thread.join(5.0)
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [], "아직 적히기 전")
        self.runtime.absorb([medevac_item()])
        self.assertEqual(self.runtime._reading, set())
        shown = self.runtime.snapshot()["notices"]
        self.assertEqual([(n["id"], n["held"]) for n in shown], [("nofly-2026-09-medevac", True)])
        self.assertEqual([p["action"] for p in self.runtime.snapshot()["awaiting_human"]],
                         ["publish_notice"])
        self.assertEqual(len(self.llm.asked), 1)

    def test_an_answer_that_arrives_after_the_round_changed_is_dropped(self):
        import threading

        self.runtime.notice_async = True
        self.runtime._round = 1
        self.runtime.absorb([medevac_item()])
        for thread in threading.enumerate():
            if thread.name.startswith("notice-"):
                thread.join(5.0)
        self.runtime._follow_round(2)
        self.runtime.absorb([medevac_item()])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [], "지난 판의 답은 버립니다")
        # 같은 폴링이 새 판의 이름으로 다시 묻고, 다음 폴링이 그 답을 적습니다
        self.assertEqual(self.runtime._reading, {"nofly-2026-09-medevac"})
        for thread in threading.enumerate():
            if thread.name.startswith("notice-"):
                thread.join(5.0)
        self.runtime.absorb([medevac_item()])
        self.assertEqual(len(self.llm.asked), 2)

    def test_a_held_notice_lapses_with_its_window_and_leaves_no_card(self):
        """사람이 안 봤는데 창이 닫혔습니다. 카드와 배너는 내려가고, 걸린 적이 없으니 뺄 것도
        없음."""
        self.runtime.absorb([medevac_item()])
        pending = self.runtime.snapshot()["awaiting_human"]
        self.assertEqual(len(pending), 1)
        self.runtime.tick = 2101
        self.runtime.absorb([medevac_item()])          # 목록에 아직 있어도 창은 닫혔습니다
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        self.assertEqual(self.runtime.airspace.all(), [])
        lapsed = [e for e in ledger_lines(self.runtime) if e["outcome"] == "lapsed"]
        self.assertEqual([(e["proposal"]["id"], e["decision"]["code"], e["decision"]["verdict"])
                          for e in lapsed], [(pending[0]["id"], "notice_lapsed", "denied")])
        self.runtime.absorb([medevac_item()])
        self.assertEqual(len(self.llm.asked), 1, "닫힌 창의 공지를 다시 묻지 않습니다")
        self.assertIsNone(self.runtime.approve(pending[0]["id"], "관제사", allow=True),
                          "내려간 카드는 승인할 수 없습니다")

    def test_a_held_notice_dropped_from_the_feed_leaves_no_card_either(self):
        self.runtime.absorb([medevac_item()])
        self.runtime.tick = 1500
        self.runtime.absorb([])
        self.assertEqual(self.runtime.snapshot()["notices"], [])
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        self.assertEqual([e["decision"]["reason"] for e in ledger_lines(self.runtime)
                          if e["outcome"] == "lapsed"],
                         ["사람이 확인하기 전에 공지가 내려감 — 걸린 적 없음"])

    def test_the_local_stand_in_answer_is_held_and_a_person_can_refuse_it(self):
        """Ollama 의 30B 스탠드인이 실제로 준 답. 양식과 검사는 통과하지만 다각형이 공지가 아닙니다.

        병원에서 3km 남쪽의 80m 짜리 조각. 사람이 봐야 하는 이유가 이것입니다."""
        llm = fixture_super("ollama-recorded")
        runtime, adapter = make_runtime(llm)
        runtime.tick = 1400
        runtime.absorb([medevac_item()])
        held = runtime.notices.pending()
        self.assertEqual([h["id"] for h in held], ["nofly-2026-09-medevac"])
        centre_lat = sum(p[0] for p in held[0]["polygon"]) / len(held[0]["polygon"])
        self.assertGreater(abs(centre_lat - self.CENTRE[0]) * 110_570, 1000)
        pending = runtime.snapshot()["awaiting_human"][0]
        self.assertEqual(pending["author"], "model:nemotron-3-nano:latest")
        decision = runtime.approve(pending["id"], "관제사", allow=False)
        self.assertEqual((decision.verdict, decision.code), (Verdict.DENIED, "notice_refused"))
        self.assertEqual(runtime.snapshot()["notices"], [])
        self.assertEqual(runtime.airspace.all(), [])
        self.assertEqual(adapter.sent, [])
        self.assertNotIn("nofly-2026-09-medevac", [p["id"] for p in runtime.snapshot()["policies"]])
        runtime.absorb([medevac_item()])
        self.assertEqual(runtime.snapshot()["awaiting_human"], [],
                         "거부한 공지를 다시 올리지 않습니다")
        self.assertEqual(len(llm.asked), 1, "같은 공지를 다시 묻지 않습니다")


if __name__ == "__main__":
    unittest.main()
