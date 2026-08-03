"""Audio bridge: protocol framing, backpressure, and the capture-source seam.

These exercise the real socket path against a real BridgeServer on an
ephemeral port -- no audio hardware, no container, no bulb. The point is that
a protocol bug should fail here, in a second, rather than while chasing a live
capture on real hardware.
"""
import os
import socket
import struct
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import audio_bridge  # noqa: E402
import capture_sources  # noqa: E402

HEADER = struct.Struct("<4sBIBI")
FRAME = struct.Struct("<4sI")

SR = 44100
BLOCK = 512


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server():
    srv = audio_bridge.BridgeServer(host="127.0.0.1", port=_free_port())
    srv.start()
    for _ in range(100):
        if srv.status()["listening"]:
            break
        time.sleep(0.02)
    yield srv
    srv.stop()


def _connect(srv, channels=2, sample_rate=SR, block=BLOCK):
    sock = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
    sock.sendall(HEADER.pack(audio_bridge.MAGIC_HEADER, audio_bridge.PROTOCOL_VERSION,
                             sample_rate, channels, block))
    return sock


def _send_block(sock, block):
    payload = np.asarray(block, dtype="<f4").tobytes()
    sock.sendall(FRAME.pack(audio_bridge.MAGIC_FRAME, len(payload)))
    sock.sendall(payload)


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_listener_reports_not_connected_before_any_bridge(server):
    st = server.status()
    assert st["listening"] is True
    assert st["connected"] is False
    assert st["streaming"] is False


def test_frames_reach_a_subscriber(server):
    got = []
    server.subscribe(got.append)
    sock = _connect(server)
    try:
        block = np.zeros((BLOCK, 2), dtype=np.float32)
        block[:, 0] = 0.5
        _send_block(sock, block)
        assert _wait_for(lambda: len(got) >= 1), "no frame reached the subscriber"
        assert got[0].shape == (BLOCK, 2)
        assert pytest.approx(0.5, abs=1e-6) == float(got[0][0, 0])
    finally:
        sock.close()


def test_status_distinguishes_connected_from_streaming(server):
    sock = _connect(server)
    try:
        assert _wait_for(lambda: server.status()["connected"])
        # Connected but nothing sent yet: this is the "bridge attached, but
        # nothing is playing" state the dashboard shows differently from
        # "not connected", because the fix is completely different.
        assert server.status()["streaming"] is False
        _send_block(sock, np.zeros((BLOCK, 2), dtype=np.float32))
        assert _wait_for(lambda: server.status()["streaming"] is True)
    finally:
        sock.close()


def test_garbage_between_frames_is_resynchronised(server):
    got = []
    server.subscribe(got.append)
    sock = _connect(server)
    try:
        _send_block(sock, np.full((BLOCK, 2), 0.25, dtype=np.float32))
        assert _wait_for(lambda: len(got) >= 1)
        # Corrupt the stream, then send a good frame. Without the per-frame
        # marker the receiver would reinterpret arbitrary bytes as audio.
        sock.sendall(b"\x00\xffJUNKJUNK\x01\x02")
        _send_block(sock, np.full((BLOCK, 2), 0.75, dtype=np.float32))
        assert _wait_for(lambda: len(got) >= 2), "did not resynchronise after garbage"
        assert np.all(np.isfinite(got[-1]))
    finally:
        sock.close()


def test_non_finite_payloads_never_reach_subscribers(server):
    got = []
    server.subscribe(got.append)
    sock = _connect(server)
    try:
        bad = np.full((BLOCK, 2), np.nan, dtype=np.float32)
        _send_block(sock, bad)
        _send_block(sock, np.full((BLOCK, 2), 0.1, dtype=np.float32))
        assert _wait_for(lambda: len(got) >= 1)
        for block in got:
            assert np.all(np.isfinite(block)), "a NaN frame reached the analysis pipeline"
        assert server.status()["drops"] >= 1
    finally:
        sock.close()


def test_a_raising_subscriber_does_not_kill_the_connection(server):
    good = []

    def boom(_block):
        raise RuntimeError("subscriber exploded")

    server.subscribe(boom)
    server.subscribe(good.append)
    sock = _connect(server)
    try:
        _send_block(sock, np.zeros((BLOCK, 2), dtype=np.float32))
        assert _wait_for(lambda: len(good) >= 1), "one bad subscriber took down the rest"
        assert server.status()["connected"] is True
    finally:
        sock.close()


def test_second_bridge_is_refused_rather_than_interleaved(server):
    first = _connect(server)
    try:
        assert _wait_for(lambda: server.status()["connected"])
        second = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        try:
            # Two capture streams into one analysis pipeline would interleave
            # into noise, so the second is dropped, not merged.
            second.settimeout(2.0)
            assert second.recv(16) == b"", "second bridge was not refused"
        finally:
            second.close()
        assert server.status()["client"] == first.getsockname()[0] + ":" + str(first.getsockname()[1])
    finally:
        first.close()


def test_mismatched_sample_rate_is_refused(server):
    """The bridge resamples on the host precisely so this cannot happen; if it
    does, tempo and pitch would be skewed and refusing beats analysing the
    wrong thing silently."""
    server.expected_sample_rate = SR
    sock = _connect(server, sample_rate=32000)
    try:
        assert _wait_for(lambda: server.status()["connected"] is False, timeout=3.0)
    finally:
        sock.close()


# ------------------------------------------------------- capture sources ---
def test_network_source_without_a_listener_fails_loudly():
    """The old failure was a session that started, reported itself running and
    never produced a frame. It must now be an error a user can act on."""
    with pytest.raises(capture_sources.CaptureError) as exc:
        with capture_sources.NetworkSource(lambda b: None, server=None):
            pass
    assert "bridge" in str(exc.value).lower()


def test_network_source_delivers_bridge_frames_to_its_callback(server):
    received = []
    source = capture_sources.NetworkSource(received.append, server=server)
    with source:
        sock = _connect(server)
        try:
            _send_block(sock, np.full((BLOCK, 2), 0.3, dtype=np.float32))
            assert _wait_for(lambda: len(received) >= 1)
        finally:
            sock.close()
    # Unsubscribed on exit -- a stopped session must stop consuming.
    assert server.subscriber_count == 0


def test_callable_source_drives_a_callback_without_hardware():
    blocks = [np.full((BLOCK, 1), 0.2, dtype=np.float32) for _ in range(3)]
    got = []
    with capture_sources.CallableSource(blocks, got.append):
        assert _wait_for(lambda: len(got) >= 3)
    assert all(b.shape == (BLOCK, 1) for b in got)


# ---------------------------------------------- start route, bridge source ---
def test_bridge_session_does_not_validate_a_local_device_index(client, fake_config, fake_tuya,
                                                               monkeypatch):
    """Regression: starting a bridge session used to be rejected with
    "input device index 0 not found (only 0 audio devices currently
    connected)".

    The start route validated device_index unconditionally. Inside the
    container there are zero audio devices by design, so every bridge session
    failed on a check that does not apply to it -- and the message sent the
    user off to re-pick a device that was never the problem.
    """
    import main as main_module

    srv = audio_bridge.BridgeServer(host="127.0.0.1", port=_free_port())
    srv.start()
    assert _wait_for(lambda: srv.status()["listening"])
    monkeypatch.setattr(audio_bridge, "_server", srv, raising=False)
    # No local devices at all -- exactly the container's situation.
    monkeypatch.setattr(main_module.audio_reactive, "list_input_devices", lambda *a, **k: [])
    monkeypatch.setattr(main_module.audio_reactive, "validate_device_index",
                        lambda idx: (False, "input device index %s not found "
                                            "(only 0 audio devices currently connected)" % idx))
    try:
        resp = client.post("/api/devices/bulb-1/audio-reactive/start",
                           json={"source": "bridge", "device_index": 0, "mode": "vu_meter",
                                 "n_bands": 3, "min_dwell_ms": 90})
        assert resp.status_code == 200, resp.text
        assert "not found" not in resp.text
        client.post("/api/devices/bulb-1/audio-reactive/stop")
    finally:
        srv.stop()


def test_bridge_session_is_refused_when_no_listener_is_running(client, fake_config, fake_tuya,
                                                               monkeypatch):
    """The failure must be immediate and named, not a 200 followed by silence."""
    monkeypatch.setattr(audio_bridge, "_server", None, raising=False)
    resp = client.post("/api/devices/bulb-1/audio-reactive/start",
                       json={"source": "bridge", "device_index": 0, "mode": "vu_meter",
                             "n_bands": 3, "min_dwell_ms": 90})
    assert resp.status_code == 409, resp.text
    assert "bridge" in resp.text.lower()
