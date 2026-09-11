"""The planner draws the candidates; this aircraft's own model picks one; the runtime judges it.

Measured: asking a 4B model to write waypoint coordinates rarely clears the judge — a long
Manhattan crossing is a search problem, and A* is better at it in milliseconds. Choosing
between routes that are already legal is a different kind of question: which one reads best
given the weather, the closed cells, the other aircraft's windows and the stops still to make.
That is a judgement, and it is the sort of thing these cards are trained to answer by calling
a tool.

So the model never writes geometry here. It calls choose_route(id, reason) with one of the
ids the planner handed it. Nothing about the guarantee changes: the chosen legs go to the
runtime exactly like A*'s, and the runtime refuses them for exactly the same reasons. If the
server has no tools, we ask again in JSON. If there is no model, no answer, or an answer that
names a route that does not exist, the rules choose and we count it.
"""

import os
import time
from dataclasses import dataclass, field

from shared.llm.client import LlmTier, TieredLlm, parse_json_object

CHOICE_TOOL = "choose_route"
# 이유는 한 문장입니다. 화면 카드 한 줄과 원장에 그대로 실리므로 길면 자릅니다.
REASON_CHARS = 160
# 고르기 한 번(도구 + 필요하면 JSON 재질문)의 예산(초). 거절 표시 5.6초 뒤에 거두므로 그 안에
# 끝나면 화면에서는 공짜입니다. 실측(Ollama nemotron-3-nano:4b, 한가한 서버): 1.1~2.6초.
CHOICE_TIMEOUT_S = 10.0
# 남은 예산이 이보다 적으면 묻지 않습니다. 첫 토큰까지가 이만큼입니다.
MIN_ASK_S = 1.0
# 도구를 줬는데 글로만 답하는 서버: 이만큼 연달아 그러면 그 서버에는 JSON 으로만 묻습니다.
# (한 번은 모델이 실수한 것일 수 있지만, 매번 두 번 묻는 것은 기체가 그만큼 더 서 있는 것입니다.)
PLAIN_TEXT_LIMIT = 2

# 시스템 문구는 짧게 둡니다. 실측(Ollama nemotron-3-nano:4b, 실주행 브리프 820 토큰): 선호까지
# 적은 긴 시스템 문구에서는 모델이 쓴 도구 호출(35 토큰)을 Ollama 가 못 읽고 빈 답(200, 내용도
# 호출도 없음)으로 돌려줬습니다 — 실주행 2/2, 재현 3/3. 짧은 문구로는 6/6 이 도구 호출로 왔습니다.
# 채팅 틀이 도구 설명을 시스템 자리에 넣으므로 긴 문구가 그 형식 지시를 밀어낸 것으로 봅니다.
# 선호는 브리프 끝에 둡니다.
SYSTEM_TOOLS = (
    "You pick one of the candidate routes for one uncrewed delivery drone by calling "
    f"{CHOICE_TOOL}. You cannot draw, change or approve routes; a runtime judges the one you pick."
)
SYSTEM_JSON = (
    "You pick one of the candidate routes for one uncrewed delivery drone. You cannot draw, "
    "change or approve routes; a runtime judges the one you pick. Reply with one JSON object "
    'and nothing else: {"id": "<one of the ids>", "reason": "one short sentence"}.'
)
PREFERENCE = ("Prefer a route that keeps clear of another aircraft's cleared corridor or an "
              "incident when one is near; otherwise a lower cruise if it costs little; otherwise "
              "the shortest.")
CLOSING_TOOLS = f"Call {CHOICE_TOOL} with one id and one short sentence why."
CLOSING_JSON = "Answer with one id and one short sentence why."


def choice_tool(ids: list[str]) -> dict:
    """모델이 부를 수 있는 도구 하나. 이 도구는 아무것도 실행하지 않습니다 —
    고른 것을 말할 뿐입니다."""
    return {
        "type": "function",
        "function": {
            "name": CHOICE_TOOL,
            "description": ("File one of the candidate routes for the runtime to judge. "
                            "Call this exactly once."),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": list(ids),
                           "description": "the id of the candidate you pick"},
                    "reason": {"type": "string",
                               "description": "one short sentence, why this one"},
                },
                "required": ["id", "reason"],
            },
        },
    }


@dataclass
class Choice:
    """고른 것 하나. path 는 어떻게 골랐나: tools | json | rules."""

    chosen: str
    reason: str
    model: str = ""
    path: str = "rules"
    latency_ms: int = 0
    asked: bool = False
    fallback_reason: str | None = None

    @property
    def by_model(self) -> bool:
        return self.path in ("tools", "json")


@dataclass
class Outcome:
    """한 번의 '다시 그리기' 결과: 후보들과 고른 것, 그리고 계획기가 쓴 시간."""

    candidates: list[dict] = field(default_factory=list)
    choice: Choice | None = None
    planned_ms: int = 0

    def ordered(self) -> list[dict]:
        """고른 것부터, 그다음은 규칙 순서. 앞의 것이 거절되면 다음 것을 냅니다."""
        by_id = {candidate["id"]: candidate for candidate in self.candidates}
        chosen = self.choice.chosen if self.choice else ""
        first = [by_id.pop(chosen)] if chosen in by_id else []
        return first + list(by_id.values())


def rule_choice(candidates: list[dict], situation: dict | None = None) -> Choice:
    """모델 없이 고릅니다: 교차가 걸린 판이면 (c), 아니면 (a).

    이 규칙이 없으면 모델이 없을 때 경로를 아예 못 냅니다. 모델이 답하지 않는 날에도 기단은
    날아야 하고, 그때 골라야 할 것은 정해져 있습니다 — 남의 회랑이 걸린 상황이면 떨어진 길.
    """
    situation = situation or {}
    by_id = {candidate["id"]: candidate for candidate in candidates}
    shortest = by_id.get("a") or (candidates[0] if candidates else None)
    if shortest is None:
        return Choice("", "", path="rules", fallback_reason="no candidates")
    crowded = bool(situation.get("traffic_refusal")) or any(
        tag.startswith("near-traffic:") for tag in shortest.get("reason_tags") or [])
    clear = by_id.get("c")
    if crowded and clear is not None:
        return Choice(clear["id"], "rules: traffic is in the way, this one keeps clear",
                      path="rules")
    return Choice(shortest["id"], "rules: the shortest legal route", path="rules")


class RouteChooser:
    def __init__(self, llm: TieredLlm, tier: LlmTier = LlmTier.NANO,
                 timeout_s: float | None = None):
        self.llm = llm
        self.tier = tier
        self.timeout_s = (float(os.getenv("CHOICE_TIMEOUT_S", str(CHOICE_TIMEOUT_S)))
                          if timeout_s is None else float(timeout_s))
        # 어떻게 골랐나의 셈. 실측 표(보고서)와 시험이 읽습니다.
        self.counts = {"tools": 0, "json": 0, "rules": 0, "invalid": 0, "plain_text": 0,
                       "empty": 0, "unsupported": 0, "timeout": 0, "differs_from_rule": 0}
        self._plain_streak = 0

    @property
    def enabled(self) -> bool:
        return bool(self.llm.enabled and self.llm.model_for(self.tier))

    @property
    def model(self) -> str:
        return self.llm.model_for(self.tier)

    def choose(self, candidates: list[dict], situation: dict | None = None,
               deadline: float | None = None) -> Choice:
        """후보 중 하나를 고릅니다. 답이 없거나 없는 id 를 말하면 규칙이 고르고 그걸 셉니다."""
        rules = rule_choice(candidates, situation)
        if len(candidates) < 2:
            return self._by_rules(rules, "one candidate" if candidates else "no candidates")
        if not self.enabled:
            return self._by_rules(rules, "no model")
        if deadline is None:
            deadline = time.monotonic() + self.timeout_s
        brief = choice_brief(candidates, situation)
        started = time.monotonic()
        if self.llm.tools_ok and self._plain_streak < PLAIN_TEXT_LIMIT:
            choice, why = self._by_tools(brief, candidates, deadline)
            if choice is not None:
                return self._counted(choice, rules, started)
            if why != "json":
                return self._by_rules(rules, why)
        choice, why = self._by_json(brief, candidates, deadline)
        if choice is not None:
            return self._counted(choice, rules, started)
        return self._by_rules(rules, why)

    # ---------- 묻기 ----------

    def _by_tools(self, brief: str, candidates: list[dict],
                  deadline: float) -> tuple[Choice | None, str]:
        """도구 호출로. 돌려주는 이유가 "json" 이면 JSON 양식으로 다시 물을 차례입니다."""
        budget = deadline - time.monotonic()
        if budget < MIN_ASK_S:
            return None, "no time"
        ids = [candidate["id"] for candidate in candidates]
        reply = self.llm.ask_tools(self.tier, SYSTEM_TOOLS, brief, [choice_tool(ids)],
                                   max_tokens=200, timeout_s=budget)
        if reply is None:
            if not self.llm.tools_ok:
                self.counts["unsupported"] += 1
                return None, "json"     # 서버가 도구를 모릅니다. 같은 질문을 JSON 으로.
            if self.llm.unreachable_within(1.0):
                self.counts["timeout"] += 1
                return None, "timeout"  # 서버에 닿지 못했습니다. 또 물어도 또 기다릴 뿐입니다.
            # 서버는 답했는데 빈 답입니다(200, 글도 도구 호출도 없음). 실측: Ollama 가 4B 의
            # 도구 호출을 못 읽으면 이렇게 삼킵니다. 서버가 없는 것이 아니라 JSON 으로 한 번 더
            # 묻습니다.
            self.counts["empty"] += 1
            self._plain_streak += 1
            return None, "json"
        if not reply.tool_calls:
            # 도구를 줬는데 글로 답했습니다. 버리고 JSON 으로 다시 묻습니다.
            self.llm.discard(self.tier)
            self.counts["plain_text"] += 1
            self._plain_streak += 1
            return None, "json"
        call = next((c for c in reply.tool_calls if c["name"] == CHOICE_TOOL), None)
        if call is None:
            # 없는 도구를 불렀습니다. 글이 아니라 틀린 답이라 다시 묻지 않고 규칙이 고릅니다.
            self.llm.discard(self.tier)
            self.counts["invalid"] += 1
            return None, "invalid tool"
        self._plain_streak = 0
        choice = self._validated(call["arguments"], candidates, reply, "tools")
        if choice is None:
            self.llm.discard(self.tier)
            self.counts["invalid"] += 1
            return None, "invalid id"
        return choice, ""

    def _by_json(self, brief: str, candidates: list[dict],
                 deadline: float) -> tuple[Choice | None, str]:
        budget = deadline - time.monotonic()
        if budget < MIN_ASK_S:
            return None, "no time"
        brief = brief.replace(CLOSING_TOOLS, CLOSING_JSON)
        reply = self.llm.ask(self.tier, SYSTEM_JSON, brief, max_tokens=160, json_object=True,
                             timeout_s=budget)
        if reply is None:
            self.counts["timeout"] += 1
            return None, "timeout"
        choice = self._validated(parse_json_object(reply.text), candidates, reply, "json")
        if choice is None:
            self.llm.discard(self.tier)
            self.counts["invalid"] += 1
            return None, "invalid id"
        return choice, ""

    def _validated(self, answer, candidates: list[dict], reply, path: str) -> Choice | None:
        """양식 검사. id 는 우리가 준 것 중 하나여야 하고, 이유는 한 문장으로 자릅니다."""
        if not isinstance(answer, dict):
            return None
        # 모델이 "(a)" 나 "a." 로 적기도 합니다. 글자·숫자만 남겨 우리가 준 id 와 맞춥니다 —
        # 느슨하게 읽는 것은 같은 id 를 알아보는 데까지이고, 없는 id 는 그대로 버립니다.
        chosen = "".join(ch for ch in str(answer.get("id") or "").lower() if ch.isalnum())
        if chosen not in {candidate["id"] for candidate in candidates}:
            return None
        reason = " ".join(str(answer.get("reason") or "").split())[:REASON_CHARS]
        return Choice(chosen, reason, model=reply.model, path=path,
                      latency_ms=reply.latency_ms, asked=True)

    # ---------- 셈 ----------

    def _counted(self, choice: Choice, rules: Choice, started: float) -> Choice:
        self.counts[choice.path] += 1
        if choice.chosen != rules.chosen:
            self.counts["differs_from_rule"] += 1
        choice.latency_ms = int((time.monotonic() - started) * 1000)
        return choice

    def _by_rules(self, rules: Choice, why: str) -> Choice:
        self.counts["rules"] += 1
        rules.fallback_reason = why
        return rules


# ---------- 모델이 읽을 것 ----------


def choice_brief(candidates: list[dict], situation: dict | None = None) -> str:
    """고르는 데 필요한 것만 한 화면에: 이 기체, 왜 다시 그리는지, 지금 걸린 것들, 후보 표."""
    situation = situation or {}
    lines = [_aircraft_line(situation)]
    if situation.get("concern"):
        lines.append(f"Task: {situation['concern']}.")
    if situation.get("refusal"):
        lines.append(f"The runtime refused the straight line — {situation['refusal']}.")
    lines.append(_now_line(situation))
    if situation.get("notices"):
        lines.append("In force: " + "; ".join(situation["notices"][:4]) + ".")
    if situation.get("traffic"):
        lines.append("Other aircraft already cleared: " + "; ".join(situation["traffic"][:4]) + ".")
    lines.append("Candidates (each one already passes the operator's airspace check; "
                 "the runtime judges the one you pick):")
    lines += [_candidate_line(candidate) for candidate in candidates]
    lines += [PREFERENCE, CLOSING_TOOLS]
    return "\n".join(lines)


def _aircraft_line(situation: dict) -> str:
    parts = [f"Aircraft {situation.get('asset') or '?'}"]
    if situation.get("model"):
        parts.append(f"({situation['model']})")
    where = "on the ground" if situation.get("airborne") is False else "airborne"
    parts.append(where)
    if situation.get("battery") is not None:
        parts.append(f"battery {float(situation['battery']):.0f}%")
    if situation.get("stops_left") is not None:
        parts.append(f"{int(situation['stops_left'])} stop(s) left this trip")
    return ", ".join([" ".join(parts[:3])] + parts[3:]) + "."


def _now_line(situation: dict) -> str:
    weather = situation.get("weather") or "no weather hold"
    return f"Now: tick {situation.get('tick', '?')}. Weather: {weather}."


def _candidate_line(candidate: dict) -> str:
    tags = ", ".join(candidate.get("reason_tags") or []) or "-"
    return (f"{candidate['id']}) {candidate.get('label', '')} — "
            f"{float(candidate.get('length_m') or 0) / 1000:.1f} km, "
            f"{len(candidate.get('legs') or [])} legs, cruise "
            f"{candidate.get('min_alt_m')}-{candidate.get('max_alt_m')} m; {tags}")


def route_choice_param(candidates: list[dict], choice: Choice) -> dict:
    """params.route_choice — 무엇 중에 무엇을 왜 골랐나. 화면과 원장이 읽고,
    런타임은 읽지 않습니다."""
    return {
        "candidates": [{"id": c["id"], "label": c["label"], "legs_count": len(c["legs"]),
                        "length_m": c["length_m"], "max_alt_m": c["max_alt_m"],
                        "min_alt_m": c["min_alt_m"], "reason_tags": list(c["reason_tags"])}
                       for c in candidates],
        "chosen": choice.chosen, "reason": choice.reason, "model": choice.model,
        "path": choice.path,
    }


def situation_from_state(state: dict, telemetry: dict, refusal: dict | None, concern: str,
                         asset_id: str) -> dict:
    """런타임 /state 와 우리 텔레메트리를 모델이 읽을 몇 줄로. 판정 자료가 아니라 맥락입니다."""
    state = state or {}
    hold = (state.get("weather") or {}).get("hold") or {}
    refused = refusal or {}
    return {
        "asset": asset_id,
        "model": telemetry.get("model"),
        "airborne": float(telemetry.get("alt_m") or 0.0) > 1.0,
        "battery": telemetry.get("battery"),
        "stops_left": telemetry.get("stops_left"),
        "concern": concern,
        "tick": state.get("tick", telemetry.get("tick")),
        "weather": (f"hold until tick {hold.get('until_tick')} ({hold.get('reason')})"
                    if hold else None),
        "notices": _notice_words(state),
        "traffic": _traffic_words(state, asset_id),
        "traffic_refusal": refused.get("policy_hit") == "traffic",
        "refusal": _refusal_words(refused),
    }


def _refusal_words(refusal: dict) -> str:
    hit = refusal.get("policy_hit")
    if not hit:
        return ""
    if hit == "traffic":
        detail = refusal.get("detail") or {}
        until = detail.get("blocked_until_tick")
        return (f"traffic: it overlaps {detail.get('blocked_asset') or 'another aircraft'}"
                + (f"'s corridor until tick {until}" if until is not None else "'s corridor"))
    return f"{hit}: {refusal.get('forbids') or 'a rule'} is in the way"


def _notice_words(state: dict) -> list[str]:
    """걸려 있는(applied) 공지만. 사람이 확인하기 전 것은 아무것도 안 막으므로 쓰지 않습니다."""
    words = []
    for notice in state.get("notices") or []:
        if not notice.get("applied"):
            continue
        until = notice.get("until_tick")
        words.append(f"{notice.get('name') or notice.get('id')}"
                     + (f" (until tick {until})" if until is not None else ""))
    return words


def _traffic_words(state: dict, asset_id: str) -> list[str]:
    words = []
    for intent in state.get("intents") or []:
        if intent.get("asset") == asset_id or intent.get("state") not in ("accepted", "activated"):
            continue
        flying = "flying" if intent.get("state") == "activated" else "waiting to depart"
        words.append(f"{intent.get('asset')} ticks {intent.get('from_tick')}-"
                     f"{intent.get('to_tick')} ({flying})")
    return words


def keep_clear_from_state(state: dict, asset_id: str) -> dict:
    """후보 (c) 가 비켜 갈 것들: 다른 기체의 승인 경로와, 걸려 있는 구역·사고 원.

    회랑의 좌표는 /state 에 따로 없어서 원장 꼬리(승인된 신청의 legs)에서 그 의도의 신청서를
    찾아 씁니다. 못 찾으면 그 기체는 빠집니다 — (c) 는 선택지일 뿐이고, 교차 판정은 어차피
    런타임이 4D 의도로 합니다. 여기서 무엇이 빠져도 규정이 느슨해지지 않습니다.
    """
    state = state or {}
    legs_by_proposal = {}
    for entry in state.get("ledger") or []:
        proposal = entry.get("proposal") or {}
        legs = (proposal.get("params") or {}).get("legs")
        if proposal.get("id") and legs:
            legs_by_proposal[proposal["id"]] = legs
    traffic = []
    for intent in state.get("intents") or []:
        if intent.get("asset") == asset_id or intent.get("state") not in ("accepted", "activated"):
            continue
        legs = legs_by_proposal.get(intent.get("proposal_id"))
        if legs:
            traffic.append({"id": intent["asset"], "legs": legs})
    keepouts = [{"id": notice.get("id"), "polygon": notice.get("polygon")}
                for notice in state.get("notices") or []
                if notice.get("applied") and notice.get("polygon")]
    keepouts += [{"id": incident.get("id"), "lat": (incident.get("centre") or [None, None])[0],
                  "lon": (incident.get("centre") or [None, None])[1],
                  "radius_m": incident.get("radius_m")}
                 for incident in state.get("incidents") or []
                 if incident.get("applied") and incident.get("centre")]
    return {"traffic": traffic, "keepouts": keepouts}
