import socket

import pytest

from beam import streams
from beam.streams import (
    StreamError,
    StreamRegistry,
    guess_kind,
    host_is_public,
    resolve_public_ips,
    validate_target,
)


def fake_getaddrinfo(ip_map):
    def _f(host, *a, **k):
        if host not in ip_map:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_map[host], 0))]
    return _f


def test_guess_kind():
    assert guess_kind("http://x/live.m3u8") == "hls"
    assert guess_kind("http://x/live.m3u8?token=1") == "hls"
    assert guess_kind("http://x/user/pass/123.ts") == "mpegts"
    assert guess_kind("http://x/user/pass/123") == "auto"


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "9.9.9.9", "208.67.222.222"])
def test_public_ip_literals_allowed(ip):
    # literal IPs need no DNS; getaddrinfo returns them directly
    assert host_is_public(ip) is True


def test_documentation_ranges_blocked():
    # TEST-NET (192.0.2/24, 198.51.100/24, 203.0.113/24) is reserved, not routable
    assert host_is_public("203.0.113.5") is False


def test_ipv4_mapped_ipv6_private_blocked():
    assert host_is_public("::ffff:10.0.0.1") is False
    assert host_is_public("::ffff:127.0.0.1") is False


def test_trailing_dot_normalized():
    # "1.1.1.1." resolves the same as "1.1.1.1"; must not sneak past
    assert host_is_public("1.1.1.1.") is True
    assert host_is_public("10.0.0.1.") is False


def test_resolve_public_ips_returns_literals_for_pinning():
    ips = resolve_public_ips("1.1.1.1")
    assert ips == ["1.1.1.1"]
    with pytest.raises(StreamError):
        resolve_public_ips("10.0.0.1")


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",           # RFC1918
        "192.168.1.1",        # RFC1918
        "172.16.5.5",         # RFC1918
        "127.0.0.1",          # loopback
        "169.254.1.1",        # link-local
        "100.64.0.5",         # CGNAT — the tailnet range
        "0.0.0.0",            # unspecified
        "::1",                # v6 loopback
        "fe80::1",            # v6 link-local
        "fc00::1",            # v6 ULA
    ],
)
def test_internal_ip_literals_blocked(ip):
    assert host_is_public(ip) is False


def test_hostname_resolving_to_private_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo({"evil.test": "10.1.2.3"}))
    assert host_is_public("evil.test") is False


def test_hostname_with_any_private_result_is_blocked(monkeypatch):
    # DNS returning a mix must fail closed on the private one
    def multi(host, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.2.3.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 0)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", multi)
    assert host_is_public("rebind.test") is False


def test_unresolvable_host_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo({}))
    assert host_is_public("nope.test") is False


def test_validate_target_rejects_bad_scheme():
    for url in ["file:///etc/passwd", "ftp://1.1.1.1/x", "gopher://1.1.1.1/"]:
        with pytest.raises(StreamError):
            validate_target(url)


def test_validate_target_rejects_private_and_accepts_public():
    with pytest.raises(StreamError):
        validate_target("http://10.0.0.1/live.m3u8")
    assert validate_target("http://1.1.1.1/live.m3u8") == "1.1.1.1"


def test_registry_mint_and_get():
    reg = StreamRegistry(ttl_seconds=100)
    cast = reg.mint("ROOM1", "http://1.1.1.1/live.m3u8", now=1000.0)
    assert cast.kind == "hls" and cast.host == "1.1.1.1"
    assert reg.get(cast.token, now=1050.0) is cast


def test_registry_ttl_expiry():
    reg = StreamRegistry(ttl_seconds=100)
    cast = reg.mint("ROOM1", "http://1.1.1.1/x.ts", now=1000.0)
    with pytest.raises(StreamError) as exc:
        reg.get(cast.token, now=1101.0)
    assert exc.value.code == "stream-not-found"


def test_one_cast_per_room_supersedes():
    reg = StreamRegistry()
    first = reg.mint("ROOM1", "http://1.1.1.1/a.m3u8")
    second = reg.mint("ROOM1", "http://1.1.1.1/b.m3u8")
    with pytest.raises(StreamError):
        reg.get(first.token)  # old token invalidated
    assert reg.get(second.token) is second


def test_drop_room():
    reg = StreamRegistry()
    cast = reg.mint("ROOM1", "http://1.1.1.1/a.m3u8")
    reg.drop_room("ROOM1")
    with pytest.raises(StreamError):
        reg.get(cast.token)


def test_mint_rejects_private_url():
    reg = StreamRegistry()
    with pytest.raises(StreamError):
        reg.mint("ROOM1", "http://169.254.169.254/latest/meta-data/")
