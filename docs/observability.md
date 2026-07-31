# Observability & Network Resilience

*Week 2 Phase D. Covers roadmap sections 13 (W2-226–240) and 11
(W2-196–210).*

Two questions this answers that nothing in the dashboard answered before:

1. **Is the backend itself healthy?** Uptime, dependency state, request
   latency, error rate, what the logs say. Distinct from the Diagnostics
   tab, which only ever answered "is *this one bulb* reachable".
2. **What is the network doing to me?** Whether the host's own IP moved,
   whether connectivity dropped and came back, whether the LAN is up but
   Tailscale isn't (or the reverse), and how each bulb's latency is
   trending over time rather than at one instant.

Everything here is in-memory and process-local by design — this is a
single-process dashboard for one household, and a time-series store would
be more machinery than the problem deserves. `/metrics` exists precisely so
that anyone who *does* want durable history can point a real Prometheus at
it instead.

---

## System Health page

**System → Health** in the dashboard. One round trip to
`GET /api/system/health-summary`, showing:

- **Backend** — version, uptime, start time, Python version, current log
  level, and a `HEALTHY`/`ATTENTION` badge with an explicit list of
  problems when it isn't healthy.
- **Dependencies** — see below.
- **Requests** — total handled, server-error rate, client-error rate, and a
  per-endpoint table of p50/p95/p99 latency and error counts.
- **Network** — the connectivity mode and every address this host answers
  to, classified (`private` / `tailscale` / `public` / `loopback`).
- **Bulb latency over time** — per-bulb p50/p95/max and failure rate.
- **Logs** — a filterable tail of recent backend log lines, plus the
  diagnostic-report download button.

### What "healthy" means

`healthy: false` (and a line in `problems[]`) when any of:

- a **required** dependency is missing or broken;
- an **optional** dependency is unavailable (degraded, not fatal);
- the host has no LAN address, so bulb control cannot work;
- the server-error rate is above 5%;
- there are any `ERROR`-level entries in the recent log buffer.

### Documented baseline

On an idle LAN-only install with one bulb, expect roughly:

| Metric | Healthy range | Investigate if |
|---|---|---|
| `/api/system/health` p95 | < 5 ms | > 50 ms — the process is starved |
| `/api/devices/{id}/status` p95 | 40–150 ms | > 1000 ms, or a rising failure rate — Wi-Fi or the bulb |
| Bulb latency p50 | 50–100 ms | consistently > 300 ms |
| Bulb failure rate | 0% | > 5% sustained (this bulb genuinely drops off Wi-Fi periodically — see `AGENTS.md`) |
| Server error rate | 0% | anything above 0 |
| Uptime | hours to weeks | resets you didn't cause — check `reconnects` in `/api/system/network` |

## `/metrics` (Prometheus)

```
GET /metrics        →  text/plain; version=0.0.4
```

Standard Prometheus text exposition format. Metric names are prefixed
`sbd_`:

| Metric | Type | Labels |
|---|---|---|
| `sbd_build_info` | gauge | `version` |
| `sbd_uptime_seconds` | gauge | — |
| `sbd_start_time_seconds` | gauge | — |
| `sbd_requests_total` | counter | `method`, `endpoint` |
| `sbd_request_errors_total` | counter | `method`, `endpoint` |
| `sbd_request_client_errors_total` | counter | `method`, `endpoint` |
| `sbd_responses_total` | counter | `status` (`2xx`, `4xx`, …) |
| `sbd_request_latency_seconds` | summary | `method`, `endpoint`, `quantile` |

`endpoint` is always the **route template** (`/api/devices/{device_id}/power`),
never the concrete path — one series per real bulb id would make the
percentiles meaningless and the cardinality unbounded.

Latency is a `summary` with pre-computed quantiles rather than a histogram:
the quantiles are what a self-hoster actually looks at, and bucket
boundaries would have to be picked up front for a workload whose latency
spans three orders of magnitude. The documented cost is that summary
quantiles can't be re-aggregated across instances — fine for one process.

Quantiles are computed over a rolling window of the last 512 requests per
endpoint, so they answer "is it slow *now*". The `_sum`/`_count` pairs are
lifetime totals.

**Auth:** `/metrics` is **not** exempt from the PIN gate. With the gate off
(the LAN-only default) it's freely scrapeable. With the gate on, a scraper
needs a session cookie. This is deliberate — endpoint names, call counts
and error rates are a decent map of the install for anyone probing it.

Minimal scrape config for the LAN-only case:

```yaml
scrape_configs:
  - job_name: smart-bulb-dashboard
    static_configs:
      - targets: ["192.168.1.50:8500"]
```

## Dependency health checks

Run once at startup, **before any background thread is spawned**, and
cached thereafter (this is a property of the install, not something that
changes at runtime — `?refresh=true` on `/api/system/dependencies` forces a
re-probe).

Each check *imports the package and exercises one real operation on it*,
because "imports fine, explodes on first use" is the failure mode worth
catching:

| Package | Required? | Probe | Why |
|---|---|---|---|
| `tinytuya` | **yes** | import + `BulbDevice`/`deviceScan` present | Nothing works without it |
| `numpy` | **yes** | import + a real `rfft` on a zero array | A numpy built against the wrong BLAS imports fine and then crashes mid-song |
| `sounddevice` | no | import + `query_devices()` | This is the call that actually touches PortAudio |

`sounddevice` is deliberately **not** required. Audio-reactive lighting is
one feature of many; a headless host with no audio backend should still get
bulb control, scenes, schedules and the API — degraded, and *told so
plainly*, rather than refusing to boot.

A missing **required** dependency raises `DependencyError` and aborts
startup with an actionable message:

```
Startup aborted -- required dependencies are missing or broken:
  - tinytuya: not installed (No module named 'tinytuya') (needed for: local bulb control -- nothing works without it)

Fix, from the repo root, using THIS project's own venv (never a shared one):
  backend/venv/Scripts/python.exe -m pip install -r backend/requirements.txt
  # Linux/macOS: backend/venv/bin/python -m pip install -r backend/requirements.txt
See SETUP.md, and AGENTS.md's note on never installing into a shared venv.
```

## Logging

- One logger (`sbd`), configured lazily. It does **not** propagate to the
  root logger: under uvicorn that would double-print, and worse, a root
  handler could write an un-redacted copy somewhere we don't control.
- Level is configurable from **Settings → Logging** (or
  `POST /api/system/log-level`), persisted in
  `backend/data/observability.json`, and applied without a restart.
- The last 500 records are kept in an in-memory ring buffer and served by
  `GET /api/system/logs?limit=&level=`. `level` filters to that severity
  *and above*.
- Log messages are **redacted on the way into the buffer** (pattern-based)
  *and* on the way out (pattern + live secret values), so a secret-shaped
  line never sits in the buffer and a bare `local_key` can't escape through
  the viewer either.

### Correlation IDs

Every request gets an `X-Correlation-ID`, echoed on the response and
stamped onto every log record emitted while handling it — so a line logged
three frames deep inside `bulb_manager` ties back to the HTTP request that
caused it.

An inbound `X-Correlation-ID` is honoured (so a trace started at a reverse
proxy or the CLI stays end-to-end), but only if it matches
`^[A-Za-z0-9._:-]{1,64}$`. Anything else is replaced with a generated id.
That check is not cosmetic: without it, an attacker controls text written
verbatim into every log line for their request, which is a log-injection
primitive.

```bash
curl -i -H "X-Correlation-ID: my-trace-1" http://localhost:8500/api/system/health
# → x-correlation-id: my-trace-1
```

## Self-diagnostic report

```
GET /api/system/diagnostic-report?log_limit=200&history_limit=20
```

Also a **Download Diagnostic Report** button on the Health page. Bundles:
config *shape* (device count, per-device id/name/ip/protocol version and
the local_key's *length*, never its value), dependency state, the full
metrics snapshot, remote-auth settings (enabled, TTL, rate limits, active
session count), network state and connectivity, remote-access status,
recent logs, and recent per-bulb action history.

Two things it deliberately does not do:

- **No outbound request.** A support bundle must not phone anywhere. It
  reads only cached remote-access state.
- **No disk write.** It's returned to the caller and nowhere else, so
  nothing generated here can end up sitting in the repo waiting to be
  committed.

### Redaction

Three independent passes, because any one of them alone has a gap:

1. **Field-name matching** — any key that looks like a secret
   (`local_key`, `pin`, `pin_hash`, `salt`, `secret_key`, `token`,
   `password`, `authorization`, `cookie`, …) has its whole value replaced,
   whatever type it is.
2. **Assignment-pattern matching** — `local_key=abc`, `pin="1234"`,
   `secret_key: deadbeef` inside free-form strings (log lines, tracebacks),
   even for values this process has never seen.
3. **Bare-value matching** — the actual live secret values (read fresh from
   `config.json` and `remote_auth.json`) are replaced wherever they appear,
   catching a raw `local_key` printed on its own by a third-party
   traceback. Only values ≥ 12 characters, so a 4-digit PIN doesn't
   corrupt every port number and timestamp in the report.

This is asserted, not claimed —
`backend/tests/test_observability.py::test_diagnostic_report_never_contains_a_real_local_key`
plants a real-shaped key three ways and asserts it's absent from the
serialized bundle;
`…_never_contains_the_pin_hash_salt_or_signing_key` does the same for the
auth secrets.

**Still skim it before you share it.** IPs, device names and timestamps are
left intact on purpose — they're what makes the report useful.

---

# Network resilience

## What gets detected

`GET /api/system/network` (and a background poll every 60s):

- **Host IP changes.** DHCP moving the host is invisible until "remote
  access stopped working"; the change is logged with a timestamp, old and
  new address, kept in `backend/data/network_state.json` (last 50).
- **Connectivity loss and regain.** Recorded with how long the outage
  lasted (last 50).
- **Automatic reconnection.** On regain — *or* on an IP change — every
  cached tinytuya socket is dropped (`bulb_manager.reset_all_connections()`).
  `set_socketPersistent(True)` means each controller holds a TCP connection
  that died with the old link; without this they keep failing until
  something forces a reconnect. Controllers themselves survive, so history,
  timers and effect state are not collateral damage of a router reboot.

The IP probe performs **no network I/O**: `connect()` on a UDP socket sends
nothing, it only asks the routing table which local address would be used.
No route at all → `None`, which is read as "this host is offline".

## Connectivity modes

| Mode | Meaning | `bulb_control_available` |
|---|---|---|
| `full` | LAN and tailnet both present | ✅ |
| `lan_only` | LAN up, no Tailscale address — local control works, tailnet remote access doesn't | ✅ |
| `tailscale_only` | Tailnet up, no LAN — the dashboard is reachable but **cannot reach any bulb** | ❌ |
| `offline` | Neither | ❌ |

`tailscale_only` is the interesting one. The dashboard is reachable, so
someone *will* open it — and saying plainly that bulb commands can't work
beats every command timing out with its own opaque error.

Tailscale addresses are classified explicitly against 100.64.0.0/10.
Python's own `ipaddress.is_private` returns `False` for that range (it's
officially "shared address space"), which would make a tailnet peer look
like a public client and wrongly trip the exposure warning.

## Router-reboot resilience

Two behaviours:

1. **Scans retry.** A discovery scan launched while the router is still
   coming back up fails outright — the UDP broadcast has nowhere to go. One
   failure is not evidence that there are no devices, so the scan retries
   twice with a widening gap (5s, then 10s) before giving up. `scan_now()`
   reports `attempts` in its result.
2. **The scheduler decides properly.** Each 5-minute tick now polls network
   state first, then decides:
   - **no LAN → skip entirely.** A broadcast scan with no LAN can't find
     anything, *and* running it would overwrite `last_scan`, pushing the
     next real opportunity a whole interval away.
   - **host IP changed → scan immediately**, regardless of the interval. A
     new IP usually means a new router or subnet, so every cached bulb IP
     is suspect.
   - otherwise, the normal interval rule.

The decision itself is `discovery.should_scan()`, split out of the thread
so it's testable without running it.

## Per-bulb latency over time

Recorded from **every real `status()` call** — the round trip is already
being paid for, so building the history costs no extra network traffic.
Failed calls are recorded as failures with no latency value, so they raise
the failure rate without dragging the percentiles around.

- `GET /api/devices/{device_id}/latency-history?limit=100` — samples newest
  first plus p50/p95/min/max/avg and failure rate.
- Surfaced in **Diagnostics** (per-bulb, with the raw sample list) and on
  **System → Health** (all bulbs, summary only).
- Rolling window of 200 samples per bulb, **in memory** — resets on
  restart, the same accepted tradeoff `remote_auth` makes for lockout
  state. Persisting a sample per bulb poll would mean a disk write per
  poll, which is the worse trade.

## Firewall guidance for LAN-only operation

For LAN-only use, **nothing needs to be open to the internet.** Also served
as data from `GET /api/system/network` so the UI and this table can't
drift.

| Port | Proto | Direction | Needed for | Safe to close externally |
|---|---|---|---|---|
| 8500 | TCP | inbound, **LAN only** | The dashboard itself | ✅ yes |
| 6668 | TCP | outbound (host → bulb) | Tuya local control | ✅ yes |
| 6666 | UDP | inbound, LAN broadcast | Tuya discovery, protocol 3.1 | ✅ yes |
| 6667 | UDP | inbound, LAN broadcast | Tuya discovery, protocol 3.3 | ✅ yes |

Notes:

- **6666/6667 are only needed for network auto-discovery.** Block them and
  everything still works; you just have to add bulb IPs by hand.
- **Do not port-forward 8500** unless you have read
  [`pin-gate-threat-model.md`](pin-gate-threat-model.md) and enabled the PIN
  gate. Tailscale needs no forwarded port at all.
- Windows Defender Firewall will prompt on first run. "Private networks"
  only — never tick "Public networks".
- On Linux with ufw, LAN-only would be:
  `sudo ufw allow from 192.168.1.0/24 to any port 8500 proto tcp`
