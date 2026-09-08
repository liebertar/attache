"""Runs the adapter against a MAVLink autopilot stub. No simulator, no Docker needed.

The stub answers COMMAND_ACK the way PX4 and ArduPilot do, including a refusal, so the
test covers the case that matters: the runtime approved the action and the autopilot
still said no. Approval and acceptance are different things and both have to hold.
"""

import threading
import time
import unittest

try:
    from pymavlink import mavutil

    HAS_PYMAVLINK = True
except ImportError:  # 기본 설치에는 없습니다. 이 어댑터를 쓸 때만 넣습니다.
    HAS_PYMAVLINK = False

PORT = 14599


class AutopilotStub:
    """하트비트와 배터리를 흘리고, 명령에 응답합니다."""

    def __init__(self, port: int, refuse: bool = False):
        self.link = mavutil.mavlink_connection(f"udpout:127.0.0.1:{port}", source_system=1)
        self.refuse = refuse
        self.received: list[int] = []
        self.error: BaseException | None = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.running = False
        self.thread.join(timeout=2)

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
                self.link.mav.heartbeat_send(
                    mavutil.mavlink.MAV_TYPE_QUADROTOR,
                    mavutil.mavlink.MAV_AUTOPILOT_PX4,
                    mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED, 0,
                    mavutil.mavlink.MAV_STATE_ACTIVE,
                )
                self.link.mav.battery_status_send(
                    0, 0, 0, 0, [4000] * 10, -1, -1, -1, 41,
                )
                self.link.mav.global_position_int_send(
                    int(now * 1000) % 2**32, 473979710, 85461640, 500000, 12000, 0, 0, 0, 0,
                )
                last_beat = now
            message = self.link.recv_match(type="COMMAND_LONG", blocking=False)
            if message is not None:
                self.received.append(message.command)
                self.link.mav.command_ack_send(
                    message.command,
                    mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED
                    if self.refuse
                    else mavutil.mavlink.MAV_RESULT_ACCEPTED,
                )
            time.sleep(0.02)


@unittest.skipUnless(HAS_PYMAVLINK, "pymavlink 미설치 (pip install pymavlink)")
class MavlinkAdapterTest(unittest.TestCase):
    def _adapter(self, port, refuse=False):
        from attache.adapters.mavlink_fleet import MavlinkFleetAdapter

        stub = AutopilotStub(port, refuse=refuse).start()
        self.addCleanup(stub.stop)
        adapter = MavlinkFleetAdapter(
            {"drone-b": f"udpin:127.0.0.1:{port}"}, ack_timeout_s=4.0, link_timeout_s=8.0
        )
        return adapter, stub

    def _check_stub(self, stub):
        if stub.error is not None:
            raise AssertionError(f"자동조종 스텁이 죽었습니다: {stub.error!r}")

    def test_telemetry_comes_from_the_autopilot(self):
        adapter, _ = self._adapter(PORT)
        deadline = time.time() + 8
        while time.time() < deadline:
            entry = adapter.telemetry()["assets"]["drone-b"]
            if entry.get("battery") == 41.0:
                break
            time.sleep(0.1)
        self._check_stub(_)
        self.assertEqual(entry["battery"], 41.0)
        self.assertAlmostEqual(entry["lat"], 47.397971, places=5)

    def test_land_is_sent_and_acknowledged(self):
        adapter, stub = self._adapter(PORT + 1)
        result = adapter.execute("drone-b", "land", {}, "l_test")
        self._check_stub(stub)
        self.assertTrue(result["ok"], result)
        self.assertIn(mavutil.mavlink.MAV_CMD_NAV_LAND, stub.received)

    def test_autopilot_refusal_is_reported_not_swallowed(self):
        adapter, _ = self._adapter(PORT + 2, refuse=True)
        result = adapter.execute("drone-b", "land", {}, "l_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], mavutil.mavlink.MAV_RESULT_TEMPORARILY_REJECTED)

    def test_ground_equipment_needs_no_autopilot_command(self):
        adapter, stub = self._adapter(PORT + 3)
        result = adapter.execute("drone-b", "fast_charge", {}, "l_test")
        self.assertTrue(result["ok"])
        self.assertEqual(stub.received, [])


if __name__ == "__main__":
    unittest.main()
