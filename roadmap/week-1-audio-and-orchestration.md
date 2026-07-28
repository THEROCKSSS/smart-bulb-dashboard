# Week 1 — Audio Depth & Multi-Bulb Orchestration

Builds directly on the v2 audio engine (`iterations/003-audio-engine-v2/`)
and the new `GroupAudioSession` orchestration layer. Each item below is
phrased as an issue title — copy the ID + text as the issue title, and use
this file's surrounding context as the issue body. See `roadmap/README.md`
for the overall plan and how these map to GitHub issues/PRs.

## 1. Additional color-mapping modes (W1-001 – W1-020)
1. `harmonic_pairs` — hue driven by complementary color-wheel pairs per detected note/pitch class
2. `chroma_wheel` — full 12-tone chromatic pitch detection mapped to a 12-color wheel
3. `energy_contour` — hue locked, only saturation/value track a smoothed energy envelope
4. `call_and_response` — alternates between two palettes based on alternating loud/quiet phrases
5. `kick_snare_split` — separate kick-drum and snare-range detectors driving two blended accents
6. `transient_sparkle` — very short white micro-flashes on any sharp transient, layered over a base mode
7. `warm_cool_swing` — hue swings only between warm and cool halves of the wheel based on spectral tilt
8. `saturation_only` — hue and brightness fixed, only saturation reacts (a subtler novelty mode)
9. `inverse_brightness` — bulb dims on loud passages, brightens on quiet ones (already sketched as a "reverse mode" idea)
10. `two_tone_duet` — cycles strictly between two user-chosen colors keyed to alternating beats
11. `energy_bucket_palette` — maps RMS into discrete brightness "buckets" with distinct palette per bucket
12. `treble_sparkle_overlay` — like band_flash_overlay but treble-only, for hi-hat/cymbal accenting
13. `bass_only_pulse` — brightness-only pulse driven purely by bass energy, hue fixed
14. `mirror_mode` — hue mirrors around a center point as balance shifts, for a "breathing" color effect
15. `random_walk_hue` — hue does a bounded random walk instead of a fixed rotation, feels less mechanical
16. `key_detection_palette` (stretch) — rough musical key estimation mapped to a fixed palette per key
17. `silence_flash_recover` — one bright flash the instant audio resumes after a long silence
18. `crescendo_ramp` — detects sustained rising energy over several seconds and ramps brightness/saturation ahead of the peak
19. `decay_trail` — brightness decays exponentially after each hit instead of an instant drop, for a "trailing" look
20. `dual_band_crossfade` — smoothly crossfades between two independently selectable band-driven hues

## 2. Beat/rhythm/tempo features (W1-021 – W1-040)
21. Real BPM estimation from autocorrelation of the beat/onset signal, not just threshold detection
22. Manual "tap tempo" input as a fallback/override for BPM estimation
23. Beat-synced effect pacing — existing effects (`rainbow`, `fade`, etc.) accept a `sync_to_audio` flag
24. Downbeat/measure detection (assume 4/4, flag every 4th beat) for a stronger accent
25. Swing/groove detection — distinguish steady 4-on-the-floor from syncopated rhythms, adjust dwell dynamically
26. Beat confidence score exposed in `/status` so the UI can show how "locked in" the tempo estimate is
27. Adaptive beat threshold — auto-adjusts sensitivity to the current song's dynamic range instead of a fixed multiplier
28. "Anticipation" pre-flash a few ms before a predicted beat, using the tempo estimate
29. Double-time / half-time detection toggle for genres with ambiguous tempo
30. Silence-aware BPM reset — don't carry a stale tempo estimate across a long pause
31. Per-band beat detection UI toggle (bass-only vs. full-mix beat detection)
32. Onset strength histogram logged to history for later "was detection accurate" review
33. Configurable beat sensitivity presets (Subtle / Normal / Aggressive) as one-click Settings options
34. Tempo-locked palette_cycle — advance exactly once per beat *and* once per measure as separate options
35. "Groove memory" — remember the last confirmed tempo across a brief dropout instead of resetting instantly
36. Manual metronome overlay — bulb can flash a fixed BPM independent of audio, for practicing/tempo-training use cases
37. Beat-triggered webhook fire (ties into Week 3's automation work) for external sync (e.g. a visualizer on another screen)
38. Configurable "beats per accent" (flash every Nth beat instead of every beat) for slower-feeling accents
39. Rolling tempo graph in the UI (sparkline of estimated BPM over the last minute)
40. Tempo drift alarm — flags in history if estimated BPM swings wildly (likely false detection, not a real tempo change)

## 3. Genre & mood presets for audio-reactive (W1-041 – W1-060)
41. "EDM/Party" preset bundle (fast dwell, high contrast, wide hue swings, aggressive beat threshold)
42. "Chill/Ambient" preset bundle (slow dwell, narrow hue range, breathing_silence-leaning)
43. "Rock/Live" preset bundle (bass-forward warm palette, band_fixed-based)
44. "Classical/Acoustic" preset bundle (brightness-only, minimal hue movement, gentle dwell)
45. "Hip-Hop/Bass-Heavy" preset (bass_only_pulse-leaning, deep warm palette)
46. "Jazz/Improv" preset (wider dynamic range tolerance, slower smoothing)
47. "Lo-fi/Study" preset (very slow, desaturated, breathing_silence baseline)
48. "Metal/Hardcore" preset (strobe_on_drop-leaning, high contrast, fast dwell)
49. User-savable custom genre presets (bundle mode+sensitivity+dwell+palette under a name)
50. Auto-suggest a preset based on detected BPM range (heuristic, dismissible suggestion banner)
51. Preset A/B compare — quickly toggle between two saved presets against the same audio to compare feel
52. Shareable preset export/import as JSON (ties into Week 3's preset-sharing work)
53. Per-preset default palette override (so "EDM" preset can pull from a punchier preset-color subset)
54. Preset preview mode — apply a preset's visual style briefly without starting a full session
55. Community preset pack placeholder structure (a `presets/audio/` folder convention, seeded with the 8 above)
56. Preset changelog/versioning so a preset can be tuned over time without losing the original
57. "Randomize preset" button for discovery/fun
58. Time-of-day-aware preset suggestion (e.g. suggest Chill preset in the evening)
59. Preset-linked schedule rule type ("apply this audio preset at 8pm Fridays")
60. Preset rating/favoriting within the dashboard (personal, not shared)

## 4. Auto-gain & signal conditioning (W1-061 – W1-075)
61. Automatic gain control (AGC) — normalize consistently quiet/loud sources without manual sensitivity tuning
62. Configurable AGC attack/release time constants
63. Noise gate — ignore input below a configurable floor so room hum doesn't register as "beats"
64. Clipping/overload detection and a UI warning if the input signal is consistently saturating
65. Per-device saved gain calibration (remember a good sensitivity value per input device, not globally)
66. Silence-floor auto-calibration — a "calibrate" button that samples 5s of room silence to set the noise gate
67. Dynamic range compressor option — squash the difference between quiet/loud sections for more consistent reactivity
68. DC offset removal (some cheap USB mics/interfaces have a DC bias that skews RMS calculations)
69. Multi-band independent gain (separate sensitivity for bass/mid/treble)
70. Input level meter with peak-hold in the UI, independent of which mode is running
71. "Duck" behavior — brief automatic brightness reduction after a very loud transient, to avoid strobe fatigue
72. Configurable max-brightness ceiling (safety/comfort cap regardless of computed brightness)
73. Configurable min-brightness floor per session (override the global default)
74. Adaptive block size — auto-shrink further under low CPU load for even lower latency, grow if the system is under load
75. CPU usage guardrail — auto-throttle analysis rate if the host is under heavy load elsewhere

## 5. Visualizer & live-preview enhancements (W1-076 – W1-095)
76. Full spectrum bar visualizer (not just 3/N summary bars — a real FFT bar graph in the UI)
77. Waveform oscilloscope view as an alternative live-preview widget
78. Color preview swatch showing exactly what's about to be sent to the bulb, updating live
79. Beat flash visual indicator synced precisely with detected beats (not just a dot toggle)
80. Historical "last 10 seconds" scrolling spectrogram view
81. Dwell timer visual — a small progress bar showing time until the next send is allowed
82. Sender latency graph (rolling chart of `last_latency_ms` over time)
83. Side-by-side dual preview when comparing two modes/presets
84. Fullscreen "party mode" preview view (large color swatch, minimal chrome, for a TV/kiosk display)
85. Export a short recording (color-over-time) as a shareable GIF/clip
86. Live BPM readout prominently displayed during a session
87. Per-band color-coded bars using each band's assigned hue anchor as its bar color
88. "What the bulb is doing right now" plain-language caption (e.g. "reacting to a bass hit")
89. Mini visualizer embeddable in the Control tab (not just the Audio Reactive tab)
90. Configurable visualizer refresh rate independent of the dwell setting
91. Colorblind-friendly palette option for the live band meter itself
92. Dark/light contrast auto-adjust for the band meter based on current bulb color (avoid low-contrast bars)
93. Session summary screen after stopping (peak BPM, dominant band, total beats detected)
94. Live device-health indicator (mic clipping, dropped audio frames) in the same panel
95. "Explain this mode" inline tooltip with a short animated preview per mode in the dropdown

## 6. Orchestration refinements (W1-096 – W1-115)
96. Configurable per-bulb hue offset in `phase_offset` mode (not just even spacing)
97. Configurable per-bulb brightness scaling in orchestration (dim bulb 2 relative to bulb 1)
98. "Wave" role mode — a traveling brightness wave across an ordered list of bulbs
99. "Mirror" role mode — bulbs in pairs mirror each other's hue around a center point
100. Manual per-bulb band assignment override in `band_split` (not just index-based rotation)
101. Room-aware orchestration — group bulbs by declared room and orchestrate per-room independently
102. Cross-group synchronization — two separate groups share a single tempo/beat clock
103. Orchestration preset save/load (role_mode + per-bulb overrides bundled under a name)
104. Failover handling — if one bulb in a group goes offline mid-session, continue orchestrating the rest without restarting
105. Group session auto-recovery after a backend restart (currently sessions don't persist across restarts)
106. Live per-bulb status grid in the UI (which bulb is doing what, right now, in a group session)
107. Configurable "leader" bulb whose band assignment always stays primary bass, regardless of split order
108. Weighted band_split — some bulbs cover 2 bands blended, for groups where bulb count isn't a clean divisor
109. Group-level sensitivity override distinct from a global default
110. Add/remove a bulb from a running group session without restarting the whole session
111. Visual "chase direction" toggle for phase_offset (clockwise vs counter-clockwise conceptually)
112. Group session history logging (which group, which mode, start/stop times) in the existing History tab
113. Orchestration dry-run mode — compute and log what each bulb *would* receive without actually sending, for tuning
114. Per-bulb mute toggle within an active group session (temporarily exclude one bulb without stopping the group)
115. Automatic orchestration mode suggestion based on group size (e.g. suggest band_split only when group size ≥ 3)

## 7. Per-bulb configuration & zones (W1-116 – W1-135)
116. "Zone" concept above groups (e.g. "Living Room" containing multiple groups) — extends the existing Room idea in ROADMAP.md
117. Per-bulb color-calibration profile (some bulbs render hue slightly differently — a per-bulb hue offset correction)
118. Per-bulb gamma override (already exists per-device for brightness; extend the same idea to audio-reactive brightness curves)
119. Per-bulb max-brightness safety cap (e.g. a bedroom bulb capped lower than a living-room one)
120. Per-bulb "audio-reactive eligible" flag — exclude specific bulbs from ever being auto-included in group sessions
121. Zone-scoped schedule rules (apply a schedule to every bulb in a zone, not just one bulb or the "all" group)
122. Zone-level default audio preset assignment
123. Visual room-map editor (drag bulbs onto a simple floor-plan-like grid) — full version of the ROADMAP.md sketch
124. Per-bulb nickname/icon customization beyond just a name string
125. Per-bulb "last known good" config snapshot for quick rollback after a bad manual edit
126. Bulk-edit UI for applying one setting (e.g. gamma) across every bulb in a zone at once
127. Zone-based test-connection (run diagnostics against every bulb in a zone with one click)
128. Per-bulb firmware/version display sourced from tinytuya's device info, where available
129. Per-bulb historical uptime percentage (ties into diagnostics work in Week 4)
130. Zone import/export as part of config backup/restore
131. Visual indicator distinguishing "audio-reactive capable" vs "excluded" bulbs in the zone editor
132. Per-bulb custom safe-brightness-at-night cap tied to the existing schedule/timer system
133. Zone-level "panic button" (kill all activity for every bulb in a zone, not just the global one)
134. Per-bulb connection quality score (rolling average of test-connection latency) surfaced in diagnostics
135. Zone-based Favorites (a favorite color applies across every bulb in a zone with one click)

## 8. Session management & presets (W1-136 – W1-155)
136. Save an entire running session's config (mode, sensitivity, dwell, n_bands, device) as a named audio preset
137. One-click "resume last session" after a restart
138. Scheduled audio-reactive sessions (start automatically at a set time, e.g. "Audio Reactive: EDM preset, Fridays 8pm")
139. Session auto-stop conditions beyond silence timeout (e.g. stop after a fixed max duration)
140. Multiple saved device+mode combos per bulb, quick-switch dropdown
141. "Duplicate session to another bulb" — copy a running single-bulb session's config to start an identical one elsewhere
142. Session conflict detection — warn if starting a group session would also affect a bulb already in an active solo session
143. Graceful session handoff — switching modes mid-session without a hard stop/restart (avoid the brief gap)
144. Session notes field (freeform text a user can attach to a saved preset, e.g. "good for movie night parties")
145. Quick-access "favorite audio presets" pinned to the top of the Audio Reactive tab
146. Session start confirmation toast showing exactly which device/mode/dwell was applied (avoid silent mismatches)
147. Undo last session-start (revert to previous bulb state) — ties into the broader undo/redo idea in FEATURE_PROPOSAL_V2
148. Session templates seeded from the genre presets in section 3 above, editable before saving as custom
149. Auto-pause audio-reactive session when a manual color/scene command is issued (avoid the two fighting)
150. Resume audio-reactive automatically after a manual override times out (configurable grace period)
151. Session activity heartbeat in `/status` (last-processed-frame timestamp) distinct from `active` boolean
152. Bulk session management page — see/stop every active session (solo + group) across the whole install at once
153. Session naming (user-assigned label, not just device/group ID) for clarity in history/logs
154. "Test drive" mode — a 15-second timed preview of a preset/mode combo that auto-stops itself
155. Session export as a shareable link/config bundle (pairs with Week 3's sharing features)

## 9. Performance & reliability hardening for audio (W1-156 – W1-175)
156. Automated regression test suite for `_apply_mode()` covering all 12 modes with synthetic signals (formalize the ad-hoc tests from iteration 003)
157. CPU profiling pass on the analysis loop at the new 512-sample block size under sustained load
158. Memory leak check for long-running sessions (multi-hour soak test)
159. Graceful degradation if `sounddevice`/PortAudio itself crashes mid-session (auto-restart the stream)
160. Configurable fallback device — if the configured input device disappears (unplugged), fall back to system default instead of erroring silently
161. Explicit shorter socket timeout for audio-reactive bulb sends specifically (flagged as unresolved in iteration 003 — a slow/offline bulb ties up its sender thread for a long single attempt)
162. Watchdog thread that restarts a stuck `BulbSender` if it hasn't completed a send attempt within an abnormally long window
163. Structured logging for the audio pipeline (currently only uses the generic history log) — a dedicated debug log level
164. Load test: multiple simultaneous single-bulb sessions on different devices, confirm no cross-talk
165. Load test: a large group session (simulate 10+ bulbs) for orchestration scaling behavior
166. Automatic sample-rate fallback if a device doesn't support 44.1kHz natively
167. Explicit error surfaced in the UI (not just silently retried) after N consecutive failed sends to a bulb
168. Config validation on session start (reject an out-of-range `n_bands`/`min_dwell_ms` with a clear message, not just a 400)
169. Session-level rate limiting on the start/stop endpoints themselves (avoid rapid start/stop thrashing from a buggy client)
170. Confirm behavior when two different devices are requested for the same bulb ID's solo session vs its group session simultaneously
171. Formal latency budget document — measured contribution of each pipeline stage (capture, FFT, mode compute, queue, send)
172. Persist last-known-good session config to disk so a crash/restart can offer "restart last session"
173. Stress test the circular hue-smoothing math across thousands of random hue transitions for numerical stability
174. Confirm zero-padding (BLOCK_SIZE=512 → FFT_SIZE=4096) doesn't introduce audible-lag perception in a real blind test
175. Cross-platform check — confirm `sounddevice`/PortAudio behavior parity if this is ever run on Linux/Mac, not just Windows

## 10. Testing & validation tooling for audio (W1-176 – W1-190)
176. A reusable synthetic-audio test harness (tones, chirps, noise bursts) as a proper pytest suite, not one-off scripts
177. Golden-value regression tests for each mode's exact hue/brightness output given a fixed synthetic input
178. A "record and replay" tool — capture real audio to a file, then replay it deterministically against the pipeline for repeatable testing
179. Automated screenshot/visual diff testing for the Audio Reactive tab's UI states
180. CI job that runs the full synthetic-signal test suite on every PR touching `audio_reactive.py`
181. Fuzz testing — feed the analyzer random/garbage/NaN-containing sample arrays and confirm no crashes
182. Latency measurement harness that reports real decision-latency and send-latency numbers automatically
183. A "does it look good" subjective rating log — after real-music testing sessions, record a quick 1-5 rating per mode to track tuning progress over time
184. Test coverage report specifically for `audio_reactive.py` as its own tracked metric
185. Integration test spinning up a real (loopback) audio device in CI if the CI environment supports it
186. Contract tests for the `/api/audio/*` and `/api/groups/*/audio-reactive/*` endpoints (schema validation)
187. Test for the exact freshness/dwell guarantees under concurrent rapid mode switches
188. Chaos test — randomly kill/restart the backend mid-session and confirm no zombie threads/streams linger
189. Documented manual test script for "ears-on" tuning sessions once the bulb is reliably online (a checklist, not automated)
190. Before/after comparison tooling for tuning constant changes (e.g. beat threshold) — run both against the same recorded clip

## 11. Audio input source expansion (W1-191 – W1-205)
191. Network audio source support (e.g. an RTSP/Icecast stream as input, not just a local device)
192. Bluetooth microphone input support and testing
193. Multi-device simultaneous capture (blend two input devices, e.g. mic + system audio)
194. Per-source independent sensitivity calibration (already partly covered in section 4, expand to true multi-source blending)
195. USB audio interface support/testing beyond built-in devices
196. Auto-detection of a newly-plugged-in preferred device (e.g. auto-switch to VoiceMeeter if it appears while a session is running on the default mic)
197. Input device health check in Settings (a dedicated "test this device" button independent of starting a full session)
198. Sample-format compatibility check across devices (some report unusual channel/format combos)
199. Explicit WASAPI loopback mode (Windows) as an alternative to VoiceMeeter, per the original design doc's "Mode A alternative"
200. Documented Linux/PulseAudio/PipeWire loopback equivalent for future cross-platform support
201. Airplay/Chromecast audio capture exploration (stretch — likely needs a virtual sink)
202. Direct DAW/streaming-software plugin exploration (e.g. an OBS audio-monitor tap) for content-creator use cases
203. Configurable capture channel selection beyond mono/stereo (e.g. pick a specific channel out of an 8-channel VoiceMeeter bus)
204. Input source presets (save "VoiceMeeter for PC audio" and "Fifine mic for room audio" as one-click swaps)
205. Graceful UI messaging when a saved input device index no longer matches any connected device after a reboot

## 12. Music-player-adjacent features (W1-206 – W1-225)
206. Minimal built-in local-file audio player embedded in the dashboard (per the original "make a music player" idea)
207. Playlist support for the built-in player
208. "Now playing" metadata display (song/artist) pulled from local file tags
209. One-click "play this song and react" flow combining playback + session start
210. Optional Spotify "now playing" read-only integration (metadata display only, not playback control)
211. Optional local media-player (e.g. VLC, Windows Media Player) "now playing" polling integration
212. Crossfade support in the built-in player so track transitions don't cause a jarring silence-detection blip
213. Volume-normalization option for the built-in player specifically (separate from the analyzer's own AGC)
214. Queue management UI for the built-in player
215. Keyboard media-key support (play/pause/skip) when the dashboard tab is focused
216. "Silence gap" tolerance tuning specifically for the built-in player's track transitions
217. Waveform scrubber for the built-in player synced with the live visualizer
218. Support for common formats (mp3/flac/wav/ogg) in the built-in player
219. Auto-generated per-track color "signature" (a quick pre-analysis pass) shown as a thumbnail-like preview before playing
220. Export a track's full color timeline (once played through) as a reusable "light show" that can replay without live audio
221. Manual light-show editor (hand-author a timed color sequence, independent of any live audio)
222. Light-show scheduling (play a pre-authored show at a specific time, e.g. a holiday display)
223. Light-show sharing/export as a JSON file
224. Sync a light-show to an external trigger (e.g. start exactly when a scheduled event fires)
225. Basic beat-matched light-show generation from an analyzed track (auto-author a show from section 220's captured timeline)

## 13. Accessibility for audio-reactive (W1-226 – W1-235)
226. Photosensitive-epilepsy safety cap — hard limit on flash frequency/contrast regardless of mode settings, with a clear warning if a user tries to exceed it
227. "Reduced motion" audio-reactive profile (gentle, no strobe modes, capped brightness swing)
228. Screen-reader-friendly live status announcements for the Audio Reactive tab (not just visual bars)
229. High-contrast mode for the live band meter and controls
230. Configurable max flash rate independent of dwell (a true safety ceiling, not just the UX-focused dwell slider)
231. One-click "disable all flash-style modes" toggle for shared/public spaces
232. Clear labeling of which modes are flash-heavy vs. ambient in the mode picker itself
233. Documentation section specifically on photosensitivity safety considerations
234. Default-safe settings for first-time users (start conservative, let advanced users opt into more intense modes)
235. Parental/guest-mode restriction — limit selectable modes to a safe subset for a shared/guest login (ties into Week 2's multi-user work)

## 14. Misc / novelty audio ideas (W1-236 – W1-250)
236. "Clap detector" — special-case fast transient detection tuned for hand claps specifically, for a simple on/off gesture control
237. "Whistle detector" — narrow high-frequency tone detection as another gesture-control trigger
238. Silence-triggered auto-off after an extended period (fall asleep to music, lights turn off when it stops for good)
239. "Encore" mode — briefly intensify all effects for the last chorus of a song (needs some notion of track structure, stretch)
240. Color-blind-friendly default palette option across all modes, not just the visualizer (ties into section 13)
241. "Duet" mode syncing two separate bulb installations over the network (stretch, needs a shared clock/protocol)
242. Karaoke-specific vocal-isolation mode refinement beyond the original design doc's rough sketch
243. Applause/cheer detection (broadband loud burst distinct from music) triggering a celebratory flash pattern
244. "DJ mode" — manual override buttons layered on top of a running audio-reactive session for live tweaking during a set
245. Audio-reactive + effect blending (run a slow color_loop effect as a base, audio only modulates brightness on top)
246. Seasonal audio-reactive palette overlays (e.g. a Halloween-only orange/purple constraint during October)
247. "Practice mode" for tap-tempo/rhythm training with visual feedback on timing accuracy
248. Silent-disco style headphone-audio capture support exploration (capture from a specific app's audio only, not full system mix)
249. Configurable "warm-up" ramp when starting a session (fade in from off instead of snapping to full reactive brightness immediately)
250. Community mode-sharing — a simple JSON schema so custom mode configs (not full code, just parameter presets) can be shared between installs
