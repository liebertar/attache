"""A Flockwave client, written by the team that wired the agent to the drones.

It is a near-copy of the runtime's adapter, and that duplication is the point. Every fleet
that talks to the vehicles directly ends up maintaining one of these, each slightly
different, each with its own idea of what the rules are.
"""

import json
import queue
import socket
import threading
import uuid

CRUISE_AMSL_MM = 90_000
SIMPLE = {
    "land": "UAV-LAND",
    "depart": "UAV-TAKEOFF",
    "divert_ground": "UAV-RTH",
    "disengage_autonomy": "UAV-HALT",
}


class FlockCommander:
    def __init__(self, host: str, port: int, uav_id: str, timeout_s: float = 5.0):
        self.uav_id = uav_id
        self.timeout_s = timeout_s
        self.state: dict = {}
        self.ready = threading.Event()
        self._replies: dict[str, queue.Queue] = {}
        self._guard = threading.Lock()
        self._send_lock = threading.Lock()
        self._socket = socket.create_connection((host, port), timeout=timeout_s)
        self._socket.settimeout(None)
        threading.Thread(target=self._read_forever, daemon=True).start()
        threading.Thread(target=self._poll_forever, daemon=True).start()

    def _read_forever(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = self._socket.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip():
                    self._absorb(line)

    def _absorb(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return
        body = message.get("body") or {}
        if body.get("type") == "UAV-INF":
            entry = (body.get("status") or {}).get(self.uav_id)
            if entry:
                with self._guard:
                    self.state = entry
                self.ready.set()
        reference = message.get("refs")
        if reference:
            with self._guard:
                inbox = self._replies.get(reference)
            if inbox is not None:
                try:
                    inbox.put_nowait(message)
                except queue.Full:
                    pass

    def _poll_forever(self) -> None:
        import time

        while True:
            self._request({"type": "UAV-INF", "ids": [self.uav_id]})
            time.sleep(0.5)

    def _request(self, body: dict) -> dict | None:
        message = {"$fw.version": "1.0", "id": uuid.uuid4().hex, "body": body}
        inbox: queue.Queue = queue.Queue(maxsize=1)
        with self._guard:
            self._replies[message["id"]] = inbox
        try:
            with self._send_lock:
                self._socket.sendall((json.dumps(message) + "\n").encode())
            return inbox.get(timeout=self.timeout_s)
        except (OSError, queue.Empty):
            return None
        finally:
            with self._guard:
                self._replies.pop(message["id"], None)

    def telemetry(self, asset_id: str, model: str) -> dict:
        with self._guard:
            entry = dict(self.state)
        position = entry.get("position") or []
        battery = entry.get("battery") or []
        altitude = (position[2] / 1000.0) if len(position) > 2 else 0.0
        return {
            "id": asset_id, "model": model, "kind": "drone",
            "lat": (position[0] / 1e7) if position else None,
            "lon": (position[1] / 1e7) if len(position) > 1 else None,
            "alt_m": round(altitude, 1),
            "battery": float(battery[1]) if len(battery) > 1 else 100.0,
            "vibration": 0.0,
            "autonomy_health": 0.0 if entry.get("errors") else 1.0,
            "passengers": 0, "assigned_pad": entry.get("_pad"),
            "state": "landed" if altitude < 1.0 else "cruising",
        }

    def send(self, action: str, params: dict, pads: dict) -> dict:
        if not self.ready.wait(timeout=self.timeout_s * 4):
            return {"ok": False, "error": "Skybrush has not reported in"}
        if action == "reserve_pad":
            target = pads.get(params.get("pad"))
            if target is None:
                return {"ok": False, "error": f"unknown pad {params.get('pad')}"}
            with self._guard:
                self.state["_pad"] = params.get("pad")
            body = {"type": "UAV-FLY", "ids": [self.uav_id],
                    "target": [int(target[0] * 1e7), int(target[1] * 1e7), CRUISE_AMSL_MM]}
        elif action in SIMPLE:
            if action == "depart":
                with self._guard:
                    self.state.pop("_pad", None)
            body = {"type": SIMPLE[action], "ids": [self.uav_id]}
        else:
            return {"ok": True, "note": "ground equipment"}

        reply = self._request(body)
        if reply is None:
            return {"ok": False, "error": "no answer"}
        answer = reply.get("body") or {}
        if self.uav_id in (answer.get("error") or {}):
            return {"ok": False, "error": answer["error"][self.uav_id]}
        return {"ok": bool(answer.get("result", {}).get(self.uav_id)
                           or self.uav_id in (answer.get("receipt") or {}))}
