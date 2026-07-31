# Security Policy

Smart Bulb Dashboard is a hobby project for controlling Tuya Wi-Fi bulbs on
your own LAN. It is maintained by one person. This document says what it
does and doesn't claim to protect against, how to report a vulnerability,
and what you can expect back.

## Threat model summary

The full PIN-gate threat model lives in
[`docs/pin-gate-threat-model.md`](docs/pin-gate-threat-model.md). The short
version:

**What this project defends against**

- Casual discovery of an internet-exposed dashboard controlling your bulbs
  (the PIN gate, when you enable it).
- Automated PIN guessing, via a per-IP lockout *and* a separate per-IP
  request-rate limit on the login endpoint.
- Session theft after logout — sessions are invalidated server-side, not
  just cookie-cleared, and can be listed and revoked individually.
- Your `local_key` leaking through the API, logs, or the diagnostic report.

**What it explicitly does NOT defend against**

- **Anyone who can read your network traffic.** The dashboard speaks plain
  HTTP. If you forward a port to the internet without a TLS-terminating
  reverse proxy in front, your PIN and session cookie travel in the clear.
  Use Tailscale (which encrypts end to end) or put Caddy in front.
- **Anyone with an account on the machine running it.** `config.json`
  contains your bulbs' `local_key` in plaintext, because that's what Tuya's
  local protocol requires. Anyone who can read that file can control your
  bulbs directly, dashboard or not.
- **A compromised host.** There is no sandboxing, no privilege separation.
- **Multiple users with different trust levels.** There is exactly one PIN.
  It is shared by everyone you give it to, and the audit log records *what*
  happened, not *who* did it.
- **Denial of service.** Nothing here is hardened against someone
  deliberately trying to exhaust the machine's CPU or bandwidth.
- **A malicious device already on your LAN.** The default posture is
  LAN-only with no authentication at all, on the assumption your home
  network is trusted. If it isn't, enable the PIN gate even for local use.

Read that list before deciding to expose this to the internet. The honest
recommendation is: don't. Use Tailscale.

## No telemetry, ever

This project does not phone home. There is no analytics, no crash
reporting, no update check, no "anonymous usage statistics", no CDN, no
external font, no third-party script. The frontend loads exactly two files
(`/static/app.js`, `/static/style.css`), both served by your own backend.

There is exactly **one** outbound internet request anywhere in the codebase:
the public-IP lookup in `backend/remote_access_status.py`, which asks
`https://api.ipify.org` what your public IP looks like. It:

- runs **only** when you press "Detect Public IP Now" in Settings (or
  `POST /api/system/remote-access/detect-public-ip`) — never at startup,
  never on a timer, never as a side effect of loading a page;
- sends nothing but the bare HTTP request (no identifiers, no payload);
- is overridable via the `SBD_PUBLIC_IP_SERVICE` environment variable if
  you'd rather point at your own endpoint;
- is not required for anything — every other feature works with it never
  called.

The self-diagnostic report (`GET /api/system/diagnostic-report`) makes no
outbound request either, and is returned to the caller rather than written
to disk, so it can't end up sitting in the repo waiting to be committed.

To verify any of this yourself:

```bash
grep -rn "urlopen\|requests\.\|httpx\|socket.create_connection\|subprocess" backend/*.py
```

Everything that turns up is either the bulb's own LAN socket (`tinytuya`,
port 6668), the local `tailscale status` CLI call, or the single opt-in
public-IP lookup above.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a security problem.**

Report privately, whichever is easier for you:

1. **GitHub private vulnerability reporting** — the "Report a vulnerability"
   button under the [Security tab](https://github.com/THEROCKSSS/smart-bulb-dashboard/security)
   of the repo. This is the preferred route.
2. **Email** the maintainer via the address on the
   [THEROCKSSS](https://github.com/THEROCKSSS) GitHub profile, with
   `SECURITY` in the subject.

Please include:

- what you found, and which file/endpoint it's in;
- how to reproduce it (a `curl` command is ideal);
- what an attacker gets out of it;
- your assessment of severity, and whether it needs the PIN gate enabled,
  public exposure, LAN access, or none of the above.

Do not include your real `local_key`, your PIN, or a real session token in
the report. If a proof of concept needs one, use a fabricated value.

### What to expect

This is a one-person hobby project, not a funded product, so the honest
commitment is deliberately modest:

| | Target |
|---|---|
| Acknowledgement | within 7 days |
| Initial assessment (severity + whether it's in scope) | within 14 days |
| Fix for something that lets an unauthenticated attacker control bulbs or read secrets | as fast as I can, and I'll tell you the plan |
| Fix for lower-severity issues | best effort, on the roadmap |

If you don't hear back in 14 days, assume the report got lost and ping the
repo publicly asking me to check my inbox (without disclosing details).

### Coordinated disclosure

I'd appreciate 90 days before public disclosure, or until a fix ships,
whichever comes first. If the issue is being actively exploited, say so and
I'll drop the request. You'll be credited in `CHANGELOG.md` unless you'd
rather not be.

There is **no bug bounty** — no money, no swag. Just credit and my thanks.

## Security update policy

- Security fixes are released as a patch version and called out at the top
  of the `CHANGELOG.md` entry under a **Security** heading, with the
  affected versions named explicitly.
- Only the latest released version is supported. There are no backports to
  older tags; upgrading is `git pull` plus
  `pip install -r backend/requirements.txt` (your `config.json` and
  `backend/data/` are untouched — see `SETUP.md`).
- A fix that requires user action (rotating a PIN, taking down a port
  forward) says so in the changelog entry's first line, not buried in it.

## Dependency vulnerabilities

`backend/requirements.txt` pins exact versions. Scanned with
[`pip-audit`](https://pypi.org/project/pip-audit/):

```bash
python -m pip install pip-audit    # in a throwaway venv, not backend/venv
python -m pip_audit -r backend/requirements.txt
```

### Known findings as of 2026-07-31

All findings are in **`starlette` 0.41.3**, which is a *transitive*
dependency pulled in by `fastapi==0.115.6` (whose own pin is
`starlette>=0.40.0,<0.42.0`). None of the four directly-pinned runtime
packages — `fastapi`, `tinytuya`, `pydantic`, `numpy`, `sounddevice`,
`uvicorn` — had a known advisory at the time of scanning.

| Advisory | CVE | Fixed in | Applies here? |
|---|---|---|---|
| PYSEC-2026-2281 | CVE-2026-48818 | starlette 1.1.0 | **Yes.** `StaticFiles` on Windows can be made to start an outbound SMB connection via a UNC path, leaking the service account's NTLMv2 hash. This project mounts `StaticFiles` and is commonly run on Windows. |
| PYSEC-2026-2280 | CVE-2026-48817 | starlette 1.1.0 | No. Requires `HTTPEndpoint` subclasses registered via `Route(...)`; this project uses only FastAPI function routes. |
| PYSEC-2026-1942 | CVE-2025-62727 | starlette 0.49.1 | **Yes.** Quadratic-time `Range` header parsing in `FileResponse` — a CPU-exhaustion DoS against any file-serving endpoint, which includes `/` and `/static/*`. |
| PYSEC-2026-1941 | CVE-2025-54121 | starlette 0.47.2 | No. Multipart upload path; this project has no file-upload endpoint. |
| PYSEC-2026-249 | — | starlette 1.3.1 | No. `max_fields`/`max_part_size` ignored for urlencoded forms; this API is JSON-only. |
| PYSEC-2026-248 | — | starlette 1.3.0 | Low. `request.url` host confusion via a path not starting with `/`. Nothing here reads `request.url.hostname` for an auth decision. |
| PYSEC-2026-161 | — | starlette 1.0.1 | Low. Same class as above (unvalidated `Host` header in URL reconstruction). The PIN gate keys on `request.url.path` and the cookie, not the host. |

**Status: not yet fixed, deliberately.** Reaching a patched starlette means
upgrading FastAPI past `0.115.6`, which is a real dependency bump that needs
its own verified test pass rather than being smuggled in alongside a feature
branch. Two of the seven are genuinely applicable to this project's
configuration, and both are mitigated in practice by the LAN-only default
(the two applicable ones need the attacker to reach the HTTP port at all).
They are **not** mitigated if you have forwarded a port to the internet.

If you are running this publicly exposed, treat the FastAPI upgrade as the
priority item. Re-run the scan yourself before trusting this table — it is a
point-in-time snapshot, and new advisories land constantly.

## Scope

**In scope:** anything in `backend/`, `frontend/`, `cli/`, the Docker
build, and the documented deployment paths in `SETUP.md` and
`docs/remote-access-security.md`.

**Out of scope:**

- Vulnerabilities in Tuya's own firmware or cloud, or in `tinytuya`
  (report those upstream).
- "The API has no auth by default" — that is the documented, intended
  LAN-only posture. See `docs/pin-gate-threat-model.md`.
- "`config.json` stores the local_key in plaintext" — a known, documented
  tradeoff forced by the Tuya local protocol. Encrypted-at-rest storage is
  a tracked roadmap item (`roadmap/week-2-remote-access-and-security.md`
  section 12), not a vulnerability report.
- Findings from an automated scanner with no demonstrated impact.
