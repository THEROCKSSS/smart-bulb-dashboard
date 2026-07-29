#!/usr/bin/env python3
"""bulbctl - command-line client for the Smart Bulb Dashboard REST API.

Wraps the FastAPI backend (backend/main.py) over plain HTTP so you can
control bulbs, apply scenes, and check status from a shell/cron job
without touching the web UI. Zero third-party dependencies: everything
here is stdlib (urllib), so this file can be copied anywhere a Python 3
interpreter is available and just run.

Examples:
    bulbctl list
    bulbctl on bulb-1
    bulbctl off bulb-1
    bulbctl color bulb-1 ff6600
    bulbctl brightness bulb-1 60
    bulbctl scene bulb-1 movie_night
    bulbctl status bulb-1
    bulbctl list --host 192.168.1.20 --port 8500
    BULBCTL_HOST=192.168.1.20 bulbctl list
    bulbctl login --pin 1234          # only needed if remote-auth/PIN gate is enabled
    bulbctl status bulb-1 --json | jq .brightness

Note: --host/--port/--base-url/--json/--timeout are per-command options,
so they go AFTER the subcommand name (e.g. `bulbctl list --json`, not
`bulbctl --json list`) -- run `bulbctl <command> --help` to see them.

Host/port resolution order (highest priority first):
    1. --base-url (full URL, overrides --host/--port entirely)
    2. --host / --port flags
    3. BULBCTL_BASE_URL env var
    4. BULBCTL_HOST / BULBCTL_PORT env vars
    5. default: http://127.0.0.1:8500

Note on the default port: the dashboard's own docs/scripts (README.md,
SETUP.md, Dockerfile, docker-compose.yml) all run uvicorn on port 8500,
so that's the default here too, not 8000 -- always override it if your
instance runs elsewhere.
"""
from __future__ import annotations

import argparse
import http.client
import http.cookies
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROG = "bulbctl"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8500
DEFAULT_TIMEOUT_S = 5.0
SESSION_FILE = Path(os.environ.get("BULBCTL_SESSION_FILE", str(Path.home() / ".bulbctl_session")))
SESSION_COOKIE_NAME = "sbd_session"  # must match backend/remote_auth.py SESSION_COOKIE


class BulbctlError(Exception):
    """Base class for errors we want to report cleanly (no stack trace)."""


class ConnectionProblem(BulbctlError):
    pass


class ApiError(BulbctlError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# --------------------------------------------------------------- session --
def load_session() -> dict | None:
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_session(base_url: str, cookie_value: str) -> None:
    data = {"base_url": base_url, "cookie": cookie_value}
    SESSION_FILE.write_text(json.dumps(data))
    try:
        # Restrictive permissions -- best-effort; no-op on Windows ACLs but
        # harmless there and does what's asked on POSIX systems.
        os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def clear_session() -> None:
    try:
        SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def session_cookie_for(base_url: str) -> str | None:
    session = load_session()
    if not session:
        return None
    if session.get("base_url") != base_url:
        return None
    return session.get("cookie")


# ------------------------------------------------------------------- http --
def resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    if args.host or args.port:
        host = args.host or os.environ.get("BULBCTL_HOST", DEFAULT_HOST)
        port = args.port or int(os.environ.get("BULBCTL_PORT", DEFAULT_PORT))
        return f"http://{host}:{port}"
    env_base = os.environ.get("BULBCTL_BASE_URL")
    if env_base:
        return env_base.rstrip("/")
    host = os.environ.get("BULBCTL_HOST", DEFAULT_HOST)
    port = int(os.environ.get("BULBCTL_PORT", DEFAULT_PORT))
    return f"http://{host}:{port}"


def api_request(
    method: str,
    path: str,
    base_url: str,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    cookie: str | None = None,
) -> Any:
    """Perform one HTTP request against the dashboard API.

    Returns the parsed JSON response body (or None for an empty body).
    Raises ConnectionProblem for network-level failures (unreachable host,
    DNS failure, timeout) and ApiError for HTTP error responses (4xx/5xx),
    both with a human-readable `.detail`/message and no raw traceback noise.
    """
    url = f"{base_url}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
            set_cookie_headers = resp.headers.get_all("Set-Cookie") or []
            parsed_cookie = None
            for raw in set_cookie_headers:
                jar = http.cookies.SimpleCookie()
                jar.load(raw)
                if SESSION_COOKIE_NAME in jar:
                    parsed_cookie = f"{SESSION_COOKIE_NAME}={jar[SESSION_COOKIE_NAME].value}"
            result = json.loads(body) if body else None
            if parsed_cookie is not None:
                # Let callers (login) pick this up without re-parsing headers.
                return {"__bulbctl_cookie__": parsed_cookie, "__bulbctl_body__": result}
            return result
    except urllib.error.HTTPError as e:
        raw = e.read()
        detail = None
        try:
            parsed = json.loads(raw) if raw else None
            if isinstance(parsed, dict):
                detail = parsed.get("detail")
        except json.JSONDecodeError:
            parsed = None
        if detail is None:
            detail = raw.decode(errors="replace") or e.reason or f"HTTP {e.code}"
        raise ApiError(e.code, str(detail)) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise ConnectionProblem(
            f"could not reach {base_url} ({reason}). Is the dashboard running there? "
            f"Check --host/--port or BULBCTL_HOST/BULBCTL_PORT."
        ) from None


def authed_request(args: argparse.Namespace, method: str, path: str, json_body: Any = None) -> Any:
    base_url = resolve_base_url(args)
    cookie = session_cookie_for(base_url)
    try:
        return api_request(method, path, base_url, json_body=json_body, timeout=args.timeout, cookie=cookie)
    except ApiError as e:
        if e.status == 401:
            raise ApiError(
                401,
                f"{e.detail} -- this dashboard has the PIN gate (remote access) enabled. "
                f"Run `bulbctl login --pin <pin>` (against the same --host/--port) and try again.",
            ) from None
        raise


# ------------------------------------------------------------------ views --
def print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2))


def print_table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns]
    header = "  ".join(c.ljust(w) for c, w in zip(columns, widths))
    print(header)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


def parse_hex_color(value: str) -> tuple[int, int, int]:
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in s):
        raise argparse.ArgumentTypeError(
            f"invalid hex color '{value}' -- expected e.g. 'ff6600' or '#ff6600'"
        )
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def brightness_value(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not an integer") from None
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(f"brightness must be 0-100, got {n}")
    return n


# --------------------------------------------------------------- commands --
def cmd_list(args: argparse.Namespace) -> int:
    devices = authed_request(args, "GET", "/api/devices")
    if args.json:
        print_json(devices)
        return 0
    print_table(devices, ["id", "name", "ip", "device_id", "version"])
    return 0


def cmd_power(args: argparse.Namespace, on: bool) -> int:
    result = authed_request(args, "POST", f"/api/devices/{args.device}/power", {"on": on})
    if args.json:
        print_json(result)
    else:
        print(f"{args.device}: turned {'on' if on else 'off'}")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    result = authed_request(args, "POST", f"/api/devices/{args.device}/toggle")
    if args.json:
        print_json(result)
    else:
        print(f"{args.device}: toggled")
    return 0


def cmd_color(args: argparse.Namespace) -> int:
    r, g, b = args.hex  # already parsed & validated by argparse's type=parse_hex_color
    result = authed_request(args, "POST", f"/api/devices/{args.device}/color", {"r": r, "g": g, "b": b})
    if args.json:
        print_json(result)
    else:
        print(f"{args.device}: color set to #{r:02x}{g:02x}{b:02x}")
    return 0


def cmd_brightness(args: argparse.Namespace) -> int:
    result = authed_request(args, "POST", f"/api/devices/{args.device}/brightness", {"value": args.value})
    if args.json:
        print_json(result)
    else:
        print(f"{args.device}: brightness set to {args.value}")
    return 0


def cmd_scene(args: argparse.Namespace) -> int:
    result = authed_request(args, "POST", f"/api/devices/{args.device}/scenes/apply", {"scene_id": args.scene})
    if args.json:
        print_json(result)
    else:
        print(f"{args.device}: scene '{args.scene}' applied")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = authed_request(args, "GET", f"/api/devices/{args.device}/status")
    if args.json:
        print_json(result)
        return 0
    for k, v in result.items():
        print(f"{k}: {v}")
    return 0


def cmd_scenes(args: argparse.Namespace) -> int:
    scenes = authed_request(args, "GET", "/api/scenes")
    if args.json:
        print_json(scenes)
        return 0
    print_table(scenes, ["id", "name", "description"])
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    presets = authed_request(args, "GET", "/api/presets")
    if args.json:
        print_json(presets)
        return 0
    print_table(presets, ["id", "name", "rgb"])
    return 0


def cmd_groups(args: argparse.Namespace) -> int:
    groups = authed_request(args, "GET", "/api/groups")
    if args.json:
        print_json(groups)
        return 0
    print_table(groups, ["id", "name", "device_ids"])
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    pin = args.pin
    if pin is None:
        import getpass

        pin = getpass.getpass("PIN: ")
    base_url = resolve_base_url(args)
    result = api_request("POST", "/api/auth/login", base_url, json_body={"pin": pin}, timeout=args.timeout)
    cookie = result.get("__bulbctl_cookie__") if isinstance(result, dict) else None
    if not cookie:
        raise BulbctlError(
            "login appeared to succeed but no session cookie was returned -- "
            "is remote-auth actually enabled on this dashboard?"
        )
    save_session(base_url, cookie)
    print(f"logged in to {base_url}, session saved to {SESSION_FILE}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args)
    cookie = session_cookie_for(base_url)
    try:
        api_request("POST", "/api/auth/logout", base_url, timeout=args.timeout, cookie=cookie)
    except ConnectionProblem:
        # Still clear the local session even if the server can't be reached.
        pass
    clear_session()
    print(f"logged out, cleared local session ({SESSION_FILE})")
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    base_url = resolve_base_url(args)
    cookie = session_cookie_for(base_url)
    result = api_request("GET", "/api/auth/status", base_url, timeout=args.timeout, cookie=cookie)
    if args.json:
        print_json(result)
        return 0
    print(f"remote-auth enabled: {result.get('enabled')}")
    print(f"authenticated:       {result.get('authenticated')}")
    return 0


def cmd_completion(args: argparse.Namespace) -> int:
    here = Path(__file__).resolve().parent / "completions"
    files = {
        "bash": here / "bulbctl-completion.bash",
        "zsh": here / "_bulbctl",
        "powershell": here / "bulbctl-completion.ps1",
    }
    target = files[args.shell]
    print(target.read_text())
    return 0


# --------------------------------------------------------------- parsing --
def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None, help=f"dashboard host (default: {DEFAULT_HOST}, or $BULBCTL_HOST)")
    parser.add_argument("--port", type=int, default=None, help=f"dashboard port (default: {DEFAULT_PORT}, or $BULBCTL_PORT)")
    parser.add_argument("--base-url", default=None, help="full base URL, overrides --host/--port (or $BULBCTL_BASE_URL)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help=f"request timeout in seconds (default: {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--json", action="store_true", help="print raw JSON instead of a formatted table/summary")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "bulbctl -- command-line client for the Smart Bulb Dashboard REST API.\n\n"
            "Talks to a running dashboard backend (backend/main.py) over plain HTTP.\n"
            "The dashboard must already be running (see SETUP.md); this tool does not\n"
            "start it. If the dashboard's PIN gate / remote-auth is enabled, run\n"
            "'bulbctl login' first."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("list", help="list known devices")
    add_connection_args(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("on", help="turn a device on")
    p.add_argument("device", help="device id, e.g. bulb-1")
    add_connection_args(p)
    p.set_defaults(func=lambda a: cmd_power(a, True))

    p = sub.add_parser("off", help="turn a device off")
    p.add_argument("device", help="device id, e.g. bulb-1")
    add_connection_args(p)
    p.set_defaults(func=lambda a: cmd_power(a, False))

    p = sub.add_parser("toggle", help="toggle a device's power state")
    p.add_argument("device", help="device id, e.g. bulb-1")
    add_connection_args(p)
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("color", help="set a device's RGB color from a hex string")
    p.add_argument("device", help="device id, e.g. bulb-1")
    p.add_argument("hex", type=parse_hex_color, help="hex color, e.g. ff6600 or #ff6600")
    add_connection_args(p)
    p.set_defaults(func=cmd_color)

    p = sub.add_parser("brightness", help="set a device's brightness (0-100)")
    p.add_argument("device", help="device id, e.g. bulb-1")
    p.add_argument("value", type=brightness_value, help="brightness percentage, 0-100")
    add_connection_args(p)
    p.set_defaults(func=cmd_brightness)

    p = sub.add_parser("scene", help="apply a named scene to a device")
    p.add_argument("device", help="device id, e.g. bulb-1")
    p.add_argument("scene", help="scene id, e.g. movie_night (see 'bulbctl scenes')")
    add_connection_args(p)
    p.set_defaults(func=cmd_scene)

    p = sub.add_parser("status", help="show a device's current live status")
    p.add_argument("device", help="device id, e.g. bulb-1")
    add_connection_args(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("scenes", help="list available scenes")
    add_connection_args(p)
    p.set_defaults(func=cmd_scenes)

    p = sub.add_parser("presets", help="list available preset colors")
    add_connection_args(p)
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("groups", help="list configured device groups")
    add_connection_args(p)
    p.set_defaults(func=cmd_groups)

    p = sub.add_parser("login", help="authenticate against a PIN-gated dashboard and save a session")
    p.add_argument("--pin", default=None, help="PIN (omit to be prompted securely)")
    add_connection_args(p)
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="clear the saved session (local and server-side)")
    add_connection_args(p)
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("auth-status", help="show whether remote-auth is enabled and whether we're authenticated")
    add_connection_args(p)
    p.set_defaults(func=cmd_auth_status)

    p = sub.add_parser("completion", help="print a shell completion script")
    p.add_argument("shell", choices=["bash", "zsh", "powershell"])
    p.set_defaults(func=cmd_completion)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse itself already printed a usage error to stderr; just
        # surface its exit code as a normal return value instead of letting
        # SystemExit propagate, so bulbctl.main() is embeddable/testable
        # without every caller needing to catch it.
        return e.code if isinstance(e.code, int) else 2
    try:
        return args.func(args)
    except ApiError as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 1
    except ConnectionProblem as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except BulbctlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
