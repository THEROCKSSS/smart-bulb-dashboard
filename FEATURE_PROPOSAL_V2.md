# Feature Proposal v2 — For Review (Nothing Below Is Built Yet)

This is an idea list, not a build log. Every item is numbered so you can
approve/reject/edit by number (e.g. "approve 1-40, drop 52, change 88 to use
X instead"). Once you've marked this up, hand it back and I'll turn the
approved subset into an actual `ROADMAP.md` update and start building.

Three sections, matching what you asked for:
- **A. Audio-Reactive Lighting** — the "audio changes light color, bass is a
  different color, multiple modes" ask.
- **B. Auto Network Discovery** — weekly auto-scan + manual "Scan Now" in
  Settings.
- **C. Everything else** — the broader "couple hundred features and ideas"
  ask, covering areas the dashboard doesn't touch yet.

Feasibility tags used throughout:
- **[easy]** — builds on code that already exists (`bulb_manager.py` effects,
  `config.json`, existing API patterns). A few hours each.
- **[moderate]** — needs a new dependency or subsystem but no new hardware.
- **[hardware]** — needs equipment you don't have yet (extra bulbs, BLE, a
  smart plug, a mic if you don't already have one hooked up).
- **[external]** — depends on a third-party service/account (Alexa, Home
  Assistant, etc.) beyond this LAN.

---

## A. Audio-Reactive Lighting

### A1. Input sources
1. **[moderate]** VoiceMeeter virtual-cable input (matches how you already
   route audio) — capture `VoiceMeeter Output` as the "microphone."
2. **[moderate]** Native WASAPI loopback capture (no VoiceMeeter needed) —
   captures whatever's playing on the PC directly.
3. **[moderate]** Real microphone input (ambient/room listening) — works with
   any speaker source, including someone else's music or a TV.
4. **[easy]** Settings dropdown listing all available audio input devices by
   name (`sounddevice.query_devices()`), so you pick the source visually
   instead of hardcoding an index.
5. **[moderate]** Auto-detect and pre-select a VoiceMeeter device if one is
   installed, falling back to default mic otherwise.
6. **[easy]** "Test input" button in Settings — shows a live VU meter so you
   can confirm the right device is selected before starting a session.
7. **[moderate]** Multi-source blending — average two inputs (e.g. mic +
   system audio) for hybrid ambient/PC-audio reactions.
8. **[easy]** Manual gain/sensitivity slider (in case a source is too quiet
   or clips).
9. **[moderate]** Auto-gain — normalizes volume swings so quiet passages
   still register instead of just going dark.
10. **[easy]** Silence-detection fallback — if no audio input is detected for
    N seconds, gracefully fall back to a slow ambient fade instead of sitting
    at black.

### A2. Analysis / signal-processing modes
11. **[moderate]** 3-band split (bass/mid/treble) — the baseline design from
    `music-reactive-lighting.md`.
12. **[moderate]** Fine-grained N-band split (e.g. 8-16 bands) for more
    nuanced color blending instead of 3 buckets.
13. **[moderate]** Overall RMS/volume-only mode (brightness pulses with
    loudness, color stays fixed) — simplest, cheapest mode, good default.
14. **[moderate]** Beat/onset detection (bass energy spike vs rolling
    average) driving a brightness "pulse" per beat.
15. **[moderate]** Tempo/BPM estimation from beat intervals, used to pace
    effects (e.g. a color-loop that completes one cycle per N beats instead
    of a fixed timer).
16. **[moderate]** Spectral centroid tracking ("brightness" of the sound
    itself, independent of loudness) mapped to color temperature — bright/
    treble-heavy passages skew cooler, warm bass-heavy passages skew warmer.
17. **[moderate]** Peak-hold / decay envelope (value rises instantly on a
    hit, decays smoothly) instead of raw per-frame energy, so it reads as
    "punchy" rather than jittery.
18. **[easy]** Adjustable smoothing/attack-decay time constants exposed as a
    slider ("snappy" vs "smooth" reaction feel).
19. **[moderate]** Silence/quiet-passage detection that dims instead of
    holding the last loud color.
20. **[hardware-adjacent, moderate]** Stereo-aware analysis (if input device
    supports 2ch) — left/right channel balance could drive a secondary
    parameter (e.g. hue drift direction) for stereo-mixed music.

### A3. Color-mapping modes (the "bass has a different color" ask)
21. **[moderate]** Fixed band-to-hue mapping — bass=red/orange, mid=green/
    yellow, treble=blue/violet (the concrete version of what you described).
22. **[moderate]** Dominant-band mode — whichever band has the most energy
    *right now* decides the hue, blending smoothly between them rather than
    hard-cutting.
23. **[moderate]** Weighted-blend mode — hue is a continuous weighted average
    across all bands' energy (no discrete jumps, always some blend).
24. **[moderate]** Auto-rotating hue mode — hue slowly cycles over time
    regardless of content, brightness/pulses still track the audio (a "less
    literal, more ambient" option).
25. **[moderate]** Genre/energy-profile presets — "EDM/Party" (fast, high
    contrast, wide hue swings), "Chill/Ambient" (slow drift, narrow hue
    range, gentle brightness), "Rock/Live" (bass-forward warm palette),
    "Classical/Acoustic" (subtle, brightness-only, minimal hue movement).
26. **[moderate]** User-defined band→hue mapping (let the user pick which
    hue each band maps to instead of hardcoded reds/blues).
27. **[moderate]** Complementary-pair mode — bass and treble mapped to
    color-wheel opposites for high visual contrast.
28. **[moderate]** Monochrome-pulse mode — hue locked to a single
    user-chosen color, only brightness/saturation react (good for parties
    where you want "red room that pulses" rather than a rainbow).
29. **[moderate]** Saturation-reactive mode — loud/energetic passages push
    saturation toward 100, quiet passages desaturate toward white/pastel.
30. **[moderate]** VU-meter mode — brightness is a direct, linear mapping of
    current volume, nothing fancier (useful as a diagnostic/baseline and as
    its own aesthetic).
31. **[moderate]** "Karaoke/vocal" mode — bandpass-isolate the vocal range
    (~300Hz-3kHz) and react primarily to that band, deprioritizing bass/
    treble.
32. **[moderate]** Strobe-on-drop mode — combine beat detection with a
    threshold so only *hard* hits (well above rolling average) trigger a
    full white flash, for drops/hits rather than every beat.
33. **[moderate]** Color-history trail mode — briefly blend in the last 1-2
    colors at reduced weight for a smoother, less flickery transition style.
34. **[moderate]** Reverse/inverse mode — bulb goes *dark* on loud hits and
    brightens in quiet passages (novelty mode, cheap to add once the
    pipeline exists).
35. **[moderate]** Fixed palette-cycling mode — instead of full hue-wheel
    freedom, cycle only through a curated palette (e.g. the existing 25
    presets) so results always look "designed" rather than raw-hue ugly.

### A4. Multi-bulb / zone audio features (ready once you have 2+ bulbs)
36. **[hardware]** Per-bulb band assignment — bulb 1 reacts to bass, bulb 2
    to mid, bulb 3 to treble, for a simple "spread across the room" effect.
37. **[hardware]** Synchronized mode — all bulbs show the identical
    color/brightness in lockstep.
38. **[hardware]** Phase-offset mode — same effect across bulbs but with a
    time/hue offset per bulb (chase-light feel).
39. **[hardware]** Stereo L/R mapping — if 2 bulbs, map left-channel energy
    to one, right-channel to the other.
40. **[hardware]** Group-scoped audio sessions — start a music-reactive
    session against a specific room/group instead of always "all bulbs."

### A5. Session controls & UX
41. **[easy]** Start/Stop music-reactive session button (reuses the existing
    effect start/stop pattern already in `bulb_manager.py`).
42. **[easy]** Live preview panel — on-screen bar/spectrum visualizer next to
    the controls so you can see what the bulb is "seeing" before/while it
    reacts (useful for tuning without staring at the physical bulb).
43. **[easy]** Sensitivity/threshold sliders exposed live (adjust while the
    session is running, not just before starting).
44. **[moderate]** Auto-timeout — session automatically stops after N minutes
    of silence (avoids a bulb stuck "listening" to nothing all night).
45. **[easy]** "Freeze current look" button — locks the bulb at its current
    reactive color/brightness and exits reactive mode with one click.
46. **[easy]** Save a tuned session (mode + sensitivity + palette choice) as
    a named preset, same pattern as the existing Favorites feature.
47. **[moderate]** Per-mode default palettes bundled in, so a first-time user
    gets something good-looking with zero tuning.
48. **[moderate]** Manual "band boost" — a listener can nudge bass/mid/treble
    weighting live via three small sliders, in case a mix is bass-shy or
    treble-harsh for their room.
49. **[easy]** Warn/disable audio-reactive mode automatically if no valid
    input device is configured, with a clear message pointing at Settings
    instead of silently failing.
50. **[moderate]** History logging of music-reactive sessions (start/stop
    time, mode used, source device) in the same History tab that already
    tracks other actions.

### A6. Standalone "music player" angle (from your mic/VoiceMeeter mention)
51. **[moderate]** Minimal built-in audio player (local file / playlist)
    inside the dashboard itself, purely so there's a one-click "play + react"
    flow without needing a separate media player app running.
52. **[moderate]** "Now reacting to: <device name>" indicator so it's always
    clear whether the dashboard is listening to VoiceMeeter, a mic, or
    nothing.
53. **[external]** Optional Spotify/local-player "now playing" metadata
    pull (song/artist name shown next to the reactive visualizer) — cosmetic
    only, doesn't affect the light logic itself.
54. **[moderate]** Manual "tap tempo" button as a fallback when auto-BPM
    detection is unreliable (tap along, dashboard uses that tempo instead).

---

## B. Auto Network Discovery

### B1. Scanning mechanics
55. **[easy]** Manual "Scan Now" button in Settings — runs a `tinytuya`-style
    UDP broadcast discovery scan against the LAN on demand.
56. **[moderate]** Scheduled weekly auto-scan (background thread, same
    pattern as the existing schedule engine) — runs quietly, no user action
    needed.
57. **[easy]** Configurable scan interval in Settings (daily/weekly/monthly/
    off), not hardcoded to exactly one week.
58. **[easy]** "Last scanned: <timestamp>" shown in Settings so it's always
    clear when discovery last ran.
59. **[moderate]** Scan progress indicator (some discovery methods take
    several seconds) instead of a frozen-looking button.
60. **[easy]** Scan-now available via API too (`POST /api/system/scan`), not
    only the UI button, so it can be triggered externally (e.g. cron, a
    future voice command).

### B2. New-device handling
61. **[moderate]** New devices found during a scan are listed as
    "discovered, unconfigured" rather than silently ignored.
62. **[moderate]** Per-discovered-device info shown: IP, device ID, protocol
    version guess, and whether it responds on port 6668 (Tuya local control
    port).
63. **[easy]** One-click "Add to dashboard" from the discovered list, which
    pre-fills device ID/IP/version into the add-device form (still requires
    `local_key`, since that's never broadcast).
64. **[moderate]** Inline shortcut into the existing cloud-assisted QR-login
    flow (SETUP.md Option B) directly from the discovered-device row, to
    grab the missing `local_key` without leaving the page.
65. **[easy]** "Ignore this device" action for non-bulb Tuya devices found on
    the network (e.g. a smart plug) so they stop showing up in the
    discovered list every scan.
66. **[easy]** Notification badge/banner when a new device is found by the
    background weekly scan (so you don't have to remember to check).
67. **[moderate]** De-duplication — a device already in `config.json` should
    never reappear as "new" even if its IP changed (match on device ID, not
    IP).
68. **[moderate]** IP-change detection for *existing* configured devices —
    if a known device's IP moved (DHCP lease renewal), auto-update
    `config.json` instead of the dashboard silently losing contact with it.

### B3. Device classification & safety
69. **[moderate]** Best-effort device-type guess from Tuya's product
    category field (`dj` = lighting, seen in your own device info) so
    discovered non-bulb Tuya devices are labeled sensibly instead of assumed
    to be bulbs.
70. **[easy]** Explicit statement in the UI that discovery only touches your
    own LAN (UDP broadcast on the local subnet) — no internet/cloud calls
    involved, for your own peace of mind about scope.
71. **[moderate]** Scan results capped/rate-limited so a weekly background
    scan can't accidentally hammer the network if something's misbehaving.
72. **[easy]** Manual "forget device" action separate from "ignore" — fully
    removes a previously-added bulb from `config.json` (e.g. bulb given
    away/broken), vs. "ignore" which just hides a non-bulb device from
    future scan results.

### B4. Settings UI
73. **[easy]** New "Discovery" section within the existing Settings tab:
    Scan Now button, interval dropdown, last-scan timestamp, discovered
    device list, ignored device list.
74. **[easy]** Discovered-device list refreshes live during a running scan
    instead of only after it completes.

---

## C. Everything Else (broader feature/idea backlog)

### C1. Scenes, effects & presets expansion
75. **[easy]** Additional built-in scenes: "Gaming," "Study," "Thunderstorm"
    (slow blue-white flicker), "Fireplace" (warm flicker, distinct from the
    existing Candle effect), "Aquarium" (slow blue/teal drift).
76. **[easy]** Additional built-in effects: "Breathing" (slow sine-wave
    brightness, distinct from the sharper existing Pulse), "Twinkle"
    (irregular brief brightness flickers), "Storm" (irregular white flashes
    at random intervals).
77. **[moderate]** User-created custom scenes — save any color+brightness+
    mode combination under a custom name, browsable alongside the built-in
    15.
78. **[moderate]** Scene/effect scheduling — "run Movie Night scene every
    Friday 8pm" as its own schedule-rule type, reusing the existing schedule
    engine.
79. **[easy]** Effect speed presets (slow/normal/fast) as one-click buttons
    instead of only a raw speed multiplier field.
80. **[moderate]** Transition duration control for scene/preset changes
    (instant snap vs. a smooth N-second crossfade).
81. **[easy]** "Preview before apply" — hover/tap-hold a preset swatch to see
    it live on the bulb, release to revert if you don't commit.
82. **[moderate]** Preset/scene import-export as JSON files, so a curated set
    can be shared or backed up outside `config.json`.
83. **[moderate]** Seasonal auto-scene suggestions (e.g. a banner suggesting
    "Halloween" scene appears automatically in late October) — cosmetic
    convenience, fully optional/dismissible.

### C2. Automation & scheduling
84. **[easy]** Sunrise/sunset-based schedule triggers (using lat/long or a
    simple sunrise-sunset API) instead of only fixed clock times.
85. **[moderate]** Conditional rules — "only run this schedule if the bulb is
    currently off" / "only if a specific scene isn't already active," to
    avoid schedule rules stepping on manual control.
86. **[moderate]** "Vacation mode" — randomized on/off + scene changes across
    a time window to simulate occupancy.
87. **[easy]** Per-schedule-rule enable/disable toggle without deleting the
    rule.
88. **[moderate]** Schedule conflict detection/warning (two rules targeting
    the same bulb at overlapping times).
89. **[easy]** "Snooze schedule" — temporarily suspend all schedule rules for
    N hours (e.g. during a party) without editing/deleting them.
90. **[moderate]** Geofencing-style "arrive home" trigger, if a phone-presence
    signal is available on the LAN (ARP-based presence detection for a known
    phone MAC as a simple, no-cloud version).

### C3. Groups, rooms & multi-bulb (ready architecture, mostly [hardware])
91. **[hardware]** "Room" concept above groups (Bedroom, Living Room) with
    per-room default scenes.
92. **[hardware]** Synchronized rainbow/chase effects across a group, with
    per-bulb hue offset (mentioned in the existing ROADMAP.md, listing here
    for completeness/numbering).
93. **[hardware]** Grid/room-map visual layout in the UI instead of a flat
    dropdown of bulbs, once there are enough bulbs to make a list unwieldy.
94. **[hardware]** Group-level brightness/color offset ("all bulbs match hue
    but bulb 2 is dimmer") instead of forcing identical settings.
95. **[hardware]** Per-room audio-reactive assignment tying into section A4
    above.

### C4. Voice / remote / external control
96. **[external]** Home Assistant integration via a simple REST/MQTT bridge
    exposing this dashboard's API as HA entities (piggybacking on the same
    `tuya_sharing` credentials already in place).
97. **[external]** Amazon Alexa / Google Home bridging (already flagged as
    future work in ROADMAP.md).
98. **[moderate]** Simple webhook endpoints for any-color/any-scene trigger,
    so other local automations (a script, a Home Assistant automation, a
    cron job) can drive the bulb without needing the dashboard UI at all.
99. **[moderate]** A tiny CLI tool (`bulbctl`) wrapping the REST API for
    quick terminal-based control, useful for scripting/testing without curl.
100. **[external]** Optional Discord bot integration (relevant since this
     workspace already has a Discord bot example) — e.g. `!bulb red` from a
     Discord channel.

### C5. Mobile / UI / UX
101. **[easy]** Add-to-homescreen PWA manifest so the mobile-responsive
     dashboard installs like a native app icon.
102. **[easy]** Dark/light theme toggle (dashboard is dark-only today) even
     though dark is the house default.
103. **[moderate]** Color-picker "wheel" widget as an alternative to the
     existing hue/sat/val sliders, for users who think in a color wheel
     rather than three sliders.
104. **[easy]** Recently-used colors strip (last 8 colors used, one-tap
     re-apply) distinct from the curated Favorites list.
105. **[easy]** Keyboard shortcuts for power toggle / next preset / next
     scene on the desktop UI.
106. **[moderate]** Multi-language UI strings (i18n scaffold), even if
     English-only content ships first.
107. **[easy]** Compact "widget" view (just power + brightness + color, no
     tabs) for a secondary always-on browser tab/kiosk display.
108. **[moderate]** Undo/redo for the last N manual color changes.

### C6. History, analytics & diagnostics
109. **[easy]** Usage stats: total on-time today/week, most-used scene, most-
     used preset color.
110. **[moderate]** Uptime/reliability graph — track how often
     `test-connection` fails over time, surfacing the "this bulb drops
     Wi-Fi periodically" reality visually instead of anecdotally.
111. **[easy]** Exportable history log (CSV/JSON) for anyone who wants to
     poke at their own usage data externally.
112. **[moderate]** Alert if a bulb hasn't responded in X minutes (banner/
     notification), instead of only showing OFFLINE passively on the
     Control tab.
113. **[hardware]** Power/energy estimate — noted in ROADMAP.md as needing a
     power-monitoring smart plug since this bulb model doesn't expose real
     draw; listing here again for numbering completeness.

### C7. Security & access
114. **[moderate]** Optional dashboard login (single shared password) for
     households where the LAN isn't fully trusted (roommates, guests on a
     guest Wi-Fi VLAN).
115. **[moderate]** Read-only "guest" mode/link — lets someone view status
     and toggle power/brightness but not touch schedules or device config.
116. **[easy]** Config backup/restore — one-click download of
     `config.json` + `data/` (favorites/schedules) as a zip, and a restore
     upload, for reinstalling the dashboard on a new machine.
117. **[moderate]** Local key rotation helper — walks back through the
     cloud-assisted login flow to refresh a `local_key` if it ever needs
     resetting (e.g. bulb was reset/re-paired in the Tuya app itself).

### C8. Bluetooth (already deferred in ROADMAP.md — cross-referenced, not new work)
118. **[hardware]** `BLEBulbController` implementing the existing
     transport-agnostic controller interface (sketch already in
     ROADMAP.md/HANDOFF.md) — no new design needed, just hardware + testing
     time once purchased.
119. **[hardware]** BLE mesh group support if the eventual hardware is a
     mesh-capable product line rather than single BLE bulbs.

### C9. Fun / novelty
120. **[easy]** "Panic button" — instantly kills any running effect/session
     and returns to a plain, safe warm-white (useful mid-party if something
     looks wrong or is too intense/flashy).
121. **[moderate]** Weather-reactive ambient mode (color shifts slowly based
     on local weather conditions via a simple weather API — cloudy=cool/
     grey-blue, sunny=warm/gold).
122. **[moderate]** Countdown/celebration mode — pick a target time (e.g.
     midnight), bulb does a slow color build-up and flashes at T-0.
123. **[easy]** "Surprise me" button — applies a random scene + effect
     combo instead of only a random color.
124. **[moderate]** Doorbell/notification-flash integration — if a webhook
     fires (e.g. from a doorbell camera's own automation), flash a specific
     color pattern as a visual notification.

---

## Notes on scope

- Sections A and B are the two things you explicitly asked for this round —
  everything in C is the "couple hundred ideas" backlog for you to skim,
  not a claim that all of it should get built.
- Nothing above touches Bluetooth hardware you don't own yet, or assumes new
  bulbs beyond the one you have — hardware-tagged items are listed for
  completeness/numbering, not proposed as next work.
- None of this has been implemented. `ROADMAP.md` stays the source of truth
  for what's actually planned; once you mark this file up, I'll fold the
  approved items into `ROADMAP.md` (and start with whichever section you
  prioritize — likely A, since it's the most concrete ask).
