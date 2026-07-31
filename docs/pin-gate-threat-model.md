# PIN Gate — Threat Model

*Roadmap item W2-070. Expands the "what the PIN gate does and doesn't
protect against" section of [`remote-access-security.md`](remote-access-security.md)
into a formal, testable model.*

**Scope:** the PIN gate implemented in `backend/remote_auth.py` and the
middleware in `backend/main.py`, as of v0.3.0 + Week 2 Phase D.
**Last reviewed:** 2026-07-31, against the code in this repo.

This document exists so that "is the PIN gate enough?" has an answer you
can check rather than a vibe. It is deliberately blunt about the gaps.

---

## 1. What is being protected

| Asset | Why it matters | Where it lives |
|---|---|---|
| Bulb control | Someone toggling your lights at 3am, or strobing them | The whole `/api/devices/*` surface |
| `local_key` (per bulb) | Grants *direct* control of the bulb over the LAN, bypassing this dashboard entirely. Cannot be rotated without re-pairing the bulb. | `backend/config.json`, plaintext |
| Device inventory | Device IDs, IPs, names — a map of your home network | `GET /api/devices` |
| The PIN itself | Grants everything above, remotely | Never stored; only a PBKDF2-SHA256 hash (200,000 iterations) + per-install random salt |
| Session signing key | Forging it forges any session | `backend/data/remote_auth.json` |
| Action/audit history | What time you're home, when lights go on/off | `backend/data/auth_audit.log`, in-memory per-bulb history |

## 2. Trust boundaries

```
 internet ──[router / port forward]──> LAN ──> host machine ──> backend process
    (1)              (2)                (3)         (4)              (5)
```

1. **The internet.** Untrusted. Only reachable if *you* forwarded a port.
2. **The router.** Assumed not compromised. If it is, everything below falls.
3. **The LAN.** Assumed trusted by default. This is the load-bearing
   assumption of the whole project — see §5.
4. **The host machine.** Assumed single-user or fully trusted. Anyone with
   read access to `backend/config.json` has the `local_key`.
5. **The backend process.** Trusted. It holds every secret in memory.

The PIN gate sits at exactly one place: boundary (1)→(5), enforced in the
`pin_gate` HTTP middleware. **It is not enforced anywhere else.** It does
nothing about (3) or (4).

## 3. Attackers considered

| # | Attacker | Capability | In scope? |
|---|---|---|---|
| A1 | Internet scanner | Finds your `host:port`, no other access | **Yes** — primary threat |
| A2 | Targeted remote attacker | Knows your DuckDNS name, will spend hours guessing | **Yes** |
| A3 | Network-path observer | Can read traffic between your phone and your house (hostile Wi-Fi, ISP) | **Yes, and NOT defended** |
| A4 | Guest on your LAN | Joined your Wi-Fi | **Partly** — see §5 |
| A5 | Other user on the host OS | Shell on the machine running the backend | **No** — out of scope |
| A6 | Someone you gave the PIN to | Valid credentials, later untrusted | **Partly** — see §6 |
| A7 | Malicious IoT device on the LAN | Already inside boundary (3) | **No** — out of scope |

## 4. What the PIN gate DOES defend against (with evidence)

Each of these is backed by a test in `backend/tests/test_remote_auth.py` or
`backend/tests/test_auth.py`.

**Against A1 (scanner):**
- Every route except `/`, `/static/*`, `/api/auth/login`, `/api/auth/status`
  and `/api/system/health` returns 401 without a valid session cookie. That
  includes the newer observability routes (`/metrics`,
  `/api/system/diagnostic-report`, `/api/system/logs`, …) — asserted in
  `test_system_observability_routes.py::test_new_system_routes_are_gated_by_the_pin_gate`.
- `/` and `/static/*` stay open on purpose: gating them means the PIN entry
  page itself can't load, which was a real total-lockout bug (iteration
  004). They serve no data.

**Against A2 (patient guesser):**
- **Lockout:** 5 wrong PINs from one IP → that IP is locked out for 5
  minutes. Counter resets on success.
- **Rate limit, independently:** a per-IP fixed-window limiter on
  `/api/auth/login` (default 20 requests / 60s, tunable at runtime via
  `POST /api/system/remote-auth/rate-limit` or the `SBD_LOGIN_RATE_LIMIT_*`
  env vars) runs *before* the lockout. This is the one that catches a slow
  guesser spreading attempts across many PINs to stay under the lockout's
  radar.
- **PBKDF2-SHA256, 200,000 iterations**, per-install random salt. An
  offline attack on a stolen `remote_auth.json` is expensive per guess —
  though see §5 on why a 4-digit PIN undercuts this badly.
- **Constant-time comparison** (`hmac.compare_digest`) on both the PIN hash
  and the session signature, so neither leaks via response timing.

**Against A6 / session theft:**
- Sessions are signed (HMAC-SHA256 over an opaque payload) **and** carry a
  random `jti` checked against a server-side allowlist. Tampering with
  either half fails.
- Logout **revokes server-side**. A cookie copied before logout is rejected
  on its next use — not merely deleted client-side.
- Sessions are listable (`GET /api/auth/sessions`) and individually
  revocable, and `POST /api/auth/sessions/revoke-all` also rotates the
  signing key, so even a session with no allowlist entry dies.
- Expiry is enforced server-side, not just by cookie lifetime.
- Cookies are `HttpOnly` (no JS access) and `SameSite=Lax` (blocks
  cross-site state-changing requests).

**Against secret leakage:**
- `local_key` is masked in every device API response (`config.redact`).
- The audit log never receives a PIN or a raw token.
- The diagnostic report strips secrets three ways (field-name matching,
  assignment-pattern matching, and bare-value matching against the live
  config), asserted in
  `test_observability.py::test_diagnostic_report_never_contains_a_real_local_key`
  and `…_never_contains_the_pin_hash_salt_or_signing_key`.

**Against silent misconfiguration (new in Week 2 Phase D):**
- If a request ever arrives from a globally-routable IP while the gate is
  off, that fact is recorded and a persistent banner appears.
- If public exposure is configured (a DuckDNS sync was reported, or you
  declared a port forward) and the gate is *later* turned off, the warning
  returns and stays up across restarts until the gate is re-enabled or you
  explicitly retract the exposure declaration.

## 5. What it explicitly does NOT defend against

This is the important half.

**5.1 — Anyone who can read your traffic (A3). Not defended at all.**
The dashboard speaks plain HTTP. Over a port forward, the PIN and the
session cookie cross the internet in cleartext. An attacker on a hostile
Wi-Fi network, or your ISP, doesn't need to guess anything — they read it.
No amount of PBKDF2 helps here.
*Mitigation:* use Tailscale (encrypted end-to-end, and `tailscale serve`
gives you a real HTTPS endpoint), or put Caddy/Nginx in front for TLS.
Doing neither means the PIN gate is theatre against A3.

**5.2 — A weak PIN.** The minimum is 4 characters and there is *no*
complexity check. A 4-digit numeric PIN is 10,000 possibilities. The rate
limiter and lockout make that slow online, but they are in-memory only and
**reset when the backend restarts** — so an attacker who can trigger or
wait out a restart gets a fresh budget. Against an offline attack on a
stolen `remote_auth.json`, 200k PBKDF2 iterations buys you hours, not
years, at 4 digits.
*Mitigation:* use a long passphrase, not a PIN. The field accepts any
string.

**5.3 — Anything on your LAN (A4, A7).** With the gate disabled (the
default), any device on your Wi-Fi has full unauthenticated control of the
API. With the gate *enabled*, LAN clients are treated exactly like internet
clients — but the bulbs themselves are still directly controllable over the
LAN by anyone holding the `local_key`, dashboard or not.

**5.4 — Host compromise (A5).** No sandboxing, no privilege separation.
`config.json`, `remote_auth.json` and `auth_audit.log` are ordinary files
with default permissions. Anyone with a shell on the box reads them all.

**5.5 — Multi-user anything.** One PIN, shared. Revoking access for one
person means changing the PIN for everyone. The audit log records the
source IP and the outcome, so it tells you *what* happened and *from
where* — not *who*.

**5.6 — Denial of service.** Deliberately not addressed:
- The login endpoint runs 200k PBKDF2 iterations per attempt. The rate
  limiter caps that at ~20/minute per IP, but nothing stops a distributed
  source set from spending your CPU.
- Slow-loris style connection holding is untested.
- Static-file serving inherits `starlette` CVE-2025-62727 (quadratic
  `Range` header parsing) — see `SECURITY.md`.

**5.7 — Reverse-proxy source-IP confusion.** The lockout and rate limiter
key on `request.client.host`. Behind a reverse proxy that would be *the
proxy's* IP for every request, collapsing per-IP tracking into one global
bucket — one attacker could lock out every legitimate user. `X-Forwarded-For`
is **not** currently honoured (and honouring it naively would be worse: a
spoofable header would let an attacker evade the lockout entirely by
rotating a fake value). Tracked as roadmap item W2-038; until it's built,
**do not put this behind a reverse proxy and assume the lockout still
works per-client.**

**5.8 — CSRF beyond `SameSite=Lax`.** There is no CSRF token. `Lax`
blocks cross-site POSTs from a hostile page, which covers the realistic
case, but it is the only line of defence.

**5.9 — CORS.** `allow_origins=["*"]`. Fine for LAN-only; it means any
website your browser visits can *attempt* requests to the dashboard. The
PIN gate's cookie is `SameSite=Lax`, so an unauthenticated cross-origin
attempt fails — but with the gate off, a page you visit could drive your
bulbs. Tightening this is roadmap item W2-080.

**5.10 — Restart-resets.** Lockout counters, rate-limit buckets, request
metrics and per-bulb latency history are all in-memory. Sessions and audit
log entries are not — those persist.

## 6. Residual risk, stated plainly

With the PIN gate **enabled**, a **long passphrase**, and access **only via
Tailscale**: the residual risk is roughly "someone compromises your
Tailscale account, or the host machine". That's a reasonable posture for a
home dashboard.

With the PIN gate **enabled** and a **port forwarded to the internet over
plain HTTP**: you are protected against A1 and A2, and not at all against
A3. Anyone able to observe the network path you connect from gets your PIN.
Whether that matters depends on where you use it from — but you should be
making that decision knowingly, which is the point of this document.

With the PIN gate **disabled** and a **port forwarded**: your bulbs and your
device inventory are public. The dashboard will show you a persistent
warning about this; it is not being dramatic.

## 7. Not yet validated

Everything above was verified by the same project that wrote the code —
unit tests, plus a live 21-check pass against a running instance with the
gate on. That is **not** an adversarial test. The following remain unproven
and are scoped as their own phase (`roadmap/week-2-remote-access-and-security.md`
section 5):

- Real-world brute-force timing against a deployed instance.
- Session-token forgery and replay attempts by someone trying to break it.
- Timing-attack analysis of the PIN comparison path under load.
- Port-scan visibility from an external vantage point.
- Fuzzing every endpoint with the gate enabled.
- Path traversal against static file serving.

Until that phase runs, treat this document as "the model as designed",
not "the model as proven".

## 8. Review cadence

Re-read this against the actual code:

- before any change to `remote_auth.py` or the `pin_gate` middleware;
- before adding any route that handles secrets;
- after any dependency upgrade that touches `starlette`/`fastapi`;
- otherwise, quarterly.

Update the "Last reviewed" date at the top when you do, and say what
changed.
