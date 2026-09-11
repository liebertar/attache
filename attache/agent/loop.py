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

from attache.agent.detect import detect
from attache.agent.drafter import ModelDrafter, service_bbox
from attache.agent.planner import OperatorPlanner
from attache.agent.propose import Proposer
from attache.core.geo import METRES_PER_DEG_LAT, METRES_PER_DEG_LON, TRAFFIC_LATERAL_M
from attache.core.http import get_json, post_json
from attache.core.route import Router
from attache.llm.client import LlmTier, TieredLlm

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
        # 초안은 거절이 오는 순간 작업 스레드에서 시작합니다. 화면이 거절을 보여주는 5.6초를
        # 모델이 그리는 시간과 겹치려고요 — 예전에는 5.6초를 다 기다린 뒤에 물어서 그만큼 더
        # 섰습니다. 초안은 한 기체에 한 번에 하나만 걸립니다. 지난 초안이 아직 서버에 걸려
        # 있으면 이번 거절은 A* 로 갑니다(뒤에 줄을 세우지 않습니다). 스레드는 데몬입니다 —
        # ThreadPoolExecutor 의 작업 스레드는 인터프리터가 끝날 때 무조건 기다려서, Ctrl-C 가
        # 서버에 걸린 초안(최대 60초)이 돌아올 때까지 안 끝났습니다.
        self._draft: PendingDraft | None = None
        self.airspace_revision = None
        # 우리 기체가 다니고 싶은 높이. 허용 천장이 더 낮으면 런타임이 거절하고,
        # 그때 계획기가 구간마다 낮춰서 다시 그립니다.
        self.preferred_alt_m = float(os.getenv("CRUISE_ALT_M", str(Router.cruise_alt_default())))

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

    def _file_with_route(self, proposal, telemetry: dict):
        """일단 최단 직선으로 냅니다. 규정에 안 맞으면 런타임이 어디가 문제인지
        알려주고, 그때 다시 그립니다. 승인은 우리가 하는 게 아닙니다."""
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = self._destination(telemetry, proposal)
        if here[0] is None or goal is None:
            return post_json(f"{self.runtime_url}/proposals", proposal.to_dict())

        airborne = float(telemetry.get("alt_m") or 0.0) > 1.0
        if airborne:
            # 나는 중에 경로를 잃었습니다(회수). 떠 있는 시간이 곧 잡음이라 직선 의식 없이
            # 우리 공역 사본으로 바로 우회로를 그려 냅니다. 모델에게 묻지도 않습니다 — 초 단위가
            # 아깝고, A* 는 밀리초에 답합니다.
            decision = None
            legs, drafter, attempts = self.planner.draw(here, goal), "astar", 0
        else:
            proposal.params = {**proposal.params, "legs": self.planner.straight(here, goal),
                               "drafter": "straight", "draft_attempts": 0}
            decision = post_json(f"{self.runtime_url}/proposals", proposal.to_dict())
            if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
                return decision
            if decision.get("policy_hit") == "traffic":
                # 다른 기체의 회랑과 겹칩니다. 길은 맞으니 높이나 시각을 바꿔 봅니다.
                decision = self._resolve_traffic(proposal, proposal.params["legs"], decision,
                                                 airborne=False)
                if not decision or decision.get("policy_hit") not in ROUTE_REFUSALS:
                    return decision
            # 다시 그리라고 했습니다. 모델은 거절이 온 지금 바로 그리기 시작하고, 화면이 거절을
            # 보여주는 동안(redraw_s) 우리는 기다립니다. 기체는 지상에서 일하는 중이라 그대로
            # 있습니다. 기다린 뒤 초안이 있으면 그걸, 아직이면 예산(초안 timeout_s, 거절 시각
            # 기준) 안에서만 더 기다리고, 그래도 없으면 A* 가 그립니다.
            refused_at = time.monotonic()
            if decision.get("policy_hit") == "airspace":
                self.planner.note_refusal(decision.get("forbids"))
            pending = self._start_draft(here, goal, decision, refused_at)
            time.sleep(self.redraw_s)
            legs, drafter, attempts = self._collect_draft(pending, here, goal)
            airborne_now, moved_to = self._position_now()
            if airborne_now:
                return decision   # 그새 떴습니다. 지상에서 그린 길은 뜻이 없어 이번 차례는 접습니다
            if legs and moved_to is not None:
                legs = self._anchored(legs, here, moved_to, goal)
        if not legs:
            if proposal.action != "fly_route" or self.planner.start_blocked(here, telemetry):
                # 이륙장에 갈 길이 없는 것과 주문을 못 받는 것은 다른 일입니다.
                # 예전에는 충전대가 막혔다고 배달을 반려하고 있었습니다.
                # 출발점이 막힌 것(닫힌 구역 안)도 주문의 문제가 아닙니다 — 나갈 때까지 기다립니다.
                return decision
            # 규정을 지키면서 갈 수 있는 길이 없습니다. 이 주문은 드론이 못 합니다.
            return post_json(f"{self.runtime_url}/proposals",
                             {**proposal.to_dict(), "action": "decline_job",
                              "cost_usd": 0.0, "blast_radius": "none", "params": {},
                              "resource": None,
                              "rationale": f"{proposal.rationale} · 규정상 경로 없음"})
        # 누가 그렸는지는 신청서에 남습니다. 런타임은 이 값을 읽지 않습니다(판정과 무관).
        proposal.params = {**proposal.params, "legs": legs,
                           "drafter": drafter, "draft_attempts": attempts}
        proposal.rationale += f" · 재작성 {len(legs)}구간"
        decision = post_json(f"{self.runtime_url}/proposals", proposal.to_dict())
        if decision and decision.get("policy_hit") == "traffic":
            # 다시 그린 길도 남의 회랑과 겹칩니다. 사다리를 한 번 더 — 그래도 안 되면 이번 차례는
            # 여기서 접고, 다음 차례에 처음부터 다시 냅니다(그때는 상대가 지나갔을 수 있습니다).
            decision = self._resolve_traffic(proposal, legs, decision, airborne=airborne)
        return decision

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

    def _resolve_traffic(self, proposal, legs: list[dict], refusal: dict, airborne: bool):
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
            decision = post_json(f"{self.runtime_url}/proposals", {
                **proposal.to_dict(),
                "params": {**proposal.params, "legs": lifted, "resolution": "altitude",
                           "altitude_shift_m": ALTITUDE_SHIFT_M, "holding_for": None},
                "rationale": f"{proposal.rationale} · {other} 회랑 위로 +{ALTITUDE_SHIFT_M:.0f}m",
            })
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
            decision = post_json(f"{self.runtime_url}/proposals", {
                **proposal.to_dict(),
                "params": {**proposal.params, "legs": legs, "resolution": "delay",
                           "holding_for": other, "depart_after_tick": int(until)},
                "rationale": f"{proposal.rationale} · {other} 지나간 뒤(틱 {int(until)}) 출발",
            })
            if not decision or decision.get("policy_hit") != "traffic":
                return decision
            detail = decision.get("detail") or {}
            later = detail.get("blocked_until_tick")
            if later is None or int(later) <= int(until):
                break
            until, other = later, detail.get("blocked_asset") or other
        return decision

    def _start_draft(self, here, goal, refusal: dict, refused_at: float) -> PendingDraft | None:
        """거절이 온 순간 모델에게 초안을 시킵니다(작업 스레드). 시키지 않으면 None.

        모델은 거절 사유를 읽고 초안을 냅니다. 초안은 양식·상자·고도·길이 검사와 우리 공역
        사본의 판정을 지나야 하고(전부 드래프터 안, 같은 스레드), 두 번 안 되면 None 입니다.
        어느 쪽이든 런타임이 다시 판정하므로, 모델이 엉뚱한 선을 그려도 실행되는 일은 없습니다.
        교차 거절 뒤에는 모델에게 묻지 않습니다 — 모델은 다른 기체를 모르고, A* 도 마찬가지지만
        A* 는 밀리초라 다른 길이라도 곧 내 볼 수 있습니다.
        마감은 거절 시각 + 초안 예산 하나. 드래프터는 두 질문을 합쳐 그 안에서만 묻습니다.
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

    def _collect_draft(self, pending: PendingDraft | None, here, goal
                       ) -> tuple[list[dict] | None, str, int]:
        """초안을 거둡니다. 없으면 A*. (legs, 누가 그렸나, 모델에게 물은 횟수).

        마감까지만 기다립니다. 그 뒤에 오는 답은 버립니다 — 기체는 이미 A* 길을 냈습니다.
        스레드는 자기 HTTP 타임아웃(같은 마감)으로 곧 끝나고, 끝나기 전에는 새 초안을
        받지 않습니다(_start_draft).
        """
        attempts = 0
        if pending is not None:
            legs = None
            try:
                legs = pending.future.result(timeout=max(0.0, pending.deadline - time.monotonic()))
            except DraftTimeout:
                pass          # 예산 끝. 초안은 버리고 A* 로 갑니다.
            except Exception as error:  # noqa: BLE001 - 초안이 죽어도 기체는 A* 로 냅니다
                print(f"[{self.asset_id}] draft failed: {error!r}", flush=True)
            attempts = pending.drafter.last_attempts
            if legs:
                return legs, pending.drafter.name, attempts
        return self.planner.draw(here, goal), "astar", attempts

    def step(self) -> None:
        telemetry = self.telemetry()
        if not telemetry:
            return
        if telemetry.get("airspace_revision") != self.airspace_revision:
            # 우리 공역 사본이 낡았습니다. 구역이 닫히거나 풀렸으니 새로 받아 그립니다.
            world = get_json(f"{self.runtime_url}/airspace") or {}
            self.planner = OperatorPlanner()
            self.planner.load(world.get("volumes", []))
            self.pads = world.get("pads", {})
            # 서비스 영역 = 착륙장·이륙장 모음의 경계 상자. 모델 초안이 이 밖으로 나가면 버립니다.
            corners = ([(a["lat"], a["lon"]) for a in world.get("landing_areas", [])]
                       + [(p["lat"], p["lon"]) for p in self.pads.values()])
            self.service_bbox = service_bbox(corners) or self.service_bbox
            self.drafter = self._build_drafter()
            self.airspace_revision = telemetry.get("airspace_revision")
        concern = detect(telemetry)
        if concern is None:
            return
        proposal = self.proposer.write(
            concern, telemetry, self._open_pad(), frozenset(self.banned),
            tuple(sorted(self.pads) or FALLBACK_PADS),
        )
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # 방금 거절당한 걸 계속 들이밀지 않습니다
        self.cooldown[proposal.action] = time.time() + self.repeat_s
        decision = self._file_with_route(proposal, telemetry)
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
    """신청서 모델이 요즘 답하나. 등록할 때마다 한 번, 지난 등록 뒤의 Nano 호출로 봅니다.

    답을 하나라도 받아 썼으면 True, 부른 것이 전부 규칙으로 넘어갔으면(시간 초과·연결 실패·양식
    아닌 답) False, 그 사이에 부른 적이 없으면 지난 판단 그대로입니다. 처음에는 None(아직 모름) —
    런타임은 설정된 모델 이름을 믿고 답니다.
    """

    def __init__(self, llm: TieredLlm):
        self.llm = llm
        self.state: bool | None = None
        self._counted = (0, 0)

    def check(self) -> bool | None:
        stats = self.llm.stats[LlmTier.NANO.value]
        answered, missed = stats.ok, stats.fallback
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
    health = ModelHealth(llm)
    registration = Registration(runtime_url, lambda: identity(asset_id, llm, health.check()),
                                float(os.getenv("REGISTER_PERIOD_S") or REGISTER_PERIOD_S))
    print(f"agent {asset_id} up, files to runtime "
          f"(llm={'on' if llm.enabled else 'off'}, host={llm.host}, "
          f"model={identity(asset_id, llm)['model'] or 'rules'}, "
          f"drafter={'nano' if agent.drafter else 'astar'})", flush=True)
    registration.start()
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
