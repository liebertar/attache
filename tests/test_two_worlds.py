"""The claim this project rests on.

Same seed, same vehicles, same detection code, same proposal writer. One world files its
requests with the runtime; the other holds the actuator address. The unguarded agents are
not sabotaged: they honour their own per-vehicle budget, they read the recall bulletin and
obey it, and they are shown the whole fleet's state, which the guarded agents never see.

They still fail, because a rule that lives in each agent is not a rule.
"""

import json
import random
import re
import unittest

from attache.agent.detect import detect
from attache.agent.drafter import ModelDrafter, service_bbox
from attache.agent.planner import OperatorPlanner
from attache.agent.propose import COSTS, by_rule
from attache.core.config import load as config_load
from attache.core.geo import first_breach
from attache.core.models import Proposal, Verdict
from attache.core.route import Router
from attache.llm.client import LlmReply, TieredLlm
from attache.runtime.service import Runtime
from sim.world import LANDING_AREAS, RECALL, RECALL_TICK, ZONE, ZONE_TICK, Simulation

# 22 m/s 로 날면 브루클린-맨해튼 한 번 왕복이 1100틱 안팎입니다.
# 구역 폐쇄(560~900틱) 이후까지 봐야 두 세계가 갈리는 지점이 나옵니다.
# 서비스 반경 11km. 한 바퀴(적재 → 배달 두 곳 → 창고)가 2천 틱 안팎이라 한 바퀴는 보려면
# 이만큼 돌려야 합니다. 구역 폐쇄(560~900틱)와 감항성 지시(1050~1350틱)는 그 안에 듭니다.
TICKS = 4000
PADS = ["pad:launch"]


class LocalAdapter:
    """HTTP 없이 같은 프로세스에서 세계를 때립니다. 경로는 동일합니다."""

    def __init__(self, world, clock):
        self.world = world
        self.clock = clock

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        return self.world.act(asset_id, action, params, ledger_id, blast, approved_by,
                              self.clock())


class JudgingAdapter(LocalAdapter):
    """조종장치 문턱에서 한 번 더 셉니다: 실행되는 경로마다 그 순간의 공역으로 다시 판정합니다.

    런타임이 승인한 것만 여기 옵니다. 그래도 세어 두는 이유는 '실행된 경로는 전부 판정을
    지난 신청서에서 왔다'를 점수판이 아니라 실행 지점에서 직접 보기 위해서입니다.
    """

    def __init__(self, world, clock, airspace):
        super().__init__(world, clock)
        self.airspace = airspace
        self.routes = 0
        self.unjudged = 0          # 실행 순간 판정을 못 넘는 경로. 0 이어야 합니다
        self.without_receipt = 0   # 원장 번호 없이 온 실행. 0 이어야 합니다

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        if action in ("fly_route", "reserve_pad") and params.get("legs"):
            self.routes += 1
            if not ledger_id:
                self.without_receipt += 1
            if first_breach(self.airspace, params["legs"]) is not None:
                self.unjudged += 1
        return super().execute(asset_id, action, params, ledger_id, blast, approved_by)


BUDGET_ESCALATIONS = {"per_asset_usd", "fleet_usd"}


class GuardedSide:
    """운영사 쪽. 길은 우리가 그리고, 되는지는 런타임에 묻습니다.

    loop.py 의 _file_with_route 를 그대로 옮겨 적었습니다(sleep 만 뺌).
    drafter 가 있으면 거절 뒤에 모델이 먼저, 안 되면 A* — 같은 순서입니다.
    """

    def __init__(self, runtime, drafter=None):
        self.runtime = runtime
        self.planner = OperatorPlanner(runtime.airspace)
        self.drafter = drafter          # None 이면 A* 만. 시험이 stub/fixture/chaos 를 꽂습니다
        self.preferred_alt_m = Router.cruise_alt_default()
        self.pad_index = {}
        self.banned = {}
        self.cooldown = {}

    def run_tick(self, snapshot):
        for asset_id, telemetry in snapshot["assets"].items():
            concern = detect(telemetry)
            if concern is None:
                continue
            index = self.pad_index.setdefault(asset_id, 0)
            banned = self.banned.setdefault(asset_id, set())
            open_pads = [pad for pad in PADS if pad not in banned] or PADS
            proposal = by_rule(
                concern, telemetry, open_pads[index % len(open_pads)], frozenset(banned)
            )
            if snapshot["tick"] < self.cooldown.get((asset_id, proposal.action), 0):
                continue
            decision = self._file(proposal, telemetry)
            if decision.verdict in (Verdict.DENIED, Verdict.HUMAN, Verdict.QUEUED):
                self.cooldown[(asset_id, proposal.action)] = snapshot["tick"] + 12
            if decision.verdict is Verdict.DENIED:
                if decision.policy_hit:
                    # 자원이 막힌 걸 행동이 막힌 걸로 배우면 영영 신청을 못 합니다
                    banned.add(decision.forbids or proposal.action)
                elif proposal.resource:
                    self.pad_index[asset_id] = (index + 1) % len(PADS)
        self.runtime._settle_contended()
        self._controller_reviews()

    def _destination(self, proposal, telemetry):
        if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
            return (telemetry["job_lat"], telemetry["job_lon"])
        if proposal.action == "reserve_pad" and proposal.resource:
            at = self.runtime.pad_coords.get(proposal.resource)
            return at
        return None

    def _file(self, proposal, telemetry):
        here = (telemetry.get("lat"), telemetry.get("lon"))
        goal = self._destination(proposal, telemetry)
        if here[0] is None or goal is None:
            return self.runtime.file(proposal.to_dict())

        # 먼저 최단 직선으로 냅니다. 운영사는 원래 제일 싼 길을 냅니다.
        proposal.params = {**proposal.params, "legs": self.planner.straight(here, goal),
                           "drafter": "straight", "draft_attempts": 0}
        decision = self.runtime.file(proposal.to_dict())
        if decision.policy_hit != "airspace":
            return decision
        # 다시 그리라고 했습니다. 모델이 먼저, 안 되면 A*.
        legs, drafter, attempts = self._redraw(here, goal, decision)
        if not legs:
            if proposal.action != "fly_route" or self.planner.start_blocked(here, telemetry):
                return decision   # 이륙장을 못 간다고, 출발점이 막혔다고 주문을 반려하지는 않습니다
            declined = Proposal.from_dict({**proposal.to_dict(), "action": "decline_job",
                                           "cost_usd": 0.0, "blast_radius": "none",
                                           "params": {}, "resource": None})
            return self.runtime.file(declined.to_dict())
        redrawn = Proposal.from_dict({**proposal.to_dict(),
                                      "params": {**proposal.params, "legs": legs,
                                                 "drafter": drafter, "draft_attempts": attempts}})
        return self.runtime.file(redrawn.to_dict())

    def _redraw(self, here, goal, refusal):
        """loop.py GuardedAgent._redraw 와 같은 순서. 모델 초안 → 안 되면 A*."""
        attempts = 0
        if self.drafter is not None:
            legs = self.drafter.draft(here, goal, {"reason": refusal.reason,
                                                   "forbids": refusal.forbids})
            attempts = self.drafter.last_attempts
            if legs:
                return legs, self.drafter.name, attempts
        return self.planner.draw(here, goal), "astar", attempts

    def _controller_reviews(self):
        """원격 관제사. 안전 때문에 올라온 건 승인하고, 예산 초과는 거부합니다."""
        for proposal_id in list(self.runtime._awaiting_human):
            decision = self.runtime._decisions[proposal_id]
            allow = decision.authority_hit not in BUDGET_ESCALATIONS
            self.runtime.approve(proposal_id, "원격 관제사", allow=allow)


class DirectSide:
    """스스로 규칙을 지키려는, 잘 만든 에이전트들."""

    def __init__(self, world, per_asset_limit=200.0, bulletin_lag_ticks=25):
        self.world = world
        self.per_asset_limit = per_asset_limit
        self.bulletin_lag = bulletin_lag_ticks
        self.spend = {}
        self.banned = {}

    def run_tick(self, snapshot, tick, bulletins):
        # 강제점이 없으니 기체마다 따로 공지를 확인합니다. 확인 주기만큼 늦습니다.
        for asset_id, telemetry in snapshot["assets"].items():
            phase = sum(ord(c) for c in asset_id) % self.bulletin_lag
            if not bulletins or tick % self.bulletin_lag != phase:
                continue
            for item in bulletins:
                if item.get("applies_to", {}).get("model") not in (None, telemetry["model"]):
                    continue
                seen = self.banned.setdefault(asset_id, set())
                if item.get("forbid_action"):
                    seen.add(item["forbid_action"])
                if item.get("forbid_resource"):
                    seen.add(item["forbid_resource"])

        # 세 기체가 같은 주기로 상태를 읽습니다. 읽은 뒤 쓰기까지가 비어 있습니다.
        for asset_id, telemetry in snapshot["assets"].items():
            concern = detect(telemetry)
            if concern is None:
                continue
            banned = self.banned.get(asset_id, set())
            pad = self._free_looking_pad(snapshot, asset_id, banned)
            proposal = by_rule(concern, telemetry, pad)
            if proposal.action in banned or (proposal.resource and proposal.resource in banned):
                continue
            spent = self.spend.get(asset_id, 0.0)
            if spent + COSTS.get(proposal.action, 0.0) > self.per_asset_limit:
                continue
            if proposal.action in ("fly_route", "reserve_pad"):
                goal = None
                if proposal.action == "fly_route" and telemetry.get("job_lat") is not None:
                    goal = (telemetry["job_lat"], telemetry["job_lon"])
                if goal:
                    proposal.params = {**proposal.params, "legs": OperatorPlanner.straight_at(
                        (telemetry["lat"], telemetry["lon"]), goal,
                        Router.cruise_alt_default())}
            result = self.world.act(asset_id, proposal.action, proposal.params, None,
                                    proposal.blast_radius, None, tick)
            if result.get("ok"):
                self.spend[asset_id] = spent + result.get("cost_usd", 0.0)

    @staticmethod
    def _free_looking_pad(snapshot, asset_id, banned=frozenset()):
        # 위치는 Remote ID 로 보이지만 예약 의도는 안 보입니다. 회사가 다르면 더욱.
        taken = {
            vehicle.get("assigned_pad")
            for vid, vehicle in snapshot["assets"].items()
            if vid != asset_id and vehicle.get("state") in ("landed", "charging")
        }
        open_pads = [pad for pad in PADS if pad not in banned]
        for pad in open_pads:
            if pad not in taken:
                return pad
        return open_pads[0] if open_pads else PADS[0]


CONFIG = "configs/fleet.yaml"


def run(tmp_ledger: str, ticks: int = TICKS, drafter_factory=None, adapter_factory=None,
        seed: int = 7):
    """한 판. drafter_factory(runtime, planner) 가 있으면 운영사가 그 초안기를 먼저 씁니다.

    adapter_factory(world, clock, airspace) 로 조종장치 문턱을 바꿔 낄 수 있습니다.
    """
    # 점수판이 재는 한도와 런타임이 강제하는 한도는 같은 숫자여야 합니다.
    # 따로 적어두면 런타임 기준으로는 정상인데 점수판만 초과라고 합니다.
    limit = config_load(CONFIG).authority.fleet_usd
    simulation = Simulation(seed=seed, fleet_limit=limit)
    runtime = Runtime(CONFIG, "http://unused", tmp_ledger, window_s=0.0)
    guarded_world = simulation.worlds["guarded"]
    if adapter_factory is None:
        adapter = LocalAdapter(guarded_world, lambda: simulation.tick_count)
    else:
        adapter = adapter_factory(guarded_world, lambda: simulation.tick_count, runtime.airspace)
    runtime.adapter = adapter
    runtime.committer.adapter = adapter

    from attache.core.geo import Volume

    opening = guarded_world.snapshot(0, volumes=True)
    for raw in opening["volumes"]:
        runtime.airspace.add(Volume.from_dict(raw))
    runtime.pad_coords = {
        name: (at["lat"], at["lon"])
        for name, at in opening["pad_coords"].items()
    }
    runtime.landing_areas = list(opening.get("landing_areas") or [])
    guarded = GuardedSide(runtime)
    if drafter_factory is not None:
        guarded.drafter = drafter_factory(runtime, guarded.planner)
    direct = DirectSide(simulation.worlds["direct"])
    # 기체가 판 동안 무엇을 했는지. 점수판은 규칙 위반을 세고, 이건 순환이 실제로 도는지 봅니다.
    trace = {vid: {"states": set(), "delivered": 0, "hovering": 0, "max_load": 0, "home": 0}
             for vid in guarded_world.vehicles}
    at_home = dict.fromkeys(guarded_world.vehicles, True)

    for _ in range(ticks):
        simulation.step()
        tick = simulation.tick_count
        for vid, vehicle in guarded_world.vehicles.items():
            row = trace[vid]
            row["states"].add(vehicle.state)
            row["delivered"] = vehicle.delivered
            row["max_load"] = max(row["max_load"], vehicle.load)
            # 공중에 떠서 승인된 갈 곳 없이 기다린 틱. 0에 가까워야 합니다(E0-4).
            if vehicle.state == "cruising" and vehicle.alt > 1.0 and not vehicle.waypoints:
                row["hovering"] += 1
            # 배달을 마치고 창고 마당의 제 자리에 돌아온 횟수
            home = vehicle.state == "ready" and vehicle.load == 0 and vehicle.job_x is None
            if home and not at_home[vid]:
                row["home"] += 1
            at_home[vid] = home or vehicle.state in ("loading", "landed", "charging")

        # 제한하는 공지는 런타임이 도착 즉시 겁니다. 푸는 정책만 사람이 풉니다.
        # 실서비스와 같은 코드로 받습니다 — 두 벌로 적으면 갈라집니다.
        runtime.absorb(simulation.bulletins())

        runtime.tick = tick
        guarded_snapshot = guarded_world.snapshot(tick)
        runtime.telemetry = guarded_snapshot["assets"]
        guarded.run_tick(guarded_snapshot)

        direct.run_tick(simulation.worlds["direct"].snapshot(tick), tick, simulation.bulletins())

    return (
        guarded_world.snapshot(ticks)["scoreboard"],
        simulation.worlds["direct"].snapshot(ticks)["scoreboard"],
        trace,
    )


def fleet_bbox():
    return service_bbox([(a["lat"], a["lon"]) for a in LANDING_AREAS])


class ChaosLlm(TieredLlm):
    """무작위·악의적 초안을 내는 모델 흉내.

    건물 한가운데를 지나는 선, 0ft 격자 안으로 들어가는 선, 5000m·-5m 고도, 상자 밖 좌표,
    구간 13개, 쓰레기 문장, 생각 속에만 있는 JSON, 그리고 가끔은 그럴듯한 직선. 어느 것도
    실행되면 안 되고, 어느 것도 런타임을 죽이면 안 됩니다.
    """

    KINDS = ("through_building", "into_cell", "absurd_altitude", "out_of_box", "too_many",
             "garbage", "think_only", "random_walk", "straight", "no_reply")

    def __init__(self, airspace, seed=11):
        super().__init__(base_url="http://chaos", models={"nano": "chaos-nano"},
                         timeout_s=0.0, request_extra={}, record_dir="")
        self.rng = random.Random(seed)
        volumes = airspace.all()
        self.buildings = [v for v in volumes if v.id.startswith("bldg-") and v.polygon]
        self.cells = [v for v in volumes if v.rule == "forbidden" and not v.id.startswith("bldg-")
                      and v.polygon]
        self.kinds_served: dict[str, int] = {}

    @staticmethod
    def _centre(volume):
        return (sum(p[0] for p in volume.polygon) / len(volume.polygon),
                sum(p[1] for p in volume.polygon) / len(volume.polygon))

    def ask(self, tier, system, user, max_tokens=400, json_object=False, timeout_s=None):
        match = re.search(r"origin ([-\d.]+),([-\d.]+) -> goal ([-\d.]+),([-\d.]+)", user)
        start = (float(match.group(1)), float(match.group(2)))
        goal = (float(match.group(3)), float(match.group(4)))
        kind = self.rng.choice(self.KINDS)
        self.kinds_served[kind] = self.kinds_served.get(kind, 0) + 1
        rng = self.rng

        def leg(point, alt):
            return {"lat": round(point[0], 6), "lon": round(point[1], 6), "alt_m": alt}

        if kind == "no_reply":
            return None
        if kind == "garbage":
            text = rng.choice(["Sure! Here is the route: go north then west.", "[]",
                               '{"legs": "north"}', '{"legs": [{"lat": "a"}]}', ""])
        elif kind == "think_only":
            text = "<think>" + json.dumps({"legs": [leg(start, 60), leg(goal, 60)]}) + "</think>"
        elif kind == "through_building":
            via = [self._centre(rng.choice(self.buildings)) for _ in range(rng.randint(1, 3))]
            text = json.dumps({"legs": [leg(start, 45)] + [leg(p, rng.choice([45, 60, 90]))
                                                            for p in via] + [leg(goal, 45)]})
        elif kind == "into_cell":
            via = [self._centre(rng.choice(self.cells))] if self.cells else []
            text = json.dumps({"legs": [leg(start, 90)] + [leg(p, 90) for p in via]
                               + [leg(goal, 90)]})
        elif kind == "absurd_altitude":
            alt = rng.choice([-5, 0, 5000, 121, 300, 1e9])
            text = json.dumps({"legs": [leg(start, alt), leg(goal, alt)]})
        elif kind == "out_of_box":
            text = json.dumps({"legs": [leg(start, 60), leg((41.5, -72.0), 60), leg(goal, 60)]})
        elif kind == "too_many":
            text = json.dumps({"legs": [leg(start, 60)] + [leg(start, 60) for _ in range(13)]
                               + [leg(goal, 60)]})
        elif kind == "random_walk":
            points = [(start[0] + rng.uniform(-0.03, 0.03), start[1] + rng.uniform(-0.03, 0.03))
                      for _ in range(rng.randint(1, 5))]
            text = json.dumps({"legs": [leg(start, 60)] + [leg(p, rng.uniform(30, 130))
                                                            for p in points] + [leg(goal, 60)]})
        else:
            text = json.dumps({"legs": [leg(start, rng.choice([40, 90, 120])),
                                        leg(goal, rng.choice([40, 90, 120]))]})
        return LlmReply(text=text, model="chaos-nano")


class RecklessDrafter(ModelDrafter):
    """운영사 쪽 사전 판정과 고도 규칙을 일부러 건너뜁니다. 양식 검사만 남깁니다.

    운영사가 게을러도 런타임의 보장은 같아야 합니다. 이 초안기는 모델이 낸 것을 거의
    그대로 신청서에 싣고, 판정은 전부 런타임 몫이 됩니다. 직결 세계가 아니라 런타임 세계의
    운영사를 못나게 만드는 것이라 G6(직결 쪽 사보타주 금지)에 걸리지 않습니다.
    """

    def _apply_altitude_rule(self, legs):
        return legs

    def breaches_along(self, legs, limit=5):
        return []


class ChaosDraftsNeverFlyTest(unittest.TestCase):
    """모델이 무엇을 그리든 실행되는 경로는 전부 판정을 지난 신청서에서 옵니다."""

    TICKS = 1500

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls.adapters = []
        cls.llms = []

        def drafter(runtime, planner):
            llm = ChaosLlm(runtime.airspace)
            cls.llms.append(llm)
            return RecklessDrafter(llm, planner, bbox=fleet_bbox())

        def adapter(world, clock, airspace):
            made = JudgingAdapter(world, clock, airspace)
            cls.adapters.append(made)
            return made

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            cls.ledger_path = handle.name
            cls.guarded, cls.direct, cls.trace = run(handle.name, ticks=cls.TICKS,
                                                     drafter_factory=drafter,
                                                     adapter_factory=adapter)

    def test_the_chaos_model_was_actually_asked_and_served_every_kind(self):
        llm = self.llms[0]
        self.assertGreater(sum(llm.kinds_served.values()), 20, llm.kinds_served)
        self.assertGreaterEqual(len(llm.kinds_served), 8, llm.kinds_served)

    def test_guarded_airspace_stays_clean(self):
        self.assertEqual(self.guarded["airspace_violations"], 0)
        self.assertEqual(self.guarded["ceiling_breaches"], 0)
        self.assertEqual(self.guarded["zone_incursions"], 0)

    def test_every_executed_route_came_from_a_judged_filing(self):
        adapter = self.adapters[0]
        self.assertGreater(adapter.routes, 0)
        self.assertEqual(adapter.unjudged, 0, "판정을 못 넘는 경로가 조종장치에 닿았습니다")
        self.assertEqual(adapter.without_receipt, 0)
        self.assertEqual(self.guarded["unrecorded_actions"], 0)
        # 원장에서도: 실행된(done) 경로 항목은 전부 승인 판정을 달고 있습니다
        done_routes, chaos_filed, chaos_refused = 0, 0, 0
        with open(self.ledger_path, encoding="utf-8") as handle:
            for line in handle:
                entry = json.loads(line)
                proposal, decision = entry["proposal"], entry["decision"]
                if proposal["action"] not in ("fly_route", "reserve_pad"):
                    continue
                drafter = proposal.get("params", {}).get("drafter", "")
                if drafter.startswith("nano:"):
                    chaos_filed += 1
                    if decision["verdict"] == "denied":
                        chaos_refused += 1
                if entry["outcome"] == "done":
                    done_routes += 1
                    self.assertEqual(decision["verdict"], "auto")
                    self.assertNotEqual(decision.get("code"), "airspace")
        self.assertGreater(done_routes, 0)
        self.assertGreater(chaos_filed, 0, "혼돈 초안이 하나도 신청서에 실리지 않았습니다")
        self.assertGreater(chaos_refused, 0, "런타임이 혼돈 초안을 하나도 거절하지 않았습니다")

    def test_the_fleet_still_delivers_because_a_star_takes_over(self):
        self.assertGreater(self.guarded["actions"], 8)
        self.assertTrue(any(row["delivered"] >= 1 for row in self.trace.values()), self.trace)


class RecordedNanoDraftsFlyTest(unittest.TestCase):
    """녹음된 진짜 Nemotron 초안이 판정을 지나 실행되고, 원장에 nano 가 그렸다고 남습니다."""

    @classmethod
    def setUpClass(cls):
        import tempfile

        from tests.fixture_llm import FixtureLlm, load_fixtures

        # 직선이 통과하는 자리에서는 초안기가 불리지 않습니다. 직선이 거절된 자리의 통과 녹음만 씁니다.
        records = [r for r in load_fixtures()
                   if r.get("kind") == "draft" and r.get("expect") == "pass"
                   and r.get("straight_refused")]
        if not records:
            raise unittest.SkipTest("직선이 거절된 자리의 통과 녹음이 없습니다 (tests/fixtures/llm)")
        seeds = {r.get("seed") for r in records if r.get("seed") is not None}
        cls.seed = sorted(seeds)[0] if seeds else 7
        cls.llms = []

        def drafter(runtime, planner):
            llm = FixtureLlm(records=[r for r in records if r.get("seed") in (None, cls.seed)])
            cls.llms.append(llm)
            return ModelDrafter(llm, planner, bbox=fleet_bbox())

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            cls.ledger_path = handle.name
            cls.guarded, _, _ = run(handle.name, ticks=500, drafter_factory=drafter,
                                    seed=cls.seed)

    def test_a_nano_route_was_executed_and_the_ledger_says_so(self):
        self.assertGreater(len(self.llms[0].served), 0, "fixture 가 한 번도 답하지 않았습니다")
        flown = []
        with open(self.ledger_path, encoding="utf-8") as handle:
            for line in handle:
                entry = json.loads(line)
                params = entry["proposal"].get("params", {})
                drafter = str(params.get("drafter", ""))
                if entry["outcome"] == "done" and drafter.startswith("nano:"):
                    flown.append(entry)
        self.assertTrue(flown, "nano 가 그린 경로가 하나도 실행되지 않았습니다")
        entry = flown[0]
        self.assertEqual(entry["proposal"]["params"]["drafter"], "nano:nemotron-3-nano")
        self.assertIsInstance(entry["proposal"]["params"]["draft_attempts"], int)
        self.assertEqual(entry["decision"]["verdict"], "auto")
        self.assertEqual(self.guarded["airspace_violations"], 0)


class TwoWorldsTest(unittest.TestCase):
    """구조적으로 항상 참이어야 하는 것만 여기서 봅니다.

    특정 사건이 그 판에 일어났는지에 기대는 단언은 넣지 않습니다. 시나리오가 조금만
    달라져도 깨지고, 깨진 걸 맞추려고 시나리오를 손대면 시험이 아니라 장식이 됩니다.
    사건별 메커니즘은 test_mechanisms.py 에서 따로 봅니다.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            cls.guarded, cls.direct, cls.trace = run(handle.name)

    # ---------- 런타임 쪽에서 반드시 참이어야 하는 것 ----------

    def test_pads_are_never_shared_under_the_runtime(self):
        self.assertEqual(self.guarded["pad_conflicts"], 0)

    def test_guarded_flight_never_enters_forbidden_airspace_or_exceeds_a_ceiling(self):
        self.assertEqual(self.guarded["airspace_violations"], 0)
        self.assertEqual(self.guarded["ceiling_breaches"], 0)

    def test_every_guarded_action_is_on_the_record(self):
        self.assertEqual(self.guarded["unrecorded_actions"], 0)

    def test_the_fleet_budget_cannot_be_exceeded_unattended(self):
        self.assertEqual(self.guarded["over_fleet_limit_usd"], 0)

    def test_passenger_impact_never_happens_unattended(self):
        self.assertEqual(self.guarded["unapproved_passenger_actions"], 0)

    def test_a_closed_zone_is_emptied_faster_where_something_enforces_it(self):
        """규칙이 도착했을 때 안에 있던 기체를 누가 빼내느냐.

        침범 횟수가 아니라 머문 시간을 봅니다. 규칙이 도착한 순간 안에 있던 것은 아무도
        잘못한 게 아닙니다. 갈리는 것은 그 다음입니다 — 한쪽은 회항 명령을 받고,
        다른 쪽은 기체가 스스로 공지를 확인할 때까지 남아 있습니다.
        """
        self.assertEqual(self.guarded["zone_incursions"], 0)
        # 머문 시간은 그 판에 누가 어디 있었느냐에 달려 있어서 '반드시 더 짧다'로 묶으면
        # 시나리오가 조금만 달라져도 깨집니다. 더 오래 남지 않는다는 것만 봅니다.
        self.assertLessEqual(self.guarded["zone_dwell_ticks"],
                             self.direct["zone_dwell_ticks"])

    # ---------- 적재 순환이 실제로 도는가 ----------

    def test_every_aircraft_loads_delivers_twice_and_comes_home(self):
        """창고에서 여섯 상자 → 배달지 두 곳에서 세 상자씩 → 이륙장. 한 판에 최소 한 바퀴."""
        for asset, row in self.trace.items():
            with self.subTest(asset=asset):
                self.assertEqual(row["max_load"], 6, "여섯 상자를 다 싣지 못했습니다")
                self.assertGreaterEqual(row["delivered"], 2, "배달지 두 곳을 못 돌았습니다")
                self.assertGreaterEqual(row["home"], 1, "창고 마당으로 돌아오지 못했습니다")
                self.assertLessEqual({"loading", "ready", "delivering", "landing", "dropping"},
                                     row["states"])

    def test_nothing_hovers_in_the_air_waiting_for_a_route(self):
        """멈추는 것은 지상에서 일할 때뿐입니다. 공중 대기는 회수당했을 때 정도만 남습니다."""
        for asset, row in self.trace.items():
            with self.subTest(asset=asset):
                self.assertLessEqual(row["hovering"], 60)   # 12초. 회수 뒤 재신청 한 번 분량

    # ---------- 직결 쪽에서 반드시 참이어야 하는 것 ----------

    def test_nothing_the_direct_side_does_is_recorded(self):
        """한 건도 아니고 전부입니다. 남길 곳이 없어서입니다."""
        self.assertEqual(self.direct["unrecorded_actions"], self.direct["actions"])
        self.assertGreater(self.direct["actions"], 0)

    # 패드 충돌은 "그 판에 두 대가 같은 순간에 같은 패드를 골랐는가"에 달려 있어서
    # 시나리오가 조금만 달라져도 났다 안 났다 합니다. 위 docstring 이 말하는 그 경우라
    # 결정적으로 볼 수 있는 test_mechanisms.PadContentionTest 로 옮겼습니다.

    def test_the_direct_side_never_gets_a_human_look(self):
        self.assertEqual(self.direct["human_approvals"], 0)

    # ---------- 양쪽이 같은 일을 하고 있다는 것 ----------

    def test_both_sides_actually_flew(self):
        """한쪽이 굶어 있으면 비교가 아닙니다."""
        self.assertGreater(self.guarded["actions"], 8)
        self.assertGreater(self.direct["actions"], 8)


if __name__ == "__main__":
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        guarded, direct, trace = run(handle.name)
    print(json.dumps({"guarded": guarded, "direct": direct,
                      "trace": {k: {**v, "states": sorted(v["states"])} for k, v in trace.items()}},
                     indent=2, ensure_ascii=False))
