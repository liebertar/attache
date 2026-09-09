"""The world. Deliberately dumb.

The actuator here does exactly what it is told, with no checks of its own. A real landing
pad gate is like this too. That is the whole point: if nothing above it holds the rules,
nothing holds the rules. Both worlds run the same code from the same seed; the only
difference is who is allowed to call act().
"""

import json
import math
import os
import random
import time
from pathlib import Path
from dataclasses import asdict, dataclass, field

from attache.core.geo import Airspace, Volume

# 물류 기지는 브루클린 네이비야드. 배달지는 이스트강 건너 맨해튼입니다.
# 실제 배송 기업이 도심 배달 거점을 두는 자리이고, 강을 건너야 해서 헬리포트 주변
# 0ft 구역을 지나게 됩니다. 그게 이 데모의 전부입니다.
DEPOT = (47.8, 54.9)                       # 브루클린 네이비야드 40.702,-73.970
PADS = {                                    # 충전대 두 자리. 기체는 셋입니다.
    "bay:A": (46.0, 54.9),
    "bay:B": (49.6, 54.9),
}

# 맨해튼. 배터리파크에서 센트럴파크 북단까지, 이스트강 건너 롱아일랜드시티까지.
# 여기를 고른 이유는 FAA 가 격자마다 허용 고도를 공개하기 때문입니다.
# 맨해튼은 전부 금지가 아닙니다. 격자의 40% 가 400ft(122m)까지 허용되고,
# 24% 는 허가 없이 못 납니다. 한 블록 건너 천장이 바뀝니다.
# configs/airspace/nyc.json 이 그 실제 데이터이고, scripts/fetch_airspace.py 가 받아옵니다.
ORIGIN_LAT, ORIGIN_LON = 40.6900, -74.0250
SPAN_LAT, SPAN_LON = 0.1400, 0.1150
CRUISE_ALT_M = 90.0
LOITER_ALT_M = 45.0  # 승인 전 대기 고도

# 격자 한 칸은 정사각형이 아닙니다. 위도 40.7도에서 동서로 약 97 m, 남북으로 약 258 m.
# 격자 단위로 등속 이동하면 남북이 2.7배 빨라집니다 — 브루클린에서 맨해튼으로 가는
# 배달은 대부분 남북이라, 그게 화면에서 보이던 속도의 정체였습니다. 그래서 미터로 움직입니다.
METRES_PER_CELL_X = 97.0
METRES_PER_CELL_Y = 258.0

# 1틱이 나타내는 시간. 화면 재생 속도(TICK_SECONDS)와는 별개입니다.
# 재생이 0.2초면 시뮬레이션 시간이 실제보다 4배 빠르게 흐릅니다.
SIM_SECONDS_PER_TICK = 0.8
CRUISE_MPS = 22.0             # 배달용 멀티로터 순항 속도
CLIMB_MPS = 2.0
DESCENT_MPS = 1.75

STEP_METRES = CRUISE_MPS * SIM_SECONDS_PER_TICK      # 틱당 17.6 m
CLIMB_RATE_M = CLIMB_MPS * SIM_SECONDS_PER_TICK      # 틱당 상승
DESCENT_RATE_M = DESCENT_MPS * SIM_SECONDS_PER_TICK  # 틱당 하강
ARRIVAL_RADIUS_M = 20.0  # 경유점에 이만큼 붙으면 다음 구간으로. 한 틱 이동이 17.6m


def to_latlon(x: float, y: float) -> tuple[float, float]:
    return ORIGIN_LAT + (1.0 - y / 60.0) * SPAN_LAT, ORIGIN_LON + (x / 100.0) * SPAN_LON

COSTS = {
    "decline_job": 0.0,
    "fly_route": 12.0,
    "reserve_pad": 28.0,
    "charge": 22.0,
    "fast_charge": 60.0,
    "divert_ground": 35.0,
    "disengage_autonomy": 0.0,
    "depart": 0.0,
}

# 기체가 기지에 닿는 데 468~605틱 걸립니다(22 m/s). 공지와 구역 폐쇄는
# 그들이 실제로 충전대에 있을 때 도착해야 의미가 있습니다.
RECALL_TICK = 520

# 상시 공역. 한 동네 안에서도 허용 고도가 갈립니다 — 실제 데이터가 그렇게 생겼습니다.
# FAA UAS Facility Map 은 격자마다 천장이 다르고, ED-269 구역은 하한·상한을 갖습니다.
AIRSPACE_FILE = os.getenv(
    "AIRSPACE_FILE", str(Path(__file__).resolve().parent.parent / "configs/airspace/nyc.json")
)


def load_bands() -> list[dict]:
    """같은 등급끼리 합쳐진 덩어리. 화면에 입체로 세울 때 씁니다."""
    try:
        return json.loads(Path(AIRSPACE_FILE).read_text(encoding="utf-8")).get("bands", [])
    except (OSError, json.JSONDecodeError):
        return []


def load_volumes() -> list[dict]:
    """FAA UAS Facility Map. 격자마다 허용 고도가 다릅니다.

    천장 0ft 는 고도 제한이 아니라 '허가 없이는 못 난다'는 뜻이라 금지로 옮겨 담습니다.
    """
    try:
        return json.loads(Path(AIRSPACE_FILE).read_text(encoding="utf-8"))["volumes"]
    except (OSError, KeyError, json.JSONDecodeError):
        return []


STANDING_VOLUMES = load_volumes()
AIRSPACE_BANDS = load_bands()

# 건물. 규정이 아니라 물체지만, 판정에서는 같은 모양입니다 — 땅에서 옥상까지 금지이고
# 그 위는 열려 있습니다. Volume 하나로 그게 그대로 표현되므로 새 판정 코드가 없습니다.
# scripts/fetch_buildings.py 가 뉴욕시 공개 데이터에서 받아옵니다.
BUILDING_FILE = os.getenv(
    "BUILDING_FILE",
    str(Path(__file__).resolve().parent.parent / "configs/airspace/nyc_buildings.json"),
)


def load_buildings() -> list[dict]:
    """건물을 안 넣고 돌릴 수도 있어야 합니다 — 파일이 없으면 빈 목록입니다."""
    if os.getenv("BUILDINGS", "1") == "0":
        return []
    try:
        return json.loads(Path(BUILDING_FILE).read_text(encoding="utf-8"))["volumes"]
    except (OSError, KeyError, json.JSONDecodeError):
        return []


BUILDINGS = load_buildings()

ADDRESS_FILE = os.getenv(
    "ADDRESS_FILE",
    str(Path(__file__).resolve().parent.parent / "configs/airspace/nyc_addresses.json"),
)


def load_addresses() -> list[dict]:
    """배달지는 실제 주소입니다. OpenStreetMap 에서 받아온 맨해튼 건물들입니다."""
    try:
        return json.loads(Path(ADDRESS_FILE).read_text(encoding="utf-8"))["addresses"]
    except (OSError, KeyError, json.JSONDecodeError):
        return []


ADDRESSES = load_addresses()


def to_grid(lat: float, lon: float) -> tuple[float, float]:
    """위경도를 격자로. to_latlon 의 역입니다."""
    return ((lon - ORIGIN_LON) / SPAN_LON * 100.0,
            (1.0 - (lat - ORIGIN_LAT) / SPAN_LAT) * 60.0)


# 배달 서비스 반경. 22 m/s 로 왕복 6분, 항속 24분이면 한 번 충전에 서너 건입니다.
# 실제 드론 배달도 이렇게 운영합니다 — 기지 하나가 도시 전체를 맡지 않습니다.
# 이 반경을 늘리면 왕복이 배터리를 넘어서고, 기단이 기지에 못 돌아옵니다.
SERVICE_RADIUS_M = 4000.0


_SERVICE_AREA: list[dict] | None = None


def pickable_addresses() -> list[dict]:
    """배달을 받을 수 있는 주소. 금지 구역 밖이고, 서비스 반경 안입니다.

    공역(AIRSPACE)이 이 아래에서 만들어지므로 처음 부를 때 한 번만 셈합니다.
    """
    global _SERVICE_AREA
    if _SERVICE_AREA is None:
        _SERVICE_AREA = _within_service_area()
    return _SERVICE_AREA


def _within_service_area() -> list[dict]:
    depot_lat, depot_lon = to_latlon(*DEPOT)
    open_ones = []
    for address in ADDRESSES:
        if any(v.rule == "forbidden" and v.covers(address["lat"], address["lon"])
               for v in AIRSPACE.all()):
            continue
        north = (address["lat"] - depot_lat) * 110_570.0
        east = (address["lon"] - depot_lon) * 84_400.0   # 위도 40.7 도 기준
        if (north * north + east * east) ** 0.5 > SERVICE_RADIUS_M:
            continue
        gx, gy = to_grid(address["lat"], address["lon"])
        if 2 <= gx <= 98 and 2 <= gy <= 58:
            open_ones.append({**address, "gx": gx, "gy": gy})
    return open_ones



# 병원 응급헬기가 뜬다고 갑자기 상공이 닫힙니다. 착륙 패드 P1 이 그 안에 있습니다.
# 이게 리콜과 같은 얘기의 공간판입니다. 금지가 언제 도착하고 누가 강제하느냐.
ZONE_TICK = 560
ZONE_UNTIL = 900   # 응급헬기가 뜨고 내리는 동안만. 구역에는 유효기간이 있습니다
ZONE = {
    "id": "nofly-2026-09-hospital",
    "kind": "zone",
    "forbid_resource": "bay:A",
    "reason": "응급헬기 이착륙. 상공 비행금지",
    "name": "병원 헬리패드 상공",
    "polygon": [[40.6985, -73.9760], [40.6985, -73.9690],
                [40.7055, -73.9690], [40.7055, -73.9760]],
    "floor_m": 0, "ceiling_m": None, "reference": "AGL",
    "rule": "forbidden", "source": "예시 데이터",
}
# 구역은 이 폴리곤 하나입니다. 예전에는 점수판이 따로 원(centre·radius)을 들고 있었는데,
# 폴리곤을 브루클린으로 옮길 때 원은 안 옮겨져서 이스트강 한복판을 세고 있었습니다.
# 런타임이 막는 곳, 점수판이 세는 곳, 화면이 그리는 곳이 같아야 합니다.
ZONE_VOLUME = Volume.from_dict(ZONE)
RECALL = {
    "id": "recall-2026-09-dv-x500",
    "kind": "recall",
    "forbid_action": "fast_charge",
    "applies_to": {"model": "dv-x500"},
    "reason": "dv-x500 급속충전 중 배터리 발화 사례. 급속충전 금지",
}


AIRSPACE = Airspace()


@dataclass
class Vehicle:
    id: str
    model: str
    kind: str
    x: float
    y: float
    battery: float
    passengers: int = 0
    cargo: bool = False
    vibration: float = 0.0
    autonomy_health: float = 1.0
    alt: float = 0.0
    state: str = "cruising"
    assigned_pad: str | None = None
    charge_mode: str = "normal"
    spend: float = 0.0
    in_zone: bool = False
    over_ceiling: bool = False
    heading: float = 0.0
    cruise_alt: float = LOITER_ALT_M
    job_label: str = ""            # 배달지 주소
    job_x: float | None = None
    job_y: float | None = None
    delivered: int = 0
    waypoints: list = field(default_factory=list)   # 승인된 경로. 없으면 못 움직입니다

    def public(self) -> dict:
        data = asdict(self)
        latitude, longitude = to_latlon(self.x, self.y)
        data["lat"] = round(latitude, 6)
        data["lon"] = round(longitude, 6)
        data["alt_m"] = round(self.alt, 1)
        data["battery"] = round(self.battery, 1)
        data["heading"] = round(self.heading, 1)
        data["job"] = self.job_label
        data["delivered"] = self.delivered
        if self.job_x is not None:
            job_lat, job_lon = to_latlon(self.job_x, self.job_y)
            data["job_lat"] = round(job_lat, 6)
            data["job_lon"] = round(job_lon, 6)
        data["route"] = [
            {"lat": round(to_latlon(x, y)[0], 6), "lon": round(to_latlon(x, y)[1], 6),
             "alt_m": alt}
            for x, y, alt in self.waypoints
        ]
        data["vibration"] = round(self.vibration, 2)
        data["autonomy_health"] = round(self.autonomy_health, 2)
        data["spend"] = round(self.spend, 2)
        return data


@dataclass
class Scoreboard:
    pad_conflicts: int = 0
    zone_incursions: int = 0
    zone_dwell_ticks: int = 0
    ceiling_breaches: int = 0
    airspace_violations: int = 0
    deliveries: int = 0
    declined: int = 0
    refused_without_receipt: int = 0
    spend_usd: float = 0.0
    over_fleet_limit_usd: float = 0.0
    post_recall_violations: int = 0
    unrecorded_actions: int = 0
    unapproved_passenger_actions: int = 0
    batteries_dead: int = 0
    actions: int = 0
    human_approvals: int = 0

    def public(self) -> dict:
        data = asdict(self)
        data["spend_usd"] = round(self.spend_usd, 2)
        data["over_fleet_limit_usd"] = round(self.over_fleet_limit_usd, 2)
        return data


for _raw in STANDING_VOLUMES + BUILDINGS:
    AIRSPACE.add(Volume.from_dict(_raw))


def fresh_fleet(seed: int) -> list[Vehicle]:
    """양쪽 세계가 똑같은 상태에서 출발합니다. 다른 건 배선뿐입니다."""
    rng = random.Random(seed)
    return [
        # 같은 기종은 같은 속도로 닳습니다. 그래서 같은 순간에 같은 패드를 원합니다.
        # 배달 기단은 창고에 삽니다. 기지(47.8, 54.9) 바로 위에서 하루를 시작합니다.
        # 예전 좌표는 어퍼 맨해튼이었는데, 그때는 남북 이동이 8배 빨라서 기지까지
        # 몇십 틱이면 갔습니다. 실제 속도로는 그 자리에서 배터리가 먼저 끝납니다.
        Vehicle("drone-01", "dv-x500", "delivery", 44.0, 52.0, 62.0, cargo=True),
        Vehicle("drone-02", "dv-hexa", "drone", 48.0, 50.0, 70.0 + rng.random(), cargo=True),
        Vehicle("drone-03", "dv-x500", "delivery", 52.0, 52.0, 55.0, cargo=True),
    ]


class World:
    def __init__(self, name: str, seed: int, fleet_limit: float,
                 require_receipt: bool = False):
        self.name = name
        self.fleet_limit = fleet_limit
        # 조종장치가 원장 번호를 요구하는가.
        # 요구하면 런타임을 안 거친 명령은 물리적으로 실행되지 않습니다.
        # 이 한 줄이 "권고"와 "강제"를 가릅니다.
        self.require_receipt = require_receipt
        self._rng = random.Random(seed + 991)
        self.vehicles = {v.id: v for v in fresh_fleet(seed)}
        for vehicle in self.vehicles.values():
            self._assign_job(vehicle)
        self.score = Scoreboard()
        self.events: list[dict] = []

    # ---------- 조종장치. 시키는 대로 합니다 ----------

    def act(self, asset: str, action: str, params: dict, ledger_id: str | None,
            blast: str, approved_by: str | None, tick: int) -> dict:
        vehicle = self.vehicles.get(asset)
        if vehicle is None:
            return {"ok": False, "error": f"unknown asset {asset}"}
        if action not in COSTS:
            return {"ok": False, "error": f"unknown action {action}"}

        if self.require_receipt and not ledger_id:
            self.score.refused_without_receipt += 1
            return {"ok": False, "error": "승인 영수증(ledger id) 없이는 실행하지 않습니다"}

        refusal = self._refuse(vehicle, action, params)
        if refusal:
            return refusal

        self.score.actions += 1
        if not ledger_id:
            self.score.unrecorded_actions += 1
        if approved_by:
            self.score.human_approvals += 1
        if blast == "passenger" and not approved_by:
            self.score.unapproved_passenger_actions += 1
        if (
            tick >= RECALL_TICK
            and action == RECALL["forbid_action"]
            and vehicle.model == RECALL["applies_to"]["model"]
        ):
            self.score.post_recall_violations += 1
            self._log(tick, "리콜 위반", f"{asset} 가 리콜 이후 급속충전")

        cost = COSTS[action]
        vehicle.spend += cost
        self.score.spend_usd += cost
        if self.score.spend_usd > self.fleet_limit:
            self.score.over_fleet_limit_usd = self.score.spend_usd - self.fleet_limit

        if action == "decline_job":
            # 규정상 갈 수 없는 주소입니다. 주문을 반려하고 다음 건을 받습니다.
            # 이것도 결정이고 기록에 남습니다 — 어느 주소가 왜 배달 불가인지가 쌓입니다.
            self.score.declined += 1
            self._log(tick, "배달 불가", f"{vehicle.job_label} — 규정상 경로 없음")
            self._assign_job(vehicle)
        elif action == "fly_route":
            # 승인된 경로. 경유점이 있으면 그대로 따라갑니다.
            # 없으면 목적지까지 직선입니다 — 그게 오른쪽 세계가 하는 일입니다.
            vehicle.waypoints = self._to_waypoints(params.get("legs"), vehicle)
            vehicle.assigned_pad = None
            vehicle.state = "delivering"
            if not vehicle.waypoints:
                vehicle.cruise_alt = float(params.get("alt_m") or CRUISE_ALT_M)
        elif action == "reserve_pad":
            vehicle.assigned_pad = params["pad"]
            vehicle.state = "approaching"
            vehicle.waypoints = self._to_waypoints(params.get("legs"), vehicle)
            # 승인된 순항 고도. 안 주면 기본값으로 납니다 — 그게 규정 위반일 수 있습니다.
            vehicle.cruise_alt = float(params.get("alt_m") or CRUISE_ALT_M)
        elif action in ("charge", "fast_charge"):
            vehicle.state = "charging"
            vehicle.charge_mode = "fast" if action == "fast_charge" else "normal"
        elif action == "depart":
            vehicle.assigned_pad = None
            vehicle.state = "cruising"
            vehicle.cruise_alt = LOITER_ALT_M
            vehicle.waypoints = []   # 다 쓴 경로입니다
            vehicle.vibration = 0.0  # 패드에 있는 동안 정비를 받았습니다
        elif action == "divert_ground":
            # 접근을 끊고 대기로 돌아갑니다. 착륙이 아니라 회항입니다.
            # 경유점을 남겨두면 회수 명령을 받고도 원래 목적지로 계속 날아갑니다 —
            # 구역이 닫혔는데 그 안으로 들어가던 게 그래서였습니다.
            vehicle.assigned_pad = None
            vehicle.state = "cruising"
            vehicle.waypoints = []
            vehicle.vibration = 0.0
        elif action == "disengage_autonomy":
            vehicle.autonomy_health = 0.0
            vehicle.state = "stranded"
            vehicle.assigned_pad = None

        return {"ok": True, "cost_usd": cost, "state": vehicle.state}

    @staticmethod
    def _to_waypoints(legs, vehicle: Vehicle) -> list:
        if not legs:
            return []
        points = []
        for leg in legs:
            gx, gy = to_grid(leg["lat"], leg["lon"])
            points.append((gx, gy, float(leg.get("alt_m") or CRUISE_ALT_M)))
        return points[1:] if len(points) > 1 else points

    @staticmethod
    def _refuse(vehicle: Vehicle, action: str, params: dict) -> dict | None:
        """물리적으로 불가능한 명령. 돈도 안 나가고 세지도 않습니다."""
        if action == "reserve_pad" and params.get("pad") not in PADS:
            return {"ok": False, "error": f"unknown pad {params.get('pad')}"}
        if action in ("charge", "fast_charge") and vehicle.state not in ("landed", "charging"):
            return {"ok": False, "error": "not on a pad"}
        if action == "fly_route" and vehicle.job_x is None:
            return {"ok": False, "error": "no delivery assigned"}
        if vehicle.state == "grounded":
            return {"ok": False, "error": "battery is dead"}
        return None

    # ---------- 시간 ----------

    def tick(self, tick: int) -> None:
        for vehicle in self.vehicles.values():
            self._advance(vehicle, tick)
        self._detect_pad_conflicts(tick)
        self._detect_zone_incursions(tick)
        self._detect_ceiling_breaches(tick)

    def _advance(self, vehicle: Vehicle, tick: int) -> None:
        if vehicle.state == "charging":
            gain = 2.4 if vehicle.charge_mode == "fast" else 1.0
            vehicle.battery = min(100.0, vehicle.battery + gain)
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        if vehicle.state in ("stranded", "diverted", "grounded"):
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        if vehicle.state == "landed" and not vehicle.waypoints:
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)

        vehicle.battery -= 0.055
        if vehicle.kind == "drone" and tick >= 60:
            vehicle.vibration = min(1.0, vehicle.vibration + 0.006)
        # 자율주행 이상은 배달이 몇 건 돌아간 뒤에 옵니다. 초반에 세워버리면
        # 기단의 3분의 1이 판 내내 멈춰 있습니다.
        if vehicle.id == "drone-03" and tick >= 900:
            vehicle.autonomy_health = max(0.0, vehicle.autonomy_health - 0.01)

        if vehicle.battery <= 0.0:
            vehicle.battery = 0.0
            if vehicle.state != "grounded":
                vehicle.state = "grounded"
                self.score.batteries_dead += 1
                self._log(tick, "배터리 소진", f"{vehicle.id} 가 멈췄습니다")
            return

        # 승인된 목적지가 없으면 제자리에 뜬 채 기다립니다.
        # 어디로 가는 것도 행동이고, 행동은 승인을 받아야 합니다.
        target = self._current_target(vehicle)
        if target is None:
            self._hold_altitude(vehicle, (vehicle.x, vehicle.y))
            return
        if vehicle.waypoints:
            vehicle.cruise_alt = vehicle.waypoints[0][2]
        # 멀티로터는 수직으로 올라간 다음 갑니다. 올라가면서 앞으로 나가면 승인받은
        # 순항 고도보다 낮은 채로 건물 사이를 지나게 됩니다 — 경로는 통과인데 기체는
        # 위반하는 상태가 됩니다. 실제 배달 드론도 뜨고 나서 이동합니다.
        if not self._at_cruise(vehicle, target):
            self._hold_altitude(vehicle, target)
            return
        self._move_toward(vehicle, target)
        self._hold_altitude(vehicle, target)
        if vehicle.waypoints and self._at(vehicle, target):
            vehicle.waypoints.pop(0)   # 이 구간 끝. 다음 구간으로
            return
        if vehicle.state == "delivering" and not vehicle.waypoints and self._at(vehicle, target):
            self._deliver(vehicle, tick)
        if (
            vehicle.state == "approaching"
            and vehicle.assigned_pad
            and self._at(vehicle, target)
            and vehicle.alt <= 1.0
        ):
            vehicle.state = "landed"

    @staticmethod
    def _current_target(vehicle: Vehicle) -> tuple[float, float] | None:
        if vehicle.waypoints:
            return (vehicle.waypoints[0][0], vehicle.waypoints[0][1])
        if vehicle.assigned_pad:
            return PADS[vehicle.assigned_pad]
        if vehicle.state == "delivering" and vehicle.job_x is not None:
            return (vehicle.job_x, vehicle.job_y)
        return None

    def _deliver(self, vehicle: Vehicle, tick: int) -> None:
        vehicle.delivered += 1
        self.score.deliveries += 1
        self._log(tick, "배달 완료", f"{vehicle.id} → {vehicle.job_label}")
        self._assign_job(vehicle)
        vehicle.state = "cruising"

    def _assign_job(self, vehicle: Vehicle) -> None:
        pool = pickable_addresses()
        if not pool:
            vehicle.job_x = vehicle.job_y = None
            vehicle.job_label = ""
            return
        address = self._rng.choice(pool)
        vehicle.job_label = address["label"]
        vehicle.job_x, vehicle.job_y = address["gx"], address["gy"]

    def _move_toward(self, vehicle: Vehicle, target: tuple[float, float]) -> None:
        # 격자가 아니라 미터로 잽니다. 그래야 동서와 남북의 속도가 같습니다.
        dx_m = (target[0] - vehicle.x) * METRES_PER_CELL_X
        dy_m = (target[1] - vehicle.y) * METRES_PER_CELL_Y
        distance_m = (dx_m * dx_m + dy_m * dy_m) ** 0.5
        if distance_m < 1.0:
            return
        step_m = min(STEP_METRES, distance_m)
        vehicle.x += dx_m / distance_m * step_m / METRES_PER_CELL_X
        vehicle.y += dy_m / distance_m * step_m / METRES_PER_CELL_Y
        # 화면 북쪽이 y 감소 방향입니다
        vehicle.heading = (math.degrees(math.atan2(dx_m, -dy_m))) % 360.0

    @staticmethod
    def _at_cruise(vehicle: Vehicle, target: tuple[float, float]) -> bool:
        """승인받은 고도에 올라왔는가. 도착점 위에서 내려가는 중이면 묻지 않습니다."""
        if vehicle.state in ("landed", "charging"):
            return True
        if World._at(vehicle, target):
            return True
        return vehicle.alt >= vehicle.cruise_alt - CLIMB_RATE_M

    @staticmethod
    def _hold_altitude(vehicle: Vehicle, target: tuple[float, float]) -> None:
        """뜨고 내리는 구간을 실제로 그립니다. 3D 로 보면 이게 전부입니다.

        멀티로터는 착륙점 위까지 순항 고도로 간 다음 수직으로 내려갑니다. 예전에는
        1.5km 앞에서부터 비스듬히 활공했는데, 그 비탈이 건물 높이를 그대로 지나가서
        승인된 경로를 날면서도 건물을 스쳤습니다.
        """
        if vehicle.state == "landed" or (
            vehicle.state == "approaching" and World._at(vehicle, target)
        ):
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        ceiling = vehicle.cruise_alt
        if vehicle.alt > ceiling:
            vehicle.alt = max(ceiling, vehicle.alt - DESCENT_RATE_M)
        else:
            vehicle.alt = min(ceiling, vehicle.alt + CLIMB_RATE_M)

    @staticmethod
    def _at(vehicle: Vehicle, target: tuple[float, float]) -> bool:
        """경유점에 닿았는가. 격자가 아니라 미터로 잽니다.

        0.6칸으로 재던 시절에는 남북으로 155m 앞에서 이미 도달로 쳤습니다. 그만큼
        일찍 다음 구간으로 넘어가며 모서리를 잘라먹었고, 승인된 경로에서 벗어난
        그 자리에서 건물을 스쳤습니다.
        """
        north = (target[1] - vehicle.y) * METRES_PER_CELL_Y
        east = (target[0] - vehicle.x) * METRES_PER_CELL_X
        return (north * north + east * east) ** 0.5 < ARRIVAL_RADIUS_M

    def _detect_pad_conflicts(self, tick: int) -> None:
        occupants: dict[str, list[str]] = {}
        for vehicle in self.vehicles.values():
            if vehicle.state in ("landed", "charging") and vehicle.assigned_pad:
                occupants.setdefault(vehicle.assigned_pad, []).append(vehicle.id)
        for pad, riders in occupants.items():
            if len(riders) > 1:
                self.score.pad_conflicts += 1
                self._log(tick, "패드 충돌", f"{pad} 에 {', '.join(riders)} 가 동시에")

    def _detect_zone_incursions(self, tick: int) -> None:
        """구역 안 기체를 셉니다. 신청이 아니라 위치입니다.

        규칙이 도착한 순간 이미 안에 있던 기체는 침범으로 세지 않습니다. 그건 아무도
        잘못한 게 아닙니다. 대신 그 뒤로 얼마나 오래 남아 있었는지를 셉니다. 나가라고
        시킬 수 있는 쪽과 각자 알아서 나가는 쪽의 차이가 거기서 벌어집니다.
        """
        if not (ZONE_TICK <= tick <= ZONE_UNTIL):
            return
        for vehicle in self.vehicles.values():
            if vehicle.state == "grounded":
                continue
            inside = ZONE_VOLUME.covers(*to_latlon(vehicle.x, vehicle.y))
            if inside:
                self.score.zone_dwell_ticks += 1
            if tick == ZONE_TICK:
                vehicle.in_zone = inside  # 규칙 도착 시점의 상태는 그냥 기록만
                continue
            if inside and not vehicle.in_zone:
                self.score.zone_incursions += 1
                self._log(tick, "비행금지 구역 침범", f"{vehicle.id} 가 병원 상공에 들어감")
            vehicle.in_zone = inside

    def _detect_ceiling_breaches(self, tick: int) -> None:
        """실제 FAA 격자를 어겼나. 금지 칸 진입과 천장 초과는 다른 위반입니다."""
        for vehicle in self.vehicles.values():
            if vehicle.state in ("grounded", "landed", "charging"):
                vehicle.over_ceiling = False
                continue
            latitude, longitude = to_latlon(vehicle.x, vehicle.y)
            breach = AIRSPACE.breach(latitude, longitude, vehicle.alt)
            if breach is None:
                vehicle.over_ceiling = False
                continue
            if not vehicle.over_ceiling:
                if breach.rule == "forbidden":
                    self.score.airspace_violations += 1
                    self._log(tick, "금지 공역 진입",
                              f"{vehicle.id}: {breach.breach(latitude, longitude, vehicle.alt)}")
                else:
                    self.score.ceiling_breaches += 1
                    self._log(tick, "허용 고도 초과",
                              f"{vehicle.id}: {breach.breach(latitude, longitude, vehicle.alt)}")
            vehicle.over_ceiling = True

    def _log(self, tick: int, kind: str, text: str) -> None:
        self.events.append({"tick": tick, "kind": kind, "text": text, "at": time.time()})
        del self.events[: max(0, len(self.events) - 40)]

    def snapshot(self, tick: int, volumes: bool = False) -> dict:
        """volumes 는 달라고 해야 옵니다.

        건물까지 넣으면 3천 개가 넘어서, 0.25초마다 도는 폴링에 매번 실으면
        2MB 짜리 응답이 초당 몇 번씩 오갑니다. 공역은 한 번만 받으면 됩니다.
        """
        return {
            "world": self.name,
            "tick": tick,
            "bands": AIRSPACE_BANDS + (
                [{k: v for k, v in ZONE.items()
                  if k in ("id", "name", "polygon", "ceiling_m", "rule", "reason")}]
                if ZONE_TICK <= tick <= ZONE_UNTIL else []
            ),
            **({"volumes": [v.to_dict() for v in AIRSPACE.all()] + (
                [{k: v for k, v in ZONE.items()
                  if k in ("id", "name", "polygon", "floor_m", "ceiling_m",
                           "reference", "rule", "reason", "source")}]
                if ZONE_TICK <= tick <= ZONE_UNTIL else []
            )} if volumes else {}),
            "zone": {**ZONE, "active": tick >= ZONE_TICK},
            "pads": PADS,
            "pad_coords": {
                name: {"lat": round(lat, 6), "lon": round(lon, 6)}
                for name, (lat, lon) in (
                    (n, to_latlon(px, py)) for n, (px, py) in PADS.items()
                )
            },
            "depot": DEPOT,
            "depot_coords": {
                "lat": round(to_latlon(*DEPOT)[0], 6),
                "lon": round(to_latlon(*DEPOT)[1], 6),
            },
            "assets": {vid: v.public() for vid, v in self.vehicles.items()},
            "scoreboard": self.score.public(),
            "fleet_limit": self.fleet_limit,
            "events": list(reversed(self.events[-12:])),
        }


class Simulation:
    """두 세계를 같은 씨앗, 같은 시계로 돌립니다."""

    def __init__(self, seed: int = 7, fleet_limit: float = 500.0, tick_seconds: float = 0.2,
                 lock_actuator: bool = False, max_ticks: int = 0):
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.lock_actuator = lock_actuator
        self.max_ticks = max_ticks
        self.rounds = 0
        self.tick_count = 0
        self.worlds = self._fresh_worlds(fleet_limit)

    def _fresh_worlds(self, fleet_limit: float) -> dict:
        return {
            "guarded": World("guarded", self.seed, fleet_limit),
            # 조종장치를 잠그면 직결 배선은 아무것도 못 합니다.
            # 잠그지 않은 것이 오늘의 기본값이고, 그래서 이 데모가 필요합니다.
            "direct": World("direct", self.seed, fleet_limit,
                            require_receipt=self.lock_actuator),
        }

    def step(self) -> None:
        self.tick_count += 1
        for world in self.worlds.values():
            world.tick(self.tick_count)
        if self._round_is_over():
            self.rounds += 1
            self.reset(keep_rounds=True)

    def _round_is_over(self) -> bool:
        """한 판이 끝났나. 화면을 켜두면 계속 돌아야 하니 알아서 다시 시작합니다."""
        if self.max_ticks and self.tick_count >= self.max_ticks:
            return True
        return all(
            vehicle.state in ("grounded", "stranded")
            for world in self.worlds.values()
            for vehicle in world.vehicles.values()
        )

    def bulletins(self) -> list[dict]:
        out = []
        if ZONE_TICK <= self.tick_count <= ZONE_UNTIL:
            out.append({**ZONE,
                        "published_tick": ZONE_TICK, "until_tick": ZONE_UNTIL})
        if self.tick_count >= RECALL_TICK:
            out.append({**RECALL, "published_tick": RECALL_TICK})
        return out

    def reset(self, keep_rounds: bool = False) -> None:
        limit = self.worlds["guarded"].fleet_limit
        self.tick_count = 0
        if not keep_rounds:
            self.rounds = 0
        self.worlds = self._fresh_worlds(limit)
