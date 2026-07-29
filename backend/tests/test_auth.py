"""Auth login/status: POST /api/auth/login, GET /api/auth/status.

Uses the real remote_auth.py PBKDF2/HMAC session logic (pointed at a
pytest tmp_path state file via the `auth_reset` fixture instead of the
real backend/data/remote_auth.json) with real correct vs incorrect PINs.
"""

from fastapi.testclient import TestClient

import main as main_module
import remote_auth


def test_login_with_correct_pin_succeeds_and_sets_session_cookie(auth_reset):
    remote_auth.enable("1234")
    http_client = TestClient(main_module.app)

    resp = http_client.post("/api/auth/login", json={"pin": "1234"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert remote_auth.SESSION_COOKIE in resp.cookies


def test_login_with_incorrect_pin_fails_and_sets_no_cookie(auth_reset):
    remote_auth.enable("1234")
    http_client = TestClient(main_module.app)

    resp = http_client.post("/api/auth/login", json={"pin": "0000"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "incorrect PIN"
    assert remote_auth.SESSION_COOKIE not in resp.cookies


def test_login_when_pin_auth_not_enabled_fails(auth_reset):
    http_client = TestClient(main_module.app)
    resp = http_client.post("/api/auth/login", json={"pin": "anything"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "PIN auth is not enabled"


def test_auth_status_before_and_after_real_login(auth_reset):
    remote_auth.enable("5678")
    http_client = TestClient(main_module.app)

    before = http_client.get("/api/auth/status").json()
    assert before == {"enabled": True, "authenticated": False}

    login_resp = http_client.post("/api/auth/login", json={"pin": "5678"})
    assert login_resp.status_code == 200

    after = http_client.get("/api/auth/status").json()
    assert after == {"enabled": True, "authenticated": True}


def test_auth_status_when_disabled_reports_authenticated_true(auth_reset):
    http_client = TestClient(main_module.app)
    status = http_client.get("/api/auth/status").json()
    # per main.py: (not state["enabled"]) or verify_session_token(...) --
    # disabled means every request is considered authenticated (no gate)
    assert status == {"enabled": False, "authenticated": True}


def test_pin_gate_blocks_protected_route_without_a_session(auth_reset, fake_config, fake_tuya):
    remote_auth.enable("1234")
    http_client = TestClient(main_module.app)

    resp = http_client.get("/api/devices")

    assert resp.status_code == 401
    assert resp.json() == {"detail": "authentication required"}


def test_pin_gate_allows_protected_route_after_real_login(auth_reset, fake_config, fake_tuya):
    remote_auth.enable("1234")
    http_client = TestClient(main_module.app)

    login_resp = http_client.post("/api/auth/login", json={"pin": "1234"})
    assert login_resp.status_code == 200

    resp = http_client.get("/api/devices")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_repeated_wrong_pins_eventually_lock_out(auth_reset):
    remote_auth.enable("1234")
    http_client = TestClient(main_module.app)

    last = None
    for _ in range(remote_auth.MAX_ATTEMPTS):
        last = http_client.post("/api/auth/login", json={"pin": "0000"})
    assert last.status_code == 401

    locked_resp = http_client.post("/api/auth/login", json={"pin": "1234"})
    assert locked_resp.status_code == 401
    assert "locked out" in locked_resp.json()["detail"]
