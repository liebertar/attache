"""The single door to the world.

Every path in this system funnels through commit(). It writes the ledger first, takes the
lock, calls the adapter, then closes the ledger. Agents cannot import this module: it is
not in their container image.
"""

from attache.core.models import Decision, Proposal, Verdict
from attache.runtime.authority import AuthorityCheck
from attache.runtime.ledger import Ledger
from attache.runtime.locks import LockTable

RELEASING_ACTIONS = {"depart", "divert_ground"}


class Committer:
    def __init__(
        self,
        adapter,
        locks: LockTable,
        ledger: Ledger,
        authority: AuthorityCheck,
    ):
        self.adapter = adapter
        self.locks = locks
        self.ledger = ledger
        self.authority = authority

    def commit(self, proposal: Proposal, decision: Decision) -> Decision:
        if decision.verdict is Verdict.DENIED:
            return decision

        if proposal.resource and not self.locks.acquire(
            proposal.resource, proposal.asset_id, proposal.id
        ):
            decision.verdict = Verdict.DENIED
            decision.reason = f"{proposal.resource} 는 다른 기체가 쓰는 중입니다"
            return decision

        entry = self.ledger.open_entry(proposal, decision)
        decision.ledger_id = entry.id

        result = self.adapter.execute(
            proposal.asset_id,
            proposal.action,
            proposal.params,
            entry.id,
            blast=proposal.blast_radius,
            approved_by=decision.approved_by,
        )
        ok = bool(result.get("ok"))

        if ok:
            self.authority.record_spend(proposal)
            decision.committed = True
            if proposal.action in RELEASING_ACTIONS:
                self.locks.release_all(proposal.asset_id)
        elif proposal.resource:
            self.locks.release(proposal.resource, proposal.asset_id)

        self.ledger.close_entry(
            entry, "done" if ok else f"failed: {result.get('error')}", decision
        )
        return decision
