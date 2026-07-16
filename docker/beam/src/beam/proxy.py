"""HLS/TS reverse proxy for stream casting (plan §2/§10, v2).

Fetches the upstream IPTV stream server-side (bypassing the browser's
mixed-content + CORS refusal), rewriting HLS playlists so every child request
comes back through this proxy, and streaming TS/segment bytes through untouched.

Every URL fetched — cast root, playlist children, redirect hops — passes
`validate_target` first, so a hostile playlist can never steer the proxy at an
internal address (see streams.py for the SSRF model).
"""

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import quote, urljoin, urlsplit

import httpcore
import httpx
from fastapi.responses import Response, StreamingResponse

from .streams import StreamCast, StreamError, resolve_public_ips, validate_target

# Some IPTV origins 403 non-player user-agents.
IPTV_UA = "VLC/3.0.20 LibVLC/3.0.20"
MAX_REDIRECTS = 5
MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
PASSTHROUGH_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")

# Per-process key signing the child URLs we emit. Child (`/s?u=`) fetches must
# carry a matching sig, so a token holder can't point the proxy at an arbitrary
# URL of their own — only at URLs beam produced by rewriting a real playlist
# the sender legitimately cast (closes the open-relay vector, Codex round 3).
# In-memory like everything else: a restart invalidates old sigs, receiver
# re-casts. SSRF is separately closed by the IP-pinned backend below.
_SIG_KEY = secrets.token_bytes(32)

_URI_ATTR = re.compile(r'URI="([^"]*)"')


class PinnedBackend(httpcore.AnyIOBackend):
    """Resolve + validate the host, then dial the exact validated IP so httpx
    never performs a second (rebindable) resolution. This is the authoritative
    SSRF guard — every connection the proxy makes goes through here."""

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        ips = resolve_public_ips(host)  # raises StreamError on any non-public IP
        return await super().connect_tcp(
            ips[0], port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )


def _encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_target(b64: str) -> str:
    pad = "=" * (-len(b64) % 4)
    return base64.urlsafe_b64decode(b64 + pad).decode()


def _sign(url: str) -> str:
    return hmac.new(_SIG_KEY, url.encode(), hashlib.sha256).hexdigest()[:32]


def verify_child(u_b64: str, sig: str) -> str:
    """Decode a child `u=` param and confirm we signed it. Raises StreamError
    on a bad/absent signature — the anti-open-relay gate."""
    target = decode_target(u_b64)
    if not hmac.compare_digest(_sign(target), sig or ""):
        raise StreamError("bad-target", "unsigned or tampered child URL")
    return target


def _child(token: str, absolute: str) -> str:
    # Absolute-path proxy URL (unambiguous at any playlist depth) + HMAC sig.
    return f"/stream/{token}/s?u={quote(_encode(absolute), safe='')}&sig={_sign(absolute)}"


def rewrite_m3u8(body: str, base_url: str, token: str) -> str:
    """Rewrite every URI in an HLS playlist to route back through the proxy.
    Handles bare segment/variant lines and URI="..." tag attributes
    (EXT-X-KEY, EXT-X-MEDIA, EXT-X-MAP, EXT-X-I-FRAME-STREAM-INF)."""
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            m = _URI_ATTR.search(stripped)
            if m:
                child = urljoin(base_url, m.group(1))
                line = _URI_ATTR.sub(f'URI="{_child(token, child)}"', line, count=1)
            out.append(line)
        else:
            out.append(_child(token, urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"


async def _open(client: httpx.AsyncClient, url: str, headers: dict):
    """Open a streaming GET, following redirects manually so each hop's host is
    re-validated as public. Returns (response, final_url)."""
    validate_target(url)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        req = client.build_request("GET", current, headers=headers)
        resp = await client.send(req, stream=True)
        if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
            nxt = urljoin(current, resp.headers["location"])
            await resp.aclose()
            validate_target(nxt)
            current = nxt
            continue
        return resp, current
    raise StreamError("bad-stream", "too many redirects")


async def _capped_read(resp: httpx.Response, cap: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > cap:
            raise StreamError("bad-stream", "playlist too large")
    return bytes(buf)


async def _streaming_body(resp: httpx.Response):
    try:
        async for chunk in resp.aiter_bytes():
            yield chunk
    finally:
        await resp.aclose()


async def proxy_fetch(
    client: httpx.AsyncClient, cast: StreamCast, target_url: str, range_header: str | None
) -> Response:
    """Proxy one upstream request: rewrite if it's a playlist, stream if it's
    media. `target_url` is the cast root or a decoded child; both are validated."""
    validate_target(target_url)
    headers = {"User-Agent": IPTV_UA, "Accept": "*/*"}
    if range_header:
        headers["Range"] = range_header
    resp, final_url = await _open(client, target_url, headers)

    ctype = resp.headers.get("content-type", "").lower()
    looks_like_playlist = "mpegurl" in ctype or urlsplit(target_url).path.lower().endswith(".m3u8")

    if looks_like_playlist:
        try:
            body = await _capped_read(resp, MAX_PLAYLIST_BYTES)
        finally:
            await resp.aclose()
        text = body.decode("utf-8", "replace")
        if text.lstrip().startswith("#EXTM3U"):
            return Response(
                rewrite_m3u8(text, final_url, cast.token),
                media_type="application/vnd.apple.mpegurl",
            )
        # mislabeled — hand the bytes back untouched
        return Response(body, media_type=ctype or "application/octet-stream")

    passthrough = {
        k: resp.headers[k] for k in PASSTHROUGH_HEADERS if k in resp.headers
    }
    return StreamingResponse(
        _streaming_body(resp),
        status_code=resp.status_code,
        media_type=ctype or "video/mp2t",
        headers=passthrough,
    )
