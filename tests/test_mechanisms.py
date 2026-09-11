"""Each mechanism on its own, without depending on a 420-tick scenario going a certain way.

A full run is good for the structural guarantees. It is a bad place to assert that a
particular event happened, because the moment the scenario shifts the test starts failing
for reasons that have nothing to do with the code being wrong.
"""

import tempfile
import unittest

from backend.authority import AuthorityCheck
from backend.policy import PolicyBook
from backend.service import Runtime
from shared.config import Authority, Policy
from shared.models import Proposal, Verdict
from sim.world import RECALL, RECALL_TICK, Simulation


def proposal(**kwargs) -> Proposal:
    base = dict(asset_id="drone-01", action="fast_charge", cost_usd=60.0,
                blast_radius="none", rationale="배터리 12%")
    return Proposal(**{**base, **kwargs})


class RecallTest(unittest.TestCase):
    """공지가 도착한 순간부터 막느냐, 각자 확인할 때까지 기다리느냐."""

    def setUp(self):
        self.policies = PolicyBook()
        self.check = AuthorityCheck(Authority(320, 720), self.policies)
        self.recall = Policy(RECALL["id"], RECALL["reason"],
                             forbid_action=RECALL["forbid_action"],
                             applies_to=RECALL["applies_to"])
        self.asset = {"model": "dv-x500"}

    def test_before_the_recall_it_clears(self):
        self.assertIs(self.check.evaluate(proposal(), self.asset, 0).verdict, Verdict.AUTO)

    def test_the_tick_the_recall_lands_it_stops(self):
        self.policies.add(self.recall)
        banned = proposal(action=RECALL["forbid_action"])
        decision = self.check.evaluate(banned, self.asset, 0)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, RECALL["id"])
        self.assertEqual(decision.forbids, RECALL["forbid_action"])

    def test_an_agent_polling_on_its_own_has_a_window(self):
        """강제점이 없으면 공지와 이행 사이가 빕니다. 그 창이 위험의 크기입니다."""
        # 공지 시점은 sim 이 정합니다. 여기에 숫자를 박아두면 시나리오를 조정할 때
        # 시험이 조용히 다른 순간을 보게 됩니다.
        published_at, poll_every = RECALL_TICK, 25
        simulation = Simulation()
        world = simulation.worlds["direct"]
        for _ in range(published_at + 5):
            simulation.step()
        # 공지는 나갔고, 기체는 아직 다음 폴링 전입니다
        self.assertTrue(any(b["id"] == RECALL["id"] for b in simulation.bulletins()))
        vehicle = world.vehicles["drone-01"]
        vehicle.state = "cruising"
        result = world.act("drone-01", RECALL["forbid_action"], {}, None, "schedule", None,
                           simulation.tick_count)
        self.assertGreater(world.score.post_recall_violations, 0,
                           "강제점이 없으면 이 창에서 금지 행동이 실제로 나갑니다")
        del result, poll_every


class PassengerGateTest(unittest.TestCase):
    def setUp(self):
        self.check = AuthorityCheck(
            Authority(320, 720, human_required_blast=["passenger", "public"],
                      human_required_actions=["disengage_autonomy"]),
            PolicyBook(),
        )

    def test_a_free_action_still_needs_a_human_when_passengers_are_aboard(self):
        decision = self.check.evaluate(
            proposal(action="disengage_autonomy", cost_usd=0.0, blast_radius="passenger"),
            {}, 0,
        )
        self.assertIs(decision.verdict, Verdict.HUMAN)

    def test_the_actuator_records_it_as_unapproved_when_nobody_looked(self):
        world = Simulation().worlds["direct"]
        world.act("drone-01", "disengage_autonomy", {}, None, "passenger", None, 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 1)

    def test_and_not_when_someone_did(self):
        world = Simulation().worlds["guarded"]
        world.act("drone-01", "disengage_autonomy", {}, "l_1", "passenger", "관제사", 1)
        self.assertEqual(world.score.unapproved_passenger_actions, 0)
        self.assertEqual(world.score.human_approvals, 1)


class RevocationTest(unittest.TestCase):
    """거절하는 것과 이미 벌어진 일을 되돌리는 것은 다릅니다."""

    def test_a_ban_takes_back_a_resource_already_held(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

        class LocalAdapter:
            def __init__(self):
                self.sent = []

            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                self.sent.append((asset_id, action))
                return {"ok": True}

            def telemetry(self):
                return {}

        adapter = LocalAdapter()
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        runtime.locks.acquire("pad:P1", "drone-01", "p_1")

        decision = runtime.revoke_under(
            Policy("nofly-x", "응급헬기", forbid_resource="pad:P1")
        )
        self.assertIsNotNone(decision)
        self.assertIn(("drone-01", "divert_ground"), adapter.sent)
        self.assertIsNone(runtime.locks.holder("pad:P1"))
        self.assertEqual(decision.policy_hit, "nofly-x")

    def test_a_closing_zone_pulls_a_crossing_flight_back_through_a_json_adapter(self):
        """회수 명령은 HTTP 어댑터를 지나갑니다. 원장 항목 객체가 아니라 번호가 가야 합니다.

        로컬 어댑터만 쓰는 시험은 이걸 못 잡았고, 실제 스택에서는 이 한 줄이 배경 스레드를
        죽여 런타임이 옛 위치를 계속 내보냈습니다.
        """
        import json

        from shared.geo import Volume, box

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

        class JsonAdapter:
            def __init__(self):
                self.sent = []

            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                # FleetSimAdapter 가 하는 그대로 — JSON 으로 만들 수 없으면 여기서 터집니다.
                json.dumps({"asset": asset_id, "action": action, "params": params,
                            "ledger_id": ledger_id, "blast": blast, "approved_by": approved_by})
                self.sent.append((asset_id, action, ledger_id))
                return {"ok": True}

            def telemetry(self):
                return {}

        adapter = JsonAdapter()
        runtime.adapter = adapter
        runtime.committer.adapter = adapter
        runtime.telemetry = {"drone-01": {"lat": 40.7200, "lon": -73.9850, "alt_m": 55.0,
                                          "route": [{"lat": 40.7300, "lon": -73.9850,
                                                     "alt_m": 55.0}]}}
        zone = Volume("nofly-t", "시험 구역", box(40.7230, -73.9900, 40.7260, -73.9800))
        pulled = runtime.recall_flights(zone)
        self.assertEqual(len(pulled), 1)
        self.assertEqual(adapter.sent[0][:2], ("drone-01", "divert_ground"))
        self.assertIsInstance(adapter.sent[0][2], str)
        self.assertEqual(pulled[0].ledger_id, adapter.sent[0][2])

    def test_it_does_nothing_when_nobody_holds_it(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
        self.assertIsNone(
            runtime.revoke_under(Policy("nofly-y", "x", forbid_resource="pad:P9"))
        )


if __name__ == "__main__":
    unittest.main()


class PadContentionTest(unittest.TestCase):
    """착륙 패드는 한 번에 한 대입니다. 그걸 지키는 게 무엇인가.

    직결 배선에는 지킬 것이 없습니다. 두 기체가 같은 순간에 같은 패드를 고르면
    둘 다 갑니다 — 서로의 예약 의도를 볼 방법이 없어서입니다. 두 세계를 오래 돌려서
    이 순간이 우연히 오기를 기다리는 대신, 여기서 그 순간을 직접 만듭니다.
    """

    def test_without_a_lock_table_two_vehicles_take_the_same_pad(self):
        from sim.world import Simulation

        world = Simulation(lock_actuator=False).worlds["direct"]
        for asset in ("drone-01", "drone-02"):
            vehicle = world.vehicles[asset]
            vehicle.battery = 40.0
            result = world.act(asset, "reserve_pad", {"pad": "pad:launch"}, None, "schedule",
                               None, 1)
            self.assertTrue(result["ok"], "조종장치가 아무나 받습니다")
            vehicle.state = "landed"
            vehicle.assigned_pad = "pad:launch"

        world._detect_pad_conflicts(2)
        self.assertGreater(world.score.pad_conflicts, 0)

    def test_a_lock_table_hands_the_pad_to_one_of_them(self):
        from backend.locks import LockTable

        locks = LockTable(["pad:launch", "pad:launch"])
        self.assertTrue(locks.acquire("pad:launch", "drone-01", "p1"))
        self.assertFalse(locks.acquire("pad:launch", "drone-02", "p2"))
        self.assertEqual(locks.holder("pad:launch").asset_id, "drone-01")


class RoundResetTest(unittest.TestCase):
    """판이 바뀌면 한 판짜리 상태도 새로 시작해야 합니다.

    화면을 켜두면 시뮬레이터가 알아서 다음 판을 시작합니다. 그때 런타임의 예산이
    누적으로 남아 있으면 두 번째 판부터는 한도가 다 차서 아무것도 승인되지 않습니다.
    가드 쪽만 멈추고 직결 쪽은 계속 날아서, 데모가 정반대를 말하게 됩니다.
    """

    def _runtime(self):
        import tempfile

        from backend.service import Runtime

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            return Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)

    def test_a_new_round_hands_the_budget_back(self):
        from shared.models import Proposal

        runtime = self._runtime()
        runtime._follow_round(0)
        # 데모 설정은 한도가 없습니다(null = 돈을 판정하지 않음). 이 시험은 한도가 있을 때의 판
        # 초기화입니다.
        runtime.authority.authority.fleet_usd = 100.0
        limit = runtime.authority.authority.fleet_usd
        while runtime.authority.fleet_spend < limit:
            runtime.authority.record_spend(
                Proposal(asset_id="drone-01", action="charge", cost_usd=22.0,
                         blast_radius="none", rationale="")
            )
        self.assertGreaterEqual(runtime.authority.fleet_spend, limit)

        runtime._follow_round(1)
        self.assertEqual(runtime.authority.fleet_spend, 0.0)
        self.assertEqual(runtime.authority.asset_spend("drone-01"), 0.0)

    def test_a_new_round_lets_go_of_pads_and_stale_bans(self):
        from shared.config import Policy

        runtime = self._runtime()
        runtime.telemetry = {"drone-01": {}}
        runtime._follow_round(0)
        runtime.locks.acquire("pad:launch", "drone-01", "p_1")
        runtime.policies.add(Policy("nofly-x", "지난 판의 공지", forbid_resource="pad:launch"))

        runtime._follow_round(1)
        self.assertIsNone(runtime.locks.holder("pad:launch"),
                          "기체가 새로 세워졌는데 지난 판의 예약이 남으면 아무도 못 씁니다")
        self.assertEqual(runtime.policies.all(), [])

    def test_the_same_round_is_not_a_reset(self):
        from shared.models import Proposal

        runtime = self._runtime()
        runtime._follow_round(3)
        runtime.authority.record_spend(
            Proposal(asset_id="drone-01", action="charge", cost_usd=22.0,
                     blast_radius="none", rationale="")
        )
        runtime._follow_round(3)
        self.assertEqual(runtime.authority.fleet_spend, 22.0)


class CountingAdapter:
    """실행된 경로를 셉니다. 런타임의 보장은 여기 닿는 것으로 재야 합니다."""

    def __init__(self):
        self.routes = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        if params.get("legs"):
            self.routes.append((asset_id, action, params["legs"]))
        return {"ok": True}

    def telemetry(self):
        return {}


def _runtime_with(volumes):
    from shared.geo import Volume

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
    adapter = CountingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    for raw in volumes:
        runtime.airspace.add(raw if isinstance(raw, Volume) else Volume.from_dict(raw))
    return runtime, adapter


class RouteFormTest(unittest.TestCase):
    """판정 이전의 양식. 유한한 숫자라고 다 경로가 아닙니다.

    음수 고도는 모든 구역의 '아래' 로 빠져 건물 한가운데를 지나는 경로가 통과했고, 1e300 짜리
    좌표는 색인 격자 1e600 칸을 돌며 판정이 영영 안 끝났습니다. 배송된 초안기는 둘 다
    자기 검사에서 거르지만, 런타임의 보장은 초안기가 없어도 같아야 합니다.
    """

    @classmethod
    def setUpClass(cls):
        from sim.world import seat_of, to_latlon

        cls.volumes = Simulation(seed=7).worlds["guarded"].snapshot(0, volumes=True)["volumes"]
        cls.start = tuple(round(v, 6) for v in to_latlon(*seat_of(0)))
        # 출발점에서 가장 가까운, 옥상이 100m 를 넘는 건물. 그 한가운데를 지납니다.
        tall = [v for v in cls.volumes if v["id"].startswith("bldg-")
                and (v.get("ceiling_m") or 0) >= 100]
        nearest = min(tall, key=lambda v: (v["polygon"][0][0] - cls.start[0]) ** 2
                      + (v["polygon"][0][1] - cls.start[1]) ** 2)
        cls.centre = (sum(p[0] for p in nearest["polygon"]) / len(nearest["polygon"]),
                      sum(p[1] for p in nearest["polygon"]) / len(nearest["polygon"]))

    def setUp(self):
        self.runtime, self.adapter = _runtime_with(self.volumes)

    def through_building(self, alt_m):
        return [{"lat": self.start[0], "lon": self.start[1], "alt_m": alt_m},
                {"lat": self.centre[0], "lon": self.centre[1], "alt_m": alt_m},
                {"lat": self.start[0] + 0.001, "lon": self.start[1], "alt_m": alt_m}]

    def file_route(self, legs):
        return self.runtime.file(proposal(action="fly_route", cost_usd=40.0, blast_radius="cargo",
                                          params={"legs": legs}).to_dict())

    def test_a_route_below_ground_through_a_building_is_refused(self):
        for alt_m in (-1.0, -0.001, -1e9):
            with self.subTest(alt_m=alt_m):
                decision = self.file_route(self.through_building(alt_m))
                self.assertIs(decision.verdict, Verdict.DENIED)
                self.assertEqual(decision.code, "airspace")
                self.assertIn("양식", decision.reason)
        self.assertEqual(self.adapter.routes, [])
        # 같은 길을 60m 로 내면 양식이 아니라 판정이 막습니다 — 건물이 정말 거기 있다는 뜻입니다.
        decision = self.file_route(self.through_building(60.0))
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertNotIn("양식", decision.reason)

    def test_the_judge_itself_treats_below_ground_as_ground(self):
        """양식 검사를 지나쳐도(다른 진입점) 판정은 -1m 를 건물 안으로 봅니다."""
        from shared.geo import first_breach

        found = first_breach(self.runtime.airspace, self.through_building(-1.0))
        self.assertIsNotNone(found)
        self.assertTrue(found[1].id.startswith("bldg-"))

    def test_coordinates_off_the_planet_are_refused_at_once(self):
        import time

        legs = [{"lat": 1e300, "lon": 1e300, "alt_m": 60},
                {"lat": -1e300, "lon": 1e300, "alt_m": 60}]
        started = time.monotonic()
        decision = self.file_route(legs)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertIn("양식", decision.reason)

    def test_a_leg_longer_than_the_runtime_maximum_is_refused(self):
        from backend.service import MAX_LEG_M

        legs = [{"lat": 40.70, "lon": -73.97, "alt_m": 60},
                {"lat": 40.70 + (MAX_LEG_M + 1000) / 110_570.0, "lon": -73.97, "alt_m": 60}]
        decision = self.file_route(legs)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertIn("너무 김", decision.reason)

    def test_the_index_refuses_to_walk_a_planet_sized_leg(self):
        """마지막 방어선. 양식 검사 없이 판정 함수에 바로 넣어도 돌지 않고 거부합니다."""
        from shared.geo import Airspace, Volume, box, first_breach

        airspace = Airspace([Volume("nofly", "x", box(40.72, -73.99, 40.73, -73.98))])
        with self.assertRaises(ValueError):
            first_breach(airspace, [{"lat": -90, "lon": -180, "alt_m": 60},
                                    {"lat": 90, "lon": 180, "alt_m": 60}])


class RejudgeBeforeCommitTest(unittest.TestCase):
    """판정은 접수 때 한 번이 아니라 실행 직전에 다시 합니다.

    사람 승인을 기다리거나 자원 줄에 서 있는 동안 구역이 닫히면, 그 경로는 옛 공역으로 판정된
    것입니다. 실행 시점에 다시 보지 않으면 승인·배정이 닫힌 구역으로 기체를 내보냅니다.
    """

    def setUp(self):
        from sim.world import ZONE

        self.zone = {**ZONE, "published_tick": 560, "until_tick": 900}
        # 구역 말고는 아무것도 없는 공역. 구역 한가운데를 지나는 길이 닫히기 전에는 통과합니다.
        self.runtime, self.adapter = _runtime_with([])
        centre = (sum(p[0] for p in ZONE["polygon"]) / 4, sum(p[1] for p in ZONE["polygon"]) / 4)
        self.legs = [{"lat": 40.7100, "lon": -73.9855, "alt_m": 60},
                     {"lat": centre[0], "lon": centre[1], "alt_m": 60},
                     {"lat": 40.7350, "lon": -73.9855, "alt_m": 60}]

    def test_a_human_approval_does_not_revive_a_route_the_zone_has_since_closed(self):
        filed = self.runtime.file(proposal(action="fly_route", cost_usd=40.0,
                                           blast_radius="passenger",
                                           params={"legs": self.legs}).to_dict())
        self.assertIs(filed.verdict, Verdict.HUMAN)
        self.runtime.absorb([self.zone])
        decision = self.runtime.approve(filed.proposal_id, "관제사", allow=True)
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.code, "airspace")
        self.assertEqual(decision.forbids, self.zone["id"])
        self.assertFalse(decision.committed)
        self.assertEqual(self.adapter.routes, [])

    def test_the_arbiter_does_not_hand_a_pad_to_a_route_the_zone_has_since_closed(self):
        filed = self.runtime.file(proposal(action="reserve_pad", cost_usd=18.0,
                                           blast_radius="schedule", resource="pad:launch",
                                           params={"legs": self.legs}).to_dict())
        self.assertIs(filed.verdict, Verdict.QUEUED)
        self.runtime.absorb([self.zone])
        self.runtime._settle_contended()
        self.assertIs(filed.verdict, Verdict.DENIED)
        self.assertEqual(filed.code, "airspace")
        self.assertEqual(self.adapter.routes, [])
        self.assertIsNone(self.runtime.locks.holder("pad:launch"))

    def test_without_a_zone_change_the_same_paths_still_commit(self):
        filed = self.runtime.file(proposal(action="fly_route", cost_usd=40.0,
                                           blast_radius="passenger",
                                           params={"legs": self.legs}).to_dict())
        decision = self.runtime.approve(filed.proposal_id, "관제사", allow=True)
        self.assertTrue(decision.committed)
        # 두 번째 기체는 같은 시각에 옆으로 300m 떨어진 길을 냅니다. 같은 길을 같은 시각에 내면
        # 그건 공역이 아니라 교차(traffic) 거절이고, 그건 test_intents 가 봅니다.
        beside = [{**leg, "lon": leg["lon"] + 300 / 84_400.0} for leg in self.legs]
        queued = self.runtime.file(proposal(asset_id="drone-02", action="reserve_pad",
                                            cost_usd=18.0, blast_radius="schedule",
                                            resource="pad:launch",
                                            params={"legs": beside}).to_dict())
        self.runtime._settle_contended()
        self.assertTrue(queued.committed, queued.reason)
        self.assertEqual(len(self.adapter.routes), 2)
