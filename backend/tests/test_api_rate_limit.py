"""General API rate limiting (Week 2 Phase A, W2-101..120): per-IP limits on
the public HTTP surface, per-endpoint tiers, 429 + Retry-After, LAN/loopback
exemption, diagnostics metrics, window reset over time, and the W2-111
guarantee that the audio-reactive sender's internal dispatch is out of scope.

Per-IP behaviour is asserted against api_rate_limit.check() directly, because
Starlette 0.41's TestClient has no way to vary the reported client host --
every request through it arrives as "testclient". The HTTP-level tests use
that single identity and lowered thresholds instead.

The autouse `reset_api_rate_limit` fixture in conftest.py clears the module's
counters and restores default limits between tests.
"""
import os
import re
import time

import pytest

import api_rate_limit
import audio_reactive
import bulb_manager as bm
import net_utils


# Deliberately NOT the RFC5737 documentation ranges (203.0.113.0/24 and
# friends): Python's ipaddress reports those as private, so they'd be
# silently exempted by the default LAN exemption and every enforcement
# assertion below would pass for the wrong reason.
PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "9.9.9.9"


# ------------------------------------------------------------ classification

TIER_CASES = [
    ("GET", "/api/devices", "read"),
    ("HEAD", "/api/devices", "read"),
    ("POST", "/api/devices/bulb-1/color", "write"),
    ("DELETE", "/api/schedule/rule-1", "write"),
    ("PATCH", "/api/devices/bulb-1", "write"),
    # Path overrides beat the method split: an 18-second LAN scan is not
    # priced like a dict lookup just because it happens to be a POST.
    ("POST", "/api/system/scan", "expensive"),
    ("POST", "/api/devices/bulb-1/rescan", "expensive"),
    ("POST", "/api/devices/bulb-1/test-connection", "expensive"),
    # ...and the endpoints the dashboard polls on a timer get their own,
    # much larger budget.
    ("GET", "/api/devices/bulb-1/audio-reactive/status", "poll"),
    ("GET", "/api/system/health", "poll"),
]


def test_tier_classification(check_all):
    def _classifies(case):
        method, path, expected = case
        actual = api_rate_limit.tier_for(method, path)
        assert actual == expected, f"got {actual!r}, expected {expected!r}"

    check_all(TIER_CASES, _classifies, label="route",
              name=lambda c: f"{c[0]} {c[1]}")


def test_page_assets_and_login_are_not_counted():
    """One page load is a burst of static requests that says nothing about
    API abuse, and the login endpoint has its own stricter limiter in
    remote_auth whose 429 semantics must not be muddled by this one."""
    for path in ("/", "/static/app.js", "/favicon.ico", "/api/auth/login"):
        for _ in range(500):
            allowed, _retry, tier = api_rate_limit.check(PUBLIC_IP, "GET", path)
            assert allowed is True
            assert tier is None
    assert api_rate_limit.metrics()["allowed"] == 0


# -------------------------------------------------------------- enforcement

def test_read_and_write_tiers_have_independent_budgets():
    api_rate_limit.configure(exempt_local=False, limits={"read": 3, "write": 2})

    for _ in range(3):
        assert api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")[0] is True
    blocked, retry_after, tier = api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")
    assert blocked is False
    assert tier == "read"
    assert 0 < retry_after <= api_rate_limit.WINDOW_S

    # Exhausting reads must not have spent the write budget.
    for _ in range(2):
        assert api_rate_limit.check(PUBLIC_IP, "POST", "/api/devices/bulb-1/power")[0] is True
    assert api_rate_limit.check(PUBLIC_IP, "POST", "/api/devices/bulb-1/power")[0] is False


def test_one_clients_limit_does_not_affect_another():
    api_rate_limit.configure(exempt_local=False, limits={"read": 2})
    for _ in range(3):
        api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")
    assert api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")[0] is False
    assert api_rate_limit.check(OTHER_PUBLIC_IP, "GET", "/api/devices")[0] is True


def test_blocked_requests_do_not_extend_their_own_penalty():
    """A client hammering while blocked recovers when its oldest real
    request ages out, not when it finally stops -- otherwise a buggy client
    retrying in a tight loop could never recover."""
    api_rate_limit.configure(exempt_local=False, limits={"read": 2})
    api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")
    api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")

    _, first_retry, _ = api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")
    for _ in range(20):
        api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")
    _, later_retry, _ = api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")
    assert later_retry <= first_retry


def test_window_resets_over_time(monkeypatch):
    """W2-120's other half: limits must reset, not just trigger. Uses a 1s
    window rather than sleeping through the real 60s one."""
    monkeypatch.setattr(api_rate_limit, "WINDOW_S", 1.0)
    api_rate_limit.configure(exempt_local=False, limits={"write": 2})

    for _ in range(2):
        assert api_rate_limit.check(PUBLIC_IP, "POST", "/api/devices/bulb-1/power")[0] is True
    assert api_rate_limit.check(PUBLIC_IP, "POST", "/api/devices/bulb-1/power")[0] is False

    time.sleep(1.05)

    allowed, retry_after, _ = api_rate_limit.check(PUBLIC_IP, "POST", "/api/devices/bulb-1/power")
    assert allowed is True
    assert retry_after == 0.0


def test_disabling_the_limiter_stops_enforcement():
    api_rate_limit.configure(enabled=False, exempt_local=False, limits={"read": 1})
    for _ in range(20):
        assert api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")[0] is True


def test_unknown_tier_and_nonsense_limit_are_refused():
    with pytest.raises(ValueError):
        api_rate_limit.configure(limits={"nonsense": 5})
    with pytest.raises(ValueError):
        api_rate_limit.configure(limits={"read": 0})


# ------------------------------------------------------- local exemption ---

LOCAL_IPS = [
    "127.0.0.1", "::1", "192.168.1.42", "10.0.0.5", "172.16.3.9",
    "fd00::1", "fe80::1", "::ffff:192.168.1.42",
]


def test_loopback_and_lan_are_exempt_by_default(check_all):
    """W2-104: this limiter exists for unattended public exposure.
    Throttling the user's own phone on their own Wi-Fi is a bug.

    All 8 local forms in one test: if an exemption regresses it usually
    takes a whole family with it (every IPv6 form, say), and that is much
    easier to read as one grouped report.
    """
    def _exempt(ip):
        api_rate_limit.configure(limits={"read": 1})
        for i in range(50):
            allowed = api_rate_limit.check(ip, "GET", "/api/devices")[0]
            assert allowed is True, f"throttled on request {i + 1} of 50"
        blocked = api_rate_limit.metrics()["blocked"]
        assert blocked == 0, f"limiter recorded {blocked} blocked requests"

    check_all(LOCAL_IPS, _exempt, label="local address")


def test_public_addresses_are_not_exempt():
    api_rate_limit.configure(limits={"read": 1})
    assert api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")[0] is True
    assert api_rate_limit.check(PUBLIC_IP, "GET", "/api/devices")[0] is False


def test_local_exemption_can_be_turned_off():
    api_rate_limit.configure(exempt_local=False, limits={"read": 1})
    assert api_rate_limit.check("192.168.1.42", "GET", "/api/devices")[0] is True
    assert api_rate_limit.check("192.168.1.42", "GET", "/api/devices")[0] is False


def test_ipv6_clients_are_tracked_by_prefix_not_by_address():
    """Same reason the lockout does it: a client can mint unlimited
    addresses inside its own /64 for free, so per-address tracking is no
    tracking at all."""
    api_rate_limit.configure(exempt_local=False, limits={"read": 2})
    api_rate_limit.check("2001:db8:1234:5678::1", "GET", "/api/devices")
    api_rate_limit.check("2001:db8:1234:5678::99ff", "GET", "/api/devices")
    blocked = api_rate_limit.check("2001:db8:1234:5678::dead", "GET", "/api/devices")
    assert blocked[0] is False

    # A genuinely different network keeps its own budget.
    assert api_rate_limit.check("2001:db8:1234:9999::1", "GET", "/api/devices")[0] is True


def test_normalize_ip_passes_non_addresses_through():
    assert net_utils.normalize_ip("testclient") == "testclient"
    assert net_utils.normalize_ip(None) == "unknown"
    # A non-IP host must not be mistaken for a trusted local client.
    assert net_utils.is_local_ip("testclient") is False


# ------------------------------------------------------------ HTTP surface --

def test_http_request_gets_429_with_retry_after(client):
    api_rate_limit.configure(exempt_local=False, limits={"read": 2})

    assert client.get("/api/devices").status_code == 200
    assert client.get("/api/devices").status_code == 200

    blocked = client.get("/api/devices")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "rate limit" in blocked.json()["detail"]


def test_http_write_limit_is_separate_from_read_limit(client):
    api_rate_limit.configure(exempt_local=False, limits={"read": 1, "write": 3})
    assert client.get("/api/devices").status_code == 200
    assert client.get("/api/devices").status_code == 429
    # Reads being exhausted must not stop the user turning a light off.
    assert client.post("/api/devices/bulb-1/power", json={"on": True}).status_code == 200


def test_rate_limit_config_is_readable_and_settable_over_the_api(client):
    current = client.get("/api/system/rate-limit").json()
    assert current["enabled"] is True
    assert set(current["limits"]) == {"poll", "read", "write", "expensive"}

    updated = client.post("/api/system/rate-limit", json={"limits": {"write": 42}}).json()
    assert updated["limits"]["write"] == 42

    bad = client.post("/api/system/rate-limit", json={"limits": {"write": 0}})
    assert bad.status_code == 400


def test_diagnostics_exposes_rate_limit_metrics(client):
    """W2-109. The two mechanisms are separate and worth reading side by
    side when judging whether something is being attacked right now."""
    api_rate_limit.configure(exempt_local=False, limits={"read": 1})
    client.get("/api/devices")
    client.get("/api/devices")  # blocked

    # Raise the ceiling before reading diagnostics -- the diagnostics route
    # is itself a counted read, and the point here is the recorded metrics,
    # not another 429. Counters are unaffected by a limit change.
    api_rate_limit.configure(limits={"read": 100})
    body = client.get("/api/system/diagnostics/rate-limit").json()
    assert body["api"]["blocked"] >= 1
    assert body["api"]["blocked_by_tier"]["read"] >= 1
    assert body["api"]["top_blocked_ips"][0]["blocked"] >= 1
    assert body["api"]["last_blocked_path"] == "/api/devices"
    assert body["api"]["config"]["limits"]["read"] == 100

    # The auth side's counters ride along -- separate mechanism, same panel.
    assert "lockouts_triggered" in body["auth"]
    assert "login_rate_limit_blocks" in body["auth"]
    assert "locked_out_now" in body["auth"]


def test_metrics_report_live_window_usage(client):
    api_rate_limit.configure(exempt_local=False, limits={"read": 10})
    for _ in range(4):
        client.get("/api/devices")
    usage = api_rate_limit.metrics()["current_window_usage"]
    assert usage
    assert usage[0]["requests"] == 4
    assert "[read]" in usage[0]["client"]


# --------------------------------------------------------------- W2-111 ----

def test_audio_reactive_internal_dispatch_is_not_rate_limited(fake_config, fake_tuya):
    """W2-111, behaviourally. The audio engine's per-bulb sender drives the
    controller directly from an in-process queue -- it never enters the ASGI
    stack -- so a lightshow running for minutes at dozens of updates per
    second must not consume a single unit of the HTTP budget."""
    api_rate_limit.configure(exempt_local=False, limits={"read": 1, "write": 1})
    controller = bm.get_controller("bulb-1")

    sender = audio_reactive.BulbSender(controller, min_dwell_ms=1)
    try:
        for i in range(60):
            sender.queue(("hsv", float(i * 6 % 360), 100.0, 80.0))
            time.sleep(0.005)
    finally:
        sender.stop()

    device = fake_tuya["dev-fake-1"]
    assert any(call[0] == "set_colour" for call in device.calls), \
        "the sender should actually have driven the device"

    metrics = api_rate_limit.metrics()
    assert metrics["allowed"] == 0
    assert metrics["blocked"] == 0
    assert metrics["tracked_clients"] == 0


def test_check_is_only_ever_called_from_the_http_middleware():
    """W2-111, structurally. The behavioural test above only proves today's
    code is clean; this fails the moment someone calls the limiter from
    service-layer code, which is what would silently start counting internal
    dispatch against a budget meant for external clients."""
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    call_sites = []
    for name in sorted(os.listdir(backend_dir)):
        if not name.endswith(".py") or name == "api_rate_limit.py":
            continue
        with open(os.path.join(backend_dir, name), "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if line.lstrip().startswith("#"):  # prose about the rule isn't a call
                    continue
                if re.search(r"api_rate_limit\.check\s*\(", line):
                    call_sites.append((name, lineno))

    assert len(call_sites) == 1 and call_sites[0][0] == "main.py", (
        f"api_rate_limit.check() must only be called from the HTTP middleware; found {call_sites}"
    )


def test_audio_reactive_keeps_its_own_separate_limiter():
    """The audio start/stop limiter and this one are different mechanisms
    with different keys (device/group vs client IP). Collapsing them would
    make an HTTP flood able to stop a lightshow, or vice versa."""
    assert audio_reactive.check_rate_limit is not api_rate_limit.check
    assert audio_reactive._rate_limit_hits is not api_rate_limit._hits
