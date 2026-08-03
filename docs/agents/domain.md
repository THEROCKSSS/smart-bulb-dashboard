# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

**Layout: single-context.** One `CONTEXT.md` + `docs/adr/` at the repo root.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

Neither exists yet as of this file being written — that is expected, not a gap to fill.

## Read these too, on this repo specifically

This project keeps its real decision record outside `docs/adr/`, and skipping
these is how a session rediscovers something already settled:

- **`HANDOFF.md`** — the START HERE recovery document. The top section
  describes the project *now*; everything under "History" is round-by-round
  past state. Read the top before doing anything; read the history only when
  you need the *why* behind a decision.
- **`AGENTS.md`** — repo conventions and constraints.
- **`docs/audio-modes.md`** — all 20 audio modes, the genre presets, and
  tuning-by-symptom guidance. Required reading before touching anything in
  `audio_reactive.py` or `audio_signal.py`.
- **`SECURITY.md`** — the threat model. Required before touching the PIN
  gate, rate limiter, reverse-proxy handling, or secret redaction.

## File structure

```
/
├── CONTEXT.md          ← not yet created
├── docs/adr/           ← not yet created
├── docs/agents/        ← this directory
└── backend/, frontend/, cli/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

Until `CONTEXT.md` exists, this codebase's working vocabulary is already
fairly settled and worth matching rather than reinventing: **session**
(one running audio-reactive capture), **mode** (one of the 20 colour
behaviours), **genre preset** (a saved bundle of session arguments),
**role mode** (how multiple bulbs divide a signal), **dwell** (minimum ms
between bulb commands), **band** (a log-spaced frequency bucket).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_
