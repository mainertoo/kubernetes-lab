# beam

Self-hosted WebRTC screen beamer: a TV-attached computer opens the receiver page (browser
only, nothing installed), a phone/laptop opens the sender page, media flows
browser-to-browser — direct on the LAN, relayed via our coturn on hostile networks.

**The plan, architecture, protocol contract, and deployment checklist live in
[`docs/plans/beam-webrtc-beamer.md`](../../docs/plans/beam-webrtc-beamer.md).** Read that
first; this directory is the image build context only.

## Status

v0 scaffold. Signaling server + room state machine + TURN credential minting are real and
tested; front-end pages are working skeletons with `TODO(v0)`/`TODO(v1)` markers aligned to
the plan's phase ladder.

## Layout

```
src/beam/
  main.py       FastAPI app: pages, /api/rooms, /api/turn-credentials, /ws/{code}
  rooms.py      room registry + membership/approval state machine (pure logic, tested)
  protocol.py   wire-protocol models — keep in lockstep with plan §3 table
  turncreds.py  ephemeral coturn credentials (use-auth-secret HMAC)
  config.py     BEAM_* env settings
  static/       landing / receiver (/screen) / sender (/s) pages + webrtc.js
tests/          pytest suite for rooms + turncreds
```

## Run locally

```bash
uv run --extra dev pytest                 # tests
uv run uvicorn beam.main:app --reload --port 8080
# open http://localhost:8080/screen (receiver) and /s (sender) in two windows
```

Without a `BEAM_TURN_SECRET`, TURN is disabled and connections are LAN/host-candidates only
— fine for local dev.

## Configuration (env)

| var | default | meaning |
|---|---|---|
| `BEAM_PUBLIC_ORIGIN` | `http://localhost:8080` | canonical origin, used for sender links/QR |
| `BEAM_TURN_SECRET` | *(empty = TURN off)* | shared with coturn `--static-auth-secret` |
| `BEAM_TURN_URIS` | *(empty)* | comma-separated `stun:`/`turn:` URIs handed to clients |
| `BEAM_TURN_CRED_TTL_SECONDS` | `7200` | minted credential lifetime |
| `BEAM_ROOM_TTL_SECONDS` | `900` | idle room reap |
| `BEAM_MAX_SENDERS_PER_ROOM` | `4` | capacity guard |
