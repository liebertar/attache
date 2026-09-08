"""The world. Deliberately dumb.

The actuator here does exactly what it is told, with no checks of its own. A real landing
pad gate is like this too. That is the whole point: if nothing above it holds the rules,
nothing holds the rules. Both worlds run the same code from the same seed; the only
difference is who is allowed to call act().
"""

import random
import time
from dataclasses import asdict, dataclass, field

PADS = {"pad:P1": (25.0, 45.0), "pad:P2": (75.0, 45.0)}
DEPOT = (50.0, 8.0)

# 격자를 실제 위경도로 옮깁니다. 서울 잠실, 한강변 아파트 단지 상공입니다.
# 이래야 SITL 이 보내오는 진짜 좌표와 같은 지도 위에 그릴 수 있습니다.
ORIGIN_LAT, ORIGIN_LON = 37.5040, 127.0720
SPAN_LAT, SPAN_LON = 0.0130, 0.0220
CRUISE_ALT_M = 90.0
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
RECALL = {
    "id": "recall-2026-09-robotaxi-v3",
    "kind": "recall",
    "forbid_action": "fast_charge",
    "applies_to": {"model": "robotaxi-v3"},
    "reason": "robotaxi-v3 급속충전 중 배터리 발화 사례. 급속충전 금지",
}


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

    def public(self) -> dict:
        data = asdict(self)
        latitude, longitude = to_latlon(self.x, self.y)
        data["lat"] = round(latitude, 6)
        data["lon"] = round(longitude, 6)
        data["alt_m"] = round(self.alt, 1)
        data["battery"] = round(self.battery, 1)
        data["vibration"] = round(self.vibration, 2)
        data["autonomy_health"] = round(self.autonomy_health, 2)
        data["spend"] = round(self.spend, 2)
        return data


@dataclass
class Scoreboard:
    pad_conflicts: int = 0
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
    def __init__(self, name: str, seed: int, fleet_limit: float):
        self.name = name
        self.fleet_limit = fleet_limit
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
        elif action in ("charge", "fast_charge"):
            vehicle.state = "charging"
            vehicle.charge_mode = "fast" if action == "fast_charge" else "normal"
        elif action == "depart":
            vehicle.assigned_pad = None
            vehicle.state = "cruising"
            vehicle.vibration = 0.0  # 패드에 있는 동안 정비를 받았습니다
        elif action == "divert_ground":
            vehicle.assigned_pad = None
            vehicle.state = "diverted"
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
            glide = CRUISE_ALT_M * (distance / APPROACH_RADIUS)
            vehicle.alt = max(0.0, min(vehicle.alt, glide), vehicle.alt - DESCENT_RATE_M)
            vehicle.alt = min(vehicle.alt, glide)
            return
        vehicle.alt = min(CRUISE_ALT_M, vehicle.alt + CLIMB_RATE_M)

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

    def _log(self, tick: int, kind: str, text: str) -> None:
        self.events.append({"tick": tick, "kind": kind, "text": text, "at": time.time()})
        del self.events[: max(0, len(self.events) - 40)]

    def snapshot(self, tick: int) -> dict:
        return {
            "world": self.name,
            "tick": tick,
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

    def __init__(self, seed: int = 7, fleet_limit: float = 500.0, tick_seconds: float = 0.2):
        self.seed = seed
        self.tick_seconds = tick_seconds
        self.tick_count = 0
        self.worlds = {
            "guarded": World("guarded", seed, fleet_limit),
            "direct": World("direct", seed, fleet_limit),
        }

    def step(self) -> None:
        self.tick_count += 1
        for world in self.worlds.values():
            world.tick(self.tick_count)

    def bulletins(self) -> list[dict]:
        if self.tick_count < RECALL_TICK:
            return []
        return [{**RECALL, "published_tick": RECALL_TICK}]

    def reset(self) -> None:
        limit = self.worlds["guarded"].fleet_limit
        self.tick_count = 0
        self.worlds = {
            "guarded": World("guarded", self.seed, limit),
            "direct": World("direct", self.seed, limit),
        }
