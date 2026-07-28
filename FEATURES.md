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

## Quick actions (2)
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

## Timers (7)
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

---

**Total: 137 working features**, verified end-to-end against a real Bytech
A19 Wi-Fi RGB+CCT bulb (Tuya protocol v3.5) and this machine's real audio
devices (VoiceMeeter + physical microphone) — see the verification log in
`HANDOFF.md`, and `iterations/001` through `iterations/004` for each new
feature area's actual test results, including five real bugs found and
fixed across these rounds of testing (an IP-change log field bug, a
brightness-floor bug, the audio/bulb-I/O blocking bug, a circular hue
smoothing bug, and a root-page auth lockout bug).
