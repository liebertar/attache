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

    def covers(self, lat: float, lon: float) -> bool:
        return bool(self.polygon) and point_in_polygon(lat, lon, self.polygon)

    def contains(self, lat: float, lon: float, alt_m: float) -> bool:
        if not self.covers(lat, lon):
            return False
        if alt_m < self.floor_m:
            return False
        return self.ceiling_m is None or alt_m <= self.ceiling_m

    def breach(self, lat: float, lon: float, alt_m: float) -> str | None:
        """이 좌표·고도가 이 구역의 규칙을 어기는가. 어기면 왜인지 한 줄로."""
        if self.polygon and not self.covers(lat, lon):
            return None
        if not self.polygon:
            # 폴리곤 없는 구역은 기본 상한처럼 어디에나 걸리는 규칙입니다
            if self.rule == "ceiling" and self.ceiling_m is not None and alt_m > self.ceiling_m:
                return f"{self.name} 초과 ({alt_m:.0f}m > {self.ceiling_m:.0f}m)"
            return None
        if self.rule == "forbidden":
            if self.ceiling_m is None or self.floor_m <= alt_m <= self.ceiling_m:
                band = self.band()
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

    def forbidden_at(self, lat: float, lon: float, alt_m: float) -> bool:
        """이 좌표를 이 고도로 지날 수 있는가. 계획기와 런타임이 같이 씁니다.

        고도를 봐야 합니다. 건물은 옥상까지만 막고 그 위는 열려 있는데, 고도를 안 보면
        20m 짜리 건물도 영영 돌아가야 할 벽이 됩니다.
        폴리곤 판정 전에 상자로 거릅니다 — 탐색 한 번에 수십만 번 불리는 자리입니다.
        """
        if self._index_for != self.revision:
            self._rebuild_index()
        cell = (int(math.floor(lat / INDEX_CELL_DEG)), int(math.floor(lon / INDEX_CELL_DEG)))
        for volume, south, north, west, east in self._grid.get(cell, EMPTY):
            if (volume.rule == "forbidden" and south <= lat <= north
                    and west <= lon <= east
                    and (volume.ceiling_m is None or volume.floor_m <= alt_m <= volume.ceiling_m)
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
            for row in range(int(math.floor(min(lats) / INDEX_CELL_DEG)),
                             int(math.floor(max(lats) / INDEX_CELL_DEG)) + 1):
                for col in range(int(math.floor(min(lons) / INDEX_CELL_DEG)),
                                 int(math.floor(max(lons) / INDEX_CELL_DEG)) + 1):
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


SAMPLE_EVERY_M = 8.0     # 표본 간격. 가장 작은 건물(약 20m)보다 촘촘해야 합니다
MIN_SAMPLES = 4         # 아주 짧은 구간에도 양 끝 말고 몇 점은 봅니다
MAX_SAMPLES = 4000


def _leg_samples(here: dict, nxt: dict) -> int:
    """구간 길이에 맞춰 표본 수를 정합니다.

    길이와 무관하게 40개만 찍으면, 2km 구간은 50m마다 보게 됩니다. FAA 격자(약 900m)
    에는 충분했지만 건물은 30~60m 라 통째로 건너뜁니다. 판정자가 못 본 것은 아무도
    못 봅니다 — 계획기도 이 함수를 쓰기 때문입니다.
    """
    north = (nxt["lat"] - here["lat"]) * 110_570.0
    east = (nxt["lon"] - here["lon"]) * 84_400.0
    metres = (north * north + east * east) ** 0.5
    return max(MIN_SAMPLES, min(MAX_SAMPLES, int(metres / SAMPLE_EVERY_M) + 1))


def first_breach(airspace: "Airspace", legs: list[dict], samples: int | None = None):
    """경로에서 처음으로 규정을 어기는 지점. 런타임과 계획기가 같은 함수를 씁니다.

    같은 판정을 두 곳에 따로 적으면 반드시 갈라집니다. 계획기는 통과라고 보고 런타임은
    거절하는 상태가 되고, 그러면 아무 경로도 승인되지 않습니다. 실제로 그렇게 됐었습니다.
    """
    for index in range(len(legs) - 1):
        here, nxt = legs[index], legs[index + 1]
        altitude = float(nxt.get("alt_m") or here.get("alt_m") or 0.0)
        steps = samples if samples is not None else _leg_samples(here, nxt)
        for step in range(steps + 1):
            fraction = step / steps
            lat = here["lat"] + (nxt["lat"] - here["lat"]) * fraction
            lon = here["lon"] + (nxt["lon"] - here["lon"]) * fraction
            volume = airspace.breach(lat, lon, altitude)
            if volume is not None:
                # 부딪힌 자리까지 같이 돌려줍니다. 화면이 '어디서' 막혔는지 그릴 수
                # 있어야 승인·거절이 눈에 보입니다.
                return index + 1, volume, volume.breach(lat, lon, altitude), (lat, lon)
    return None
