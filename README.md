# Smart Bulb Dashboard

A local, cloud-independent dashboard for controlling Tuya-based smart bulbs
(Wi-Fi RGB+CCT bulbs sold under many brands — Bytech, Merkury, Feit, Teckin,
Govee's Tuya-based line, etc). Built as a prototype after reverse-engineering
local control of a Bytech A19 bulb — see `HANDOFF.md` for the full story of
how the device credentials were obtained.

Everything talks to the bulb directly over your LAN via [tinytuya](https://github.com/jasonacox/tinytuya).
No cloud round-trip for day-to-day control.

> **Repo:** GitHub → [`THEROCKSSS/smart-bulb-dashboard`](https://github.com/THEROCKSSS/smart-bulb-dashboard) (public).
> Licensed **noncommercial** — see [License](#license).

![Control panel](docs/screenshots/control.png)

## What's here

- **97 working features** — see `FEATURES.md` for the full itemized list
  (power/brightness/color control, 25 color presets, 15 mood scenes, 7
  animated effects, sleep/wake timers, a recurring schedule engine,
  multi-bulb groups, history, diagnostics, and more).
- A FastAPI backend (`backend/`) exposing a 49-route REST API.
- A vanilla-JS dark-themed dashboard (`frontend/`) — no build step, no
  framework.
- Project-local Claude Code skills (`.claude/skills/`) that teach an agent
  how to set this up and drive it.
- `docs/music-reactive-lighting.md` — a design doc (not yet implemented)
  for syncing the bulb to music via microphone/system-audio input.

## Quickstart

```bash
cd backend
python -m venv venv
./venv/Scripts/activate   # or source venv/bin/activate on Linux/Mac
pip install -r requirements.txt
cp config.example.json config.json
# edit config.json with your bulb's device_id, local_key, and ip — see SETUP.md
python -m uvicorn main:app --host 0.0.0.0 --port 8500
```

Then open **http://localhost:8500**.

Full step-by-step instructions, including how to obtain your bulb's
`device_id` and `local_key` (the hard part), are in **SETUP.md**.

## Docs

| File | What it covers |
|---|---|
| `SETUP.md` | Step-by-step setup from zero, including getting Tuya credentials |
| `API.md` | Full REST API reference with curl examples |
| `FEATURES.md` | Every implemented feature, itemized |
| `ROADMAP.md` | What's planned but not built yet (multi-bulb hardware, Bluetooth) |
| `docs/music-reactive-lighting.md` | Design insight for a future audio-reactive mode |
| `HANDOFF.md` | How this was built, verified, and what's known-fragile |

## Screenshots

| Scenes | Presets |
|---|---|
| ![Scenes](docs/screenshots/scenes.png) | ![Presets](docs/screenshots/presets.png) |

## Multi-bulb ready

Only one physical bulb exists today, but the config format
(`backend/config.example.json`) is already a list, and there's a working
`groups` feature (broadcast power/color to a set of device IDs). Adding a
second bulb later is: add an entry to `config.json`, restart, done — no code
changes.

## Security note

`backend/config.json` holds your bulb's `local_key` in plaintext (that's
what Tuya's local protocol requires). It's git-ignored and never leaves your
network — the dashboard redacts it in every API response. Don't commit your
real `config.json`; use `config.example.json` as the template.

## License

Released under the **Polyform Noncommercial License 1.0.0** — see
[`LICENSE`](LICENSE). Personal, study, and contribution use is free and
welcome. **Commercial use (selling it, offering it as a paid service, or
building a paid product on top of it) is NOT permitted** without a separate
commercial license from the author. See the LICENSE file for how to inquire
about commercial terms.
