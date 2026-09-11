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

## Submission checklist

- [ ] Repository public, license present, README in English with run instructions (`docker compose up --build`)
- [ ] Runs on Nebius Token Factory: `NEBIUS_API_KEY` wiring measured and the numbers in `docs/MODELS.md`
- [ ] Video < 3 min following `docs/DEMO.md`, with the real autopilot scene (PX4 SITL) if it lands in time
- [ ] Demo URL: the map served from a public host, or the compose stack on a small VM
- [ ] Devpost text: what it does, how it is built, what the models do and do not do, the standards position
- [ ] Tool feedback for Nebius and NVIDIA (required field; also a bonus prize)
- [ ] Optional: a Tavily source live in the intake for the Tavily bonus

## Open questions

- City prize selection differs between the Devpost rules and the Korean organiser's notice.
- Credit amounts and validity for Token Factory and Tavily.
- Nebius Robotics & Physical AI Awards ($1.5M in credits) is a separate programme from the hackathon prizes;
  application path unknown.
