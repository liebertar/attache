#!/usr/bin/env python3
"""Proves the link to a real autopilot before wiring the runtime to it.

    python3 scripts/px4_check.py                            # udpin:0.0.0.0:14540
    python3 scripts/px4_check.py udpin:0.0.0.0:14540
    python3 scripts/px4_check.py udpin:0.0.0.0:14540 --fly   # 짧게 한 번 띄웠다 내립니다

Prints what the autopilot reports, then asks it to land and prints its answer. The answer
may be a refusal, which is the point: we grant authority, the autopilot still decides
whether the aircraft can do it.

Then it uploads a two-leg mission the same way the runtime uploads a cleared route, and
clears it again — the aircraft does not move. With --fly it arms and flies that hop, which
is the whole command path the demo needs, end to end.
"""

import sys
import time

from backend.adapters.mavlink_fleet import MavlinkFleetAdapter, route_items

ASSET = "vehicle"
HOP_ALT_M = 20.0
HOP_NORTH_DEG = 0.0009      # 약 100 m
FLIGHT_TIMEOUT_S = 180.0


def _wait(condition, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.25)
    return bool(condition())


def _show(view: dict) -> None:
    for key in ("link", "last_heartbeat_s", "mode", "landed", "armed", "battery",
                "lat", "lon", "alt_m"):
        print(f"  {key:16} {view.get(key)}")


def main() -> int:
    arguments = sys.argv[1:]
    fly = "--fly" in arguments
    endpoint = next((a for a in arguments if not a.startswith("--")), "udpin:0.0.0.0:14540")
    adapter = MavlinkFleetAdapter({ASSET: endpoint}, link_timeout_s=30.0)
    print(f"{endpoint} 에서 하트비트를 기다립니다...")
    if not _wait(lambda: adapter.link_up(ASSET), 30.0):
        print("자동조종이 응답하지 않습니다.")
        return 1
    _wait(lambda: adapter.autopilot_view(ASSET)["lat"] is not None, 10.0)
    view = adapter.autopilot_view(ASSET)
    _show(view)

    print("\nland 명령을 보냅니다...")
    print("  ->", adapter.execute(ASSET, "land", {}, "l_check"))

    if view["lat"] is None:
        print("위치를 못 받아 임무는 건너뜁니다.")
        return 1
    legs = [{"lat": view["lat"], "lon": view["lon"], "alt_m": HOP_ALT_M},
            {"lat": view["lat"] + HOP_NORTH_DEG, "lon": view["lon"], "alt_m": HOP_ALT_M}]
    items = route_items(legs, airborne=adapter.airborne(ASSET))
    print(f"\n임무 {len(items)}항목을 올립니다 — 승인된 경로가 가는 바로 그 길입니다.")
    upload = adapter.upload_mission(ASSET, items)
    print("  ->", upload)
    ok = bool(upload["ok"])

    if not ok or not fly:
        print("임무를 지웁니다 (--fly 를 주면 이 임무를 실제로 납니다).")
        print("  ->", adapter.clear_mission(ASSET))
        print("\nPASS" if ok else "\nFAIL")
        return 0 if ok else 1

    started = adapter.start_mission(ASSET, arm=not adapter.airborne(ASSET))
    print("  시동·시작 ->", started)
    ok = ok and bool(started["ok"])
    flew = False
    deadline = time.monotonic() + FLIGHT_TIMEOUT_S
    while ok and time.monotonic() < deadline:
        now = adapter.autopilot_view(ASSET)
        print(f"  {str(now['mode']):14} armed={now['armed']} alt={now['alt_m']}"
              f" seq={now['mission_seq']} {now['landed']}")
        flew = flew or bool(now["armed"])
        if flew and not now["armed"]:
            print("  내려서 시동이 꺼졌습니다.")
            break
        time.sleep(2)
    else:
        if ok:
            print("  시한 안에 못 내렸습니다.")
            ok = False
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
