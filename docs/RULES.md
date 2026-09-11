# Rules the runtime enforces

Every number here is enforced by code. Nothing in this table is decided by a model. Constants live in
`holdshort/core/geo.py`, `holdshort/core/route.py`, `holdshort/runtime/intents.py` and `configs/fleet.yaml`.

## Airspace

| Rule | Value | Where it comes from |
|---|---|---|
| Buildings known to the judge | every OpenStreetMap building ≥ 20 m in the service box (34,581) | tile extraction, same tiles the map draws |
| Vertical clearance over a roof | 50 m | operator requirement |
| Reduced clearance | 20 m, only for roofs < 40 m that sit under an FAA cell whose ceiling makes 50 m impossible (200 ft cells) | derived per building at load |
| Lateral separation from a building | 10 m | judge |
| Lateral separation from a closed cell or zone | 40 m | judge |
| Ceiling | FAA UAS Facility Map cell ceiling (0 / 30 / 61 / 91 / 122 m); 0 ft cells are forbidden at every altitude; 121.9 m outside the grid | FAA data |
| Cruise band | 70 m to 120 m; in a cell with a lower ceiling, ceiling − 1 m | planner floor = 20 m threshold + 50 m clearance |
| Leg length cap | 50 km, and a cell-scan cap so a malformed leg cannot stall the judge | judge |
| Takeoff and landing columns | the vertical climb and descent at each end are judged as legs, in steps | judge |
| Landing site | no forbidden volume within 50 m (15 m for buildings under 40 m); not inside a zone; not within 30 m of a parked aircraft; no other aircraft due at the same area in the same window | judge + intents |

## Separation between aircraft (4D intents)

| Rule | Value |
|---|---|
| Lateral half-width of a live corridor | 30 m, plus the other aircraft's navigation tolerance (10 m) |
| Vertical half-height | 25 m, plus one tick of climb |
| Time padding | ±30 ticks around the schedule computed from the fleet performance in `configs/fleet.yaml` |
| Resolution ladder (operator side) | climb +30 m if the ceiling allows → depart after the conflicting volume clears (up to 3 tries, increasing) → redraw with A* → decline |
| Withdrawal | an undeparted intent may be withdrawn to make room for an airborne re-file; the withdrawal is executed only after the re-file committed |
| Presence | an airborne aircraft with no live intent (recalled, declined) is a column nobody may enter |
| Lost link | a silent aircraft keeps its cleared volumes reserved until its planned arrival plus the time padding |

## Rules that arrive during flight

| Arrival | Effect |
|---|---|
| NOTAM the grammar can read (FAA-style bounded area, altitude band, Zulu window) | applied at the tick it is due: cleared routes through it are recalled and leave by the nearest exit, new routes refused |
| Airworthiness directive for a type | every `fly_route` of that type refused until the directive ends |
| Prose the grammar cannot read | the Super model structures it; code validates; a person confirms in the inbox before it applies |
| Weather: wind > 10 m/s, gust > 12 m/s or visibility < 1,500 m | fleet-wide takeoff hold at once (trusted sources), airborne aircraft continue and land; lifted by a person or expiry |
| Incident at a building (fire, collapse, police) | forbidden circle, default 150 m (50–500 m accepted), all altitudes; routes through it recalled; landing areas inside unusable |

## What is not a rule here

Money, budgets and per-aircraft spend are the operator's business; the demo configuration sets the caps to
`null` and the runtime does not look. Battery and charging are the operator's business. Route choice is the
operator's business; the runtime never draws a route. Tactical collision avoidance on board the aircraft is
the manufacturer's business; the runtime does not rely on it.
