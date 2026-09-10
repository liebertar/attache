"""What the tower took in, and what code made of it.

Every item (a simulator bulletin, a search result, a line a person typed) is recorded once.
The grammar reads the regular dialects; text it cannot read goes to the Super tier, which
fills the same schema. Code validates every reading — numbers, address, radius, window —
whoever produced it. What applies at once is only a grammar reading of a bulletin from the
tower's own feed; a search snippet or a typed line is held for a person even when the grammar
read it, and so is anything a model read. A model can say "this is weather" or "this is
nothing"; it can never open a hold or close a street by itself. The weather hold this book
keeps is a policy: it tightens at once and loosens only by expiry or by a person.
"""

import hashlib
import os
from dataclasses import dataclass, field

from attache.core.config import Policy, WeatherLimits
from attache.core.geo import Volume
from attache.core.intake import (
    INTAKE_SYSTEM,
    Compiled,
    Gazetteer,
    IncidentReport,
    WeatherReport,
    from_intake_form,
    hint_number,
    incident_problems,
    parse_incident,
    parse_weather,
    weather_problems,
)
from attache.core.notam import Clock, parse_notice, validate
from attache.llm.client import LlmTier, TieredLlm, parse_json_object
from attache.runtime.notices import NoticeRecord

# 한 항목을 모델이 구조화하는 데 주는 시간(초). 공지(NOTICE_TIMEOUT_S)와 같은 이유로 넉넉히 —
# 읽기는 세계 스레드 밖에서 돌고, 늦은 답은 다음 폴링이 거둡니다.
INTAKE_TIMEOUT_S = float(os.getenv("INTAKE_TIMEOUT_S", "60"))
# Tavily 를 몇 초마다 묻나. 뉴스·예보는 분 단위로 바뀌지 않습니다.
INTAKE_PERIOD_S = float(os.getenv("INTAKE_PERIOD_S", "300"))
# 기상 대기가 막는 행동. 땅에서 낸 것만(Policy.ground_only) — 떠 있는 기체는 내려야 합니다.
HELD_ACTIONS = ("fly_route", "reserve_pad", "depart")
HOLD_POLICY_PREFIX = "weather-hold"
# 화면·원장에 남기는 원문 길이.
TEXT_CHARS = 180
KEPT_ITEMS = 20
# 문법이 읽은 것이 그 틱에 걸리는 출처. 시뮬레이터 공지는 관제탑 자기 피드(NOTAM·리콜과 같은 줄)의
# 대역입니다. 검색 결과는 웹 페이지이고 POST /intake 는 누가 보냈는지 모르는 문장이라, 문법이 읽어
# 냈어도 사람이 승인 화면에서 확인해야 규칙이 됩니다 — 2012년 태풍 기사 한 줄이 기단을 세우면 안
# 됩니다.
TRUSTED_SOURCES = frozenset({"sim"})
# 창의 상한. 보고서나 힌트가 말한 until_tick 이 지금 + 기본 길이 × 이 배수를 넘으면 거기서
# 자릅니다 — 모델이 99999999 를 말해도 27분(0.8 s/틱) 넘게 세우지 않습니다.
MAX_WINDOW_HOLDS = 4


@dataclass
class IntakeRecord:
    id: str
    source: str                  # sim | tavily | manual
    text: str
    kind: str | None = None      # weather | incident | notice | none | None(못 읽음)
    read_by: str = ""            # grammar | model:<id> | ""(못 읽음)
    why: str = ""                # 못 읽은 이유
    held: bool = False
    tick: int = 0
    url: str = ""

    @property
    def trusted(self) -> bool:
        return self.source in TRUSTED_SOURCES

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "kind": self.kind, "read_by": self.read_by,
                "why": self.why, "held": self.held, "tick": self.tick, "url": self.url,
                "trusted": self.trusted, "text": self.text[:TEXT_CHARS]}


@dataclass
class WeatherHold:
    """이륙 정지 하나. 정책 셋(HELD_ACTIONS)이 그것을 강제하고, 여기는 그 근거입니다."""

    id: str
    reason: str
    until_tick: int
    since_tick: int
    source: str                  # grammar | human
    report: dict
    breaches: list[str] = field(default_factory=list)
    lift_card: str | None = None  # 승인 화면의 '풀기' 카드(신청서 id)
    later_report: dict | None = None   # 대기 중 도착한, 한도 안의 보고서

    @property
    def policy_ids(self) -> list[str]:
        return [f"{HOLD_POLICY_PREFIX}:{action}" for action in HELD_ACTIONS]

    def policies(self) -> list[Policy]:
        return [Policy(id=policy_id, reason=self.reason, forbid_action=action,
                       active_from_tick=self.since_tick, active_until_tick=self.until_tick,
                       ground_only=True)
                for policy_id, action in zip(self.policy_ids, HELD_ACTIONS, strict=True)]

    def to_dict(self) -> dict:
        return {"id": self.id, "reason": self.reason, "until_tick": self.until_tick,
                "since_tick": self.since_tick, "source": self.source, "report": self.report,
                "breaches": list(self.breaches), "lift_card": self.lift_card,
                "later_report": self.later_report}


def item_id(item: dict) -> str:
    """항목의 이름. 없으면 문장의 해시 — 같은 문장은 한 번만 읽습니다."""
    given = str(item.get("id") or "").strip()
    if given:
        return given
    digest = hashlib.sha1(" ".join(str(item.get("text") or "").split()).encode()).hexdigest()
    return f"{item.get('source') or 'intake'}-{digest[:12]}"


class IntakeBook:
    def __init__(self, clock: Clock, llm: TieredLlm | None, limits: WeatherLimits,
                 gazetteer: Gazetteer):
        self.clock = clock
        self.llm = llm
        self.limits = limits
        self.gazetteer = gazetteer
        self.records: dict[str, IntakeRecord] = {}
        self.hold: WeatherHold | None = None
        self.last_report: dict | None = None
        # 모델이 읽어 한도를 넘은 날씨. 사람이 확인하기 전에는 아무것도 세우지 않습니다.
        self.held_weather: dict[str, dict] = {}
        # 이 책이 만든 공지(사고·제한) id. NoticeBook 의 피드 검사에서 빠지지 않게 합니다.
        self.notice_ids: set[str] = set()
        # 검색 출처의 상태. last_fetch_tick 은 성공한 주기만 — 실패한 빈 주기로 앞당기면 '방금
        # 물었다' 로 읽힙니다. source_failed 는 판을 넘어 남습니다(출처의 일이지 판의 일이 아님).
        self.last_fetch_tick: int | None = None
        self.fetch: dict | None = None
        self.source_failed = False

    # ---------- 알고 있는 것 ----------

    def known(self, key: str) -> bool:
        return key in self.records

    def receive(self, item: dict, tick: int) -> IntakeRecord | None:
        """처음 보는 항목이면 적고 돌려줍니다. 본 것이면 None — 다시 읽지 않습니다."""
        key = item_id(item)
        if key in self.records:
            return None
        record = IntakeRecord(id=key, source=str(item.get("source") or "sim"),
                              text=str(item.get("text") or ""), tick=tick,
                              url=str(item.get("url") or ""))
        self.records[key] = record
        return record

    @property
    def items_read(self) -> int:
        return sum(1 for r in self.records.values() if r.kind is not None)

    @property
    def items_unreadable(self) -> int:
        return sum(1 for r in self.records.values() if r.kind is None and r.why)

    @property
    def can_compile(self) -> bool:
        return (self.llm is not None and self.llm.enabled
                and bool(self.llm.model_for(LlmTier.SUPER)))

    # ---------- 읽기 ----------

    def read_grammar(self, item: dict) -> Compiled | None:
        """문법으로. NOTAM 어투 → 사고(주소 있는) → 날씨 순. 종류가 적혀 왔으면 그 문법만."""
        text = str(item.get("text") or "")
        hint = str(item.get("kind") or "")
        if hint not in ("weather", "incident"):
            notice = parse_notice(text, self.clock)
            if notice is not None:
                return Compiled("notice", notice=notice)
        if hint != "weather":
            incident = parse_incident(text, self.gazetteer, self.clock, item)
            if incident is not None:
                return Compiled("incident", incident=incident)
        if hint != "incident":
            weather = parse_weather(text, self.clock)
            if weather is not None:
                return Compiled("weather", weather=weather)
        return None

    def needs_model(self, item: dict) -> bool:
        if not self.can_compile:
            return False
        return bool(str(item.get("text") or "").strip()) and self.read_grammar(item) is None

    def compile_item(self, item: dict, bbox) -> tuple[Compiled | None, str, str]:
        """(읽은 것, 누가, 못 읽은 이유). 저장하지 않습니다 — 다른 스레드에서 불러도 됩니다.

        범위 검사는 문법이 읽은 것에도 겁니다. 문법이 읽어 냈는데 범위 밖이면 모델에게 다시 묻지
        않습니다 — 그 문장은 읽힌 것이고, 읽힌 값이 말이 안 되는 것입니다.
        """
        text = str(item.get("text") or "")
        if not text.strip():
            return None, "", "빈 문장"
        compiled = self.read_grammar(item)
        if compiled is not None:
            problems = self.problems(compiled, bbox)
            if problems:
                return None, "grammar", "; ".join(problems)
            return compiled, "grammar", ""
        if not self.can_compile:
            return None, "", "문법으로 못 읽었고 구조화할 모델이 없음"
        reply = self.llm.ask(LlmTier.SUPER, INTAKE_SYSTEM,
                             f"Clock: tick 0 is {self.clock.epoch_z}Z, one tick is "
                             f"{self.clock.seconds_per_tick} s.\nText: {text[:2000]}",
                             max_tokens=600, json_object=True, timeout_s=INTAKE_TIMEOUT_S)
        if reply is None:
            return None, "", "모델 답 없음"
        form = parse_json_object(reply.text)
        try:
            compiled = None if form is None else from_intake_form(form, self.gazetteer,
                                                                  self.clock, text, item)
        except ValueError as error:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", str(error)
        if compiled is None:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", "모델 답이 양식이 아님"
        problems = self.problems(compiled, bbox)
        if problems:
            self.llm.discard(LlmTier.SUPER)
            return None, f"model:{reply.model}", "; ".join(problems)
        return compiled, f"model:{reply.model}", ""

    def problems(self, compiled: Compiled, bbox) -> list[str]:
        """모델이 구조화한 것에 거는 검사 전부. 하나라도 걸리면 보류도 안 합니다."""
        if compiled.kind == "weather":
            return weather_problems(compiled.weather)
        if compiled.kind == "incident":
            return incident_problems(compiled.incident, bbox)
        if compiled.kind == "notice":
            return validate(compiled.notice, bbox)
        return []

    def settle(self, record: IntakeRecord, compiled: Compiled | None, read_by: str,
               why: str) -> None:
        record.read_by = read_by
        if compiled is None:
            record.kind = None
            record.why = why or "못 읽음"
            return
        record.kind = compiled.kind
        record.held = compiled.kind != "none" and self.must_hold(record, read_by)

    @staticmethod
    def must_hold(record: IntakeRecord, read_by: str) -> bool:
        """사람이 확인해야 적용되나. 모델이 읽었거나, 관제탑 피드 밖에서 온 글이면 그렇습니다."""
        return read_by.startswith("model:") or not record.trusted

    @staticmethod
    def held_why(record: IntakeRecord, read_by: str) -> str:
        if read_by.startswith("model:"):
            return "모델이 읽은 것은 사람이 확인해야 적용됩니다"
        return f"{record.source} 에서 온 글은 관제탑 공지가 아닙니다 — 사람이 확인해야 적용됩니다"

    # ---------- 날씨 ----------

    def breaches(self, report: WeatherReport) -> list[str]:
        """한도를 넘는 것. 같으면 안 넘은 것입니다 — 한도는 뜰 수 있는 마지막 값입니다."""
        found = []
        if report.gust_mps is not None and report.gust_mps > self.limits.max_gust_mps:
            found.append(f"gusts {report.gust_mps:.0f} m/s > {self.limits.max_gust_mps:.0f}")
        if report.wind_mps is not None and report.wind_mps > self.limits.max_wind_mps:
            found.append(f"wind {report.wind_mps:.0f} m/s > {self.limits.max_wind_mps:.0f}")
        if (report.visibility_m is not None
                and report.visibility_m < self.limits.min_visibility_m):
            found.append(f"visibility {report.visibility_m:.0f} m < "
                         f"{self.limits.min_visibility_m:.0f}")
        return found

    def horizon(self, tick: int) -> int:
        """창이 닿을 수 있는 가장 먼 틱. 그 너머를 말한 보고서·힌트는 여기서 잘립니다."""
        return tick + MAX_WINDOW_HOLDS * int(self.limits.hold_default_ticks)

    def hold_until(self, report: WeatherReport, item: dict, tick: int) -> int:
        """언제까지 세우나. 문장의 창 → 항목의 until_tick → 기본 길이. 상한은 horizon."""
        return self.window_until(report.from_tick, report.until_tick, item, tick)

    def open_hold(self, record_id: str, report: WeatherReport, breaches: list[str],
                  until_tick: int, tick: int, source: str) -> WeatherHold:
        reason = "WEATHER HOLD · " + " · ".join(breaches)
        self.hold = WeatherHold(id=record_id, reason=reason, until_tick=until_tick,
                                since_tick=tick, source=source, report=report.to_dict(),
                                breaches=list(breaches))
        self.last_report = {**report.to_dict(), "id": record_id, "source": source,
                            "breaches": list(breaches), "tick": tick}
        return self.hold

    def note_report(self, record_id: str, report: WeatherReport, breaches: list[str],
                    tick: int, source: str) -> None:
        self.last_report = {**report.to_dict(), "id": record_id, "source": source,
                            "breaches": list(breaches), "tick": tick}
        if self.hold is not None and not breaches:
            self.hold.later_report = dict(self.last_report)

    def close_hold(self) -> WeatherHold | None:
        hold, self.hold = self.hold, None
        return hold

    # ---------- 사고 ----------

    def incident_record(self, record_id: str, report: IncidentReport, text: str, source: str,
                        until_tick: int, held: bool) -> NoticeRecord:
        """사고를 공지 기록으로. 그 뒤는 NoticeBook 의 길 — 회수·거절·착륙 불가·화면 채색."""
        volume = Volume(
            id=record_id, name=report.name, polygon=report.polygon(), floor_m=0.0,
            ceiling_m=None, reference="AGL", rule="forbidden", reason=report.name, source=source,
            tags={"text": text, "incident": report.kind, "place": report.place,
                  "centre": [round(report.centre[0], 6), round(report.centre[1], 6)],
                  "radius_m": round(report.radius_m, 1), "building_id": report.building_id},
            from_tick=report.from_tick, until_tick=until_tick,
        )
        self.notice_ids.add(record_id)
        return NoticeRecord(record_id, report.name, "incident", text, volume, report.from_tick,
                            until_tick, source, held=held)

    def window_until(self, from_tick: int | None, until_tick: int | None, item: dict,
                     tick: int) -> int:
        """문장의 창 → 항목의 until_tick 힌트(수가 아니면 없는 것) → 기본 길이. 상한은 horizon."""
        hinted = hint_number(item.get("until_tick"), float("nan"))
        if until_tick is not None:
            chosen = int(until_tick)
        elif hinted == hinted:
            chosen = int(hinted)
        else:
            chosen = max(tick, from_tick or tick) + int(self.limits.hold_default_ticks)
        return min(chosen, self.horizon(tick))

    # ---------- 판이 바뀌면 ----------

    def clear(self) -> None:
        self.records.clear()
        self.hold = None
        self.last_report = None
        self.held_weather.clear()
        self.notice_ids.clear()

    # ---------- 화면 ----------

    def snapshot(self, tavily_on: bool) -> dict:
        recent = sorted(self.records.values(), key=lambda r: r.tick)[-KEPT_ITEMS:]
        tavily = "off" if not tavily_on else "failed" if self.source_failed else "enabled"
        return {
            "sources": {"tavily": tavily, "sim": True},
            "last_fetch_tick": self.last_fetch_tick,
            "fetch": self.fetch,
            "items_read": self.items_read, "items_unreadable": self.items_unreadable,
            "items": [r.to_dict() for r in recent],
        }

    def weather_snapshot(self) -> dict:
        return {
            "hold": None if self.hold is None else self.hold.to_dict(),
            "last_report": self.last_report,
            "held": list(self.held_weather.values()),
        }


def incident_snapshot(records: list[NoticeRecord], tick: int) -> list[dict]:
    """/state.incidents. 사고 공지만, 중심·반경까지 — 화면 배너와 승인 카드가 읽는 값입니다.

    창이 닫힌 것은 뺍니다(문법이 읽은 공지 기록은 창이 닫혀도 책에 남습니다)."""
    out = []
    for record in records:
        if record.kind != "incident":
            continue
        if record.until_tick is not None and tick > record.until_tick:
            continue
        tags = record.volume.tags or {}
        out.append({"id": record.id, "name": record.name, "kind": tags.get("incident"),
                    "place": tags.get("place"), "centre": tags.get("centre"),
                    "radius_m": tags.get("radius_m"), "from_tick": record.from_tick,
                    "until_tick": record.until_tick, "source": record.source,
                    "applied": record.applied, "held": record.held,
                    "confirmed_by": record.confirmed_by, "text": record.text[:TEXT_CHARS]})
    return out
