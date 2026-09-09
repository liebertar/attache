"""Turns a concern into a request form.

Both worlds use this same file. The agents in the unguarded world are exactly as capable
and exactly as well behaved; what they lack is a place to put the rules.
"""

from attache.agent.detect import Concern
from attache.core.models import Proposal
from attache.llm.client import LlmTier, TieredLlm, parse_json_object

ALLOWED_ACTIONS = {"decline_job", "fly_route", "reserve_pad", "charge", "fast_charge", "divert_ground",
                   "disengage_autonomy", "depart"}

COSTS = {"decline_job": 0.0, "fly_route": 12.0, "reserve_pad": 28.0, "charge": 22.0, "fast_charge": 60.0,
         "divert_ground": 35.0, "disengage_autonomy": 0.0, "depart": 0.0}

BLAST = {"decline_job": "none", "fly_route": "schedule", "reserve_pad": "schedule", "charge": "none",
         "fast_charge": "none", "divert_ground": "cargo",
         "disengage_autonomy": "public", "depart": "none"}

def system_for(pads: tuple[str, ...]) -> str:
    """양식 설명. 패드 이름은 런타임이 알려준 것을 그대로 씁니다 —
    여기에 적어두면 이름이 바뀌는 순간 조용히 어긋납니다(실제로 어긋나 있었습니다)."""
    choices = "|".join(f'"{pad}"' for pad in pads) or "null"
    return (
        "You watch one uncrewed vehicle. You cannot act. You may only fill in a request form "
        "that a runtime will judge. Reply with one JSON object and nothing else: "
        '{"action": one of ' + str(sorted(ALLOWED_ACTIONS)) + f', "pad": {choices}|null, '
        '"rationale": "one short sentence"}. Never invent an action outside the list.'
    )


def by_rule(
    concern: Concern, telemetry: dict, pad: str, banned: frozenset[str] = frozenset()
) -> Proposal:
    asset_id = telemetry.get("id", "?")
    if concern.kind == "needs_route":
        action, chosen_pad = "fly_route", None
    elif concern.kind in ("charged", "needs_reload"):
        action, chosen_pad = "depart", None
    elif concern.kind == "autonomy_fault":
        action, chosen_pad = "disengage_autonomy", None
    elif concern.kind in ("motor_fault", "needs_pad"):
        action, chosen_pad = "reserve_pad", pad
    elif concern.kind == "needs_charge":
        wants_fast = concern.urgency == "high" and "fast_charge" not in banned
        action = "fast_charge" if wants_fast else "charge"
        chosen_pad = None
    else:
        action, chosen_pad = "reserve_pad", pad

    return _build(asset_id, action, chosen_pad, concern.detail, "rules")


def _build(asset_id: str, action: str, pad: str | None, rationale: str, author: str) -> Proposal:
    return Proposal(
        asset_id=asset_id,
        action=action,
        cost_usd=COSTS.get(action, 0.0),
        blast_radius=BLAST.get(action, "none"),
        rationale=rationale,
        params={"pad": pad} if pad else {},
        resource=pad,
        author=author,
    )


class Proposer:
    def __init__(self, llm: TieredLlm):
        self.llm = llm

    def write(
        self, concern: Concern, telemetry: dict, pad: str,
        banned: frozenset[str] = frozenset(), pads: tuple[str, ...] = ()
    ) -> Proposal:
        known = tuple(pads) or (pad,)
        fallback = by_rule(concern, telemetry, pad, banned)
        tier = LlmTier.SUPER if concern.urgency == "high" else LlmTier.NANO
        reply = self.llm.ask(tier, system_for(known),
                             self._brief(concern, telemetry, pad), max_tokens=160,
                             json_object=True)
        if reply is None:
            return fallback

        form = parse_json_object(reply.text)
        if not form or form.get("action") not in ALLOWED_ACTIONS:
            self.llm.discard(tier)
            return fallback  # 양식이 아니면 버립니다
        if form["action"] in banned:
            self.llm.discard(tier)
            return fallback  # 이미 금지된 걸 골랐으면 버립니다

        chosen_pad = form.get("pad") if form.get("action") == "reserve_pad" else None
        if chosen_pad is not None and chosen_pad not in known:
            chosen_pad = pad  # 없는 패드를 골랐습니다. 양식은 맞으니 가까운 것으로 되돌립니다
        rationale = str(form.get("rationale") or concern.detail)[:180]
        return _build(telemetry.get("id", "?"), form["action"], chosen_pad, rationale, reply.model)

    @staticmethod
    def _brief(concern: Concern, telemetry: dict, pad: str) -> str:
        return (
            f"vehicle={telemetry.get('id')} model={telemetry.get('model')} "
            f"state={telemetry.get('state')} battery={telemetry.get('battery')}% "
            f"vibration={telemetry.get('vibration')} "
            f"autonomy={telemetry.get('autonomy_health')} "
            f"passengers={telemetry.get('passengers')} cargo={telemetry.get('cargo')}\n"
            f"concern={concern.kind} ({concern.urgency}): {concern.detail}\n"
            f"nearest free-looking pad: {pad}"
        )
