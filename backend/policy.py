"""Fleet-wide bans. Checked before limits, and before any model is consulted."""

from shared.config import Policy


class PolicyBook:
    def __init__(self, policies: list[Policy] | None = None):
        self._policies: list[Policy] = list(policies or [])

    def add(self, policy: Policy) -> None:
        self._policies = [p for p in self._policies if p.id != policy.id] + [policy]

    def remove(self, policy_id: str) -> None:
        """푸는 쪽. 부르는 곳은 사람의 답과 창의 끝뿐입니다 — 코드가 스스로 풀지 않습니다."""
        self._policies = [p for p in self._policies if p.id != policy_id]

    def all(self) -> list[Policy]:
        return list(self._policies)

    def clear(self) -> None:
        """판이 바뀌면 비웁니다. 공지는 새 판에서 다시 게시되고 그때 다시 걸립니다."""
        self._policies = []

    def active(self, tick: int) -> list[Policy]:
        return [p for p in self._policies if tick >= p.active_from_tick]

    def hit(
        self, action: str, resource: str | None, asset: dict, tick: int
    ) -> Policy | None:
        for policy in self._policies:
            if policy.matches(action, resource, asset, tick):
                return policy
        return None
