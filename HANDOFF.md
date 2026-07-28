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
