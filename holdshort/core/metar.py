"""METAR from aviationweather.gov: an official observation, read by code, no key needed.

The tower's own weather feed in the simulator is a METAR-like line; this module brings the
real thing. aviationweather.gov answers with JSON per station (wind, gust, visibility as
numbers plus the raw observation). The numbers are folded into the same spelled-out dialect
the intake grammar already reads deterministically ("KLGA WIND 220 AT 13 GUST 22 KT VIS
10SM"), so a real observation and a scripted bulletin take exactly the same path — grammar,
limits, hold — and nothing here decides anything. When the network is not there the source
is off, once in the ledger, and the poller keeps trying quietly.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from holdshort.core.tavily import ERROR_CHARS, FetchStatus

METAR_URL = "https://aviationweather.gov/api/data/metar"
SOURCE = "metar"
# 관측소 하나의 JSON 을 몇 초 기다리나. 세계 스레드 밖이라 길어도 틱은 안 멈춥니다.
DEFAULT_TIMEOUT_S = 10.0
# 몇 초마다 묻나. METAR 는 시간마다(특별 관측은 사이사이) 나오니 5 분이면 충분합니다.
DEFAULT_PERIOD_S = 300.0


class MetarFailed(Exception):
    """한 주기가 실패했습니다 — 닿지 못함, 거절, 늦음, 깨진 답."""


@dataclass
class Observation:
    """관측 하나를 접수 문장으로 옮기는 데 필요한 것만. 단위는 원문대로(kt, SM)입니다."""

    station: str
    obs_time: int                    # 관측 시각(epoch 초). 항목 id 가 됩니다 — 같은 관측은 한 번만
    wind_kt: float | None
    gust_kt: float | None
    visibility_sm: float | None
    wind_dir: str                    # "220" | "VRB" | ""
    weather: str = ""                # 현상 코드(RA, BR …). 문법이 강수로 읽습니다
    raw: str = ""

    def text(self) -> str:
        """문법이 읽는 어투. 시각은 넣지 않습니다 — 판의 시계(틱 0 = 0900Z)와 실제 시각은
        다릅니다."""
        parts = [self.station]
        if self.wind_kt is not None:
            direction = self.wind_dir if self.wind_dir in ("VRB",) or self.wind_dir.isdigit() \
                else "000"
            parts.append(f"WIND {direction.zfill(3) if direction.isdigit() else direction} "
                         f"AT {self.wind_kt:.0f}")
            if self.gust_kt is not None:
                parts.append(f"GUST {self.gust_kt:.0f}")
            parts.append("KT")
        if self.visibility_sm is not None:
            parts.append(f"VIS {_fraction(self.visibility_sm)}SM")
        if self.weather:
            parts.append(self.weather)
        return " ".join(parts)

    def item(self) -> dict:
        return {"id": f"metar-{self.station}-{self.obs_time}", "source": SOURCE,
                "kind": "weather", "text": self.text(), "station": self.station,
                "title": self.raw[:120], "url": f"{METAR_URL}?ids={self.station}",
                "obs_time": self.obs_time, "fetched_at": time.time()}


def _fraction(value: float) -> str:
    """시정을 문법이 읽는 꼴로. 정수면 정수, 1/2·1/4·3/4 는 분수, 나머지는 소수 한 자리."""
    if float(value).is_integer():
        return f"{int(value)}"
    for numerator, denominator in ((1, 4), (1, 2), (3, 4), (1, 8), (3, 8), (5, 8), (7, 8)):
        if abs(value - numerator / denominator) < 1e-6:
            return f"{numerator}/{denominator}"
    return f"{value:.1f}"


def _number(value) -> float | None:
    """수가 아니면 None. "10+"(10 SM 이상)는 10, "1/2" 는 0.5, "M1/4"(1/4 미만)는 0.25."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().rstrip("+").lstrip("M")
    if "/" in text:
        head, _, tail = text.partition("/")
        whole, _, top = head.rpartition(" ")
        try:
            return (float(whole) if whole else 0.0) + float(top or head) / float(tail)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_observation(raw: dict) -> Observation | None:
    """aviationweather.gov 의 관측 하나 → Observation. 바람도 시정도 없으면 None(읽을 것이 없음)."""
    if not isinstance(raw, dict):
        return None
    station = str(raw.get("icaoId") or "").strip().upper()
    if not station:
        return None
    wind = _number(raw.get("wspd"))
    gust = _number(raw.get("wgst"))
    visibility = _number(raw.get("visib"))
    if wind is None and gust is None and visibility is None:
        return None
    direction = raw.get("wdir")
    direction = "VRB" if str(direction).upper() == "VRB" else (
        f"{int(direction):03d}" if isinstance(direction, (int, float)) else "")
    obs_time = raw.get("obsTime")
    try:
        obs_time = int(obs_time)
    except (TypeError, ValueError):
        obs_time = int(time.time())
    weather = " ".join(str(raw.get("wxString") or "").split())
    return Observation(station=station, obs_time=obs_time, wind_kt=wind, gust_kt=gust,
                       visibility_sm=visibility, wind_dir=direction, weather=weather,
                       raw=str(raw.get("rawOb") or ""))


class MetarClient:
    def __init__(self, stations: list[str], base_url: str = METAR_URL,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.stations = [s.strip().upper() for s in stations if s.strip()]
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    @classmethod
    def from_env(cls, stations: list[str]) -> "MetarClient | None":
        """관측소가 없거나 METAR=off 면 None — 출처가 꺼진 것이고 아무것도 묻지 않습니다."""
        if os.getenv("METAR", "on").strip().lower() in ("off", "0", "false", "no"):
            return None
        if not [s for s in stations if s.strip()]:
            return None
        return cls(stations, os.getenv("METAR_URL", METAR_URL),
                   float(os.getenv("METAR_TIMEOUT_S") or DEFAULT_TIMEOUT_S))

    def fetch(self) -> list[dict]:
        """관측소 전부를 한 번에. 닿지 못하거나 깨진 답이면 MetarFailed."""
        query = urllib.parse.urlencode({"ids": ",".join(self.stations), "format": "json"})
        self.calls += 1
        try:
            request = urllib.request.Request(f"{self.base_url}?{query}", method="GET")
            request.add_header("Accept", "application/json")
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            self._fail(f"HTTP {error.code}")
        except urllib.error.URLError as error:
            self._fail(f"unreachable: {error.reason}")
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._fail(f"{type(error).__name__}: {error}")
        if not isinstance(body, list):
            self._fail("답이 관측 목록이 아님")
        items = []
        for raw in body:
            observation = parse_observation(raw)
            if observation is not None:
                items.append(observation.item())
        return items

    def _fail(self, why: str) -> None:
        self.failures += 1
        self.last_error = why[:ERROR_CHARS]
        raise MetarFailed(self.last_error)


class MetarPoller:
    """주기마다 관측을 받아 넘깁니다. 자기 스레드에서 — 세계 스레드는 네트워크를 기다리지 않습니다.

    deliver(items, status) — 항목이 없어도 상태는 넘깁니다(tavily.IntakePoller 와 같은 약속).
    """

    def __init__(self, client: MetarClient, period_s: float, deliver):
        self.client = client
        self.period_s = period_s
        self.deliver = deliver
        self.fetches = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, daemon=True, name="intake-metar")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self.period_s)

    def fetch_once(self) -> list[dict]:
        found, error = [], ""
        try:
            found = self.client.fetch()
        except MetarFailed as failed:
            error = str(failed)
        self.fetches += 1
        status = FetchStatus(ok=not error, error=error, calls=self.client.calls,
                             failures=self.client.failures)
        try:
            self.deliver(found, status)
        except Exception as problem:  # noqa: BLE001 — 넘기다 죽어도 다음 주기는 돕니다
            print(f"metar deliver: {problem!r}", flush=True)
        return found
