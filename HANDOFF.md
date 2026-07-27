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

- Music-reactive lighting — design doc only, see `docs/music-reactive-lighting.md`
  and `ROADMAP.md`. Needs real audio hardware iteration to tune well.
- Bluetooth bulb support — roadmap only, no BLE hardware to test against yet.
- A second physical bulb — architecture (config list + groups) already
  supports it; just needs another bulb purchased and its credentials
  pulled via the same cloud-assisted login flow.

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
