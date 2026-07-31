# 005 — Observability, Network Resilience & Security Docs

Week 2 Phase D. Roadmap sections 13 (W2-226–240), 11 (W2-196–210),
14 (W2-241–250), plus the surfacing items from sections 1 and 2.
Tracking issue: `THEROCKSSS/smart-bulb-dashboard#72`.

## Goal

Give the backend a way to answer "am *I* healthy?" (as opposed to "is this
bulb reachable?"), make it survive the two things a home network actually
does to it (the router reboots; DHCP moves the host), and write down —
honestly — what the PIN gate does and doesn't protect against.

## Approach

Three new backend modules, deliberately standalone so a broken audio stack
can't take the observability layer down with it:

- `observability.py` — metrics + latency percentiles + error rates,
  Prometheus text output, correlation IDs, a level-configurable log ring
  buffer, startup dependency probes, and the redacted diagnostic report.
  Imports nothing from the rest of the backend at module scope (config and
  bulb_manager are imported lazily inside the functions that need them).
- `network_health.py` — host-IP change detection, connectivity poll with a
  reconnect hook, per-bulb latency history, connectivity-mode
  classification.
- `remote_access_status.py` — public IP, DuckDNS last-sync, Tailscale
  status, exposure warnings.

Plus wiring: an `observe_request` HTTP middleware, a startup dependency
gate, `bulb_manager.reset_all_connections()`, retry + a real scan/skip
decision in `discovery.py`, a System → Health tab, Diagnostics and Settings
additions, and a persistent exposure banner.

Docs: `SECURITY.md`, `docs/pin-gate-threat-model.md`,
`docs/observability.md`, plus README/API/FEATURES/remote-access updates.

## What happened

Most of it worked as designed. Four things did not, and one pre-existing
bug fell out along the way.

## Failures

### 1. Route templates could not be read from the request scope

The obvious way to label a metric with its route template is to read
`request.scope["route"]` after `call_next`. Starlette 0.41.3 doesn't put it
there — `Route.matches()` returns only `{"endpoint", "path_params"}`, and
which internals a middleware can rely on has changed between versions.

**Root cause:** an assumption about framework internals, not a bug.

**Fix:** `observability.route_template()` matches the concrete path against
the router's own compiled `path_regex` list and returns `path_format`.
Explicit, version-independent, and cached (bounded at 2048 entries — an
unbounded per-path cache in front of a metrics recorder is a
memory-exhaustion primitive for anyone hammering random 404s).

**Second failure inside the fix:** the first version had a prefix fallback
(`path.startswith(route.path + "/")`) intended for `StaticFiles` mounts. It
fired on the *first* matching prefix, so `POST /api/devices/bulb-1/power`
resolved to `/api/devices` — caught by
`test_route_template_maps_a_concrete_path_onto_its_template`, which is
exactly why that test exists. Mounts have a `path_regex` of their own, so
the fallback was unnecessary; removed.

### 2. Two test expectations were wrong about `ipaddress`

`test_classify_ip` originally asserted `203.0.113.7` (TEST-NET-3) is
`public`. Python folds every reserved, not-globally-routable range into
`is_private`, so it comes back `private`. The *code* was right and the test
was wrong — corrected, with a comment recording why, so nobody later
"fixes" the classifier by re-basing it on `is_global` alone.

Separately: Tailscale's 100.64.0.0/10 is **not** `is_private` in Python
(it's officially shared address space, not private). Without an explicit
CGNAT check a tailnet peer would classify as a public client and wrongly
trip the exposure warning banner. That check is in `classify_ip()` and is
covered by its own parametrized case.

### 3. The tests were reading and writing the real `backend/data/`

After the first run of the new suite, `backend/data/network_state.json`
appeared on the developer machine — the new modules' state files were not
isolated, so tests were reading (and overwriting) real machine state. Same
class of bug the existing `auth_reset` fixture was written for.

**Fix:** an autouse `observability_reset` fixture in `conftest.py` that
redirects all three new state files at a pytest `tmp_path` and clears the
process-global metric/log/latency state before and after every test.

**Pre-existing, not fixed here:** the *existing* suite still writes
`backend/data/audio_safety.json`, `audio_last_session.json`,
`audio_session_presets.json` and `auth_audit.log` on the real install. Out
of scope for this phase, but it's the same problem and should get the same
treatment.

### 4. Pre-existing frontend bug — a blank dashboard on a first visit

`bootDashboard()` referenced a `ROUTES` map that stopped existing when the
eleven sidebar panels were consolidated into five `PAGES`:

```js
location.hash = "#/" + (savedRoute && ROUTES[savedRoute] ? savedRoute : "control");
```

On a first visit (no `#` in the URL) that line throws a `ReferenceError`
before the assignment happens, so the hash is never set, `hashchange` never
fires, `router()` below it never runs, and the dashboard renders **nothing
at all**. Anyone with a `#/...` bookmark or a `localStorage` route never
hit it, which is presumably why it survived.

**Fix:** `location.hash = "#/" + (lsGet(LS_KEY_ROUTE) || DEFAULT_ROUTE)`.
`currentRoute()` already sanitizes an unknown/stale saved route back to
`DEFAULT_ROUTE`, so no extra validation is needed.

## Design decisions worth recording

- **`sounddevice` is deliberately not a required dependency.** A missing
  `tinytuya` or `numpy` aborts startup; a broken PortAudio logs a warning
  and marks the install degraded. Audio-reactive lighting is one feature of
  many, and a headless host should still get bulb control, scenes,
  schedules and the API — told so plainly, rather than refusing to boot.
- **`/metrics` is *not* exempt from the PIN gate.** Endpoint names, call
  counts and error rates are a decent map of the install. The LAN-only
  default already leaves it open for a local Prometheus; scraping through
  an enabled gate needs a session cookie. Documented rather than silently
  exempted.
- **Latency is a Prometheus `summary`, not a `histogram`.** Bucket
  boundaries would have to be chosen up front for a workload (LAN bulb
  calls) whose latency spans three orders of magnitude. The cost —
  summary quantiles can't be re-aggregated across instances — is
  irrelevant for one process.
- **Per-bulb latency is recorded from real `status()` calls**, so the
  history costs no extra network traffic, and is kept in memory (200-sample
  rolling window). Persisting a sample per bulb poll would mean a disk
  write per poll; losing the window on restart is the better trade, and it
  matches the tradeoff `remote_auth` already makes for lockout state.
- **The exposure banner is not dismissable.** The condition it reports is
  an unauthenticated dashboard reachable from the public internet. A
  dismiss-once banner would specifically fail the case the roadmap item
  (W2-011) is about: exposure set up correctly *with* the gate on, gate
  turned off months later.
- **The public-IP lookup has exactly one caller, by design.**
  `SECURITY.md` promises no telemetry; that's only worth something if the
  code has no background caller, so `status()` always serves the cached
  value and there's a test asserting it never performs the lookup itself.

## Fix / what shipped

New: `backend/observability.py`, `backend/network_health.py`,
`backend/remote_access_status.py`, `SECURITY.md`,
`docs/pin-gate-threat-model.md`, `docs/observability.md`, three new test
modules.

Modified: `backend/main.py` (middleware, 15 routes, startup gate),
`backend/bulb_manager.py` (`reset_all_connections()`, latency recording),
`backend/discovery.py` (scan retry, `should_scan()`),
`backend/tests/conftest.py` (isolation fixture), `frontend/app.js`
(Health tab, Diagnostics/Settings additions, exposure banner, the `ROUTES`
fix), `frontend/style.css`, plus README/API/FEATURES/AGENTS/remote-access
docs.

## Verification

**Test suite:** `353 → 472 passing`, 0 failures, ~20s.

```
backend/venv/Scripts/python.exe -m pytest backend/tests/ -q
472 passed, 2 warnings in 19.84s
```

**Live run** (`uvicorn main:app --port 8611`, real machine, real network):

- Startup dependency gate ran for real and logged three OK probes:
  `dependency ok: tinytuya 1.20.0` / `numpy 2.4.6` / `sounddevice 0.5.5`.
- `/metrics` returned well-formed exposition text with real quantiles:
  `sbd_request_latency_seconds{method="GET",endpoint="/api/system/health",quantile="0.95"} 0.00227`.
- `X-Correlation-ID` present on every response; a supplied id echoed back.
- `/api/system/health-summary` reported `mode: full` and correctly
  classified all five of this machine's real addresses (three `private`,
  one `tailscale` at 100.x, plus loopback).
- Tailscale check found the **real** daemon: `installed: true`,
  `running: true`, `backend_state: Running`, 24 peers, and produced a real
  MagicDNS tailnet URL.
- DuckDNS sync POST → `exposure_configured: true` →
  `warnings: ["exposure_configured_gate_disabled"]` (gate off) → retracting
  exposure → `warnings: []`.
- The opt-in public-IP lookup returned a real address with no error.
- Log level POST DEBUG → GET confirmed DEBUG → restored to INFO.
- All 15 endpoints the new UI calls returned 200, including `/`,
  `/static/app.js` and `/static/style.css`.
- Diagnostic report generated against a config whose `local_key` was
  `REPLACE_WITH_LOCAL_KEY`: **0 occurrences** of that string in the
  response body, and `os.listdir(DATA_DIR)` unchanged before/after
  (asserted in `test_diagnostic_report_writes_nothing_to_disk`).

**pip-audit** (run in a throwaway venv, never installed into
`backend/venv`): 7 unique advisories, all in transitive `starlette 0.41.3`,
none in a directly-pinned package. Findings, per-advisory applicability and
the reason they are not yet fixed are recorded in `SECURITY.md`.

**Not verified:** the frontend was only verified at the data-contract
level (every endpoint it calls returns 200 with the expected shape) and by
`node --check`. It has **not** been opened in a browser — no visual
confirmation that the Health tab, the log viewer, the latency tables or the
exposure banner actually render. That is the main thing a reviewer should
do by hand.
