"""Talks to real autopilots over MAVLink.

PX4 and ArduPilot are the assured layer. They fly the aircraft; nothing here and nothing
in any model touches attitude or thrust. This adapter only translates an approved proposal
into the mission-level command the autopilot already knows how to refuse or accept, which
is exactly the split ASTM F3269 describes.

A cleared route becomes a MAVLink mission item for item: take off where the aircraft stands,
one waypoint per judged leg at that leg's altitude, land at the destination. Nothing here
draws, shortens or smooths a route; the autopilot is handed exactly what the runtime judged.

Ground equipment (pads, chargers, money) is not an autopilot concern and does not appear
here. Those resources live in the runtime's lock table and authority envelope.
"""

import math
import os
import queue
import threading
import time
from collections import deque

# 착륙 지점. 실제 배치에서는 버티포트 좌표가 들어갑니다.
PAD_COORDS = {
    "pad:P1": (47.397971, 8.546164),
    "pad:P2": (47.398500, 8.547500),
}
DEPOT_ALT_M = 30.0

# 이 높이(m)보다 높으면 떠 있는 것으로 봅니다. 자동조종이 착지 판정을 보내 주면 그것이 먼저입니다.
AIRBORNE_M = 1.0
# 하트비트가 이만큼(초) 끊기면 화면에 링크를 lost 로 씁니다. PX4 는 자기 시계로 1초마다 보내는데,
# 실시간보다 느리게 도는 SITL(이 Mac 에서 0.66배)이면 벽시계로 1.5초 간격이고, Docker Desktop 의
# 주소 변환을 지나며 한 번 잃으면 3초가 넘습니다. 화면이 5초부터 오래됨으로 쓰는 것과 맞춥니다.
LINK_STALE_S = 5.0
# 우리도 1초마다 하트비트를 보냅니다. udpout 으로 붙은 자동조종은 이것으로 우리 주소를 압니다.
GCS_HEARTBEAT_S = 1.0
# 임무 개수(MISSION_COUNT)를 보내고 첫 요청이 안 오면 다시 보내는 횟수. UDP 는 잃어버립니다.
MISSION_COUNT_TRIES = 3
# 같은 항목을 이만큼 넘게 다시 달라면 규약이 어긋난 것으로 보고 올리기를 접습니다. 잃어버린 항목을
# 한두 번 다시 달라는 것은 정상입니다(UDP). 끝없이 달라는 자동조종은 거울의 작업 스레드를
# 붙잡습니다.
MISSION_ITEM_RESENDS = 5
STATUS_TEXT_KEEP = 6

# PX4 는 HEARTBEAT.custom_mode 의 16~23비트에 main, 24~31비트에 sub 모드를 싣습니다.
PX4_MAIN_MODES = {1: "MANUAL", 2: "ALTCTL", 3: "POSCTL", 4: "AUTO", 5: "ACRO", 6: "OFFBOARD",
                  7: "STABILIZED"}
PX4_AUTO_MODES = {1: "READY", 2: "TAKEOFF", 3: "LOITER", 4: "MISSION", 5: "RTL", 6: "LAND",
                  8: "FOLLOW_TARGET", 9: "PRECLAND"}
PX4_MAIN_AUTO = 4
PX4_AUTO_MISSION = 4
LANDED_STATES = {1: "on_ground", 2: "in_air", 3: "taking_off", 4: "landing"}
MISSION_REPLIES = ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK")
# 자동조종에 보낼 것이 없는 행동. 짐 싣기(depart)는 땅의 일이고, 뜨는 것은 경로 임무가 합니다.
NO_AUTOPILOT_COMMAND = {"depart": "loading is ground work; the route mission is the departure"}


def route_items(legs: list[dict], airborne: bool) -> list[dict]:
    """승인된 경로 → 자동조종 임무. 점을 더하거나 빼지 않고 그대로 옮깁니다.

    legs[0] 은 신청할 때 기체가 있던 자리입니다. 땅에 있으면 거기서 첫 구간 고도로 이륙하고,
    떠 있으면 그 자리도 경유점으로 밟습니다 — 자동조종이 늦게 가고 있어도 승인된 경로의
    출발점부터 따라가게 하려는 것입니다. 마지막 점에서는 내립니다. 시뮬레이터도 목적지에
    내려 짐을 내립니다.
    """
    points = [_point(leg) for leg in legs]
    if len(points) < 2:
        return []
    first, rest = points[0], points[1:]
    items = [{"command": "waypoint" if airborne else "takeoff", **first}]
    items += [{"command": "waypoint", **point} for point in rest]
    items.append({"command": "land", "lat": rest[-1]["lat"], "lon": rest[-1]["lon"],
                  "alt_m": 0.0})
    return items


def exit_items(exit_point: dict, alt_m: float) -> list[dict]:
    """회수 임무 두 항목. 런타임이 준 바깥 자리(exit)까지 지금 고도로 가서 내립니다."""
    at = {"lat": float(exit_point["lat"]), "lon": float(exit_point["lon"])}
    return [{"command": "waypoint", **at, "alt_m": round(float(alt_m), 1)},
            {"command": "land", **at, "alt_m": 0.0}]


def px4_mode_name(custom_mode: int) -> str:
    main = (int(custom_mode) >> 16) & 0xFF
    sub = (int(custom_mode) >> 24) & 0xFF
    if main == PX4_MAIN_AUTO:
        return f"AUTO.{PX4_AUTO_MODES.get(sub, sub)}"
    return PX4_MAIN_MODES.get(main, f"MODE{main}")


def _point(leg: dict) -> dict:
    return {"lat": float(leg["lat"]), "lon": float(leg["lon"]),
            "alt_m": float(leg.get("alt_m") or 0.0)}


def _drain(inbox: queue.Queue) -> None:
    while True:
        try:
            inbox.get_nowait()
        except queue.Empty:
            return


def _blank_autopilot() -> dict:
    return {"mode": None, "landed": None, "mission_seq": None, "reached_seq": None,
            "last_beat": None, "status_text": deque(maxlen=STATUS_TEXT_KEEP)}


class MavlinkFleetAdapter:
    def __init__(self, endpoints: dict[str, str], world: str = "guarded",
                 ack_timeout_s: float = 3.0, link_timeout_s: float = 30.0):
        """endpoints: {"drone-01": "udpin:0.0.0.0:14540", ...} — 기체 하나에 링크 하나."""
        from pymavlink import mavutil  # 이 어댑터를 쓸 때만 필요합니다

        self._mavutil = mavutil
        self._mav = mavutil.mavlink
        self.world = world
        self.endpoints = dict(endpoints)
        self.ack_timeout_s = ack_timeout_s
        self.link_timeout_s = link_timeout_s
        self.links = {
            asset_id: mavutil.mavlink_connection(endpoint)
            for asset_id, endpoint in endpoints.items()
        }
        self._state: dict[str, dict] = {
            asset_id: {"id": asset_id, "state": "unknown"} for asset_id in endpoints
        }
        # 판정용 텔레메트리(_state)와 따로 둡니다. 자동조종만 아는 것 — 모드·임무 순번·상태 문장.
        self._autopilot: dict[str, dict] = {a: _blank_autopilot() for a in endpoints}
        # 링크 하나는 스레드 하나만 읽습니다. 응답은 큐로 건네받습니다.
        self._acks: dict[str, queue.Queue] = {a: queue.Queue() for a in endpoints}
        self._mission_replies: dict[str, queue.Queue] = {a: queue.Queue() for a in endpoints}
        self._ready: dict[str, threading.Event] = {a: threading.Event() for a in endpoints}
        # 명령을 받을 자동조종의 (system, component). 하트비트를 받으면 그 값으로 바꿉니다.
        # PX4 는 임무 메시지를 대상 번호가 자기 것일 때만 받습니다(0 = 모두 는 안 받습니다).
        self._targets: dict[str, tuple[int, int]] = {a: (1, 1) for a in endpoints}
        # 명령 하나는 여러 번의 왕복입니다. 둘이 겹치면 서로의 응답을 가져가서 한 번에 하나만.
        self._operations: dict[str, threading.RLock] = {a: threading.RLock() for a in endpoints}
        self._writes: dict[str, threading.Lock] = {a: threading.Lock() for a in endpoints}
        self._tick = 0
        self._guard = threading.Lock()
        self._closed = threading.Event()
        self._listeners = [
            threading.Thread(target=self._listen, args=(asset_id,), daemon=True,
                             name=f"mavlink-{asset_id}")
            for asset_id in self.links
        ]
        for listener in self._listeners:
            listener.start()

    def close(self) -> None:
        """듣는 스레드를 멈추고 링크를 닫습니다. 시험이 포트를 비우려면 필요합니다."""
        self._closed.set()
        for listener in self._listeners:
            listener.join(timeout=2.0)
        for link in self.links.values():
            link.close()

    # ---------- 텔레메트리 ----------

    def _listen(self, asset_id: str) -> None:
        link = self.links[asset_id]
        next_beat = 0.0
        while not self._closed.is_set():
            if time.monotonic() >= next_beat:
                self._beat(asset_id)
                next_beat = time.monotonic() + GCS_HEARTBEAT_S
            try:
                message = link.recv_match(blocking=True, timeout=0.5)
            except Exception as error:  # noqa: BLE001 — 듣는 스레드가 죽으면 옛 상태를 계속 보여 줍니다
                if self._closed.is_set():
                    return
                print(f"mavlink {asset_id}: {error!r}", flush=True)
                time.sleep(0.5)
                continue
            if message is not None:
                self._route(asset_id, message)

    def _beat(self, asset_id: str) -> None:
        """지상국 하트비트. udpin 은 아직 말을 걸어 온 쪽이 없으면 조용히 버립니다."""
        mav = self._mav
        try:
            with self._writes[asset_id]:
                self.links[asset_id].mav.heartbeat_send(
                    mav.MAV_TYPE_GCS, mav.MAV_AUTOPILOT_INVALID, 0, 0, mav.MAV_STATE_ACTIVE)
        except OSError:
            pass  # 받는 쪽이 아직 없으면(udpout 의 ICMP 거절) 다음 박동에 다시

    def _route(self, asset_id: str, message) -> None:
        kind = message.get_type()
        if kind == "COMMAND_ACK":
            self._acks[asset_id].put(message)
            return
        if kind in MISSION_REPLIES:
            self._mission_replies[asset_id].put(message)
            return
        if kind == "HEARTBEAT":
            if not self._from_vehicle(message):
                return
            with self._guard:
                self._targets[asset_id] = (message.get_srcSystem(), message.get_srcComponent())
                self._autopilot[asset_id]["last_beat"] = time.monotonic()
            self._ready[asset_id].set()
        self._absorb(asset_id, message)

    def _from_vehicle(self, message) -> bool:
        """기체의 하트비트인가. 지상국·카메라 같은 다른 구성품의 것은 대상으로 삼지 않습니다."""
        return (message.type != self._mav.MAV_TYPE_GCS
                and message.autopilot != self._mav.MAV_AUTOPILOT_INVALID)

    def _absorb(self, asset_id: str, message) -> None:
        kind = message.get_type()
        with self._guard:
            entry = self._state[asset_id]
            extra = self._autopilot[asset_id]
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
                entry["armed"] = bool(message.base_mode & self._mav.MAV_MODE_FLAG_SAFETY_ARMED)
                entry["state"] = self._read_state(entry)
                entry["autonomy_health"] = 1.0 if entry.get("guided", True) else 0.0
                extra["mode"] = self._mode_name(message)
            elif kind == "EXTENDED_SYS_STATE":
                extra["landed"] = LANDED_STATES.get(message.landed_state)
            elif kind == "MISSION_CURRENT":
                extra["mission_seq"] = int(message.seq)
            elif kind == "MISSION_ITEM_REACHED":
                extra["reached_seq"] = int(message.seq)
            elif kind == "STATUSTEXT":
                text = message.text
                text = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
                extra["status_text"].append(text.rstrip("\x00"))
            entry.setdefault("model", os.getenv("VEHICLE_MODEL", "px4-sitl"))
            entry.setdefault("kind", "drone")
            entry.setdefault("battery", 100.0)
            entry.setdefault("vibration", 0.0)
            entry.setdefault("autonomy_health", 1.0)
            entry.setdefault("passengers", 0)
            entry.setdefault("assigned_pad", None)

    def _mode_name(self, message) -> str:
        if message.autopilot == self._mav.MAV_AUTOPILOT_PX4:
            return px4_mode_name(message.custom_mode)
        return self._mavutil.mode_string_v10(message)

    @staticmethod
    def _read_state(entry: dict) -> str:
        if not entry.get("armed"):
            return "landed" if (entry.get("alt_m") or 0.0) < 1.0 else "grounded"
        return "approaching" if entry.get("assigned_pad") else "cruising"

    def telemetry(self) -> dict:
        with self._guard:
            self._tick += 1
            return {"tick": self._tick, "assets": {k: dict(v) for k, v in self._state.items()}}

    def airborne(self, asset_id: str) -> bool:
        """떠 있나. PX4 의 착지 판정(EXTENDED_SYS_STATE)이 먼저이고, 없으면 시동과 고도로 봅니다."""
        with self._guard:
            landed = self._autopilot[asset_id]["landed"]
            entry = self._state[asset_id]
            armed, alt = bool(entry.get("armed")), float(entry.get("alt_m") or 0.0)
        if landed is not None:
            return landed != "on_ground"
        return armed and alt > AIRBORNE_M

    def altitude(self, asset_id: str) -> float:
        with self._guard:
            return float(self._state[asset_id].get("alt_m") or 0.0)

    def link_up(self, asset_id: str, within_s: float = LINK_STALE_S) -> bool:
        """within_s 안에 하트비트를 들었나. 한 번도 못 들었으면 언제나 False 입니다."""
        with self._guard:
            beat = self._autopilot[asset_id]["last_beat"]
        return beat is not None and time.monotonic() - beat < within_s

    def autopilot_view(self, asset_id: str) -> dict:
        """화면에 내보낼 자동조종 한 대의 상태. 읽기만 합니다."""
        now = time.monotonic()
        with self._guard:
            entry, extra = self._state[asset_id], self._autopilot[asset_id]
            beat = extra["last_beat"]
            since = None if beat is None else round(now - beat, 1)
            link = "waiting" if since is None else ("up" if since < LINK_STALE_S else "lost")
            return {
                "endpoint": self.endpoints[asset_id], "link": link, "last_heartbeat_s": since,
                "lat": entry.get("lat"), "lon": entry.get("lon"), "alt_m": entry.get("alt_m"),
                "armed": entry.get("armed"), "mode": extra["mode"], "landed": extra["landed"],
                "battery": entry.get("battery"), "mission_seq": extra["mission_seq"],
                "reached_seq": extra["reached_seq"], "status_text": list(extra["status_text"]),
            }

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
        if action in NO_AUTOPILOT_COMMAND:
            return {"ok": True, "note": NO_AUTOPILOT_COMMAND[action]}
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            # 지상 설비는 자동조종 소관이 아닙니다. 런타임이 원장에만 남깁니다.
            return {"ok": True, "note": f"{action} is ground equipment, no autopilot command"}
        if not self._ready[asset_id].wait(timeout=self.link_timeout_s):
            return {"ok": False, "error": f"{asset_id} autopilot has not reported in"}
        return handler(link, asset_id, params)

    def _do_fly_route(self, link, asset_id: str, params: dict) -> dict:
        return self.fly_route(asset_id, params.get("legs") or [])

    def _do_reserve_pad(self, link, asset_id: str, params: dict) -> dict:  # noqa: D401
        # 고장 기체의 착륙대 경로. 런타임이 legs 를 주면 그대로 임무로 갑니다.
        if params.get("legs"):
            return self.fly_route(asset_id, params["legs"])
        pad = params.get("pad")
        target = PAD_COORDS.get(pad)
        if target is None:
            return {"ok": False, "error": f"unknown pad {pad}"}
        with self._guard:
            self._state[asset_id]["assigned_pad"] = pad
        with self._writes[asset_id]:
            link.mav.set_position_target_global_int_send(
                0, *self._targets[asset_id],
                self._mav.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                0b0000111111111000,
                int(target[0] * 1e7), int(target[1] * 1e7), DEPOT_ALT_M,
                0, 0, 0, 0, 0, 0, 0, 0,
            )
        return {"ok": True, "sent": "set_position_target", "pad": pad}

    def _do_land(self, link, asset_id: str, params: dict) -> dict:
        return self.land_here(asset_id)

    def _do_divert_ground(self, link, asset_id: str, params: dict) -> dict:
        with self._guard:
            self._state[asset_id]["assigned_pad"] = None
        return self.recall(asset_id, params.get("exit"))

    def _do_disengage_autonomy(self, link, asset_id: str, params: dict) -> dict:
        """자동 조종을 놓고 사람에게 넘깁니다. 이 한 줄이 승객을 세우는 행동입니다."""
        with self._guard:
            self._state[asset_id]["guided"] = False
        return self._command(asset_id, self._mav.MAV_CMD_DO_SET_MODE,
                             self._mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)

    # ---------- 임무 수준의 명령 (거울도 이것들을 부릅니다) ----------

    def fly_route(self, asset_id: str, legs: list[dict]) -> dict:
        """승인된 경로를 임무로 올리고 바로 시작합니다(단독 배선). 거울은 둘을 따로 부릅니다."""
        items = route_items(legs, self.airborne(asset_id))
        if not items:
            return {"ok": False, "error": "a route needs at least two legs"}
        upload = self.upload_mission(asset_id, items)
        if not upload["ok"]:
            return upload
        return {**self.start_mission(asset_id, arm=not self.airborne(asset_id)),
                "items": len(items)}

    def recall(self, asset_id: str, exit_point: dict | None) -> dict:
        """승인 회수. 떠 있으면 런타임이 준 바깥 자리(exit)로 가서 내립니다.

        바깥 자리가 없으면(구역 밖에서 회수) 그 자리에 내립니다. 땅에 있으면 올려 둔 임무를
        지웁니다 — 뜨지 않은 기체는 그 임무로 시동이 걸릴 길이 없어집니다.
        """
        if not self.airborne(asset_id):
            return {**self.clear_mission(asset_id), "did": "cleared"}
        if exit_point and exit_point.get("lat") is not None and exit_point.get("lon") is not None:
            items = exit_items(exit_point, max(self.altitude(asset_id), AIRBORNE_M))
            upload = self.upload_mission(asset_id, items)
            if not upload["ok"]:
                return {**upload, "did": "exit_mission", "items": items, "uploaded": False}
            return {**self.start_mission(asset_id, arm=False), "did": "exit_mission",
                    "items": items, "uploaded": True}
        return {**self.land_here(asset_id), "did": "land_here"}

    def upload_mission(self, asset_id: str, items: list[dict]) -> dict:
        """MISSION_COUNT → (MISSION_REQUEST_INT n → MISSION_ITEM_INT n)… → MISSION_ACK.

        자동조종이 항목을 하나씩 달라고 합니다. 같은 번호를 다시 달라면 다시 보냅니다(잃어버린 것).
        끝이 있습니다: 항목 하나를 MISSION_ITEM_RESENDS 번 넘게 다시 달라거나, 전체가 답 하나의
        기다림 × (항목 수 + MISSION_COUNT 재시도)를 넘기면 접습니다. 끝이 없던 때는 같은 번호만
        끝없이 달라는 자동조종에 작업 스레드가 붙잡혀, 뒤에 줄 선 회수가 영영 안 나갔습니다
        (실측: 25초에 항목 1,866개를 보냈고 회수는 한 번도 안 나감).
        """
        if not items:
            return {"ok": False, "error": "empty mission"}
        replies = self._mission_replies[asset_id]
        deadline = time.monotonic() + self.ack_timeout_s * (len(items) + MISSION_COUNT_TRIES)
        with self._operations[asset_id]:
            _drain(replies)
            sent: dict[int, int] = {}
            tries = 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"ok": False, "error": "mission upload did not finish in time",
                            "count": len(items), "asked": len(sent)}
                if not sent:
                    if tries >= MISSION_COUNT_TRIES:
                        return {"ok": False, "error": "autopilot never asked for the mission items",
                                "count": len(items)}
                    self._send(asset_id, "mission_count_send", len(items))
                    tries += 1
                try:
                    reply = replies.get(timeout=min(self.ack_timeout_s, left))
                except queue.Empty:
                    if sent:
                        return {"ok": False, "error": "autopilot stopped asking for mission items",
                                "count": len(items), "asked": len(sent)}
                    continue
                if reply.get_type() == "MISSION_ACK":
                    accepted = reply.type == self._mav.MAV_MISSION_ACCEPTED
                    return {"ok": accepted, "result": int(reply.type), "count": len(items)}
                if 0 <= reply.seq < len(items):
                    sent[reply.seq] = sent.get(reply.seq, 0) + 1
                    if sent[reply.seq] > MISSION_ITEM_RESENDS + 1:
                        return {"ok": False,
                                "error": f"autopilot kept asking for mission item {reply.seq}",
                                "count": len(items), "asked": len(sent)}
                    self._send_item(asset_id, reply.seq, items[reply.seq])

    def _send_item(self, asset_id: str, seq: int, item: dict) -> None:
        mav = self._mav
        command = {"takeoff": mav.MAV_CMD_NAV_TAKEOFF, "waypoint": mav.MAV_CMD_NAV_WAYPOINT,
                   "land": mav.MAV_CMD_NAV_LAND}[item["command"]]
        # 고도는 이륙 자리(home) 기준. 시뮬의 고도도 땅 기준이고, SIH 의 땅은 home 높이입니다.
        # 방향(param4)은 NaN — 자동조종이 진행 방향을 스스로 봅니다.
        self._send(asset_id, "mission_item_int_send", seq, mav.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                   command, 0, 1, 0.0, 0.0, 0.0, math.nan,
                   int(round(item["lat"] * 1e7)), int(round(item["lon"] * 1e7)),
                   float(item["alt_m"]))

    def start_mission(self, asset_id: str, arm: bool = True) -> dict:
        """AUTO.MISSION 으로 두고, (땅이면) 시동을 걸고, 첫 항목부터 시작합니다.

        한 단계라도 거절되면 거기서 멈추고 그 답을 그대로 돌려줍니다. 승인은 우리가 했어도
        지금 뜰 수 있는지는 자동조종이 정합니다.
        """
        mav = self._mav
        steps = [("mode", mav.MAV_CMD_DO_SET_MODE,
                  (mav.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, PX4_MAIN_AUTO, PX4_AUTO_MISSION))]
        if arm:
            steps.append(("arm", mav.MAV_CMD_COMPONENT_ARM_DISARM, (1,)))
        steps.append(("start", mav.MAV_CMD_MISSION_START, (0, 0)))
        answered: dict[str, object] = {}
        with self._operations[asset_id]:
            for name, command, params in steps:
                reply = self._command(asset_id, command, *params)
                answered[name] = reply.get("result", "no answer")
                if not reply["ok"]:
                    why = "refused" if "result" in reply else "did not answer"
                    return {"ok": False, "error": f"autopilot {why} {name}",
                            "result": reply.get("result"), "steps": answered}
        return {"ok": True, "steps": answered}

    def clear_mission(self, asset_id: str) -> dict:
        replies = self._mission_replies[asset_id]
        with self._operations[asset_id]:
            _drain(replies)
            self._send(asset_id, "mission_clear_all_send")
            deadline = time.monotonic() + self.ack_timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    reply = replies.get(timeout=remaining)
                except queue.Empty:
                    break
                if reply.get_type() == "MISSION_ACK":
                    return {"ok": reply.type == self._mav.MAV_MISSION_ACCEPTED,
                            "result": int(reply.type)}
        return {"ok": False, "error": "autopilot did not acknowledge the clear"}

    def land_here(self, asset_id: str) -> dict:
        # 위치(param5·6)는 NaN — 지금 자리에 내립니다. 0 을 넣으면 위도 0·경도 0 으로 읽힙니다.
        nan = math.nan
        return self._command(asset_id, self._mav.MAV_CMD_NAV_LAND, 0, 0, 0, nan, nan, nan, nan)

    def _send(self, asset_id: str, name: str, *args) -> None:
        with self._guard:
            target = self._targets[asset_id]
        with self._writes[asset_id]:
            getattr(self.links[asset_id].mav, name)(*target, *args)

    def _command(self, asset_id: str, command: int, *params: float) -> dict:
        """자동조종이 거절할 수도 있습니다. 승인은 우리가, 수락은 자동조종이 합니다."""
        values = [float(value) for value in params] + [0.0] * (7 - len(params))
        acks = self._acks[asset_id]
        with self._operations[asset_id]:
            _drain(acks)  # 지난 응답을 먼저 비웁니다
            self._send(asset_id, "command_long_send", command, 0, *values)
            deadline = time.monotonic() + self.ack_timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    ack = acks.get(timeout=remaining)
                except queue.Empty:
                    break
                if ack.command != command or ack.result == self._mav.MAV_RESULT_IN_PROGRESS:
                    continue  # 다른 명령의 늦은 응답이거나 아직 하는 중
                accepted = ack.result == self._mav.MAV_RESULT_ACCEPTED
                return {"ok": accepted, "result": int(ack.result)}
        return {"ok": False, "error": "autopilot did not acknowledge"}


def from_env() -> "MavlinkFleetAdapter":
    """MAVLINK_ENDPOINTS='drone-01=udpin:0.0.0.0:14540,drone-02=udpin:0.0.0.0:14541'"""
    raw = os.environ["MAVLINK_ENDPOINTS"]
    endpoints = dict(pair.split("=", 1) for pair in raw.split(","))
    return MavlinkFleetAdapter(endpoints)
