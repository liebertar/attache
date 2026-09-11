"""Notices the runtime holds: what each one closes, from when, on whose word.

A notice comes in as text. The grammar reads the FAA dialect and the result applies the
tick it lands — a restriction never waits for anyone. Text the grammar cannot read goes to
the Super tier, which compiles it into the same schema; that result is validated hard and
then held until a person confirms it on the approval screen. A model can propose a closure
and can never impose one. The banner on the screen is drawn from this book, not from the
simulator: what is enforced is what is shown.
"""

import os
from dataclasses import dataclass, field

from holdshort.core.geo import Volume
from holdshort.core.notam import (
    COMPILE_SYSTEM,
    Clock,
    Notice,
    from_model_form,
    parse_notice,
    shape_problems,
    validate,
)
from holdshort.llm.client import LlmTier, TieredLlm, parse_json_object

# 공지 하나를 모델이 구조화하는 데 주는 시간(초). 런타임의 기본 20초로는 로컬 30B 대역이 넉 판 중
# 세 판을 못 읽었습니다(5~27초 걸림). 읽기는 세계 스레드 밖에서 도니(service._read_later) 길어도
# 틱은 멈추지 않고, 늦은 답은 다음 폴링이 거둡니다.
NOTICE_TIMEOUT_S = float(os.getenv("NOTICE_TIMEOUT_S", "60"))


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

    @property
    def can_compile(self) -> bool:
        return (self.llm is not None and self.llm.enabled
                and bool(self.llm.model_for(LlmTier.SUPER)))

    def needs_model(self, item: dict) -> bool:
        """문법·구조화 양식으로는 못 읽고 모델이 있어야 읽히는 공지인가(모델이 없으면 False —
        그건 그냥 못 읽는 것이고 즉시 그렇게 기록됩니다)."""
        if item.get("polygon") or not self.can_compile:
            return False
        text = str(item.get("text") or "")
        return bool(text.strip()) and parse_notice(text, self.clock) is None

    def read(self, item: dict,
             bbox: tuple[float, float, float, float] | None) -> NoticeRecord | None:
        """공지 하나를 기록으로. 문법 → 즉시, 모델 → 보류, 둘 다 아니면 None(못 읽음)."""
        notice_id = str(item.get("id") or "")
        if not notice_id or self.known(notice_id):
            return self.records.get(notice_id)
        return self.settle(item, self.compile_item(item, bbox))

    def compile_item(self, item: dict, bbox: tuple[float, float, float, float] | None
                     ) -> tuple[NoticeRecord | None, str]:
        """기록을 만들되 저장하지 않습니다 — 다른 스레드에서 불러도 됩니다. (기록, 못 읽은 이유).

        모델 호출이 여기 있습니다. 세계 스레드는 settle() 로 결과만 받아 적습니다.
        """
        notice_id = str(item.get("id") or "")
        name = str(item.get("name") or notice_id)
        if item.get("polygon"):
            # 구조화된 옛 양식(polygon 이 실려 옴). 문법과 같은 신뢰도 — 데이터로 온 것입니다.
            problems = shape_problems(item["polygon"])
            if problems:
                return None, "; ".join(problems)
            # 게시 틱은 정보이지 창이 아닙니다 — 목록에 실려 왔으면 지금 걸립니다.
            volume = Volume.from_dict({**item, "name": name, "from_tick": None})
            return NoticeRecord(notice_id, name, str(item.get("kind") or "zone"),
                                str(item.get("text") or ""), volume,
                                None, item.get("until_tick"), "structured"), ""

        text = str(item.get("text") or "")
        parsed = parse_notice(text, self.clock)
        if parsed is not None:
            return self._record(notice_id, name, item, parsed, "grammar"), ""

        compiled, model, problems = self.compile(text)
        if compiled is None:
            return None, "; ".join(problems) or "모델 답 없음"
        problems = validate(compiled, bbox)
        if problems:
            return None, "; ".join(problems)
        return self._record(notice_id, name, item, compiled, f"model:{model}", held=True), ""

    def adopt(self, notice_id: str, name: str, item: dict, notice: Notice, source: str,
              held: bool = False) -> NoticeRecord:
        """다른 책(정보 수집)이 읽어 낸 공지를 이 책에 올립니다. 그 뒤는 같은 길입니다 —
        due/lapsed/held, 회수와 거절, 화면 채색."""
        record = self._record(notice_id, name, item, notice, source, held=held)
        self.records[notice_id] = record
        return record

    def settle(self, item: dict, result: tuple[NoticeRecord | None, str]) -> NoticeRecord | None:
        """compile_item 의 결과를 적습니다. 못 읽은 것은 이유와 함께(매 폴링마다 다시 묻지 않게)."""
        notice_id = str(item.get("id") or "")
        record, why = result
        if record is None:
            self.unreadable[notice_id] = why or "모델 답 없음"
            return None
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
        if not self.can_compile:
            return None, "", ["문법으로 못 읽었고 구조화할 모델이 없음"]
        reply = self.llm.ask(LlmTier.SUPER, COMPILE_SYSTEM,
                             f"Clock: tick 0 is {self.clock.epoch_z}Z, one tick is "
                             f"{self.clock.seconds_per_tick} s.\nNotice: {text}",
                             max_tokens=600, json_object=True, timeout_s=NOTICE_TIMEOUT_S)
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

    # 아래 목록들은 records 의 사본 위에서 돕니다. 승인(HTTP 스레드)이 기록을 지우는 사이에 세계
    # 스레드가 같은 dict 를 돌면 "dictionary changed size during iteration" 으로 폴링이 죽습니다.

    def due(self, tick: int) -> list[NoticeRecord]:
        return [r for r in list(self.records.values()) if not r.applied and r.due(tick)]

    def lapsed(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """더는 걸려 있으면 안 되는 것: 창이 닫혔거나 공지 목록에서 사라졌습니다."""
        return [r for r in list(self.records.values())
                if r.applied and (not r.due(tick) or r.id not in feed_ids)]

    def stale_held(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """보류 중인데 더는 물을 것이 없는 것: 창이 닫혔거나 공지 목록에서 빠졌습니다.

        그대로 두면 사람 확인 카드와 '사람 대기' 배너가 판이 끝날 때까지 남고, 뒤늦은 확인은
        아무것도 걸지 못합니다(due 가 창을 봅니다).
        """
        return [r for r in list(self.records.values())
                if r.held and self._over(r, tick, feed_ids)]

    def stale_confirmed(self, tick: int, feed_ids: set[str]) -> list[NoticeRecord]:
        """사람이 확인했지만 걸린 적 없이 지나간 것: 창이 열리기 전에 닫혔거나 목록에서 빠졌습니다.

        그대로 두면 /state.notices 에 판이 끝날 때까지 남고, 다시 물을 문법도 없어 잊어야 합니다
        (문법이 읽은 기록은 창이 닫혀도 남겨 둡니다 — 다시 읽는 데 드는 것이 없습니다).
        """
        return [r for r in list(self.records.values())
                if r.source == "human" and not r.held and not r.applied
                and self._over(r, tick, feed_ids)]

    @staticmethod
    def _over(record: NoticeRecord, tick: int, feed_ids: set[str]) -> bool:
        return ((record.until_tick is not None and tick > record.until_tick)
                or record.id not in feed_ids)

    def forget(self, notice_id: str, why: str | None = None) -> None:
        """기록을 지웁니다. why 를 주면 같은 id 가 다시 와도 모델에게 다시 묻지 않습니다."""
        self.records.pop(notice_id, None)
        if why:
            self.unreadable[notice_id] = why

    def clear(self) -> None:
        self.records.clear()
        self.unreadable.clear()

    def snapshot(self) -> list[dict]:
        """화면 배너의 원천. 걸려 있거나 예정된 것과, 사람을 기다리는 것.

        보류 기록도 싣습니다(held=True, applied=False). 배너는 held 를 보고 '사람이 확인해야
        적용됩니다' 라고 씁니다 — 빼면 모델이 읽은 공지는 승인 화면에만 있고 지도에는 그 공지가
        '아직 못 읽음' 으로 남습니다. 강제되는 것은 applied 가 말하고, 보류는 아무것도 안 막습니다.
        """
        return [r.to_dict() for r in list(self.records.values())]

    def pending(self) -> list[dict]:
        """사람 확인을 기다리는 것만. 승인 화면과 같은 목록입니다."""
        return [r.to_dict() for r in list(self.records.values()) if r.held]
