"""Tests for the PIN-gate hardening + rate-limiting work (Week 2, W2-051..070
and W2-101..120): session listing/revocation, auth audit logging, and
per-IP login rate limiting -- plus regression checks for the pre-existing
lockout and the documented "root path must stay reachable" invariant.

Runs against the real FastAPI app via TestClient (no mocked auth internals),
mocking out nothing except that no real Tuya device calls happen because no
device endpoints are exercised here.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

import remote_auth
import main


# --------------------------------------------------------------- fixtures --

@pytest.fixture(scope="session")
def _isolated_auth_paths(tmp_path_factory):
    """Redirect remote_auth's persisted state file and audit log to a
    throwaway directory for the whole test session, so these tests never
    touch (or get contaminated by) a real backend/data/ directory. Safe to
    do once at session scope since every test also resets file contents
    via the per-test `reset_auth_state` fixture below."""
    tmp_dir = tmp_path_factory.mktemp("remote_auth_tests")
    remote_auth.AUTH_PATH = str(tmp_dir / "remote_auth.json")
    remote_auth.AUDIT_LOG_PATH = str(tmp_dir / "auth_audit.log")
    return tmp_dir


@pytest.fixture(scope="session")
def client(_isolated_auth_paths):
    """One shared TestClient/app instance for the whole test session, so the
    background discovery/schedule threads spun up in main.on_startup() are
    only started once."""
    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_auth_state(_isolated_auth_paths):
    """Full reset before every single test: wipe the persisted auth state
    (back to disabled/no-sessions), the audit log, and the in-memory
    lockout/rate-limit trackers. Guarantees each test starts from a clean
    slate regardless of test order."""
    if os.path.exists(remote_auth.AUTH_PATH):
        os.remove(remote_auth.AUTH_PATH)
    if os.path.exists(remote_auth.AUDIT_LOG_PATH):
        os.remove(remote_auth.AUDIT_LOG_PATH)
    remote_auth._attempts.clear()
    remote_auth._rate_limit_buckets.clear()
    yield


def _read_audit_lines():
    if not os.path.exists(remote_auth.AUDIT_LOG_PATH):
        return []
    with open(remote_auth.AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


TEST_PIN = "7412963085"  # arbitrary ephemeral test-only PIN, never a real deployment PIN


# -------------------------------------------------------- regression: root -

def test_root_path_reachable_with_gate_enabled_and_no_session(client):
    """The documented iteration-004 bug: enabling the PIN gate must never
    make '/' itself unreachable, or the PIN entry page can't load."""
    remote_auth.enable(TEST_PIN)
    try:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        # A genuinely gated route, for contrast -- confirms the gate really
        # is active and it's specifically "/" that's deliberately exempted,
        # not that the gate silently did nothing.
        gated = client.get("/api/system/remote-auth/status")
        assert gated.status_code == 401
    finally:
        remote_auth.disable()


# ----------------------------------------------------- regression: lockout -

def test_lockout_still_triggers_after_five_failed_attempts(client):
    """Existing brute-force lockout (MAX_ATTEMPTS=5 / LOCKOUT_SECONDS=300)
    must still work unchanged by the new rate limiter / session tracking."""
    remote_auth.enable(TEST_PIN)
    try:
        # Rate limit default (20/60s) comfortably covers 6 requests, so this
        # exercises the lockout counter specifically, not the new limiter.
        for _ in range(5):
            resp = client.post("/api/auth/login", json={"pin": "0000000000"})
            assert resp.status_code == 401

        # 6th attempt -- even with the CORRECT pin -- must still be rejected
        # because the account is locked out. A lockout that only blocks
        # wrong guesses isn't a real lockout.
        resp = client.post("/api/auth/login", json={"pin": TEST_PIN})
        assert resp.status_code == 401
        assert "locked out" in resp.json()["detail"]
    finally:
        remote_auth.disable()


# --------------------------------------------------------- new: rate limit -

def test_rate_limiter_blocks_independent_of_lockout(client):
    """The new per-IP rate limiter must trip before the 5-attempt lockout
    would, using a low configured threshold, and must return 429 with a
    Retry-After header -- distinct from the lockout's 401."""
    remote_auth.set_login_rate_limit(max_attempts=3, window_s=60)
    remote_auth.enable(TEST_PIN)
    try:
        for i in range(3):
            resp = client.post("/api/auth/login", json={"pin": "0000000000"})
            assert resp.status_code == 401, f"attempt {i} should be a normal wrong-pin rejection"

        # 4th request in the same window exceeds the rate limit (threshold 3)
        # well before the lockout's own 5-attempt threshold would fire.
        resp = client.post("/api/auth/login", json={"pin": "0000000000"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) > 0

        # Confirm this really is independent of/before the lockout counter:
        # only 3 failures were ever recorded against the lockout tracker,
        # not 4 -- the 4th request never reached verify_pin() at all.
        locked, _ = remote_auth._is_locked_out("testclient")
        assert locked is False
        assert remote_auth._attempts.get("testclient", {}).get("count", 0) == 3
    finally:
        remote_auth.disable()


def test_rate_limiter_resets_after_window(client):
    remote_auth.set_login_rate_limit(max_attempts=2, window_s=1)
    remote_auth.enable(TEST_PIN)
    try:
        client.post("/api/auth/login", json={"pin": "0000000000"})
        client.post("/api/auth/login", json={"pin": "0000000000"})
        blocked = client.post("/api/auth/login", json={"pin": "0000000000"})
        assert blocked.status_code == 429

        import time
        time.sleep(1.1)

        resp = client.post("/api/auth/login", json={"pin": TEST_PIN})
        assert resp.status_code == 200
    finally:
        remote_auth.disable()


# ------------------------------------------------- new: session revocation -

def test_revoked_session_rejected_but_unrevoked_session_still_works(client):
    remote_auth.enable(TEST_PIN)
    try:
        login = client.post("/api/auth/login", json={"pin": TEST_PIN})
        assert login.status_code == 200
        cookie = login.cookies.get(remote_auth.SESSION_COOKIE)
        assert cookie

        client.cookies.set(remote_auth.SESSION_COOKIE, cookie)
        ok_before = client.get("/api/system/remote-auth/status")
        assert ok_before.status_code == 200

        sessions = client.get("/api/auth/sessions")
        assert sessions.status_code == 200
        session_list = sessions.json()["sessions"]
        assert len(session_list) == 1
        session_id = session_list[0]["id"]
        assert "created_at" in session_list[0]
        assert "ip" in session_list[0]

        revoke = client.post("/api/auth/sessions/revoke", json={"session_id": session_id})
        assert revoke.status_code == 200

        rejected = client.get("/api/system/remote-auth/status")
        assert rejected.status_code == 401

        # A second, fresh login (new token/new jti) must be unaffected by
        # the previous token's revocation.
        login2 = client.post("/api/auth/login", json={"pin": TEST_PIN})
        assert login2.status_code == 200
        cookie2 = login2.cookies.get(remote_auth.SESSION_COOKIE)
        assert cookie2 != cookie
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie2)
        still_ok = client.get("/api/system/remote-auth/status")
        assert still_ok.status_code == 200
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_revoke_all_sessions_logs_out_everyone(client):
    remote_auth.enable(TEST_PIN)
    try:
        login_a = client.post("/api/auth/login", json={"pin": TEST_PIN})
        cookie_a = login_a.cookies.get(remote_auth.SESSION_COOKIE)
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie_a)
        login_b = client.post("/api/auth/login", json={"pin": TEST_PIN})
        cookie_b = login_b.cookies.get(remote_auth.SESSION_COOKIE)

        result = client.post("/api/auth/sessions/revoke-all")
        assert result.status_code == 200
        assert result.json()["revoked"] >= 2

        client.cookies.set(remote_auth.SESSION_COOKIE, cookie_a)
        assert client.get("/api/system/remote-auth/status").status_code == 401
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie_b)
        assert client.get("/api/system/remote-auth/status").status_code == 401
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_logout_revokes_session_server_side(client):
    """Logout should be a real server-side revocation, not just a
    client-side cookie delete -- a copy of the old cookie must not work
    after logout."""
    remote_auth.enable(TEST_PIN)
    try:
        login = client.post("/api/auth/login", json={"pin": TEST_PIN})
        cookie = login.cookies.get(remote_auth.SESSION_COOKIE)
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie)

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200

        # Re-set the (now-revoked) cookie value manually, simulating an
        # attacker who captured it before logout.
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie)
        resp = client.get("/api/system/remote-auth/status")
        assert resp.status_code == 401
    finally:
        client.cookies.clear()
        remote_auth.disable()


# ------------------------------------------------------- new: audit logging -

def test_audit_log_records_failed_login_without_leaking_pin(client):
    remote_auth.enable(TEST_PIN)
    secret_wrong_pin = "9999999999"
    try:
        resp = client.post("/api/auth/login", json={"pin": secret_wrong_pin})
        assert resp.status_code == 401

        entries = _read_audit_lines()
        assert len(entries) >= 1
        last = entries[-1]
        assert last["event"] in ("login_failure", "login_lockout")
        assert last["outcome"] == "failure"
        assert "ts" in last and "timestamp" in last

        raw_log_text = open(remote_auth.AUDIT_LOG_PATH, "r", encoding="utf-8").read()
        assert secret_wrong_pin not in raw_log_text
        assert TEST_PIN not in raw_log_text
        assert "pin" not in raw_log_text.lower()
    finally:
        remote_auth.disable()


def test_audit_log_records_login_success_and_session_revoke(client):
    remote_auth.enable(TEST_PIN)
    try:
        login = client.post("/api/auth/login", json={"pin": TEST_PIN})
        cookie = login.cookies.get(remote_auth.SESSION_COOKIE)
        client.cookies.set(remote_auth.SESSION_COOKIE, cookie)

        session_id = client.get("/api/auth/sessions").json()["sessions"][0]["id"]
        client.post("/api/auth/sessions/revoke", json={"session_id": session_id})

        entries = _read_audit_lines()
        events = [e["event"] for e in entries]
        assert "login_success" in events
        assert "session_revoked" in events

        raw_log_text = open(remote_auth.AUDIT_LOG_PATH, "r", encoding="utf-8").read()
        assert TEST_PIN not in raw_log_text
        # Cookie value itself (payload.signature) must never appear raw.
        assert cookie not in raw_log_text
    finally:
        client.cookies.clear()
        remote_auth.disable()


def test_audit_log_records_lockout_trigger(client):
    remote_auth.enable(TEST_PIN)
    try:
        for _ in range(5):
            client.post("/api/auth/login", json={"pin": "1111111111"})
        entries = _read_audit_lines()
        events = [e["event"] for e in entries]
        assert "login_lockout" in events
    finally:
        remote_auth.disable()
