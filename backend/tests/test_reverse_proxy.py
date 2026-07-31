"""Tests for reverse-proxy / TLS awareness (Week 2, W2-034..038, W2-043,
W2-044): trusted-proxy X-Forwarded-For handling in the PIN gate's per-IP
lockout, the conditional `Secure` cookie flag, HSTS, redirect-to-HTTPS,
the infrastructure health endpoint, and a mixed-content check on the
shipped frontend assets.

The X-Forwarded-For tests are the security-critical ones and are written
from both directions: honoring the header from a trusted proxy must make
the lockout per-client, and NOT honoring it from anyone else must leave a
spoofed header completely inert. A test suite that only proved the first
half would pass just as happily against a build that trusts every client.

Runs against the real FastAPI app through TestClient. Starlette's
TestClient hardcodes the socket peer to ("testclient", 50000), so
`peer_client` wraps the app in a tiny ASGI shim that rewrites it --
without control over the peer there is no way to test peer-dependent
trust at all.
"""
import os
import re

import pytest
from fastapi.testclient import TestClient

import main
import remote_auth
import reverse_proxy


TEST_PIN = "8261havoc09"  # ephemeral, test-only; never a real deployment PIN
PROXY_IP = "10.9.0.2"
DIRECT_IP = "203.0.113.77"


# --------------------------------------------------------------- fixtures --

class _FixedPeer:
    """ASGI shim that rewrites scope['client'] so a test can choose which
    socket peer the request appears to come from. Everything in this file
    turns on that value -- a trusted-proxy test that can't vary the peer
    isn't testing trust, it's testing a constant."""

    def __init__(self, app, host, port=50000):
        self.app = app
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (self.host, self.port)
        await self.app(scope, receive, send)


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path, monkeypatch):
    """Point remote_auth's persisted state + audit log at a throwaway dir
    and clear the in-memory lockout/rate-limit trackers, so no test here
    reads (or corrupts) a real backend/data/ or inherits another test's
    attempt counters."""
    monkeypatch.setattr(remote_auth, "AUTH_PATH", str(tmp_path / "remote_auth.json"))
    monkeypatch.setattr(remote_auth, "AUDIT_LOG_PATH", str(tmp_path / "auth_audit.log"))
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()
    yield
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()


@pytest.fixture
def peer_client():
    """Factory: a TestClient whose requests arrive from `host`. Not opened
    as a context manager on purpose -- these tests exercise middleware and
    routes only, and triggering lifespan would spin up the real scheduler
    and discovery background threads for no reason."""
    def _make(host, base_url="http://testserver", port=50000):
        return TestClient(_FixedPeer(main.app, host, port), base_url=base_url)
    return _make


def trust(*entries, **flags):
    """Apply a trusted-proxy / TLS env config. Mirrors what an operator
    actually does (set env, restart) rather than reaching into internals."""
    env = {"SBD_TRUSTED_PROXIES": ",".join(entries)}
    env.update(flags)
    reverse_proxy.reload_from_env(env=env, warn=False)


# ------------------------------------------------------ unit: client_ip ----

def test_forwarded_for_ignored_when_nothing_is_trusted():
    """The shipped default. A client that reaches the app directly can put
    anything it likes in X-Forwarded-For, and it must change nothing."""
    assert reverse_proxy.resolve_client_ip(DIRECT_IP, "1.2.3.4") == DIRECT_IP
    assert reverse_proxy.resolve_client_ip(DIRECT_IP, "1.2.3.4, 5.6.7.8") == DIRECT_IP


def test_forwarded_for_honored_from_trusted_peer():
    trust(PROXY_IP)
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "198.51.100.5") == "198.51.100.5"
    # ...but still only from that peer. A different, untrusted source
    # sending the identical header gets nothing.
    assert reverse_proxy.resolve_client_ip(DIRECT_IP, "198.51.100.5") == DIRECT_IP


def test_rightmost_untrusted_entry_wins_over_client_supplied_prefix():
    """Both Caddy and nginx's conventional $proxy_add_x_forwarded_for
    APPEND the real peer to whatever the client sent, so a client sending
    'X-Forwarded-For: 9.9.9.9' arrives as '9.9.9.9, <real ip>'. Reading
    left-to-right -- the obvious implementation -- would hand the attacker
    their forged value and defeat the entire point."""
    trust(PROXY_IP)
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "9.9.9.9, 198.51.100.5") == "198.51.100.5"


def test_chained_trusted_proxies_are_skipped():
    trust("10.9.0.0/24")
    resolved = reverse_proxy.resolve_client_ip(
        "10.9.0.2", "198.51.100.5, 10.9.0.7, 10.9.0.3")
    assert resolved == "198.51.100.5"


def test_every_hop_trusted_falls_back_to_peer():
    """No untrusted address anywhere in the chain means there's no client
    to attribute this to -- stay with the unforgeable socket peer rather
    than inventing an attribution."""
    trust("10.9.0.0/24")
    assert reverse_proxy.resolve_client_ip("10.9.0.2", "10.9.0.7, 10.9.0.3") == "10.9.0.2"


def test_unparseable_forwarded_entry_falls_back_to_peer():
    """Some proxies write a literal 'unknown'. That's not an address, and
    accepting arbitrary strings as lockout keys would let a client behind
    a *replacing* proxy pick its own bucket."""
    trust(PROXY_IP)
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "unknown") == PROXY_IP
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "not-an-ip") == PROXY_IP


def test_empty_or_missing_forwarded_header_falls_back_to_peer():
    trust(PROXY_IP)
    assert reverse_proxy.resolve_client_ip(PROXY_IP, None) == PROXY_IP
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "") == PROXY_IP
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "  ,  ") == PROXY_IP


def test_port_suffixes_and_ipv6_forms_are_normalised():
    trust(PROXY_IP)
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "198.51.100.5:44321") == "198.51.100.5"
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "[2001:db8::1]:443") == "2001:db8::1"
    # A bare IPv6 address has more than one colon, so it must NOT be
    # mistaken for a host:port pair and truncated.
    assert reverse_proxy.resolve_client_ip(PROXY_IP, "2001:db8::1") == "2001:db8::1"


def test_ipv6_proxy_can_be_trusted():
    trust("2001:db8::/32")
    assert reverse_proxy.resolve_client_ip("2001:db8::99", "198.51.100.5") == "198.51.100.5"
    assert reverse_proxy.is_trusted_proxy("2001:dba::99") is False


def test_wildcard_trusts_every_peer():
    trust("*")
    assert reverse_proxy.is_trusted_proxy(DIRECT_IP) is True
    assert reverse_proxy.resolve_client_ip(DIRECT_IP, "198.51.100.5") == "198.51.100.5"
    # Regression: "*" means "believe whichever peer hands me the header",
    # NOT "every address on the internet is one of my proxies". Conflating
    # the two made the right-to-left walk treat every forwarded entry as a
    # hop to skip, fall off the end, and hand back the peer -- quietly
    # collapsing "*" into the no-trust behaviour it was set to escape.
    assert reverse_proxy.is_known_proxy_hop("198.51.100.5") is False
    assert reverse_proxy.resolve_client_ip(DIRECT_IP, "9.9.9.9, 198.51.100.5") == "198.51.100.5"


def test_non_ip_peer_is_never_trusted():
    """A peer identifier that isn't an address (unix socket, an odd ASGI
    server) can't be bounded, so it can't be a proxy -- except under the
    explicit '*' opt-in, which trusts everything by definition."""
    trust("127.0.0.1")
    assert reverse_proxy.is_trusted_proxy("testclient") is False
    assert reverse_proxy.is_trusted_proxy(None) is False
    assert reverse_proxy.is_trusted_proxy("") is False


def test_invalid_trusted_proxy_entries_are_ignored_not_fatal():
    """A typo in a deployment env var must degrade to 'trusts less than you
    meant', never to a backend that won't boot -- and must still be
    findable afterwards."""
    trust("127.0.0.1", "not-a-cidr", "999.1.1.1")
    settings = reverse_proxy.get_settings()
    assert reverse_proxy.is_trusted_proxy("127.0.0.1") is True
    assert set(settings.invalid_entries) == {"not-a-cidr", "999.1.1.1"}


# ------------------------------------------------------- unit: is_https ----

def test_direct_tls_is_https_without_any_proxy_trust():
    assert reverse_proxy.resolve_is_https(DIRECT_IP, "https", None) is True


def test_forwarded_proto_only_believed_from_trusted_peer():
    assert reverse_proxy.resolve_is_https(DIRECT_IP, "http", "https") is False
    trust(PROXY_IP)
    assert reverse_proxy.resolve_is_https(PROXY_IP, "http", "https") is True
    assert reverse_proxy.resolve_is_https(PROXY_IP, "http", "http") is False
    # Leftmost value is the original client's protocol.
    assert reverse_proxy.resolve_is_https(PROXY_IP, "http", "https, http") is True
    assert reverse_proxy.resolve_is_https(PROXY_IP, "http", "http, https") is False


# ----------------------------------------------- e2e: lockout attribution --

def _fail_login(client, headers=None):
    return client.post("/api/auth/login", json={"pin": "0000000000"}, headers=headers or {})


def test_spoofed_forwarded_for_cannot_evade_the_lockout(peer_client):
    """The attack this whole feature has to not create. With trust off
    (the default), a direct attacker rotating X-Forwarded-For per guess
    must NOT get a fresh lockout bucket each time -- otherwise the 5-try
    lockout becomes unlimited tries."""
    client = peer_client(DIRECT_IP)
    remote_auth.enable(TEST_PIN)
    try:
        for i in range(5):
            resp = _fail_login(client, {"X-Forwarded-For": f"198.51.100.{i}"})
            assert resp.status_code == 401

        # All five landed in the one real bucket: the attacker's own IP.
        assert list(remote_auth._attempts) == [DIRECT_IP]
        locked, _ = remote_auth._is_locked_out(DIRECT_IP)
        assert locked is True

        # And the lockout actually bites, even with the correct PIN and yet
        # another fresh forged address.
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-For": "198.51.100.200"})
        assert resp.status_code == 401
        assert "locked out" in resp.json()["detail"]
    finally:
        remote_auth.disable()


def test_untrusted_proxy_deployment_shares_one_bucket(peer_client):
    """The problem W2-038 exists to fix, pinned as a test so it can't be
    'fixed' by accident and then silently regress: behind a proxy with
    trust NOT configured, one attacker's five wrong guesses lock out every
    other remote user too, because everyone is keyed to the proxy's IP."""
    client = peer_client(PROXY_IP)
    remote_auth.enable(TEST_PIN)
    try:
        for _ in range(5):
            _fail_login(client, {"X-Forwarded-For": "198.51.100.5"})

        # An entirely different remote client, correct PIN, still refused.
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-For": "198.51.100.99"})
        assert resp.status_code == 401
        assert "locked out" in resp.json()["detail"]
    finally:
        remote_auth.disable()


def test_trusted_proxy_makes_the_lockout_per_client(peer_client):
    """Same deployment, trust configured: the attacker locks out only
    themselves and an unrelated client logs in normally."""
    trust(PROXY_IP)
    client = peer_client(PROXY_IP)
    remote_auth.enable(TEST_PIN)
    try:
        for _ in range(5):
            resp = _fail_login(client, {"X-Forwarded-For": "198.51.100.5"})
            assert resp.status_code == 401

        assert remote_auth._is_locked_out("198.51.100.5")[0] is True
        assert remote_auth._is_locked_out("198.51.100.99")[0] is False
        assert PROXY_IP not in remote_auth._attempts

        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-For": "198.51.100.99"})
        assert resp.status_code == 200

        # The locked-out client stays locked out, correct PIN or not.
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-For": "198.51.100.5"})
        assert resp.status_code == 401
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_login_rate_limiter_also_keys_on_the_forwarded_client(peer_client):
    """The rate limiter reads the same resolved address as the lockout, so
    a trusted-proxy deployment throttles per client instead of throttling
    the whole proxy at once."""
    trust(PROXY_IP)
    client = peer_client(PROXY_IP)
    remote_auth.set_login_rate_limit(max_attempts=2, window_s=60)
    remote_auth.enable(TEST_PIN)
    try:
        for _ in range(2):
            assert _fail_login(client, {"X-Forwarded-For": "198.51.100.5"}).status_code == 401
        blocked = _fail_login(client, {"X-Forwarded-For": "198.51.100.5"})
        assert blocked.status_code == 429

        # A different client behind the same proxy is unaffected.
        other = _fail_login(client, {"X-Forwarded-For": "198.51.100.99"})
        assert other.status_code == 401
    finally:
        remote_auth.disable()


def test_audit_log_records_the_real_client_not_the_proxy(peer_client):
    trust(PROXY_IP)
    client = peer_client(PROXY_IP)
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-For": "198.51.100.5"})
        assert resp.status_code == 200
        log = open(remote_auth.AUDIT_LOG_PATH, "r", encoding="utf-8").read()
        assert "198.51.100.5" in log
        assert PROXY_IP not in log
    finally:
        client.cookies.clear()
        remote_auth.disable()


# ------------------------------------------------- e2e: Secure cookie flag -

def _set_cookie_header(resp):
    headers = resp.headers.get_list("set-cookie")
    return next(h for h in headers if h.startswith(remote_auth.SESSION_COOKIE + "="))


def test_session_cookie_has_no_secure_flag_over_plain_http(peer_client):
    """Load-bearing: browsers silently DROP a Secure cookie delivered over
    plain HTTP. Setting it unconditionally would lock every LAN user out
    of a dashboard that has no HTTPS to fall back to."""
    client = peer_client(DIRECT_IP)
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN})
        assert resp.status_code == 200
        assert "secure" not in _set_cookie_header(resp).lower()
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_session_cookie_gets_secure_flag_behind_trusted_https_proxy(peer_client):
    trust(PROXY_IP)
    client = peer_client(PROXY_IP)
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-Proto": "https"})
        assert resp.status_code == 200
        cookie = _set_cookie_header(resp)
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_session_cookie_ignores_forwarded_proto_from_untrusted_peer(peer_client):
    """A direct client claiming X-Forwarded-Proto: https must not get a
    Secure cookie -- the connection really is plaintext, and the browser
    would drop the cookie, breaking its own login."""
    client = peer_client(DIRECT_IP)
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN},
                            headers={"X-Forwarded-Proto": "https"})
        assert resp.status_code == 200
        assert "secure" not in _set_cookie_header(resp).lower()
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_session_cookie_gets_secure_flag_on_direct_tls(peer_client):
    """uvicorn --ssl-keyfile, no proxy in the picture at all."""
    client = peer_client(DIRECT_IP, base_url="https://testserver")
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN})
        assert resp.status_code == 200
        assert "Secure" in _set_cookie_header(resp)
    finally:
        client.cookies.clear()
        remote_auth.disable()


# ------------------------------------------------------------------ HSTS ---

def test_hsts_header_absent_by_default(peer_client):
    client = peer_client(DIRECT_IP, base_url="https://testserver")
    assert "strict-transport-security" not in client.get("/healthz").headers


def test_hsts_header_sent_on_https_when_enabled(peer_client):
    trust(SBD_HSTS="on")
    client = peer_client(DIRECT_IP, base_url="https://testserver")
    resp = client.get("/healthz")
    assert resp.headers["strict-transport-security"] == "max-age=31536000"


def test_hsts_header_not_sent_over_plain_http(peer_client):
    """Browsers ignore HSTS over plaintext (RFC 6797 sec. 7.2), so sending
    it there would only make the header look active when it isn't."""
    trust(SBD_HSTS="on")
    client = peer_client(DIRECT_IP)
    assert "strict-transport-security" not in client.get("/healthz").headers


def test_hsts_header_sent_behind_trusted_https_proxy(peer_client):
    trust(PROXY_IP, SBD_HSTS="on")
    client = peer_client(PROXY_IP)
    resp = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers["strict-transport-security"] == "max-age=31536000"


def test_hsts_directives_and_zero_max_age_escape_hatch():
    trust(SBD_HSTS="on", SBD_HSTS_MAX_AGE="600",
          SBD_HSTS_INCLUDE_SUBDOMAINS="on", SBD_HSTS_PRELOAD="true")
    assert reverse_proxy.hsts_header_value() == \
        "max-age=600; includeSubDomains; preload"
    # max-age=0 is the documented way to actively retract a pin a browser
    # already stored -- SBD_HSTS=off alone only stops sending the header.
    trust(SBD_HSTS="on", SBD_HSTS_MAX_AGE="0")
    assert reverse_proxy.hsts_header_value() == "max-age=0"
    trust(SBD_HSTS="off")
    assert reverse_proxy.hsts_header_value() is None


def test_typo_in_boolean_env_var_falls_back_to_the_safe_default():
    """SBD_HTTPS_REDIRECT=yess must not enable a redirect that would lock a
    LAN-only user out of their own dashboard."""
    trust(SBD_HTTPS_REDIRECT="yess", SBD_HSTS="enabled-please")
    assert reverse_proxy.get_settings().https_redirect is False
    assert reverse_proxy.get_settings().hsts is False


# ------------------------------------------------------- HTTPS redirect ----

def test_no_redirect_by_default(peer_client):
    client = peer_client(DIRECT_IP)
    resp = client.get("/api/system/info", follow_redirects=False)
    assert resp.status_code == 200


def test_redirect_to_https_when_enabled(peer_client):
    trust(SBD_HTTPS_REDIRECT="on")
    client = peer_client(DIRECT_IP)
    resp = client.get("/api/system/info", follow_redirects=False)
    assert resp.status_code == 307  # method-preserving, and NOT cached like a 301
    assert resp.headers["location"] == "https://testserver/api/system/info"


def test_redirect_preserves_query_string(peer_client):
    trust(SBD_HTTPS_REDIRECT="on")
    client = peer_client(DIRECT_IP)
    resp = client.get("/api/analytics/usage?period=today", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://testserver/api/analytics/usage?period=today"


def test_no_redirect_when_already_https_behind_trusted_proxy(peer_client):
    trust(PROXY_IP, SBD_HTTPS_REDIRECT="on")
    client = peer_client(PROXY_IP)
    resp = client.get("/api/system/info", headers={"X-Forwarded-Proto": "https"},
                       follow_redirects=False)
    assert resp.status_code == 200


def test_health_paths_are_never_redirected(peer_client):
    """A Docker HEALTHCHECK or proxy upstream probe hits these over plain
    HTTP from inside the container/host network, where there is no HTTPS
    listener to redirect to -- redirecting them would make the container
    report itself permanently unhealthy."""
    trust(SBD_HTTPS_REDIRECT="on")
    client = peer_client(DIRECT_IP)
    for path in ("/healthz", "/api/system/health"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 200, path


def test_redirect_runs_before_the_pin_gate(peer_client):
    """Ordering check: an unauthenticated request to a gated route should
    be redirected to HTTPS, not answered with a 401 over plaintext."""
    trust(SBD_HTTPS_REDIRECT="on")
    client = peer_client(DIRECT_IP)
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.get("/api/devices", follow_redirects=False)
        assert resp.status_code == 307
    finally:
        remote_auth.disable()


# ------------------------------------------------------- health endpoint ---

def test_healthz_open_when_pin_gate_enabled(peer_client):
    client = peer_client(DIRECT_IP)
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        # Contrast: a gated route really is gated, so this isn't just the
        # PIN gate quietly doing nothing.
        assert client.get("/api/system/info").status_code == 401
    finally:
        remote_auth.disable()


def test_healthz_leaks_nothing_about_the_install(peer_client):
    """It's the endpoint most likely to be publicly reachable (a proxy
    probes it ahead of any auth), so it must not hand out a version string
    or uptime the way /api/system/health and /api/system/info do."""
    client = peer_client(DIRECT_IP)
    body = client.get("/healthz").json()
    assert body == {"status": "ok"}
    assert main.APP_VERSION not in str(body)


def test_healthz_is_distinct_from_the_app_health_route(peer_client):
    client = peer_client(DIRECT_IP)
    app_health = client.get("/api/system/health").json()
    assert "uptime_seconds" in app_health
    assert "uptime_seconds" not in client.get("/healthz").json()


# --------------------------------------------------------- proxy status ----

def test_proxy_status_reports_resolved_client_and_is_gated(peer_client):
    trust(PROXY_IP, SBD_HSTS="on")
    client = peer_client(PROXY_IP)
    resp = client.get("/api/system/proxy-status", headers={
        "X-Forwarded-For": "198.51.100.5", "X-Forwarded-Proto": "https",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["peer_ip"] == PROXY_IP
    assert body["peer_is_trusted_proxy"] is True
    assert body["client_ip"] == "198.51.100.5"
    assert body["is_https"] is True
    assert body["settings"]["trusted_proxies"] == [PROXY_IP + "/32"]
    assert body["settings"]["hsts_header"] == "max-age=31536000"

    # It echoes deployment config, so it sits behind the PIN gate.
    remote_auth.enable(TEST_PIN)
    try:
        assert client.get("/api/system/proxy-status").status_code == 401
    finally:
        remote_auth.disable()


def test_proxy_status_detects_a_server_rewritten_peer(peer_client):
    """uvicorn's own ProxyHeadersMiddleware is enabled by default and
    trusts 127.0.0.1, rewriting scope['client'] from X-Forwarded-For before
    this app ever runs -- so SBD_TRUSTED_PROXIES stops being the thing
    deciding whether the header is believed. deploy/ and the Dockerfile
    pass --no-proxy-headers to stop that, and this field makes it obvious
    when someone's own run command didn't. The tell is the substituted
    port 0, which no real TCP peer has."""
    rewritten = peer_client(DIRECT_IP, port=0)
    body = rewritten.get("/api/system/proxy-status",
                          headers={"X-Forwarded-For": "198.51.100.5"}).json()
    assert body["peer_rewritten_by_server"] is True

    # A normal peer, same forged header, is not flagged.
    normal = peer_client(DIRECT_IP)
    body = normal.get("/api/system/proxy-status",
                       headers={"X-Forwarded-For": "198.51.100.5"}).json()
    assert body["peer_rewritten_by_server"] is False

    # Port 0 alone, with no forwarded header, isn't evidence of anything.
    body = rewritten.get("/api/system/proxy-status").json()
    assert body["peer_rewritten_by_server"] is False


def test_proxy_status_shows_when_trust_is_not_configured(peer_client):
    """The 'why is my lockout still keyed to the proxy' answer, visible
    without reverse-engineering it from the audit log."""
    client = peer_client(PROXY_IP)
    body = client.get("/api/system/proxy-status",
                       headers={"X-Forwarded-For": "198.51.100.5"}).json()
    assert body["peer_is_trusted_proxy"] is False
    assert body["client_ip"] == PROXY_IP
    assert body["settings"]["trusted_proxies"] == []


# ------------------------------------------------------ W2-043: mixed content

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")

# Absolute plaintext URLs in a resource-loading position. Served over
# HTTPS, a browser hard-blocks these as mixed content (scripts, styles,
# fetch/XHR) or silently downgrades them (ws://) -- which for this app
# would mean a dashboard that loads unstyled and can't talk to its own API.
_MIXED_CONTENT_PATTERNS = [
    re.compile(r"""(?:src|href)\s*=\s*["'](?:http|ws)://""", re.IGNORECASE),
    re.compile(r"""url\(\s*["']?(?:http|ws)://""", re.IGNORECASE),
    re.compile(r"""(?:fetch|import|new\s+WebSocket|new\s+EventSource)\s*\(\s*["'`](?:http|ws)://""",
               re.IGNORECASE),
    re.compile(r"""@import\s+["'](?:http|ws)://""", re.IGNORECASE),
]


def test_frontend_assets_have_no_mixed_content_references():
    """W2-043. Every asset and API call the dashboard makes must be
    same-origin/relative so the page works identically on http:// and
    https:// with no per-scheme configuration."""
    offenders = []
    for name in os.listdir(FRONTEND_DIR):
        path = os.path.join(FRONTEND_DIR, name)
        if not os.path.isfile(path):
            continue
        text = open(path, "r", encoding="utf-8").read()
        for pattern in _MIXED_CONTENT_PATTERNS:
            for match in pattern.finditer(text):
                offenders.append(f"{name}: {match.group(0)!r}")
    assert offenders == [], (
        "absolute http:// or ws:// resource references break when the "
        f"dashboard is served over HTTPS; use relative URLs instead: {offenders}"
    )


def test_frontend_api_base_is_relative():
    """The API base is what would most plausibly get hardcoded to
    http://host:8500 during debugging and then shipped."""
    app_js = open(os.path.join(FRONTEND_DIR, "app.js"), "r", encoding="utf-8").read()
    match = re.search(r"""^const API\s*=\s*(["'])(.*?)\1""", app_js, re.MULTILINE)
    assert match, "expected a top-level `const API = ...` in frontend/app.js"
    assert match.group(2) == "", (
        "frontend/app.js must build request URLs relative to the serving "
        f"origin, got API={match.group(2)!r}"
    )
