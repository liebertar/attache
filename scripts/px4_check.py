#!/usr/bin/env python3
"""Proves the link to a real autopilot before wiring the runtime to it.

    python3 scripts/px4_check.py udpin:0.0.0.0:14540

Prints what the autopilot reports, then asks it to land and prints its answer. The answer
may be a refusal, which is the point: we grant authority, the autopilot still decides
whether the aircraft can do it.
"""

import sys

from attache.adapters.mavlink_fleet import MavlinkFleetAdapter


def main() -> int:
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "udpin:0.0.0.0:14540"
    adapter = MavlinkFleetAdapter({"vehicle": endpoint}, link_timeout_s=30.0)
    print(f"{endpoint} 에서 하트비트를 기다립니다...")

    import time

    for _ in range(60):
        entry = adapter.telemetry()["assets"]["vehicle"]
        if entry.get("state") != "unknown":
            break
        time.sleep(0.5)
    else:
        print("자동조종이 응답하지 않습니다.")
        return 1

    for key in ("state", "armed", "battery", "lat", "lon", "alt_m", "vibration"):
        if key in entry:
            print(f"  {key:12} {entry[key]}")

    print("\nland 명령을 보냅니다...")
    print("  ->", adapter.execute("vehicle", "land", {}, "l_check"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
