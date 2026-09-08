"""Talks to real autopilots over MAVLink.

PX4 and ArduPilot are the assured layer. They fly the aircraft; nothing here and nothing
in any model touches attitude or thrust. This adapter only translates an approved proposal
into the mission-level command the autopilot already knows how to refuse or accept, which
is exactly the split ASTM F3269 describes.

Ground equipment (pads, chargers, money) is not an autopilot concern and does not appear
here. Those resources live in the runtime's lock table and authority envelope.
"""

import os
import queue
import threading

# 착륙 지점. 실제 배치에서는 버티포트 좌표가 들어갑니다.
PAD_COORDS = {
    "pad:P1": (47.397971, 8.546164),
    "pad:P2": (47.398500, 8.547500),
}
DEPOT_ALT_M = 30.0


class MavlinkFleetAdapter:
    def __init__(self, endpoints: dict[str, str], world: str = "guarded",
                 ack_timeout_s: float = 3.0, link_timeout_s: float = 30.0):
        """endpoints: {"taxi-a": "udpin:0.0.0.0:14540", ...} — 기체 하나에 링크 하나."""
        from pymavlink import mavutil  # 이 어댑터를 쓸 때만 필요합니다

        self._mavutil = mavutil
        self.world = world
        self.ack_timeout_s = ack_timeout_s
        self.link_timeout_s = link_timeout_s
        self.links = {
            asset_id: mavutil.mavlink_connection(endpoint)
            for asset_id, endpoint in endpoints.items()
        }
        self._state: dict[str, dict] = {
            asset_id: {"id": asset_id, "state": "unknown"} for asset_id in endpoints
        }
        # 링크 하나는 스레드 하나만 읽습니다. 응답은 큐로 건네받습니다.
        self._acks: dict[str, queue.Queue] = {a: queue.Queue() for a in endpoints}
        self._ready: dict[str, threading.Event] = {a: threading.Event() for a in endpoints}
        self._tick = 0
        self._guard = threading.Lock()
        for asset_id in self.links:
            threading.Thread(target=self._listen, args=(asset_id,), daemon=True).start()

    # ---------- 텔레메트리 ----------

    def _listen(self, asset_id: str) -> None:
        link = self.links[asset_id]
        while True:
            message = link.recv_match(blocking=True, timeout=5)
            if message is None:
                continue
            if message.get_type() == "COMMAND_ACK":
                self._acks[asset_id].put(message)
                continue
            if message.get_type() == "HEARTBEAT":
                self._ready[asset_id].set()
            self._absorb(asset_id, message)

    def _absorb(self, asset_id: str, message) -> None:
        kind = message.get_type()
        with self._guard:
            entry = self._state[asset_id]
            if kind == "GLOBAL_POSITION_INT":
                entry["lat"] = message.lat / 1e7
                entry["lon"] = message.lon / 1e7
                entry["alt_m"] = message.relative_alt / 1000.0
            elif kind == "BATTERY_STATUS" and message.battery_remaining >= 0:
                entry["battery"] = float(message.battery_remaining)
            elif kind == "SYS_STATUS" and message.battery_remaining >= 0:
                entry.setdefault("battery", float(message.battery_remaining))
            elif kind == "VIBRATION":
                worst = max(message.vibration_x, message.vibration_y, message.vibration_z)
                entry["vibration"] = round(min(1.0, worst / 60.0), 3)
            elif kind == "HEARTBEAT":
                entry["armed"] = bool(
                    message.base_mode & self._mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED
                )
                entry["state"] = self._read_state(entry)
                entry["autonomy_health"] = 1.0 if entry.get("guided", True) else 0.0
            entry.setdefault("model", os.getenv("VEHICLE_MODEL", "px4-sitl"))
            entry.setdefault("kind", "drone")
            entry.setdefault("battery", 100.0)
            entry.setdefault("vibration", 0.0)
            entry.setdefault("autonomy_health", 1.0)
            entry.setdefault("passengers", 0)
            entry.setdefault("assigned_pad", None)

    @staticmethod
    def _read_state(entry: dict) -> str:
        if not entry.get("armed"):
            return "landed" if (entry.get("alt_m") or 0.0) < 1.0 else "grounded"
        return "approaching" if entry.get("assigned_pad") else "cruising"

    def telemetry(self) -> dict:
        with self._guard:
            self._tick += 1
            return {"tick": self._tick, "assets": {k: dict(v) for k, v in self._state.items()}}

    # ---------- 명령 ----------

    def execute(
        self,
        asset_id: str,
        action: str,
        params: dict,
        ledger_id: str,
        blast: str = "none",
        approved_by: str | None = None,
    ) -> dict:
        link = self.links.get(asset_id)
        if link is None:
            return {"ok": False, "error": f"no link for {asset_id}"}
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            # 지상 설비는 자동조종 소관이 아닙니다. 런타임이 원장에만 남깁니다.
            return {"ok": True, "note": f"{action} is ground equipment, no autopilot command"}
        if not self._ready[asset_id].wait(timeout=self.link_timeout_s):
            return {"ok": False, "error": f"{asset_id} autopilot has not reported in"}
        return handler(link, asset_id, params)

    def _do_reserve_pad(self, link, asset_id: str, params: dict) -> dict:  # noqa: D401
        pad = params.get("pad")
        target = PAD_COORDS.get(pad)
        if target is None:
            return {"ok": False, "error": f"unknown pad {pad}"}
        with self._guard:
            self._state[asset_id]["assigned_pad"] = pad
        link.mav.set_position_target_global_int_send(
            0, link.target_system, link.target_component,
            self._mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            0b0000111111111000,
            int(target[0] * 1e7), int(target[1] * 1e7), DEPOT_ALT_M,
            0, 0, 0, 0, 0, 0, 0, 0,
        )
        return {"ok": True, "sent": "set_position_target", "pad": pad}

    def _do_land(self, link, asset_id: str, params: dict) -> dict:
        return self._command(link, asset_id, self._mavutil.mavlink.MAV_CMD_NAV_LAND)

    def _do_depart(self, link, asset_id: str, params: dict) -> dict:
        with self._guard:
            self._state[asset_id]["assigned_pad"] = None
        return self._command(
            link, asset_id, self._mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            params7=float(params.get("alt_m", DEPOT_ALT_M)),
        )

    def _do_divert_ground(self, link, asset_id: str, params: dict) -> dict:
        with self._guard:
            self._state[asset_id]["assigned_pad"] = None
        return self._command(
            link, asset_id, self._mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH
        )

    def _do_disengage_autonomy(self, link, asset_id: str, params: dict) -> dict:
        """자동 조종을 놓고 사람에게 넘깁니다. 이 한 줄이 승객을 세우는 행동입니다."""
        with self._guard:
            self._state[asset_id]["guided"] = False
        return self._command(
            link, asset_id, self._mavutil.mavlink.MAV_CMD_DO_SET_MODE,
            params1=float(self._mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED),
        )

    def _command(self, link, asset_id: str, command: int,
                 params1: float = 0.0, params7: float = 0.0) -> dict:
        """자동조종이 거절할 수도 있습니다. 승인은 우리가, 수락은 자동조종이 합니다."""
        while not self._acks[asset_id].empty():  # 지난 응답을 먼저 비웁니다
            self._acks[asset_id].get_nowait()
        link.mav.command_long_send(
            link.target_system, link.target_component, command, 0,
            params1, 0, 0, 0, 0, 0, params7,
        )
        try:
            ack = self._acks[asset_id].get(timeout=self.ack_timeout_s)
        except queue.Empty:
            return {"ok": False, "error": "autopilot did not acknowledge"}
        accepted = ack.result == self._mavutil.mavlink.MAV_RESULT_ACCEPTED
        return {"ok": accepted, "result": int(ack.result)}


def from_env() -> "MavlinkFleetAdapter":
    """MAVLINK_ENDPOINTS='taxi-a=udpin:0.0.0.0:14540,drone-b=udpin:0.0.0.0:14541'"""
    raw = os.environ["MAVLINK_ENDPOINTS"]
    endpoints = dict(pair.split("=", 1) for pair in raw.split(","))
    return MavlinkFleetAdapter(endpoints)
