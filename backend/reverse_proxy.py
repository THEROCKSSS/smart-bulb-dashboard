"""Reverse-proxy / TLS awareness (Week 2, W2-034..038, W2-044).

Everything here exists because the dashboard is meant to sit behind a
TLS-terminating reverse proxy (Caddy or nginx -- see `deploy/`) the moment
it leaves the LAN. Behind a proxy, two things the existing security
controls quietly depend on stop being true:

  1. The socket peer is the *proxy*, not the client. remote_auth's per-IP
     lockout and login rate limiter both key off that peer address, so
     behind a proxy every remote user shares one bucket: one attacker
     burning five wrong PINs locks out everybody, and the rate limiter
     throttles legitimate users on the attacker's behalf. The lockout
     doesn't fail open exactly -- it fails *indiscriminate*, which is its
     own denial of service.
  2. The connection into the app looks like plain HTTP even though the
     browser is speaking HTTPS, so the session cookie would never earn its
     `Secure` flag and would happily be sent over a plaintext downgrade.

Both are fixed by reading `X-Forwarded-For` / `X-Forwarded-Proto` -- but
only from a peer we have been explicitly told to trust. Those headers are
pure client input otherwise: anything that can reach the app directly can
claim to be any source IP it likes, which turns the per-IP lockout into
decoration (a fresh forged IP per guess = an unlimited number of guesses).
That is strictly worse than the misattribution problem being fixed here,
so the default is to trust nothing and use the real socket peer.

The trusted set is deliberately process-level configuration (env vars, set
by whoever deploys the thing) and NOT a runtime API setting: a setting
that could be flipped through the API would let one session that got in
once permanently disable brute-force protection for everyone, which is
exactly the kind of privilege escalation the PIN gate is supposed to stop.

Environment variables (all optional; the defaults are the LAN-only,
plain-HTTP posture this project ships with):

  SBD_TRUSTED_PROXIES
      Comma-separated IPs and/or CIDRs whose X-Forwarded-* headers are
      honored -- e.g. "127.0.0.1,::1" for a proxy on the same host, or
      "172.18.0.0/16" for a Docker compose network. Empty (the default)
      means trust nothing. The literal "*" trusts every peer: only correct
      when the app is genuinely unreachable except through the proxy (it
      binds 127.0.0.1 only, or is on a Docker network with no published
      port), and a footgun anywhere else.
  SBD_HSTS                      on|off (default off)
  SBD_HSTS_MAX_AGE              seconds (default 31536000 = 1 year)
  SBD_HSTS_INCLUDE_SUBDOMAINS   on|off (default off)
  SBD_HSTS_PRELOAD              on|off (default off)
  SBD_HTTPS_REDIRECT            on|off (default off)

Defaults are off for both HSTS and the redirect on purpose. The redirect
would instantly break every LAN-only HTTP user, and HSTS is a *sticky*
browser-side commitment -- once a browser has seen it for a hostname it
refuses plain HTTP there until max-age elapses, which is painful to undo
on a home hostname you later want to serve over HTTP again. Set
SBD_HSTS_MAX_AGE=0 with SBD_HSTS=on to actively clear a pin you regret;
plain SBD_HSTS=off only stops sending the header, it doesn't retract one
a browser already stored.
"""

import ipaddress
import os
import sys

# Health/liveness paths, exempted from the HTTPS redirect. A proxy or
# Docker HEALTHCHECK probes these over plain HTTP from inside the host or
# container network, where there is no HTTPS listener to redirect to --
# without this exemption, turning the redirect on turns every health probe
# into a 307 and the container starts reporting itself unhealthy.
HEALTH_PATHS = {"/healthz", "/api/system/health"}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


class _Settings:
    """Parsed snapshot of the SBD_* environment. Rebuilt by
    reload_from_env(); read on every request, so parsing (especially the
    CIDR list) happens once at startup rather than per-request."""

    def __init__(self, trusted_networks, trust_all, invalid_entries,
                 hsts, hsts_max_age, hsts_include_subdomains, hsts_preload,
                 https_redirect):
        self.trusted_networks = trusted_networks
        self.trust_all = trust_all
        self.invalid_entries = invalid_entries
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age
        self.hsts_include_subdomains = hsts_include_subdomains
        self.hsts_preload = hsts_preload
        self.https_redirect = https_redirect

    @property
    def trusts_any_proxy(self):
        return self.trust_all or bool(self.trusted_networks)


def _parse_bool(raw, default=False):
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    # Anything unrecognised falls back to the default rather than being
    # guessed at -- a typo'd SBD_HTTPS_REDIRECT=yess must not silently
    # enable a redirect that locks a LAN-only user out of their dashboard.
    return default


def _parse_int(raw, default):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _parse_trusted(raw):
    """Split SBD_TRUSTED_PROXIES into ip_network objects. Returns
    (networks, trust_all, invalid_entries). Bare IPs become /32 or /128
    networks so membership testing is one code path. Unparseable entries
    are collected rather than raising -- a typo in a deployment env var
    should degrade to 'trusts less than you meant', never to a backend
    that refuses to boot, and the bad entries are surfaced through
    /api/system/proxy-status so the mistake is actually findable."""
    networks = []
    invalid = []
    trust_all = False
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry == "*":
            trust_all = True
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            invalid.append(entry)
    return networks, trust_all, invalid


def _build_settings(env):
    networks, trust_all, invalid = _parse_trusted(env.get("SBD_TRUSTED_PROXIES"))
    return _Settings(
        trusted_networks=networks,
        trust_all=trust_all,
        invalid_entries=invalid,
        hsts=_parse_bool(env.get("SBD_HSTS"), False),
        hsts_max_age=_parse_int(env.get("SBD_HSTS_MAX_AGE"), 31536000),
        hsts_include_subdomains=_parse_bool(env.get("SBD_HSTS_INCLUDE_SUBDOMAINS"), False),
        hsts_preload=_parse_bool(env.get("SBD_HSTS_PRELOAD"), False),
        https_redirect=_parse_bool(env.get("SBD_HTTPS_REDIRECT"), False),
    )


def reload_from_env(env=None, warn=True):
    """Re-read the SBD_* environment into the module-level settings.
    Called once at import; exposed so tests (and an operator poking at a
    live process) can re-apply changed env without a restart."""
    global _settings
    _settings = _build_settings(env if env is not None else os.environ)
    if warn and _settings.trust_all:
        # Loud, once, at the point of misconfiguration -- "*" means any
        # client that can open a socket to this process can forge its own
        # source IP and walk past the lockout, so it deserves to be visible
        # in the service log rather than only in /api/system/proxy-status.
        print(
            "WARNING: SBD_TRUSTED_PROXIES=* trusts X-Forwarded-For from ANY peer. "
            "Only safe if this process is unreachable except through your reverse proxy.",
            file=sys.stderr,
        )
    return _settings


_settings = _build_settings(os.environ)


def get_settings():
    return _settings


# ------------------------------------------------------------ peer trust --

def is_trusted_proxy(host):
    """May this peer's X-Forwarded-* headers be believed at all? Non-IP peer
    identifiers (starlette's TestClient default, a unix-socket deployment
    where uvicorn reports no client at all) are never trusted -- there is
    no way to bound who they represent, so treating them as a proxy would
    be trusting an unknown."""
    if not host:
        return False
    if _settings.trust_all:
        return True
    return is_known_proxy_hop(host)


def is_known_proxy_hop(host):
    """Is this address one of *our* proxies -- i.e. an entry inside
    X-Forwarded-For that we put there ourselves and should skip past when
    hunting for the client?

    Deliberately does NOT honour the "*" wildcard, and that distinction is
    load-bearing: "*" means "believe whatever peer hands me the header",
    not "every address in the world is one of my proxies". Folding the two
    together made every forwarded entry look like a hop to skip, so the
    right-to-left walk fell off the end and returned the proxy's own
    address -- silently reducing "*" to the broken no-trust behaviour it
    was configured to escape. Caught by test_wildcard_trusts_every_peer."""
    if not host or not _settings.trusted_networks:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in network for network in _settings.trusted_networks)


def _strip_port(value):
    """`1.2.3.4:5678` / `[::1]:5678` -> the bare address. Some proxies (and
    plenty of hand-written X-Forwarded-For headers) include the source
    port. A bare IPv6 address always has more than one colon, so a single
    colon can only be an IPv4 host:port pair."""
    value = value.strip().strip('"')
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def resolve_client_ip(peer, forwarded_for):
    """The address the per-IP lockout and rate limiter should key off.

    Walks X-Forwarded-For RIGHT to left, skipping entries that are
    themselves trusted proxies, and takes the first untrusted address it
    finds. Right-to-left is the part that matters: both Caddy and nginx
    (with the conventional `$proxy_add_x_forwarded_for`) *append* the real
    peer to whatever the client sent, so a client who sends
    `X-Forwarded-For: 9.9.9.9` produces `9.9.9.9, <their real ip>` and the
    rightmost entry is the only one the proxy vouches for. Reading
    left-to-right -- the obvious implementation, and the common bug --
    would hand the attacker their forged value.

    Falls back to `peer` whenever the header can't be believed: peer isn't
    a trusted proxy, header absent, or the winning entry isn't a parseable
    IP (some proxies write literal "unknown"). The socket peer can't be
    forged, so it's always the safe answer."""
    if not peer:
        return peer
    if not is_trusted_proxy(peer):
        return peer
    entries = [_strip_port(e) for e in (forwarded_for or "").split(",") if e.strip()]
    if not entries:
        return peer
    for candidate in reversed(entries):
        if is_known_proxy_hop(candidate):
            continue
        return candidate if _is_ip(candidate) else peer
    # Every hop in the chain is one of our own proxies. There's no client
    # address in here to attribute anything to, so stay with the peer
    # rather than inventing one.
    return peer


def resolve_is_https(peer, scheme, forwarded_proto):
    """Whether the *browser's* connection is HTTPS -- which is not the same
    question as whether this process's socket is. Direct TLS (uvicorn with
    --ssl-keyfile) shows up as scheme=https; a terminating proxy shows up
    as scheme=http plus X-Forwarded-Proto, believed only from a trusted
    peer. The leftmost X-Forwarded-Proto value is the original client's."""
    if (scheme or "").lower() == "https":
        return True
    if not is_trusted_proxy(peer):
        return False
    first = (forwarded_proto or "").split(",")[0].strip().lower()
    return first == "https"


# --------------------------------------------------- request-level helpers --

def _peer_host(request):
    return request.client.host if request.client else None


def server_rewrote_peer(request):
    """Heuristic: did the ASGI *server* already replace the socket peer from
    X-Forwarded-For before this app ever saw the request?

    uvicorn ships ProxyHeadersMiddleware enabled by default, trusting
    127.0.0.1, and it rewrites scope["client"] in place. When that happens
    everything in this module is reading a value the server already
    substituted, and SBD_TRUSTED_PROXIES is no longer the thing deciding
    whether the header is believed -- including for a plain local client
    that simply set the header itself. Run uvicorn with --no-proxy-headers
    so this layer owns the decision (that's what deploy/ and the Dockerfile
    do).

    The tell: uvicorn cannot recover the original source port, so it
    substitutes 0. A real TCP peer never has port 0. Combined with an
    X-Forwarded-For actually being present, that's a reliable signal.
    Diagnostic only -- nothing here changes behaviour based on it, because
    a heuristic is not a thing to make security decisions on."""
    client = request.scope.get("client")
    if not client or len(client) < 2:
        return False
    return client[1] == 0 and bool(request.headers.get("x-forwarded-for"))


def client_ip(request, default="unknown"):
    """Client address for auth accounting. Mirrors the pre-existing
    `request.client.host if request.client else "unknown"` fallback so the
    lockout keeps a stable bucket key even when the server reports no peer
    at all (unix socket)."""
    peer = _peer_host(request)
    resolved = resolve_client_ip(peer, request.headers.get("x-forwarded-for"))
    return resolved or default


def request_is_https(request):
    return resolve_is_https(
        _peer_host(request), request.url.scheme, request.headers.get("x-forwarded-proto"),
    )


def hsts_header_value():
    """The Strict-Transport-Security value to send, or None when HSTS is
    off. Callers must only apply this to a request that is actually HTTPS
    -- browsers ignore the header over plaintext (RFC 6797 sec. 7.2), so
    sending it there is noise that also makes the header look enabled when
    it isn't doing anything."""
    s = _settings
    if not s.hsts:
        return None
    value = f"max-age={s.hsts_max_age}"
    if s.hsts_include_subdomains:
        value += "; includeSubDomains"
    if s.hsts_preload:
        value += "; preload"
    return value


def should_redirect_to_https(request):
    if not _settings.https_redirect:
        return False
    if request.url.path in HEALTH_PATHS:
        return False
    return not request_is_https(request)


def https_redirect_url(request):
    """Same URL, https scheme. Host (and port, if the Host header carries
    one) are preserved as-is, so this assumes the same hostname also serves
    HTTPS -- true for the normal proxy setup, where the proxy is on 443 and
    does its own redirect anyway and this is only a backstop for traffic
    that somehow reached the app directly."""
    return str(request.url.replace(scheme="https"))


def proxy_status(request):
    """Diagnostic snapshot: what the app currently believes about this
    request's origin and TLS, plus the settings that produced that belief.
    Exists because "my lockout is still keyed to the proxy" is otherwise
    invisible until someone reverse-engineers it from the audit log. Not
    an open path -- it echoes deployment configuration, so it sits behind
    the PIN gate like every other /api/system route."""
    s = _settings
    peer = _peer_host(request)
    return {
        "peer_ip": peer,
        "peer_is_trusted_proxy": is_trusted_proxy(peer),
        "peer_rewritten_by_server": server_rewrote_peer(request),
        "client_ip": client_ip(request),
        "is_https": request_is_https(request),
        "forwarded_for": request.headers.get("x-forwarded-for"),
        "forwarded_proto": request.headers.get("x-forwarded-proto"),
        "settings": {
            "trusted_proxies": ["*"] if s.trust_all else [str(n) for n in s.trusted_networks],
            "invalid_trusted_proxy_entries": list(s.invalid_entries),
            "hsts": s.hsts,
            "hsts_header": hsts_header_value(),
            "https_redirect": s.https_redirect,
        },
    }
