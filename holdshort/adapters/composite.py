"""The simulated city stays the world of record; one guarded aircraft is also flown by a real PX4.

The simulator decides everything the runtime judges with — positions, the cargo cycle, the
scoreboards — for all four aircraft. One of them (MAVLINK_MIRROR, default drone-01) is also
flown by a PX4 autopilot. Every command the runtime executes for that aircraft goes to the
simulator first and then to PX4. The simulator's answer is the answer the runtime gets; PX4's
answer is written beside it and never changes a verdict.

PX4 is a mirror that proves the command path, not the source of truth: the same cleared route
the map shows is uploaded as a real mission, a recall or a weather hold reaches a real
autopilot, a refused filing never arms it. Nothing judged is read back from PX4.
"""

import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path

from holdshort.adapters.fleet_sim import FleetSimAdapter
from holdshort.adapters.mavlink_fleet import route_items

DEFAULT_MIRROR = "drone-01"
DEFAULT_ENDPOINT = "udpin:0.0.0.0:14540"
# 시뮬 기체가 이 고도(m)를 넘으면 떠난 것으로 봅니다. PX4 는 그때 시동을 겁니다.
LIFTOFF_M = 1.0
# 거울의 명령은 일꾼 스레드 하나가 순서대로 보냅니다. 줄이 이만큼 차면 새 명령은 버리고 적습니다 —
# 세계 스레드가 PX4 를 기다리는 일은 없어야 합니다.
JOB_QUEUE_LIMIT = 32
COMMAND_LOG_KEEP = 16
# 일꾼이 명령을 보내도 되는 링크: 이 시간(초) 안에 하트비트를 들은 자동조종. 화면의 lost 기준보다
# 넉넉합니다 — 하트비트 한두 번을 잃었다고 시동 명령을 버렸더니 거울 비행이 통째로 사라졌습니다.
# 정말 말이 없는 자동조종은 단계마다의 응답 시한(ACK)이 걸러 냅니다.
LINK_SEND_S = 15.0
# 경로를 싣고 오는 행동. reserve_pad 는 고장 때만 오고, legs 가 있으면 그 경로로 갑니다.
ROUTE_ACTIONS = ("fly_route", "reserve_pad")
# 그대로 자동조종에 넘기는 행동. 나머지(짐 싣기·거절·충전)는 땅의 일이라 보낼 것이 없습니다.
PASS_THROUGH = ("land", "disengage_autonomy")
NO_COMMAND_WHY = {
    "depart": "짐 싣기는 땅의 일 — 뜨는 것은 경로 임무가 합니다",
    "decline_job": "배달을 안 받는 것은 운영사의 일",
}
GROUND_WORK_WHY = "지상 설비 — 자동조종에 보낼 명령이 없습니다"


class AutopilotJournal:
    """PX4 의 답을 적는 줄 파일(JSONL). 원장 옆에 둡니다.

    원장 파일에 적지 않는 이유: PX4 의 답은 원장 줄이 닫힌 뒤에 옵니다. 같은 원장 번호로 줄을
    하나 더 쓰면 화면의 최근 기록과 보고서의 비행 접기가 같은 결정을 두 번 셉니다.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as error:
                print(f"autopilot journal: {error!r}", flush=True)


class AutopilotMirror:
    """기체 한 대의 PX4 거울.

    세계 스레드는 할 일을 줄에 놓고 곧바로 돌아갑니다. 일꾼 스레드 하나가 그 줄을 순서대로
    자동조종에 보내고, 돌아온 답을 기록합니다. 판정도 원장의 성패도 이 답을 기다리지 않습니다.
    """

    def __init__(self, asset_id: str, autopilot, journal: AutopilotJournal | None = None):
        self.asset_id = asset_id
        self.autopilot = autopilot
        self.journal = journal
        self._jobs: queue.Queue = queue.Queue(maxsize=JOB_QUEUE_LIMIT)
        self._guard = threading.Lock()
        self._commands: deque = deque(maxlen=COMMAND_LOG_KEEP)
        self._mission: dict | None = None
        # 올려 두고 시뮬 기체가 뜨기를 기다리는 경로(원장 번호). 짐 싣기·승인 확인·미룬 출발은
        # 기록의 세계에서 일어납니다. 올리자마자 시동을 걸면 지도의 기체가 아직 짐을 싣는 동안
        # PX4 가 먼저 떠나 버립니다.
        self._pending_start: str | None = None
        self._world_airborne = False
        threading.Thread(target=self._work, daemon=True, name=f"mirror-{asset_id}").start()

    # ---------- 세계 스레드 쪽. 여기서는 아무것도 기다리지 않습니다 ----------

    def submit(self, action: str, params: dict, ledger_id: str) -> dict:
        # 줄에 놓는 순간의 경로를 복사해 둡니다. 일꾼이 보낼 때 원본이 바뀌어 있으면 판정된 것과
        # 다른 경로가 올라갑니다.
        legs = [dict(leg) for leg in params.get("legs") or []]
        if action in ROUTE_ACTIONS and legs:
            with self._guard:
                start_now = self._world_airborne
                self._pending_start = None if start_now else ledger_id
            return self._enqueue(ledger_id, action,
                                 lambda: self._fly(ledger_id, legs, start_now))
        if action == "divert_ground":
            with self._guard:
                self._pending_start = None  # 뜨기 전에 회수된 경로는 시동까지 가지 않습니다
            exit_point = params.get("exit")
            return self._enqueue(ledger_id, action, lambda: self._recall(ledger_id, exit_point))
        if action in PASS_THROUGH:
            return self._enqueue(ledger_id, action,
                                 lambda: self._pass_through(action, params, ledger_id))
        return self._record(ledger_id, action, True, "no_command",
                            NO_COMMAND_WHY.get(action, GROUND_WORK_WHY))

    def skip(self, ledger_id: str, action: str, why: str) -> dict:
        """기록의 세계가 받지 않은 명령. 거기서 일어나지 않은 일은 거울에서도 일어나지 않습니다."""
        return self._record(ledger_id, action, None, "not_sent", why)

    def observe_world(self, asset_state: dict) -> None:
        """시뮬 기체의 텔레메트리 한 줄. 그 기체가 뜨는 순간 기다리던 경로를 시작합니다."""
        airborne = float(asset_state.get("alt_m") or 0.0) > LIFTOFF_M
        with self._guard:
            self._world_airborne = airborne
            ready = self._pending_start if airborne else None
            if ready is not None:
                self._pending_start = None
        if ready is not None:
            self._enqueue(ready, "start", lambda: self._start(ready))

    def view(self) -> dict:
        base = self.autopilot.autopilot_view(self.asset_id)
        with self._guard:
            mission = dict(self._mission) if self._mission else None
            return {**base, "role": "mirror", "world_airborne": self._world_airborne,
                    "pending_start": self._pending_start, "mission": mission,
                    "commands": list(self._commands)}

    def _enqueue(self, ledger_id: str, action: str, job) -> dict:
        try:
            self._jobs.put_nowait((ledger_id, action, job))
        except queue.Full:
            return self._record(ledger_id, action, False, "dropped",
                                "PX4 일꾼이 밀려 있어 보내지 않았습니다")
        return {"ok": None, "result": "queued", "detail": "PX4 로 보내는 중"}

    # ---------- 일꾼 스레드 쪽 ----------

    def _work(self) -> None:
        while True:
            ledger_id, action, job = self._jobs.get()
            if not self.autopilot.link_up(self.asset_id, within_s=LINK_SEND_S):
                # 끊긴 링크에 보내면 단계마다 응답 시한을 기다려 뒤의 명령이 줄을 섭니다.
                self._record(ledger_id, action, False, "link_down", "PX4 하트비트가 없습니다")
                continue
            try:
                ok, result, detail = job()
            except Exception as error:  # noqa: BLE001 — 일꾼이 죽으면 이후 명령이 조용히 사라집니다
                ok, result, detail = False, "error", repr(error)
            self._record(ledger_id, action, ok, result, detail)

    def _fly(self, ledger_id: str, legs: list[dict], start_now: bool):
        items = route_items(legs, self.autopilot.airborne(self.asset_id))
        upload = self.autopilot.upload_mission(self.asset_id, items)
        with self._guard:
            self._mission = {"ledger_id": ledger_id, "items": items,
                             "uploaded": bool(upload["ok"]), "started": False}
        if not upload["ok"]:
            return False, "upload_failed", upload
        if not start_now:
            return True, "uploaded", {"items": len(items), "start": "시뮬 기체가 뜰 때"}
        return self._start(ledger_id)

    def _start(self, ledger_id: str):
        with self._guard:
            mission = self._mission
        if mission is None or mission["ledger_id"] != ledger_id or not mission["uploaded"]:
            return False, "not_started", "이 경로의 임무가 올라가 있지 않습니다"
        reply = self.autopilot.start_mission(
            self.asset_id, arm=not self.autopilot.airborne(self.asset_id))
        with self._guard:
            mission["started"] = bool(reply["ok"])
        return bool(reply["ok"]), "started" if reply["ok"] else "start_refused", reply

    def _recall(self, ledger_id: str, exit_point: dict | None):
        reply = self.autopilot.recall(self.asset_id, exit_point)
        did = reply.get("did", "recall")
        with self._guard:
            if did == "exit_mission":
                self._mission = {"ledger_id": ledger_id, "items": reply.get("items") or [],
                                 "uploaded": bool(reply.get("uploaded")),
                                 "started": bool(reply["ok"])}
            elif did == "cleared" and reply["ok"]:
                self._mission = None
        return bool(reply["ok"]), did, reply

    def _pass_through(self, action: str, params: dict, ledger_id: str):
        reply = self.autopilot.execute(self.asset_id, action, params, ledger_id)
        return bool(reply.get("ok")), "sent", reply

    def _record(self, ledger_id: str, action: str, ok, result: str, detail) -> dict:
        outcome = {"ok": ok, "result": result, "detail": detail}
        record = {"ledger_id": ledger_id, "asset": self.asset_id, "action": action,
                  **outcome, "at": round(time.time(), 3)}
        with self._guard:
            self._commands.appendleft(record)
        if self.journal is not None:
            self.journal.write(record)
        return outcome


class CompositeAdapter:
    """시뮬레이터(기록의 세계) + PX4 거울 한 대. 런타임에게는 어댑터 하나로 보입니다."""

    def __init__(self, world, mirror: AutopilotMirror):
        self.world = world
        self.mirror = mirror

    def execute(
        self,
        asset_id: str,
        action: str,
        params: dict,
        ledger_id: str,
        blast: str = "none",
        approved_by: str | None = None,
    ) -> dict:
        # 기록의 세계가 먼저입니다. 그 답이 런타임이 받는 답이고 원장의 성패를 정합니다.
        result = self.world.execute(asset_id, action, params, ledger_id, blast=blast,
                                    approved_by=approved_by)
        if asset_id != self.mirror.asset_id:
            return result
        if not result.get("ok"):
            return {**result, "autopilot": self.mirror.skip(ledger_id, action,
                                                            "시뮬레이터가 받지 않았습니다")}
        return {**result, "autopilot": self.mirror.submit(action, dict(params or {}), ledger_id)}

    def telemetry(self) -> dict:
        # 판정에 쓰는 텔레메트리는 시뮬레이터의 것입니다. PX4 의 위치는 /state.autopilots 로만.
        state = self.world.telemetry()
        seen = ((state or {}).get("assets") or {}).get(self.mirror.asset_id)
        if seen is not None:
            self.mirror.observe_world(seen)
        return state

    def autopilots(self) -> dict:
        """/state.autopilots. 읽기 전용 — 판정은 이 값을 보지 않습니다."""
        return {self.mirror.asset_id: self.mirror.view()}


def from_env(sim_url: str, world: str = "guarded", journal_path: str | None = None):
    """ADAPTER=composite. MAVLINK_MIRROR(기본 drone-01), MAVLINK_ENDPOINT(기본 udpin:0.0.0.0:14540).

    pymavlink 이 없거나 포트를 못 열면 시뮬레이터만으로 돕니다. 거울이 없다고 판이 멈춰서는
    안 됩니다 — 거울은 명령 경로를 증명할 뿐이고, 기록의 세계는 시뮬레이터입니다.
    """
    sim = FleetSimAdapter(sim_url, world=world)
    asset = os.getenv("MAVLINK_MIRROR") or DEFAULT_MIRROR
    endpoint = os.getenv("MAVLINK_ENDPOINT") or DEFAULT_ENDPOINT
    try:
        from holdshort.adapters.mavlink_fleet import MavlinkFleetAdapter

        autopilot = MavlinkFleetAdapter({asset: endpoint}, world=world, link_timeout_s=1.0)
    except (ImportError, OSError) as error:
        print(f"composite: PX4 거울 없이 시뮬레이터만 ({error!r})", flush=True)
        return sim
    journal = AutopilotJournal(journal_path) if journal_path else None
    print(f"composite: {asset} 는 PX4({endpoint})도 같이 납니다 — 판정은 시뮬레이터", flush=True)
    return CompositeAdapter(sim, AutopilotMirror(asset, autopilot, journal))
