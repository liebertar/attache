# Attaché

**Agents can propose. Only the runtime can commit.**

*[한국어](README.md)*

A decision-authority runtime for fleets of long-lived agents bound to real assets —
machines, accounts, nodes. Agents observe and propose. The runtime holds the locks,
arbitrates conflicting proposals, checks each against an authority envelope denominated
in money and blast radius, executes the ones that pass, and writes an append-only record
of who committed what and why.

Agents have no `execute()`. There is exactly one code path to the real world, and the
runtime owns it.

---

## The problem

Three agents on a factory floor. Safety says stop the machine. Operations says the deadline
matters, keep running. Maintenance says order a $340 part. All three are reasonable.
**Who decides?**

Today nobody does, so nobody gives agents real authority and a human checks everything.

## The approach

A corporate card. Employees do not touch the company account — they request a charge.
Under the limit it clears; over it, a manager looks. Either way there is a record of who
asked and who approved.

Attaché is that system for agents. An agent cannot act. It files a request with a cost and
a blast radius attached. The runtime checks the limit, resolves conflicts, executes what
passes, and records who committed what. Agent code has no `execute()`.

## Why it is needed

An AI can answer differently to the same situation twice. Nothing can certify that it will
never be wrong. To put something uncertifiable in front of real machines or real money, you
stop policing what the model thinks and put a fence around what it can actually do.

That fence does not exist yet:

| Layer | Answers |
|---|---|
| MCP / A2A | how agents reach tools |
| NVIDIA NeMo Relay | what happens during a single call — block, retry, route, trace |
| **Attaché** | **who may propose, who wins a conflict, how much is autonomous, who committed** |
| Policy pack | the numbers, per domain |

Guardrails block bad calls. Attaché chooses among good ones.

---

## Architecture

```
        configs/robot.yaml            configs/finance.yaml
                 └──────────┬──────────────────┘
                            │  assets, subsystems, resources, authority
   ┌────────────────────────▼────────────────────────┐
   │                     runtime                     │
   │                                                 │
   │   lock table        exclusive resources         │
   │   arbiter           only when proposals contend │
   │   authority         cost_usd, blast_radius      │
   │   commit            the one path outward        │
   │   ledger            append-only, why-chain      │
   └───┬───────────────────────────────────┬─────────┘
       │                                   │
  asset agents                         adapters
  rules → Nano → Super → propose       gpio · payments · kubectl
       │                                   │
   NeMo Relay                          the world
   scopes, tracing, middleware         a motor stops
                                       a refund posts
       │
   Tavily
   what the fleet cannot know from inside itself
```

| Component | Responsibility |
|---|---|
| `runtime` | locks, arbitration, authority, commit, ledger |
| `agent` | observe on rules, escalate to a model, propose — never execute |
| `adapters` | the only code that touches the world; called by commit alone |
| `configs` | what the domain is and what the limits are |
| `ui` | the approval screen a human sees when a limit is crossed |

## Domain independence

The runtime knows nothing about motors or refunds. Domains are config.

```
attache run configs/robot.yaml      # a motor stops
attache run configs/finance.yaml    # a refund is approved
```

Same binary. Same commit path. Same ledger.

---

## Path of one proposal

```
asset agent ── rules, always on, no model call
     │  anomaly → small model → diagnosis (+ external evidence via Tavily)
     ▼
  propose(action, cost_usd, blast_radius, evidence)
════════════ runtime boundary — agents have no execute() ════════════
     ▼
  resource lock ──contended──► arbitrate (large model, only on conflict)
     ▼
  authority check ──over limit──► human approval
     ▼
  COMMIT ──────────────────────► the world (GPIO / API / kubectl)
     ▼
  ledger.jsonl  { committed_by: runtime, from, evidence, limit_checked }
```

---

## Escalation ladder

Most subsystems never call a model. Cost is the design constraint, not an afterthought.

| Tier | Runs | Model |
|---|---|---|
| rules | always | none |
| subsystem | on rule trip | Nemotron 3 Nano — 30B total / 3B active |
| asset | on anomaly | Nemotron 3 Super — 100B / 10B active |
| arbitration | on conflict only | Nemotron 3 Ultra — 550B / 55B active |

Served on Nebius Token Factory.

---

## Primitives

`asset` · `subsystem` · `signal` · `resource` · `proposal` · `commit` · `escalation` · `authority`

Audit is not a ninth primitive. It is an invariant: every commit carries the chain that produced it.

---

## Status

Early. Built for the Nebius x NVIDIA Global AI Hackathon 2026, Physical AI track.

## License

Apache-2.0
