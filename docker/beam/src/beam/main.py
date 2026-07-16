"""FastAPI transport layer: pages, room REST, and the /ws/{code} signaling relay.

Protocol contract: docs/plans/beam-webrtc-beamer.md §3. Room semantics live in
rooms.py; this module only moves frames and owns sockets.
"""

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import settings
from .protocol import (
    PING_FRAME,
    Approve,
    Bye,
    CastStream,
    Hello,
    Pong,
    Signal,
    error_frame,
    parse_client_message,
    room_state_frame,
    signal_frame,
    stream_frame,
    turn_frame,
)
from .proxy import PinnedBackend, proxy_fetch, verify_child
from .rooms import Peer, Room, RoomError, RoomRegistry
from .streams import StreamError, StreamRegistry
from .turncreds import mint_turn_credentials

log = logging.getLogger("beam")

registry = RoomRegistry(
    code_length=settings.room_code_length,
    max_senders=settings.max_senders_per_room,
    max_rooms=settings.max_rooms,
)
streams = StreamRegistry(
    ttl_seconds=settings.stream_ttl_seconds,
    max_active=settings.max_active_streams,
)

STATIC_DIR = Path(__file__).parent / "static"

# Shared HTTP client for the stream proxy — set in lifespan.
http_client: httpx.AsyncClient | None = None


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global http_client
    # IP-pinning transport: the proxy dials only addresses resolve_public_ips
    # validated, closing DNS-rebinding SSRF (Codex review round 3). Private
    # _pool attr is httpx-internal but stable; test_proxy pins the behaviour.
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool._network_backend = PinnedBackend()
    http_client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,  # proxy._open follows manually, revalidating each hop
        timeout=httpx.Timeout(
            connect=settings.stream_connect_timeout,
            read=settings.stream_read_timeout,
            write=settings.stream_read_timeout,
            pool=settings.stream_connect_timeout,
        ),
    )
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    await http_client.aclose()


app = FastAPI(title="beam", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    # 'self' covers same-origin fetch AND ws(s): in current browsers. All JS is
    # served from /static — no inline scripts, no CDNs; vendor any future libs.
    # blob: in img-src AND media-src — cast photos/videos arrive over the
    # DataChannel and render from blob URLs (v1 field bug: videos played but
    # photos were silently CSP-blocked because img-src lacked blob:).
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # no-cache = "revalidate before use", not "don't store": every load does a
    # conditional request and gets a 304 unless the image changed. Heuristic
    # caching served week-old JS after deploys and burned two field tests.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def landing():
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/screen")
async def receiver_page():
    return FileResponse(STATIC_DIR / "receiver.html")


@app.get("/s")
async def sender_page():
    return FileResponse(STATIC_DIR / "sender.html")


@app.post("/api/rooms")
async def create_room():
    # TODO(v0): per-IP rate limit before public exposure (plan §5) — needs the
    # real client IP, i.e. trusted X-Forwarded-For from Traefik/cloudflared.
    room = registry.create()
    return {"code": room.code, "receiver_token": room.receiver_token}


# --- stream proxy (v2) --------------------------------------------------------
# Unauthenticated by URL, but a valid token is only obtainable by an approved
# sender over the WS (streams.py) and is unguessable + short-lived. Anyone who
# can hit these already has a live token, i.e. is in the room.


async def _proxy(token: str, target_url: str, range_header: str | None) -> Response:
    if not settings.stream_proxy_enabled or http_client is None:
        return JSONResponse({"error": "stream proxy disabled"}, status_code=404)
    try:
        cast = streams.get(token)
        return await proxy_fetch(http_client, cast, target_url, range_header)
    except StreamError as exc:
        status = 404 if exc.code == "stream-not-found" else 400
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=status)
    except (httpx.HTTPError, httpx.StreamError):
        return JSONResponse({"error": "upstream-error"}, status_code=502)


@app.get("/stream/{token}")
async def stream_root(token: str, request: Request):
    try:
        cast = streams.get(token)
    except StreamError as exc:
        return JSONResponse({"error": exc.code}, status_code=404)
    return await _proxy(token, cast.url, request.headers.get("range"))


@app.get("/stream/{token}/s")
async def stream_child(token: str, u: str, sig: str, request: Request):
    # `u` must be a URL our own rewriter emitted (HMAC-signed) — a token holder
    # cannot craft an arbitrary target (anti-open-relay). It is ALSO re-validated
    # (scheme + public host) and IP-pinned inside the fetch.
    try:
        target = verify_child(u, sig)
    except StreamError:
        return JSONResponse({"error": "bad-target"}, status_code=403)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "bad-target"}, status_code=400)
    return await _proxy(token, target, request.headers.get("range"))


def _turn_payload(room: Room, peer: Peer) -> dict:
    """ICE config for one peer. Username embeds room + peer id so coturn's
    user-quota isolates per peer instead of per second (review round 1)."""
    uris = settings.turn_uri_list
    if not settings.turn_secret:
        # TURN disabled (dev): stun: URIs only, LAN paths still form.
        return {"username": "", "credential": "", "ttl": 0,
                "uris": [u for u in uris if u.startswith("stun:")]}
    return mint_turn_credentials(
        settings.turn_secret,
        uris,
        settings.turn_cred_ttl_seconds,
        label=f"beam-{room.code}-{peer.id}",
    )


# --- websocket plumbing -------------------------------------------------------


async def _send(ws: WebSocket, frame: dict) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(json.dumps(frame))


async def _close_with_error(ws: WebSocket, code: str, message: str) -> None:
    await _send(ws, error_frame(code, message))
    with contextlib.suppress(Exception):
        await ws.close()


async def _broadcast_state(room: Room) -> None:
    for peer in list(room.peers.values()):
        snap = room.snapshot_for(peer)
        await _send(peer.ws, room_state_frame(snap["code"], snap["you"], snap["peers"]))


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(60)
        for room in registry.sweep(settings.room_ttl_seconds):
            log.info("reaping idle room %s", room.code)
            streams.drop_room(room.code)
            for peer in list(room.peers.values()):
                await _close_with_error(peer.ws, "room-closed", "room expired")


async def _pinger(ws: WebSocket, peer: Peer) -> None:
    # Cloudflare idles quiet websockets (~100 s); this also detects dead peers.
    while True:
        await asyncio.sleep(settings.ping_interval_seconds)
        if peer.missed_pongs >= 2:
            await _close_with_error(ws, "bad-message", "keepalive timeout")
            return
        peer.missed_pongs += 1
        await _send(ws, PING_FRAME)


async def _receive_frame(ws: WebSocket) -> str:
    raw = await ws.receive_text()
    if len(raw) > settings.max_frame_bytes:
        raise RoomError("bad-message", "frame too large")
    return raw


@app.websocket("/ws/{code}")
async def ws_endpoint(ws: WebSocket, code: str):
    origin = ws.headers.get("origin")
    if origin is not None and origin not in settings.allowed_ws_origins:
        # Browser cross-site WS (SOP does not apply) — reject before accept.
        await ws.close(code=4403)
        return
    await ws.accept()
    room: Room | None = None
    peer: Peer | None = None
    ping_task: asyncio.Task | None = None
    try:
        hello = parse_client_message(
            await asyncio.wait_for(_receive_frame(ws), settings.hello_deadline_seconds)
        )
        if not isinstance(hello, Hello):
            await _close_with_error(ws, "bad-message", "first frame must be hello")
            return
        room = registry.get(code)
        if room is None:
            await _close_with_error(ws, "room-not-found", "no such room")
            return
        if hello.role == "receiver":
            peer = room.add_receiver(hello.name, hello.receiver_token)
        else:
            peer = room.add_sender(hello.name)
        peer.ws = ws
        await _broadcast_state(room)
        if peer.role == "receiver":
            await _send(ws, turn_frame(_turn_payload(room, peer)))
        ping_task = asyncio.create_task(_pinger(ws, peer))

        while True:
            msg = parse_client_message(await _receive_frame(ws))
            try:
                if isinstance(msg, Pong):
                    peer.missed_pongs = 0
                elif isinstance(msg, Approve):
                    target = room.approve(peer, msg.peer_id, msg.allow)
                    if msg.allow:
                        # Creds only exist for approved peers; sent before the
                        # room-state flip so the client has ICE config in hand.
                        await _send(target.ws, turn_frame(_turn_payload(room, target)))
                    else:
                        await _close_with_error(target.ws, "denied", "receiver denied the request")
                    await _broadcast_state(room)
                elif isinstance(msg, Signal):
                    target = room.route(peer, msg.to)
                    await _send(target.ws, signal_frame(peer.id, msg.payload))
                elif isinstance(msg, CastStream):
                    # Only an approved sender may cast; the receiver plays it.
                    if peer.role != "sender" or peer.state != "approved":
                        raise RoomError("not-approved", "not allowed to cast")
                    receiver = room.receiver
                    if receiver is None:
                        raise RoomError("room-closed", "no screen to cast to")
                    try:
                        cast = streams.mint(room.code, msg.url)
                    except StreamError as exc:
                        raise RoomError(exc.code, exc.message) from exc
                    await _send(receiver.ws, stream_frame(cast.token, cast.kind))
                elif isinstance(msg, Bye):
                    break
                elif isinstance(msg, Hello):
                    raise RoomError("bad-message", "already joined")
            except RoomError as exc:
                # Loop-time protocol violations are answered, not fatal.
                await _send(ws, error_frame(exc.code, exc.message))
    except (WebSocketDisconnect, TimeoutError):
        pass
    except RoomError as exc:
        # Join-time failures (bad token, room full, oversized frame) close the socket.
        await _close_with_error(ws, exc.code, exc.message)
    except Exception:
        log.exception("ws error in room %s", code)
        await _close_with_error(ws, "bad-message", "unparseable frame")
    finally:
        if ping_task:
            ping_task.cancel()
        if room is not None and peer is not None and peer.id in room.peers:
            room.remove(peer.id)
            if peer.role == "receiver":
                # Receiver gone → the room is over.
                registry.close(room.code)
                streams.drop_room(room.code)
                for p in list(room.peers.values()):
                    await _close_with_error(p.ws, "room-closed", "the screen left")
            else:
                await _broadcast_state(room)
        with contextlib.suppress(Exception):
            await ws.close()
