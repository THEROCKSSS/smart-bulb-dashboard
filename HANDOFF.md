# Handoff

## What this is

A local dashboard + REST API for controlling a Tuya-based smart bulb
(Bytech A19 Wi-Fi RGB+CCT, protocol v3.5) with zero cloud dependency for
day-to-day use. Built 2026-07-27 in one session, end to end: from getting
local control working on a bulb with no existing credentials, through a
97-feature dashboard, to a pushed Forgejo repo.

## How local control was actually obtained (worth knowing if it breaks)

This took several real attempts — recorded here so nobody re-derives it:

1. **Network scan** (`tinytuya scan`) found the bulb immediately: device ID
   `REPLACE_WITH_TUYA_DEVICE_ID`, protocol v3.5, IP `192.168.1.100`. No local key
   from this — Tuya's local key is never broadcast.
2. **mitmproxy + re-pairing attempt** — set up mitmproxy on this PC, put the
   phone's Wi-Fi proxy through it, installed the mitm CA cert on iOS
   (confirmed working — verified Safari traffic to support.apple.com was
   readable in plaintext), factory-reset the bulb, re-paired it through the
   Tuya Smart app while capturing. **This failed**: across the entire
   capture, only 3 single-hit Tuya branding/deep-link domains appeared — no
   actual pairing API traffic. Root cause is almost certainly the Tuya app
   using HTTP/3 (QUIC over UDP) for its real API calls, which slips past a
   standard HTTP CONNECT proxy entirely (the OS proxy setting only redirects
   TCP). No data was lost — the bulb was already re-paired and working via
   the app throughout.
3. **Tuya IoT Platform (iot.tuya.com) attempt** — user already had a
   developer account, but the QR-code "Link Tuya App Account" step scanned
   successfully but never showed a confirmation popup on the phone. This is
   a known Tuya console bug, almost always caused by a data-center mismatch
   between the Cloud Project's region and the app account's registered
   region, or scanning while logged in as a shared "family member" account
   rather than the actual device owner.
4. **What actually worked**: the `tuya-device-sharing-sdk` (`tuya_sharing`
   on PyPI) — the same official QR-login flow Home Assistant's built-in
   "Tuya" integration uses. It needs only the app's **User Code** (Me →
   Settings → Account and Security) and scans with the app's own in-app
   scanner — no Tuya IoT Platform project required at all. This is
   documented step by step in `SETUP.md` and `.claude/skills/bulb-dashboard-setup/`.
   `local_key` obtained: present in `backend/config.json` (git-ignored, not
   in this repo).

## Bugs found and fixed during this build (via real testing, not review)

1. **Brightness silently flipped the bulb to white mode.** The device's
   `dp22` (`bright_value`) only applies in white mode; colour-mode
   brightness lives in the V component of `dp24`'s HSV colour data. The
   first `set_brightness()` implementation always wrote `dp22`, so dimming
   a colored bulb would silently switch it to white. Found by actually
   calling the API and watching the mode flip in the response. Fixed in
   `bulb_manager.py`'s `set_brightness()` — now mode-aware.
2. **`/status` returned partial/null fields after a color-only command.**
   This device (and likely others) sometimes replies to a status query with
   only the `dps` keys that just changed, not a full snapshot — so a status
   call right after `set_colour()` showed `power: null, mode: null` even
   though the bulb was clearly on. Fixed by caching and merging deltas into
   a `_last_dps` dict per controller instead of trusting each response in
   isolation.
3. **Status badge flashed "OFFLINE" on every page load** for the first
   ~3-4 seconds, even when the bulb was reachable — a single transient
   status-poll miss (real Wi-Fi flakiness on cheap IoT hardware, verified
   this bulb genuinely does drop off Wi-Fi occasionally during this same
   session) was enough to show a false offline state. Fixed by requiring 2
   consecutive failed polls before displaying OFFLINE, plus an extra quick
   re-poll ~1.2s after initial load.

## Verification performed (real, not claimed)

- Direct API curl tests against the real bulb: status, power, brightness,
  RGB/HSV color, scenes, presets, favorites, diagnostics — all confirmed
  against actual device state changes (hue/saturation/value read back
  matched what was sent).
- Playwright headless-Chromium test: loaded the dashboard, clicked through
  all 9 tabs, zero console/page errors.
- Playwright **interactive** test: clicked a preset swatch in the real UI
  and confirmed the bulb's hue changed to the expected value via a separate
  curl status check; started/stopped the "pulse" effect via UI click and
  confirmed the backend's effect-state matched; clicked the power toggle
  and confirmed the bulb's power state actually flipped.
- Visual screenshots of Control/Scenes/Presets panels reviewed (copies in
  `docs/screenshots/`).

## Known-fragile / watch for

- The bulb genuinely drops off Wi-Fi periodically (observed twice in this
  same session, unprompted) — this is bulb/router behavior, not a dashboard
  bug. The Diagnostics tab's connection test and the debounced offline
  badge exist specifically to handle this gracefully.
- `docker-compose.yml`'s `network_mode: host` only works on Linux Docker
  hosts. Noted in `SETUP.md`.
- Tuya's cloud-assisted login access token expires in a few hours
  (`expire_time: 7200` seconds seen in this session) — irrelevant to normal
  dashboard operation (that's all local-only after setup), only matters if
  re-running the setup flow to add more bulbs later.

## What's NOT built (intentionally)

- Bluetooth bulb support — roadmap only, no BLE hardware to test against yet.
- A second physical bulb — architecture (config list + groups) already
  supports it; just needs another bulb purchased and its credentials
  pulled via the same cloud-assisted login flow.
- Ears-on tuning of the audio-reactive modes against real music with the
  bulb online (it was offline for this entire build) — see
  `docs/music-reactive-lighting.md`'s "Known limitations" section.

## Round 2 — audio-reactive lighting + network auto-discovery

Added per user request, after the initial 97-feature prototype above:
**8 audio-reactive lighting modes** (`backend/audio_reactive.py`, the
**Audio Reactive** tab) and **network auto-discovery** with a weekly
scheduler + manual "Scan Now" (`backend/discovery.py`, Settings tab).
Brings the total to 121 working features — see `FEATURES.md`.

Full process detail — what was tried, what broke, what fixed it — now lives
in `iterations/` (see `iterations/README.md` for the convention). Short
version of the two real bugs found this round, both only findable by
actually running the code against real conditions:

1. **Audio callback froze when the bulb was offline.** Bulb commands were
   originally called directly from the sounddevice audio callback; tinytuya's
   blocking socket calls stalled the entire capture pipeline for as long as
   the bulb took to time out. Confirmed via 4 consecutive identical
   `/audio-reactive/status` polls a second apart (frozen) with the bulb
   independently confirmed offline. Fixed by moving all bulb I/O onto a
   separate sender thread that always acts on the latest queued value —
   the audio callback never blocks now. Full writeup:
   `iterations/002-audio-reactive-lighting/`.
2. **IP-change detection logged the wrong "old IP".** In
   `discovery.py`'s dedup logic, the config was mutated before the old
   value was read for the log entry, so a device whose IP changed would
   report `old_ip` equal to the *new* IP. Found via a mocked-scan test (the
   real LAN's one bulb happened to be offline during testing, so this path
   couldn't be exercised against real hardware — see the same iteration
   note for why). Fixed by capturing `old_ip` before mutating.
   Full writeup: `iterations/001-network-auto-discovery/`.

Both bulb offline observations above (Round 1 and Round 2) are the same
recurring, documented hardware behavior — this bulb genuinely drops off
Wi-Fi periodically. It happened to be offline for this entire second
session, which is *why* two of this round's verifications used synthetic
tones / mocked scans instead of the physical device, and why "does the
audio-reactive mode look good" is explicitly left as a follow-up rather
than claimed done.

## Round 3 — audio engine v2 + PIN-gated remote access

Reworked the audio pipeline for lower latency and more modes, added
multi-bulb orchestration, and added a PIN gate for exposing the dashboard
beyond the LAN. Now 137 working features total (`FEATURES.md`). Bulb was
still offline this entire round too — same recurring Wi-Fi behavior as
Rounds 1 and 2, not a new issue.

**Audio v2** (`backend/audio_reactive.py`, rewritten; full detail in
`iterations/003-audio-engine-v2/`):
- Capture block size 1024→512 samples (~23ms→~11.6ms), zero-padded to a
  4096-point FFT. Removed the old artificial analysis-rate gate entirely —
  every callback now computes and queues a fresh target.
- New `BulbSender` class per bulb: enforces a configurable `min_dwell_ms`
  (how long a color stays visible) completely independent of decision
  latency, always sending the freshest queued value.
- 4 new modes (12 total): `spectrum_gradient`, `band_flash_overlay`,
  `stereo_split`, `breathing_silence`.
- `GroupAudioSession`: one shared capture analysis driving multiple bulbs
  via `unison`/`phase_offset`/`band_split` roles, each bulb still getting
  its own independent sender.
- **Real bug caught in review, not testing**: every mode's hue smoothing
  used a plain linear blend, which breaks at the 0°/360° wrap boundary
  (harmless for the original 3 modes' anchors, but `stereo_split`'s target
  genuinely crosses it). Fixed with a proper circular-mean blend
  (`_smooth_hue()`), applied everywhere for consistency.

**PIN-gate remote auth** (`backend/remote_auth.py`, new; full detail in
`iterations/004-pin-gate-remote-auth/`):
- PBKDF2-SHA256-hashed PIN (never plaintext), stateless HMAC-signed
  session tokens, per-IP brute-force lockout (5 attempts / 5 minutes).
- **Real bug found via a live Playwright test**: the root page `/` itself
  was gated, so enabling the PIN feature meant the browser got a raw 401
  instead of the HTML page containing the PIN form — a real
  lock-yourself-out bug, not hypothetical. Fixed by adding `/` to the
  always-open path list; only the API underneath stays gated.
- Verified end-to-end for real: enable → blocked without a session →
  wrong PIN rejected → 5 failures triggers a lockout that blocks even the
  correct PIN → correct PIN (once unlocked) issues a working session →
  session token expiry enforced server-side (tested with a 10s TTL) →
  disable restores open access. Full real-browser login flow also
  confirmed via Playwright, zero console errors.
- `docs/remote-access-security.md` covers Tailscale (recommended) vs.
  DuckDNS+port-forward (requires this PIN gate, still plaintext HTTP —
  documented, not yet solved with TLS). A dedicated adversarial pentest
  phase (a separate agent actually attacking a live exposed instance) is
  intentionally scoped as future roadmap work, not squeezed into this
  build — see `roadmap/`.

## Round 4 — parallel-phase build, Roadmap One QoL, mobile fix, Tailscale, docs (2026-07-29)

### Current state

`master` at commit `2123262`, working tree clean, fully pushed to GitHub
(`THEROCKSSS/smart-bulb-dashboard`). `APP_VERSION = "0.3.0"`. **159 working
features** (`FEATURES.md`). 76/76 tests pass (`backend/tests/` + `cli/tests/`).
A real backend server is running locally right now: `127.0.0.1:8502`
(started via `backend/venv/Scripts/python.exe -m uvicorn main:app --host
127.0.0.1 --port 8502` from inside `backend/`), plus reachable over the
tailnet at `https://owens-pc-vpn.tailff2683.ts.net:8502` via `tailscale
serve` (tailnet-only, not Funnel). **The PIN gate is currently enabled** —
see Credentials below.

### What was done this session

1. **Four roadmap phases, built in parallel** (one subagent each, isolated
   git worktrees, hub-verified, hand-merged where diffs overlapped):
   audio modes (`harmonic_pairs`, `kick_snare_split` — a real flicker bug
   found and fixed, see `docs/music-reactive-lighting.md`), PIN-gate
   hardening (session listing/revocation, an audit log that never records
   the PIN, per-IP rate limiting independent of lockout), `cli/bulbctl.py`
   (stdlib-only REST client with shell completions), and a real
   `backend/tests/`/`cli/tests/` pytest suite (76 tests) plus
   `GET /api/analytics/usage` (real per-device on-time, no fabricated
   wattage).
2. **Roadmap One** — a QoL round, also built via parallel subagents: live-
   ticking sleep/wake timer countdowns and status badge (no more refresh-
   to-see), a visual polish pass (8px→12px radius, real card shadows,
   smoother hover states), and power-user niceties (remembered
   device/panel via `localStorage`, keyboard shortcuts on Control, copy-
   to-clipboard on Diagnostics, an Undo action on cancel-timer toasts that
   actually recreates the timer).
3. **Mobile-friendliness fix, shipped as PR #63** (merged): found and
   fixed a real bug — `.topbar`'s desktop `grid-column: 1 / 3` was never
   reset in the mobile `@media` block, so the sidebar was collapsing to a
   ~16px sliver on phones. Also added 44px minimum tap targets and a
   responsive PIN-gate screen. Verified live at 375/414/800/1400px.
4. **Tailscale Serve** set up and verified end-to-end as the actual
   off-LAN access path (tailnet-only HTTPS, real cert, no port
   forwarding), with the PIN gate enabled alongside it — 21/21 live
   security checks passed (root path stays open, protected routes 401
   without a session, lockout/rate-limiter/session-revocation all work,
   audit log never leaks the PIN).
5. **Git identity / attribution cleanup**: rewrote the 6 commits that had
   been authored as `agentsoul` with `Co-Authored-By`/session-link
   trailers — stripped the trailers, reauthored as
   `THEROCKSSS <193167949+THEROCKSSS@users.noreply.github.com>` (repo-local
   git config, not global), force-pushed. **Caught and fixed a mistake
   made mid-cleanup**: the first force-push accidentally clobbered 3 real
   `roadmap-status-bot` sync commits on `master` (local `master` had
   quietly diverged from remote before this round started) — rebuilt
   master properly by resetting to right after the CI-fix commit and
   replaying the bot's 3 real commits on top with their actual authorship
   intact, then force-pushed the corrected version. Verified the bot
   commits' content matched exactly before re-pushing.
6. **Documentation pass to v0.3.0**: `CHANGELOG.md`/`docs/changelog.html`
   (new v0.3.0 entry), `FEATURES.md` (22 new itemized entries, 138–159),
   `README.md`, `AGENTS.md`, `docs/index.html`, `docs/features.html`,
   `docs/api.html` (analytics + session-management + `bulbctl` sections),
   `docs/audio.html` (14 modes), `docs/security.html` +
   `docs/remote-access-security.md` (session/audit/rate-limit hardening,
   Tailscale Serve now exercised not just recommended). Marked 3 real
   Week 3/4 roadmap issues (`#44` energy/usage analytics, `#35` CLI tool,
   `#51` developer experience/tests) `in-progress` with honest partial-
   progress notes, since this round's work genuinely advances them without
   completing their full W-item ranges. Closed `#59`–`#61` (Roadmap One
   tracking issues, now merged). Re-ran `.github/scripts/sync_roadmap_status.py`
   so the live Active Roadmap page reflects this immediately.
7. **Real bug found and fixed while re-verifying**: enabling the PIN gate
   for real (step 4) broke 16 previously-passing tests, because most of
   `backend/tests/`'s shared `client` fixture didn't isolate
   `remote_auth`'s on-disk state and was reading the real, now-enabled
   `backend/data/remote_auth.json`. Fixed by making the fixture depend on
   the existing (previously opt-in) `auth_reset` fixture. 76/76 pass again.

### What's NOT done (the gap)

- The adversarial security-test phase is still not done — this round's
  21-check security pass was a same-machine verification (the same
  session that built the feature also wrote the checks), not an
  independent attacker's attempt against a real deployed instance. See
  `docs/remote-access-security.md`'s "Still planned" section.
- The Undo action's wake-timer path was code-reviewed and its backend
  payload verified correct, but not independently clicked through in a
  real browser (only the sleep-timer undo path was UI-driven end-to-end).
- The PIN gate is currently **enabled** on the locally-running server
  (was disabled before this session touched it) — intentional for the
  Tailscale exposure, but worth knowing before assuming the dashboard is
  open like it used to be.

### How to resume

```bash
cd "C:\Users\User\Documents\Hermes stuff\hermes workspace\projects\smart-bulb-dashboard"
git status --short            # should be clean, on master, at or after 2123262
backend\venv\Scripts\python.exe -m pytest backend\tests\ cli\tests\ -q   # expect 76 passed

# Bring the dashboard up locally:
cd backend
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8502
# Then: http://127.0.0.1:8502 (PIN gate enabled — see Credentials)

# Re-expose it over Tailscale if the serve mapping isn't still active:
tailscale serve status                                    # check current mappings
tailscale serve --bg --https=8502 http://127.0.0.1:8502    # re-create if missing
```

### Credentials / config

- **Local server PIN**: `143490` (set this session for the Tailscale
  exposure — rotate it if this handoff is ever shared beyond this
  machine's owner). Session TTL: 30 days once logged in.
- **Tailscale URL**: `https://owens-pc-vpn.tailff2683.ts.net:8502`
  (tailnet-only — reachable from any device signed into the same
  Tailscale account, not the public internet).
- `backend/config.json` (real device credentials) remains git-ignored,
  never committed.
- Git identity for this repo (local, not global):
  `user.name = THEROCKSSS`, `user.email =
  193167949+THEROCKSSS@users.noreply.github.com`. Do not add
  `Co-Authored-By`/session-link trailers to commits, PR bodies, or issue
  comments unless explicitly asked — this was an explicit standing
  instruction from this session.

## Round 5 — Week 1 roadmap, built via 4 parallel phases (open as PR #68, not yet merged)

### Current state

Working on branch `week1-integration`, pushed to GitHub, open as
[PR #68](https://github.com/THEROCKSSS/smart-bulb-dashboard/pull/68) —
**not merged to `master` yet**, pending Owen's own testing. Tracked in
issues #64–#67 (one per phase). 353/353 backend tests pass. A real backend
server is running locally against this branch's code:
`127.0.0.1:8502` (also reachable over the tailnet at
`https://owens-pc-vpn.tailff2683.ts.net:8502`, PIN `143490`).

### What was done this round

1. **Four roadmap phases, built in parallel** (one subagent each, isolated
   git worktrees, hub-verified via real `git merge` rather than patch
   application since all four phases modified the same core classes):
   - **Phase A** — 6 new audio modes (`energy_contour`, `bass_only_pulse`,
     `mirror_mode`, `random_walk_hue`, `silence_flash_recover`,
     `crescendo_ramp`), a `TempoTracker` (BPM autocorrelation, tap-tempo,
     beat confidence, sensitivity presets), all 8 genre presets.
   - **Phase B** — `SignalConditioner` (AGC, noise gate, clip detection, DC
     removal, per-band gain, calibrate-from-silence), full N-band status
     exposure, a reusable synthetic-audio test harness with golden-value
     regression tests.
   - **Phase C** — `wave`/`mirror` group role modes, per-bulb overrides,
     failover handling, orchestration presets, a Zone data model, and
     per-device-index sensitivity calibration.
   - **Phase D** — session conflict detection, max-duration/warmup/
     auto-pause, socket-timeout + watchdog restart, session presets, a
     photosensitive-safety flash-rate cap (WCAG 2.3.1), lightshow capture/
     replay, scheduled audio sessions.
2. **Merging four phases that all touched the same core classes** required
   real hand-resolved `git merge`s (not patch application) across ~40
   conflict blocks total, always combining both sides' intent rather than
   picking one — full detail is in the git history on `week1-integration`,
   not repeated here.
3. **5 real post-merge test failures, found and fixed** (all previously
   passing in each phase's own isolated worktree, broken only once merged
   together): a mode-validation check silently dropped during the merge,
   a rate-limiter's module-level state leaking across tests, 3 test mocks
   that predated the merged `.confirmation()` contract, plus a genuine
   naming collision where Phase A's `audio_presets()` route handler
   shadowed the `audio_presets` module Phase D's routes needed to import.
4. **3 more real bugs found by actually testing this live** against a real
   physical bulb (that turned out to be unreachable on the network at
   the time) — see the "Bugs found via live testing" note below.
5. **Git hygiene fix**: the merge commit's `git add -A` had accidentally
   committed the 4 subagent worktree directories as embedded git repos
   (gitlink entries) — untracked them and added `.claude/worktrees/` to
   `.gitignore`.

### Bugs found via live testing (not caught by the test suite — it mocks the device layer)

Testing this over Tailscale surfaced a real physical bulb that had gone
unreachable on the LAN, which exposed three genuine bugs:

1. **An unreachable device hung `/status` for minutes.** Timed directly
   against the real bulb: **3m26s** before tinytuya gave up, because
   nothing bounded its socket timeout (default 5s) or retry limit
   (default 5, and each retry cost noticeably more than that in
   practice — not a clean `timeout × retries` relationship). Fixed in
   `bulb_manager.py`'s `_get_device()`: capped at a 2s timeout / 1 retry.
   Same real device now fails in **~2s**.
2. **The status badge got stuck reading "connecting…" forever** once a
   poll never succeeded even once. `renderStatusText()` returned early
   on `!state.lastStatus`, and `lastStatus` only ever got set on a
   *successful* poll — so the badge's placeholder text never updated, no
   matter how many times it polled. Fixed with an explicit
   `state.hasPolledOnce` flag that's set regardless of outcome.
3. **The badge and the Control panel briefly showed contradictory
   labels** ("LIVE DATA · OFF" vs "OFFLINE" for the same device) once fix
   #2 let `renderControl()`'s existing unconditional status-caching
   populate `lastStatus` with an `{online: false}` object — the badge
   logic then read `.power` off it instead of checking `.online`
   explicitly. Fixed by checking `.online === false` directly.

Verified all three with real timing tests and Playwright screenshots
against the actual unreachable device, not just unit tests: full backend
suite stayed green (353/353) throughout.

### What's NOT done (the gap)

- Owen has not yet tested/approved this round — do not start Week 2
  until he has.
- A short shared cache/de-dupe layer for concurrent `/status` polls to
  the same device was flagged as a possible follow-up (an offline device
  still takes ~9-13s to fully settle across the badge + panel's
  independent poll calls, each bound by the new ~2s timeout) — not built
  yet, pending Owen's input on whether it's worth it.
- PR #68 has not been merged to `master`.

## Week 2 Phase B — TLS / reverse proxy + deployment (issue #70)

Roadmap sections 3 (W2-031..050) and 10 (W2-176..195). Branched from
`master`; test suite went 353 → 395 passing.

### Backend changes

New module **`backend/reverse_proxy.py`** — everything about running behind
a TLS-terminating proxy. Configured by env var only (`SBD_TRUSTED_PROXIES`,
`SBD_HSTS*`, `SBD_HTTPS_REDIRECT`), deliberately *not* through the API: a
runtime-flippable trust setting would let one session that got in once
permanently disable brute-force protection for everyone.

- **W2-038, the important one.** `remote_auth`'s per-IP lockout and login
  rate limiter now key off the real client behind a proxy instead of the
  proxy's own address. Opt-in and default-off: `X-Forwarded-For` is
  attacker input unless a specific peer has been named, and believing it
  unconditionally would let anyone forge a fresh source IP per guess and
  delete the lockout. The chain is walked **right to left**, because both
  nginx (`$proxy_add_x_forwarded_for`) and forwarding proxies generally
  append the real peer to whatever the client sent — reading left-to-right
  is the classic bug and hands the attacker their forged value.
- **W2-036.** Session cookie gets `Secure` when the request really is
  HTTPS, and *not* otherwise. Unconditional would be worse than absent:
  browsers silently discard a `Secure` cookie over plain HTTP, so every LAN
  user's login would appear to do nothing.
- **W2-034/035.** HSTS and 307 redirect-to-HTTPS, both opt-in, both off by
  default (reasoning in `docs/deployment.md` §3.3). Health paths are exempt
  from the redirect so container probes don't 307.
- **W2-044.** `/healthz`, added to `remote_auth.OPEN_PATHS`. Returns
  `{"status":"ok"}` and nothing else — it's the endpoint most likely to be
  publicly reachable.
- `/api/system/proxy-status` (gated) — makes a proxy misconfiguration
  visible instead of silent.

### The pre-existing bug this turned up

**uvicorn ships `ProxyHeadersMiddleware` enabled by default, trusting
`127.0.0.1`.** It rewrites the request's client address from
`X-Forwarded-For` *before the app runs*. With the app bound to loopback
that matches every request, so any local process could hand itself an
arbitrary source IP and a fresh lockout bucket — and this predates Phase B;
the old `request.client.host` in `auth_login` was already reading a
substituted value. Confirmed live: before the fix, a direct `curl` with
`X-Forwarded-For: 198.51.100.42` made the app report that as `peer_ip`.

Fixed by running uvicorn with `--no-proxy-headers` (`Dockerfile`,
`deploy/systemd/`, `deploy/windows-service.md`) so this app's explicit trust
list is the only thing deciding it. `/api/system/proxy-status` reports
`peer_rewritten_by_server` to catch a start command that missed the flag.
**Anyone running their own start command needs to add it.**

### Reference artifacts (`deploy/`)

Caddy (DuckDNS + automatic Let's Encrypt, and a LAN `tls internal`
variant), nginx + certbot renewal automation, a self-signed cert
generator, `docker-compose.caddy.yml`, systemd service + health-check
timer, NSSM notes, and `deploy/smoke-test.py`. `docs/deployment.md` covers
min Python/OS versions, measured resource footprint, update/rollback, and
version pinning.

### What was actually validated vs. only written

Validated with real binaries: Caddyfiles via `caddy validate` (v2.11.4),
nginx conf via `nginx -t` (1.27) with real certs in place, systemd units
via `systemd-analyze verify` (Debian 12), compose via `docker compose
config`, the cert script executed on Linux, and the smoke test run against
a live instance — including **through a real Caddy container** proxying to
it, confirming a forged header is ignored without trust and the real client
IP comes through with it.

**Not validated:** no Let's Encrypt cert issued against a real domain (needs
a real domain + open ports), no systemd unit started on a real Linux host,
NSSM instructions not executed, and the Raspberry Pi CPU guidance is
extrapolated from desktop measurements. `deploy/README.md` carries this
same split so it doesn't get lost.

### Bugs found while building this

1. **`SBD_TRUSTED_PROXIES=*` was silently inert** — the wildcard made every
   forwarded entry look like a proxy hop to skip, so the right-to-left walk
   fell off the end and returned the peer, collapsing `*` into the no-trust
   behaviour it was set to escape. Caught by its own test. Fixed by
   splitting "may I believe this peer" from "is this entry one of my own
   proxies" (`is_trusted_proxy` vs `is_known_proxy_hop`).
2. **`StartLimitIntervalSec` was in `[Service]`** in the systemd unit, where
   systemd has silently ignored it since v229 — the crash-loop guard looked
   present and did nothing. Caught by `systemd-analyze verify`.
3. **The smoke test looked up response headers case-sensitively**, so it
   reported the app wasn't setting `Content-Type` on static assets. The app
   was fine; the script wasn't.

### What a reviewer should check by hand

- A real Let's Encrypt issuance on an actual DuckDNS domain with ports
  80/443 forwarded — the one thing no amount of local validation covers.
- Installing the systemd unit on a real Linux host, including whether the
  `ProtectSystem=strict` + `ReadWritePaths` set is actually sufficient for
  `backend/data/` and tinytuya's `snapshot.json`.
- Whether audio-reactive lighting survives the systemd sandboxing on a real
  machine (the unit documents the `PrivateDevices` / `SupplementaryGroups=audio`
  caveats, but they weren't exercised).

## Repo

Pushed to Forgejo: `agentsoul/smart-bulb-dashboard` (see commit log for
history). `backend/config.json` (real device credentials) is git-ignored
and was never committed — `config.example.json` is the template that ships
instead.

Also mirrored to **GitHub** (`THEROCKSSS/smart-bulb-dashboard`, public) under
the **Polyform Noncommercial License 1.0.0** — personal/contribution use is
free; commercial use requires a separate license from the author. A real
bulb `device_id` and LAN IP that were briefly committed here were scrubbed
before the GitHub push (no `local_key` was ever committed).
