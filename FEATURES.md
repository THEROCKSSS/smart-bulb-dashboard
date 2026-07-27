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

---

**Total: 97 working features**, verified end-to-end against a real Bytech
A19 Wi-Fi RGB+CCT bulb (Tuya protocol v3.5) — see the verification log in
`HANDOFF.md` for the actual commands run and their results, including two
real bugs found and fixed during testing (brightness mode-flip, partial
status merging).
