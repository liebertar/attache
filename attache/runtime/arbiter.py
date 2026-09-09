"""Picks one proposal out of several that already passed authority.

It never authors an action. It returns an index into a list the runtime built, plus one
sentence saying why, which goes on the record. If the model answers with anything else,
or does not answer, the rule below decides instead.
"""

from dataclasses import dataclass

from attache.core.models import BLAST_RANK, Proposal
from attache.llm.client import LlmTier, TieredLlm, parse_choice, parse_json_object

# 돈 이야기는 없습니다. 예산은 이미 authority 가 봤고, 여기서 다시 꺼내면 모델이 싼 쪽을
# 고르는 것을 안전 판단처럼 적게 됩니다. 남은 것은 누가 먼저 자원을 받아야 하느냐뿐입니다.
SYSTEM = (
    "You are an arbiter for an uncrewed fleet. Several requests want the same single "
    "resource at the same time. Every one of them has already passed the safety checks; "
    "you only decide the order. Choose exactly one. Reply with one JSON object and nothing "
    'else: {"choice": <the number from the list>, "reason": "one short sentence"}. '
    "Do not invent new actions or conditions."
)
REASON_LIMIT = 140


@dataclass
class Choice:
    proposal: Proposal
    how: str            # single | rule:… | ultra:<model id>
    reason: str = ""    # 모델이 말한 한 줄. 규칙이 골랐으면 비어 있습니다


def by_rule(candidates: list[Proposal], telemetry: dict) -> tuple[Proposal, str]:
    """영향 범위가 큰 쪽, 그다음 배터리가 낮은 쪽, 그다음 먼저 신청한 쪽."""

    def key(proposal: Proposal):
        asset = telemetry.get(proposal.asset_id, {})
        return (
            -BLAST_RANK.get(proposal.blast_radius, 0),
            asset.get("battery", 100.0),
            proposal.filed_at,
        )

    winner = sorted(candidates, key=key)[0]
    return winner, "rule:blast>battery>filed"


def parse_verdict(text: str, option_count: int) -> tuple[int, str] | None:
    """{"choice": n, "reason": "…"} 또는 번호 하나. 문장이거나 범위 밖이면 None."""
    form = parse_json_object(text)
    if form is not None and "choice" in form:
        try:
            index = int(form["choice"]) - 1
        except (TypeError, ValueError):
            return None
        if not 0 <= index < option_count:
            return None
        return index, str(form.get("reason") or "")[:REASON_LIMIT]
    index = parse_choice(text, option_count)
    return None if index is None else (index, "")


class Arbiter:
    def __init__(self, llm: TieredLlm):
        self.llm = llm

    def pick(self, candidates: list[Proposal], telemetry: dict) -> Choice:
        if len(candidates) == 1:
            return Choice(candidates[0], "single")

        fallback, fallback_reason = by_rule(candidates, telemetry)
        verdict = self._ask(candidates, telemetry)
        if verdict is None:
            return Choice(fallback, fallback_reason)

        picked, reason, model = verdict
        return Choice(candidates[picked], f"ultra:{model}", reason)

    def choose(self, candidates: list[Proposal], telemetry: dict) -> tuple[Proposal, str]:
        choice = self.pick(candidates, telemetry)
        return choice.proposal, choice.how

    def _ask(self, candidates: list[Proposal], telemetry: dict):
        lines = []
        for index, proposal in enumerate(candidates, start=1):
            asset = telemetry.get(proposal.asset_id, {})
            lines.append(
                f"{index}. asset={proposal.asset_id} action={proposal.action} "
                f"impact={proposal.blast_radius} battery={asset.get('battery', '?')}% "
                f"passengers={asset.get('passengers', 0)} why={proposal.rationale}"
            )
        reply = self.llm.ask(
            LlmTier.ULTRA,
            SYSTEM,
            "Resource: " + (candidates[0].resource or "?") + "\n" + "\n".join(lines),
            max_tokens=160,
            json_object=True,
        )
        if reply is None:
            return None
        verdict = parse_verdict(reply.text, len(candidates))
        if verdict is None:
            self.llm.discard(LlmTier.ULTRA)
            return None
        index, reason = verdict
        return index, reason, reply.model
