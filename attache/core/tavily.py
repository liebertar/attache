"""A small search client. Stdlib only, one timeout, and it never runs on the world thread.

Tavily answers a query with a handful of pages (title, url, content). The runtime turns each
into an intake item and reads it like any other text: the grammar first, then the Super tier,
then code validates. The client itself decides nothing — it fetches and reduces. When it cannot
fetch it says so: a source that is quietly failing looks exactly like a quiet day, and the
tower must be able to tell the two apart.
"""

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

TAVILY_URL = "https://api.tavily.com/search"
# 한 질문에 몇 쪽까지. 많이 받아도 읽는 것은 모델이고, 모델 호출은 한 건에 수십 초입니다.
MAX_RESULTS = 5
# 요약 길이. 모델에게 주는 문장이고, 화면의 원문 배너에도 앞부분이 오릅니다.
SNIPPET_CHARS = 400
# 원장·화면에 남기는 실패 사유 길이.
ERROR_CHARS = 120


class SearchFailed(Exception):
    """한 질문이 실패했습니다 — 닿지 못함, 거절(401 등), 늦음, 깨진 답, 잘못된 URL."""


@dataclass
class FetchStatus:
    """한 주기의 결과. 항목과 같이 런타임에 넘어가고, 실패↔회복이 바뀔 때만 원장에 오릅니다."""

    ok: bool
    error: str = ""
    calls: int = 0
    failures: int = 0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "error": self.error or None, "calls": self.calls,
                "failures": self.failures}


class TavilyClient:
    def __init__(self, api_key: str, base_url: str = TAVILY_URL, timeout_s: float = 15.0):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.calls = 0
        self.failures = 0
        self.last_error = ""

    @classmethod
    def from_env(cls) -> "TavilyClient | None":
        """키가 없으면 None — 그러면 이 출처는 꺼진 것이고 아무것도 기록하지 않습니다."""
        key = os.getenv("TAVILY_API_KEY", "").strip()
        if not key:
            return None
        return cls(key, os.getenv("TAVILY_URL", TAVILY_URL),
                   float(os.getenv("TAVILY_TIMEOUT_S", "15")))

    def search(self, query: str) -> list[dict]:
        """한 질문. 닿지 못하거나 늦으면 SearchFailed — 폴러가 상태로 넘기고 다음 주기에 다시
        묻습니다.

        Request 도 try 안에서 만듭니다. 잘못된 URL 은 urlopen 이 아니라 Request 가 ValueError 를
        내고, 그것이 밖으로 새면 폴링 스레드가 첫 호출에 죽어 출처가 '켜진 채' 영영 묻지 않습니다.
        """
        payload = {"api_key": self.api_key, "query": query, "max_results": MAX_RESULTS,
                   "search_depth": "basic", "include_answer": False}
        self.calls += 1
        try:
            request = urllib.request.Request(self.base_url, data=json.dumps(payload).encode(),
                                             method="POST")
            request.add_header("Content-Type", "application/json")
            request.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            self._fail(f"HTTP {error.code}")
        except urllib.error.URLError as error:
            self._fail(f"unreachable: {error.reason}")
        except (TimeoutError, json.JSONDecodeError, OSError, ValueError) as error:
            self._fail(f"{type(error).__name__}: {error}")
        return reduce_results(query, body)

    def _fail(self, why: str) -> None:
        self.failures += 1
        self.last_error = why[:ERROR_CHARS]
        raise SearchFailed(self.last_error)


def reduce_results(query: str, body: dict) -> list[dict]:
    """응답을 접수 항목으로. id 는 url(없으면 제목)의 해시 — 같은 쪽은 다시 읽지 않습니다."""
    items = []
    for raw in (body.get("results") if isinstance(body, dict) else None) or []:
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
        found, errors = [], []
        for query in self.queries:
            if self._stop.is_set():
                break
            try:
                found += self.client.search(query)
            except SearchFailed as error:
                errors.append(str(error))
        self.fetches += 1
        status = FetchStatus(ok=not errors, error=errors[0] if errors else "",
                             calls=self.client.calls, failures=self.client.failures)
        try:
            self.deliver(found, status)
        except Exception as error:  # noqa: BLE001 — 넘기다 죽어도 다음 주기는 돕니다
            print(f"intake deliver: {error!r}", flush=True)
        return found
