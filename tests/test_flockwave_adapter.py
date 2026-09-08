"""Runs the Flockwave adapter against a stub Skybrush server. No Docker needed.

The stub speaks the real wire format from skybrush-io/flockwave-spec: newline-delimited
JSON envelopes over TCP, responses carrying `refs` back to the request id. It also refuses
one command, because the runtime approving something and the aircraft accepting it are
different events and both have to be recorded.
"""

import json
import socket
import threading
import time
import unittest

from attache.adapters.flockwave import FlockwaveAdapter

STATUS = {
    "SIM-00": {
        "id": "SIM-00",
        "position": [375072500, 1270775000, 90000],
        "battery": [124, 41],
        "errors": [],
    }
}


class SkybrushStub:
    """진짜 서버가 하는 것만 합니다. 봉투를 읽고, refs 를 달아 돌려줍니다."""

    def __init__(self, refuse: bool = False):
        self.refuse = refuse
        self.seen: list[dict] = []
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.server.close()
        except OSError:
            pass

    def _serve(self):
        try:
            connection, _ = self.server.accept()
        except OSError:
            return
        buffer = b""
        while self.running:
            try:
                chunk = connection.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                request = json.loads(line)
                reply = self._answer(request)
                connection.sendall((json.dumps(reply) + "\n").encode())

    def _answer(self, request: dict) -> dict:
        body = request.get("body", {})
        self.seen.append(body)
        kind = body.get("type")
        if kind == "UAV-INF":
            answer = {"type": "UAV-INF", "status": STATUS}
        elif self.refuse:
            answer = {"type": kind, "result": {},
                      "error": {i: "UAV is not armed." for i in body.get("ids", [])}}
        else:
            answer = {"type": kind, "result": {i: True for i in body.get("ids", [])}}
        return {"$fw.version": "1.0", "id": "r" + request["id"],
                "refs": request["id"], "body": answer}


class FlockwaveAdapterTest(unittest.TestCase):
    def _adapter(self, refuse=False):
        stub = SkybrushStub(refuse=refuse)
        self.addCleanup(stub.stop)
        adapter = FlockwaveAdapter("127.0.0.1", stub.port,
                                   uav_ids={"drone-01": "SIM-00"}, timeout_s=4.0)
        return adapter, stub

    def test_telemetry_comes_back_in_our_shape(self):
        adapter, _ = self._adapter()
        deadline = time.time() + 6
        while time.time() < deadline:
            asset = adapter.telemetry()["assets"]["drone-01"]
            if asset["battery"] == 41.0:
                break
            time.sleep(0.1)
        self.assertEqual(asset["battery"], 41.0)
        self.assertAlmostEqual(asset["lat"], 37.50725, places=5)
        self.assertAlmostEqual(asset["lon"], 127.0775, places=4)
        self.assertEqual(asset["alt_m"], 90.0)

    def test_reserving_a_pad_becomes_a_fly_command(self):
        adapter, stub = self._adapter()
        result = adapter.execute("drone-01", "reserve_pad", {"pad": "pad:P2"}, "l_1")
        self.assertTrue(result["ok"], result)
        flights = [b for b in stub.seen if b.get("type") == "UAV-FLY"]
        self.assertEqual(len(flights), 1)
        self.assertEqual(flights[0]["ids"], ["SIM-00"])
        self.assertEqual(flights[0]["target"][0], int(37.50725 * 1e7))

    def test_landing_becomes_a_land_command(self):
        adapter, stub = self._adapter()
        self.assertTrue(adapter.execute("drone-01", "land", {}, "l_1")["ok"])
        self.assertIn("UAV-LAND", [b.get("type") for b in stub.seen])

    def test_a_refusal_is_reported_not_swallowed(self):
        adapter, _ = self._adapter(refuse=True)
        result = adapter.execute("drone-01", "land", {}, "l_1")
        self.assertFalse(result["ok"])
        self.assertIn("not armed", result["error"])

    def test_ground_equipment_sends_no_vehicle_command(self):
        adapter, stub = self._adapter()
        result = adapter.execute("drone-01", "fast_charge", {}, "l_1")
        self.assertTrue(result["ok"])
        self.assertEqual([b for b in stub.seen if b.get("type", "").startswith("UAV-")
                          and b["type"] != "UAV-INF"], [])

    def test_an_unmapped_asset_is_refused(self):
        adapter, _ = self._adapter()
        result = adapter.execute("nobody", "land", {}, "l_1")
        self.assertFalse(result["ok"])

    def test_signalling_the_fleet_sends_one_message_for_all(self):
        adapter, stub = self._adapter()
        adapter.signal()
        signals = [b for b in stub.seen if b.get("type") == "UAV-SIGNAL"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["ids"], ["SIM-00"])
        self.assertEqual(signals[0]["signals"], ["light"])


if __name__ == "__main__":
    unittest.main()
