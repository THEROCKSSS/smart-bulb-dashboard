# Handoff

> **START HERE.** This file is a recovery document, not a product spec.
> Everything below the "Round 1" heading is history, kept for context.
> This top section is the only part that describes the project *now*.

## Session

| | |
|---|---|
| **Date** | 2026-08-03 |
| **Repo** | https://github.com/THEROCKSSS/smart-bulb-dashboard (also mirrored to local Forgejo) |
| **Branch** | `master`, at or after `8097c82` (hash as written; later commits are fine — `git log` is the source of truth) |
| **Local URL** | http://127.0.0.1:8502 |
| **Tailnet URL** | https://owens-pc-vpn.tailff2683.ts.net:8502 |
| **PIN** | not in this repo — see "Credentials / config" |
| **Lineage** | Rounds 1–7 below. This is Round 8. |

## Current state

`master` was at `8097c82` when this was written, pushed to GitHub, working
tree clean and nothing unpushed, with **693 tests passing**
(`backend/tests/` + `cli/tests/`).

The count went **836 -> 658** deliberately, then to 671 as bridge tests
landed, then to 693 with the latency-instrumentation tests: 17 families of
parametrised tests were collapsed into single tests that still run every case
but report all failures together. No coverage was lost — if you are comparing
against an older handoff, 836 is not the number to restore.

Note the roadmap-sync CI bot pushes `docs/assets/roadmap-status.json`
whenever issues change, so `git push` will be rejected as non-fast-forward
surprisingly often. Rebase; its commits touch only that one file and have
never conflicted.
Roadmap **Weeks 1 and 2 are merged and closed**; Week 3 is partly done.
There is exactly **one branch** (`master`) and one tag
(`prototype/nav-layouts-50`, holding 50 throwaway nav prototypes — the two
that were adopted are already live).

The backend **now runs as a Docker container** (`smart-bulb-dashboard`,
`restart: unless-stopped`) and is no longer tied to whoever started it.
Assume it is **up**, not down — check with `docker ps` before starting
anything, or you will fight a port conflict on 8502. This replaced the
"run uvicorn in a terminal" model; see "How to resume" below.

**Audio in the container now works via the audio bridge.** The container
still has no audio devices of its own (`/api/audio/devices` returns zero
there, by design). `tools/sbd-audio-bridge.py` runs on the Windows host,
captures audio, and streams PCM to the backend on port 8503; the dashboard's
Audio page shows a live connectivity chip with four states (off / waiting /
silent / live).

Start it with `tools/start-audio-bridge.cmd` (double-clickable, stays open
with a level meter). `--probe` finds which device actually has sound on it;
`--list` shows the de-duplicated device list.

Verified working end to end: 48000Hz capture resampled to 44100 on the host,
thousands of frames, zero drops. **The resampling is not optional** — WASAPI
shared mode refuses any rate other than the device's own, so asking for
44100 directly fails with `Invalid sample rate [PaErrorCode -9997]`.

The host path in `deploy/windows/` remains for zero-added-latency work
(tuning presets by ear).

The physical bulb (`Bytech A19`, `192.168.0.134`) was reachable as of this
writing, which had not been true for most of this project's history; it does
drop off Wi-Fi periodically, so check before concluding the code is broken.

## What was done this session (Round 8)

Closed two of the four remaining tickets on the #75 low-latency audio spec.
Every number below was measured on this machine against the real bulb, not
derived from the design.

- **#78 — per-stage latency instrumentation.** New `backend/audio_latency.py`.
  A running session now reports **capture**, **analysis** and **bulb
  round-trip** separately, each with a representative figure *and* a worst
  case, plus late/dropped frame counts. Surfaced in `status()`, through the
  existing API routes unchanged, and in a new **Latency** card on the Audio
  page. Before this the only figure anywhere was `BulbSender._last_latency_ms`
  — one number, bulb only, no worst case.
- **#77 — native audio mode.** `tools\native-audio-mode.cmd` (and the
  `tools\sbd-native-audio.ps1` it drives) switches the dashboard between
  container mode and host mode in one command each way, on the same port.
  See "The two audio paths" below.
- **Instrumentation overhead measured**: 1.43 µs/frame against the ~111 µs the
  analysis itself costs — 1.3%, on a path that runs ~86x/second.
- **Dropped frames are now counted at all.** `SoundDeviceSource` was throwing
  PortAudio's `status_flags` away, so input overflow — samples that existed
  and never reached analysis — was invisible in every metric. The bridge
  server's own drop counter is now consumed too.

### Measured on the real bulb (12s session, real audio, 1031 frames)

| Stage | Typical | p95 | Worst |
|---|---|---|---|
| Capture | 11.61ms (the block period) | 30.6ms | 46.1ms |
| Analysis | 0.58ms | 0.92ms | 5.0ms |
| Bulb round-trip | 11.3ms | 87.9ms | **637ms** |

Software budget **12.19ms against a 10ms target — `within_target: false`.**
That is the honest current state and exactly what **#80** exists to fix: at
`BLOCK_SIZE = 512` / 44100Hz the pipeline is over budget before any bridge hop
is added.

Note the bulb round-trip is far more *variable* than the spec's "111-152ms"
suggests — p50 as low as 11ms, p95 88ms, worst 637ms in a single 12s run.
When #82 writes the latency budget, use the spread, not a single figure.

### Real bug found and fixed — in this session's own instrumentation

The first version measured capture latency as the **median gap between
capture callbacks**. Run live against DirectSound it reported p50 **0.93ms**
with a mean of **11.59ms**: the backend delivers blocks in bursts — several
back-to-back, then a ~30ms pause — so half the gaps are sub-millisecond while
the data is no fresher for it. Those back-to-back blocks are a backlog being
flushed, and their samples are correspondingly stale.

The effect was a 12ms pipeline reporting **1.5ms** and declaring
`within_target: true` — the exact false pass the ticket exists to prevent, and
it would have let #80 claim victory without changing anything.

Capture is now reported as **block period + delivery lateness**, with the raw
gaps kept alongside as a delivery-health diagnostic where a bursty median is
informative rather than a lie. `test_bursty_delivery_cannot_flatter_the_capture_figure`
replays the observed burst shape as a regression guard.

The lesson generalises: a median is the wrong statistic for anything delivered
in bursts, and only running it against real hardware exposed it.

## What was done in Round 7

Verified state, not claims — every item below was checked by running it.

- **Both applicable starlette CVEs closed.** `starlette 0.41.3 → 1.3.1`,
  `fastapi 0.115.6 → 0.141.1`, both pinned explicitly in
  `backend/requirements.txt`. All 7 advisories verified closed against the
  installed version. Only forced code change: `@app.on_event("startup")` →
  `lifespan` in `backend/main.py`.
- **Audio analysis 57% faster** (`0.260 → 0.111 ms/frame`, 2.24% → 0.96% of
  one core). Cached the Hann window, rfft frequencies and — the real cost,
  found by profiling not by micro-benchmark — `log_band_edges`, plus replaced
  per-band boolean masks with precomputed slice bounds. **Output is
  bit-identical**; a test compares 1848 values with exact equality.
- **Beat detection now uses the bass band**, not broadband RMS
  (`TempoTracker.update(rms, bass_energy=…)`). Measured 70–174 BPM on a dense
  mix: broadband **1/9** correct, bass band **9/9**. Broadband remains the
  fallback for sources with no low end.
- **Live SSE stream** (`backend/live_stream.py`, `GET /api/stream`). One
  connection carries bulb colour, History rows and log lines. PIN-gated,
  proxy-safe (`X-Accel-Buffering: no`), exempt from the request rate limiter,
  bounded per-subscriber queues that drop oldest.
- **History tab updates in real time**; live colour swatch in the right-hand
  Quick Control panel. Verified: 44 colour events / 39 distinct colours in a
  6-second session, History gaining rows with no reload.
- **Genre presets 8 → 24**, all carrying `tempo_range` and `best_for`.
- **`docs/audio-modes.md`** — all 20 modes explained, party/fast/slow
  guidance, tuning-by-symptom table. Preset tables generated from live data.
- **In-app documentation browser** — System → Docs. Discovers all 24 project
  docs, categorised, with server-side search. Slug-based lookup means path
  traversal is structurally impossible (verified: `../../backend/config` and
  percent-encoded variants all 404).
- **Two CI workflows fixed.** Link check had been silently checking *zero*
  links for weeks (bad glob — `docs/**/*.html` needs a subdirectory, all pages
  are directly in `docs/`); now checks 438. Roadmap sync now rebases/retries
  instead of failing on a non-fast-forward.
- **Repo tidied**: 5 branches → 1; prototype preserved as a tag.

### Real bugs found and fixed (all reproduced before fixing)

1. **`local_key` leak** — `bulb_manager.status()` passed raw tinytuya
   exception text into both the API response and the history log. Reproduced
   with a device double whose exception contains the key.
2. **Auth state file corruption bricked the whole dashboard.** `_save()` used
   `open(path, "w")`, which truncates before writing; a force-kill mid-write
   left half a file, and since the PIN gate reads it on *every* request,
   every request 500'd. Now an atomic temp-file + `os.replace`, and `_load`
   **fails closed** on an unreadable file rather than silently disabling auth.
3. **Blank dashboard on a returning visit** — `bootDashboard()` referenced
   `ROUTES`, deleted during the nav consolidation. Only fired when
   `localStorage` had a saved route *and* the URL had no hash.
4. **Non-ASCII session cookie returned 500 instead of 401** —
   `hmac.compare_digest` raises on non-ASCII `str`.
5. **uvicorn trusts proxy headers by default** for `127.0.0.1`, letting a
   local process forge its source IP. Fixed with `--no-proxy-headers` in all
   launch paths.
6. **Suite hang** — `test_secrets.py` auto-sweeps every GET route and picked
   up the never-ending `/api/stream`. Streaming paths are now tested against
   the generator directly.

## What's NOT done (the gap)

- **The #75 audio spec is 4/6 done.** Shipped: #76 capture seam, #79 bridge
  protocol, #81 the Windows capture tool, #78 latency instrumentation, #77
  native mode. **Still open:**
  - **#80 — sub-10ms analysis.** Decouple *hop* from *window*: keep a long
    window so bass stays resolvable (bass-band onset tracking gets 9/9 tempos
    where broadband RMS gets 1/9) and advance it by a short hop. Latency is set
    by the hop, frequency resolution by the window. **Must not touch
    `analyze_frame()`** — the 21 golden-value tests assert bit-identical output
    and are the guard proving the change is timing-only. Note `FFT_SIZE = 4096`
    already zero-pads, which interpolates bins without adding real resolution,
    so the window must stay long and only the hop shrinks. The measurement to
    prove it now exists: watch `budget.within_target` flip to true.
    When you do this, pass the **hop** period as the tracker's
    `block_period_ms` — capture latency is bounded by how often a block is
    produced, and the derived capture figure depends on that being right.
  - **#82 — documentation.** Deliberately last so it documents measured
    reality. Needs both modes, every setting's trade-off, a symptom-to-setting
    table, and the honest budget including the bulb's hardware floor.
- **Week 3 is ~20% done.** Only Phase A (the CVE fix, #74) shipped as a
  planned phase. The rest of Week 3 — Home Assistant, HomeKit, Alexa/Google,
  voice control, Discord bot, webhooks, PWA, scenes/effects expansion,
  scheduling depth, groups UX, notifications, presets sharing, accessibility,
  kiosk UX — is **not started**. Issues #29–#43.
- **Week 4 not started** (issues #44–#58).
- **The 24 genre presets are reasoned, not tuned by ear.** This is the single
  biggest known gap. The bulb was offline for most of this project, so nobody
  has heard any preset against real music. The bulb now works, so this is
  finally possible — and it is the highest-value remaining audio task.
  `docs/audio-modes.md` states this caveat in its opening paragraph.
- **Adversarial security-test phase (roadmap section W2-071–100) deliberately
  deferred.** It needs a real deployed target, and should run *after* the
  Week 2 hardening it tests — which has only just landed.
- **Multi-user auth (W2-121–140) deferred** per the roadmap's own instruction
  to revisit only if the single PIN proves insufficient.
- **No frontend visualizer** for the spectrum/beat data (W1-076–095). Phase B
  shipped the data; nothing renders it.
- **Two unreconciled preset systems** (genre bundles vs. session-config
  snapshots) and **two unreconciled calibration systems** (per-device-key
  signal conditioning vs. per-device-index sensitivity). Both real, both
  shipping, neither merged into one concept.
- **Backups exclude auth state by design**, so a migrated install starts with
  no PIN set. Deliberate (it makes "a restore can never flip the gate"
  structural) but a behaviour difference worth knowing.
- **Only one physical bulb exists.** Every multi-bulb feature — groups, zones,
  `wave`/`mirror`/`band_split` role modes, failover — is tested only against
  fakes and has never run on real hardware.
- **`docs/*.html` (the GitHub Pages site) is hand-maintained** and does not
  yet reference `SECURITY.md`, the threat model, or `docs/audio-modes.md`.

## How to resume

```bash
cd "C:\Users\User\Documents\Hermes stuff\hermes workspace\projects\smart-bulb-dashboard"

git status --short          # expect clean, on master, at or after 83b7ed5
git log --oneline -3

# Full suite — expect 836 passed. Takes 2-4 min; the audio and
# tempo tests do real signal processing, so it is CPU-bound and varies.
backend/venv/Scripts/python.exe -m pytest backend/tests/ cli/tests/ -q

# The dashboard should already be running as a container. Check first:
docker ps --filter name=smart-bulb-dashboard
```

**Normal path — Docker (survives reboot and session end):**

```bash
# Both files, always. The base file alone sets network_mode: "host", which
# on Docker Desktop for Windows is a no-op that ALSO makes the ports mapping
# inert -- so the base file on its own publishes nothing at all.
docker compose -f docker-compose.yml -f docker-compose.windows.yml up -d

# Or as part of the workspace apps tier (it is registered in docker/apps.yml):
#   docker compose -f ../../docker/apps.yml up -d smart-bulb-dashboard

docker logs -f smart-bulb-dashboard
```

Published on `127.0.0.1:8502` -> container `8500`. `--no-proxy-headers` is
already baked into the image's CMD and is REQUIRED, not optional: uvicorn
trusts `X-Forwarded-For` from `127.0.0.1` by default, which lets any local
process forge its source IP and dodge the PIN-gate lockout.

### The two audio paths

The container cannot reach Windows audio devices — a session started there
runs, reports itself running, and never reacts to sound. There are two ways
around that and they are both first-class. Pick by what you are doing:

| | **Bridge mode** (everyday) | **Native mode** (tuning by ear) |
|---|---|---|
| Dashboard | keeps serving from the container | container stopped, host serves |
| Added capture latency | ~1-2ms loopback hop | zero |
| Start with | `tools\start-audio-bridge.cmd` | `tools\native-audio-mode.cmd` |
| Use when | normal use, remote use | judging presets against real music |

**Bridge mode** leaves everything up and streams PCM from the host into the
container on 8503. **Native mode** is one command each way:

```bash
tools\native-audio-mode.cmd            # stop container, serve natively, Ctrl-C to end
tools\native-audio-mode.cmd status     # which mode is serving right now
tools\native-audio-mode.cmd off        # restore the container after a hard kill
```

Native mode serves the **same** `127.0.0.1:8502`, so the dashboard URL and the
tailnet URL both keep working across the switch (verified: tailnet `/healthz`
returned 200 while native mode was serving). The container is restored in a
`finally` block, so Ctrl-C, a crash, or a hard kill all put it back — verified
by killing uvicorn outright and watching the container come up again. If the
port is held by something unrelated it refuses **before** touching the
container, naming the process.

One trap it already handles: three processes listen on 8502 on this machine —
`com.docker.backend` on 127.0.0.1 plus `tailscaled` on both tailnet addresses.
Only the 127.0.0.1 bind can conflict; checking "the first listener on the port"
picks tailscaled and refuses to start in the completely normal case.

The older manual route still works:

```bash
docker stop smart-bulb-dashboard

# Supervised, restarts on crash, logs to logs/ -- and it is what the
# Scheduled Task in deploy/windows/install-scheduled-task.ps1 runs.
pwsh -File deploy/windows/run-dashboard.ps1

# Or plain, in a terminal:
cd backend
venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8502 --no-proxy-headers
```

Then open http://127.0.0.1:8502 and unlock with the household PIN (not
recorded here — see "Credentials / config" below).

If the tailnet URL is not serving:

```bash
tailscale serve status
tailscale serve --bg --https=8502 http://127.0.0.1:8502
```

**If every request returns 500**, suspect `backend/data/remote_auth.json`.
It is now written atomically so this should not recur, but if it is
unparseable the gate **fails closed by design** and logs once. Move the file
aside to reset the PIN gate to its disabled default.

## Credentials / config

- **Dashboard PIN**: **deliberately not written down in this repo.** This
  repository is public, so any PIN committed here is a published PIN — and
  git history keeps it even after the line is deleted, which is why the
  previous value was *rotated* rather than merely redacted. Ask the repo
  owner for the current one.
  Session TTL 30 days. Locked out with no way in? The gate reads
  `backend/data/remote_auth.json` on every request; move that file aside and
  the gate returns to its disabled default, then set a fresh PIN from the
  UI. Do not "fix" a lost PIN by committing the new one.
- **Bulb**: `Bytech A19`, `192.168.0.134`, Tuya protocol v3.5. Credentials
  live in `backend/config.json`, which is **git-ignored and must never be
  committed**. `config.example.json` is the template.
- **Git identity for this repo** (local, not global):
  `user.name = THEROCKSSS`,
  `user.email = 193167949+THEROCKSSS@users.noreply.github.com`.
- **Pinned runtime**: fastapi 0.141.1, starlette 1.3.1, tinytuya 1.20.0,
  numpy 2.4.6, Python 3.11.15.

## Standing constraints — read before committing anything

These are explicit, repeated instructions from the repo owner. Violating them
has required history rewrites before.

1. **Never add `Co-Authored-By`, "Generated with Claude Code", or session-link
   trailers** to commits, PR bodies or issue comments unless explicitly asked.
2. **Never self-credit or credit anyone else** in PRs, repos or commits unless
   asked. The `agentsoul` identity must not appear.
3. **Tracker is GitHub** for this project. Do not also post to Forgejo.
4. **Mobile changes go only inside `@media` blocks.** The desktop layout must
   remain untouched — two separate bugs have been caused by a desktop
   `grid-column` not being reset on mobile (issues #62 and a repeat during the
   Week 2 merge).
5. **Never trust a subagent's self-report.** Re-run the tests yourself. Every
   parallel-phase round so far has produced cross-phase bugs that only
   appeared once branches were combined.
6. **Never write a live credential into this repository — it is public.**
   The dashboard PIN lived in this file for five rounds and was therefore
   published on GitHub the whole time. Deleting the line is not a fix: git
   history keeps it, so the value had to be rotated. Credentials belong in
   `backend/config.json` or `.env` (both git-ignored) or in chat, never in
   a tracked file. Before committing, `git grep` for the current PIN.

## Known issues / blockers

- **`StarletteDeprecationWarning: Using httpx with starlette.testclient is
  deprecated; install httpx2`** — test-only, non-blocking. `httpx` is not in
  `requirements.txt` at all, so this is its own small task.
- **`backend/main.py` carries 17 pre-existing flake8 findings.** Count is
  unchanged across recent work; none are in modified regions.
- **Audio test-isolation gap**: `audio_last_session.json`,
  `audio_safety.json` and `audio_session_presets.json` are still written into
  the real `backend/data/` during test runs. Other modules were fixed; these
  were left as out of scope and are noted here so the next session does not
  rediscover them.
- **`test_remote_auth.py` enters `TestClient` as a context manager**, so real
  `on_startup()` runs during the suite and can fire a genuine ~18s LAN
  discovery scan when `discovery.json` has no `last_scan`. Pre-existing.
- **An offline bulb still takes ~9–13s for the page to fully settle** — the
  status badge and Control panel each make their own ~2s-bounded call. A
  shared in-flight cache would fix it; not built.

## Build order (what to do next)

The established loop, confirmed with the owner: **build one week as four
parallel phases → hub re-verifies every phase itself → hand-merge → owner
tests → next week.** See `.claude/skills/phase-delegation-loop/`.

0. **Low-latency audio bridge — spec #75, tickets #76–#82.**
   **#76, #79 and #81 are done, closed and pushed** (the capture-source
   seam, the wire protocol + network source, and the host capture tool).
   The bridge works end to end: real desktop audio drove the bulb through
   the container at a measured 147 BPM.

   **Frontier — startable now, no open blockers: #77 and #78.**
   #77 is the native audio mode (one command to swap the container for a
   host backend); #78 is per-stage latency instrumentation, which #80 then
   needs in order to prove it reaches ≤10ms. #82 (docs) is last on purpose
   so it documents measured reality.

   Original ticket note, still true for what remains: Audio-reactive lighting **does not work in the
   container** (no host audio devices), so this gates item 1 below unless you
   use the host path. Two tickets are startable now with no blockers:
   **#76** (capture source seam — the prefactor everything else needs) and
   **#77** (native audio mode — one command to swap the container for a host
   backend, which is what makes item 1 possible today).
   Critical path afterwards: #79 → #81 → #82.

   Read #75 before any of them. The headline constraint: **≤10ms is
   achievable from sound to decision, but the bulb's own round-trip was
   measured at 111–152ms** and no software change alters that. Do not chase
   it; do not let a reader think the bridge is the slow part.

   Device de-duplication from that spec is **already done** (72 → 41 entries,
   host-API preference, aliases retained, calibration made alias-aware).

1. **Tune the 24 presets against real music.** Highest value, now possible for
   the first time. Needs the owner present — it is a judgement task, not a
   code task. Note this needs the **host** audio path (#77), not the
   container.
2. **Week 3, phases B–D** (issues #29–#43): integrations (Home Assistant,
   webhooks, Discord), UX (PWA, scenes/effects, notifications,
   accessibility). Phase A is already closed.
3. **Adversarial security phase** (W2-071–100) against a real deployed
   target, now that the hardening has landed.
4. **Week 4** (issues #44–#58): analytics, diagnostics deepening, release
   process.

---

# History

Everything below is the record of how the project got here, round by
round. It is kept because the *why* behind several decisions only exists
here — but it describes past states, not the current one. For current
state, read START HERE at the top.

## What this is

A local dashboard + REST API for controlling a Tuya-based smart bulb
(Bytech A19 Wi-Fi RGB+CCT, protocol v3.5) with zero cloud dependency for
day-to-day use. Built 2026-07-27 in one session, end to end: from getting
local control working on a bulb with no existing credentials, through a
97-feature dashboard, to a pushed Forgejo repo.

## How local control was actually obtained (worth knowing if it breaks)

This took several real attempts — recorded here so nobody re-derives it:

1. **Network scan** (`tinytuya scan`) found the bulb immediately: device ID
   `REPLACE_WITH_TUYA_DEVICE_ID`, protocol v3.5, IP `192.168.1.100`. No local key
   from this — Tuya's local key is never broadcast.
2. **mitmproxy + re-pairing attempt** — set up mitmproxy on this PC, put the
   phone's Wi-Fi proxy through it, installed the mitm CA cert on iOS
   (confirmed working — verified Safari traffic to support.apple.com was
   readable in plaintext), factory-reset the bulb, re-paired it through the
   Tuya Smart app while capturing. **This failed**: across the entire
   capture, only 3 single-hit Tuya branding/deep-link domains appeared — no
   actual pairing API traffic. Root cause is almost certainly the Tuya app
   using HTTP/3 (QUIC over UDP) for its real API calls, which slips past a
   standard HTTP CONNECT proxy entirely (the OS proxy setting only redirects
   TCP). No data was lost — the bulb was already re-paired and working via
   the app throughout.
3. **Tuya IoT Platform (iot.tuya.com) attempt** — user already had a
   developer account, but the QR-code "Link Tuya App Account" step scanned
   successfully but never showed a confirmation popup on the phone. This is
   a known Tuya console bug, almost always caused by a data-center mismatch
   between the Cloud Project's region and the app account's registered
   region, or scanning while logged in as a shared "family member" account
   rather than the actual device owner.
4. **What actually worked**: the `tuya-device-sharing-sdk` (`tuya_sharing`
   on PyPI) — the same official QR-login flow Home Assistant's built-in
   "Tuya" integration uses. It needs only the app's **User Code** (Me →
   Settings → Account and Security) and scans with the app's own in-app
   scanner — no Tuya IoT Platform project required at all. This is
   documented step by step in `SETUP.md` and `.claude/skills/bulb-dashboard-setup/`.
   `local_key` obtained: present in `backend/config.json` (git-ignored, not
   in this repo).

## Bugs found and fixed during this build (via real testing, not review)

1. **Brightness silently flipped the bulb to white mode.** The device's
   `dp22` (`bright_value`) only applies in white mode; colour-mode
   brightness lives in the V component of `dp24`'s HSV colour data. The
   first `set_brightness()` implementation always wrote `dp22`, so dimming
   a colored bulb would silently switch it to white. Found by actually
   calling the API and watching the mode flip in the response. Fixed in
   `bulb_manager.py`'s `set_brightness()` — now mode-aware.
2. **`/status` returned partial/null fields after a color-only command.**
   This device (and likely others) sometimes replies to a status query with
   only the `dps` keys that just changed, not a full snapshot — so a status
   call right after `set_colour()` showed `power: null, mode: null` even
   though the bulb was clearly on. Fixed by caching and merging deltas into
   a `_last_dps` dict per controller instead of trusting each response in
   isolation.
3. **Status badge flashed "OFFLINE" on every page load** for the first
   ~3-4 seconds, even when the bulb was reachable — a single transient
   status-poll miss (real Wi-Fi flakiness on cheap IoT hardware, verified
   this bulb genuinely does drop off Wi-Fi occasionally during this same
   session) was enough to show a false offline state. Fixed by requiring 2
   consecutive failed polls before displaying OFFLINE, plus an extra quick
   re-poll ~1.2s after initial load.

## Verification performed (real, not claimed)

- Direct API curl tests against the real bulb: status, power, brightness,
  RGB/HSV color, scenes, presets, favorites, diagnostics — all confirmed
  against actual device state changes (hue/saturation/value read back
  matched what was sent).
- Playwright headless-Chromium test: loaded the dashboard, clicked through
  all 9 tabs, zero console/page errors.
- Playwright **interactive** test: clicked a preset swatch in the real UI
  and confirmed the bulb's hue changed to the expected value via a separate
  curl status check; started/stopped the "pulse" effect via UI click and
  confirmed the backend's effect-state matched; clicked the power toggle
  and confirmed the bulb's power state actually flipped.
- Visual screenshots of Control/Scenes/Presets panels reviewed (copies in
  `docs/screenshots/`).

## Known-fragile / watch for

- The bulb genuinely drops off Wi-Fi periodically (observed twice in this
  same session, unprompted) — this is bulb/router behavior, not a dashboard
  bug. The Diagnostics tab's connection test and the debounced offline
  badge exist specifically to handle this gracefully.
- `docker-compose.yml`'s `network_mode: host` only works on Linux Docker
  hosts. Noted in `SETUP.md`.
- Tuya's cloud-assisted login access token expires in a few hours
  (`expire_time: 7200` seconds seen in this session) — irrelevant to normal
  dashboard operation (that's all local-only after setup), only matters if
  re-running the setup flow to add more bulbs later.

## What's NOT built (intentionally)

- Bluetooth bulb support — roadmap only, no BLE hardware to test against yet.
- A second physical bulb — architecture (config list + groups) already
  supports it; just needs another bulb purchased and its credentials
  pulled via the same cloud-assisted login flow.
- Ears-on tuning of the audio-reactive modes against real music with the
  bulb online (it was offline for this entire build) — see
  `docs/music-reactive-lighting.md`'s "Known limitations" section.

## Round 2 — audio-reactive lighting + network auto-discovery

Added per user request, after the initial 97-feature prototype above:
**8 audio-reactive lighting modes** (`backend/audio_reactive.py`, the
**Audio Reactive** tab) and **network auto-discovery** with a weekly
scheduler + manual "Scan Now" (`backend/discovery.py`, Settings tab).
Brings the total to 121 working features — see `FEATURES.md`.

Full process detail — what was tried, what broke, what fixed it — now lives
in `iterations/` (see `iterations/README.md` for the convention). Short
version of the two real bugs found this round, both only findable by
actually running the code against real conditions:

1. **Audio callback froze when the bulb was offline.** Bulb commands were
   originally called directly from the sounddevice audio callback; tinytuya's
   blocking socket calls stalled the entire capture pipeline for as long as
   the bulb took to time out. Confirmed via 4 consecutive identical
   `/audio-reactive/status` polls a second apart (frozen) with the bulb
   independently confirmed offline. Fixed by moving all bulb I/O onto a
   separate sender thread that always acts on the latest queued value —
   the audio callback never blocks now. Full writeup:
   `iterations/002-audio-reactive-lighting/`.
2. **IP-change detection logged the wrong "old IP".** In
   `discovery.py`'s dedup logic, the config was mutated before the old
   value was read for the log entry, so a device whose IP changed would
   report `old_ip` equal to the *new* IP. Found via a mocked-scan test (the
   real LAN's one bulb happened to be offline during testing, so this path
   couldn't be exercised against real hardware — see the same iteration
   note for why). Fixed by capturing `old_ip` before mutating.
   Full writeup: `iterations/001-network-auto-discovery/`.

Both bulb offline observations above (Round 1 and Round 2) are the same
recurring, documented hardware behavior — this bulb genuinely drops off
Wi-Fi periodically. It happened to be offline for this entire second
session, which is *why* two of this round's verifications used synthetic
tones / mocked scans instead of the physical device, and why "does the
audio-reactive mode look good" is explicitly left as a follow-up rather
than claimed done.

## Round 3 — audio engine v2 + PIN-gated remote access

Reworked the audio pipeline for lower latency and more modes, added
multi-bulb orchestration, and added a PIN gate for exposing the dashboard
beyond the LAN. Now 137 working features total (`FEATURES.md`). Bulb was
still offline this entire round too — same recurring Wi-Fi behavior as
Rounds 1 and 2, not a new issue.

**Audio v2** (`backend/audio_reactive.py`, rewritten; full detail in
`iterations/003-audio-engine-v2/`):
- Capture block size 1024→512 samples (~23ms→~11.6ms), zero-padded to a
  4096-point FFT. Removed the old artificial analysis-rate gate entirely —
  every callback now computes and queues a fresh target.
- New `BulbSender` class per bulb: enforces a configurable `min_dwell_ms`
  (how long a color stays visible) completely independent of decision
  latency, always sending the freshest queued value.
- 4 new modes (12 total): `spectrum_gradient`, `band_flash_overlay`,
  `stereo_split`, `breathing_silence`.
- `GroupAudioSession`: one shared capture analysis driving multiple bulbs
  via `unison`/`phase_offset`/`band_split` roles, each bulb still getting
  its own independent sender.
- **Real bug caught in review, not testing**: every mode's hue smoothing
  used a plain linear blend, which breaks at the 0°/360° wrap boundary
  (harmless for the original 3 modes' anchors, but `stereo_split`'s target
  genuinely crosses it). Fixed with a proper circular-mean blend
  (`_smooth_hue()`), applied everywhere for consistency.

**PIN-gate remote auth** (`backend/remote_auth.py`, new; full detail in
`iterations/004-pin-gate-remote-auth/`):
- PBKDF2-SHA256-hashed PIN (never plaintext), stateless HMAC-signed
  session tokens, per-IP brute-force lockout (5 attempts / 5 minutes).
- **Real bug found via a live Playwright test**: the root page `/` itself
  was gated, so enabling the PIN feature meant the browser got a raw 401
  instead of the HTML page containing the PIN form — a real
  lock-yourself-out bug, not hypothetical. Fixed by adding `/` to the
  always-open path list; only the API underneath stays gated.
- Verified end-to-end for real: enable → blocked without a session →
  wrong PIN rejected → 5 failures triggers a lockout that blocks even the
  correct PIN → correct PIN (once unlocked) issues a working session →
  session token expiry enforced server-side (tested with a 10s TTL) →
  disable restores open access. Full real-browser login flow also
  confirmed via Playwright, zero console errors.
- `docs/remote-access-security.md` covers Tailscale (recommended) vs.
  DuckDNS+port-forward (requires this PIN gate, still plaintext HTTP —
  documented, not yet solved with TLS). A dedicated adversarial pentest
  phase (a separate agent actually attacking a live exposed instance) is
  intentionally scoped as future roadmap work, not squeezed into this
  build — see `roadmap/`.

## Round 4 — parallel-phase build, Roadmap One QoL, mobile fix, Tailscale, docs (2026-07-29)

### Current state

`master` at commit `2123262`, working tree clean, fully pushed to GitHub
(`THEROCKSSS/smart-bulb-dashboard`). `APP_VERSION = "0.3.0"`. **159 working
features** (`FEATURES.md`). 76/76 tests pass (`backend/tests/` + `cli/tests/`).
A real backend server is running locally right now: `127.0.0.1:8502`
(started via `backend/venv/Scripts/python.exe -m uvicorn main:app --host
127.0.0.1 --port 8502` from inside `backend/`), plus reachable over the
tailnet at `https://owens-pc-vpn.tailff2683.ts.net:8502` via `tailscale
serve` (tailnet-only, not Funnel). **The PIN gate is currently enabled** —
see Credentials below.

### What was done this session

1. **Four roadmap phases, built in parallel** (one subagent each, isolated
   git worktrees, hub-verified, hand-merged where diffs overlapped):
   audio modes (`harmonic_pairs`, `kick_snare_split` — a real flicker bug
   found and fixed, see `docs/music-reactive-lighting.md`), PIN-gate
   hardening (session listing/revocation, an audit log that never records
   the PIN, per-IP rate limiting independent of lockout), `cli/bulbctl.py`
   (stdlib-only REST client with shell completions), and a real
   `backend/tests/`/`cli/tests/` pytest suite (76 tests) plus
   `GET /api/analytics/usage` (real per-device on-time, no fabricated
   wattage).
2. **Roadmap One** — a QoL round, also built via parallel subagents: live-
   ticking sleep/wake timer countdowns and status badge (no more refresh-
   to-see), a visual polish pass (8px→12px radius, real card shadows,
   smoother hover states), and power-user niceties (remembered
   device/panel via `localStorage`, keyboard shortcuts on Control, copy-
   to-clipboard on Diagnostics, an Undo action on cancel-timer toasts that
   actually recreates the timer).
3. **Mobile-friendliness fix, shipped as PR #63** (merged): found and
   fixed a real bug — `.topbar`'s desktop `grid-column: 1 / 3` was never
   reset in the mobile `@media` block, so the sidebar was collapsing to a
   ~16px sliver on phones. Also added 44px minimum tap targets and a
   responsive PIN-gate screen. Verified live at 375/414/800/1400px.
4. **Tailscale Serve** set up and verified end-to-end as the actual
   off-LAN access path (tailnet-only HTTPS, real cert, no port
   forwarding), with the PIN gate enabled alongside it — 21/21 live
   security checks passed (root path stays open, protected routes 401
   without a session, lockout/rate-limiter/session-revocation all work,
   audit log never leaks the PIN).
5. **Git identity / attribution cleanup**: rewrote the 6 commits that had
   been authored as `agentsoul` with `Co-Authored-By`/session-link
   trailers — stripped the trailers, reauthored as
   `THEROCKSSS <193167949+THEROCKSSS@users.noreply.github.com>` (repo-local
   git config, not global), force-pushed. **Caught and fixed a mistake
   made mid-cleanup**: the first force-push accidentally clobbered 3 real
   `roadmap-status-bot` sync commits on `master` (local `master` had
   quietly diverged from remote before this round started) — rebuilt
   master properly by resetting to right after the CI-fix commit and
   replaying the bot's 3 real commits on top with their actual authorship
   intact, then force-pushed the corrected version. Verified the bot
   commits' content matched exactly before re-pushing.
6. **Documentation pass to v0.3.0**: `CHANGELOG.md`/`docs/changelog.html`
   (new v0.3.0 entry), `FEATURES.md` (22 new itemized entries, 138–159),
   `README.md`, `AGENTS.md`, `docs/index.html`, `docs/features.html`,
   `docs/api.html` (analytics + session-management + `bulbctl` sections),
   `docs/audio.html` (14 modes), `docs/security.html` +
   `docs/remote-access-security.md` (session/audit/rate-limit hardening,
   Tailscale Serve now exercised not just recommended). Marked 3 real
   Week 3/4 roadmap issues (`#44` energy/usage analytics, `#35` CLI tool,
   `#51` developer experience/tests) `in-progress` with honest partial-
   progress notes, since this round's work genuinely advances them without
   completing their full W-item ranges. Closed `#59`–`#61` (Roadmap One
   tracking issues, now merged). Re-ran `.github/scripts/sync_roadmap_status.py`
   so the live Active Roadmap page reflects this immediately.
7. **Real bug found and fixed while re-verifying**: enabling the PIN gate
   for real (step 4) broke 16 previously-passing tests, because most of
   `backend/tests/`'s shared `client` fixture didn't isolate
   `remote_auth`'s on-disk state and was reading the real, now-enabled
   `backend/data/remote_auth.json`. Fixed by making the fixture depend on
   the existing (previously opt-in) `auth_reset` fixture. 76/76 pass again.

### What's NOT done (the gap)

- The adversarial security-test phase is still not done — this round's
  21-check security pass was a same-machine verification (the same
  session that built the feature also wrote the checks), not an
  independent attacker's attempt against a real deployed instance. See
  `docs/remote-access-security.md`'s "Still planned" section.
- The Undo action's wake-timer path was code-reviewed and its backend
  payload verified correct, but not independently clicked through in a
  real browser (only the sleep-timer undo path was UI-driven end-to-end).
- The PIN gate is currently **enabled** on the locally-running server
  (was disabled before this session touched it) — intentional for the
  Tailscale exposure, but worth knowing before assuming the dashboard is
  open like it used to be.

### How to resume

```bash
cd "C:\Users\User\Documents\Hermes stuff\hermes workspace\projects\smart-bulb-dashboard"
git status --short            # should be clean, on master, at or after 2123262
backend\venv\Scripts\python.exe -m pytest backend\tests\ cli\tests\ -q   # expect 76 passed

# Bring the dashboard up locally:
cd backend
venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8502
# Then: http://127.0.0.1:8502 (PIN gate enabled — see Credentials)

# Re-expose it over Tailscale if the serve mapping isn't still active:
tailscale serve status                                    # check current mappings
tailscale serve --bg --https=8502 http://127.0.0.1:8502    # re-create if missing
```

### Credentials / config

- **Local server PIN**: set this session for the Tailscale exposure. The
  value that used to be recorded here has since been rotated, because this
  repository is public and it had already been published. Session TTL: 30
  days once logged in.
- **Tailscale URL**: `https://owens-pc-vpn.tailff2683.ts.net:8502`
  (tailnet-only — reachable from any device signed into the same
  Tailscale account, not the public internet).
- `backend/config.json` (real device credentials) remains git-ignored,
  never committed.
- Git identity for this repo (local, not global):
  `user.name = THEROCKSSS`, `user.email =
  193167949+THEROCKSSS@users.noreply.github.com`. Do not add
  `Co-Authored-By`/session-link trailers to commits, PR bodies, or issue
  comments unless explicitly asked — this was an explicit standing
  instruction from this session.

## Round 5 — Week 1 roadmap, built via 4 parallel phases (merged in PR #68)

### Current state

Built on branch `week1-integration` and **merged to `master` via
[PR #68](https://github.com/THEROCKSSS/smart-bulb-dashboard/pull/68)** on
2026-07-31. Branch since deleted. Left here as history; see START HERE at the
top of this file for current state. Tracked in
issues #64–#67 (one per phase). 353/353 backend tests pass. A real backend
server is running locally against this branch's code:
`127.0.0.1:8502` (also reachable over the tailnet at
`https://owens-pc-vpn.tailff2683.ts.net:8502`, PIN held by the repo
owner — this value has since been rotated).

### What was done this round

1. **Four roadmap phases, built in parallel** (one subagent each, isolated
   git worktrees, hub-verified via real `git merge` rather than patch
   application since all four phases modified the same core classes):
   - **Phase A** — 6 new audio modes (`energy_contour`, `bass_only_pulse`,
     `mirror_mode`, `random_walk_hue`, `silence_flash_recover`,
     `crescendo_ramp`), a `TempoTracker` (BPM autocorrelation, tap-tempo,
     beat confidence, sensitivity presets), all 8 genre presets.
   - **Phase B** — `SignalConditioner` (AGC, noise gate, clip detection, DC
     removal, per-band gain, calibrate-from-silence), full N-band status
     exposure, a reusable synthetic-audio test harness with golden-value
     regression tests.
   - **Phase C** — `wave`/`mirror` group role modes, per-bulb overrides,
     failover handling, orchestration presets, a Zone data model, and
     per-device-index sensitivity calibration.
   - **Phase D** — session conflict detection, max-duration/warmup/
     auto-pause, socket-timeout + watchdog restart, session presets, a
     photosensitive-safety flash-rate cap (WCAG 2.3.1), lightshow capture/
     replay, scheduled audio sessions.
2. **Merging four phases that all touched the same core classes** required
   real hand-resolved `git merge`s (not patch application) across ~40
   conflict blocks total, always combining both sides' intent rather than
   picking one — full detail is in the git history on `week1-integration`,
   not repeated here.
3. **5 real post-merge test failures, found and fixed** (all previously
   passing in each phase's own isolated worktree, broken only once merged
   together): a mode-validation check silently dropped during the merge,
   a rate-limiter's module-level state leaking across tests, 3 test mocks
   that predated the merged `.confirmation()` contract, plus a genuine
   naming collision where Phase A's `audio_presets()` route handler
   shadowed the `audio_presets` module Phase D's routes needed to import.
4. **3 more real bugs found by actually testing this live** against a real
   physical bulb (that turned out to be unreachable on the network at
   the time) — see the "Bugs found via live testing" note below.
5. **Git hygiene fix**: the merge commit's `git add -A` had accidentally
   committed the 4 subagent worktree directories as embedded git repos
   (gitlink entries) — untracked them and added `.claude/worktrees/` to
   `.gitignore`.

### Bugs found via live testing (not caught by the test suite — it mocks the device layer)

Testing this over Tailscale surfaced a real physical bulb that had gone
unreachable on the LAN, which exposed three genuine bugs:

1. **An unreachable device hung `/status` for minutes.** Timed directly
   against the real bulb: **3m26s** before tinytuya gave up, because
   nothing bounded its socket timeout (default 5s) or retry limit
   (default 5, and each retry cost noticeably more than that in
   practice — not a clean `timeout × retries` relationship). Fixed in
   `bulb_manager.py`'s `_get_device()`: capped at a 2s timeout / 1 retry.
   Same real device now fails in **~2s**.
2. **The status badge got stuck reading "connecting…" forever** once a
   poll never succeeded even once. `renderStatusText()` returned early
   on `!state.lastStatus`, and `lastStatus` only ever got set on a
   *successful* poll — so the badge's placeholder text never updated, no
   matter how many times it polled. Fixed with an explicit
   `state.hasPolledOnce` flag that's set regardless of outcome.
3. **The badge and the Control panel briefly showed contradictory
   labels** ("LIVE DATA · OFF" vs "OFFLINE" for the same device) once fix
   #2 let `renderControl()`'s existing unconditional status-caching
   populate `lastStatus` with an `{online: false}` object — the badge
   logic then read `.power` off it instead of checking `.online`
   explicitly. Fixed by checking `.online === false` directly.

Verified all three with real timing tests and Playwright screenshots
against the actual unreachable device, not just unit tests: full backend
suite stayed green (353/353) throughout.

### What's NOT done (the gap)

- Owen has not yet tested/approved this round — do not start Week 2
  until he has.
- A short shared cache/de-dupe layer for concurrent `/status` polls to
  the same device was flagged as a possible follow-up (an offline device
  still takes ~9-13s to fully settle across the badge + panel's
  independent poll calls, each bound by the new ~2s timeout) — not built
  yet, pending Owen's input on whether it's worth it.
- PR #68 has not been merged to `master`.

## Round 6 — Week 2 Phase A: PIN gate hardening + API rate limiting (issue #69)

### Current state

Built in an isolated worktree branched from `master`. **431/431 backend
tests pass** (353 before this round), 22/22 CLI tests unchanged. Verified
against a real `uvicorn` server on `127.0.0.1:8577` over real HTTP, not just
the test client — full transcript in
`iterations/005-pin-hardening-and-rate-limiting/README.md`.

### What was done

Covers `roadmap/week-2-remote-access-and-security.md` section 4
(W2-051..070) and section 6 (W2-101..120):

- **Lockout is configurable and escalates.** Threshold/base/ceiling live in
  persisted state with env-var defaults; repeat lockouts for one source
  double the wait, decaying after a quiet day.
- **Weak PINs are refused, not warned about** — including this project's own
  `test1234`-style dev PINs. No override flag, on purpose.
- **Household + up to 5 guest PINs.** Revoking a guest PIN signs out only
  the sessions it opened. The household PIN can't be revoked (that would be
  a self-inflicted lockout); it's changed instead, which revokes every
  session and rotates the signing key.
- **New `backend/net_utils.py`** — shared client-IP normalization, so an
  IPv6 attacker can't walk out of a lockout by picking a new address inside
  their own /64.
- **New `backend/api_rate_limit.py`** — per-IP sliding-window limiter with
  four tiers (poll/read/write/expensive), 429 + `Retry-After`, LAN/loopback
  exempt by default, metrics in Diagnostics.
- Settings UI gained a live PIN strength meter, guest-PIN management,
  session length, lockout policy, and a Sign Out All Devices button;
  Diagnostics gained a rate-limiting card.

### One real pre-existing bug found and fixed

A session cookie containing any non-ASCII byte produced a **500 instead of a
401**: `hmac.compare_digest`'s `str` form raises `TypeError` on non-ASCII,
and Starlette decodes the `Cookie` header as latin-1. Present since the
session allowlist shipped, in both `verify_session_token()` and
`get_token_jti()`. Fixed by comparing UTF-8 bytes (`remote_auth._compare`).
Reproduced against the real app with the old comparison restored before
fixing — see the iteration entry.

### Watch for

- **`api_rate_limit.check()` must stay callable from the HTTP middleware
  only.** That single call site is the entire W2-111 guarantee that a
  running lightshow can't spend an HTTP budget. There's a test that fails if
  a second call site appears.
- Rate-limit **config** is in-memory like its counters — `SBD_RATE_LIMIT_*`
  env vars are the durable path; the API is a runtime override that a
  restart discards. This is documented but is a plausible surprise.

### What's NOT done

- Anything needing a reverse proxy in front: `X-Forwarded-For` handling
  (W2-038/W2-112) can't be verified without one, so it wasn't guessed at.
- Notifications on repeated failures (W2-053/W2-113), PIN rotation reminders
  (W2-055), "remember this device" (W2-058), TOTP (W2-059), IP
  allow/denylists and ban escalation (W2-107/108), abuse-pattern detection
  (W2-105), a persisted rate-limit store (W2-116), load testing (W2-118).
- The adversarial security-test phase (section 5) is still untouched and
  still needs a real deployed target.
## Week 2 Phase B — TLS / reverse proxy + deployment (issue #70)

Roadmap sections 3 (W2-031..050) and 10 (W2-176..195). Branched from
`master`; test suite went 353 → 395 passing.

### Backend changes

New module **`backend/reverse_proxy.py`** — everything about running behind
a TLS-terminating proxy. Configured by env var only (`SBD_TRUSTED_PROXIES`,
`SBD_HSTS*`, `SBD_HTTPS_REDIRECT`), deliberately *not* through the API: a
runtime-flippable trust setting would let one session that got in once
permanently disable brute-force protection for everyone.

- **W2-038, the important one.** `remote_auth`'s per-IP lockout and login
  rate limiter now key off the real client behind a proxy instead of the
  proxy's own address. Opt-in and default-off: `X-Forwarded-For` is
  attacker input unless a specific peer has been named, and believing it
  unconditionally would let anyone forge a fresh source IP per guess and
  delete the lockout. The chain is walked **right to left**, because both
  nginx (`$proxy_add_x_forwarded_for`) and forwarding proxies generally
  append the real peer to whatever the client sent — reading left-to-right
  is the classic bug and hands the attacker their forged value.
- **W2-036.** Session cookie gets `Secure` when the request really is
  HTTPS, and *not* otherwise. Unconditional would be worse than absent:
  browsers silently discard a `Secure` cookie over plain HTTP, so every LAN
  user's login would appear to do nothing.
- **W2-034/035.** HSTS and 307 redirect-to-HTTPS, both opt-in, both off by
  default (reasoning in `docs/deployment.md` §3.3). Health paths are exempt
  from the redirect so container probes don't 307.
- **W2-044.** `/healthz`, added to `remote_auth.OPEN_PATHS`. Returns
  `{"status":"ok"}` and nothing else — it's the endpoint most likely to be
  publicly reachable.
- `/api/system/proxy-status` (gated) — makes a proxy misconfiguration
  visible instead of silent.

### The pre-existing bug this turned up

**uvicorn ships `ProxyHeadersMiddleware` enabled by default, trusting
`127.0.0.1`.** It rewrites the request's client address from
`X-Forwarded-For` *before the app runs*. With the app bound to loopback
that matches every request, so any local process could hand itself an
arbitrary source IP and a fresh lockout bucket — and this predates Phase B;
the old `request.client.host` in `auth_login` was already reading a
substituted value. Confirmed live: before the fix, a direct `curl` with
`X-Forwarded-For: 198.51.100.42` made the app report that as `peer_ip`.

Fixed by running uvicorn with `--no-proxy-headers` (`Dockerfile`,
`deploy/systemd/`, `deploy/windows-service.md`) so this app's explicit trust
list is the only thing deciding it. `/api/system/proxy-status` reports
`peer_rewritten_by_server` to catch a start command that missed the flag.
**Anyone running their own start command needs to add it.**

### Reference artifacts (`deploy/`)

Caddy (DuckDNS + automatic Let's Encrypt, and a LAN `tls internal`
variant), nginx + certbot renewal automation, a self-signed cert
generator, `docker-compose.caddy.yml`, systemd service + health-check
timer, NSSM notes, and `deploy/smoke-test.py`. `docs/deployment.md` covers
min Python/OS versions, measured resource footprint, update/rollback, and
version pinning.

### What was actually validated vs. only written

Validated with real binaries: Caddyfiles via `caddy validate` (v2.11.4),
nginx conf via `nginx -t` (1.27) with real certs in place, systemd units
via `systemd-analyze verify` (Debian 12), compose via `docker compose
config`, the cert script executed on Linux, and the smoke test run against
a live instance — including **through a real Caddy container** proxying to
it, confirming a forged header is ignored without trust and the real client
IP comes through with it.

**Not validated:** no Let's Encrypt cert issued against a real domain (needs
a real domain + open ports), no systemd unit started on a real Linux host,
NSSM instructions not executed, and the Raspberry Pi CPU guidance is
extrapolated from desktop measurements. `deploy/README.md` carries this
same split so it doesn't get lost.

### Bugs found while building this

1. **`SBD_TRUSTED_PROXIES=*` was silently inert** — the wildcard made every
   forwarded entry look like a proxy hop to skip, so the right-to-left walk
   fell off the end and returned the peer, collapsing `*` into the no-trust
   behaviour it was set to escape. Caught by its own test. Fixed by
   splitting "may I believe this peer" from "is this entry one of my own
   proxies" (`is_trusted_proxy` vs `is_known_proxy_hop`).
2. **`StartLimitIntervalSec` was in `[Service]`** in the systemd unit, where
   systemd has silently ignored it since v229 — the crash-loop guard looked
   present and did nothing. Caught by `systemd-analyze verify`.
3. **The smoke test looked up response headers case-sensitively**, so it
   reported the app wasn't setting `Content-Type` on static assets. The app
   was fine; the script wasn't.

### What a reviewer should check by hand

- A real Let's Encrypt issuance on an actual DuckDNS domain with ports
  80/443 forwarded — the one thing no amount of local validation covers.
- Installing the systemd unit on a real Linux host, including whether the
  `ProtectSystem=strict` + `ReadWritePaths` set is actually sufficient for
  `backend/data/` and tinytuya's `snapshot.json`.
- Whether audio-reactive lighting survives the systemd sandboxing on a real
  machine (the unit documents the `PrivateDevices` / `SupplementaryGroups=audio`
  caveats, but they weren't exercised).
## Round 6 — Week 2 Phase C: audit logging, backups, secrets (2026-07-31)

Tracked in issue #71 (W2-141–160, W2-161–175, W2-211–225). Built on a
worktree branch off `master`. **481/481 backend tests pass** (353 before,
128 new); CLI suite still 22/22.

### What was built

- **`backend/security_audit.py`** — a security-events log distinct from
  both the per-device history and the existing `data/auth_audit.log`. That
  auth log is unchanged; `remote_auth.log_audit_event()` now *forwards*
  into the new one, so a future auth event can't be added to one and
  forgotten in the other. Four severities, per-event overrides, size-based
  rotation + age/count retention, JSON/CSV export, search/filter, local
  alert queue + optional (default-off) webhook, rate-based thresholds,
  canary self-test, digest.
- **Tamper-evidence**: every line is HMAC-chained to the previous one, with
  the head recorded in a separate state file. Detects an edited entry, a
  removed entry, a truncated tail, and a deleted file. **Limit, stated
  plainly:** an attacker holding both the key file and the state file can
  rebuild a consistent forged chain — that's inherent to keeping the anchor
  on the same host, and the webhook is the honest off-box upgrade path.
- **`backend/backup_restore.py`** — encrypted (AES-256-GCM, PBKDF2 200k) or
  plain zip; per-file SHA-256 manifest; integrity check before any restore
  is offered; explicit confirm flag; automatic pre-restore safety backup;
  selective restore; keep-last-N with overwrite-before-delete.
- **`backend/secrets_env.py`** — `.env` / env-var `local_key`s (never
  written back into `config.json`), the shared redaction helper, and the
  per-secret sensitivity table that `GET /api/security/secrets` serves.
- **Redaction hardening in `bulb_manager`**: every exception string that
  leaves `BulbController` is scrubbed of that device's own key first. This
  closed a real (unexploited) hole — `status()` passed raw tinytuya
  exception text straight into an API response *and* the history log, so a
  tinytuya version that echoed the key it was constructed with would have
  leaked it with nothing else in the app to catch it.
- **CI secret scan** (`.github/workflows/secret-scan.yml`) — `.gitignore`
  is a default, not a control; `git add -f` bypasses it silently.
- Dashboard: two new System tabs, **Security Log** and **Backup**.
- Docs: `docs/security-secrets.md` (secret sensitivity table, incident-
  response checklist, secure deletion, quarterly review) and
  `docs/backup-restore.md` (migration, test-your-restore).

### Verified for real (not claimed)

Against a live `uvicorn` on `127.0.0.1:8577` with a throwaway config:

- Full backup → verify → preflight → restore cycle, plain and encrypted.
  Wrong password reports the same message as a modified archive (no oracle).
- **W2-175 live**: PIN gate enabled, restored an archive taken while it was
  *disabled*, gate stayed enabled (`{"enabled_before": true,
  "enabled_after": true, "changed": false}`). Structurally guaranteed —
  nothing in `backup_restore.py` writes `remote_auth.json`.
- Tamper detection, all three shapes, against the running server: edited
  entry → `hmac mismatch at seq 4`; deleted middle entry → `broken link at
  seq 5`; wiped file → `log is empty but state records 10 entries`.
- Confirmed the plain archive's decompressed `config.json` really does
  contain the key (so the warning isn't theatre) and the encrypted one is
  not even a readable zip.
- Swept the whole live log + all responses for the test `local_key`, the
  test PIN and the backup password: none present.
- Disabling the gate produced a `critical` event and an alert.

### Known gaps / next

- **No scheduled anything.** No automatic backup timer and no periodic
  audit digest job — deliberately, rather than adding another background
  thread. `POST /api/security/self-test` and `POST /api/backups` are
  cron-shaped; `docs/backup-restore.md` shows the invocation.
- **No upload-a-backup endpoint** — that needs `python-multipart`, and a
  new dependency wasn't worth it for this pass. Restoring a file from
  elsewhere means dropping it into `backend/backups/` first.
- **`keyring` / encrypted-at-rest `config.json` (W2-212, W2-213) not
  built** — env vars cover the same threat with no new dependency.
  Discord alert integration (W2-148) is Week 3's, and the webhook is the
  seam it will plug into.
- **Pre-existing test-isolation gap, not fixed here:** the audio modules
  (`audio_reactive`, `audio_safety`, `audio_presets`) still write
  `audio_last_session.json`, `audio_safety.json` and
  `audio_session_presets.json` into the *real* `backend/data/` during a
  test run. The auth audit log had the same problem and is fixed
  (`conftest.auth_reset` now redirects `AUDIT_LOG_PATH` too); the audio
  ones need the same treatment and are out of this phase's scope.
- **Pre-existing latent deadlock, fixed in passing:** `config.load_config()`
  called `save_config()` while already holding a non-reentrant `_lock`.
  Only reachable when *both* `config.json` and `config.example.json` are
  missing, which never happens in a checkout — now writes directly instead.
- The browser UI for both new tabs has not been through a real browser
  pass; the JS parses and the assets serve, but a human should click it.
## Round 6 — Week 2 Phase D: observability, network resilience, security docs (2026-07-31)

Tracking issue `THEROCKSSS/smart-bulb-dashboard#72`. Full write-up in
`iterations/005-observability-network-resilience/README.md`.

**Three new backend modules**, deliberately standalone (a broken audio
stack must not take the observability layer down with it):

- `backend/observability.py` — `/metrics` in Prometheus text format,
  per-endpoint p50/p95/p99 latency and error rates, request correlation
  IDs, a level-configurable in-memory log buffer the UI can tail, startup
  dependency probes, and the secrets-redacted self-diagnostic report.
- `backend/network_health.py` — host-IP change detection, connectivity
  loss/regain with an automatic bulb-reconnect hook, per-bulb latency
  history over time, and `full`/`lan_only`/`tailscale_only`/`offline`
  connectivity modes.
- `backend/remote_access_status.py` — public IP (opt-in lookup only),
  DuckDNS last-sync, Tailscale status + tailnet URL, and the exposure
  warning banner including the persistent fail-safe.

**New docs:** `SECURITY.md` (disclosure process, no-telemetry guarantee,
real `pip-audit` findings), `docs/pin-gate-threat-model.md` (formal, blunt
about the ten things it doesn't defend), `docs/observability.md`.

**Tests:** 353 → **472 passing**. New `conftest.py` `observability_reset`
fixture isolates all three new state files at a tmp path — the first run
had been reading/writing the real `backend/data/`.

### Real bugs found

1. **Pre-existing, user-visible:** `frontend/app.js` `bootDashboard()`
   referenced a `ROUTES` map that stopped existing when the eleven panels
   were consolidated into five `PAGES`. On a **first visit with no `#` in
   the URL** it threw a `ReferenceError` before `router()` ran, so the
   dashboard rendered nothing at all. Fixed.
2. My own: route-template resolution collapsed
   `/api/devices/{id}/power` onto `/api/devices` via a prefix fallback
   meant for static mounts. Caught by its own test; fallback removed.

### What a reviewer must check by hand

- **The frontend has not been opened in a browser.** It was verified at
  the data-contract level only (every endpoint the new UI calls returns
  200 with the expected shape) plus `node --check`. The Health tab, log
  viewer, latency tables, Tailscale card and exposure banner need visual
  confirmation.
- ~~The `pip-audit` findings in `SECURITY.md` are **not fixed**~~ — done
  in #74: `fastapi==0.141.1` + a direct `starlette==1.3.1` pin, all seven
  advisories closed, `pip-audit` clean, 743 tests still green.
- The tailnet URL is built with port **8500** unless `SBD_PORT` is set.

### Known gap, pre-existing

The *existing* test suite still writes `backend/data/audio_safety.json`,
`audio_last_session.json`, `audio_session_presets.json` and
`auth_audit.log` on the real install. Same class of problem the new
`observability_reset` fixture solves; out of scope for this phase.

## Repo

Pushed to Forgejo: `agentsoul/smart-bulb-dashboard` (see commit log for
history). `backend/config.json` (real device credentials) is git-ignored
and was never committed — `config.example.json` is the template that ships
instead.

Also mirrored to **GitHub** (`THEROCKSSS/smart-bulb-dashboard`, public) under
the **Polyform Noncommercial License 1.0.0** — personal/contribution use is
free; commercial use requires a separate license from the author. A real
bulb `device_id` and LAN IP that were briefly committed here were scrubbed
before the GitHub push (no `local_key` was ever committed).
