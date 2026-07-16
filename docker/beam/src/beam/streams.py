"""Stream-cast registry + SSRF guard for the v2 stream proxy.

The sports milestone: a sender casts an IPTV stream URL; the receiver plays it
directly (full quality, phone is just a remote). IPTV upstreams are plain-http
and CORS-less, so an https receiver page can't fetch them — beam proxies
(`proxy.py`). This is a deliberate, bounded break of the "server never carries
media" invariant (plan §2/§10): stream bytes flow through the pod.

Security model:
- The raw upstream URL (which often embeds provider credentials) lives ONLY in
  server memory, keyed by an opaque random token. It never reaches the receiver
  browser, the DataChannel, or any log.
- Tokens are minted only for APPROVED senders, over the WS, room-scoped, and
  die with the room or after a TTL — same trust boundary as `turn` frames.
- Every fetched URL — the cast URL, every m3u8-referenced child, every redirect
  hop — is validated to resolve to a PUBLIC IP before connecting. This is the
  hard SSRF guard: the proxy can never be pointed at cluster/tailnet/loopback
  addresses regardless of what a malicious playlist contains.
"""

import ipaddress
import secrets
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}

# 100.64.0.0/10 (CGNAT) is NOT flagged by ipaddress.is_private on older Pythons
# and is exactly the tailnet range — block it explicitly. IPv6 ULA/link-local
# are covered by is_private/is_link_local but listed for clarity.
_CGNAT4 = ipaddress.ip_network("100.64.0.0/10")


class StreamError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def host_is_public(host: str) -> bool:
    """True only if EVERY address `host` resolves to is a public, routable IP.
    Fail-closed: resolution failure or any internal address → False."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or ip in _CGNAT4
        ):
            return False
    return True


def validate_target(url: str) -> str:
    """Reject non-http(s) schemes and any host that isn't provably public.
    Returns the host on success; raises StreamError otherwise."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise StreamError("bad-stream", f"scheme not allowed: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise StreamError("bad-stream", "no host in URL")
    if not host_is_public(host):
        raise StreamError("bad-stream", "target does not resolve to a public address")
    return host


def guess_kind(url: str) -> str:
    """Player hint from the URL shape: 'hls' (.m3u8), 'mpegts' (.ts / Xtream
    live), or 'auto' (receiver tries HLS then falls back)."""
    path = urlsplit(url).path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".ts"):
        return "mpegts"
    return "auto"


@dataclass
class StreamCast:
    token: str
    room_code: str
    url: str          # raw upstream, credentials and all — server-memory only
    host: str
    kind: str
    created_at: float = field(default_factory=time.time)


class StreamRegistry:
    def __init__(self, ttl_seconds: int = 900, max_active: int = 200):
        self.ttl_seconds = ttl_seconds
        self.max_active = max_active
        self.casts: dict[str, StreamCast] = {}

    def mint(self, room_code: str, url: str, now: float | None = None) -> StreamCast:
        host = validate_target(url)  # raises on bad scheme / non-public host
        if len(self.casts) >= self.max_active:
            self._sweep(now)
        if len(self.casts) >= self.max_active:
            raise StreamError("bad-stream", "too many active streams")
        # One live cast per room: replace any prior token so old ones stop working.
        self.drop_room(room_code)
        cast = StreamCast(
            token=secrets.token_urlsafe(24),
            room_code=room_code,
            url=url,
            host=host,
            kind=guess_kind(url),
        )
        if now is not None:
            cast.created_at = now
        self.casts[cast.token] = cast
        return cast

    def get(self, token: str, now: float | None = None) -> StreamCast:
        cast = self.casts.get(token)
        now = now if now is not None else time.time()
        if cast is None or (now - cast.created_at) > self.ttl_seconds:
            if cast is not None:
                del self.casts[cast.token]
            raise StreamError("stream-not-found", "no such stream (or it expired)")
        return cast

    def drop_room(self, room_code: str) -> None:
        for tok in [t for t, c in self.casts.items() if c.room_code == room_code]:
            del self.casts[tok]

    def _sweep(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        for tok in [t for t, c in self.casts.items() if (now - c.created_at) > self.ttl_seconds]:
            del self.casts[tok]
