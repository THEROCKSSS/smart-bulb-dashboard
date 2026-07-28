# Roadmap

Things intentionally **not** built yet in this prototype, and why.

## Multi-bulb (near-term, needs hardware)

The architecture is already ready for this — `config.json` is a list of
devices, `groups` already broadcast actions to multiple device IDs, and the
Settings tab can add/remove devices without touching code. What's actually
missing is more physical bulbs to add. When you buy more:

1. Get each new bulb's `device_id`/`local_key`/`ip` (SETUP.md Step 2 — once
   you're logged into the cloud-assisted flow, every bulb on the account
   shows up in the same `manager.device_map` loop, so getting keys for
   bulb #2, #3, etc. is nearly free once you've done it once for bulb #1).
2. Add each as an entry in `config.json`, and to the `all` group (or a new
   group, e.g. "Bedroom Bulbs").
3. No backend code changes needed.

Possible future UI work once there are multiple real bulbs: a "room" concept
above groups, synchronized effects across bulbs (e.g. rainbow effect where
each bulb is offset in hue), and a grid/room-map view instead of a dropdown.

## Bluetooth bulbs (future — different hardware class)

Explicitly deferred until there's Bluetooth Tuya/mesh hardware to test
against — this is not just a software feature flag, it's a different
transport and often a different device class (Tuya BLE devices, or BLE mesh
via a hub). Rough shape of what it'd need when the time comes:

- Tuya BLE devices are typically supported via the
  [`tuya_ble`](https://github.com/PlusPlus-ua/ha_tuya_ble/) protocol
  implementation rather than `tinytuya` (which is Wi-Fi/local-network only).
- A BLE-capable host (most desktops/servers don't have great BLE range —
  may want a Raspberry Pi or a BLE-to-Wi-Fi bridge near the bulbs).
- The `BulbController` abstraction in `bulb_manager.py` is already
  transport-agnostic at the API layer (routes call `controller.set_rgb()`
  etc., not tinytuya directly) — a `BLEBulbController` implementing the same
  method surface could plug in alongside the existing Wi-Fi one without
  changing `main.py`'s routes.

## Music-reactive lighting — implemented (v2), tuning is what's left

No longer roadmap-only: this is now the **Audio Reactive** tab, with 12
working interpretation modes and multi-bulb orchestration (unison /
phase-offset / band-split) — see `docs/music-reactive-lighting.md`,
`FEATURES.md`, and `iterations/002` + `iterations/003` for the two build
rounds. v2 also cut internal decision latency to sub-15ms and separated it
from a configurable minimum dwell time. What's still ahead:

- **Ears-on tuning** against real music with the bulb online — the hue
  anchors and beat thresholds are verified *correct in direction* (via
  synthetic tones) but not yet tuned by taste, since the physical bulb was
  offline for both build/test sessions so far.
- **Auto-gain** — sensitivity is currently a manual multiplier; an
  automatic level normalizer for consistently quiet/loud sources isn't
  built.
- **Real multi-bulb testing** — orchestration (unison/phase-offset/
  band-split) is built and tested with fake controllers plus the real API
  against a 1-bulb group; needs a second physical bulb to confirm it looks
  right, not just structurally correct.
- See `roadmap/` for the much larger backlog of audio-specific ideas
  (visualizer styles, per-genre presets, tap-tempo, etc.) slated across the
  month-long plan.

## Network auto-discovery — implemented

Weekly (configurable) background scanning plus an on-demand "Scan Now" in
Settings, covered in `docs/network-discovery.md`. The classification logic
(known/ignored/new device, IP-change detection) was verified via a mocked
scan since this LAN only has one bulb; a live "genuinely new device found"
scenario is still worth re-confirming the next time a second Tuya device
actually joins the network.

## Remote access & security — PIN gate implemented, pentest phase pending

The PIN gate (`docs/remote-access-security.md`, `iterations/004`) is real
and tested: brute-force lockout, server-verified session expiry, PBKDF2
PIN hashing. Explicitly **not yet done**:

- **A dedicated adversarial security test** — a separate agent actually
  attacking a real, exposed (DuckDNS + port forward) instance from the
  outside: brute-force timing analysis, session token forgery attempts,
  replay attacks, discovery-timing via port scan, lockout-bypass attempts.
  This needs an actual deployed instance to attack meaningfully rather than
  a same-machine simulation — see `roadmap/` for this as its own phase.
- **TLS / reverse proxy** (e.g. Caddy with automatic certs for a DuckDNS
  domain) — the PIN currently travels over plaintext HTTP once forwarded
  publicly; flagged clearly in the security doc, not yet built.
- **Tailscale is the currently-recommended path** for anyone who doesn't
  need public access at all — no code changes needed, just documented
  setup steps.

## Other plausible future features (not started)

- Multi-user accounts / auth on the dashboard itself (currently assumes a
  trusted home LAN, no login).
- HomeKit / Google Home / Alexa bridging.
- Energy usage estimates (Tuya's DP set doesn't expose real power draw on
  this bulb model — would need a smart plug with power monitoring instead).
- Mobile app wrapper (the web dashboard is already mobile-responsive;
  a native wrapper would mainly buy home-screen icon + notifications).
