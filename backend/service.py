"""The runtime process. Holds locks, limits, the arbiter, the single commit path, the ledger."""

import json
import math
import os
import threading
import time
from pathlib import Path

from backend.adapters import build as build_adapter
from backend.intake.book import (
    HOLD_POLICY_PREFIX,
    INTAKE_PERIOD_S,
    TowerIntake,
    WeatherHold,
    incident_snapshot,
)
from backend.intake.briefing import BriefingDesk
from backend.intake.desk import IntakeDeskMixin
from backend.intake.entries import (
    FLEET_ASSET,
    INTAKE_ASSET,
    INTAKE_CHECKS,
    IntakeEntriesMixin,
)
from backend.intake.notice_flow import NoticeFlowMixin
from backend.intake.notices import NoticeBook
from backend.intake.rules import RulesMixin
from backend.intake.sources import METAR_PERIOD_S, SourcesMixin
from backend.intake.weather import WeatherMixin
from backend.runtime.advisory import AdvisoryDesk, Refusal, build_options
from backend.runtime.arbiter import Arbiter
from backend.runtime.authority import AuthorityCheck
from backend.runtime.commit import Committer
from backend.runtime.form import NOT_REFUSALS, ROUTED, _form_problem
from backend.runtime.intents import (
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
from backend.runtime.locks import LockTable
from backend.runtime.policy import PolicyBook
from backend.store.intake_store import DEFAULT_PATH as STORE_DEFAULT_PATH
from backend.store.intake_store import IntakeStore
from backend.store.ledger import Ledger
from backend.store.reports.ledger import build_report, to_markdown
from shared import config as config_module
from shared.geo import (
    METRES_PER_DEG_LAT,
    METRES_PER_DEG_LON,
    TRAFFIC_LATERAL_M,
    Airspace,
    Volume,
    first_breach,
    nearest_exit,
    vertical_column,
)
from shared.http import JsonServer, get_json
from shared.intake import Gazetteer
from shared.llm.client import TieredLlm
from shared.metar import MetarClient, MetarPoller
from shared.models import AgentIdentity, Decision, Proposal, Verdict
from shared.notam import Clock
from shared.route import Router
from shared.tavily import FetchStatus, IntakePoller, TavilyClient

# The single startup request that fetches the airspace (some 30,000 volumes) from the
# simulator. The world thread has nothing to do without it, so it waits generously — a short
# timeout re-downloads the large response from scratch every time.
AIRSPACE_FETCH_TIMEOUT_S = 30.0
# Service-area box margin (about 2 km). A model-structured notice outside it was invented.
SERVICE_MARGIN_DEG = 0.02
# The gazetteer intake uses to place things. Same file as the delivery addresses — the places an
# incident can "be" must be the same list as the places a delivery can go, and an address
# outside the list was invented by the model.
ADDRESS_FILE = os.getenv(
    "ADDRESS_FILE", str(Path(__file__).resolve().parent.parent
                        / "configs/airspace/nyc_addresses.json"))
# Drop an aircraft from /state.agents when its registration goes unrenewed for this many ticks.
# Aircraft re-announce every 30 s (drone/agent/loop.py REGISTER_PERIOD_S) — at 0.2 s/tick,
# 600 ticks is 2 minutes, i.e. more than two missed announcements.
AGENT_STALE_TICKS = int(os.getenv("AGENT_STALE_TICKS") or "600")
# Fields of one /state.agents row.
AGENT_FIELDS = ("model", "host", "world", "last_seen_tick", "display", "base_url_port", "model_ok")


class Runtime(IntakeDeskMixin, IntakeEntriesMixin, NoticeFlowMixin, RulesMixin,
              SourcesMixin, WeatherMixin):
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
            # Replies from the PX4 mirror (ADAPTER=composite) go to a line file next to the
            # ledger. The autopilot answers after the ledger line has closed, and another line
            # under the same id would make the UI and the report count one decision twice.
            # AUTOPILOT_LOG moves the file.
            journal_path=os.getenv("AUTOPILOT_LOG")
            or str(Path(ledger_path).with_name("autopilot.jsonl")),
        )
        self.committer = Committer(self.adapter, self.locks, self.ledger, self.authority)
        self.committer.on_committed = self._on_committed

        self.sim_url = sim_url
        self.pad_coords: dict[str, tuple[float, float]] = {}
        self.landing_areas: list[dict] = []   # delivery landing sites, passed to operators as is
        self.window_s = window_s
        self.tick = 0
        self.telemetry: dict = {}
        self.airspace = Airspace()
        self.router = Router(self.airspace)
        # A runtime that fetches its airspace from the simulator (the service) does not judge
        # until the fetch is complete. Under compose, aircraft filed before the runtime was up
        # and four routes were cleared against an empty airspace (version 0); judging against a
        # half-loaded one (version 14480) let the guarded wiring produce 19 restricted-airspace
        # incursions within 3 minutes. An empty airspace means "not known yet", not "no rules".
        # A runtime built in code (tests, harness) loads its airspace by hand and does not wait
        # — only the service (main) turns this on.
        self.await_airspace = await_airspace
        self.airspace_loaded = False
        self.zone_volumes: set[str] = set()   # zones from notices; removed when they end
        # Declared performance and the round's clock. Where and when a cleared route will be
        # (the intent) and NOTAM time windows are computed from these.
        self.performance = self.config.performance
        self.clock = Clock(self.performance.clock_epoch_z, self.performance.seconds_per_tick)
        self.intents = IntentRegistry()
        # Telemetry heartbeat. If an airborne aircraft's record goes unrefreshed for the declared
        # timeout_ticks, that is lost link — its intent (cleared route + landing column) stays
        # reserved and a human card goes up.
        self.links = LinkWatch(self.performance.lost_link.timeout_ticks)
        # Intents whose reservation was extended during lost link (for the conformance check
        # after recovery).
        self._dark: dict[str, Intent] = {}
        self._link_cards: dict[str, str] = {}    # aircraft → standing lost-link card (filing id)
        # Lost-link aircraft whose reservation a human released. No column (presence) goes up
        # at the spot where their telemetry froze, either.
        self._released: set[str] = set()
        # The world thread advances the heartbeat; the UI (HTTP thread) reads it.
        self._link_lock = threading.Lock()
        # What each aircraft process announced about itself (what writes its filings). A UI
        # label only; judgement ignores it.
        self.agents: dict[str, dict] = {}
        self.notices = NoticeBook(self.clock, self.llm)
        # Intake (weather, incidents, restrictions). Simulator notices, Tavily searches and manual
        # input enter the same book and pass grammar → model → code checks; weather becomes a
        # policy (takeoff halt), incidents become notices (zones).
        self.gazetteer = Gazetteer(_load_addresses(ADDRESS_FILE),
                                   lookup=lambda bid: getattr(self.airspace.get(bid), "polygon",
                                                              None))
        self.intake = TowerIntake(self.clock, self.llm, self.config.weather, self.gazetteer)
        # Record of what came in (sqlite). With no path it lives in memory, so tests don't share
        # what they have "seen". For the service, main() passes INTAKE_DB (default
        # .run/intake.sqlite).
        self.store = IntakeStore(intake_db)
        self._rule_ids: dict[str, int] = {}     # item id (the rule's basis) → rules table row
        self.tavily = TavilyClient.from_env()
        # METAR. Runs without a key. Only the service (main) turns it on — a plain instance
        # (tests) has it off and never touches the real network. None (source off) when
        # METAR=off or there are no stations.
        self.metar = MetarClient.from_env(self.config.intake.metar_stations) if metar else None
        self.metar_poller: MetarPoller | None = None
        self._metar_fetch: FetchStatus | None = None
        # starting (never fetched yet) | on | off. One line when it becomes unreachable (off),
        # one when it answers again (on) — written only on change. Writing every cycle would
        # fill the ledger with failures.
        self.metar_status = "starting" if self.metar is not None else "off"
        self.metar_fetch: dict | None = None
        self.metar_last_fetch_tick: int | None = None
        # Last observations received. Re-fed into the new round when the round changes
        # (_follow_round).
        self._metar_current: list[dict] = []
        self.intake_poller: IntakePoller | None = None
        self.intake_async = True
        self._reading_intake: set[str] = set()
        self._read_intake: list[tuple] = []
        self._intake_inbox: list[dict] = []     # left by search/manual input; world thread reads
        # Items that were waiting for a human before the restart. The cards died with the
        # process, so they are read again and the cards go back up — otherwise a report nobody
        # ever answered stays marked "seen" and is never read again.
        waiting = self.store.reopen_waiting()
        # Pre-flight briefing (Tavily). Only the service (main) turns it on — a runtime built in
        # code (tests, harness) has it off, like METAR. What the briefing already read is taken
        # here: waiting cards go back up (without re-reading), and rules that were in force are
        # read back from the record and applied as-is in the next round.
        self.briefing = BriefingDesk(self, config_path, enabled=briefing)
        self._intake_inbox.extend(self.briefing.adopt_waiting(waiting))
        self._intake_fetch: FetchStatus | None = None   # search thread's last cycle status
        # Runtime advisories. Counts consecutive refusals, checks the code-built options through
        # judgement and records them in the ledger. When a model writes the wording it runs on
        # its own thread — a refusal reply that waited on the model would stall the operator.
        self.advisor = AdvisoryDesk(self.llm)
        self.advisory_async = True
        # Model readings of notices outside the grammar also run off the world thread (tests
        # set this False and read inline).
        self.notice_async = True
        self._reading: set[str] = set()       # notice ids the model is reading
        self._read_notices: list[tuple] = []  # (round, notice, result) left by the reader thread
        # Both the world thread and the approval (HTTP) thread apply notices.
        self._notice_lock = threading.Lock()
        self._round = None                    # follows the simulator when it starts a new round
        self._contended: dict[str, list[tuple[Proposal, Decision, float]]] = {}
        self._awaiting_human: dict[str, Proposal] = {}
        # Human card (awaiting approval) filing id → its open ledger entry. Closed by the human's
        # answer, the end of the window or the end of the round.
        self._open_cards: dict[str, object] = {}
        self._decisions: dict[str, Decision] = {}
        self._checks: dict[str, list[str]] = {}   # filing id → names of the checks run so far
        # Counted on the world's clock, not the wall clock, so runs are reproducible.
        self._recent_commits: dict[tuple[str, str], int] = {}
        self.dedupe_ticks = int(os.getenv("DEDUPE_TICKS", "15"))
        self._guard = threading.Lock()
        # One at a time from judgement to intent registration. Each HTTP handler thread runs
        # file() on its own, so two filings 0.2 s apart both passed the traffic check before
        # either intent was registered (_on_committed) and both were cleared — in a live run
        # (rules mode) two recalled aircraft re-filed the same A* corridor, giving two losses of
        # separation on the runtime side. Lock order is always _judging → _guard (nothing takes
        # this while holding _guard). The world thread (recalls, notices) does not take it — the
        # clock must not stall behind a slow autopilot command, and an airborne aircraft left
        # without an intent mid-recall is covered by the presence column _others builds from
        # telemetry.
        self._judging = threading.RLock()

    # ---------- Filings ----------

    @property
    def ready(self) -> bool:
        """Whether judgement can start.

        A service that awaits airspace is ready once it has all of it; any other runtime from
        the start.
        """
        return self.airspace_loaded or not self.await_airspace

    def file(self, raw: dict) -> Decision:
        """Judge one filing and execute it if cleared.

        One at a time from judgement to intent registration (_judging).
        """
        with self._judging:
            return self._judge_and_commit(raw)

    def _judge_and_commit(self, raw: dict) -> Decision:
        proposal = Proposal.from_dict({**raw, "world": "guarded"})
        asset = self.telemetry.get(proposal.asset_id, {})
        # Checks for this filing. When an operator re-files under the same id (straight line →
        # rewrite) the list starts over — one ledger line must describe one filing. A later
        # rejudge appends to this list.
        checks = self._checks[proposal.id] = []
        self._observe()

        if not self.ready:
            # A gate before judgement: nothing is cleared until the whole airspace is in.
            # policy_hit stays empty — an operator that learned "this action is banned" from it
            # would stop filing that action even after the airspace arrived.
            checks.append("airspace_loaded")
            return self._deny(proposal, Decision(
                proposal.id, Verdict.DENIED,
                "런타임이 아직 공역을 다 받지 못했습니다 — 판정할 수 없어 거절, 잠시 뒤 다시",
                code="airspace_not_loaded",
                detail={"airspace_revision": self.airspace.revision}))

        checks.append("dedupe")
        seen_at = self._recent_commits.get((proposal.asset_id, proposal.action))
        if seen_at is not None and self.tick - seen_at < self.dedupe_ticks:
            # The same filing arriving back to back goes out once; otherwise it is billed twice.
            # This is a judgement too, so it goes in the ledger — without it, "why did that
            # filing get no answer?" has no answer.
            decision = Decision(proposal.id, Verdict.DENIED, "직전에 같은 신청이 실행됐습니다",
                                code="duplicate")
            return self._deny(proposal, decision)

        if self.links.lost(proposal.asset_id):
            # An aircraft with a lost link cannot hear commands. A gate before judgement — not
            # recorded on filings that pass; it goes in the check list only on refusal.
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
        """Put up a human card. Its ledger entry stays open until the human's answer (or the
        end of the round) closes it.

        In a live run one aircraft went over its limit and got a 'human' answer 309 times with
        not a single ledger line, and the card survived the round change. If the same card is
        already up, its decision is returned as is (the approval screen doesn't stack duplicate
        cards), but that is a judgement too, so one line is written (outcome waiting).
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
        """Close an open card entry. If it is missing (it shouldn't be), write a fresh pair."""
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

    # ---------- Runtime advisories ----------

    def _record_refusal(self, proposal: Proposal, decision: Decision) -> None:
        """One refusal of a route filing. Three in a row produce an advisory.

        Duplicate refusals don't count — the path isn't blocked; the same filing was sent twice.
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
        """Why this route is blocked right now. Only checks advisory options; files nothing."""
        probe = Proposal(asset_id=refusal.asset, action=refusal.action, cost_usd=0.0,
                         blast_radius="none", rationale="advisory probe",
                         params={**refusal.params, "legs": legs},
                         resource=refusal.resource or refusal.params.get("pad"))
        checks: list[str] = []
        return self.check_route(probe, checks) or self._check_traffic(probe, checks)

    def _notice_until(self, volume_id: str | None) -> int | None:
        """The tick a notice zone lifts; None for permanent zones and buildings."""
        record = self.notices.get(volume_id or "")
        return record.until_tick if record is not None else None

    def _advise(self, asset: str, trigger: str) -> None:
        """Write one advisory. Options are judged against the current state; the wording (if a
        model is available) is written on a separate thread.
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
                return      # round changed while the model answered; skip last round's advisory
            self._ledger_advisory(asset, params, context)

        if self.advisor.has_model and self.advisory_async:
            threading.Thread(target=finish, daemon=True, name=f"advisory-{asset}").start()
        else:
            finish()

    def _ledger_advisory(self, asset: str, params: dict, context: dict) -> None:
        """An advisory is a ledger entry (action advisory, outcome noted). Nothing is executed."""
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
        """Traffic refusal. The code is airspace (the UI replays it like other refusals); the
        policy_hit is traffic.

        What the operator needs to know (the other aircraft, the tick its volume clears) rides
        in detail and goes back in the reply — params stay in the ledger only, not the reply.
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
        """A filing for an aircraft with a lost link. Commands can't reach it, so a clearance
        could not be executed.

        policy_hit stays empty — an operator that learned "this action is banned" from it would
        never file that action again, even after the link returns. Once the link is back, the
        same filing is judged as usual.
        """
        link = self.links.links[proposal.asset_id]
        return Decision(proposal.id, Verdict.DENIED,
                        f"{proposal.asset_id} 의 링크가 틱 {link.since_tick} 부터 끊겨 있습니다 — "
                        "기체가 명령을 들을 수 없습니다", code="lost_link_refused",
                        detail={"resource": proposal.asset_id, "since_tick": link.since_tick,
                                "last_seen_tick": link.last_seen_tick})

    def _contingency_problem(self, proposal: Proposal, checks: list[str]) -> Decision | None:
        """Judge the lost-link contingency volume. An unknown contingency behaviour is refused.

        For continue_and_land the contingency volume is the cleared route itself plus the
        landing column at the destination, so the route/columns/landing checks just passed are
        that judgement — here we only check that the declared behaviour is that one. Other
        behaviours such as return_to_launch create a different volume (the straight line back),
        and clearing without judging it means flying an unjudged path the moment the link drops.
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
        """Judgement context for a ledger entry.

        The tick at the time, airspace version, policies in force and the order of checks.
        """
        if checks is None:
            checks = self._checks.get(proposal.id, []) if proposal is not None else []
        active = [p.id for p in self.policies.all()
                  if p.active_from_tick <= self.tick
                  and (p.active_until_tick is None or self.tick <= p.active_until_tick)]
        return {"tick": self.tick, "airspace_revision": self.airspace.revision,
                "policies": active, "intent_id": intent_id, "checks_run": list(checks)}

    def check_route(self, proposal: Proposal, checks: list[str] | None = None) -> str | None:
        """Does the filed route comply? Drawing routes is not our job.

        The operator knows its aircraft and its schedule and draws the path. All we do is answer
        whether that path is allowed, and if not, say which leg and which zone are the reason.
        The moment we draw the path for them we become the operator, and a bad path becomes our
        responsibility. Authority and execution must stay separate.
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
            # A form problem, caught before judgement. Non-numeric coordinates passed to the
            # judgement functions killed the request with a 500 and the operator never learned
            # why it was refused. If it is not valid form, say so.
            return f"경로 양식이 아닙니다 ({malformed})"
        # The route's two ends must be where the aircraft is now and where it is going. The
        # autopilot drops the first point and flies from its current position to the second,
        # and after the last point it carries on to the delivery site unjudged — a first point
        # filed somewhere else makes the judged path and the flown path different paths.
        checks.append("endpoints")
        astray = self._endpoint_problem(proposal, legs)
        if astray:
            return astray

        checks.append("route")
        found = first_breach(self.airspace, legs)
        if found is not None:
            segment, volume, why, at = found
            # Record what was blocked and why as values. If the UI parsed the sentence back,
            # every wording change would quietly break the UI.
            self._note_block(proposal, volume, segment, volume.rule, at)
            return f"{segment}번 구간이 규정을 어깁니다 — {why}"
        # Vertical segments. The takeoff column, waypoint climbs/descents and the landing
        # column are lines through the airspace too. A 60 m building beside the route does not
        # block a 120 m cruise leg, but it does block a column climbing from 0 m to 120 m.
        checks.append("columns")
        column = self._column_breach(proposal, legs)
        if column is not None:
            kind, segment, volume, why, at = column
            self._note_block(proposal, volume, segment, kind, at)
            what = {"takeoff": "이륙 기둥", "column": f"{segment}번 꼭짓점 승강",
                    "landing": "착륙 기둥"}[kind]
            return f"{what}이 규정을 어깁니다 — {why}"
        # The end of the route is where it touches down. A path that can pass alongside and a
        # spot that can be descended onto are different standards, so the ring around the end
        # point (LANDING_SEPARATION_M) is checked separately for buildings and restricted zones.
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
        """Whether the first point is farther than TRAFFIC_LATERAL_M from the aircraft, or the
        last point from the destination (delivery site or pad).

        Without a position (no telemetry) there is no comparison — that is unknown, not wrong.
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
        """The first breach among the takeoff column, waypoint climbs/descents and the landing
        column, as (kind, segment, volume, why, at).

        Judgement is first_breach alone (G7) — the columns are just cut into several
        zero-length segments. For an airborne aircraft re-filing, the takeoff column runs from
        its current altitude to the first leg's altitude. But if its current altitude at that
        spot already breaks the rules (where a recall left it), that is a fact, not a plan, so
        it is skipped — otherwise the aircraft could not even file a way down and would be
        stuck there.
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

    # ---------- Intents (4D) and traffic ----------

    def _departure(self, proposal: Proposal) -> tuple[int, float]:
        """The tick this filing really departs and its altitude then: (tick, start altitude).

        On the ground: now + clearance confirmation (CLEARANCE_TICKS); if still loading or
        unloading, after that finishes; if the operator deferred departure
        (depart_after_tick), then. Airborne: now, from the current altitude.
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
        """The intent for one filing.

        The operator filed only the path; the timing is ours, computed from declared performance.
        """
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
        """Does this overlap another aircraft's live intent in space and time (F3548 strategic
        deconfliction)?

        First filed wins. The exception is an airborne aircraft's emergency re-filing (after a
        recall): it is not refused, and if the overlapping aircraft has not taken off yet, that
        intent is withdrawn. Re-filing from the ground is cheaper than hovering and waiting. If
        the other aircraft is airborne too, it cannot be withdrawn, so the filing is refused.
        """
        checks = checks if checks is not None else []
        if proposal.action not in ROUTED or not proposal.params.get("legs"):
            return None
        checks.append("traffic")
        intent = self._intend(proposal)
        asset = proposal.asset_id
        airborne = float(self.telemetry.get(asset, {}).get("alt_m") or 0.0) > 1.0
        last_leg = len(proposal.params["legs"]) - 1
        # Aircraft to withdraw. They are only chosen here; the actual withdrawal happens after
        # this filing executes (_on_committed) — if the check withdrew someone else's clearance
        # and this filing then never went out (human hold, limit, autopilot failure), the ground
        # aircraft would lose its route and the airborne one would be left without a clearance.
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
            # A lost-link aircraft's intent cannot be withdrawn — withdrawal is a command to the
            # autopilot, and that aircraft cannot hear it. In that case this filing is refused.
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
        """Everything this filing must avoid: other aircraft's live intents plus the positions
        of airborne aircraft that have no intent.

        An airborne aircraft is always somewhere. If it dropped out of judgement for lacking an
        intent after a recall, withdrawal or declined job, filings through its position would
        be cleared. If telemetry says it is airborne, its position stands as an open column
        (presence) even when the registry has nothing.
        """
        live = [i for i in self.intents.others(asset) if i.asset not in exclude]
        covered = {i.asset for i in live}
        for other, state in self.telemetry.items():
            if other == asset or other in covered or other in exclude or other in self._released:
                # A lost-link aircraft whose reservation a human released: its telemetry is frozen
                # where the link dropped. The aircraft isn't there, and the human released it
                # knowing that.
                continue
            if float(state.get("alt_m") or 0.0) <= 1.0 or state.get("lat") is None:
                continue
            live.append(hold(other, (float(state["lat"]), float(state["lon"])),
                             float(state["alt_m"]), self.tick, self.performance, kind=PRESENCE))
        return live

    def _occupants(self, asset: str, exclude: list[str] | tuple[str, ...] = ()) \
            -> list[tuple[str, tuple[float, float], Intent | None]]:
        """Other aircraft standing on the ground: (aircraft, position, live intent or None).

        Intents of aircraft being withdrawn (exclude) count as absent — the aircraft itself
        still stands there.
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
        """End an intent. If the aircraft is airborne, a holding spot (contingency) takes over.

        A recalled aircraft flies to the nearest way out (exit) and hovers there. That path and
        that spot are not empty — other filings must see them until a new clearance replaces
        them or the aircraft lands.
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
        """Advance intent states from telemetry.

        An aircraft that took off earlier than its cleared window is recorded in the ledger.
        """
        self.intents.observe(self.telemetry, self.tick)
        for intent, planned in self.intents.drain_nonconforming():
            self._ledger_nonconformance(intent, planned)

    def _ledger_nonconformance(self, intent: Intent, planned_tick: int) -> None:
        """The autopilot did not honour a deferred departure. The intent was re-registered at
        the actual departure; this records it.

        Nothing is undone — stopping an airborne aircraft is a recall, and that call isn't made
        here.
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
        """Withdraw an intent that has not taken off. The runtime authors this decision; the
        autopilot only receives a waypoint clear.

        The grounded aircraft just loses its route and stays where it is (sim divert_ground).
        Its operator sees the route is gone and files again — by then the airborne one counts
        as having filed first.
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
        # Drop the withdrawn route's commit record from dedupe. Otherwise that aircraft's
        # re-filing is refused as "the same filing just executed" — even though what executed
        # was just withdrawn.
        for action in ROUTED:
            self._recent_commits.pop((intent.asset, action), None)
        return decision

    def _on_committed(self, proposal: Proposal, decision: Decision, entry) -> dict | None:
        """Right after a successful execution. For a route, (withdraw the chosen aircraft and)
        create the intent; for any other action, end that aircraft's intent.

        Withdrawal happens only here — a filing that did not execute withdraws no one.
        """
        if proposal.action in ROUTED and proposal.params.get("legs"):
            self.advisor.succeeded(proposal.asset_id)   # cleared, so the refusal streak ends
            # If the corridor enters a neighbourhood not yet asked about this round, the
            # pre-flight briefing asks about that cell (next poll, worker thread). Only the cell
            # is noted here — the approval thread does not wait on the network.
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
            entry.proposal = proposal.to_dict()   # closing line holds the filing after withdrawals
            return {"intent_id": intent.id, **({"withdrew": withdrew} if withdrew else {})}
        if proposal.action == "decline_job":
            # Declining the job after refusals ("no legal route"). Written after the decline
            # executes — writing it at filing time would add another advisory for a decline that
            # was refused as a duplicate. The order is gone, so the refusal streak ends too.
            if self.advisor.declined(proposal.asset_id):
                self._advise(proposal.asset_id, "decline_after_refusals")
            self.advisor.succeeded(proposal.asset_id)
        ended = self._end_intent(proposal.asset_id, proposal.action,
                                 exit_point=proposal.params.get("exit"))
        return {"intent_id": ended.id} if ended is not None else None

    def _rejudge(self, proposal: Proposal, decision: Decision) -> str | None:
        """Judge again right before execution, against the current airspace and intents. If
        blocked, close it as a refusal and return the reason.

        Judgement happens once, at filing (file). When a zone notice arrived while a filing
        waited for human approval or sat in a resource queue, the later execution sent a route
        judged against the old airspace into a closed zone. "Every executed route passed
        judgement against the runtime's airspace" must hold at execution time. Judgement takes
        milliseconds, so running it every time is cheaper than remembering the airspace version
        and re-checking only on change.
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
        """The reason to refuse right before execution, or None.

        Checked in order: link → airspace → traffic → policy. If the link dropped while waiting
        for human approval or an arbitration grant, that approval must not send a command to an
        aircraft that cannot hear it. With an adapter that reports a command as received once
        sent (MAVLink says ok on send), the new route ended the lost-link reservation, and
        another aircraft's crossing was cleared through the path the aircraft was actually
        still flying. A ban (weather hold, airworthiness directive) may also have arrived
        meanwhile, so the same policy check as at filing runs once more.
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
                # Already queued. The same aircraft does not queue twice for the same resource.
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
        # A human's answer queues on the judgement lock too — an approval leads to rejudge,
        # execution and intent registration.
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
        # The card's line closes with the human's answer; the rejudge and execution that follow
        # write their own lines.
        self._close_card(card, proposal, decision, "approved")
        if self._rejudge(proposal, decision):
            return decision   # airspace changed while waiting; approval can't revive the old route
        return self._queue_or_commit(proposal, decision)

    # ---------- Resource arbitration ----------

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
        """Grant queued resources.

        This rejudges, executes and registers intents, so it holds the same lock as file()
        (_judging).
        """
        for resource, waiting in batches.items():
            # The airspace may have changed while queued. Blocked routes don't enter arbitration.
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
            # The model's reason for its pick goes on the record. What it picked is one number,
            # and if that number is out of range the rule picked instead — the reason explains,
            # it does not decide.
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

    # ---------- News from outside ----------

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
        """Fetch the simulator's airspace in one go. Load it all at once, then start judging.

        If the fetch fails or comes back empty, try again on the next poll. When volumes were
        added one by one, HTTP threads judged against a half-filled airspace (Airspace.add_all
        loads with a single swap).
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
        """On a new round, clear the per-round state — budget, locks, dedupe, intents, notices.

        The ledger stays. It is history, and a new round does not undo it.
        Notice policies are cleared too; reposted under the same id, they apply again then.
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
        # Links are per-round too — the new round's aircraft are freshly placed and their
        # heartbeat stamps count from scratch. Standing lost-link cards come down with the other
        # cards in _expire_cards below.
        with self._link_lock:
            self.links.clear()
        self._dark.clear()
        self._link_cards.clear()
        self._released.clear()
        # Aircraft registrations survive the round (the processes keep running); only their
        # ticks move onto the new round's clock.
        with self._guard:
            for row in self.agents.values():
                row["last_seen_tick"] = min(int(row["last_seen_tick"]), self.tick)
        self._expire_cards("판이 바뀜")
        self.notices.clear()
        # An open weather hold gets a closing line. Without one, the report lists that hold as
        # "open" forever.
        if self.intake.hold is not None:
            self._ledger_hold_end(self.intake.hold, "weather_hold_closed",
                                  f"판이 바뀌어 닫힘 (창은 틱 {self.intake.hold.until_tick} 까지)")
        self._close_rules("round")
        self.intake.clear()
        # METAR is the observation in force now. A new round does not stop the gusts, so the
        # last observation goes into the new round's first poll — waiting for the next cycle (up
        # to METAR_PERIOD_S later) would let the new round's aircraft take off into the gusts.
        with self._guard:
            self._intake_inbox.extend(dict(item) for item in self._metar_current)
        with self._guard:
            self.advisor.clear()

    def _expire_cards(self, why: str) -> None:
        """When the round ends, human cards come down too: held notices as lapsed, the rest
        because the round changed.

        In a live run five cards from the previous round were still up after the change — no
        one should approve a previous round's over-limit card for the new round's aircraft.
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
        """Box around landing sites and pads + margin. Model-structured notices must lie inside."""
        points = [(a["lat"], a["lon"]) for a in self.landing_areas] + list(self.pad_coords.values())
        if not points:
            return None
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        return (min(lats) - SERVICE_MARGIN_DEG, min(lons) - SERVICE_MARGIN_DEG,
                max(lats) + SERVICE_MARGIN_DEG, max(lons) + SERVICE_MARGIN_DEG)

    def _lapse_held(self, record, why: str) -> None:
        """A held notice lapsed unapproved. It never applied, so only the record is closed."""
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
        """Post a held notice to the approval screen; it blocks nothing until a human approves.

        Notices the model structured, and notices from outside the runtime's feed (search, manual
        input), come here. A held notice is shaped like a filing (action publish_notice), so the
        existing approval screen shows it as is."""
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

    # ---------- Intake: weather, incidents, restrictions ----------

    def _ground_for_hold(self, hold: WeatherHold) -> None:
        """Pull back routes cleared on the ground but not yet flown. Airborne aircraft are left
        alone: they need to come down.

        Policies block only new filings. Departing on a route already cleared (25 ticks after
        the clearance is confirmed) is not a filing, so unless pulled back it takes off during
        the hold.
        """
        for asset_id, state in list(self.telemetry.items()):
            if float(state.get("alt_m") or 0.0) > 1.0:
                continue
            # The telemetry route is from the last poll. A route cleared on the very tick the
            # hold opened wasn't there yet, so it was never pulled back and took off 25 ticks
            # later, mid-hold (one 'takeoff during hold' on the runtime side).
            # Go by the intent the runtime recorded: an intent that is cleared but hasn't
            # departed gets pulled back.
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
        """The 'lift early?' card on the approval screen. Approval lifts the hold on the spot;
        refusal keeps it until the window ends."""
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
        """The hold's circumstances changed (a within-limits report came later, the window was
        extended). The hold doesn't lift itself; the card is made to state the current window
        and that fact."""
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
        """Weather over limits, but not a grammar read of the runtime's feed (model, search,
        manual). Nothing is held until a human approves; the card states the window the hold
        would cover (until tick)."""
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

    def _drop_card(self, proposal_id: str | None, code: str, why: str) -> None:
        """Take down one standing card (lapsed). No-op if a human already answered."""
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

    def background(self) -> None:
        """Pulls the world in. Never on the arbitration thread (see settle_forever below)."""
        while True:
            # A failure is retried next cycle. If this thread dies, the runtime keeps judging
            # against a stale world, the worst state there is: silently wrong.
            try:
                self._pull_world()
            except Exception as error:  # noqa: BLE001 — staying alive comes first
                print(f"runtime background: {error!r}", flush=True)
            time.sleep(0.25)

    def settle_forever(self) -> None:
        """A thread that only arbitrates resources, so a slow model never stops the world clock.

        With arbitration on the world-update thread, ticks, positions and notices go 3 s stale
        while Ultra thinks for 3 s, and filings arriving meanwhile are judged at old positions.
        Arbitration waits window_s before acting anyway, so running it apart delays nothing but
        arbitration.
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
        # Search only with a key, on its own thread. Results go into the inbox for the world
        # thread to read on its next poll; the world thread never waits on the network.
        if self.tavily is not None and self.intake_poller is None:
            self.intake_poller = IntakePoller(self.tavily, self.config.intake.queries,
                                              INTAKE_PERIOD_S, self.take_in)
            threads.append(self.intake_poller.start())
        # METAR on its own thread too. It runs without a key; when unreachable, the source
        # gets one 'off' line.
        if self.metar is not None and self.metar_poller is None:
            self.metar_poller = MetarPoller(self.metar, METAR_PERIOD_S, self.take_metar)
            threads.append(self.metar_poller.start())
        return threads

    def recall_flights(self, volume) -> list[Decision]:
        """Re-judge routes already cleared and in flight against a new zone.

        Refusing is not enough: a flight cleared before the rule arrived doesn't know the rule
        and keeps flying. Having an enforcement point means undoing what has already happened.
        A recalled aircraft's route intent ends; that route won't be flown any more, so it must
        not block anyone else. The way out, and a place to hover there (contingency), take its
        place.
        """
        pulled = []
        for asset_id, telemetry in self.telemetry.items():
            if self.links.lost(asset_id):
                # A recall can't reach an aircraft with a lost link, so none is sent. Its space
                # stays reserved and judgement keeps avoiding that volume.
                continue
            legs = [{"lat": telemetry.get("lat"), "lon": telemetry.get("lon"),
                     "alt_m": telemetry.get("alt_m", 0.0)}]
            legs += [{"lat": leg["lat"], "lon": leg["lon"], "alt_m": leg.get("alt_m", 0.0)}
                     for leg in (telemetry.get("route") or [])]
            if len(legs) < 2 or legs[0]["lat"] is None:
                continue
            if first_breach(Airspace([volume], default_ceiling_m=None), legs) is None:
                continue
            # An aircraft inside is sent to the nearest point outside. Stopped in place, it
            # stays in the closed zone, where every route is forbidden from its first point
            # and nothing can be redrawn.
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
            # The adapter is handed the ledger id (a string). Passing the ledger entry object
            # itself made the HTTP adapter fail to serialise it to JSON, which killed the
            # background thread; from then on the runtime kept serving old positions and every
            # filing started from the wrong place. Tests using only the local adapter missed it.
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
        """A ban arrived on a resource already held: revoke it and divert the aircraft.

        This is what separates it from merely refusing: an enforcement point undoes what has
        already happened. The diversion goes out without checking limits, since leaving a
        closed zone is not a budget question. Who ordered it and why still goes on the ledger.
        """
        if not policy.forbid_resource:
            return None
        hold = self.locks.holder(policy.forbid_resource)
        if hold is None or self.links.lost(hold.asset_id):
            return None     # no holder, or its link is lost so it can't hear the divert order

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

    # ---------- Aircraft registration ----------

    def register_agent(self, body: dict) -> tuple[int, dict]:
        """POST /agents/register. An aircraft process says what writes its filings. Only a
        label."""
        try:
            identity = AgentIdentity.from_dict(body or {})
        except ValueError as error:
            return 400, {"error": str(error)}
        if identity.world != "guarded":
            return 400, {"error": "직결 세계는 런타임에 신청하지 않습니다 — 그 모델은 시뮬레이터가 "
                                  "기체마다 싣습니다(DIRECT_MODEL)"}
        # The world (telemetry) knows the fleet roster. Accepting a name off the roster turns the
        # screen's 'drones · … ×4' into ×5. Before the world has arrived, 503: the aircraft
        # registers again a few seconds later.
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
        """/state.agents. Drops aircraft not heard from for AGENT_STALE_TICKS: a screen still
        showing a dead process's model name would be lying."""
        with self._guard:
            for asset in [a for a, row in self.agents.items()
                          if self.tick - int(row["last_seen_tick"]) > AGENT_STALE_TICKS]:
                del self.agents[asset]
            return {asset: {key: row.get(key) for key in AGENT_FIELDS}
                    for asset, row in self.agents.items()}

    # ---------- Lost link ----------

    def watch_links(self) -> list[LinkEvent]:
        """Watch the telemetry heartbeat. The world thread (_pull_world) and the harness call it
        every poll."""
        with self._link_lock:
            events = self.links.observe(self.telemetry, self.tick)
        for event in events:
            if event.kind == "lost":
                self._link_lost(event)
            else:
                self._link_restored(event)
        return events

    def _link_lost(self, event: LinkEvent) -> None:
        """Link lost. Nothing is sent to the aircraft (it can't hear). Its intent stays reserved
        (position unknown, so the whole remaining route up to the nominal landing plus margin),
        a ledger line is written and a human card goes up. Reserving tightens, so it applies on
        that tick; releasing early is a human's call."""
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
        """Lost-link notice on the approval screen. Approval releases the held space now;
        refusal keeps it until telemetry returns. When telemetry returns, the card comes down
        by itself."""
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
        """Telemetry is back. Check whether the aircraft stayed inside the cleared volume while
        the link was lost (conformance), undo the extended window and take the card down. It is
        visible again, so judgement goes by telemetry from here."""
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
        self._observe()     # advance the intent from fresh telemetry (arrived if it has landed)

    def _ledger_link(self, code: str, asset: str, reason: str, detail: dict,
                     intent: Intent | None) -> None:
        """One line for a lost or restored link (outcome noted). Nothing is executed: an
        aircraft with a lost link can't hear."""
        noted = Proposal(asset_id=asset, action=code, cost_usd=0.0, blast_radius="none",
                         author="runtime", rationale=reason[:180], params=dict(detail))
        decision = Decision(noted.id, Verdict.AUTO, reason, code=code, detail=dict(detail))
        entry = self.ledger.open_entry(
            noted, decision, self._context(None, ["link"], intent.id if intent else None))
        self.ledger.close_entry(entry, "noted")

    def _ledger_link_nonconformance(self, event: LinkEvent, intent: Intent, at) -> None:
        """It left the cleared volume while the link was lost. Not undone, only recorded."""
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
        """Telemetry is back; take the lost-link card down (lapsed). No-op if a human already
        answered."""
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
        """The human's answer. Approval releases the held space now: it means the human knows
        where the aircraft is, and that responsibility stays on the ledger under their name.
        Refusal keeps it until telemetry returns. Either way nothing is sent to the aircraft."""
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

    # ---------- What the screen shows ----------

    def snapshot(self) -> dict:
        self._observe()
        with self._guard:
            pending = [p.to_dict() for p in self._awaiting_human.values()]
            waiting = {r: len(v) for r, v in self._contended.items()}
        return {
            "tick": self.tick,
            "config": self.config.name,
            # The model is visible but has no say: which server, how many calls, and how often
            # the rules stood in. Only the runtime's own calls (arbitration, notice structuring)
            # are counted here; the aircraft processes count their own.
            "llm": {"enabled": self.llm.enabled, "models": self.llm.models,
                    "host": self.llm.host, "calls": self.llm.stats_dict()},
            "locks": self.locks.snapshot(),
            "contended": waiting,
            "awaiting_human": pending,
            "policies": [vars(p) for p in self.policies.all()],
            # Where cleared routes will be, and when. The screen draws who is waiting for whom
            # from this.
            "intents": self.intents.snapshot(),
            # Notices in force. The banner comes from here, not the simulator: what is enforced
            # is what is shown. Held records ride along too: until a human approves, applied is
            # False and they block nothing. The banner shows them as 'waiting for a person'.
            "notices": self.notices.snapshot(),
            # Runtime advisories, the latest per aircraft. Information only; they change nothing.
            "advisories": self.advisor.snapshot(),
            # Intake: which sources are on and what was read, the active weather hold, and
            # incident zones.
            "intake": self._intake_snapshot(),
            "weather": self.intake.weather_snapshot(),
            "incidents": incident_snapshot(list(self.notices.records.values()), self.tick),
            # Pre-flight briefing (Tavily): where it came from (live|recorded|off), credits
            # spent, summary, and what was read with its sources.
            "briefing": self.briefing.snapshot(),
            # What writes each aircraft's filings (the registered model). A label, unrelated to
            # judgement.
            "agents": self.agents_snapshot(),
            # Telemetry heartbeat. A lost aircraft's space stays reserved.
            "links": self._links_snapshot(),
            # The real autopilot behind the runtime (ADAPTER=composite). Read-only: judgement
            # still uses only telemetry from the world of record (the simulator).
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
        """The ledger folded into flights. Built only from the ledger file; memory holds just
        200 lines."""
        built = build_report(self.ledger.read_all(), self.tick, self.airspace.revision, asset)
        # Intake (sqlite) too: the same facts as the ledger's intake lines, folded into what
        # became what.
        built["intake"] = self.store.report()
        return to_markdown(built) if fmt == "md" else built

    def _links_snapshot(self) -> dict:
        with self._link_lock:
            return self.links.snapshot()

    def _autopilots_snapshot(self) -> dict:
        """Only an adapter with a mirror answers. Empty in the simulator-only wiring."""
        view = getattr(self.adapter, "autopilots", None)
        return view() if callable(view) else {}


def _load_addresses(path: str) -> list[dict]:
    """Addresses from the gazetteer. No file means an empty list, and incidents that give an
    address are then unreadable."""
    try:
        return list(json.loads(Path(path).read_text(encoding="utf-8")).get("addresses") or [])
    except (OSError, ValueError, AttributeError):
        return []


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot((b[0] - a[0]) * METRES_PER_DEG_LAT, (b[1] - a[1]) * METRES_PER_DEG_LON)


def _position(state: dict) -> tuple[float, float, float] | None:
    """Position from telemetry (lat, lon, alt_m), or None if unknown."""
    if state.get("lat") is None or state.get("lon") is None:
        return None
    return float(state["lat"]), float(state["lon"]), float(state.get("alt_m") or 0.0)


def _position_dict(at: tuple[float, float, float] | None) -> dict | None:
    if at is None:
        return None
    return {"lat": round(at[0], 6), "lon": round(at[1], 6), "alt_m": round(at[2], 1)}


def main() -> None:
    runtime = Runtime(
        config_path=os.getenv("CONFIG", "configs/fleet.yaml"),
        sim_url=os.getenv("SIM_URL", "http://sim:8100"),
        ledger_path=os.getenv("LEDGER_PATH", "ledger.jsonl"),
        window_s=float(os.getenv("ARBITRATION_WINDOW_S", "1.5")),
        # Intake record. Under compose: /data/intake.sqlite (same volume as the ledger).
        intake_db=os.getenv("INTAKE_DB") or STORE_DEFAULT_PATH,
        metar=True,
        await_airspace=True,
        # Pre-flight briefing. Without a key it plays recordings (tests/fixtures/tavily) and
        # the screen shows 'recorded'.
        briefing=True,
    )
    runtime.start_background()

    server = JsonServer(int(os.getenv("PORT", "8000")))
    server.add("POST", "/proposals", lambda body, query: (200, runtime.file(body).to_dict()))
    # Aircraft processes introduce themselves (model, server), again every 30 s.
    server.add("POST", "/agents/register", lambda body, query: runtime.register_agent(body))
    server.add(
        "POST",
        "/approve",
        lambda body, query: _approval(runtime, body, allow=True),
    )
    server.add("POST", "/deny", lambda body, query: _approval(runtime, body, allow=False))
    server.add("GET", "/state", lambda body, query: (200, runtime.snapshot()))
    # Send the airspace revision along, so when a zone newly closes the operator refreshes its
    # copy and routes around it from the start; otherwise it files a straight line into the
    # closed zone and only finds out from the refusal.
    # The tick goes along too: a refiling that delays departure (depart_after_tick) must be
    # stated in the world's clock.
    server.add(
        "GET",
        "/telemetry/{asset}",
        lambda body, query, asset: (200, {**runtime.telemetry.get(asset, {}),
                                          "airspace_revision": runtime.airspace.revision,
                                          "tick": runtime.tick}
                                    if asset in runtime.telemetry else {}),
    )
    # Landing sites go along too; the operator computes its service area (the box model drafts
    # must not leave) from them. Data of the same kind as pad coordinates, unrelated to
    # judgement.
    server.add("GET", "/airspace", lambda body, query: (200, {
        "volumes": [v.to_dict() for v in runtime.airspace.all()],
        "pads": {n: {"lat": a[0], "lon": a[1]} for n, a in runtime.pad_coords.items()},
        "landing_areas": runtime.landing_areas,
    }))
    # ready means ready to judge (all airspace received); the compose healthcheck waits on it
    # before starting the aircraft. Alive (ok) and able to judge (ready) are different
    # questions, so both are here.
    server.add("GET", "/health", lambda body, query: (200, {
        "ok": True, "tick": runtime.tick, "ready": runtime.ready,
        "airspace_revision": runtime.airspace.revision}))
    # Intake input (demo, manual injection): queues one sentence in the inbox and returns.
    # The world thread does the reading.
    server.add("POST", "/intake", lambda body, query: runtime.submit_intake(body))
    # Run the pre-flight briefing again; the worker thread asks on the next poll. 503 when
    # off, 409 while one is running.
    server.add("POST", "/briefing/run", lambda body, query: runtime.briefing.request_run())
    # Ledger report. ?asset=<id> for one aircraft, ?format=md for a human-readable table.
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
