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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from attache.core.geo import (
    DEFAULT_CEILING_M,
    TRAFFIC_LATERAL_M,
    TRAFFIC_VERTICAL_M,
    VERTICAL_CLEARANCE_M,
    Airspace,
    Volume,
    building_clearance_m,
)
from attache.core.notam import Clock, parse_notice

# 물류 기지는 브루클린 네이비야드. 배달지는 이스트강 건너 맨해튼입니다.
# 실제 배송 기업이 도심 배달 거점을 두는 자리이고, 강을 건너야 해서 헬리포트 주변
# 0ft 구역을 지나게 됩니다. 그게 이 데모의 전부입니다.
DEPOT = (47.8, 54.9)                       # 브루클린 네이비야드 40.702,-73.970
# 이륙장 하나. 창고 바로 옆입니다. 두 자리를 350m 떨어뜨려 놨더니 한 거점으로
# 안 읽혔고, 자리가 남으면 두 기체가 다툴 일도 없어 잠금표와 중재가 놀았습니다.
# 비상 착륙대(모터 고장 때만). 창고 옥상 자리와 떨어진 마당 동쪽에 둡니다 — 옥상 자리와 겹치면
# 고장 기체가 옆 자리 기체 위로 내리려다 거절만 받습니다.
PADS = {"pad:launch": (48.55, 54.95)}
# 창고 마당의 자리. 기체마다 제 자리가 있고, 한 줄로 24m 씩 떨어져 있습니다.
# 여기서 하루를 시작하고(싣는 중), 배달을 마치면 여기로 돌아와 땅에서 다음 차례를 기다립니다.
# 이륙장(충전대)은 하나뿐이라 자원이고, 마당 자리는 자원이 아니라 잠금이 없습니다.
# 이륙장을 돌아오는 비행 내내 잡고 있으면 나머지 기체가 배달지에서 몇 분씩 서 있었고,
# 한 점에 겹쳐 내리면 네 대가 한 대로 보였습니다.
# 자리는 창고 옥상 위 한 줄입니다(긴 축 97 m 를 따라 31 m 간격, 바깥 둘은 옥상 가장자리에 걸침).
# 마당에 두었더니 창고 옆 아무 데나 앉는 것처럼 보였습니다. 31 m 는 서 있는 기체와의 이격(30 m)
# 바로 밖이라 옆 자리에 내릴 수 있고, 이륙 기둥(회랑 폭 40 m)은 겹쳐서 동시에 뜨면 런타임이 한 대를
# 기다리게 합니다 — 그게 맞는 그림입니다. 옥상 높이는 화면이 올려 그리는 데만 씁니다(시뮬 고도는
# 지면 기준).
SEATS = [
    (47.4462, 54.9415),   # 40.701803, -73.970437,
    (47.6652, 55.0284),   # 40.7016, -73.970185,
    (47.8842, 55.1153),   # 40.701398, -73.969933,
    (48.1032, 55.2021),   # 40.701195, -73.969681,
]
SEAT_ROOF_M = 9.0


def seat_of(index: int) -> tuple[float, float]:
    return SEATS[index % len(SEATS)]

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
# 항속. 배터리 관리는 런타임이 아니라 운영사의 몫이라, 여기서는 '충전을 신청할 이유'만
# 있으면 됩니다 — 충전대 경쟁과 리콜 공지가 물 자리가 그것뿐입니다. 기체가 떨어지는 건
# 보여줄 것이 아니라 잡음이라, 한 판 안에 소진되지 않을 만큼 넉넉하게 둡니다.
# 11km 반경에서 한 바퀴(배달 두 곳 + 창고)가 2천 틱 = 시뮬레이션 27분입니다. 한 바퀴에
# 절반 안쪽만 쓰고 마당에서 채우도록 넉넉히 둡니다. 배터리 때문에 배달을 중단하고
# 돌아오는 장면은 이 데모가 보여줄 것이 아닙니다.
ENDURANCE_MIN = 60.0
CLIMB_MPS = 2.0
# 짐. 창고에서 여섯 상자를 싣고 나가 착륙장 두 곳에서 세 상자씩 내리고, 그 자리에서 돌아갈
# 상자를 두 개씩 받아 창고로 가져옵니다. 창고에서 그것을 내리고 다시 여섯 개를 싣습니다.
# 상자는 그렇게 계속 쌓이고 내려집니다 — 기체가 앉아만 있는 순간은 없습니다.
PARCELS_PER_TRIP = 6
PARCELS_PER_STOP = 3
PICKUP_PER_STOP = 2
STOPS_PER_TRIP = 2
# 상자 하나를 싣거나 내리는 시간. 화면(틱 0.2초)에서 1.2초 — 상자가 하나씩 늘고 줄어드는 게
# 보여야 멈춰서 싣고 내리는 중이라는 것이 읽힙니다. 1초보다 짧으면 한꺼번에 사라져 보입니다.
BOX_TICKS = 5
LOAD_TICKS = PARCELS_PER_TRIP * BOX_TICKS   # 창고에서 싣는 시간(36틱, 7.2초)
DROP_TICKS = PARCELS_PER_STOP * BOX_TICKS   # 배달지에서 내리는 시간(18틱, 3.6초)
# 승인을 확인하고 출발하기까지. 화면의 승인 표시(노란 선 2.4초 + 판정 0.6초 + 초록 깜빡임
# 1.4초 = 4.4초, 22틱)가 끝난 다음 떠야 '승인 전에 날아간다'로 보이지 않습니다.
# 폴링 0.5초 여유를 더합니다. ui/map-route.mjs 의 GROW/CHECK/APPROVED_HOLD 와 같이 바꿀 것.
# 거절은 여기서 세지 않습니다 — 운영사가 화면의 거절 표시가 끝난 뒤에 다시 그리므로
# (attache/agent/loop.py REDRAW_DELAY_S) 거절과 승인은 실제 시간에서 이미 떨어져 있습니다.
CLEARANCE_TICKS = 25
DESCENT_MPS = 1.75
# 지상에서 일하는 상태. 이 동안 들어온 승인은 상태를 바꾸지 않고 기다렸다가 ready 에서 띄웁니다.
GROUND_WORK = ("loading", "dropping", "picking", "ready")

STEP_METRES = CRUISE_MPS * SIM_SECONDS_PER_TICK      # 틱당 17.6 m
BATTERY_PER_TICK = 100.0 / (ENDURANCE_MIN * 60.0) * SIM_SECONDS_PER_TICK
CLIMB_RATE_M = CLIMB_MPS * SIM_SECONDS_PER_TICK      # 틱당 상승
DESCENT_RATE_M = DESCENT_MPS * SIM_SECONDS_PER_TICK  # 틱당 하강
# 경유점에 이만큼 붙으면 다음 구간으로. 한 틱 이동(17.6m)보다 작게 잡아야
# 기체가 경유점에 정확히 내려앉습니다. 넉넉하게 잡으면 그만큼 모서리를 자르고,
# 50m 격자로 건물 사이를 지나는 경로에서는 그 몇 미터가 건물입니다.
ARRIVAL_RADIUS_M = 6.0


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
RECALL_TICK = 1050

# 상시 공역. 한 동네 안에서도 허용 고도가 갈립니다 — 실제 데이터가 그렇게 생겼습니다.
# FAA UAS Facility Map 은 격자마다 천장이 다르고, ED-269 구역은 하한·상한을 갖습니다.
AIRSPACE_FILE = os.getenv(
    "AIRSPACE_FILE", str(Path(__file__).resolve().parent.parent / "configs/airspace/nyc.json")
)


def load_bands() -> list[dict]:
    """같은 등급끼리 합쳐진 덩어리. 화면 바닥에 칠할 때 씁니다. 격자 없는 칸도 채웁니다."""
    try:
        raw = json.loads(Path(AIRSPACE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    bands = list(raw.get("bands", []))
    default = default_band(raw.get("volumes", []))
    if default:
        bands.append(default)
    return bands


# FAA UAS 시설 지도 격자는 30초각(0.008333도) 정사각형이고 통제 공역에만 칸이 있습니다.
# 칸이 없는 곳(허드슨 강 한가운데 같은)은 비어 있는 게 아니라 14 CFR 107.51 의 기본 상한
# 400ft 가 적용되는 곳입니다. 판정(geo.DEFAULT_CEILING_M)이 이미 그렇게 보고 있으니 화면도 같은
# 색으로 칠합니다 — 비어 보이면 "여긴 뭐지"가 됩니다.
GRID_CELL_DEG = 0.008333
GRID_ORIGIN = (40.68334, -74.02501)      # nyc.json 격자의 남서쪽 모서리


def default_band(volumes: list[dict]) -> dict | None:
    have = set()
    for volume in volumes:
        if not volume["id"].startswith("uasfm"):
            continue
        lat0 = min(p[0] for p in volume["polygon"])
        lon0 = min(p[1] for p in volume["polygon"])
        have.add((round((lat0 - GRID_ORIGIN[0]) / GRID_CELL_DEG),
                  round((lon0 - GRID_ORIGIN[1]) / GRID_CELL_DEG)))
    rings = []
    for row in range(-12, 19):          # 위도 40.58 ~ 40.84
        for col in range(-10, 22):      # 경도 -74.11 ~ -73.84
            if (row, col) in have:
                continue
            lat0 = GRID_ORIGIN[0] + row * GRID_CELL_DEG
            lon0 = GRID_ORIGIN[1] + col * GRID_CELL_DEG
            rings.append([[lat0, lon0], [lat0, lon0 + GRID_CELL_DEG],
                          [lat0 + GRID_CELL_DEG, lon0 + GRID_CELL_DEG], [lat0 + GRID_CELL_DEG, lon0]])
    if not rings:
        return None
    return {"id": "band-default-121", "name": "Part 107 기본 상한 (격자 밖)", "polygon": rings[0],
            "rings": rings, "floor_m": 0.0, "ceiling_m": DEFAULT_CEILING_M, "reference": "AGL",
            "rule": "ceiling", "reason": "시설 지도 격자가 없는 곳. 14 CFR 107.51 기본 상한 400ft",
            "source": "14 CFR 107.51"}


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

# 착륙장. 배달은 아무 주소가 아니라 지정된 드론 착륙장으로만 갑니다. 맨해튼 11곳 + 브루클린 3곳 +
# 퀸스 2곳. 좌표는 공원·부두의 열린 자리이고, 착륙 지점 둘레 LANDING_SEPARATION_M 안에 건물이
# 없고 순항 90m 로 이륙장과 왕복 길이 나는지 tests/test_cycle.py 가 봅니다.
# 센트럴파크·미드타운 동쪽·칼슈어츠파크는 KLGA 0ft 격자 안이라(FAA 데이터) 착륙장이 될 수 없고,
# 브라이언트파크·매디슨스퀘어는 둘레에 건물 없는 자리가 없거나 길이 안 났습니다.
# 판 시작 때 정해 두는 첫 배달지. 01·03 의 직선은 미드타운 KLGA 0ft 격자를 관통합니다(우회 장면).
# 02·04 는 자리 순서와 반대 방향으로 갑니다 — 서쪽 자리(02)는 북동쪽 맥캐런으로, 동쪽 자리(04)는
# 북서쪽 콜리어스 훅으로. 두 직선이 자리에서 60m 쯤 북쪽에서 교차하고, 넷이 같은 틱에 뜨므로
# 직결 세계에서는 두 대가 같은 순간 그 점을 지나 분리를 잃습니다. 런타임 세계는 같은 신청을
# 교차로 거절하고 고도나 출발 시각을 바꿔 다시 냅니다 — 그 차이가 점수판의 separation_losses 입니다.
OPENING_STOPS = {"drone-01": "Central Park North 110th", "drone-02": "McCarren Park",
                 "drone-03": "Morningside Park", "drone-04": "Corlears Hook Park"}

LANDING_AREAS = [
    # 맨해튼 섬 (17)
    {"id": "la-battery", "name": "Battery Park", "lat": 40.70335, "lon": -74.01565},
    {"id": "la-minuit", "name": "Peter Minuit Plaza", "lat": 40.70106, "lon": -74.01243},
    {"id": "la-seaport", "name": "Seaport Pier 17", "lat": 40.70620, "lon": -74.00110},
    {"id": "la-pier25", "name": "Pier 25 Tribeca", "lat": 40.72050, "lon": -74.01350},
    {"id": "la-corlears", "name": "Corlears Hook Park", "lat": 40.71150, "lon": -73.97900},
    {"id": "la-eastriver", "name": "East River Park", "lat": 40.71864, "lon": -73.97575},
    {"id": "la-sara", "name": "Sara D. Roosevelt Park", "lat": 40.719, "lon": -73.99262},
    {"id": "la-tompkins", "name": "Tompkins Square", "lat": 40.72650, "lon": -73.98170},
    {"id": "la-washington", "name": "Washington Square", "lat": 40.73080, "lon": -73.99730},
    {"id": "la-pier45", "name": "Pier 45 West Village", "lat": 40.73300, "lon": -74.01100},
    {"id": "la-union", "name": "Union Square", "lat": 40.73590, "lon": -73.99063},
    {"id": "la-stuytown", "name": "Stuy Town Oval", "lat": 40.73180, "lon": -73.97777},
    {"id": "la-stuyvesant", "name": "Stuyvesant Cove", "lat": 40.73300, "lon": -73.97400},
    {"id": "la-pier62", "name": "Pier 62 Chelsea", "lat": 40.74700, "lon": -74.01050},
    {"id": "la-abzug", "name": "Bella Abzug Park", "lat": 40.75603, "lon": -74.00160},
    {"id": "la-pier76", "name": "Pier 76 Midtown", "lat": 40.75868, "lon": -74.00391},
    {"id": "la-pier84", "name": "Pier 84 Hudson", "lat": 40.76284, "lon": -74.00069},
    # 센트럴파크 북쪽·할렘 (7). 공원 남쪽 절반은 KLGA 0ft 격자라 못 둡니다.
    {"id": "la-eastmeadow", "name": "Central Park East Meadow", "lat": 40.7887, "lon": -73.96},
    {"id": "la-northmeadow", "name": "Central Park North Meadow", "lat": 40.79350, "lon": -73.95900},
    {"id": "la-harlemmeer", "name": "Harlem Meer", "lat": 40.79670, "lon": -73.95200},
    {"id": "la-cpnorth", "name": "Central Park North 110th", "lat": 40.79850, "lon": -73.95500},
    {"id": "la-morningside", "name": "Morningside Park", "lat": 40.804, "lon": -73.95824},
    {"id": "la-stnicholas", "name": "St. Nicholas Park", "lat": 40.81550, "lon": -73.94900},
    {"id": "la-jefferson", "name": "Thomas Jefferson Park", "lat": 40.79350, "lon": -73.93700},
    # 브루클린·퀸스·거버너스 (6)
    {"id": "la-bbp", "name": "Brooklyn Bridge Park", "lat": 40.70200, "lon": -73.99650},
    {"id": "la-governors", "name": "Governors Island", "lat": 40.68950, "lon": -74.01680},
    {"id": "la-mccarren", "name": "McCarren Park", "lat": 40.72060, "lon": -73.95200},
    {"id": "la-bushwick", "name": "Bushwick Inlet Park", "lat": 40.72150, "lon": -73.96050},
    {"id": "la-hunters", "name": "Hunters Point South", "lat": 40.74250, "lon": -73.96050},
    {"id": "la-gantry", "name": "Gantry Plaza", "lat": 40.74584, "lon": -73.95862},
]


def to_grid(lat: float, lon: float) -> tuple[float, float]:
    """위경도를 격자로. to_latlon 의 역입니다."""
    return ((lon - ORIGIN_LON) / SPAN_LON * 100.0,
            (1.0 - (lat - ORIGIN_LAT) / SPAN_LAT) * 60.0)


# 배달 서비스 반경. 4km 로는 배달지가 전부 로어맨해튼이라 FAA 0ft 격자(맨해튼 한가운데를
# 지나는 붉은 띠)에 걸리는 경로가 안 나왔습니다. 11km 면 어퍼웨스트사이드까지 들어가고,
# 그리로 가는 직선은 그 띠를 관통해 거절되고 우회로가 나옵니다 — 이 데모의 핵심 장면입니다.
# 편도 10km 는 570틱이라 한 판(ROUND_TICKS)도 같이 늘렸습니다.
SERVICE_RADIUS_M = 11_000.0


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
        # 금지 구역 안이거나 건물에 붙은 주소는 뺍니다. 마지막 구간이 이격 거리를 못 지켜
        # 어떤 경로도 승인이 안 나고, 그러면 그 주문은 반려만 됩니다.
        if AIRSPACE.too_close(address["lat"], address["lon"], 0.0):
            continue
        north = (address["lat"] - depot_lat) * 110_570.0
        east = (address["lon"] - depot_lon) * 84_400.0   # 위도 40.7 도 기준
        if (north * north + east * east) ** 0.5 > SERVICE_RADIUS_M:
            continue
        gx, gy = to_grid(address["lat"], address["lon"])
        if 2 <= gx <= 98 and 2 <= gy <= 58:
            open_ones.append({**address, "gx": gx, "gy": gy})
    return open_ones



# 병원 응급헬기가 뜬다고 갑자기 상공이 닫힙니다. 배달 항로 위에 섭니다 — 유일한 이륙장 위에
# 두면 기단이 갈 곳이 없어져서, 규칙이 무엇을 막는지가 아니라 기체가 갇힌 것만 보였습니다.
# 이게 리콜과 같은 얘기의 공간판입니다. 금지가 언제 도착하고 누가 강제하느냐.
# 공지는 FAA 문장 그대로 나갑니다. 시뮬레이터는 폴리곤을 주지 않습니다 — 런타임이 문장을 읽어
# 구역을 만들고, 못 읽으면 못 읽었다고 기록합니다. 실제 NOTAM 이 그렇게 옵니다.
# 좌표는 DDMMSS 라 초 단위입니다: 404310N = 40°43'10", 0735920W = 73°59'20" (이스트빌리지).
# 판의 시계: 틱 0 = 0900Z, 틱 하나 0.8초. 0907-0912Z 가 525~900틱입니다.
CLOCK = Clock(epoch_z="0900", seconds_per_tick=SIM_SECONDS_PER_TICK)
ZONE_TEXT = ("AREA BOUNDED BY 404310N0735920W 404310N0735855W 404332N0735855W 404332N0735920W "
             "SFC-400FT AGL 0907-0912Z")
ZONE_NOTICE = parse_notice(ZONE_TEXT, CLOCK)
ZONE_TICK = ZONE_NOTICE.from_tick
ZONE_UNTIL = ZONE_NOTICE.until_tick   # 응급헬기가 뜨고 내리는 동안만. 구역에는 유효기간이 있습니다
ZONE = {
    "id": "nofly-2026-09-hospital",
    "kind": "notam",
    "reason": "응급헬기 이착륙. 상공 비행금지",
    "name": "이스트빌리지 응급헬기 회랑",
    "text": ZONE_TEXT,
    # 아래는 시뮬레이터 자신이 점수를 매기고 화면 바닥에 칠할 때 쓰는 값입니다. 공지에는 안 실립니다
    # (bulletins 가 text 만 고릅니다).
    "polygon": [[lat, lon] for lat, lon in ZONE_NOTICE.polygon],
    "floor_m": ZONE_NOTICE.floor_m, "ceiling_m": ZONE_NOTICE.ceiling_m, "reference": "AGL",
    "rule": "forbidden", "source": "예시 데이터",
}
# 구역은 이 폴리곤 하나입니다. 예전에는 점수판이 따로 원(centre·radius)을 들고 있었는데,
# 폴리곤을 브루클린으로 옮길 때 원은 안 옮겨져서 이스트강 한복판을 세고 있었습니다.
# 런타임이 막는 곳, 점수판이 세는 곳, 화면이 그리는 곳이 같아야 합니다.
ZONE_VOLUME = Volume.from_dict(ZONE)
# 두 번째 공지는 문법이 못 읽는 문장입니다. 실제 NOTAM 도 서식 밖의 자유 문장으로 올 때가 있고,
# 그때 런타임이 무엇을 하는지(모델이 구조화 → 사람 확인 전에는 아무것도 안 막음, 모델이 없으면
# '못 읽음' 으로 기록)가 첫 공지와 다른 얘기입니다. 첫 구역이 걷힌 뒤(0912Z 이후)에 옵니다.
# 반지름은 0.5NM — 모델이 지어낸 구역의 넓이 상한(core/notam.MAX_AREA_M2 4km²) 안이어야 사람 앞에
# 갑니다. 좌표는 할렘 병원(레녹스 애비뉴·W 136th) 40°48'52"N 73°56'23"W.
MEDEVAC_TEXT = ("MEDEVAC INBOUND HARLEM HOSPITAL HELIPAD. KEEP CLEAR WITHIN 0.5 NM OF "
                "404852N0735623W BELOW 400 FT AGL FROM 0918Z TO 0928Z")
assert parse_notice(MEDEVAC_TEXT, CLOCK) is None, "두 번째 공지는 문법이 못 읽는 문장이어야 합니다"
MEDEVAC_TICK = CLOCK.tick_of("0918")
MEDEVAC_UNTIL = CLOCK.tick_of("0928")
MEDEVAC = {
    "id": "nofly-2026-09-medevac",
    "kind": "notam",
    "reason": "응급헬기 진입. 병원 헬리패드 주변 비행금지",
    "name": "할렘 병원 응급헬기",
    "text": MEDEVAC_TEXT,
}
# 규제기관이 특정 기종의 운항을 세우는 지시. 배터리 관리 같은 운영사의 몫이 아니라,
# 밖에서 도착해서 즉시 강제되어야 하는 규칙입니다 — 그게 런타임이 있는 이유입니다.
RECALL = {
    "id": "ad-2026-09-dv-x500",
    "kind": "recall",
    "forbid_action": "fly_route",
    "applies_to": {"model": "dv-x500"},
    "reason": "감항성 지시 — dv-x500 운항 정지",
}
RECALL_UNTIL = 1350


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
    hold_ticks: int = 0             # 승인 확인 후 출발까지. 즉시 튀어나가지 않습니다
    work_ticks: int = 0             # 싣거나 내리는 데 남은 시간
    load: int = 0                   # 실린 상자 수. 화면에 그대로 쌓입니다
    stops_left: int = 0             # 이번에 나가서 들를 착륙장 수
    pickup: int = 0                 # 이 착륙장에서 받아 갈 상자 수
    delivered: int = 0              # 다녀온 배달지 수
    waypoints: list = field(default_factory=list)   # 승인된 경로. 없으면 못 움직입니다
    # 출발을 미룬 승인. 운영사가 앞 기체의 회랑이 빌 때까지 기다리기로 하고 낸 경로입니다.
    # 이 틱 전에는 지상에서 준비된 채 서 있고, 화면에는 누구를 기다리는지 씁니다.
    depart_after: int = 0
    holding_for: str | None = None

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
    # 두 기체가 같은 틱에 수평 30m·수직 25m 안에 든 일. 쌍마다 한 번(틱마다가 아니라).
    separation_losses: int = 0
    # 떠 있는(내리는) 기체가 땅에 서 있는 기체의 30m·25m 안에 든 일. 착륙장 하나에 두 대.
    site_conflicts: int = 0

    def public(self) -> dict:
        data = asdict(self)
        data["spend_usd"] = round(self.spend_usd, 2)
        data["over_fleet_limit_usd"] = round(self.over_fleet_limit_usd, 2)
        return data


for _raw in STANDING_VOLUMES:
    AIRSPACE.add(Volume.from_dict(_raw))
# 건물은 옥상 위 이격까지 막습니다. 데이터 파일에는 옥상 높이만 있고, 규격은 여기서 붙입니다 —
# 런타임은 이 목록을 그대로 받아 같은 기준으로 판정합니다. 이격은 기본 50 m 이고, 그 건물이 선
# FAA 칸의 천장이 50 m 를 허락하지 않으면(61 m 칸의 30 m 건물) 40 m 미만 건물만 20 m 입니다
# (geo.building_clearance_m). 칸은 위에서 먼저 넣었으므로 여기서 물을 수 있습니다.
def _building_clearance(raw: dict) -> float:
    polygon = raw.get("polygon") or []
    if not polygon:
        return VERTICAL_CLEARANCE_M
    lat = sum(p[0] for p in polygon) / len(polygon)
    lon = sum(p[1] for p in polygon) / len(polygon)
    return building_clearance_m(raw.get("ceiling_m"), AIRSPACE.ceiling_at(lat, lon))


# 이격은 건물을 넣기 전에 한꺼번에 계산합니다. 넣으면서 물으면 동마다 색인이 다시 만들어져
# (3만 4천 동 × 색인 재구성) 불러오기가 몇 시간이 됩니다.
_CLEARANCES = [_building_clearance(_raw) for _raw in BUILDINGS]
for _raw, _clearance in zip(BUILDINGS, _CLEARANCES, strict=True):
    AIRSPACE.add(Volume.from_dict({**_raw, "clearance_m": _clearance}))


def fresh_fleet(seed: int) -> list[Vehicle]:
    """양쪽 세계가 똑같은 상태에서 출발합니다. 다른 건 배선뿐입니다.

    네 대가 이륙장에서 상자를 싣는 장면이 첫 화면입니다. 기종은 반반 — 감항성 지시가
    한 기종에만 걸리므로 '같은 기단인데 절반만 멈춘다'가 보입니다. 이륙장이 하나라
    돌아올 때 경쟁이 생기고, 잠금표와 중재가 걸리는 자리가 그것입니다.
    """
    rng = random.Random(seed)
    fleet = []
    for index, (name, model, kind, battery) in enumerate([
        ("drone-01", "dv-x500", "delivery", 62.0),
        ("drone-02", "dv-hexa", "drone", 70.0 + rng.random()),
        ("drone-03", "dv-x500", "delivery", 66.0),
        ("drone-04", "dv-hexa", "drone", 58.0 + rng.random()),
    ]):
        seat_x, seat_y = seat_of(index)
        fleet.append(Vehicle(name, model, kind, seat_x, seat_y, battery, cargo=True,
                             state="loading", work_ticks=LOAD_TICKS, stops_left=STOPS_PER_TRIP))
    return fleet


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
        # 판의 첫 배달은 정해진 곳으로. 첫 정차가 무작위면 "브루클린에서 할렘까지 직선을 내고
        # 미드타운 0ft 격자에 거절당해 돌아가는" 장면이 한 판에 안 나올 수 있습니다.
        # 두 세계에 똑같이 적용되고, 그 뒤 정차는 무작위입니다.
        for asset_id, name in OPENING_STOPS.items():
            vehicle = self.vehicles.get(asset_id)
            area = next((a for a in LANDING_AREAS if a["name"] == name), None)
            if vehicle is not None and area is not None:
                vehicle.job_label = area["name"]
                vehicle.job_x, vehicle.job_y = to_grid(area["lat"], area["lon"])
        self.score = Scoreboard()
        self.events: list[dict] = []
        # 지금 분리를 잃은 채인 쌍. 쌍마다 한 번만 세려고 기억합니다.
        self._too_close: set[frozenset] = set()
        self._site_close: set[frozenset] = set()

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
            RECALL_TICK <= tick <= RECALL_UNTIL
            and action == RECALL["forbid_action"]
            and vehicle.model == RECALL["applies_to"]["model"]
        ):
            self.score.post_recall_violations += 1
            self._log(tick, "지시 위반", f"{asset} 가 운항 정지 지시 이후 비행")

        cost = COSTS[action]
        vehicle.spend += cost
        self.score.spend_usd += cost
        # 기단 한도는 운영사 설정입니다. 없으면(None) 넘을 것도 없습니다.
        if self.fleet_limit is not None and self.score.spend_usd > self.fleet_limit:
            self.score.over_fleet_limit_usd = self.score.spend_usd - self.fleet_limit

        if action == "decline_job":
            # 규정상 갈 수 없는 주소입니다. 주문을 반려하고 다음 건을 받습니다.
            # 이것도 결정이고 기록에 남습니다 — 어느 주소가 왜 배달 불가인지가 쌓입니다.
            # 가던 비행도 여기서 끝납니다. 경유점을 남겨두면 옛 목적지까지 날아간 뒤
            # 새 주문 쪽으로 승인 없이 이어서 갑니다 — 실제로 그러고 있었습니다.
            self.score.declined += 1
            self._log(tick, "배달 불가", f"{vehicle.job_label} — 규정상 경로 없음")
            vehicle.waypoints = []
            vehicle.depart_after, vehicle.holding_for = 0, None
            if vehicle.state not in GROUND_WORK:
                vehicle.state = self._idle_state(vehicle)
            # 들를 곳이 남았으면 다른 착륙장을, 아니면 다시 창고입니다. 예전에는 여기서
            # 새 주문을 받아 빈 채로 배달지로 날아갔습니다.
            if vehicle.stops_left > 0:
                self._assign_job(vehicle)
            else:
                self._send_home(vehicle)
        elif action == "fly_route":
            # 승인된 경로. 경유점이 있으면 그대로 따라갑니다.
            # 없으면 목적지까지 직선입니다 — 그게 오른쪽 세계가 하는 일입니다.
            vehicle.waypoints = self._to_waypoints(params.get("legs"), vehicle)
            vehicle.assigned_pad = None
            vehicle.hold_ticks = CLEARANCE_TICKS
            self._delay_departure(vehicle, params)
            if not vehicle.waypoints:
                vehicle.cruise_alt = float(params.get("alt_m") or CRUISE_ALT_M)
            # 지상에서 싣거나 내리는 중이면 상태는 그대로입니다. 일이 끝나고 승인 확인이
            # 끝나면 ready 가 띄웁니다. 그래서 출발은 언제나 지상에서, 일하던 자리에서 일어납니다.
            # 땅에 있는 다른 상태(landed·cruising)도 ready 를 거칩니다 — 승인 확인과 미룬 출발
            # (depart_after)은 ready 만 지키므로, 거기서 바로 delivering 이 되면 미룬 출발이
            # 무시됩니다.
            if vehicle.state not in GROUND_WORK:
                vehicle.state = self._idle_state(vehicle) if vehicle.alt <= 1.0 else (
                    "returning" if vehicle.stops_left <= 0 else "delivering")
        elif action == "reserve_pad":
            vehicle.assigned_pad = params["pad"]
            vehicle.hold_ticks = CLEARANCE_TICKS
            vehicle.waypoints = self._to_waypoints(params.get("legs"), vehicle)
            self._delay_departure(vehicle, params)
            # 승인된 순항 고도. 안 주면 기본값으로 납니다 — 그게 규정 위반일 수 있습니다.
            vehicle.cruise_alt = float(params.get("alt_m") or CRUISE_ALT_M)
            if vehicle.state not in GROUND_WORK:
                vehicle.state = "ready" if vehicle.alt <= 1.0 else "approaching"
        elif action in ("charge", "fast_charge"):
            vehicle.state = "charging"
            vehicle.charge_mode = "fast" if action == "fast_charge" else "normal"
        elif action == "depart":
            # 뜨기 전에 싣습니다. 남아 있던 상자 위에 여섯 개까지 채웁니다.
            # 새 주문도 여기서 받습니다 — 안 받으면 다 싣고도 갈 곳이 없어 다시 싣기만 반복합니다.
            vehicle.assigned_pad = None
            vehicle.state = "loading"
            vehicle.stops_left = STOPS_PER_TRIP
            if vehicle.job_x is None:
                self._assign_job(vehicle)
            vehicle.load = min(PARCELS_PER_TRIP, max(0, vehicle.load))
            vehicle.work_ticks = (PARCELS_PER_TRIP - vehicle.load) * BOX_TICKS
            vehicle.cruise_alt = LOITER_ALT_M
            vehicle.waypoints = []   # 다 쓴 경로입니다
            vehicle.depart_after, vehicle.holding_for = 0, None
            vehicle.vibration = 0.0  # 패드에 있는 동안 정비를 받았습니다
        elif action == "divert_ground":
            # 승인됐던 경로를 회수합니다. 경유점을 남겨두면 회수 명령을 받고도 원래 목적지로
            # 계속 날아갑니다 — 구역이 닫혔는데 그 안으로 들어가던 게 그래서였습니다.
            # 닫힌 구역 안에 있었으면 런타임이 준 가장 가까운 바깥 자리까지만 나가서 기다립니다.
            vehicle.assigned_pad = None
            vehicle.waypoints = []
            vehicle.depart_after, vehicle.holding_for = 0, None
            vehicle.vibration = 0.0
            door = params.get("exit") or {}
            if door.get("lat") is not None and vehicle.alt > 1.0:
                gx, gy = to_grid(door["lat"], door["lon"])
                vehicle.waypoints = [(gx, gy, vehicle.alt)]
            if vehicle.state not in GROUND_WORK:
                vehicle.state = self._idle_state(vehicle)
        elif action == "disengage_autonomy":
            vehicle.autonomy_health = 0.0
            vehicle.state = "stranded"
            vehicle.assigned_pad = None

        return {"ok": True, "cost_usd": cost, "state": vehicle.state}

    @staticmethod
    def _idle_state(vehicle: Vehicle) -> str:
        """갈 곳을 잃은 기체의 상태. 떠 있으면 제자리 대기(cruising), 땅이면 ready.

        땅에 있는 기체를 cruising 으로 두면 _hold_altitude 가 순항 고도까지 스스로 올려서, 승인
        없이 뜬 채 떠 있었습니다. 어디로 가는 것도 행동이고 뜨는 것도 행동입니다.
        """
        return "cruising" if vehicle.alt > 1.0 else "ready"

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
    def _delay_departure(vehicle: Vehicle, params: dict) -> None:
        """운영사가 출발을 미뤘으면(depart_after_tick) 그 틱까지 지상에서 준비된 채 기다립니다.

        조종장치는 왜 미루는지 모릅니다. 화면에 쓸 이름(holding_for)만 같이 받아 둡니다.
        """
        after = params.get("depart_after_tick")
        vehicle.depart_after = int(after) if after else 0
        vehicle.holding_for = (str(params.get("holding_for")) if vehicle.depart_after
                               and params.get("holding_for") else None)

    @staticmethod
    def _refuse(vehicle: Vehicle, action: str, params: dict) -> dict | None:
        """물리적으로 불가능한 명령. 돈도 안 나가고 세지도 않습니다."""
        if action == "reserve_pad" and params.get("pad") not in PADS:
            return {"ok": False, "error": f"unknown pad {params.get('pad')}"}
        if action == "depart" and vehicle.alt > 1.0:
            return {"ok": False, "error": "not on the ground"}
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
        self._detect_separation_losses(tick)

    def _advance(self, vehicle: Vehicle, tick: int) -> None:
        if vehicle.state in ("dropping", "loading", "picking"):
            # 지상. 상자가 BOX_TICKS 마다 하나씩 실리거나 내려집니다. 땅에서는 배터리가 안 닳습니다.
            vehicle.work_ticks -= 1
            if vehicle.state == "loading":
                # 남은 시간을 상자 단위로 올림해서 뺍니다. 첫 상자는 BOX_TICKS 가 지나야 실립니다.
                remaining_boxes = -(-max(0, vehicle.work_ticks) // BOX_TICKS)
                vehicle.load = max(vehicle.load, PARCELS_PER_TRIP - remaining_boxes)
            elif vehicle.work_ticks % BOX_TICKS == 0:
                vehicle.load = (max(0, vehicle.load - 1) if vehicle.state == "dropping"
                                else min(PARCELS_PER_TRIP, vehicle.load + 1))
            # 승인 확인은 일하는 동안 같이 흐릅니다. 일이 끝나고 다시 세면 그만큼 더 서 있습니다.
            if vehicle.hold_ticks > 0:
                vehicle.hold_ticks -= 1
            if vehicle.work_ticks <= 0:
                if vehicle.state == "dropping" and vehicle.pickup > 0:
                    # 내린 자리에서 돌아갈 상자를 받습니다.
                    vehicle.state = "picking"
                    vehicle.work_ticks = vehicle.pickup * BOX_TICKS
                    vehicle.pickup = 0
                else:
                    vehicle.state = "ready"
            return
        if vehicle.state == "ready":
            # 지상. 다 실었거나 다 내렸습니다. 갈 곳이 승인되고 확인이 끝나야 뜹니다 —
            # 그 전까지는 자리에서 기다리고, 화면에는 무엇을 기다리는지 씁니다.
            if vehicle.hold_ticks > 0:
                vehicle.hold_ticks -= 1
                return
            if vehicle.waypoints and tick < vehicle.depart_after:
                return   # 미룬 출발. 앞 기체의 회랑이 빌 때까지 준비된 채 섭니다
            vehicle.depart_after, vehicle.holding_for = 0, None
            if vehicle.assigned_pad:
                vehicle.state = "approaching"
            elif vehicle.waypoints:
                # 들를 곳이 없으면 창고로 돌아가는 비행입니다. 받아 온 상자만 싣고 갑니다.
                vehicle.state = "returning" if vehicle.stops_left <= 0 else "delivering"
            return
        if vehicle.hold_ticks > 0:
            # 승인 확인은 지상에서만 합니다. 공중에서 멈춰 서면 그 자리에 붙박이가 되고,
            # 마침 닫힌 구역 위였다면 거기 그대로 머물게 됩니다 — 실제로 그랬습니다.
            if vehicle.alt > 1.0:
                vehicle.hold_ticks = 0
            else:
                vehicle.hold_ticks -= 1
                return
        if vehicle.state == "charging":
            gain = 2.4 if vehicle.charge_mode == "fast" else 1.0
            vehicle.battery = min(100.0, vehicle.battery + gain)
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        if vehicle.state == "landing":
            vehicle.battery -= BATTERY_PER_TICK
            self._hold_altitude(vehicle, (vehicle.x, vehicle.y))
            if vehicle.alt <= 1.0:
                if vehicle.assigned_pad:
                    vehicle.state = "landed"      # 이륙장. 충전하고 다시 싣습니다
                else:
                    self._touch_down(vehicle, tick)   # 배달지. 상자를 내립니다
            return
        if vehicle.state in ("stranded", "diverted", "grounded"):
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        # 나는 동안만 닳습니다. 방전으로 멈추는 줄거리는 뺐습니다 — 항속 35분에 한 판 6분이라
        # 실제로는 안 일어나고, 일어나면 그건 보여줄 것이 아니라 잡음입니다.
        if vehicle.state == "landed" and not vehicle.waypoints:
            vehicle.alt = max(0.0, vehicle.alt - DESCENT_RATE_M)
            return
        vehicle.battery = max(1.0, vehicle.battery - BATTERY_PER_TICK)
        # 모터 진동·자율주행 고장 줄거리도 뺐습니다. 배달 가던 기체가 정비하러 되돌아오거나
        # 공중에서 멈춰 서는 장면이 됐고, 그건 런타임이 아니라 운영사 정비의 몫입니다.
        # 사람 승인 경로(disengage_autonomy)는 tests/test_mechanisms.py 가 따로 봅니다.

        # 승인된 목적지가 없으면 제자리에 뜬 채 기다립니다.
        # 어디로 가는 것도 행동이고, 행동은 승인을 받아야 합니다.
        target = self._current_target(vehicle)
        if target is None:
            # 땅에 있고 갈 곳이 없으면 땅에 있습니다. 뜨는 것은 승인된 경로가 시킵니다.
            if vehicle.alt > 1.0:
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
        flying_in = vehicle.state in ("delivering", "returning", "approaching")
        if flying_in and self._at(vehicle, target):
            # 도착점 위에 왔습니다. 여기서부터 수직으로 내려앉습니다 — 배달지든 이륙장이든.
            # 공중에서 내려놓지 않습니다. 착륙하고, 내리고, 다시 뜹니다.
            vehicle.state = "landing"

    @staticmethod
    def _current_target(vehicle: Vehicle) -> tuple[float, float] | None:
        if vehicle.waypoints:
            return (vehicle.waypoints[0][0], vehicle.waypoints[0][1])
        if vehicle.assigned_pad:
            return PADS[vehicle.assigned_pad]
        if vehicle.state in ("delivering", "returning") and vehicle.job_x is not None:
            # 경유점 없이 목적지로 직행하는 배선. 조종장치는 승인을 안 봅니다 —
            # 그래서 직결 쪽은 이렇게 날고, 그게 두 세계가 갈리는 자리입니다.
            return (vehicle.job_x, vehicle.job_y)
        return None

    def _touch_down(self, vehicle: Vehicle, tick: int) -> None:
        """내려앉았습니다. 착륙장이면 상자를 내리고 돌아갈 상자를 받고, 창고면 가져온 것을 내립니다.

        다음 갈 곳은 내리기 전에 정해 둡니다. 그래야 운영사가 내리는 동안 다음 경로를
        신청하고, 일이 끝난 기체가 그 자리에서 승인을 기다리며 서 있지 않습니다.
        마지막 착륙장을 들른 기체의 다음 갈 곳은 창고 마당이고, 마당에 닿으면 갈 곳이
        없어지는데 — 그때 운영사가 다음 짐(depart)이나 충전대를 신청합니다.
        """
        if vehicle.job_label == "Warehouse" or vehicle.stops_left <= 0:
            self._log(tick, "창고 도착", f"{vehicle.id} 가져온 상자 {vehicle.load}개")
            vehicle.job_x = vehicle.job_y = None
            vehicle.job_label = ""
            vehicle.pickup = 0
            if vehicle.load > 0:
                vehicle.state = "dropping"          # 착륙장에서 받아 온 상자를 내립니다
                vehicle.work_ticks = vehicle.load * BOX_TICKS
            else:
                vehicle.state = "ready"
            return
        vehicle.delivered += 1
        vehicle.stops_left -= 1
        self.score.deliveries += 1
        self._log(tick, "배달 완료", f"{vehicle.id} → {vehicle.job_label}")
        vehicle.state = "dropping"
        vehicle.work_ticks = min(vehicle.load, PARCELS_PER_STOP) * BOX_TICKS
        vehicle.pickup = PICKUP_PER_STOP
        if vehicle.stops_left > 0:
            self._assign_job(vehicle)
        else:
            self._send_home(vehicle)

    def _send_home(self, vehicle: Vehicle) -> None:
        """창고 마당의 제 자리로. 운영사는 이것도 배달지처럼 경로를 신청합니다."""
        vehicle.job_x, vehicle.job_y = seat_of(sorted(self.vehicles).index(vehicle.id))
        vehicle.job_label = "Warehouse"

    def _assign_job(self, vehicle: Vehicle) -> None:
        """다음 배달지. 첫 정차는 아무 착륙장이고, 두 번째는 첫 정차에서 가까운 여섯 곳 중 하나.

        먼 두 곳을 연달아 찍으면(할렘 → 배터리파크) 한 바퀴가 한 판을 넘깁니다. 실제 배차도
        한 번 나가서는 같은 동네를 돕니다.
        """
        # 다른 기체가 지금 가고 있는 착륙장은 피합니다. 착륙장은 한 번에 한 대라(런타임 규칙),
        # 같은 곳을 두 대가 잡으면 뒤의 기체가 앞 기체가 떠날 때까지 첫 정차에서 1300틱을 앉아
        # 있었습니다. 실제 배차도 같은 곳에 두 대를 동시에 보내지 않습니다.
        taken = {other.job_label for other in self.vehicles.values()
                 if other is not vehicle and other.job_label}
        pool = [area for area in LANDING_AREAS
                if area["name"] != vehicle.job_label and area["name"] not in taken]
        if not pool:
            pool = [area for area in LANDING_AREAS if area["name"] != vehicle.job_label]
        if not pool:
            vehicle.job_x = vehicle.job_y = None
            vehicle.job_label = ""
            return
        if vehicle.stops_left < STOPS_PER_TRIP and vehicle.job_x is not None:
            here_lat, here_lon = to_latlon(vehicle.job_x, vehicle.job_y)
            pool = sorted(pool, key=lambda a: math.hypot((a["lat"] - here_lat) * 110_570,
                                                         (a["lon"] - here_lon) * 84_400))[:6]
        area = self._rng.choice(pool)
        vehicle.job_label = area["name"]
        vehicle.job_x, vehicle.job_y = to_grid(area["lat"], area["lon"])

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
        """이 구간의 승인 고도에 있는가. 아니면 제자리에서 오르내린 다음 갑니다.

        내려가면서 앞으로 나가면 다음 구간의 첫 부분을 판정받은 것보다 높게 날아 낮은 천장을
        넘고, 올라가면서 나가면 낮게 날아 옥상 이격을 못 지킵니다. 실제 배달 드론도 꼭짓점에서
        고도를 맞추고 갑니다.
        """
        if vehicle.state in ("landed", "landing", "charging"):
            return True
        if World._at(vehicle, target):
            return True
        return abs(vehicle.alt - vehicle.cruise_alt) <= max(CLIMB_RATE_M, DESCENT_RATE_M)

    @staticmethod
    def _hold_altitude(vehicle: Vehicle, target: tuple[float, float]) -> None:
        """뜨고 내리는 구간을 실제로 그립니다. 3D 로 보면 이게 전부입니다.

        멀티로터는 착륙점 위까지 순항 고도로 간 다음 수직으로 내려갑니다. 예전에는
        1.5km 앞에서부터 비스듬히 활공했는데, 그 비탈이 건물 높이를 그대로 지나가서
        승인된 경로를 날면서도 건물을 스쳤습니다.
        """
        if vehicle.state in ("landed", "landing") or (
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
            if vehicle.state in ("grounded", "landed", "charging") or vehicle.state in GROUND_WORK:
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

    def _detect_separation_losses(self, tick: int) -> None:
        """떠 있는 두 기체가 수평 30m·수직 25m 안에 든 순간. 쌍마다 한 번씩 셉니다.

        런타임 세계에서는 0 이어야 합니다 — 의도(4D)가 같은 자리·같은 시각의 두 신청을 미리
        갈랐으니까요. 직결 세계는 아무도 가르지 않아서 자리에서 반대 방향으로 뜬 두 대가
        같은 틱에 같은 점을 지납니다. 문턱은 판정과 같은 숫자(geo.TRAFFIC_*)입니다.
        """
        airborne = [v for v in self.vehicles.values()
                    if v.alt > 1.0 and v.state not in ("grounded", "stranded")]
        # 땅에 서 있는 기체도 자리입니다. 떠 있는 기체가 그 위로 내려오면 착륙장 하나에 두 대입니다
        # (site_conflicts). 판정은 텔레메트리로 서 있는 기체를 보고, 계측은 여기서 그것을 봅니다.
        parked = [v for v in self.vehicles.values() if v.alt <= 1.0]
        close_now: set[frozenset] = set()
        site_now: set[frozenset] = set()

        def within(first: Vehicle, second: Vehicle) -> bool:
            east = (second.x - first.x) * METRES_PER_CELL_X
            north = (second.y - first.y) * METRES_PER_CELL_Y
            return (math.hypot(east, north) < TRAFFIC_LATERAL_M
                    and abs(second.alt - first.alt) < TRAFFIC_VERTICAL_M)

        for index, first in enumerate(airborne):
            for second in airborne[index + 1:]:
                if within(first, second):
                    close_now.add(frozenset((first.id, second.id)))
            for second in parked:
                if within(first, second):
                    site_now.add(frozenset((first.id, second.id)))
        for pair in close_now - self._too_close:
            self.score.separation_losses += 1
            self._log(tick, "분리 상실", f"{' · '.join(sorted(pair))} 가 30m 안에서 교차")
        for pair in site_now - self._site_close:
            self.score.site_conflicts += 1
            self._log(tick, "착륙장 충돌", f"{' · '.join(sorted(pair))} — 서 있는 기체 위로 내려옴")
        self._too_close = close_now
        self._site_close = site_now

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
            # 유효기간이 끝나면 꺼집니다. 안 끄면 화면에 영영 빨갛게 남습니다.
            "zone": {**ZONE, "active": ZONE_TICK <= tick <= ZONE_UNTIL},
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
            # 기체 자리. 화면이 그 밑의 건물을 창고로 칠합니다 — 창고는 점이 아니라 건물입니다.
            "seat_coords": [
                {"asset": vid, "ground_m": SEAT_ROOF_M,
                 "lat": round(to_latlon(*seat_of(i))[0], 6),
                 "lon": round(to_latlon(*seat_of(i))[1], 6)}
                for i, vid in enumerate(sorted(self.vehicles))
            ],
            "landing_areas": LANDING_AREAS,
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
        """지금 걸려 있는 공지. 구역 공지는 문장(text)만 갑니다. 폴리곤은 런타임이 읽어 만듭니다."""
        out = []
        if ZONE_TICK <= self.tick_count <= ZONE_UNTIL:
            out.append({key: ZONE[key] for key in ("id", "kind", "name", "reason", "text")}
                       | {"published_tick": ZONE_TICK, "until_tick": ZONE_UNTIL})
        if MEDEVAC_TICK <= self.tick_count <= MEDEVAC_UNTIL:
            out.append({**MEDEVAC, "published_tick": MEDEVAC_TICK, "until_tick": MEDEVAC_UNTIL})
        if RECALL_TICK <= self.tick_count <= RECALL_UNTIL:
            out.append({**RECALL, "published_tick": RECALL_TICK,
                        "until_tick": RECALL_UNTIL})
        return out

    def reset(self, keep_rounds: bool = False) -> None:
        limit = self.worlds["guarded"].fleet_limit
        self.tick_count = 0
        if not keep_rounds:
            self.rounds = 0
        self.worlds = self._fresh_worlds(limit)
