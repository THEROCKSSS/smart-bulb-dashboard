# 005 — PIN Gate Hardening & API Rate Limiting (Week 2 Phase A)

## Goal
Close out `roadmap/week-2-remote-access-and-security.md` section 4
(W2-051..070, PIN gate hardening) and section 6 (W2-101..120, rate limiting
& abuse prevention) — everything in those sections that can be built and
verified without a real deployed instance or a reverse proxy in front.

## Approach
Backend work, in three pieces:

1. `backend/net_utils.py` (new) — one place that normalizes a client
   address and classifies it as local. Both per-IP subsystems import it, so
   "locked out" and "rate limited" refer to the same client.
2. `backend/remote_auth.py` — lockout policy moved into persisted state
   (env-var defaults) with exponential backoff; a `pins` map replacing the
   single `pin_hash`/`salt` pair, with an in-place migration; `assess_pin()`
   complexity rules; `change_pin()`, `set_session_ttl()`,
   `set_lockout_policy()`, `add_guest_pin()`, `revoke_pin()`,
   `auth_metrics()`.
3. `backend/api_rate_limit.py` (new) + one middleware in `main.py` — a
   sliding-window per-IP limiter with four sensitivity tiers, wired at the
   ASGI layer only.

Verified against a real running server (`uvicorn` on 127.0.0.1:8577) with
raw `urllib` calls, plus 78 new pytest cases.

## What happened
Most of it worked as designed. Three things were genuinely non-obvious and
are the reason this entry exists.

## Failures (if any)

**1. A non-ASCII session cookie returned 500, not 401 (pre-existing bug).**
`hmac.compare_digest`'s `str` form raises
`TypeError: comparing strings with non-ASCII characters is not supported`.
One side of that comparison is the attacker-supplied cookie value, and
Starlette decodes the `Cookie` header as latin-1 — so a single accented byte
in a forged token turned a rejected signature into an unhandled exception.
Reproduced against the real app with the original comparison restored:

```
$ python -c "... remote_auth._compare = lambda a,b: hmac.compare_digest(a,b) ..."
with original compare_digest -> 500
```

This was in `verify_session_token()` and `get_token_jti()` since the session
allowlist shipped. It's an availability/information bug rather than an auth
bypass — the request is still refused — but a 500 tells an attacker their
input reached the comparison, and it's noise in the logs.

**2. Python reports the RFC5737 documentation ranges as private.**
`ipaddress.ip_address("203.0.113.7").is_private` is `True`. The first draft
of the rate-limit tests used those "obviously public" addresses, so every
enforcement assertion passed for the wrong reason — the default LAN
exemption was silently letting them through. Caught because one assertion
(`test_public_addresses_are_not_exempt`) expected a block and got an allow.

**3. The audio panel polls 200×/minute all by itself.**
`frontend/app.js` polls `/audio-reactive/status` every 300ms while a session
runs. A single flat "read" allowance of 240/min would have left one
well-behaved browser tab within 20% of a 429 during normal remote use of a
feature this project considers headline. That's what drove the separate
`poll` tier (default 600/min) rather than one read budget for everything.

## Fix
- `remote_auth._compare()` — compares UTF-8 bytes instead of `str`, keeping
  the constant-time property while accepting any input. Both call sites in
  `verify_session_token()` and `get_token_jti()` now use it, as does the
  multi-PIN comparison loop.
- Tests use real public addresses (`93.184.216.34`, `9.9.9.9`), with a
  comment at the constant explaining why the documentation ranges are wrong
  for this purpose.
- `api_rate_limit.TIER_BY_PATH` maps the polled endpoints to a `poll` tier.

Two other decisions worth recording, because both are the kind of thing a
future refactor would "simplify" away:

- **`api_rate_limit.check()` is called from exactly one place**, the HTTP
  middleware in `main.py`. That is the whole of the W2-111 guarantee: the
  audio engine's `BulbSender` threads dispatch from an in-process queue
  straight to tinytuya and never enter the ASGI stack, so a lightshow cannot
  spend an HTTP budget. Making the limiter callable from service-layer code
  would break that quietly — a long lightshow would start 429-ing the user's
  own browser. `test_check_is_only_ever_called_from_the_http_middleware`
  scans the backend source and fails if a second call site appears.
- **`verify_pin()` does not early-exit on a match.** With multiple PINs,
  returning on the first hit would make a household-PIN login measurably
  faster than a guest-PIN one. Every active PIN is hashed and compared.
  That also makes the count of PINs the CPU cost an unauthenticated request
  can force, which is why guest PINs are capped at 5.

## Verification

Full suite, run from the worktree:

```
$ backend/venv/Scripts/python.exe -m pytest backend/tests/ -q
431 passed, 2 warnings in 34.18s        # 353 before this work
$ backend/venv/Scripts/python.exe -m pytest cli/tests/ -q
22 passed in 0.28s
```

Live server (`uvicorn main:app --port 8577`), real HTTP:

```
weak pin enable  (400, {'detail': 'PIN rejected: it must be at least 6 characters;
                        it is a well-known or development/test PIN;
                        it is a simple ascending/descending sequence'})
test1234 enable  (400, {'detail': 'PIN rejected: it is a well-known or development/test PIN'})
gated no session 401
login            (200, {'ok': True})
guest add        (200, {'id': '9aa765b9...', 'label': 'Sitter', 'kind': 'guest', ...})
change pin       (200, {'ok': True, 'revoked_sessions': 1})
old pin login    (401, {'detail': 'incorrect PIN'})
new pin login    (200, {'ok': True})
```

Rate limiter, exemption turned off so loopback is actually counted:

```
reset       {"exempt_local":false,"limits":{"poll":600,"read":2,"write":120,"expensive":1}}
  attempt 0: 429 retry-after=26 {"detail":"rate limit exceeded for read requests -- slow down"}
  attempt 1: 429 retry-after=26 ...
metrics     {"allowed":7,"blocked":8,"blocked_by_tier":{"read":8},
             "top_blocked_ips":[{"ip":"127.0.0.1","blocked":8}],
             "last_blocked_path":"/api/system/info", ...}
```

Not verified here, and deliberately so: anything needing a reverse proxy in
front (`X-Forwarded-For` handling, W2-112) or a real exposed instance. Those
stay open in `ROADMAP.md`.
