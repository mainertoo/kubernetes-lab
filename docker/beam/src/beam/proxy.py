"""HLS/TS reverse proxy for stream casting (plan §2/§10, v2).

Fetches the upstream IPTV stream server-side (bypassing the browser's
mixed-content + CORS refusal), rewriting HLS playlists so every child request
comes back through this proxy, and streaming TS/segment bytes through untouched.

Every URL fetched — cast root, playlist children, redirect hops — passes
`validate_target` first, so a hostile playlist can never steer the proxy at an
internal address (see streams.py for the SSRF model).
"""

import base64
import re
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi.responses import Response, StreamingResponse

from .streams import StreamCast, StreamError, validate_target

# Some IPTV origins 403 non-player user-agents.
IPTV_UA = "VLC/3.0.20 LibVLC/3.0.20"
MAX_REDIRECTS = 5
MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
PASSTHROUGH_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")

_URI_ATTR = re.compile(r'URI="([^"]*)"')


def _encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def decode_target(b64: str) -> str:
    pad = "=" * (-len(b64) % 4)
    return base64.urlsafe_b64decode(b64 + pad).decode()


def _child(token: str, absolute: str) -> str:
    # Absolute-path proxy URL: unambiguous regardless of playlist depth.
    return f"/stream/{token}/s?u={quote(_encode(absolute), safe='')}"


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
