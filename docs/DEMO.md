# Demo

Seed 7. One round is 5,000 ticks; the simulator advances a tick every 0.2 s of wall clock (0.8 s of simulated
time), so a round is about 17 minutes. Every scene is scheduled; the fleet does the rest on its own.

## Timeline

| Tick | Sim clock | Scene | What to look at |
|---|---|---|---|
| 0–30 | 09:00Z | Four aircraft load six boxes each on the warehouse roof (Brooklyn Navy Yard) | bays, cargo stacks, model names under each aircraft |
| ~30–400 | | Straight lines refused, detours drawn and cleared; departures serialise because takeoff columns overlap | red line → yellow redraw → green corridor; refusal card names the building |
| 525–900 | 09:07–09:12Z | NOTAM closes the East Village helipad corridor | banner "read by the rule grammar"; corridors through it recalled; exits |
| 1050–1350 | | Airworthiness directive grounds one type (dv-x500) | two aircraft refuse to take off, the other two keep flying |
| 1350–2100 | 09:18–09:28Z | MEDEVAC prose around Harlem Hospital | with a Super model: held for a person, then a circle; without: "not yet read" |
| 2175–2700 | 09:29–09:36Z | METAR: gusts 28 kt | fleet-wide takeoff hold within one tick; airborne aircraft land; lift card in the inbox |
| 3000–3600 | | Fire at 4705 Center Boulevard | 150 m keep-out around the building; a corridor recalled; Gantry Plaza landing area unusable |
| 3800–3950 | | One airborne aircraft goes dark | LOST LINK on its label; its corridor stays reserved; it lands where it was cleared; conformance line on return |
| any | | Same reason refused three times | TOWER ADVISORY card: legal options, the chosen one marked |

The scoreboard compares both wirings throughout. The runtime column stays at zero.

## Video script (3 minutes)

1. 0:00 — Warehouse roof, four aircraft, model names. "Every aircraft has its own agent."
2. 0:20 — A straight line to Harlem drawn and refused with the building named; the agent redraws; cleared.
   "The agent chooses. The tower checks. Nothing moved until it was cleared."
3. 0:50 — Two corridors would cross; the second is refused with the other aircraft named; it waits, then goes.
4. 1:10 — NOTAM arrives as text; the corridor through it is pulled back mid-flight; the aircraft leaves by the
   nearest exit. "Rules that arrive during flight are enforced the tick they arrive."
5. 1:35 — METAR: gusts. Takeoffs held fleet-wide; the direct fleet keeps taking off. Scoreboard.
6. 1:55 — Fire report in prose. The circle appears; a corridor is recalled. "A model read the sentence.
   Code decided what it means."
7. 2:15 — One aircraft goes dark; it finishes its cleared route; others are kept out of its space.
8. 2:35 — Ledger report for one flight: who filed, which checks ran, which rule refused, who confirmed.
9. 2:50 — Two columns: runtime zero, direct not. "Same fleet, same agents, one difference."

## Camera and keys

Drag to pan, scroll to zoom, right-drag to orbit. Arrows pan, Shift+arrows rotate and tilt, +/− zoom,
N north, H home, 1–4 focus an aircraft. `?demo=1` follows the scenes automatically.
