import httpx
import pytest

from beam import proxy
from beam.proxy import decode_target, proxy_fetch, rewrite_m3u8
from beam.streams import StreamCast, StreamError

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
    # every non-comment line becomes a proxied child
    lines = [l for l in out.splitlines() if l and not l.startswith("#")]
    assert all(l.startswith("/stream/TOK/s?u=") for l in lines)
    # relative resolves against the playlist base; absolute preserved
    decoded = [decode_target(l.split("u=")[1]) for l in lines]
    assert "http://origin.example.com/hls/720p/index.m3u8" in decoded
    assert "http://cdn.example.com/1080p/index.m3u8" in decoded


def test_rewrite_rewrites_uri_attributes():
    out = rewrite_m3u8(MEDIA, "http://origin.example.com/live/media.m3u8", "TOK")
    assert 'URI="/stream/TOK/s?u=' in out  # KEY + MAP rewritten
    # comment/tag structure preserved
    assert "#EXT-X-KEY:METHOD=AES-128" in out
    assert "#EXTINF:6.0," in out
    # the AES key URI resolves relative to the playlist
    key_line = next(l for l in out.splitlines() if l.startswith("#EXT-X-KEY"))
    enc = key_line.split('URI="')[1].split('"')[0].split("u=")[1]
    assert decode_target(enc) == "http://origin.example.com/live/key.bin"


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
