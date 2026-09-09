"""The envelope: what clears on its own, what a human must see, what is simply refused."""

from collections import defaultdict

from attache.core.config import Authority, Policy
from attache.core.models import Decision, Proposal, Verdict
from attache.runtime.policy import PolicyBook


class AuthorityCheck:
    def __init__(self, authority: Authority, policies: PolicyBook):
        self.authority = authority
        self.policies = policies
        self._spent_by_asset: dict[str, float] = defaultdict(float)
        self._spent_fleet = 0.0

    @property
    def fleet_spend(self) -> float:
        return round(self._spent_fleet, 2)

    def asset_spend(self, asset_id: str) -> float:
        return round(self._spent_by_asset[asset_id], 2)

    def new_round(self) -> None:
        """판이 새로 시작하면 쓴 돈도 새로 셉니다.

        예산은 한 판 안에서의 재량입니다. 시뮬레이터가 기체를 새로 세우는데 여기만
        누적으로 남으면, 두 번째 판부터는 한도가 다 차서 아무것도 승인되지 않습니다.
        """
        self._spent_by_asset.clear()
        self._spent_fleet = 0.0

    def record_spend(self, proposal: Proposal) -> None:
        """실행이 끝난 뒤에만 부릅니다. 승인 대기 중인 돈은 아직 쓴 돈이 아닙니다."""
        self._spent_by_asset[proposal.asset_id] += proposal.cost_usd
        self._spent_fleet += proposal.cost_usd

    def evaluate(self, proposal: Proposal, asset: dict, tick: int) -> Decision:
        problems = proposal.validate()
        if problems:
            return Decision(proposal.id, Verdict.DENIED, "; ".join(problems), code="invalid")

        banned: Policy | None = self.policies.hit(
            proposal.action, proposal.resource, asset, tick
        )
        if banned:
            return Decision(
                proposal.id,
                Verdict.DENIED,
                f"{banned.reason} ({banned.id})",
                policy_hit=banned.id,
                forbids=banned.forbid_resource or banned.forbid_action,
                code="policy", detail={"policy": banned.id},
            )

        if proposal.action in self.authority.human_required_actions:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"'{proposal.action}' 은 사람이 봐야 하는 행동입니다",
                authority_hit="human_required_actions",
                code="human_action", detail={"action": proposal.action},
            )

        if proposal.blast_radius in self.authority.human_required_blast:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"영향 범위가 '{proposal.blast_radius}' 입니다",
                authority_hit="human_required_blast",
                code="human_blast", detail={"blast": proposal.blast_radius},
            )

        asset_after = self._spent_by_asset[proposal.asset_id] + proposal.cost_usd
        if asset_after > self.authority.per_asset_usd:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"기체 한도 초과: ${asset_after:.0f} > ${self.authority.per_asset_usd:.0f}",
                authority_hit="per_asset_usd",
                code="over_asset",
                detail={"spent": round(asset_after), "cap": round(self.authority.per_asset_usd)},
            )

        fleet_after = self._spent_fleet + proposal.cost_usd
        if fleet_after > self.authority.fleet_usd:
            return Decision(
                proposal.id,
                Verdict.HUMAN,
                f"기단 한도 초과: ${fleet_after:.0f} > ${self.authority.fleet_usd:.0f}",
                authority_hit="fleet_usd",
                code="over_fleet",
                detail={"spent": round(fleet_after), "cap": round(self.authority.fleet_usd)},
            )

        return Decision(proposal.id, Verdict.AUTO, "한도 안", code="within_limits")
