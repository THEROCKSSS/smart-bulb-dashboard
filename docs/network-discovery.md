# Network Auto-Discovery

Automatically finds Tuya devices on this LAN that aren't yet in
`config.json`, either on a schedule (default weekly) or on demand.
Implemented in `backend/discovery.py`, tested in
`iterations/001-network-auto-discovery/`.

## How it works

1. A scan (`tinytuya.deviceScan()` — the same UDP broadcast mechanism used
   by `tinytuya scan` on the command line and by this project's existing
   `/rescan` endpoint) runs either on a schedule or when you click **Scan
   Now** in Settings.
2. Every device found is classified:
   - **Already configured** (its device ID matches an entry in
     `config.json`) → skipped, unless its IP changed (e.g. a DHCP lease
     renewal), in which case `config.json` is updated automatically and the
     change is reported in the scan result.
   - **Previously ignored** → skipped silently.
   - **Everything else** → added to the "discovered" list shown in
     Settings.
3. Discovered devices show their device ID, IP, and best-guess protocol
   version. Clicking **Add** pre-fills the "Add a Device" form above (you
   still need to obtain the `local_key` separately — see `SETUP.md` — since
   Tuya never broadcasts it). Clicking **Ignore** removes it from the list
   permanently until you click **Unignore**.

## Scope and safety

This only listens for UDP broadcasts on your local subnet (ports 6666,
6667, 7000) — nothing is sent to the internet, and no cloud account is
involved. It's the same mechanism the Tuya Smart app itself uses to find
devices during setup.

## Scheduling

- Default interval: weekly (`168` hours). Changeable in Settings
  (Daily/Weekly/Monthly) — stored in `backend/data/discovery.json` as
  `interval_hours`.
- A background thread checks every 5 minutes whether the interval has
  elapsed since the last scan, rather than sleeping for the whole interval —
  so changing the interval takes effect within minutes, not after waiting
  out whatever the old interval was.
- The first scan after a fresh install runs almost immediately on startup
  (since there's no `last_scan` yet to compare against), establishing a
  baseline discovered-device list without waiting a full week.
- A manual **Scan Now** and the scheduled scan share a lock — they can't run
  concurrently against the same network.

## API

```bash
# Current state: last scan time, interval, discovered/ignored lists
curl -s http://localhost:8500/api/system/discovery

# Trigger a scan immediately
curl -s -X POST http://localhost:8500/api/system/scan

# Change the auto-scan interval (hours)
curl -s -X POST http://localhost:8500/api/system/discovery/interval \
  -H "Content-Type: application/json" -d '{"hours": 24}'

# Ignore / unignore a discovered device
curl -s -X POST http://localhost:8500/api/system/discovery/<device_id>/ignore
curl -s -X POST http://localhost:8500/api/system/discovery/<device_id>/unignore

# Remove a device from the discovered list without permanently ignoring it
# (it can reappear on the next scan)
curl -s -X DELETE http://localhost:8500/api/system/discovery/<device_id>
```

## Known-fragile / watch for

- If the physical bulb (or any Tuya device) is off Wi-Fi at scan time, it
  simply won't appear — confirmed during testing that a genuinely offline
  device produces `scanned_count: 0` correctly, rather than a false
  positive or an error. This project's one bulb is documented elsewhere
  (`HANDOFF.md`) as dropping off Wi-Fi periodically — that's the device,
  not the scanner.
- The classification/dedup logic (known vs. ignored vs. new, and the
  IP-change-detection path) was verified with a mocked scan result during
  testing, since the real LAN only has one bulb and it was offline at the
  time — see `iterations/001-network-auto-discovery/` for exactly what was
  tested and a bug that testing caught (an IP-change log entry that showed
  the wrong "old IP" before the fix).
