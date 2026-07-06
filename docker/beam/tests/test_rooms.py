import pytest

from beam.rooms import CODE_ALPHABET, RoomError, RoomRegistry


@pytest.fixture
def registry():
    return RoomRegistry(code_length=5, max_senders=2)


@pytest.fixture
def room(registry):
    return registry.create()


def receiver_of(room):
    return room.add_receiver("screen", room.receiver_token)


def test_codes_use_unambiguous_alphabet(registry):
    codes = {registry.create().code for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert len(code) == 5
        assert set(code) <= set(CODE_ALPHABET)


def test_room_count_is_capped():
    small = RoomRegistry(code_length=5, max_senders=1, max_rooms=3)
    for _ in range(3):
        small.create()
    with pytest.raises(RoomError) as exc:
        small.create()
    assert exc.value.code == "room-full"


def test_lookup_is_case_insensitive(registry, room):
    assert registry.get(room.code.lower()) is room


def test_receiver_needs_valid_token(room):
    with pytest.raises(RoomError) as exc:
        room.add_receiver("screen", "wrong-token")
    assert exc.value.code == "bad-message"


def test_only_one_receiver(room):
    receiver_of(room)
    with pytest.raises(RoomError) as exc:
        room.add_receiver("second", room.receiver_token)
    assert exc.value.code == "room-full"


def test_sender_capacity(room):
    room.add_sender("a")
    room.add_sender("b")
    with pytest.raises(RoomError) as exc:
        room.add_sender("c")
    assert exc.value.code == "room-full"


def test_sender_joins_pending_then_approved(room):
    receiver = receiver_of(room)
    sender = room.add_sender("phone")
    assert sender.state == "pending"
    room.approve(receiver, sender.id, allow=True)
    assert sender.state == "approved"


def test_deny_removes_sender(room):
    receiver = receiver_of(room)
    sender = room.add_sender("phone")
    denied = room.approve(receiver, sender.id, allow=False)
    assert denied is sender
    assert sender.id not in room.peers


def test_sender_cannot_approve(room):
    receiver_of(room)
    a = room.add_sender("a")
    b = room.add_sender("b")
    with pytest.raises(RoomError) as exc:
        room.approve(a, b.id, allow=True)
    assert exc.value.code == "bad-message"


def test_pending_sender_cannot_signal(room):
    receiver_of(room)
    sender = room.add_sender("phone")
    with pytest.raises(RoomError) as exc:
        room.route(sender, None)
    assert exc.value.code == "not-approved"


def test_approved_sender_signals_to_receiver(room):
    receiver = receiver_of(room)
    sender = room.add_sender("phone")
    room.approve(receiver, sender.id, allow=True)
    assert room.route(sender, None) is receiver


def test_receiver_must_address_signals(room):
    receiver = receiver_of(room)
    sender = room.add_sender("phone")
    room.approve(receiver, sender.id, allow=True)
    with pytest.raises(RoomError) as exc:
        room.route(receiver, None)
    assert exc.value.code == "bad-message"
    assert room.route(receiver, sender.id) is sender


def test_receiver_cannot_signal_pending_sender(room):
    receiver = receiver_of(room)
    sender = room.add_sender("phone")
    with pytest.raises(RoomError) as exc:
        room.route(receiver, sender.id)
    assert exc.value.code == "not-approved"


def test_snapshot_shape(room):
    receiver = receiver_of(room)
    sender = room.add_sender("phone")
    snap = room.snapshot_for(sender)
    assert snap["code"] == room.code
    assert snap["you"] == {"id": sender.id, "role": "sender", "state": "pending"}
    ids = {p["id"] for p in snap["peers"]}
    assert ids == {receiver.id, sender.id}


def test_sweep_reaps_idle_rooms(registry):
    stale = registry.create()
    fresh = registry.create()
    stale.last_active = 0.0
    fresh.touch(now=1_000_000.0)
    dead = registry.sweep(ttl_seconds=900, now=1_000_000.0)
    assert dead == [stale]
    assert registry.get(stale.code) is None
    assert registry.get(fresh.code) is fresh
