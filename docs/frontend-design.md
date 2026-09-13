# Lighting studio frontend

The September 2026 redesign keeps FastAPI and the no-build vanilla JavaScript
frontend. All five navigation groups share the new theme. The lighting surface
lives in `frontend/control.js`; existing feature panels remain in `frontend/app.js`.

## Visual system

| Role | Token |
| --- | --- |
| Background | `#171917` |
| Raised surface | `#232620` |
| Borders | `#383d33` |
| Primary text | `#f1efe7` |
| Secondary text | `#b3b6a9` |
| Accent | `#e9bd7e` |

Display type uses the locally available Segoe UI Variable Display family; body
text uses Segoe UI Variable Text / Segoe UI. Measurements use Cascadia Mono /
Consolas. No fonts, icons, JavaScript packages, or images load from a CDN.

The desktop layout combines persistent labeled navigation, the active view,
and quick controls. Tablet layouts omit the quick-control column; phones use
bottom navigation and a single column. Responsive changes live in media queries.
Native scene, effect, and preset buttons support keyboard activation. Focus rings,
an inert background for the PIN dialog, and a route-preserving skip link support
keyboard navigation.

The bulb illustration uses CSS light falloff, glass, and a screw base. Color and
brightness preview while dragging. Short opacity/transform transitions announce
page and power changes; reduced-motion preference disables animations.

## Responsiveness and truthful state

- Controls render before the first hardware status response. Unknown/offline
  state is explicit and commands stay disabled until a bulb answers.
- One selected-device status request runs at a time; hidden tabs skip polling.
- Switching devices clears the old reading. Late responses cannot overwrite the
  selected device or a newer control action.
- Power changes preview immediately with a pending label. A separate hardware
  read confirms success. Failed commands restore the previous reading.
- A Tuya error inside HTTP 200 is treated as a command failure.
- Slider commands retain only the latest queued value per endpoint, in gesture
  order. White temperature stays a draft until the user applies it.

This removes the frontend's dependency on bulb response time for first rendering.
It does not make Wi-Fi hardware response instantaneous.

## Verification

`python -B tools/verify-frontend.py` runs Chromium against local static assets
with synthetic API responses. It deliberately holds device status and power
responses, exercises success and rejection, native sliders, scene/preset actions,
navigation, saved device selection, offline recovery, and empty-device recovery.
It checks widths 320, 390, 768, 1024, and 1440, plus reduced motion.

To exercise deployed assets with the same isolated API fixtures in PowerShell:

```powershell
$env:SBD_VERIFY_URL = 'http://127.0.0.1:8504'
python -B tools/verify-frontend.py
Remove-Item Env:SBD_VERIFY_URL
```

The runner requires Playwright and its Chromium browser in the invoking Python
environment. Test mutations are intercepted and never reach the physical bulb.
Screenshots in `docs/screenshots/control*.png` use synthetic data; they are not
evidence of the real bulb being on or off. The PIN screenshot also uses a fixture.

September 13 verification: 744 backend/CLI tests passed; fixture browser journeys
passed on deployed assets; all 14 real dashboard views loaded without JavaScript
errors. The real bulb rejected its saved connection credentials/protocol, so
physical power-off could not be confirmed. The runtime currently reports its PIN
gate disabled; no authentication configuration change was part of this redesign.

## Upgrade path

Keep feature panels independent as they are changed. Extract the audio and system
views from the large app file when work on those views warrants it; a framework
migration is unnecessary for this redesign. If hardware remains slow after valid
credentials are restored, measure device round-trip separately from UI feedback.

Image builds now exclude local credentials, runtime state, and the host virtual
environment through `.dockerignore`. Continue binding the existing configuration
and data directory into the container using the project's compose overlays.
