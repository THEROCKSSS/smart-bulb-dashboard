"""Tests for cli/bulbctl.py.

Mocks urllib.request.urlopen (the lowest-level call bulbctl makes) so we can
assert the exact HTTP request bulbctl builds -- method, URL, and JSON body --
without needing a real backend running, and so we can simulate both success
and error (device-not-found, bad/expired PIN session) responses.
"""
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bulbctl  # noqa: E402


class FakeHeaders:
    """Mimics http.client.HTTPMessage enough for bulbctl's needs."""

    def __init__(self, set_cookie=None):
        self._set_cookie = set_cookie or []

    def get_all(self, name):
        if name == "Set-Cookie":
            return self._set_cookie
        return None


class FakeResponse:
    def __init__(self, body: bytes, set_cookie=None):
        self._body = body
        self.headers = FakeHeaders(set_cookie)

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def json_response(obj, set_cookie=None):
    return FakeResponse(json.dumps(obj).encode(), set_cookie=set_cookie)


@pytest.fixture(autouse=True)
def isolated_session(tmp_path, monkeypatch):
    """Never touch the real ~/.bulbctl_session during tests."""
    monkeypatch.setattr(bulbctl, "SESSION_FILE", tmp_path / ".bulbctl_session")
    yield


def run(monkeypatch, argv, urlopen_side_effect):
    """Run bulbctl.main(argv) with urlopen replaced, capturing stdout/stderr."""
    captured_requests = []

    def fake_urlopen(request, timeout=None):
        captured_requests.append(request)
        result = urlopen_side_effect(request)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    rc = bulbctl.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue(), captured_requests


# ------------------------------------------------------------------ list --
def test_list_builds_correct_get_request_and_prints_json(monkeypatch):
    devices = [{"id": "bulb-1", "name": "Lamp", "ip": "192.168.1.50", "device_id": "abc", "version": 3.3}]
    rc, out, err, reqs = run(monkeypatch, ["list", "--json"], lambda req: json_response(devices))

    assert rc == 0
    assert err == ""
    assert len(reqs) == 1
    assert reqs[0].get_method() == "GET"
    assert reqs[0].full_url == "http://127.0.0.1:8500/api/devices"
    assert reqs[0].data is None
    assert json.loads(out) == devices


def test_list_table_output_when_no_devices(monkeypatch):
    rc, out, err, reqs = run(monkeypatch, ["list"], lambda req: json_response([]))
    assert rc == 0
    assert "(none)" in out


# -------------------------------------------------------------- on / off --
def test_on_builds_correct_post_request_with_body(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch, ["on", "bulb-1"], lambda req: json_response({"result": "ok"})
    )
    assert rc == 0
    assert len(reqs) == 1
    req = reqs[0]
    assert req.get_method() == "POST"
    assert req.full_url == "http://127.0.0.1:8500/api/devices/bulb-1/power"
    assert json.loads(req.data) == {"on": True}
    assert "turned on" in out


def test_off_builds_correct_post_request_with_body(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch, ["off", "bulb-1"], lambda req: json_response({"result": "ok"})
    )
    assert rc == 0
    req = reqs[0]
    assert req.get_method() == "POST"
    assert req.full_url == "http://127.0.0.1:8500/api/devices/bulb-1/power"
    assert json.loads(req.data) == {"on": False}


# ------------------------------------------------------------------ color --
def test_color_parses_hex_and_posts_rgb_body(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch, ["color", "bulb-1", "#ff6600", "--json"], lambda req: json_response({"result": "ok"})
    )
    assert rc == 0
    req = reqs[0]
    assert req.get_method() == "POST"
    assert req.full_url == "http://127.0.0.1:8500/api/devices/bulb-1/color"
    assert json.loads(req.data) == {"r": 255, "g": 102, "b": 0}


def test_color_rejects_invalid_hex_without_crashing(monkeypatch):
    rc, out, err, reqs = run(monkeypatch, ["color", "bulb-1", "not-a-color"], lambda req: json_response({}))
    assert rc == 2  # argparse usage error
    assert reqs == []  # never even made a request
    assert "invalid hex color" in err


# -------------------------------------------------------------- brightness --
def test_brightness_builds_correct_post_request(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch, ["brightness", "bulb-1", "60"], lambda req: json_response({"result": "ok"})
    )
    assert rc == 0
    req = reqs[0]
    assert req.get_method() == "POST"
    assert req.full_url == "http://127.0.0.1:8500/api/devices/bulb-1/brightness"
    assert json.loads(req.data) == {"value": 60}


def test_brightness_rejects_out_of_range_value(monkeypatch):
    rc, out, err, reqs = run(monkeypatch, ["brightness", "bulb-1", "150"], lambda req: json_response({}))
    assert rc == 2
    assert reqs == []
    assert "0-100" in err


# ------------------------------------------------------------------ scene --
def test_scene_builds_correct_post_request(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch, ["scene", "bulb-1", "movie_night"], lambda req: json_response({"ok": True})
    )
    assert rc == 0
    req = reqs[0]
    assert req.get_method() == "POST"
    assert req.full_url == "http://127.0.0.1:8500/api/devices/bulb-1/scenes/apply"
    assert json.loads(req.data) == {"scene_id": "movie_night"}


# ----------------------------------------------------------------- status --
def test_status_builds_correct_get_request(monkeypatch):
    status = {"data_source": "LIVE DATA", "on": True, "brightness": 60}
    rc, out, err, reqs = run(monkeypatch, ["status", "bulb-1", "--json"], lambda req: json_response(status))
    assert rc == 0
    req = reqs[0]
    assert req.get_method() == "GET"
    assert req.full_url == "http://127.0.0.1:8500/api/devices/bulb-1/status"
    assert req.data is None
    assert json.loads(out) == status


# -------------------------------------------------------- error handling --
def test_device_not_found_prints_clean_error_and_exits_nonzero(monkeypatch):
    def raise_404(req):
        body = json.dumps({"detail": "device 'nope' not found"}).encode()
        return urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(body))

    rc, out, err, reqs = run(monkeypatch, ["status", "nope"], raise_404)

    assert rc == 1
    assert "device 'nope' not found" in err
    # No raw traceback leaked to the user.
    assert "Traceback" not in err
    assert "Traceback" not in out


def test_expired_or_missing_session_gives_actionable_401_message(monkeypatch):
    def raise_401(req):
        body = json.dumps({"detail": "authentication required"}).encode()
        return urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    rc, out, err, reqs = run(monkeypatch, ["status", "bulb-1"], raise_401)

    assert rc == 1
    assert "authentication required" in err
    assert "bulbctl login" in err
    assert "Traceback" not in err


def test_unreachable_host_gives_clean_connection_error(monkeypatch):
    def raise_conn_error(req):
        return urllib.error.URLError(OSError("Connection refused"))

    rc, out, err, reqs = run(
        monkeypatch,
        ["list", "--host", "10.255.255.1", "--port", "9"],
        raise_conn_error,
    )

    assert rc == 1
    assert "could not reach" in err
    assert "10.255.255.1:9" in err
    assert "Traceback" not in err


# --------------------------------------------------------------- host/port --
def test_host_and_port_flags_change_the_target_url(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch, ["list", "--host", "192.168.1.99", "--port", "9000"], lambda req: json_response([])
    )
    assert rc == 0
    assert reqs[0].full_url == "http://192.168.1.99:9000/api/devices"


def test_base_url_flag_overrides_host_and_port(monkeypatch):
    rc, out, err, reqs = run(
        monkeypatch,
        ["list", "--base-url", "http://example.local:1234", "--host", "ignored"],
        lambda req: json_response([]),
    )
    assert rc == 0
    assert reqs[0].full_url == "http://example.local:1234/api/devices"


def test_env_vars_change_the_target_url(monkeypatch):
    monkeypatch.setenv("BULBCTL_HOST", "10.0.0.5")
    monkeypatch.setenv("BULBCTL_PORT", "8888")
    rc, out, err, reqs = run(monkeypatch, ["list"], lambda req: json_response([]))
    assert rc == 0
    assert reqs[0].full_url == "http://10.0.0.5:8888/api/devices"


# ------------------------------------------------------------------- login --
def test_login_saves_session_cookie_from_set_cookie_header(monkeypatch):
    def fake_login(req):
        assert req.get_method() == "POST"
        assert req.full_url == "http://127.0.0.1:8500/api/auth/login"
        assert json.loads(req.data) == {"pin": "1234"}
        return json_response({"ok": True}, set_cookie=["sbd_session=abc123token; HttpOnly; Path=/; SameSite=lax"])

    rc, out, err, reqs = run(monkeypatch, ["login", "--pin", "1234"], fake_login)

    assert rc == 0
    assert "logged in" in out
    saved = json.loads(bulbctl.SESSION_FILE.read_text())
    assert saved["base_url"] == "http://127.0.0.1:8500"
    assert saved["cookie"] == "sbd_session=abc123token"


def test_login_wrong_pin_reports_clean_error(monkeypatch):
    def raise_401(req):
        body = json.dumps({"detail": "incorrect PIN"}).encode()
        return urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    rc, out, err, reqs = run(monkeypatch, ["login", "--pin", "0000"], raise_401)
    assert rc == 1
    assert "incorrect PIN" in err
    assert not bulbctl.SESSION_FILE.exists()


def test_authenticated_request_sends_saved_cookie(monkeypatch):
    base_url = "http://127.0.0.1:8500"
    bulbctl.save_session(base_url, "sbd_session=abc123token")

    rc, out, err, reqs = run(monkeypatch, ["list", "--json"], lambda req: json_response([]))

    assert rc == 0
    assert reqs[0].get_header("Cookie") == "sbd_session=abc123token"


def test_logout_clears_local_session_even_if_server_unreachable(monkeypatch):
    base_url = "http://127.0.0.1:8500"
    bulbctl.save_session(base_url, "sbd_session=abc123token")

    def raise_conn_error(req):
        return urllib.error.URLError(OSError("Connection refused"))

    rc, out, err, reqs = run(monkeypatch, ["logout"], raise_conn_error)

    assert rc == 0
    assert not bulbctl.SESSION_FILE.exists()


# -------------------------------------------------------------------- misc --
def test_hex_color_parsing_helper():
    assert bulbctl.parse_hex_color("#ff6600") == (255, 102, 0)
    assert bulbctl.parse_hex_color("ff6600") == (255, 102, 0)
    assert bulbctl.parse_hex_color("fff") == (255, 255, 255)


def test_completion_command_prints_bash_script(monkeypatch, capsys):
    rc = bulbctl.main(["completion", "bash"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "_bulbctl_completions" in out
