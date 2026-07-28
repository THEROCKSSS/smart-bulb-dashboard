# Week 3 — Integrations, Automation & Mobile UX

Assumes Week 1 (audio depth) and Week 2 (secure remote access) are in
reasonable shape — several items here (Discord alerts, webhook-triggered
scenes) depend on Week 2's audit/notification groundwork.

## 1. Home Assistant integration (W3-001 – W3-020)
1. REST-based HA integration exposing this dashboard's devices as HA light entities
2. MQTT bridge as an alternative integration path for HA users who prefer MQTT discovery
3. HA custom component packaging (proper `custom_components/` structure, not just raw REST calls)
4. Sync HA scenes with this dashboard's own scene list (bidirectional or one-way, to be decided)
5. Expose audio-reactive start/stop as an HA service call
6. Expose group orchestration as an HA service call
7. HA automation examples documented (e.g. "turn on scene X when HA's own presence detection fires")
8. HA energy-dashboard placeholder integration (once/if real power data becomes available per ROADMAP.md's existing note)
9. HA device_tracker-based "arrive home" trigger tying into this dashboard's own scheduling
10. Documented HACS (Home Assistant Community Store) submission process if this integration matures enough to publish
11. Config-flow-based setup in HA (enter dashboard URL + PIN, not manual YAML) for a polished experience
12. HA reauth flow if the PIN gate is enabled/rotated after initial HA setup
13. Bidirectional state sync (a manual dashboard change reflects in HA within a reasonable delay)
14. HA-side history/logbook entries generated from this dashboard's own action history
15. Test plan: HA integration continues working correctly through a dashboard backend restart
16. Test plan: HA integration respects the PIN gate correctly when remote access is enabled
17. Documented network topology requirements (HA and dashboard must be able to reach each other)
18. HA integration versioning aligned with this project's own release versioning (Week 4)
19. Community documentation/README specifically for the HA integration, separate from the main project docs
20. Long-term evaluation: is a full custom component worth the maintenance burden vs. documenting the REST API for power users to wire up themselves

## 2. HomeKit bridging (W3-021 – W3-035)
21. HomeKit bridge exploration via `homebridge` + a custom plugin, exposing bulbs as HomeKit accessories
22. Homebridge plugin packaging and publishing process documented
23. Siri voice command support via the HomeKit bridge (inherits from HomeKit's own Siri integration)
24. HomeKit scene mapping from this dashboard's existing scene list
25. HomeKit color/brightness/on-off mapping correctness testing (HomeKit's HSV model vs. this project's own)
26. Apple Home app widget compatibility testing
27. HomeKit automation triggers (e.g. "when I arrive" from Apple's own presence detection) documented as examples
28. Multi-bulb HomeKit room/accessory grouping aligned with this project's own zones (Week 1, section 7)
29. HomeKit-specific PIN-gate interaction testing (bridge traffic shouldn't be blocked if PIN gate is enabled without a session)
30. Homebridge Docker deployment alongside the main dashboard container
31. Documented HomeKit pairing/setup walkthrough for end users
32. Fallback behavior if the Homebridge bridge process itself goes down (dashboard should keep working standalone)
33. HomeKit-side audio-reactive session start exposed as a scene/accessory trigger (best-effort, HomeKit's model doesn't map perfectly to a "mode")
34. Test plan across iOS versions for compatibility
35. Long-term evaluation: HomeKit's stricter local-network/Matter direction and whether a native Matter bridge is a better investment than Homebridge

## 3. Google Home / Alexa bridging (W3-036 – W3-050)
36. Google Home bridge via a local fulfillment server (no cloud dependency ideally, though Google's model often requires cloud)
37. Documented tradeoff: Google/Alexa's cloud-required architecture vs. this project's local-first philosophy — clearly flag this as a deliberate compromise if built
38. Alexa Smart Home Skill exploration (again, cloud-required by Alexa's design)
39. Voice command examples documented for both platforms once built
40. Scene/effect exposure to Google/Alexa as discrete "routines"
41. Brightness/color voice command mapping and testing
42. Multi-bulb group voice commands ("turn off all the lights")
43. Fallback/local-only mode clearly documented for users unwilling to accept the cloud dependency this requires
44. Security review specifically for this integration (it necessarily punches a hole for an external cloud service — treat with the same rigor as Week 2's security work)
45. Rate-limiting consideration for cloud-triggered commands hitting the local API
46. Documented setup walkthrough for both Google Home and Alexa
47. Test plan for voice command latency (cloud round-trip adds delay vs. local control)
48. Revocation procedure if this integration needs to be disabled/unlinked
49. Decide relative priority of this vs. a fully local voice-control alternative (section 4) given the local-first philosophy tension
50. Long-term evaluation: whether this integration is worth maintaining given the cloud-dependency compromise, versus focusing effort on local alternatives

## 4. Local/non-cloud voice control (W3-051 – W3-065)
51. Local wake-word detection (e.g. via `openWakeWord` or similar) running entirely on the host machine
52. Local speech-to-command parsing for a fixed vocabulary ("lights red", "party mode", "lights off")
53. No-cloud-round-trip voice control as the local-first alternative to section 3
54. Configurable wake word
55. Per-room microphone support (if multiple mics are set up, commands apply to the nearest room's bulbs)
56. Visual/audio confirmation feedback when a voice command is recognized (a brief flash or chime)
57. Fallback text-command input (type the same commands the voice system would recognize, for testing/accessibility)
58. Voice command history log (what was heard, what action was taken) for debugging misrecognitions
59. Custom command vocabulary editor (let a user define their own trigger phrases)
60. Multi-language wake-word/command support exploration
61. Noise-robustness testing (does it work with music already playing, given this project's own audio-reactive feature also wants the mic?)
62. Resolve the mic-contention question explicitly: can voice control and audio-reactive-from-mic run simultaneously, and if not, how is that communicated to the user
63. Privacy-first documentation emphasizing zero cloud transmission for this path, as a selling point over section 3
64. Performance profiling (wake-word detection CPU cost, especially alongside an active audio-reactive session)
65. Test plan comparing local voice control's accuracy/latency against the cloud-based alternatives from section 3

## 5. Discord bot integration (W3-066 – W3-080)
66. Discord bot exposing basic commands (`!bulb red`, `!bulb off`, `!bulb scene movie_night`) — ties into existing Discord bot example already in this workspace
67. Discord bot security review (who can issue commands — server role restrictions)
68. Discord-based security alerting (Week 2's audit events posted to a private Discord channel)
69. Discord slash-command support (modern Discord bot UX) instead of only prefix commands
70. Rich embed responses showing current bulb status after a command
71. Discord-based audio-reactive session control (`!bulb audio start band_fixed`)
72. Per-server bot configuration (which bulbs/groups a given Discord server can control, for shared-household-with-friends scenarios)
73. Discord bot deployment alongside the main dashboard (same host, separate process)
74. Discord bot uses the same PIN-gate-protected API as everything else — document how bot credentials are kept separate from the PIN itself
75. Rate limiting specifically for the Discord bot's command surface (avoid a channel-spam DoS against the bulb)
76. Discord bot help command auto-generated from the current feature set
77. Discord-based scheduling ("remind me / auto-run scene X") as a lightweight alternative front-end to the existing schedule engine
78. Test plan for bot reconnection after a Discord API outage
79. Documented bot invite/setup walkthrough
80. Long-term evaluation: whether Discord integration sees real use vs. remaining a novelty, informing further investment

## 6. Webhook & automation platform hooks (W3-081 – W3-095)
81. Generic incoming-webhook endpoints for any-color/any-scene/any-effect trigger (already sketched in FEATURE_PROPOSAL_V2, formalize here)
82. Outgoing webhooks on events (beat detected, schedule fired, device went offline) for external automation platforms
83. IFTTT-style applet documentation/examples using the generic webhook endpoints
84. Zapier-style integration documentation
85. Webhook payload schema versioning (so external integrations don't silently break on future changes)
86. Webhook authentication (a separate webhook-specific token, distinct from the PIN gate, for machine-to-machine calls)
87. Webhook retry/delivery-confirmation for outgoing webhooks
88. Webhook rate limiting distinct from the general API rate limiting (Week 2, section 6)
89. Webhook testing tool built into the dashboard (fire a test payload, see the response, without needing external tooling)
90. Documented example: doorbell-camera automation triggering a flash-alert pattern (from the original FEATURE_PROPOSAL_V2 idea)
91. Documented example: weather-API-triggered ambient color shift (also from the original proposal)
92. Node-RED integration example/documentation for users who prefer visual automation building
93. Home Assistant automations calling this dashboard's webhooks as an alternative to the full native integration (section 1)
94. Webhook audit logging (who/what called which webhook, when) feeding into Week 2's audit system
95. Webhook endpoint discovery/documentation auto-generated from the FastAPI schema (already available at `/docs`, cross-link clearly)

## 7. CLI tool (W3-096 – W3-110)
96. `bulbctl` command-line tool wrapping the full REST API for scripting/terminal use
97. Shell-completion support (bash/zsh/PowerShell) for `bulbctl`
98. Config-file-based `bulbctl` setup (point it at a dashboard URL + PIN once, reuse across invocations)
99. `bulbctl` scripting examples (a cron-driven sunrise script, a one-liner "movie night" alias)
100. Cross-platform packaging (single-binary or pip-installable) for `bulbctl`
101. `bulbctl` interactive mode (a simple REPL for exploratory use)
102. `bulbctl` JSON output mode for piping into other tools (`jq`, etc.)
103. `bulbctl` audio-reactive session control subcommands
104. `bulbctl` group orchestration subcommands
105. `bulbctl` discovery/scan subcommands
106. `bulbctl` diagnostics subcommands (test-connection, rescan)
107. `bulbctl` backup/restore subcommands (ties into Week 2, section 9)
108. `bulbctl` PIN-gate login/session management subcommands
109. `bulbctl` man page / `--help` documentation generation
110. Published to a package registry (PyPI or similar) once mature enough

## 8. Mobile PWA & native wrapper (W3-111 – W3-130)
111. Proper PWA manifest with icons for add-to-homescreen (already flagged in FEATURE_PROPOSAL_V2, implement here)
112. Service worker for offline-shell caching (the app shell loads even with no network briefly, though live control obviously needs connectivity)
113. Push notification support via the PWA (ties into Week 2's alerting work — a security or device-offline alert reaching a phone)
114. Home-screen icon and splash-screen polish
115. Touch-gesture support review (swipe between tabs on mobile, not just the sidebar)
116. Mobile-specific compact layout audit beyond the existing responsive CSS (dedicated mobile usability pass)
117. Native wrapper exploration (Capacitor/Tauri) if PWA limitations prove too constraining
118. App-store-adjacent distribution consideration (even a sideloadable APK) if a native wrapper is built
119. Biometric unlock (fingerprint/face) as a convenience layer on top of the PIN gate for the mobile app specifically
120. Mobile-specific quick-actions widget (iOS/Android home-screen widget showing bulb status, if a native wrapper exists)
121. Background audio-reactive control (start/stop a session from a lock-screen widget)
122. Mobile data-usage consideration (this should be tiny, but document expected bandwidth for cellular users on the remote-access path)
123. Haptic feedback on mobile for key actions (power toggle, scene applied)
124. Mobile accessibility audit (VoiceOver/TalkBack compatibility)
125. Mobile-specific onboarding flow (first-run walkthrough) distinct from the desktop experience
126. Mobile performance audit (avoid excessive polling draining battery — reconsider polling intervals specifically for mobile/background tabs)
127. Mobile dark-mode-only confirmation (project default is dark; confirm no light-mode leakage on any mobile browser quirk)
128. Cross-device session handoff (start a session on desktop, monitor/control from mobile seamlessly)
129. QR-code-based quick-connect (scan a code shown on desktop to open the same dashboard URL on mobile, pre-filling remote-access details if applicable)
130. Full mobile Playwright/device-lab testing pass across a few real device sizes

## 9. Scenes/effects expansion (W3-131 – W3-155)
131. "Gaming" scene (already sketched in FEATURE_PROPOSAL_V2 — implement with real tuning)
132. "Study" scene
133. "Thunderstorm" effect (slow blue-white flicker, distinct from Candle)
134. "Fireplace" effect (warmer/slower flicker than the existing Candle)
135. "Aquarium" scene (slow blue/teal drift)
136. "Breathing" effect (slow sine-wave brightness, distinct from the sharper existing Pulse)
137. "Twinkle" effect (irregular brief brightness flickers across... well, one bulb for now, more interesting once multi-bulb)
138. "Storm" effect (irregular white flashes at random intervals)
139. User-created custom scenes (save any color+brightness+mode combo under a custom name)
140. Scene scheduling as its own rule type reusing the schedule engine ("run Movie Night every Friday 8pm")
141. Effect speed presets (Slow/Normal/Fast one-click buttons instead of only a raw multiplier)
142. Transition duration control for scene/preset changes (instant snap vs. a smooth N-second crossfade)
143. Preview-before-apply (hover/tap-hold a preset swatch to preview live, release to revert)
144. Preset/scene import-export as shareable JSON files
145. Seasonal auto-scene suggestions (dismissible banner, e.g. suggest Halloween in late October)
146. Scene-of-the-day rotation option (cycle through favorites automatically each day)
147. Combined scene+effect macros (apply a scene, then immediately start an effect on top)
148. Scene versioning/history (see what a scene's settings were before the last edit)
149. Scene categories/tags for easier browsing as the list grows
150. Scene search/filter in the UI once the list is large enough to need it
151. Community scene-sharing repository structure (a `scenes/community/` convention, mirroring the audio-preset sharing idea from Week 1)
152. Scene rating/favoriting
153. Per-scene default target (a specific bulb/group/zone) remembered alongside the scene itself
154. Scene "randomize within constraints" (e.g. "something warm and dim" picks randomly from a filtered subset)
155. Full regression test suite for every scene/effect/preset combination (a real test matrix, not just manual spot checks)

## 10. Scheduling & automation depth (W3-156 – W3-180)
156. Sunrise/sunset-based schedule triggers (lat/long or a simple sunrise-sunset API) instead of only fixed clock times
157. Conditional rules ("only run if the bulb is currently off" / "only if scene X isn't already active")
158. "Vacation mode" — randomized on/off + scene changes across a time window to simulate occupancy
159. Per-rule enable/disable toggle without deleting the rule (may already partially exist — confirm and extend)
160. Schedule conflict detection/warning (two rules targeting the same bulb at overlapping times)
161. "Snooze schedule" — temporarily suspend all rules for N hours without editing/deleting them
162. Geofencing-style "arrive home" trigger via ARP-based presence detection for a known phone MAC (no-cloud version)
163. Schedule rule templates (common patterns like "wake-up light" or "bedtime wind-down" as one-click starting points)
164. Multi-step schedule rules (a sequence of actions over time, not just one action at one time)
165. Schedule rule dependency chains ("run rule B only after rule A has fired")
166. Holiday-calendar-aware scheduling (auto-adjust for recognized holidays, opt-in)
167. Schedule rule audit log (when did each rule actually fire, did it succeed)
168. Bulk schedule management (apply the same rule across a whole zone at once)
169. Schedule rule versioning/rollback
170. Natural-language schedule input experiment ("every weeknight at sunset") parsed into the existing rule structure
171. Weather-conditional scheduling (e.g. only run the "cozy" scene automatically if it's actually raining, tying into Week 3 section 6's weather webhook idea)
172. Presence-based automatic audio-reactive session start (start when arriving home with music already playing on a device, stretch/exploratory)
173. Schedule simulation/dry-run (preview what a rule set would do over the next 24 hours without actually firing)
174. Cross-device schedule coordination (ensure two rules on different bulbs meant to look synchronized actually fire close enough in time)
175. Schedule rule import/export
176. Recurring rule exception dates (run every day except specific excluded dates, e.g. skip a rule while traveling)
177. Time-zone handling review (confirm correct behavior for any future multi-location/travel use case)
178. Schedule engine performance review at scale (many rules, many bulbs)
179. Manual "fire this rule now" test button for debugging a rule without waiting for its scheduled time
180. Full documentation pass on the schedule engine covering every rule type and edge case

## 11. Groups/rooms UX (W3-181 – W3-200)
181. Full room/zone visual editor (builds on the sketch already added to ROADMAP.md and Week 1 section 7)
182. Drag-and-drop bulb assignment between rooms
183. Room-level quick-scene buttons on a dedicated Rooms tab
184. Room-level audio-reactive quick-start
185. Room thumbnail/icon customization
186. Grid/room-map view as a genuine alternative to the dropdown-based device picker, once bulb count justifies it
187. Room-based history filtering (see activity for just one room)
188. Room-based diagnostics rollup (health status per room, not just per bulb)
189. Favorite rooms pinned to the top-level navigation for quick access
190. Room-to-room "copy settings" (apply one room's current look to another with one click)
191. Multi-room synchronized effects (an effect that treats a room as one coordinated light, building on Week 1's orchestration work)
192. Room occupancy indicator placeholder (manual toggle for now; ties into presence-detection ideas elsewhere for future automation)
193. Room-level default brightness cap (e.g. bedroom never exceeds 60% automatically at night, distinct from a hard per-bulb cap)
194. Room search/filter once the room count grows
195. Room-based notification routing (alerts about a specific room's bulbs go to a specific channel/person)
196. Guest-accessible room subset (once multi-user/guest-links exist, scope a guest to specific rooms only)
197. Room import/export as part of config backup/restore
198. Room-level test-connection (batch diagnostic across every bulb in a room)
199. Visual distinction in the UI between "room" (physical space) and "group" (logical grouping) if both concepts coexist, avoiding user confusion
200. Full UX research pass — actually watch someone unfamiliar with the dashboard try to set up their first room, fix whatever confuses them

## 12. Notifications & alerts (W3-201 – W3-215)
201. Device-offline notification (a bulb hasn't responded in X minutes) — already sketched in FEATURE_PROPOSAL_V2, implement fully
202. Configurable notification channels (browser push, Discord, webhook, email) as a unified preferences system
203. Notification digest mode (batch non-urgent notifications into a daily summary instead of real-time spam)
204. Do-not-disturb hours for notifications (don't alert about a minor blip at 3am)
205. Notification history/log viewable in the dashboard
206. Test-notification button per configured channel
207. Severity-based routing (critical alerts always real-time, informational ones batched)
208. New-device-discovered notification (ties into the existing discovery feature)
209. Schedule-rule-fired notification (optional, for users who want confirmation their automations ran)
210. Audio-reactive session auto-timeout notification (so a user knows why the bulb stopped reacting)
211. Backup-completed / backup-failed notifications
212. Security-event notifications (ties directly into Week 2's audit/alerting work)
213. Notification preference per-user once multi-user exists
214. Snooze/mute specific notification types temporarily
215. Notification delivery reliability testing (confirm they actually arrive, don't silently fail)

## 13. Presets marketplace/sharing (W3-216 – W3-230)
216. Formal shareable-preset JSON schema covering colors/scenes/effects/audio-presets uniformly
217. Local import/export UI (drag a shared preset file onto the dashboard to install it)
218. Community preset repository structure (a public repo or curated list others can contribute to)
219. Preset preview before import (see what it looks like without committing)
220. Preset conflict resolution (importing a preset with a name that already exists)
221. Preset attribution (credit whoever authored/shared a community preset)
222. Preset rating/review system if a real marketplace-style repository takes off
223. Preset versioning (a shared preset can be updated; users can choose to accept updates or not)
224. Curated "starter pack" of community presets bundled with fresh installs (opt-in, not forced)
225. Preset moderation guidelines if this becomes a public community effort (avoid inappropriate content in shared names/descriptions)
226. Preset search/discovery UI once a real library exists
227. Export a full "look" (scenes + effects + audio presets bundled) as a single shareable theme
228. Theme marketplace as a natural extension of the preset-sharing work
229. Offline-first design (presets work without any network access; sharing is opt-in, not required)
230. Legal/licensing clarity for shared community content (consistent with this project's own noncommercial license)

## 14. Accessibility & i18n (W3-231 – W3-245)
231. Full i18n scaffold (extract all UI strings, structure for translation) — sketched in FEATURE_PROPOSAL_V2, implement here
232. First non-English translation (community-contributed or machine-translated-then-reviewed) as a proof of concept
233. RTL layout support testing
234. Screen-reader audit across every tab, not just the audio-reactive one (extends Week 1 section 13's audio-specific work)
235. Keyboard-only navigation audit (can every action be performed without a mouse/touch)
236. Color-contrast audit against WCAG guidelines across the dark theme
237. Font-size scaling respect (don't break layout if a user increases browser text size)
238. Reduced-motion media-query respect for non-audio-reactive animations too (toasts, transitions)
239. Alt-text/ARIA-label audit across all icon-only buttons
240. Focus-order review (logical tab order through forms and controls)
241. Accessible color-picker alternative to the raw hue/sat/val sliders (numeric input option)
242. Localization of date/time formats (schedule times, history timestamps) per locale
243. Documented accessibility statement/conformance level
244. Community accessibility feedback channel
245. Automated accessibility testing (axe-core or similar) integrated into the CI pipeline once CI exists (Week 4)

## 15. Keyboard/kiosk/secondary-display UX (W3-246 – W3-250)
246. Compact "widget" kiosk view (just power + brightness + color, no tabs) for a secondary always-on browser tab/display, per the original FEATURE_PROPOSAL_V2 sketch
247. Full-screen kiosk mode toggle (hide browser chrome guidance, auto-reconnect if the display sleeps/wakes)
248. Keyboard shortcuts for power toggle / next preset / next scene on desktop, with a visible cheat-sheet overlay
249. Multi-monitor awareness (remember which display a kiosk window should reopen on)
250. Idle-screen "screensaver" mode for a kiosk display — cycle through ambient scenes when nothing's been touched in a while
