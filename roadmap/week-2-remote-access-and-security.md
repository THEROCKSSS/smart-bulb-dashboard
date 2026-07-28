# Week 2 — Remote Access, Security & Infrastructure

Builds on the PIN gate (`iterations/004-pin-gate-remote-auth/`) and
`docs/remote-access-security.md`. This week's centerpiece is section 5 —
the dedicated adversarial security-test phase, which should be treated as
its own mini-project with a real deployed target, not simulated locally.

## 1. DuckDNS / dynamic DNS hardening (W2-001 – W2-015)
1. Automated DuckDNS update via a scheduled task/cron instead of manual script runs
2. Health check that confirms the DuckDNS record actually matches the current public IP, alerts on drift
3. Support for alternative dynamic DNS providers (No-IP, Cloudflare Dynamic DNS) as documented alternatives
4. IPv6 support for dynamic DNS setups (many ISPs now dual-stack)
5. Documented failure mode: what happens/what a user should check if DuckDNS's IP update silently stops working
6. A dashboard Settings display showing the current detected public IP and last successful DuckDNS sync time
7. Warning banner if the dashboard detects it's reachable from a public-looking IP but the PIN gate is disabled
8. Automatic PIN-gate-required nudge if a public port-forward is detected (best-effort heuristic, not foolproof)
9. Documented router-specific port-forwarding walkthroughs for a few common consumer router brands
10. UPnP-based automatic port forwarding as an optional convenience (with clear security caveats documented)
11. Fail-safe: if DuckDNS/public exposure is configured but the PIN gate later gets disabled, surface a persistent warning until re-enabled
12. Non-default port randomization helper/reminder in the enable-remote-access flow
13. Domain-based access allowlist (only serve to requests presenting a specific Host header) as an extra layer
14. Documented teardown steps (how to safely disable public exposure again, not just how to set it up)
15. Test plan: confirm the dashboard behaves correctly when DuckDNS's IP briefly lags behind an actual IP change

## 2. Tailscale deepening (W2-016 – W2-030)
16. Documented Tailscale ACL example scoping which tailnet devices can reach the dashboard port
17. Tailscale Funnel exploration (Tailscale's own public-exposure feature) as an alternative to DuckDNS+port-forward
18. MagicDNS name auto-detection surfaced in the dashboard's own Settings (show the tailnet-reachable URL)
19. Tailscale SSH exploration for remote admin access to the host machine itself (separate from the dashboard's own auth)
20. Documented mobile Tailscale + dashboard PWA combo as the recommended "phone control from anywhere" setup
21. Tailscale-aware "you're on the tailnet" indicator in the dashboard UI (skip showing the PIN prompt if already on a trusted tailnet, configurable)
22. Fallback behavior definition if Tailscale is the only access path and it goes down (no public path also configured)
23. Multi-device Tailscale testing checklist (phone + laptop + tablet all reaching the same instance)
24. Tailscale exit-node compatibility check (confirm dashboard access still works when a device is also using Tailscale as an exit node)
25. Guide for combining Tailscale (trusted access) with a *separate* DuckDNS+PIN path (untrusted/guest access) simultaneously
26. Tailscale tags/ACL example for a household with multiple trust levels (family vs. guest devices)
27. Automated Tailscale status check surfaced in Diagnostics (is Tailscale even running on this host)
28. Tailscale-specific troubleshooting section in `docs/remote-access-security.md`
29. Evaluate Tailscale Serve (local HTTPS termination) as a way to get real TLS without a separate reverse proxy
30. Cost/complexity comparison table (Tailscale vs. DuckDNS+PIN vs. VPN-on-router) added to the security doc for user decision-making

## 3. TLS / reverse proxy (W2-031 – W2-050)
31. Caddy reverse-proxy reference config for automatic Let's Encrypt certs on a DuckDNS domain
32. Nginx alternative reverse-proxy reference config for users who prefer it
33. Document the exact cert-renewal automation (Caddy handles this natively; confirm and describe for Nginx too)
34. HSTS header configuration once TLS is in place
35. Redirect-to-HTTPS behavior once a reverse proxy is added (with a clear opt-out for LAN-only HTTP use)
36. Confirm the PIN-gate session cookie gets `Secure` flag automatically applied once served over HTTPS
37. WebSocket/SSE compatibility check through the reverse proxy (relevant if live-updating features are added later)
38. Reverse-proxy-aware `X-Forwarded-For` handling in the PIN gate's IP-based lockout (currently would misattribute all requests to the proxy's IP without this)
39. Rate-limiting at the reverse-proxy layer as a second line of defense in front of the app-level lockout
40. Documented Docker Compose example bundling the dashboard + Caddy together for a one-command TLS-enabled deploy
41. Client-certificate (mTLS) exploration as a stronger alternative to PIN-only auth for advanced users
42. Automatic self-signed-cert fallback for LAN-only HTTPS (no public domain needed) as a lighter-weight option
43. Mixed-content check — confirm no dashboard asset breaks when served over HTTPS vs HTTP
44. Reverse-proxy health-check endpoint separate from the app's own `/api/system/health`
45. Documented reverse-proxy log format so security-relevant events (repeated 401s) are visible at the proxy layer too
46. Certificate expiry monitoring/alerting
47. Support for a custom/purchased domain instead of only `*.duckdns.org`
48. HTTP/2 support verification through the chosen reverse proxy
49. Reverse-proxy-level request size limits (defense against abusive payloads)
50. End-to-end TLS setup test: fresh machine, follow the docs verbatim, confirm a working HTTPS URL results

## 4. PIN gate hardening (W2-051 – W2-070)
51. Configurable lockout duration and attempt threshold (currently hardcoded 5 attempts / 5 minutes)
52. Exponential backoff instead of a flat lockout window (escalating delay per repeated lockout)
53. Optional email/webhook notification on repeated failed PIN attempts
54. PIN complexity guidance in the UI (warn against `1234`-style PINs, don't silently allow the test PINs used during development)
55. PIN rotation reminder (nudge after N months since last change)
56. Multiple PINs support (e.g. one for the household, a separate revocable one for a guest — foundation for Week 3's multi-user work)
57. Session listing/management — see all currently-valid sessions and revoke one without changing the PIN
58. "Remember this device" longer-lived token option vs. a short-lived default session
59. Two-factor option (PIN + a TOTP code) for higher-security setups
60. Audit log specifically for auth events (successful logins, failures, lockouts) separate from the general action history
61. Rate-limit the login endpoint itself at the network layer (not just via the attempt-counter) against very fast automated guessing
62. Constant-time PIN comparison review (confirm `hmac.compare_digest` usage is correctly applied everywhere sensitive)
63. Session token rotation on privilege-sensitive actions (e.g. re-issue token after changing the PIN itself)
64. Clear-all-sessions action (force every currently-logged-in client to re-authenticate, e.g. after a suspected compromise)
65. Configurable session TTL exposed in the Settings UI (currently only settable via the API)
66. PIN gate applied to the WebSocket/SSE layer too, once those exist (currently N/A, tracked for when they're added)
67. Brute-force simulation test as an automated regression (confirm lockout still triggers correctly after any future refactor)
68. Distinguish "locked out" from "wrong PIN" more carefully in logs (already distinguished in the API response; extend to the audit log)
69. IPv6 address handling correctness check for the per-IP lockout tracking
70. Formal threat model document specifically for the PIN gate (what it protects against, explicitly what it doesn't — expand the existing doc section)

## 5. Adversarial security-test phase (W2-071 – W2-100)
This is the big one — a dedicated agent (or the user, or a delegated
security-focused session) actually attacking a **real, deployed** instance
(DuckDNS + port forward + PIN gate enabled), not a same-machine
simulation. Treat this as its own mini-project with a written report at
the end.
71. Port-scan the exposed host from an external vantage point, confirm only the intended port is reachable
72. Attempt PIN brute-force against the live lockout mechanism, measure actual real-world timing/effectiveness
73. Attempt session token forgery (tamper with the HMAC signature, confirm rejection)
74. Attempt session token replay after logout (confirm the token is actually invalidated, not just cookie-cleared client-side)
75. Timing-attack analysis on the PIN comparison and session verification paths
76. Attempt to bypass the lockout via a spoofed `X-Forwarded-For` header (relevant once a reverse proxy is added — confirm the app trusts the right source)
77. Fuzz every API endpoint with malformed/oversized payloads while the PIN gate is enabled, confirm no crash or auth bypass
78. Attempt path-traversal against the static file serving (`/static/../../config.json` style attempts)
79. Confirm `local_key` truly never appears in any API response, error message, or log line reachable externally
80. CORS policy review — the current `allow_origins=["*"]` is fine for LAN-only but should be tightened before any public exposure; test the actual exposure
81. Attempt CSRF against state-changing endpoints (bulb control, PIN disable) — confirm cookie `SameSite` settings actually prevent it
82. Dependency vulnerability scan (`pip-audit`/`npm audit`-equivalent) against the full `requirements.txt`
83. Confirm the PBKDF2 iteration count (200k) is still reasonable against current hardware — revisit annually
84. Attempt to enumerate valid device IDs/configuration details via error-message differences (information leakage check)
85. Load-test the login endpoint specifically for a denial-of-service angle (can repeated PBKDF2 hashing be used to exhaust CPU?)
86. Confirm the `/api/system/health` and other always-open endpoints don't themselves leak sensitive info
87. Review whether the discovery/scan endpoints could be abused remotely if accidentally left reachable (confirm auth gate covers them)
88. Social-engineering-adjacent review: confirm no default/backdoor PIN or bypass exists anywhere in the codebase
89. Confirm HTTPS is actually enforced (not just recommended) once a reverse proxy is in place, via an external HTTP-downgrade attempt
90. Test behavior under a slow-loris style connection-holding attack against the exposed port
91. Confirm log files (backend logs, history) don't themselves become a secondary information-leak vector if the host is otherwise compromised
92. Verify file permissions on `config.json`/`remote_auth.json`/`discovery.json` are appropriately restrictive on a shared/multi-user host OS
93. Attempt privilege escalation from "PIN-authenticated user" to "host machine access" — confirm the dashboard has no path to arbitrary code execution
94. Review the Docker deployment path specifically for container escape / excessive privilege concerns
95. Confirm audio-reactive endpoints can't be abused to exhaust resources remotely (e.g. spamming session start/stop)
96. Document every finding from this phase, severity-ranked, in a `roadmap/security-test-report.md` (template to be created alongside this phase)
97. Re-test after each fix from this phase to confirm it actually closed the gap (a mini regression pass)
98. Publish a redacted summary of what was tested and fixed as part of the project's transparency (not full exploit detail, but "we tested X and fixed Y")
99. Establish a recurring cadence (e.g. before each future public-facing feature ships) for repeating a lighter version of this pass
100. Decide, based on findings, whether the current PIN-gate approach is sufficient long-term or whether Week 2's multi-user/2FA items should be prioritized sooner

## 6. Rate limiting & abuse prevention (W2-101 – W2-120)
101. General API rate limiting (requests/minute per IP), independent of the auth-specific lockout
102. Configurable rate-limit thresholds per endpoint sensitivity (bulb control vs. read-only status)
103. Graceful 429 responses with a `Retry-After` header instead of silent drops
104. Rate-limit exemption for the local LAN/loopback by default (don't throttle trusted local use)
105. Abuse-pattern detection (e.g. many devices being toggled rapidly) surfaced as a Diagnostics warning
106. Configurable global "max commands per bulb per minute" safety ceiling, independent of any single feature's own pacing
107. IP allowlist/denylist management in Settings
108. Automatic temporary ban escalation for an IP that repeatedly hits rate limits after warnings
109. Rate-limit metrics exposed in Diagnostics (how often limits are actually being hit, by whom)
110. Distinguish rate-limiting for authenticated vs. unauthenticated requests once multi-user exists
111. Confirm rate limiting doesn't accidentally throttle the audio-reactive sender's own legitimate high-frequency internal calls (scope correctly to the public API surface, not internal dispatch)
112. Test rate limiting behavior specifically through a reverse proxy (make sure real client IPs are used, not the proxy's)
113. Configurable notification when rate limits are being hit repeatedly (possible attack in progress)
114. Document recommended rate-limit defaults for LAN-only vs. publicly-exposed setups (different needs)
115. Ensure rate limiting state (like lockout state) is documented as in-memory/restart-resets, consistent with the PIN gate's existing tradeoff
116. Consider a persisted rate-limit store (e.g. SQLite) if in-memory-only proves insufficient in practice
117. Confirm effects/schedule engine background threads aren't themselves capable of tripping the bulb's own physical rate limits under a misconfigured rule
118. Load test the full API surface to establish real baseline "normal use" request rates before setting limit defaults
119. Document the interaction between dwell-based bulb pacing (already a form of rate limiting) and this new API-level rate limiting so they're understood as separate concerns
120. Regression test suite for rate limiting itself (confirm limits trigger and reset correctly over time)

## 7. Multi-user auth exploration (W2-121 – W2-140)
121. Design doc for a real multi-user system (usernames + per-user credentials, not a single shared PIN)
122. Role-based access (admin vs. guest/read-only) building on the existing PIN gate's session mechanism
123. Per-user audit trail in the history log (who did what, not just what happened)
124. Guest link generation — a time-limited, scoped-down access link shareable without exposing the main PIN
125. Per-user allowed-device/zone restrictions (a guest can only control the living room, not the whole house)
126. Migration path from the current single-PIN model to multi-user without breaking existing setups
127. Password reset flow design (currently N/A with a single PIN; needed once real accounts exist)
128. Session management UI showing all users' active sessions (admin view)
129. Per-user rate limits distinct from the global IP-based ones
130. OAuth/passkey exploration as a longer-term alternative to password-based multi-user auth
131. Family-sharing-style setup (one "owner" account, invited "member" accounts) modeled loosely on how the Tuya app itself handles device sharing
132. Decide whether multi-user is worth the complexity for a single-household dashboard, or whether the guest-link approach (item 124) covers the real need more simply
133. Prototype a minimal version (just guest links, no full account system) as a faster win before committing to full multi-user
134. Document security tradeoffs of each approach considered (this section is explicitly exploratory, not committed)
135. User feedback checkpoint — this entire section should be revisited based on whether Week 1-2's simpler PIN gate actually proves insufficient in practice first
136. If built: per-user theme/dashboard-layout preferences
137. If built: per-user notification preferences
138. If built: admin ability to revoke a specific user's access instantly
139. If built: audit log export per user for personal review
140. If built: rate-limit and lockout behavior specifically tested per-user, not just per-IP

## 8. Audit logging & alerting (W2-141 – W2-160)
141. Dedicated security-events log distinct from the general action history (auth attempts, lockouts, config changes)
142. Configurable alert thresholds (e.g. "notify me after 3 failed logins") surfaced via a webhook/notification channel
143. Log rotation/retention policy for the security-events log
144. Exportable audit log (CSV/JSON) for external review
145. Real-time alert on remote-auth being disabled (a security-relevant config change worth flagging prominently)
146. Real-time alert on a new device being added to `config.json` (in case of unauthorized access)
147. Change-tracking for sensitive config files (config.json, remote_auth.json) — log every modification with a timestamp
148. Integration with the existing Discord-bot idea (Week 3) for real-time security alerts to a phone
149. Local-only notification option (browser notification) for users who don't want external integrations
150. Configurable severity levels for different audit event types
151. Correlation view — line up a security event (e.g. a lockout) with what was happening on the bulbs at that time
152. Tamper-evidence for the audit log itself (append-only, checksum-verified) so a compromise can't quietly erase its own trail
153. Scheduled audit-log digest (weekly summary emailed/notified, even with no incidents, as a "still working" heartbeat)
154. Configurable retention limits to avoid unbounded log growth on a long-running install
155. Audit log search/filter UI in the dashboard itself
156. Alert fatigue consideration — sensible defaults so legitimate daily use doesn't generate constant noise
157. Documented incident-response checklist (what to actually do if the audit log shows something suspicious)
158. Test the alerting pipeline end-to-end (deliberately trigger a lockout, confirm the alert actually arrives)
159. Distinguish "informational" security log entries (PIN gate enabled) from "actionable" ones (lockout triggered) in the UI
160. Periodic self-test — a scheduled job that verifies the alerting pipeline itself is still functioning (canary event)

## 9. Backup / restore / config sync (W2-161 – W2-175)
161. One-click full backup (config.json + data/ + remote_auth.json, excluding the plaintext PIN itself) as a downloadable archive
162. Encrypted backup option (password-protect the export, since it contains device local_keys)
163. Scheduled automatic backups to a configurable location
164. Restore flow with a clear confirmation step (this overwrites current config — make sure that's obvious)
165. Backup versioning (keep the last N backups, not just the most recent)
166. Cross-machine migration guide (moving the whole dashboard to a new host using a backup)
167. Selective restore (bring back just favorites, or just schedules, without touching device credentials)
168. Backup integrity check (verify a backup file isn't corrupted before offering to restore from it)
169. Cloud-storage backup destination options (documented, optional, not required)
170. Backup exclusion list (let a user exclude specific data, e.g. history log, from backups to keep them small)
171. Config diff tool — compare current config against a backup before restoring, show exactly what would change
172. Automatic pre-restore safety backup (back up current state before overwriting with a restored one)
173. Test restore procedure documented and periodically re-verified (a backup nobody's ever restored from isn't trustworthy)
174. Backup/restore covered by its own iteration-log-style test writeup once built
175. Document how this interacts with the PIN gate specifically (a restored config shouldn't silently re-enable/disable remote access unexpectedly)

## 10. Deployment / infra (W2-176 – W2-195)
176. Systemd service file for running the backend as a proper Linux service (auto-restart on crash/reboot)
177. Windows service wrapper (NSSM or similar) for running without a visible terminal window
178. NAS-friendly deployment guide (Synology/QNAP Docker instructions)
179. Raspberry Pi deployment guide and performance notes (audio capture + FFT load on lower-powered hardware)
180. Health-check-based auto-restart (if `/api/system/health` stops responding, restart the process)
181. Resource limits documentation (expected RAM/CPU footprint at idle vs. during an active audio session)
182. Multi-instance guidance (running two independent installs on one machine for testing vs. production)
183. Update/upgrade procedure documentation (how to pull latest code without losing local config)
184. Version-pinning guidance for dependencies to avoid surprise breakage on `pip install -r requirements.txt` re-runs
185. Docker image size optimization pass
186. Docker healthcheck directive added to `docker-compose.yml`
187. Documented reverse-proxy + Docker combined deployment (ties into section 3)
188. Automatic dependency vulnerability alerts (Dependabot-style) once this is on GitHub with Pages/CI set up
189. Staging vs. production config separation guidance for anyone running more than one instance
190. Documented rollback procedure if an update breaks something
191. Log aggregation guidance for anyone running this alongside other home-server services
192. Resource monitoring integration guidance (e.g. exposing basic metrics for a Grafana/Prometheus setup, optional)
193. Documented minimum supported Python version and OS versions
194. Automated smoke test script anyone can run post-deploy to confirm the install is healthy
195. "Getting help" documentation section (where to file issues, what info to include)

## 11. Network resilience (W2-196 – W2-210)
196. mDNS/Bonjour advertisement so the dashboard is discoverable on the LAN by hostname, not just IP
197. Static-IP/DHCP-reservation setup guide per common router brand (reduces "IP changed" issues generally, not just for discovery)
198. Automatic reconnection logic if the backend loses network connectivity entirely and regains it
199. Graceful behavior if the LAN itself goes down but Tailscale/internet remains up (or vice versa)
200. Dual-homing guidance (host machine with both Wi-Fi and Ethernet) and which the dashboard should prefer
201. Network change detection (log when the host's own IP changes, useful for debugging remote-access issues)
202. Explicit guidance on firewall rules needed for LAN-only operation (what ports, what's safe to leave closed)
203. IPv6-only network compatibility check
204. VLAN-segmented network guidance (running the dashboard/bulbs on a separate IoT VLAN from main devices)
205. Guest-network compatibility notes (many IoT bulbs don't work well on isolated guest networks — document the tradeoff)
206. Router reboot resilience (auto-reconnect discovery/scanning after a router restart)
207. Captive-portal detection avoidance (relevant if ever run on a machine that roams networks)
208. Network latency monitoring between the backend and each bulb, surfaced in Diagnostics over time (not just a point-in-time test)
209. Documented behavior when multiple network interfaces could all reach the bulb (which one tinytuya actually uses)
210. Test plan for a full network topology change (new router, new subnet) and what config needs updating

## 12. Secrets management (W2-211 – W2-225)
211. Environment-variable-based secrets option as an alternative to plaintext `config.json` for the truly security-conscious
212. Integration with a local secrets manager (e.g. `keyring`) for storing local_keys instead of a JSON file
213. Encrypted-at-rest `config.json` option (password-protected, decrypted only in memory at runtime)
214. Secrets redaction audit — systematically confirm every log line, error message, and API response redacts local_key correctly (extends the ad-hoc checks already done)
215. `.env` file support with a documented `.env.example` template
216. Secure secret rotation procedure (how to update a local_key after re-pairing a bulb without a full reconfigure)
217. Confirm the PIN itself is never logged anywhere, including in debug-level logs
218. Confirm the session-signing secret key is generated with sufficient entropy and never logged
219. Documented secure-deletion guidance for old backup files containing secrets
220. Secrets scanning as a CI check (prevent an actual `config.json` from ever being accidentally committed, beyond just `.gitignore`)
221. Key-rotation reminder system (nudge to rotate the session-signing secret periodically)
222. Hardware-security-module/TPM exploration for secret storage on supporting hardware (stretch, likely overkill for this project's scale)
223. Clear documentation on the actual sensitivity level of each secret (local_key = bulb control; PIN = dashboard access; session secret = auth integrity) so users understand what's at stake
224. Confirm secrets aren't inadvertently included in diagnostic/support-bundle exports if that feature (Week 4) gets built
225. Regular (e.g. quarterly) manual secrets-handling review as a documented recurring task

## 13. Observability / monitoring (W2-226 – W2-240)
226. Basic metrics endpoint (`/metrics` in Prometheus format) for uptime, request counts, error rates
227. Dashboard-level "system health" summary page distinct from per-device Diagnostics
228. Historical uptime graph for the backend process itself, not just individual bulbs
229. Alerting integration for backend crashes/restarts (tie into section 8's notification channels)
230. Resource usage graphs (CPU/RAM over time) surfaced in the dashboard for self-hosted awareness
231. Request latency percentiles (p50/p95/p99) tracked per endpoint
232. Error-rate tracking and alerting (spike in 500s indicates something's wrong)
233. Synthetic monitoring — a scheduled self-check that exercises key flows (status, a benign command) and alerts if they fail
234. Dependency health checks (confirm tinytuya/sounddevice imports and basic functionality at startup, fail fast with a clear message if not)
235. Log-level configuration exposed in Settings (debug/info/warning) without needing to edit code
236. Centralized log viewer in the dashboard itself (tail recent backend logs from the UI)
237. Correlation IDs on requests for easier tracing through logs during debugging
238. Documented baseline "healthy" metrics so a user can recognize when something's degraded
239. Export monitoring data for external tools (Grafana dashboard template, optional)
240. Self-diagnostic report generator (bundle logs + config summary + recent history for sharing when asking for help, secrets redacted)

## 14. Misc security ideas (W2-241 – W2-250)
241. Security.md file at repo root documenting how to responsibly report a vulnerability
242. Documented security update policy (how fixes get communicated/released)
243. Dependency license compliance check (relevant given the project's noncommercial license terms)
244. Explicit statement of what this project does and doesn't claim to protect against, prominently in the README, not just buried in docs
245. Periodic re-read of `docs/remote-access-security.md` against the actual current codebase to catch documentation drift
246. Security-focused code review pass specifically on `remote_auth.py` and the middleware, independent of the adversarial test in section 5
247. Consider a bug-bounty-lite approach (explicit invitation for responsible disclosure) once this is more widely used
248. Supply-chain review of `tinytuya`/`sounddevice`/`numpy`/`fastapi` for known past CVEs
249. Confirm no telemetry/analytics of any kind phones home anywhere in the codebase (this project should stay fully local/offline-capable by design)
250. Final Week 2 retro — which of the above actually got built, which got deferred, and why, written up before starting Week 3
