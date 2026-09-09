"""The delivery cycle the screen shows: load six, deliver twice, come home, do it again.

These are the mechanics behind what the map draws. Each one here was a real bug first —
a drone that flew off with no boxes, a drone that loaded forever, a drone that declined an
order it did not have.
"""

import unittest

from sim.world import (
    AIRSPACE,
    BOX_TICKS,
    DROP_TICKS,
    LANDING_AREAS,
    LOAD_TICKS,
    PARCELS_PER_STOP,
    PARCELS_PER_TRIP,
    PICKUP_PER_STOP,
    Simulation,
    seat_of,
    to_grid,
    to_latlon,
)


def _fleet_world():
    return Simulation(seed=7).worlds["guarded"]


def _legs_to(world, vehicle, x, y):
    lat0, lon0 = to_latlon(vehicle.x, vehicle.y)
    lat1, lon1 = to_latlon(x, y)
    return [{"lat": lat0, "lon": lon0, "alt_m": 55.0}, {"lat": lat1, "lon": lon1, "alt_m": 55.0}]


def _run(world, ticks, start=1):
    for tick in range(start, start + ticks):
        world.tick(tick)
    return start + ticks


class LoadingTest(unittest.TestCase):
    def test_the_day_starts_with_every_aircraft_loading_at_its_own_seat(self):
        world = _fleet_world()
        seats = {seat_of(i) for i in range(len(world.vehicles))}
        for vehicle in world.vehicles.values():
            self.assertEqual(vehicle.state, "loading")
            self.assertEqual(vehicle.load, 0)
            self.assertIn((round(vehicle.x, 6), round(vehicle.y, 6)),
                          {(round(x, 6), round(y, 6)) for x, y in seats})
        self.assertEqual(len({(v.x, v.y) for v in world.vehicles.values()}), len(world.vehicles),
                         "두 대가 같은 자리에 있습니다")

    def test_boxes_stack_one_at_a_time_and_the_aircraft_waits_loaded(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-01"]
        seen = []
        for tick in range(1, LOAD_TICKS + 2):
            world.tick(tick)
            seen.append(vehicle.load)
        self.assertEqual(sorted(set(seen)), list(range(0, PARCELS_PER_TRIP + 1)))
        self.assertEqual(vehicle.load, PARCELS_PER_TRIP)
        self.assertEqual(vehicle.state, "ready", "다 실었으면 승인을 기다립니다")
        self.assertEqual(vehicle.alt, 0.0)

    def test_an_approval_during_loading_waits_for_the_boxes_and_the_clearance(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-01"]
        result = world.act("drone-01", "fly_route",
                           {"legs": _legs_to(world, vehicle, vehicle.job_x, vehicle.job_y)},
                           "l_1", "schedule", None, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(vehicle.state, "loading", "싣는 중에 승인이 와도 뜨지 않습니다")
        _run(world, LOAD_TICKS)
        self.assertEqual(vehicle.load, PARCELS_PER_TRIP)
        self.assertIn(vehicle.state, ("ready", "delivering"))
        _run(world, 60, LOAD_TICKS + 1)
        self.assertEqual(vehicle.state, "delivering")
        self.assertGreater(vehicle.alt, 0.0)


class UnloadingTest(unittest.TestCase):
    def _land_at_job(self, world, vehicle):
        """배달지 위에 놓고 내려앉히는 지름길. 비행 자체는 다른 시험이 봅니다."""
        vehicle.x, vehicle.y = vehicle.job_x, vehicle.job_y
        vehicle.alt = 0.5
        vehicle.state = "landing"
        vehicle.load = PARCELS_PER_TRIP
        world.tick(1)

    def test_three_boxes_come_off_then_two_are_picked_up_and_the_next_stop_is_known(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-01"]
        first_stop = vehicle.job_label
        self._land_at_job(world, vehicle)
        self.assertEqual(vehicle.state, "dropping")
        self.assertNotEqual(vehicle.job_label, first_stop,
                            "내리는 동안 다음 배달지를 알아야 합니다")
        seen = []
        for tick in range(2, DROP_TICKS + 3):
            world.tick(tick)
            seen.append(vehicle.load)
        self.assertEqual(min(seen), PARCELS_PER_TRIP - PARCELS_PER_STOP)
        self.assertEqual(len(set(seen)), PARCELS_PER_STOP + 1, "상자는 하나씩 사라져야 합니다")
        self.assertEqual(vehicle.state, "picking", "내린 자리에서 돌아갈 상자를 받습니다")
        picked = []
        for tick in range(DROP_TICKS + 3, DROP_TICKS + 3 + PICKUP_PER_STOP * BOX_TICKS + 1):
            world.tick(tick)
            picked.append(vehicle.load)
        self.assertEqual(max(picked), PARCELS_PER_TRIP - PARCELS_PER_STOP + PICKUP_PER_STOP)
        self.assertEqual(vehicle.state, "ready")
        self.assertEqual(vehicle.delivered, 1)
        self.assertEqual(vehicle.stops_left, 1)

    def test_after_the_last_stop_the_next_leg_is_the_warehouse_seat(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-02"]
        vehicle.stops_left = 1               # 마지막 정차
        self._land_at_job(world, vehicle)
        self.assertEqual(vehicle.job_label, "Warehouse")
        self.assertEqual((round(vehicle.job_x, 3), round(vehicle.job_y, 3)),
                         tuple(round(c, 3) for c in seat_of(1)))
        self.assertEqual(vehicle.stops_left, 0)

    def test_pickups_come_off_at_the_warehouse_before_the_next_load(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-02"]
        vehicle.stops_left, vehicle.load = 0, PICKUP_PER_STOP * 2
        world._send_home(vehicle)
        vehicle.x, vehicle.y = vehicle.job_x, vehicle.job_y
        vehicle.state, vehicle.alt = "landing", 0.5
        world.tick(1)
        self.assertEqual(vehicle.state, "dropping", "가져온 상자를 내립니다")
        _run(world, PICKUP_PER_STOP * 2 * BOX_TICKS + 1, 2)
        self.assertEqual(vehicle.load, 0)
        self.assertEqual(vehicle.state, "ready")
        self.assertIsNone(vehicle.job_x)

    def test_an_aircraft_with_no_stops_left_flies_home_as_returning(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-03"]
        area = LANDING_AREAS[0]
        vehicle.x, vehicle.y = to_grid(area["lat"], area["lon"])
        vehicle.state, vehicle.load, vehicle.alt, vehicle.stops_left = "ready", 2, 0.0, 0
        world._send_home(vehicle)
        world.act("drone-03", "fly_route",
                  {"legs": _legs_to(world, vehicle, vehicle.job_x, vehicle.job_y)},
                  "l_2", "schedule", None, 1)
        _run(world, 60)
        self.assertEqual(vehicle.state, "returning")

    def test_declining_with_no_stops_left_sends_it_home_instead_of_inventing_an_order(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-04"]
        vehicle.state, vehicle.load, vehicle.stops_left = "ready", 0, 0
        world._send_home(vehicle)
        world.act("drone-04", "decline_job", {}, "l_3", "none", None, 1)
        self.assertEqual(vehicle.job_label, "Warehouse",
                         "들를 곳이 없는 기체는 배달 주문을 받지 않습니다")


class HomeTest(unittest.TestCase):
    def test_touching_down_at_the_seat_means_waiting_with_nothing_to_deliver(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-01"]
        vehicle.stops_left = 0
        world._send_home(vehicle)
        vehicle.state, vehicle.load, vehicle.alt = "landing", 0, 0.5
        world.tick(1)
        self.assertEqual(vehicle.state, "ready")
        self.assertIsNone(vehicle.job_x)

    def test_every_landing_area_is_clear_and_reachable_both_ways(self):
        """착륙장은 건물에서 떨어져 있고, 이륙장에서 오가는 길이 나야 합니다.

        길이 안 나는 착륙장 하나면 거기 간 기체가 영영 '승인 대기'로 서 있습니다.
        """
        from attache.core.route import Router

        router = Router(AIRSPACE)
        seat = to_latlon(*seat_of(0))
        for area in LANDING_AREAS:
            with self.subTest(area=area["name"]):
                point = (area["lat"], area["lon"])
                self.assertIsNone(AIRSPACE.landing_breach(*point),
                                  "둘레 50m 안에 건물·금지 구역이 있습니다")
                self.assertIsNotNone(router.plan(seat, point), "이륙장에서 가는 길이 없습니다")
                self.assertIsNotNone(router.plan(point, seat), "돌아오는 길이 없습니다")

    def test_depart_from_the_seat_takes_a_new_order_and_tops_up_the_boxes(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-01"]
        vehicle.state, vehicle.load, vehicle.alt = "ready", 0, 0.0
        vehicle.job_x = vehicle.job_y = None
        world.act("drone-01", "depart", {}, "l_4", "none", None, 1)
        self.assertEqual(vehicle.state, "loading")
        self.assertIsNotNone(vehicle.job_x, "싣기만 하고 갈 곳이 없으면 영영 싣기만 합니다")
        self.assertEqual(vehicle.work_ticks, LOAD_TICKS)
        vehicle.load, vehicle.state = 3, "ready"
        world.act("drone-01", "depart", {}, "l_5", "none", None, 2)
        self.assertEqual(vehicle.work_ticks, (PARCELS_PER_TRIP - 3) * BOX_TICKS,
                         "남은 상자 위에 채웁니다")

    def test_the_battery_does_not_drain_on_the_ground_and_never_dies(self):
        world = _fleet_world()
        vehicle = world.vehicles["drone-01"]
        before = vehicle.battery
        _run(world, LOAD_TICKS + 40)
        self.assertEqual(vehicle.battery, before, "땅에서는 배터리가 안 닳습니다")
        vehicle.battery = 0.5
        vehicle.state, vehicle.alt = "cruising", 55.0
        _run(world, 5, LOAD_TICKS + 41)
        self.assertGreater(vehicle.battery, 0.0)
        self.assertNotEqual(vehicle.state, "grounded")


if __name__ == "__main__":
    unittest.main()
