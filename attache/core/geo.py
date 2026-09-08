"""Airspace volumes: a footprint on the ground plus a slab of altitude.

Real zone data looks like this and not like a circle. The FAA's UAS Facility Maps are a
grid of cells each carrying its own ceiling; EUROCAE ED-269 zones carry `lowerLimit`,
`upperLimit` and a vertical reference. So one neighbourhood is not one rule: a river
corridor, an apartment block and a school can each sit under a different ceiling.

Heights are metres. AGL is height above the ground under the aircraft, AMSL is height
above sea level. Converting between them needs terrain, which is why a volume says which
one it means instead of pretending they are the same.
"""

from dataclasses import dataclass, field


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
        return point_in_polygon(lat, lon, self.polygon)

    def contains(self, lat: float, lon: float, alt_m: float) -> bool:
        if not self.covers(lat, lon):
            return False
        if alt_m < self.floor_m:
            return False
        return self.ceiling_m is None or alt_m <= self.ceiling_m

    def breach(self, lat: float, lon: float, alt_m: float) -> str | None:
        """이 좌표·고도가 이 구역의 규칙을 어기는가. 어기면 왜인지 한 줄로."""
        if not self.covers(lat, lon):
            return None
        if self.rule == "forbidden":
            if self.ceiling_m is None or self.floor_m <= alt_m <= self.ceiling_m:
                band = self._band()
                return f"{self.name} 진입 금지 ({band})"
            return None
        if self.rule == "ceiling" and self.ceiling_m is not None and alt_m > self.ceiling_m:
            return f"{self.name} 허용 고도 초과 ({alt_m:.0f}m > {self.ceiling_m:.0f}m {self.reference})"
        return None

    def _band(self) -> str:
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


class Airspace:
    """활성 구역 묶음. 신청과 위치를 둘 다 여기에 물어봅니다."""

    def __init__(self, volumes: list[Volume] | None = None):
        self._volumes: dict[str, Volume] = {v.id: v for v in (volumes or [])}

    def add(self, volume: Volume) -> None:
        self._volumes[volume.id] = volume

    def remove(self, volume_id: str) -> None:
        self._volumes.pop(volume_id, None)

    def all(self) -> list[Volume]:
        return list(self._volumes.values())

    def breach(self, lat: float | None, lon: float | None, alt_m: float) -> Volume | None:
        """어기는 구역 중 첫 번째. 금지가 고도 제한보다 먼저입니다."""
        if lat is None or lon is None:
            return None
        ordered = sorted(self._volumes.values(), key=lambda v: v.rule != "forbidden")
        for volume in ordered:
            if volume.breach(lat, lon, alt_m):
                return volume
        return None

    def ceiling_at(self, lat: float, lon: float) -> float | None:
        """여기서 올라갈 수 있는 최대 고도. 겹치면 제일 낮은 천장을 따릅니다."""
        ceilings = [
            v.ceiling_m for v in self._volumes.values()
            if v.rule == "ceiling" and v.covers(lat, lon) and v.ceiling_m is not None
        ]
        return min(ceilings) if ceilings else None
