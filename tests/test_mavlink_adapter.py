"""Runs the adapter against a MAVLink autopilot stub. No simulator, no Docker needed.

The stub answers COMMAND_ACK and the mission protocol the way PX4 does, including refusals,
so the tests cover the cases that matter: a cleared route arrives item for item at the
altitudes the runtime judged; a recall replaces the mission in flight; the runtime approved
the action and the autopilot still said no. Approval and acceptance are different things and
both have to hold.
"""

import socket
import threading
import time
import unittest

try:
    from pymavlink import mavutil

    HAS_PYMAVLINK = True
except ImportError:  # 기본 설치에는 없습니다. 이 어댑터를 쓸 때만 넣습니다.
    HAS_PYMAVLINK = False

def next_port() -> int:
    """운영체제가 비어 있다고 주는 UDP 포트. 시험마다 새 포트입니다.

    고정 범위(14599~)를 쓰던 때는 같은 기계에서 시험을 둘 돌리면 서로의 포트를 잡았습니다.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(condition, timeout_s: float = 5.0, step_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step_s)
    return bool(condition())


class AutopilotStub:
    """PX4 처럼 답하는 자동조종 흉내.

    하트비트·위치·배터리를 흘리고, 명령(COMMAND_LONG)과 임무 올리기(MISSION_*)에 답합니다.
    받은 것을 순서대로 적어 두어 '무엇을 언제 보냈나' 를 시험이 확인할 수 있게 합니다.
    """

    def __init__(self, port: int, refuse: bool = False, refuse_commands=(),
                 refuse_mission: bool = False, silent: bool = False, airborne: bool = False,
                 rerequest: bool = False):
        self.link = mavutil.mavlink_connection(f"udpout:127.0.0.1:{port}", source_system=1,
                                               source_component=1)
        self.refuse = refuse                        # 모든 명령을 거절
        self.refuse_commands = set(refuse_commands)  # 이 명령만 거절 (예: 시동)
        self.refuse_mission = refuse_mission        # 임무를 끝까지 받고 오류로 답함
        self.silent = silent                        # 하트비트만 흘리고 아무것에도 답하지 않음
        self.rerequest = rerequest                  # 항목을 받아도 늘 0번을 다시 달라고 함(고장)
        self.armed = airborne
        self.alt_m = 60.0 if airborne else 0.0
        self.custom_mode = 0
        self.received: list[int] = []               # 받은 COMMAND_LONG 의 command, 순서대로
        self.commands: list = []                    # 받은 COMMAND_LONG 전체
        self.missions: list[list] = []              # 끝까지 올라온 임무(MISSION_ITEM_INT 목록)
        self.cleared = 0
        self.traffic: list[str] = []                # 하트비트 말고 받은 메시지 종류, 순서대로
        self.error: BaseException | None = None
        self.running = True
        self._upload: dict | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.running = False
        self.thread.join(timeout=2)
        self.link.close()

    def _run(self):
        try:
            self._loop()
        except BaseException as exc:  # noqa: BLE001 - 스텁이 조용히 죽으면 진단이 안 됩니다
            self.error = exc

    def _loop(self):
        last_beat = 0.0
        while self.running:
            now = time.time()
            if now - last_beat > 0.2:
                self._beat(now)
                last_beat = now
            while True:
                message = self.link.recv_match(blocking=False)
                if message is None:
                    break
                self._answer(message)
            time.sleep(0.01)

    def _beat(self, now: float):
        mav = self.link.mav
        base = mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        if self.armed:
            base |= mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
        mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_QUADROTOR, mavutil.mavlink.MAV_AUTOPILOT_PX4,
                           base, self.custom_mode, mavutil.mavlink.MAV_STATE_ACTIVE)
        mav.battery_status_send(0, 0, 0, 0, [4000] * 10, -1, -1, -1, 41)
        mav.global_position_int_send(int(now * 1000) % 2**32, 473979710, 85461640, 500000,
                                     int(self.alt_m * 1000), 0, 0, 0, 0)
        mav.extended_sys_state_send(0, 2 if self.alt_m > 1.0 else 1)

    def _answer(self, message):
        kind = message.get_type()
        if kind == "HEARTBEAT":
            return
        self.traffic.append(kind)
        if self.silent:
            return
        if kind == "COMMAND_LONG":
            self._command(message)
        elif kind == "MISSION_COUNT":
            self._upload = {"count": int(message.count), "items": []}
            self._request(0)
        elif kind == "MISSION_ITEM_INT":
            self._item(message)
        elif kind == "MISSION_CLEAR_ALL":
            self.cleared += 1
            self._mission_ack(mavutil.mavlink.MAV_MISSION_ACCEPTED)

    def _command(self, message):
        self.received.append(message.command)
        self.commands.append(message)
        refused = self.refuse or message.command in self.refuse_commands
        if not refused:
            if message.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
                self.armed = message.param1 == 1
            elif message.command == mavutil.mavlink.MAV_CMD_DO_SET_MODE:
                self.custom_mode = (int(message.param2) << 16) | (int(message.param3) << 24)
        self.link.mav.command_ack_send(
            message.command,
            mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED if refused
            else mavutil.mavlink.MAV_RESULT_ACCEPTED,
        )

    def _item(self, message):
        if self.rerequest:
            self._request(0)
            return
        upload = self._upload
        if upload is None or message.seq != len(upload["items"]):
            return  # 순서 밖의 항목. 자동조종은 같은 번호를 다시 요청합니다
        upload["items"].append(message)
        if len(upload["items"]) < upload["count"]:
            self._request(len(upload["items"]))
            return
        self._upload = None
        if self.refuse_mission:
            self._mission_ack(mavutil.mavlink.MAV_MISSION_ERROR)
            return
        self.missions.append(upload["items"])
        self._mission_ack(mavutil.mavlink.MAV_MISSION_ACCEPTED)

    def _request(self, seq: int):
        self.link.mav.mission_request_int_send(255, 0, seq)

    def _mission_ack(self, kind: int):
        self.link.mav.mission_ack_send(255, 0, kind)


LEGS = [{"lat": 40.701803, "lon": -73.970437, "alt_m": 70.0},
        {"lat": 40.710000, "lon": -73.980000, "alt_m": 70.0},
        {"lat": 40.720000, "lon": -73.990000, "alt_m": 60.0}]


class RouteItemsTest(unittest.TestCase):
    """경로 → 임무 항목. 네트워크가 필요 없는 부분이라 pymavlink 없이도 돕니다."""

    def test_a_grounded_aircraft_takes_off_where_it_stands(self):
        from holdshort.adapters.mavlink_fleet import route_items

        items = route_items(LEGS, airborne=False)
        self.assertEqual([item["command"] for item in items],
                         ["takeoff", "waypoint", "waypoint", "land"])
        self.assertEqual([item["alt_m"] for item in items], [70.0, 70.0, 60.0, 0.0])
        self.assertEqual((items[0]["lat"], items[0]["lon"]), (LEGS[0]["lat"], LEGS[0]["lon"]))
        self.assertEqual((items[-1]["lat"], items[-1]["lon"]), (LEGS[-1]["lat"], LEGS[-1]["lon"]))

    def test_an_airborne_aircraft_flies_the_first_leg_too(self):
        from holdshort.adapters.mavlink_fleet import route_items

        items = route_items(LEGS, airborne=True)
        self.assertEqual([item["command"] for item in items],
                         ["waypoint", "waypoint", "waypoint", "land"])

    def test_half_a_route_is_no_mission(self):
        from holdshort.adapters.mavlink_fleet import route_items

        self.assertEqual(route_items([LEGS[0]], airborne=False), [])

    def test_the_exit_mission_is_the_way_out_and_a_landing(self):
        from holdshort.adapters.mavlink_fleet import exit_items

        items = exit_items({"lat": 40.705, "lon": -73.975}, 55.0)
        self.assertEqual([item["command"] for item in items], ["waypoint", "land"])
        self.assertEqual(items[0]["alt_m"], 55.0)

    def test_px4_mode_names_come_from_the_heartbeat(self):
        from holdshort.adapters.mavlink_fleet import px4_mode_name

        self.assertEqual(px4_mode_name((4 << 16) | (4 << 24)), "AUTO.MISSION")
        self.assertEqual(px4_mode_name((4 << 16) | (6 << 24)), "AUTO.LAND")
        self.assertEqual(px4_mode_name(3 << 16), "POSCTL")


@unittest.skipUnless(HAS_PYMAVLINK, "pymavlink 미설치 (pip install pymavlink)")
class MavlinkAdapterTest(unittest.TestCase):
    def _adapter(self, **stub_options):
        from holdshort.adapters.mavlink_fleet import MavlinkFleetAdapter

        port = next_port()
        stub = AutopilotStub(port, **stub_options).start()
        self.addCleanup(stub.stop)
        adapter = MavlinkFleetAdapter(
            {"drone-02": f"udpin:127.0.0.1:{port}"}, ack_timeout_s=2.0, link_timeout_s=8.0
        )
        self.addCleanup(adapter.close)
        return adapter, stub

    def _check_stub(self, stub):
        if stub.error is not None:
            raise AssertionError(f"자동조종 스텁이 죽었습니다: {stub.error!r}")

    def test_telemetry_comes_from_the_autopilot(self):
        adapter, stub = self._adapter()
        entry = {}

        def battery_and_position_seen():
            # 배터리와 위치는 같은 박동에 오지만 따로 흡수됩니다. 배터리만 보고 멈추면 부하가 걸린
            # 기계에서 위치가 아직 없었습니다.
            nonlocal entry
            entry = adapter.telemetry()["assets"]["drone-02"]
            return entry.get("battery") == 41.0 and entry.get("lat") is not None

        self.assertTrue(wait_until(battery_and_position_seen, 8))
        self._check_stub(stub)
        self.assertEqual(entry["battery"], 41.0)
        self.assertAlmostEqual(entry["lat"], 47.397971, places=5)

    def test_land_is_sent_and_acknowledged(self):
        adapter, stub = self._adapter()
        result = adapter.execute("drone-02", "land", {}, "l_test")
        self._check_stub(stub)
        self.assertTrue(result["ok"], result)
        self.assertIn(mavutil.mavlink.MAV_CMD_NAV_LAND, stub.received)

    def test_autopilot_refusal_is_reported_not_swallowed(self):
        adapter, _ = self._adapter(refuse=True)
        result = adapter.execute("drone-02", "land", {}, "l_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED)

    def test_ground_equipment_needs_no_autopilot_command(self):
        adapter, stub = self._adapter()
        result = adapter.execute("drone-02", "fast_charge", {}, "l_test")
        self.assertTrue(result["ok"])
        self.assertEqual(stub.received, [])

    def test_a_cleared_route_is_uploaded_item_for_item_then_armed_and_started(self):
        adapter, stub = self._adapter()
        result = adapter.execute("drone-02", "fly_route", {"legs": LEGS}, "l_route")
        self._check_stub(stub)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(stub.missions), 1)
        items = stub.missions[0]
        self.assertEqual([item.command for item in items],
                         [mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                          mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                          mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                          mavutil.mavlink.MAV_CMD_NAV_LAND])
        # 판정된 구간의 고도와 좌표 그대로. 어댑터가 경로를 다시 그리지 않는다는 뜻입니다.
        for item, leg in zip(items, LEGS, strict=False):
            self.assertEqual(item.frame, mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT)
            self.assertEqual(item.x, round(leg["lat"] * 1e7))
            self.assertEqual(item.y, round(leg["lon"] * 1e7))
            self.assertAlmostEqual(item.z, leg["alt_m"], places=3)
        self.assertEqual(items[-1].x, round(LEGS[-1]["lat"] * 1e7))
        # 임무를 다 올린 뒤에야 모드·시동·시작입니다.
        self.assertEqual(stub.received, [mavutil.mavlink.MAV_CMD_DO_SET_MODE,
                                         mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                                         mavutil.mavlink.MAV_CMD_MISSION_START])
        mode = stub.commands[0]
        self.assertEqual((int(mode.param2), int(mode.param3)), (4, 4))  # AUTO.MISSION
        self.assertEqual(int(stub.commands[1].param1), 1)               # 시동 걸기

    def test_departure_sends_the_autopilot_nothing(self):
        adapter, stub = self._adapter()
        result = adapter.execute("drone-02", "depart", {}, "l_depart")
        self.assertTrue(result["ok"], result)
        time.sleep(0.2)
        self.assertEqual(stub.received, [])
        self.assertEqual(stub.missions, [])

    def test_a_recall_in_flight_becomes_a_mission_to_the_exit(self):
        adapter, stub = self._adapter(airborne=True)
        self.assertTrue(wait_until(lambda: adapter.airborne("drone-02"), 8))
        result = adapter.execute("drone-02", "divert_ground",
                                 {"exit": {"lat": 40.705, "lon": -73.975}}, "l_recall")
        self._check_stub(stub)
        self.assertTrue(result["ok"], result)
        items = stub.missions[-1]
        self.assertEqual([item.command for item in items],
                         [mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, mavutil.mavlink.MAV_CMD_NAV_LAND])
        self.assertEqual(items[0].x, round(40.705 * 1e7))
        self.assertAlmostEqual(items[0].z, 60.0, places=1)   # 지금 고도로 나갑니다
        # 이미 떠 있으니 시동은 다시 걸지 않습니다.
        self.assertNotIn(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, stub.received)
        self.assertIn(mavutil.mavlink.MAV_CMD_MISSION_START, stub.received)

    def test_a_recall_without_an_exit_lands_where_it_is(self):
        adapter, stub = self._adapter(airborne=True)
        self.assertTrue(wait_until(lambda: adapter.airborne("drone-02"), 8))
        result = adapter.execute("drone-02", "divert_ground", {"volume": "nofly-t"}, "l_recall")
        self.assertTrue(result["ok"], result)
        self.assertEqual(stub.received, [mavutil.mavlink.MAV_CMD_NAV_LAND])

    def test_a_recall_on_the_ground_clears_the_mission_so_it_never_arms(self):
        adapter, stub = self._adapter()
        adapter.execute("drone-02", "fly_route", {"legs": LEGS}, "l_route")
        stub.received.clear()
        result = adapter.execute("drone-02", "divert_ground", {}, "l_recall")
        self.assertTrue(result["ok"], result)
        self.assertEqual(stub.cleared, 1)
        self.assertEqual(stub.received, [])

    def test_a_refused_mission_is_reported_and_nothing_is_armed(self):
        adapter, stub = self._adapter(refuse_mission=True)
        result = adapter.execute("drone-02", "fly_route", {"legs": LEGS}, "l_route")
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["result"], mavutil.mavlink.MAV_MISSION_ERROR)
        self.assertEqual(stub.received, [])

    def test_a_silent_autopilot_times_out_instead_of_hanging(self):
        adapter, stub = self._adapter(silent=True)
        self.assertTrue(wait_until(lambda: adapter.link_up("drone-02"), 8))
        started = time.monotonic()
        result = adapter.execute("drone-02", "fly_route", {"legs": LEGS}, "l_route")
        spent = time.monotonic() - started
        self.assertFalse(result["ok"], result)
        self.assertIn("never asked", result["error"])
        self.assertLess(spent, 10.0, "응답 없는 자동조종을 영영 기다렸습니다")
        self.assertEqual(stub.received, [])

    def test_an_autopilot_that_keeps_asking_for_one_item_is_given_up_on(self):
        """같은 항목만 끝없이 다시 달라는 자동조종. 올리기에 끝이 없으면 거울의 작업 스레드가
        거기 붙잡혀 뒤에 줄 선 회수가 안 나갑니다(실측: 25초에 항목 1,866개, 회수 미전송)."""
        adapter, stub = self._adapter(rerequest=True)
        self.assertTrue(wait_until(lambda: adapter.link_up("drone-02"), 8))
        started = time.monotonic()
        result = adapter.execute("drone-02", "fly_route", {"legs": LEGS}, "l_route")
        self.assertFalse(result["ok"], result)
        self.assertIn("kept asking for mission item 0", result["error"])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(stub.received, [], "올리지 못한 임무로 시동을 걸지 않습니다")
        self._check_stub(stub)


if __name__ == "__main__":
    unittest.main()
