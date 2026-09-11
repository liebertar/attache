"""A HUMAN verdict is a runtime decision: it is ledgered, and its card does not outlive the round.

The card's ledger entry opens when the runtime parks the request and closes with the person's
answer (approved / denied), or as lapsed when the round ends first. A repeat filing while the
card stands returns the same decision and still leaves a line (outcome waiting).
"""

import json
import tempfile
import unittest

from holdshort.core.models import Proposal, Verdict
from holdshort.runtime.service import Runtime
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
HERE = (40.7100, -73.9855)


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.advisory_async = False
    runtime.notice_async = False
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.telemetry = {"drone-01": {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0}}
    runtime.tick = 600
    return runtime, adapter


def lines(runtime):
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def rows_for(runtime, proposal_id):
    return [(e["id"], e["outcome"], e["decision"]["verdict"], e["decision"]["code"])
            for e in lines(runtime) if e["proposal"]["id"] == proposal_id]


def open_ids(runtime):
    opened = {e["id"] for e in lines(runtime) if e["outcome"] == "pending"}
    closed = {e["id"] for e in lines(runtime) if e["outcome"] != "pending"}
    return opened - closed


def public_route(rationale="승객 위"):
    return Proposal(asset_id="drone-01", action="fly_route", cost_usd=12.0,
                    blast_radius="public", rationale=rationale,
                    params={"legs": [{"lat": HERE[0], "lon": HERE[1], "alt_m": 60},
                                     {"lat": 40.7000, "lon": -73.9855, "alt_m": 60}]})


class HumanCardLedgerTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()

    def test_the_card_opens_a_line_and_the_persons_answer_closes_it(self):
        first = public_route()
        decision = self.runtime.file(first.to_dict())
        self.assertIs(decision.verdict, Verdict.HUMAN)
        rows = rows_for(self.runtime, first.id)
        self.assertEqual(rows, [(rows[0][0], "pending", "human", "human_blast")])
        # 같은 카드가 서 있는 동안의 재신청: 같은 결정, 그래도 한 줄
        repeat = public_route()
        again = self.runtime.file(repeat.to_dict())
        self.assertEqual((again.verdict, again.proposal_id), (Verdict.HUMAN, first.id))
        self.assertEqual([r[1:] for r in rows_for(self.runtime, repeat.id)],
                         [("pending", "human", "human_blast"), ("waiting", "human", "human_blast")])
        self.assertEqual(len(self.runtime.snapshot()["awaiting_human"]), 1)
        # 승인: 카드 줄이 닫히고, 실행은 자기 줄을 남깁니다
        approved = self.runtime.approve(first.id, "관제사", allow=True)
        self.assertTrue(approved.committed)
        rows = rows_for(self.runtime, first.id)
        self.assertEqual([r[1] for r in rows], ["pending", "approved", "pending", "done"])
        self.assertEqual(rows[0][0], rows[1][0], "카드를 연 줄을 닫습니다")
        closed_card = next(e for e in lines(self.runtime) if e["id"] == rows[0][0]
                           and e["outcome"] == "approved")
        self.assertEqual(closed_card["decision"]["approved_by"], "관제사")
        self.assertEqual(closed_card["context"]["checks_run"][-1], "human")
        self.assertEqual(open_ids(self.runtime), set())

    def test_a_refusal_closes_the_card_line_as_denied(self):
        first = public_route()
        self.runtime.file(first.to_dict())
        denied = self.runtime.approve(first.id, "관제사", allow=False)
        self.assertIs(denied.verdict, Verdict.DENIED)
        rows = rows_for(self.runtime, first.id)
        self.assertEqual([r[1] for r in rows], ["pending", "denied"])
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(open_ids(self.runtime), set())

    def test_a_round_change_takes_the_card_down_and_closes_it_as_lapsed(self):
        first = public_route()
        self.runtime.file(first.to_dict())
        self.assertEqual(len(self.runtime.snapshot()["awaiting_human"]), 1)
        self.runtime._follow_round(2)
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        rows = rows_for(self.runtime, first.id)
        self.assertEqual([(r[1], r[2], r[3]) for r in rows],
                         [("pending", "human", "human_blast"), ("lapsed", "denied", "card_lapsed")])
        self.assertIsNone(self.runtime.approve(first.id, "관제사", allow=True),
                          "내려간 카드는 승인할 수 없습니다")
        self.assertEqual(open_ids(self.runtime), set())


if __name__ == "__main__":
    unittest.main()
