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
            "recall_tick": simulation.bulletins()[0]["published_tick"]
            if simulation.bulletins()
            else None,
            "worlds": {
                name: world.snapshot(simulation.tick_count)
                for name, world in simulation.worlds.items()
            },
        }

    server = JsonServer(int(os.getenv("PORT", "8100")))
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
