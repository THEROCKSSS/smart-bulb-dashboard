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

## Music-reactive lighting (design doc exists, not implemented)

See `docs/music-reactive-lighting.md` for the full design — deliberately
written as an insight/design doc rather than shipped code, since it needs
real-world tuning against actual audio hardware (VoiceMeeter routing,
microphone gain, latency) that's better done as its own follow-up project
once the core dashboard is in daily use.

## Other plausible future features (not started)

- Multi-user accounts / auth on the dashboard itself (currently assumes a
  trusted home LAN, no login).
- HomeKit / Google Home / Alexa bridging.
- Energy usage estimates (Tuya's DP set doesn't expose real power draw on
  this bulb model — would need a smart plug with power monitoring instead).
- Mobile app wrapper (the web dashboard is already mobile-responsive;
  a native wrapper would mainly buy home-screen icon + notifications).
