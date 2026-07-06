"""Room registry + membership/approval state machine.

Pure logic, no I/O: the transport layer (main.py) owns websockets and calls in;
tests exercise this module directly. Error codes match the wire protocol
(docs/plans/beam-webrtc-beamer.md §3).
"""

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

# No 0/O/1/I/L — codes get read off a TV across a room.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

Role = Literal["receiver", "sender"]
PeerState = Literal["pending", "approved"]


class RoomError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Peer:
    id: str
    role: Role
    name: str
    state: PeerState
    ws: Any = None  # set by the transport layer; never touched here
    missed_pongs: int = 0


def _new_id() -> str:
    return secrets.token_urlsafe(8)


@dataclass
class Room:
    code: str
    receiver_token: str
    max_senders: int = 4
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    peers: dict[str, Peer] = field(default_factory=dict)

    def touch(self, now: float | None = None) -> None:
        self.last_active = now if now is not None else time.time()

    @property
    def receiver(self) -> Peer | None:
        return next((p for p in self.peers.values() if p.role == "receiver"), None)

    def senders(self) -> list[Peer]:
        return [p for p in self.peers.values() if p.role == "sender"]

    def add_receiver(self, name: str, receiver_token: str | None) -> Peer:
        if receiver_token != self.receiver_token:
            raise RoomError("bad-message", "invalid receiver token")
        if self.receiver is not None:
            raise RoomError("room-full", "room already has a receiver")
        peer = Peer(id=_new_id(), role="receiver", name=name, state="approved")
        self.peers[peer.id] = peer
        self.touch()
        return peer

    def add_sender(self, name: str) -> Peer:
        if len(self.senders()) >= self.max_senders:
            raise RoomError("room-full", "too many senders")
        peer = Peer(id=_new_id(), role="sender", name=name, state="pending")
        self.peers[peer.id] = peer
        self.touch()
        return peer

    def approve(self, actor: Peer, peer_id: str, allow: bool) -> Peer:
        """Receiver's Allow/Deny for a pending sender. On deny the peer is
        removed and returned so the transport can close its socket."""
        if actor.role != "receiver":
            raise RoomError("bad-message", "only the receiver approves peers")
        target = self.peers.get(peer_id)
        if target is None or target.role != "sender":
            raise RoomError("bad-message", "unknown sender")
        if allow:
            target.state = "approved"
        else:
            del self.peers[peer_id]
        self.touch()
        return target

    def route(self, sender_of_frame: Peer, to: str | None) -> Peer:
        """Validate a signal frame and return the single peer it goes to.
        Only receiver ↔ approved-sender pairs may exchange signals."""
        self.touch()
        if sender_of_frame.role == "sender":
            if sender_of_frame.state != "approved":
                raise RoomError("not-approved", "wait for the receiver to allow you")
            receiver = self.receiver
            if receiver is None:
                raise RoomError("room-closed", "receiver is gone")
            return receiver
        # actor is the receiver
        if to is None:
            raise RoomError("bad-message", "receiver signals must set 'to'")
        target = self.peers.get(to)
        if target is None or target.role != "sender":
            raise RoomError("bad-message", "unknown signal target")
        if target.state != "approved":
            raise RoomError("not-approved", "target sender not approved")
        return target

    def remove(self, peer_id: str) -> None:
        self.peers.pop(peer_id, None)
        self.touch()

    def snapshot_for(self, peer: Peer) -> dict:
        return {
            "code": self.code,
            "you": {"id": peer.id, "role": peer.role, "state": peer.state},
            "peers": [
                {"id": p.id, "role": p.role, "name": p.name, "state": p.state}
                for p in self.peers.values()
            ],
        }

    def expired(self, ttl_seconds: int, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_active) > ttl_seconds


class RoomRegistry:
    def __init__(self, code_length: int = 5, max_senders: int = 4):
        self.code_length = code_length
        self.max_senders = max_senders
        self.rooms: dict[str, Room] = {}

    def _new_code(self) -> str:
        for _ in range(64):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(self.code_length))
            if code not in self.rooms:
                return code
        raise RoomError("room-full", "code space exhausted")  # pragma: no cover

    def create(self) -> Room:
        room = Room(
            code=self._new_code(),
            receiver_token=secrets.token_urlsafe(16),
            max_senders=self.max_senders,
        )
        self.rooms[room.code] = room
        return room

    def get(self, code: str) -> Room | None:
        return self.rooms.get(code.strip().upper())

    def close(self, code: str) -> None:
        self.rooms.pop(code, None)

    def sweep(self, ttl_seconds: int, now: float | None = None) -> list[Room]:
        """Remove idle rooms; returns them so the transport can close sockets."""
        dead = [r for r in self.rooms.values() if r.expired(ttl_seconds, now)]
        for room in dead:
            del self.rooms[room.code]
        return dead
