# Audio Studio verification — 2026-09-13

- Backend/CLI and opt-in Chromium journeys: **760 passed**, 114.36 seconds.
- Ordered mixer queue: **5 Node tests passed**.
- Native TypeScript check: passed. Expo SDK57 web/Android/iOS exports: passed.
- Browser exercised initial connection recovery, Windows input identity, wheel
  selection, visible swatches, favorite colors, two-bulb routing and remembered
  selection, genre/saved mixes, cross-client updates, nullable duration and the
  dashboard-tool handoff with subsequent device selection/reload.
- Deployed image `bc932b41fae0`: healthy; API/web + Expo in one rootless container.
  Ten served source/lockfile SHA256 hashes matched local source. Settings and the
  Everyday listening preset survived restart. Bridge connected on CABLE index86.
- Actual Metro bundle: 4,334,646 bytes, correct app slug and published port8106;
  wheel, unified Audio and Windows dropdown code present. QR directory rendered.
- Deployed desktop and 390px web layouts rendered with zero page errors; no
  horizontal overflow. A saved slider value survived reload.
- Independent Metro recovery: terminating its process restarted Metro while
  the existing API process remained up and served the bounded live audio probe.
- Live audio path: 1,021 frames, no pipeline drops, 52 successful bulb sends,
  no sender errors. Software median6.332ms, bulb median12.321ms. Capture lateness
  was16.55%; this is a short local run, not a universal latency guarantee.
- Final physical bulb readback: online=true, power=false, error=null.
- Staged repository secret scanner: clean. Broad credential scan: zero matches.
  Whitespace check: clean. Two focused reviewers' concrete findings resolved.

Native phone gestures, embedded WebView rendering, its PIN-cookie handoff and
native file downloads remain unverified on physical Android/iPhone hardware.
Advanced Tools screens reuse the existing dashboard in a WebView; they are not
separate native rewrites. See the iteration report for the CPU benchmark and
the remaining moderate npm build-tool dependency advisories.
