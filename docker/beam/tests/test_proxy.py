from urllib.parse import parse_qs, urlsplit

import httpcore
import httpx
import pytest

from beam import proxy
from beam.proxy import (
    PinnedBackend,
    _encode,
    decode_target,
    proxy_fetch,
    rewrite_m3u8,
    verify_child,
)
from beam.streams import StreamCast, StreamError


def child_target(line: str) -> str:
    """Decode the upstream URL from a rewritten '/stream/TOK/s?u=..&sig=..' line."""
    return decode_target(parse_qs(urlsplit(line).query)["u"][0])

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000
720p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2560000
http://cdn.example.com/1080p/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key.bin"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.0,
seg0.ts
#EXTINF:6.0,
http://cdn.example.com/live/seg1.ts
"""


def _cast():
    return StreamCast(token="TOK", room_code="R", url="http://1.1.1.1/live.m3u8",
                      host="1.1.1.1", kind="hls")


def test_decode_target_roundtrip():
    import base64
    url = "http://cdn.example.com/live/seg 1.ts?x=1"
    enc = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    assert decode_target(enc) == url


def test_rewrite_rewrites_bare_uris_and_resolves_relative():
    out = rewrite_m3u8(MASTER, "http://origin.example.com/hls/master.m3u8", "TOK")
    lines = [l for l in out.splitlines() if l and not l.startswith("#")]
    assert all(l.startswith("/stream/TOK/s?u=") and "&sig=" in l for l in lines)
    decoded = [child_target(l) for l in lines]
    assert "http://origin.example.com/hls/720p/index.m3u8" in decoded
    assert "http://cdn.example.com/1080p/index.m3u8" in decoded


def test_rewrite_rewrites_uri_attributes():
    out = rewrite_m3u8(MEDIA, "http://origin.example.com/live/media.m3u8", "TOK")
    assert 'URI="/stream/TOK/s?u=' in out  # KEY + MAP rewritten
    assert "#EXT-X-KEY:METHOD=AES-128" in out
    assert "#EXTINF:6.0," in out
    key_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-KEY"))
    key_uri = key_line.split('URI="')[1].split('"')[0]
    assert child_target(key_uri) == "http://origin.example.com/live/key.bin"


def test_child_urls_are_signed_and_verify():
    out = rewrite_m3u8("#EXTM3U\nseg0.ts\n", "http://1.1.1.1/live.m3u8", "TOK")
    child = next(l for l in out.splitlines() if l.startswith("/stream/"))
    q = parse_qs(urlsplit(child).query)
    assert verify_child(q["u"][0], q["sig"][0]) == "http://1.1.1.1/seg0.ts"


def test_verify_child_rejects_forged_and_missing_sig():
    # a token holder cannot craft their own target — open-relay gate
    with pytest.raises(StreamError):
        verify_child(_encode("http://evil.example/10gb.bin"), "deadbeefdeadbeefdeadbeefdeadbeef")
    with pytest.raises(StreamError):
        verify_child(_encode("http://evil.example/10gb.bin"), "")


async def test_pinned_backend_dials_the_validated_ip(monkeypatch):
    dialed = {}

    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        dialed["host"] = host
        return object()

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)
    monkeypatch.setattr(proxy, "resolve_public_ips", lambda h: ["93.184.216.34"])
    await PinnedBackend().connect_tcp("rebind.example.com", 80)
    # dialed the validated IP literal, NOT the (rebindable) hostname
    assert dialed["host"] == "93.184.216.34"


async def test_pinned_backend_blocks_rebind(monkeypatch):
    def boom(host):
        raise StreamError("bad-stream", "resolves to a non-public address")

    monkeypatch.setattr(proxy, "resolve_public_ips", boom)
    with pytest.raises(StreamError):
        await PinnedBackend().connect_tcp("rebind.example.com", 80)


async def _run(handler, cast, url, range_header=None):
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        return await proxy_fetch(client, cast, url, range_header)


async def test_proxy_rewrites_playlist():
    def handler(req):
        return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                              content=MEDIA.encode())
    resp = await _run(handler, _cast(), "http://1.1.1.1/live.m3u8")
    body = bytes(resp.body).decode()
    assert body.lstrip().startswith("#EXTM3U")
    assert "/stream/TOK/s?u=" in body


async def test_proxy_streams_ts_passthrough():
    payload = b"\x47" + b"\x00" * 187  # a TS packet-ish blob
    def handler(req):
        return httpx.Response(200, headers={"content-type": "video/mp2t"}, content=payload)
    resp = await _run(handler, _cast(), "http://1.1.1.1/live/seg0.ts")
    chunks = b"".join([c async for c in resp.body_iterator])
    assert chunks == payload
    assert resp.media_type == "video/mp2t"


async def test_proxy_follows_and_revalidates_redirect():
    def handler(req):
        if req.url.path == "/live.m3u8":
            return httpx.Response(302, headers={"location": "http://1.1.1.1/real.m3u8"})
        return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                              content=MEDIA.encode())
    resp = await _run(handler, _cast(), "http://1.1.1.1/live.m3u8")
    assert bytes(resp.body).decode().lstrip().startswith("#EXTM3U")


async def test_proxy_redirect_to_private_is_blocked(monkeypatch):
    def handler(req):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
    with pytest.raises(StreamError):
        await _run(handler, _cast(), "http://1.1.1.1/live.m3u8")


async def test_proxy_rejects_oversized_playlist(monkeypatch):
    monkeypatch.setattr(proxy, "MAX_PLAYLIST_BYTES", 100)
    def handler(req):
        return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                              content=b"#EXTM3U\n" + b"#" * 500)
    with pytest.raises(StreamError):
        await _run(handler, _cast(), "http://1.1.1.1/big.m3u8")
