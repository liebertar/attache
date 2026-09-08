"""Picks one proposal out of several that already passed authority.

It never authors an action. It returns an index into a list the runtime built. If the
model answers with anything else, or does not answer, the rule below decides instead.
"""

from attache.core.models import BLAST_RANK, Proposal
from attache.llm.client import LlmTier, TieredLlm, parse_choice

SYSTEM = (
    "You are an arbiter for an uncrewed fleet. Several proposals want the same single "
    "resource. All of them already passed the safety and budget checks. Choose exactly "
    "one. Reply with only its number and nothing else. Do not invent new actions."
)


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


class Arbiter:
    def __init__(self, llm: TieredLlm):
        self.llm = llm

    def choose(self, candidates: list[Proposal], telemetry: dict) -> tuple[Proposal, str]:
        if len(candidates) == 1:
            return candidates[0], "single"

        fallback, fallback_reason = by_rule(candidates, telemetry)
        reply = self._ask(candidates, telemetry)
        if reply is None:
            return fallback, fallback_reason

        picked, model = reply
        return candidates[picked], f"ultra:{model}"

    def _ask(self, candidates: list[Proposal], telemetry: dict):
        lines = []
        for index, proposal in enumerate(candidates, start=1):
            asset = telemetry.get(proposal.asset_id, {})
            lines.append(
                f"{index}. asset={proposal.asset_id} action={proposal.action} "
                f"cost=${proposal.cost_usd:.0f} blast={proposal.blast_radius} "
                f"battery={asset.get('battery', '?')}% "
                f"passengers={asset.get('passengers', 0)} why={proposal.rationale}"
            )
        reply = self.llm.ask(
            LlmTier.ULTRA,
            SYSTEM,
            "Resource: " + (candidates[0].resource or "?") + "\n" + "\n".join(lines),
            max_tokens=8,
        )
        if reply is None:
            return None
        index = parse_choice(reply.text, len(candidates))
        return None if index is None else (index, reply.model)
