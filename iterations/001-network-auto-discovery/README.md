# 001 — Network Auto-Discovery

## Goal
Add a system that automatically scans the LAN for Tuya devices not yet in
`config.json`, on a configurable schedule (default weekly) or on demand via
a "Scan Now" button in Settings, per the user's explicit request.

## Approach
- `backend/discovery.py`: wraps the same `tinytuya.deviceScan()` call
  already used by the existing `/api/devices/{id}/rescan` endpoint, reusing
  a proven pattern rather than inventing a new scan mechanism.
- Persists state to `backend/data/discovery.json`: `last_scan`,
  `interval_hours`, `discovered` (unconfigured devices seen on the LAN), and
  `ignored` (device IDs the user dismissed).
- Classification on every scan: devices already in `config.json` are
  skipped (unless their IP moved, in which case `config.json` is
  auto-updated); ignored device IDs are skipped; everything else is
  new/discovered.
- A background daemon thread checks every 5 minutes whether
  `interval_hours` has elapsed since `last_scan` and runs a scan if so —
  chosen over a single long `sleep(interval)` so that changing the interval
  in Settings takes effect within minutes instead of waiting out whatever
  the old interval was.
- A non-blocking lock (`_scan_lock`) prevents the scheduled scan and a
  manual "Scan Now" click from running concurrently against the same
  network.
- New endpoints in `main.py`: `GET/POST /api/system/discovery*`, `POST
  /api/system/scan`. New "Network Discovery" card in the Settings tab:
  Scan Now button, interval dropdown, discovered-device table with
  Add/Ignore actions, ignored-device list with Unignore.

## What happened
Wiring and the API worked on the first pass. Testing surfaced one real bug
(below) before this could be called done.

## Failures

**Real LAN scan returned 0 devices during testing.** Running
`POST /api/system/scan` against the actual home network found nothing, and
running raw `python -m tinytuya scan` directly showed the same (0 devices,
0 broadcasts, 18-second scan window). Investigated with `ping
192.168.0.134` (the known bulb's IP) — got "Destination host unreachable."
**Root cause: the physical bulb was off the network at the time**, which
matches the flakiness already documented in `HANDOFF.md` ("the bulb
genuinely drops off Wi-Fi periodically — observed twice in this same
session"). This is real-world IoT hardware behavior, not a bug in
`discovery.py` — the raw tinytuya scan agreeing with the ping test rules out
a code-level cause. Because of this, the "a genuinely new device gets found
on a live scan" path could not be exercised against real hardware this
session (there's only one bulb, and it happened to be offline).

**Found via a mocked-network test instead**: since the real network
couldn't exercise the happy path, `discovery._raw_scan()` was monkeypatched
in a throwaway test script to return fake scan results (a fake "known"
device matching the real configured bulb's ID, plus fake "new" devices),
and the actual classification/dedup/ignore/IP-update logic was run against
that. This caught a real bug: in the IP-change-detection branch, the code
mutated `existing["ip"] = ip` **before** reading `existing.get("ip")` for
the `old_ip` field in the returned `ip_updates` entry — so a device whose IP
changed would report `old_ip` equal to the *new* IP, making the change
invisible in the API response even though `config.json` was correctly
updated underneath.

## Fix
`discovery.py`'s `scan_now()`: capture `old_ip = existing.get("ip")` before
mutating `existing["ip"]`, then use the captured value when building the
`ip_updates` entry. One-line reordering, verified by the same mock test
(see Verification).

## Verification
1. **Real network, real behavior**: `POST /api/system/scan` against the
   actual LAN returned `{"ok": true, "scanned_count": 0, "new_count": 0}` —
   correct given the bulb was independently confirmed offline (ping +ni
   `tinytuya scan` both agree).
2. **Mocked classification logic** (`test_discovery_logic.py`, run via the
   backend's own venv, `config.json`/`discovery.json` backed up before and
   restored after so the real device config was never permanently altered):
   - A known device (matching the real configured bulb's ID) is correctly
     excluded from `discovered`.
   - A new fake device is correctly added, with `new_count: 1`.
   - Re-scanning the same fake device does not double-count it or duplicate
     the entry.
   - Ignoring a device removes it from `discovered` immediately and it does
     not reappear on a subsequent scan that still reports it.
   - A genuinely new device introduced in a later scan is still detected
     correctly alongside an already-ignored one.
   - The IP-change path correctly reports `old_ip` vs `new_ip` after the
     fix (confirmed by re-running the same assertions, which failed before
     the fix and passed after).
   - Unignoring removes a device from the ignored list.
3. Backend restarted after the fix so the running server picks up the
   corrected module (Python doesn't hot-reload without `--reload`), then
   `GET /api/system/discovery` re-checked to confirm the real device state
   (`config.json`, `data/discovery.json`) was intact and unaffected by the
   test run.
