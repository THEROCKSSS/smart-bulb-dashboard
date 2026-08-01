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

### Security — starlette CVEs closed (#74)

**No user action required beyond `pip install -r backend/requirements.txt`.**
Affected: every version up to and including the current `master`.

`fastapi` `0.115.6` → **`0.141.1`**, and `starlette` is now pinned
**directly** at **`1.3.1`** instead of being left to FastAPI's floor. This
closes all seven advisories previously listed in `SECURITY.md`, two of
which genuinely applied to this project:

- **CVE-2026-48818** (PYSEC-2026-2281) — `StaticFiles` could be steered
  onto a UNC path on Windows, making the service open an outbound SMB
  connection and leak its NTLMv2 hash. This project mounts `StaticFiles`
  at `/static` and is commonly run on Windows.
- **CVE-2025-62727** (PYSEC-2026-1942) — quadratic `Range`-header parsing
  in `FileResponse`, a cheap CPU-exhaustion DoS against `/` and
  `/static/*`.

Both were mitigated in practice by the LAN-only default and **not**
mitigated for anyone running a forwarded port. If you have exposed this to
the internet, upgrade.

The only code change the bump forced: `@app.on_event("startup")` is gone,
replaced by the `lifespan` context manager FastAPI now requires. Middleware
execution order (`observe_request` → `api_rate_limiter` →
`https_enforcement` → `pin_gate`) is unchanged, verified by a runtime
probe as well as the suite. All 743 tests pass.

**Week 1 roadmap — open as [PR #68](https://github.com/THEROCKSSS/smart-bulb-dashboard/pull/68), not yet merged to `master`.**

Built as four parallel phases (one subagent each, in isolated git
worktrees, hub-verified, then hand-merged), tracked in issues #64–#67 and
mapping back to the 14 Week 1 section issues #1–#14. Backend test suite
grew from 76 to **353 tests**, all passing.

Because all four phases modified the same core classes (`AudioSession`,
`GroupAudioSession`, `BulbSender`, and the audio routes in `main.py`),
integration was done as real 3-way `git merge`s with every conflict
hand-resolved to combine both sides' intent — roughly 40 conflict blocks —
rather than by applying patches. The merge itself surfaced 5 real
regressions, listed under Fixed.

### Added — audio modes & musical intelligence (Phase A, #64 → issues #1–#3)
- Six new color-mapping modes, bringing the total to 20: `energy_contour`,
  `bass_only_pulse`, `mirror_mode`, `random_walk_hue`,
  `silence_flash_recover`, `crescendo_ramp`.
- `TempoTracker` — BPM estimation by autocorrelation over an onset-strength
  signal, tap-tempo (`POST /api/devices/{id}/audio-reactive/tap-tempo`),
  beat-confidence scoring, an adaptive beat threshold, and three
  sensitivity presets (`POST .../beat-sensitivity`). Tempo state is
  surfaced in the session status payload.
- All eight genre preset bundles (`GET /api/audio/presets`,
  `POST .../apply-preset`) plus custom preset save/apply. Each bundle sets
  mode, sensitivity, hue, band count, dwell and beat-sensitivity together.

### Added — signal quality & test tooling (Phase B, #65 → issues #4, #5, #10)
- `backend/audio_signal.py` — a real `SignalConditioner`: automatic gain
  control with configurable attack/release, a noise gate, clip/overload
  detection, DC-offset removal, independent per-band gain, and a
  calibrate-from-silence flow with per-device-key persisted calibration
  (`POST /api/audio/calibrate`).
- Full N-band spectrum exposure in the session `/status` payload, a rolling
  latency window on `BulbSender`, and beat-flash / BPM data hooks — the
  data layer a live visualizer needs.
- `backend/tests/audio_fixtures.py` — a reusable synthetic-audio harness,
  plus golden-value regression tests that lock every mode's exact
  hue/brightness output against a fixed multi-tone input, fuzz tests, and a
  latency-measurement harness.

### Added — orchestration, per-bulb config & zones (Phase C, #66 → issues #6, #7, #11)
- Two new group role modes — `wave` and `mirror` — alongside the existing
  unison / phase-offset / band-split.
- Per-bulb hue-offset, brightness-scale and band-assignment overrides
  within a group session.
- Failover handling for unreachable bulbs (`consecutive_failures` tracking
  with a threshold before a bulb is marked offline) so one dead bulb no
  longer degrades the whole group.
- Orchestration presets — full CRUD, so a multi-bulb arrangement can be
  saved and reapplied.
- A **Zone** data model sitting above groups: CRUD plus
  `zone_resolved_device_ids`, which resolves direct device membership and
  group membership into one deduped list.
- Per-bulb configuration enforced down in `bulb_manager.py`'s `set_hsv` /
  `set_brightness` — hue-calibration offset, max-brightness cap, and an
  audio-reactive-eligible flag that filters bulbs out of group audio
  sessions. Enforcement is at the device layer, not advisory in the API, so
  a capped bulb stays capped regardless of which route drives it.
- Per-device-index audio-input sensitivity calibration (saved and
  auto-applied on session start unless explicitly overridden), an
  audio-input health-check endpoint, and `validate_device_index()` so a bad
  index fails with a clear 400 instead of deep inside the capture thread.

### Added — sessions, reliability, safety & accessibility (Phase D, #67 → issues #8, #9, #12, #13, #14)
- Session-conflict detection — a solo session and a group session competing
  for the same bulb is now caught, with a `force` override that stops the
  loser instead of letting two senders fight over one device.
- Session lifecycle controls: max-duration auto-stop, a warm-up ramp,
  auto-pause on a manual command with auto-resume after a grace period, and
  one-click resume-last-session.
- Named session presets (`backend/audio_presets.py`) — snapshots of a full
  session config, saved and reapplied.
- Schedule-engine support for scheduled audio sessions via a new
  `audio_reactive_preset` action type.
- Reliability hardening: an explicit socket timeout on audio sends, a
  watchdog thread that restarts a stalled `BulbSender`, device fallback
  when the configured audio input disappears, graceful restart on stream
  errors, config validation (`validate_start_config` / `AudioConfigError`),
  and sliding-window rate limiting on the start/stop endpoints.
- **Photosensitive-epilepsy safety** (`backend/audio_safety.py`) — a hard
  flash-rate cap citing WCAG 2.3.1 "Three Flashes or Below Threshold" and
  ITU-R BT.1702, enforced in the send path rather than left to the caller;
  per-mode `flash_heavy` metadata via `GET /api/audio/modes/info`; a
  one-click disable-flash-heavy toggle; a reduced-motion profile; and a
  configurable max-brightness-swing.
- Lightshow capture / export / replay (`backend/audio_lightshow.py`) —
  records every (timestamp, hue, saturation, brightness) action a
  `BulbSender` actually sends, exports it as JSON, and replays it later
  with no live audio.
- Applause/cheer detection and silence-triggered auto-off.

### Fixed
- A mode-validation check on the solo audio-reactive start route was
  silently dropped while hand-merging the four phases — the group route
  kept its check, the solo route didn't. Restored.
- The new rate limiter's state is module-level and leaked across tests in
  the same run, so unrelated tests started failing with 429s. Now reset by
  an autouse fixture.
- Three test mocks returned a bare `object()` from a mocked `start_session`,
  which broke once the merged route began calling `.confirmation()` on the
  returned session.
- A genuine naming collision: Phase A's `audio_presets()` route handler
  shadowed the `audio_presets` module that Phase D's session-preset routes
  import. Renamed the handler to `list_audio_genre_presets()`.
- Four subagent worktree directories were accidentally committed as
  embedded git repositories (gitlink entries) by the merge's `git add -A`.
  Untracked, and `.claude/worktrees/` is now gitignored.

#### Fixed — found by live testing, not by the suite
The backend suite mocks the Tuya device layer, so none of the following
were reachable by it. All three surfaced from testing against a real
physical bulb that had gone unreachable on the LAN:

- **An unreachable device hung any `/status` call for minutes.** Timed
  directly against the real device: **3m26s** before tinytuya gave up,
  because nothing bounded its connection timeout (default 5s) or retry
  limit (default 5 — and each retry cost noticeably more than the timeout
  in practice, so it isn't a clean `timeout × retries` bound). Capped at
  2s / 1 retry in `bulb_manager.py`; the same real device now fails in
  **~2s**.
- **The status badge got stuck reading "connecting…" forever.**
  `renderStatusText()` returned early on `!state.lastStatus`, and
  `lastStatus` was only ever set on a *successful* poll — so if a device
  never came back, the badge's initial placeholder never updated no matter
  how many times it polled. Fixed with an explicit `hasPolledOnce` flag
  that's set regardless of poll outcome.
- **The badge and the Control panel showed contradictory labels** —
  "LIVE DATA · OFF" versus "OFFLINE" for the same device — once the fix
  above let an `{online: false}` object populate `lastStatus`, at which
  point the badge read `.power` off it instead of checking `.online`. Fixed
  by checking `.online === false` explicitly.

### Known gaps in this round (deliberately not claimed as done)
- **No frontend visualizer.** Phase B shipped the spectrum/beat/latency
  *data*, but nothing renders it yet (issue #5).
- **Genre presets are reasoned, not tuned by ear.** The bulb has been
  offline for every build session so far, so nobody has actually heard
  "jazz" vs "metal" on real hardware (issue #3).
- **Two unreconciled preset systems** ship together: genre bundles (Phase A)
  and session-config snapshots (Phase D). Both real, both useful,
  overlapping in name only — worth a follow-up decision.
- **Two unreconciled calibration systems**: per-device-*key* signal
  conditioning (Phase B) and per-device-*index* sensitivity (Phase C).
- **Most of the music-player-adjacent section is not built** (issue #12) —
  scope was deliberately cut to just lightshow capture/replay rather than
  taking on media-player integration.
- An offline device still takes ~9–13s for the page to fully settle, since
  the status badge and the Control panel each make their own independent
  (now ~2s-bounded) call. A shared in-flight cache would fix it; not built.

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
