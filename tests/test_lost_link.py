"""Lost link: the runtime keeps a dark aircraft's space and commands nothing to it.

(a) The operator declares what the autopilot does when the link drops (continue_and_land) and how
long telemetry may go stale (timeout_ticks). (b) An airborne aircraft whose telemetry stamp stops
for that long is LOST LINK: its intent — the cleared route and the landing column — stays
reserved until planned arrival plus a margin, every other filing through it is refused, a person
gets a card, and nothing is sent to the aircraft, which cannot hear. (c) When the stamp moves
again the runtime checks the aircraft is inside what it cleared and takes the card down. (d) A
filing whose declared behaviour the runtime cannot judge is refused.
"""

import json
import tempfile
import unittest

from holdshort.core import config as config_module
from holdshort.core.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, Volume, box
from holdshort.core.models import Proposal, Verdict
from holdshort.runtime.intents import (
    ACTIVATED,
    ENDED,
    LOST_LINK_MARGIN_TICKS,
    TIME_PAD_TICKS,
    Intent,
    LinkWatch,
    first_conflict,
    schedule,
)
from holdshort.runtime.service import Runtime
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
LAT0, LON0 = 40.7000, -73.9700


def north(metres: float) -> float:
    return metres / METRES_PER_DEG_LAT


def east(metres: float) -> float:
    return metres / METRES_PER_DEG_LON


def leg(north_m: float, east_m: float, alt_m: float) -> dict:
    return {"lat": round(LAT0 + north(north_m), 7), "lon": round(LON0 + east(east_m), 7),
            "alt_m": alt_m}


def route(asset: str, legs: list[dict], **params) -> dict:
    return Proposal(asset_id=asset, action="fly_route", cost_usd=12.0, blast_radius="schedule",
                    rationale="시험", params={"legs": legs, **params}).to_dict()


def ground(north_m: float, east_m: float, tick: int | None = None) -> dict:
    state = {"lat": LAT0 + north(north_m), "lon": LON0 + east(east_m), "alt_m": 0.0,
             "state": "ready"}
    return state if tick is None else {**state, "telemetry_tick": tick}


def aloft(north_m: float, east_m: float, tick: int, alt_m: float = 60.0) -> dict:
    return {"lat": LAT0 + north(north_m), "lon": LON0 + east(east_m), "alt_m": alt_m,
            "state": "delivering", "telemetry_tick": tick}


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params, ledger_id))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.notice_async = False
    runtime.intake_async = False
    runtime.advisory_async = False
    return runtime, adapter


def closed_lines(runtime) -> list[dict]:
    return [e for e in runtime.ledger.read_all() if e["outcome"] != "pending"]


def cards(runtime, action: str) -> list[dict]:
    return [p for p in runtime.snapshot()["awaiting_human"] if p["action"] == action]


class DeclaredBehaviourTest(unittest.TestCase):
    def test_the_fleet_declares_continue_and_land_with_the_timeout_the_simulator_scores_by(self):
        lost_link = config_module.load(CONFIG).performance.lost_link
        self.assertEqual((lost_link.behaviour, lost_link.timeout_ticks),
                         ("continue_and_land", sim_world.LINK_TIMEOUT_TICKS))
        self.assertTrue(lost_link.known)

    def test_an_approved_route_carries_its_contingency_and_the_check_is_on_the_record(self):
        runtime, _ = make_runtime()
        runtime.telemetry = {"drone-01": ground(0, 0)}
        self.assertTrue(runtime.file(route("drone-01", [leg(0, 0, 60), leg(1500, 0, 60)]))
                        .committed)
        self.assertEqual(runtime.intents.get("drone-01").contingency, "continue_and_land")
        done = [e for e in closed_lines(runtime) if e["outcome"] == "done"][-1]
        self.assertIn("contingency", done["context"]["checks_run"])

    def test_a_behaviour_the_runtime_cannot_judge_refuses_every_route(self):
        runtime, adapter = make_runtime()
        runtime.performance.lost_link = config_module.LostLink("return_to_launch", 15)
        runtime.telemetry = {"drone-01": ground(0, 0)}
        decision = runtime.file(route("drone-01", [leg(0, 0, 60), leg(1500, 0, 60)]))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual((decision.code, decision.policy_hit), ("contingency_unknown", "lost_link"))
        self.assertEqual(decision.detail["behaviour"], "return_to_launch")
        self.assertEqual(adapter.sent, [])


class LinkWatchTest(unittest.TestCase):
    def test_a_stamp_that_stops_for_the_timeout_is_lost_once_and_moving_again_restores_it(self):
        watch = LinkWatch(15)
        self.assertEqual(watch.observe({"d": aloft(0, 0, 100)}, 100), [])
        self.assertEqual(watch.observe({"d": aloft(0, 0, 100)}, 114), [], "14틱은 아직입니다")
        events = watch.observe({"d": aloft(0, 0, 100)}, 115)
        self.assertEqual([(e.kind, e.since_tick, e.last_seen_tick, e.tick) for e in events],
                         [("lost", 101, 100, 115)])
        self.assertEqual(watch.observe({"d": aloft(0, 0, 100)}, 140), [], "두절은 한 번")
        self.assertEqual(watch.snapshot()["d"], {"status": "lost", "since_tick": 101,
                                                 "last_seen_tick": 100, "declared_tick": 115})
        events = watch.observe({"d": aloft(0, 0, 150)}, 150)
        self.assertEqual([(e.kind, e.since_tick, e.last_seen_tick, e.tick) for e in events],
                         [("restored", 101, 100, 150)])
        self.assertEqual(watch.snapshot()["d"], {"status": "ok", "since_tick": 150,
                                                 "last_seen_tick": 150, "declared_tick": None})

    def test_the_ground_and_a_record_without_a_stamp_are_never_lost(self):
        watch = LinkWatch(15)
        frozen = {"g": ground(0, 0, 100), "n": {**aloft(0, 0, 0), "telemetry_tick": None}}
        for tick in (100, 200, 500):
            self.assertEqual(watch.observe(frozen, tick), [])
        self.assertFalse(watch.lost("g") or watch.lost("n"))


class IntentReserveTest(unittest.TestCase):
    """두절 예약은 남은 경로만 늘립니다. 지나온 부피를 다시 막으면 이륙 기둥이 막힙니다."""

    def setUp(self):
        performance = config_module.load(CONFIG).performance
        self.legs = [leg(0, 0, 60), leg(2000, 0, 60), leg(2000, 2000, 60)]
        volumes, arrive = schedule(self.legs, 100, 0.0, performance)
        self.intent = Intent("d", "p", volumes, (LAT0, LON0), (0.0, 0.0), 100, arrive, 100)
        self.before = [(v.t_enter, v.t_exit) for v in volumes]
        self.kinds = ["column" if v.is_column else "cruise" for v in volumes]

    def test_the_remaining_route_is_stretched_and_what_is_behind_is_left_alone(self):
        self.assertEqual(self.kinds, ["column", "cruise", "cruise", "column"])
        second = self.intent.volumes[2]
        seen = second.t_enter + 60
        at = (LAT0 + north(2000), LON0 + east(1000), 60.0)
        until = self.intent.reserve_dark(seen, at)
        self.assertEqual([(v.t_enter, v.t_exit) for v in self.intent.volumes[:2]],
                         self.before[:2], "이륙 기둥과 지나온 첫 구간은 그대로")
        arrive = self.intent.arrive_tick
        for volume in self.intent.volumes[2:]:
            self.assertLessEqual(volume.t_enter, seen)
            self.assertGreaterEqual(volume.t_exit, arrive + LOST_LINK_MARGIN_TICKS)
        self.assertEqual(until, arrive + LOST_LINK_MARGIN_TICKS + TIME_PAD_TICKS)
        self.assertEqual(self.intent.dark_since, seen + 1)
        self.intent.release_dark()
        self.assertEqual([(v.t_enter, v.t_exit) for v in self.intent.volumes], self.before)
        self.assertIsNone(self.intent.dark_since)

    def test_an_aircraft_behind_its_schedule_keeps_the_leg_it_is_actually_on(self):
        seen = self.intent.volumes[2].t_enter + 60        # 일정상으로는 둘째 구간
        at = (LAT0 + north(1500), LON0, 60.0)             # 실제로는 첫 구간 위
        self.intent.reserve_dark(seen, at)
        self.assertEqual((self.intent.volumes[0].t_enter, self.intent.volumes[0].t_exit),
                         self.before[0])
        self.assertLessEqual(self.intent.volumes[1].t_enter, seen)
        self.assertGreater(self.intent.volumes[1].t_exit, self.before[1][1])

    def test_covers_is_space_only(self):
        self.assertTrue(self.intent.covers(LAT0 + north(1000), LON0 + east(20), 70.0))
        self.assertFalse(self.intent.covers(LAT0 + north(1000), LON0 + east(200), 60.0))
        self.assertTrue(self.intent.covers(LAT0 + north(2000), LON0 + east(2000), 0.0),
                        "목적지에 내려앉은 것은 착륙 기둥 안입니다")


class LostLinkRuntimeTest(unittest.TestCase):
    """drone-01 이 북쪽 3000m 를 60m 로 납니다. drone-02 는 동쪽에 서서 그 길을 가로지르려
    합니다."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.tick = 100
        self.runtime.telemetry = {"drone-01": ground(0, 0, 100), "drone-02": ground(2000, 400, 100)}
        self.assertTrue(self.runtime.file(route("drone-01", [leg(0, 0, 60), leg(3000, 0, 60)]))
                        .committed)
        self.intent = self.runtime.intents.get("drone-01")
        self.windows = [(v.t_enter, v.t_exit) for v in self.intent.volumes]
        self.cruise = self.intent.volumes[1]
        self.assertFalse(self.cruise.is_column)
        # 명목 창(여유 포함)이 닫히는 틱. 두절 뒤에는 cruise.to_tick 이 늘어나므로 미리 적어 둡니다.
        self.nominal_to = self.cruise.to_tick
        # 떴습니다. 순항 구간 1000m 지점에서 마지막으로 보입니다.
        self.seen = self.cruise.t_enter + 57
        self._at(self.seen, drone_01=aloft(1000, 0, self.seen))
        self.assertEqual(self.intent.state, ACTIVATED)
        self.sent = len(self.adapter.sent)

    def _at(self, tick: int, drone_01: dict | None = None) -> list:
        """틱을 옮기고 텔레메트리를 받습니다. drone-02 의 도장은 늘 새것, drone-01 은 준 것만."""
        self.runtime.tick = tick
        if drone_01 is not None:
            self.runtime.telemetry["drone-01"] = drone_01
        self.runtime.telemetry["drone-02"] = ground(2000, 400, tick)
        self.runtime._observe()
        return self.runtime.watch_links()

    def _go_dark(self) -> None:
        self.assertEqual(self._at(self.seen + 14), [])
        events = self._at(self.seen + 15)
        self.assertEqual([e.kind for e in events], ["lost"])

    def _crossing(self, cross_tick: int) -> dict:
        """drone-02 가 2000m 북쪽에서 동 → 서로 drone-01 의 길을 cross_tick 무렵에 건너는 신청.

        오르는 데 38틱(60m / 1.6m), 400m 를 가는 데 23틱 — 출발을 그만큼 앞에 둡니다."""
        return route("drone-02", [leg(2000, 400, 60), leg(2000, -400, 60)],
                     depart_after_tick=cross_tick - 61)

    def test_the_link_is_declared_lost_ledgered_and_raised_as_a_card_and_nothing_is_sent(self):
        self._go_dark()
        links = self.runtime.snapshot()["links"]
        self.assertEqual((links["drone-01"]["status"], links["drone-01"]["since_tick"],
                          links["drone-01"]["last_seen_tick"]), ("lost", self.seen + 1, self.seen))
        self.assertEqual(links["drone-02"]["status"], "ok")
        lost = [e for e in closed_lines(self.runtime) if e["proposal"]["action"] == "link_lost"]
        self.assertEqual(len(lost), 1)
        detail = lost[0]["decision"]["detail"]
        self.assertEqual((lost[0]["decision"]["code"], detail["intent"], detail["behaviour"]),
                         ("link_lost", self.intent.id, "continue_and_land"))
        self.assertEqual(detail["reserved_until_tick"],
                         self.intent.arrive_tick + LOST_LINK_MARGIN_TICKS + TIME_PAD_TICKS)
        self.assertEqual(lost[0]["context"]["intent_id"], self.intent.id)
        card = cards(self.runtime, "lost_link_notice")
        self.assertEqual(len(card), 1)
        self.assertEqual((card[0]["asset_id"], card[0]["blast_radius"]), ("drone-01", "schedule"))
        self.assertTrue(card[0]["rationale"].startswith(f"no telemetry since tick {self.seen + 1}"))
        self.assertEqual(self.runtime._decisions[card[0]["id"]].code, "human_lost_link")
        self.assertEqual(len(self.adapter.sent), self.sent, "끊긴 기체는 들을 수 없습니다")

    def _crossing_volumes(self, cross_tick: int):
        crossing = self._crossing(cross_tick)
        volumes, _ = schedule(crossing["params"]["legs"], crossing["params"]["depart_after_tick"],
                              0.0, self.runtime.performance)
        return volumes

    def test_a_crossing_the_nominal_window_allowed_is_refused_while_the_aircraft_is_dark(self):
        cross = self.nominal_to + 10              # 명목 창(여유 포함)이 닫힌 뒤
        self.assertIsNone(first_conflict(self._crossing_volumes(cross), [self.intent]),
                          "링크가 살아 있으면 명목 창 밖의 교차는 겹치지 않습니다")
        self._go_dark()
        self.assertIsNotNone(first_conflict(self._crossing_volumes(cross), [self.intent]))
        decision = self.runtime.file(self._crossing(cross))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual((decision.policy_hit, decision.forbids), ("traffic", "drone-01"))
        self.assertEqual(decision.detail["blocked_intent"], self.intent.id)
        self.assertEqual(decision.detail["blocked_until_tick"],
                         self.intent.arrive_tick + LOST_LINK_MARGIN_TICKS + TIME_PAD_TICKS)

    def test_after_planned_arrival_and_margin_the_route_frees_but_the_landing_site_does_not(self):
        self._go_dark()
        reserved_until = self.intent.arrive_tick + LOST_LINK_MARGIN_TICKS + TIME_PAD_TICKS
        later = reserved_until + 30
        self._at(later)
        self.assertTrue(self.runtime.file(self._crossing(later + 90)).committed,
                        "신고한 대로 내렸을 시각이 지나면 길은 풀립니다")
        self.runtime.telemetry["drone-03"] = ground(2600, 300, later)
        onto = self.runtime.file(route("drone-03", [leg(2600, 300, 60), leg(3000, 20, 60)]))
        self.assertIs(onto.verdict, Verdict.DENIED)
        self.assertEqual((onto.detail["blocked_kind"], onto.forbids), ("landing", "drone-01"),
                         "착륙장은 링크가 돌아올 때까지 그 기체의 것입니다")

    def test_the_dark_aircraft_cannot_be_given_a_route_and_nothing_is_banned(self):
        self._go_dark()
        decision = self.runtime.file(route("drone-01", [leg(1000, 0, 60), leg(1000, 800, 60)]))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual((decision.code, decision.policy_hit), ("lost_link_refused", None))
        self.assertEqual(decision.detail["since_tick"], self.seen + 1)
        self.assertEqual(closed_lines(self.runtime)[-1]["context"]["checks_run"],
                         ["dedupe", "link"])

    def test_a_zone_closing_over_the_dark_route_sends_it_nothing(self):
        self._go_dark()
        self.runtime.telemetry["drone-01"] = {**self.runtime.telemetry["drone-01"],
                                              "route": [leg(3000, 0, 60)]}
        closing = Volume("nofly-t", "닫힘", box(LAT0 + north(1800), LON0 - east(100),
                                                LAT0 + north(2200), LON0 + east(100)))
        self.assertEqual(self.runtime.recall_flights(closing), [])
        self.assertEqual(len(self.adapter.sent), self.sent)

    def test_telemetry_back_inside_what_was_cleared_restores_conforms_and_closes_the_card(self):
        self._go_dark()
        back = self.seen + 120
        events = self._at(back, drone_01=aloft(2600, 5, back))
        self.assertEqual([e.kind for e in events], ["restored"])
        restored = [e for e in closed_lines(self.runtime)
                    if e["proposal"]["action"] == "link_restored"]
        detail = restored[0]["decision"]["detail"]
        self.assertEqual((detail["conforming"], detail["restored_tick"], detail["dark_ticks"]),
                         (True, back, back - self.seen - 1))
        self.assertFalse([e for e in closed_lines(self.runtime)
                          if e["decision"]["code"] == "nonconforming"])
        self.assertEqual(cards(self.runtime, "lost_link_notice"), [])
        closed_card = [e for e in closed_lines(self.runtime)
                       if e["proposal"]["action"] == "lost_link_notice"]
        self.assertEqual((closed_card[0]["outcome"], closed_card[0]["decision"]["code"]),
                         ("lapsed", "link_restored"))
        self.assertEqual(self.runtime.snapshot()["links"]["drone-01"]["status"], "ok")
        self.assertEqual([(v.t_enter, v.t_exit) for v in self.intent.volumes], self.windows,
                         "다시 보이니 승인 때의 창으로")

    def test_telemetry_back_outside_what_was_cleared_is_nonconforming(self):
        self._go_dark()
        back = self.seen + 120
        self._at(back, drone_01=aloft(2000, 500, back))
        nonconforming = [e for e in closed_lines(self.runtime)
                         if e["decision"]["code"] == "nonconforming"]
        self.assertEqual(len(nonconforming), 1)
        self.assertEqual((nonconforming[0]["proposal"]["params"]["kind"],
                          nonconforming[0]["proposal"]["params"]["intent"]),
                         ("lost_link", self.intent.id))

    def test_landed_at_the_cleared_destination_conforms_and_ends_the_intent(self):
        self._go_dark()
        back = self.intent.arrive_tick + 40
        self._at(back, drone_01=ground(3000, 0, back))
        restored = [e for e in closed_lines(self.runtime)
                    if e["proposal"]["action"] == "link_restored"]
        self.assertTrue(restored[0]["decision"]["detail"]["conforming"])
        self.assertEqual((self.intent.state, self.intent.ended_reason), (ENDED, "arrived"))

    def test_a_person_can_release_the_space_early(self):
        self._go_dark()
        card = cards(self.runtime, "lost_link_notice")[0]
        decision = self.runtime.approve(card["id"], "관제사", allow=True)
        self.assertEqual((decision.code, decision.approved_by), ("lost_link_released", "관제사"))
        self.assertEqual((self.intent.state, self.intent.ended_reason), (ENDED, "released"))
        self.assertTrue(self.runtime.file(self._crossing(self.nominal_to + 10)).committed)
        self.assertEqual(self.runtime.snapshot()["links"]["drone-01"]["status"], "lost",
                         "사람이 풀어도 링크는 끊긴 채입니다")
        self.assertEqual([s for s in self.adapter.sent[self.sent:] if s[0] == "drone-01"], [],
                         "푸는 것도 기체에 보내는 명령이 아닙니다")

    def test_or_keep_it_until_telemetry_returns(self):
        self._go_dark()
        card = cards(self.runtime, "lost_link_notice")[0]
        self.assertEqual(self.runtime.approve(card["id"], "관제사", allow=False).code,
                         "lost_link_kept")
        self.assertIs(self.runtime.file(self._crossing(self.nominal_to + 10)).verdict,
                      Verdict.DENIED)

    def test_a_round_change_forgets_the_links_and_takes_the_card_down(self):
        self._go_dark()
        self.runtime.tick = 5
        self.runtime._follow_round(1)
        self.assertEqual(self.runtime.snapshot()["links"], {})
        self.assertEqual(cards(self.runtime, "lost_link_notice"), [])
        lapsed = [e for e in closed_lines(self.runtime)
                  if e["proposal"]["action"] == "lost_link_notice"]
        self.assertEqual(lapsed[0]["decision"]["code"], "card_lapsed")


class SimSceneTest(unittest.TestCase):
    """시뮬레이터: 창이 열리면 떠서 경로를 날던 첫 기체가 끊기고, 그대로 날며, 명령을
    못 듣습니다."""

    def setUp(self):
        self.world = sim_world.World("guarded", 7, None)
        self.dark = self.world.vehicles["drone-02"]
        far = sim_world.to_grid(40.7400, -73.9700)
        self.dark.alt, self.dark.cruise_alt, self.dark.state = 90.0, 90.0, "delivering"
        self.dark.waypoints = [(far[0], far[1], 90.0)]

    def test_the_aircraft_goes_dark_keeps_flying_and_comes_back_at_the_end_of_the_window(self):
        start = sim_world.LINK_LOSS_TICK
        self.world.tick(start - 1)
        self.assertEqual(self.world.dark, {})
        self.world.tick(start)
        self.assertEqual(set(self.world.dark), {"drone-02"})
        frozen = self.world.snapshot(start)["assets"]["drone-02"]
        self.assertEqual(frozen["telemetry_tick"], start - 1)
        self.assertFalse(frozen["link_lost"],
                         "텔레메트리는 두절을 말하지 않습니다 — 도장이 멈출 뿐")
        for tick in range(start + 1, start + 20):
            self.world.tick(tick)
        later = self.world.snapshot(start + 19, truth=True)
        self.assertEqual((later["assets"]["drone-02"]["lat"], later["assets"]["drone-02"]["lon"]),
                         (frozen["lat"], frozen["lon"]))
        self.assertNotEqual(later["dark"]["drone-02"]["lat"], frozen["lat"], "기체는 계속 납니다")
        self.assertEqual(later["assets"]["drone-01"]["telemetry_tick"], start + 19)
        actions = self.world.score.actions
        refused = self.world.act("drone-02", "divert_ground", {}, "l_x", "cargo", None, start + 19)
        self.assertFalse(refused["ok"])
        self.assertIn("link lost", refused["error"])
        self.assertEqual(self.world.score.actions, actions)
        self.world.tick(sim_world.LINK_LOSS_UNTIL)
        back = self.world.snapshot(sim_world.LINK_LOSS_UNTIL)["assets"]["drone-02"]
        self.assertEqual(back["telemetry_tick"], sim_world.LINK_LOSS_UNTIL)
        self.assertFalse(self.dark.link_lost)

    def test_only_routes_cleared_after_the_loss_was_knowable_count_as_incursions(self):
        start = sim_world.LINK_LOSS_TICK
        self.world.tick(start)
        other = self.world.vehicles["drone-03"]
        ahead = sim_world.to_grid(40.7300, -73.9700)
        path = self.dark.waypoints[0]
        # drone-02 의 남은 길 위, 같은 고도. 그 길 위의 한 점을 고릅니다.
        other.x = self.dark.x + (path[0] - self.dark.x) * 0.5
        other.y = self.dark.y + (path[1] - self.dark.y) * 0.5
        other.alt = 90.0
        other.route_tick = start + sim_world.LINK_TIMEOUT_TICKS - 1
        self.world._detect_link_lost_incursions(start + 30)
        self.assertEqual(self.world.score.link_lost_incursions, 0,
                         "두절을 알 수 없던 때 받은 경로는 세지 않습니다")
        other.route_tick = start + sim_world.LINK_TIMEOUT_TICKS
        self.world._detect_link_lost_incursions(start + 31)
        self.world._detect_link_lost_incursions(start + 32)
        self.assertEqual(self.world.score.link_lost_incursions, 1, "쌍마다 들어갈 때 한 번")
        other.x, other.y = ahead[0] + 20, ahead[1]
        self.world._detect_link_lost_incursions(start + 33)
        other.x = self.dark.x + (path[0] - self.dark.x) * 0.5
        self.world._detect_link_lost_incursions(start + 34)
        self.assertEqual(self.world.score.link_lost_incursions, 2)


if __name__ == "__main__":
    unittest.main()
