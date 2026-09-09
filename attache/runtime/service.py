"""The runtime process. Holds locks, limits, the arbiter, the single commit path, the ledger."""

import math
import os
import threading
import time

from attache.adapters import build as build_adapter
from attache.core import config as config_module
from attache.core.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    Airspace,
    Volume,
    first_breach,
    nearest_exit,
    vertical_column,
)
from attache.core.http import JsonServer, get_json
from attache.core.models import Decision, Proposal, Verdict
from attache.core.notam import Clock
from attache.core.route import Router
from attache.llm.client import TieredLlm
from attache.runtime.arbiter import Arbiter
from attache.runtime.authority import AuthorityCheck
from attache.runtime.commit import Committer
from attache.runtime.intents import (
    ACCEPTED,
    PRESENCE,
    Intent,
    IntentRegistry,
    first_conflict,
    ground_conflict,
    hold,
    landing_conflict,
    schedule,
)
from attache.runtime.ledger import Ledger
from attache.runtime.locks import LockTable
from attache.runtime.notices import NoticeBook
from attache.runtime.policy import PolicyBook

# 한 구간의 최대 길이. 서비스 반경이 11km 라 그 안의 어떤 경로도 이보다 긴 구간은 없습니다.
# 유한하기만 한 좌표로 지구 반 바퀴짜리 구간을 내면 판정이 색인 격자 1e10 칸을 돌며 영영 안
# 끝났고, 그동안 런타임 스레드가 GIL 을 쥐어 세계·중재가 멈췄습니다. 판정 이전의 양식 문제입니다.
MAX_LEG_M = 50_000.0
# 경로를 실어 오는 행동. 이것만 공역·의도 판정을 받습니다.
ROUTED = ("reserve_pad", "fly_route")
# 서비스 영역 상자의 여유(약 2km). 모델이 구조화한 공지가 이 밖이면 지어낸 것입니다.
SERVICE_MARGIN_DEG = 0.02


class Runtime:
    def __init__(self, config_path: str, sim_url: str, ledger_path: str, window_s: float = 1.5):
        self.config = config_module.load(config_path)
        self.policies = PolicyBook(self.config.policies)
        self.authority = AuthorityCheck(self.config.authority, self.policies)
        self.locks = LockTable(self.config.resources)
        self.ledger = Ledger(ledger_path)
        self.llm = TieredLlm(models=vars(self.config.escalation))
        self.arbiter = Arbiter(self.llm)
        self.adapter = build_adapter(
            os.getenv("ADAPTER", "sim"), sim_url=sim_url, world="guarded"
        )
        self.committer = Committer(self.adapter, self.locks, self.ledger, self.authority)
        self.committer.on_committed = self._on_committed

        self.sim_url = sim_url
        self.pad_coords: dict[str, tuple[float, float]] = {}
        self.landing_areas: list[dict] = []   # 운영사에게 그대로 넘겨주는 배달 착륙장 목록
        self.window_s = window_s
        self.tick = 0
        self.telemetry: dict = {}
        self.airspace = Airspace()
        self.router = Router(self.airspace)
        self.zone_volumes: set[str] = set()   # 공지로 들어온 구역. 끝나면 빼야 합니다
        # 신고 성능과 판의 시계. 승인한 경로가 언제 어디에 있을지(의도)와 NOTAM 의 시간 창을
        # 여기서 셉니다.
        self.performance = self.config.performance
        self.clock = Clock(self.performance.clock_epoch_z, self.performance.seconds_per_tick)
        self.intents = IntentRegistry()
        self.notices = NoticeBook(self.clock, self.llm)
        self._round = None                    # 시뮬레이터가 판을 새로 시작하면 따라갑니다
        self._contended: dict[str, list[tuple[Proposal, Decision, float]]] = {}
        self._awaiting_human: dict[str, Proposal] = {}
        self._decisions: dict[str, Decision] = {}
        self._checks: dict[str, list[str]] = {}   # 신청서 id → 지금까지 돈 검사 이름
        # 벽시계가 아니라 세계의 시계로 셉니다. 그래야 재현이 됩니다.
        self._recent_commits: dict[tuple[str, str], int] = {}
        self.dedupe_ticks = int(os.getenv("DEDUPE_TICKS", "15"))
        self._guard = threading.Lock()

    # ---------- 신청 접수 ----------

    def file(self, raw: dict) -> Decision:
        proposal = Proposal.from_dict({**raw, "world": "guarded"})
        asset = self.telemetry.get(proposal.asset_id, {})
        # 이 접수의 검사 목록. 운영사가 같은 id 로 다시 내면(직선 → 재작성) 새로 셉니다 — 원장
        # 한 줄은 한 번의 접수를 말해야 합니다. 나중의 재판정(rejudge)은 이 목록 뒤에 덧붙습니다.
        checks = self._checks[proposal.id] = []
        self._observe()

        checks.append("dedupe")
        seen_at = self._recent_commits.get((proposal.asset_id, proposal.action))
        if seen_at is not None and self.tick - seen_at < self.dedupe_ticks:
            # 같은 신청이 연달아 오면 한 번만 나갑니다. 아니면 중복 청구가 됩니다.
            # 이것도 판정이라 원장에 남습니다 — 안 남기면 "왜 그 신청은 답이 없었나" 를 못 답합니다.
            decision = Decision(proposal.id, Verdict.DENIED, "직전에 같은 신청이 실행됐습니다",
                                code="duplicate")
            return self._deny(proposal, decision)

        blocked = self.check_route(proposal, checks)
        if blocked:
            return self._deny(proposal, self._airspace_denial(proposal, blocked))
        blocked = self._check_traffic(proposal, checks)
        if blocked:
            return self._deny(proposal, self._traffic_denial(proposal, blocked))

        checks.append("authority")
        decision = self.authority.evaluate(proposal, asset, self.tick)
        self._decisions[proposal.id] = decision

        if decision.verdict is Verdict.DENIED:
            return self._deny(proposal, decision)
        if decision.verdict is Verdict.HUMAN:
            with self._guard:
                existing = next(
                    (
                        waiting
                        for waiting in self._awaiting_human.values()
                        if waiting.asset_id == proposal.asset_id
                        and waiting.action == proposal.action
                    ),
                    None,
                )
                if existing is not None:
                    # 승인 화면에 같은 카드를 쌓지 않습니다
                    return self._decisions[existing.id]
                self._awaiting_human[proposal.id] = proposal
            return decision
        return self._queue_or_commit(proposal, decision)

    def _deny(self, proposal: Proposal, decision: Decision) -> Decision:
        self._decisions[proposal.id] = decision
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
        self._checks.pop(proposal.id, None)
        return decision

    @staticmethod
    def _airspace_denial(proposal: Proposal, blocked: str) -> Decision:
        return Decision(proposal.id, Verdict.DENIED, blocked, policy_hit="airspace",
                        forbids=proposal.params.get("blocked_volume"), code="airspace")

    @staticmethod
    def _traffic_denial(proposal: Proposal, blocked: str) -> Decision:
        """교차 거절. 코드는 airspace(화면이 다른 거절처럼 재생), policy_hit 은 traffic 입니다.

        운영사가 알아야 할 값(상대 기체·그 부피가 비는 틱)은 detail 에 실어 답장으로 갑니다 —
        params 는 원장에만 남고 답장에는 없습니다.
        """
        params = proposal.params
        return Decision(
            proposal.id, Verdict.DENIED, blocked, policy_hit="traffic",
            forbids=params.get("blocked_asset"), code="airspace",
            detail={key: params.get(key) for key in (
                "blocked_kind", "blocked_asset", "blocked_leg", "blocked_at",
                "blocked_until_tick", "blocked_intent")},
        )

    def _context(self, proposal: Proposal | None, checks: list[str] | None = None,
                 intent_id: str | None = None) -> dict:
        """원장 항목의 판정 맥락. 그때의 틱·공역 판본·걸린 정책·검사 순서."""
        if checks is None:
            checks = self._checks.get(proposal.id, []) if proposal is not None else []
        active = [p.id for p in self.policies.all()
                  if p.active_from_tick <= self.tick
                  and (p.active_until_tick is None or self.tick <= p.active_until_tick)]
        return {"tick": self.tick, "airspace_revision": self.airspace.revision,
                "policies": active, "intent_id": intent_id, "checks_run": list(checks)}

    def check_route(self, proposal: Proposal, checks: list[str] | None = None) -> str | None:
        """받은 경로가 규정에 맞나. 경로를 그리는 건 우리 일이 아닙니다.

        운영사가 자기 기체와 자기 일정을 알고 길을 그립니다. 우리가 하는 건 그 길이
        허용되는지 답하는 것뿐이고, 안 되면 어느 구간의 어느 구역 때문인지 말해줍니다.
        길을 대신 그려주면 그 순간 우리가 운영사가 되고, 잘못된 길의 책임도 우리 것이
        됩니다. 권한과 실행은 나뉘어 있어야 합니다.
        """
        checks = checks if checks is not None else []
        if proposal.action not in ROUTED:
            return None
        legs = proposal.params.get("legs")
        if not legs:
            return None if not self.airspace.all() else "경로를 같이 내야 합니다"
        checks.append("form")
        malformed = _form_problem(legs)
        if malformed:
            # 판정 이전의 양식 문제입니다. 숫자가 아닌 좌표를 판정 함수에 넣으면 요청 하나가
            # 500 으로 죽고, 운영사는 왜 거절됐는지 모릅니다. 양식이 아니면 양식이 아니라고 합니다.
            return f"경로 양식이 아닙니다 ({malformed})"
        # 경로의 양 끝은 기체가 지금 있는 자리와 갈 곳이어야 합니다. 조종장치는 첫 점을 버리고 지금
        # 자리에서 두 번째 점으로 날고, 마지막 점 다음에는 배달지까지 판정 없이 이어 갑니다 —
        # 첫 점을 딴 데 적어 내면 판정한 길과 나는 길이 다른 길이 됩니다.
        checks.append("endpoints")
        astray = self._endpoint_problem(proposal, legs)
        if astray:
            return astray

        checks.append("route")
        found = first_breach(self.airspace, legs)
        if found is not None:
            segment, volume, why, at = found
            # 무엇이 왜 막혔는지를 값으로 남깁니다. 화면이 문장을 다시 뜯으면
            # 문구를 고칠 때마다 화면이 조용히 깨집니다.
            self._note_block(proposal, volume, segment, volume.rule, at)
            return f"{segment}번 구간이 규정을 어깁니다 — {why}"
        # 수직 구간. 이륙 기둥·꼭짓점 승강·착륙 기둥도 공역을 지나는 선입니다. 옆 60m 건물은
        # 120m 순항 구간을 안 막지만 0m 에서 120m 로 오르는 기둥은 막습니다.
        checks.append("columns")
        column = self._column_breach(proposal, legs)
        if column is not None:
            kind, segment, volume, why, at = column
            self._note_block(proposal, volume, segment, kind, at)
            what = {"takeoff": "이륙 기둥", "column": f"{segment}번 꼭짓점 승강",
                    "landing": "착륙 기둥"}[kind]
            return f"{what}이 규정을 어깁니다 — {why}"
        # 경로의 끝은 내려앉는 자리입니다. 옆으로 지나갈 수 있는 길과 수직으로 내려올 수 있는 자리는
        # 다른 기준이라, 끝점 둘레(LANDING_SEPARATION_M)에 건물·금지 구역이 없는지 따로 봅니다.
        checks.append("landing")
        last = legs[-1]
        landing = self.airspace.landing_breach(float(last["lat"]), float(last["lon"]))
        if landing is not None:
            volume, gap = landing
            self._note_block(proposal, volume, len(legs) - 1, "landing",
                             (float(last["lat"]), float(last["lon"])))
            return f"착륙 지점 둘레에 {volume.name} ({gap:.0f}m) — 내려앉을 수 없습니다"
        return None

    def _endpoint_problem(self, proposal: Proposal, legs: list[dict]) -> str | None:
        """첫 점이 기체 자리에서, 끝점이 목적지(배달지·이륙장)에서 TRAFFIC_LATERAL_M 보다 멀면.

        자리를 모르면(텔레메트리 없음) 비교하지 않습니다 — 그건 모르는 것이지 어긋난 것이 아닙니다.
        """
        state = self.telemetry.get(proposal.asset_id) or {}
        first = (float(legs[0]["lat"]), float(legs[0]["lon"]))
        last = (float(legs[-1]["lat"]), float(legs[-1]["lon"]))
        if state.get("lat") is not None and state.get("lon") is not None:
            gap = _distance_m((float(state["lat"]), float(state["lon"])), first)
            if gap > TRAFFIC_LATERAL_M:
                self._note_endpoint(proposal, "origin", 1, first, gap)
                return (f"경로의 첫 점이 기체 자리에서 {gap:.0f}m 떨어져 있습니다 — "
                        "판정한 길과 나는 길이 달라집니다")
        goal = None
        if proposal.action == "fly_route" and state.get("job_lat") is not None:
            goal = (float(state["job_lat"]), float(state["job_lon"]))
        elif proposal.action == "reserve_pad":
            goal = self.pad_coords.get(proposal.resource or proposal.params.get("pad"))
        if goal is not None:
            gap = _distance_m((float(goal[0]), float(goal[1])), last)
            if gap > TRAFFIC_LATERAL_M:
                self._note_endpoint(proposal, "destination", len(legs) - 1, last, gap)
                return (f"경로의 끝점이 목적지에서 {gap:.0f}m 떨어져 있습니다 — "
                        "그 다음은 판정 없이 나는 길입니다")
        return None

    @staticmethod
    def _note_endpoint(proposal: Proposal, kind: str, segment: int, at: tuple[float, float],
                       gap_m: float) -> None:
        proposal.params = {
            **proposal.params, "blocked_kind": kind, "blocked_leg": segment,
            "blocked_at": {"lat": round(at[0], 6), "lon": round(at[1], 6)},
            "blocked_gap_m": round(gap_m, 1),
        }

    @staticmethod
    def _note_block(proposal: Proposal, volume: Volume, segment: int, kind: str,
                    at: tuple[float, float]) -> None:
        proposal.params = {
            **proposal.params,
            "blocked_volume": volume.id,
            "blocked_leg": segment,
            "blocked_name": volume.name,
            "blocked_floor_m": volume.floor_m,
            "blocked_kind": kind,
            "blocked_at": {"lat": round(at[0], 6), "lon": round(at[1], 6)},
            "blocked_polygon": [[lat, lon] for lat, lon in volume.polygon],
            "blocked_ceiling_m": volume.ceiling_m,
        }

    def _column_breach(self, proposal: Proposal, legs: list[dict]):
        """이륙 기둥·꼭짓점 승강·착륙 기둥 중 처음 어기는 것. (종류, 구간, 구역, 왜, 어디).

        판정은 first_breach 하나입니다(G7) — 기둥을 길이 0 인 구간 여럿으로 잘라 넣을 뿐입니다.
        떠 있는 기체의 재신청은 지금 고도에서 첫 구간 고도까지가 이륙 기둥입니다. 다만 그 자리에서
        지금 고도가 이미 어긋나 있으면(회수돼 나온 자리) 그건 계획이 아니라 사실이라 건너뜁니다 —
        안 그러면 내려올 경로조차 못 내고 그 자리에 갇힙니다.
        """
        points = [(float(leg["lat"]), float(leg["lon"]), float(leg.get("alt_m") or 0.0))
                  for leg in legs]
        state = self.telemetry.get(proposal.asset_id, {})
        current_alt = float(state.get("alt_m") or 0.0)
        airborne = current_alt > 1.0
        columns = []
        lat0, lon0, _ = points[0]
        if not (airborne and self.airspace.breach(lat0, lon0, current_alt) is not None):
            columns.append(("takeoff", 1, lat0, lon0, current_alt if airborne else 0.0,
                            points[1][2]))
        for index in range(1, len(points) - 1):
            lat, lon, alt = points[index]
            columns.append(("column", index + 1, lat, lon, alt, points[index + 1][2]))
        lat_n, lon_n, alt_n = points[-1]
        columns.append(("landing", len(points) - 1, lat_n, lon_n, alt_n, 0.0))
        for kind, segment, lat, lon, from_m, to_m in columns:
            found = first_breach(self.airspace, vertical_column(lat, lon, from_m, to_m))
            if found is not None:
                _, volume, why, at = found
                return kind, segment, volume, why, at
        return None

    # ---------- 의도(4D)와 교차 ----------

    def _departure(self, proposal: Proposal) -> tuple[int, float]:
        """이 신청이 실제로 뜨는 틱과 그때의 고도. (틱, 시작 고도).

        지상이면 지금 + 승인 확인(CLEARANCE_TICKS), 아직 싣거나 내리는 중이면 그 일이 끝난 뒤,
        운영사가 출발을 미뤘으면(depart_after_tick) 그때. 떠 있으면 지금, 지금 고도에서.
        """
        state = self.telemetry.get(proposal.asset_id, {})
        altitude = float(state.get("alt_m") or 0.0)
        if altitude > 1.0:
            return self.tick, altitude
        work = max(0, int(state.get("work_ticks") or 0))
        depart = self.tick + max(self.performance.clearance_ticks, work)
        after = proposal.params.get("depart_after_tick")
        if after is not None:
            depart = max(depart, int(after))
        return depart, 0.0

    def _intend(self, proposal: Proposal) -> Intent:
        """신청서 하나의 의도. 운영사가 낸 것은 경로뿐이고, 시간은 신고 성능으로 우리가 셉니다."""
        legs = proposal.params["legs"]
        depart, start_alt = self._departure(proposal)
        volumes, arrive = schedule(legs, depart, start_alt, self.performance)
        return Intent(
            asset=proposal.asset_id, proposal_id=proposal.id, volumes=volumes,
            start=(float(legs[0]["lat"]), float(legs[0]["lon"])),
            landing=(float(legs[-1]["lat"]), float(legs[-1]["lon"])),
            depart_tick=depart, arrive_tick=arrive, filed_tick=self.tick,
        )

    def _check_traffic(self, proposal: Proposal, checks: list[str] | None = None) -> str | None:
        """다른 기체의 살아 있는 의도와 공간·시간이 겹치나 (F3548 전략적 비충돌).

        먼저 낸 쪽이 이깁니다. 예외는 떠 있는 기체의 비상 재신청(회수 뒤) — 그건 거절하지 않고,
        겹치는 상대가 아직 안 떴으면 그 의도를 물립니다(withdrawn). 땅에 있는 쪽이 다시 내는
        것이 하늘에 떠서 기다리는 것보다 쌉니다. 상대도 떠 있으면 물릴 수 없어 거절합니다.
        """
        checks = checks if checks is not None else []
        if proposal.action not in ROUTED or not proposal.params.get("legs"):
            return None
        checks.append("traffic")
        intent = self._intend(proposal)
        asset = proposal.asset_id
        airborne = float(self.telemetry.get(asset, {}).get("alt_m") or 0.0) > 1.0
        last_leg = len(proposal.params["legs"]) - 1
        # 물릴 상대. 여기서는 고르기만 하고, 실제로 물리는 것은 이 신청이 실행된 뒤(_on_committed)
        # 입니다 — 검사가 남의 승인을 물렸는데 이 신청이 사람 보류·한도·조종장치 실패로 안 나가면,
        # 땅의 기체는 경로를 잃고 하늘의 기체는 승인이 없는 채가 됩니다.
        withdraw: list[str] = []
        while True:
            others = self._others(asset, exclude=withdraw)
            conflict = first_conflict(intent.volumes, others)
            if conflict is None:
                checks.append("landing_site")
                conflict = landing_conflict(intent.landing, intent.arrive_tick, last_leg,
                                            others, self.tick)
            if conflict is None:
                conflict = ground_conflict(intent.landing, intent.arrive_tick, last_leg,
                                           self._occupants(asset, exclude=withdraw), self.tick)
            if conflict is None:
                proposal.params = {k: v for k, v in proposal.params.items() if k != "withdraw"}
                if withdraw:
                    proposal.params = {**proposal.params, "withdraw": withdraw}
                return None
            other = self.intents.get(conflict.asset)
            if (not airborne or conflict.asset in withdraw or other is None
                    or other.state != ACCEPTED or other.id != conflict.intent_id):
                break
            withdraw.append(other.asset)

        proposal.params = {
            **proposal.params,
            "blocked_kind": conflict.kind,
            "blocked_asset": conflict.asset,
            "blocked_intent": conflict.intent_id,
            "blocked_leg": conflict.leg,
            "blocked_at": {"lat": round(conflict.at[0], 6), "lon": round(conflict.at[1], 6)},
            "blocked_until_tick": conflict.until_tick,
        }
        if conflict.kind == "landing":
            return (f"착륙 지점을 {conflict.asset} 가 틱 {conflict.until_tick} 까지 씁니다 — "
                    "한 착륙장에 두 대는 없습니다")
        return (f"{conflict.leg}번 구간이 {conflict.asset} 의 승인 경로와 겹칩니다 "
                f"(틱 {conflict.tick}, 상대 회랑은 틱 {conflict.until_tick} 까지)")

    def _others(self, asset: str, exclude: list[str] | tuple[str, ...] = ()) -> list[Intent]:
        """이 신청이 피해야 할 것 전부: 다른 기체의 살아 있는 의도 + 의도 없이 떠 있는 기체의 자리.

        떠 있는 기체는 언제나 어딘가에 있습니다. 회수·물림·반려 뒤에 의도가 없다고 판정에서 빠지면
        그 자리를 지나는 신청이 승인됩니다. 등록부에 없어도 텔레메트리가 떠 있다고 하면 그 자리를
        열린 기둥(presence)으로 세워 둡니다.
        """
        live = [i for i in self.intents.others(asset) if i.asset not in exclude]
        covered = {i.asset for i in live}
        for other, state in self.telemetry.items():
            if other == asset or other in covered or other in exclude:
                continue
            if float(state.get("alt_m") or 0.0) <= 1.0 or state.get("lat") is None:
                continue
            live.append(hold(other, (float(state["lat"]), float(state["lon"])),
                             float(state["alt_m"]), self.tick, self.performance, kind=PRESENCE))
        return live

    def _occupants(self, asset: str, exclude: list[str] | tuple[str, ...] = ()) \
            -> list[tuple[str, tuple[float, float], Intent | None]]:
        """땅에 서 있는 다른 기체들 (기체, 자리, 살아 있는 의도 또는 None).

        물릴 상대(exclude)의 의도는 없는 것으로 봅니다 — 기체는 그대로 거기 서 있습니다.
        """
        found = []
        for other, state in self.telemetry.items():
            if other == asset or state.get("lat") is None:
                continue
            if float(state.get("alt_m") or 0.0) > 1.0:
                continue
            intent = self.intents.get(other)
            if intent is not None and (not intent.live or other in exclude):
                intent = None
            found.append((other, (float(state["lat"]), float(state["lon"])), intent))
        return found

    def _end_intent(self, asset: str, reason: str, exit_point: dict | None = None) -> Intent | None:
        """의도를 끝냅니다. 기체가 떠 있으면 서 있을 자리(contingency)가 그 뒤를 잇습니다.

        회수된 기체는 가장 가까운 바깥(exit)까지 날아가 거기 떠서 기다립니다. 그 길과 그 자리는
        비어 있지 않습니다 — 새 승인이 대신하거나 내릴 때까지 다른 신청이 봐야 합니다.
        """
        ended = self.intents.end(asset, reason)
        state = self.telemetry.get(asset) or {}
        altitude = float(state.get("alt_m") or 0.0)
        if altitude > 1.0 and state.get("lat") is not None:
            door = None
            if exit_point and exit_point.get("lat") is not None:
                door = (float(exit_point["lat"]), float(exit_point["lon"]))
            self.intents.accept(hold(asset, (float(state["lat"]), float(state["lon"])), altitude,
                                     self.tick, self.performance, exit_point=door,
                                     proposal_id=ended.proposal_id if ended else ""))
        return ended

    def _observe(self) -> None:
        """텔레메트리로 의도 상태를 옮기고, 승인한 창보다 일찍 뜬 기체는 원장에 남깁니다."""
        self.intents.observe(self.telemetry, self.tick)
        for intent, planned in self.intents.drain_nonconforming():
            self._ledger_nonconformance(intent, planned)

    def _ledger_nonconformance(self, intent: Intent, planned_tick: int) -> None:
        """미룬 출발을 조종장치가 안 지켰습니다. 의도는 실제 출발로 옮겨 등록됐고, 기록에 남깁니다.

        되돌리지는 않습니다 — 떠 있는 기체를 세우는 것은 회수이고, 그 판단은 여기 것이 아닙니다.
        """
        noted = Proposal(asset_id=intent.asset, action="conformance", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"승인한 출발 틱 {planned_tick} 보다 일찍 뜸 (틱 {self.tick})",
                         params={"intent": intent.id, "planned_depart_tick": planned_tick,
                                 "actual_depart_tick": intent.depart_tick})
        decision = Decision(noted.id, Verdict.AUTO,
                            f"{intent.asset} 가 승인한 창보다 {planned_tick - self.tick}틱 "
                            "일찍 떴습니다 — 의도를 실제 출발로 옮김", code="nonconforming",
                            detail={"resource": intent.asset, "intent": intent.id,
                                    "planned_depart_tick": planned_tick})
        entry = self.ledger.open_entry(noted, decision,
                                       self._context(None, ["conformance"], intent.id))
        self.ledger.close_entry(entry, "noted")

    def _withdraw(self, intent: Intent, for_proposal: Proposal) -> Decision:
        """아직 안 뜬 의도를 물립니다. 런타임이 쓴 결정이고, 조종장치에는 경유점 지우기만 갑니다.

        땅에 있는 기체는 경로만 잃고 그 자리에 그대로 있습니다(sim divert_ground). 그 운영사는
        경로가 없어진 것을 보고 다시 냅니다 — 그때는 떠 있는 쪽이 먼저 낸 것이 됩니다.
        """
        retreat = Proposal(
            asset_id=intent.asset, action="divert_ground", cost_usd=0.0, blast_radius="cargo",
            rationale=f"{for_proposal.asset_id} 의 공중 재신청에 자리를 내줌",
            author="runtime",
            params={"withdrawn_for": for_proposal.asset_id, "intent": intent.id},
        )
        decision = Decision(
            retreat.id, Verdict.AUTO,
            f"{for_proposal.asset_id} 의 공중 재신청과 겹쳐 아직 안 뜬 경로를 물림",
            policy_hit="traffic", code="withdrawn",
            detail={"resource": intent.asset, "for": for_proposal.asset_id, "intent": intent.id},
        )
        self._decisions[retreat.id] = decision
        entry = self.ledger.open_entry(retreat, decision,
                                       self._context(None, ["withdraw"], intent.id))
        decision.ledger_id = entry.id
        result = self.adapter.execute(intent.asset, "divert_ground", retreat.params, entry.id,
                                      blast=retreat.blast_radius)
        ok = bool(result.get("ok"))
        self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}", decision)
        self._end_intent(intent.asset, "withdrawn")
        # 물린 경로의 실행 기록은 중복 방지에서 뺍니다. 안 빼면 그 기체의 재신청이 '직전에 같은
        # 신청이 실행됐다' 로 거절됩니다 — 실행된 것을 방금 무른 것인데도.
        for action in ROUTED:
            self._recent_commits.pop((intent.asset, action), None)
        return decision

    def _on_committed(self, proposal: Proposal, decision: Decision, entry) -> dict | None:
        """실행이 성공한 직후. 경로면 (고른 상대를 물리고) 의도를 만들고, 그 밖의 행동이면 그 기체의
        의도를 끝냅니다.

        물림은 여기서만 일어납니다 — 실행되지 않은 신청은 아무도 물리지 않습니다.
        """
        if proposal.action in ROUTED and proposal.params.get("legs"):
            withdrew = []
            for other in proposal.params.get("withdraw") or []:
                standing = self.intents.get(other)
                if standing is not None and standing.state == ACCEPTED:
                    self._withdraw(standing, proposal)
                    withdrew.append(other)
            intent = self._intend(proposal)
            self.intents.accept(intent)
            proposal.params = {k: v for k, v in proposal.params.items() if k != "withdraw"}
            if withdrew:
                proposal.params = {**proposal.params, "withdrew": withdrew}
            entry.proposal = proposal.to_dict()   # 닫는 줄은 물린 뒤의 신청서를 담아야 합니다
            return {"intent_id": intent.id, **({"withdrew": withdrew} if withdrew else {})}
        ended = self._end_intent(proposal.asset_id, proposal.action,
                                 exit_point=proposal.params.get("exit"))
        return {"intent_id": ended.id} if ended is not None else None

    def _rejudge(self, proposal: Proposal, decision: Decision) -> str | None:
        """실행 직전에 지금의 공역·의도로 다시 판정합니다. 막히면 거절로 닫고 이유를 돌려줍니다.

        판정은 접수(file) 때 한 번 합니다. 사람 승인을 기다리거나 자원 줄에 서 있는 동안 구역
        공지가 오면, 나중의 실행은 옛 공역으로 판정한 경로를 닫힌 구역으로 내보냈습니다.
        '실행된 경로는 전부 런타임의 공역으로 판정을 지났다' 는 실행 시점의 말이어야 합니다.
        판정은 밀리초라 공역 판본을 기억해 두고 바뀐 때만 다시 보는 것보다 매번 보는 게 쌉니다.
        """
        checks = self._checks.setdefault(proposal.id, [])
        checks.append("rejudge")
        blocked = self.check_route(proposal, checks)
        traffic = None if blocked else self._check_traffic(proposal, checks)
        if blocked is None and traffic is None:
            return None
        fresh = (self._airspace_denial(proposal, blocked) if blocked
                 else self._traffic_denial(proposal, traffic))
        decision.verdict = Verdict.DENIED
        decision.reason = fresh.reason
        decision.policy_hit = fresh.policy_hit
        decision.forbids = fresh.forbids
        decision.code = fresh.code
        decision.detail = fresh.detail
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
        return decision.reason

    def _queue_or_commit(self, proposal: Proposal, decision: Decision) -> Decision:
        if not proposal.resource:
            committed = self.committer.commit(proposal, decision, self._context(proposal))
            if committed.committed:
                self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
            self._checks.pop(proposal.id, None)
            return committed

        with self._guard:
            waiting = self._contended.setdefault(proposal.resource, [])
            standing = next(
                (d for p, d, _ in waiting if p.asset_id == proposal.asset_id), None
            )
            if standing is not None:
                # 이미 줄을 서 있습니다. 같은 기체가 같은 자원으로 두 번 서지 않습니다.
                return standing
            waiting.append((proposal, decision, time.time()))

        decision.verdict = Verdict.QUEUED
        decision.reason = f"{proposal.resource} 배정을 기다리는 중"
        return decision

    def approve(self, proposal_id: str, actor: str, allow: bool) -> Decision | None:
        with self._guard:
            proposal = self._awaiting_human.pop(proposal_id, None)
        if proposal is None:
            return None
        decision = self._decisions[proposal_id]
        decision.approved_by = actor
        if proposal.action == "publish_notice":
            return self._confirm_notice(proposal, decision, actor, allow)
        if not allow:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} 가 거부했습니다"
            self.ledger.close_entry(
                self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
            return decision
        decision.verdict = Verdict.AUTO
        decision.reason = f"{actor} 가 승인했습니다"
        if self._rejudge(proposal, decision):
            return decision   # 기다리는 사이 공역이 바뀌었습니다. 승인은 옛 경로를 살리지 못합니다
        return self._queue_or_commit(proposal, decision)

    # ---------- 자원 중재 ----------

    def _settle_contended(self) -> None:
        now = time.time()
        with self._guard:
            ready = [
                resource
                for resource, waiting in self._contended.items()
                if waiting and now - waiting[0][2] >= self.window_s
            ]
            batches = {resource: self._contended.pop(resource) for resource in ready}

        for resource, waiting in batches.items():
            # 줄 서 있는 동안 공역이 바뀌었을 수 있습니다. 막힌 경로는 중재에 들어가지 않습니다.
            waiting = [item for item in waiting if self._rejudge(item[0], item[1]) is None]
            if not waiting:
                continue
            held = self.locks.holder(resource)
            candidates = [item[0] for item in waiting]
            if held and held.asset_id not in {p.asset_id for p in candidates}:
                for proposal, decision, _ in waiting:
                    decision.verdict = Verdict.DENIED
                    decision.reason = f"{resource} 는 {held.asset_id} 가 쓰는 중입니다"
                    decision.code = "resource_held"
                    decision.detail = {"resource": resource, "holder": held.asset_id}
                    self.ledger.close_entry(
                        self.ledger.open_entry(proposal, decision, self._context(proposal)),
                        "denied")
                continue

            choice = self.arbiter.pick(candidates, self.telemetry)
            winner, how = choice.proposal, choice.how
            # 모델이 고른 이유는 기록에 남습니다. 고른 것은 번호 하나고, 그 번호가 범위 밖이면
            # 규칙이 골랐습니다 — 이유는 설명이지 결정이 아닙니다.
            detail = {"resource": resource}
            if choice.reason:
                detail["arbiter_reason"] = choice.reason
            for proposal, decision, _ in waiting:
                if proposal.id == winner.id:
                    decision.arbiter = how if len(candidates) > 1 else None
                    decision.verdict = Verdict.AUTO
                    decision.reason = f"{resource} 배정됨"
                    decision.code = "resource_granted"
                    decision.detail = dict(detail)
                    self._checks.setdefault(proposal.id, []).append("arbiter")
                    self.committer.commit(proposal, decision, self._context(proposal))
                    if decision.committed:
                        self._recent_commits[(proposal.asset_id, proposal.action)] = self.tick
                    self._checks.pop(proposal.id, None)
                else:
                    decision.verdict = Verdict.DENIED
                    decision.arbiter = how
                    decision.reason = f"{winner.asset_id} 가 {resource} 를 받았습니다"
                    decision.detail = dict(detail)
                    self.ledger.close_entry(
                        self.ledger.open_entry(proposal, decision, self._context(proposal)),
                        "denied")

    # ---------- 바깥에서 오는 소식 ----------

    def _pull_world(self) -> None:
        state = self.adapter.telemetry()
        if state:
            self.tick = state.get("tick", self.tick)
            self.telemetry = state.get("assets", {})
            self._follow_round(state.get("round"))
            self._observe()

        if not self.airspace.all():
            world = get_json(f"{self.sim_url}/state?world=guarded&volumes=1") or {}
            for raw in world.get("volumes", []):
                self.airspace.add(Volume.from_dict(raw))
            self.pad_coords = {
                name: (at["lat"], at["lon"])
                for name, at in (world.get("pad_coords") or {}).items()
            }
            self.landing_areas = list(world.get("landing_areas") or [])

        bulletins = get_json(f"{self.sim_url}/bulletins?world=guarded") or {}
        self.absorb(bulletins.get("bulletins", []))

    def _follow_round(self, round_number) -> None:
        """판이 바뀌면 한 판짜리 상태를 비웁니다 — 예산, 잠금, 중복 방지, 의도, 공지.

        원장은 남습니다. 그건 역사이고, 판이 바뀐다고 없던 일이 되지 않습니다.
        공지 정책도 비웁니다. 같은 id 로 다시 게시되면 그때 다시 걸립니다.
        """
        if round_number is None or round_number == self._round:
            return
        self._round = round_number
        self.authority.new_round()
        for asset_id in list(self.telemetry):
            self.locks.release_all(asset_id)
        self._recent_commits.clear()
        for volume_id in self.zone_volumes:
            self.airspace.remove(volume_id)
        self.zone_volumes.clear()
        self.policies.clear()
        self.intents.clear()
        self.notices.clear()
        with self._guard:
            for proposal_id, waiting in list(self._awaiting_human.items()):
                if waiting.action == "publish_notice":
                    self._awaiting_human.pop(proposal_id)

    def service_bbox(self) -> tuple[float, float, float, float] | None:
        """착륙장·이륙장을 담는 상자 + 여유. 모델이 구조화한 공지는 이 안이어야 합니다."""
        points = [(a["lat"], a["lon"]) for a in self.landing_areas] + list(self.pad_coords.values())
        if not points:
            return None
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        return (min(lats) - SERVICE_MARGIN_DEG, min(lons) - SERVICE_MARGIN_DEG,
                max(lats) + SERVICE_MARGIN_DEG, max(lons) + SERVICE_MARGIN_DEG)

    def absorb(self, bulletins: list[dict]) -> None:
        """공지를 규칙으로 받습니다. 구역 공지는 문장이고, 읽어서 공역에 넣습니다.

        자원만 막고 공역을 그대로 두면, 닫힌 구역을 지나는 경로가 계속 승인됩니다.
        기지 위에 구역이 닫혔는데 그리로 날아가는 경로가 통과하던 게 그래서였습니다.
        문법이 읽은 공지는 그 틱에 걸립니다. 모델이 읽은 공지는 사람이 확인할 때까지 보류입니다.
        유효기간이 끝나거나 공지에서 빠지면 판정 기준에서도 빠집니다.
        """
        items = [i for i in bulletins if i.get("kind") in ("recall", "zone", "notam")]
        known = {p.id for p in self.policies.all()}
        for item in items:
            if item["id"] in known:
                continue
            if item.get("kind") == "recall":
                self._enforce_policy(item)
                continue
            if self.notices.known(item["id"]):
                continue
            record = self.notices.read(item, self.service_bbox())
            if record is None:
                self._ledger_notice(item, "unreadable",
                                    self.notices.unreadable.get(item["id"], ""))
            elif record.held:
                self._hold_notice(item, record)
        self._apply_notices({i["id"] for i in items})

    def _enforce_policy(self, item: dict) -> None:
        # 제한하는 정책은 즉시 걸립니다. 푸는 정책만 사람이 풉니다.
        policy = config_module.Policy(
            id=item["id"],
            reason=item.get("reason", item.get("kind", "")),
            forbid_action=item.get("forbid_action"),
            forbid_resource=item.get("forbid_resource"),
            applies_to=item.get("applies_to", {}),
            active_from_tick=0,
            active_until_tick=item.get("until_tick"),
        )
        self.policies.add(policy)
        self.revoke_under(policy)

    def _apply_notices(self, feed_ids: set[str] | None = None) -> None:
        """창이 열린 공지는 공역에 넣고 날던 경로를 회수하고, 닫힌 공지는 뺍니다."""
        for record in self.notices.due(self.tick):
            record.applied = True
            self.airspace.add(record.volume)
            self.zone_volumes.add(record.id)
            self.recall_flights(record.volume)
            if record.id not in {p.id for p in self.policies.all()}:
                self.policies.add(config_module.Policy(
                    id=record.id, reason=record.volume.reason or record.name,
                    active_from_tick=record.from_tick or 0, active_until_tick=record.until_tick))
        feed = feed_ids if feed_ids is not None else {r.id for r in self.notices.records.values()}
        for record in self.notices.lapsed(self.tick, feed):
            record.applied = False
            self.airspace.remove(record.id)
            self.zone_volumes.discard(record.id)
            if record.id not in feed:
                self.notices.forget(record.id)
        for expired in self.zone_volumes - {r.id for r in self.notices.records.values()
                                            if r.applied}:
            self.airspace.remove(expired)
            self.zone_volumes.discard(expired)

    def _hold_notice(self, item: dict, record) -> None:
        """모델이 구조화한 공지를 승인 화면에 올립니다. 사람이 승인하기 전에는 아무것도 안 막습니다.

        보류된 공지는 신청서 모양(action publish_notice)이라 기존 승인 화면이 그대로 보여 줍니다."""
        held = Proposal(
            asset_id="airspace", action="publish_notice", cost_usd=0.0, blast_radius="none",
            rationale=f"{record.name} — {record.text}"[:180], author=record.source,
            params={"notice_id": record.id, "text": record.text, "notice": record.to_dict(),
                    "source": record.source},
        )
        decision = Decision(held.id, Verdict.HUMAN,
                            "모델이 읽은 공지는 사람이 확인해야 걸립니다",
                            authority_hit="model_notice", code="human_notice",
                            detail={"notice": record.id, "source": record.source})
        self._decisions[held.id] = decision
        with self._guard:
            self._awaiting_human[held.id] = held
        self.ledger.open_entry(held, decision,
                               self._context(None, ["notice:grammar", "notice:model"]))

    def _confirm_notice(self, proposal: Proposal, decision: Decision, actor: str,
                        allow: bool) -> Decision:
        record = self.notices.confirm(proposal.params.get("notice_id", ""), actor, allow)
        if allow and record is not None:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 공지를 확인했습니다"
            decision.code = "notice_published"
        else:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} 가 공지를 거부했습니다" if record is not None \
                else "그런 공지가 없습니다"
            decision.code = "notice_refused"
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(None, ["notice:human"])),
            "done" if allow and record is not None else "denied")
        self._apply_notices()
        return decision

    def _ledger_notice(self, item: dict, outcome: str, why: str) -> None:
        """못 읽은 공지도 기록입니다. 안 걸린 이유가 원장에 있어야 합니다."""
        unread = Proposal(asset_id="airspace", action="publish_notice", cost_usd=0.0,
                          blast_radius="none", rationale=str(item.get("text") or "")[:180],
                          author="runtime", params={"notice_id": item["id"],
                                                    "text": item.get("text")})
        decision = Decision(unread.id, Verdict.DENIED, f"공지를 읽지 못했습니다 — {why}",
                            code="notice_unreadable", detail={"notice": item["id"], "why": why})
        self.ledger.close_entry(
            self.ledger.open_entry(unread, decision,
                                   self._context(None, ["notice:grammar", "notice:model"])),
            outcome)

    def background(self) -> None:
        """세계를 받아오는 쪽. 중재와 같은 스레드에 두면 안 됩니다(아래 settle_forever)."""
        while True:
            # 한 번 실패해도 다음 주기에 다시 봅니다. 이 스레드가 죽으면 런타임은 옛 세계를
            # 보면서 판정하게 되고, 그건 조용히 틀리는 최악의 상태입니다.
            try:
                self._pull_world()
            except Exception as error:  # noqa: BLE001 — 살아남는 것이 먼저입니다
                print(f"runtime background: {error!r}", flush=True)
            time.sleep(0.25)

    def settle_forever(self) -> None:
        """자원 중재만 하는 스레드. 모델이 느려도 세계의 시계는 멈추지 않습니다.

        중재가 세계 갱신과 한 스레드에 있으면 Ultra 가 3초 생각하는 동안 틱·위치·공지가
        3초 낡습니다. 그동안 들어온 신청은 옛 위치로 판정됩니다. 중재는 어차피
        window_s 만큼 기다렸다 하는 일이라 따로 돌아도 늦어지는 것은 중재뿐입니다.
        """
        while True:
            try:
                self._settle_contended()
            except Exception as error:  # noqa: BLE001
                print(f"runtime arbiter: {error!r}", flush=True)
            time.sleep(0.25)

    def start_background(self) -> list[threading.Thread]:
        threads = [threading.Thread(target=self.background, daemon=True, name="world"),
                   threading.Thread(target=self.settle_forever, daemon=True, name="arbiter")]
        for thread in threads:
            thread.start()
        return threads

    def recall_flights(self, volume) -> list[Decision]:
        """이미 승인해서 날고 있는 경로를 새 구역으로 다시 판정합니다.

        거절만으로는 부족합니다. 규칙이 도착하기 전에 승인한 비행은 그 규칙을 모르고
        계속 날아갑니다. 강제점이 있다는 말은 이미 벌어진 일도 되돌린다는 뜻입니다.
        회수된 기체의 경로 의도는 끝납니다 — 그 경로는 더는 날지 않으니 남을 막아서도 안 됩니다.
        대신 바깥까지 나가는 길과 거기 떠 있을 자리(contingency)가 그 뒤를 잇습니다.
        """
        pulled = []
        for asset_id, telemetry in self.telemetry.items():
            legs = [{"lat": telemetry.get("lat"), "lon": telemetry.get("lon"),
                     "alt_m": telemetry.get("alt_m", 0.0)}]
            legs += [{"lat": leg["lat"], "lon": leg["lon"], "alt_m": leg.get("alt_m", 0.0)}
                     for leg in (telemetry.get("route") or [])]
            if len(legs) < 2 or legs[0]["lat"] is None:
                continue
            if first_breach(Airspace([volume], default_ceiling_m=None), legs) is None:
                continue
            # 안에 있던 기체는 가장 가까운 바깥으로 내보냅니다. 제자리에 세워두면 닫힌 구역
            # 안에 머무는 것이고, 거기서는 어떤 경로도 출발점부터 금지라 다시 그릴 수 없습니다.
            exit_point = nearest_exit(volume, legs[0]["lat"], legs[0]["lon"])
            params = ({"exit": {"lat": exit_point[0], "lon": exit_point[1]}, "volume": volume.id}
                      if exit_point else {"volume": volume.id})
            retreat = Proposal(
                asset_id=asset_id, action="divert_ground", cost_usd=35.0,
                blast_radius="cargo", rationale=f"{volume.name} ({volume.id})",
                author="runtime", params=params,
            )
            decision = Decision(
                retreat.id, Verdict.AUTO,
                f"{volume.id} 로 비행 중이던 경로를 회수",
                policy_hit=volume.id, code="recalled",
                detail={"resource": asset_id, "policy": volume.id},
            )
            # 조종장치에는 원장 번호(문자열)가 갑니다. 원장 항목 객체를 그대로 넘겼더니 HTTP
            # 어댑터가 JSON 으로 못 만들어 배경 스레드가 죽었고, 그 뒤로 런타임이 옛 위치를
            # 계속 내보내서 모든 신청이 엉뚱한 자리에서 시작됐습니다. 로컬 어댑터만 쓰는
            # 시험은 못 잡았습니다.
            standing = self.intents.get(asset_id)
            intent_id = standing.id if standing is not None and standing.live else None
            entry = self.ledger.open_entry(retreat, decision,
                                           self._context(None, ["recall"], intent_id))
            decision.ledger_id = entry.id
            result = self.adapter.execute(asset_id, "divert_ground", params, entry.id,
                                          blast=retreat.blast_radius)
            ok = bool(result.get("ok"))
            self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}",
                                    decision)
            self._end_intent(asset_id, "recalled", exit_point=params.get("exit"))
            for action in ROUTED:
                self._recent_commits.pop((asset_id, action), None)
            pulled.append(decision)
        return pulled

    def revoke_under(self, policy) -> Decision | None:
        """금지가 도착했는데 이미 그 자원을 잡고 있으면 뺏고 회항시킵니다.

        거절만 하는 것과 이게 다릅니다. 강제점이 있으면 이미 벌어진 일도 되돌립니다.
        회항은 한도를 보지 않고 나갑니다. 닫힌 구역에서 빠져나오는 건 예산 문제가
        아닙니다. 대신 누가 왜 시켰는지는 원장에 그대로 남습니다.
        """
        if not policy.forbid_resource:
            return None
        hold = self.locks.holder(policy.forbid_resource)
        if hold is None:
            return None

        self.locks.release(policy.forbid_resource, hold.asset_id)
        retreat = Proposal(
            asset_id=hold.asset_id,
            action="divert_ground",
            cost_usd=35.0,
            blast_radius="cargo",
            rationale=f"{policy.reason} ({policy.id})",
            author="runtime",
        )
        decision = Decision(
            retreat.id, Verdict.AUTO, f"{policy.id} 로 {policy.forbid_resource} 회수",
            policy_hit=policy.id, code="recalled",
            detail={"resource": policy.forbid_resource, "policy": policy.id},
        )
        self._decisions[retreat.id] = decision
        return self.committer.commit(retreat, decision, self._context(None, ["revoke"]))

    # ---------- 화면에 보여줄 것 ----------

    def snapshot(self) -> dict:
        self._observe()
        with self._guard:
            pending = [p.to_dict() for p in self._awaiting_human.values()]
            waiting = {r: len(v) for r, v in self._contended.items()}
        return {
            "tick": self.tick,
            "config": self.config.name,
            # 모델은 보이되 결정권이 없습니다. 어느 서버에 몇 번 물었고 몇 번 규칙이 대신했는지.
            # 여기 세는 것은 런타임 자신의 호출(중재·공지 구조화)뿐이고, 기체 쪽 호출은 기체
            # 프로세스가 압니다.
            "llm": {"enabled": self.llm.enabled, "models": self.llm.models,
                    "host": self.llm.host, "calls": self.llm.stats_dict()},
            "locks": self.locks.snapshot(),
            "contended": waiting,
            "awaiting_human": pending,
            "policies": [vars(p) for p in self.policies.all()],
            # 승인한 경로가 언제 어디에 있을지. 화면은 이것으로 누가 누구를 기다리는지 그립니다.
            "intents": self.intents.snapshot(),
            # 걸려 있는 공지. 배너는 시뮬레이터가 아니라 여기서 — 강제되는 것이 보이는 것입니다.
            "notices": self.notices.snapshot(),
            "spend": {
                "fleet": self.authority.fleet_spend,
                "fleet_limit": self.config.authority.fleet_usd,
                "per_asset_limit": self.config.authority.per_asset_usd,
                "by_asset": {
                    asset: self.authority.asset_spend(asset) for asset in self.telemetry
                },
            },
            "ledger": self.ledger.tail(25),
        }


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _form_problem(legs) -> str | None:
    """legs 가 판정에 넣을 양식인가, 아니면 무엇이 아닌지. 판정이 아니라 양식 검사입니다.

    좌표는 지구 위(|lat| ≤ 90, |lon| ≤ 180), 고도는 땅 위(≥ 0), 구간은 MAX_LEG_M 이하여야 합니다.
    유한하기만 한 값은 양식이 아닙니다 — 음수 고도는 모든 구역의 '아래' 로 빠져 건물을 관통했고,
    1e300 짜리 좌표는 판정을 영영 끝나지 않게 했습니다.
    """
    if not isinstance(legs, list) or len(legs) < 2:
        return "legs 는 둘 이상의 점 목록"
    previous = None
    for index, leg in enumerate(legs, start=1):
        if not isinstance(leg, dict):
            return f"{index}번 점이 객체가 아님"
        try:
            lat, lon, alt = float(leg["lat"]), float(leg["lon"]), float(leg.get("alt_m", 0.0))
        except (KeyError, TypeError, ValueError):
            return f"{index}번 점에 숫자 lat/lon/alt_m 가 없음"
        if not all(math.isfinite(value) for value in (lat, lon, alt)):
            return f"{index}번 점이 유한한 수가 아님"
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return f"{index}번 점이 지구 위 좌표가 아님"
        if alt < 0.0:
            return f"{index}번 점의 고도가 땅 밑 ({alt:.0f}m)"
        if previous is not None:
            length = math.hypot((lat - previous[0]) * METRES_PER_DEG_LAT,
                                (lon - previous[1]) * METRES_PER_DEG_LON)
            if length > MAX_LEG_M:
                return (f"{index - 1}번 구간이 너무 김 "
                        f"({length / 1000:.0f}km > {MAX_LEG_M / 1000:.0f}km)")
        previous = (lat, lon)
    return None


def main() -> None:
    runtime = Runtime(
        config_path=os.getenv("CONFIG", "configs/fleet.yaml"),
        sim_url=os.getenv("SIM_URL", "http://sim:8100"),
        ledger_path=os.getenv("LEDGER_PATH", "ledger.jsonl"),
        window_s=float(os.getenv("ARBITRATION_WINDOW_S", "1.5")),
    )
    runtime.start_background()

    server = JsonServer(int(os.getenv("PORT", "8000")))
    server.add("POST", "/proposals", lambda body, query: (200, runtime.file(body).to_dict()))
    server.add(
        "POST",
        "/approve",
        lambda body, query: _approval(runtime, body, allow=True),
    )
    server.add("POST", "/deny", lambda body, query: _approval(runtime, body, allow=False))
    server.add("GET", "/state", lambda body, query: (200, runtime.snapshot()))
    # 공역 판본을 같이 보냅니다. 구역이 새로 닫히면 운영사가 사본을 갱신하고 처음부터
    # 피해서 그리게 — 안 그러면 닫힌 구역으로 직선을 내고 거절당한 뒤에야 압니다.
    # 틱도 같이 갑니다. 출발을 미루는 재신청(depart_after_tick)은 세계의 시계로 말해야 합니다.
    server.add(
        "GET",
        "/telemetry/{asset}",
        lambda body, query, asset: (200, {**runtime.telemetry.get(asset, {}),
                                          "airspace_revision": runtime.airspace.revision,
                                          "tick": runtime.tick}
                                    if asset in runtime.telemetry else {}),
    )
    # 착륙장 목록도 같이 줍니다. 운영사가 서비스 영역(모델 초안이 나가면 안 되는 상자)을
    # 여기서 셈합니다. 판정과는 무관한, 이륙장 좌표와 같은 종류의 자료입니다.
    server.add("GET", "/airspace", lambda body, query: (200, {
        "volumes": [v.to_dict() for v in runtime.airspace.all()],
        "pads": {n: {"lat": a[0], "lon": a[1]} for n, a in runtime.pad_coords.items()},
        "landing_areas": runtime.landing_areas,
    }))
    server.add("GET", "/health", lambda body, query: (200, {"ok": True, "tick": runtime.tick}))
    print(f"runtime listening on :{os.getenv('PORT', '8000')}", flush=True)
    server.serve_forever()


def _approval(runtime: Runtime, body: dict, allow: bool):
    decision = runtime.approve(
        body.get("proposal_id", ""), body.get("actor", "관제사"), allow=allow
    )
    if decision is None:
        return 404, {"error": "그런 신청서가 없습니다"}
    return 200, decision.to_dict()


if __name__ == "__main__":
    main()
