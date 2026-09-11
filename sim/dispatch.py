"""Who takes which delivery, and in what order. The operator's call, never the runtime's.

The runtime judges routes. It does not decide which drone serves which customer — that is
fleet dispatch, and dispatch is the operator's business (here the simulator plays the
operator's order desk). Putting it in the runtime would make the runtime the operator, and
then the party handing out the work would also be the party saying the work is safe.

Two dispatchers share one interface, `next_stop(world, vehicle) -> landing area | None`:

- RuleDispatcher — the rule this world always used, moved here unchanged from
  World._assign_job, so seed 7 plays exactly the same round as before.
- CuOptDispatcher — posts the fleet and the open orders to an NVIDIA cuOpt server (open
  source, Apache-2.0) and follows the vehicle routes it returns. cuOpt solves on a GPU, and
  there is none on this laptop, so it runs on a Nebius AI Cloud instance: it stays off unless
  CUOPT_URL (or configs/fleet.yaml dispatch.solver: cuopt) says where the server is. Every
  failure — no server, a timeout, an infeasible or malformed answer, an unknown id — falls
  back to the rule for that stop and backs off before asking again, because the simulator's
  clock thread is what calls this and a stalled dispatcher would stop the world.

Either way the aircraft still has to ask the runtime for a route to whatever stop it is
given. A better dispatch plan changes the order book, never the clearance.
"""

import math
import os
import random
import threading
import time
import weakref
from pathlib import Path

from holdshort.core.http import get_json, post_json_status
from sim.world import (
    CRUISE_MPS,
    DEPOT,
    DROP_TICKS,
    LANDING_AREAS,
    PARCELS_PER_STOP,
    SIM_SECONDS_PER_TICK,
    STOPS_PER_TRIP,
    to_latlon,
)

CONFIG_FILE = os.getenv("CONFIG",
                        str(Path(__file__).resolve().parent.parent / "configs/fleet.yaml"))
# 세계 스레드가 cuOpt 를 기다리는 최대 시간(초). 이만큼은 틱이 멈추므로 짧아야 합니다.
CUOPT_TIMEOUT_S = 2.0
# 서버가 푸는 데 쓰는 시간(초). 정차 30곳·기체 4대짜리 문제라 1초면 충분합니다.
CUOPT_TIME_LIMIT_S = 1.0
# 한 번 실패하면 이만큼은 묻지 않고 규칙으로 돕니다. 서버가 죽었는데 매 정차마다 2초씩
# 기다리면 화면이 그만큼 끊깁니다.
CUOPT_BACKOFF_S = 60.0
POLL_S = 0.05
# cuOpt 서버가 요구하는 헤더. 값은 클라이언트 판본이고 서버는 있는지만 봅니다.
CLIENT_VERSION = "custom"


class RuleDispatcher:
    """예전 World._assign_job 그대로입니다. 한 줄도 바꾸지 않았습니다 — 씨앗 7 의 판이
    같아야 합니다.

    첫 정차는 아무 착륙장이고, 두 번째는 첫 정차에서 가까운 여섯 곳 중 하나입니다. 먼 두 곳을
    연달아 찍으면(할렘 → 배터리파크) 한 바퀴가 한 판을 넘깁니다. 실제 배차도 한 번 나가서는
    같은 동네를 돕니다. 다른 기체가 지금 가고 있는 착륙장은 피합니다 — 착륙장은 한 번에 한
    대라(런타임 규칙), 같은 곳을 두 대가 잡으면 뒤의 기체가 앞 기체가 떠날 때까지 첫 정차에서
    1300틱을 앉아 있었습니다.
    """

    name = "rules"

    def next_stop(self, world, vehicle) -> dict | None:
        taken = _taken_by_others(world, vehicle)
        pool = [area for area in LANDING_AREAS
                if area["name"] != vehicle.job_label and area["name"] not in taken]
        if not pool:
            pool = [area for area in LANDING_AREAS if area["name"] != vehicle.job_label]
        if not pool:
            return None
        if vehicle.stops_left < STOPS_PER_TRIP and vehicle.job_x is not None:
            here_lat, here_lon = to_latlon(vehicle.job_x, vehicle.job_y)
            pool = sorted(pool, key=lambda a: math.hypot((a["lat"] - here_lat) * 110_570,
                                                         (a["lon"] - here_lon) * 84_400))[:6]
        return world._rng.choice(pool)


class CuOptDispatcher:
    """기단의 다음 한 바퀴를 cuOpt 에게 풉니다: 어느 기체가 어느 착륙장을 어떤 순서로.

    한 정차씩 묻지 않고 한 번에 짭니다(그게 VRP 솔버가 잘하는 일입니다). 받은 계획은 기체마다
    줄로 들고 있다가 하나씩 꺼내 줍니다. 줄이 비면 다시 풉니다. 무엇이든 어긋나면 그 정차는
    규칙이 정합니다 — 배차가 멈추면 기단이 서는데, 배차는 안전이 아니라 일감이라 멈출 이유가
    없습니다.
    """

    name = "cuopt"

    def __init__(self, url: str, timeout_s: float = CUOPT_TIMEOUT_S,
                 time_limit_s: float = CUOPT_TIME_LIMIT_S, backoff_s: float = CUOPT_BACKOFF_S,
                 seed: int = 7, fallback=None):
        self.url = (url or "").rstrip("/")
        self.timeout_s = float(timeout_s)
        self.time_limit_s = float(time_limit_s)
        self.backoff_s = float(backoff_s)
        # 주문서를 뽑는 주사위. 세계의 것(world._rng)을 쓰지 않습니다 — 규칙 배차가 그것을 쓰므로,
        # 섞어 쓰면 cuOpt 를 켜고 끄는 것만으로 규칙 배차의 판까지 달라집니다.
        self.rng = random.Random(seed + 4242)
        self.fallback = fallback or RuleDispatcher()
        self.queues: dict[str, list[str]] = {}
        self.skip_until = 0.0
        self.stats = {"solved": 0, "served": 0, "fell_back": 0, "last_error": None}
        self._lock = threading.Lock()
        # 지금 cuOpt 와 주고받는 작업 스레드. 마감에 걸려 버린 것이 아직 돌면 새로 걸지 않습니다.
        self._asking: threading.Thread | None = None

    def next_stop(self, world, vehicle) -> dict | None:
        with self._lock:
            area = self._from_queue(world, vehicle)
            if area is not None:
                self.stats["served"] += 1
                return area
            if time.monotonic() < self.skip_until:
                return self._fall_back(world, vehicle, None)
            plan = self._solve(world, vehicle)
            if plan is None:
                self.skip_until = time.monotonic() + self.backoff_s
                return self._fall_back(world, vehicle, None)
            self.queues.update(plan)
            self.stats["solved"] += 1
            area = self._from_queue(world, vehicle)
            if area is None:
                return self._fall_back(world, vehicle, "no stop for this aircraft")
            self.stats["served"] += 1
            return area

    # ---------- 계획 ----------

    def _from_queue(self, world, vehicle) -> dict | None:
        """이 기체의 줄에서 아직 쓸 수 있는 첫 정차. 그새 남이 가고 있는 곳은 건너뜁니다."""
        taken = _taken_by_others(world, vehicle)
        queue = self.queues.get(vehicle.id) or []
        while queue:
            name = queue.pop(0)
            if name in taken or name == vehicle.job_label:
                continue
            area = _area_named(name)
            if area is not None:
                return area
        return None

    def _solve(self, world, vehicle):
        """{기체: [착륙장 이름, ...]} 또는 None. 여기서 실패는 규칙으로 넘어간다는 뜻뿐입니다."""
        crew = [vehicle] + [other for other in world.vehicles.values()
                            if other is not vehicle and not self.queues.get(other.id)
                            and not other.job_label]
        stops = {member.id: _stops_for(member) for member in crew}
        orders = self._orders(world, vehicle, sum(stops.values()))
        if not orders:
            self.stats["last_error"] = "no open landing areas to dispatch"
            return None
        body = self._problem(crew, stops, orders)
        answer = self._ask(body)
        if answer is None:
            return None
        return self._plan_from(answer, crew, stops, orders)

    def _orders(self, world, vehicle, count: int) -> list[dict]:
        """이번에 짤 주문서. 지금 누가 가고 있는 착륙장은 뺍니다(한 곳에 한 대)."""
        taken = _taken_by_others(world, vehicle) | {vehicle.job_label}
        pool = [area for area in LANDING_AREAS if area["name"] not in taken]
        if len(pool) < count:
            return []
        return self.rng.sample(pool, count)

    def _problem(self, crew, stops: dict, orders: list[dict]) -> dict:
        """cuOpt 의 자료 모형으로. 0번 자리는 창고입니다 — 한 바퀴는 거기서 끝납니다."""
        places = [to_latlon(*DEPOT)] + [(order["lat"], order["lon"]) for order in orders]
        starts = []
        for member in crew:
            places.append(_vehicle_place(member))
            starts.append(len(places) - 1)
        matrix = [[round(_metres(a, b)) for b in places] for a in places]
        seconds = [[round(cost / CRUISE_MPS) for cost in row] for row in matrix]
        return {
            "cost_matrix_data": {"data": {"0": matrix}},
            "travel_time_matrix_data": {"data": {"0": seconds}},
            "fleet_data": {
                # [출발 자리, 돌아올 자리]. 한 바퀴는 창고에서 끝납니다.
                "vehicle_locations": [[start, 0] for start in starts],
                "vehicle_ids": [member.id for member in crew],
                "capacities": [[stops[member.id] * PARCELS_PER_STOP for member in crew]],
            },
            "task_data": {
                "task_locations": list(range(1, len(orders) + 1)),
                "task_ids": [order["id"] for order in orders],
                "demand": [[PARCELS_PER_STOP] * len(orders)],
                "service_times": [round(DROP_TICKS * SIM_SECONDS_PER_TICK)] * len(orders),
            },
            "solver_config": {"time_limit": self.time_limit_s},
        }

    def _ask(self, body: dict) -> dict | None:
        """cuOpt 에 한 번 묻습니다. 부르는 쪽(세계의 시계 스레드)은 timeout_s 까지만 기다립니다.

        주고받기 전체는 작업 스레드에서 합니다. urllib 의 timeout 은 소켓 읽기 한 번마다라서, 답을
        한 바이트씩 흘리는 서버는 그 몇 배를 붙잡습니다 — 실측: 2초 예산에 21~30초 동안 두 세계가
        멈췄습니다. 마감을 넘기면 규칙으로 가고, 늦게 온 답은 버립니다(기다리던 쪽은 이미 갔습니다).
        """
        if self._asking is not None and self._asking.is_alive():
            self.stats["last_error"] = "the last cuOpt request is still hanging"
            return None
        deadline = time.monotonic() + self.timeout_s
        box: dict = {}
        worker = threading.Thread(target=self._exchange, args=(body, deadline, box),
                                  daemon=True, name="cuopt-ask")
        self._asking = worker
        worker.start()
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive() or "solution" not in box:
            self.stats["last_error"] = box.get("error") or "cuOpt did not answer in time"
            return None
        return box["solution"]

    def _exchange(self, body: dict, deadline: float, box: dict) -> None:
        """POST /cuopt/request → {"reqId"}, 그다음 GET /cuopt/solution/{id} 를 답이 붙을 때까지.

        서버는 다 풀기 전에는 {"reqId": ...} 만 돌려줍니다(200). response 가 붙으면 끝난 것입니다.
        작업 스레드에서 돕니다. 결과는 box 에만 적습니다 — stats 는 세계 스레드의 것이고, 버려진
        스레드가 늦게 적으면 다음 정차의 기록을 덮습니다.
        """
        try:
            status, answer = post_json_status(f"{self.url}/cuopt/request", body,
                                              timeout=max(0.1, deadline - time.monotonic()),
                                              headers={"CLIENT-VERSION": CLIENT_VERSION})
            if status != 200 or not isinstance(answer, dict):
                box["error"] = f"POST /cuopt/request → {status}"
                return
            solution = answer.get("response")
            request_id = answer.get("reqId")
            while solution is None and request_id and time.monotonic() < deadline:
                time.sleep(POLL_S)
                answer = get_json(f"{self.url}/cuopt/solution/{request_id}",
                                  timeout=max(0.1, deadline - time.monotonic())) or {}
                solution = answer.get("response")
            if not isinstance(solution, dict):
                box["error"] = "cuOpt did not answer in time"
                return
            box["solution"] = solution
        except Exception as error:  # noqa: BLE001 - 무엇이 터지든 그 정차는 규칙으로 갑니다
            box["error"] = f"cuOpt request failed: {error!r}"[:200]

    def _plan_from(self, solution: dict, crew, stops: dict, orders: list[dict]):
        """cuOpt 의 답을 기체별 정차 줄로. 모르는 이름이 하나라도 있으면 통째로 버립니다."""
        found = solution.get("solver_response") or {}
        if str(found.get("status")) != "0":
            self.stats["last_error"] = f"solver status {found.get('status')}"
            return None
        by_id = {order["id"]: order for order in orders}
        ids = [member.id for member in crew]
        plan: dict[str, list[str]] = {}
        for key, route in (found.get("vehicle_data") or {}).items():
            vehicle_id = key if key in ids else _by_index(ids, key)
            if vehicle_id is None or not isinstance(route, dict):
                self.stats["last_error"] = f"unknown vehicle {key!r}"
                return None
            names = []
            kinds = route.get("type") or []
            for index, task in enumerate(route.get("task_id") or []):
                kind = kinds[index] if index < len(kinds) else ""
                if task == "Depot" or kind in ("Depot", "Break", "w"):
                    continue
                order = by_id.get(task)
                if order is None:
                    self.stats["last_error"] = f"unknown task {task!r}"
                    return None
                names.append(order["name"])
            plan[vehicle_id] = names[:stops.get(vehicle_id, STOPS_PER_TRIP)]
        if not any(plan.values()):
            self.stats["last_error"] = "cuOpt returned no stops"
            return None
        return plan

    def _fall_back(self, world, vehicle, why: str | None) -> dict | None:
        self.stats["fell_back"] += 1
        if why:
            self.stats["last_error"] = why
        return self.fallback.next_stop(world, vehicle)


# ---------- 어느 배차기를 쓰나 ----------


def settings() -> dict:
    """configs/fleet.yaml 의 dispatch 절. 환경 변수(DISPATCH·CUOPT_*)가 이깁니다.

    런타임은 이 절을 읽지 않습니다 — 배차는 운영사의 일입니다. 파일을 못 읽으면 규칙으로 돕니다.
    설정 하나 때문에 세계가 안 뜨는 일은 없어야 합니다.
    """
    raw = {}
    try:
        import yaml
        text = Path(CONFIG_FILE).read_text(encoding="utf-8")
        raw = (yaml.safe_load(text) or {}).get("dispatch") or {}
    except FileNotFoundError:
        pass    # 시뮬레이터 이미지에는 configs/airspace 만 들어갑니다. 그때는 환경 변수만 봅니다
    except Exception as error:  # noqa: BLE001 - 설정을 못 읽어도 세계는 돌아야 합니다
        print(f"dispatch 설정을 못 읽었습니다 ({error!r}) — 규칙 배차로 돕니다", flush=True)
    return {
        "solver": (os.getenv("DISPATCH") or raw.get("solver") or "rules").strip().lower(),
        "url": (os.getenv("CUOPT_URL") or raw.get("cuopt_url") or "").strip(),
        "timeout_s": _number("CUOPT_TIMEOUT_S", raw.get("timeout_s"), CUOPT_TIMEOUT_S),
        "time_limit_s": _number("CUOPT_TIME_LIMIT_S", raw.get("time_limit_s"),
                                CUOPT_TIME_LIMIT_S),
        "backoff_s": _number("CUOPT_BACKOFF_S", raw.get("backoff_s"), CUOPT_BACKOFF_S),
    }


def build_dispatcher(seed: int = 7):
    found = settings()
    if found["solver"] == "cuopt" and found["url"]:
        print(f"dispatch: cuOpt at {found['url']} (규칙으로 물러설 준비는 되어 있습니다)",
              flush=True)
        return CuOptDispatcher(found["url"], timeout_s=found["timeout_s"],
                               time_limit_s=found["time_limit_s"],
                               backoff_s=found["backoff_s"], seed=seed)
    if found["solver"] == "cuopt":
        print("DISPATCH=cuopt 인데 CUOPT_URL 이 없습니다 — 규칙 배차로 돕니다", flush=True)
    return RuleDispatcher()


_DISPATCHERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def dispatcher_for(world):
    """이 세계의 배차기. 세계마다 하나입니다 — 두 세계가 서로의 계획을 나눠 쓰면 안 됩니다."""
    found = _DISPATCHERS.get(world)
    if found is None:
        found = build_dispatcher(seed=_seed())
        _DISPATCHERS[world] = found
    return found


def _seed() -> int:
    try:
        return int(os.getenv("SEED", "7"))
    except ValueError:
        return 7


def _number(name: str, configured, fallback: float) -> float:
    for value in (os.getenv(name), configured):
        try:
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            continue
    return fallback


def _stops_for(vehicle) -> int:
    """이 기체가 이번에 더 들를 곳의 수."""
    if 0 < vehicle.stops_left < STOPS_PER_TRIP:
        return vehicle.stops_left
    return STOPS_PER_TRIP


def _taken_by_others(world, vehicle) -> set:
    return {other.job_label for other in world.vehicles.values()
            if other is not vehicle and other.job_label}


def _area_named(name: str) -> dict | None:
    return next((area for area in LANDING_AREAS if area["name"] == name), None)


def _vehicle_place(vehicle) -> tuple[float, float]:
    """이 기체가 다음 정차를 시작할 자리: 가고 있는 배달지, 없으면 지금 있는 자리."""
    if vehicle.job_x is not None:
        return to_latlon(vehicle.job_x, vehicle.job_y)
    return to_latlon(vehicle.x, vehicle.y)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * 110_570.0, (b[1] - a[1]) * 84_400.0)


def _by_index(ids: list[str], key) -> str | None:
    """cuOpt 가 기체 이름 대신 번호로 답하는 서버도 있습니다."""
    text = str(key)
    if text.isdigit() and int(text) < len(ids):
        return ids[int(text)]
    return None
