"""A tower advisory: what the desk tells an operator whose filings keep being refused.

It is information, not a decision. After a run of refusals the runtime lists the options the
code can see — hold until the other corridor clears, lift the same legs, wait for the notice
window, decline, ask a person — and has the deterministic judge check each one where a check
applies. A model (the Super tier, if configured) may write the two-sentence summary and pick
one id out of that list; anything else it says is discarded and a rule picks instead. The
advisory goes on the ledger and into /state, and changes nothing: the operator still files
whatever it files, and the same judge reads it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from shared.llm.client import LlmTier, TieredLlm, parse_json_object

# 연속 거절 이 횟수마다 권고 하나. 두 번은 재작성 사다리(직선 → 우회 → 고도) 안이라 정상입니다.
ADVISORY_AFTER = 3
# 운영사의 교차 해결 사다리와 같은 높이(drone/agent/loop.py ALTITUDE_SHIFT_M). 런타임은 기체
# 패키지를 들여오지 않으므로 값을 따로 둡니다 — 두 값이 갈리면 권고가 운영사가 못 내는 길을
# 말합니다.
CLIMB_M = 30.0
# 권고에 싣는 최근 거절 수. 여섯 번째 거절의 권고에 여섯 개가 다 실리면 됩니다.
KEEP_REFUSALS = ADVISORY_AFTER * 2
SUMMARY_LIMIT = 400
# 권고 문구를 모델에게 맡길 때의 예산(초). 거절 답장이 이 뒤에 나가므로 길면 운영사가 기다립니다.
ADVISORY_TIMEOUT_S = 12.0

ADVISORY_SYSTEM = (
    "You are the tower desk for an uncrewed delivery fleet. One aircraft's route filings keep "
    "being refused by a deterministic judge. You are given the refusals and a fixed list of "
    "options that the code has already checked against the same judge. You decide nothing and "
    "change nothing: pick ONE option id from the list and write a two-sentence summary for the "
    'controller. Reply with one JSON object and nothing else: {"choice": "<option id>", '
    '"summary": "two sentences"}. Never invent an option that is not in the list.'
)


@dataclass
class Refusal:
    """거절 하나에서 권고가 필요로 하는 것. 원장에 남는 값과 같은 이름을 씁니다."""

    asset: str
    tick: int
    code: str
    policy_hit: str | None
    blocked_kind: str | None
    blocked_volume: str | None
    blocked_asset: str | None
    blocked_until_tick: int | None
    proposal_id: str
    action: str
    legs: list = field(default_factory=list)        # 마지막으로 낸 경로. 고도 선택지의 재료
    params: dict = field(default_factory=dict)      # legs 를 뺀 신청서 params (pad 등)
    resource: str | None = None                     # 신청서의 resource(착륙대). 끝점 검사의 목적지

    @property
    def traffic(self) -> bool:
        return self.policy_hit == "traffic" or self.blocked_kind in ("traffic", "landing")

    @property
    def signature(self) -> tuple:
        """같은 막힘인가. 같은 상대·같은 구역·같은 틱까지의 거절은 되풀이지 새 사정이 아닙니다."""
        return (self.action, self.code, self.policy_hit, self.blocked_kind, self.blocked_volume,
                self.blocked_asset, self.blocked_until_tick)

    def to_dict(self) -> dict:
        return {"tick": self.tick, "code": self.code, "policy_hit": self.policy_hit,
                "blocked_kind": self.blocked_kind, "blocked_volume": self.blocked_volume,
                "blocked_asset": self.blocked_asset,
                "blocked_until_tick": self.blocked_until_tick, "proposal": self.proposal_id}


@dataclass
class Option:
    id: str
    label: str
    legal: bool
    why: str
    until_tick: int | None = None
    shift_m: float | None = None

    def to_dict(self) -> dict:
        out = {"id": self.id, "label": self.label, "legal": self.legal, "why": self.why}
        if self.until_tick is not None:
            out["until_tick"] = self.until_tick
        if self.shift_m is not None:
            out["shift_m"] = self.shift_m
        return out


def lifted(legs: list[dict], shift_m: float = CLIMB_M) -> list[dict]:
    return [{**leg, "alt_m": round(float(leg.get("alt_m") or 0.0) + shift_m, 1)} for leg in legs]


def build_options(refusals: list[Refusal], judge: Callable[[Refusal, list[dict]], str | None],
                  notice_until: Callable[[str | None], int | None],
                  airborne: bool) -> list[Option]:
    """코드가 만드는 선택지. 판정이 닿는 것은 판정으로 확인합니다 — 실행은 하지 않습니다.

    순서가 곧 규칙의 우선순위입니다: 지상 대기 → 고도 → 공지 창 → 반려 → 사람.
    judge(refusal, legs) 는 그 경로가 지금 막히는 이유(없으면 None), notice_until(volume_id) 는
    그 구역이 공지라면 닫히는 틱입니다.
    """
    options: list[Option] = []
    last = refusals[-1] if refusals else None

    crossing = next((r for r in reversed(refusals) if r.traffic and r.blocked_until_tick), None)
    if crossing is not None:
        until = int(crossing.blocked_until_tick)
        options.append(Option(
            "hold", f"hold on the ground until tick {until}", not airborne,
            (f"{crossing.blocked_asset} clears that volume at tick {until}" if not airborne
             else "the aircraft is airborne — it cannot hold on the ground"),
            until_tick=until))

    if last is not None and len(last.legs) >= 2:
        higher = lifted(last.legs)
        problem = judge(last, higher)
        options.append(Option(
            "climb", f"climb +{CLIMB_M:.0f} m on the last filed legs", problem is None,
            problem or f"the same legs pass the judge {CLIMB_M:.0f} m higher", shift_m=CLIMB_M))

    notice = next(((r, notice_until(r.blocked_volume)) for r in reversed(refusals)
                   if r.blocked_volume and notice_until(r.blocked_volume) is not None), None)
    if notice is not None:
        refusal, until = notice
        options.append(Option(
            "notice_window", f"wait for the notice window to close at tick {until}", True,
            f"{refusal.blocked_volume} lapses at tick {until}", until_tick=int(until)))

    options.append(Option("decline", "decline the job", True,
                          "no aircraft flies; the order goes back to dispatch"))
    options.append(Option("escalate", "escalate to a person", True,
                          "a controller looks at the refusals"))
    return options


def rule_pick(options: list[Option]) -> str:
    """위 순서에서 처음 합법인 것. 마지막 둘은 항상 합법이라 언제나 하나는 있습니다."""
    return next(o.id for o in options if o.legal)


def template_summary(asset: str, refusals: list[Refusal], options: list[Option],
                     chosen: str, trigger: str) -> str:
    codes = ", ".join(sorted({r.blocked_kind or r.policy_hit or r.code for r in refusals}))
    label = next((o.label for o in options if o.id == chosen), chosen)
    if trigger == "decline_after_refusals":
        head = f"{asset} declined the order after {len(refusals)} refusals ({codes})."
    else:
        head = f"{asset} was refused {len(refusals)} times in a row ({codes})."
    return f"{head} The rules suggest: {label}."


def parse_advice(text: str, options: list[Option]) -> tuple[str | None, str]:
    """모델 답에서 (선택지 id 또는 None, 요약). 목록 밖이거나 판정이 막은 것이면 id 는 None."""
    form = parse_json_object(text)
    if not form:
        return None, ""
    legal = {o.id for o in options if o.legal}
    # 목록은 "[hold] …" 꼴이라 모델이 괄호째 되읽습니다(실주행: 권고 160건 중 155건이 "[hold]" 로
    # 답해 규칙 선택으로 떨어짐). 괄호·따옴표는 양식이지 선택이 아니라 벗기고 봅니다.
    choice = str(form.get("choice") or "").strip().strip("[]()\"'` ").strip().lower()
    summary = " ".join(str(form.get("summary") or "").split())[:SUMMARY_LIMIT]
    return (choice if choice in legal else None), summary


class AdvisoryDesk:
    """기체마다 연속 거절을 세고, 권고가 필요할 때 그 내용을 씁니다."""

    def __init__(self, llm: TieredLlm | None):
        self.llm = llm
        self.streaks: dict[str, list[Refusal]] = {}
        self.latest: dict[str, dict] = {}       # 기체 → 마지막 권고 (화면용)
        # 기체마다, 같은 막힘(signature)에 몇 번 거절됐나. 같은 막힘 세 번에 권고 하나, 그 막힘에는
        # 다시 없습니다 — 실주행에서 착륙대를 남이 쓰는 몇 틱마다 재신청이 오면 세 번째마다
        # "틱 X 까지 지상 대기" 가 또 적혀 65분에 190건이 됐고, 그때마다 30B 를 한 번씩 불렀습니다.
        self._by_block: dict[str, dict[tuple, int]] = {}

    @property
    def has_model(self) -> bool:
        return (self.llm is not None and self.llm.enabled
                and bool(self.llm.model_for(LlmTier.SUPER)))

    def refused(self, asset: str, refusal: Refusal) -> bool:
        """거절 하나를 더합니다. 이번 것으로 권고 차례가 됐으면 True.

        같은 막힘(같은 상대·구역·틱까지)의 세 번째 거절에 하나. 그 막힘에는 다시 쓰지 않고,
        다른 막힘이 세 번 쌓이면 그것에 하나 — 같은 상대가 같은 틱까지 막고 있는데 세 번마다
        같은 말을 되풀이하지 않습니다. 승인이 나가면 전부 새로 셉니다.
        """
        streak = self.streaks.setdefault(asset, [])
        streak.append(refusal)
        counts = self._by_block.setdefault(asset, {})
        counts[refusal.signature] = counts.get(refusal.signature, 0) + 1
        return counts[refusal.signature] == ADVISORY_AFTER

    def succeeded(self, asset: str) -> None:
        """승인이 실행됐습니다(또는 주문이 반려됐습니다). 연속은 끊깁니다."""
        self.streaks.pop(asset, None)
        self._by_block.pop(asset, None)

    def declined(self, asset: str) -> bool:
        """주문 반려 신청이 왔습니다. 거절 뒤의 반려면 권고 차례입니다."""
        return bool(self.streaks.get(asset))

    def streak(self, asset: str) -> list[Refusal]:
        return list(self.streaks.get(asset, []))[-KEEP_REFUSALS:]

    def clear(self) -> None:
        self.streaks.clear()
        self.latest.clear()
        self._by_block.clear()

    def snapshot(self) -> list[dict]:
        return [self.latest[asset] for asset in sorted(self.latest)]

    def compose(self, asset: str, trigger: str, refusals: list[Refusal],
                options: list[Option], airborne: bool) -> dict:
        """권고 본문. 모델이 있으면 요약과 선택을 묻고, 목록 밖이거나 답이 없으면 규칙이 고릅니다.
        """
        fallback = rule_pick(options)
        chosen, summary, model, source = fallback, "", "", "rules"
        if self.has_model:
            reply = self.llm.ask(LlmTier.SUPER, ADVISORY_SYSTEM,
                                 self._brief(asset, refusals, options, airborne),
                                 max_tokens=240, json_object=True, timeout_s=ADVISORY_TIMEOUT_S)
            if reply is not None:
                model = reply.model
                picked, said = parse_advice(reply.text, options)
                if picked is None:
                    # 목록 밖의 답. 요약도 같이 버립니다 — 없는 선택지를 설명한 문장일 수 있습니다.
                    self.llm.discard(LlmTier.SUPER)
                else:
                    chosen, summary, source = picked, said, "super"
        if not summary:
            summary = template_summary(asset, refusals, options, chosen, trigger)
        return {"trigger": trigger, "refusals": [r.to_dict() for r in refusals],
                "options": [o.to_dict() for o in options], "chosen": chosen,
                "summary": summary, "model": model, "source": source}

    @staticmethod
    def _brief(asset: str, refusals: list[Refusal], options: list[Option],
               airborne: bool) -> str:
        lines = [f"Aircraft: {asset} (airborne: {'yes' if airborne else 'no'})",
                 "Refusals, oldest first:"]
        for index, r in enumerate(refusals, start=1):
            what = r.blocked_kind or r.policy_hit or r.code
            who = r.blocked_asset or r.blocked_volume or "-"
            until = f" until tick {r.blocked_until_tick}" if r.blocked_until_tick else ""
            lines.append(f"{index}. tick {r.tick}: {r.action} refused ({what}) by {who}{until}")
        lines.append("Options (ids in brackets):")
        for o in options:
            verdict = "legal" if o.legal else f"NOT legal: {o.why}"
            lines.append(f"- [{o.id}] {o.label} — {verdict}")
        return "\n".join(lines)
