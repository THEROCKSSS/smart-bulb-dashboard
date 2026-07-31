# Smart Bulb Dashboard

A local, cloud-independent dashboard for controlling Tuya-based smart bulbs
(Wi-Fi RGB+CCT bulbs sold under many brands — Bytech, Merkury, Feit, Teckin,
Govee's Tuya-based line, etc). Built as a prototype after reverse-engineering
local control of a Bytech A19 bulb — see `HANDOFF.md` for the full story of
how the device credentials were obtained.

Everything talks to the bulb directly over your LAN via [tinytuya](https://github.com/jasonacox/tinytuya).
No cloud round-trip for day-to-day control.

> **Repo:** GitHub → [`THEROCKSSS/smart-bulb-dashboard`](https://github.com/THEROCKSSS/smart-bulb-dashboard) (public).
> Licensed **noncommercial** — see [License](#license).

![Control panel](docs/screenshots/control.png)

## What's here

- **186 working features** — see `FEATURES.md` for the full itemized list
  (power/brightness/color control, 25 color presets, 15 mood scenes, 7
  animated effects, sleep/wake timers, a recurring schedule engine,
  multi-bulb groups, history, diagnostics, a CLI, usage analytics, and more).
- **14 audio-reactive lighting modes** with sub-15ms decision latency and
  multi-bulb orchestration (unison/chase/band-split) — see
  `docs/music-reactive-lighting.md`.
- **Network auto-discovery** (weekly or on-demand LAN scanning) — see
  `docs/network-discovery.md`.
- **A PIN-gated remote-access path** for reaching the dashboard safely
  from outside your LAN (Tailscale recommended, DuckDNS+PIN as the public
  alternative) — see `docs/remote-access-security.md`.
- **Observability built in** — a Prometheus `/metrics` endpoint, per-endpoint
  latency percentiles, a System Health page, a log viewer with correlation
  IDs, and a secrets-redacted self-diagnostic report — see
  `docs/observability.md`.
- A FastAPI backend (`backend/`) exposing a 70+-route REST API.
- A vanilla-JS dark-themed dashboard (`frontend/`) — no build step, no
  framework.
- Project-local Claude Code skills (`.claude/skills/`) that teach an agent
  how to set this up and drive it. **If you're an AI agent, read
  `AGENTS.md` first.**
- A **month-long, ~1000-item roadmap** (`roadmap/`) for planned future
  work, phased across 4 weeks with a dependency graph.
- A live documentation site at
  [therocksss.github.io/smart-bulb-dashboard](https://therocksss.github.io/smart-bulb-dashboard/) —
  a hand-built, hand-styled 9-page site (Home, Setup, Features, API,
  Audio-Reactive, Security, Roadmap, Changelog, Build History), not the
  stock Jekyll theme this used to be.
- A real `CHANGELOG.md`, with `.github/release.yml` set up so
  `gh release create --generate-notes` drafts future entries automatically.

## Quickstart

```bash
cd backend
python -m venv venv
./venv/Scripts/activate   # or source venv/bin/activate on Linux/Mac
pip install -r requirements.txt
cp config.example.json config.json
# edit config.json with your bulb's device_id, local_key, and ip — see SETUP.md
python -m uvicorn main:app --host 0.0.0.0 --port 8500
```

Then open **http://localhost:8500**.

Full step-by-step instructions, including how to obtain your bulb's
`device_id` and `local_key` (the hard part), are in **SETUP.md**.

## Docs

| File | What it covers |
|---|---|
| `SETUP.md` | Step-by-step setup from zero, including getting Tuya credentials |
| `API.md` | Full REST API reference with curl examples |
| `FEATURES.md` | Every implemented feature, itemized (159 total) |
| `ROADMAP.md` | Near-term planned work and what's deliberately not built yet |
| `roadmap/` | The month-long, ~1000-item phased backlog with a dependency graph |
| `docs/music-reactive-lighting.md` | The 14 audio-reactive modes, latency/dwell design, orchestration |
| `docs/network-discovery.md` | Auto-scan and manual network discovery |
| `docs/remote-access-security.md` | Tailscale vs. DuckDNS+PIN gate, session management, audit logging, firewall rules |
| `SECURITY.md` | Responsible disclosure, no-telemetry guarantee, dependency scan findings |
| `docs/pin-gate-threat-model.md` | Formal threat model: assets, attackers, what's defended and what explicitly isn't |
| `docs/observability.md` | `/metrics`, the System Health page, log viewer, diagnostic report, network resilience |
| `cli/bulbctl.py` | Stdlib-only CLI wrapping the full REST API — see `cli/tests/` and `cli/examples/` |
| `HANDOFF.md` | How this was built, verified, and what's known-fragile |
| `iterations/` | Real build-test-fix logs — what broke and how it was fixed, round by round |
| `AGENTS.md` | Start here if you're an AI agent picking this repo up cold |

## Screenshots

| Scenes | Presets |
|---|---|
| ![Scenes](docs/screenshots/scenes.png) | ![Presets](docs/screenshots/presets.png) |

## Multi-bulb ready

Only one physical bulb exists today, but the config format
(`backend/config.example.json`) is already a list, and there's a working
`groups` feature (broadcast power/color to a set of device IDs). Adding a
second bulb later is: add an entry to `config.json`, restart, done — no code
changes. Multi-bulb audio orchestration (unison/chase/band-split roles) is
already built and tested against fake controllers plus the real API with a
1-bulb group — real multi-bulb visual testing is the first thing to do
once a second bulb is purchased (see `ROADMAP.md`).

## Security: what this does and doesn't claim to protect against

Read this before you expose the dashboard beyond your LAN. Full detail in
[`SECURITY.md`](SECURITY.md) and the formal
[PIN-gate threat model](docs/pin-gate-threat-model.md).

**It does protect against:**

- Casual/automated discovery of an internet-exposed dashboard — *when you
  enable the PIN gate*, which is off by default.
- PIN guessing: a per-IP lockout (5 wrong attempts → 5 minutes) plus an
  independent per-IP rate limit on the login endpoint itself.
- Session theft after logout — sessions are revoked server-side, listable,
  and individually revocable, not just cookie-cleared.
- Your bulb's `local_key` leaking through the API, the logs, or the
  self-diagnostic report.
- Silently ending up exposed: if the dashboard is ever reached from a public
  IP with the PIN gate off — or the gate gets turned off *after* you set up
  a port forward — it shows a persistent warning until you fix it.

**It does NOT protect against, and does not claim to:**

- **Anyone who can read your network traffic.** The dashboard speaks plain
  HTTP. Forward a port to the internet without TLS in front and your PIN
  crosses the wire in cleartext. Use Tailscale, or put Caddy in front.
- **Anyone with an account on the machine running it.**
  `backend/config.json` holds your bulb's `local_key` in plaintext — that's
  what Tuya's local protocol requires. Anyone who can read that file
  controls your bulbs directly, dashboard or not. It's git-ignored and the
  dashboard redacts it in every API response; don't commit your real
  `config.json`, use `config.example.json` as the template.
- **Other devices on your LAN.** The default posture is LAN-only with *no*
  authentication, on the assumption your home network is trusted.
- **Multiple users at different trust levels.** There is exactly one PIN,
  shared by everyone you give it to.
- **Denial of service.**

**No telemetry.** Nothing here phones home — no analytics, no crash
reporting, no update check, no CDN, no external fonts. The single outbound
internet request in the whole codebase is the public-IP lookup in Settings,
which runs only when you press the button. See `SECURITY.md` for how to
verify that yourself.

Found a security problem? Report it privately — see
[`SECURITY.md`](SECURITY.md).

## License

Released under the **Polyform Noncommercial License 1.0.0** — see
[`LICENSE`](LICENSE). Personal, study, and contribution use is free and
welcome. **Commercial use (selling it, offering it as a paid service, or
building a paid product on top of it) is NOT permitted** without a separate
commercial license from the author. See the LICENSE file for how to inquire
about commercial terms.
