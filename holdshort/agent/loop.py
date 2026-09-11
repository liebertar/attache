"""One process per vehicle. It files requests. That is the whole of it.

The only address this process knows is the runtime's. It cannot reach an actuator: there is
no actuator client in this package, none in its container image, and in compose it is not
even on the network the vehicles live on.

The other wiring — an agent holding the actuator address — lives in the `direct_agent`
package, which is built into a different image.
"""

import math
import os
import threading
import time
import urllib.parse
from concurrent.futures import Future
from concurrent.futures import TimeoutError as DraftTimeout
from dataclasses import dataclass

from holdshort.agent.chooser import (
    Choice,
    Outcome,
    RouteChooser,
    keep_clear_from_state,
    route_choice_param,
    situation_from_state,
)
from holdshort.agent.detect import detect
from holdshort.agent.drafter import ModelDrafter, service_bbox
from holdshort.agent.planner import OperatorPlanner
from holdshort.agent.propose import Proposer
from holdshort.agent.trace import choice_part, draft_part, form_part, model_trace, route_part
from holdshort.core.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, TRAFFIC_LATERAL_M
from holdshort.core.http import get_json, post_json
from holdshort.core.route import Router
from holdshort.llm.client import LlmTier, TieredLlm

FALLBACK_PADS = ["pad:launch"]
# 몇 초마다 런타임에 자기를 다시 알리나. 런타임은 AGENT_STALE_TICKS(기본 600틱, 0.2 s/틱에서 2분)
# 동안 소식이 없으면 목록에서 뺍니다 — 프로세스가 죽었는데 화면이 그 모델 이름을 계속 달면
# 거짓말입니다.
REGISTER_PERIOD_S = 30.0
# 등록이 받아들여지지 않았을 때(런타임이 없거나, 세계를 받기 전이라 503) 다시 알리기까지(초).
# 30초를 기다리면 시작하고 반 분 동안 화면의 기체에 모델 이름이 없습니다.
REGISTER_RETRY_S = 3.0
# 거절당한 뒤 다시 그리기까지. 화면이 거절을 보여주는 시간(ui/map-route.mjs stageLife('rejected'):
# 그리기 2.4 + 판정 0.6 + 붉게 1.6 + 흐려짐 1.0 = 5.6초)과 같습니다. 운영사가 거절 사유를
# 읽고 나서 다시 그리는 시간이고, 이게 있어야 거절 표시와 승인 표시가 실제 시간에서 겹치지
# 않아 시뮬레이터가 승인 하나만큼만(CLEARANCE_TICKS) 기다리면 됩니다.
REDRAW_DELAY_S = 5.6
# 교차 거절의 해결 사다리. 먼저 같은 길을 이만큼 높여서(상대 회랑은 수직 ±25m 라 30m 면 비켜 감),
# 안 되면 상대 회랑이 빌 때까지 출발을 미뤄서. 지연은 새 거절이 다른 틱을 말할 때마다 최대
# 이 횟수만 다시 냅니다 — 틱은 앞으로만 가므로 끝이 있고, 그 뒤는 A* 재작성 → 반려입니다.
ALTITUDE_SHIFT_M = 30.0
MAX_DELAY_TRIES = 3
ROUTE_REFUSALS = ("airspace", "traffic")
# 후보를 그리고 고르는 데 최대 이만큼(초)까지 기다립니다. 안전줄일 뿐입니다 — 계획기는 빈 기억에서
# 맨해튼 한 판이 40~50초, 그 뒤로는 대개 1초 안이고 모델은 10초 예산입니다. 여기에 걸리면 이번
# 차례는 접고 다음 차례에 처음부터 다시 냅니다.
CHOICE_WAIT_S = 300.0
# 고를 때 런타임 상태(공지·기상·다른 기체의 창)를 읽는 데 주는 시간. 없으면 (c) 후보 없이 고릅니다.
STATE_TIMEOUT_S = 3.0
# 공역 사본을 받는 데 주는 시간(초). /airspace 는 건물 3만 4천 동이라 크고, 네 기체가 한꺼번에
# 받습니다. 기본 5초로는 잘렸고, 잘린 것을 빈 사본으로 받아 계획기가 건물 없는 도시를 그렸습니다.
AIRSPACE_TIMEOUT_S = 60.0


@dataclass
class PendingDraft:
    """거절 순간에 작업 스레드로 보낸 초안 하나. 마감(monotonic)까지만 기다립니다."""

    future: Future
    drafter: ModelDrafter
    deadline: float

    @property
    def in_flight(self) -> bool:
        return not self.future.done()


class GuardedAgent:
    """신청서를 냅니다. 그게 전부입니다."""

    def __init__(self, asset_id: str, runtime_url: str, proposer: Proposer,
                 llm: TieredLlm | None = None):
        self.asset_id = asset_id
        self.runtime_url = runtime_url.rstrip("/")
        self.proposer = proposer
        self.llm = llm or proposer.llm
        self.pad_index = 0
        self.pads: dict = {}
        self.banned: set[str] = set()
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self.denial_s = float(os.getenv("DENIAL_COOLDOWN_S", "6"))
        self.redraw_s = float(os.getenv("REDRAW_DELAY_S", str(REDRAW_DELAY_S)))
        self.banned_retry_s = float(os.getenv("BANNED_RETRY_S", "20"))
        self.planner = OperatorPlanner()
        # 모델이 그리는 경로 초안. 모델이 없으면 None 이고 A* 만 씁니다. 어느 쪽이 그렸든
        # 판정은 런타임이 합니다 — 초안은 신청서에 legs 로 실릴 뿐입니다.
        self.service_bbox = None
        self.drafter = self._build_drafter()
        # 후보 중 하나를 고르는 이 기체의 모델. 신청서를 쓰는 것과 같은 모델(NANO)입니다 — 화면의
        # model_ok 는 신청서와 이 고르기를 누가 썼는지로 정합니다(ModelHealth).
        self.chooser = RouteChooser(self.llm)
        # 초안은 거절이 오는 순간 작업 스레드에서 시작합니다. 화면이 거절을 보여주는 5.6초를
        # 모델이 그리는 시간과 겹치려고요 — 예전에는 5.6초를 다 기다린 뒤에 물어서 그만큼 더
        # 섰습니다. 초안은 한 기체에 한 번에 하나만 걸립니다. 지난 초안이 아직 서버에 걸려
        # 있으면 이번 거절은 A* 로 갑니다(뒤에 줄을 세우지 않습니다). 스레드는 데몬입니다 —
        # ThreadPoolExecutor 의 작업 스레드는 인터프리터가 끝날 때 무조건 기다려서, Ctrl-C 가
        # 서버에 걸린 초안(최대 60초)이 돌아올 때까지 안 끝났습니다.
        self._draft: PendingDraft | None = None
        # 돌고 있는 후보 고르기(Future)와, 이번 차례의 신청서 흔적·초안 횟수.
        self._choice = None
        self._form: dict | None = None
        self._draft_attempts = 0
        # 모델에게 물어 그 답을 쓴 횟수와, 물었지만 규칙이 대신 쓴 횟수(신청서·경로 고르기만).
        # 등록(ModelHealth)이 이것으로 화면에 모델 이름을 달지 rules 를 달지 정합니다.
        self.model_answers = 0
        self.model_misses = 0
        self.airspace_revision = None
        # 우리 기체가 다니고 싶은 높이. 허용 천장이 더 낮으면 런타임이 거절하고,
        # 그때 계획기가 구간마다 낮춰서 다시 그립니다.
        self.preferred_alt_m = float(os.getenv("CRUISE_ALT_M", str(Router.cruise_alt_default())))

    def _count_form(self, trace: dict | None) -> None:
        """신청서 하나. 모델이 없어서 규칙이 쓴 것(no model)은 물은 적이 없으니 세지 않습니다."""
        if not trace or trace.get("fallback_reason") == "no model":
            return
        self._count(bool(trace.get("used")))

    def _count_choice(self, choice) -> None:
        """경로 고르기 하나. 모델에게 묻지 않고 규칙이 고른 것은 세지 않습니다."""
        if choice is not None and (choice.asked or choice.by_model):
            self._count(choice.by_model)

    def _count(self, used: bool) -> None:
        if used:
            self.model_answers += 1
        else:
            self.model_misses += 1

    def _build_drafter(self) -> ModelDrafter | None:
        drafter = ModelDrafter(self.llm, self.planner, bbox=self.service_bbox)
        return drafter if drafter.enabled else None

    @property
    def draft_in_flight(self) -> bool:
        return self._draft is not None and self._draft.in_flight

    def _open_pad(self) -> str:
        names = sorted(self.pads) or FALLBACK_PADS
        open_pads = [pad for pad in names if pad not in self.banned] or names
        return open_pads[self.pad_index % len(open_pads)]

    def telemetry(self) -> dict:
        return get_json(f"{self.runtime_url}/telemetry/{self.asset_id}") or {}

    def _destination(self, telemetry: dict, proposal) -> tuple | None:
        if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
            return (telemetry["job_lat"], telemetry["job_lon"])
        if proposal.action == "reserve_pad" and proposal.resource:
            pads = self.pads or {}
            at = pads.get(proposal.resource)
            return (at["lat"], at["lon"]) if at else None
        return None

    def _file_with_route(self, proposal, telemetry: dict, form: dict | None = None):
        """일단 최단 직선으로 냅니다. 규정에 안 맞으면 런타임이 어디가 문제인지 알려주고, 그때
        계획기가 규정 안의 후보를 셋까지 그리고 이 기체의 모델이 그중 하나를 고릅니다.
        고른 길도 런타임이 다시 판정합니다 — 승인은 우리가 하는 게 아닙니다."""
        self._form = form if form is not None else _rules_form(proposal)
        self._draft_attempts = 0
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = self._destination(telemetry, proposal)
        if here[0] is None or goal is None:
            return self._file(proposal.to_dict(), None)

        if float(telemetry.get("alt_m") or 0.0) > 1.0:
            # 나는 중에 경로를 잃었습니다(회수). 떠 있는 시간이 곧 잡음이라 직선 의식도 고르기도
            # 없이 우리 공역 사본으로 바로 우회로를 그려 냅니다 — A* 는 밀리초에 답합니다.
            legs = self.planner.draw(here, goal)
            if not legs:
                return self._nothing_legal(proposal, telemetry, here, None, None, None)
            return self._file_legs(proposal, legs, "astar", route_part("astar"), airborne=True)

        proposal.params = {**proposal.params, "legs": self.planner.straight(here, goal),
                           "drafter": "straight", "draft_attempts": 0}
        straight = route_part("straight")
        decision = self._file(proposal.to_dict(), straight)
        if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
            return decision
        if decision.get("policy_hit") == "traffic":
            # 다른 기체의 회랑과 겹칩니다. 길은 맞으니 높이나 시각을 바꿔 봅니다.
            decision = self._resolve_traffic(proposal, proposal.params["legs"], decision,
                                             airborne=False, route=straight)
            if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
                return decision
        # 다시 그리라고 했습니다. 계획기는 거절이 온 지금 바로 후보를 그리고 모델이 그중 하나를
        # 고릅니다(작업 스레드). 화면이 거절을 보여주는 5.6초가 그 시간을 덮습니다 — 기체는
        # 지상에서 일하는 중이라 그대로 있습니다.
        refused_at = time.monotonic()
        if decision.get("policy_hit") == "airspace":
            self.planner.note_refusal(decision.get("forbids"))
        pending = self._start_choice(here, goal, telemetry, decision)
        time.sleep(self.redraw_s)
        if pending is None:
            return decision      # 지난 고르기가 아직 돌고 있습니다. 이번 차례는 접습니다
        outcome = self._collect_choice(pending, refused_at)
        self._count_choice(outcome.choice if outcome else None)
        airborne_now, moved_to = self._position_now()
        if airborne_now:
            return decision      # 그새 떴습니다. 지상에서 그린 길은 뜻이 없어 이번 차례는 접습니다
        return self._file_candidates(proposal, telemetry, here, goal, moved_to, outcome, decision)

    def _file_candidates(self, proposal, telemetry: dict, here, goal, moved_to,
                         outcome: Outcome, refusal):
        """모델이 고른 것부터 냅니다. 거절되면 남은 후보를 차례로, 그것도 다 거절되면 초안을.

        후보는 전부 우리 사본의 판정을 통과한 길이지만, 런타임의 사본이 더 새것일 수 있고(방금
        닫힌 구역), 시각까지 보는 교차 판정은 여기서 못 합니다. 그래서 거절이 곧 '틀린 후보' 는
        아니고, 남은 것을 내보는 것이 맞습니다.
        """
        if moved_to is not None and _distance_m(here, moved_to) > TRAFFIC_LATERAL_M:
            # 기다리는 사이 멀리 움직였습니다. 옛 자리에서 그린 후보는 다른 길입니다.
            legs = self.planner.draw(moved_to, goal)
            if not legs:
                return self._nothing_legal(proposal, telemetry, here, None, None, refusal)
            return self._file_legs(proposal, legs, "astar", route_part("astar"), airborne=False)
        base = proposal.rationale
        decision = refusal
        for index, candidate in enumerate(outcome.ordered()):
            legs = (self._anchored(candidate["legs"], here, moved_to, goal) if moved_to
                    else candidate["legs"])
            if not legs:
                continue
            filed = (outcome.choice if index == 0 and outcome.choice is not None
                     else _next_best(candidate, decision))
            route = route_part("choice" if filed.by_model else "astar",
                               choice=choice_part(outcome.candidates, filed.chosen, filed.reason))
            proposal.rationale = f"{base} · 후보 {candidate['id']} ({candidate['label']})"
            decision = self._file_legs(
                proposal, legs, f"choice:{filed.model}" if filed.by_model else "astar", route,
                airborne=False,
                extra={"route_choice": route_choice_param(outcome.candidates, filed)})
            if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
                return decision
        proposal.rationale = base
        return self._after_candidates(proposal, telemetry, here, goal, moved_to, outcome, decision)

    def _after_candidates(self, proposal, telemetry: dict, here, goal, moved_to,
                          outcome: Outcome, decision):
        """후보가 하나도 안 통했습니다(또는 하나도 없습니다). 이제 모델 초안이 마지막 수단입니다."""
        chosen = outcome.choice
        choice = (choice_part(outcome.candidates, chosen.chosen, chosen.reason)
                  if chosen is not None else None)
        legs, draft, drew = self._last_resort_draft(moved_to or here, goal, decision)
        if legs:
            proposal.rationale = f"{proposal.rationale} · 모델 초안"
            return self._file_legs(proposal, legs, drew, route_part("draft", choice, draft),
                                   airborne=False)
        if outcome.candidates:
            # 규정 안의 길은 있었고 런타임이 전부 거절했습니다. 이번 차례는 여기서 접고 다음
            # 차례에 처음부터 다시 냅니다(그때는 상대가 지나갔거나 구역이 풀렸을 수 있습니다).
            return decision
        return self._nothing_legal(proposal, telemetry, here, choice, draft, decision)

    def _nothing_legal(self, proposal, telemetry: dict, here, choice, draft, decision):
        """우리 사본에는 규정을 지키면서 갈 수 있는 길이 없습니다."""
        if proposal.action != "fly_route" or self.planner.start_blocked(here, telemetry):
            # 이륙장에 갈 길이 없는 것과 주문을 못 받는 것은 다른 일입니다.
            # 예전에는 충전대가 막혔다고 배달을 반려하고 있었습니다.
            # 출발점이 막힌 것(닫힌 구역 안)도 주문의 문제가 아닙니다 — 나갈 때까지 기다립니다.
            return decision
        return self._file({**proposal.to_dict(), "action": "decline_job", "cost_usd": 0.0,
                           "blast_radius": "none", "params": {}, "resource": None,
                           "rationale": f"{proposal.rationale} · 규정상 경로 없음"},
                          route_part("astar", choice, draft))

    def _file_legs(self, proposal, legs: list[dict], drafter: str, route: dict,
                   airborne: bool, extra: dict | None = None):
        """경로 하나를 신청합니다. 누가 그렸는지는 신청서에 남고, 런타임은 그 값을 읽지 않습니다."""
        # 앞 신청의 route_choice 는 앞 신청 것입니다. 초안·A* 신청에 남으면 고르지 않은 길에
        # '고른 것' 이 붙습니다.
        kept = {key: value for key, value in proposal.params.items() if key != "route_choice"}
        proposal.params = {**kept, "legs": legs, "drafter": drafter,
                           "draft_attempts": self._draft_attempts, **(extra or {})}
        filed = {**proposal.to_dict(),
                 "rationale": f"{proposal.rationale} · 재작성 {len(legs)}구간"}
        decision = self._file(filed, route)
        if decision and decision.get("policy_hit") == "traffic":
            # 낸 길이 남의 회랑과 겹칩니다. 길은 맞으니 높이나 시각을 바꿔 봅니다.
            decision = self._resolve_traffic(proposal, legs, decision, airborne, route)
        return decision

    def _file(self, payload: dict, route: dict | None):
        """신청 하나를 냅니다. 모든 신청에 model_trace 가 실립니다 — 화면 카드가 읽는 라벨이고,
        런타임의 판정은 그것을 읽지 않습니다(판정이 보는 것은 legs 입니다)."""
        params = {**(payload.get("params") or {}),
                  "model_trace": model_trace(self._form, route)}
        return self._send({**payload, "params": params})

    def _send(self, payload: dict):
        """신청서 한 장을 런타임에 보냅니다. 전선은 여기 하나입니다 — 오프라인 하네스
        (tests/test_two_worlds.py)가 이것만 같은 프로세스 호출로 바꿔 끼우고 흐름은
        그대로 씁니다."""
        return post_json(f"{self.runtime_url}/proposals", payload)

    def _runtime_state(self) -> dict:
        """런타임 /state 한 번. 못 읽으면 빈 것 — 후보 (c) 와 모델이 읽을 맥락이 빠질 뿐입니다."""
        return get_json(f"{self.runtime_url}/state", timeout=STATE_TIMEOUT_S) or {}

    def _position_now(self) -> tuple[bool, tuple[float, float] | None]:
        """초안을 기다린 뒤의 기체 자리. (떠 있나, 지금 자리 또는 모르면 None)."""
        now = self.telemetry()
        if now.get("lat") is None or now.get("lon") is None:
            return False, None
        return float(now.get("alt_m") or 0.0) > 1.0, (float(now["lat"]), float(now["lon"]))

    def _anchored(self, legs: list[dict], here, moved_to, goal) -> list[dict] | None:
        """초안을 기다리는 20~60초 사이에 기체가 움직였으면 첫 점을 지금 자리로 옮깁니다.

        런타임은 첫 점이 기체 자리에서 TRAFFIC_LATERAL_M 보다 멀면 거절합니다(실주행: 이전 승인
        경로로 뜨는 동안 초안이 돌아와 자리 70m 옆의 옛 출발점으로 냈다가 거절). 그보다 멀리
        옮겨졌으면 옛 자리에서 그린 선은 다른 길이라 A* 로 다시 그립니다.
        """
        gap = _distance_m(here, moved_to)
        if gap < 1.0:
            return legs
        if gap > TRAFFIC_LATERAL_M:
            return self.planner.draw(moved_to, goal)
        return [{**legs[0], "lat": round(moved_to[0], 6), "lon": round(moved_to[1], 6)}] + legs[1:]

    def _resolve_traffic(self, proposal, legs: list[dict], refusal: dict, airborne: bool,
                         route: dict | None = None):
        """교차 거절의 해결 사다리. 고도 +30m → 출발 지연. 마지막 답을 돌려줍니다.

        길은 맞고 시각이 문제입니다. 먼저 같은 길을 30m 높여 냅니다(모든 구간이 천장 아래일 때만).
        그것도 겹치면 상대 회랑이 비는 틱(거절이 알려 준 blocked_until_tick)까지 출발을 미뤄
        냅니다. 조종장치가 그 틱까지 지상에서 준비된 채 기다리고, 화면에는 누구를 기다리는지 씁니다.
        떠 있는 기체는 미룰 수 없습니다(지상 대기가 아니라 공중 정지가 되므로). 고도만 시도합니다.
        """
        detail = refusal.get("detail") or {}
        other = detail.get("blocked_asset") or refusal.get("forbids")
        lifted = self.planner.lift(legs, ALTITUDE_SHIFT_M)
        decision = refusal
        if lifted is not None:
            decision = self._file({
                **proposal.to_dict(),
                "params": {**proposal.params, "legs": lifted, "resolution": "altitude",
                           "altitude_shift_m": ALTITUDE_SHIFT_M, "holding_for": None},
                "rationale": f"{proposal.rationale} · {other} 회랑 위로 +{ALTITUDE_SHIFT_M:.0f}m",
            }, route)
            if not decision or decision.get("policy_hit") != "traffic":
                return decision
            detail = decision.get("detail") or {}
            other = detail.get("blocked_asset") or other
        if airborne:
            return decision
        until = detail.get("blocked_until_tick")
        for _ in range(MAX_DELAY_TRIES):
            if until is None:
                break
            decision = self._file({
                **proposal.to_dict(),
                "params": {**proposal.params, "legs": legs, "resolution": "delay",
                           "holding_for": other, "depart_after_tick": int(until)},
                "rationale": f"{proposal.rationale} · {other} 지나간 뒤(틱 {int(until)}) 출발",
            }, route)
            if not decision or decision.get("policy_hit") != "traffic":
                return decision
            detail = decision.get("detail") or {}
            later = detail.get("blocked_until_tick")
            if later is None or int(later) <= int(until):
                break
            until, other = later, detail.get("blocked_asset") or other
        return decision

    def _start_choice(self, here, goal, telemetry: dict, refusal: dict):
        """거절이 온 순간, 작업 스레드에서 후보를 그리고 모델에게 고르게 합니다. 안 시키면 None.

        런타임 /state 를 한 번 읽어 (c) 후보가 비켜 갈 것(다른 기체의 승인 회랑·걸린 구역)과
        모델이 읽을 맥락(기상·공지·다른 기체의 창·남은 정차)을 만듭니다. /state 를 못 읽어도
        후보는 나옵니다 — (c) 가 빠지고 (a)(b) 중에서 고를 뿐입니다. 판정 자료가 아닙니다.
        """
        if self._choice is not None and not self._choice.done():
            return None
        planner, chooser, asset = self.planner, self.chooser, self.asset_id
        concern = (self._form or {}).get("concern", "")

        def work() -> Outcome:
            state = self._runtime_state()
            began = time.monotonic()
            candidates = planner.candidates(here, goal, keep_clear_from_state(state, asset))
            planned_ms = int((time.monotonic() - began) * 1000)
            if not candidates:
                return Outcome([], None, planned_ms)
            situation = situation_from_state(state, telemetry, refusal, concern, asset)
            return Outcome(candidates, chooser.choose(candidates, situation), planned_ms)

        self._choice = self._in_background(work)
        return self._choice

    def _collect_choice(self, pending, refused_at: float) -> Outcome:
        """후보와 고른 것을 거둡니다. 못 거두면 빈 것 — 그다음은 초안, 그다음은 반려입니다.

        계획기가 오래 걸릴 수 있습니다(빈 기억에서 맨해튼 한 판 40~50초). 그동안 기체는 지상에서
        기다립니다 — 오늘 A* 가 그랬던 것과 같고, 화면에는 '거절 뒤 다시 그리는 중' 으로 보입니다.
        """
        try:
            left = max(0.0, refused_at + CHOICE_WAIT_S - time.monotonic())
            outcome = pending.result(timeout=left)
            self._log_choice(outcome)
            return outcome
        except DraftTimeout:
            print(f"[{self.asset_id}] route choice did not finish in time", flush=True)
        except Exception as error:  # noqa: BLE001 - 고르기가 죽어도 다음 차례에 다시 냅니다
            print(f"[{self.asset_id}] route choice failed: {error!r}", flush=True)
        return Outcome()

    def _log_choice(self, outcome: Outcome) -> None:
        """후보와 고른 것 한 줄. 무엇 중에서 어떻게 골랐는지가 실주행 기록에 남아야 잽니다."""
        ids = ",".join(candidate["id"] for candidate in outcome.candidates) or "-"
        choice = outcome.choice
        how = "" if choice is None else (
            f" chose {choice.chosen} by {choice.path}"
            + (f" ({choice.fallback_reason})" if choice.fallback_reason else "")
            + (f" in {choice.latency_ms} ms" if choice.asked else ""))
        print(f"[choice:{self.asset_id}] candidates {ids} drawn in {outcome.planned_ms} ms"
              f"{how}", flush=True)

    def _last_resort_draft(self, here, goal, refusal: dict | None):
        """후보가 하나도 안 통했습니다. 그제서야 모델에게 새로 그려 보라고 합니다.

        예전에는 거절이 오는 순간 이것부터 시켰습니다. 지금은 후보 고르기가 그 자리를 쓰고,
        초안은 후보가 전부 거절된 뒤에만 갑니다 — 기체마다 모델 서버 슬롯이 하나뿐이라 두 질문을
        같이 걸면 고르기가 초안(최대 60초) 뒤에 줄을 서고, 그만큼 기체가 더 서 있습니다.
        돌려주는 것: (초안 legs 또는 None, 화면 카드에 실을 draft 기록 또는 None, 그린 이).
        """
        pending = self._start_draft(here, goal, refusal or {}, time.monotonic())
        if pending is None:
            return None, None, ""
        legs = None
        try:
            legs = pending.future.result(timeout=max(0.0, pending.deadline - time.monotonic()))
        except DraftTimeout:
            pass          # 예산 끝. 초안은 버립니다.
        except Exception as error:  # noqa: BLE001 - 초안이 죽어도 기체는 다음 차례에 다시 냅니다
            print(f"[{self.asset_id}] draft failed: {error!r}", flush=True)
        drafter = pending.drafter
        self._draft_attempts = drafter.last_attempts
        return legs, draft_part(drafter.last_attempts > 0, drafter.last_latency_ms,
                                drafter.last_breach, bool(legs)), drafter.name

    def _start_draft(self, here, goal, refusal: dict, refused_at: float) -> PendingDraft | None:
        """모델에게 초안 하나를 시킵니다(작업 스레드). 시키지 않으면 None.

        모델은 거절 사유를 읽고 초안을 냅니다. 초안은 양식·상자·고도·길이 검사와 우리 공역
        사본의 판정을 지나야 하고(전부 드래프터 안, 같은 스레드), 두 번 안 되면 None 입니다.
        어느 쪽이든 런타임이 다시 판정하므로, 모델이 엉뚱한 선을 그려도 실행되는 일은 없습니다.
        교차 거절 뒤에는 묻지 않습니다 — 모델은 다른 기체를 모르고, 고쳐야 할 것은 길이 아니라
        시각입니다(그건 사다리가 합니다).
        마감은 시작 시각 + 초안 예산 하나. 드래프터는 두 질문을 합쳐 그 안에서만 묻습니다.
        """
        drafter = self.drafter
        if drafter is None or refusal.get("policy_hit") == "traffic":
            return None
        if self.draft_in_flight:
            # 지난 거절의 초안이 아직 서버에 걸려 있습니다. 그 뒤에 또 세우면 둘 다 늦습니다.
            return None
        context = {"reason": refusal.get("reason"), "forbids": refusal.get("forbids")}
        deadline = refused_at + drafter.timeout_s
        future = self._in_background(drafter.draft, here, goal, context, deadline)
        self._draft = PendingDraft(future=future, drafter=drafter, deadline=deadline)
        return self._draft

    def _in_background(self, work, *args) -> Future:
        """데몬 스레드 하나에서 work(*args) 를 돌리고 Future 로 돌려줍니다."""
        future: Future = Future()

        def run() -> None:
            try:
                future.set_result(work(*args))
            except BaseException as error:  # noqa: BLE001 - 결과로 넘겨 본 스레드가 처리합니다
                future.set_exception(error)

        threading.Thread(target=run, daemon=True, name=f"draft-{self.asset_id}").start()
        return future

    def _refresh_airspace(self, revision) -> bool:
        """우리 공역 사본이 낡았습니다(구역이 닫히거나 풀림). 새로 받아 그 사본으로 그립니다.

        못 받으면(시간 초과·런타임 없음·빈 답) 지난 사본을 그대로 두고 판본도 적지 않습니다 —
        다음 차례에 다시 받습니다. 예전에는 못 받은 것을 빈 사본으로 바꿔 끼우고 판본까지 적어서,
        판본이 또 바뀔 때까지 계획기가 건물 없는 도시를 그렸습니다: 실주행에서 모닝사이드까지
        '직선 4구간 70 m' 후보가 나왔고, 런타임은 옥상 22 m 건물 위 48 m(이격 50 m 필요)로
        전부 거절했습니다.
        """
        world = get_json(f"{self.runtime_url}/airspace", timeout=AIRSPACE_TIMEOUT_S) or {}
        if not world.get("volumes"):
            print(f"[{self.asset_id}] airspace copy not refreshed (revision {revision}); "
                  "keeping the last one and retrying", flush=True)
            return False
        self.planner = OperatorPlanner()
        self.planner.load(world["volumes"])
        self.pads = world.get("pads", {})
        # 서비스 영역 = 착륙장·이륙장 모음의 경계 상자. 모델 초안이 이 밖으로 나가면 버립니다.
        corners = ([(a["lat"], a["lon"]) for a in world.get("landing_areas", [])]
                   + [(p["lat"], p["lon"]) for p in self.pads.values()])
        self.service_bbox = service_bbox(corners) or self.service_bbox
        self.drafter = self._build_drafter()
        self.airspace_revision = revision
        return True

    def step(self) -> None:
        telemetry = self.telemetry()
        if not telemetry:
            return
        if telemetry.get("airspace_revision") != self.airspace_revision:
            self._refresh_airspace(telemetry.get("airspace_revision"))
        concern = detect(telemetry)
        if concern is None:
            return
        proposal = self.proposer.write(
            concern, telemetry, self._open_pad(), frozenset(self.banned),
            tuple(sorted(self.pads) or FALLBACK_PADS),
        )
        self._count_form(self.proposer.last_trace)
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # 방금 거절당한 걸 계속 들이밀지 않습니다
        self.cooldown[proposal.action] = time.time() + self.repeat_s
        # 신청서를 누가 썼는지(모델·규칙과 그 이유)가 이 차례의 모든 신청에 실립니다.
        decision = self._file_with_route(proposal, telemetry, self.proposer.last_trace)
        if decision and decision.get("verdict") in ("denied", "human", "queued"):
            self.cooldown[proposal.action] = time.time() + self.denial_s
        route_refusal = bool(decision) and decision.get("policy_hit") in ROUTE_REFUSALS
        if decision and decision.get("verdict") == "denied" and decision.get("policy_hit") \
                and not route_refusal:
            # 지시(감항성 지시 등)로 막힌 행동입니다. 지시가 풀렸는지는 다시 내봐야 알지만,
            # 매번 내면 화면이 거절 표시로 도배됩니다. 가끔만 다시 냅니다.
            self.cooldown[proposal.action] = time.time() + self.banned_retry_s
        if decision and decision.get("verdict") == "denied":
            if decision.get("policy_hit"):
                # 강제점이 있으면 무엇이 금지됐는지 그 자리에서 알게 됩니다.
                # 자원이 막힌 것을 행동이 막힌 것으로 잘못 배우면 영영 신청을 못 합니다.
                # 길·시각이 막힌 것(공역·교차)은 행동이 막힌 게 아니라 여기서 배우지 않습니다.
                if not route_refusal:
                    self.banned.add(decision.get("forbids") or proposal.action)
            elif proposal.resource:
                self.pad_index += 1
        _report(self.asset_id, "guarded", proposal, decision)


def _rules_form(proposal) -> dict:
    """신청서 흔적 없이 불린 경우(시험·직접 호출). 규칙이 쓴 것으로 적습니다."""
    return form_part("", "", proposal.action, proposal.rationale, 0, False, "rules")


def _next_best(candidate: dict, refusal: dict | None) -> Choice:
    """고른 것이 거절된 뒤 다음 후보를 낼 때의 '고름'. 이건 모델이 아니라 규칙입니다."""
    why = (refusal or {}).get("policy_hit") or "refused"
    return Choice(candidate["id"], f"rules: the previous candidate was refused ({why})",
                  path="rules")


def _distance_m(a, b) -> float:
    return math.hypot((float(b[0]) - float(a[0])) * METRES_PER_DEG_LAT,
                      (float(b[1]) - float(a[1])) * METRES_PER_DEG_LON)


def _report(asset_id: str, mode: str, proposal, outcome) -> None:
    verdict = (outcome or {}).get("verdict") or ("ok" if (outcome or {}).get("ok") else "?")
    print(f"[{mode}:{asset_id}] {proposal.action} ${proposal.cost_usd:.0f} -> {verdict}",
          flush=True)


def build_llm() -> TieredLlm:
    # 기체 쪽은 6초만 기다립니다. 답이 없으면 규칙이 신청서를 쓰고 A* 가 길을 그립니다.
    # 거절 표시(5.6초)보다 오래 기다리면 화면에서 기체가 멈춘 것처럼 보입니다.
    return TieredLlm(models={
        "nano": os.getenv("MODEL_NANO", ""),
        "super": os.getenv("MODEL_SUPER", ""),
        "ultra": os.getenv("MODEL_ULTRA", ""),
    }, timeout_s=float(os.getenv("LLM_TIMEOUT_S") or "6"))


def identity(asset_id: str, llm: TieredLlm, model_ok: bool | None = None) -> dict:
    """런타임에 알리는 자기소개. 이 프로세스가 신청서를 무엇으로 쓰는지(모델·서버)뿐입니다.

    부를 수 없는 모델은 적지 않습니다 — 서버 주소가 없거나, 키 없는 Nebius 면 모든 호출이 실패하고
    신청서는 규칙이 씁니다. 그때 화면이 모델 이름을 달면 거짓말이라 model 을 비우고 host 는 off
    입니다. 설정은 멀쩡한데 답이 안 오는 것은 model_ok(ModelHealth)가 말합니다 — False 면
    런타임은 이름 대신 rules 라고 답니다.
    """
    model = llm.model_for(LlmTier.NANO) if llm.enabled else ""
    host = llm.host if llm.host in ("ollama", "nebius", "other") else "off"
    if host == "nebius" and not llm.api_key:
        model = ""
    port = None
    if model:
        parsed = urllib.parse.urlparse(llm.base_url)
        port = parsed.port or {"https": 443, "http": 80}.get(parsed.scheme)
    return {"asset_id": asset_id, "world": "guarded", "model": model,
            "host": host if model else "off", "base_url_port": port,
            "model_ok": model_ok if model else None}


class ModelHealth:
    """이 기체의 모델이 요즘 이 기체의 판단(신청서·경로 고르기)을 쓰고 있나. 등록할 때마다 한 번.

    지난 등록 뒤로 모델에게 물어 그 답을 쓴 적이 한 번이라도 있으면 True, 물었는데 전부 규칙이
    대신 썼으면(시간 초과·양식 아닌 답·없는 후보) False, 그 사이에 물은 적이 없으면 지난 판단
    그대로입니다. 처음에는 None(아직 모름) — 런타임은 설정된 모델 이름을 믿고 답니다.

    경로 초안은 세지 않습니다. 초안은 모든 후보가 거절된 뒤의 마지막 수단이라 잘 안 통하는 게
    정상입니다. 예전처럼 LLM 통계를 통째로 보면, 초안 실패만 쌓인 창에서 신청서는 전부 모델이
    썼는데도 화면이 rules 로 바뀌었습니다(2026-09-11 실측, drone-01·03).
    """

    def __init__(self, counts):
        self.counts = counts          # () -> (모델의 답을 쓴 횟수, 물었지만 규칙이 쓴 횟수)
        self.state: bool | None = None
        self._counted = (0, 0)

    def check(self) -> bool | None:
        answered, missed = self.counts()
        if answered > self._counted[0]:
            self.state = True
        elif missed > self._counted[1]:
            self.state = False
        self._counted = (answered, missed)
        return self.state


class Registration:
    """POST /agents/register. 받아들여지면 REGISTER_PERIOD_S 마다, 아니면 REGISTER_RETRY_S 뒤에.

    자기 스레드에서 돕니다(start). 신청(step)과 한 줄에 두면 런타임이 느릴 때 등록이 신청을
    3초씩 붙잡습니다 — 등록은 화면 라벨이고 신청이 먼저입니다. 알릴 내용은 보낼 때마다
    describe() 로 새로 만듭니다: 모델이 요즘 답하는지는 바뀝니다.
    """

    def __init__(self, runtime_url: str, describe, period_s: float = REGISTER_PERIOD_S,
                 retry_s: float = REGISTER_RETRY_S):
        self.url = f"{runtime_url.rstrip('/')}/agents/register"
        self.describe = describe
        self.period_s = period_s
        self.retry_s = retry_s
        self.payload: dict | None = None
        self.sent_at: float | None = None
        self.accepted = False
        self._stop = threading.Event()

    def maybe_send(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        wait = self.period_s if self.accepted else self.retry_s
        if self.sent_at is not None and now - self.sent_at < wait:
            return False
        self.send(now)
        return True

    def send(self, now: float | None = None) -> bool:
        self.sent_at = time.monotonic() if now is None else now
        self.payload = self.describe()
        # 짧게 기다립니다. 등록은 화면 라벨이지 판정이 아닙니다.
        answer = post_json(self.url, self.payload, timeout=3.0)
        self.accepted = bool(answer and answer.get("ok"))
        return self.accepted

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run, daemon=True, name="register")
        thread.start()
        return thread

    def run(self) -> None:
        while not self._stop.is_set():
            self.send()
            self._stop.wait(self.period_s if self.accepted else self.retry_s)

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    asset_id = os.environ["ASSET_ID"]
    period = float(os.getenv("AGENT_PERIOD_S", "0.6"))
    llm = build_llm()
    runtime_url = os.getenv("RUNTIME_URL", "http://runtime:8000")
    agent = GuardedAgent(asset_id, runtime_url, Proposer(llm))
    health = ModelHealth(lambda: (agent.model_answers, agent.model_misses))
    registration = Registration(runtime_url, lambda: identity(asset_id, llm, health.check()),
                                float(os.getenv("REGISTER_PERIOD_S") or REGISTER_PERIOD_S))
    print(f"agent {asset_id} up, files to runtime "
          f"(llm={'on' if llm.enabled else 'off'}, host={llm.host}, "
          f"model={identity(asset_id, llm)['model'] or 'rules'}, "
          f"chooser={'nano' if agent.chooser.enabled else 'rules'}, "
          f"drafter={'nano' if agent.drafter else 'astar'})", flush=True)
    registration.start()
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
