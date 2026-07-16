"""Wire protocol for /ws/{code}.

The contract table lives in docs/plans/beam-webrtc-beamer.md §3 — keep this file
and that table in lockstep.
"""

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

MAX_NAME_LEN = 40


class Hello(BaseModel):
    type: Literal["hello"]
    role: Literal["receiver", "sender"]
    name: str = Field(default="", max_length=MAX_NAME_LEN)
    receiver_token: str | None = None


class Approve(BaseModel):
    type: Literal["approve"]
    peer_id: str
    allow: bool


class Signal(BaseModel):
    type: Literal["signal"]
    to: str | None = None
    payload: dict[str, Any]


class CastStream(BaseModel):
    # v2 stream casting: an approved sender asks beam to proxy an IPTV URL to
    # the receiver. The URL stays server-side (streams.py); only a token is
    # relayed. Length-capped — real IPTV URLs (with creds) fit easily.
    type: Literal["cast-stream"]
    url: str = Field(min_length=1, max_length=2048)


class Bye(BaseModel):
    type: Literal["bye"]


class Pong(BaseModel):
    type: Literal["pong"]


ClientMessage = Annotated[
    Union[Hello, Approve, Signal, CastStream, Bye, Pong], Field(discriminator="type")
]

_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(raw: str | bytes) -> ClientMessage:
    return _adapter.validate_json(raw)


# --- server → client frames -------------------------------------------------


def room_state_frame(code: str, you: dict, peers: list[dict]) -> dict:
    return {"type": "room-state", "code": code, "you": you, "peers": peers}


def signal_frame(from_id: str, payload: dict) -> dict:
    return {"type": "signal", "from": from_id, "payload": payload}


def turn_frame(creds: dict) -> dict:
    """ICE config, pushed over the WS to the receiver on join and to a sender on
    approval. Never fetchable via unauthenticated REST (review round 1)."""
    return {"type": "turn", **creds}


def stream_frame(token: str, kind: str) -> dict:
    """Tells the receiver to play a proxied stream at /stream/{token}. `kind`
    is the player hint: hls | mpegts | auto (v2)."""
    return {"type": "stream", "token": token, "kind": kind}


def error_frame(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


PING_FRAME = {"type": "ping"}
