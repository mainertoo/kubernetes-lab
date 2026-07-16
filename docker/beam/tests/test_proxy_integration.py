"""End-to-end proxy test against a REAL local HLS origin over a real socket:
mint → GET /stream/{token} (master) → child (media playlist) → child (segment).

The SSRF guard blocks loopback by design, so this test monkeypatches
host_is_public to also permit 127.0.0.1 — the one place loopback is allowed,
and only in-process.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import ipaddress
import pytest
from fastapi.testclient import TestClient

from beam import proxy as proxy_mod
from beam import streams as streams_mod
from beam.main import app, streams
from beam.proxy import decode_target

MASTER = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000000\nmedia.m3u8\n"
MEDIA = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
SEGMENT = b"\x47\x40\x00\x10" + b"\xff" * 184  # one TS-shaped packet


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        body, ctype = {
            "/master.m3u8": (MASTER, "application/vnd.apple.mpegurl"),
            "/media.m3u8": (MEDIA, "application/vnd.apple.mpegurl"),
            "/seg0.ts": (SEGMENT, "video/mp2t"),
        }.get(path, (None, None))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def origin(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    real = streams_mod.resolve_public_ips

    def allow_loopback(host):
        h = host.strip("[]").rstrip(".")
        try:
            if ipaddress.ip_address(h).is_loopback:
                return [h]
        except ValueError:
            pass
        return real(host)

    # resolve_public_ips is the single source of truth: host_is_public /
    # validate_target call it in the streams module, the IP-pinning backend
    # calls it in the proxy module — patch both refs.
    monkeypatch.setattr(streams_mod, "resolve_public_ips", allow_loopback)
    monkeypatch.setattr(proxy_mod, "resolve_public_ips", allow_loopback)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def _child_url(playlist_line: str) -> str:
    # "/stream/TOK/s?u=<b64>" → decode the u param back to the upstream URL
    return decode_target(parse_qs(urlsplit(playlist_line).query)["u"][0])


def test_full_hls_chain_through_proxy(origin):
    with TestClient(app) as c:
        streams.casts.clear()
        cast = streams.mint("ROOM", f"{origin}/master.m3u8")

        # master playlist, rewritten
        master = c.get(f"/stream/{cast.token}")
        assert master.status_code == 200
        assert "mpegurl" in master.headers["content-type"]
        lines = [l for l in master.text.splitlines() if l and not l.startswith("#")]
        assert len(lines) == 1 and lines[0].startswith(f"/stream/{cast.token}/s?u=")
        assert _child_url(lines[0]) == f"{origin}/media.m3u8"

        # follow to the media playlist, also rewritten
        media = c.get(lines[0])
        assert media.status_code == 200
        seg_lines = [l for l in media.text.splitlines() if l and not l.startswith("#")]
        assert _child_url(seg_lines[0]) == f"{origin}/seg0.ts"

        # follow to the segment — raw TS bytes passthrough
        seg = c.get(seg_lines[0])
        assert seg.status_code == 200
        assert seg.content == SEGMENT
        assert "mp2t" in seg.headers["content-type"]


def test_proxy_refuses_loopback_without_the_patch():
    # Sanity: outside the origin fixture, loopback is blocked (guard intact)
    with pytest.raises(streams_mod.StreamError):
        streams.mint("ROOM", "http://127.0.0.1:9/master.m3u8")
