"""Dispatch is the operator's business, and changing the dispatcher must not move the world.

RuleDispatcher is the rule this world always used, lifted out of World._assign_job. This file
keeps a verbatim copy of that old code beside it and plays the same vehicles through both, so
"unchanged for seed 7" is checked rather than asserted in a comment.

CuOptDispatcher speaks the REST API a cuOpt server actually speaks: POST /cuopt/request
answers {"reqId"} straight away, and GET /cuopt/solution/{id} answers {"reqId"} again until
the solve is done, then carries the solution. There is no GPU on this machine — cuOpt runs on
a Nebius AI Cloud instance — so the server here is a fake that answers in that shape. What is
tested is our half of the contract: what we send, what we make of the answer, and that every
way it can go wrong ends with the rule choosing that stop and the world carrying on.
"""

import http.server
import json
import math
import threading
import time
import unittest
from unittest import mock

from sim import dispatch as dispatch_module
from sim.dispatch import CuOptDispatcher, RuleDispatcher, build_dispatcher, dispatcher_for
from sim.world import LANDING_AREAS, PARCELS_PER_STOP, STOPS_PER_TRIP, Simulation, to_grid


def old_assign_job(world, vehicle):
    """World._assign_job 의 옛 본문 그대로. 마지막 두 줄(배달지 대입)만 정차 반환으로
    바꿨습니다."""
    taken = {other.job_label for other in world.vehicles.values()
             if other is not vehicle and other.job_label}
    pool = [area for area in LANDING_AREAS
            if area["name"] != vehicle.job_label and area["name"] not in taken]
    if not pool:
        pool = [area for area in LANDING_AREAS if area["name"] != vehicle.job_label]
    if not pool:
        return None
    if vehicle.stops_left < STOPS_PER_TRIP and vehicle.job_x is not None:
        from sim.world import to_latlon
        here_lat, here_lon = to_latlon(vehicle.job_x, vehicle.job_y)
        pool = sorted(pool, key=lambda a: math.hypot((a["lat"] - here_lat) * 110_570,
                                                     (a["lon"] - here_lon) * 84_400))[:6]
    return world._rng.choice(pool)


def _drive(world, pick, rounds: int = 80) -> list:
    """정차를 뽑고 그대로 세계에 반영하며 rounds 번. 두 세계가 같은 상태를 지나야
    비교가 뜻이 있습니다."""
    names = []
    vehicles = list(world.vehicles.values())
    for step in range(rounds):
        vehicle = vehicles[step % len(vehicles)]
        vehicle.stops_left = (step % STOPS_PER_TRIP) + 1
        area = pick(world, vehicle)
        names.append(None if area is None else area["name"])
        if area is not None:
            vehicle.job_label = area["name"]
            vehicle.job_x, vehicle.job_y = to_grid(area["lat"], area["lon"])
    return names


class RuleDispatcherIsTheOldCodeTest(unittest.TestCase):
    def test_seed_7_hands_out_exactly_the_same_stops_as_before(self):
        before = Simulation(seed=7).worlds["guarded"]
        after = Simulation(seed=7).worlds["guarded"]
        dispatcher = RuleDispatcher()
        self.assertEqual(_drive(before, old_assign_job),
                         _drive(after, dispatcher.next_stop))

    def test_the_world_still_opens_on_the_scripted_first_stops(self):
        world = Simulation(seed=7).worlds["guarded"]
        self.assertEqual(world.vehicles["drone-01"].job_label, "Central Park North 110th")
        self.assertEqual(world.vehicles["drone-03"].job_label, "Morningside Park")
        for vehicle in world.vehicles.values():
            self.assertIsNotNone(vehicle.job_x)

    def test_no_open_landing_area_means_no_job(self):
        world = Simulation(seed=7).worlds["guarded"]
        with mock.patch.object(dispatch_module, "LANDING_AREAS", []):
            self.assertIsNone(RuleDispatcher().next_stop(world, world.vehicles["drone-01"]))


# ---------- 가짜 cuOpt 서버 ----------


def _solution_for(body: dict, keys: str = "ids") -> dict:
    """cuOpt 모양의 답. 보낸 과제를 기체마다 번갈아 나눠 줍니다."""
    ids = body["fleet_data"]["vehicle_ids"]
    tasks = body["task_data"]["task_ids"]
    routes = {}
    for index, vehicle_id in enumerate(ids):
        mine = tasks[index::len(ids)]
        routes[vehicle_id if keys == "ids" else str(index)] = {
            "task_id": ["Depot"] + mine + ["Depot"],
            "type": ["Depot"] + ["Delivery"] * len(mine) + ["Depot"],
            "arrival_stamp": list(range(len(mine) + 2)),
            "route": list(range(len(mine) + 2)),
        }
    return {"response": {"solver_response": {
        "status": 0, "num_vehicles": len(ids), "solution_cost": 1.0,
        "vehicle_data": routes, "dropped_tasks": {"task_id": [], "task_index": []}}},
        "reqId": "req-1"}


class FakeCuOpt(http.server.BaseHTTPRequestHandler):
    posts: list = []
    gets: list = []
    keys = "ids"
    post_status = 200
    polls_pending = 1
    mangle = None            # 답을 망가뜨리는 함수(시험마다)
    trickle_s = 0.0          # 0 이 아니면 답 본문을 한 바이트씩 이만큼 간격으로 흘립니다

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        type(self).posts.append({"path": self.path, "body": body})
        if type(self).post_status != 200:
            self.send_error(type(self).post_status)
            return
        self._json({"reqId": "req-1"})

    def do_GET(self):
        type(self).gets.append(self.path)
        if len(type(self).gets) <= type(self).polls_pending:
            self._json({"reqId": "req-1"})      # 아직 푸는 중입니다
            return
        answer = _solution_for(type(self).posts[-1]["body"], type(self).keys)
        if type(self).mangle is not None:
            answer = type(self).mangle(answer)
        self._json(answer)

    def _json(self, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not type(self).trickle_s:
            self.wfile.write(data)
            return
        # 읽기 한 번마다 새로 재는 timeout 은 이런 답을 끊지 못합니다.
        for index in range(len(data)):
            time.sleep(type(self).trickle_s)
            try:
                self.wfile.write(data[index:index + 1])
                self.wfile.flush()
            except OSError:
                return

    def log_message(self, *args):
        pass


class CuOptDispatchTest(unittest.TestCase):
    def setUp(self):
        FakeCuOpt.posts, FakeCuOpt.gets = [], []
        FakeCuOpt.keys, FakeCuOpt.post_status = "ids", 200
        FakeCuOpt.polls_pending, FakeCuOpt.mangle, FakeCuOpt.trickle_s = 1, None, 0.0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCuOpt)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.world = Simulation(seed=7).worlds["guarded"]
        for vehicle in self.world.vehicles.values():   # 주문 없는 기단에서 시작합니다
            vehicle.job_label, vehicle.job_x, vehicle.job_y = "", None, None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _dispatcher(self, **kwargs):
        return CuOptDispatcher(self.url, timeout_s=5.0, time_limit_s=0.1, **kwargs)

    def test_it_posts_the_fleet_and_the_orders_in_cuopt_shape(self):
        dispatcher = self._dispatcher()
        area = dispatcher.next_stop(self.world, self.world.vehicles["drone-01"])
        self.assertIn(area["name"], {a["name"] for a in LANDING_AREAS})
        self.assertEqual(FakeCuOpt.posts[0]["path"], "/cuopt/request")
        body = FakeCuOpt.posts[0]["body"]
        self.assertEqual(sorted(body), ["cost_matrix_data", "fleet_data", "solver_config",
                                        "task_data", "travel_time_matrix_data"])
        fleet, tasks = body["fleet_data"], body["task_data"]
        self.assertEqual(fleet["vehicle_ids"], sorted(self.world.vehicles))
        # 한 기체의 적재량 = 이번에 들를 곳 수 × 한 곳에 내리는 상자 수. 짐이 딱 맞아야 모든 주문이
        # 배달되고, 남으면 솔버가 한 기체에 몰아줍니다.
        self.assertEqual(fleet["capacities"],
                         [[STOPS_PER_TRIP * PARCELS_PER_STOP] * len(self.world.vehicles)])
        self.assertTrue(all(pair[1] == 0 for pair in fleet["vehicle_locations"]),
                        "한 바퀴는 창고(0번 자리)에서 끝납니다")
        self.assertEqual(len(tasks["task_ids"]), STOPS_PER_TRIP * len(self.world.vehicles))
        self.assertEqual(tasks["demand"], [[PARCELS_PER_STOP] * len(tasks["task_ids"])])
        matrix = body["cost_matrix_data"]["data"]["0"]
        self.assertTrue(all(len(row) == len(matrix) for row in matrix))
        self.assertEqual(matrix[0][0], 0)
        self.assertEqual(body["solver_config"]["time_limit"], 0.1)

    def test_it_polls_until_the_solution_appears_and_then_serves_the_queue(self):
        FakeCuOpt.polls_pending = 2
        dispatcher = self._dispatcher()
        drone = self.world.vehicles["drone-01"]
        first = dispatcher.next_stop(self.world, drone)
        drone.job_label = first["name"]
        self.assertGreaterEqual(len(FakeCuOpt.gets), 3)
        self.assertTrue(FakeCuOpt.gets[0].startswith("/cuopt/solution/req-1"))
        # 두 번째 정차는 이미 받아 둔 계획에서 나옵니다 — 다시 풀지 않습니다.
        second = dispatcher.next_stop(self.world, drone)
        self.assertEqual(len(FakeCuOpt.posts), 1)
        self.assertNotEqual(second["name"], first["name"])
        self.assertEqual(dispatcher.stats["solved"], 1)
        self.assertEqual(dispatcher.stats["served"], 2)
        self.assertEqual(dispatcher.stats["fell_back"], 0)

    def test_a_solution_keyed_by_vehicle_index_is_read_too(self):
        FakeCuOpt.keys = "index"
        dispatcher = self._dispatcher()
        area = dispatcher.next_stop(self.world, self.world.vehicles["drone-02"])
        self.assertIsNotNone(area)
        self.assertEqual(dispatcher.stats["fell_back"], 0)

    def test_the_plan_covers_the_whole_fleet_so_nobody_asks_twice(self):
        dispatcher = self._dispatcher()
        for vehicle in self.world.vehicles.values():
            area = dispatcher.next_stop(self.world, vehicle)
            vehicle.job_label = area["name"]
        self.assertEqual(len(FakeCuOpt.posts), 1, "기단 전체를 한 번에 짭니다")
        labels = [vehicle.job_label for vehicle in self.world.vehicles.values()]
        self.assertEqual(len(set(labels)), len(labels), "한 착륙장에 두 대를 보내지 않습니다")


class CuOptFallsBackTest(unittest.TestCase):
    """cuOpt 가 어떻게 어긋나든 그 정차는 규칙이 정하고 세계는 계속 돕니다."""

    def setUp(self):
        FakeCuOpt.posts, FakeCuOpt.gets = [], []
        FakeCuOpt.keys, FakeCuOpt.post_status = "ids", 200
        FakeCuOpt.polls_pending, FakeCuOpt.mangle, FakeCuOpt.trickle_s = 1, None, 0.0
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCuOpt)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.world = Simulation(seed=7).worlds["guarded"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _falls_back(self, dispatcher, why: str):
        drone = self.world.vehicles["drone-01"]
        area = dispatcher.next_stop(self.world, drone)
        self.assertIsNotNone(area, why)
        self.assertIn(area["name"], {a["name"] for a in LANDING_AREAS})
        self.assertEqual(dispatcher.stats["fell_back"], 1, why)
        self.assertEqual(dispatcher.stats["solved"], 0, why)

    def test_a_server_that_is_not_there(self):
        dispatcher = CuOptDispatcher("http://127.0.0.1:9", timeout_s=1.0)
        self._falls_back(dispatcher, "닿지 않는 서버")
        self.assertIn("cuopt/request", dispatcher.stats["last_error"])

    def test_a_server_that_answers_500(self):
        FakeCuOpt.post_status = 500
        self._falls_back(CuOptDispatcher(self.url, timeout_s=1.0), "500")

    def test_a_solve_that_never_finishes_inside_the_budget(self):
        FakeCuOpt.polls_pending = 10_000
        dispatcher = CuOptDispatcher(self.url, timeout_s=0.5)
        self._falls_back(dispatcher, "시간 안에 안 풀림")
        self.assertIn("did not answer", dispatcher.stats["last_error"])

    def test_an_answer_trickled_a_byte_at_a_time_cannot_hold_the_world(self):
        """urllib 의 timeout 은 읽기 한 번마다라서, 한 바이트씩 흘리는 답은 예산의 몇 배를
        붙잡았습니다(실측: 2초 예산에 21~30초). 세계 스레드는 예산만큼만 기다리고 규칙으로
        갑니다."""
        FakeCuOpt.trickle_s = 0.2
        dispatcher = CuOptDispatcher(self.url, timeout_s=0.5)
        started = time.monotonic()
        self._falls_back(dispatcher, "한 바이트씩 흘리는 답")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn("did not answer", dispatcher.stats["last_error"])

    def test_a_solver_status_that_is_not_success(self):
        def broken(answer):
            answer["response"]["solver_response"]["status"] = 1
            return answer

        FakeCuOpt.mangle = staticmethod(broken)
        dispatcher = CuOptDispatcher(self.url, timeout_s=2.0)
        self._falls_back(dispatcher, "solver status 1")
        self.assertIn("status", dispatcher.stats["last_error"])

    def test_a_task_we_never_sent(self):
        def broken(answer):
            routes = answer["response"]["solver_response"]["vehicle_data"]
            first = next(iter(routes.values()))
            first["task_id"][1] = "la-mars"
            return answer

        FakeCuOpt.mangle = staticmethod(broken)
        dispatcher = CuOptDispatcher(self.url, timeout_s=2.0)
        self._falls_back(dispatcher, "모르는 과제")
        self.assertIn("la-mars", dispatcher.stats["last_error"])

    def test_garbage_instead_of_a_solution(self):
        FakeCuOpt.mangle = staticmethod(lambda answer: {"response": {"nonsense": True}})
        self._falls_back(CuOptDispatcher(self.url, timeout_s=2.0), "답이 답이 아님")

    def test_after_a_failure_it_backs_off_instead_of_stalling_every_stop(self):
        FakeCuOpt.post_status = 500
        dispatcher = CuOptDispatcher(self.url, timeout_s=1.0, backoff_s=60.0)
        drone = self.world.vehicles["drone-01"]
        self.assertIsNotNone(dispatcher.next_stop(self.world, drone))
        posted = len(FakeCuOpt.posts)
        for _ in range(3):
            self.assertIsNotNone(dispatcher.next_stop(self.world, drone))
        self.assertEqual(len(FakeCuOpt.posts), posted, "물러선 동안에는 묻지 않습니다")
        self.assertEqual(dispatcher.stats["fell_back"], 4)


class WhichDispatcherTest(unittest.TestCase):
    def test_rules_unless_a_cuopt_server_is_named(self):
        with mock.patch.dict("os.environ", {"DISPATCH": "", "CUOPT_URL": ""}, clear=False):
            self.assertIsInstance(build_dispatcher(), RuleDispatcher)
        with mock.patch.dict("os.environ", {"DISPATCH": "cuopt", "CUOPT_URL": ""}):
            self.assertIsInstance(build_dispatcher(), RuleDispatcher)
        with mock.patch.dict("os.environ", {"DISPATCH": "cuopt",
                                            "CUOPT_URL": "http://gpu.test:8000"}):
            dispatcher = build_dispatcher()
        self.assertIsInstance(dispatcher, CuOptDispatcher)
        self.assertEqual(dispatcher.url, "http://gpu.test:8000")

    def test_each_world_keeps_its_own_dispatcher(self):
        simulation = Simulation(seed=7)
        guarded = dispatcher_for(simulation.worlds["guarded"])
        direct = dispatcher_for(simulation.worlds["direct"])
        self.assertIsNot(guarded, direct)
        self.assertIs(guarded, dispatcher_for(simulation.worlds["guarded"]))

    def test_the_runtime_knows_nothing_about_dispatch(self):
        """배차는 운영사의 일입니다. 런타임 파일이 이 이름들을 알면 그 경계가 무너진 것입니다."""
        import pathlib
        runtime = pathlib.Path(__file__).resolve().parent.parent / "backend"
        for path in runtime.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in ("sim.dispatch", "dispatcher_for", "Dispatcher", "cuopt", "cuOpt",
                         "CUOPT"):
                self.assertNotIn(word, text, f"{path.name} 이 배차를 압니다")


if __name__ == "__main__":
    unittest.main()
