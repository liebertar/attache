"""Notices the runtime holds: what each one closes, from when, on whose word.

A notice comes in as text. The grammar reads the FAA dialect and the result applies the
tick it lands — a restriction never waits for anyone. Text the grammar cannot read goes to
the Super tier, which compiles it into the same schema; that result is validated hard and
then held until a person confirms it on the approval screen. A model can propose a closure
and can never impose one. The banner on the screen is drawn from this book, not from the
simulator: what is enforced is what is shown.
"""

from dataclasses import dataclass, field

from attache.core.geo import Volume
from attache.core.notam import (
    COMPILE_SYSTEM,
    Clock,
    Notice,
    from_model_form,
    parse_notice,
    shape_problems,
    validate,
)
from attache.llm.client import LlmTier, TieredLlm, parse_json_object


@dataclass
class NoticeRecord:
    id: str
    name: str
    kind: str                       # notam | zone(구조화된 옛 양식)
    text: str
    volume: Volume
    from_tick: int | None
    until_tick: int | None
    source: str                     # grammar | structured | model:<id> | human
    held: bool = False              # 사람 확인 대기. 이 동안은 판정에 안 들어갑니다
    applied: bool = False           # 지금 공역에 들어가 있나
    problems: list[str] = field(default_factory=list)
    confirmed_by: str | None = None

    def due(self, tick: int) -> bool:
        """지금 걸려 있어야 하나. 보류 중이면 아니고, 창이 있으면 창 안이어야 합니다."""
        if self.held:
            return False
        if self.from_tick is not None and tick < self.from_tick:
            return False
        return self.until_tick is None or tick <= self.until_tick

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "from_tick": self.from_tick, "until_tick": self.until_tick,
            "source": self.source, "polygon": [[lat, lon] for lat, lon in self.volume.polygon],
            "floor_m": self.volume.floor_m, "ceiling_m": self.volume.ceiling_m,
            "text": self.text, "held": self.held, "applied": self.applied,
            "confirmed_by": self.confirmed_by,
        }


class NoticeBook:
    def __init__(self, clock: Clock, llm: TieredLlm | None = None):
        self.clock = clock
        self.llm = llm
        self.records: dict[str, NoticeRecord] = {}
        self.unreadable: dict[str, str] = {}     # id → 왜 못 읽었나. 매 폴링마다 다시 묻지 않게

    def get(self, notice_id: str) -> NoticeRecord | None:
        return self.records.get(notice_id)

    def known(self, notice_id: str) -> bool:
        return notice_id in self.records or notice_id in self.unreadable

    def read(self, item: dict,
             bbox: tuple[float, float, float, float] | None) -> NoticeRecord | None:
        """공지 하나를 기록으로. 문법 → 즉시, 모델 → 보류, 둘 다 아니면 None(못 읽음)."""
        notice_id = str(item.get("id") or "")
        if not notice_id or self.known(notice_id):
            return self.records.get(notice_id)
        name = str(item.get("name") or notice_id)
        if item.get("polygon"):
            # 구조화된 옛 양식(polygon 이 실려 옴). 문법과 같은 신뢰도 — 데이터로 온 것입니다.
            problems = shape_problems(item["polygon"])
            if problems:
                self.unreadable[notice_id] = "; ".join(problems)
                return None
            # 게시 틱은 정보이지 창이 아닙니다 — 목록에 실려 왔으면 지금 걸립니다.
            volume = Volume.from_dict({**item, "name": name, "from_tick": None})
            record = NoticeRecord(notice_id, name, str(item.get("kind") or "zone"),
                                  str(item.get("text") or ""), volume,
                                  None, item.get("until_tick"), "structured")
            self.records[notice_id] = record
            return record

        text = str(item.get("text") or "")
        parsed = parse_notice(text, self.clock)
        if parsed is not None:
            record = self._record(notice_id, name, item, parsed, "grammar")
            self.records[notice_id] = record
            return record

        compiled, model, problems = self.compile(text)
        if compiled is None:
            self.unreadable[notice_id] = "; ".join(problems) or "모델 답 없음"
            return None
        problems = validate(compiled, bbox)
        if problems:
            self.unreadable[notice_id] = "; ".join(problems)
            return None
        record = self._record(notice_id, name, item, compiled, f"model:{model}", held=True)
        self.records[notice_id] = record
        return record

    def _record(self, notice_id: str, name: str, item: dict, notice: Notice, source: str,
                held: bool = False) -> NoticeRecord:
        # 문장에 창이 있으면 그 창이 규칙입니다. 없으면 도착한 지금부터, 목록의 until_tick 까지.
        from_tick = notice.from_tick
        until_tick = notice.until_tick if notice.until_tick is not None else item.get("until_tick")
        volume = Volume(
            id=notice_id, name=notice.name or name, polygon=list(notice.polygon),
            floor_m=notice.floor_m, ceiling_m=notice.ceiling_m, reference=notice.reference,
            rule="forbidden", reason=str(item.get("reason") or notice.text), source=source,
            tags={"text": notice.text}, from_tick=from_tick, until_tick=until_tick,
        )
        return NoticeRecord(notice_id, volume.name, str(item.get("kind") or "notam"), notice.text,
                            volume, from_tick, until_tick, source, held=held)

    def compile(self, text: str) -> tuple[Notice | None, str, list[str]]:
        """문법이 못 읽은 문장을 모델에게. (공지, 모델 id, 문제). 모델이 없으면 못 읽은 것입니다."""
        if not text.strip():
            return None, "", ["빈 문장"]
        if self.llm is None or not self.llm.enabled or not self.llm.model_for(LlmTier.SUPER):
            return None, "", ["문법으로 못 읽었고 구조화할 모델이 없음"]
        reply = self.llm.ask(LlmTier.SUPER, COMPILE_SYSTEM,
                             f"Clock: tick 0 is {self.clock.epoch_z}Z, one tick is "
                             f"{self.clock.seconds_per_tick} s.\nNotice: {text}",
                             max_tokens=600, json_object=True)
        if reply is None:
            return None, "", ["모델 답 없음"]
        form = parse_json_object(reply.text)
        notice = None if form is None else from_model_form(form, self.clock, text)
        if notice is None:
            self.llm.discard(LlmTier.SUPER)
            return None, reply.model, ["모델 답이 양식이 아님"]
        return notice, reply.model, []

    def confirm(self, notice_id: str, actor: str, allow: bool) -> NoticeRecord | None:
        """사람이 봤습니다. 승인이면 이제부터 사람의 말로 걸리고, 거부면 기록만 남고 안 걸립니다."""
        record = self.records.get(notice_id)
        if record is None or not record.held:
            return None
        record.confirmed_by = actor
        if allow:
            record.held = False
            record.source = "human"
            record.volume.source = "human"
        else:
            self.records.pop(notice_id)
            self.unreadable[notice_id] = f"{actor} 가 거부"
        return record

    def due(self, tick: int) -> list[NoticeRecord]:
        return [r for r in self.records.values() if not r.applied and r.due(tick)]

    def lapsed(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """더는 걸려 있으면 안 되는 것: 창이 닫혔거나 공지 목록에서 사라졌습니다."""
        return [r for r in self.records.values()
                if r.applied and (not r.due(tick) or r.id not in feed_ids)]

    def forget(self, notice_id: str) -> None:
        self.records.pop(notice_id, None)

    def clear(self) -> None:
        self.records.clear()
        self.unreadable.clear()

    def snapshot(self) -> list[dict]:
        """화면 배너의 원천. 걸려 있거나 예정된 것만 — 보류 중인 것은 승인 목록에 따로 있습니다."""
        return [r.to_dict() for r in self.records.values() if not r.held]
