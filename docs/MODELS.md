# Models

One client, one contract: OpenAI-compatible `/chat/completions`, three tiers, JSON answers, a timeout per
call, every reply recorded to disk when `LLM_RECORD_DIR` is set. Whatever a model returns is a proposal that
code validates. If a model is missing, slow or wrong, the rule that would have been used anyway is used.

## Who runs where

| Tier | Nebius Token Factory | Local (Ollama) | Sits | Reads | Writes |
|---|---|---|---|---|---|
| Nano | `nvidia/Nemotron-3_5-Lightning` (30B-A3B) | `nemotron-3-nano:4b`, one server per aircraft (`scripts/ollama_fleet.sh`) | in each aircraft's agent | telemetry, the concern, the airspace copy, refusal feedback | the request form (action + one-line rationale), a route draft after an airspace refusal |
| Super | `nvidia/nemotron-3-super-120b-a12b` (120B-A12B, 1M context) | `nemotron-3-nano:4b` as a stand-in | beside the runtime | prose notices, incident reports, search snippets, the ledger context behind an advisory | a structured form (kind, place, window, numbers), a two-sentence advisory summary and one option id |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | not run locally | beside the runtime | the set of proposals that all passed | one choice from that set and a reason |

Why these: the Nemotron 3 cards describe agentic workflows, tool calling, long-context reasoning and
instruction following. That is exactly the shape of the work here — forms, JSON, reading long messy text.
None of them is trained for route geometry between buildings, and none is asked to be. The judge is code.

## What the Nano actually achieves

Measured on the local fleet (four `nemotron-3-nano:4b` servers, `reasoning_effort=none`):

| Task | Result |
|---|---|
| Request forms | answered every time, median 1.2 s |
| Route drafts after a refusal | median 20 s; a minority clear the judge on the first try; routes over 100 m buildings on long Manhattan crossings fail and fall back to A* |
| Retry with feedback (which building, its roof, which side is clear) | improves short Brooklyn routes; long crossings still fall back |

The map labels every corridor with who drew it: `A*`, `nano`, or `straight`. The ledger keeps the model id.
Each agent registers every 30 s with its model and whether that model answered since the last time; if every
call fell back to rules, the map says `rules` under that aircraft instead of the model name.

## Fallback chain

`scripts/dev.sh` and `compose.yaml` resolve this without flags and print what they chose:

```
NEBIUS_API_KEY set        → Nebius: Nano per aircraft, Super and Ultra at the tower
else Ollama fleet answers → local 4B per aircraft, 4B stand-in at the tower
else Ollama answers       → one local model for everyone
else                      → rules only (forms by rules, routes by A*, prose left for a person)

TAVILY_API_KEY set        → live search, held for a person
else                      → simulated bulletins on the same schedule

network                   → METAR from aviationweather.gov
else                      → simulated weather report
```

Nothing else changes between these modes. The judge, the ledger and the scoreboard are identical.

## Running with a model

```sh
# local fleet
ollama pull nemotron-3-nano:4b
scripts/ollama_fleet.sh start 4
./scripts/dev.sh

# Nebius
NEBIUS_API_KEY=... ./scripts/dev.sh
```

`LLM_REQUEST_EXTRA='{"reasoning_effort":"none"}'` keeps Nemotron's thinking mode off for the short forms.
`DRAFT_TIMEOUT_S` (default 60) bounds a route draft from the moment of the refusal; the display delay runs
inside it, so the screen never waits on the model.
