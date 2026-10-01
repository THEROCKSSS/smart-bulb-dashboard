# 006 — Lighting studio redesign

## Goal

Redesign the full dashboard with smooth motion and immediate control feedback,
and fulfill a separate request to turn the physical bulb off.

## Approach

Preserve the no-build frontend and existing API contracts. Replace the shell and
control surface, share new visual tokens across existing panels, and extract the
lighting surface into `frontend/control.js`. Use browser interception for control
tests so testing never turns the user's real light on.

## Failures and fixes

The initial Chromium regression held the device-status response and showed that
the old page never rendered its brightness slider. The new control surface renders
synchronously while one background request obtains the hardware reading.

Review found stale quick controls while a button retained focus, a white-temperature
draft overwritten by polling, queue replacement preserving the wrong ordering,
a skip link changing routes, and an overbroad no-device guard blocking global
recovery pages. These were corrected and the browser journeys exercised again.

The bulb was discovered at the saved IP with the saved device identity and protocol
3.5. Container, host, an exclusive host connection with the container stopped, and
the final real HTTP power endpoint all returned `Check device key or version`.
Power readback remained unknown. No credentials or pairing state were changed.

## Verification

- 744 backend/CLI tests passed in 87.21 seconds.
- Chromium fixture journeys passed on source and on deployed assets, including
  pending power, protocol-error rollback, keyboard sliders, temperature draft,
  scene/preset buttons, offline retry, route/device persistence, and empty state.
- Widths 320, 390, 768, 1024, 1440 passed horizontal overflow checks.
- Live-browser reads covered all 14 dashboard views; no JavaScript errors or
  external network requests were observed.
- Deployed HTML/CSS/JS bytes matched the local frontend files.
- Local and Tailscale `/healthz` returned 200; container reported healthy.
- The PIN UI was tested with intercepted authentication responses because the
  real installation currently reports its gate disabled.

See `docs/frontend-design.md` for tokens, behavior, verification commands, and
limitations. Physical bulb power-off remains unconfirmed.
