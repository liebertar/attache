# Decisions

Short records of choices that shaped the system, with the reason. Newest last.

**No model in the judgement path.** Aviation already settled this: automation with authority is
deterministic, advisory tools may use whatever they like. A refusal must be reproducible from the ledger.

**The runtime verifies; it never plans.** Route choice, dispatch, budgets and battery belong to the
operator. If the runtime planned routes it would own liability for them and stop being an authority.

**Two worlds from one seed, never rigged.** The direct wiring runs the same agent code against the same
detector and form writer; it merely has nowhere to file. A demo that sabotages the control loses the argument.

**Buildings come from the tiles the screen draws.** The judge and the map must see the same shapes, or the
picture lies. Threshold 20 m, because the first version knew only 40 m+ and approved 40 m legs over 35 m roofs.

**70 m cruise floor, bent under low cells.** A fixed 70 m floor made the whole of upper Manhattan
unreachable (200 ft cells). Under such a cell the floor is the ceiling and low roofs get 20 m; tall roofs keep
50 m, which forces a lateral detour.

**Free routing, not sky lanes.** Fixed corridors would make separation trivial and the agents pointless.
Cleared routes become temporary reserved volumes instead.

**Tighten now, loosen with a person.** A rule that closes airspace applies the tick it arrives; a rule that
opens it waits for a human or an expiry. Model-read prose is always held for a person before it applies.

**Lost link: continue and land, never return.** A return path is an uncleared path, and if the tower itself
fails every aircraft would turn at once into each other. Finishing the cleared route keeps the deconfliction
that already exists. The runtime's job is to keep the dark aircraft's space reserved.

**Nemotron for forms, prose and summaries; A\* for geometry.** Measured, not assumed: local drafts clear the
judge only on short routes. The map says who drew each corridor so this is visible, not hidden.

**Budgets removed from the runtime.** Money caps put a healthy aircraft into a human queue mid-round. That
is the operator's concern; the runtime's authority is physical.

**Roof bays 31 m apart.** Designated, visible, one per aircraft. 22 m tripped the 30 m parked-aircraft rule;
31 m clears it while takeoff columns still overlap, so simultaneous departures serialise — the right picture.

**One round is 5,000 ticks.** A Harlem round trip under the strict rules takes about 4,040 ticks.

**Intake sources are trusted by origin, not by parser.** A METAR from the aviation weather service applies at
once; a search snippet or a manual post waits for a person even when the grammar read it perfectly.

**SQLite for the intake store, on the same volume as the ledger.** Same availability needs, no new service,
swappable behind a thin interface if a deployment wants Postgres.

**Name.** Attaché described a helper that attaches to something. The system is a clearance authority: agents
hold short until the tower clears. Hence Holdshort.
