# AGENTS.md — Start Here If You're an AI Agent

This file is written for an AI coding agent (Claude Code, or any other)
picking this repo up cold, with no prior context. If you're a human, the
[README](README.md) and [Setup Guide](SETUP.md) are the better starting
points — this file is deliberately terser and more directive.

## What this project is

A local, cloud-independent dashboard + REST API for controlling Tuya-based
Wi-Fi smart bulbs. FastAPI backend (`backend/`), vanilla-JS frontend
(`frontend/`, no build step). 170 working features as of the last count in
`FEATURES.md` — audio-reactive lighting (14 modes, multi-bulb
orchestration), network auto-discovery, scenes/effects/schedules, a
PIN-gated remote-access path with multi-PIN support, session management,
audit logging and per-IP rate limiting, a stdlib CLI (`cli/bulbctl.py`),
and a real pytest suite (`backend/tests/`, `cli/tests/`).

## Do this first, in order

1. **Read `HANDOFF.md`** — the condensed, current-state summary of what's
   real, what's fragile, and what's explicitly not built yet. It's kept up
   to date after every significant round of work.
2. **Read `iterations/README.md`**, then skim the numbered entries under
   `iterations/`. Each one documents a real build-test-fix cycle: what was
   attempted, what actually broke, and how it was fixed. **This is where
   the non-obvious lessons live** — don't re-derive them from scratch.
3. **Check `ROADMAP.md`** for what's deliberately deferred and why
   (hardware-gated items, explicitly-not-yet-decided tradeoffs).
4. **Check `roadmap/`** if you're picking up planned-but-not-started work
   — it's a large (~1000-item), phased backlog. `roadmap/README.md`
   explains how items there are meant to become real issues/PRs, and
   includes a dependency graph showing which phases build on which.
5. **Check `.claude/skills/`** for the specific subsystem you're touching
   — each major feature area has its own skill file with a tested
   procedure, known pitfalls, and a verification method:
   - `bulb-dashboard-setup` — getting a bulb's local_key, installing, running
   - `bulb-dashboard-control` — driving color/brightness/scenes/effects via the API
   - `bulb-dashboard-audio` — the audio-reactive engine, all 12 modes, orchestration
   - `bulb-dashboard-discovery` — network auto-scan
   - `bulb-dashboard-remote-access` — the PIN gate, Tailscale/DuckDNS setup

## Mistakes already made here — don't repeat them

- **Never call blocking network I/O (tinytuya's `set_*` methods, or
  anything socket-based) directly from inside a `sounddevice` audio
  callback.** A real bug (iteration 002) showed this freezes the entire
  audio analysis pipeline whenever the bulb is slow or offline. The fix —
  a dedicated per-bulb sender thread (`BulbSender` in `audio_reactive.py`)
  that the callback only ever enqueues into, never calls directly — is the
  established pattern. Follow it for any new bulb-I/O-adjacent feature.
- **Hue smoothing must use circular-mean blending (`_smooth_hue()` in
  `audio_reactive.py`), never a plain linear blend.** A linear blend
  breaks at the 0°/360° wrap boundary. This was caught in code review, not
  testing, for exactly one new mode (`stereo_split`) whose target crosses
  that boundary — but it's now applied everywhere for consistency. Any
  new mode computing a smoothed hue target should use this helper.
- **Every route must be reachable when the PIN gate is disabled (the
  default), and the root page `/` itself must stay in
  `remote_auth.OPEN_PATHS` even when the gate is enabled.** A real bug
  (iteration 004) gated `/` itself, meaning the PIN entry page couldn't
  load once the feature was turned on — a total lockout. If you touch
  `remote_auth.py`'s open-path list, understand why `/` is there before
  changing it.
- **Never install a new Python dependency into a shared/global venv.**
  Every dependency for this project goes into `backend/venv/` specifically
  (see `backend/requirements.txt`). This project's own early build session
  polluted a shared `hermes-agent` venv this way and had to undo it.
- **This bulb (and Tuya Wi-Fi bulbs generally) genuinely drops off Wi-Fi
  periodically.** If a test session shows the bulb offline, that's very
  likely real hardware behavior, not a code bug — confirm independently
  (a raw `ping`, a raw `tinytuya scan`) before assuming your code is
  broken. Several rounds of this project's own testing hit exactly this
  and had to fall back to synthetic-signal tests or mocked data instead of
  live-hardware verification — that's a legitimate, documented fallback,
  not a shortcut to feel bad about.
- **The general API rate limiter (`api_rate_limit.check()`) must only ever
  be called from the HTTP middleware in `main.py`.** Scoping it to the ASGI
  layer is the only thing keeping the audio-reactive engine's internal
  per-bulb dispatch out of a budget meant for external clients — call it
  from service-layer code and a long lightshow starts 429-ing the user's own
  browser. A test scans the backend source and fails on a second call site.
- **Local `local_key` values and any real PIN must never end up in a
  commit, a log line, or a doc.** `config.json` and `backend/data/` are
  git-ignored for exactly this reason — check `.gitignore` before adding
  any new file that might carry a secret, and confirm via a Forgejo/GitHub
  API readback after pushing (this project has done that check after
  every push so far) rather than assuming `.gitignore` alone is proof.

## Definition of done, on this project

Before considering a change complete:
- It was actually run/tested — a command that executed, a test that
  passed, an API response that was read back. Not "should work."
- If it's a genuinely new feature or a real bug was found while building
  it, write an `iterations/NNN-slug/README.md` entry following the
  template in `iterations/README.md`. If it's a small fix with no real
  dead end, a good commit message is enough — not everything needs an
  iteration entry.
- Relevant docs are updated in the same pass: `FEATURES.md` (feature
  count), `API.md` (new endpoints), `ROADMAP.md` (move an item from
  planned to done, or update its status), and the relevant skill file
  under `.claude/skills/`.
- `HANDOFF.md` gets a short update if the change is significant enough
  that someone resuming cold would need to know about it.
- Cutting a new version updates `CHANGELOG.md` **and** `docs/changelog.html`
  together — the live Pages changelog isn't generated from the markdown
  file, it's hand-kept in sync. Each version needs both the rendered HTML
  section and a matching `<template>` with Discord-flavored plain text
  (see `CHANGELOG.md`'s own process note for the exact pattern) — that
  template is what the page's "Copy for Discord" button actually copies.

## Explicit scope boundaries

- Don't enable the PIN gate with a placeholder/test PIN and call security
  work "done" — `test1234` and similar were used during this project's own
  development and explicitly need rotating before any real exposure.
- Don't build Bluetooth bulb support or buy hardware on the user's behalf
  — it's explicitly deferred in `ROADMAP.md` until real BLE hardware is
  acquired, and `roadmap/week-4-analytics-polish-and-release.md` section 4
  is prep-only work, not a green light to purchase anything.
- Don't materialize the entire `roadmap/` backlog (~1000 items) as live
  GitHub issues in one pass — convert a small, relevant slice at a time
  (see `roadmap/README.md`'s own guidance on this).
- Don't claim a security-hardening item "done" without the verification it
  actually calls for — see how `iterations/004-pin-gate-remote-auth/` was
  verified (real lockout test, real session-expiry test, real browser
  flow) as the bar to match.
