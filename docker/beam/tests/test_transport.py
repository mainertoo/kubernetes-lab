"""Transport-layer tests over real (test) websockets: origin gate, join flow,
approval gate, TURN frame delivery, relay rules, frame caps.

These cover the review-round-1 finding that only pure room logic was tested.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from beam.main import app, registry, streams

GOOD_ORIGIN = {"origin": "http://localhost:8080"}
EVIL_ORIGIN = {"origin": "https://evil.example"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        registry.rooms.clear()
        streams.casts.clear()
        yield c
        registry.rooms.clear()
        streams.casts.clear()


def approved_sender(client, rx, room):
    """Join a sender and get it approved; returns (tx_context, sender_id).
    Caller enters tx_context."""
    tx = client.websocket_connect(f"/ws/{room['code']}")
    ws = tx.__enter__()
    hello(ws, "sender", name="phone")
    sender_id = recv_type(ws, "room-state")["you"]["id"]
    recv_type(rx, "room-state")
    rx.send_json({"type": "approve", "peer_id": sender_id, "allow": True})
    recv_type(ws, "turn")
    recv_type(ws, "room-state")
    recv_type(rx, "room-state")
    return tx, ws, sender_id


def make_room(client):
    res = client.post("/api/rooms")
    assert res.status_code == 200
    return res.json()


def hello(ws, role, name="x", token=None):
    frame = {"type": "hello", "role": role, "name": name}
    if token is not None:
        frame["receiver_token"] = token
    ws.send_json(frame)


def recv_type(ws, wanted):
    while True:
        frame = ws.receive_json()
        if frame["type"] == "ping":
            ws.send_json({"type": "pong"})
            continue
        assert frame["type"] == wanted, f"expected {wanted}, got {frame}"
        return frame


def test_csp_header_on_pages(client):
    res = client.get("/screen")
    csp = res.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    # Cast photos/videos render from blob: URLs (DataChannel transfers) — a
    # missing blob: silently blanks them (v1 field bug: photos, not videos).
    assert "img-src 'self' data: blob:" in csp
    assert "media-src 'self' blob:" in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    # Stale cached JS burned two field tests — every asset must revalidate.
    assert res.headers["cache-control"] == "no-cache"
    static = client.get("/static/webrtc.js")
    assert static.headers["cache-control"] == "no-cache"


def test_cross_origin_ws_rejected_before_accept(client):
    room = make_room(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/{room['code']}", headers=EVIL_ORIGIN):
            pass  # pragma: no cover


def test_allowed_origin_and_originless_clients_accepted(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}", headers=GOOD_ORIGIN) as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
    room2 = make_room(client)
    with client.websocket_connect(f"/ws/{room2['code']}") as rx:  # no Origin (native client)
        hello(rx, "receiver", token=room2["receiver_token"])
        recv_type(rx, "room-state")


def test_receiver_gets_turn_frame_on_join(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        turn = recv_type(rx, "turn")
        # TURN disabled in tests (no BEAM_TURN_SECRET) → stun-only shape
        assert turn["username"] == "" and turn["uris"] == []


def test_wrong_receiver_token_rejected(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as ws:
        hello(ws, "receiver", token="nope")
        err = recv_type(ws, "error")
        assert err["code"] == "bad-message"


def test_unknown_room_rejected(client):
    with client.websocket_connect("/ws/ZZZZZ") as ws:
        hello(ws, "sender")
        err = recv_type(ws, "error")
        assert err["code"] == "room-not-found"


def test_full_approval_and_signal_flow(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        recv_type(rx, "turn")

        with client.websocket_connect(f"/ws/{room['code']}") as tx:
            hello(tx, "sender", name="laptop")
            state = recv_type(tx, "room-state")
            assert state["you"]["state"] == "pending"
            sender_id = state["you"]["id"]

            # pending sender cannot signal
            tx.send_json({"type": "signal", "payload": {"x": 1}})
            assert recv_type(tx, "error")["code"] == "not-approved"

            # receiver approves → sender gets turn THEN approved room-state
            state = recv_type(rx, "room-state")
            assert any(p["id"] == sender_id for p in state["peers"])
            rx.send_json({"type": "approve", "peer_id": sender_id, "allow": True})
            recv_type(tx, "turn")
            assert recv_type(tx, "room-state")["you"]["state"] == "approved"
            recv_type(rx, "room-state")

            # signal relay, `from` stamped by the server
            tx.send_json({"type": "signal", "payload": {"sdp": "offer"}})
            frame = recv_type(rx, "signal")
            assert frame["from"] == sender_id and frame["payload"]["sdp"] == "offer"

            rx.send_json({"type": "signal", "to": sender_id, "payload": {"sdp": "answer"}})
            assert recv_type(tx, "signal")["payload"]["sdp"] == "answer"


def test_deny_closes_sender_socket(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        recv_type(rx, "turn")
        with client.websocket_connect(f"/ws/{room['code']}") as tx:
            hello(tx, "sender")
            sender_id = recv_type(tx, "room-state")["you"]["id"]
            recv_type(rx, "room-state")
            rx.send_json({"type": "approve", "peer_id": sender_id, "allow": False})
            assert recv_type(tx, "error")["code"] == "denied"
            with pytest.raises(WebSocketDisconnect):
                tx.receive_json()


def test_single_sender_capacity_default(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as first:
        hello(first, "sender", name="a")
        recv_type(first, "room-state")
        with client.websocket_connect(f"/ws/{room['code']}") as second:
            hello(second, "sender", name="b")
            err = recv_type(second, "error")
            assert err["code"] == "room-full"


def test_oversized_frame_closes_connection(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as ws:
        hello(ws, "receiver", token=room["receiver_token"])
        recv_type(ws, "room-state")
        recv_type(ws, "turn")
        ws.send_json({"type": "signal", "to": "x", "payload": {"blob": "A" * 70000}})
        err = recv_type(ws, "error")
        assert err["code"] == "bad-message" and "large" in err["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_receiver_leaving_closes_room(client):
    room = make_room(client)
    rx = client.websocket_connect(f"/ws/{room['code']}")
    with rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        recv_type(rx, "turn")
        with client.websocket_connect(f"/ws/{room['code']}") as tx:
            hello(tx, "sender")
            recv_type(tx, "room-state")
            recv_type(rx, "room-state")
            rx.send_json({"type": "bye"})
            err = recv_type(tx, "error")
            assert err["code"] == "room-closed"
    assert registry.get(room["code"]) is None


# --- stream casting (v2) ------------------------------------------------------


def test_approved_sender_can_cast_stream(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        recv_type(rx, "turn")
        tx, ws, _ = approved_sender(client, rx, room)
        try:
            # public literal IP → no DNS, passes the SSRF guard
            ws.send_json({"type": "cast-stream", "url": "http://1.1.1.1/live.m3u8"})
            frame = recv_type(rx, "stream")
            assert frame["kind"] == "hls" and frame["token"]
            assert streams.get(frame["token"]).url == "http://1.1.1.1/live.m3u8"
        finally:
            tx.__exit__(None, None, None)


def test_pending_sender_cannot_cast(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        recv_type(rx, "turn")
        with client.websocket_connect(f"/ws/{room['code']}") as tx:
            hello(tx, "sender")
            recv_type(tx, "room-state")  # pending
            tx.send_json({"type": "cast-stream", "url": "http://1.1.1.1/live.m3u8"})
            assert recv_type(tx, "error")["code"] == "not-approved"


def test_cast_rejects_private_url(client):
    room = make_room(client)
    with client.websocket_connect(f"/ws/{room['code']}") as rx:
        hello(rx, "receiver", token=room["receiver_token"])
        recv_type(rx, "room-state")
        recv_type(rx, "turn")
        tx, ws, _ = approved_sender(client, rx, room)
        try:
            ws.send_json({"type": "cast-stream", "url": "http://169.254.169.254/meta"})
            assert recv_type(ws, "error")["code"] == "bad-stream"
        finally:
            tx.__exit__(None, None, None)


def test_stream_endpoint_unknown_token_404(client):
    assert client.get("/stream/nope-nope-nope").status_code == 404
