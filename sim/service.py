"""Runs the clock and exposes the actuator. It never asks who is calling."""

import os
import threading
import time

from attache.core.http import JsonServer
from sim.world import Simulation


def main() -> None:
    simulation = Simulation(
        seed=int(os.getenv("SEED", "7")),
        fleet_limit=float(os.getenv("FLEET_LIMIT_USD", "500")),
        tick_seconds=float(os.getenv("TICK_SECONDS", "0.2")),
        lock_actuator=os.getenv("LOCK_ACTUATOR", "0") == "1",
        # 화면을 켜두면 계속 돌아야 합니다. 한 판이 끝나면 알아서 다시 시작합니다.
        max_ticks=int(os.getenv("ROUND_TICKS", "420")),
    )

    def clock() -> None:
        while True:
            simulation.step()
            time.sleep(simulation.tick_seconds)

    threading.Thread(target=clock, daemon=True).start()

    def state(body, query):
        name = query.get("world", "guarded")
        world = simulation.worlds.get(name)
        if world is None:
            return 404, {"error": f"unknown world {name}"}
        return 200, world.snapshot(simulation.tick_count)

    def act(body, query):
        world = simulation.worlds.get(body.get("world", "guarded"))
        if world is None:
            return 404, {"error": "unknown world"}
        result = world.act(
            asset=body.get("asset", ""),
            action=body.get("action", ""),
            params=body.get("params") or {},
            ledger_id=body.get("ledger_id"),
            blast=body.get("blast", "none"),
            approved_by=body.get("approved_by"),
            tick=simulation.tick_count,
        )
        return 200, result

    def compare(body, query):
        return 200, {
            "tick": simulation.tick_count,
            "round": simulation.rounds,
            "recall_tick": simulation.bulletins()[0]["published_tick"]
            if simulation.bulletins()
            else None,
            "worlds": {
                name: world.snapshot(simulation.tick_count)
                for name, world in simulation.worlds.items()
            },
        }

    def rows(body, query):
        """Grafana 가 그대로 먹을 수 있는 평평한 표. 중첩이 없어야 설정이 안 늘어납니다."""
        out = []
        for name, world in simulation.worlds.items():
            snapshot = world.snapshot(simulation.tick_count)
            crowded: dict[str, int] = {}
            for vehicle in snapshot["assets"].values():
                if vehicle["state"] in ("landed", "charging") and vehicle["assigned_pad"]:
                    crowded[vehicle["assigned_pad"]] = (
                        crowded.get(vehicle["assigned_pad"], 0) + 1
                    )
            for vehicle in snapshot["assets"].values():
                out.append({
                    "world": "런타임" if name == "guarded" else "직접",
                    "wiring": name,
                    "id": vehicle["id"],
                    "model": vehicle["model"],
                    "lat": vehicle["lat"],
                    "lon": vehicle["lon"],
                    "alt_m": vehicle["alt_m"],
                    "battery": vehicle["battery"],
                    "state": vehicle["state"],
                    "pad": vehicle["assigned_pad"] or "",
                    "spend_usd": vehicle["spend"],
                    "in_conflict": crowded.get(vehicle["assigned_pad"], 0) > 1,
                })
        return 200, out

    def scoreboard_rows(body, query):
        labels = [
            ("pad_conflicts", "착륙 패드 충돌"),
            ("post_recall_violations", "리콜 이후 금지 행동"),
            ("unapproved_passenger_actions", "승객 영향 무단 실행"),
            ("unrecorded_actions", "기록 없는 실행"),
            ("batteries_dead", "방전으로 멈춤"),
            ("spend_usd", "기단 지출 ($)"),
            ("over_fleet_limit_usd", "기단 한도 초과 ($)"),
            ("human_approvals", "사람이 승인한 건"),
        ]
        guarded = simulation.worlds["guarded"].score.public()
        direct = simulation.worlds["direct"].score.public()
        return 200, [
            {"지표": label, "런타임": guarded[key], "직접": direct[key]}
            for key, label in labels
        ]

    def pad_rows(body, query):
        snapshot = simulation.worlds["guarded"].snapshot(simulation.tick_count)
        return 200, [
            {"pad": name.replace("pad:", ""), "lat": at["lat"], "lon": at["lon"]}
            for name, at in snapshot["pad_coords"].items()
        ]

    server = JsonServer(int(os.getenv("PORT", "8100")))
    server.add("GET", "/rows", rows)
    server.add("GET", "/scoreboard_rows", scoreboard_rows)
    server.add("GET", "/pad_rows", pad_rows)
    server.add("GET", "/state", state)
    server.add("POST", "/act", act)
    server.add("GET", "/compare", compare)
    server.add("GET", "/bulletins", lambda body, query: (200, {
        "tick": simulation.tick_count, "bulletins": simulation.bulletins()
    }))
    server.add("POST", "/reset", lambda body, query: (200, simulation.reset() or {"ok": True}))
    server.add("GET", "/health", lambda body, query: (200, {"ok": True}))
    print(f"sim listening on :{os.getenv('PORT', '8100')}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
