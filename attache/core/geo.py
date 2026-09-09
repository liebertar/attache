"""Airspace volumes: a footprint on the ground plus a slab of altitude.

Real zone data looks like this and not like a circle. The FAA's UAS Facility Maps are a
grid of cells each carrying its own ceiling; EUROCAE ED-269 zones carry `lowerLimit`,
`upperLimit` and a vertical reference. So one neighbourhood is not one rule: a river
corridor, an apartment block and a school can each sit under a different ceiling.

Heights are metres. AGL is height above the ground under the aircraft, AMSL is height
above sea level. Converting between them needs terrain, which is why a volume says which
one it means instead of pretending they are the same.
"""

import math
from dataclasses import dataclass, field

INDEX_CELL_DEG = 0.002   # 색인 격자 한 칸. 위도로 약 220m
EMPTY: list = []
# 금지 구역 옆으로 띄워야 하는 거리. 선분이 건물 모서리를 0.5m 로 스치면 판정은 통과인데
# 화면의 18m 회랑은 건물을 뚫고 지나갑니다. 회랑 반폭(9m)보다 큽니다.
# 격자·폐쇄 구역은 900m 짜리 칸이라 10m 로 스치면 두 붉은 칸 사이를 칼끝으로 지나는 그림이
# 됩니다. 규제 구역은 더 멀리 돕니다.
SEPARATION_M = 10.0
ZONE_SEPARATION_M = 40.0
# 건물 위로 지날 때 옥상에서 이만큼은 떠 있어야 합니다. 유인기는 14 CFR 91.119 가 장애물 위
# 1,000ft/500ft 를 요구하고 드론은 Part 107 에 최소 이격이 없어서, 운영 규격으로 50m 를 둡니다.
# 건물 Volume 의 clearance_m 로 실립니다 — 판정에서는 옥상 + 이격까지가 그 건물입니다.
VERTICAL_CLEARANCE_M = 50.0
# 내려앉는 자리 둘레에 이만큼은 건물이 없어야 합니다. 옆으로 10m 만 띄운 자리는 순항으로 지나갈 수는
# 있어도 90m 를 수직으로 내려오기에는 탑 사이 골짜기입니다. 착륙 지점은 경로의 끝점이고, 런타임이
# 경로를 판정할 때 끝점도 같이 봅니다.
LANDING_SEPARATION_M = 50.0
# 기체 사이의 분리 최소치. 수평 30m·수직 25m 안에 두 기체가 같은 틱에 있으면 분리 상실입니다.
# EU U-space(CORUS) 와 NASA UTM TCL 시연이 소형 무인기에 쓴 값을 그대로 둡니다 — 유인기의
# 3NM/1,000ft 를 기체 크기·속도로 줄인 규모이고, 런타임의 의도(4D) 회랑 폭과 시뮬레이터의
# 분리 상실 계측이 같은 숫자를 씁니다. 두 곳이 다른 숫자를 쓰면 판정과 계측이 갈립니다.
TRAFFIC_LATERAL_M = 30.0
TRAFFIC_VERTICAL_M = 25.0
# 수직 구간(이륙 기둥·꼭짓점 승강·착륙 기둥)을 판정할 때의 표본 간격. 건물은 옥상 + 이격까지
# 막으니 5m 마다 보면 어떤 건물 띠도 안 빠집니다(띠는 최소 50m).
COLUMN_STEP_M = 5.0
# 색인 격자에서 선분 하나가 걸칠 수 있는 최대 칸 수. 50km 대각선이 13만 칸쯤이라 넉넉합니다.
# 유한하기만 한 좌표(1e300)로 온 선분은 칸이 1e600 개라 판정이 영영 안 끝났습니다 — 그 사이
# 런타임 스레드가 GIL 을 잡고 있어 세계·중재 스레드가 굶습니다. 셀 수 없이 긴 선분은 판정을
# 거부합니다. 양식 검사(runtime.service)가 먼저 거르고, 여기는 마지막 방어선입니다.
MAX_LEG_CELLS = 1_000_000


def separation_for(volume: "Volume") -> float:
    return SEPARATION_M if volume.id.startswith("bldg-") else ZONE_SEPARATION_M


def ground_clamped(alt_m: float) -> float:
    """땅 밑은 땅입니다. 건물은 바닥이 0m 라 -1m 는 '건물 아래'가 아니라 건물 안입니다.

    바닥과 천장을 구간으로만 보면 음수 고도가 모든 구역의 바깥이 되어, -1m 로 건물 한가운데를
    지나는 경로가 판정을 통과했습니다.
    """
    return alt_m if alt_m > 0.0 else 0.0


METRES_PER_DEG_LAT = 110_570.0
METRES_PER_DEG_LON = 84_400.0    # 위도 40.7도 기준


@dataclass
class Volume:
    id: str
    name: str
    polygon: list[tuple[float, float]]      # [(lat, lon), ...] 닫히지 않아도 됩니다
    floor_m: float = 0.0
    ceiling_m: float | None = None          # None 이면 위로 끝까지
    reference: str = "AGL"                  # "AGL" 또는 "AMSL"
    rule: str = "forbidden"                 # forbidden | ceiling | permitted
    reason: str = ""
    source: str = ""                        # 어느 기관 데이터에서 왔는지
    tags: dict = field(default_factory=dict)
    clearance_m: float = 0.0                # 위로 더 비워야 하는 높이. 건물이면 옥상 위 이격
    # 유효기간(틱). 공지로 온 구역은 언제부터 언제까지인지가 있습니다. 판정은 '지금' 만 보므로
    # 런타임이 창이 열릴 때 넣고 닫힐 때 뺍니다 — 여기 적힌 것은 그 근거입니다.
    from_tick: int | None = None
    until_tick: int | None = None

    @property
    def top_m(self) -> float | None:
        """이 구역이 실제로 막는 높이. 천장이 없으면 끝까지입니다."""
        return None if self.ceiling_m is None else self.ceiling_m + self.clearance_m

    def covers(self, lat: float, lon: float) -> bool:
        return bool(self.polygon) and point_in_polygon(lat, lon, self.polygon)

    def contains(self, lat: float, lon: float, alt_m: float) -> bool:
        if not self.covers(lat, lon):
            return False
        alt_m = ground_clamped(alt_m)
        if alt_m < self.floor_m:
            return False
        return self.top_m is None or alt_m <= self.top_m

    def breach(self, lat: float, lon: float, alt_m: float) -> str | None:
        """이 좌표·고도가 이 구역의 규칙을 어기는가. 어기면 왜인지 한 줄로."""
        alt_m = ground_clamped(alt_m)
        if self.polygon and not self.covers(lat, lon):
            return None
        if not self.polygon:
            # 폴리곤 없는 구역은 기본 상한처럼 어디에나 걸리는 규칙입니다
            if self.rule == "ceiling" and self.ceiling_m is not None and alt_m > self.ceiling_m:
                return f"{self.name} 초과 ({alt_m:.0f}m > {self.ceiling_m:.0f}m)"
            return None
        if self.rule == "forbidden":
            if self.top_m is None or self.floor_m <= alt_m <= self.top_m:
                band = self.band()
                if self.clearance_m and self.ceiling_m is not None and alt_m > self.ceiling_m:
                    return (f"{self.name} 옥상 위 {alt_m - self.ceiling_m:.0f}m "
                            f"(이격 {self.clearance_m:.0f}m 필요)")
                return f"{self.name} 진입 금지 ({band})"
            return None
        if self.rule == "ceiling" and self.ceiling_m is not None and alt_m > self.ceiling_m:
            return f"{self.name} 허용 고도 초과 ({alt_m:.0f}m > {self.ceiling_m:.0f}m {self.reference})"
        return None

    def band(self) -> str:
        """이 구역이 걸리는 고도 띠. 화면이 문장을 다시 뜯지 않도록 따로 냅니다."""
        top = "제한 없음" if self.ceiling_m is None else f"{self.ceiling_m:.0f}m"
        return f"{self.floor_m:.0f}~{top} {self.reference}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "polygon": [[lat, lon] for lat, lon in self.polygon],
            "floor_m": self.floor_m, "ceiling_m": self.ceiling_m,
            "reference": self.reference, "rule": self.rule,
            "reason": self.reason, "source": self.source, "tags": dict(self.tags),
            "clearance_m": self.clearance_m,
            "from_tick": self.from_tick, "until_tick": self.until_tick,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Volume":
        return cls(
            id=raw["id"], name=raw.get("name", raw["id"]),
            polygon=[(float(a), float(b)) for a, b in raw["polygon"]],
            floor_m=float(raw.get("floor_m", 0.0)),
            ceiling_m=None if raw.get("ceiling_m") is None else float(raw["ceiling_m"]),
            reference=raw.get("reference", "AGL"),
            rule=raw.get("rule", "forbidden"),
            reason=raw.get("reason", ""), source=raw.get("source", ""),
            tags=raw.get("tags", {}),
            clearance_m=float(raw.get("clearance_m", 0.0)),
            from_tick=None if raw.get("from_tick") is None else int(raw["from_tick"]),
            until_tick=None if raw.get("until_tick") is None else int(raw["until_tick"]),
        )


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray casting. 구역이 작아서 평면으로 봐도 됩니다.

    도시 한 구역 크기(수 km)에서 위경도를 평면 좌표처럼 다뤄도 오차가 미터 단위입니다.
    나라 하나를 덮는 구역이라면 측지 계산이 필요합니다.
    """
    if len(polygon) < 3:
        return False
    inside = False
    count = len(polygon)
    for index in range(count):
        lat_a, lon_a = polygon[index]
        lat_b, lon_b = polygon[(index + 1) % count]
        if (lat_a > lat) != (lat_b > lat):
            crossing = (lon_b - lon_a) * (lat - lat_a) / (lat_b - lat_a) + lon_a
            if lon < crossing:
                inside = not inside
    return inside


def box(lat_min: float, lon_min: float, lat_max: float, lon_max: float):
    return [(lat_min, lon_min), (lat_min, lon_max), (lat_max, lon_max), (lat_max, lon_min)]


# Part 107 기본 상한. 격자가 없는 곳은 규칙이 없는 게 아니라 이 값이 적용됩니다.
# 400 ft AGL = 121.92 m. 이걸 빼먹으면 아무 데나 마음껏 나는 것처럼 모델링됩니다.
DEFAULT_CEILING_M = 121.9


class Airspace:
    """활성 구역 묶음. 신청과 위치를 둘 다 여기에 물어봅니다."""

    def __init__(self, volumes: list[Volume] | None = None,
                 default_ceiling_m: float | None = DEFAULT_CEILING_M):
        self._volumes: dict[str, Volume] = {v.id: v for v in (volumes or [])}
        self.default_ceiling_m = default_ceiling_m
        # 구역이 하나 열리고 하나 닫히면 개수는 그대로입니다. 캐시를 개수로 무효화하면
        # 그 순간을 놓칩니다. 그래서 바뀔 때마다 올라가는 번호를 둡니다.
        self.revision = 0
        self._grid: dict[tuple[int, int], list[Volume]] = {}
        self._everywhere: list[Volume] = []
        self._index_for = -1

    def add(self, volume: Volume) -> None:
        self._volumes[volume.id] = volume
        self.revision += 1

    def remove(self, volume_id: str) -> None:
        if self._volumes.pop(volume_id, None) is not None:
            self.revision += 1

    def all(self) -> list[Volume]:
        return list(self._volumes.values())

    def near(self, lat: float, lon: float) -> list[Volume]:
        """이 좌표에 걸릴 수 있는 구역만. 나머지는 볼 필요가 없습니다.

        격자 한 칸(0.002도, 약 220m)에 걸치는 구역을 미리 담아둡니다. 건물을 넣으면
        구역이 200개에서 3천 개가 넘어가는데, 표본마다 전부 훑으면 경로 하나 검사에
        수십만 번 폴리곤 판정이 들어갑니다. 답은 그대로이고 보는 개수만 줄입니다.
        """
        if self._index_for != self.revision:
            self._rebuild_index()
        cell = (int(math.floor(lat / INDEX_CELL_DEG)), int(math.floor(lon / INDEX_CELL_DEG)))
        boxes = self._grid.get(cell)
        if not boxes:
            return list(self._everywhere)
        return [box[0] for box in boxes] + self._everywhere

    def landing_breach(self, lat: float, lon: float) -> tuple["Volume", float] | None:
        """여기에 내려앉을 수 있는가. 땅에서 금지 구역·건물이 착륙 둘레(50m) 안에 있으면 안 됩니다."""
        point = {"lat": lat, "lon": lon}
        worst = None
        for volume in self.near(lat, lon):
            if volume.rule != "forbidden" or not volume.polygon or volume.floor_m > 0.0:
                continue
            gap = 0.0 if volume.covers(lat, lon) else _clearance_m(point, point, volume.polygon)[0]
            needed = max(LANDING_SEPARATION_M, separation_for(volume))
            if gap < needed and (worst is None or gap < worst[1]):
                worst = (volume, gap)
        return worst

    def too_close(self, lat: float, lon: float, alt_m: float) -> bool:
        """이 점이 금지 구역 안이거나 이격 거리 안인가. first_breach 와 같은 기준입니다."""
        if self.forbidden_at(lat, lon, alt_m):
            return True
        alt_m = ground_clamped(alt_m)
        point = {"lat": lat, "lon": lon}
        for volume in self.near(lat, lon):
            if (volume.rule != "forbidden" or not volume.polygon or alt_m < volume.floor_m
                    or (volume.top_m is not None and alt_m > volume.top_m)):
                continue
            if _clearance_m(point, point, volume.polygon)[0] < separation_for(volume):
                return True
        return False

    def forbidden_at(self, lat: float, lon: float, alt_m: float) -> bool:
        """이 좌표를 이 고도로 지날 수 있는가. 계획기와 런타임이 같이 씁니다.

        고도를 봐야 합니다. 건물은 옥상까지만 막고 그 위는 열려 있는데, 고도를 안 보면
        20m 짜리 건물도 영영 돌아가야 할 벽이 됩니다.
        폴리곤 판정 전에 상자로 거릅니다 — 탐색 한 번에 수십만 번 불리는 자리입니다.
        """
        if self._index_for != self.revision:
            self._rebuild_index()
        alt_m = ground_clamped(alt_m)
        cell = (int(math.floor(lat / INDEX_CELL_DEG)), int(math.floor(lon / INDEX_CELL_DEG)))
        for volume, south, north, west, east in self._grid.get(cell, EMPTY):
            if (volume.rule == "forbidden" and south <= lat <= north
                    and west <= lon <= east
                    and (volume.top_m is None or volume.floor_m <= alt_m <= volume.top_m)
                    and volume.covers(lat, lon)):
                return True
        return any(v.rule == "forbidden" and v.breach(lat, lon, alt_m)
                   for v in self._everywhere)

    def _rebuild_index(self) -> None:
        grid: dict[tuple[int, int], list[Volume]] = {}
        everywhere: list[Volume] = []
        for volume in self._volumes.values():
            if not volume.polygon:
                everywhere.append(volume)   # 폴리곤 없는 규칙은 어디에나 걸립니다
                continue
            lats = [point[0] for point in volume.polygon]
            lons = [point[1] for point in volume.polygon]
            box = (volume, min(lats), max(lats), min(lons), max(lons))
            # 이격 거리만큼 넓혀서 담습니다. 한 칸만 보는 near() 가 옆 칸과의 거리도 재야 합니다.
            widest = max(ZONE_SEPARATION_M, LANDING_SEPARATION_M)
            pad_lat = widest / METRES_PER_DEG_LAT
            pad_lon = widest / METRES_PER_DEG_LON
            for row in range(int(math.floor((min(lats) - pad_lat) / INDEX_CELL_DEG)),
                             int(math.floor((max(lats) + pad_lat) / INDEX_CELL_DEG)) + 1):
                for col in range(int(math.floor((min(lons) - pad_lon) / INDEX_CELL_DEG)),
                                 int(math.floor((max(lons) + pad_lon) / INDEX_CELL_DEG)) + 1):
                    grid.setdefault((row, col), []).append(box)
        self._grid = grid
        self._everywhere = everywhere
        self._index_for = self.revision

    def breach(self, lat: float | None, lon: float | None, alt_m: float) -> Volume | None:
        """어기는 구역 중 첫 번째. 금지가 고도 제한보다 먼저입니다."""
        if lat is None or lon is None:
            return None
        ordered = sorted(self.near(lat, lon), key=lambda v: v.rule != "forbidden")
        for volume in ordered:
            if volume.breach(lat, lon, alt_m):
                return volume
        if self.default_ceiling_m is not None and alt_m > self.default_ceiling_m:
            return Volume(
                id="part107-default", name="Part 107 기본 상한",
                polygon=[], ceiling_m=self.default_ceiling_m, rule="ceiling",
                reason="격자가 없는 곳의 기본 상한 400ft AGL", source="14 CFR 107.51",
            )
        return None

    def ceiling_at(self, lat: float, lon: float) -> float | None:
        """여기서 올라갈 수 있는 최대 고도. 겹치면 제일 낮은 천장을 따릅니다."""
        ceilings = [
            v.ceiling_m for v in self.near(lat, lon)
            if v.rule == "ceiling" and v.covers(lat, lon) and v.ceiling_m is not None
        ]
        if self.default_ceiling_m is not None:
            ceilings.append(self.default_ceiling_m)
        return min(ceilings) if ceilings else None


def _leg_volumes(airspace: Airspace, here: dict, nxt: dict):
    """선분의 경계 상자(이격 거리만큼 넓힌)에 걸친 후보만 모읍니다. 판정은 first_breach 뿐입니다."""
    if airspace._index_for != airspace.revision:
        airspace._rebuild_index()
    south, north = sorted((here["lat"], nxt["lat"]))
    west, east = sorted((here["lon"], nxt["lon"]))
    widest = max(SEPARATION_M, ZONE_SEPARATION_M)
    south -= widest / METRES_PER_DEG_LAT
    north += widest / METRES_PER_DEG_LAT
    west -= widest / METRES_PER_DEG_LON
    east += widest / METRES_PER_DEG_LON
    rows = range(math.floor(south / INDEX_CELL_DEG), math.floor(north / INDEX_CELL_DEG) + 1)
    cols = range(math.floor(west / INDEX_CELL_DEG), math.floor(east / INDEX_CELL_DEG) + 1)
    if len(rows) * len(cols) > MAX_LEG_CELLS:
        raise ValueError(f"선분이 너무 길어 판정할 수 없습니다 ({len(rows)}x{len(cols)} 칸)")
    candidates = {v.id: v for v in airspace._everywhere}
    for row in rows:
        for col in cols:
            for volume, lo_lat, hi_lat, lo_lon, hi_lon in airspace._grid.get((row, col), EMPTY):
                if lo_lat <= north and hi_lat >= south and lo_lon <= east and hi_lon >= west:
                    candidates[volume.id] = volume
    return candidates.values()


def _crossing_fractions(here: dict, nxt: dict, polygon: list[tuple[float, float]]):
    """다각형의 변을 지나는 비율. 교차점 사이에서는 안/밖이 바뀌지 않습니다."""
    lat, lon = here["lat"], here["lon"]
    dy, dx = nxt["lat"] - lat, nxt["lon"] - lon
    cuts = {0.0, 1.0}
    for i, (ay, ax) in enumerate(polygon):
        by, bx = polygon[(i + 1) % len(polygon)]
        ey, ex = by - ay, bx - ax
        denominator = dx * ey - dy * ex
        if abs(denominator) < 1e-20:
            # 같은 직선 위의 변도 끝점에서 안/밖이 바뀔 수 있습니다.
            if abs((ax - lon) * dy - (ay - lat) * dx) < 1e-20:
                for py, px in ((ay, ax), (by, bx)):
                    if abs(dx) > abs(dy):
                        t = (px - lon) / dx
                    else:
                        t = (py - lat) / dy if dy else 0
                    if 0 <= t <= 1:
                        cuts.add(t)
            continue
        t = ((ax - lon) * ey - (ay - lat) * ex) / denominator
        u = ((ax - lon) * dy - (ay - lat) * dx) / denominator
        if 0 <= t <= 1 and 0 <= u <= 1:
            cuts.add(t)
    return sorted(cuts)


def _point_segment_m(lat: float, lon: float, a: tuple[float, float],
                     b: tuple[float, float]) -> tuple[float, float]:
    """점에서 선분까지의 거리(m)와, 선분 위 가장 가까운 점의 비율."""
    ax, ay = (a[1] - lon) * METRES_PER_DEG_LON, (a[0] - lat) * METRES_PER_DEG_LAT
    bx, by = (b[1] - lon) * METRES_PER_DEG_LON, (b[0] - lat) * METRES_PER_DEG_LAT
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    return math.hypot(ax + t * dx, ay + t * dy), t


def _clearance_m(here: dict, nxt: dict, polygon: list[tuple[float, float]]) -> tuple[float, float]:
    """선분과 다각형 경계 사이의 최소 거리(m)와 그 자리의 선분 비율.

    두 선분이 안 만나면 가장 가까운 자리는 어느 한쪽의 끝점입니다. 그래서 선분 양 끝에서
    변까지, 변 양 끝에서 선분까지 넷만 보면 됩니다. 만나는 경우는 first_breach 의 교차
    판정이 먼저 잡습니다.
    """
    a, b = (here["lat"], here["lon"]), (nxt["lat"], nxt["lon"])
    best = (float("inf"), 0.0)
    for i, edge_a in enumerate(polygon):
        edge_b = polygon[(i + 1) % len(polygon)]
        for point, fraction in ((a, 0.0), (b, 1.0)):
            distance, _ = _point_segment_m(point[0], point[1], edge_a, edge_b)
            if distance < best[0]:
                best = (distance, fraction)
        for corner in (edge_a, edge_b):
            distance, t = _point_segment_m(corner[0], corner[1], a, b)
            if distance < best[0]:
                best = (distance, t)
    return best


def nearest_exit(volume: Volume, lat: float, lon: float,
                 margin_m: float | None = None) -> tuple[float, float] | None:
    """구역 안에 있는 점에서 가장 가까운 바깥 자리. 밖에 있으면 None.

    구역이 닫혔을 때 안에 있던 기체는 나가야 합니다. 제자리에 떠 있으면 닫힌 구역 안에
    계속 머무는 것이고, 경로를 다시 그리려 해도 출발점이 금지 구역이라 길이 안 나옵니다.
    가장 가까운 경계로 나가서 이격 거리만큼 더 벗어난 자리가 답입니다.
    """
    if not volume.polygon or not volume.covers(lat, lon):
        return None
    if margin_m is None:
        margin_m = separation_for(volume) + 5.0
    best = (float("inf"), None)
    for i, edge_a in enumerate(volume.polygon):
        edge_b = volume.polygon[(i + 1) % len(volume.polygon)]
        distance, t = _point_segment_m(lat, lon, edge_a, edge_b)
        if distance < best[0]:
            best = (distance, (edge_a[0] + (edge_b[0] - edge_a[0]) * t,
                               edge_a[1] + (edge_b[1] - edge_a[1]) * t))
    distance, (door_lat, door_lon) = best
    north = (door_lat - lat) * METRES_PER_DEG_LAT
    east = (door_lon - lon) * METRES_PER_DEG_LON
    length = math.hypot(north, east) or 1.0
    push = (distance + margin_m) / length
    return (lat + north * push / METRES_PER_DEG_LAT, lon + east * push / METRES_PER_DEG_LON)


def highest_roof_along(airspace: "Airspace", here: dict, nxt: dict,
                       margin_m: float = SEPARATION_M) -> float:
    """이 선분 아래(옆 이격 안까지)에서 가장 높은 옥상. 건물이 없으면 0.

    운영사가 구간 고도를 정할 때 씁니다 — 옥상 + 이격만큼 떠야 그 위를 지날 수 있고,
    그게 천장을 넘으면 옆으로 돌아야 합니다. 판정(first_breach)은 이 값을 믿지 않고 따로 봅니다.
    """
    top = 0.0
    for volume in _leg_volumes(airspace, here, nxt):
        if not volume.id.startswith("bldg-") or volume.ceiling_m is None or not volume.polygon:
            continue
        if volume.ceiling_m <= top:
            continue
        crosses = len(_crossing_fractions(here, nxt, volume.polygon)) > 2
        if crosses or _clearance_m(here, nxt, volume.polygon)[0] < margin_m:
            top = volume.ceiling_m
    return top


def first_breach(airspace: "Airspace", legs: list[dict], samples: int | None = None):
    """선분이 규정을 어기는 첫 구간. 런타임과 계획기가 같은 함수를 씁니다.

    고정 간격 표본은 건물 모서리의 짧은 관통을 놓칩니다. 다각형 경계에서 선분을
    나눈 뒤 각 구간을 검사합니다. samples는 기존 호출 호환용이며 정확도를 낮추지 않습니다.
    """
    for index in range(len(legs) - 1):
        here, nxt = legs[index], legs[index + 1]
        altitude = ground_clamped(float(nxt.get("alt_m", here.get("alt_m", 0.0))))
        first = None
        for volume in _leg_volumes(airspace, here, nxt):
            cuts = _crossing_fractions(here, nxt, volume.polygon)
            probes = sorted(set(cuts + [(a + b) / 2 for a, b in zip(cuts, cuts[1:], strict=False)]))
            hit = False
            for fraction in probes:
                if first is not None and fraction >= first[0]:
                    break
                lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
                lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
                reason = volume.breach(lat, lon, altitude)
                if reason:
                    first = (fraction, volume, reason, (lat, lon))
                    hit = True
                    break
            # 안 들어가도 너무 붙으면 안 됩니다. 금지 구역이고 그 고도가 구역의 높이 안일 때만.
            if (not hit and volume.polygon and volume.rule == "forbidden"
                    and volume.floor_m <= altitude
                    and (volume.top_m is None or altitude <= volume.top_m)):
                gap, fraction = _clearance_m(here, nxt, volume.polygon)
                needed = separation_for(volume)
                if gap < needed and (first is None or fraction < first[0]):
                    lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
                    lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
                    first = (fraction, volume,
                             f"{volume.name} 에 {gap:.0f}m 로 접근 (이격 {needed:.0f}m 필요)",
                             (lat, lon))
        # 폴리곤 없는 기본 천장도 같은 진입점으로 판정합니다.
        start_breach = airspace.breach(here["lat"], here["lon"], altitude)
        if start_breach is not None:
            return (index + 1, start_breach,
                    start_breach.breach(here["lat"], here["lon"], altitude),
                    (here["lat"], here["lon"]))
        if first is not None:
            return index + 1, first[1], first[2], first[3]
    return None


def vertical_column(lat: float, lon: float, from_m: float, to_m: float,
                    step_m: float = COLUMN_STEP_M) -> list[dict]:
    """한 자리에서 오르내리는 구간을 판정 함수에 넣을 모양으로.

    멀티로터는 꼭짓점에서 제자리로 오르내립니다(sim World._at_cruise). 그 수직선도 공역을
    지나는 선이라 판정을 받아야 합니다 — 출발점 옆 60m 건물은 120m 순항 구간은 안 막지만
    0m 에서 120m 로 올라가는 기둥은 막습니다. 길이 0 인 구간을 고도만 바꿔 이어 붙이면
    first_breach 가 구간마다 그 고도로 판정하므로 판정 코드가 늘지 않습니다(G7).
    """
    lo, hi = sorted((ground_clamped(from_m), ground_clamped(to_m)))
    altitudes = [lo]
    while altitudes[-1] + step_m < hi:
        altitudes.append(altitudes[-1] + step_m)
    if hi > lo:
        altitudes.append(hi)
    if from_m > to_m:
        altitudes.reverse()
    if len(altitudes) == 1:
        altitudes.append(altitudes[0])
    return [{"lat": lat, "lon": lon, "alt_m": altitude} for altitude in altitudes]
