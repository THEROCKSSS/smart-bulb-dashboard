# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning
follows [Semantic Versioning](https://semver.org/) once the project
leaves prototype status.

Entries are written from the real git history and the build logs in
`iterations/` — nothing here is backfilled from memory. See `HANDOFF.md`
for the narrative version of the same history, and `iterations/` for the
blow-by-blow of what broke and got fixed along the way.

Going forward, GitHub's release-notes generation (`.github/release.yml`
categorizes PRs by label) drafts the bulk of each entry automatically from
merged PRs — see the note at the bottom of this file for the process.

## [Unreleased]

Nothing queued yet — see `roadmap/` for what's planned next.

## [0.3.0] — 2026-07-29

Built as four parallel phases (one subagent each, in isolated git worktrees,
hub-verified and hand-merged), plus a follow-up QoL round and a mobile fix
shipped as its own PR. Feature count: 137 → **159**.

### Added
- Two new audio-reactive modes: `harmonic_pairs` (two most-energetic
  non-adjacent bands mapped to complementary hues) and `kick_snare_split`
  (bass drives brightness, a mid/high band drives a hue accent).
- Remote-access hardening: session listing/revocation, an audit log
  (`backend/data/auth_audit.log`) that never records the PIN, and per-IP
  login rate limiting independent of the existing lockout.
- `bulbctl` — a stdlib-only CLI wrapping the full REST API (list/on/off/
  color/brightness/scene/status/login), with shell completions and
  scripting examples.
- `GET /api/analytics/usage` — real per-device on-time derived from logged
  history (not fabricated wattage — this bulb has no real power-draw data).
- A real backend pytest suite (mocked Tuya hardware layer) — previously
  none existed.
- Sleep/wake timer countdowns and the device status badge now tick live,
  client-side, instead of requiring a page refresh.
- Remembers your last device + panel across reloads, keyboard shortcuts on
  the Control panel (space = power, arrows = brightness), copy-to-clipboard
  on Diagnostics, and an Undo action on the cancel-timer toast that actually
  recreates the timer.
- A rounder, softer visual pass (8px → 12px radius, real card shadows,
  smoother hover states) and a genuine mobile-friendliness fix — the
  sidebar was collapsing to a ~16px sliver on phones due to an unreset
  grid-column span, now fixed, plus 44px tap targets throughout.
- Tailscale Serve documented and exercised as the recommended way to reach
  this dashboard off-LAN (tailnet-only HTTPS, no port-forwarding).
- Two new Pages-site views: an Active Roadmap (status-aware kanban, live
  issues tracker, per-week progress rings) and a Roadmap Archive
  (timeline, tagged database, time-share sunburst, analytics dashboard),
  replacing the old single roadmap page — plus a real network-graph page
  built from this repo's own `graphify` output (570 nodes, 1,002 edges).

### Fixed
- A real flicker bug in `harmonic_pairs`: two hue anchors exactly 180°
  apart cancel to `(0,0)` under the shortest-arc blend at a 50/50 energy
  split — caught by its own test suite, fixed with a fixed-direction blend.
- The mobile sidebar-squeeze bug described above (issue #62).
- A real test-isolation gap: most of the pytest suite's `client` fixture
  didn't isolate `remote_auth`'s on-disk state, so enabling the PIN gate
  for real (for the Tailscale exposure below) made 16 previously-passing
  tests fail with 401s — found by actually re-running the suite after
  that change, not caught in review.

## [0.2.0] — 2026-07-28

Two build rounds shipped together: audio-reactive lighting (and its v2
rework) and secure remote access, bringing the feature count from 97 to
**137**.

### Added
- Audio-reactive lighting: 12 interpretation modes (`band_fixed`,
  `dominant_band`, `weighted_blend`, `vu_meter`, `auto_rotate_hue`,
  `monochrome_pulse`, `strobe_on_drop`, `palette_cycle`,
  `spectrum_gradient`, `band_flash_overlay`, `stereo_split`,
  `breathing_silence`), capturing from any input device including
  VoiceMeeter/virtual-cable sources and real microphones.
- Multi-bulb audio orchestration (`GroupAudioSession`): unison,
  phase-offset (chase), and band-split roles sharing one audio analysis
  across a group.
- Sub-15ms internal decision latency, decoupled from a configurable
  per-bulb minimum "dwell" time (`min_dwell_ms`) so fast analysis doesn't
  outrun what's actually visible on the bulb.
- Network auto-discovery: weekly (configurable) background LAN scanning
  plus an on-demand "Scan Now," with discovered/ignored device management
  and automatic IP-change detection for already-configured bulbs.
- PIN-gated remote access for exposing the dashboard beyond the LAN:
  PBKDF2-hashed PIN, signed sessions, brute-force lockout (5 attempts / 5
  minutes), and a Settings toggle.
- `docs/remote-access-security.md` — Tailscale (recommended) vs.
  DuckDNS+PIN-gate guidance and threat model.
- `docs/network-discovery.md`, updated `docs/music-reactive-lighting.md`
  covering all 12 modes and the v2 latency/dwell design.
- `AGENTS.md` — onboarding doc for AI agents picking up this repo cold.
- `roadmap/` — a ~1000-item, 4-week phased backlog with a dependency
  graph, for future planned work.
- Three new project skills: `bulb-dashboard-audio`,
  `bulb-dashboard-discovery`, `bulb-dashboard-remote-access`.
- GitHub Pages documentation site.

### Fixed
Five real bugs found via actual testing, not just review — full detail in
`iterations/001` through `iterations/004`:
- An IP-change log entry reported the wrong "old IP" (mutated the value
  before reading it for the log).
- The audio brightness floor never actually reached its floor during
  silence — a stale additive constant dominated the calculation.
- Bulb network calls made directly inside the `sounddevice` audio
  callback froze the entire analysis pipeline whenever the bulb was
  slow/offline — fixed by moving all bulb I/O onto a dedicated per-bulb
  sender thread.
- Hue smoothing used a plain linear blend, which breaks at the 0°/360°
  wrap boundary (found in code review for the new `stereo_split` mode,
  fixed everywhere for consistency).
- The PIN gate blocked the root page itself once enabled — a real
  lock-yourself-out bug, found via a live Playwright test.

## [0.1.1] — 2026-07-27

### Added
- Polyform Noncommercial License 1.0.0.
- GitHub mirror links in `README.md`.

### Fixed
- Scrubbed a leaked device ID and LAN IP that had briefly been committed,
  before the GitHub mirror was made public.
- Copyright holder name corrected in `LICENSE`.

## [0.1.0] — 2026-07-27

Initial prototype. 97 working features — power/brightness/color control,
25 color presets, 15 mood scenes, 7 animated effects, sleep/wake timers, a
recurring schedule engine, multi-bulb groups, history, and diagnostics —
built after reverse-engineering local control of a Bytech A19 Wi-Fi
RGB+CCT bulb (Tuya protocol v3.5). See `HANDOFF.md` for how the local
credentials were obtained (the cloud-assisted QR login path, after two
other approaches failed).

---

## Process note

`.github/release.yml` configures label-based categorization for GitHub's
`gh release create --generate-notes`. When cutting a new version:

1. Tag PRs/commits with the right label (`enhancement`, `bug`, `documentation`, etc. — see `.github/release.yml`).
2. Run `gh release create vX.Y.Z --generate-notes` to draft categorized notes from everything merged since the last tag.
3. Fold the drafted notes into a new entry here, in this file's voice (not a raw PR-title dump) — the automation drafts, it doesn't replace editorial judgment.
4. Bump `APP_VERSION` in `backend/main.py` to match.
5. Add the matching entry to `docs/changelog.html`: a `<section class="changelog-entry">` with the rendered HTML (heading + Added/Fixed lists) **and** a `<template id="cl-XYZ">` holding a plain-text, Discord-flavored (`**bold**`, `- ` bullets) version of the same entry — that's what the page's "Copy for Discord" button actually copies. Give the button a `data-target` matching the template's `id`. Keep the two in sync; the template isn't auto-generated from the HTML.
