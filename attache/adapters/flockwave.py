"""Talks to Skybrush Server over Flockwave.

Flockwave is newline-delimited JSON over TCP (port 5001 by default), so this needs no
library at all. The server owns the drones; we send mission-level messages and it decides
whether the aircraft can do them, exactly like the MAVLink adapter.

Message shapes come from skybrush-io/flockwave-spec:
  envelope   {"$fw.version": "1.0", "id": "<uuid>", "body": {...}}
  UAV-FLY    body {"type": "UAV-FLY", "ids": [...], "target": [lat*1e7, lon*1e7, amsl_mm]}
  UAV-INF    body {"type": "UAV-INF", "ids": [...]} -> status per id
  UAV-SIGNAL body {"type": "UAV-SIGNAL", "ids": [...], "signals": [...], "duration": ms}
"""

import json
import os
import queue
import socket
import threading
import uuid

FLOCKWAVE_VERSION = "1.0"
DEFAULT_PORT = 5001

# 착륙 패드 좌표. 실제 배치에서는 버티포트 좌표가 들어갑니다.
PAD_COORDS = {
    "pad:P1": (37.50725, 127.07750),
    "pad:P2": (37.50725, 127.08850),
}
CRUISE_AMSL_M = 90.0

# 우리 동작 이름 → Flockwave 메시지. 여기 없는 것은 지상 설비라 기체 명령이 없습니다.
SIMPLE_COMMANDS = {
    "land": "UAV-LAND",
    "depart": "UAV-TAKEOFF",
    "divert_ground": "UAV-RTH",
    "disengage_autonomy": "UAV-HALT",
}


def _envelope(body: dict) -> dict:
    return {"$fw.version": FLOCKWAVE_VERSION, "id": uuid.uuid4().hex, "body": body}


class FlockwaveAdapter:
    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 uav_ids: dict[str, str] | None = None,
                 timeout_s: float = 5.0):
        """uav_ids: {"taxi-a": "SIM-00", ...} — 우리 이름과 Skybrush 기체 이름의 대응."""
        self.host = host
        self.port = port
        self.uav_ids = uav_ids or {}
        self.timeout_s = timeout_s

        self._socket: socket.socket | None = None
        self._replies: dict[str, queue.Queue] = {}
        self._status: dict[str, dict] = {}
        self._tick = 0
        self._guard = threading.Lock()
        self._send_lock = threading.Lock()
        self._connect()
        threading.Thread(target=self._read_forever, daemon=True).start()
        threading.Thread(target=self._poll_forever, daemon=True).start()

    # ---------- 연결 ----------

    def _connect(self) -> None:
        self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        self._socket.settimeout(None)

    def _send(self, body: dict) -> dict | None:
        """봉투를 보내고 refs 로 돌아온 응답을 기다립니다."""
        message = _envelope(body)
        inbox: queue.Queue = queue.Queue(maxsize=1)
        with self._guard:
            self._replies[message["id"]] = inbox
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        try:
            with self._send_lock:
                self._socket.sendall(payload)
        except OSError as error:
            with self._guard:
                self._replies.pop(message["id"], None)
            return {"error": str(error)}
        try:
            return inbox.get(timeout=self.timeout_s)
        except queue.Empty:
            return None
        finally:
            with self._guard:
                self._replies.pop(message["id"], None)

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
            return  # 스펙이 말한 대로, 못 읽는 줄은 버리고 다음 줄부터 다시
        body = message.get("body") or {}
        if body.get("type") == "UAV-INF":
            self._absorb_status(body.get("status") or {})
        reference = message.get("refs")
        if not reference:
            return
        with self._guard:
            inbox = self._replies.get(reference)
        if inbox is not None:
            try:
                inbox.put_nowait(message)
            except queue.Full:
                pass

    def _absorb_status(self, status: dict) -> None:
        with self._guard:
            for uav_id, entry in status.items():
                self._status[uav_id] = entry

    def _poll_forever(self) -> None:
        import time

        while True:
            if self.uav_ids:
                self._send({"type": "UAV-INF", "ids": list(self.uav_ids.values())})
            time.sleep(0.5)

    # ---------- 텔레메트리 ----------

    def telemetry(self) -> dict:
        with self._guard:
            self._tick += 1
            raw = {k: dict(v) for k, v in self._status.items()}
            tick = self._tick
        assets = {}
        for asset_id, uav_id in self.uav_ids.items():
            assets[asset_id] = self._to_asset(asset_id, raw.get(uav_id, {}))
        return {"tick": tick, "assets": assets}

    @staticmethod
    def _to_asset(asset_id: str, entry: dict) -> dict:
        position = entry.get("position") or []
        battery = entry.get("battery") or []
        # BatteryInfo 는 [decivolt] 또는 [decivolt, percentage] 입니다
        percentage = float(battery[1]) if len(battery) > 1 else 100.0
        altitude = (position[2] / 1000.0) if len(position) > 2 else 0.0
        return {
            "id": asset_id,
            "model": os.getenv("VEHICLE_MODEL", "skybrush-virtual"),
            "kind": "drone",
            "lat": (position[0] / 1e7) if position else None,
            "lon": (position[1] / 1e7) if len(position) > 1 else None,
            "alt_m": round(altitude, 1),
            "battery": percentage,
            "vibration": 0.0,
            "autonomy_health": 0.0 if entry.get("errors") else 1.0,
            "passengers": 0,
            "assigned_pad": None,
            "state": "landed" if altitude < 1.0 else "cruising",
        }

    # ---------- 명령 ----------

    def execute(self, asset_id: str, action: str, params: dict, ledger_id: str,
                blast: str = "none", approved_by: str | None = None) -> dict:
        uav_id = self.uav_ids.get(asset_id)
        if uav_id is None:
            return {"ok": False, "error": f"no Skybrush id mapped for {asset_id}"}

        if action == "reserve_pad":
            target = PAD_COORDS.get(params.get("pad"))
            if target is None:
                return {"ok": False, "error": f"unknown pad {params.get('pad')}"}
            body = {
                "type": "UAV-FLY",
                "ids": [uav_id],
                "target": [
                    int(target[0] * 1e7), int(target[1] * 1e7), int(CRUISE_AMSL_M * 1000)
                ],
            }
        elif action in SIMPLE_COMMANDS:
            body = {"type": SIMPLE_COMMANDS[action], "ids": [uav_id]}
        else:
            # 충전기와 착륙료는 자동조종 소관이 아닙니다. 런타임이 원장에만 남깁니다.
            return {"ok": True, "note": f"{action} is ground equipment, no vehicle command"}

        reply = self._send(body)
        return self._read_result(reply, uav_id)

    @staticmethod
    def _read_result(reply: dict | None, uav_id: str) -> dict:
        """서버가 거절할 수 있습니다. 승인은 우리가, 수락은 기체가 합니다."""
        if reply is None:
            return {"ok": False, "error": "Skybrush did not answer"}
        body = reply.get("body") or {}
        if uav_id in (body.get("error") or {}):
            return {"ok": False, "error": body["error"][uav_id]}
        if body.get("result", {}).get(uav_id) is True:
            return {"ok": True}
        if uav_id in (body.get("receipt") or {}):
            return {"ok": True, "receipt": body["receipt"][uav_id]}
        return {"ok": False, "error": f"no verdict for {uav_id}"}

    def signal(self, signals: list[str] | None = None,
               duration_ms: int = 5000) -> dict | None:
        """기단 전체에 신호를 켭니다. 화면에서 어느 쪽인지 눈으로 찾을 때 씁니다.

        스펙상 signals 는 색이 아니라 장치 이름입니다("sound", "light"). 색까지 지정하려면
        조명 프로그램을 따로 올려야 합니다. 두 기단을 가르는 확실한 수단은 이름과 위치입니다.
        """
        if not self.uav_ids:
            return None
        return self._send({
            "type": "UAV-SIGNAL",
            "ids": list(self.uav_ids.values()),
            "signals": signals or ["light"],
            "duration": duration_ms,
        })


def from_env() -> "FlockwaveAdapter":
    """FLOCKWAVE_HOST, FLOCKWAVE_PORT, UAV_IDS='taxi-a=SIM-00,drone-b=SIM-01'"""
    raw = os.environ.get("UAV_IDS", "")
    ids = dict(pair.split("=", 1) for pair in raw.split(",") if "=" in pair)
    return FlockwaveAdapter(
        host=os.environ.get("FLOCKWAVE_HOST", "skybrush"),
        port=int(os.environ.get("FLOCKWAVE_PORT", DEFAULT_PORT)),
        uav_ids=ids,
    )
