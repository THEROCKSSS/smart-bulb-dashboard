"""PIN gate hardening (Week 2 Phase A, W2-051..070): configurable lockout
with exponential backoff, PIN complexity rules, household + revocable guest
PINs, session-TTL configuration, token rotation on PIN change, IPv6-correct
per-IP tracking, and the brute-force simulation regression (W2-067).

Same isolation approach as test_remote_auth.py: the real FastAPI app via
TestClient with remote_auth's state file and audit log redirected to a
throwaway directory. Nothing is mocked out of the auth path itself.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

import remote_auth
import main


# --------------------------------------------------------------- fixtures --

@pytest.fixture(scope="session")
def _isolated_auth_paths(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("pin_hardening_tests")
    remote_auth.AUTH_PATH = str(tmp_dir / "remote_auth.json")
    remote_auth.AUDIT_LOG_PATH = str(tmp_dir / "auth_audit.log")
    return tmp_dir


@pytest.fixture(scope="session")
def client(_isolated_auth_paths):
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_auth_state(_isolated_auth_paths):
    if os.path.exists(remote_auth.AUTH_PATH):
        os.remove(remote_auth.AUTH_PATH)
    if os.path.exists(remote_auth.AUDIT_LOG_PATH):
        os.remove(remote_auth.AUDIT_LOG_PATH)
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()
    yield
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()


# Throwaway PINs that pass the complexity rules. Never deployment values.
HOUSEHOLD_PIN = "h4v9q2xk7m"
GUEST_PIN = "g8t3zq5wpn"
WRONG_PIN = "zq41m7vx93"


def _login(client, pin):
    """Log in and leave the session cookie on the shared client. Needed
    because everything under /api/system/remote-auth/ is itself gated once
    the gate is on -- managing PINs is not an unauthenticated action."""
    resp = client.post("/api/auth/login", json={"pin": pin})
    assert resp.status_code == 200, resp.text
    cookie = resp.cookies.get(remote_auth.SESSION_COOKIE)
    client.cookies.set(remote_auth.SESSION_COOKIE, cookie)
    return cookie


# --------------------------------------------------------- PIN complexity --

WEAK_PINS = [
    "",             # empty
    "abc",          # under the minimum length
    "12345",        # under the minimum length AND a sequence
    "1234",         # the canonical bad PIN
    "0000",
    "123456",
    "test1234",     # a PIN this project's own docs used during development
    "password",
    "111111",       # single repeated character
    "abcdef",       # ascending sequence, non-numeric
    "987654",       # descending sequence
    "123123",       # short block repeated to look longer
    "abababab",
]


def test_trivially_weak_and_dev_pins_are_refused(check_all):
    """AGENTS.md is explicit that the development PINs must never survive
    into a deployment, so these are refused outright rather than warned
    about -- there is deliberately no override flag.

    One test over the whole list rather than 13 parametrised ones: if a
    change to assess_pin() starts letting weak PINs through, the security
    question is "which ones", and that should be one report, not 13 lines.
    """
    def _refused(pin):
        assert remote_auth.assess_pin(pin)["ok"] is False, "assess_pin accepted it"
        with pytest.raises(ValueError):
            remote_auth.enable(pin)

    check_all(WEAK_PINS, _refused, label="weak PIN", name=lambda p: repr(p))


def test_a_real_pin_is_accepted_and_graded():
    result = remote_auth.assess_pin(HOUSEHOLD_PIN)
    assert result["ok"] is True
    assert result["issues"] == []
    assert result["strength"] in ("fair", "strong")


def test_pin_strength_endpoint_returns_the_same_verdict(client):
    resp = client.post("/api/system/remote-auth/pin-strength", json={"pin": "1234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["issues"]

    ok_resp = client.post("/api/system/remote-auth/pin-strength", json={"pin": HOUSEHOLD_PIN})
    assert ok_resp.json()["ok"] is True


def test_enable_endpoint_rejects_a_weak_pin_with_400(client):
    resp = client.post("/api/system/remote-auth/enable", json={"pin": "1234"})
    assert resp.status_code == 400
    assert "PIN rejected" in resp.json()["detail"]
    # The gate must not be half-armed by a rejected PIN.
    assert remote_auth.is_enabled() is False


# ------------------------------------------------- configurable lockout ----

def test_lockout_threshold_is_configurable(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.set_lockout_policy(max_attempts=2, base_seconds=60)
    remote_auth.set_login_rate_limit(max_attempts=100, window_s=60)
    try:
        for _ in range(2):
            assert client.post("/api/auth/login", json={"pin": WRONG_PIN}).status_code == 401
        # Threshold is 2 now, not the built-in default of 5.
        locked = client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN})
        assert locked.status_code == 401
        assert "locked out" in locked.json()["detail"]
    finally:
        remote_auth.disable()


def test_lockout_duration_is_configurable(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.set_lockout_policy(max_attempts=1, base_seconds=900, max_seconds=3600)
    remote_auth.set_login_rate_limit(max_attempts=100, window_s=60)
    try:
        resp = client.post("/api/auth/login", json={"pin": WRONG_PIN})
        assert resp.status_code == 401
        entry = remote_auth._attempts[remote_auth.normalize_ip("testclient")]
        remaining = entry["locked_until"] - time.time()
        assert 800 < remaining <= 900, "lockout should use the configured 900s, not the 300s default"
    finally:
        remote_auth.disable()


def test_lockout_backoff_math_doubles_and_caps():
    assert remote_auth._lockout_duration(1, 300, 86400) == 300
    assert remote_auth._lockout_duration(2, 300, 86400) == 600
    assert remote_auth._lockout_duration(3, 300, 86400) == 1200
    # Cap matters: an uncapped doubling eventually locks out the household
    # member sharing the attacker's public IP effectively forever.
    assert remote_auth._lockout_duration(20, 300, 3600) == 3600


def test_repeat_lockout_lasts_longer_than_the_first(client):
    """Exponential backoff end-to-end: a second lockout for the same IP must
    be measurably longer than the first, or a patient attacker pays a fixed
    price per batch of guesses forever."""
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.set_lockout_policy(max_attempts=1, base_seconds=1, max_seconds=3600)
    remote_auth.set_login_rate_limit(max_attempts=100, window_s=60)
    key = remote_auth.normalize_ip("testclient")
    try:
        client.post("/api/auth/login", json={"pin": WRONG_PIN})
        first = remote_auth._attempts[key]["locked_until"] - time.time()

        time.sleep(1.15)  # let the first (1s) lockout expire

        client.post("/api/auth/login", json={"pin": WRONG_PIN})
        second = remote_auth._attempts[key]["locked_until"] - time.time()

        assert remote_auth._attempts[key]["lockouts"] == 2
        assert second > first * 1.5
    finally:
        remote_auth.disable()


def test_lockout_policy_rejects_incoherent_values(client):
    resp = client.post(
        "/api/system/remote-auth/lockout-policy",
        json={"max_attempts": 0},
    )
    assert resp.status_code == 400

    resp = client.post(
        "/api/system/remote-auth/lockout-policy",
        json={"base_seconds": 600, "max_seconds": 60},
    )
    assert resp.status_code == 400


# ----------------------------------------------- brute-force regression ----

def test_brute_force_simulation_never_lets_a_guess_through(client):
    """W2-067. A refactor that quietly breaks the lockout would still pass
    every "wrong PIN returns 401" test, because wrong PINs are supposed to
    return 401 either way. This asserts the properties that only a *working*
    lockout has: guessing stops being evaluated after the threshold, the
    correct PIN is refused mid-lockout, the penalty escalates across rounds,
    and the household still gets back in afterwards."""
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.set_lockout_policy(max_attempts=5, base_seconds=1, max_seconds=3600)
    # Deliberately out of the way: this test is about the lockout, not the
    # login endpoint's separate request-volume limiter.
    remote_auth.set_login_rate_limit(max_attempts=1000, window_s=60)
    key = remote_auth.normalize_ip("testclient")
    try:
        # Round 1 -- a dictionary attack of 15 guesses.
        details = []
        for i in range(15):
            r = client.post("/api/auth/login", json={"pin": f"guess{i:05d}xy"})
            assert r.status_code == 401, "no guess may ever succeed"
            details.append(r.json()["detail"])

        # Guesses 1-4 are evaluated; the 5th trips the lockout and every
        # request after it is refused without being evaluated at all.
        assert details[:4] == ["incorrect PIN"] * 4
        assert all("locked out" in d for d in details[4:])
        assert remote_auth._attempts[key]["lockouts"] == 1
        first_lockout = remote_auth._attempts[key]["locked_until"] - time.time()

        # Even the RIGHT PIN is refused while locked. A lockout that only
        # blocks wrong guesses isn't a lockout.
        assert client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN}).status_code == 401

        # Round 2 -- the attacker waits out the (deliberately 1s) lockout and
        # resumes. The penalty must escalate, not reset to the same price.
        time.sleep(1.15)
        for i in range(5):
            assert client.post(
                "/api/auth/login", json={"pin": f"again{i:05d}zz"},
            ).status_code == 401
        assert remote_auth._attempts[key]["lockouts"] == 2
        second_lockout = remote_auth._attempts[key]["locked_until"] - time.time()
        assert second_lockout > first_lockout * 1.5

        # ...and the legitimate household still gets in once it expires.
        remote_auth._attempts.clear()
        assert client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN}).status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_audit_log_distinguishes_lockout_from_a_wrong_pin(client):
    """W2-068: the API response already told these apart; the audit log
    must too, without ever recording a PIN value."""
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.set_lockout_policy(max_attempts=2, base_seconds=60)
    remote_auth.set_login_rate_limit(max_attempts=100, window_s=60)
    try:
        for _ in range(3):
            client.post("/api/auth/login", json={"pin": WRONG_PIN})

        with open(remote_auth.AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        reasons = {e.get("reason") for e in entries}
        assert "wrong_credential" in reasons
        assert "locked_out" in reasons

        raw = open(remote_auth.AUDIT_LOG_PATH, "r", encoding="utf-8").read()
        assert WRONG_PIN not in raw
        assert HOUSEHOLD_PIN not in raw
        assert "pin" not in raw.lower()
    finally:
        remote_auth.disable()


# ------------------------------------------------------ IPv6 correctness ---

def test_ipv6_lockout_is_not_escaped_by_rewriting_the_address():
    """The same IPv6 host written compressed, expanded, bracketed, or with a
    scope id must land in one lockout bucket -- otherwise each spelling buys
    a fresh set of guesses."""
    remote_auth.set_lockout_policy(max_attempts=2, base_seconds=60)
    remote_auth._record_failure("2001:db8::1")
    remote_auth._record_failure("2001:0db8:0000:0000:0000:0000:0000:0001")

    for spelling in (
        "2001:db8::1",
        "2001:0db8:0000:0000:0000:0000:0000:0001",
        "[2001:db8::1]:51000",
        "2001:db8::1%eth0",
    ):
        locked, _ = remote_auth._is_locked_out(spelling)
        assert locked is True, f"{spelling} should share the lockout"


def test_ipv6_lockout_covers_the_whole_prefix_but_not_a_different_one():
    """A residential IPv6 allocation hands out unlimited addresses inside one
    /64, so tracking must be per-prefix; a genuinely different network must
    stay unaffected."""
    remote_auth.set_lockout_policy(max_attempts=1, base_seconds=60)
    remote_auth._record_failure("2001:db8:aaaa:1::5")

    assert remote_auth._is_locked_out("2001:db8:aaaa:1::9999")[0] is True
    assert remote_auth._is_locked_out("2001:db8:aaaa:2::5")[0] is False


def test_ipv4_mapped_ipv6_shares_the_plain_ipv4_bucket():
    remote_auth.set_lockout_policy(max_attempts=1, base_seconds=60)
    remote_auth._record_failure("::ffff:203.0.113.9")
    assert remote_auth._is_locked_out("203.0.113.9")[0] is True


# ----------------------------------------------------- multiple PIN slots --

def test_guest_pin_opens_the_gate_alongside_the_household_pin(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        _login(client, HOUSEHOLD_PIN)
        created = client.post(
            "/api/system/remote-auth/pins",
            json={"pin": GUEST_PIN, "label": "Dog sitter"},
        )
        assert created.status_code == 200
        assert created.json()["kind"] == "guest"
        assert created.json()["label"] == "Dog sitter"

        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": GUEST_PIN}).status_code == 200
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN}).status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_revoking_a_guest_pin_kills_only_its_own_sessions(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        household_cookie = _login(client, HOUSEHOLD_PIN)
        guest = client.post(
            "/api/system/remote-auth/pins", json={"pin": GUEST_PIN},
        ).json()

        client.cookies.clear()
        guest_cookie = client.post(
            "/api/auth/login", json={"pin": GUEST_PIN},
        ).cookies.get(remote_auth.SESSION_COOKIE)

        client.cookies.set(remote_auth.SESSION_COOKIE, household_cookie)
        revoke = client.delete(f"/api/system/remote-auth/pins/{guest['id']}")
        assert revoke.status_code == 200
        assert revoke.json()["revoked_sessions"] == 1

        client.cookies.set(remote_auth.SESSION_COOKIE, guest_cookie)
        assert client.get("/api/system/remote-auth/status").status_code == 401
        # The guest PIN itself no longer works either.
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": GUEST_PIN}).status_code == 401

        # The household's own session is untouched -- that's the whole point
        # of a separately-revocable guest PIN.
        client.cookies.set(remote_auth.SESSION_COOKIE, household_cookie)
        assert client.get("/api/system/remote-auth/status").status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_household_pin_cannot_be_revoked(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        _login(client, HOUSEHOLD_PIN)
        pins = client.get("/api/system/remote-auth/pins").json()["pins"]
        household = next(p for p in pins if p["kind"] == "household")
        resp = client.delete(f"/api/system/remote-auth/pins/{household['id']}")
        # Allowing this would leave the gate enabled with nothing able to
        # open it -- a lockout only fixable by editing the state file.
        assert resp.status_code == 400
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN}).status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_an_expired_guest_pin_stops_working():
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        created = remote_auth.add_guest_pin(GUEST_PIN, "Weekend", expires_in_s=-1)
        assert created["expires_at"] is not None
        ok, _detail, _pin_id = remote_auth.verify_pin(GUEST_PIN, "203.0.113.30")
        assert ok is False
        ok2, _, _ = remote_auth.verify_pin(HOUSEHOLD_PIN, "203.0.113.31")
        assert ok2 is True
    finally:
        remote_auth.disable()


def test_duplicate_and_excess_guest_pins_are_refused():
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        remote_auth.add_guest_pin(GUEST_PIN)
        with pytest.raises(ValueError, match="already in use"):
            remote_auth.add_guest_pin(GUEST_PIN)
        with pytest.raises(ValueError, match="already in use"):
            remote_auth.add_guest_pin(HOUSEHOLD_PIN)

        for i in range(remote_auth.MAX_GUEST_PINS - 1):
            remote_auth.add_guest_pin(f"gst{i}q7wz4m")
        # Each active PIN costs a full PBKDF2 pass on every login attempt,
        # so the count is capped rather than unbounded.
        with pytest.raises(ValueError, match="at most"):
            remote_auth.add_guest_pin("overflow99xk")
    finally:
        remote_auth.disable()


def test_listed_pins_never_expose_the_hash_or_salt(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        _login(client, HOUSEHOLD_PIN)
        client.post("/api/system/remote-auth/pins", json={"pin": GUEST_PIN})
        body = client.get("/api/system/remote-auth/pins").text
        assert "salt" not in body
        assert "pin_hash" not in body
        assert GUEST_PIN not in body
        assert HOUSEHOLD_PIN not in body
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_enabling_again_drops_previously_issued_guest_pins():
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.add_guest_pin(GUEST_PIN)
    remote_auth.enable("fresh7k2mq9")
    try:
        ok, _, _ = remote_auth.verify_pin(GUEST_PIN, "203.0.113.40")
        assert ok is False, "a re-armed gate must not still accept the old guest PIN"
    finally:
        remote_auth.disable()


def test_legacy_single_pin_state_file_still_opens_the_gate():
    """A state file written before multi-PIN support has pin_hash/salt at the
    top level and no `pins` map. Upgrading in place must not leave an
    enabled gate that nothing can open."""
    salt = "a" * 32
    legacy = {
        "enabled": True,
        "salt": salt,
        "pin_hash": remote_auth._hash_pin(HOUSEHOLD_PIN, salt),
        "secret_key": "b" * 64,
        "session_ttl_s": 3600,
        "sessions": {},
    }
    with open(remote_auth.AUTH_PATH, "w") as f:
        json.dump(legacy, f)
    try:
        ok, _detail, pin_id = remote_auth.verify_pin(HOUSEHOLD_PIN, "203.0.113.50")
        assert ok is True
        assert pin_id is not None
        assert remote_auth.list_pins()[0]["kind"] == "household"
    finally:
        remote_auth.disable()


# ---------------------------------------- PIN change / session lifecycle ---

def test_changing_the_pin_rotates_every_session_and_reissues_the_callers(client):
    """W2-063. The old PIN's sessions must die with it, but the operator who
    just changed it shouldn't be bounced to the login screen."""
    remote_auth.enable(HOUSEHOLD_PIN)
    new_pin = "n5w7q2mz8k"
    try:
        first = client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN})
        old_cookie = first.cookies.get(remote_auth.SESSION_COOKIE)
        client.cookies.set(remote_auth.SESSION_COOKIE, old_cookie)

        changed = client.post("/api/system/remote-auth/pin", json={"pin": new_pin})
        assert changed.status_code == 200
        assert changed.json()["revoked_sessions"] >= 1
        new_cookie = changed.cookies.get(remote_auth.SESSION_COOKIE)
        assert new_cookie and new_cookie != old_cookie

        # The pre-change cookie is dead even though it was validly signed.
        client.cookies.set(remote_auth.SESSION_COOKIE, old_cookie)
        assert client.get("/api/system/remote-auth/status").status_code == 401

        # The reissued one works immediately.
        client.cookies.set(remote_auth.SESSION_COOKIE, new_cookie)
        assert client.get("/api/system/remote-auth/status").status_code == 200

        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": new_pin}).status_code == 200
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN}).status_code == 401
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_changing_to_a_weak_pin_is_refused_and_changes_nothing(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        _login(client, HOUSEHOLD_PIN)
        resp = client.post("/api/system/remote-auth/pin", json={"pin": "1234"})
        assert resp.status_code == 400
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN}).status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_session_ttl_is_settable_from_the_api_and_bounds_checked(client):
    """W2-065: the TTL was API-only and undocumented in the UI. Bounds
    matter as much as the setting -- a 5-second TTL makes the dashboard
    unusable and a 10-year one makes revocation meaningless."""
    # The gate is off here, so these settings calls aren't themselves gated.
    resp = client.post("/api/system/remote-auth/session-ttl", json={"session_ttl_s": 1800})
    assert resp.status_code == 200
    assert resp.json()["session_ttl_s"] == 1800
    assert remote_auth.get_session_ttl() == 1800

    assert client.post(
        "/api/system/remote-auth/session-ttl", json={"session_ttl_s": 5},
    ).status_code == 400
    assert client.post(
        "/api/system/remote-auth/session-ttl", json={"session_ttl_s": 10 ** 9},
    ).status_code == 400
    assert remote_auth.get_session_ttl() == 1800, "a rejected value must not be applied"

    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        _login(client, HOUSEHOLD_PIN)
        session = client.get("/api/auth/sessions").json()["sessions"][0]
        assert 1700 < session["expires_at"] - session["created_at"] <= 1800
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_clear_all_sessions_forces_everyone_to_reauthenticate(client):
    """W2-064. Already shipped in Week 1's session work -- kept here so the
    Phase A hardening suite fails if the multi-PIN rework breaks it."""
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        cookie_a = _login(client, HOUSEHOLD_PIN)
        b = client.post("/api/auth/login", json={"pin": HOUSEHOLD_PIN})
        cookie_b = b.cookies.get(remote_auth.SESSION_COOKIE)

        result = client.post("/api/auth/sessions/revoke-all")
        assert result.status_code == 200
        assert result.json()["revoked"] >= 2

        for cookie in (cookie_a, cookie_b):
            client.cookies.set(remote_auth.SESSION_COOKIE, cookie)
            assert client.get("/api/system/remote-auth/status").status_code == 401
    finally:
        client.cookies.clear()
        remote_auth.disable()


# ------------------------------------------- constant-time comparison ------

def test_compare_handles_non_ascii_without_raising():
    """Pre-existing bug: hmac.compare_digest's str form raises TypeError on
    any non-ASCII character, and one side is an attacker-supplied cookie. A
    forged token with a single accented byte turned a rejected signature
    into an unhandled exception."""
    assert remote_auth._compare("café", "abcd") is False
    assert remote_auth._compare("abcd", "abcd") is True
    assert remote_auth._compare(None, "abcd") is False


def test_non_ascii_session_cookie_is_rejected_not_crashed(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    try:
        assert remote_auth.verify_session_token("abc.café") is False
        assert remote_auth.get_token_jti("abc.café") is None

        # Sent as a raw latin-1 header rather than through the cookie jar:
        # httpx refuses to encode a non-ASCII cookie value, but nothing stops
        # an attacker's socket from putting those bytes on the wire, and
        # Starlette decodes the Cookie header as latin-1 into exactly the
        # non-ASCII str that used to blow up the comparison.
        resp = client.get(
            "/api/system/remote-auth/status",
            headers={b"cookie": f"{remote_auth.SESSION_COOKIE}=caf\xe9.caf\xe9".encode("latin-1")},
        )
        assert resp.status_code == 401
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_status_surfaces_the_lockout_policy(client):
    remote_auth.enable(HOUSEHOLD_PIN)
    remote_auth.set_lockout_policy(max_attempts=7, base_seconds=120, max_seconds=7200)
    try:
        _login(client, HOUSEHOLD_PIN)
        body = client.get("/api/system/remote-auth/status").json()
        assert body["lockout_max_attempts"] == 7
        assert body["lockout_base_s"] == 120
        assert body["lockout_max_s"] == 7200
        assert body["guest_pin_count"] == 0
        assert body["pin_changed_at"] is not None
    finally:
        remote_auth.disable()
