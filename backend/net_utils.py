"""Client-address normalization and local/trusted classification.

Lives in its own module because both per-IP subsystems need it and neither
should import the other: the PIN gate's lockout tracker (`remote_auth`) and
the general API rate limiter (`api_rate_limit`). Keying them the same way
is what makes "locked out" and "rate limited" refer to the same client.
"""

import ipaddress

# IPv6 clients are tracked by prefix, not by exact address. A single
# residential IPv6 allocation is typically a /64 or shorter, inside which a
# client can mint effectively unlimited addresses (SLAAC privacy
# extensions rotate them automatically, without any attacker effort). Exact
# per-address tracking would therefore hand out a fresh lockout budget per
# packet source -- the lockout would look like it worked while doing
# nothing. /64 is the smallest prefix that is guaranteed to be one
# subscriber rather than many.
IPV6_TRACKING_PREFIX = 64


def _parse(raw):
    """Best-effort parse of whatever the ASGI layer handed us as the client
    host. Returns an ipaddress object, or None for a non-IP value (pytest's
    TestClient reports "testclient"; `request.client` can be absent
    entirely, in which case callers pass "unknown")."""
    if not raw:
        return None
    host = str(raw).strip()
    # "[2001:db8::1]:8500" -- bracketed form, sometimes with the port still
    # attached, shows up from proxies and some ASGI servers.
    if host.startswith("[") and "]" in host:
        host = host[1 : host.index("]")]
    # "fe80::1%eth0" -- a link-local scope id identifies the local
    # interface, not the remote client, so it must not vary the key.
    host = host.split("%", 1)[0]
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def normalize_ip(raw):
    """Canonicalize a client address so one real client always maps to one
    tracking key.

    Without this, IPv6 silently breaks per-IP tracking three ways: the same
    address written two ways ("2001:db8::1" vs its fully expanded form)
    gets two independent buckets; a scope id or bracketed host varies the
    key for one client; and "::ffff:10.0.0.5" doesn't match the same
    client's plain-IPv4 key. Non-IP values pass through unchanged -- they
    are still stable keys, which is all a tracker needs.
    """
    addr = _parse(raw)
    if addr is None:
        return str(raw).strip() if raw else "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        net = ipaddress.ip_network(f"{addr}/{IPV6_TRACKING_PREFIX}", strict=False)
        return str(net)
    return str(addr)


def is_local_ip(raw):
    """True for loopback, RFC1918/CGNAT-free private ranges, link-local and
    IPv6 unique-local addresses -- i.e. "this request came from the LAN or
    from the host itself". Used to exempt trusted local traffic from the
    general rate limiter by default. A non-IP host is NOT treated as local:
    failing closed here means an unrecognized client still gets limited."""
    addr = _parse(raw)
    if addr is None:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)
