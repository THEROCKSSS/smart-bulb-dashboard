---
name: bulb-dashboard-discovery
description: "Use the Smart Bulb Dashboard's network auto-discovery: scan for new Tuya devices, manage the discovered/ignored lists."
---

# Smart Bulb Dashboard — Network Discovery

## When to use
User wants to find new Tuya devices on the LAN without manually running
`tinytuya scan`, check when the last auto-scan happened, change the scan
interval, or manage devices that showed up but shouldn't be added (e.g. a
smart plug, not a bulb). Assumes the backend is running.

## The procedure

### Check current state
```bash
curl -s http://localhost:8500/api/system/discovery
```
Returns `last_scan`, `interval_hours`, `discovered` (unconfigured devices
seen on the LAN), `ignored` (device IDs dismissed by the user).

### Scan now
```bash
curl -s -X POST http://localhost:8500/api/system/scan
```
Returns `{"ok": true, "scanned_count": N, "new_count": N, "ip_updates": [...]}`.
`scanned_count: 0` legitimately means nothing responded to the UDP
broadcast — if the user expects a device to show up and it doesn't,
check that device is actually powered on and on Wi-Fi before assuming a
discovery bug (Tuya bulbs drop off Wi-Fi periodically; this is real
hardware behavior documented in `HANDOFF.md`).

### Change the auto-scan interval
```bash
curl -s -X POST http://localhost:8500/api/system/discovery/interval \
  -H "Content-Type: application/json" -d '{"hours": 24}'
```
A background thread checks every 5 minutes whether `interval_hours` has
elapsed since `last_scan` — changing this takes effect within minutes, not
after the old interval fully elapses.

### Add a discovered device to the dashboard
Discovered entries only have `device_id`/`ip`/`version` — never `local_key`
(Tuya never broadcasts it). Get the key via `bulb-dashboard-setup`'s Step 2,
then:
```bash
curl -X POST http://localhost:8500/api/devices \
  -H "Content-Type: application/json" \
  -d '{"id":"bulb-2","name":"Bedroom","device_id":"<from discovered>","local_key":"<obtained separately>","ip":"<from discovered>","version":3.3}'
```
The dashboard's Settings UI does this pre-fill automatically when you click
"Add" next to a discovered device row.

### Ignore / unignore a device that isn't a bulb
```bash
curl -s -X POST http://localhost:8500/api/system/discovery/<device_id>/ignore
curl -s -X POST http://localhost:8500/api/system/discovery/<device_id>/unignore
```
Ignored devices are permanently excluded from future scan results until
explicitly unignored.

## Pitfalls

1. **Don't expect a device already in `config.json` to ever show up in
   `discovered`** — it's deliberately excluded (that's the whole point of
   the dedup logic). If its IP changed, it's reported in the scan's
   `ip_updates` field instead, and `config.json` is updated automatically.
2. **Two scans can't run at once** — a manual "Scan Now" while the
   scheduled background scan is mid-run returns
   `{"already_scanning": true}` rather than stacking. Not an error, just
   wait and retry.
3. **A `discovered` entry that never gets "Add"-ed just sits there** — it's
   not re-flagged as new on every subsequent scan (that would be noisy), it
   just persists with an updated `last_seen`. Use `ignore` if it's not
   something the user wants to track at all.

## Verification
```bash
curl -s -X POST http://localhost:8500/api/system/scan
curl -s http://localhost:8500/api/system/discovery
```
Confirm `last_scan` updated to a recent timestamp and (if a genuinely new
Tuya device is on the network) it appears in `discovered` with a plausible
IP/version. The classification logic itself (known vs. ignored vs. new,
IP-change detection) was verified via a mocked scan during development —
see `iterations/001-network-auto-discovery/` for exactly what was tested,
including a real bug (wrong "old IP" in the log) that was caught and fixed.
