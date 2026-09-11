"""Turns a concern into a request form.

Both worlds use this same file. The agents in the unguarded world are exactly as capable
and exactly as well behaved; what they lack is a place to put the rules.
"""

import time

from drone.agent.detect import Concern
from drone.agent.trace import concern_words, form_part
from shared.llm.client import LlmTier, TieredLlm, parse_json_object
from shared.models import Proposal

# 충전은 없습니다. 배터리 관리는 운영사 몫이고, 이 순환(적재 → 배달 → 수거 → 복귀)에 충전대가
# 없습니다.
# 목록에 두었더니 모델이 마당에 선 기체에 "charge" 를 써서 조종장치가 "패드 위가 아님" 으로 61번
# 거절했습니다.
ALLOWED_ACTIONS = {"decline_job", "fly_route", "reserve_pad", "disengage_autonomy", "depart"}
# 모델이 고를 수 있는 것은 걱정거리에 맞는 것뿐입니다. 배달 순환에서는 배달·이륙·포기 셋이고,
# 비상 착륙(reserve_pad)과 자율주행 해제는 고장 걱정이 있을 때만입니다. 4B 가 마당에 선 기체에
# '착륙대 예약' 을 일곱 번 적어 옆 자리 기체 위로 내리려다 전부 거절됐습니다. 경로
# 회수(divert_ground)
# 는 런타임이 쓰는 것이라 목록에 없습니다.
ROUTINE_ACTIONS = {"fly_route", "depart", "decline_job"}
FAULT_ACTIONS = {"motor_fault": {"reserve_pad"}, "needs_pad": {"reserve_pad"},
                 "autonomy_fault": {"disengage_autonomy"}}


def allowed_for(concern: Concern) -> set[str]:
    return ROUTINE_ACTIONS | FAULT_ACTIONS.get(concern.kind, set())

COSTS = {"decline_job": 0.0, "fly_route": 12.0, "reserve_pad": 28.0, "charge": 22.0,
         "fast_charge": 60.0, "divert_ground": 35.0, "disengage_autonomy": 0.0, "depart": 0.0}

BLAST = {"decline_job": "none", "fly_route": "schedule", "reserve_pad": "schedule",
         "charge": "none", "fast_charge": "none", "divert_ground": "cargo",
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
    else:
        action, chosen_pad = "reserve_pad", pad

    return _build(asset_id, action, chosen_pad, concern.detail, "rules")


def possible_now(action: str, telemetry: dict) -> bool:
    """이 행동을 기체가 지금 물리적으로 할 수 있나 — 시뮬레이터가 거절하는 것과 같은 기준입니다."""
    state = str(telemetry.get("state") or "")
    airborne = float(telemetry.get("alt_m") or 0.0) > 1.0
    if action in ("charge", "fast_charge"):
        return state in ("landed", "charging")
    if action == "depart":
        return not airborne
    return True


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
        # 마지막 신청서를 누가 썼나(trace.form_part). 기체 에이전트가 이것을 신청서의
        # model_trace 에 싣습니다.
        self.last_trace: dict | None = None

    def write(
        self, concern: Concern, telemetry: dict, pad: str,
        banned: frozenset[str] = frozenset(), pads: tuple[str, ...] = ()
    ) -> Proposal:
        known = tuple(pads) or (pad,)
        fallback = by_rule(concern, telemetry, pad, banned)
        tier = LlmTier.SUPER if concern.urgency == "high" else LlmTier.NANO
        words = concern_words(concern, telemetry)
        started = time.monotonic()
        reply = self.llm.ask(tier, system_for(known),
                             self._brief(concern, telemetry, pad), max_tokens=160,
                             json_object=True)
        if reply is None:
            waited = int((time.monotonic() - started) * 1000)
            self.last_trace = form_part("", words, fallback.action, fallback.rationale, waited,
                                        False, self._silence_reason(tier))
            return fallback

        form = parse_json_object(reply.text)
        problem = None
        if not form:
            problem = "not a form"
        elif form.get("action") not in allowed_for(concern):
            problem = "invalid action"  # 이 걱정거리에 맞지 않는 행동이면 버립니다
        elif form["action"] in banned:
            problem = "banned action"   # 이미 금지된 걸 골랐으면 버립니다
        elif not possible_now(form["action"], telemetry):
            # 지금 기체가 할 수 없는 일(패드 위가 아닌데 충전, 떠 있는데 이륙). 양식은 맞지만
            # 조종장치가 거절할 신청입니다 — 실주행에서 4B 가 마당의 기체에 '충전' 을 61번 적어
            # 전부 "not on a pad" 로 실패했고, 그동안 그 기체는 짐을 싣지 못했습니다.
            # 판정이 아니라 운영사의 상식입니다.
            problem = "impossible now"
        if problem is not None:
            self.llm.discard(tier)
            self.last_trace = form_part("", words, fallback.action, fallback.rationale,
                                        reply.latency_ms, False, problem)
            return fallback

        chosen_pad = form.get("pad") if form.get("action") == "reserve_pad" else None
        if chosen_pad is not None and chosen_pad not in known:
            chosen_pad = pad  # 없는 패드를 골랐습니다. 양식은 맞으니 가까운 것으로 되돌립니다
        rationale = str(form.get("rationale") or concern.detail)[:180]
        self.last_trace = form_part(reply.model, words, form["action"], rationale,
                                    reply.latency_ms, True, None)
        return _build(telemetry.get("id", "?"), form["action"], chosen_pad, rationale, reply.model)

    def _silence_reason(self, tier: LlmTier) -> str:
        """답이 없었던 이유. 모델이 없으면 no model, 서버에 못 닿았으면(시간 초과 포함) timeout."""
        if not (self.llm.enabled and self.llm.model_for(tier)):
            return "no model"
        return "timeout" if self.llm.unreachable_within(5.0) else "no reply"

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
