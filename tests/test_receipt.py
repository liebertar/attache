"""What makes the runtime binding rather than advisory.

Network isolation and separate images stop an agent that plays by the rules. The thing
that stops one that does not is the actuator refusing to move without an authorization
receipt. The ledger id is that receipt: the runtime writes the entry before it acts, and
the entry id travels with the command.
"""

import unittest

from sim.world import Simulation


class ReceiptTest(unittest.TestCase):
    def test_an_open_actuator_obeys_anyone(self):
        world = Simulation(lock_actuator=False).worlds["direct"]
        result = world.act("drone-01", "reserve_pad", {"pad": "bay:A"}, None, "schedule",
                           None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.unrecorded_actions, 1)

    def test_a_locked_actuator_refuses_a_command_with_no_receipt(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("drone-01", "reserve_pad", {"pad": "bay:A"}, None, "schedule",
                           None, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 1)
        self.assertEqual(world.vehicles["drone-01"].assigned_pad, None)

    def test_a_locked_actuator_still_obeys_the_runtime(self):
        world = Simulation(lock_actuator=True).worlds["direct"]
        result = world.act("drone-01", "reserve_pad", {"pad": "bay:A"}, "l_abc123",
                           "schedule", None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(world.score.refused_without_receipt, 0)
        self.assertEqual(world.score.unrecorded_actions, 0)

    def test_locking_costs_nothing_when_everyone_already_goes_through(self):
        """런타임을 거치는 쪽은 잠그든 안 잠그든 결과가 같습니다."""
        for locked in (False, True):
            world = Simulation(lock_actuator=locked).worlds["guarded"]
            result = world.act("drone-01", "reserve_pad", {"pad": "bay:A"}, "l_1",
                               "schedule", None, 1)
            self.assertTrue(result["ok"], f"locked={locked}")


if __name__ == "__main__":
    unittest.main()


class LedgerTruthTest(unittest.TestCase):
    """원장이 거짓을 적으면 원장이 아닙니다."""

    def test_the_closing_entry_reports_what_actually_happened(self):
        import tempfile

        from attache.core.models import Decision, Proposal, Verdict
        from attache.runtime.authority import AuthorityCheck
        from attache.runtime.commit import Committer
        from attache.runtime.ledger import Ledger
        from attache.runtime.locks import LockTable
        from attache.runtime.policy import PolicyBook
        from attache.core.config import Authority

        simulation = Simulation()
        world = simulation.worlds["guarded"]

        class LocalAdapter:
            def execute(self, asset_id, action, params, ledger_id,
                        blast="none", approved_by=None):
                return world.act(asset_id, action, params, ledger_id, blast, approved_by, 1)

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            ledger = Ledger(handle.name)
        authority = AuthorityCheck(Authority(200, 500), PolicyBook())
        committer = Committer(LocalAdapter(), LockTable(["bay:A"]), ledger, authority)

        proposal = Proposal(asset_id="drone-01", action="reserve_pad", cost_usd=28.0,
                            blast_radius="schedule", rationale="", resource="bay:A",
                            params={"pad": "bay:A"})
        committer.commit(proposal, Decision(proposal.id, Verdict.AUTO, "한도 안"))

        closed = [e for e in ledger.tail(10) if e["outcome"] != "pending"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["outcome"], "done")
        self.assertTrue(closed[0]["decision"]["committed"],
                        "결과는 done 인데 실행 안 했다고 적혀 있습니다")
        self.assertIsNotNone(closed[0]["decision"]["ledger_id"])


class AirspaceTest(unittest.TestCase):
    """실제 FAA 격자를 읽고 쓰는지."""

    def test_real_faa_cells_are_loaded(self):
        from sim.world import AIRSPACE

        volumes = AIRSPACE.all()
        self.assertGreater(len(volumes), 40, "FAA 격자를 못 읽었습니다")
        self.assertTrue(any(v.rule == "forbidden" for v in volumes))
        self.assertTrue(any(v.rule == "ceiling" for v in volumes))
        self.assertTrue(any(v.source.startswith("FAA") for v in volumes))
        # 공역에는 이제 규정과 물체가 같이 있습니다. 어느 쪽이든 출처는 있어야 합니다.
        self.assertTrue(all(v.source for v in volumes), "출처 없는 구역이 있습니다")

    def test_a_building_is_a_volume_you_may_not_be_inside(self):
        """건물은 새 개념이 아닙니다. 땅에서 옥상까지 금지이고 그 위는 열린 구역입니다.

        그래서 판정 코드가 한 줄도 안 늘어납니다. 같은 first_breach 가 답합니다.
        """
        from attache.core.geo import first_breach
        from sim.world import AIRSPACE, BUILDINGS

        if not BUILDINGS:
            self.skipTest("건물 데이터가 없습니다 (scripts/fetch_buildings.py)")
        tall = max((v for v in AIRSPACE.all() if v.id.startswith("bldg-")),
                   key=lambda v: v.ceiling_m)
        inside = tall.polygon[0]
        below = [{"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m - 10},
                 {"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m - 10}]
        above = [{"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m + 10},
                 {"lat": inside[0], "lon": inside[1], "alt_m": tall.ceiling_m + 10}]
        self.assertIsNotNone(first_breach(AIRSPACE, below), "건물을 관통하는데 통과입니다")
        self.assertEqual(tall.rule, "forbidden")
        self.assertIsNotNone(tall.ceiling_m, "옥상이 없으면 위로도 못 갑니다")
        del above   # 옥상 위는 FAA 천장이 따로 보므로 여기서는 건물만 봅니다

    def test_a_zero_foot_cell_becomes_a_ban_not_a_ceiling(self):
        """천장 0ft 는 '낮게 날아라'가 아니라 '허가 없이는 못 난다'입니다."""
        from sim.world import AIRSPACE

        zeros = [v for v in AIRSPACE.all() if v.tags.get("ceiling_ft") == 0]
        self.assertTrue(zeros)
        for volume in zeros:
            self.assertEqual(volume.rule, "forbidden")
            self.assertIsNone(volume.ceiling_m)

    def test_the_runtime_refuses_a_route_through_restricted_airspace(self):
        import tempfile

        from attache.core.geo import Volume
        from attache.core.models import Verdict
        from attache.runtime.service import Runtime
        from sim.world import Simulation as Sim

        simulation = Sim()
        snapshot = simulation.worlds["guarded"].snapshot(0, volumes=True)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            runtime = Runtime("configs/fleet.yaml", "http://unused", handle.name, 0.0)
        for raw in snapshot["volumes"]:
            runtime.airspace.add(Volume.from_dict(raw))

        forbidden = next(v for v in runtime.airspace.all() if v.rule == "forbidden")
        centre = (
            sum(p[0] for p in forbidden.polygon) / len(forbidden.polygon),
            sum(p[1] for p in forbidden.polygon) / len(forbidden.polygon),
        )
        runtime.pad_coords = {"pad:X": centre}
        runtime.telemetry = {"drone-01": {"lat": centre[0] + 0.02, "lon": centre[1] + 0.02}}

        # 운영사가 그린 경로가 금지 구역을 지나갑니다
        decision = runtime.file({
            "asset_id": "drone-01", "action": "reserve_pad", "resource": "pad:X",
            "params": {"pad": "pad:X", "legs": [
                {"lat": centre[0] + 0.02, "lon": centre[1] + 0.02, "alt_m": 60.0},
                {"lat": centre[0], "lon": centre[1], "alt_m": 60.0},
            ]},
            "cost_usd": 28.0, "blast_radius": "schedule", "rationale": "배터리 낮음",
        })
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "airspace")
        self.assertIn("구간이 규정을 어깁니다", decision.reason)


class RouterAgreesWithTheJudgeTest(unittest.TestCase):
    """계획기와 런타임이 같은 판정을 해야 합니다.

    격자점만 보면 대각선 한 칸이 폴리곤 귀퉁이를 관통해도 양 끝이 바깥이라 통과로 보입니다.
    판정자는 선분을 보므로 위반이라고 합니다. 그러면 A* 가 내놓는 경로가 전부 마지막
    검사에서 떨어지고, 아무것도 승인되지 않은 채 전량 decline_job 이 됩니다.
    """

    def test_a_diagonal_step_that_clips_a_corner_is_not_a_free_step(self):
        from attache.core.geo import Airspace, Volume, first_breach
        from attache.core.route import Router

        airspace = Airspace()
        airspace.add(Volume(
            id="corner", name="0ft", rule="forbidden",
            # FAA UASFM 의 실제 KTEB 0ft 칸 경계입니다.
            polygon=[(40.791673474, -74.000005947), (40.800006809, -74.000005947),
                     (40.800006809, -73.991672612), (40.791673474, -73.991672612)],
        ))
        router = Router(airspace)
        # 하나는 구역 동쪽 바로 밖, 하나는 남쪽 바로 밖. 잇는 선분은 귀퉁이를 지납니다.
        outside_east = router._node(40.7934, -73.9908)
        outside_south = router._node(40.7916, -73.9926)
        self.assertFalse(router._blocked(outside_east))
        self.assertFalse(router._blocked(outside_south))
        self.assertTrue(router._crosses(outside_east, outside_south))

        legs = router._to_legs([outside_east, outside_south])
        breach = first_breach(airspace, [leg.to_dict() for leg in legs])
        self.assertIsNotNone(breach, "판정자는 위반이라고 하는데 계획기가 통과시키면 안 됩니다")

    def test_every_route_the_planner_hands_over_survives_the_judge(self):
        from attache.agent.planner import OperatorPlanner
        from attache.core.geo import first_breach
        from sim.world import Simulation as Sim

        planner = OperatorPlanner()
        planner.load(Sim().worlds["guarded"].snapshot(0, volumes=True)["volumes"])

        bay = (40.7019, -73.9721)
        starts = [(40.7969, -73.9704), (40.7580, -73.9855), (40.7280, -73.9955),
                  (40.7750, -73.9600), (40.7100, -74.0100)]
        drawn = 0
        for start in starts:
            legs = planner.draw(start, bay)
            if legs is None:
                continue   # 규정상 길이 없는 자리는 있습니다. 없다고 말하는 것도 답입니다
            drawn += 1
            self.assertIsNone(first_breach(planner.airspace, legs),
                              f"{start} 에서 그린 경로를 런타임이 거절합니다")
        self.assertTrue(drawn, "실제 공역에서 단 하나도 못 그리면 계획기가 고장난 것입니다")


class WeavingBetweenBuildingsTest(unittest.TestCase):
    """건물이 들어오면 길찾기가 다른 문제가 됩니다.

    FAA 격자만 있을 때는 한 칸이 900m 라 200m 격자로 충분했습니다. 건물은 30~60m 라
    간선 하나가 200m 면 맨해튼에서는 거의 모든 간선이 무언가를 스칩니다. 그러면
    경로가 아니라 '경로 없음'만 나옵니다.
    """

    def _router(self):
        from attache.core.route import Router
        from sim.world import AIRSPACE, BUILDINGS

        if not BUILDINGS:
            self.skipTest("건물 데이터가 없습니다 (scripts/fetch_buildings.py)")
        return Router(AIRSPACE)

    def test_a_route_through_the_city_exists_and_bends_around_things(self):
        from attache.core.geo import first_breach

        router = self._router()
        route = router.plan((40.7019, -73.9721), (40.7250, -73.9900))
        self.assertIsNotNone(route, "도심을 가로지르는 경로가 하나도 안 나옵니다")
        legs = [leg.to_dict() for leg in route.legs]
        self.assertIsNone(first_breach(router.airspace, legs),
                          "계획기가 스스로 어기는 경로를 내놨습니다")
        self.assertGreater(len(legs), 5, "직선 하나면 아무것도 피하지 않은 것입니다")
        self.assertTrue(all(leg["alt_m"] <= router.cruise_alt_m + 0.1 for leg in legs),
                        "순항 고도보다 높이 날면 건물을 볼 일이 없습니다")

    def test_the_route_starts_where_you_are_and_ends_where_you_are_going(self):
        """격자점에서 끝나면 남은 100m 를 아무도 판정한 적 없는 채로 날게 됩니다."""
        router = self._router()
        start, goal = (40.7019, -73.9721), (40.7250, -73.9900)
        route = router.plan(start, goal)
        self.assertIsNotNone(route)
        self.assertAlmostEqual(route.legs[0].lat, start[0], places=5)
        self.assertAlmostEqual(route.legs[0].lon, start[1], places=5)
        self.assertAlmostEqual(route.legs[-1].lat, goal[0], places=5)
        self.assertAlmostEqual(route.legs[-1].lon, goal[1], places=5)

    def test_a_building_taller_than_the_cruise_altitude_is_not_a_shortcut(self):
        from attache.core.geo import first_breach
        from sim.world import AIRSPACE

        router = self._router()
        tall = max((v for v in AIRSPACE.all()
                    if v.id.startswith("bldg-") and v.ceiling_m > router.cruise_alt_m),
                   key=lambda v: v.ceiling_m)
        centre = (sum(p[0] for p in tall.polygon) / len(tall.polygon),
                  sum(p[1] for p in tall.polygon) / len(tall.polygon))
        through = [{"lat": centre[0] - 0.004, "lon": centre[1], "alt_m": router.cruise_alt_m},
                   {"lat": centre[0] + 0.004, "lon": centre[1], "alt_m": router.cruise_alt_m}]
        self.assertIsNotNone(first_breach(AIRSPACE, through),
                             "건물 한복판을 지나는 직선이 통과로 나옵니다")
