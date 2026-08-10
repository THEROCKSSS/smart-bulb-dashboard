#!/usr/bin/env python
"""start-audio-session -- log in and start a BRIDGE audio-reactive session.

Why this exists
---------------
Getting music onto the lights takes two independent things, and having one
without the other looks like a bug rather than a missing step:

1. `tools/start-audio-bridge.py` captures Windows audio and streams it to the
   backend. It happily streams into a void -- a connected bridge with no
   session running is silent lights and no error anywhere.
2. A *session* consuming `source="bridge"` has to be started on the backend.

Step 2 is behind the PIN gate, which has no loopback exemption by design
(see backend/main.py's `pin_gate` and remote_auth.OPEN_PATHS). So it cannot
be done from a script that does not know the PIN, and it cannot be done by
importing the backend modules either: `start_session` attaches the session to
*the running server process*, and a fresh `python -c` import would start one
inside a throwaway interpreter that exits a moment later, doing nothing.

Hence this: one command, one hidden PIN prompt, session started.

Usage
-----
    python tools/start-audio-session.py                  # bulb-1, band_fixed
    python tools/start-audio-session.py --mode vu_meter
    python tools/start-audio-session.py --device bulb-1 --n-bands 6
    python tools/start-audio-session.py --stop

The PIN is read with getpass -- never echoed, never a command-line argument,
so it stays out of shell history and out of any terminal transcript. Pass
`--pin-env SBD_PIN` to read it from an environment variable instead (useful
from a scheduler, where there is no terminal to prompt at).

Stdlib only, matching cli/bulbctl.py, so it runs under any Python 3 without
the backend venv.
"""
from __future__ import annotations

import argparse
import getpass
import http.cookies
import json
import os
import sys
import urllib.error
import urllib.request

# Must match backend/remote_auth.py's SESSION_COOKIE and cli/bulbctl.py.
SESSION_COOKIE_NAME = "sbd_session"

# 8504, not 8500/8502. The container publishes the app on host 8504 -- 8502
# cannot be bound any more because tailscaled holds it for `tailscale serve`
# (see docker-compose.podman.yml). The tailnet URL is still :8502 and proxies
# here, so this default is right for a LOCAL run and the tailnet keeps working
# for everyone else.
DEFAULT_BASE_URL = os.environ.get("SBD_BASE_URL", "http://127.0.0.1:8504")

DEFAULT_DEVICE = "bulb-1"


class Failed(Exception):
    """Anything the operator can act on. Printed without a traceback."""


def _request(method, base_url, path, body=None, cookie=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode() or "{}"
            return json.loads(raw), resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        # 409 is the backend telling us the bridge listener is not running --
        # a real configuration problem, so surface its message verbatim rather
        # than flattening it into "request failed".
        raise Failed(f"HTTP {e.code} from {path}: {detail}")
    except urllib.error.URLError as e:
        raise Failed(
            f"could not reach {base_url} ({e.reason}). Is the container up? "
            f"Check: podman ps --filter name=smart-bulb-dashboard"
        )


def login(base_url, pin):
    _, set_cookies = _request("POST", base_url, "/api/auth/login", {"pin": pin})
    for header in set_cookies:
        jar = http.cookies.SimpleCookie()
        jar.load(header)
        if SESSION_COOKIE_NAME in jar:
            return f"{SESSION_COOKIE_NAME}={jar[SESSION_COOKIE_NAME].value}"
    raise Failed(
        "login returned no session cookie -- is the PIN gate actually enabled? "
        "Check: curl http://127.0.0.1:8504/api/auth/status"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default {DEFAULT_BASE_URL}")
    ap.add_argument("--device", default=DEFAULT_DEVICE, help=f"bulb id (default {DEFAULT_DEVICE})")
    ap.add_argument("--mode", default="band_fixed", help="audio mode (default band_fixed)")
    ap.add_argument("--n-bands", type=int, default=6, help="frequency bands (default 6)")
    ap.add_argument("--sensitivity", type=float, default=None, help="explicit gain; omit to use saved calibration")
    ap.add_argument("--min-dwell-ms", type=int, default=90, help="minimum ms between colour changes (default 90)")
    ap.add_argument("--stop", action="store_true", help="stop the session instead of starting one")
    ap.add_argument("--pin-env", default=None, metavar="VAR", help="read the PIN from this env var instead of prompting")
    args = ap.parse_args(argv)

    if args.pin_env:
        pin = os.environ.get(args.pin_env)
        if not pin:
            raise Failed(f"--pin-env {args.pin_env} given but that variable is empty")
    else:
        pin = getpass.getpass("PIN: ")
    if not pin:
        raise Failed("no PIN entered")

    cookie = login(args.base_url, pin)
    print("authenticated")

    if args.stop:
        _request("POST", args.base_url, f"/api/devices/{args.device}/audio-reactive/stop", {}, cookie)
        print(f"stopped the audio-reactive session on {args.device}")
        return 0

    # device_index is required by the model but meaningless for a bridge
    # session -- the audio comes from the host, not from any index this
    # container could enumerate. The backend keys bridge calibration on the
    # literal string "bridge" for exactly this reason (main.py's device_key).
    body = {
        "device_index": 0,
        "source": "bridge",
        "mode": args.mode,
        "n_bands": args.n_bands,
        "min_dwell_ms": args.min_dwell_ms,
    }
    if args.sensitivity is not None:
        body["sensitivity"] = args.sensitivity

    result, _ = _request("POST", args.base_url, f"/api/devices/{args.device}/audio-reactive/start", body, cookie)
    print(f"session started on {args.device}: " + json.dumps(
        {k: result[k] for k in ("mode", "source", "n_bands", "min_dwell_ms") if k in result}))
    print("\nPlay something. If the lights do not move, the bridge is not")
    print("streaming -- check its window, or re-run tools/start-audio-bridge.cmd --probe")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failed as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
