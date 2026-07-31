#!/usr/bin/env python3
"""Post-deploy smoke test for the Smart Bulb Dashboard (W2-050, W2-194).

Run this immediately after any deploy, update, or reverse-proxy change. It
checks the things that are easy to get subtly wrong and hard to notice:
the app is up, the frontend and its assets actually load, the PIN gate is
in the state you think it is, TLS is real and not about to expire, and --
the one this exists for -- that a reverse proxy is passing through the
client's real IP rather than silently collapsing every remote user into
one lockout bucket.

Zero third-party dependencies (stdlib urllib/ssl only), same as
cli/bulbctl.py, so it runs on a bare server with no venv activated.

Usage:
    python3 deploy/smoke-test.py
    python3 deploy/smoke-test.py --base-url https://yourname.duckdns.org
    python3 deploy/smoke-test.py --base-url https://bulbs.lan --insecure
    python3 deploy/smoke-test.py --base-url https://x.duckdns.org --pin 1234
    python3 deploy/smoke-test.py --json

Exit codes:
    0  everything passed (warnings may still be present)
    1  at least one check failed
    2  could not reach the server at all
"""
from __future__ import annotations

import argparse
import datetime
import json
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8500"
# Renewal automation fails quietly -- the failure mode is nothing
# happening. Let's Encrypt certs are 90 days and renew at 30 remaining, so
# under 14 means two renewal windows have already been missed.
CERT_EXPIRY_FAIL_DAYS = 14
CERT_EXPIRY_WARN_DAYS = 30

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


class Results:
    def __init__(self):
        self.rows = []

    def add(self, status, name, detail=""):
        self.rows.append({"status": status, "check": name, "detail": detail})
        return status

    @property
    def failed(self):
        return sum(1 for r in self.rows if r["status"] == FAIL)

    @property
    def warned(self):
        return sum(1 for r in self.rows if r["status"] == WARN)


def build_opener(insecure):
    if not insecure:
        return urllib.request.build_opener()
    # Only for the self-signed LAN case (deploy/nginx/make-selfsigned-cert.sh
    # or Caddy's `tls internal`). It disables the check that would otherwise
    # tell you the cert is wrong, so never point it at a public deployment
    # and conclude from a green run that TLS is healthy.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _normalise_headers(message):
    """Response headers as a plain dict with lowercase keys. HTTP header
    names are case-insensitive and uvicorn sends them lowercased, so a
    dict(resp.headers)["Content-Type"] lookup silently misses -- which read
    as "the app isn't setting a Content-Type" rather than as a bug here.
    Set-Cookie can legitimately repeat, so its values are joined rather
    than having all but the last one dropped."""
    out = {k.lower(): v for k, v in message.items()}
    cookies = message.get_all("Set-Cookie")
    if cookies:
        out["set-cookie"] = "\n".join(cookies)
    return out


def fetch(opener, url, timeout, method="GET", body=None, headers=None, cookie=None):
    """Returns (status, headers, body_text). Header keys are lowercased.
    Never raises for an HTTP error status -- a 401 is a legitimate answer
    here, not an exception."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/html")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return (resp.status, _normalise_headers(resp.headers),
                    resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, _normalise_headers(e.headers), e.read().decode("utf-8", "replace")


def as_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ checks --

def check_health(results, opener, base, timeout):
    """/healthz, the dedicated infrastructure endpoint. If this fails,
    nothing else is worth reporting."""
    try:
        status, _, text = fetch(opener, base + "/healthz", timeout)
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as e:
        results.add(FAIL, "reachable", f"could not connect to {base}: {e}")
        return False
    if status != 200:
        results.add(FAIL, "/healthz", f"expected 200, got {status}")
        return False
    if as_json(text) != {"status": "ok"}:
        results.add(FAIL, "/healthz", f"unexpected body: {text[:120]!r}")
        return False
    results.add(PASS, "/healthz", "200 {'status': 'ok'}")
    return True


def check_frontend(results, opener, base, timeout):
    """The dashboard page and its two assets. A reverse proxy that forwards
    /api but not /static is a real and easy mistake, and it presents as an
    unstyled page rather than an error anyone reports usefully."""
    status, headers, text = fetch(opener, base + "/", timeout)
    if status != 200:
        results.add(FAIL, "GET /", f"expected 200, got {status}")
    elif "<html" not in text.lower():
        results.add(FAIL, "GET /", "response is not HTML")
    else:
        results.add(PASS, "GET /", f"{len(text)} bytes of HTML")

    for path, expected in (("/static/app.js", "javascript"), ("/static/style.css", "css")):
        status, headers, _ = fetch(opener, base + path, timeout)
        ctype = headers.get("content-type", "")
        if status != 200:
            results.add(FAIL, f"GET {path}", f"expected 200, got {status}")
        elif expected not in ctype.lower():
            results.add(WARN, f"GET {path}", f"unexpected Content-Type {ctype!r}")
        else:
            results.add(PASS, f"GET {path}", ctype)


def check_auth_status(results, opener, base, timeout):
    status, _, text = fetch(opener, base + "/api/auth/status", timeout)
    body = as_json(text)
    if status != 200 or body is None:
        results.add(FAIL, "PIN gate", f"/api/auth/status returned {status}")
        return None
    if body.get("enabled"):
        results.add(PASS, "PIN gate", "enabled")
    else:
        # Not a failure on its own -- LAN-only with no exposure is the
        # documented default posture. It IS a failure over a public URL.
        detail = "disabled (correct for LAN-only; NOT safe if this URL is public)"
        results.add(WARN if base.startswith("https") else PASS, "PIN gate", detail)
    return body.get("enabled")


def check_gate_actually_gates(results, opener, base, timeout, gate_enabled):
    """A PIN gate that reports itself enabled but doesn't reject anything
    is worse than no gate, because you'd stop looking."""
    if not gate_enabled:
        results.add(SKIP, "gate enforcement", "PIN gate is disabled")
        return
    status, _, _ = fetch(opener, base + "/api/devices", timeout)
    if status == 401:
        results.add(PASS, "gate enforcement", "/api/devices -> 401 without a session")
    else:
        results.add(FAIL, "gate enforcement",
                    f"/api/devices returned {status} with no session, expected 401")


def check_login(results, opener, base, timeout, pin):
    """Optional: prove the PIN actually works end to end, and that the
    session cookie carries the flags it should for this scheme."""
    if not pin:
        results.add(SKIP, "login", "no --pin given")
        return None
    status, headers, text = fetch(opener, base + "/api/auth/login", timeout,
                                   method="POST", body={"pin": pin})
    if status != 200:
        detail = (as_json(text) or {}).get("detail", text[:120])
        results.add(FAIL, "login", f"POST /api/auth/login -> {status}: {detail}")
        return None
    set_cookie = headers.get("set-cookie", "")
    results.add(PASS, "login", "PIN accepted, session issued")

    lowered = set_cookie.lower()
    if "httponly" not in lowered:
        results.add(FAIL, "cookie HttpOnly", "session cookie is readable from JavaScript")
    else:
        results.add(PASS, "cookie HttpOnly", "set")

    # W2-036. Over HTTPS the flag must be present; over plain HTTP it must
    # be absent, or the browser drops the cookie and login silently fails.
    if base.startswith("https://"):
        if "secure" in lowered:
            results.add(PASS, "cookie Secure", "set (served over HTTPS)")
        else:
            results.add(FAIL, "cookie Secure",
                        "missing over HTTPS -- the app does not believe this "
                        "connection is TLS. Behind a proxy, check that "
                        "X-Forwarded-Proto is set and SBD_TRUSTED_PROXIES "
                        "names the proxy.")
    else:
        if "secure" in lowered:
            results.add(FAIL, "cookie Secure",
                        "set over plain HTTP -- browsers will DISCARD this "
                        "cookie and login will appear to do nothing")
        else:
            results.add(PASS, "cookie Secure", "correctly absent over plain HTTP")

    return set_cookie.split(";")[0] if set_cookie else None


def check_proxy_awareness(results, opener, base, timeout, cookie):
    """W2-038, and the reason this script exists. Behind a proxy without
    SBD_TRUSTED_PROXIES, every remote client shares one lockout bucket:
    one attacker's five wrong PINs lock out everyone. Nothing about that
    is visible from the outside -- the dashboard works perfectly right up
    until it locks the whole household out."""
    status, _, text = fetch(opener, base + "/api/system/proxy-status", timeout, cookie=cookie)
    if status == 401:
        results.add(SKIP, "proxy trust",
                    "gated; re-run with --pin to check reverse-proxy IP handling")
        return
    body = as_json(text)
    if status != 200 or body is None:
        results.add(WARN, "proxy trust", f"/api/system/proxy-status returned {status}")
        return

    trusted = body.get("settings", {}).get("trusted_proxies", [])
    invalid = body.get("settings", {}).get("invalid_trusted_proxy_entries", [])
    peer, client = body.get("peer_ip"), body.get("client_ip")

    if invalid:
        results.add(FAIL, "proxy trust",
                    f"unparseable SBD_TRUSTED_PROXIES entries ignored: {invalid}")

    if body.get("peer_rewritten_by_server"):
        results.add(FAIL, "proxy headers",
                    "uvicorn rewrote the client address from X-Forwarded-For before "
                    "the app saw it, so SBD_TRUSTED_PROXIES is not what decides "
                    "whether that header is believed. Restart uvicorn with "
                    "--no-proxy-headers.")
    else:
        results.add(PASS, "proxy headers", "the app owns forwarded-header trust")

    if not trusted:
        # Only a problem if a proxy is actually in play. The giveaway is
        # the app seeing loopback while you connected from elsewhere.
        looks_proxied = peer in ("127.0.0.1", "::1") and not base.startswith(
            ("http://127.0.0.1", "http://localhost", "http://[::1]"))
        if looks_proxied:
            results.add(FAIL, "proxy trust",
                        f"app sees peer {peer} but you connected remotely -- a proxy is "
                        "in front and SBD_TRUSTED_PROXIES is unset, so the PIN gate's "
                        "per-IP lockout is keyed to the proxy. One attacker will lock "
                        "out every remote user.")
        else:
            results.add(PASS, "proxy trust", "no proxy configured, none detected")
    elif body.get("peer_is_trusted_proxy"):
        if client and client != peer:
            results.add(PASS, "proxy trust",
                        f"real client IP resolved: {client} (via proxy {peer})")
        else:
            results.add(FAIL, "proxy trust",
                        f"proxy {peer} is trusted but no client IP came through "
                        "(client_ip == peer_ip). The proxy is not sending "
                        "X-Forwarded-For.")
    else:
        results.add(WARN, "proxy trust",
                    f"SBD_TRUSTED_PROXIES={trusted} but this request's peer ({peer}) "
                    "is not in it -- either you bypassed the proxy, or the list is wrong")


def check_tls(results, base, timeout, insecure):
    """W2-046: certificate validity and days remaining. Reads the cert off
    a fresh socket rather than the response, because a trusted-chain
    failure has to be observable -- it's the whole point of the check."""
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https":
        results.add(SKIP, "TLS", "not an https:// URL")
        return
    host = parsed.hostname
    port = parsed.port or 443

    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                proto = tls.version()
    except ssl.SSLCertVerificationError as e:
        results.add(FAIL, "TLS certificate",
                    f"not trusted: {e.verify_message or e}. Self-signed? re-run with --insecure")
        return
    except (OSError, socket.timeout, ssl.SSLError) as e:
        results.add(FAIL, "TLS certificate", f"handshake failed: {e}")
        return

    results.add(PASS, "TLS protocol", proto)
    if proto in ("TLSv1", "TLSv1.1"):
        results.add(WARN, "TLS protocol", f"{proto} is deprecated; prefer TLS 1.2+")

    if insecure:
        results.add(SKIP, "TLS certificate", "--insecure: chain and hostname not verified")

    if not cert or "notAfter" not in cert:
        # CERT_NONE gives an empty dict -- expected under --insecure.
        results.add(SKIP, "certificate expiry", "no certificate detail available")
        return

    expires = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
    expires = expires.replace(tzinfo=datetime.timezone.utc)
    days = (expires - datetime.datetime.now(datetime.timezone.utc)).days
    detail = f"{days} days remaining (expires {expires:%Y-%m-%d})"
    if days < CERT_EXPIRY_FAIL_DAYS:
        results.add(FAIL, "certificate expiry",
                    detail + " -- renewal has almost certainly stopped working")
    elif days < CERT_EXPIRY_WARN_DAYS:
        results.add(WARN, "certificate expiry", detail)
    else:
        results.add(PASS, "certificate expiry", detail)


def check_security_headers(results, opener, base, timeout):
    status, headers, _ = fetch(opener, base + "/", timeout)
    hsts = headers.get("strict-transport-security")
    if base.startswith("https://"):
        if hsts:
            results.add(PASS, "HSTS", hsts)
        else:
            results.add(WARN, "HSTS",
                        "not set. Enable it at the proxy (recommended) or with "
                        "SBD_HSTS=on. Note browsers ignore HSTS on bare IPs.")
    else:
        if hsts:
            results.add(WARN, "HSTS", "sent over plain HTTP, where browsers ignore it")
        else:
            results.add(SKIP, "HSTS", "not applicable over plain HTTP")


# -------------------------------------------------------------------- main --

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Post-deploy smoke test for the Smart Bulb Dashboard.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"dashboard base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--pin", default=None,
                        help="if given, also verify the PIN gate accepts it and "
                             "check the session cookie's flags")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS verification (self-signed LAN certs only)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    opener = build_opener(args.insecure)
    results = Results()

    if not check_health(results, opener, base, args.timeout):
        emit(results, args.as_json, base)
        return 2

    check_frontend(results, opener, base, args.timeout)
    check_tls(results, base, args.timeout, args.insecure)
    check_security_headers(results, opener, base, args.timeout)
    gate_enabled = check_auth_status(results, opener, base, args.timeout)
    check_gate_actually_gates(results, opener, base, args.timeout, gate_enabled)
    cookie = check_login(results, opener, base, args.timeout, args.pin)
    check_proxy_awareness(results, opener, base, args.timeout, cookie)

    emit(results, args.as_json, base)
    return 1 if results.failed else 0


def emit(results, machine_readable, base):
    if machine_readable:
        print(json.dumps({
            "base_url": base,
            "failed": results.failed,
            "warnings": results.warned,
            "checks": results.rows,
        }, indent=2))
        return
    print(f"Smart Bulb Dashboard smoke test -- {base}\n")
    width = max(len(r["check"]) for r in results.rows)
    for row in results.rows:
        print(f"  [{row['status']:4}] {row['check']:<{width}}  {row['detail']}")
    print()
    if results.failed:
        print(f"{results.failed} check(s) FAILED, {results.warned} warning(s).")
    elif results.warned:
        print(f"All checks passed, with {results.warned} warning(s) worth reading.")
    else:
        print("All checks passed.")


if __name__ == "__main__":
    sys.exit(main())
