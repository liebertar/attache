# Attaché

**Agents can propose. Only the runtime can commit.**

A decision-authority runtime for fleets of long-lived agents bound to real assets —
machines, accounts, nodes. Agents observe and propose. The runtime holds the locks,
arbitrates conflicting proposals, checks each against an authority envelope denominated
in money and blast radius, executes the ones that pass, and writes an append-only record
of who committed what and why.

Agents have no `execute()`. There is exactly one code path to the real world, and the
runtime owns it.

---

## Why a runtime and not a framework

Neural policies are non-deterministic. You cannot certify their output the way you certify
a PLC. The only way to deploy one against real money or real motors is to stop trying to
constrain the policy and start constraining what may be committed.

That layer is missing today:

| Layer | Answers |
|---|---|
| MCP / A2A | how agents reach tools |
| NVIDIA NeMo Relay | what happens during a single call — block, retry, route, trace |
| **Attaché** | **who may propose, who wins a conflict, how much is autonomous, who committed** |
| Policy pack | the numbers, per domain |

Guardrails block bad calls. Attaché chooses among good ones.

---

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
