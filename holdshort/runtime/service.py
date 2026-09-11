"""The runtime process. Holds locks, limits, the arbiter, the single commit path, the ledger."""

import json
import math
import os
import threading
import time
from pathlib import Path

from holdshort.adapters import build as build_adapter
from holdshort.core import config as config_module
from holdshort.core.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    Airspace,
    Volume,
    first_breach,
    nearest_exit,
    vertical_column,
)
from holdshort.core.http import JsonServer, get_json
from holdshort.core.intake import Gazetteer
from holdshort.core.metar import DEFAULT_PERIOD_S as METAR_DEFAULT_PERIOD_S
from holdshort.core.metar import SOURCE as METAR_SOURCE
from holdshort.core.metar import MetarClient, MetarPoller
from holdshort.core.models import AgentIdentity, Decision, Proposal, Verdict
from holdshort.core.notam import Clock
from holdshort.core.route import Router
from holdshort.core.tavily import FetchStatus, IntakePoller, TavilyClient
from holdshort.llm.client import TieredLlm
from holdshort.runtime.advisory import AdvisoryDesk, Refusal, build_options
from holdshort.runtime.arbiter import Arbiter
from holdshort.runtime.authority import AuthorityCheck
from holdshort.runtime.briefing import BriefingDesk
from holdshort.runtime.commit import Committer
from holdshort.runtime.intake import (
    HOLD_POLICY_PREFIX,
    INTAKE_PERIOD_S,
    IntakeBook,
    IntakeRecord,
    WeatherHold,
    incident_snapshot,
    item_id,
)
from holdshort.runtime.intents import (
    ACCEPTED,
    PRESENCE,
    Intent,
    IntentRegistry,
    LinkEvent,
    LinkWatch,
    first_conflict,
    ground_conflict,
    hold,
    landing_conflict,
    schedule,
)
from holdshort.runtime.ledger import Ledger
from holdshort.runtime.locks import LockTable
from holdshort.runtime.notices import NoticeBook
from holdshort.runtime.policy import PolicyBook
from holdshort.runtime.reports.ledger import build_report, to_markdown
from holdshort.runtime.store import DEFAULT_PATH as STORE_DEFAULT_PATH
from holdshort.runtime.store import IntakeStore

# 한 구간의 최대 길이. 서비스 반경이 11km 라 그 안의 어떤 경로도 이보다 긴 구간은 없습니다.
# 유한하기만 한 좌표로 지구 반 바퀴짜리 구간을 내면 판정이 색인 격자 1e10 칸을 돌며 영영 안
# 끝났고, 그동안 런타임 스레드가 GIL 을 쥐어 세계·중재가 멈췄습니다. 판정 이전의 양식 문제입니다.
MAX_LEG_M = 50_000.0
# 경로를 실어 오는 행동. 이것만 공역·의도 판정을 받습니다.
ROUTED = ("reserve_pad", "fly_route")
# 권고의 연속 거절로 세지 않는 거절. 길이 막힌 게 아니라 같은 신청을 두 번 냈거나(duplicate),
# 런타임이 아직 판정할 준비가 안 됐습니다(airspace_not_loaded).
NOT_REFUSALS = ("duplicate", "airspace_not_loaded")
# 시작할 때 시뮬레이터에서 공역(3만여 개)을 받는 한 번의 요청. 세계 스레드는 공역 없이 할 일이 없어
# 넉넉히 기다립니다 — 짧게 끊으면 큰 답을 매번 처음부터 다시 받습니다.
AIRSPACE_FETCH_TIMEOUT_S = 30.0
# 서비스 영역 상자의 여유(약 2km). 모델이 구조화한 공지가 이 밖이면 지어낸 것입니다.
SERVICE_MARGIN_DEG = 0.02
# 정보 수집이 자리를 찾는 지명 사전. 배달 주소와 같은 파일입니다 — 사고가 "있는 곳" 은 배달이 갈
# 수 있는 곳과 같은 목록이어야 하고, 목록 밖의 주소는 모델이 지어낸 것입니다.
ADDRESS_FILE = os.getenv(
    "ADDRESS_FILE", str(Path(__file__).resolve().parent.parent.parent
                        / "configs/airspace/nyc_addresses.json"))
# 정보 수집의 접수 카드·정책이 쓰는 이름. 기체가 아니라 기단·관제탑의 일입니다.
INTAKE_ASSET = "intake"
FLEET_ASSET = "fleet"
INTAKE_CHECKS = ["intake:grammar", "intake:model"]
# METAR 를 몇 초마다 받나. 관측은 시간마다(특별 관측은 사이사이) 나옵니다.
METAR_PERIOD_S = float(os.getenv("METAR_PERIOD_S") or METAR_DEFAULT_PERIOD_S)
# 기체 등록이 이만큼(틱) 새로 오지 않으면 /state.agents 에서 뺍니다. 기체는 30초마다 다시 알립니다
# (holdshort/agent/loop.py REGISTER_PERIOD_S) — 0.2 s/틱에서 600틱은 2분, 두 번 넘게 빠진 것입니다.
AGENT_STALE_TICKS = int(os.getenv("AGENT_STALE_TICKS") or "600")
# 공식 관측. 관제탑 자기 피드(시뮬레이터 공지)처럼 문법이 읽으면 그 틱에 걸립니다 —
# aviationweather.gov 의 숫자를 코드가 문장으로 옮겼고 문법이 다시 읽은 것이지 웹 페이지가
# 아닙니다. 모델이 읽은 것은 출처와 상관없이 여전히 사람 뒤입니다.
OFFICIAL_SOURCES = frozenset({METAR_SOURCE})
# 출처 실패·회복 줄에 쓰는 이름.
SOURCE_NAMES = {"tavily": "검색", METAR_SOURCE: "METAR"}
# /state.agents 한 줄의 필드.
AGENT_FIELDS = ("model", "host", "world", "last_seen_tick", "display", "base_url_port", "model_ok")
# 항목과 함께 기록에 남기는 구조화 값(POST /intake 의 힌트, 시뮬레이터 사고 공지의 주소·반경).
# 재시작 뒤에 다시 읽을 때 문장만 있으면 주소로 온 사고를 못 읽습니다.
INTAKE_HINT_KEYS = ("name", "address", "building_id", "radius_m", "until_tick")


class TowerIntake(IntakeBook):
    """관제탑의 접수 책. 공식 관측(METAR)을 관제탑 피드와 같이 믿는다는 것 하나를 더합니다."""

    @staticmethod
    def must_hold(record: IntakeRecord, read_by: str) -> bool:
        if record.source in OFFICIAL_SOURCES:
            return read_by.startswith("model:")
        return IntakeBook.must_hold(record, read_by)

    def snapshot(self, tavily_on: bool) -> dict:
        out = super().snapshot(tavily_on)
        for item in out["items"]:
            if item["source"] in OFFICIAL_SOURCES:
                item["trusted"] = True
        return out


class Runtime:
    def __init__(self, config_path: str, sim_url: str, ledger_path: str, window_s: float = 1.5,
                 intake_db: str | None = None, metar: bool = False,
                 await_airspace: bool = False, briefing: bool = False):
        self.config = config_module.load(config_path)
        self.policies = PolicyBook(self.config.policies)
        self.authority = AuthorityCheck(self.config.authority, self.policies)
        self.locks = LockTable(self.config.resources)
        self.ledger = Ledger(ledger_path)
        self.llm = TieredLlm(models=vars(self.config.escalation))
        self.arbiter = Arbiter(self.llm)
        self.adapter = build_adapter(
            os.getenv("ADAPTER", "sim"), sim_url=sim_url, world="guarded",
            # PX4 거울(ADAPTER=composite)의 답은 원장 옆 줄 파일에 적힙니다. 자동조종의 답은
            # 원장 줄이 닫힌 뒤에 오고, 같은 번호로 줄을 하나 더 쓰면 화면과 보고서가 한 결정을
            # 두 번 셉니다. AUTOPILOT_LOG 로 자리를 옮길 수 있습니다.
            journal_path=os.getenv("AUTOPILOT_LOG")
            or str(Path(ledger_path).with_name("autopilot.jsonl")),
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
        # 공역을 시뮬레이터에서 받아 오는 런타임(서비스)은 다 받기 전에는 판정하지 않습니다. compose
        # 에서 기체가 런타임보다 먼저 신청해 빈 공역(판본 0)으로 네 경로가 승인됐고, 반쯤 받은
        # 공역(판본 14480)으로도 판정해 런타임 세계가 3분 안에 금지 공역 19건을 냈습니다. 빈 공역은
        # '규칙 없음' 이 아니라 '아직 모름' 입니다. 코드로 만든 런타임(시험·하네스)은 공역을 손으로
        # 넣으므로 기다리지 않습니다 — 서비스(main)만 켭니다.
        self.await_airspace = await_airspace
        self.airspace_loaded = False
        self.zone_volumes: set[str] = set()   # 공지로 들어온 구역. 끝나면 빼야 합니다
        # 신고 성능과 판의 시계. 승인한 경로가 언제 어디에 있을지(의도)와 NOTAM 의 시간 창을
        # 여기서 셉니다.
        self.performance = self.config.performance
        self.clock = Clock(self.performance.clock_epoch_z, self.performance.seconds_per_tick)
        self.intents = IntentRegistry()
        # 텔레메트리 심장박동. 떠 있는 기체의 기록이 신고한 timeout_ticks 동안 안 새로워지면
        # 링크 두절 — 그 기체의 의도(승인 경로 + 착륙 기둥)를 예약된 채로 두고 사람 카드를 올립니다.
        self.links = LinkWatch(self.performance.lost_link.timeout_ticks)
        self._dark: dict[str, Intent] = {}       # 두절 중 예약을 늘린 의도(복구 뒤 순응 검사에 씀)
        self._link_cards: dict[str, str] = {}    # 기체 → 서 있는 두절 카드(신청서 id)
        # 사람이 예약을 푼 두절 기체. 끊긴 순간에 멈춘 자리에 기둥(presence)도 세우지 않습니다.
        self._released: set[str] = set()
        # 심장박동은 세계 스레드가 옮기고 화면(HTTP 스레드)이 읽습니다.
        self._link_lock = threading.Lock()
        # 기체 프로세스가 알린 자기소개(무엇으로 신청서를 쓰나). 화면 라벨일 뿐 판정과 무관합니다.
        self.agents: dict[str, dict] = {}
        self.notices = NoticeBook(self.clock, self.llm)
        # 정보 수집(날씨·사고·제한). 시뮬레이터 공지·Tavily 검색·수동 입력이 같은 책으로 들어와
        # 문법 → 모델 → 코드 검사를 지나고, 날씨는 정책(이륙 정지)으로, 사고는 공지(구역)로 갑니다.
        self.gazetteer = Gazetteer(_load_addresses(ADDRESS_FILE),
                                   lookup=lambda bid: getattr(self.airspace.get(bid), "polygon",
                                                              None))
        self.intake = TowerIntake(self.clock, self.llm, self.config.weather, self.gazetteer)
        # 들어온 것의 기록(sqlite). 경로가 없으면 메모리 — 시험끼리 '본 것' 이 섞이지 않게. 서비스는
        # main() 이 INTAKE_DB(기본 .run/intake.sqlite)를 줍니다.
        self.store = IntakeStore(intake_db)
        self._rule_ids: dict[str, int] = {}     # 항목(규칙의 근거) id → rules 표의 줄 번호
        self.tavily = TavilyClient.from_env()
        # METAR. 키 없이 돕니다. 서비스(main)만 켭니다 — 클래스를 그냥 만들면(시험) 꺼져 있어서
        # 실제 네트워크를 부르지 않습니다. METAR=off 이거나 관측소가 없으면 None(출처 꺼짐).
        self.metar = MetarClient.from_env(self.config.intake.metar_stations) if metar else None
        self.metar_poller: MetarPoller | None = None
        self._metar_fetch: FetchStatus | None = None
        # starting(아직 한 번도 안 받음) | on | off. 닿지 못하면 off 로 한 줄, 다시 답하면 on 으로
        # 한 줄 — 바뀔 때만 적습니다. 주기마다 적으면 원장이 실패로 가득 찹니다.
        self.metar_status = "starting" if self.metar is not None else "off"
        self.metar_fetch: dict | None = None
        self.metar_last_fetch_tick: int | None = None
        # 마지막으로 받은 관측. 판이 바뀌면 새 판에 다시 넣습니다(_follow_round).
        self._metar_current: list[dict] = []
        self.intake_poller: IntakePoller | None = None
        self.intake_async = True
        self._reading_intake: set[str] = set()
        self._read_intake: list[tuple] = []
        self._intake_inbox: list[dict] = []     # 검색·수동 입력이 놓고 간 항목. 세계 스레드가 읽음
        # 재시작 전에 사람을 기다리던 항목. 카드는 프로세스와 함께 사라졌으니 다시 읽어 카드를
        # 다시 올립니다 — 안 그러면 아무도 답한 적 없는 보고서가 '본 것' 으로 남아 영영 안 읽힙니다.
        waiting = self.store.reopen_waiting()
        # 사전 브리핑(Tavily). 서비스(main)만 켭니다 — 코드로 만든 런타임(시험·하네스)은 METAR
        # 처럼 꺼져 있습니다. 브리핑이 읽어 둔 것은 여기서 받습니다: 기다리던 카드는 다시
        # 올리고(다시 읽지 않고), 걸려 있던 규칙은 기록에서 되읽어 다음 판에 그대로 겁니다.
        self.briefing = BriefingDesk(self, config_path, enabled=briefing)
        self._intake_inbox.extend(self.briefing.adopt_waiting(waiting))
        self._intake_fetch: FetchStatus | None = None   # 검색 스레드의 마지막 주기 상태
        # 관제 권고. 연속 거절을 세고, 코드가 만든 선택지를 판정으로 확인해 원장에 남깁니다.
        # 모델이 문구를 쓸 때는 따로 스레드에서 — 거절 답장이 모델을 기다리면 운영사가 멈춥니다.
        self.advisor = AdvisoryDesk(self.llm)
        self.advisory_async = True
        # 문법 밖의 공지를 모델이 읽는 일도 세계 스레드 밖에서(시험은 False 로 두고 바로 봅니다).
        self.notice_async = True
        self._reading: set[str] = set()       # 모델이 읽는 중인 공지 id
        self._read_notices: list[tuple] = []  # 읽기 스레드가 놓고 간 (판, 공지, 결과)
        # 공지 적용은 세계 스레드와 승인(HTTP) 스레드가 같이 부릅니다.
        self._notice_lock = threading.Lock()
        self._round = None                    # 시뮬레이터가 판을 새로 시작하면 따라갑니다
        self._contended: dict[str, list[tuple[Proposal, Decision, float]]] = {}
        self._awaiting_human: dict[str, Proposal] = {}
        # 사람 카드(승인 대기) 신청서 id → 열어 둔 원장 항목. 사람의 답·창의 끝·판의 끝이 닫습니다.
        self._open_cards: dict[str, object] = {}
        self._decisions: dict[str, Decision] = {}
        self._checks: dict[str, list[str]] = {}   # 신청서 id → 지금까지 돈 검사 이름
        # 벽시계가 아니라 세계의 시계로 셉니다. 그래야 재현이 됩니다.
        self._recent_commits: dict[tuple[str, str], int] = {}
        self.dedupe_ticks = int(os.getenv("DEDUPE_TICKS", "15"))
        self._guard = threading.Lock()
        # 판정에서 의도 등록까지 한 번에 하나. HTTP 처리 스레드마다 file() 이 따로 돌아,
        # 0.2초 간격으로 온 두 신청이 서로의 의도가 등록되기 전에(_on_committed) 교차 판정을
        # 지나 둘 다 승인됐습니다 — 실주행(규칙 모드)에서 회수된 두 기체가 같은 A* 회랑을 다시
        # 내 런타임 쪽 분리 상실이 둘. 잡는 순서는 늘 _judging → _guard 입니다(_guard 를 쥔 채
        # 이것을 잡는 곳은 없습니다). 세계 스레드(회수·공지)는 이것을 잡지 않습니다 — 느린
        # 조종장치 명령 뒤에 시계가 서면 안 되고, 회수 도중 의도가 빈 떠 있는 기체는 _others 가
        # 텔레메트리로 세우는 자리(presence)가 막습니다.
        self._judging = threading.RLock()

    # ---------- 신청 접수 ----------

    @property
    def ready(self) -> bool:
        """판정할 준비가 됐나. 공역을 기다리는 서비스는 다 받은 뒤부터, 나머지는 처음부터."""
        return self.airspace_loaded or not self.await_airspace

    def file(self, raw: dict) -> Decision:
        """신청 하나를 판정하고, 되면 실행합니다. 판정에서 의도 등록까지 한 번에 하나(_judging)."""
        with self._judging:
            return self._judge_and_commit(raw)

    def _judge_and_commit(self, raw: dict) -> Decision:
        proposal = Proposal.from_dict({**raw, "world": "guarded"})
        asset = self.telemetry.get(proposal.asset_id, {})
        # 이 접수의 검사 목록. 운영사가 같은 id 로 다시 내면(직선 → 재작성) 새로 셉니다 — 원장
        # 한 줄은 한 번의 접수를 말해야 합니다. 나중의 재판정(rejudge)은 이 목록 뒤에 덧붙습니다.
        checks = self._checks[proposal.id] = []
        self._observe()

        if not self.ready:
            # 판정 이전의 문. 공역을 다 받기 전에는 아무것도 승인하지 않습니다. policy_hit 은
            # 비워 둡니다 — 운영사가 이것을 '행동이 금지됐다' 로 배우면 공역이 온 뒤에도 그
            # 행동을 안 냅니다.
            checks.append("airspace_loaded")
            return self._deny(proposal, Decision(
                proposal.id, Verdict.DENIED,
                "런타임이 아직 공역을 다 받지 못했습니다 — 판정할 수 없어 거절, 잠시 뒤 다시",
                code="airspace_not_loaded",
                detail={"airspace_revision": self.airspace.revision}))

        checks.append("dedupe")
        seen_at = self._recent_commits.get((proposal.asset_id, proposal.action))
        if seen_at is not None and self.tick - seen_at < self.dedupe_ticks:
            # 같은 신청이 연달아 오면 한 번만 나갑니다. 아니면 중복 청구가 됩니다.
            # 이것도 판정이라 원장에 남습니다 — 안 남기면 "왜 그 신청은 답이 없었나" 를 못 답합니다.
            decision = Decision(proposal.id, Verdict.DENIED, "직전에 같은 신청이 실행됐습니다",
                                code="duplicate")
            return self._deny(proposal, decision)

        if self.links.lost(proposal.asset_id):
            # 링크가 끊긴 기체는 명령을 못 듣습니다. 판정 이전의 문 — 통과한 신청에는 적지 않고
            # 거절할 때만 검사 목록에 남깁니다.
            checks.append("link")
            return self._deny(proposal, self._dark_denial(proposal))
        blocked = self.check_route(proposal, checks)
        if blocked:
            return self._deny(proposal, self._airspace_denial(proposal, blocked))
        unknown = self._contingency_problem(proposal, checks)
        if unknown:
            return self._deny(proposal, unknown)
        blocked = self._check_traffic(proposal, checks)
        if blocked:
            return self._deny(proposal, self._traffic_denial(proposal, blocked))

        checks.append("authority")
        decision = self.authority.evaluate(proposal, asset, self.tick)
        self._decisions[proposal.id] = decision

        if decision.verdict is Verdict.DENIED:
            return self._deny(proposal, decision)
        if decision.verdict is Verdict.HUMAN:
            return self._park_for_human(proposal, decision)
        return self._queue_or_commit(proposal, decision)

    def _park_for_human(self, proposal: Proposal, decision: Decision) -> Decision:
        """사람 카드를 올립니다. 원장 항목은 열어 두고 사람의 답(또는 판의 끝)이 닫습니다.

        실주행에서 기체 하나가 한도를 넘긴 뒤 'human' 답을 309번 받았는데 원장에는 한 줄도 없었고,
        카드는 판이 바뀌어도 남았습니다. 같은 카드가 이미 있으면 그 결정을 그대로 돌려주되
        (승인 화면에 같은 카드를 쌓지 않습니다), 그것도 판정이라 한 줄은 남깁니다(outcome waiting).
        """
        with self._guard:
            existing = next((waiting for waiting in self._awaiting_human.values()
                             if waiting.asset_id == proposal.asset_id
                             and waiting.action == proposal.action), None)
            if existing is None:
                self._awaiting_human[proposal.id] = proposal
        if existing is not None:
            repeat = Decision(proposal.id, Verdict.HUMAN,
                              f"같은 카드({existing.id})가 이미 승인 대기 중 — {decision.reason}",
                              authority_hit=decision.authority_hit, code=decision.code,
                              detail={**decision.detail, "waiting_on": existing.id})
            self.ledger.close_entry(
                self.ledger.open_entry(proposal, repeat, self._context(proposal)), "waiting")
            self._checks.pop(proposal.id, None)
            return self._decisions[existing.id]
        self._open_cards[proposal.id] = self.ledger.open_entry(proposal, decision,
                                                              self._context(proposal))
        return decision

    def _close_card(self, card, proposal: Proposal, decision: Decision, outcome: str,
                    check: str = "human") -> None:
        """열어 둔 카드 항목을 닫습니다. 항목이 없으면(있어서는 안 되지만) 한 쌍을 새로 적습니다."""
        if card is None:
            card = self.ledger.open_entry(proposal, decision, self._context(proposal))
        checks = list(card.context.get("checks_run") or []) + [check]
        self.ledger.close_entry(card, outcome, decision, {"tick": self.tick, "checks_run": checks})

    def _deny(self, proposal: Proposal, decision: Decision) -> Decision:
        self._decisions[proposal.id] = decision
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
        self._checks.pop(proposal.id, None)
        self._record_refusal(proposal, decision)
        return decision

    # ---------- 관제 권고 ----------

    def _record_refusal(self, proposal: Proposal, decision: Decision) -> None:
        """경로 신청의 거절 하나. 세 번 연속이면 권고를 씁니다.

        중복 거절은 세지 않습니다 — 길이 막힌 게 아니라 같은 신청을 두 번 낸 것입니다.
        """
        if proposal.action not in ROUTED or decision.code in NOT_REFUSALS:
            return
        params = proposal.params or {}
        refusal = Refusal(
            asset=proposal.asset_id, tick=self.tick, code=decision.code,
            policy_hit=decision.policy_hit,
            blocked_kind=params.get("blocked_kind"), blocked_volume=params.get("blocked_volume"),
            blocked_asset=params.get("blocked_asset"),
            blocked_until_tick=params.get("blocked_until_tick"), proposal_id=proposal.id,
            action=proposal.action, legs=list(params.get("legs") or []),
            params={k: v for k, v in params.items()
                    if k != "legs" and not k.startswith("blocked_")},
            resource=proposal.resource,
        )
        if self.advisor.refused(proposal.asset_id, refusal):
            self._advise(proposal.asset_id, "refusals")

    def _judge_legs(self, refusal: Refusal, legs: list[dict]) -> str | None:
        """이 경로가 지금 막히는 이유. 권고의 선택지를 확인할 뿐, 아무것도 접수하지 않습니다."""
        probe = Proposal(asset_id=refusal.asset, action=refusal.action, cost_usd=0.0,
                         blast_radius="none", rationale="advisory probe",
                         params={**refusal.params, "legs": legs},
                         resource=refusal.resource or refusal.params.get("pad"))
        checks: list[str] = []
        return self.check_route(probe, checks) or self._check_traffic(probe, checks)

    def _notice_until(self, volume_id: str | None) -> int | None:
        """그 구역이 공지라면 닫히는 틱. 상시 구역·건물이면 None."""
        record = self.notices.get(volume_id or "")
        return record.until_tick if record is not None else None

    def _advise(self, asset: str, trigger: str) -> None:
        """권고 하나를 씁니다. 선택지는 지금 상태로 판정하고, 문구는(모델이 있으면) 따로 스레드에서.
        """
        refusals = self.advisor.streak(asset)
        if not refusals:
            return
        airborne = float(self.telemetry.get(asset, {}).get("alt_m") or 0.0) > 1.0
        options = build_options(refusals, self._judge_legs, self._notice_until, airborne)
        context = self._context(None, ["advisory"])
        round_at = self._round

        def finish() -> None:
            params = self.advisor.compose(asset, trigger, refusals, options, airborne)
            if round_at != self._round:
                return      # 모델이 답하는 사이 판이 바뀌었습니다. 지난 판의 권고는 적지 않습니다
            self._ledger_advisory(asset, params, context)

        if self.advisor.has_model and self.advisory_async:
            threading.Thread(target=finish, daemon=True, name=f"advisory-{asset}").start()
        else:
            finish()

    def _ledger_advisory(self, asset: str, params: dict, context: dict) -> None:
        """권고는 원장 항목입니다(action advisory, outcome noted). 실행은 없습니다."""
        noted = Proposal(asset_id=asset, action="advisory", cost_usd=0.0, blast_radius="none",
                         author="runtime", rationale=params["summary"][:180], params=params)
        decision = Decision(noted.id, Verdict.AUTO, params["summary"], code="advisory",
                            detail={"resource": asset, "chosen": params["chosen"],
                                    "trigger": params["trigger"], "source": params["source"]})
        entry = self.ledger.open_entry(noted, decision, context)
        self.ledger.close_entry(entry, "noted")
        with self._guard:
            self.advisor.latest[asset] = {"asset": asset, "tick": context.get("tick"),
                                          "at": entry.at, "ledger_id": entry.id, **params}

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

    def _dark_denial(self, proposal: Proposal) -> Decision:
        """링크가 끊긴 기체의 신청. 명령이 닿지 않으니 승인해도 실행할 수 없습니다.

        policy_hit 은 비워 둡니다 — 운영사가 이것을 '행동이 금지됐다' 로 배우면 링크가 돌아온 뒤에도
        그 행동을 영영 안 냅니다. 링크가 돌아오면 같은 신청이 그대로 판정받습니다.
        """
        link = self.links.links[proposal.asset_id]
        return Decision(proposal.id, Verdict.DENIED,
                        f"{proposal.asset_id} 의 링크가 틱 {link.since_tick} 부터 끊겨 있습니다 — "
                        "기체가 명령을 들을 수 없습니다", code="lost_link_refused",
                        detail={"resource": proposal.asset_id, "since_tick": link.since_tick,
                                "last_seen_tick": link.last_seen_tick})

    def _contingency_problem(self, proposal: Proposal, checks: list[str]) -> Decision | None:
        """통신 두절 대비 부피를 판정합니다. 모르는 대비 행동이면 거절.

        continue_and_land 의 대비 부피는 승인 경로 그 자체 + 목적지의 착륙 기둥이라, 방금 지난
        route·columns·landing 검사가 곧 그 판정입니다 — 여기서는 신고한 행동이 그것인지를 봅니다.
        return_to_launch 같은 다른 행동은 되돌아가는 직선이라는 딴 부피를 만들고, 그 부피를 판정하지
        않은 채 승인하면 링크가 끊긴 순간 판정 안 된 길을 날게 됩니다.
        """
        if proposal.action not in ROUTED or not proposal.params.get("legs"):
            return None
        checks.append("contingency")
        lost_link = self.performance.lost_link
        if lost_link.known:
            return None
        known = ", ".join(config_module.KNOWN_LOST_LINK_BEHAVIOURS)
        return Decision(proposal.id, Verdict.DENIED,
                        f"통신 두절 대비 행동 {lost_link.behaviour!r} 의 부피를 판정할 수 없습니다 "
                        f"(아는 것: {known})", policy_hit="lost_link",
                        code="contingency_unknown",
                        detail={"behaviour": lost_link.behaviour,
                                "known": list(config_module.KNOWN_LOST_LINK_BEHAVIOURS)})

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
            contingency=self.performance.lost_link.behaviour,
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
            # 링크가 끊긴 기체의 의도는 물릴 수 없습니다 — 물림은 조종장치에 가는 명령이고, 그
            # 기체는 듣지 못합니다. 그때는 이 신청을 거절합니다.
            if (not airborne or conflict.asset in withdraw or other is None
                    or other.state != ACCEPTED or other.id != conflict.intent_id
                    or self.links.lost(conflict.asset)):
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
            if other == asset or other in covered or other in exclude or other in self._released:
                # 사람이 예약을 푼 두절 기체의 텔레메트리는 끊긴 순간에 멈춘 자리입니다. 기체는
                # 거기 없고, 사람이 그것을 알고 풀었습니다.
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
            self.advisor.succeeded(proposal.asset_id)   # 승인이 나갔으니 연속 거절은 끊깁니다
            # 이 판에 아직 안 물어본 동네로 들어가는 회랑이면 사전 브리핑이 그 칸을 묻습니다
            # (다음 폴링, 작업 스레드). 여기서는 칸만 적습니다 — 승인 스레드는 네트워크를
            # 기다리지 않습니다.
            self.briefing.corridor_cleared(proposal.params["legs"], proposal.asset_id)
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
        if proposal.action == "decline_job":
            # 거절 뒤의 반려("규정상 경로 없음"). 반려가 실행된 뒤에 씁니다 — 접수 때 쓰면 중복으로
            # 거절된 반려에도 권고가 하나 더 적힙니다. 주문이 사라졌으니 연속 거절도 끊습니다.
            if self.advisor.declined(proposal.asset_id):
                self._advise(proposal.asset_id, "decline_after_refusals")
            self.advisor.succeeded(proposal.asset_id)
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
        fresh = self._fresh_refusal(proposal, checks)
        if fresh is None:
            return None
        decision.verdict = Verdict.DENIED
        decision.reason = fresh.reason
        decision.policy_hit = fresh.policy_hit
        decision.forbids = fresh.forbids
        decision.code = fresh.code
        decision.detail = fresh.detail
        self.ledger.close_entry(
            self.ledger.open_entry(proposal, decision, self._context(proposal)), "denied")
        self._record_refusal(proposal, decision)
        return decision.reason

    def _fresh_refusal(self, proposal: Proposal, checks: list[str]) -> Decision | None:
        """실행 직전에 거절할 이유. 없으면 None. 링크 → 공역 → 교차 → 정책 순서입니다.

        사람 승인·중재 배정을 기다리는 사이 링크가 끊겼으면, 그 승인이 들을 수 없는 기체에 명령을
        보내게 하지 못합니다. 보냈더니 명령을 받았다고 답하는 어댑터(MAVLink 는 보내면 ok)에서는 새
        경로가 두절 예약을 끝냈고, 그 기체의 실제 남은 길로 남의 교차가 승인됐습니다. 금지(기상 대기
        ·감항성 지시)도 기다리는 사이 왔을 수 있어 접수 때와 같은 정책 검사를 한 번 더 합니다.
        """
        if self.links.lost(proposal.asset_id):
            checks.append("link")
            return self._dark_denial(proposal)
        blocked = self.check_route(proposal, checks)
        if blocked:
            return self._airspace_denial(proposal, blocked)
        traffic = self._check_traffic(proposal, checks)
        if traffic:
            return self._traffic_denial(proposal, traffic)
        checks.append("policy")
        banned = self.policies.hit(proposal.action, proposal.resource,
                                   self.telemetry.get(proposal.asset_id, {}), self.tick)
        return AuthorityCheck.policy_denial(proposal, banned) if banned else None

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
        # 사람의 답도 판정 줄에 섭니다 — 승인은 재판정·실행·의도 등록으로 이어집니다.
        with self._judging:
            return self._answer_card(proposal, proposal_id, actor, allow)

    def _answer_card(self, proposal: Proposal, proposal_id: str, actor: str,
                     allow: bool) -> Decision:
        decision = self._decisions[proposal_id]
        decision.approved_by = actor
        card = self._open_cards.pop(proposal_id, None)
        if proposal.action == "publish_notice":
            return self._confirm_notice(proposal, decision, actor, allow, card)
        if proposal.action == "publish_weather":
            return self._confirm_weather(proposal, decision, actor, allow, card)
        if proposal.action == "lift_weather_hold":
            return self._confirm_lift(proposal, decision, actor, allow, card)
        if proposal.action == "lost_link_notice":
            return self._confirm_lost_link(proposal, decision, actor, allow, card)
        if not allow:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} 가 거부했습니다"
            self._close_card(card, proposal, decision, "denied")
            return decision
        decision.verdict = Verdict.AUTO
        decision.reason = f"{actor} 가 승인했습니다"
        # 카드 줄은 사람의 답으로 닫힙니다. 그 다음의 재판정·실행은 자기 줄을 따로 남깁니다.
        self._close_card(card, proposal, decision, "approved")
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

        if batches:
            with self._judging:
                self._settle_batches(batches)

    def _settle_batches(self, batches: dict) -> None:
        """줄 선 자원의 배정. 재판정·실행·의도 등록이라 신청(file)과 같은 줄(_judging)에 섭니다."""
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
                    self._record_refusal(proposal, decision)
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
                    self._record_refusal(proposal, decision)

    # ---------- 바깥에서 오는 소식 ----------

    def _pull_world(self) -> None:
        state = self.adapter.telemetry()
        if state:
            self.tick = state.get("tick", self.tick)
            self.telemetry = state.get("assets", {})
            self._follow_round(state.get("round"))
            self._observe()
            self.watch_links()

        if not self.airspace_loaded:
            self._load_airspace()

        bulletins = get_json(f"{self.sim_url}/bulletins?world=guarded") or {}
        self.absorb(bulletins.get("bulletins", []))

    def _load_airspace(self) -> None:
        """시뮬레이터의 공역을 한 번에 받습니다. 다 받은 뒤 한꺼번에 넣고, 그때부터 판정합니다.

        받지 못했거나 비어 있으면 다음 폴링에 다시 봅니다. 하나씩 넣던 때는 HTTP 스레드가 반쯤 찬
        공역으로 판정했습니다(Airspace.add_all 이 바꿔 끼우기 한 번으로 넣습니다).
        """
        world = get_json(f"{self.sim_url}/state?world=guarded&volumes=1",
                         timeout=AIRSPACE_FETCH_TIMEOUT_S) or {}
        volumes = [Volume.from_dict(raw) for raw in world.get("volumes") or []]
        if not volumes:
            return
        self.pad_coords = {name: (at["lat"], at["lon"])
                           for name, at in (world.get("pad_coords") or {}).items()}
        self.landing_areas = list(world.get("landing_areas") or [])
        self.airspace.add_all(volumes)
        self.airspace_loaded = True

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
        # 링크도 한 판짜리입니다 — 새 판의 기체는 새로 세워졌고 도장도 처음부터 셉니다. 서 있던 두절
        # 카드는 아래 _expire_cards 가 다른 카드와 같이 내립니다.
        with self._link_lock:
            self.links.clear()
        self._dark.clear()
        self._link_cards.clear()
        self._released.clear()
        # 기체 등록은 판을 넘어 남습니다(프로세스는 그대로). 틱만 새 판의 시계로 옮깁니다.
        with self._guard:
            for row in self.agents.values():
                row["last_seen_tick"] = min(int(row["last_seen_tick"]), self.tick)
        self._expire_cards("판이 바뀜")
        self.notices.clear()
        # 열려 있던 기상 대기는 닫는 줄을 남깁니다. 없으면 보고서가 그 대기를 영원히 '열림' 으로
        # 적습니다.
        if self.intake.hold is not None:
            self._ledger_hold_end(self.intake.hold, "weather_hold_closed",
                                  f"판이 바뀌어 닫힘 (창은 틱 {self.intake.hold.until_tick} 까지)")
        self._close_rules("round")
        self.intake.clear()
        # METAR 는 지금 유효한 관측입니다. 판이 바뀌었다고 돌풍이 멎지 않으니 마지막 관측을 새 판의
        # 첫 폴링에 다시 넣습니다 — 다음 주기(최대 METAR_PERIOD_S 뒤)를 기다리면 그 사이에 새 판의
        # 기체가 돌풍 속으로 뜹니다.
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in self._metar_current)
        with self._guard:
            self.advisor.clear()

    def _expire_cards(self, why: str) -> None:
        """판이 끝나면 사람 카드도 내립니다. 보류 공지는 lapsed 로, 나머지도 판이 바뀌었다고.

        실주행에서 지난 판의 카드 다섯이 판이 바뀐 뒤에도 남아 있었습니다 — 새 판의 기체에 지난 판의
        한도 초과 카드를 승인하는 일은 없어야 합니다.
        """
        for record in [r for r in list(self.notices.records.values()) if r.held]:
            self._lapse_held(record, why)
        with self._guard:
            cards = list(self._awaiting_human.items())
            self._awaiting_human.clear()
        for proposal_id, proposal in cards:
            decision = self._decisions.get(proposal_id) or Decision(proposal_id, Verdict.HUMAN, "")
            decision.verdict = Verdict.DENIED
            decision.reason = f"사람이 보기 전에 {why} — 카드를 내림"
            decision.code = "card_lapsed"
            self._close_card(self._open_cards.pop(proposal_id, None), proposal, decision, "lapsed")

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
        self._collect_read_notices()
        known = {p.id for p in self.policies.all()}
        bbox = self.service_bbox()
        for item in items:
            if item["id"] in known:
                continue
            if item.get("kind") == "recall":
                self._enforce_policy(item)
                continue
            if self.notices.known(item["id"]) or item["id"] in self._reading:
                continue
            if self.notice_async and self.notices.needs_model(item):
                self._read_later(item, bbox)
                continue
            self._settle_notice(item, self.notices.read(item, bbox))
        # 정보 수집. 날씨·사고 공지와 검색·수동 입력이 같은 길을 갑니다. 사고는 공지 기록이 되어
        # 아래 _apply_notices 가 같은 폴링에 겁니다.
        self._collect_read_intake()
        self._note_fetch()
        self._note_metar_fetch()
        self._take_intake([i for i in bulletins if _is_intake(i)] + self._drain_intake_inbox(),
                          bbox)
        # 사전 브리핑. 끝난 실행을 규칙으로 옮기고(공지 책으로 — 아래 _apply_notices 가 같은
        # 폴링에 겁니다), 판이 바뀌었거나 새 동네가 쌓였으면 작업 스레드에 새로 묻게 합니다.
        self.briefing.poll(bbox)
        self._apply_notices({i["id"] for i in items} | self.intake.notice_ids)
        self._tick_intake()

    def _settle_notice(self, item: dict, record) -> None:
        if record is None:
            self._ledger_notice(item, "unreadable", self.notices.unreadable.get(item["id"], ""))
        elif record.held:
            self._hold_notice(item, record)

    def _read_later(self, item: dict, bbox) -> None:
        """문법 밖의 공지는 모델이 읽습니다 — 세계 스레드 밖에서. 답은 다음 폴링이 거둡니다.

        같은 스레드에서 물었더니 30B 대역이 답하는 5~27초 동안 런타임의 틱·텔레메트리가 멈췄고
        (그 사이 들어온 신청은 옛 위치로 판정됐습니다), 20초 타임아웃에 넉 판 중 세 판을 못 읽음.
        같은 id 는 한 번에 하나만 묻습니다(_reading).
        """
        self._reading.add(item["id"])
        round_at = self._round

        def work() -> None:
            try:
                result = self.notices.compile_item(item, bbox)
            except Exception as error:  # noqa: BLE001 — 못 읽은 것으로 적습니다
                result = (None, f"모델 읽기 실패 {error!r}")
            with self._guard:
                self._read_notices.append((round_at, item, result))

        threading.Thread(target=work, daemon=True, name=f"notice-{item['id']}").start()

    def _collect_read_notices(self) -> None:
        """읽기 스레드가 놓고 간 결과를 세계 스레드에서 적습니다. 판이 바뀐 뒤의 답은 버립니다."""
        with self._guard:
            arrived, self._read_notices = self._read_notices, []
        for round_at, item, result in arrived:
            self._reading.discard(item["id"])
            if round_at != self._round or self.notices.known(item["id"]):
                continue
            self._settle_notice(item, self.notices.settle(item, result))

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
        """창이 열린 공지는 공역에 넣고 날던 경로를 회수하고, 닫힌 공지는 뺍니다.

        세계 스레드(폴링)와 승인 스레드(사람 확인)가 같이 부릅니다. 잠그지 않으면 둘이 같은 공지를
        due() 에서 같이 집어 두 번 걸고 같은 기체를 두 번 회수합니다.
        """
        with self._notice_lock:
            self._apply_notices_locked(feed_ids)

    def _apply_notices_locked(self, feed_ids: set[str] | None) -> None:
        for record in self.notices.due(self.tick):
            record.applied = True
            self._rule_apply(record.id)
            self.airspace.add(record.volume)
            self.zone_volumes.add(record.id)
            self.recall_flights(record.volume)
            if record.id not in {p.id for p in self.policies.all()}:
                self.policies.add(config_module.Policy(
                    id=record.id, reason=record.volume.reason or record.name,
                    active_from_tick=record.from_tick or 0, active_until_tick=record.until_tick))
            if record.kind == "incident":
                self._ledger_incident(record)
        feed = feed_ids if feed_ids is not None else {r.id for r in self.notices.records.values()}
        for record in self.notices.lapsed(self.tick, feed):
            record.applied = False
            self._rule_close(record.id, "window", self.tick)
            self.airspace.remove(record.id)
            self.zone_volumes.discard(record.id)
            if record.id not in feed:
                self.notices.forget(record.id)
        for expired in self.zone_volumes - {r.id for r in self.notices.records.values()
                                            if r.applied}:
            self.airspace.remove(expired)
            self.zone_volumes.discard(expired)
        # 사람을 기다리다 창이 닫힌(또는 목록에서 빠진) 공지. 카드와 배너를 내리고 원장에 남깁니다.
        for record in self.notices.stale_held(self.tick, feed):
            self._lapse_held(record, "창이 닫힘" if record.id in feed else "공지가 내려감")
        # 사람이 확인했지만 걸리기 전에 지나간 것. 남겨 두면 /state.notices 에 판 끝까지 남습니다.
        for record in self.notices.stale_confirmed(self.tick, feed):
            self.notices.forget(record.id, "확인 뒤 걸리기 전에 "
                                + ("창이 닫힘" if record.id in feed else "공지가 내려감"))

    def _lapse_held(self, record, why: str) -> None:
        """확인 없이 지나간 보류 공지. 걸린 적이 없으니 뺄 것도 없고, 기록만 닫습니다."""
        self.notices.forget(record.id, f"확인 전에 {why}")
        self._rule_close(record.id, "lapsed")
        with self._guard:
            waiting = next((pid for pid, p in self._awaiting_human.items()
                            if p.action == "publish_notice"
                            and p.params.get("notice_id") == record.id), None)
            if waiting is not None:
                self._awaiting_human.pop(waiting)
        entry = self._open_cards.pop(waiting, None) if waiting is not None else None
        if entry is None:
            return
        decision = self._decisions[waiting]
        decision.verdict = Verdict.DENIED
        decision.reason = f"사람이 확인하기 전에 {why} — 걸린 적 없음"
        decision.code = "notice_lapsed"
        self.ledger.close_entry(entry, "lapsed", decision, {"tick": self.tick})

    def _hold_notice(self, item: dict, record, why: str | None = None) -> None:
        """보류 공지를 승인 화면에 올립니다. 사람이 승인하기 전에는 아무것도 안 막습니다.

        모델이 구조화한 공지, 그리고 관제탑 피드 밖(검색·수동 입력)에서 온 공지가 여기로 옵니다.
        보류된 공지는 신청서 모양(action publish_notice)이라 기존 승인 화면이 그대로 보여 줍니다."""
        held = Proposal(
            asset_id="airspace", action="publish_notice", cost_usd=0.0, blast_radius="none",
            rationale=f"{record.name} — {record.text}"[:180], author=record.source,
            params={"notice_id": record.id, "text": record.text, "notice": record.to_dict(),
                    "source": record.source},
        )
        decision = Decision(held.id, Verdict.HUMAN,
                            why or "모델이 읽은 공지는 사람이 확인해야 걸립니다",
                            authority_hit="model_notice", code="human_notice",
                            detail={"notice": record.id, "source": record.source})
        self._decisions[held.id] = decision
        with self._guard:
            self._awaiting_human[held.id] = held
        self._open_cards[held.id] = self.ledger.open_entry(
            held, decision, self._context(None, ["notice:grammar", "notice:model"]))

    def _confirm_notice(self, proposal: Proposal, decision: Decision, actor: str,
                        allow: bool, card=None) -> Decision:
        """사람의 답. 보류할 때 열어 둔 원장 항목(card)을 그 답으로 닫습니다.

        창이 이미 닫힌 공지를 승인하면 걸 것이 없습니다 — 그때는 '걸렸다'(notice_published)가 아니라
        notice_lapsed 로 적습니다. 실주행에서는 폴링 한 번 사이의 경주입니다.
        """
        notice_id = proposal.params.get("notice_id", "")
        record = self.notices.get(notice_id)
        if allow and record is not None and record.until_tick is not None \
                and self.tick > record.until_tick:
            self.notices.forget(notice_id, "창이 닫힌 뒤에 확인")
            self._rule_close(notice_id, "lapsed")
            decision.verdict = Verdict.DENIED
            decision.reason = (f"{actor} 가 확인했지만 창이 틱 {record.until_tick} 에 이미 "
                               "닫혔습니다 — 걸린 적 없음")
            decision.code = "notice_lapsed"
            self._close_card(card, proposal, decision, "lapsed", "notice:human")
            return decision
        record = self.notices.confirm(notice_id, actor, allow)
        if allow and record is not None:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 공지를 확인했습니다"
            decision.code = "notice_published"
        else:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} 가 공지를 거부했습니다" if record is not None \
                else "그런 공지가 없습니다"
            decision.code = "notice_refused"
            self._rule_close(notice_id, "refused")
        self._close_card(card, proposal, decision,
                         "done" if allow and record is not None else "denied", "notice:human")
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

    # ---------- 정보 수집: 날씨·사고·제한 ----------

    def take_in(self, items: list[dict], status: FetchStatus | None = None) -> None:
        """검색 스레드가 결과와 주기 상태를 놓고 갑니다. 여기서는 적지도 읽지도 않습니다 — 다음
        폴링이. last_fetch_tick 은 성공한 주기만 앞당깁니다. 실패한 빈 주기로 앞당기면 화면이
        '방금 물었고 아무것도 없었다' 로 읽습니다."""
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in items)
            if status is not None:
                self._intake_fetch = status
            if status is None or status.ok:
                self.intake.last_fetch_tick = self.tick

    def _note_fetch(self) -> None:
        """검색 출처의 실패↔회복. 바뀔 때 한 줄씩만 — 주기마다 적으면 원장이 실패로 가득 찹니다."""
        with self._guard:
            status, self._intake_fetch = self._intake_fetch, None
        if status is None:
            return
        self.intake.fetch = status.to_dict()
        failed_now = not status.ok
        if failed_now == self.intake.source_failed:
            return
        self.intake.source_failed = failed_now
        self._ledger_source_change("tavily", status, failed_now)

    def _ledger_source_change(self, source: str, status: FetchStatus, failed_now: bool) -> None:
        """출처 하나의 실패 ↔ 회복 한 줄. 검색(tavily)과 METAR 가 같은 줄을 씁니다."""
        name = SOURCE_NAMES.get(source, source)
        noted = Proposal(asset_id=INTAKE_ASSET, action="intake_source", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"{source} · {status.error or 'ok'}"[:180],
                         params={"source": source, **status.to_dict()})
        decision = Decision(
            noted.id, Verdict.DENIED if failed_now else Verdict.AUTO,
            (f"{name} 출처에 닿지 못합니다 — {status.error}" if failed_now
             else f"{name} 출처가 다시 답합니다 (실패 {status.failures} 회 뒤)"),
            code="intake_source_failed" if failed_now else "intake_source_recovered",
            detail={"source": source, **status.to_dict()})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, ["intake:source"])),
            "failed" if failed_now else "noted")

    def take_metar(self, items: list[dict], status: FetchStatus | None = None) -> None:
        """METAR 스레드가 관측과 주기 상태를 놓고 갑니다. 적고 읽는 것은 다음 폴링(세계 스레드)."""
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in items)
            if status is not None:
                self._metar_fetch = status
            if status is None or status.ok:
                self.metar_last_fetch_tick = self.tick
                # 실패한 주기는 지난 관측을 지우지 않습니다 — 돌풍 대기를 푸는 것은 창이나
                # 사람이지, 망이 끊긴 탓이 아닙니다.
                self._metar_current = [dict(item) for item in items]

    def _note_metar_fetch(self) -> None:
        """METAR 출처의 on ↔ off. 닿지 못하면 off 로 한 줄, 다시 답하면 on 으로 한 줄."""
        with self._guard:
            status, self._metar_fetch = self._metar_fetch, None
        if status is None:
            return
        self.metar_fetch = status.to_dict()
        now = "on" if status.ok else "off"
        was, self.metar_status = self.metar_status, now
        if now == was or (was == "starting" and status.ok):
            return      # 첫 성공은 적을 일이 아닙니다 — 켜진 채 시작한 것입니다
        self._ledger_source_change(METAR_SOURCE, status, failed_now=not status.ok)

    def submit_intake(self, body: dict):
        """POST /intake. 사람이 넣은 문장 하나. 접수함에 넣고 id 를 돌려줍니다.

        id 는 manual- 로 시작합니다. 부른 쪽이 시뮬레이터 공지의 id 를 쓰면 그 공지가 '이미 본 것'
        이 되어 진짜 돌풍 보고서가 안 읽힙니다. 힌트(radius_m·until_tick)는 여기서 수인지 봅니다 —
        세계 스레드에서 터지면 그 폴링의 나머지 항목까지 잃습니다."""
        text = " ".join(str(body.get("text") or "").split())[:2000]
        if not text:
            return 400, {"error": "text 가 비어 있습니다"}
        hints, problem = _intake_hints(body)
        if problem:
            return 400, {"error": problem}
        kind = body.get("kind") if body.get("kind") in ("weather", "incident", "notam") else None
        given = "".join(str(body.get("id") or "").split())[:80]
        item = {"id": f"manual-{given}" if given else item_id({"text": text, "source": "manual"}),
                "kind": kind, "text": text, "source": "manual", **hints}
        self.take_in([item])
        return 200, {"ok": True, "id": item["id"], "queued": True}

    def _drain_intake_inbox(self) -> list[dict]:
        with self._guard:
            arrived, self._intake_inbox = self._intake_inbox, []
        return arrived

    def _take_intake(self, items: list[dict], bbox) -> None:
        """항목마다 한 번: 적고(intake_received) 읽습니다. 문법이면 지금, 모델이면 딴 스레드."""
        for item in items:
            key = item_id(item)
            if not self.intake.known(key) and self.store.seen(key, str(item.get("source") or "")):
                # 재시작 전에 읽은 검색 결과·수동 입력. 사람 카드를 다시 올리지 않습니다.
                continue
            record = self.intake.receive(item, self.tick)
            if record is None:
                continue        # 본 것입니다. 다시 읽지도 다시 적지도 않습니다
            received = ("재시작 전에 사람을 기다리던 것 — 카드를 다시 올림" if item.get("reopened")
                        else f"{record.source} 에서 받음")
            self._ledger_intake(record, item, "intake_received", "noted", received,
                                {"kind_hint": item.get("kind"),
                                 **({"reopened": True} if item.get("reopened") else {})})
            if self.intake_async and self.intake.needs_model(item):
                self._read_intake_later(item, record, bbox)
                continue
            try:
                result = self.intake.compile_item(item, bbox)
            except Exception as error:  # noqa: BLE001 — 한 항목이 폴링을 멈추면 안 됩니다
                result = (None, "", f"읽기 실패 {error!r}")
            self._settle_intake(item, record, result)

    def _read_intake_later(self, item: dict, record, bbox) -> None:
        """문법 밖의 문장은 모델이 읽습니다 — 세계 스레드 밖에서(_read_later 와 같은 이유)."""
        self._reading_intake.add(record.id)
        round_at = self._round

        def work() -> None:
            try:
                result = self.intake.compile_item(item, bbox)
            except Exception as error:  # noqa: BLE001 — 못 읽은 것으로 적습니다
                result = (None, "", f"모델 읽기 실패 {error!r}")
            with self._guard:
                self._read_intake.append((round_at, item, record, result))

        threading.Thread(target=work, daemon=True, name=f"intake-{record.id}").start()

    def _collect_read_intake(self) -> None:
        """읽기 스레드가 놓고 간 결과를 세계 스레드에서 적습니다. 판이 바뀐 뒤의 답은 버립니다."""
        with self._guard:
            arrived, self._read_intake = self._read_intake, []
        for round_at, item, record, result in arrived:
            self._reading_intake.discard(record.id)
            if round_at != self._round or self.intake.records.get(record.id) is not record:
                continue
            self._settle_intake(item, record, result)

    def _settle_intake(self, item: dict, record, result: tuple) -> None:
        """읽은 결과를 적고 적용합니다. 항목 하나가 터져도 폴링은 계속 — 못 읽은 것으로 남깁니다."""
        compiled, read_by, why = result
        self.intake.settle(record, compiled, read_by, why)
        if compiled is None:
            self._ledger_intake(record, item, "intake_unreadable", "unreadable",
                                f"읽지 못했습니다 — {record.why}", {"why": record.why,
                                                                "read_by": read_by})
            return
        try:
            self._apply_intake(item, record, compiled, read_by)
        except Exception as error:  # noqa: BLE001 — 항목이 깨졌지 런타임이 깨진 게 아닙니다
            self.intake.settle(record, None, read_by, f"적용 실패 {error!r}")
            self._ledger_intake(record, item, "intake_unreadable", "unreadable",
                                f"읽었지만 적용하지 못했습니다 — {record.why}",
                                {"why": record.why, "read_by": read_by})

    def _apply_intake(self, item: dict, record, compiled, read_by: str) -> None:
        if compiled.kind == "none":
            self._ledger_intake(record, item, "intake_read", "noted",
                                "기단과 무관한 글", {"kind": "none", "read_by": read_by})
        elif compiled.kind == "weather":
            self._take_weather(item, record, compiled.weather, read_by)
        elif compiled.kind == "incident":
            self._take_incident(item, record, compiled.incident, read_by)
        elif compiled.kind == "notice":
            self._take_notice(item, record, compiled.notice, read_by)

    def _take_weather(self, item: dict, record, report, read_by: str) -> None:
        """한도 안이면 기록만(대기 중이면 '풀까요' 카드에 붙임). 넘으면 관제탑 피드의 문법 읽기는
        즉시, 그 밖(모델·검색·수동)은 사람 뒤에. 창이 이미 닫힌 보고서는 기록만."""
        breaches = self.intake.breaches(report)
        until_tick = self.intake.hold_until(report, item, self.tick)
        detail = {"kind": "weather", "read_by": read_by, "breaches": breaches,
                  "report": report.to_dict(), "until_tick": until_tick}
        if not breaches:
            self.intake.note_report(record.id, report, breaches, self.tick, read_by)
            self._ledger_intake(record, item, "intake_read", "noted", "날씨 보고서 · 한도 안",
                                detail)
            if self.intake.hold is not None:
                self._refresh_lift_card(self.intake.hold)
            return
        if until_tick <= self.tick:
            # 지난 창의 보고서. 세웠다가 같은 폴링에 풀면 아직 안 뜬 승인 경로만 헛되이 물립니다.
            self._ledger_intake(record, item, "intake_read", "noted",
                                f"날씨 보고서 · 한도 밖 · 창이 틱 {until_tick} 에 이미 닫힘",
                                {**detail, "window_closed": True})
            return
        must_hold = self.intake.must_hold(record, read_by)
        if self.intake.hold is not None:
            # 이미 세워 두었습니다. 관제탑 피드가 더 늦게까지라면 그만큼 늘리고, 카드도 그 창으로.
            hold = self.intake.hold
            if until_tick > hold.until_tick and not must_hold:
                hold.until_tick = until_tick
                self._rule_extend(hold.id, until_tick)
                for policy in hold.policies():
                    self.policies.add(policy)
                self._refresh_lift_card(hold)
                detail["extended_until"] = until_tick
            self.intake.note_report(record.id, report, breaches, self.tick, read_by)
            self._ledger_intake(record, item, "intake_read", "noted",
                                "날씨 보고서 · 한도 밖 (대기 중)", detail)
            return
        if must_hold:
            self._ledger_intake(record, item, "intake_read", "noted",
                                "날씨 보고서 · 한도 밖 (사람 확인 대기)", {**detail, "held": True})
            self._hold_weather(record, report, breaches, until_tick, read_by)
            return
        self._ledger_intake(record, item, "intake_read", "noted", "날씨 보고서 · 한도 밖", detail)
        self._open_weather_hold(record.id, report, breaches, until_tick, "grammar")

    def _open_weather_hold(self, record_id: str, report, breaches: list[str], until_tick: int,
                           source: str) -> WeatherHold:
        """이륙 정지. 정책은 지금 걸리고, 땅에서 아직 안 뜬 승인 경로는 물립니다. 푸는 것은
        사람과 창뿐."""
        hold = self.intake.open_hold(record_id, report, breaches, until_tick, self.tick, source)
        self._rule_apply(record_id, "weather_hold", self.tick, until_tick)
        for policy in hold.policies():
            self.policies.add(policy)
        noted = Proposal(asset_id=FLEET_ASSET, action="weather_hold", cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=hold.reason[:180],
                         params={"hold": hold.to_dict()})
        decision = Decision(noted.id, Verdict.AUTO, hold.reason, policy_hit=HOLD_POLICY_PREFIX,
                            code="weather_hold",
                            detail={"until_tick": until_tick, "since_tick": self.tick,
                                    "source": source, "breaches": list(breaches),
                                    "report": report.to_dict()})
        entry = self.ledger.open_entry(noted, decision, self._context(None, ["weather"]))
        self.ledger.close_entry(entry, "noted")
        self._ground_for_hold(hold)
        self._raise_lift_card(hold)
        return hold

    def _ground_for_hold(self, hold: WeatherHold) -> None:
        """땅에서 승인만 받고 아직 안 뜬 경로를 물립니다. 떠 있는 기체는 건드리지 않습니다 —
        내려야 하니까.

        정책은 새 신청만 막습니다. 이미 승인된 경로의 출발(승인 확인 25틱 뒤)은 신청이 아니라서,
        물리지 않으면 대기 중에 뜹니다.
        """
        for asset_id, state in list(self.telemetry.items()):
            if float(state.get("alt_m") or 0.0) > 1.0:
                continue
            # 텔레메트리의 route 는 지난 폴링 때의 것입니다. 보류가 열린 바로 그 틱에 막 승인된
            # 경로는 아직 거기 없어서, 물리지 않은 채 25틱 뒤 보류 중에 떴습니다(런타임 쪽
            # '보류 중 이륙' 1건).
            # 기준은 런타임이 적은 의도입니다 — 승인됐고 아직 안 뜬 의도가 있으면 물립니다.
            waiting = self.intents.get(asset_id)
            undeparted = waiting is not None and waiting.state == ACCEPTED
            if not state.get("route") and not undeparted:
                continue
            retreat = Proposal(asset_id=asset_id, action="divert_ground", cost_usd=0.0,
                               blast_radius="cargo", author="runtime",
                               rationale=f"{hold.reason} — 아직 안 뜬 경로를 물림",
                               params={"hold": hold.id})
            decision = Decision(retreat.id, Verdict.AUTO,
                                f"{hold.reason} — {asset_id} 의 아직 안 뜬 경로를 물림",
                                policy_hit=HOLD_POLICY_PREFIX, code="recalled",
                                detail={"resource": asset_id, "policy": HOLD_POLICY_PREFIX,
                                        "until_tick": hold.until_tick})
            standing = self.intents.get(asset_id)
            intent_id = standing.id if standing is not None and standing.live else None
            entry = self.ledger.open_entry(retreat, decision,
                                           self._context(None, ["weather"], intent_id))
            decision.ledger_id = entry.id
            result = self.adapter.execute(asset_id, "divert_ground", retreat.params, entry.id,
                                          blast=retreat.blast_radius)
            ok = bool(result.get("ok"))
            self.ledger.close_entry(entry, "done" if ok else f"failed: {result.get('error')}",
                                    decision)
            self._end_intent(asset_id, "weather_hold")
            for action in ROUTED:
                self._recent_commits.pop((asset_id, action), None)

    def _raise_lift_card(self, hold: WeatherHold) -> None:
        """승인 화면의 '일찍 풀까요' 카드. 승인이면 그 자리에서 풀리고, 거부면 창이 끝날 때까지."""
        card = Proposal(asset_id=FLEET_ASSET, action="lift_weather_hold", cost_usd=0.0,
                        blast_radius="none", author="runtime",
                        rationale=f"{hold.reason} · until tick {hold.until_tick}"[:180],
                        params={"hold": hold.id, "reason": hold.reason,
                                "until_tick": hold.until_tick, "report": hold.report})
        decision = Decision(card.id, Verdict.HUMAN,
                            "기상 대기를 창보다 일찍 푸는 것은 사람 몫입니다",
                            authority_hit="weather_hold", code="human_lift",
                            detail={"hold": hold.id, "until_tick": hold.until_tick})
        self._decisions[card.id] = decision
        with self._guard:
            self._awaiting_human[card.id] = card
        self._open_cards[card.id] = self.ledger.open_entry(card, decision,
                                                           self._context(None, ["weather"]))
        hold.lift_card = card.id

    def _refresh_lift_card(self, hold: WeatherHold) -> None:
        """대기의 사정이 바뀌었습니다(한도 안 보고서가 뒤에 옴, 창이 늘어남). 대기는 저절로 안
        풀립니다 — 카드가 지금의 창과 그 사실을 말하게 합니다."""
        later = hold.later_report
        with self._guard:
            standing = self._awaiting_human.get(hold.lift_card or "")
        if standing is None:
            self._raise_lift_card(hold)
            with self._guard:
                standing = self._awaiting_human.get(hold.lift_card or "")
        if standing is None:
            return
        standing.params = {**standing.params, "until_tick": hold.until_tick,
                           "later_report": later or {}}
        rationale = f"{hold.reason} · until tick {hold.until_tick}"
        if later:
            rationale += f" · later report within limits ({later.get('text', '')})"
        standing.rationale = rationale[:180]

    def _hold_weather(self, record, report, breaches: list[str], until_tick: int,
                      read_by: str) -> None:
        """한도를 넘었지만 관제탑 피드의 문법 읽기가 아닌 날씨(모델·검색·수동). 사람이 확인하기
        전에는 아무것도 세우지 않습니다. 카드는 세울 창(until tick)까지 말합니다."""
        held = Proposal(asset_id=INTAKE_ASSET, action="publish_weather", cost_usd=0.0,
                        blast_radius="none", author=read_by,
                        rationale=(f"WEATHER · {' · '.join(breaches)} · until tick {until_tick} "
                                   f"— {record.text}")[:180],
                        params={"item": record.id, "report": report.to_dict(),
                                "breaches": list(breaches), "until_tick": until_tick,
                                "source": read_by, "origin": record.source,
                                "text": record.text[:400]})
        decision = Decision(held.id, Verdict.HUMAN, self.intake.held_why(record, read_by),
                            authority_hit="model_weather", code="human_weather",
                            detail={"item": record.id, "source": read_by,
                                    "origin": record.source, "breaches": list(breaches),
                                    "until_tick": until_tick})
        self._decisions[held.id] = decision
        with self._guard:
            self._awaiting_human[held.id] = held
        self._open_cards[held.id] = self.ledger.open_entry(held, decision,
                                                           self._context(None, INTAKE_CHECKS))
        self.intake.held_weather[record.id] = {
            "id": record.id, "report": report.to_dict(), "breaches": list(breaches),
            "until_tick": until_tick, "source": read_by, "card": held.id,
            "text": record.text[:180]}
        self._rule_open(record.id, "weather_hold", self.tick, until_tick, applied=False)

    def _confirm_weather(self, proposal: Proposal, decision: Decision, actor: str,
                         allow: bool, card=None) -> Decision:
        """사람의 답. 승인이면 그때부터 사람의 말로 세우고, 거부면 기록만 남습니다."""
        key = str(proposal.params.get("item") or "")
        held = self.intake.held_weather.pop(key, None)
        report = _report_from(held or proposal.params)
        until_tick = int((held or proposal.params).get("until_tick") or self.tick)
        if not (allow and held is not None and self.intake.hold is None
                and self.tick <= until_tick):
            # 이 보고서로는 아무것도 안 섭니다: 창이 지났거나, 거부했거나, 이미 대기 중입니다.
            self._rule_close(key, "lapsed" if allow and self.tick > until_tick
                             else "already_held" if allow else "refused")
        if allow and self.tick > until_tick:
            decision.verdict = Verdict.DENIED
            decision.reason = f"{actor} 가 확인했지만 창이 틱 {until_tick} 에 이미 닫혔습니다"
            decision.code = "weather_lapsed"
            self._close_card(card, proposal, decision, "lapsed", "weather:human")
            return decision
        if allow and held is not None and self.intake.hold is None:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 날씨 보고서를 확인했습니다 — 이륙 정지"
            decision.code = "weather_confirmed"
            self._close_card(card, proposal, decision, "done", "weather:human")
            self._open_weather_hold(held["id"], report, list(held["breaches"]), until_tick, "human")
            return decision
        if allow:
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 확인 — 이미 대기 중이라 기록만"
            decision.code = "weather_confirmed"
            self._close_card(card, proposal, decision, "done", "weather:human")
            return decision
        decision.verdict = Verdict.DENIED
        decision.reason = f"{actor} 가 날씨 보고서를 거부했습니다"
        decision.code = "weather_refused"
        self._close_card(card, proposal, decision, "denied", "weather:human")
        return decision

    def _confirm_lift(self, proposal: Proposal, decision: Decision, actor: str, allow: bool,
                      card=None) -> Decision:
        """'일찍 풀까요' 의 답. 승인이면 정책을 걷고, 거부면 창이 닫힐 때까지 그대로입니다."""
        hold = self.intake.hold
        if hold is not None:
            hold.lift_card = None
        if allow and hold is not None and hold.id == proposal.params.get("hold"):
            self._lift_hold(hold, "human")
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 기상 대기를 풀었습니다"
            decision.code = "weather_hold_lifted"
            self._close_card(card, proposal, decision, "done", "weather:human")
            return decision
        decision.verdict = Verdict.DENIED
        decision.reason = (f"{actor} 가 풀지 않았습니다 — 창이 닫힐 때까지 대기" if hold is not None
                           else "풀 대기가 없습니다")
        decision.code = "lift_refused"
        self._close_card(card, proposal, decision, "denied", "weather:human")
        return decision

    def _lift_hold(self, hold: WeatherHold, lifted_by: str) -> None:
        for policy_id in hold.policy_ids:
            self.policies.remove(policy_id)
        self.intake.close_hold()
        self._rule_close(hold.id, lifted_by, self.tick)

    def _tick_intake(self) -> None:
        """창이 끝난 것을 거둡니다: 대기는 풀고 카드를 내리고, 사람 없이 지나간 보류 날씨는 잊음."""
        hold = self.intake.hold
        if hold is not None and self.tick > hold.until_tick:
            self._lift_hold(hold, "window")
            self._ledger_hold_end(hold, "weather_hold_expired",
                                  f"틱 {hold.until_tick} 에 창이 닫혀 풀림")
            if hold.lift_card:
                self._drop_card(hold.lift_card, "weather_hold_expired", "창이 닫혀 대기가 풀림")
        for key, held in list(self.intake.held_weather.items()):
            if self.tick > int(held.get("until_tick") or self.tick):
                self.intake.held_weather.pop(key, None)
                self._rule_close(key, "lapsed")
                self._drop_card(held.get("card"), "weather_lapsed",
                                "사람이 확인하기 전에 창이 닫힘")

    def _ledger_hold_end(self, hold: WeatherHold, code: str, reason: str) -> None:
        """대기가 사람 없이 끝난 줄(창이 닫힘, 판이 바뀜). 보고서가 이 줄로 대기를 닫습니다."""
        noted = Proposal(asset_id=FLEET_ASSET, action="weather_hold", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"{hold.reason} — {reason}"[:180],
                         params={"hold": hold.to_dict()})
        decision = Decision(noted.id, Verdict.AUTO, reason, code=code,
                            detail={"hold": hold.id, "until_tick": hold.until_tick})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, ["weather"])), "noted")

    def _drop_card(self, proposal_id: str | None, code: str, why: str) -> None:
        """서 있는 카드 하나를 내립니다(lapsed). 사람이 이미 답했으면 아무것도 없습니다."""
        with self._guard:
            proposal = self._awaiting_human.pop(proposal_id or "", None)
        if proposal is None:
            return
        decision = self._decisions.get(proposal.id) or Decision(proposal.id, Verdict.HUMAN, "")
        decision.verdict = Verdict.DENIED
        decision.reason = why
        decision.code = code
        self._close_card(self._open_cards.pop(proposal.id, None), proposal, decision, "lapsed",
                         "weather:window")

    def _take_incident(self, item: dict, record, report, read_by: str) -> None:
        """사고 → 금지 구역(원). 공지의 길로 갑니다: 관제탑 피드의 문법 읽기는 이번 폴링에 걸리고,
        그 밖(모델·검색·수동)은 사람 뒤에. 창이 이미 닫힌 사고는 기록만."""
        until_tick = self.intake.window_until(report.from_tick, report.until_tick, item, self.tick)
        detail = {"kind": "incident", "read_by": read_by, "incident": report.to_dict(),
                  "until_tick": until_tick}
        if until_tick <= self.tick:
            self._ledger_intake(record, item, "intake_read", "noted",
                                f"사고 · {report.name} · 창이 틱 {until_tick} 에 이미 닫힘",
                                {**detail, "window_closed": True})
            return
        held = self.intake.must_hold(record, read_by)
        notice = self.intake.incident_record(record.id, report, record.text, read_by, until_tick,
                                             held)
        self.notices.records[record.id] = notice
        self._rule_open(record.id, "incident", report.from_tick or self.tick, until_tick,
                        applied=not held)
        self._ledger_intake(record, item, "intake_read", "noted",
                            f"사고 · {report.name} · {report.radius_m:.0f} m",
                            {**detail, "held": held})
        if held:
            self._hold_notice({"id": record.id, "text": record.text}, notice,
                              self.intake.held_why(record, read_by))

    def _take_notice(self, item: dict, record, notice, read_by: str) -> None:
        """제한 공지(FAA 어투 또는 모델이 구조화한 것). 공지 책에 올리고 같은 길을 갑니다."""
        held = self.intake.must_hold(record, read_by)
        adopted = self.notices.adopt(record.id, str(item.get("name") or notice.name or record.id),
                                     {"kind": "notam", "until_tick": item.get("until_tick")},
                                     notice, read_by, held=held)
        self.intake.notice_ids.add(record.id)
        self._rule_open(record.id, "notice", adopted.from_tick or self.tick, adopted.until_tick,
                        applied=not held)
        self._ledger_intake(record, item, "intake_read", "noted", f"제한 공지 · {adopted.name}",
                            {"kind": "notice", "read_by": read_by, "held": held,
                             "notice": adopted.to_dict()})
        if held:
            self._hold_notice({"id": record.id, "text": record.text}, adopted,
                              self.intake.held_why(record, read_by))

    def _ledger_incident(self, record) -> None:
        """사고 구역이 걸렸습니다. 문법이 읽은 것은 이 줄이 유일한 기록이라 남깁니다."""
        tags = record.volume.tags or {}
        noted = Proposal(asset_id=FLEET_ASSET, action="incident_keepout", cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=record.name[:180],
                         params={"incident": record.id, "name": record.name,
                                 "centre": tags.get("centre"), "radius_m": tags.get("radius_m"),
                                 "until_tick": record.until_tick, "source": record.source})
        decision = Decision(noted.id, Verdict.AUTO,
                            f"{record.name} · {tags.get('radius_m')} m keep-out until tick "
                            f"{record.until_tick}", policy_hit=record.id, code="incident_keepout",
                            detail={"incident": record.id, "name": record.name,
                                    "until_tick": record.until_tick, "source": record.source,
                                    "radius_m": tags.get("radius_m")})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, ["incident"])), "noted")

    def _ledger_intake(self, record, item: dict, code: str, outcome: str, reason: str,
                       detail: dict | None = None) -> None:
        """접수 한 줄. 받은 것·읽은 것·못 읽은 것이 전부 원장에 있어야 합니다."""
        noted = Proposal(asset_id=INTAKE_ASSET, action="intake", cost_usd=0.0,
                         blast_radius="none", author="runtime", rationale=record.text[:180],
                         params={"item": record.id, "source": record.source,
                                 "url": record.url or None, "query": item.get("query"),
                                 "title": item.get("title"), "kind_hint": item.get("kind")})
        verdict = Verdict.DENIED if code == "intake_unreadable" else Verdict.AUTO
        decision = Decision(noted.id, verdict, reason, code=code,
                            detail={"item": record.id, "source": record.source,
                                    "url": record.url or None, **(detail or {})})
        self.ledger.close_entry(
            self.ledger.open_entry(noted, decision, self._context(None, INTAKE_CHECKS)), outcome)
        self._store_intake(record, item, code, detail or {})

    def _store_intake(self, record, item: dict, code: str, detail: dict) -> None:
        """접수 줄을 sqlite 에도. 받으면 한 줄을 넣고, 읽으면 그 줄을 무엇이 되었는지로 고칩니다."""
        if code == "intake_received":
            self.store.put_item(record.id, record.source, record.text, self.tick, record.url,
                                item.get("kind"), _hints_of(item))
            return
        outcome = ("unreadable" if code == "intake_unreadable"
                   else "window_closed" if detail.get("window_closed")
                   else "held" if detail.get("held") else "read")
        self.store.settle_item(record.id, record.kind, record.read_by, outcome)

    # ---------- 규칙 기록(sqlite) ----------

    def _rule_open(self, item_id: str, kind: str, from_tick, until_tick, applied: bool) -> None:
        self._rule_ids[item_id] = self.store.open_rule(item_id, kind, from_tick, until_tick,
                                                       applied)

    def _rule_apply(self, item_id: str, kind: str | None = None, from_tick=None,
                    until_tick=None) -> None:
        """규칙이 걸렸습니다. 보류 줄이 있으면 적용으로 고치고, 없으면(kind 를 주면) 새로 엽니다."""
        self.store.decide_item(item_id, "approved")
        rule = self._rule_ids.get(item_id)
        if rule is not None:
            self.store.apply_rule(rule, from_tick)
        elif kind is not None:
            self._rule_open(item_id, kind, from_tick, until_tick, True)

    def _rule_extend(self, item_id: str, until_tick: int) -> None:
        self.store.extend_rule(self._rule_ids.get(item_id), until_tick)

    def _rule_close(self, item_id: str, lifted_by: str, until_tick: int | None = None) -> None:
        """규칙 줄을 닫고, 그 항목이 사람을 기다리던 것이면 끝(거부·지나감·판 바뀜)을 적습니다 —
        안 적으면 재시작할 때 이미 답한 카드가 다시 오릅니다."""
        self.store.close_rule(self._rule_ids.pop(item_id, None), lifted_by, until_tick)
        self.store.decide_item(item_id, lifted_by)

    def _close_rules(self, lifted_by: str) -> None:
        for key in list(self._rule_ids):
            self._rule_close(key, lifted_by)

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
        # 검색은 키가 있을 때만, 자기 스레드에서. 결과는 inbox 에 놓고 세계 스레드가 다음 폴링에
        # 읽습니다 — 세계 스레드는 네트워크를 기다리지 않습니다.
        if self.tavily is not None and self.intake_poller is None:
            self.intake_poller = IntakePoller(self.tavily, self.config.intake.queries,
                                              INTAKE_PERIOD_S, self.take_in)
            threads.append(self.intake_poller.start())
        # METAR 도 자기 스레드에서. 키가 없어도 돌고, 닿지 못하면 출처가 off 로 한 줄.
        if self.metar is not None and self.metar_poller is None:
            self.metar_poller = MetarPoller(self.metar, METAR_PERIOD_S, self.take_metar)
            threads.append(self.metar_poller.start())
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
            if self.links.lost(asset_id):
                # 링크가 끊긴 기체에는 회수 명령이 닿지 않습니다. 보내지 않습니다 — 그 기체의 공간은
                # 예약된 채이고 판정은 그 부피를 계속 피합니다.
                continue
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
        if hold is None or self.links.lost(hold.asset_id):
            return None     # 잡은 기체가 없거나, 있어도 링크가 끊겨 회항 명령을 못 듣습니다

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

    # ---------- 기체 등록 ----------

    def register_agent(self, body: dict) -> tuple[int, dict]:
        """POST /agents/register. 기체 프로세스가 무엇으로 신청서를 쓰는지 알립니다. 라벨일
        뿐입니다."""
        try:
            identity = AgentIdentity.from_dict(body or {})
        except ValueError as error:
            return 400, {"error": str(error)}
        if identity.world != "guarded":
            return 400, {"error": "직결 세계는 런타임에 신청하지 않습니다 — 그 모델은 시뮬레이터가 "
                                  "기체마다 싣습니다(DIRECT_MODEL)"}
        # 편대 명단은 세계(텔레메트리)가 압니다. 명단 밖의 이름을 받으면 화면의 '드론 · … ×4' 가
        # ×5 가 됩니다. 세계를 받기 전이면 503 — 기체는 몇 초 뒤에 다시 알립니다.
        fleet = set(self.telemetry)
        if not fleet:
            return 503, {"error": "아직 세계를 받지 못했습니다 — 곧 다시 알려 주세요",
                         "retry": True}
        if identity.asset_id not in fleet:
            return 404, {"error": f"{identity.asset_id} 는 이 편대에 없습니다"}
        with self._guard:
            self.agents[identity.asset_id] = {**identity.to_dict(), "last_seen_tick": self.tick}
        return 200, {"ok": True, "display": identity.display, "tick": self.tick}

    def agents_snapshot(self) -> dict:
        """/state.agents. AGENT_STALE_TICKS 동안 소식 없는 기체는 뺍니다 — 죽은 프로세스의 모델
        이름을 화면이 계속 달면 거짓말입니다."""
        with self._guard:
            for asset in [a for a, row in self.agents.items()
                          if self.tick - int(row["last_seen_tick"]) > AGENT_STALE_TICKS]:
                del self.agents[asset]
            return {asset: {key: row.get(key) for key in AGENT_FIELDS}
                    for asset, row in self.agents.items()}

    # ---------- 링크 두절 ----------

    def watch_links(self) -> list[LinkEvent]:
        """텔레메트리 심장박동을 봅니다. 세계 스레드(_pull_world)와 하네스가 폴링마다 부릅니다."""
        with self._link_lock:
            events = self.links.observe(self.telemetry, self.tick)
        for event in events:
            if event.kind == "lost":
                self._link_lost(event)
            else:
                self._link_restored(event)
        return events

    def _link_lost(self, event: LinkEvent) -> None:
        """두절. 기체에는 아무것도 보내지 않습니다(들을 수 없음). 그 기체의 의도를 예약된 채로
        두고 — 어디쯤인지 모르니 남은 경로 전부를 명목 착지 + 여유까지 — 원장에 적고 사람 카드를
        올립니다. 예약은 조이는 것이라 그 틱에 걸리고, 일찍 푸는 것은 사람의 몫입니다."""
        at = _position(self.telemetry.get(event.asset) or {})
        standing = self.intents.get(event.asset)
        standing = standing if standing is not None and standing.live else None
        reserved_until = None
        if standing is not None:
            reserved_until = standing.reserve_dark(event.last_seen_tick, at)
            self._dark[event.asset] = standing
        behaviour = self.performance.lost_link.behaviour
        detail = {"resource": event.asset, "since_tick": event.since_tick,
                  "last_seen_tick": event.last_seen_tick, "declared_tick": event.tick,
                  "intent": standing.id if standing is not None else None,
                  "behaviour": behaviour, "reserved_until_tick": reserved_until,
                  "last_position": _position_dict(at)}
        reason = (f"{event.asset} 텔레메트리가 틱 {event.since_tick} 부터 없음 — {behaviour}, "
                  + (f"승인 경로 + 착륙 기둥을 틱 {reserved_until} 까지 예약"
                     if reserved_until is not None else "예약할 의도 없음"))
        self._ledger_link("link_lost", event.asset, reason, detail, standing)
        self._raise_link_card(event.asset, detail, standing)

    def _raise_link_card(self, asset: str, detail: dict, intent: Intent | None) -> None:
        """승인 화면의 두절 통보. 승인하면 잡아 둔 공간을 지금 풀고, 거부하면 텔레메트리가 돌아올
        때까지 그대로입니다. 텔레메트리가 돌아오면 카드는 저절로 내려갑니다."""
        until = detail.get("reserved_until_tick")
        card = Proposal(asset_id=asset, action="lost_link_notice", cost_usd=0.0,
                        blast_radius="schedule", author="runtime",
                        rationale=(f"no telemetry since tick {detail['since_tick']} · "
                                   f"{detail['behaviour']} · "
                                   + (f"space reserved until tick {until}" if until is not None
                                      else "nothing filed to reserve"))[:180],
                        params=dict(detail))
        decision = Decision(card.id, Verdict.HUMAN,
                            "링크가 끊긴 기체의 공간을 일찍 푸는 것은 사람 몫입니다 — 승인하면 "
                            "지금 풀고, 거부하면 텔레메트리가 돌아올 때까지 잡아 둡니다",
                            authority_hit="lost_link", code="human_lost_link", detail=dict(detail))
        self._decisions[card.id] = decision
        with self._guard:
            self._awaiting_human[card.id] = card
        self._open_cards[card.id] = self.ledger.open_entry(
            card, decision, self._context(None, ["link"], intent.id if intent else None))
        self._link_cards[asset] = card.id

    def _link_restored(self, event: LinkEvent) -> None:
        """텔레메트리가 돌아왔습니다. 끊긴 사이 승인한 부피 안에 있었나(순응)를 보고, 늘렸던 창을
        되돌리고, 카드를 내립니다. 다시 보이니 판정은 이제 텔레메트리로 합니다."""
        at = _position(self.telemetry.get(event.asset) or {})
        intent = self._dark.pop(event.asset, None)
        self._released.discard(event.asset)
        conforming = intent.covers(*at) if intent is not None and at is not None else None
        if intent is not None:
            intent.release_dark()
        detail = {"resource": event.asset, "since_tick": event.since_tick,
                  "last_seen_tick": event.last_seen_tick, "restored_tick": event.tick,
                  "dark_ticks": event.tick - event.since_tick,
                  "intent": intent.id if intent is not None else None,
                  "conforming": conforming, "position": _position_dict(at)}
        where = ("승인한 부피 안" if conforming
                 else "승인한 부피 밖" if conforming is False else "잡아 둔 의도 없음")
        reason = (f"{event.asset} 텔레메트리가 틱 {event.tick} 에 돌아옴 "
                  f"({detail['dark_ticks']}틱 끊김) — {where}")
        self._ledger_link("link_restored", event.asset, reason, detail, intent)
        if conforming is False:
            self._ledger_link_nonconformance(event, intent, at)
        self._drop_link_card(event.asset, detail)
        self._observe()     # 다시 보이는 텔레메트리로 의도를 옮깁니다(내려앉았으면 arrived)

    def _ledger_link(self, code: str, asset: str, reason: str, detail: dict,
                     intent: Intent | None) -> None:
        """링크 두절·복구 한 줄(outcome noted). 실행은 없습니다 — 끊긴 기체는 들을 수 없습니다."""
        noted = Proposal(asset_id=asset, action=code, cost_usd=0.0, blast_radius="none",
                         author="runtime", rationale=reason[:180], params=dict(detail))
        decision = Decision(noted.id, Verdict.AUTO, reason, code=code, detail=dict(detail))
        entry = self.ledger.open_entry(
            noted, decision, self._context(None, ["link"], intent.id if intent else None))
        self.ledger.close_entry(entry, "noted")

    def _ledger_link_nonconformance(self, event: LinkEvent, intent: Intent, at) -> None:
        """끊긴 사이 승인한 부피 밖으로 나갔습니다. 되돌리지는 않습니다 — 기록에 남깁니다."""
        noted = Proposal(asset_id=event.asset, action="conformance", cost_usd=0.0,
                         blast_radius="none", author="runtime",
                         rationale=f"링크가 끊긴 사이 승인한 부피 밖 (틱 {event.tick} 에 "
                                   "다시 보임)",
                         params={"intent": intent.id, "kind": "lost_link",
                                 "since_tick": event.since_tick, "restored_tick": event.tick,
                                 "position": _position_dict(at)})
        decision = Decision(noted.id, Verdict.AUTO,
                            f"{event.asset} 가 링크가 끊긴 사이 승인한 부피 밖에 있었습니다",
                            code="nonconforming",
                            detail={"resource": event.asset, "intent": intent.id,
                                    "kind": "lost_link", "restored_tick": event.tick,
                                    "position": _position_dict(at)})
        entry = self.ledger.open_entry(noted, decision,
                                       self._context(None, ["conformance"], intent.id))
        self.ledger.close_entry(entry, "noted")

    def _drop_link_card(self, asset: str, detail: dict) -> None:
        """텔레메트리가 돌아와 두절 카드를 내립니다(lapsed). 사람이 이미 답했으면 아무것도
        없습니다."""
        card_id = self._link_cards.pop(asset, None)
        with self._guard:
            proposal = self._awaiting_human.pop(card_id or "", None)
        if proposal is None:
            return
        decision = self._decisions.get(proposal.id) or Decision(proposal.id, Verdict.HUMAN, "")
        decision.verdict = Verdict.AUTO
        decision.reason = "텔레메트리가 돌아와 카드를 내림"
        decision.code = "link_restored"
        decision.detail = {**decision.detail, **detail}
        self._close_card(self._open_cards.pop(proposal.id, None), proposal, decision, "lapsed",
                         "link")

    def _confirm_lost_link(self, proposal: Proposal, decision: Decision, actor: str,
                           allow: bool, card=None) -> Decision:
        """사람의 답. 승인이면 잡아 둔 공간을 지금 풉니다 — 그 기체가 어디 있는지 사람이 안다는
        뜻이고, 그 책임이 원장에 그 사람 이름으로 남습니다. 거부면 텔레메트리가 돌아올 때까지
        그대로입니다. 어느 쪽이든 기체에는 아무것도 보내지 않습니다."""
        asset = proposal.asset_id
        self._link_cards.pop(asset, None)
        if allow and self.links.lost(asset):
            standing = self._dark.get(asset)
            if standing is not None and standing.live:
                self.intents.end(asset, "released")
            self._released.add(asset)
            decision.verdict = Verdict.AUTO
            decision.reason = f"{actor} 가 {asset} 의 잡아 둔 공간을 풀었습니다"
            decision.code = "lost_link_released"
            self._close_card(card, proposal, decision, "done", "link:human")
            return decision
        decision.verdict = Verdict.DENIED
        decision.reason = (f"{actor} 가 텔레메트리가 돌아올 때까지 공간을 잡아 둡니다"
                           if self.links.lost(asset) else "링크가 이미 돌아왔습니다")
        decision.code = "lost_link_kept"
        self._close_card(card, proposal, decision, "denied", "link:human")
        return decision

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
            # 보류 기록(held)도 실립니다: 사람이 확인하기 전에는 applied 가 False 이고 아무것도
            # 안 막습니다. 배너는 그것을 '사람이 확인해야 적용' 으로 씁니다.
            "notices": self.notices.snapshot(),
            # 관제 권고. 기체마다 마지막 것. 정보일 뿐이고 아무것도 바꾸지 않습니다.
            "advisories": self.advisor.snapshot(),
            # 정보 수집. 어느 출처가 켜져 있고 무엇을 읽었나, 지금 걸린 기상 대기, 사고 구역.
            "intake": self._intake_snapshot(),
            "weather": self.intake.weather_snapshot(),
            "incidents": incident_snapshot(list(self.notices.records.values()), self.tick),
            # 사전 브리핑(Tavily). 어디서 왔나(live|recorded|off), 쓴 크레딧, 요약, 읽은 것과
            # 그 출처.
            "briefing": self.briefing.snapshot(),
            # 기체마다 무엇이 신청서를 쓰나(등록한 모델). 판정과 무관한 라벨입니다.
            "agents": self.agents_snapshot(),
            # 텔레메트리 심장박동. lost 인 기체의 공간은 예약된 채입니다.
            "links": self._links_snapshot(),
            # 런타임 뒤에 붙은 진짜 자동조종(ADAPTER=composite). 읽기 전용입니다 — 판정은
            # 여전히 기록의 세계(시뮬레이터)의 텔레메트리로만 합니다.
            "autopilots": self._autopilots_snapshot(),
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

    def report(self, asset: str | None = None, fmt: str = "json"):
        """원장을 비행 단위로 접은 보고서. 원장 파일에서만 만듭니다 — 기억은 200줄뿐입니다."""
        built = build_report(self.ledger.read_all(), self.tick, self.airspace.revision, asset)
        # 들어온 것(sqlite)도 같이. 원장의 접수 줄과 같은 사실을 무엇이 무엇이 되었는지로 접은 것.
        built["intake"] = self.store.report()
        return to_markdown(built) if fmt == "md" else built

    def _intake_snapshot(self) -> dict:
        out = self.intake.snapshot(self.tavily is not None)
        out["sources"]["metar"] = self.metar_status
        out["metar"] = {"stations": list(self.metar.stations) if self.metar is not None else [],
                        "period_s": METAR_PERIOD_S, "last_fetch_tick": self.metar_last_fetch_tick,
                        "fetch": self.metar_fetch}
        out["store"] = self.store.counts()
        return out

    def _links_snapshot(self) -> dict:
        with self._link_lock:
            return self.links.snapshot()

    def _autopilots_snapshot(self) -> dict:
        """거울이 붙은 어댑터만 답합니다. 시뮬레이터만 쓰는 배선에서는 빈 표입니다."""
        view = getattr(self.adapter, "autopilots", None)
        return view() if callable(view) else {}


def _is_intake(item: dict) -> bool:
    """공지 목록에서 정보 수집이 읽을 것: 날씨·사고, 그리고 종류 없이 문장만 온 것."""
    kind = item.get("kind")
    if kind in ("weather", "incident"):
        return True
    return kind not in ("recall", "zone", "notam") and bool(str(item.get("text") or "").strip())


def _intake_hints(body: dict) -> tuple[dict, str]:
    """POST /intake 의 구조화 값. 수여야 하는 것이 수가 아니면 400 — 문장은 그대로 받지 않습니다."""
    hints = {}
    for key in ("name", "address", "building_id"):
        if body.get(key) is not None:
            hints[key] = " ".join(str(body[key]).split())[:120]
    try:
        if body.get("radius_m") is not None:
            hints["radius_m"] = float(body["radius_m"])
        if body.get("until_tick") is not None:
            hints["until_tick"] = int(body["until_tick"])
    except (TypeError, ValueError):
        return {}, "radius_m 과 until_tick 은 수여야 합니다"
    return hints, ""


def _hints_of(item: dict) -> dict:
    return {key: item[key] for key in INTAKE_HINT_KEYS if item.get(key) is not None}


def _load_addresses(path: str) -> list[dict]:
    """지명 사전의 주소. 파일이 없으면 빈 목록 — 그러면 주소가 있는 사고는 못 읽은 것으로."""
    try:
        return list(json.loads(Path(path).read_text(encoding="utf-8")).get("addresses") or [])
    except (OSError, ValueError, AttributeError):
        return []


def _report_from(source: dict):
    """카드·보류 목록에 dict 로 남긴 보고서를 다시 WeatherReport 로."""
    from holdshort.core.intake import WeatherReport

    raw = dict(source.get("report") or {})
    return WeatherReport(wind_mps=raw.get("wind_mps"), gust_mps=raw.get("gust_mps"),
                         visibility_m=raw.get("visibility_m"),
                         precipitation=raw.get("precipitation"), from_tick=raw.get("from_tick"),
                         until_tick=raw.get("until_tick"), text=str(raw.get("text") or ""))


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _position(state: dict) -> tuple[float, float, float] | None:
    """텔레메트리의 자리 (lat, lon, alt_m). 모르면 None."""
    if state.get("lat") is None or state.get("lon") is None:
        return None
    return float(state["lat"]), float(state["lon"]), float(state.get("alt_m") or 0.0)


def _position_dict(at: tuple[float, float, float] | None) -> dict | None:
    if at is None:
        return None
    return {"lat": round(at[0], 6), "lon": round(at[1], 6), "alt_m": round(at[2], 1)}


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
        # 들어온 것의 기록. compose 는 /data/intake.sqlite(원장과 같은 볼륨).
        intake_db=os.getenv("INTAKE_DB") or STORE_DEFAULT_PATH,
        metar=True,
        await_airspace=True,
        # 사전 브리핑. 키가 없으면 녹음(tests/fixtures/tavily)으로 돌고 화면에 recorded 로 뜹니다.
        briefing=True,
    )
    runtime.start_background()

    server = JsonServer(int(os.getenv("PORT", "8000")))
    server.add("POST", "/proposals", lambda body, query: (200, runtime.file(body).to_dict()))
    # 기체 프로세스의 자기소개(모델·서버). 30초마다 다시 옵니다.
    server.add("POST", "/agents/register", lambda body, query: runtime.register_agent(body))
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
    # ready 는 판정할 준비(공역을 다 받음)입니다. compose 의 healthcheck 가 이것을 보고 기체를
    # 띄웁니다. 살아 있는지(ok)와 판정할 수 있는지(ready)는 다른 질문이라 둘 다 둡니다.
    server.add("GET", "/health", lambda body, query: (200, {
        "ok": True, "tick": runtime.tick, "ready": runtime.ready,
        "airspace_revision": runtime.airspace.revision}))
    # 정보 입력. 시연·수동 주입 — 문장 하나를 접수함에 넣고 돌아옵니다. 읽기는 세계 스레드가 합니다.
    server.add("POST", "/intake", lambda body, query: runtime.submit_intake(body))
    # 사전 브리핑을 한 번 더 — 다음 폴링에 작업 스레드가 묻습니다. 꺼져 있으면 503, 도는 중이면 409.
    server.add("POST", "/briefing/run", lambda body, query: runtime.briefing.request_run())
    # 원장 보고서. ?asset=<id> 로 한 기체만, ?format=md 로 사람이 읽는 표.
    server.add("GET", "/ledger/report", lambda body, query: (
        200, runtime.report(query.get("asset") or None,
                            "md" if query.get("format") == "md" else "json")))
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
