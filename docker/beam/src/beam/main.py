"""FastAPI transport layer: pages, room REST, and the /ws/{code} signaling relay.

Protocol contract: docs/plans/beam-webrtc-beamer.md §3. Room semantics live in
rooms.py; this module only moves frames and owns sockets.
"""

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .protocol import (
    PING_FRAME,
    Approve,
    Bye,
    Hello,
    Pong,
    Signal,
    error_frame,
    parse_client_message,
    room_state_frame,
    signal_frame,
    turn_frame,
)
from .rooms import Peer, Room, RoomError, RoomRegistry
from .turncreds import mint_turn_credentials

log = logging.getLogger("beam")

registry = RoomRegistry(
    code_length=settings.room_code_length,
    max_senders=settings.max_senders_per_room,
    max_rooms=settings.max_rooms,
)

STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    sweeper = asyncio.create_task(_sweep_loop())
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper


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
                for p in list(room.peers.values()):
                    await _close_with_error(p.ws, "room-closed", "the screen left")
            else:
                await _broadcast_state(room)
        with contextlib.suppress(Exception):
            await ws.close()
