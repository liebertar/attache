# Hackathon notes

Nebius × NVIDIA Global AI Hackathon 2026 · Devpost · track: **Physical AI**.

## Dates

| | |
|---|---|
| Submission window | Aug 26 – **Oct 30, 10:00 PT** (Oct 31 02:00 KST) |
| Judging | Dec 1 – 15, one global round |
| Announcement | Jan 11, 2027 |
| Seoul build session (Sep 11) | a build session, not a submission: no judging, no pitch. Attending only qualifies for the city prize and the Nebius/Tavily credits |

## Rules that matter for us (checked on the official rules page, Sep 9)

- Several submissions per entrant are allowed if they are "substantially different". Each project can win one
  Overall or one Track award plus one Bonus. A person may be on several teams and also enter alone.
- Cash is only in the Overall awards ($20,000 / $10,000 / $6,000); all four tracks compete together for it.
  Track winners get a Jetson Orin Nano. Bonus: Best Use of Tavily $3,000; 20 city prizes of $500;
  Most Valuable Feedback $100 × 10.
- Requirements: at least one NVIDIA open model (Nemotron), runs on Nebius Token Factory or AI Cloud, public
  repository with an open-source license, video under three minutes, working demo URL. Projects that existed
  before Aug 26 must have been significantly updated since (this one started Aug 31).
- Judging: four criteria, equally weighted — technological implementation, design, potential impact,
  quality of the idea.

## What answers which prize

| Prize or requirement | What answers it |
|---|---|
| NVIDIA open model (required) | Nemotron in every drone's agent: it writes the request form and picks a route by calling `choose_route`. Super reads prose notices and the briefing pages the grammar cannot. Every filing carries `params.model_trace`, which the map's hover card shows in plain words |
| Runs on Nebius (required) | `NEBIUS_API_KEY` alone switches every tier to Token Factory, under `scripts/dev.sh` and under compose. Optional cuOpt dispatch (`DISPATCH=cuopt`, `CUOPT_URL`) is meant for a Nebius AI Cloud GPU; so far it is tested only against a fake server that follows cuOpt's REST protocol |
| Best Use of Tavily | The pre-flight briefing: search, extract, crawl and research about the places and the day being flown. Every rule carries its source URL, title, domain and fetch time into the ledger (`briefing_run`, `briefing_item`, `briefing_rule`), the intake store and `/state.briefing` |
| Physical AI track | One guarded aircraft flown by a real PX4 autopilot (SIH) behind the runtime: the cleared route becomes a PX4 mission, a recall reaches the autopilot, a refused filing never arms it |

## Submission checklist

- [ ] Repository public, license present, README in English with run instructions (`docker compose up --build`,
  and `docker compose --env-file .env.local up --build` with keys)
- [ ] Runs on Nebius Token Factory: `NEBIUS_API_KEY` wiring measured, tool calling included, and the numbers in
  `docs/MODELS.md`
- [ ] Video < 3 min following `docs/DEMO.md`, recorded with `scripts/demo.sh`; the PX4 scene with `scripts/sitl.sh`
- [ ] Demo URL: the map served from a public host, or the compose stack on a small VM
- [ ] Devpost text: what it does, how it is built, what the models do and do not do, the standards position
- [ ] Tool feedback for Nebius and NVIDIA (required field; also a bonus prize)
- [ ] Tavily key set and one live briefing on video, so the cited rules are live rather than "recorded"
- [ ] Optional: cuOpt dispatch run once against a real cuOpt server on a Nebius AI Cloud GPU

## Open questions

- City prize selection differs between the Devpost rules and the Korean organiser's notice.
- Credit amounts and validity for Token Factory and Tavily.
- Nebius Robotics & Physical AI Awards ($1.5M in credits) is a separate programme from the hackathon prizes;
  application path unknown.
