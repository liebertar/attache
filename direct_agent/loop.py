"""The agent that holds the actuator address. Same eyes, same hands, different wiring.

TRANSPORT=http     → the built-in city simulator's actuator endpoint
TRANSPORT=mavlink  → a real autopilot over MAVLink, through this package's own client

It is not a straw man. It keeps to its own per-vehicle budget, it reads the recall bulletin
and obeys it, it does not repeat commands, and it is shown the whole fleet's state, which
the guarded agent never sees. What it cannot do is know what the other vehicles intend, or
what the fleet has spent, or refuse an action the moment a recall lands. There is nowhere
to put those rules when every agent is its own island.
"""

import os
import time

from attache.agent.detect import detect
from attache.agent.loop import build_llm
from attache.agent.propose import COSTS, Proposer
from attache.core.http import get_json, post_json

PADS = ["pad:P1", "pad:P2"]
PAD_COORDS = {
    "pad:P1": (37.50725, 127.07750),
    "pad:P2": (37.50725, 127.08850),
}


class DirectAgent:
    def __init__(self, asset_id: str, sim_url: str, proposer: Proposer,
                 per_asset_limit: float, bulletin_period_s: float,
                 transport: str = "http", mavlink_endpoint: str = ""):
        self.asset_id = asset_id
        self.sim_url = sim_url.rstrip("/")
        self.proposer = proposer
        self.per_asset_limit = per_asset_limit
        self.bulletin_period_s = bulletin_period_s
        self.transport = transport
        self.spend = 0.0
        self.banned_actions: set[str] = set()
        self.cooldown: dict[str, float] = {}
        self.repeat_s = float(os.getenv("REPEAT_COOLDOWN_S", "2"))
        self._last_bulletin_check = 0.0

        self.commander = None
        if transport == "mavlink":
            from direct_agent.mav_client import MavCommander

            self.commander = MavCommander(mavlink_endpoint)

    # ---------- 무엇이 보이나 ----------

    def observe(self) -> tuple[dict, dict]:
        """(내 기체 상태, 옆 기체들 상태). 옆 기체 의도는 어느 쪽에서도 안 보입니다."""
        if self.commander is not None:
            mine = self.commander.telemetry(
                self.asset_id, os.getenv("VEHICLE_MODEL", "robotaxi-v3")
            )
            neighbours = get_json(f"{self.sim_url}/state?world=direct") or {}
            return mine, neighbours.get("assets", {})
        state = get_json(f"{self.sim_url}/state?world=direct") or {}
        assets = state.get("assets") or {}
        return assets.get(self.asset_id) or {}, assets

    def _refresh_bulletins(self, model: str) -> None:
        now = time.time()
        if now - self._last_bulletin_check < self.bulletin_period_s:
            return
        self._last_bulletin_check = now
        payload = get_json(f"{self.sim_url}/bulletins") or {}
        for item in payload.get("bulletins", []):
            if item.get("applies_to", {}).get("model") in (None, model):
                self.banned_actions.add(item["forbid_action"])

    def _free_looking_pad(self, neighbours: dict) -> str:
        """다른 기체가 실제로 내려앉아 있는 패드만 피할 수 있습니다.

        위치는 Remote ID 로 공개되지만 '내가 저 패드를 잡아뒀다'는 의도는 공개되지
        않습니다. 회사가 다르면 서로의 예약을 볼 방법이 아예 없습니다. 하늘길은
        ASTM F3548 이 이 문제를 풀어놨는데, 땅 위 패드는 아무도 안 풀었습니다.
        """
        taken = {
            vehicle.get("assigned_pad")
            for vehicle_id, vehicle in neighbours.items()
            if vehicle_id != self.asset_id
            and vehicle.get("state") in ("landed", "charging")
        }
        return next((pad for pad in PADS if pad not in taken), PADS[0])

    # ---------- 무엇을 하나 ----------

    def step(self) -> None:
        telemetry, neighbours = self.observe()
        if not telemetry or telemetry.get("state") == "unknown":
            return
        self._refresh_bulletins(telemetry.get("model", ""))

        concern = detect(telemetry)
        if concern is None:
            return

        proposal = self.proposer.write(
            concern, telemetry, self._free_looking_pad(neighbours),
            frozenset(self.banned_actions),
        )
        if proposal.action in self.banned_actions:
            return  # 공지를 본 뒤에는 스스로 지킵니다
        cost = COSTS.get(proposal.action, 0.0)
        if self.spend + cost > self.per_asset_limit:
            return  # 자기 한도는 스스로 지킵니다. 기단 합계는 알 방법이 없습니다
        if time.time() < self.cooldown.get(proposal.action, 0.0):
            return  # 방금 낸 명령을 또 보내지 않습니다
        self.cooldown[proposal.action] = time.time() + self.repeat_s

        result = self._act(proposal)
        if result and result.get("ok"):
            self.spend += result.get("cost_usd", cost)
        verdict = "ok" if (result or {}).get("ok") else "실패"
        print(f"[direct:{self.asset_id}] {proposal.action} ${cost:.0f} -> {verdict}", flush=True)

    def _act(self, proposal) -> dict | None:
        if self.commander is not None:
            return self.commander.send(proposal.action, proposal.params, PAD_COORDS)
        return post_json(
            f"{self.sim_url}/act",
            {
                "world": "direct",
                "asset": self.asset_id,
                "action": proposal.action,
                "params": proposal.params,
                "blast": proposal.blast_radius,
            },
        )


def main() -> None:
    asset_id = os.environ["ASSET_ID"]
    period = float(os.getenv("AGENT_PERIOD_S", "0.6"))
    llm = build_llm()
    agent = DirectAgent(
        asset_id,
        os.getenv("SIM_URL", "http://sim:8100"),
        Proposer(llm),
        per_asset_limit=float(os.getenv("PER_ASSET_LIMIT_USD", "200")),
        bulletin_period_s=float(os.getenv("BULLETIN_PERIOD_S", "5")),
        transport=os.getenv("TRANSPORT", "http"),
        mavlink_endpoint=os.getenv("MAVLINK_ENDPOINT", ""),
    )
    print(f"agent {asset_id} up, holds the actuator "
          f"({agent.transport}, llm={'on' if llm.enabled else 'off'})", flush=True)
    while True:
        agent.step()
        time.sleep(period)


if __name__ == "__main__":
    main()
