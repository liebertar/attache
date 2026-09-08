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

# 위치는 진짜 FAA 격자를 보고 골랐습니다. 둘 다 합법 경로가 있고, 경로 최저 천장은 61m
# 입니다. 기본 순항 고도 90m 로 그냥 날면 규정 위반이 됩니다 — 그게 오른쪽 세계입니다.
PADS = {"pad:P1": (20.0, 45.0), "pad:P2": (90.0, 50.0)}
DEPOT = (95.0, 52.0)

# 맨해튼. 배터리파크에서 센트럴파크 북단까지, 이스트강 건너 롱아일랜드시티까지.
# 여기를 고른 이유는 FAA 가 격자마다 허용 고도를 공개하기 때문입니다.
# 맨해튼은 전부 금지가 아닙니다. 격자의 40% 가 400ft(122m)까지 허용되고,
# 24% 는 허가 없이 못 납니다. 한 블록 건너 천장이 바뀝니다.
# configs/airspace/nyc.json 이 그 실제 데이터이고, scripts/fetch_airspace.py 가 받아옵니다.
ORIGIN_LAT, ORIGIN_LON = 40.7000, -74.0250
SPAN_LAT, SPAN_LON = 0.0900, 0.0850
CRUISE_ALT_M = 90.0
LOITER_ALT_M = 45.0  # 승인 전 대기 고도
CLIMB_RATE_M = 4.0      # 틱당 상승
DESCENT_RATE_M = 3.0    # 틱당 하강
APPROACH_RADIUS = 12.0  # 이 안에 들어오면 내려가기 시작합니다


def to_latlon(x: float, y: float) -> tuple[float, float]:
    return ORIGIN_LAT + (1.0 - y / 60.0) * SPAN_LAT, ORIGIN_LON + (x / 100.0) * SPAN_LON

COSTS = {
    "reserve_pad": 28.0,
    "charge": 22.0,
    "fast_charge": 60.0,
    "divert_ground": 35.0,
    "disengage_autonomy": 0.0,
    "depart": 0.0,
}

RECALL_TICK = 158

# 상시 공역. 한 동네 안에서도 허용 고도가 갈립니다 — 실제 데이터가 그렇게 생겼습니다.
# FAA UAS Facility Map 은 격자마다 천장이 다르고, ED-269 구역은 하한·상한을 갖습니다.
AIRSPACE_FILE = os.getenv(
    "AIRSPACE_FILE", str(Path(__file__).resolve().parent.parent / "configs/airspace/nyc.json")
)


def load_volumes() -> list[dict]:
    """FAA UAS Facility Map. 격자마다 허용 고도가 다릅니다.

    천장 0ft 는 고도 제한이 아니라 '허가 없이는 못 난다'는 뜻이라 금지로 옮겨 담습니다.
    """
    try:
        return json.loads(Path(AIRSPACE_FILE).read_text(encoding="utf-8"))["volumes"]
    except (OSError, KeyError, json.JSONDecodeError):
        return []


STANDING_VOLUMES = load_volumes()


# 병원 응급헬기가 뜬다고 갑자기 상공이 닫힙니다. 착륙 패드 P1 이 그 안에 있습니다.
# 이게 리콜과 같은 얘기의 공간판입니다. 금지가 언제 도착하고 누가 강제하느냐.
ZONE_TICK = 40
ZONE_UNTIL = 170   # 응급헬기가 뜨고 내리는 동안만. 구역에는 유효기간이 있습니다
ZONE = {
    "id": "nofly-2026-09-hospital",
    "kind": "zone",
    "forbid_resource": "pad:P1",
    "reason": "응급헬기 이착륙. 상공 비행금지",
    "name": "병원 헬리패드 상공",
    "polygon": [[40.7180, -74.0140], [40.7180, -74.0020],
                [40.7270, -74.0020], [40.7270, -74.0140]],
    "floor_m": 0, "ceiling_m": None, "reference": "AGL",
    "rule": "forbidden", "source": "예시 데이터",
    "centre": (25.0, 45.0),
    "radius": 16.0,
}
RECALL = {
    "id": "recall-2026-09-robotaxi-v3",
    "kind": "recall",
    "forbid_action": "fast_charge",
    "applies_to": {"model": "robotaxi-v3"},
    "reason": "robotaxi-v3 급속충전 중 배터리 발화 사례. 급속충전 금지",
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

    def public(self) -> dict:
        data = asdict(self)
        latitude, longitude = to_latlon(self.x, self.y)
        data["lat"] = round(latitude, 6)
        data["lon"] = round(longitude, 6)
        data["alt_m"] = round(self.alt, 1)
        data["battery"] = round(self.battery, 1)
        data["heading"] = round(self.heading, 1)
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


for _raw in STANDING_VOLUMES:
    AIRSPACE.add(Volume.from_dict(_raw))


def fresh_fleet(seed: int) -> list[Vehicle]:
    """양쪽 세계가 똑같은 상태에서 출발합니다. 다른 건 배선뿐입니다."""
    rng = random.Random(seed)
    return [
        # 같은 기종은 같은 속도로 닳습니다. 그래서 같은 순간에 같은 패드를 원합니다.
        Vehicle("taxi-a", "robotaxi-v3", "robotaxi", 18.0, 20.0, 31.0, passengers=2),
        Vehicle("drone-b", "hexa-2", "drone", 62.0, 14.0, 70.0 + rng.random(), cargo=True),
        Vehicle("taxi-c", "robotaxi-v3", "robotaxi", 84.0, 26.0, 31.0, passengers=1),
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
        self.vehicles = {v.id: v for v in fresh_fleet(seed)}
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

        if action == "reserve_pad":
            vehicle.assigned_pad = params["pad"]
            vehicle.state = "approaching"
            # 승인된 순항 고도. 안 주면 기본값으로 납니다 — 그게 규정 위반일 수 있습니다.
            vehicle.cruise_alt = float(params.get("alt_m") or CRUISE_ALT_M)
        elif action in ("charge", "fast_charge"):
            vehicle.state = "charging"
            vehicle.charge_mode = "fast" if action == "fast_charge" else "normal"
        elif action == "depart":
            vehicle.assigned_pad = None
            vehicle.state = "cruising"
            vehicle.cruise_alt = LOITER_ALT_M
            vehicle.vibration = 0.0  # 패드에 있는 동안 정비를 받았습니다
        elif action == "divert_ground":
            # 접근을 끊고 대기로 돌아갑니다. 착륙이 아니라 회항입니다.
            vehicle.assigned_pad = None
            vehicle.state = "cruising"
            vehicle.vibration = 0.0
        elif action == "disengage_autonomy":
            vehicle.autonomy_health = 0.0
            vehicle.state = "stranded"
            vehicle.assigned_pad = None

        return {"ok": True, "cost_usd": cost, "state": vehicle.state}

    @staticmethod
    def _refuse(vehicle: Vehicle, action: str, params: dict) -> dict | None:
        """물리적으로 불가능한 명령. 돈도 안 나가고 세지도 않습니다."""
        if action == "reserve_pad" and params.get("pad") not in PADS:
            return {"ok": False, "error": f"unknown pad {params.get('pad')}"}
        if action in ("charge", "fast_charge") and vehicle.state not in ("landed", "charging"):
            return {"ok": False, "error": "not on a pad"}
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
            gain = 5.0 if vehicle.charge_mode == "fast" else 2.0
            vehicle.battery = min(100.0, vehicle.battery + gain)
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        if vehicle.state in ("stranded", "diverted", "grounded"):
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return

        vehicle.battery -= 0.3
        if vehicle.kind == "drone" and tick >= 60:
            vehicle.vibration = min(1.0, vehicle.vibration + 0.006)
        if vehicle.id == "taxi-c" and tick >= 200:
            vehicle.autonomy_health = max(0.0, vehicle.autonomy_health - 0.01)

        if vehicle.battery <= 0.0:
            vehicle.battery = 0.0
            if vehicle.state != "grounded":
                vehicle.state = "grounded"
                self.score.batteries_dead += 1
                self._log(tick, "배터리 소진", f"{vehicle.id} 가 멈췄습니다")
            return

        target = PADS.get(vehicle.assigned_pad) if vehicle.assigned_pad else DEPOT
        self._move_toward(vehicle, target)
        self._hold_altitude(vehicle, target)
        if (
            vehicle.state == "approaching"
            and vehicle.assigned_pad
            and self._at(vehicle, target)
            and vehicle.alt <= 1.0
        ):
            vehicle.state = "landed"

    def _move_toward(self, vehicle: Vehicle, target: tuple[float, float]) -> None:
        dx, dy = target[0] - vehicle.x, target[1] - vehicle.y
        distance = (dx * dx + dy * dy) ** 0.5
        if distance < 0.01:
            return
        step = min(2.2, distance)
        vehicle.x += dx / distance * step
        vehicle.y += dy / distance * step
        # 화면 북쪽이 y 감소 방향입니다
        vehicle.heading = (math.degrees(math.atan2(dx, -dy))) % 360.0

    @staticmethod
    def _hold_altitude(vehicle: Vehicle, target: tuple[float, float]) -> None:
        """뜨고 내리는 구간을 실제로 그립니다. 3D 로 보면 이게 전부입니다."""
        if vehicle.state == "landed":
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        distance = (
            (target[0] - vehicle.x) ** 2 + (target[1] - vehicle.y) ** 2
        ) ** 0.5
        if vehicle.state == "approaching" and distance < APPROACH_RADIUS:
            glide = vehicle.cruise_alt * (distance / APPROACH_RADIUS)
            vehicle.alt = max(0.0, min(vehicle.alt, glide), vehicle.alt - DESCENT_RATE_M)
            vehicle.alt = min(vehicle.alt, glide)
            return
        ceiling = vehicle.cruise_alt
        if vehicle.alt > ceiling:
            vehicle.alt = max(ceiling, vehicle.alt - DESCENT_RATE_M)
        else:
            vehicle.alt = min(ceiling, vehicle.alt + CLIMB_RATE_M)

    @staticmethod
    def _at(vehicle: Vehicle, target: tuple[float, float]) -> bool:
        return abs(vehicle.x - target[0]) < 0.6 and abs(vehicle.y - target[1]) < 0.6

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
        centre_x, centre_y = ZONE["centre"]
        for vehicle in self.vehicles.values():
            if vehicle.state == "grounded":
                continue
            distance = ((vehicle.x - centre_x) ** 2 + (vehicle.y - centre_y) ** 2) ** 0.5
            inside = distance <= ZONE["radius"]
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

    def snapshot(self, tick: int) -> dict:
        return {
            "world": self.name,
            "tick": tick,
            "volumes": [v.to_dict() for v in AIRSPACE.all()] + (
                [{k: v for k, v in ZONE.items()
                  if k in ("id", "name", "polygon", "floor_m", "ceiling_m",
                           "reference", "rule", "reason", "source")}]
                if ZONE_TICK <= tick <= ZONE_UNTIL else []
            ),
            "zone": {
                **{k: v for k, v in ZONE.items() if k != "centre"},
                "lat": to_latlon(*ZONE["centre"])[0],
                "lon": to_latlon(*ZONE["centre"])[1],
                "radius_m": round(ZONE["radius"] / 100.0 * SPAN_LON * 88_000, 0),
                "active": tick >= ZONE_TICK,
            },
            "pads": PADS,
            "pad_coords": {
                name: {"lat": round(lat, 6), "lon": round(lon, 6)}
                for name, (lat, lon) in (
                    (n, to_latlon(px, py)) for n, (px, py) in PADS.items()
                )
            },
            "depot": DEPOT,
            "assets": {vid: v.public() for vid, v in self.vehicles.items()},
            "scoreboard": self.score.public(),
            "fleet_limit": self.fleet_limit,
            "events": list(reversed(self.events[-12:])),
        }


class Simulation:
    """두 세계를 같은 씨앗, 같은 시계로 돌립니다."""

    def __init__(self, seed: int = 7, fleet_limit: float = 500.0, tick_seconds: float = 0.2,
                 lock_actuator: bool = False):
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.lock_actuator = lock_actuator
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

    def bulletins(self) -> list[dict]:
        out = []
        if ZONE_TICK <= self.tick_count <= ZONE_UNTIL:
            out.append({**{k: v for k, v in ZONE.items() if k != "centre"},
                        "published_tick": ZONE_TICK, "until_tick": ZONE_UNTIL})
        if self.tick_count >= RECALL_TICK:
            out.append({**RECALL, "published_tick": RECALL_TICK})
        return out

    def reset(self) -> None:
        limit = self.worlds["guarded"].fleet_limit
        self.tick_count = 0
        self.worlds = self._fresh_worlds(limit)
