# Week 4 — Analytics, Bluetooth-Readiness, Polish & Release

Closes out the month. Sections 6-7 (GitHub Pages, agent onboarding) turn
everything built across the month into something a stranger — human or
agent — can actually pick up and run. Section 15 is a genuine retro,
not filler.

## 1. Energy/usage analytics (W4-001 – W4-020)
1. Usage stats dashboard: total on-time today/week/month, most-used scene, most-used preset color (some already listed in FEATURES.md — deepen into a real analytics view)
2. Per-bulb usage comparison (which bulb gets used most)
3. Time-of-day usage heatmap
4. Estimated runtime-hours tracking (a proxy metric even without real wattage data)
5. Smart-plug-based real power monitoring integration, if a compatible plug is added (per the existing ROADMAP.md note that this bulb model doesn't expose real draw)
6. Cost estimation given a user-entered electricity rate, once real wattage data exists
7. Usage trend graphs (week-over-week, month-over-month)
8. Export usage data (CSV/JSON) for external analysis
9. Audio-reactive session usage stats (total time spent, most-used mode, most-used preset)
10. Effect/scene popularity ranking across the whole install's history
11. "Insights" digest (weekly summary: "you used Movie Night 5 times this week")
12. Idle-bulb detection (a configured bulb that's rarely/never used — surfaced as a suggestion to reconfigure or remove)
13. Comparison view across bulbs/rooms once multiple bulbs exist
14. Historical uptime-vs-usage correlation (does the bulb drop offline more when heavily used)
15. Configurable analytics retention period
16. Privacy-respecting design confirmation — all analytics stay fully local, never phone home (consistent with Week 2 section 14's no-telemetry commitment)
17. Analytics opt-out toggle for users who'd rather not track usage at all
18. Analytics data included/excluded choice in backup/restore (Week 2 section 9)
19. Dashboard widget summarizing "this month at a glance" on the Control tab
20. Year-in-review style annual summary (a fun, low-priority polish item for later)

## 2. History/audit deepening (W4-021 – W4-035)
21. Full-text search across the history log
22. History filtering by action type, device, date range
23. History export (already partially covered under discovery/backup — make it a first-class History-tab feature)
24. Configurable history retention (currently a fixed 200-entry ring buffer per device — make it configurable/larger)
25. Persisted history across restarts (currently in-memory per `BulbController` — confirm and fix if it doesn't survive a restart)
26. Cross-device unified history view (see everything across all bulbs in one timeline, not per-device only)
27. History entry detail expansion (click an entry for full raw params/response, not just the summary line)
28. Annotate history entries (let a user add a note to a specific event, e.g. "this is when it flickered oddly")
29. History-based "replay" — re-apply whatever action a past history entry represents, with one click
30. Correlate history entries with diagnostics (was the bulb's connection healthy at the time of a given action)
31. History entry success/failure visual distinction improvements (already tracked as ok/error — polish the UI presentation)
32. Configurable history verbosity (some users want every single audio-reactive frame logged; most don't — sensible defaults plus an advanced toggle)
33. History archive/export-then-clear workflow for long-running installs accumulating a lot of data
34. Cross-reference history with the Week 2 security audit log in a unified "everything that happened" view (with clear visual separation between the two categories)
35. Automated tests confirming history entries are created correctly for every action type across the whole API surface

## 3. Diagnostics & health monitoring (W4-036 – W4-050)
36. Historical connection-quality graph per bulb (extends the existing point-in-time test-connection)
37. Automated periodic health checks (not just on-demand) with results feeding into the uptime graph
38. Diagnostics summary page aggregating every bulb's current health at a glance
39. Predictive "this bulb might be about to drop off Wi-Fi" heuristic based on historical patterns
40. Network-quality diagnostics beyond just the bulb (host machine's own Wi-Fi signal strength, if obtainable)
41. Firmware-version-aware diagnostics (flag known-problematic firmware versions if patterns emerge across the community)
42. One-click "generate support bundle" (logs + config summary + recent history + diagnostics, secrets redacted) for easier troubleshooting help requests
43. Self-test suite runnable from the UI (exercises every major feature area briefly, reports pass/fail)
44. Diagnostics-driven auto-suggestions ("this bulb fails often — consider a DHCP reservation") surfaced contextually
45. Latency trend alerting (bulb response time degrading over weeks, even if not yet fully offline)
46. Cross-bulb comparison in diagnostics (is one bulb consistently slower than others on the same network)
47. Diagnostics export for sharing when asking for community help
48. Scheduled diagnostics reports (weekly health summary notification)
49. Diagnostics API endpoints fully documented and covered by their own test suite
50. Visual diagnostics history graph embedded directly on each bulb's row in Settings, not just a separate tab

## 4. Bluetooth bulb readiness (W4-051 – W4-065)
Explicitly prep-only — no BLE hardware exists yet per ROADMAP.md. These are
groundwork items that don't require purchasing anything yet.
51. Finalize the `BLEBulbController` interface sketch already in ROADMAP.md/HANDOFF.md into an actual (unimplemented but structured) class stub
52. Research and document which specific Tuya BLE bulb models are known-compatible with `tuya_ble`
53. Document expected BLE host hardware requirements (range limitations, whether a Raspberry Pi/bridge is needed)
54. Design how `config.json` would represent a BLE bulb alongside Wi-Fi ones (transport field, connection params)
55. Design how the audio-reactive engine's `BulbSender` pattern would adapt to BLE's different latency characteristics
56. Research BLE mesh protocols in case the eventual hardware is mesh-capable rather than single BLE bulbs
57. Document expected differences in local_key/pairing flow for BLE devices vs. the existing Wi-Fi cloud-assisted flow
58. Identify which existing features (scenes, effects, schedules) would need zero changes vs. BLE-specific adaptation
59. Draft a test plan for once BLE hardware is actually acquired
60. Budget/hardware shopping list research (which BLE bulbs or hub are worth buying, cost comparison)
61. Investigate whether a single host can manage both Wi-Fi and BLE bulbs simultaneously without conflict
62. Document expected latency/range tradeoffs so user expectations are set correctly before buying anything
63. Confirm the existing transport-agnostic `BulbController` abstraction doesn't need any changes to accommodate this (validate the design, don't just assume it)
64. Write a "day one" checklist for what to actually do the moment BLE hardware arrives
65. Explicitly re-confirm with the user before any real purchase-driven work begins, since this was always deferred until hardware is bought

## 5. Second-bulb / multi-bulb real-world testing checklist (W4-066 – W4-075)
66. Full checklist for adding bulb #2 once purchased (credential retrieval, config entry, group assignment)
67. Real visual test of `unison` orchestration mode with 2 real bulbs
68. Real visual test of `phase_offset` (chase effect) with 2-3 real bulbs
69. Real visual test of `band_split` with 2-3 real bulbs, confirming it actually looks like distinct "bass bulb"/"treble bulb" roles
70. Real test of per-bulb band/zone assignment (Week 1 item) once hardware exists
71. Real test of room/zone visual editor with an actual multi-room, multi-bulb setup
72. Real test of synchronized rainbow/chase effects across bulbs (existing ROADMAP.md idea)
73. Real-world latency/dwell tuning specifically for orchestrated multi-bulb sessions (does one dwell setting suit all bulbs, or does each need its own)
74. Document any surprises found once real multi-bulb hardware is in play, iteration-log style
75. Update all "tested with fake controllers only" caveats across `iterations/` and `FEATURES.md` once real hardware confirms the behavior

## 6. GitHub Pages site content (W4-076 – W4-100)
76. `docs/index.md` landing page (what this project is, screenshots, quickstart)
77. `docs/pages/setup.md` — the GitHub-Pages-rendered version of SETUP.md, kept in sync
78. `docs/pages/features.md` — a rendered, browsable version of FEATURES.md
79. `docs/pages/audio.md` — a dedicated page for the 12 audio modes with visuals/GIFs if feasible
80. `docs/pages/security.md` — the Pages-rendered version of the remote-access-security doc
81. `docs/pages/roadmap.md` — a rendered summary of this whole `roadmap/` directory with links to each week
82. Screenshots refreshed and re-embedded across the Pages site (existing ones in `docs/screenshots/` may need updates as UI evolves)
83. A short demo GIF/video embedded on the landing page showing the dashboard in actual use
84. Jekyll (or plain static) theme configuration matching this project's dark-mode aesthetic
85. Navigation/sidebar structure for the Pages site distinct from the raw repo file browser
86. Mobile-responsive check on the Pages site itself, not just the dashboard app
87. `CNAME` / custom domain setup if the user wants a nicer URL than the default `github.io` one
88. Search functionality on the Pages site once content volume justifies it
89. Changelog page reflecting real version history (ties into section 13's release process)
90. FAQ page collecting common setup questions (data-center-mismatch QR bug, IP-changed troubleshooting, etc. — mine directly from existing SETUP.md troubleshooting entries)
91. Contribution guide linked prominently (ties into section 14)
92. License page clearly presenting the noncommercial license terms
93. Analytics-free confirmation on the Pages site itself (no tracking scripts, consistent with the project's own philosophy)
94. Automated Pages deployment via GitHub Actions on every push to main (ties into section 8's CI work)
95. Broken-link check as part of the Pages build/deploy process
96. Accessibility check on the Pages site itself (not just the live dashboard app)
97. Open Graph / social-preview meta tags so shared links render nicely
98. Versioned docs consideration (does Pages need to reflect multiple releases, or always just latest — decide and document the choice)
99. Feedback mechanism on the Pages site (link to GitHub Issues for doc corrections)
100. Full read-through/proofread pass of every Pages doc before considering this section done

## 7. Agent-facing onboarding docs (W4-101 – W4-115)
101. Root-level `AGENTS.md` — the single file an AI coding agent reads first to understand this repo (already drafted this round, deepen it here)
102. Explicit "if you are an AI agent setting this up, do X then Y then Z" numbered flow, tested by actually having a fresh agent session follow it verbatim
103. Machine-readable project manifest (a small JSON/YAML describing key paths, entry points, test commands) for agents that prefer structured input over prose
104. Documented expectations for agent-authored PRs against this repo (commit message style, test requirements, iteration-log expectations)
105. A dedicated `roadmap/for-agents.md` explaining how to pick up a roadmap item and turn it into a PR, referencing this project's existing skill files as the procedural source of truth
106. Explicit warnings in `AGENTS.md` about the things that bit this project before (venv isolation discipline, the audio-callback-blocking bug pattern, the root-path-auth-gate bug) so an agent doesn't repeat them
107. A minimal "smoke test in under 5 minutes" script an agent can run immediately after cloning to confirm a healthy baseline before starting work
108. Cross-links from `AGENTS.md` to every relevant skill file (`bulb-dashboard-setup`, `-control`, `-audio`, `-discovery`, `-remote-access`) so an agent knows exactly where to look for each subsystem
109. Documented conventions for adding a new audio mode (where the code goes, what tests are expected, per the actual pattern established in `audio_reactive.py`)
110. Documented conventions for adding a new roadmap item / converting one into a GitHub issue
111. A "definition of done" checklist agents should hold themselves to (tests run, iteration log written, docs updated) mirroring what was actually done throughout this project's own build sessions
112. Test that a completely fresh Claude Code (or other agent) session, given only this repo and `AGENTS.md`, can successfully set up and run the dashboard end-to-end
113. Feedback loop — after that fresh-agent test, update `AGENTS.md` with whatever confused it
114. Explicit scope boundaries documented (what an agent should NOT do unprompted — e.g. don't enable the PIN gate with a placeholder PIN and call it done, don't commit real device credentials)
115. Version this file alongside releases so agent guidance doesn't silently drift from the actual codebase

## 8. Developer experience (tests, CI, linting) (W4-116 – W4-135)
116. Formal pytest test suite for the backend, consolidating the various ad-hoc test scripts written across `iterations/` into real, repeatable tests
117. GitHub Actions CI workflow running the test suite on every PR
118. Linting (ruff/flake8) configured and enforced in CI
119. Type-checking (mypy or similar) evaluation — decide whether it's worth adopting given the codebase's current style
120. Frontend JS linting (the codebase is vanilla JS with no build step — decide if ESLint is worth adding without introducing a build step)
121. Automated Playwright test suite formalized from the ad-hoc verification scripts used throughout this project's development, running in CI
122. Code coverage reporting and a tracked minimum threshold
123. Pre-commit hook configuration (formatting, basic lint) for contributors
124. Dependency update automation (Dependabot or Renovate) configured
125. PR template requiring: what changed, what was tested, iteration-log entry if applicable
126. Issue templates (bug report, feature request, referencing the roadmap format) for consistent incoming issues
127. Branch protection rules on `main` (require CI passing before merge)
128. Automated changelog generation from merged PRs/commits
129. Local development setup script (one command to create the venv, install deps, copy config template)
130. Documented debugging setup (how to attach a debugger to the FastAPI backend, how to use browser devtools effectively against the vanilla-JS frontend)
131. Test data/fixtures for consistent local development without needing real bulb hardware (mock controllers, formalizing the `FakeController` pattern used throughout `iterations/`)
132. Docker-based CI test environment matching the documented Docker deployment path
133. Performance regression tracking in CI (fail a PR if audio decision latency measurably regresses)
134. Security-scanning CI step (dependency vulnerabilities, secret-pattern scanning per Week 2 section 12)
135. Full CI pipeline documented in its own `docs/ci.md` or equivalent

## 9. Config validation & migration tooling (W4-136 – W4-150)
136. JSON schema validation for `config.json` on load, with clear error messages for malformed entries
137. Config migration tooling for breaking changes across versions (auto-upgrade an old config format to the new one)
138. Config version field added so the app can detect and handle old formats gracefully
139. Dry-run config validation command (`bulbctl config validate`) usable before restarting the backend with a hand-edited config
140. Sensible default-filling for missing optional fields rather than hard failures
141. Config schema documentation auto-generated and kept in sync with the actual Pydantic models
142. Duplicate-ID detection and clear error messaging (two devices/groups sharing an ID)
143. Orphaned-reference detection (a group referencing a device_id that no longer exists in `config.json`)
144. Config linting as part of the pre-commit/CI pipeline for the project's own `config.example.json`
145. Graceful handling of a corrupted `config.json` (clear error + suggestion to restore from backup, not a silent crash)
146. Config hot-reload exploration (apply certain config changes without a full restart) — evaluate feasibility given current architecture
147. Environment-specific config overlays (a `config.local.json` pattern for per-machine overrides without touching the shared base config)
148. Config export redaction levels (full export for personal backup vs. a redacted export safe to share for support purposes)
149. Automated test confirming `config.example.json` always stays a valid, loadable template as the schema evolves
150. Migration test suite (feed a deliberately old-format config through the migration tool, confirm correct upgrade)

## 10. Performance profiling & optimization (W4-151 – W4-165)
151. Full backend startup-time profiling (identify and trim any unnecessary import/init overhead)
152. API response-time profiling across every endpoint under realistic load
153. Frontend bundle/asset-size audit (no build step currently — confirm this remains reasonable as the JS file grows)
154. Database-equivalent (JSON file I/O) performance review as history/config files grow over a long-running install
155. Memory footprint profiling for long-running sessions across every feature area, not just audio (Week 1 already covers audio-specific soak testing)
156. Concurrent-request handling review under realistic multi-tab/multi-user load
157. WebSocket/SSE evaluation for replacing polling-based status updates with push-based ones, if justified by measured overhead
158. Static asset caching headers review (avoid unnecessary re-fetching of unchanged CSS/JS)
159. Lazy-loading evaluation for tabs/routes not currently in view
160. Profiling report published (methodology + findings) so future contributors have a baseline to compare against
161. Identify and fix any N+1-equivalent inefficiencies in config/group lookups as bulb count scales up
162. Frontend render-performance audit (does re-rendering a whole tab on every poll cause any visible jank)
163. Backend thread/resource cleanup audit (confirm every background thread — effects, timers, audio sessions — is provably cleaned up on stop, no leaks over a long session)
164. Load test the discovery scheduler and schedule engine's background threads together under a long uptime simulation
165. Establish and document explicit performance budgets (e.g. "status endpoint must respond under Xms") as a measurable target going forward

## 11. UI/UX polish pass (W4-166 – W4-185)
166. Full visual consistency audit across every tab (spacing, typography, color usage) now that the feature set has grown significantly since v1
167. Loading-state polish (skeleton screens instead of a bare "Loading…" text where it still appears)
168. Empty-state polish audit (confirm every list/table has a clear, non-generic empty state, per the project's own design conventions)
169. Toast notification consistency review (success/error styling, timing, stacking behavior under rapid actions)
170. Animation/transition audit against the project's own anti-AI-slop rules (no `transition: all`, no generic effects)
171. Form validation UX improvements (inline validation instead of only failing on submit)
172. Consistent iconography pass (currently text-only nav — evaluate whether icons would help without adding clutter)
173. Color-picker UX refinement (the wheel-widget idea from FEATURE_PROPOSAL_V2, if not already built)
174. Undo/redo for manual color changes (also from FEATURE_PROPOSAL_V2, if not already built)
175. Recently-used-colors strip (also from FEATURE_PROPOSAL_V2)
176. Keyboard shortcut cheat-sheet overlay (ties into Week 3 section 15)
177. Consistent confirmation-dialog pattern for destructive actions (remove device, disable PIN gate, restore backup) — audit for consistency across all of them
178. Settings-tab reorganization once it has grown to contain devices, discovery, and remote-access — consider sub-tabs or a cleaner layout
179. Dashboard information density review (is the Control tab showing the right amount of info, not too sparse or cluttered)
180. First-time-user empty-dashboard experience polish (no devices configured yet — a clear, welcoming setup prompt instead of a bare table)
181. Consistent use of the `data_source` LIVE DATA labeling convention audited across every endpoint that should have it
182. Visual regression testing setup (screenshot diffing) to catch unintended UI drift going forward
183. Dark-theme contrast re-audit now that several new UI elements (band meter, PIN overlay, orchestration controls) have been added
184. Copy/microcopy pass — review every button label, help text, and error message for clarity and consistency
185. Final cross-browser check (Chrome/Firefox/Edge/Safari) now that the feature set is much larger than the original prototype

## 12. Documentation polish (W4-186 – W4-200)
186. Full read-through of every doc file for accuracy against current code (docs drift is real — this project's own docs have already needed several rounds of updates)
187. Consistent formatting/style pass across all markdown files
188. Cross-link audit (every doc that references another should link it correctly, no broken relative paths)
189. Glossary of project-specific terms (dwell, orchestration role modes, discovery classification) for new readers
190. Consolidated "start here" index if the doc count has grown large enough to need one beyond the README
191. Diagram audit — confirm any architecture diagrams (including this roadmap's own dependency graph) stay accurate as things change
192. Example-command audit — actually re-run every curl example in API.md against current code to confirm none have silently broken
193. Screenshot refresh pass across all docs referencing UI screenshots
194. Documentation versioning strategy finalized (does documentation track releases, or always reflect `main`)
195. Non-English documentation consideration, tying into Week 3's i18n work if that progresses
196. Video walkthrough consideration for the most complex setup steps (local_key acquisition, remote access)
197. Consistent terminology audit (e.g. "bulb" vs "device" vs "light" — pick one primary term and use it consistently)
198. Doc-linting (markdown-lint) added to CI
199. "What changed" migration notes for anyone upgrading from the v1/97-feature prototype to the current 137+-feature version
200. Final documentation completeness self-audit against every feature listed in `FEATURES.md` — confirm each one is actually documented somewhere

## 13. Release process & versioning (W4-201 – W4-215)
201. Move off the current `0.1.0-prototype` version string to a real semantic-versioning scheme
202. Tagged GitHub releases with changelogs for each milestone
203. Release notes template covering new features, fixes, and breaking changes
204. Pre-release/beta channel consideration for testing roadmap items before they hit a stable tag
205. Version compatibility matrix (which dashboard version works with which config schema version, tying into section 9)
206. Automated version-bump tooling
207. Release checklist (tests pass, docs updated, changelog written, tag created, Pages site redeployed)
208. Docker image tagging aligned with releases (not just `latest`)
209. Deprecation policy for any feature that might eventually be removed/replaced
210. Backward-compatibility testing before each release (does upgrading break an existing install)
211. Release announcement process (where/how the user wants to share new releases, if at all)
212. Hotfix process distinct from the normal release cadence for urgent fixes (e.g. a security issue from Week 2's testing)
213. Long-term support consideration (is there a "stable" branch separate from active development)
214. Release-readiness gate tied to the CI pipeline from section 8 (can't tag a release without green CI)
215. First real tagged release (`v1.0.0` or similar) as a concrete milestone marking "the prototype phase is over"

## 14. Community/contribution docs (W4-216 – W4-230)
216. `CONTRIBUTING.md` covering how to propose changes, coding conventions, test expectations
217. Code-of-conduct file
218. Issue triage process documented (labels, priority scheme, how roadmap items get picked up)
219. "Good first issue" labeling applied to a subset of simpler roadmap items for new contributors
220. Contributor recognition (a CONTRIBUTORS file or equivalent)
221. Discussion forum/space decision (GitHub Discussions vs. Issues-only vs. Discord, given the existing Discord bot work)
222. Clear scope statement — what kinds of contributions are welcome vs. out of scope for this project's goals
223. Style guide document (Python + JS conventions actually used across this codebase, extracted from real patterns rather than invented)
224. Review process documentation (who reviews PRs, what the bar is)
225. Roadmap-to-issue conversion process fully documented so external contributors (not just the original agent-driven sessions) can pick up a `roadmap/` item and turn it into real work
226. License clarity for contributions (contributor agreement or equivalent given the noncommercial license)
227. Public roadmap visibility (GitHub Projects board mirroring this `roadmap/` directory) so contributors can see what's planned/in-progress/done
228. Regular triage cadence documented (how often the maintainer reviews new issues/PRs)
229. Escalation path for security reports distinct from normal issues (ties into Week 2 section 14's `SECURITY.md`)
230. Community showcase (a place to share personal setups/photos of the dashboard controlling real bulbs, if there's interest)

## 15. Final month-end retro & next-month planning (W4-231 – W4-250)
231. Full audit of everything actually built this month vs. everything proposed in `roadmap/` — an honest completed/deferred/dropped breakdown
232. Update `FEATURES.md`'s working-feature count to reflect the month's real, tested additions (not the roadmap's aspirational count)
233. Update every `iterations/` entry created this month is cross-linked from `HANDOFF.md`
234. Retro on what took longer than expected and why (informs more realistic estimates for the next planning cycle)
235. Retro on which roadmap sections turned out to be lower-value than expected once explored (be willing to explicitly deprioritize/cut things)
236. Retro on any new real bugs found during the month's work, and whether a pattern suggests a process fix (e.g. "we keep finding blocking-I/O-in-a-callback bugs — maybe add a lint rule for it")
237. User feedback session — what actually got used vs. ignored after a month of availability, informing next month's priorities
238. Decide whether a second month-long roadmap cycle is warranted, or whether a lighter, more reactive backlog process fits better going forward
239. Archive/close out any roadmap items that are no longer relevant (e.g. superseded by a different approach that emerged during the month)
240. Confirm the security-test phase (Week 2, section 5) actually ran against a real deployed instance, not just planned — escalate if it didn't happen
241. Confirm real multi-bulb testing (Week 4, section 5) actually happened if hardware was purchased during the month
242. Publish a public-facing "what's new" summary distinct from the raw changelog, written for actual users rather than developers
243. Re-baseline performance budgets (section 10) against the now-larger, more feature-rich codebase
244. Re-run the full adversarial security checklist (Week 2, section 5) as a lighter recurring pass, not just once
245. Confirm every new feature this month has a corresponding skill file or skill update so future agent sessions stay well-onboarded
246. Draft the next month's roadmap using this month's actual velocity as a more realistic planning input, rather than guessing from scratch again
247. Explicit gratitude/credit note for any community contributions received during the month, if applicable
248. Snapshot the entire repo state (tag, backup, or equivalent) as a clean "end of month 1" reference point
249. Final GitHub Pages deploy reflecting every doc update from the whole month
250. Present the full month's work back to the user in a clear, organized summary — this document's own bookend
