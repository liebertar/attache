"""The tower's eyes on the live city: search, extract, map, crawl, research. Stdlib only.

Tavily answers a question with pages (title, url, content), pulls the clean text out of a
page (extract), walks an official site (map/crawl), and runs a multi-step research task that
comes back as JSON in a schema we hand it (research). The runtime turns every page into an
intake item and reads it like any other text: the grammar first, then the Super tier, then
code validates. The client itself decides nothing — it fetches, counts what it spent and
reduces. When it cannot fetch it says so: a source that is quietly failing looks exactly like
a quiet day, and the tower must be able to tell the two apart.

Nothing here runs on the world thread, and every call is bounded twice: by a timeout, and by
the credit book. A round has a budget; when it is gone the next call does not leave the
process at all (BudgetExhausted) — an empty wallet is not an outage.
"""

import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API_ROOT = "https://api.tavily.com"
TAVILY_URL = f"{API_ROOT}/search"
# 한 질문에 몇 쪽까지. 많이 받아도 읽는 것은 모델이고, 모델 호출은 한 건에 수십 초입니다.
MAX_RESULTS = 5
# 요약 길이. 모델에게 주는 문장이고, 화면의 원문 배너에도 앞부분이 오릅니다.
SNIPPET_CHARS = 400
# 원장·화면에 남기는 실패 사유 길이.
ERROR_CHARS = 120
# 한 쪽에서 읽어 들이는 본문 길이. 공지 한 장은 몇 KB 면 충분하고, 긴 쪽을 통째로 들면
# 모델 프롬프트와 sqlite 줄만 부풀어 오릅니다.
CONTENT_CHARS = 8000
# 한 판에 쓸 수 있는 크레딧. Tavily 요금은 호출 종류로 매겨집니다(검색 basic 1, extract 5쪽당 1,
# map 10쪽당 1, crawl = map + extract, research 는 요청마다 mini 4~110). 한도를 넘는 호출은
# 프로세스를 떠나지 않습니다.
DEFAULT_BUDGET = 20.0
# research 를 시작하기 전에 잡아 두는 최소 크레딧(mini 의 요청당 하한). 실제 요금은 답에 실려
# 오는 usage 로 적습니다 — 그래서 한 번의 research 는 남은 예산을 넘길 수 있고, 그 뒤의 호출이
# 막힙니다. 그것이 Tavily 가 요금을 뒤에 알려 주는 일에 우리가 할 수 있는 정직한 대응입니다.
RESEARCH_RESERVE = 4.0
RESEARCH_TIMEOUT_S = float(os.getenv("TAVILY_RESEARCH_TIMEOUT_S", "120"))
RESEARCH_POLL_S = 3.0
# 기록해 둔 호출 목록의 길이(/state.briefing.calls).
KEPT_CALLS = 12


class SearchFailed(Exception):
    """한 호출이 실패했습니다 — 닿지 못함, 거절(401 등), 늦음, 깨진 답, 잘못된 URL."""


class BudgetExhausted(SearchFailed):
    """이 판의 크레딧을 다 썼습니다. 실패가 아니라 안 보낸 것입니다 — 원장에 출처 실패로 적지
    않습니다."""


@dataclass
class FetchStatus:
    """한 주기의 결과. 항목과 같이 런타임에 넘어가고, 실패↔회복이 바뀔 때만 원장에 오릅니다."""

    ok: bool
    error: str = ""
    calls: int = 0
    failures: int = 0
    credits: float = 0.0
    skipped: int = 0          # 예산이 없어 안 보낸 호출

    def to_dict(self) -> dict:
        return {"ok": self.ok, "error": self.error or None, "calls": self.calls,
                "failures": self.failures, "credits": round(self.credits, 2),
                "skipped": self.skipped}


@dataclass
class Call:
    """호출 하나와 그 값. estimated 는 요금이 답에 안 실려 와 우리가 셈했다는 뜻입니다."""

    op: str
    credits: float
    ok: bool
    estimated: bool = False
    detail: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"op": self.op, "credits": round(self.credits, 2), "ok": self.ok,
                "estimated": self.estimated, "detail": self.detail[:80], "at": self.at}


class CreditBook:
    """이 판에 검색에 쓴 것. 한도는 판마다 새로 찹니다(new_round).

    Tavily 는 요금을 답에 실어 줍니다(include_usage). 안 실려 오면 요금표대로 셈해서 적습니다 —
    모르는 값을 0 으로 적으면 한도가 한도가 아니게 됩니다.
    """

    def __init__(self, budget: float | None = None):
        self.budget = (float(os.getenv("TAVILY_BUDGET_PER_ROUND", str(DEFAULT_BUDGET)))
                       if budget is None else float(budget))
        self.used = 0.0          # 이 판에 쓴 것
        self.total = 0.0         # 프로세스가 켜진 뒤로 쓴 것
        self.blocked = 0         # 예산이 없어 안 보낸 호출
        self.pending = 0.0       # 나갔지만 아직 값을 모르는 호출들의 어림값
        self.calls: list[Call] = []
        self._lock = threading.Lock()
        # 이 스레드가 잡아 둔 몫. 같은 스레드의 다음 book() 이 풉니다 — 한 호출은 처음부터 끝까지
        # 한 스레드에서 돌고(나가기 → 답 → 값 매기기), 스레드마다 한 번에 한 호출입니다.
        self._held = threading.local()

    def allow(self, estimate: float) -> bool:
        """예산이 되면 어림값만큼 잡아 두고 True.

        보기만 하고 값은 답이 온 뒤에 적으면, 브리핑과 검색 폴러(같은 장부)가 같은 남은 몫을 보고
        둘 다 나갑니다 — 실측: 한도 3 에 네 번이 나가 4 를 썼습니다. 잡아 두면 나중 쪽이 막힙니다.
        """
        amount = max(0.0, estimate)
        with self._lock:
            self._release_locked()     # 이 스레드가 전에 잡고 값을 못 매긴 몫은 풉니다
            if self.used + self.pending + amount > self.budget:
                return False
            self.pending += amount
            self._held.amount = amount
            return True

    def release(self) -> None:
        """이 스레드가 잡아 둔 몫을 값 없이 풉니다(호출이 도중에 예외로 끝났을 때)."""
        with self._lock:
            self._release_locked()

    def _release_locked(self) -> None:
        held = getattr(self._held, "amount", 0.0)
        if held:
            self.pending = max(0.0, self.pending - held)
            self._held.amount = 0.0

    def book(self, op: str, credits: float, ok: bool = True, estimated: bool = False,
             detail: str = "") -> None:
        with self._lock:
            self._release_locked()     # 잡아 둔 어림값 대신 실제 값이 들어갑니다
            self.used += max(0.0, credits)
            self.total += max(0.0, credits)
            self.calls.append(Call(op, credits, ok, estimated, detail))
            del self.calls[:-KEPT_CALLS]

    def block(self, op: str) -> None:
        with self._lock:
            self.blocked += 1
            self.calls.append(Call(op, 0.0, ok=False, detail="budget"))
            del self.calls[:-KEPT_CALLS]

    def new_round(self) -> None:
        with self._lock:
            self.used = 0.0
            self.blocked = 0

    @property
    def left(self) -> float:
        with self._lock:
            return max(0.0, self.budget - self.used - self.pending)

    def to_dict(self) -> dict:
        with self._lock:
            return {"used": round(self.used, 2), "total": round(self.total, 2),
                    "budget": self.budget, "blocked": self.blocked,
                    "calls": [call.to_dict() for call in self.calls]}


def _ceil_units(count: int, per_credit: int) -> float:
    """요금표의 '몇 쪽당 1 크레딧'. 0 쪽이면 0 입니다."""
    return float(math.ceil(count / per_credit)) if count > 0 else 0.0


class TavilyClient:
    recorded = False

    def __init__(self, api_key: str, base_url: str = TAVILY_URL, timeout_s: float = 15.0,
                 credits: CreditBook | None = None, record_dir: str | None = None):
        self.api_key = api_key
        self.base_url = base_url
        # /search 를 뗀 나머지가 다른 창구(extract·map·crawl·research)의 뿌리입니다.
        self.root = base_url[: -len("/search")] if base_url.endswith("/search") else base_url
        self.timeout_s = timeout_s
        self.research_timeout_s = RESEARCH_TIMEOUT_S
        self.credits = credits if credits is not None else CreditBook()
        self.record_dir = os.getenv("TAVILY_RECORD_DIR", "") if record_dir is None else record_dir
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self._recorded_files = 0
        self._books = threading.Lock()

    @classmethod
    def from_env(cls) -> "TavilyClient | None":
        """키가 없으면 None — 그러면 이 출처는 꺼진 것이고 아무것도 기록하지 않습니다."""
        key = os.getenv("TAVILY_API_KEY", "").strip()
        if not key:
            return None
        return cls(key, os.getenv("TAVILY_URL", TAVILY_URL),
                   float(os.getenv("TAVILY_TIMEOUT_S", "15")))

    # ---------- 창구 ----------

    def search(self, query: str, **options) -> list[dict]:
        """한 질문. 접수 항목으로 줄여서 돌려줍니다. 닿지 못하거나 늦으면 SearchFailed."""
        return reduce_results(query, self.search_raw(query, **options))

    def search_raw(self, query: str, topic: str = "general", time_range: str | None = None,
                   max_results: int = MAX_RESULTS, include_domains: list[str] | None = None,
                   search_depth: str = "basic") -> dict:
        """답을 그대로. 브리핑은 published_date·score 까지 보고 고릅니다."""
        payload = {"api_key": self.api_key, "query": query, "max_results": max_results,
                   "search_depth": search_depth, "include_answer": False, "include_usage": True}
        if topic and topic != "general":
            payload["topic"] = topic
        if time_range:
            payload["time_range"] = time_range
        if include_domains:
            payload["include_domains"] = list(include_domains)
        estimate = 2.0 if search_depth == "advanced" else 1.0
        body = self._call("search", self.base_url, payload, estimate, detail=query)
        self._settle("search", body, estimate, len(_results(body)), detail=query)
        return body

    def extract(self, urls: list[str], extract_depth: str = "basic", fmt: str = "text",
                query: str | None = None) -> dict:
        """쪽의 본문. 검색 조각(content)은 몇 줄뿐이라 높이·반경·시간 창이 잘려 옵니다."""
        wanted = [str(url) for url in urls if str(url).strip()]
        if not wanted:
            return {"results": [], "failed_results": []}
        payload = {"urls": wanted, "extract_depth": extract_depth, "format": fmt,
                   "include_usage": True}
        if query:
            payload["query"] = query
        factor = 2.0 if extract_depth == "advanced" else 1.0
        estimate = _ceil_units(len(wanted), 5) * factor
        body = self._call("extract", f"{self.root}/extract", payload, estimate,
                          detail=wanted[0])
        got = len(_results(body))
        self._settle("extract", body, _ceil_units(got, 5) * factor, got, detail=wanted[0])
        return body

    def map_site(self, url: str, instructions: str | None = None, max_depth: int = 1,
                 max_breadth: int = 20, limit: int = 20,
                 select_paths: list[str] | None = None) -> dict:
        """공식 사이트의 쪽 목록. 무엇이 있는지부터 봐야 무엇을 읽을지 고를 수 있습니다."""
        payload = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth,
                   "limit": limit, "include_usage": True}
        if instructions:
            payload["instructions"] = instructions
        if select_paths:
            payload["select_paths"] = list(select_paths)
        factor = 2.0 if instructions else 1.0
        body = self._call("map", f"{self.root}/map", payload,
                          _ceil_units(limit, 10) * factor, detail=url)
        found = len(_results(body))
        self._settle("map", body, _ceil_units(found, 10) * factor, found, detail=url)
        return body

    def crawl_site(self, url: str, instructions: str | None = None, max_depth: int = 1,
                   max_breadth: int = 20, limit: int = 10, extract_depth: str = "basic",
                   fmt: str = "text", select_paths: list[str] | None = None) -> dict:
        """공식 사이트를 걸어 본문까지. FAA 의 TFR 목록이나 시 기관의 공지 쪽이 이렇게 옵니다."""
        payload = {"url": url, "max_depth": max_depth, "max_breadth": max_breadth,
                   "limit": limit, "extract_depth": extract_depth, "format": fmt,
                   "include_usage": True}
        if instructions:
            payload["instructions"] = instructions
        if select_paths:
            payload["select_paths"] = list(select_paths)
        walk = 2.0 if instructions else 1.0
        read = 2.0 if extract_depth == "advanced" else 1.0
        estimate = _ceil_units(limit, 10) * walk + _ceil_units(limit, 5) * read
        body = self._call("crawl", f"{self.root}/crawl", payload, estimate, detail=url)
        found = len(_results(body))
        self._settle("crawl", body,
                     _ceil_units(found, 10) * walk + _ceil_units(found, 5) * read, found,
                     detail=url)
        return body

    def research(self, question: str, output_schema: dict | None = None, model: str = "mini",
                 output_length: str = "short", include_domains: list[str] | None = None,
                 deadline_s: float | None = None, poll_s: float = RESEARCH_POLL_S) -> dict:
        """여러 단계를 스스로 밟는 조사 하나. 답은 우리가 준 스키마대로 옵니다.

        만든 뒤(201 pending) 끝날 때까지 물어봅니다 — 이 함수는 작업 스레드에서만 부릅니다.
        요금은 요청마다 다르고(mini 4~110) 끝난 답의 usage 에 실려 옵니다. 시작 전에는 하한만
        잡아 두므로, 비싼 조사 하나가 남은 예산을 넘길 수 있습니다. 넘으면 그다음 호출이 막힙니다.
        """
        payload = {"input": question, "model": model, "stream": False,
                   "output_length": output_length}
        if output_schema:
            payload["output_schema"] = output_schema
        if include_domains:
            payload["include_domains"] = list(include_domains)
        body = self._call("research", f"{self.root}/research", payload, RESEARCH_RESERVE,
                          timeout_s=min(self.timeout_s * 2, 60.0), detail=question[:60])
        if body.get("status") == "completed" or body.get("content") is not None:
            self._settle("research", body, RESEARCH_RESERVE, 1, detail=question[:60])
            return body
        request_id = str(body.get("request_id") or "")
        if not request_id:
            self.credits.book("research", RESEARCH_RESERVE, ok=False, estimated=True)
            self._fail("research: 답에 request_id 가 없음")
        deadline = time.monotonic() + float(deadline_s or self.research_timeout_s)
        while time.monotonic() < deadline:
            time.sleep(max(0.05, poll_s))
            body = self._get(f"{self.root}/research/{urllib.parse.quote(request_id)}")
            status = str(body.get("status") or "")
            if status == "completed":
                self._settle("research", body, RESEARCH_RESERVE, 1, detail=question[:60])
                return body
            if status == "failed":
                self.credits.book("research", _usage(body) or 0.0, ok=False, estimated=False)
                self._fail("research failed")
        self.credits.book("research", RESEARCH_RESERVE, ok=False, estimated=True)
        self._fail("research timed out")
        return {}       # _fail 은 반드시 일으킵니다. 읽는 사람을 위한 줄입니다

    # ---------- 바깥으로 나가는 한 번 ----------

    def _call(self, op: str, url: str, payload: dict, estimate: float,
              timeout_s: float | None = None, detail: str = "") -> dict:
        """한 호출. 예산을 먼저 보고, 그다음에야 프로세스를 떠납니다.

        Request 도 try 안에서 만듭니다. 잘못된 URL 은 urlopen 이 아니라 Request 가 ValueError 를
        내고, 그것이 밖으로 새면 폴링 스레드가 첫 호출에 죽어 출처가 '켜진 채' 영영 묻지 않습니다.
        """
        if not self.credits.allow(estimate):
            self.credits.block(op)
            raise BudgetExhausted(f"{op}: 이 판의 크레딧 {self.credits.budget:.0f} 를 다 썼습니다")
        with self._books:
            self.calls += 1
        try:
            body = self._send(op, url, payload, timeout_s, detail)
            self._write_record(op, payload, body)
        except BaseException:
            self.credits.release()     # 값을 매길 답이 없습니다. 잡아 둔 몫이 새지 않게
            raise
        return body

    def _send(self, op: str, url: str, payload: dict, timeout_s: float | None,
              detail: str) -> dict:
        try:
            request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                             method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(request,
                                        timeout=timeout_s or self.timeout_s) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            self._spent_nothing(op, f"HTTP {error.code}", detail)
        except urllib.error.URLError as error:
            self._spent_nothing(op, f"unreachable: {error.reason}", detail)
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._spent_nothing(op, f"{type(error).__name__}: {error}", detail)
        return {}

    def _get(self, url: str) -> dict:
        """조사 하나의 진행을 묻습니다. 같은 호출의 일부라 예산을 다시 보지 않습니다."""
        try:
            request = urllib.request.Request(url, method="GET")
            request.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            self._spent_nothing("research", f"HTTP {error.code}", url)
        except urllib.error.URLError as error:
            self._spent_nothing("research", f"unreachable: {error.reason}", url)
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._spent_nothing("research", f"{type(error).__name__}: {error}", url)
        return {}

    def _settle(self, op: str, body: dict, fallback: float, got: int, detail: str = "") -> None:
        """이 호출이 얼마였나. 답의 usage 가 먼저, 없으면 요금표대로 셈한 값."""
        credits = _usage(body)
        self.credits.book(op, fallback if credits is None else credits, ok=True,
                          estimated=credits is None, detail=f"{detail} ({got})")

    def _spent_nothing(self, op: str, why: str, detail: str) -> None:
        """거절·두절·늦음. 요금은 안 나가지만 실패는 셉니다."""
        self.credits.book(op, 0.0, ok=False, detail=f"{detail}: {why}"[:80])
        self._fail(why)

    def _fail(self, why: str) -> None:
        with self._books:
            self.failures += 1
            self.last_error = why[:ERROR_CHARS]
        raise SearchFailed(self.last_error)

    def _write_record(self, op: str, payload: dict, body: dict) -> None:
        """TAVILY_RECORD_DIR 이 있으면 진짜 답을 파일로 남깁니다 — fixture 의 원료입니다.

        키는 빼고 적습니다. 파일 모양은 tests/fixtures/tavily 와 같아서, 쓸 만한 답은 그대로
        옮기면 녹음 모드가 그 쪽을 다시 냅니다.
        """
        if not self.record_dir or not body:
            return
        with self._books:
            self._recorded_files += 1
            serial = self._recorded_files
        request = {key: value for key, value in payload.items() if key != "api_key"}
        record = {
            "fixture": {"kind": "recorded", "note": "real Tavily response recorded by skynet",
                        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "calls": [{"op": op, "request": request, "response": body}],
        }
        try:
            folder = Path(self.record_dir)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{int(time.time() * 1000)}-{os.getpid()}-{serial:04d}-{op}.json"
            (folder / name).write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        except OSError as error:
            print(f"TAVILY_RECORD_DIR 에 못 씁니다: {error}", flush=True)


def _results(body: dict) -> list:
    found = body.get("results") if isinstance(body, dict) else None
    return found if isinstance(found, list) else []


def _usage(body: dict) -> float | None:
    """답이 말한 요금. include_usage 를 모르는 서버도 있어 없으면 None 입니다."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    credits = usage.get("credits")
    return float(credits) if isinstance(credits, (int, float)) else None


def reduce_results(query: str, body: dict) -> list[dict]:
    """응답을 접수 항목으로. id 는 url(없으면 제목)의 해시 — 같은 쪽은 다시 읽지 않습니다."""
    items = []
    for raw in _results(body):
        if not isinstance(raw, dict):
            continue
        title = " ".join(str(raw.get("title") or "").split())
        url = str(raw.get("url") or "")
        snippet = " ".join(str(raw.get("content") or "").split())[:SNIPPET_CHARS]
        if not title and not snippet:
            continue
        key = hashlib.sha1((url or title).encode("utf-8")).hexdigest()[:12]
        items.append({
            "id": f"tavily-{key}", "source": "tavily", "query": query, "title": title,
            "url": url, "snippet": snippet, "fetched_at": time.time(),
            "text": f"{title}. {snippet}".strip(". ") if title else snippet,
        })
    return items


# ---------- 녹음된 Tavily ----------

FIXTURE_DIR = str(Path(__file__).resolve().parent.parent / "tests/fixtures/tavily")


def load_tavily_fixtures(folder: str | Path | None = None) -> list[dict]:
    """fixture 파일들을 호출 목록으로. 손으로 쓴 장면과 진짜 답의 녹음이 같은 모양입니다."""
    root = Path(folder or os.getenv("TAVILY_FIXTURE_DIR") or FIXTURE_DIR)
    calls = []
    if not root.is_dir():
        return calls
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"tavily fixture {path.name}: {error}", flush=True)
            continue
        note = dict(payload.get("fixture") or {})
        note.setdefault("file", path.name)
        for call in payload.get("calls") or []:
            if not isinstance(call, dict) or not call.get("op"):
                continue
            match = dict(call.get("match") or {})
            if not match and isinstance(call.get("request"), dict):
                # 녹음된 진짜 호출. 무엇에 답했는지는 그 요청이 말해 줍니다.
                request = call["request"]
                if request.get("query"):
                    match = {"query": str(request["query"])}
                elif request.get("url"):
                    match = {"url": str(request["url"])}
            calls.append({"op": str(call["op"]), "match": match,
                          "response": call.get("response") or {}, "fixture": note})
    return calls


def _matches(match: dict, subject: str) -> bool:
    """이 fixture 가 이 질문(또는 이 주소)에 답하나. 조건이 없으면 아무거나 답합니다."""
    lowered = subject.lower()
    exact = match.get("query") or match.get("url")
    if exact and str(exact).lower() == lowered:
        return True
    terms_all = [str(term).lower() for term in match.get("all") or []]
    terms_any = [str(term).lower() for term in match.get("any") or []]
    terms_none = [str(term).lower() for term in match.get("none") or []]
    if not terms_all and not terms_any and not terms_none:
        return not exact
    if any(term not in lowered for term in terms_all):
        return False
    if terms_any and not any(term in lowered for term in terms_any):
        return False
    return not any(term in lowered for term in terms_none)


class RecordedTavily:
    """키 없이 도는 Tavily. tests/fixtures/tavily 의 답을 냅니다 — 모양은 진짜와 같습니다.

    데모와 시험이 키 없이도 같은 장면을 돌게 하려고 있습니다. 여기서 나온 것은 전부
    recorded 로 표가 나고(화면·원장·기록), 살아 있는 답인 척하지 않습니다.
    """

    recorded = True

    def __init__(self, folder: str | Path | None = None, credits: CreditBook | None = None):
        self.fixtures = load_tavily_fixtures(folder)
        self.credits = credits if credits is not None else CreditBook()
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.timeout_s = 0.0
        self.asked: list[tuple[str, str]] = []      # (op, 무엇을 물었나) — 시험이 봅니다

    # 녹음은 크레딧을 쓰지 않습니다. 호출은 세되 요금은 0 입니다.
    def _note(self, op: str, subject: str) -> None:
        self.calls += 1
        self.asked.append((op, subject))
        self.credits.book(op, 0.0, ok=True, detail=f"recorded: {subject}"[:80])

    def _pick(self, op: str, subject: str) -> list[dict]:
        return [call for call in self.fixtures
                if call["op"] == op and _matches(call["match"], subject)]

    def search(self, query: str, **options) -> list[dict]:
        return reduce_results(query, self.search_raw(query, **options))

    def search_raw(self, query: str, topic: str = "general", time_range: str | None = None,
                   max_results: int = MAX_RESULTS, include_domains: list[str] | None = None,
                   search_depth: str = "basic") -> dict:
        self._note("search", query)
        results, seen = [], set()
        for call in self._pick("search", query):
            for raw in _results(call["response"]):
                url = str(raw.get("url") or "")
                if url in seen:
                    continue
                seen.add(url)
                results.append({**raw, "fixture": call["fixture"]})
        return {"query": query, "results": results[:max_results], "usage": {"credits": 0}}

    def extract(self, urls: list[str], extract_depth: str = "basic", fmt: str = "text",
                query: str | None = None) -> dict:
        self._note("extract", ", ".join(urls)[:80])
        found, missing = [], []
        for url in urls:
            hit = None
            for call in self._pick("extract", url):
                for raw in _results(call["response"]):
                    if str(raw.get("url") or "") == url:
                        hit = {**raw, "fixture": call["fixture"]}
                        break
                if hit is not None:
                    break
            if hit is None:
                missing.append({"url": url, "error": "no fixture"})
            else:
                found.append(hit)
        return {"results": found, "failed_results": missing, "usage": {"credits": 0}}

    def map_site(self, url: str, instructions: str | None = None, **options) -> dict:
        self._note("map", url)
        results = []
        for call in self._pick("map", url):
            results += [raw for raw in _results(call["response"])]
        return {"base_url": url, "results": results, "usage": {"credits": 0}}

    def crawl_site(self, url: str, instructions: str | None = None, **options) -> dict:
        self._note("crawl", url)
        results = []
        for call in self._pick("crawl", url):
            results += [{**raw, "fixture": call["fixture"]} for raw in _results(call["response"])
                        if isinstance(raw, dict)]
        return {"base_url": url, "results": results, "usage": {"credits": 0}}

    def research(self, question: str, output_schema: dict | None = None, **options) -> dict:
        self._note("research", question[:80])
        for call in self._pick("research", question):
            body = dict(call["response"])
            body.setdefault("status", "completed")
            body["fixture"] = call["fixture"]
            return body
        return {"status": "completed", "content": {"hazards": []}, "sources": []}


class IntakePoller:
    """주기마다 질문 목록을 돌며 결과를 넘깁니다. 자기 스레드에서 — 세계 스레드는 안 기다립니다.

    deliver(items, status) — 항목이 없어도 상태는 넘깁니다. 빈 목록만 넘기면 받는 쪽이
    "조용한 날" 과 "닿지 못한 날" 을 가를 수 없습니다.
    """

    def __init__(self, client: TavilyClient, queries: list[str], period_s: float, deliver):
        self.client = client
        self.queries = list(queries)
        self.period_s = period_s
        self.deliver = deliver
        self.fetches = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, daemon=True, name="intake-tavily")
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.fetch_once()
            self._stop.wait(self.period_s)

    def fetch_once(self) -> list[dict]:
        found, errors, skipped = [], [], 0
        for query in self.queries:
            if self._stop.is_set():
                break
            try:
                found += self.client.search(query)
            except BudgetExhausted:
                # 예산이 없는 것은 출처가 죽은 것이 아닙니다. 이번 주기는 건너뛰고 다음 판에.
                skipped += 1
            except SearchFailed as error:
                errors.append(str(error))
        self.fetches += 1
        status = FetchStatus(ok=not errors, error=errors[0] if errors else "",
                             calls=self.client.calls, failures=self.client.failures,
                             credits=self.client.credits.used, skipped=skipped)
        try:
            self.deliver(found, status)
        except Exception as error:  # noqa: BLE001 — 넘기다 죽어도 다음 주기는 돕니다
            print(f"intake deliver: {error!r}", flush=True)
        return found
