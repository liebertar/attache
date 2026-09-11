"""The pre-flight briefing: what is happening today at the places this fleet is flying to.

Structured feeds tell the tower about airspace. They do not tell it that a tower crane went up
on Broadway last week, that a park it lands in is closed this morning, or that the FAA put a
VIP restriction over the East Side for the General Assembly. Those live in prose on official
pages, so the tower reads them: at the start of a round, and again whenever a corridor it has
just cleared enters a neighbourhood nobody has asked about yet (one question per ~1 km cell
per round).

Nothing here decides anything. Tavily finds and fetches; the grammar in core/intake reads;
the Super tier only structures what the grammar cannot; code validates every number against
the tower's own gazetteer and ranges; and only then does a rule exist. A rule from an official
domain that the grammar read applies at once, because it can only tighten. Everything else —
another domain, or anything a model read — waits on the approval screen for a person. Every
rule carries where it came from: url, title, domain, when it was fetched, who read it.

The work happens on its own thread: the world thread hands over a plan and picks up the result
at a later poll. When there is no key (or the key is refused) the same scenes come from
recorded fixtures, and everything says "recorded" — on screen, in the ledger, in the store.
"""

import datetime
import hashlib
import math
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

import yaml

from backend.notices import NoticeRecord
from shared.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, Volume
from shared.intake import (
    BRIEFING_SYSTEM,
    EVIDENCE_CHARS,
    Hazard,
    Window,
    from_briefing_form,
    hazard_problems,
    read_hazard,
    utc_to_eastern,
    worth_a_model,
)
from shared.intake import UnknownPlace as UnknownBriefingPlace
from shared.llm.client import LlmTier, parse_json_object
from shared.models import Decision, Proposal, Verdict
from shared.notam import circle
from shared.tavily import (
    CONTENT_CHARS,
    BudgetExhausted,
    FetchStatus,
    RecordedTavily,
    SearchFailed,
)

# 브리핑이 원장에 쓰는 이름. 기체가 아니라 관제탑의 일입니다.
BRIEFING_ASSET = "briefing"
BRIEFING_CHECKS = ["briefing:grammar", "briefing:model", "briefing:validate"]
# 항목 id 앞머리. 녹음된 장면은 따로 둡니다 — 키를 넣은 날 녹음이 살아 있는 답 행세를 하면
# 안 됩니다.
LIVE_PREFIX = "brief-"
RECORDED_PREFIX = "brief-rec-"
# 폐쇄 구역의 천장. 땅보다 낮은 천장이라 어떤 고도에서도 걸리지 않고(경로·이륙 기둥은 그대로),
# 착륙 검사(Airspace.landing_breach — 자리 둘레의 지상 금지 구역만 봅니다)에만 걸립니다.
# 닫힌 공원에 내리는 것은 막고, 닫힌 공원에서 뜨는 것은 막지 않습니다.
CLOSED_CEILING_M = -1.0
# 폐쇄된 착륙장으로 가던 기체를 회수할 때 쓰는 기둥의 반경. 그 자리에 내리려는 경로만 걸립니다.
CLOSURE_COLUMN_M = 10.0
# 한 실행에서 본문까지 읽어 오는 쪽 수(extract 는 5쪽당 1 크레딧).
EXTRACT_MAX = 5
# 사람이 읽는 요약의 길이와 문장 수.
SUMMARY_CHARS = 400
SUMMARY_SENTENCES = 2
# 모델이 쪽 하나를 구조화하는 데 주는 시간(초). 작업 스레드라 틱과 무관합니다.
BRIEFING_TIMEOUT_S = float(os.getenv("BRIEFING_TIMEOUT_S", "60"))
KEPT_ITEMS = 40

DEFAULT_TRUSTED = ("faa.gov", "weather.gov", "noaa.gov", "nyc.gov", "nycgovparks.org",
                   "cityofnewyork.us", "mta.info", "parks.ny.gov", "ny.gov")
DEFAULT_QUERIES = {
    "round": [
        "FAA temporary flight restriction New York City drone {date}",
        "National Weather Service New York City wind advisory {date}",
    ],
    "seasonal": {
        9: ["United Nations General Assembly {year} flight restrictions East Side Manhattan"],
    },
    "park": ["{park} closure {date}"],
    "street": ["tower crane permit near {street} {borough}"],
}
# 착륙장이 어느 구인가. 질문에 동네 이름이 없으면 엉뚱한 도시의 크레인이 옵니다.
BOROUGH = {"la-bbp": "Brooklyn", "la-mccarren": "Brooklyn", "la-bushwick": "Brooklyn",
           "la-hunters": "Queens", "la-gantry": "Queens", "la-governors": "New York"}
DEFAULT_BOROUGH = "Manhattan"

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "hazards": {
            "type": "array",
            "description": "Hazards to low-altitude drone flight, one object each",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string",
                             "description": "crane, event, closure, restriction or weather"},
                    "place": {"type": "string", "description": "where, in plain words"},
                    "address": {"type": "string", "description": "street address for a crane"},
                    "park": {"type": "string", "description": "park name for a closure"},
                    "venue": {"type": "string", "description": "venue for an event"},
                    "lat": {"type": "number", "description": "centre latitude of a restriction"},
                    "lon": {"type": "number", "description": "centre longitude of a restriction"},
                    "radius_m": {"type": "number", "description": "radius in metres"},
                    "height_ft": {"type": "number", "description": "crane height in feet"},
                    "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                    "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                    "timezone": {"type": "string", "description": "UTC or local"},
                    "summary": {"type": "string", "description": "one short sentence"},
                    "source_url": {"type": "string", "description": "the page this came from"},
                },
            },
        },
    },
    "required": ["hazards"],
}
SUMMARY_SYSTEM = (
    "You write two sentences for a drone tower's pre-flight briefing screen. Use only the "
    "hazards and the source domains listed in the message; never add advice, a hazard or a "
    "domain that is not listed. Plain text, no markup, at most 60 words."
)


class _Unset:
    def __init__(self, label: str):
        self.label = label

    def __repr__(self) -> str:
        return f"<{self.label}>"


# 판을 아직 모르는 접수대, 한 번도 걸린 적 없는 기억. 둘 다 None 과 달라야 합니다 — 시험과 하네스의
# 판 번호가 None 입니다.
_UNSET = _Unset("no round yet")
NEVER = _Unset("never applied")


# ---------- 설정 ----------

@dataclass
class BriefingSettings:
    """configs/fleet.yaml 의 briefing 절. 없으면 아래 기본값으로 돕니다."""

    trusted_domains: tuple = DEFAULT_TRUSTED
    cell_km: float = 1.0
    max_cells_per_run: int = 3
    corridor_debounce_ticks: int = 12
    corridor_min_gap_ticks: int = 50
    closure_radius_m: float = 60.0
    crane_radius_m: float = 30.0
    crane_clearance_m: float = 50.0
    max_window_ticks: int = 6000
    extract_max: int = EXTRACT_MAX
    research: bool = True
    research_model: str = "mini"
    queries: dict = field(default_factory=lambda: dict(DEFAULT_QUERIES))
    crawl: tuple = ()
    max_queries_per_run: int = 8

    @classmethod
    def load(cls, config_path: str | None) -> "BriefingSettings":
        raw = {}
        if config_path:
            try:
                with open(config_path, encoding="utf-8") as handle:
                    raw = (yaml.safe_load(handle) or {}).get("briefing") or {}
            except (OSError, ValueError) as error:
                print(f"briefing 설정을 못 읽었습니다: {error}", flush=True)
        settings = cls()
        for key, value in raw.items():
            if key == "queries" and isinstance(value, dict):
                merged = {**DEFAULT_QUERIES, **value}
                merged["seasonal"] = {int(month): list(items) for month, items
                                      in (value.get("seasonal")
                                          or DEFAULT_QUERIES["seasonal"]).items()}
                settings.queries = merged
            elif key == "crawl" and isinstance(value, list):
                settings.crawl = tuple(value)
            elif key == "trusted_domains" and isinstance(value, list):
                settings.trusted_domains = tuple(str(item).lower().strip() for item in value)
            elif hasattr(settings, key) and not isinstance(value, (dict, list)):
                setattr(settings, key, type(getattr(settings, key))(value))
        # 한 판의 크레딧은 환경(TAVILY_BUDGET_PER_ROUND)이 정합니다 — 여기서는 안 겹칩니다.
        return settings


def domain_of(url: str) -> str:
    """주소의 호스트. 신뢰는 도메인으로 가릅니다 — 그 문장을 누가 냈느냐가 규칙의 무게입니다."""
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host.lower()


def trusted_domain(url: str, trusted: tuple) -> bool:
    """공식 출처인가. 정확히 그 도메인이거나 그 아래여야 합니다.

    'nyc.gov.example.com' 은 남의 도메인입니다.
    """
    host = domain_of(url)
    return any(host == name or host.endswith("." + name) for name in trusted)


# ---------- 읽은 것 하나 ----------

@dataclass
class Citation:
    """이 규칙이 어디서 왔나. 원장·기록·화면·승인 카드에 그대로 실립니다."""

    url: str = ""
    title: str = ""
    domain: str = ""
    fetched_at: float = 0.0
    read_by: str = ""
    trust: str = "unofficial"      # official | unofficial
    query: str = ""
    recorded: bool = False
    fixture: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"source_url": self.url, "title": self.title, "domain": self.domain,
                "fetched_at": self.fetched_at, "read_by": self.read_by, "trust": self.trust,
                "query": self.query, "recorded": self.recorded,
                **({"fixture": self.fixture} if self.fixture else {})}

    @classmethod
    def from_dict(cls, raw: dict) -> "Citation":
        return cls(url=str(raw.get("source_url") or raw.get("url") or ""),
                   title=str(raw.get("title") or ""), domain=str(raw.get("domain") or ""),
                   fetched_at=float(raw.get("fetched_at") or 0.0),
                   read_by=str(raw.get("read_by") or ""),
                   trust=str(raw.get("trust") or "unofficial"),
                   query=str(raw.get("query") or ""), recorded=bool(raw.get("recorded")),
                   fixture=dict(raw.get("fixture") or {}))


@dataclass
class Reading:
    """쪽 하나에서 읽어 낸 것과 그 뒤로 벌어진 일. 판을 넘어 기억에 남습니다.

    status: applied(걸림) · held(사람 대기) · approved(사람이 확인) · refused(사람이 거부) ·
    lapsed(답 없이 지나감) · info(규칙 아님) · none(무관) · invalid(검사 탈락) · unreadable.
    """

    item_id: str
    hazard: Hazard
    citation: Citation
    status: str = "info"
    text: str = ""
    why: str = ""
    from_tick: int | None = None
    until_tick: int | None = None
    confirmed_by: str | None = None
    round_applied: object = field(default_factory=lambda: NEVER)

    @property
    def held(self) -> bool:
        return self.status in ("held", "lapsed")

    def to_hints(self) -> dict:
        return {"briefing": {"hazard": self.hazard.to_dict(), "citation": self.citation.to_dict(),
                             "status": self.status, "why": self.why,
                             "confirmed_by": self.confirmed_by}}

    @classmethod
    def from_hints(cls, item_id: str, hints: dict, text: str = "") -> "Reading | None":
        raw = (hints or {}).get("briefing")
        if not isinstance(raw, dict) or not isinstance(raw.get("hazard"), dict):
            return None
        return cls(item_id=item_id, hazard=Hazard.from_dict(raw["hazard"]),
                   citation=Citation.from_dict(raw.get("citation") or {}),
                   status=str(raw.get("status") or "info"), text=text,
                   why=str(raw.get("why") or ""), confirmed_by=raw.get("confirmed_by"))

    def to_dict(self, tick: int = 0) -> dict:
        return {"id": self.item_id, "kind": self.hazard.kind, "place": self.hazard.place,
                "summary": self.hazard.detail or self.why, "url": self.citation.url,
                "domain": self.citation.domain, "title": self.citation.title,
                "trust": self.citation.trust, "read_by": self.citation.read_by,
                "recorded": self.citation.recorded, "status": self.status,
                "rule_id": self.item_id if self.status in ("applied", "approved", "held") else None,
                "from_tick": self.from_tick, "until_tick": self.until_tick,
                "fetched_at": self.citation.fetched_at, "why": self.why,
                "note": self.hazard.note}

    def active_in(self, round_key) -> bool:
        """이 판에 걸려 있거나(사람 대기 포함) 걸리기로 된 규칙인가."""
        return self.round_applied is not NEVER and self.round_applied == round_key


@dataclass
class BriefingNotice(NoticeRecord):
    """브리핑이 만든 공지. 공지 책의 길을 그대로 가되 출처를 들고 다닙니다."""

    citation: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**super().to_dict(), "citation": dict(self.citation)}


@dataclass
class Finding:
    """실행 스레드가 들고 오는 한 쪽의 결과. 세계 스레드가 이것을 규칙으로 옮깁니다."""

    item_id: str
    citation: Citation
    text: str
    hazard: Hazard | None = None
    why: str = ""
    remembered: bool = False


@dataclass
class RunPlan:
    """한 번의 브리핑에 무엇을 물을지. 세계 스레드가 짜고 작업 스레드가 실행합니다."""

    trigger: str                 # round | corridor | manual
    round: object = None
    tick: int = 0
    day: datetime.date = None
    queries: tuple = ()
    crawls: tuple = ()
    places: tuple = ()
    cells: tuple = ()
    bbox: tuple | None = None
    landing_areas: tuple = ()
    known: frozenset = frozenset()
    research: bool = False
    window_text: str = ""


@dataclass
class RunResult:
    plan: RunPlan
    source: str = "recorded"
    fallback: dict | None = None
    status: FetchStatus | None = None
    findings: list = field(default_factory=list)
    summary: str = ""
    summary_by: str = ""
    domains: tuple = ()
    errors: list = field(default_factory=list)
    credits: float = 0.0
    calls: int = 0
    ignored: int = 0


# ---------- 읽기(작업 스레드) ----------

@dataclass
class Reader:
    """쪽 하나를 읽는 데 필요한 것 전부. 읽기는 작업 스레드에서만 돕니다."""

    gazetteer: object
    landing_areas: tuple
    day: datetime.date
    bbox: tuple | None
    llm: object = None

    def read(self, title: str, text: str) -> tuple[Hazard | None, str, str]:
        """(읽은 것, 누가, 못 읽은 이유). 문법 → (필요하면) Super → 코드 검사."""
        body = f"{title}. {text}".strip(". ") if title else text
        if not body.strip():
            return None, "", "빈 쪽"
        hazard = read_hazard(body, self.gazetteer, list(self.landing_areas), self.day)
        read_by = "grammar"
        if hazard is None:
            if not worth_a_model(body):
                return Hazard(kind="none", detail="not about flying"), "grammar", ""
            hazard, read_by, why = self._ask_model(body)
            if hazard is None:
                return None, read_by, why
        problems = hazard_problems(hazard, self.bbox)
        if problems:
            return None, read_by, "; ".join(problems)
        return hazard, read_by, ""

    def _ask_model(self, body: str) -> tuple[Hazard | None, str, str]:
        if not self._can_compile():
            return None, "", "문법으로 못 읽었고 구조화할 모델이 없음"
        reply = self.llm.ask(
            LlmTier.SUPER, BRIEFING_SYSTEM,
            f"Today is {self.day.isoformat()} in New York. Landing areas the tower uses: "
            f"{', '.join(area['name'] for area in self.landing_areas)}.\nPage: {body[:3000]}",
            max_tokens=500, json_object=True, timeout_s=BRIEFING_TIMEOUT_S)
        if reply is None:
            return None, "", "모델 답 없음"
        form = parse_json_object(reply.text)
        try:
            hazard = None if form is None else from_briefing_form(
                form, self.gazetteer, list(self.landing_areas), self.day, body)
        except UnknownBriefingPlace as error:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", str(error)
        if hazard is None:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", "모델 답이 양식이 아님"
        return hazard, f"model:{reply.model}", ""

    def from_form(self, form: dict, text: str, read_by: str) -> tuple[Hazard | None, str, str]:
        """Tavily 의 research 가 채워 온 양식 하나. 모델이 읽은 것과 같은 검사를 지납니다."""
        try:
            hazard = from_briefing_form(form, self.gazetteer, list(self.landing_areas),
                                        self.day, text)
        except UnknownBriefingPlace as error:
            return None, read_by, str(error)
        if hazard is None:
            return None, read_by, "research 답이 양식이 아님"
        problems = hazard_problems(hazard, self.bbox)
        if problems:
            return None, read_by, "; ".join(problems)
        return hazard, read_by, ""

    def _can_compile(self) -> bool:
        return (self.llm is not None and getattr(self.llm, "enabled", False)
                and bool(self.llm.model_for(LlmTier.SUPER)))


def brief_id(url: str, extra: str = "", recorded: bool = False) -> str:
    digest = hashlib.sha1(f"{url}|{extra}".encode()).hexdigest()[:12]
    return f"{RECORDED_PREFIX if recorded else LIVE_PREFIX}{digest}"


def execute(plan: RunPlan, client, reader: Reader, settings: BriefingSettings) -> RunResult:
    """한 번의 브리핑. 검색 → 공식 쪽 본문 → 읽기 → research → 요약. 전부 작업 스레드에서.

    예산이 없으면 그 호출은 나가지 않고(BudgetExhausted) 실행은 계속됩니다 — 빈 지갑은 고장이
    아닙니다. 실패는 세어서 상태로 넘깁니다.
    """
    result = RunResult(plan=plan, source="recorded" if client.recorded else "live")
    docs: dict[str, dict] = {}
    errors: list[str] = []
    successes = 0
    skipped = 0

    for crawl in plan.crawls:
        try:
            body = client.crawl_site(str(crawl.get("url")),
                                     instructions=crawl.get("instructions"),
                                     limit=int(crawl.get("limit") or 6))
            successes += 1
        except BudgetExhausted:
            skipped += 1
            continue
        except SearchFailed as error:
            errors.append(f"crawl {crawl.get('url')}: {error}")
            continue
        for raw in body.get("results") or []:
            _note_doc(docs, plan, raw.get("url"), raw.get("title") or "",
                      raw.get("raw_content") or "", f"crawl {crawl.get('url')}", raw, client)

    for query in plan.queries:
        try:
            body = client.search_raw(query["text"], topic=query.get("topic", "general"),
                                     time_range=query.get("time_range"))
            successes += 1
        except BudgetExhausted:
            skipped += 1
            continue
        except SearchFailed as error:
            errors.append(f"search {query['text']}: {error}")
            continue
        for raw in body.get("results") or []:
            _note_doc(docs, plan, raw.get("url"), raw.get("title") or "",
                      raw.get("content") or "", query["text"], raw, client)

    _fetch_pages(docs, client, settings, errors, plan.known)

    for doc in docs.values():
        if doc["item_id"] in plan.known:
            result.findings.append(Finding(doc["item_id"], doc["citation"], doc["text"],
                                           remembered=True))
            continue
        hazard, read_by, why = reader.read(doc["title"], doc["text"])
        doc["citation"].read_by = read_by
        if hazard is not None and hazard.kind == "none":
            result.ignored += 1
        result.findings.append(Finding(doc["item_id"], doc["citation"], doc["text"],
                                       hazard=hazard, why=why))

    if plan.research:
        try:
            _research(plan, client, reader, settings, result, docs)
            successes += 1
        except BudgetExhausted:
            skipped += 1
        except SearchFailed as error:
            errors.append(f"research: {error}")

    result.errors = errors
    result.credits = client.credits.used
    result.calls = client.calls
    result.status = FetchStatus(ok=successes > 0 and not errors,
                                error=errors[0][:120] if errors else "",
                                calls=client.calls, failures=client.failures,
                                credits=client.credits.used, skipped=skipped)
    result.domains = tuple(sorted({finding.citation.domain for finding in result.findings
                                   if finding.citation.domain and finding.hazard is not None
                                   and finding.hazard.kind != "none"}))
    result.summary, result.summary_by = write_summary(result, reader)
    return result


def _note_doc(docs: dict, plan: RunPlan, url, title: str, text: str, query: str, raw: dict,
              client) -> None:
    """검색·크롤이 준 쪽 하나를 실행의 목록에. 같은 주소는 한 번만 읽습니다."""
    url = str(url or "")
    if not url and not text:
        return
    item_id = brief_id(url or title, recorded=bool(client.recorded))
    if item_id in docs:
        return
    citation = Citation(url=url, title=" ".join(str(title).split())[:200],
                        domain=domain_of(url), fetched_at=_now(), query=query,
                        recorded=bool(client.recorded), fixture=dict(raw.get("fixture") or {}))
    docs[item_id] = {"item_id": item_id, "citation": citation,
                     "text": " ".join(str(text).split())[:CONTENT_CHARS],
                     "title": citation.title, "full": bool(raw.get("raw_content"))}


def _fetch_pages(docs: dict, client, settings: BriefingSettings, errors: list,
                 known: frozenset = frozenset()) -> None:
    """공식 쪽은 본문까지 받아 읽습니다. 검색 조각은 높이·반경·시간 창이 잘려 옵니다.
    이미 읽은 쪽(known)은 받지 않습니다 — 재시작 뒤에 같은 쪽에 크레딧을 다시 쓰지 않게."""
    wanted = [doc for doc in docs.values()
              if doc["item_id"] not in known and not doc["full"] and doc["citation"].url
              and trusted_domain(doc["citation"].url, settings.trusted_domains)]
    if not wanted:
        return
    urls = [doc["citation"].url for doc in wanted[: settings.extract_max]]
    try:
        body = client.extract(urls)
    except BudgetExhausted:
        return
    except SearchFailed as error:
        errors.append(f"extract: {error}")
        return
    by_url = {str(raw.get("url") or ""): raw for raw in body.get("results") or []}
    for doc in wanted:
        raw = by_url.get(doc["citation"].url)
        if raw is None:
            continue
        doc["text"] = " ".join(str(raw.get("raw_content") or "").split())[:CONTENT_CHARS]
        doc["full"] = True
        if raw.get("fixture"):
            doc["citation"].fixture = dict(raw["fixture"])


def _research(plan: RunPlan, client, reader: Reader, settings: BriefingSettings,
              result: RunResult, docs: dict) -> None:
    """조사 한 번. 찾아 주는 것은 Tavily 이고, 읽는 것은 우리 문법입니다.

    공식 도메인을 짚어 주면 그 쪽의 본문을 받아 문법이 다시 읽습니다 — 그러면 적용되는 것은
    모델의 말이 아니라 공식 문장입니다. 문법이 못 읽거나 출처가 공식이 아니면, 그 구조화된 답은
    모델이 읽은 것이라 사람 앞으로 갑니다.
    """
    body = client.research(_research_question(plan), RESEARCH_SCHEMA,
                           model=settings.research_model)
    content = body.get("content")
    hazards = content.get("hazards") if isinstance(content, dict) else None
    if not isinstance(hazards, list):
        return
    seen_urls = {doc["citation"].url for doc in docs.values()}
    for raw in hazards[:8]:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("source_url") or "")
        if url and url in seen_urls:
            continue        # 그 쪽은 이미 우리 문법이 읽었습니다
        text = " ".join(str(raw.get("summary") or "").split())
        title = next((str(raw[key]) for key in ("place", "park", "venue", "address", "summary")
                      if raw.get(key)), "research")
        citation = Citation(url=url, title=title[:200], domain=domain_of(url),
                            fetched_at=_now(), query="tavily research",
                            recorded=bool(client.recorded), fixture=dict(body.get("fixture") or {}))
        item_id = brief_id(url or text, extra=str(raw.get("kind") or ""),
                           recorded=bool(client.recorded))
        page_id = brief_id(url, recorded=bool(client.recorded)) if url else item_id
        if {item_id, page_id} & set(plan.known) or item_id in docs or page_id in docs:
            continue
        if url and trusted_domain(url, settings.trusted_domains):
            page = _read_official(url, client, reader, citation, result)
            if page is not None:
                result.findings.append(page)
                seen_urls.add(url)
                continue
        hazard, read_by, why = reader.from_form(raw, text, "model:tavily-research")
        citation.read_by = read_by
        result.findings.append(Finding(item_id, citation, text, hazard=hazard, why=why))


def _read_official(url: str, client, reader: Reader, citation: Citation,
                   result: RunResult) -> Finding | None:
    """research 가 짚은 공식 쪽을 우리가 직접 읽습니다. 못 읽으면 None — 그러면 모델의 양식으로."""
    try:
        body = client.extract([url])
    except (SearchFailed, BudgetExhausted):
        return None
    raw = next((item for item in body.get("results") or []
                if str(item.get("url") or "") == url), None)
    if raw is None:
        return None
    text = " ".join(str(raw.get("raw_content") or "").split())[:CONTENT_CHARS]
    if raw.get("fixture"):
        citation.fixture = dict(raw["fixture"])
    hazard, read_by, why = reader.read(citation.title, text)
    citation.read_by = read_by
    if hazard is None or hazard.kind == "none":
        return None
    return Finding(brief_id(url, recorded=citation.recorded), citation, text, hazard=hazard,
                   why=why)


def _research_question(plan: RunPlan) -> str:
    places = ", ".join(plan.places) or "Manhattan and the Brooklyn and Queens waterfront"
    return (
        f"Hazards to low-altitude drone flight over {places} on {plan.day.isoformat()} "
        f"{plan.window_text}. Look for temporary flight restrictions (including VIP movements "
        "and United Nations General Assembly week), large events and street closures, park and "
        "pier closures, tower cranes, and severe weather advisories. Prefer official sources: "
        "faa.gov, weather.gov, nyc.gov, nycgovparks.org. Give the address, the park name or the "
        "centre coordinates and radius, the local start and end time, and the source url."
    )


def write_summary(result: RunResult, reader: Reader) -> tuple[str, str]:
    """두 문장. Super 가 쓰되 코드가 검사하고, 없으면 틀로 씁니다.

    모델이 목록에 없는 도메인을 대면 버립니다 — 요약에서 출처를 지어내면 요약이 출처가 됩니다.
    """
    rules = [f.hazard for f in result.findings if f.hazard is not None and f.hazard.rule_kind]
    template = _template_summary(result, rules)
    if not reader._can_compile() or not result.findings:
        return template, "template"
    lines = [f"- {f.hazard.kind}: {f.hazard.detail or f.hazard.place} "
             f"({f.citation.domain or 'unknown'})"
             for f in result.findings if f.hazard is not None and f.hazard.kind != "none"]
    if not lines:
        return template, "template"
    reply = reader.llm.ask(
        LlmTier.SUPER, SUMMARY_SYSTEM,
        "Hazards read for the briefing:\n" + "\n".join(lines[:10])
        + f"\nSource domains: {', '.join(result.domains) or 'none'}",
        max_tokens=180, timeout_s=BRIEFING_TIMEOUT_S)
    if reply is None:
        return template, "template"
    text = " ".join(reply.text.split())[:SUMMARY_CHARS]
    if _summary_problem(text, result.domains):
        reader.llm.discard(LlmTier.SUPER)
        return template, "template"
    return text, f"model:{reply.model}"


DOMAIN_TOKEN = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
ABBREVIATIONS = re.compile(r"\b(?:St|Ave|Blvd|Dr|Mt|No|Jr|Sr|U\.S|N\.Y|a\.m|p\.m)\.", re.IGNORECASE)


def _summary_problem(text: str, domains: tuple) -> str:
    if not text or len(text) < 20:
        return "너무 짧음"
    if "http" in text or "<" in text:
        return "주소나 표시를 실었음"
    spoken = set(DOMAIN_TOKEN.findall(text.lower()))
    known = {d.lower() for d in domains}
    unknown = [name for name in spoken
               if not any(d == name or d.endswith("." + name) for d in known)]
    if unknown:
        return f"목록에 없는 출처 {unknown}"
    plain = ABBREVIATIONS.sub("", DOMAIN_TOKEN.sub("", text))
    sentences = [part for part in re.split(r"[.!?]+(?:\s+|$)", plain) if part.strip()]
    return "" if len(sentences) <= SUMMARY_SENTENCES else f"{len(sentences)} 문장"


def _template_summary(result: RunResult, rules: list) -> str:
    kinds: dict[str, int] = {}
    for hazard in rules:
        kinds[hazard.kind] = kinds.get(hazard.kind, 0) + 1
    made = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items())) or "none"
    where = ", ".join(result.domains[:4]) or "no source"
    return (f"Briefing for {', '.join(result.plan.places[:4]) or 'the fleet'}: "
            f"{len(result.findings)} pages read, rules {made}. "
            f"Drawn from {where} ({result.source}).")


def _now() -> float:
    return time.time()


# ---------- 격자와 자리 ----------

def cell_of(lat: float, lon: float, cell_km: float) -> tuple[int, int]:
    """약 1 km 칸. 같은 칸은 한 판에 한 번만 묻습니다."""
    size = max(0.2, cell_km) * 1000.0
    return (int(math.floor(lat * METRES_PER_DEG_LAT / size)),
            int(math.floor(lon * METRES_PER_DEG_LON / size)))


def cells_along(legs: list, cell_km: float, step_m: float = 250.0) -> list[tuple]:
    """회랑이 지나는 칸과 그 칸에 들어서는 자리, 지나는 순서대로. [(칸, (lat, lon)), ...]

    구간을 걸으며 훑습니다 — 끝점만 보면 사이의 동네를 건너뜁니다.
    """
    found: dict = {}
    points = [(float(leg["lat"]), float(leg["lon"])) for leg in legs or []
              if leg.get("lat") is not None and leg.get("lon") is not None]
    for here, nxt in zip(points, points[1:], strict=False):
        length = math.hypot((nxt[0] - here[0]) * METRES_PER_DEG_LAT,
                            (nxt[1] - here[1]) * METRES_PER_DEG_LON)
        steps = max(1, int(length / step_m))
        for index in range(steps + 1):
            fraction = index / steps
            at = (here[0] + (nxt[0] - here[0]) * fraction,
                  here[1] + (nxt[1] - here[1]) * fraction)
            found.setdefault(cell_of(at[0], at[1], cell_km), at)
    return list(found.items())


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def street_of(label: str) -> str:
    """주소에서 길 이름만. '2701 Broadway' → 'Broadway'."""
    parts = str(label or "").split()
    return " ".join(parts[1:]) if parts and parts[0][:1].isdigit() else " ".join(parts)


# ---------- 접수대 ----------

class BriefingDesk:
    """관제탑의 사전 브리핑. 세계 스레드가 두드리고, 바깥일은 자기 스레드에서 합니다."""

    def __init__(self, tower, config_path: str | None = None, enabled: bool = False):
        self.tower = tower
        self.settings = BriefingSettings.load(config_path)
        self.enabled = bool(enabled)
        self.run_async = True
        self.readings: dict[str, Reading] = {}
        self.round_key: object = _UNSET
        self.briefed_cells: set = set()
        self.pending_cells: dict = {}
        self.asked: set[str] = set()
        self.runs = 0
        self.last_run_tick: int | None = None
        self.last_trigger: str | None = None
        self.summary = ""
        self.summary_by = ""
        self.domains: tuple = ()
        self.status: FetchStatus | None = None
        self.source = "off"
        self.fallback: dict | None = None
        self.source_failed = False
        self.ignored = 0
        self._recorded: RecordedTavily | None = None
        self._running = False
        self._done: list[RunResult] = []
        self._manual = False
        self._recalled: set[str] = set()
        self._lock = threading.Lock()
        self._bbox: tuple | None = None
        self.load_memory()

    # ---------- 무엇으로 도나 ----------

    @property
    def live(self):
        """살아 있는 Tavily. 없거나 녹음 모드면 None."""
        forced = os.getenv("TAVILY_RECORDED", "").strip().lower()
        if forced in ("1", "true", "yes", "on"):
            return None
        return getattr(self.tower, "tavily", None)

    @property
    def mode(self) -> str:
        """live · recorded · off. 키가 없으면 녹음, TAVILY_RECORDED=0 이면 아예 끕니다."""
        if self.live is not None:
            return "live"
        forced = os.getenv("TAVILY_RECORDED", "").strip().lower()
        if forced in ("0", "false", "no", "off"):
            return "off"
        return "recorded"

    def _serving_recorded(self) -> bool:
        """지금 녹음을 내고 있나. 녹음 모드이거나, 살아 있는 쪽이 실패해 녹음으로 돌았을 때."""
        return self.mode == "recorded" or self.fallback is not None

    @property
    def fallback_allowed(self) -> bool:
        return os.getenv("TAVILY_RECORDED", "").strip().lower() not in ("0", "false", "no", "off")

    def recorded_client(self) -> RecordedTavily:
        if self._recorded is None:
            self._recorded = RecordedTavily()
        return self._recorded

    @property
    def day(self) -> datetime.date:
        """어느 날의 브리핑인가. 살아 있으면 오늘, 녹음이면 그 fixture 가 녹음된 날입니다."""
        given = os.getenv("BRIEFING_DATE", "").strip()
        if given:
            try:
                return datetime.date.fromisoformat(given)
            except ValueError:
                pass
        if self.mode == "recorded":
            for call in self.recorded_client().fixtures:
                as_of = (call.get("fixture") or {}).get("as_of")
                if as_of:
                    try:
                        return datetime.date.fromisoformat(str(as_of))
                    except ValueError:
                        continue
        return datetime.datetime.now(datetime.UTC).date()

    # ---------- 세계 스레드가 두드리는 곳 ----------

    def poll(self, bbox=None) -> None:
        """폴링마다 한 번. 끝난 실행을 적용하고, 판이 바뀌었으면 새로 묻습니다."""
        if not self.enabled:
            return
        self._bbox = bbox if bbox is not None else self._bbox
        self._collect()
        self._sync_cards()
        self._recall_closures()
        if self.round_key is not _UNSET and self.round_key == self.tower._round:
            self._maybe_corridor_run()
            return
        self._new_round()

    def corridor_cleared(self, legs: list, asset: str = "") -> None:
        """방금 승인한 회랑. 아직 이 판에 안 물어본 동네가 있으면 물을 목록에 올립니다."""
        if not self.enabled or not legs:
            return
        cells = cells_along(legs, self.settings.cell_km)
        with self._lock:
            for order, (cell, entry) in enumerate(cells):
                if cell in self.briefed_cells or cell in self.pending_cells:
                    continue
                # (처음 본 틱, 회랑에서의 순서, 들어서는 자리). 먼저 지나갈 동네부터 묻습니다.
                self.pending_cells[cell] = (self.tower.tick, order, entry)

    def request_run(self) -> tuple[int, dict]:
        """POST /briefing/run. 다음 폴링에 한 번 더 묻습니다."""
        if not self.enabled or self.mode == "off":
            return 503, {"error": "브리핑이 꺼져 있습니다"}
        if self._running:
            return 409, {"error": "브리핑이 이미 돌고 있습니다"}
        self._manual = True
        return 200, {"ok": True, "queued": True, "source": self.mode}

    def adopt_waiting(self, items: list[dict]) -> list[dict]:
        """재시작 때 사람을 기다리던 줄. 브리핑 것은 여기서 받고 나머지는 접수함으로 돌려줍니다."""
        rest = []
        for item in items or []:
            reading = Reading.from_hints(str(item.get("id") or ""), item,
                                         str(item.get("text") or ""))
            if reading is None:
                rest.append(item)
                continue
            reading.status = "held"     # 카드를 잃었으니 다시 올립니다(다시 읽지는 않습니다)
            self.readings[reading.item_id] = reading
        return rest

    def load_memory(self) -> None:
        """끝난 줄까지 되읽습니다. 재시작이 규칙을 푸는 일이 되면 안 됩니다 — 푸는 것은 사람과
        창뿐입니다. 쪽은 다시 읽지 않습니다(문장도 힌트도 기록에 있습니다)."""
        store = getattr(self.tower, "store", None)
        if store is None or not hasattr(store, "briefed"):
            return
        for row in store.briefed(LIVE_PREFIX):
            reading = Reading.from_hints(str(row.get("id") or ""), row.get("hints") or {},
                                         str(row.get("text") or ""))
            if reading is not None:
                self.readings.setdefault(reading.item_id, reading)

    # ---------- 판 ----------

    def _new_round(self) -> None:
        """판이 바뀌었습니다. 기억한 규칙을 이 판의 창으로 다시 걸고, 한 번 묻습니다."""
        self.round_key = self.tower._round
        with self._lock:
            self.briefed_cells.clear()
            self.pending_cells.clear()
        self.asked.clear()
        self._recalled.clear()
        live = self.live
        if live is not None:
            live.credits.new_round()
        for reading in list(self.readings.values()):
            self._place(reading, remembered=True)
        self._start(self._plan("round"))

    def _maybe_corridor_run(self) -> None:
        if self._manual:
            self._manual = False
            self.asked.clear()
            self._start(self._plan("manual"))
            return
        if self._running or not self.pending_cells or self._broke():
            return
        with self._lock:
            oldest = min(first for first, _, _ in self.pending_cells.values())
            if self.tower.tick - oldest < self.settings.corridor_debounce_ticks:
                return
            if (self.last_run_tick is not None and self.tower.tick - self.last_run_tick
                    < self.settings.corridor_min_gap_ticks):
                return
            cells = sorted(self.pending_cells, key=lambda cell: self.pending_cells[cell][:2])
            chosen = [(cell, self.pending_cells[cell][2])
                      for cell in cells[: self.settings.max_cells_per_run]]
            for cell, _ in chosen:
                self.pending_cells.pop(cell, None)
                self.briefed_cells.add(cell)
        self._start(self._plan("corridor", cells=chosen))

    def _broke(self) -> bool:
        """이 판의 크레딧을 다 썼나. 그러면 회랑 브리핑을 띄우지 않습니다 — 나가지도 않을 호출로
        원장에 빈 실행 줄만 쌓입니다. 칸은 그대로 두었다가 다음 판에 버립니다."""
        live = self.live
        return live is not None and live.credits.left < 1.0

    # ---------- 계획 ----------

    def _plan(self, trigger: str, cells: list | None = None) -> RunPlan:
        day = self.day
        areas = tuple(self.tower.landing_areas or [])
        cells = list(cells or [])
        places, queries = [], []
        if trigger in ("round", "manual"):
            for area in self._destinations(areas):
                places.append(str(area.get("name")))
                queries += self._park_queries(area, day)
            queries += self._city_queries(day)
        for cell, entry in cells:
            for area in areas:
                if cell_of(float(area["lat"]), float(area["lon"]),
                           self.settings.cell_km) == cell:
                    places.append(str(area.get("name")))
                    queries += self._park_queries(area, day)
            streets = self._streets_near(entry)
            queries += self._street_queries(entry, streets, areas, day)
            places.append(" and ".join(streets) if streets else f"{entry[0]:.3f},{entry[1]:.3f}")
        fresh = []
        for query in queries:
            if query["text"] in self.asked:
                continue
            self.asked.add(query["text"])
            fresh.append(query)
        return RunPlan(
            trigger=trigger, round=self.tower._round, tick=self.tower.tick, day=day,
            queries=tuple(fresh[: self.settings.max_queries_per_run]),
            crawls=tuple(self.settings.crawl) if trigger in ("round", "manual") else (),
            places=tuple(dict.fromkeys(places)), cells=tuple(cells), bbox=self._bbox,
            landing_areas=areas, known=frozenset(self._known()),
            research=self.settings.research and trigger in ("round", "manual"),
            window_text=self._window_text(day))

    def _destinations(self, areas: tuple) -> list[dict]:
        """지금 기단이 가고 있는 착륙장들. 그 자리와 그 시간을 묻는 것이 브리핑입니다."""
        found = []
        for state in (self.tower.telemetry or {}).values():
            if state.get("job_lat") is None or state.get("job_lon") is None:
                continue
            goal = (float(state["job_lat"]), float(state["job_lon"]))
            near = min(areas, key=lambda area: _distance_m(goal, (area["lat"], area["lon"])),
                       default=None)
            if near is not None and _distance_m(goal, (near["lat"], near["lon"])) < 200.0 \
                    and near not in found:
                found.append(near)
        return found

    def _park_queries(self, area: dict, day: datetime.date) -> list[dict]:
        return [{"text": template.format(park=area.get("name"), date=_spoken_date(day),
                                         year=day.year, borough=_borough(area)),
                 "topic": "news", "time_range": "week", "why": "park"}
                for template in self.settings.queries.get("park") or []]

    def _street_queries(self, at: tuple, streets: list[str], areas: tuple,
                        day: datetime.date) -> list[dict]:
        """그 자리의 길모퉁이를 묻습니다(길 이름 둘).

        동네 이름이 없으면 딴 도시의 크레인이 옵니다.
        """
        if not streets:
            return []
        near = min(areas, key=lambda area: _distance_m(at, (area["lat"], area["lon"])),
                   default=None)
        return [{"text": template.format(street=" and ".join(streets), date=_spoken_date(day),
                                         year=day.year,
                                         borough=_borough(near) if near else DEFAULT_BOROUGH),
                 "topic": "general", "time_range": "month", "why": "street"}
                for template in self.settings.queries.get("street") or []]

    def _streets_near(self, at: tuple, limit: int = 2) -> list[str]:
        """그 자리의 길 이름 한둘. 지명 사전이 아는 주소에서 뽑습니다.

        우리가 아는 자리만 묻습니다.
        """
        addresses = getattr(self.tower.gazetteer, "addresses", []) or []
        close = sorted(
            (address for address in addresses
             if _distance_m(at, (address["lat"], address["lon"])) < 700.0),
            key=lambda address: _distance_m(at, (address["lat"], address["lon"])))
        streets = []
        for address in close:
            street = street_of(address.get("label", ""))
            if street and street not in streets:
                streets.append(street)
            if len(streets) >= limit:
                break
        return streets

    def _city_queries(self, day: datetime.date) -> list[dict]:
        templates = list(self.settings.queries.get("round") or [])
        seasonal = (self.settings.queries.get("seasonal") or {}).get(day.month) or []
        return [{"text": template.format(date=_spoken_date(day), year=day.year,
                                         borough=DEFAULT_BOROUGH, park="", street=""),
                 "topic": "news", "time_range": "week", "why": "city"}
                for template in templates + list(seasonal)]

    def _window_text(self, day: datetime.date) -> str:
        """이 판이 덮는 시간, 뉴욕 지방시로. 질문은 '그 자리' 만큼이나 '그 시간' 이어야 합니다."""
        start = self._round_start()
        end = start + datetime.timedelta(
            seconds=self.settings.max_window_ticks * self.tower.performance.seconds_per_tick)
        return (f"between {utc_to_eastern(start).strftime('%H:%M')} and "
                f"{utc_to_eastern(end).strftime('%H:%M')} local time")

    def _known(self) -> set:
        """이미 읽은 쪽. 기억(재시작 뒤에는 기록에서 되읽은 것 포함)에 있으면 다시 읽지 않습니다."""
        return set(self.readings)

    # ---------- 실행 ----------

    def _start(self, plan: RunPlan) -> None:
        if self.mode == "off" or self._running:
            return
        self._running = True
        self.last_trigger = plan.trigger
        if not self.run_async:
            self._work(plan)
            self._collect()
            return
        threading.Thread(target=self._work, args=(plan,), daemon=True,
                         name=f"briefing-{plan.trigger}").start()

    def _work(self, plan: RunPlan) -> None:
        """작업 스레드. 여기서만 바깥에 나가고 모델에게 묻습니다."""
        try:
            result = self._run_with_fallback(plan)
        except Exception as error:  # noqa: BLE001 — 브리핑이 죽어도 런타임은 돕니다
            print(f"briefing: {error!r}", flush=True)
            result = RunResult(plan=plan, source=self.mode,
                               status=FetchStatus(ok=False, error=f"{type(error).__name__}"),
                               errors=[f"{error!r}"])
        with self._lock:
            self._done.append(result)
            self._running = False

    def _run_with_fallback(self, plan: RunPlan) -> RunResult:
        """살아 있는 Tavily 로 먼저. 한 건도 못 받으면 녹음으로 — 그리고 그렇게 말합니다."""
        reader = Reader(gazetteer=self.tower.gazetteer, landing_areas=plan.landing_areas,
                        day=plan.day, bbox=plan.bbox, llm=getattr(self.tower, "llm", None))
        live = self.live
        if live is not None:
            result = execute(plan, live, reader, self.settings)
            if result.status.ok or not self.fallback_allowed:
                return result
            if result.findings:
                return result       # 일부는 받았습니다. 녹음으로 덮지 않습니다
            recorded = execute(plan, self.recorded_client(), reader, self.settings)
            recorded.fallback = {"from": "live", "why": result.status.error or "no answer"}
            recorded.status = result.status
            recorded.source = "recorded"
            return recorded
        return execute(plan, self.recorded_client(), reader, self.settings)

    # ---------- 결과를 규칙으로(세계 스레드) ----------

    def _collect(self) -> None:
        with self._lock:
            arrived, self._done = self._done, []
        for result in arrived:
            if result.plan.round != self.tower._round:
                continue        # 지난 판의 답입니다. 이 판의 규칙이 아닙니다
            self._absorb(result)

    def _absorb(self, result: RunResult) -> None:
        self.runs += 1
        self.last_run_tick = self.tower.tick
        self.source = result.source
        self.fallback = result.fallback
        self.status = result.status
        found = [f for f in result.findings if f.hazard is not None and f.hazard.kind != "none"
                 and not f.remembered]
        self.ignored += result.ignored
        self._ledger_run(result)
        self._note_source(result.status)
        for finding in result.findings:
            self._take(finding)
        if result.summary_by.startswith("model:") and result.plan.trigger != "corridor":
            self.summary, self.summary_by = result.summary, result.summary_by
        elif found or not self.summary or self.summary_by == "template":
            self.summary, self.summary_by = self._round_summary(), "template"
        self.domains = self._round_domains()

    def _take(self, finding: Finding) -> None:
        """쪽 하나의 결과를 적고, 규칙이면 겁니다."""
        known = self.readings.get(finding.item_id)
        if finding.remembered:
            if known is not None:
                self._place(known, remembered=True)
            return
        if known is not None:
            return      # 이미 아는 쪽입니다. 새로 적으면 카드가 두 장이 됩니다
        reading = Reading(item_id=finding.item_id,
                          hazard=finding.hazard or Hazard(kind="none"),
                          citation=finding.citation, text=finding.text[:EVIDENCE_CHARS],
                          why=finding.why)
        reading.citation.trust = ("official"
                                  if trusted_domain(reading.citation.url,
                                                    self.settings.trusted_domains)
                                  else "unofficial")
        if reading.hazard.kind == "restriction" and reading.hazard.centre is not None:
            reading.hazard.place = self._nearest_label(reading.hazard.centre) or "TFR"
        if finding.hazard is None:
            reading.status = "unreadable" if finding.why else "invalid"
        elif finding.hazard.rule_kind is None:
            reading.status = "none" if finding.hazard.kind == "none" else "info"
        self.readings[finding.item_id] = reading
        self._store(reading)
        self._ledger_item(reading)
        if reading.hazard.rule_kind is not None:
            self._place(reading)

    def _place(self, reading: Reading, remembered: bool = False) -> None:
        """규칙 하나를 이 판에 겁니다(또는 사람 앞에 올립니다). 창 밖이면 아무것도 안 합니다."""
        if reading.hazard.rule_kind is None or reading.status in ("refused", "invalid",
                                                                  "unreadable", "none"):
            return
        if remembered and reading.round_applied == self.tower._round:
            return
        if remembered and reading.citation.recorded != self._serving_recorded():
            return      # 녹음된 규칙이 살아 있는 답 행세를 하면 안 됩니다(반대도 마찬가지)
        from_tick, until_tick, why = self._ticks(reading.hazard.window)
        if why:
            reading.why = why
            reading.from_tick, reading.until_tick = from_tick, until_tick
            return
        reading.from_tick, reading.until_tick = from_tick, until_tick
        reading.round_applied = self.tower._round
        held = reading.status in ("held", "lapsed") or not self._applies_at_once(reading)
        record = self._notice(reading, from_tick, until_tick, held)
        self.tower.notices.records[reading.item_id] = record
        self.tower.intake.notice_ids.add(reading.item_id)
        self.tower._rule_open(reading.item_id, reading.hazard.kind, from_tick, until_tick,
                              applied=not held)
        reading.status = "held" if held else ("approved" if reading.confirmed_by else "applied")
        self._store(reading)
        self._ledger_rule(reading, record, held)
        if held:
            self.tower._hold_notice({"id": reading.item_id, "text": record.text}, record,
                                    self._hold_why(reading))

    def _round_summary(self) -> str:
        """이 판의 브리핑을 두 문장으로 — 걸린 규칙, 사람 대기·정보, 그리고 출처."""
        readings = list(self.readings.values())
        active = [r for r in readings if r.active_in(self.tower._round)
                  and r.hazard.rule_kind is not None]
        applied = [r for r in active if r.status in ("applied", "approved")]
        waiting = [r for r in active if r.status == "held"]
        info = [r for r in readings if r.status == "info" and r.hazard.kind == "weather"]
        rules = "; ".join(f"{r.hazard.kind} {r.hazard.place}" for r in applied) or "none"
        return (f"In force this round: {rules}. {len(waiting)} waiting for a person, "
                f"{len(info)} advisory for information, drawn from "
                f"{', '.join(self._round_domains()) or 'no source'} ({self.source}).")

    def _round_domains(self) -> tuple:
        return tuple(sorted({r.citation.domain for r in list(self.readings.values())
                             if r.citation.domain and r.status not in ("none", "invalid",
                                                                       "unreadable")}))

    def _applies_at_once(self, reading: Reading) -> bool:
        """지금 걸리나. 공식 출처를 문법이 읽었을 때만 — 조이는 규칙이라 사람을 안 기다립니다.

        사람이 이미 확인한 것(approved)도 그대로 걸립니다. 모델이 읽은 것은 출처가 공식이어도
        사람 뒤입니다 — 모델은 무엇도 걸지 못한다는 것이 이 시스템의 뼈대입니다.
        """
        if reading.confirmed_by:
            return True
        return (reading.citation.trust == "official"
                and reading.citation.read_by.startswith("grammar"))

    def _hold_why(self, reading: Reading) -> str:
        if reading.citation.read_by.startswith("model:"):
            return (f"{reading.citation.read_by} 가 읽은 것입니다 — 사람이 확인해야 적용됩니다 "
                    f"({reading.citation.domain})")
        return (f"{reading.citation.domain or '출처 불명'} 은 공식 출처가 아닙니다 — "
                "사람이 확인해야 적용됩니다")

    def _notice(self, reading: Reading, from_tick: int, until_tick: int | None,
                held: bool) -> BriefingNotice:
        hazard = reading.hazard
        kind = hazard.kind
        citation = reading.citation.to_dict()
        if kind == "crane":
            polygon = circle(hazard.centre, self.settings.crane_radius_m)
            ceiling, clearance = hazard.height_m, self.settings.crane_clearance_m
            name = f"CRANE · {hazard.place} · {hazard.height_m:.0f} m"
        elif kind == "closure":
            polygon = circle(hazard.centre, self.settings.closure_radius_m)
            ceiling, clearance = CLOSED_CEILING_M, 0.0
            name = f"CLOSED · {hazard.place}"
        elif kind == "restriction":
            polygon = circle(hazard.centre, hazard.radius_m)
            ceiling, clearance = hazard.ceiling_m, 0.0
            name = (f"TFR · {hazard.radius_m / 1852.0:.1f} NM · "
                    f"{self._nearest_label(hazard.centre) or hazard.place}")
        else:
            polygon = circle(hazard.centre, hazard.radius_m)
            ceiling, clearance = hazard.ceiling_m, 0.0
            name = f"EVENT · {hazard.place}"
        text = f"{reading.citation.title} — {hazard.detail}".strip(" —")
        volume = Volume(
            id=reading.item_id, name=name, polygon=polygon, floor_m=0.0, ceiling_m=ceiling,
            reference="AGL", rule="forbidden", reason=hazard.detail or name,
            source=reading.confirmed_by or reading.citation.read_by,
            clearance_m=clearance, from_tick=from_tick, until_tick=until_tick,
            tags={"kind": kind, "briefing": citation, "place": hazard.place,
                  "landing_area": hazard.landing_area, "detail": hazard.detail,
                  "centre": [round(hazard.centre[0], 6), round(hazard.centre[1], 6)],
                  "note": hazard.note},
        )
        source = "human" if reading.confirmed_by else reading.citation.read_by
        record = BriefingNotice(reading.item_id, name, kind, text, volume, from_tick, until_tick,
                                source, held=held, citation=citation)
        record.confirmed_by = reading.confirmed_by
        return record

    def _nearest_label(self, centre: tuple) -> str:
        """중심에서 가장 가까운 지명(사전의 주소). 이름만 붙이는 것이지 자리를 옮기지 않습니다."""
        addresses = getattr(self.tower.gazetteer, "addresses", []) or []
        near = min(addresses, key=lambda a: _distance_m(centre, (a["lat"], a["lon"])),
                   default=None)
        if near is None or _distance_m(centre, (near["lat"], near["lon"])) > 800.0:
            return ""
        return f"near {near['label']}"

    def _ticks(self, window: Window | None) -> tuple[int, int | None, str]:
        """창을 이 판의 틱으로. 지났으면 왜 안 거는지 한 줄로 말합니다.

        틱 0 은 판의 시계(clock_epoch_z)이고, 브리핑하는 날의 그 시각이 기준입니다. 끝은 지금 +
        max_window_ticks 에서 자릅니다 — 오늘 하루짜리 공지 하나가 영원한 규칙이 되면 안 됩니다.
        """
        tick = self.tower.tick
        horizon = tick + self.settings.max_window_ticks
        if window is None:
            return tick, horizon, ""
        start = self._round_start()
        spt = float(self.tower.performance.seconds_per_tick)
        from_tick = tick if window.start is None else int(
            math.ceil((window.start - start).total_seconds() / spt))
        until_tick = horizon if window.end is None else int(
            (window.end - start).total_seconds() / spt)
        if until_tick <= tick:
            return from_tick, until_tick, "창이 이 판보다 앞에서 닫혔습니다"
        if from_tick > horizon:
            return from_tick, until_tick, "창이 이 판 뒤에 열립니다"
        return max(0, from_tick), min(until_tick, horizon), ""

    def _round_start(self) -> datetime.datetime:
        """틱 0 의 UTC 시각. 판의 시계(0900Z)가 브리핑하는 날의 그 시각입니다."""
        epoch = str(self.tower.performance.clock_epoch_z or "0900").zfill(4)
        day = self.day
        return datetime.datetime(day.year, day.month, day.day, int(epoch[:2]) % 24,
                                 int(epoch[2:]) % 60, tzinfo=datetime.UTC)

    # ---------- 사람의 답, 그리고 폐쇄 회수 ----------

    def _sync_cards(self) -> None:
        """사람이 카드에 답했나. 공지 책이 답입니다 — 우리가 다시 물을 일이 아닙니다."""
        for reading in list(self.readings.values()):
            if reading.status != "held":
                continue
            record = self.tower.notices.get(reading.item_id)
            if record is not None and not record.held:
                reading.status = "approved"
                reading.confirmed_by = record.confirmed_by
                self._store(reading)
                continue
            if record is None:
                why = self.tower.notices.unreadable.get(reading.item_id, "")
                if "거부" in why:
                    reading.status = "refused"
                    self._store(reading)
                elif why:
                    reading.status = "lapsed"
                    self._store(reading)

    def _recall_closures(self) -> None:
        """닫힌 착륙장으로 가던 기체를 불러들입니다. 조이는 규칙은 이미 뜬 비행에도 걸립니다.

        폐쇄 구역 자체는 하늘에서 아무것도 막지 않습니다(천장이 땅보다 낮습니다). 여기서 쓰는
        기둥은 그 자리에 내리려는 경로만 잡기 위한 것이고, 공역에 들어가지 않습니다.
        """
        for reading in list(self.readings.values()):
            if reading.hazard.kind != "closure" or reading.item_id in self._recalled:
                continue
            record = self.tower.notices.get(reading.item_id)
            if record is None or not record.applied:
                continue
            self._recalled.add(reading.item_id)
            if not self._anyone_landing(reading.hazard.centre):
                continue
            column = Volume(id=reading.item_id, name=record.name,
                            polygon=circle(reading.hazard.centre, CLOSURE_COLUMN_M),
                            floor_m=0.0, ceiling_m=None, rule="forbidden",
                            reason=record.name, source=record.source)
            self.tower.recall_flights(column)

    def _anyone_landing(self, centre: tuple) -> bool:
        for state in (self.tower.telemetry or {}).values():
            route = state.get("route") or []
            if not route or float(state.get("alt_m") or 0.0) <= 1.0:
                continue
            last = route[-1]
            if _distance_m(centre, (float(last["lat"]), float(last["lon"]))) \
                    <= self.settings.closure_radius_m:
                return True
        return False

    # ---------- 기록 ----------

    def _store(self, reading: Reading) -> None:
        store = getattr(self.tower, "store", None)
        if store is None:
            return
        # 출처는 tavily 로 적습니다 — 그래야 재시작 뒤에 '본 것' 으로 셉니다(store.DEDUPE_SOURCES).
        store.put_item(reading.item_id, "tavily", reading.text or reading.hazard.detail,
                       self.tower.tick, reading.citation.url, reading.hazard.kind,
                       reading.to_hints())
        store.settle_item(reading.item_id, reading.hazard.kind, reading.citation.read_by,
                          _outcome(reading.status))

    def _ledger(self, action: str, code: str, reason: str, detail: dict, outcome: str,
                verdict=Verdict.AUTO, rationale: str = "", params: dict | None = None) -> None:
        noted = Proposal(asset_id=BRIEFING_ASSET, action=action, cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=rationale[:180],
                         params=params or {})
        decision = Decision(noted.id, verdict, reason[:400], code=code, detail=detail)
        self.tower.ledger.close_entry(
            self.tower.ledger.open_entry(noted, decision,
                                         self.tower._context(None, BRIEFING_CHECKS)), outcome)

    def _ledger_run(self, result: RunResult) -> None:
        plan = result.plan
        detail = {"trigger": plan.trigger, "source": result.source, "places": list(plan.places),
                  "queries": [query["text"] for query in plan.queries],
                  "crawls": [crawl.get("url") for crawl in plan.crawls],
                  "research": plan.research, "cells": len(plan.cells),
                  "pages": len(result.findings), "credits": round(result.credits, 2),
                  "calls": result.calls, "errors": result.errors[:3],
                  "fallback": result.fallback, "summary": result.summary,
                  "summary_by": result.summary_by, "day": plan.day.isoformat()}
        ok = result.status is None or result.status.ok
        self._ledger("briefing_run", "briefing_run",
                     f"브리핑 ({plan.trigger}, {result.source}) · 쪽 {len(result.findings)} · "
                     f"크레딧 {result.credits:.0f}",
                     detail, "noted" if ok else "failed",
                     Verdict.AUTO if ok else Verdict.DENIED, result.summary)

    def _ledger_item(self, reading: Reading) -> None:
        detail = {"item": reading.item_id, "kind": reading.hazard.kind, "status": reading.status,
                  "why": reading.why, "hazard": reading.hazard.to_dict(),
                  **reading.citation.to_dict()}
        readable = reading.status not in ("unreadable", "invalid")
        self._ledger("briefing_item", "briefing_item",
                     f"{reading.hazard.detail or reading.citation.title or '쪽'} · "
                     f"{reading.citation.domain or '출처 불명'}"
                     + ("" if readable else f" · 읽지 못함 — {reading.why}"),
                     detail, "noted" if readable else "unreadable",
                     Verdict.AUTO if readable else Verdict.DENIED,
                     reading.citation.title or reading.text)

    def _ledger_rule(self, reading: Reading, record: BriefingNotice, held: bool) -> None:
        detail = {"item": reading.item_id, "rule": reading.item_id, "kind": reading.hazard.kind,
                  "held": held, "from_tick": reading.from_tick, "until_tick": reading.until_tick,
                  "hazard": reading.hazard.to_dict(), **reading.citation.to_dict()}
        self._ledger("briefing_rule", "briefing_rule",
                     f"{record.name} · 틱 {reading.from_tick}~{reading.until_tick}"
                     + (" · 사람 확인 대기" if held else " · 적용"),
                     detail, "held" if held else "applied",
                     Verdict.HUMAN if held else Verdict.AUTO, record.name,
                     params={"rule": reading.item_id, "kind": reading.hazard.kind})

    def _note_source(self, status: FetchStatus | None) -> None:
        """출처의 실패 ↔ 회복 한 줄. 바뀔 때만 — 주기마다 적으면 원장이 실패로 가득 찹니다."""
        if status is None or self.mode != "live":
            return
        failed_now = not status.ok
        if failed_now == self.source_failed:
            return
        self.source_failed = failed_now
        self.tower._ledger_source_change(BRIEFING_ASSET, status, failed_now)

    # ---------- 화면 ----------

    def snapshot(self) -> dict:
        live = self.live
        credits = (live or self.recorded_client()).credits.to_dict()
        items = sorted((reading for reading in list(self.readings.values())
                        if reading.status != "none"),
                       key=lambda reading: reading.citation.fetched_at)[-KEPT_ITEMS:]
        return {
            "enabled": self.enabled,
            "source": "off" if not self.enabled else (self.source if self.runs else self.mode),
            "mode": self.mode,
            "fallback": self.fallback,
            "last_run_tick": self.last_run_tick,
            "last_trigger": self.last_trigger,
            "runs": self.runs,
            "running": self._running,
            "credits_used": credits["used"],
            "credits_total": credits["total"],
            "budget": credits["budget"],
            "calls": credits["calls"],
            "day": self.day.isoformat(),
            "summary": self.summary or _no_summary(self),
            "summary_by": self.summary_by,
            "domains": list(self.domains),
            "cells": {"briefed": len(self.briefed_cells), "pending": len(self.pending_cells),
                      "km": self.settings.cell_km},
            "ignored": self.ignored,
            "status": None if self.status is None else self.status.to_dict(),
            "items": [{**reading.to_dict(self.tower.tick),
                       "active": reading.active_in(self.tower._round)} for reading in items],
        }




def _outcome(status: str) -> str:
    """기록(sqlite)에 남기는 끝.

    사람을 기다리는 것만 'held' 로 남아야 재시작 때 카드가 돌아옵니다.
    """
    return {"applied": "read", "approved": "approved", "refused": "refused",
            "held": "held", "lapsed": "lapsed", "info": "read", "none": "read",
            "invalid": "unreadable", "unreadable": "unreadable"}.get(status, "read")


def _spoken_date(day: datetime.date) -> str:
    return f"{day.strftime('%B')} {day.day}, {day.year}"


def _borough(area: dict | None) -> str:
    return BOROUGH.get(str((area or {}).get("id") or ""), DEFAULT_BOROUGH)


def _no_summary(desk: BriefingDesk) -> str:
    if not desk.enabled:
        return "브리핑이 꺼져 있습니다."
    return "아직 브리핑하지 않았습니다."
