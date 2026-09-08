import unittest

from attache.core.config import Authority, Policy
from attache.core.models import Proposal, Verdict
from attache.runtime.authority import AuthorityCheck
from attache.runtime.policy import PolicyBook


def make(**kwargs) -> Proposal:
    base = dict(
        asset_id="taxi-a", action="charge", cost_usd=12.0,
        blast_radius="none", rationale="배터리 낮음",
    )
    return Proposal(**{**base, **kwargs})


class AuthorityTest(unittest.TestCase):
    def setUp(self):
        self.policies = PolicyBook()
        self.authority = AuthorityCheck(
            Authority(
                per_asset_usd=200,
                fleet_usd=500,
                human_required_blast=["passenger", "public"],
                human_required_actions=["disengage_autonomy"],
            ),
            self.policies,
        )
        self.asset = {"model": "robotaxi-v3"}

    def test_clears_under_limit(self):
        self.assertIs(self.authority.evaluate(make(), self.asset, 0).verdict, Verdict.AUTO)

    def test_policy_denies_before_any_limit(self):
        self.policies.add(Policy("recall-1", "fast_charge", "발화 사례",
                                 {"model": "robotaxi-v3"}))
        decision = self.authority.evaluate(
            make(action="fast_charge", cost_usd=0.0), self.asset, 0
        )
        self.assertIs(decision.verdict, Verdict.DENIED)
        self.assertEqual(decision.policy_hit, "recall-1")

    def test_policy_ignores_other_models(self):
        self.policies.add(Policy("recall-1", "fast_charge", "발화 사례",
                                 {"model": "robotaxi-v3"}))
        decision = self.authority.evaluate(
            make(action="fast_charge"), {"model": "hexa-2"}, 0
        )
        self.assertIs(decision.verdict, Verdict.AUTO)

    def test_passenger_blast_needs_a_human_even_when_free(self):
        decision = self.authority.evaluate(
            make(action="disengage_autonomy", cost_usd=0.0, blast_radius="passenger"),
            self.asset, 0,
        )
        self.assertIs(decision.verdict, Verdict.HUMAN)

    def test_fleet_limit_binds_before_per_asset_limits_are_reached(self):
        # 기체당 200, 기체 셋이면 600. 기단 한도 500 에서 먼저 걸립니다.
        for asset in ("taxi-a", "drone-b"):
            for _ in range(10):
                proposal = make(asset_id=asset, cost_usd=20.0)
                if self.authority.evaluate(proposal, self.asset, 0).verdict is Verdict.AUTO:
                    self.authority.record_spend(proposal)
        decision = self.authority.evaluate(make(asset_id="taxi-c", cost_usd=200.0),
                                           self.asset, 0)
        self.assertIs(decision.verdict, Verdict.HUMAN)
        self.assertEqual(decision.authority_hit, "fleet_usd")

    def test_malformed_proposal_is_refused(self):
        decision = self.authority.evaluate(make(blast_radius="어쩌구"), self.asset, 0)
        self.assertIs(decision.verdict, Verdict.DENIED)


if __name__ == "__main__":
    unittest.main()
