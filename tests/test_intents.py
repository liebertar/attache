"""Separation: vertical columns, 4D intents, strategic conflicts, the operator's ladder.

Every mechanism here is exercised on its own, on a synthetic sky, so a test says which rule
broke rather than which scenario drifted. The seeded two-world round (test_two_worlds) is
where the numbers are compared; this file is where each rule is pinned down.
"""

import json
import math
import tempfile
import unittest

from attache.core.config import load as config_load
from attache.core.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    TRAFFIC_VERTICAL_M,
    Airspace,
    Volume,
    box,
    first_breach,
    vertical_column,
)
from attache.core.models import Proposal, Verdict
from attache.runtime.intents import (
    ACCEPTED,
    ACTIVATED,
    CONTINGENCY,
    ENDED,
    OPEN_ENDED_TICK,
    TIME_PAD_TICKS,
    Intent,
    IntentRegistry,
    corridor_widths,
    first_conflict,
    schedule,
)
from attache.runtime.service import Runtime
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
# 합성 하늘의 원점. 맨해튼 위도라 미터 환산이 실제와 같습니다.
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


class RecordingAdapter:
    """실행된 것을 셉니다. 런타임의 보장은 여기 닿는 것으로 재야 합니다."""

    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)   # HTTP 어댑터처럼 JSON 이어야 합니다
        self.sent.append((asset_id, action, params, ledger_id))
        return {"ok": True}

    def telemetry(self):
        return {}


def ground(north_m: float, east_m: float, **extra) -> dict:
    return {"lat": LAT0 + north(north_m), "lon": LON0 + east(east_m), "alt_m": 0.0,
            "state": "ready", "work_ticks": 0, **extra}


def make_runtime(volumes=()) -> tuple[Runtime, RecordingAdapter]:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    for volume in volumes:
        runtime.airspace.add(volume)
    runtime.tick = 100
    return runtime, adapter


def ledger_lines(runtime: Runtime) -> list[dict]:
    """닫힌 항목만. 원장은 열 때 한 줄, 닫을 때 한 줄이라 같은 id 가 두 번 나옵니다."""
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [entry for entry in (json.loads(line) for line in handle)
                if entry["outcome"] != "pending"]


# ---------- C1. 수직 구간 ----------


class VerticalColumnTest(unittest.TestCase):
    """출발점 옆 건물은 순항 구간을 안 막아도 이륙 기둥은 막습니다."""

    def setUp(self):
        # 출발점에서 동쪽 5m 에 옥상 60m 건물. 이격 50m 까지 막으니 0~110m 가 그 건물입니다.
        self.building = Volume("bldg-next-door", "옆 건물",
                               box(LAT0 - north(10), LON0 + east(5), LAT0 + north(10),
                                   LON0 + east(40)),
                               ceiling_m=60.0, clearance_m=50.0, source="시험")
        self.runtime, self.adapter = make_runtime([self.building])
        self.runtime.telemetry = {"drone-01": ground(0, 0)}

    def test_the_column_is_zero_length_legs_judged_by_first_breach(self):
        column = vertical_column(LAT0, LON0, 0.0, 115.0)
        self.assertEqual([c["alt_m"] for c in column][:3], [0.0, 5.0, 10.0])
        self.assertEqual(column[-1]["alt_m"], 115.0)
        self.assertTrue(all(c["lat"] == LAT0 and c["lon"] == LON0 for c in column))
        found = first_breach(Airspace([self.building]), column)
        self.assertIsNotNone(found)
        self.assertEqual(found[1].id, "bldg-next-door")
        # 건물 띠 위에서 시작하는 기둥은 통과합니다
        self.assertIsNone(first_breach(Airspace([self.building]),
                                       vertical_column(LAT0, LON0, 111.0, 120.0)))

    def test_the_cruise_leg_passes_but_the_takeoff_column_is_refused(self):
        legs = [leg(0, 0, 115.0), leg(600, 0, 115.0)]
        self.assertIsNone(first_breach(self.runtime.airspace, legs), "순항 구간은 옥상 위입니다")
        decision = self.runtime.file(route("drone-01", legs))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.code, "airspace")
        self.assertEqual(decision.policy_hit, "airspace")
        params = self.runtime._decisions[decision.proposal_id] and \
            ledger_lines(self.runtime)[-1]["proposal"]["params"]
        self.assertEqual(params["blocked_kind"], "takeoff")
        self.assertEqual(params["blocked_volume"], "bldg-next-door")
        self.assertEqual(params["blocked_leg"], 1)
        self.assertAlmostEqual(params["blocked_at"]["lat"], LAT0, places=5)
        self.assertEqual(params["blocked_ceiling_m"], 60.0)
        self.assertIn("이륙 기둥", decision.reason)
        self.assertEqual(self.adapter.sent, [])

    def test_a_vertex_climb_through_a_band_neither_leg_touches_is_refused(self):
        # 꼭짓점 위에만 걸린 50~80m 띠. 앞 구간은 40m(아래), 뒤 구간은 100m(위)라 둘 다 통과인데
        # 꼭짓점에서 40 → 100 으로 오르는 동안 띠를 지납니다.
        band = Volume("nofly-band", "띠", box(LAT0 + north(290), LON0 - east(20),
                                              LAT0 + north(310), LON0 + east(20)),
                      floor_m=50.0, ceiling_m=80.0, source="시험")
        runtime, adapter = make_runtime([band])
        runtime.telemetry = {"drone-01": ground(0, 0)}
        legs = [leg(0, 0, 40.0), leg(300, 0, 40.0), leg(600, 0, 100.0)]
        self.assertIsNone(first_breach(runtime.airspace, legs))
        decision = runtime.file(route("drone-01", legs))
        self.assertIs(decision.verdict, Verdict.DENIED)
        params = ledger_lines(runtime)[-1]["proposal"]["params"]
        self.assertEqual(params["blocked_kind"], "column")
        self.assertEqual(params["blocked_leg"], 2)
        self.assertEqual(params["blocked_volume"], "nofly-band")
        self.assertEqual(adapter.sent, [])

    def test_the_landing_column_is_judged_down_to_the_ground(self):
        runtime, _ = make_runtime([self.building])
        runtime.telemetry = {"drone-01": ground(600, 0)}
        legs = [leg(600, 0, 115.0), leg(0, 0, 115.0)]
        decision = runtime.file(route("drone-01", legs))
        self.assertIs(decision.verdict, Verdict.DENIED)
        params = ledger_lines(runtime)[-1]["proposal"]["params"]
        self.assertEqual(params["blocked_kind"], "landing")
        self.assertEqual(params["blocked_volume"], "bldg-next-door")

    def test_an_airborne_refile_climbs_from_where_it_is_not_from_the_ground(self):
        runtime, adapter = make_runtime([self.building])
        # 떠 있는 기체는 지금 고도(112m, 옥상 띠 위)에서 첫 구간 고도까지가 기둥입니다.
        runtime.telemetry = {"drone-01": {**ground(0, 0), "alt_m": 112.0, "state": "cruising"}}
        decision = runtime.file(route("drone-01", [leg(0, 0, 115.0), leg(600, 0, 115.0)]))
        self.assertIs(decision.verdict, Verdict.AUTO, decision.reason)
        self.assertTrue(decision.committed)
        self.assertEqual(adapter.sent[0][1], "fly_route")

    def test_an_airborne_descent_through_a_band_is_refused_unless_already_inside_it(self):
        # 출발점 위에만 걸린 50~80m 띠. 112m 에서 40m 로 내려오는 기둥이 띠를 지납니다.
        band = Volume("nofly-band", "띠", box(LAT0 - north(10), LON0 - east(20),
                                              LAT0 + north(10), LON0 + east(20)),
                      floor_m=50.0, ceiling_m=80.0, source="시험")
        runtime, adapter = make_runtime([band])
        runtime.telemetry = {"drone-01": {**ground(0, 0), "alt_m": 112.0, "state": "cruising"}}
        legs = [leg(0, 0, 40.0), leg(600, 0, 40.0)]
        decision = runtime.file(route("drone-01", legs))
        self.assertIs(decision.verdict, Verdict.DENIED)
        params = ledger_lines(runtime)[-1]["proposal"]["params"]
        self.assertEqual((params["blocked_kind"], params["blocked_volume"]),
                         ("takeoff", "nofly-band"))
        # 이미 띠 안에 있는 기체(회수돼 나온 자리)는 그 사실을 계획으로 보지 않습니다 —
        # 내려올 길은 내야 합니다.
        runtime.telemetry = {"drone-01": {**ground(0, 0), "alt_m": 60.0, "state": "cruising"}}
        decision = runtime.file(route("drone-01", legs))
        self.assertTrue(decision.committed, decision.reason)
        self.assertEqual(adapter.sent[-1][1], "fly_route")

    def test_the_route_check_records_the_order_of_checks(self):
        legs = [leg(0, 0, 115.0), leg(600, 0, 115.0)]
        self.runtime.file(route("drone-01", legs))
        context = ledger_lines(self.runtime)[-1]["context"]
        self.assertEqual(context["checks_run"], ["dedupe", "form", "endpoints", "route", "columns"])


# ---------- C2. 의도(4D) ----------


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.performance = config_load(CONFIG).performance

    def test_declared_performance_matches_the_simulator(self):
        """런타임이 셈하는 시간과 시뮬레이터가 나는 시간이 같은 숫자여야 합니다."""
        self.assertEqual(self.performance.cruise_mps, sim_world.CRUISE_MPS)
        self.assertEqual(self.performance.climb_mps, sim_world.CLIMB_MPS)
        self.assertEqual(self.performance.descent_mps, sim_world.DESCENT_MPS)
        self.assertEqual(self.performance.seconds_per_tick, sim_world.SIM_SECONDS_PER_TICK)
        self.assertEqual(self.performance.clearance_ticks, sim_world.CLEARANCE_TICKS)
        self.assertEqual(self.performance.clock_epoch_z, sim_world.CLOCK.epoch_z)
        # 항법 오차는 시뮬레이터가 모서리를 자르는 만큼(경유점 반경)보다 커야 합니다.
        self.assertGreaterEqual(self.performance.nav_tolerance_m, sim_world.ARRIVAL_RADIUS_M)
        # 자리는 창고 옥상 위 22 m 간격입니다. 옆자리는 회랑(40 m) 안이라 같은 틱에 뜨면 이륙 기둥이
        # 겹치고, 그때는 런타임이 한 대를 기다리게 합니다(위 TakeoffColumn·delay 시험). 자리끼리는
        # 최소한 항법 오차 두 배는 떨어져 있어야 앉은 기체끼리 겹쳐 보이지 않습니다.
        seat0 = sim_world.to_latlon(*sim_world.seat_of(0))
        seat1 = sim_world.to_latlon(*sim_world.seat_of(1))
        spacing = math.hypot((seat1[1] - seat0[1]) * math.cos(math.radians(seat0[0])),
                             seat1[0] - seat0[0]) * 111_320
        self.assertGreaterEqual(spacing, 2 * self.performance.nav_tolerance_m)

    def test_windows_follow_climb_cruise_and_descent_in_order(self):
        # 0 → 40m 상승(1.6m/틱 → 25틱), 870m 순항(17.6m/틱 → 50틱), 40 → 100m 상승(38틱),
        # 170m 순항(10틱), 100 → 0 하강(1.4m/틱 → 72틱)
        legs = [leg(0, 0, 40.0), leg(870, 0, 40.0), leg(1040, 0, 100.0)]
        volumes, arrive = schedule(legs, depart_tick=1000, start_alt_m=0.0,
                                   performance=self.performance)
        kinds = [(v.leg, v.is_column, v.t_enter, v.t_exit, v.alt_lo, v.alt_hi) for v in volumes]
        self.assertEqual(kinds, [
            (1, True, 1000, 1025, 0.0, 40.0),
            (1, False, 1025, 1075, 40.0, 40.0),
            (2, True, 1075, 1113, 40.0, 100.0),
            (2, False, 1113, 1123, 100.0, 100.0),
            (2, True, 1123, 1195, 0.0, 100.0),
        ])
        self.assertEqual(arrive, 1195)
        cruise = volumes[1]
        self.assertEqual((cruise.from_tick, cruise.to_tick), (1025 - TIME_PAD_TICKS,
                                                              1075 + TIME_PAD_TICKS))
        # 회랑은 분리 최소치 + 신고한 항법 오차(옆 10m, 위아래 한 틱의 승강 1.6m)만큼 넓습니다.
        lateral, vertical = corridor_widths(self.performance)
        self.assertEqual((lateral, vertical), (TRAFFIC_LATERAL_M + 10.0, TRAFFIC_VERTICAL_M + 1.6))
        self.assertAlmostEqual(cruise.floor_m, 40 - vertical)
        self.assertAlmostEqual(cruise.ceiling_m, 40 + vertical)
        self.assertEqual(cruise.lateral_m, lateral)

    def test_a_volume_contains_a_point_only_inside_all_four_dimensions(self):
        legs = [leg(0, 0, 60.0), leg(1000, 0, 60.0)]
        volumes, _ = schedule(legs, 0, 0.0, self.performance)
        cruise = volumes[1]
        mid = (LAT0 + north(500), LON0)
        self.assertTrue(cruise.contains(mid[0], mid[1], 60.0, cruise.t_enter + 10))
        self.assertTrue(cruise.contains(mid[0], mid[1] + east(39.9), 60.0, cruise.t_enter + 10))
        self.assertFalse(cruise.contains(mid[0], mid[1] + east(40.5), 60.0, cruise.t_enter + 10))
        self.assertFalse(cruise.contains(mid[0], mid[1], 60.0 + 27.0, cruise.t_enter + 10))
        self.assertTrue(cruise.contains(mid[0], mid[1], 60.0 + 26.0, cruise.t_enter + 10))
        self.assertFalse(cruise.contains(mid[0], mid[1], 60.0, cruise.to_tick))
        self.assertTrue(cruise.contains(mid[0], mid[1], 60.0, cruise.from_tick))
        polygon = cruise.polygon()
        self.assertEqual(len(polygon), 4)
        self.assertGreater(max(p[1] for p in polygon) - min(p[1] for p in polygon),
                           east(59.0))


class RegistryTest(unittest.TestCase):
    def _intent(self, asset="drone-01", depart=100):
        performance = config_load(CONFIG).performance
        legs = [leg(0, 0, 60.0), leg(500, 0, 60.0)]
        volumes, arrive = schedule(legs, depart, 0.0, performance)
        return Intent(asset=asset, proposal_id="p", volumes=volumes, start=(LAT0, LON0),
                      landing=(LAT0 + north(500), LON0), depart_tick=depart,
                      arrive_tick=arrive, filed_tick=depart - 25)

    def test_accepted_activated_ended_follow_the_telemetry(self):
        registry = IntentRegistry()
        intent = self._intent()
        registry.accept(intent)
        self.assertEqual(intent.state, ACCEPTED)
        registry.observe({"drone-01": {"alt_m": 0.0}}, 110)
        self.assertEqual(intent.state, ACCEPTED, "땅에 있으면 아직 accepted")
        registry.observe({"drone-01": {"alt_m": 12.0}}, 130)
        self.assertEqual(intent.state, ACTIVATED)
        registry.observe({"drone-01": {"alt_m": 0.5}}, 200)
        self.assertEqual((intent.state, intent.ended_reason), (ENDED, "arrived"))
        self.assertEqual(registry.others("drone-02"), [])

    def test_a_new_approval_replaces_and_a_stale_acceptance_expires(self):
        registry = IntentRegistry()
        first = self._intent()
        registry.accept(first)
        second = self._intent(depart=400)
        replaced = registry.accept(second)
        self.assertIs(replaced, first)
        self.assertEqual((first.state, first.ended_reason), (ENDED, "replaced"))
        registry.observe({"drone-01": {"alt_m": 0.0}}, second.to_tick + 1)
        self.assertEqual((second.state, second.ended_reason), (ENDED, "expired"))
        self.assertEqual([i["state"] for i in registry.snapshot()], ["ended"])

    def test_the_snapshot_carries_what_the_screen_needs(self):
        registry = IntentRegistry()
        registry.accept(self._intent())
        row = registry.snapshot()[0]
        self.assertEqual(set(row) >= {"asset", "state", "id", "from_tick", "to_tick"}, True)
        self.assertLess(row["from_tick"], row["to_tick"])


# ---------- C3. 전략적 비충돌 ----------


class StrategicConflictTest(unittest.TestCase):
    """먼저 낸 쪽이 이깁니다. 겹치는 것은 공간과 시간이 같이 겹칠 때뿐입니다."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.telemetry = {
            "drone-01": ground(0, 0), "drone-02": ground(0, 82),
            "drone-03": ground(0, 164), "drone-04": ground(0, 246),
        }
        # 01 은 북동으로, 02 는 북서로 — 자리 60m 북쪽에서 직선이 교차합니다.
        self.first = [leg(0, 0, 60.0), leg(1500, 1500, 60.0)]
        self.crossing = [leg(0, 82, 60.0), leg(1500, -1500, 60.0)]

    def approve_first(self):
        decision = self.runtime.file(route("drone-01", self.first))
        self.assertTrue(decision.committed, decision.reason)
        return decision

    def last_params(self):
        return ledger_lines(self.runtime)[-1]["proposal"]["params"]

    def test_the_second_filing_through_the_first_corridor_is_refused_with_the_fields(self):
        self.approve_first()
        decision = self.runtime.file(route("drone-02", self.crossing))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.code, "airspace")
        self.assertEqual(decision.policy_hit, "traffic")
        self.assertEqual(decision.forbids, "drone-01")
        params = self.last_params()
        self.assertEqual(params["blocked_kind"], "traffic")
        self.assertEqual(params["blocked_asset"], "drone-01")
        self.assertEqual(params["blocked_leg"], 1)
        self.assertIn("lat", params["blocked_at"])
        self.assertIsInstance(params["blocked_until_tick"], int)
        self.assertEqual(decision.detail["blocked_until_tick"], params["blocked_until_tick"])
        # 교차점은 새 경로 위의 점이고, 상대 회랑(30m) 안입니다
        at = (params["blocked_at"]["lat"], params["blocked_at"]["lon"])
        other = self.runtime.intents.get("drone-01")
        self.assertTrue(any(v.contains(at[0], at[1], 60.0, v.t_enter) for v in other.volumes))
        self.assertEqual([s[1] for s in self.adapter.sent], ["fly_route"])

    def test_thirty_metres_higher_clears_the_band_and_is_approved(self):
        self.approve_first()
        lifted = [{**point, "alt_m": 90.0} for point in self.crossing]
        decision = self.runtime.file(route("drone-02", lifted, resolution="altitude",
                                           altitude_shift_m=30))
        self.assertTrue(decision.committed, decision.reason)
        self.assertEqual(self.last_params()["resolution"], "altitude")
        self.assertEqual(len(self.runtime.intents.live()), 2)

    def test_a_centimetre_matters_but_far_enough_in_time_does_not(self):
        self.approve_first()
        other = self.runtime.intents.get("drone-01")
        # 같은 길을 상대 의도가 끝난 뒤에 나가면 겹치지 않습니다
        decision = self.runtime.file(route("drone-02", self.crossing, resolution="delay",
                                           holding_for="drone-01",
                                           depart_after_tick=other.to_tick))
        self.assertTrue(decision.committed, decision.reason)
        mine = self.runtime.intents.get("drone-02")
        self.assertEqual(mine.depart_tick, other.to_tick)
        self.assertEqual(self.last_params()["holding_for"], "drone-01")

    def test_own_intents_never_block_a_refile(self):
        self.approve_first()
        self.runtime.tick += 20   # 중복 방지 창(15틱) 밖에서 다시 냅니다
        again = self.runtime.file(route("drone-01", [leg(0, 0, 60.0), leg(1500, -1500, 60.0)]))
        self.assertTrue(again.committed, again.reason)
        self.assertEqual(len(self.runtime.intents.live()), 1, "새 승인이 앞 의도를 대신합니다")

    def test_parallel_corridors_forty_metres_apart_do_not_conflict(self):
        self.approve_first()
        # 대각선에서 동쪽으로 60m 는 수직 거리 42m 입니다(30m 회랑 밖)
        beside = [leg(0, 60, 60.0), leg(1500, 1560, 60.0)]
        decision = self.runtime.file(route("drone-02", beside))
        self.assertTrue(decision.committed, decision.reason)

    def test_two_aircraft_cannot_land_on_one_site_inside_the_window(self):
        # 01 은 틱 600 에 뜨기로 하고 자리에 서 있습니다. 회랑 부피는 570 부터만 자리를 덮지만
        # 그 전에도 기체는 거기 있습니다 — 04 가 그 자리(10m 옆)에 틱 180 쯤 내리려 합니다.
        decision = self.runtime.file(route("drone-01", self.first, depart_after_tick=600))
        self.assertTrue(decision.committed, decision.reason)
        onto_the_seat = [leg(0, 246, 60.0), leg(0, 10, 60.0)]
        decision = self.runtime.file(route("drone-04", onto_the_seat))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "traffic")
        params = self.last_params()
        self.assertEqual(params["blocked_kind"], "landing")
        self.assertEqual(params["blocked_asset"], "drone-01")
        self.assertEqual(params["blocked_until_tick"], 600 + TIME_PAD_TICKS)
        self.assertEqual(decision.detail["blocked_kind"], "landing")
        # 01 이 나중에 내릴 자리(20m 옆)에 03 이 먼저 내리는 것도 안 됩니다 — 01 이 올 때 03 이
        # 거기 서 있습니다. 내린 기체는 다음 승인까지 그 자리에 있고, 그게 언제인지는 모릅니다.
        arriving = [leg(0, 164, 60.0), leg(1500, 1520, 60.0)]
        decision = self.runtime.file(route("drone-03", arriving))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual((self.last_params()["blocked_kind"], self.last_params()["blocked_asset"]),
                         ("landing", "drone-01"))
        # 140m 옆 자리는 됩니다. 같은 무렵 그 자리로 오는 04 는 03 에 막힙니다.
        beside = [leg(0, 164, 60.0), leg(1400, 1600, 60.0)]
        self.assertTrue(self.runtime.file(route("drone-03", beside)).committed)
        same_site = [leg(0, 246, 60.0), leg(1400, 1600, 60.0)]
        decision = self.runtime.file(route("drone-04", same_site))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(self.last_params()["blocked_asset"], "drone-03")

    # 떠 있는 02 가 북서쪽 2km 에서 01 의 대각선 회랑을 가로질러 옵니다. 01 이 그 자리를 지날
    # 무렵(틱 190 안팎) 02 도 거기(틱 220 안팎) 있습니다 — 01 의 구간 창 [120, 301) 안입니다.
    AIRBORNE_AT = (1500, -1400)
    ACROSS = [leg(1500, -1400, 60.0), leg(-100, 1500, 60.0)]

    def test_an_airborne_contingent_refile_withdraws_the_undeparted_intent(self):
        self.approve_first()
        # 02 는 떠 있습니다(회수돼 나온 자리). 01 은 아직 안 떴습니다.
        self.runtime.telemetry["drone-02"] = {**ground(*self.AIRBORNE_AT), "alt_m": 60.0,
                                              "state": "cruising"}
        decision = self.runtime.file(route("drone-02", self.ACROSS))
        self.assertTrue(decision.committed, decision.reason)
        first = self.runtime.intents.get("drone-01")
        self.assertEqual((first.state, first.ended_reason), (ENDED, "withdrawn"))
        self.assertEqual(self.last_params().get("withdrew"), ["drone-01"])
        pulled = [s for s in self.adapter.sent if s[1] == "divert_ground"]
        self.assertEqual(len(pulled), 1)
        self.assertEqual(pulled[0][0], "drone-01")
        self.assertEqual(pulled[0][2]["withdrawn_for"], "drone-02")
        entry = next(e for e in ledger_lines(self.runtime)
                     if e["decision"].get("code") == "withdrawn" and e["outcome"] == "done")
        self.assertEqual(entry["proposal"]["author"], "runtime")
        self.assertEqual(entry["decision"]["policy_hit"], "traffic")
        self.assertEqual(entry["context"]["intent_id"], first.id)
        self.assertEqual(entry["context"]["checks_run"], ["withdraw"])
        # 물린 기체는 바로 다시 낼 수 있습니다 — 중복으로 거절되지 않습니다
        again = self.runtime.file(route("drone-01", [leg(0, 0, 60.0), leg(1500, 1500, 90.0)]))
        self.assertNotEqual(again.code, "duplicate")

    def test_an_airborne_refile_against_an_activated_intent_is_still_refused(self):
        self.approve_first()
        self.runtime.telemetry["drone-01"] = {**ground(0, 0), "alt_m": 60.0, "state": "delivering"}
        self.runtime.telemetry["drone-02"] = {**ground(*self.AIRBORNE_AT), "alt_m": 60.0,
                                              "state": "cruising"}
        self.runtime.intents.observe(self.runtime.telemetry, self.runtime.tick)
        self.assertEqual(self.runtime.intents.get("drone-01").state, ACTIVATED)
        decision = self.runtime.file(route("drone-02", self.ACROSS))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "traffic")
        self.assertEqual([s[1] for s in self.adapter.sent], ["fly_route"], "물린 것이 없습니다")

    def test_a_recall_ends_the_route_and_leaves_only_the_hover_in_the_way(self):
        self.approve_first()
        flown = self.runtime.intents.get("drone-01")
        self.runtime.telemetry["drone-01"] = {
            **ground(0, 0), "alt_m": 60.0, "state": "delivering",
            "route": [{"lat": self.first[1]["lat"], "lon": self.first[1]["lon"], "alt_m": 60.0}],
        }
        closing = Volume("nofly-t", "닫힘", box(LAT0 + north(700), LON0 + east(600),
                                                LAT0 + north(900), LON0 + east(900)))
        pulled = self.runtime.recall_flights(closing)
        self.assertEqual(len(pulled), 1)
        self.assertEqual((flown.state, flown.ended_reason), (ENDED, "recalled"))
        # 경로는 끝났지만 기체는 (0,0) 60m 에 떠 있습니다. 그 자리는 끝없는 기둥으로 남습니다.
        standing = self.runtime.intents.get("drone-01")
        self.assertEqual((standing.kind, standing.state), (CONTINGENCY, ACTIVATED))
        self.assertEqual(standing.to_tick, OPEN_ENDED_TICK + TIME_PAD_TICKS)
        # 02 의 대각선은 (0,0) 에서 56m 비켜 갑니다 — 옛 회랑은 더는 막지 않습니다.
        decision = self.runtime.file(route("drone-02", self.crossing))
        self.assertTrue(decision.committed, decision.reason)

    def test_the_snapshot_lists_intents(self):
        self.approve_first()
        rows = self.runtime.snapshot()["intents"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset"], "drone-01")
        self.assertEqual(rows[0]["state"], "accepted")
        self.assertTrue(rows[0]["id"].startswith("i_"))

    def test_first_conflict_reports_the_first_sample_along_the_new_route(self):
        self.approve_first()
        other = self.runtime.intents.get("drone-01")
        volumes, _ = schedule(self.crossing, self.runtime.tick + 25, 0.0,
                              self.runtime.performance)
        conflict = first_conflict(volumes, [other])
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict.asset, "drone-01")
        # 새 경로의 첫 구간 위, 출발점보다 앞선 자리입니다
        self.assertGreater(conflict.at[0], LAT0)
        self.assertGreaterEqual(conflict.until_tick, conflict.tick)


# ---------- C4. 운영사의 해결 사다리 (오프라인 하네스 = loop.py 의 거울) ----------


class ResolutionLadderTest(unittest.TestCase):
    def setUp(self):
        from tests.test_two_worlds import GuardedSide

        self.runtime, self.adapter = make_runtime()
        self.runtime.telemetry = {"drone-01": ground(0, 0), "drone-02": ground(0, 82)}
        self.runtime.pad_coords = {}
        self.side = GuardedSide(self.runtime)
        # 운영사의 직선은 빈 하늘에서 70m(FLOOR_ALT_M)입니다. 01 도 70m 로 두면 02 의 직선이
        # 겹치고, 30m 올린 100m 는 01 의 띠(45~95m) 밖입니다.
        self.first = [leg(0, 0, 70.0), leg(1500, 1500, 70.0)]
        self.assertTrue(self.runtime.file(route("drone-01", self.first)).committed)

    def _telemetry(self, asset, goal):
        return {**self.runtime.telemetry[asset], "id": asset,
                "job_lat": goal["lat"], "job_lon": goal["lon"], "job": "x"}

    def test_altitude_first(self):
        proposal = Proposal.from_dict(route("drone-02", []))
        goal = leg(1500, -1500, 0.0)
        decision = self.side._file(proposal, self._telemetry("drone-02", goal))
        self.assertTrue(decision.committed, decision.reason)
        done = [e for e in ledger_lines(self.runtime) if e["outcome"] == "done"
                and e["proposal"]["asset_id"] == "drone-02"]
        params = done[-1]["proposal"]["params"]
        self.assertEqual(params["resolution"], "altitude")
        self.assertEqual(params["altitude_shift_m"], 30.0)
        self.assertTrue(all(point["alt_m"] >= 70.0 + 30.0 - 1e-6 for point in params["legs"]))
        refused = [e for e in ledger_lines(self.runtime) if e["decision"]["verdict"] == "denied"]
        self.assertEqual([e["decision"]["policy_hit"] for e in refused], ["traffic"])

    def test_delay_when_the_ceiling_leaves_no_room_above(self):
        # 이 하늘의 천장은 100m: 70 + 30 은 넘습니다 → 출발 지연으로.
        self.runtime.airspace.default_ceiling_m = 100.0
        self.side.planner = type(self.side.planner)(self.runtime.airspace)
        proposal = Proposal.from_dict(route("drone-02", []))
        goal = leg(1500, -1500, 0.0)
        decision = self.side._file(proposal, self._telemetry("drone-02", goal))
        self.assertTrue(decision.committed, decision.reason)
        done = [e for e in ledger_lines(self.runtime) if e["outcome"] == "done"
                and e["proposal"]["asset_id"] == "drone-02"]
        params = done[-1]["proposal"]["params"]
        self.assertEqual(params["resolution"], "delay")
        self.assertEqual(params["holding_for"], "drone-01")
        other = self.runtime.intents.get("drone-01")
        self.assertGreaterEqual(params["depart_after_tick"], other.volumes[1].to_tick)
        self.assertEqual(self.runtime.intents.get("drone-02").depart_tick,
                         params["depart_after_tick"])

    def test_the_ladder_is_finite(self):
        """고도도 지연도 안 되면 A* 로 다시 그리고, 그것도 안 되면 이번 차례는 접습니다."""
        from attache.agent.loop import MAX_DELAY_TRIES
        from attache.runtime.intents import Volume4D

        filings = []
        original = self.runtime.file

        def counting(raw):
            filings.append(raw["params"].get("resolution"))
            return original(raw)

        self.runtime.file = counting
        # 하늘 전부를 차례로 덮는 의도 여섯 개. 어느 틱으로 미뤄도 다음 것이 기다립니다.
        for index in range(6):
            span = 10 ** 6
            everywhere = Volume4D(1, (LAT0, LON0), (LAT0, LON0), 0.0, 1000.0,
                                  index * span, (index + 1) * span, lateral_m=10 ** 7)
            self.runtime.intents.accept(Intent(
                asset=f"drone-1{index}", proposal_id="p", volumes=[everywhere],
                start=(LAT0 + north(5000), LON0), landing=(LAT0 + north(6000), LON0),
                depart_tick=index * span, arrive_tick=(index + 1) * span, filed_tick=0))
        proposal = Proposal.from_dict(route("drone-02", []))
        decision = self.side._file(proposal, self._telemetry("drone-02", leg(1500, -1500, 0.0)))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "traffic")
        self.assertLessEqual(filings.count("delay"), 2 * MAX_DELAY_TRIES)
        self.assertLessEqual(len(filings), 2 * (2 + MAX_DELAY_TRIES))


# ---------- C5. 시뮬레이터: 분리 상실과 미룬 출발 ----------


class SeparationLossTest(unittest.TestCase):
    def test_one_episode_per_pair_not_per_tick(self):
        world = sim_world.Simulation().worlds["direct"]
        first, second = world.vehicles["drone-01"], world.vehicles["drone-02"]
        for vehicle in (first, second):
            vehicle.state, vehicle.alt = "delivering", 60.0
        second.x, second.y = first.x, first.y
        world._detect_separation_losses(1)
        world._detect_separation_losses(2)
        self.assertEqual(world.score.separation_losses, 1)
        second.x = first.x + 100 / sim_world.METRES_PER_CELL_X
        world._detect_separation_losses(3)
        second.x = first.x
        world._detect_separation_losses(4)
        self.assertEqual(world.score.separation_losses, 2)
        # 수직으로 25m 이상 떨어지면 분리 상실이 아닙니다. 땅에 있는 기체도 세지 않습니다.
        second.alt = 60.0 + 30.0
        world._detect_separation_losses(5)
        second.alt, second.state = 0.0, "ready"
        world._detect_separation_losses(6)
        self.assertEqual(world.score.separation_losses, 2)
        self.assertIn("separation_losses", world.snapshot(6)["scoreboard"])

    def test_the_opening_stops_of_02_and_04_cross_near_the_seats(self):
        """직결 세계의 분리 상실은 우연이 아니라 배치입니다."""
        areas = {a["name"]: (a["lat"], a["lon"]) for a in sim_world.LANDING_AREAS}
        seat2 = sim_world.to_latlon(*sim_world.seat_of(1))
        seat4 = sim_world.to_latlon(*sim_world.seat_of(3))
        goal2 = areas[sim_world.OPENING_STOPS["drone-02"]]
        goal4 = areas[sim_world.OPENING_STOPS["drone-04"]]

        def ccw(a, b, c):
            return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])

        crosses = (ccw(seat2, goal4[:2], seat4) != ccw(goal2, goal4, seat4)
                   and ccw(seat2, goal2, seat4) != ccw(seat2, goal2, goal4))
        self.assertTrue(crosses, "02·04 의 직선이 교차하지 않습니다")

    def test_a_delayed_departure_waits_on_the_ground_and_says_for_whom(self):
        world = sim_world.Simulation().worlds["guarded"]
        vehicle = world.vehicles["drone-01"]
        lat0, lon0 = sim_world.to_latlon(vehicle.x, vehicle.y)
        lat1, lon1 = sim_world.to_latlon(vehicle.job_x, vehicle.job_y)
        result = world.act("drone-01", "fly_route", {
            "legs": [{"lat": lat0, "lon": lon0, "alt_m": 55.0}, {"lat": lat1, "lon": lon1,
                                                                   "alt_m": 55.0}],
            "depart_after_tick": 200, "holding_for": "drone-03"}, "l_1", "schedule", None, 1)
        self.assertTrue(result["ok"])
        for tick in range(2, 200):
            world.tick(tick)
        self.assertEqual(vehicle.state, "ready")
        self.assertEqual(vehicle.alt, 0.0)
        self.assertEqual(vehicle.public()["holding_for"], "drone-03")
        world.tick(200)
        world.tick(201)
        self.assertEqual(vehicle.state, "delivering")
        self.assertIsNone(vehicle.public()["holding_for"])
        # 미루지 않은 승인은 전처럼 바로 뜹니다
        other = world.vehicles["drone-02"]
        self.assertIsNone(other.public()["holding_for"])

    def test_a_withdrawal_on_the_ground_leaves_the_aircraft_ready_to_refile(self):
        world = sim_world.Simulation().worlds["guarded"]
        vehicle = world.vehicles["drone-01"]
        lat0, lon0 = sim_world.to_latlon(vehicle.x, vehicle.y)
        lat1, lon1 = sim_world.to_latlon(vehicle.job_x, vehicle.job_y)
        world.act("drone-01", "fly_route", {
            "legs": [{"lat": lat0, "lon": lon0, "alt_m": 55.0},
                     {"lat": lat1, "lon": lon1, "alt_m": 55.0}],
            "depart_after_tick": 300, "holding_for": "drone-03"}, "l_1", "schedule", None, 1)
        for tick in range(2, 60):
            world.tick(tick)
        self.assertEqual(vehicle.state, "ready")
        world.act("drone-01", "divert_ground", {"withdrawn_for": "drone-04"}, "l_2", "cargo",
                  None, 60)
        self.assertEqual(vehicle.waypoints, [])
        self.assertEqual(vehicle.state, "ready")
        self.assertIsNone(vehicle.holding_for)
        self.assertIsNotNone(vehicle.job_x, "주문은 그대로입니다. 운영사가 다시 냅니다")


# ---------- C6. 원장 맥락 ----------


class LedgerContextTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.telemetry = {"drone-01": ground(0, 0)}
        self.legs = [leg(0, 0, 60.0), leg(800, 0, 60.0)]

    def test_an_approved_route_records_every_check_in_order_and_its_intent(self):
        decision = self.runtime.file(route("drone-01", self.legs))
        self.assertTrue(decision.committed)
        entry = [e for e in ledger_lines(self.runtime) if e["outcome"] == "done"][-1]
        context = entry["context"]
        self.assertEqual(context["tick"], 100)
        self.assertEqual(context["airspace_revision"], self.runtime.airspace.revision)
        self.assertEqual(context["policies"], [])
        self.assertEqual(context["checks_run"], ["dedupe", "form", "endpoints", "route", "columns",
                                                 "landing", "contingency", "traffic",
                                                 "landing_site", "authority"])
        self.assertEqual(context["intent_id"], self.runtime.intents.get("drone-01").id)
        self.assertEqual(entry["decision"]["ledger_id"], entry["id"])

    def test_a_duplicate_filing_is_denied_on_the_record(self):
        self.runtime.file(route("drone-01", self.legs))
        self.runtime.telemetry["drone-01"]["route"] = self.legs[1:]
        again = self.runtime.file(route("drone-01", self.legs))
        self.assertIs(again.verdict, Verdict.DENIED)
        self.assertEqual(again.code, "duplicate")
        entry = ledger_lines(self.runtime)[-1]
        self.assertEqual(entry["decision"]["code"], "duplicate")
        self.assertEqual(entry["outcome"], "denied")
        self.assertEqual(entry["context"]["checks_run"], ["dedupe"])

    def test_policies_in_force_are_named(self):
        from attache.core.config import Policy

        self.runtime.policies.add(Policy("ad-1", "지시", forbid_action="fast_charge"))
        self.runtime.policies.add(Policy("later", "나중", forbid_action="charge",
                                         active_from_tick=10_000))
        self.runtime.file(route("drone-01", self.legs))
        self.assertEqual(ledger_lines(self.runtime)[-1]["context"]["policies"], ["ad-1"])

    def test_replay_still_reads_the_ledger(self):
        from attache.runtime.replay import read_commits

        self.runtime.file(route("drone-01", self.legs))
        commits = read_commits(self.runtime.ledger.path)
        self.assertEqual(len(commits), 1)
        self.assertIn("context", commits[0])


# ---------- C7. 검토가 찾은 구멍: 떠 있는 기체, 양 끝, 순응, 착륙장, 회랑 폭, 물림 시점 ----------


class AirbornePresenceTest(unittest.TestCase):
    """떠 있는 기체는 의도가 없어도 어딘가에 있습니다. 그 자리는 판정이 봐야 합니다."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.telemetry = {"drone-01": ground(0, 0), "drone-02": ground(800, -250)}
        self.first = [leg(0, 0, 60.0), leg(1500, 0, 60.0)]
        self.assertTrue(self.runtime.file(route("drone-01", self.first)).committed)

    def _recall_at(self, north_m: float):
        """01 이 north_m 북쪽에 떠 있는데 앞이 닫힙니다. (회수 결정, 나갈 자리)."""
        self.runtime.telemetry["drone-01"] = {
            **ground(north_m, 0), "alt_m": 60.0, "state": "delivering",
            "route": [{"lat": self.first[1]["lat"], "lon": self.first[1]["lon"], "alt_m": 60.0}]}
        closing = Volume("nofly-ahead", "닫힘", box(LAT0 + north(700), LON0 - east(100),
                                                    LAT0 + north(1100), LON0 + east(100)))
        pulled = self.runtime.recall_flights(closing)
        self.assertEqual(len(pulled), 1)
        sent = [s for s in self.adapter.sent if s[1] == "divert_ground"][-1]
        door = sent[2].get("exit")
        return pulled[0], door

    def test_a_recalled_aircraft_hovering_at_the_exit_blocks_a_filing_through_it(self):
        # 구역 안(800m)에서 회수 → 가장 가까운 바깥(남쪽 경계 아래)으로 나가 거기 떠 있습니다.
        _, door = self._recall_at(800)
        self.assertIsNotNone(door)
        standing = self.runtime.intents.get("drone-01")
        self.assertEqual((standing.kind, standing.state), (CONTINGENCY, ACTIVATED))
        self.assertEqual(len(standing.volumes), 2, "나가는 길 + 끝없는 기둥")
        self.assertEqual(standing.volumes[-1].t_exit, OPEN_ENDED_TICK)
        # 나갔습니다. 텔레메트리도 그 자리에 떠 있다고 합니다.
        hover = (door["lat"], door["lon"])
        self.runtime.telemetry["drone-01"] = {"lat": hover[0], "lon": hover[1], "alt_m": 60.0,
                                              "state": "cruising"}
        self.runtime.tick += 60
        # 02 가 그 자리를 동서로 가로지르는 직선을 냅니다.
        hover_n = (hover[0] - LAT0) * METRES_PER_DEG_LAT
        self.runtime.telemetry["drone-02"] = ground(hover_n, -250)
        across = [leg(hover_n, -250, 60.0), leg(hover_n, 250, 60.0)]
        decision = self.runtime.file(route("drone-02", across))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "traffic")
        self.assertEqual(decision.forbids, "drone-01")
        self.assertEqual(decision.detail["blocked_intent"], standing.id)
        # 30m 위로는 지나갑니다(기둥은 위아래 ±26.6m).
        lifted = [{**point, "alt_m": 90.0} for point in across]
        self.assertTrue(self.runtime.file(route("drone-02", lifted)).committed)

    def test_the_hover_ends_when_the_aircraft_lands_or_files_again(self):
        self._recall_at(800)
        standing = self.runtime.intents.get("drone-01")
        self.runtime.telemetry["drone-01"] = {**ground(700, 0), "alt_m": 60.0, "state": "cruising"}
        self.runtime.tick += 20
        again = self.runtime.file(route("drone-01", [leg(700, 0, 60.0), leg(700, 600, 60.0)]))
        self.assertTrue(again.committed, again.reason)
        self.assertEqual((standing.state, standing.ended_reason), (ENDED, "replaced"))
        # 내려앉으면 끝납니다
        landed = self.runtime.intents.get("drone-01")
        self.runtime.intents.observe(self.runtime.telemetry, self.runtime.tick + 1)
        self.assertEqual(landed.state, ACTIVATED)
        self.runtime.telemetry["drone-01"] = ground(700, 600)
        self.runtime.intents.observe(self.runtime.telemetry, self.runtime.tick + 200)
        self.assertEqual((landed.state, landed.ended_reason), (ENDED, "arrived"))

    def test_an_aircraft_that_never_filed_still_occupies_its_place_in_the_air(self):
        # 등록부에 아무것도 없는 03 이 (800, 300) 60m 에 떠 있습니다(다른 회사, 직결 배선…).
        self.runtime.telemetry["drone-03"] = {**ground(800, 300), "alt_m": 60.0,
                                              "state": "cruising"}
        self.runtime.telemetry["drone-02"] = ground(800, 100)
        through = [leg(800, 100, 60.0), leg(800, 800, 60.0)]
        decision = self.runtime.file(route("drone-02", through))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.forbids, "drone-03")
        self.assertIsNone(decision.detail["blocked_intent"], "의도가 아니라 자리입니다")
        self.assertEqual(self.runtime.intents.get("drone-03"), None, "등록부에는 들어가지 않습니다")
        # 땅에 있으면 자리를 차지하지 않습니다(착륙장 점유는 따로 봅니다)
        self.runtime.telemetry["drone-03"] = ground(800, 300)
        self.assertTrue(self.runtime.file(route("drone-02", through)).committed)

    def test_a_declined_job_in_the_air_leaves_the_hover_behind(self):
        self.runtime.telemetry["drone-01"] = {**ground(400, 0), "alt_m": 60.0,
                                              "state": "delivering"}
        self.runtime.tick += 20
        declined = Proposal(asset_id="drone-01", action="decline_job", cost_usd=0.0,
                            blast_radius="none", rationale="규정상 경로 없음", params={})
        self.assertTrue(self.runtime.file(declined.to_dict()).committed)
        standing = self.runtime.intents.get("drone-01")
        self.assertEqual((standing.kind, standing.state), (CONTINGENCY, ACTIVATED))
        self.assertEqual(standing.start, (self.runtime.telemetry["drone-01"]["lat"],
                                          self.runtime.telemetry["drone-01"]["lon"]))


class EndpointConformanceTest(unittest.TestCase):
    """경로의 첫 점은 기체가 있는 자리, 끝점은 갈 곳이어야 합니다. 조종장치는 그 사이만 납니다."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        goal = leg(1500, 0, 0.0)
        self.runtime.telemetry = {"drone-01": {**ground(0, 0), "job_lat": goal["lat"],
                                               "job_lon": goal["lon"], "job": "x"}}

    def test_a_first_point_away_from_the_aircraft_is_refused(self):
        decision = self.runtime.file(route("drone-01", [leg(200, 0, 60.0), leg(1500, 0, 60.0)]))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "airspace")
        entry = ledger_lines(self.runtime)[-1]
        self.assertEqual(entry["proposal"]["params"]["blocked_kind"], "origin")
        self.assertAlmostEqual(entry["proposal"]["params"]["blocked_gap_m"], 200.0, delta=1.0)
        self.assertEqual(entry["context"]["checks_run"], ["dedupe", "form", "endpoints"])
        self.assertEqual(self.adapter.sent, [])

    def test_a_last_point_away_from_the_destination_is_refused(self):
        decision = self.runtime.file(route("drone-01", [leg(0, 0, 60.0), leg(1500, 200, 60.0)]))
        self.assertIs(decision.verdict, Verdict.DENIED)
        params = ledger_lines(self.runtime)[-1]["proposal"]["params"]
        self.assertEqual(params["blocked_kind"], "destination")
        self.assertEqual(params["blocked_leg"], 1)
        # 이륙장 예약도 같습니다: 끝점은 그 이륙장이어야 합니다
        self.runtime.pad_coords = {"pad:launch": (LAT0 + north(900), LON0)}
        astray = Proposal(asset_id="drone-01", action="reserve_pad", cost_usd=28.0,
                          blast_radius="schedule", rationale="충전", resource="pad:launch",
                          params={"pad": "pad:launch",
                                  "legs": [leg(0, 0, 60.0), leg(900, 100, 60.0)]})
        decision = self.runtime.file(astray.to_dict())
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(ledger_lines(self.runtime)[-1]["proposal"]["params"]["blocked_kind"],
                         "destination")

    def test_honest_endpoints_pass_and_an_unknown_position_is_not_judged(self):
        self.assertTrue(self.runtime.file(route("drone-01", [leg(0, 0, 60.0),
                                                              leg(1500, 0, 60.0)])).committed)
        self.runtime.tick += 20
        self.runtime.telemetry["drone-02"] = {"state": "ready"}   # 자리를 모릅니다
        self.assertTrue(self.runtime.file(route("drone-02", [leg(0, 300, 60.0),
                                                              leg(1500, 300, 60.0)])).committed)


class DepartureConformanceTest(unittest.TestCase):
    """미룬 출발을 조종장치가 안 지키면 의도는 실제 출발로 옮겨지고 기록에 남습니다."""

    def test_an_early_departure_reanchors_the_intent_and_is_ledgered(self):
        runtime, _ = make_runtime()
        runtime.telemetry = {"drone-01": ground(0, 0), "drone-02": ground(0, 82)}
        first = [leg(0, 0, 60.0), leg(1500, 1500, 60.0)]
        decision = runtime.file(route("drone-01", first, depart_after_tick=600,
                                      resolution="delay", holding_for="x"))
        self.assertTrue(decision.committed, decision.reason)
        intent = runtime.intents.get("drone-01")
        self.assertEqual(intent.depart_tick, 600)
        # 틱 130 에 벌써 떠 있습니다.
        runtime.tick = 130
        runtime.telemetry["drone-01"] = {**ground(0, 0), "alt_m": 12.0, "state": "delivering"}
        runtime.snapshot()
        self.assertEqual(intent.state, ACTIVATED)
        self.assertEqual(intent.depart_tick, 130)
        self.assertEqual(intent.volumes[0].t_enter, 130)
        noted = [e for e in ledger_lines(runtime) if e["decision"].get("code") == "nonconforming"]
        self.assertEqual(len(noted), 1)
        self.assertEqual(noted[0]["proposal"]["params"]["planned_depart_tick"], 600)
        self.assertEqual(noted[0]["context"]["intent_id"], intent.id)
        self.assertEqual(noted[0]["context"]["checks_run"], ["conformance"])
        # 옮겨진 창으로 판정합니다: 02 의 교차 직선은 이제 겹칩니다(600 근처였다면 비어 있었을
        # 자리).
        crossing = [leg(0, 82, 60.0), leg(1500, -1500, 60.0)]
        refused = runtime.file(route("drone-02", crossing))
        self.assertIs(refused.verdict, Verdict.DENIED)
        self.assertEqual(refused.forbids, "drone-01")


class LandingSiteOccupancyTest(unittest.TestCase):
    """착륙장 하나에 두 대는 없습니다 — 의도로도, 서 있는 기체로도."""

    SITE = (500, 0)

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.telemetry = {"drone-01": ground(*self.SITE), "drone-02": ground(0, 300)}
        self.onto = [leg(0, 300, 60.0), leg(*self.SITE, 60.0)]

    def test_landing_on_a_parked_aircraft_is_refused_from_telemetry_alone(self):
        self.assertEqual(self.runtime.intents.live(), [])
        decision = self.runtime.file(route("drone-02", self.onto))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "traffic")
        self.assertEqual(decision.detail["blocked_kind"], "landing")
        self.assertEqual(decision.forbids, "drone-01")
        self.assertIsNone(decision.detail["blocked_intent"])
        self.assertEqual(decision.detail["blocked_until_tick"], self.runtime.tick + TIME_PAD_TICKS)

    def test_landing_after_the_parked_aircraft_has_left_is_approved(self):
        # 01 은 틱 300 에 그 자리에서 뜹니다. 그 전에 내리면 안 되고, 이륙 기둥이 빈 뒤에는 됩니다.
        away = [leg(*self.SITE, 60.0), leg(500, 1500, 60.0)]
        self.assertTrue(self.runtime.file(route("drone-01", away, depart_after_tick=300,
                                                resolution="delay", holding_for="x")).committed)
        early = self.runtime.file(route("drone-02", self.onto))
        self.assertIs(early.verdict, Verdict.DENIED)
        self.assertEqual(early.detail["blocked_kind"], "landing")
        # 01 의 나가는 구간(그 자리에서 시작)이 빈 뒤에 도착하도록 미룹니다.
        gone = self.runtime.intents.get("drone-01").to_tick
        later = self.runtime.file(route("drone-02", self.onto, depart_after_tick=gone - 60,
                                        resolution="delay", holding_for="drone-01"))
        self.assertTrue(later.committed, later.reason)
        self.assertGreater(self.runtime.intents.get("drone-02").arrive_tick, gone)

    def test_two_live_intents_never_land_on_one_site_whatever_the_gap(self):
        # 리뷰 S20: 01 이 틱 129 에 내리고 02 가 226 에 내리면 예전 규칙(착륙 기둥 ±30틱)은
        # 통과였고, 02 는 아직 상자를 내리고 있는 01 위로 내렸습니다.
        self.runtime.telemetry["drone-01"] = ground(0, -300)
        first = [leg(0, -300, 60.0), leg(*self.SITE, 60.0)]
        self.assertTrue(self.runtime.file(route("drone-01", first)).committed)
        other = self.runtime.intents.get("drone-01")
        decision = self.runtime.file(route("drone-02", self.onto,
                                           depart_after_tick=other.to_tick + 40,
                                           resolution="delay", holding_for="drone-01"))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.detail["blocked_kind"], "landing")
        self.assertEqual(decision.forbids, "drone-01")
        self.assertGreaterEqual(decision.detail["blocked_until_tick"], other.to_tick)
        # 다른 자리(60m 옆)에 내리는 것은 됩니다
        beside = [leg(0, 300, 60.0), leg(500, 60, 60.0)]
        self.assertTrue(self.runtime.file(route("drone-02", beside)).committed)

    def test_the_simulator_counts_a_descent_onto_a_parked_aircraft(self):
        world = sim_world.Simulation().worlds["direct"]
        parked, arriving = world.vehicles["drone-01"], world.vehicles["drone-02"]
        parked.state, parked.alt = "ready", 0.0
        arriving.state, arriving.alt = "landing", 20.0
        arriving.x, arriving.y = parked.x, parked.y
        world._detect_separation_losses(1)
        world._detect_separation_losses(2)
        self.assertEqual((world.score.site_conflicts, world.score.separation_losses), (1, 0))
        arriving.alt = 60.0
        world._detect_separation_losses(3)
        self.assertEqual(world.score.site_conflicts, 1, "25m 위로 지나가는 것은 아닙니다")
        self.assertIn("site_conflicts", world.snapshot(3)["scoreboard"])


class CorridorWidthTest(unittest.TestCase):
    """회랑 반폭은 분리 최소치 + 항법 오차. 31m 옆의 평행선은 겹치고, 41m 는 비켜 갑니다."""

    def setUp(self):
        self.runtime, _ = make_runtime()
        self.runtime.telemetry = {"drone-01": ground(0, 0), "drone-02": ground(0, 31),
                                  "drone-03": ground(0, 41)}
        self.assertTrue(self.runtime.file(route("drone-01", [leg(0, 0, 60.0),
                                                              leg(1500, 0, 60.0)])).committed)

    def test_thirty_one_metres_beside_is_refused_and_forty_one_is_not(self):
        decision = self.runtime.file(route("drone-02", [leg(0, 31, 60.0), leg(1500, 31, 60.0)]))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "traffic")
        decision = self.runtime.file(route("drone-03", [leg(0, 41, 60.0), leg(1500, 41, 60.0)]))
        self.assertTrue(decision.committed, decision.reason)


class DeferredWithdrawalTest(unittest.TestCase):
    """물림은 검사가 아니라 실행의 일부입니다. 실행되지 않은 재신청은 아무도 물리지 않습니다."""

    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.telemetry = {"drone-01": ground(0, 0), "drone-02": ground(0, 82)}
        first = [leg(0, 0, 60.0), leg(1500, 1500, 60.0)]
        self.assertTrue(self.runtime.file(route("drone-01", first)).committed)
        self.first = self.runtime.intents.get("drone-01")
        self.runtime.telemetry["drone-02"] = {**ground(1500, -1400), "alt_m": 60.0,
                                              "state": "cruising"}
        self.across = [leg(1500, -1400, 60.0), leg(-100, 1500, 60.0)]

    def _refile(self, **overrides):
        base = Proposal(asset_id="drone-02", action="fly_route", cost_usd=12.0,
                        blast_radius="schedule", rationale="재신청", params={"legs": self.across})
        return self.runtime.file({**base.to_dict(), **overrides})

    def _diverted(self):
        return [s for s in self.adapter.sent if s[1] == "divert_ground"]

    def test_a_refile_held_for_a_human_withdraws_nothing_until_it_is_approved(self):
        decision = self._refile(blast_radius="public")
        self.assertIs(decision.verdict, Verdict.HUMAN)
        self.assertEqual(self.first.state, ACCEPTED)
        self.assertEqual(self._diverted(), [])
        approved = self.runtime.approve(decision.proposal_id, "관제사", allow=True)
        self.assertTrue(approved.committed, approved.reason)
        self.assertEqual((self.first.state, self.first.ended_reason), (ENDED, "withdrawn"))
        self.assertEqual(len(self._diverted()), 1)
        done = [e for e in ledger_lines(self.runtime) if e["outcome"] == "done"
                and e["proposal"]["asset_id"] == "drone-02"][-1]
        self.assertEqual(done["proposal"]["params"]["withdrew"], ["drone-01"])
        self.assertNotIn("withdraw", done["proposal"]["params"])
        self.assertEqual(done["context"]["withdrew"], ["drone-01"])

    def test_an_invalid_refile_withdraws_nothing(self):
        decision = self._refile(cost_usd=-1)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(self.first.state, ACCEPTED)
        self.assertEqual(self._diverted(), [])

    def test_a_refile_the_actuator_refuses_withdraws_nothing(self):
        def refusing(asset_id, action, params, ledger_id, blast="none", approved_by=None):
            self.adapter.sent.append((asset_id, action, params, ledger_id))
            return {"ok": asset_id != "drone-02", "error": "link down"}

        self.adapter.execute = refusing
        decision = self._refile()
        self.assertFalse(decision.committed)
        self.assertEqual(self.first.state, ACCEPTED)
        self.assertEqual(self._diverted(), [])
        self.assertEqual(len(self.runtime.intents.live()), 1)


class GroundDepartureTest(unittest.TestCase):
    """미룬 출발은 땅의 어느 상태에서 받았든 지켜지고, 땅의 기체는 경로 없이 뜨지 않습니다."""

    def _world_and_legs(self):
        world = sim_world.Simulation().worlds["guarded"]
        vehicle = world.vehicles["drone-01"]
        lat0, lon0 = sim_world.to_latlon(vehicle.x, vehicle.y)
        lat1, lon1 = sim_world.to_latlon(vehicle.job_x, vehicle.job_y)
        return world, vehicle, [{"lat": lat0, "lon": lon0, "alt_m": 55.0},
                                {"lat": lat1, "lon": lon1, "alt_m": 55.0}]

    def test_a_delayed_departure_waits_whatever_the_ground_state_was(self):
        for state in ("landed", "cruising", "ready", "loading"):
            with self.subTest(state=state):
                world, vehicle, legs = self._world_and_legs()
                vehicle.state, vehicle.alt, vehicle.work_ticks = state, 0.0, (
                    10 if state == "loading" else 0)
                world.act("drone-01", "fly_route", {"legs": legs, "depart_after_tick": 200,
                                                    "holding_for": "drone-03"},
                          "l_1", "schedule", None, 1)
                for tick in range(2, 200):
                    world.tick(tick)
                self.assertEqual(vehicle.alt, 0.0)
                self.assertEqual(vehicle.state, "ready")
                world.tick(200)
                world.tick(201)
                self.assertEqual(vehicle.state, "delivering")

    def test_a_pad_reservation_from_the_ground_also_waits(self):
        world, vehicle, legs = self._world_and_legs()
        vehicle.state, vehicle.alt = "landed", 0.0
        world.act("drone-01", "reserve_pad", {"pad": "pad:launch", "legs": legs,
                                              "depart_after_tick": 150}, "l_1", "schedule",
                  None, 1)
        for tick in range(2, 150):
            world.tick(tick)
        self.assertEqual((vehicle.state, vehicle.alt), ("ready", 0.0))
        world.tick(150)
        self.assertEqual(vehicle.state, "approaching")

    def test_a_withdrawal_from_a_ground_flight_state_never_launches(self):
        # 리뷰 S5: landed 에서 받은 fly_route 가 delivering 이 됐다가 물리면 cruising 이 되고,
        # 땅의 cruising 은 스스로 순항 고도까지 올라갔습니다.
        world, vehicle, legs = self._world_and_legs()
        vehicle.state, vehicle.alt, vehicle.waypoints = "delivering", 0.0, [(1, 1, 55.0)]
        world.act("drone-01", "divert_ground", {"withdrawn_for": "drone-03"}, "l_2", "cargo",
                  None, 5)
        self.assertEqual(vehicle.state, "ready")
        for tick in range(6, 90):
            world.tick(tick)
        self.assertEqual(vehicle.alt, 0.0)
        # 반려도 같습니다
        vehicle.state = "cruising"
        world.act("drone-01", "decline_job", {}, "l_3", "none", None, 90)
        self.assertEqual(vehicle.state, "ready")
        # 떠 있는 기체가 물리면 제자리 대기(cruising)입니다
        vehicle.state, vehicle.alt = "delivering", 60.0
        world.act("drone-01", "divert_ground", {}, "l_4", "cargo", None, 91)
        self.assertEqual(vehicle.state, "cruising")
        world.tick(92)
        self.assertGreater(vehicle.alt, 1.0)

    def test_a_grounded_vehicle_with_nowhere_to_go_stays_on_the_ground(self):
        world, vehicle, _ = self._world_and_legs()
        vehicle.state, vehicle.alt, vehicle.waypoints = "cruising", 0.0, []
        for tick in range(1, 60):
            world.tick(tick)
        self.assertEqual(vehicle.alt, 0.0)


if __name__ == "__main__":
    unittest.main()
