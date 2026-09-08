"""Fleet-wide bans. Checked before limits, and before any model is consulted."""

from attache.core.config import Policy


class PolicyBook:
    def __init__(self, policies: list[Policy] | None = None):
        self._policies: list[Policy] = list(policies or [])

    def add(self, policy: Policy) -> None:
        self._policies = [p for p in self._policies if p.id != policy.id] + [policy]

    def all(self) -> list[Policy]:
        return list(self._policies)

    def active(self, tick: int) -> list[Policy]:
        return [p for p in self._policies if tick >= p.active_from_tick]

    def hit(self, action: str, asset: dict, tick: int) -> Policy | None:
        for policy in self._policies:
            if policy.matches(action, asset, tick):
                return policy
        return None
