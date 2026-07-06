"""FastAPI transport layer: pages, room REST, and the /ws/{code} signaling relay.

Protocol contract: docs/plans/beam-webrtc-beamer.md §3. Room semantics live in
rooms.py; this module only moves frames and owns sockets.
"""

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
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
)
from .rooms import Peer, Room, RoomError, RoomRegistry
from .turncreds import mint_turn_credentials

log = logging.getLogger("beam")

registry = RoomRegistry(
    code_length=settings.room_code_length,
    max_senders=settings.max_senders_per_room,
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

# TODO(v1): CSP middleware — default-src 'self'; vendor any JS libs (plan §5).


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
    # TODO(v0): per-IP rate limit before public exposure (plan §5).
    room = registry.create()
    return {"code": room.code, "receiver_token": room.receiver_token}


@app.get("/api/turn-credentials")
async def turn_credentials(code: str):
    room = registry.get(code)
    if room is None:
        return JSONResponse(error_frame("room-not-found", "no such room"), status_code=404)
    uris = settings.turn_uri_list
    if not settings.turn_secret:
        # TURN disabled (dev): hand out stun: URIs only so LAN paths still form.
        return {"username": "", "credential": "", "ttl": 0,
                "uris": [u for u in uris if u.startswith("stun:")]}
    return mint_turn_credentials(settings.turn_secret, uris, settings.turn_cred_ttl_seconds)


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


@app.websocket("/ws/{code}")
async def ws_endpoint(ws: WebSocket, code: str):
    await ws.accept()
    room: Room | None = None
    peer: Peer | None = None
    ping_task: asyncio.Task | None = None
    try:
        hello = parse_client_message(
            await asyncio.wait_for(ws.receive_text(), settings.hello_deadline_seconds)
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
        ping_task = asyncio.create_task(_pinger(ws, peer))

        while True:
            msg = parse_client_message(await ws.receive_text())
            try:
                if isinstance(msg, Pong):
                    peer.missed_pongs = 0
                elif isinstance(msg, Approve):
                    target = room.approve(peer, msg.peer_id, msg.allow)
                    if not msg.allow:
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
        # Join-time failures (bad token, room full) close the socket.
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
