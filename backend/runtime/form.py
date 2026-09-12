"""What a filing must look like before anyone judges it.

The leaf of backend/runtime: it imports nothing from the package, so the five modules that
need ROUTED never take it from a sibling mixin.
"""

import math

from shared.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON

# Longest allowed leg. The service radius is 11 km, so no route inside it has a longer leg.
# A half-globe leg with merely finite coordinates made the judgement walk 1e10 index-grid
# cells and never finish, while the runtime thread held the GIL and the world and arbitration
# froze. It is a form problem, caught before judgement.
MAX_LEG_M = 50_000.0
# Actions that carry a route. Only these are judged against airspace and intents.
ROUTED = ("reserve_pad", "fly_route")
# Refusals the advisory does not count as consecutive refusals. The path is not blocked: the
# same filing was sent twice (duplicate), or the runtime is not ready to judge yet
# (airspace_not_loaded).
NOT_REFUSALS = ("duplicate", "airspace_not_loaded")


def _form_problem(legs) -> str | None:
    """Whether legs are in a form judgement can take, and if not, what's wrong. A form
    check, not a judgement.

    Coordinates must be on Earth (|lat| ≤ 90, |lon| ≤ 180), altitude above ground (≥ 0), and
    each leg at most MAX_LEG_M. Merely finite values don't qualify: a negative altitude slipped
    'under' every zone and flew through buildings, and a 1e300 coordinate made judgement never
    finish.
    """
    if not isinstance(legs, list) or len(legs) < 2:
        return "legs 는 둘 이상의 점 목록"
    previous = None
    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            return f"{index}번 점이 객체가 아님"
        try:
            lat, lon, alt = float(leg["lat"]), float(leg["lon"]), float(leg.get("alt_m", 0.0))
        except (KeyError, TypeError, ValueError):
            return f"{index}번 점에 숫자 lat/lon/alt_m 가 없음"
        if not all(math.isfinite(value) for value in (lat, lon, alt)):
            return f"{index}번 점이 유한한 수가 아님"
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return f"{index}번 점이 지구 위 좌표가 아님"
        if alt < 0.0:
            return f"{index}번 점의 고도가 땅 밑 ({alt:.0f}m)"
        if previous is not None:
            length = math.hypot((lat - previous[0]) * METRES_PER_DEG_LAT,
                                (lon - previous[1]) * METRES_PER_DEG_LON)
            if length > MAX_LEG_M:
                return (f"{index - 1}번 구간이 너무 김 "
                        f"({length / 1000:.0f}km > {MAX_LEG_M / 1000:.0f}km)")
        previous = (lat, lon)
    return None
