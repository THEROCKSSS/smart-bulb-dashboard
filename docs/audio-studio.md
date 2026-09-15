# Audio Studio and phone companion

The web studio and native Expo companion use the same per-bulb mixer settings.
Edits save while stopped and apply live while running. Save feedback distinguishes
Saving, Saved and Not saved; Retry preserves the latest pending edits.

## Everyday use

Open Audio → Live Session in the dashboard. Choose a mode, adjust Bass/Mid/Treble,
sensitivity, gate, automatic gain, smoothing and brightness limits, then start.
The three gains span the low, middle and high frequency bands in multiband modes.
Smoothing follows the shortest path around the hue wheel. Gentle modes only
switches flash-heavy selections to Weighted Blend and saves that effective mode.

Save a named mix from the mixer or Session Presets. Rename, update, delete and
recall are available on both web and phone. A recall restores the complete mix,
including explicit unset duration/limits. Changes to the source, capture window,
warmup or duration require a stopped session; live controls can change immediately.

The phone has Room, Audio, Tools and Connect tabs. Its searchable bulb dropdown
remembers the selection for each server. Room includes a draggable color wheel,
hue/saturation sliders, visible Amber/Rose/Ocean/Lavender swatches, the full color
library, saved favorite colors, brightness and white temperature. The light preview
reflects the selected color. Scenes are available alongside the color controls.

Audio combines the mixer and saved mixes in one screen. Mix & presets contains
the ready-made genre collection with palette previews and your complete saved
mixes. Fine tune exposes mode descriptions, all band gains, audio hue, band count,
noise conditioning, envelope limits, timing and gentle-light controls. Blank
optional limits clear previous values. Capture/timing changes require Stop.

Connect puts the Windows input in a searchable dropdown, with CABLE Output first.
Server-address recovery remains available even if the previous server is offline.
Server settings refresh across clients; command epochs and request sequencing
prevent older polls from overwriting confirmed commands or pending edits.

Tools opens the existing responsive dashboard **inside a native WebView** in Expo
Go: timers/schedules, rooms/zones, effects, group audio/custom presets, history,
health, diagnostics, security, backups and device management. These advanced
screens reuse the web implementation; they are not separate native rewrites.
The selected bulb is passed into the dashboard and its inventory refreshes when
returning to the studio. In the Expo browser build, Tools navigates in the same
browser tab because cross-origin framing is intentionally blocked. The existing
PIN gate still applies; an embedded dashboard may ask for its own PIN session.

## Windows audio

The Podman container receives PCM from `tools/start-audio-bridge.ps1`, which runs
the Windows capture helper invisibly and avoids duplicate launches. The helper
remembers the selected input by its exact name and host API in
`backend/data/bridge-device.json`. Its first choice is CABLE Output. Missing saved
devices produce an error rather than silently switching to a microphone.

```powershell
& tools/start-audio-bridge.ps1
# Optional: register the same helper at Windows login; lighting stays stopped.
& tools/start-audio-bridge.ps1 -RegisterLoginTask
```

Playback enters **CABLE Input**; capture reads **CABLE Output**. On Owen's PC,
Voicemeeter's A2 output already owns CABLE Input. The main VAIO and AUX strips
(5 and 6 in Potato) now send to A2, alongside their existing speaker outputs.
VAIO3 already sent there. No output volume or speaker route was changed.
Direct test playback into CABLE Input failed because Voicemeeter owns it;
playback through Voicemeeter Input successfully reached the cable and bulb.

Change the input in the connection picker when needed. PortAudio indices can
change; the saved identity is resolved against raw device enumeration, including
MME/DirectSound aliases. Reconnect clears old queued frames and recomputes channel
capacity, so switching mono → stereo restores both channels.

## One Podman container

`Dockerfile` packages Node/Metro and FastAPI together. `deploy/run-studio.py`
supervises each process independently, propagates shutdown and restarts a failed
child. `deploy/healthcheck.py` probes both servers. The Windows audio helper is a
host process because the Linux VM cannot capture a Windows audio endpoint.

```powershell
podman build --format docker -t docker.io/library/apps-smart-bulb-dashboard:latest .
podman compose -p apps -f docker-compose.yml -f docker-compose.windows.yml -f docker-compose.podman.yml up -d --no-deps --no-build smart-bulb-dashboard
```

Current private endpoints:

- Web: `https://owens-pc-vpn.tailff2683.ts.net:8502`
- API on the PC: `http://127.0.0.1:8504`
- Expo: `exp://100.69.156.71:8106`
- Live QR directory: `http://100.69.156.71:8092/` → Smart Bulb

Metro's advertised URL explicitly uses the published port 8106. Both fleet
registries include the app. Verify `extra.expoClient.slug=smart-bulb-studio`, the
launch asset's port, and the actual bundle contents; a listening socket is not
sufficient. Expo SDK 57 is pinned by the npm lockfile. The Windows override mounts
the host's Expo login state read-only; it is excluded from the image and git.
Current Expo Go on iPhone requires the same Expo account in the CLI and on the
phone ([Expo's September 2026 announcement](https://expo.dev/changelog/expo-go-57-login)).

Settings live in the existing `backend/data` mount. Restarting the app restores
settings, not automatic lighting playback. Rebooting the whole Podman VM still
requires bringing the existing apps service back up; see workspace Podman docs.

## Verification and limits

The iteration report records API/browser tests, fixed-signal timings, real CABLE
telemetry and deployment checks. The native code is typechecked and bundled for
Android/iOS; its controls also run through an Expo web build against the real
isolated API. A physical-phone scan is a separate final check for Owen.
Bulb command latency varies with Wi-Fi and hardware; zero latency is not promised.
