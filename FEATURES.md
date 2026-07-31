# Feature List

This counts every distinct, **working** capability in the prototype — including
each individual preset color, scene, and effect as its own line, which is how
most smart-home apps report their feature count. Everything below is real
code that was tested against an actual bulb (see `HANDOFF.md` for the
verification log), not a roadmap promise. Roadmap-only items (not built yet)
are listed separately in `ROADMAP.md`.

## Core control (6)
1. Power on
2. Power off
3. Power toggle
4. Mode-aware brightness control (dims the *current* color in colour mode instead of silently flipping to white mode — see `bulb_manager.py` `set_brightness()`)
5. Direct RGB color control
6. Direct HSV color control (hue/saturation/value sliders)

## White mode (2)
7. Switch to white mode
8. Color temperature control (warm ↔ cool, 0–100%)

## Quick actions (3)
9. Random color jump
10. Identify (blinks the physical bulb 3x so you can find it)
11. Flash alert (rapid flash in a chosen color, e.g. for notifications)

## Built-in color presets (25)
12–36. Red, Orange, Amber, Yellow, Lime, Green, Emerald, Teal, Cyan, Sky Blue,
Blue, Indigo, Violet, Purple, Magenta, Pink, Rose, Hot Pink, Coral, Gold,
Mint, Turquoise, Lavender, Warm White, Cool White

## Favorites (4)
37. Save current color as a named favorite
38. List saved favorites
39. Apply a favorite
40. Delete a favorite (right-click on the swatch)

## Scenes — one-tap mood presets (15)
41–55. Movie Night, Reading, Party, Sunset, Ocean, Forest, Romantic,
Focus/Work, Relax, Night Light, Wake Up, Holiday Red, Halloween, Valentine,
Gaming

## Effects — animated, run until stopped (7 effects + 2 controls = 9)
56. Rainbow Cycle
57. Pulse / Breathe
58. Strobe
59. Candle Flicker
60. Two-Color Fade
61. Preset Color Loop
62. Random Color Jump (continuous)
63. Adjustable effect speed
64. Stop current effect

## Timers (9)
65–68. Sleep timer quick presets (5 / 15 / 30 / 60 minutes)
69. Custom sleep timer duration
70. Sleep timer gradual fade-out in the final 20% of its duration (doesn't just snap off)
71. Cancel active sleep timer
72. Wake/sunrise timer — configurable target time, brightness, color temp, fade-in duration
73. Cancel active wake timer

## Recurring schedule engine (4)
74. Add a recurring time-of-day rule (daily)
75. Rule actions: power on, power off, apply scene, or apply preset
76. Delete a rule
77. Backend scheduler ticks every 20s and fires rules exactly once per matching minute

## Groups — multi-bulb ready (3)
78. Group power on/off (broadcasts to every device ID in the group)
79. Group color (broadcasts RGB to every device in the group)
80. Config-driven group membership — add a 2nd/3rd bulb to `config.json` and it's automatically groupable, no code changes needed

## History & audit (1)
81. Per-device action history log (last 200 actions, timestamped, success/fail)

## Diagnostics (3)
82. Connection test (TCP reachability + latency + live status round-trip)
83. Network rescan — re-discovers the bulb's IP via UDP broadcast if it changes (DHCP lease renewal, router reboot, etc.)
84. System info panel (version, uptime, preset/scene/effect counts)

## Device management (4)
85. Add a new device via the Settings UI (no code/restart needed)
86. Remove a device
87. Edit/rename a device
88. Local keys are redacted (`••••••`) in every API response and UI view once saved — never re-displayed in plaintext

## Dashboard UX (6)
89. Dark theme by default (no pure black — tinted dark palette)
90. Fully responsive layout — desktop layout untouched, mobile gets dedicated overrides
91. Toast notifications for every action (success/error)
92. Bookmarkable, hash-routed tabs (`#/scenes`, `#/effects`, etc. — refresh-safe)
93. Live status badge with debounced offline detection (requires 2 consecutive failed polls before showing OFFLINE, so one transient Wi-Fi blip doesn't flash a false alarm)
94. "LIVE DATA" labeling on all real-time device data, per this project's data-labeling convention

## API & deployment (3)
95. Full REST API (49 routes) usable independently of the dashboard UI — see `API.md`
96. CORS enabled so the API can be driven from other tools/scripts
97. Dockerized (`Dockerfile` + `docker-compose.yml`)

## Audio-reactive lighting (16)
98. Audio input device listing — auto-enumerates VoiceMeeter/CABLE virtual devices and real microphones
99. Live band-level meter in the UI (bass/mid/treble/RMS, updating ~3x/sec)
100. Real-time beat detection (rolling bass-average threshold)
101–108. Eight selectable interpretation modes: Band → Color, Dominant Band, Spectral Blend, VU Meter, Auto-Rotate Hue, Monochrome Pulse, Strobe on Drop, Palette Cycle (see `docs/music-reactive-lighting.md`)
109. Adjustable sensitivity (0.2x–3.0x multiplier)
110. Adjustable monochrome/VU hue picker
111. Start/stop session controls, independent per bulb
112. Auto-timeout after 5 minutes of near-silence (won't run all night against nothing)
113. Non-blocking bulb dispatch — a dedicated sender thread means a slow or offline bulb can never freeze the audio analysis loop (a real bug found and fixed during testing; see `iterations/002-audio-reactive-lighting/`)

## Network auto-discovery (8)
114. Manual "Scan Now" trigger in Settings
115. Scheduled automatic scanning, configurable Daily/Weekly/Monthly
116. Discovered-device list showing device ID, IP, protocol version, first seen
117. One-click pre-fill of the Add Device form from a discovered device
118. Ignore a discovered non-bulb device so it stops reappearing
119. Unignore a previously-ignored device
120. Automatic IP-change detection and `config.json` update for already-configured devices (e.g. after a DHCP lease renewal)
121. Scan-trigger API endpoint usable independently of the UI

## Audio engine v2 — lower latency, more modes, orchestration (12)
122. Sub-15ms internal decision latency (512-sample capture blocks, zero-padded FFT), decoupled from the actual bulb send rate
123. Configurable minimum "dwell" time — how long each color stays visible before the next one can replace it, independent of how fast the analysis reacts
124–127. Four new modes: Spectrum Gradient (continuous N-band hue), Band Flash Overlay (ambient gradient + per-band accent flashes), Stereo Split (hue driven by L/R channel balance), Breathing Silence (ambient breathing brightness during quiet passages)
128. Configurable band count (3-16) for gradient-based modes
129. Per-bulb send latency + last-error reporting in live session status
130–132. Three multi-bulb orchestration role modes: Unison, Phase Offset (chase effect), Band Split (one bulb per frequency band)
133. Group audio-reactive session API + UI, sharing one audio analysis across every bulb in a group

## Remote-access security (4)
134. PIN-gate toggle for exposing the dashboard beyond the LAN (Settings → Remote Access)
135. Brute-force lockout — 5 wrong attempts from one client locks it out for 5 minutes, including subsequently-correct PINs
136. Session expiry enforced server-side via a signed token, not just relying on cookie expiration
137. PIN stored as a salted PBKDF2-SHA256 hash — never in plaintext

## Audio engine v2 additions — v0.3.0 (2)
138. `harmonic_pairs` mode — the two most-energetic non-adjacent bands mapped to complementary hues, fixed-direction blend (a real flicker bug was found and fixed here: the shortest-arc blend cancels to `(0,0)` at a 50/50 energy split between anchors exactly 180° apart)
139. `kick_snare_split` mode — bass-band energy drives brightness, a mid/high band drives a hue accent

## Remote-access security additions — v0.3.0 (4)
140. Session listing — view every currently-active session (issued time, last-seen, IP), never exposing the raw token
141. Single-session revocation
142. Revoke-all-sessions (logs everyone out, rotates the signing secret)
143. Audit log of every auth event (login success/failure, lockout, revocation) — never records the PIN or a raw token
144. Per-IP login rate limiting, independent of and tripping ahead of the existing lockout counter

## CLI — `bulbctl` (4)
145. Core device commands: `list`/`on`/`off`/`toggle`/`color`/`brightness`/`scene`/`status`, stdlib-only (no new dependency)
146. `login`/`logout`/`auth-status` — PIN-gate session handling from the CLI, session cookie stored locally
147. Shell completions for bash, zsh, and PowerShell
148. Scripting examples (`cli/examples/`) for cron-driven sunrise/movie-night automation

## Analytics (1)
149. `GET /api/analytics/usage` — real per-device on-time for a given period, derived from logged history (no fabricated wattage — this bulb has no real power-draw data)

## Developer tooling (1)
150. A real backend pytest suite (503 tests: 481 backend + 22 CLI), mocked Tuya hardware layer — no test touches real hardware or `config.json`

## Dashboard UX additions — v0.3.0 (9)
151. Sleep-timer countdown ticks live, client-side, every second — no more refresh-to-see
152. Wake-timer countdown ticks live too, recomputed from the real target time each second
153. Device status badge shows a live-ticking "last seen Xs ago"
154. Remembers your last device + panel across reloads (`localStorage`)
155. Keyboard shortcuts on the Control panel — Space toggles power, Up/Down arrows nudge brightness ±5%
156. Copy-to-clipboard on Diagnostics' device ID and IP fields
157. Undo action on the cancel-timer toast — actually recreates the cancelled timer with its original parameters, not just a cosmetic dismissal
158. Rounder, softer visual pass — 8px → 12px corner radius, real card elevation, smoother hover states
159. Real mobile-friendliness fix — the sidebar was collapsing to a ~16px sliver on phones (an unreset grid-column span), plus 44px minimum tap targets throughout

## Security audit log & alerting — Week 2 Phase C (10)
160. Dedicated security-events log, separate from both the per-device action history and the existing auth-only audit trail (which is unchanged and now forwards into it)
161. Four configurable severity levels with per-event overrides, defaulted so ordinary daily use never crosses the alert threshold
162. Tamper-evident HMAC chain + separately-recorded head — detects an edited entry, a removed entry, a truncated tail, and a deleted log file (all four exercised against a running server)
163. Size-based rotation that keeps the chain continuous, plus age- and count-based retention that can never delete the current segment
164. Audit log search/filter UI in the dashboard (text, event type, minimum severity, rotated segments), with actionable entries visually distinguished from informational ones
165. Export to JSON or CSV, JSON keeping the chain fields so an export stays independently verifiable
166. Real-time alert when the PIN gate is disabled (`critical`) and when a new device appears in `config.json` (`warning`)
167. Rate-based alert thresholds (default: 3 failed logins in 5 minutes) that aggregate a burst into one alert rather than N
168. Local-only alert queue with browser notifications, and an optional outbound webhook that is off by default
169. Change-tracking for `config.json` — every write logged by SHA-256 fingerprint, so a change is provable without any credential reaching the log

## Backup & restore — Week 2 Phase C (7)
170. One-click full backup of config + runtime data, downloadable, with a per-file SHA-256 manifest
171. AES-256-GCM encrypted backup option, with the plaintext-`local_key` tradeoff surfaced in the UI as a required acknowledgement rather than buried in the docs
172. Integrity check before any restore is offered (zip CRCs plus every file's checksum against the manifest)
173. Restore behind an explicit overwrite confirmation, with an automatic pre-restore safety backup
174. Selective restore (favorites / schedules / audio / lightshows / discovery / groups+zones) that provably never touches device credentials
175. Backup versioning — keep the last N, older ones overwritten with random bytes before deletion
176. A restore can never turn remote access on or off — structural (nothing writes `remote_auth.json`), reported in the result, and pinned by a test in both directions

## Secrets management — Week 2 Phase C (5)
177. Environment-variable / `.env` support for device `local_key`s, with a documented `.env.example`, and an env-sourced key that is never written back into `config.json`
178. Systematic redaction audit — a test that walks the real route table, so any future endpoint leaking a `local_key` fails the suite without anyone remembering to add it
179. Exception-text scrubbing at the device layer, so a `local_key` cannot reach an API response or the history log via third-party error text
180. Secret-scanning CI check — fails the build if `backend/config.json`, `backend/data/`, `.env` or a real-looking key is ever committed, beyond what `.gitignore` can enforce
181. Documented sensitivity, storage location and rotation procedure for each of the four secrets, served live at `GET /api/security/secrets` so the docs can't drift from the code

---

**Total: 181 working features**, verified end-to-end against a real Bytech
A19 Wi-Fi RGB+CCT bulb (Tuya protocol v3.5) and this machine's real audio
devices (VoiceMeeter + physical microphone) — see the verification log in
`HANDOFF.md`, and `iterations/001` through `iterations/004` for each new
feature area's actual test results, including five real bugs found and
fixed across these rounds of testing (an IP-change log field bug, a
brightness-floor bug, the audio/bulb-I/O blocking bug, a circular hue
smoothing bug, and a root-page auth lockout bug).
