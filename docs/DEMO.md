# Demo

Seed 7. One round is 5,000 ticks; the simulator advances a tick every 0.2 s of wall clock (0.8 s of simulated
time), so a round is about 17 minutes. Every scene is scheduled; the fleet does the rest on its own.

## Start it

```sh
./scripts/demo.sh
```

It stops whatever holds ports 8000, 8100 and 3100 (a Docker container on them is stopped too), starts a clean
stack through `scripts/dev.sh` with seed 7, waits for the four drone agents to register, resets the round to
tick 0 and opens http://127.0.0.1:3100/map.html?demo=1. `DEMO_OPEN=0` skips the browser. Running it again
gives the same round. `dev.sh` picks the models itself: start the Ollama fleet first
(`scripts/ollama_fleet.sh start 4`) or set `NEBIUS_API_KEY` if the video should show model names; otherwise
one Ollama on 11434 serves everyone if it answers, else the rules fly it.

`?demo=1` turns on the director. It follows the scenes, and its captions are assembled from ledger codes and
values, never written by a model. Any drag, zoom, rotation or tilt of the map, or a camera key, pauses it for
20 s. The page fits 1280×800.

## Timeline

| Tick | Sim clock | Scene | What to look at |
|---|---|---|---|
| 0 | 09:00Z | Tower briefing for the round | TOWER BRIEFING panel: what was found and from which domain, RECORDED without a Tavily key; crane masts and closed landing areas on the map |
| 0–30 | 09:00Z | Four aircraft load six boxes each on the warehouse roof (Brooklyn Navy Yard) | bays, cargo stacks, model names under each aircraft |
| from 30 | | Straight lines refused; the planner draws up to three candidates and each drone's model picks one; departures serialise because takeoff columns overlap | red line → yellow redraw → green corridor; refusal card names the building; hover an aircraft for what its model chose and why |
| 525–900 | 09:07–09:12Z | NOTAM closes the East Village helipad corridor with drone-03 inside | banner "read by the rule grammar"; drone-03 recalled at tick 525 and out within the 22-tick exit allowance; new routes through it refused |
| 1050–1350 | | Airworthiness directive grounds one type (dv-x500) | drone-01 and drone-03 are refused every route; drone-02 and drone-04 keep flying |
| 1350–2100 | 09:18–09:28Z | MEDEVAC prose around Harlem Hospital | with a Super model: held for a person, then a circle; without: "not yet read" |
| 2175–2700 | 09:29–09:36Z | METAR: gusts 28 kt | fleet-wide takeoff hold within one tick; airborne aircraft land; lift card in the inbox |
| 3000–3600 | | Fire at 4705 Center Boulevard | 150 m keep-out around the building; the Gantry Plaza landing area unusable; every route cleared while it stands keeps clear of it |
| 3800–3950 | | One airborne aircraft goes dark | LOST LINK on its label; its tower line turns grey and broken; its corridor stays reserved; nothing is sent to it; it lands where it was cleared; conformance line when it is heard again |
| any | | Same reason refused three times | TOWER ADVISORY card: legal options, the chosen one marked |
| any | | A newly cleared corridor enters a ~1 km cell not briefed this round | a briefing for that place; a caption when a rule applies |

The scoreboard compares both wirings throughout. In this round the runtime column ends at zero on every
violation counter; the direct column ends with 48 airspace violations, 13 ceiling breaches, 36 unrecorded
actions, 3 separation losses and 2 takeoffs during the weather hold. The runtime's ledger for the same round
holds 11 traffic refusals (6 resolved by a delay), 42 landing-site refusals, 84 refusals during the weather
hold, 2 recalls, 1 lost link and 1 restored.

## Video script (3 minutes)

1. 0:00 — Warehouse roof, four aircraft, model names; the TOWER BRIEFING panel with its sources.
   "Every aircraft has its own agent. The tower reads today's notices before anyone flies."
2. 0:20 — A straight line drawn and refused with the building named. The planner draws three candidates;
   the drone's Nemotron picks one by tool call; the hover card shows its reason; cleared.
   "The agent chooses. The tower checks. Nothing moved until it was cleared."
3. 0:50 — Two corridors would cross; the second is refused with the other aircraft named; it waits, then goes.
4. 1:10 — NOTAM arrives as text with drone-03 inside the area; its corridor is pulled back and it leaves by
   the nearest exit. "Rules that arrive during flight are enforced the tick they arrive."
5. 1:35 — METAR: gusts. Takeoffs held fleet-wide; the direct fleet keeps taking off. Scoreboard.
6. 1:55 — Fire report. The circle appears; Gantry Plaza closes; no cleared route crosses it.
   "Code found the address and drew the circle."
7. 2:15 — One aircraft goes dark; its tower line breaks; it finishes its cleared route; its space stays reserved.
8. 2:35 — The PX4 cut if one was recorded (below), then the ledger report for one flight: who filed, which
   checks ran, which rule refused, who confirmed.
9. 2:50 — Two columns: runtime zero, direct not. "Same fleet, same agents, one difference."

## PX4 for the video

```sh
./scripts/sitl.sh                        # PX4 SIH in one container, the stack on the host
TICK_SECONDS=0.8 ./scripts/sitl.sh       # the simulator at real time, so PX4 keeps pace with the map
```

drone-01 (`MAVLINK_MIRROR`) is also flown by PX4, starting from its warehouse seat. Its cleared route becomes a
PX4 mission: takeoff, one waypoint per leg at that leg's altitude, land at the destination. A recall uploads an
exit mission, or lands in place when there is no exit; a refused filing sends nothing. The map draws the PX4
aircraft as a ghost beside the simulated one, and `/state.autopilots` shows what PX4 answered. PX4 runs at 1×
and the default simulator at 4×, so the ghost falls behind unless `TICK_SECONDS=0.8`.

Seen live: a 15-leg cleared route became a 16-item mission, and the tick-525 recall landed PX4 in flight. Not
seen yet: PX4 landing at a route's destination under runtime control; PX4 flies slower than the simulator,
and the next clearance replaced the mission first.

`sitl.sh` does not start the director. Open http://127.0.0.1:3100/map.html?demo=1 and restart the round with
`curl -X POST http://127.0.0.1:8100/reset`. `./scripts/sitl.sh check` tests the MAVLink link and the mission
protocol on their own; `./scripts/sitl.sh stop` removes the container.

## Camera and keys

Drag to pan, scroll to zoom, right-drag to orbit. Arrows pan, Shift+arrows rotate and tilt, +/− zoom,
N north, H home, 1–4 focus an aircraft, L tower lines on and off. Hover a guarded aircraft or a ledger row for
what its model did; on a touch screen, tap it.
