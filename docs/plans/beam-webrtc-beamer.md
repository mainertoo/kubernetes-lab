# beam — self-hosted WebRTC screen beamer

> **STATUS (2026-07-15): v0 + v1 field-verified. v2 STREAM CASTING (the sports
> feature — cast an IPTV URL, receiver plays it directly via an SSRF-guarded HLS/TS
> proxy) built on `feat/beam-v2-stream-casting`; Codex review round 3 pending before
> merge. Next after v2: v3 iPhone screen mirroring (ReplayKit — needs an Apple
> Developer account, see §7).**
> Adversarial reviews: round 1 (Codex) + round 2 (v0 acceptance) + round 3 (v2 proxy) in §11.
> **Resume here:** read this doc top to bottom, then continue at §6 "v0 deployment
> checklist" — unchecked boxes are the frontier. Code scaffold: [`docker/beam/`](../../docker/beam/).

## 1. Context and decision record

Goal: walk into any location (work, a friend's house), get my phone/laptop content onto a
TV-attached computer there, with **nothing pre-installed on that computer** — just a browser
opening `beam.mainertoo.com`.

**Declared mission (2026-07-15): live sports.** The end-game is beaming games — the IPTV
streams that apps like IBO Player Pro play — onto whatever screen is available. That makes
v2 stream casting the flagship milestone (play the stream directly on the receiver; never
re-encode through a phone), with app mirroring (v3 ReplayKit) as the fallback for
sources that can't be cast as URLs.

Decisions already litigated (do not re-open without new facts):

- **A web page cannot be an AirPlay receiver.** Browsers cannot advertise mDNS/Bonjour,
  cannot open listening TCP/UDP sockets, cannot do the FairPlay handshake. Hard platform
  boundary.
- **mDNS does not cross the tailnet.** Tailscale is point-to-point L3 WireGuard; multicast is
  never forwarded (tailscale/tailscale#1013, open since 2020). Wide-Area Bonjour (unicast
  DNS-SD) is a possible side quest for *AirPlay-to-home-from-anywhere* — see Appendix B —
  but does not help at venues whose machines are not on the tailnet.
- **Therefore: WebRTC.** The TV computer opens a receiver page (no install, no admin, no
  trace); the phone/laptop opens a sender page; media flows browser-to-browser. Direct on
  the venue LAN; relayed via our TURN server when the venue network blocks peer-to-peer
  (client isolation at offices) — both ends only ever dial outbound, which is exactly the
  hole corporate networks leave open. This beats AirPlay at hostile venues, where mDNS is
  blocked and AirPlay has no relay concept at all.
- **Sender capabilities are asymmetric, by platform, and that's accepted:**
  - Laptop (Chrome ≥141 on macOS ≥14.2, any recent Chrome on Windows): full screen/window/tab
    share **with system audio** via `getDisplayMedia`.
  - iPhone Safari: **no `getDisplayMedia`** (still true as of iOS 26). Phone contributes
    camera (`getUserMedia`), photo casting, video-file casting, URL casting. True phone
    screen mirroring = v3 native ReplayKit broadcast extension (how Jitsi does it).
- **DRM content blacks out** when captured (Netflix etc.) — identical to real AirPlay
  mirroring; not a regression. Latency class ~150–400 ms — fine for slides/photos/video,
  not gaming.
- A separate, unrelated track exists for a *real* AirPlay receiver at home (UxPlay) —
  condensed in Appendix A so this doc is the single resumable artifact.

## 2. Architecture

```
                 signaling (JSON over wss, tiny)
        ┌──────────────────────────────────────────────┐
        │   beam app — FastAPI + WebSockets             │
        │   K3s, ns `beam`, replicas: 1, in-memory      │
        │   beam.mainertoo.com (CF tunnel → Traefik)    │
        └───────▲──────────────────────────▲───────────┘
            wss │                          │ wss
   ┌────────────┴────────┐      ┌──────────┴─────────────┐
   │ SENDER page          │      │ RECEIVER page          │
   │ my phone / laptop    │═════▶│ TV-attached computer   │
   │ getDisplayMedia /    │media │ fullscreen <video>     │
   │ getUserMedia         │      │ (browser only)         │
   └─────────────────────┘      └────────────────────────┘
     media path (DTLS-SRTP, E2E encrypted), ICE picks first that works:
       1. host candidates  — direct over venue LAN (usual case, no infra touched)
       2. srflx (STUN)     — hairpin via venue router
       3. relay (TURN)     — coturn on RackNerd VPS, turn.mainertoo.com
```

| Component | Where it runs | Source of truth | Exposure |
|---|---|---|---|
| beam app (signaling + pages) | K3s, namespace `beam`, bjw-s app-template, `replicas: 1` | this repo: `docker/beam/` (image), `apps/base/beam/` (manifests, created at v0) | `beam.mainertoo.com` via CF tunnel public hostname → Traefik IngressRoute (pocket-bridge pattern) |
| coturn (STUN/TURN) | RackNerd VPS, Docker, `network_mode: host` | `mainertoo/home_server` repo, Portainer git stack (config staged in §4 here until copied) | `turn.mainertoo.com` — **grey-cloud/DNS-only A record** to the VPS IP (CF proxy would break raw UDP/TCP TURN) |
| browsers | wherever | — | — |

Design invariants:

- **The server never carries media.** Signaling is ~a dozen small JSON frames per session.
  Media is browser↔browser, DTLS-SRTP encrypted end-to-end; even the TURN relay only
  forwards ciphertext.
- **In-memory state, one replica, by design.** A beam restart drops active rooms; senders
  and receivers re-join in seconds. HA is explicitly out of scope (see §10).
- **No frameworks on the front end, no SFU, no database.** Three static pages + one
  WebSocket endpoint. Boring on purpose.

## 3. Signaling protocol contract

Transport: `wss://beam.mainertoo.com/ws/{CODE}`. First client frame MUST be `hello` within
10 s or the socket is closed. All frames are JSON objects with a `type` field. Pydantic
models in [`docker/beam/src/beam/protocol.py`](../../docker/beam/src/beam/protocol.py) are
generated from this table — **this table is the contract; change both together.**

| type | direction | fields | semantics |
|---|---|---|---|
| `hello` | client → server | `role` (`receiver`\|`sender`), `name`, `receiver_token?` | join. Receiver must present the `receiver_token` returned by `POST /api/rooms` (prevents receiver-slot races). One receiver per room; senders join as `pending`. |
| `room-state` | server → client | `code`, `you` `{id, role, state}`, `peers` `[{id, role, name, state}]` | full snapshot, re-broadcast to all members on every membership/approval change. No deltas. |
| `approve` | receiver → server | `peer_id`, `allow` | receiver's Allow/Deny tap for a pending sender. Deny closes that sender's socket with `error`. |
| `turn` | server → client | `username`, `credential`, `ttl`, `uris` | ephemeral ICE/TURN config (§4), **pushed**: to the receiver right after its first `room-state`, to a sender immediately after approval (before the approved `room-state`). Only authenticated room members can ever hold relay credentials; there is no REST endpoint to mint them (review round 1). |
| `signal` | both, relayed | `to?`, `payload` (opaque SDP offer/answer/ICE) | relayed **only** between the receiver and an **approved** sender. Sender omits `to` (implicit: receiver); receiver must set `to`. Server stamps `from`. Pending senders get `error`. |
| `cast-stream` | sender → server | `url` (≤2048) | v2 stream casting. Approved-sender-only; server validates the URL (scheme + public-IP SSRF guard, `streams.py`), mints an opaque room-scoped token, and the raw URL stays server-side. Pending/receiver → `error`. |
| `stream` | server → receiver | `token`, `kind` (`hls`\|`mpegts`\|`auto`) | tells the receiver to play `/stream/{token}` (proxied). Sent only to the room's receiver after a successful `cast-stream`. |
| `bye` | client → server | — | graceful leave. Receiver leaving closes the whole room (all peers notified via `error room-closed`). |
| `ping` / `pong` | server → client / client → server | — | app-level keepalive every 25 s (Cloudflare idles quiet websockets at ~100 s). Two missed pongs → server closes the socket. |
| `error` | server → client | `code`, `message` | machine-readable `code`: `room-not-found`, `room-full`, `not-approved`, `denied`, `room-closed`, `bad-message`. |

REST surface:

- `POST /api/rooms` → `{code, receiver_token}`. Called by the receiver page. Rate-limited
  per IP (v0 checklist); global cap `BEAM_MAX_ROOMS=500`.
- `GET /healthz` → 200. Probes + Gatus.

There is deliberately **no** TURN-credentials REST endpoint — credentials ride the WS as
`turn` frames, so only the receiver and approved senders can obtain relay access.

Room lifecycle: codes are 5 chars from the unambiguous alphabet `ABCDEFGHJKMNPQRSTUVWXYZ23456789`
(no 0/O/1/I/L; ≈28.6 M combinations, and join attempts are rate-limited + approval-gated, so
guessing buys a stranger an Allow/Deny prompt on my TV at worst). Rooms die when the receiver
disconnects, or after `BEAM_ROOM_TTL_SECONDS` (default 900 s) idle.

Transport guards (implemented): WS handshakes with a browser `Origin` outside the allow-list
(`BEAM_PUBLIC_ORIGIN` + localhost dev origins) are rejected before accept — same-origin
policy does not protect WebSockets; frames over `BEAM_MAX_FRAME_BYTES` (64 KiB) close the
socket; `BEAM_MAX_SENDERS_PER_ROOM=1` for v0 (a second approved sender would be silently
stranded by the receiver UI — raise it when v2 multi-sender lands).

WebRTC negotiation: **perfect negotiation** (MDN pattern); the **sender is the polite peer**.
Client logic in [`docker/beam/src/beam/static/webrtc.js`](../../docker/beam/src/beam/static/webrtc.js).
Path indicator: a 2 s `getStats()` loop reads the nominated candidate-pair type and shows
**direct** (host/srflx) vs **relayed** (relay) in both UIs — this is the first thing to look
at when a venue misbehaves.

## 4. Connectivity: TURN design

coturn with `use-auth-secret`: beam mints time-limited credentials per approved peer —
`username = "<unix-expiry>:beam-<room>-<peerid>"` (per-peer label so coturn's `user-quota`
isolates peers instead of everything minted in the same second),
`credential = base64(HMAC-SHA1(TURN_SECRET, username))` — implemented in
[`turncreds.py`](../../docker/beam/src/beam/turncreds.py), TTL 2 h, delivered only as WS
`turn` frames (§3). The same
`TURN_SECRET` lives in exactly two places: the beam k8s Secret (SOPS,
`apps/base/beam/beam-secret.sops.yaml`) and the coturn stack env on the VPS. Rotating it is
a two-place update; rooms survive (creds are minted per session).

**Deployed 2026-07-06**: `home_server:docker-vps/coturn/docker-compose.yml` → Portainer git
stack id 102 on endpoint 11 (docker-vps), auto-update 5m, `TURN_SECRET` as stack env. The
block below mirrors it for reference (source of truth is `home_server`):

```yaml
# home_server: stacks/coturn/docker-compose.yml   (staged here until copied)
services:
  coturn:
    image: coturn/coturn:4.7.0
    restart: unless-stopped
    network_mode: host          # 3478 + UDP relay range; simplest and honest
    command:
      - -n                      # no config file; flags only
      - --log-file=stdout
      - --listening-port=3478
      - --fingerprint
      - --use-auth-secret
      - --static-auth-secret=${TURN_SECRET}
      - --realm=turn.mainertoo.com
      - --min-port=49160
      - --max-port=49200
      - --no-cli
      - --no-multicast-peers
      # never relay into private/VPS-internal ranges (abuse containment):
      - --denied-peer-ip=0.0.0.0-0.255.255.255
      - --denied-peer-ip=10.0.0.0-10.255.255.255
      - --denied-peer-ip=100.64.0.0-100.127.255.255
      - --denied-peer-ip=127.0.0.0-127.255.255.255
      - --denied-peer-ip=169.254.0.0-169.254.255.255
      - --denied-peer-ip=172.16.0.0-172.31.255.255
      - --denied-peer-ip=192.168.0.0-192.168.255.255
      # IPv6 equivalents (review round 1) — or run the listeners IPv4-only:
      - --denied-peer-ip=::1
      - --denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
      - --denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff
      - --user-quota=8          # concurrent allocations per username (= per peer, see §4)
      - --total-quota=32
      - --max-bps=6250000       # ~50 Mbit/s cap per allocation
```

VPS firewall additions: `3478/tcp`, `3478/udp`, `49160–49200/udp` in. beam's client-side ICE
config (`BEAM_TURN_URIS`):
`stun:turn.mainertoo.com:3478,turn:turn.mainertoo.com:3478?transport=udp,turn:turn.mainertoo.com:3478?transport=tcp`.

**TURNS on tcp/443 — LIVE 2026-07-13** (the §10 trigger fired at a real venue allowing only
HTTPS egress): Traefik on the VPS terminates TLS by SNI (`turn.mainertoo.com`, LE cert) on
its existing 443 entrypoint and forwards plain TURN-TCP to coturn:3478. Config file
`turns-tcp.yml` hot-loaded from the Pangolin Traefik config dir (live copy on `/flash`,
documentation copy in `home_server:docker-vps/pangolin/traefik/`). Client ICE config gained
`turns:turn.mainertoo.com:443?transport=tcp`, tried last by ICE (TCP media: fine for
slides/playback, softer for high motion). Verified with a STUN-over-TLS exchange from the
blocking venue itself. Note: coturn sees the Traefik container IP for 443-path clients
(raw TCP, no PROXY protocol) — per-peer usernames keep quotas correct anyway. Clients also
now surface a "relay unreachable from this network" diagnostic when TURN servers are
configured but gathering yields no relay candidate.

## 5. Security model

| Threat | Control |
|---|---|
| Room-code guessing / drive-by senders | 5-char code space + **receiver approval tap before any SDP is relayed** + room TTL + WS Origin allow-list (implemented); per-IP rate limits on `POST /api/rooms` and WS joins remain the v0 gate (need trusted X-Forwarded-For); deny→rejoin prompt-spam cooldown lands v1 (review round 1) |
| TURN bandwidth theft (open-relay abuse) | no REST minting — ephemeral HMAC creds (2 h TTL) are **pushed over the WS only to the receiver and approved senders**, per-peer usernames so quotas isolate; `denied-peer-ip` blocks RFC1918/CGNAT/loopback + IPv6 local ranges; per-user and total quotas; 50 Mbit/s per-allocation cap |
| Signaling DoS | implemented: `max_rooms=500`, 64 KiB frame cap, `max_senders_per_room=1`, 10 s hello deadline, two-missed-pong reaping, TTL sweep; per-IP rate limits remain the v0 checklist item |
| XSS via peer/room names | names length-capped, rendered via `textContent` only; CSP `default-src 'self'` **shipped** (all page JS is external `/static` files — no inline scripts, no CDNs; vendor future libs) |
| Media interception | DTLS-SRTP end-to-end between browsers; TURN relays ciphertext; signaling only over wss |
| Admin surface abuse | v0 ships **no** admin routes. v1 `/admin` goes behind the existing `authentik-sso` forwardAuth Middleware (`traefik-system`) as a **separate IngressRoute route block** — public room paths must NOT sit behind forward-auth (the Immich lesson: forward-auth in front of API/WS surfaces breaks native clients) |
| Secrets hygiene (public repo) | only `TURN_SECRET` exists; SOPS-encrypted in-cluster + Portainer env on VPS. Nothing else secret by design. This plan file contains none. |

## 6. v0 deployment checklist (the frontier)

Build/test (local):
- [x] `cd docker/beam && uv run --extra dev pytest` green (31 tests incl. transport suite)
- [ ] `docker build docker/beam` succeeds; `/healthz` 200 in container
- [ ] pin base image by digest + install from `uv.lock` in the Dockerfile (review round 1)

Publish:
- [x] merge scaffold PR (#1072, 2026-07-06) — `ghcr.io/mainertoo/beam:latest` published and
      verified public/anonymous-pullable (no imagePullSecrets needed)

TURN (VPS):
- [x] `TURN_SECRET` generated → SOPS-encrypted at `apps/base/beam/beam-secret.sops.yaml` +
      set as Portainer stack env; compose at `home_server:docker-vps/coturn/` (stack 102,
      endpoint 11, auto-update 5m); ufw opened 3478/tcp+udp + 49160:49200/udp — mandatory,
      host-network stacks don't get Docker's usual ufw bypass
- [x] DNS: `turn.mainertoo.com` already resolves to the VPS IP **unproxied** (existing
      record/wildcard) — verified raw IP, not CF edge
- [x] relay allocation verified 2026-07-06: ephemeral HMAC creds (minted with beam's exact
      algorithm) + `turnutils_uclient -y` from a k3s pod → 20/20 msgs relayed, 0 % loss,
      ~65 ms RTT. Benign quirk: coturn image logs one empty "Unknown argument" at start;
      argv verified correct via `docker inspect`

Cluster app:
- [x] `/new-app beam` scaffolded 2026-07-06 (branch `feat/beam-app`): bjw-s HelmRelease
      (image pinned to `sha-…` tag, real `/healthz` probes, `envFrom: beam-secret`,
      `BEAM_PUBLIC_ORIGIN` + `BEAM_TURN_URIS` plain env), IngressRoute on the **public**
      host `beam.mainertoo.com` (no forward-auth — rooms are public by design; a `.lab`
      host would be rejected by beam's WS Origin allow-list), namespace, two
      kustomization.yaml files, Gatus internal+external via components with
      `GATUS_PATH=/healthz`. Single-service bjw-s release ⇒ Service named bare `beam`
      (vaultwarden precedent)
- [x] `apps/base/beam/beam-secret.sops.yaml` created (SOPS-encrypted; inert until the app
      kustomization references it). `BEAM_TURN_URIS`/`BEAM_PUBLIC_ORIGIN` are non-secret and
      land as plain HelmRelease env at new-app time
- [~] Gatus internal+external wired and both green (`GATUS_PATH=/healthz`). Finding: the
      external component's `[STATUS] < 500` condition treats hard outages (STATUS 0) as
      healthy — beam's wrong-connector window stayed "green" through a TLS failure.
      Fixed by adding `[CONNECTED] == true` to `components/gatus/external`. Still open:
      one deliberate break to watch the Discord alert actually arrive
- [x] Pangolin resource `beam.mainertoo.com` → `beam.beam.svc.cluster.local:8080`, auth
      disabled — live 2026-07-07 (gotcha: the resource must be attached to the
      **in-cluster newt** connector/site; any other site can't resolve `svc.cluster.local`
      and returns bad gateway). Verified from outside: `/healthz` 200 over valid TLS, CSP
      present, **wss signaling through Pangolin works** (room created, `room-state` +
      `turn` frames received with the real relay URIs + minted creds)
- [ ] PR → flux-local CI green → merge → `flux reconcile` → page loads over TLS

Acceptance (definition of v0-done):
- [ ] security gates (review round 1): cross-origin WS rejected in prod (test with a forged
      `Origin`), CSP present on all pages, TURN creds unobtainable without an approved WS
      session, per-IP rate limit on `POST /api/rooms` active and exercised
- [ ] idle WS through the CF tunnel survives > 5 min (25 s app pings vs ~100 s CF idle)
- [ ] LAN test: two machines same network → connect, path indicator **direct**, screen+audio
      visible, glass-to-glass latency subjectively < ~400 ms
- [x] Forced-relay test — passed 2026-07-13 via the e2e harness, in the strongest possible
      form: run **from a real 443-only venue** (udp/tcp 3478 blocked), so the only viable
      path was TURNS-over-443 → frames flowing, sender indicator `sharing (relayed)`
- [x] Hostile-venue rehearsal — same run subsumes it: a genuinely hostile venue (the one
      behind the 2026-07-13 silent failure) now connects relayed through TLS-443; the
      identical venue+harness combination failed pre-TURNS, isolating the fix as the cause
- [ ] beam pod restart mid-session → both pages surface a clear "room closed / rejoin" state
      (no zombie UIs)

## 7. Phase ladder

| Phase | Scope | Acceptance |
|---|---|---|
| **v0** | rooms + QR-less join by code; laptop screen+system-audio → TV; TURN fallback; path indicator; fullscreen receiver | §6 checklist all green |
| **v1** | ~~phone camera + photo casting; video-file casting~~ **shipped 2026-07-15** (camera w/ flip, photo slideshow w/ prev/next, video-file — all over a `beam-files` DataChannel, client↔client, server untouched; receiver wake-lock too). Note: video is **file-transfer + native playback**, not stream mode — iOS Safari has no `captureStream()` on media elements, and native playback is higher quality anyway. Still open in v1: QR on receiver (vendored lib), per-IP rate limiting, reconnect/ICE-restart. ~~cache-busting~~ done 2026-07-15 (`Cache-Control: no-cache` everywhere — ETag revalidation, 304s when unchanged) | photo night + present-at-work both work end-to-end |
| **v2 — STREAM CASTING (the sports milestone)** | **Core built 2026-07-15** (`feat/beam-v2-stream-casting`): sender "Cast stream" mode → `cast-stream` URL over the WS; server mints a room-scoped opaque token (raw URL + creds stay server-side, never logged); receiver plays `/stream/{token}` via vendored **hls.js** / **mpegts.js** (lazy-loaded, CSP stays CDN-free; auto-fallback HLS→TS). **Reverse proxy** (`proxy.py`) bridges CORS + mixed-content: fetches upstream, rewrites m3u8 children back through itself, streams TS. **SSRF guard** (`streams.py`): every fetched URL — cast root, m3u8 children, redirect hops — must resolve to a **public** IP (blocks RFC1918/loopback/link-local/CGNAT-tailnet/v6-local/reserved); scheme http(s) only. Knowingly bends "server never carries media" — ~5–8 Mbit/s/game through the pod, ~10 GB/game through the VPS when the receiver is remote (watch RackNerd allowance; CF-proxied hostname is the relief valve). Impl notes: the URL rides the **WS not the DataChannel** (server must see it to proxy — corrected from the original design); one live cast per room supersedes prior tokens. **Deferred**: Dispatcharr M3U channel picker (stretch), multi-receiver (≤3), `/admin` behind `authentik-sso`, stats overlay. Pending: Codex review round 3 (§11) before merge | a live game plays full-quality on a remote TV with the phone as remote; movie file plays at native quality |
| **v3 (committed 2026-07-15, next after v1 lands)** | iOS ReplayKit broadcast-upload extension feeding the same rooms → true iPhone screen mirroring | phone OS screen visible on a venue TV |

v3 shape and prerequisites (decided when the user committed to it): a small SwiftUI host app
+ a Broadcast Upload Extension (ReplayKit) that joins a beam room as a sender — speaking the
§3 signaling protocol over `wss` and publishing H.264 via an embedded WebRTC stack (either
libwebrtc via SPM binary, or a lighter RTC stack). Beam-side work is minimal (the protocol
already treats it as just another sender); the effort is almost entirely Swift.
**Prerequisites: an Apple Developer Program membership ($99/yr) for the App Group +
broadcast-extension entitlements and on-device install; Xcode on the Mac (present).**
Extension memory limit is 50 MB — encode settings must be conservative. Prove-out order:
join/signal from Swift first, camera track second, ReplayKit frames last.

## 8. Testing strategy

- **Unit (exists in scaffold):** room state machine (join/approve/deny/capacity/routing/TTL)
  and TURN credential minting (format, expiry, HMAC cross-check) — `docker/beam/tests/`.
- **E2E (exists: [`docker/beam/e2e/e2e_beam.py`](../../docker/beam/e2e/e2e_beam.py)):**
  Playwright, two headless contexts against any base URL (local or prod);
  `uv run --with playwright python e2e_beam.py [base] [--relay]`. Asserts join → approval →
  share → receiver `<video>` frames actually advancing + the sender's path indicator.
  Note: Chrome Headless Shell cannot display-capture, so the harness shims
  `getDisplayMedia → getUserMedia` (fake camera) — identical WebRTC pipeline. Paid for
  itself on first run: caught the `[hidden]`-vs-`display:inline-block` CSS bug that
  blocked the v0 acceptance test.
- **TURN verification:** forced-relay page param (above) + `turnutils_uclient` from any host.
- **Manual venue matrix (living):** home LAN / phone-hotspot / office — record path type and
  subjective latency in this doc per venue class.

## 9. Ops notes and numbers

- 1080p screen share ≈ 2.5–6 Mbit/s (screen-content encoding: slides cheap, motion hungry).
  Direct paths cost zero infra bandwidth. Relayed: VPS carries it twice (in+out) →
  a 2 h relayed movie night ≈ 5–8 GB against the RackNerd allowance — fine occasionally,
  and `max-bps`/quotas cap abuse.
- beam pod: `10m/64Mi` request-class workload; it shuffles JSON.
- Observability: Gatus external check on `/healthz`; coturn logs to stdout (Portainer);
  v2 `/admin` shows live rooms. No metrics endpoint until something hurts.
- Renovate picks up `coturn/coturn` (pinned tag) once the compose lands in `home_server`,
  and `ghcr.io/mainertoo/beam` tags in the HelmRelease once `apps/base/beam` exists.

## 10. Deferred decisions (with triggers)

| Decision | Deferred until | Leaning |
|---|---|---|
| ~~TURNS on tcp/443~~ **RESOLVED 2026-07-13** | trigger fired: real venue with 443-only egress (22/3478/8443 all blocked) | shipped as Traefik `HostSNI` TCP router on the existing 443 — no 2nd IP; SNI muxing turned out clean, not fragile (§4) |
| ~~file-mode video transport~~ **RESOLVED 2026-07-15** | pulled forward into v1 | DataChannel chunks (64 KiB, `bufferedAmount` backpressure, 8 MiB high-water) — beam stays media-free; `captureStream()` stream-mode rejected (absent on iOS Safari media elements) |
| SFU (>3 receivers) | someone actually asks for it | LiveKit if ever; not before |
| HA / multi-replica signaling | never, probably | rooms are 30-second re-creatable |
| iOS broadcast extension | v3 | prove demand with v1 phone modes first |
| Dockerfile digest-pin + `uv.lock`-frozen install | before first public deploy (§6) | uv multi-stage build; Renovate manages the digest |

## 11. Adversarial review

**Brief for the reviewer (Codex or any fresh agent):** attack this plan and scaffold, ranked
findings, most severe first. Hunt specifically:

1. **Security:** room hijack paths (join/approve race, receiver_token gaps, WS origin
   checks?), TURN credential/quota abuse, relay-into-private-network bypasses, XSS/CSP gaps,
   signaling DoS, anything that lets a stranger's pixels reach my TV or my bandwidth serve
   strangers.
2. **WebRTC correctness:** perfect-negotiation glare bugs, ICE restart on network change,
   Safari sender quirks, autoplay-policy traps on the receiver, `getStats` path detection
   fragility, TURN URI/transport set completeness.
3. **Operational:** CF tunnel WS idle/timeouts vs the 25 s ping, coturn flag mistakes
   (host networking, quotas, denied ranges), VPS provider firewall/port-range reality,
   Renovate/CI blind spots, flux-local traps in the future `apps/base/beam` wiring.
4. **Spec gaps:** states the protocol table doesn't cover (double receiver, sender rejoin
   after deny, approval after disconnect), test blind spots, anything §6 acceptance misses.

Findings → append to the log below (date, finding, disposition: fixed / accepted-risk /
deferred-with-trigger). Then update the STATUS block.

### Review log

#### Round 1 — 2026-07-06 · Codex adversarial review of fd22d4a8
*(codex-companion session `019f3895-6f70-75a3-8c98-486858e3927a` — full transcript via `codex resume`)*

Verbatim executive summary:

> - Public room, WebSocket, and TURN credential endpoints have no rate/origin/participant
>   controls, enabling room spam, WS abuse, and TURN bandwidth theft.
> - TURN credentials are minted for any live room code and all clients share the same
>   `expiry:beam` username label, weakening coturn quota isolation.
> - Client WebRTC path detection can mislabel relayed paths as direct and has no ICE
>   restart path.
> - The scaffold is not PR-tested by the dedicated image workflow; production Flux CI does
>   not cover `docker/beam/**`.
> - v0 acceptance criteria omit several security gates promised elsewhere in the plan.

Dispositions (fixes verified by the 31-test suite + live WS smoke with TURN enabled):

| # | Finding (severity) | Disposition |
|---|---|---|
| 1 | Unauthenticated TURN minting via `GET /api/turn-credentials`; shared `expiry:beam` username (High) | **Fixed structurally** — REST endpoint deleted; creds pushed as WS `turn` frames to receiver-on-join / sender-on-approval only; per-peer usernames `expiry:beam-<room>-<peer>` |
| 2 | No WS Origin validation → cross-site WS joins from hostile pages (High) | **Fixed** — Origin allow-list enforced before `accept()` (4403); origin-less native clients still pass |
| 3 | Unbounded rooms/connections/frames (High) | **Partially fixed** — `max_rooms=500`, 64 KiB frame cap, single-sender rooms; **per-IP rate limits are a blocking v0 acceptance gate** (need trusted X-Forwarded-For) |
| 4 | coturn deny list IPv4-only (Medium) | **Fixed in plan** — IPv6 loopback/ULA/link-local denies added to staged compose |
| 5 | CSP promised but absent (Low) | **Fixed** — CSP + nosniff + no-referrer shipped; inline page scripts extracted to `/static/*.js` so `script-src 'self'` holds |
| 6 | No ICE restart/recovery path (High) | **Deferred to v1 deliberately** — `pc.restartIce()` + renegotiation over the still-open WS; TODO marked at the failure hook in webrtc.js |
| 7 | Path indicator checks local candidate only → relay mislabeled as direct (Medium) | **Fixed** — relayed if either local or remote candidate is `relay` |
| 8 | Receiver UI strands a second approved sender (Medium) | **Fixed structurally** — `max_senders_per_room=1` for v0; raise with the v2 multi-sender UI |
| 9 | Receiver autoplay can fail into a silent black TV (Medium) | **Fixed** — `video.play()` promise handled; tap-to-play overlay fallback |
| 10 | iPhone sender offered a share button that can only throw (Low) | **Fixed** — `getDisplayMedia` feature-detect with explanatory status |
| 11 | `build-beam.yml` lacks PR trigger (Medium) | **Fixed** — `pull_request` on `docker/beam/**`; push skipped on PRs |
| 12 | Unpinned base image / lockfile-bypassing install (Medium) | **Deferred with trigger** — §6 checklist item before first public deploy (uv multi-stage + digest pin) |
| 13 | Deny → instant rejoin prompt spam (Medium) | **Accepted for v0** — approval gate + capacity bound the blast radius; cooldown lands with v1 rate limiting |
| 14 | Only pure room logic unit-tested (Medium) | **Fixed** — `tests/test_transport.py` over real test websockets: origin gate, approval flow, `turn` delivery order, deny close, caps, room closure |
| 15 | §6 acceptance omitted the promised security gates; CF idle + VPS port range unverifiable from repo (Medium/Low) | **Fixed in plan** — security-gate + CF-idle acceptance items added; live-verification items retained |

Process note: the review agent checked out `master` mid-session and the working tree had to
be restored from the branch — future in-repo review runs get an isolated worktree.

#### Round 2 — 2026-07-09 · v0 acceptance debugging (self, via the e2e harness)

User's first live test: "tap to play stuck mid-screen" + black receiver. Findings, all fixed
on `fix/beam-v0-ux`:

| Finding | Fix |
|---|---|
| `button { display: inline-block }` in beam.css overrides the UA's `[hidden] → display:none` — every "hidden" button (tap-to-play, sender Share) was visible since page load and **intercepted clicks** (the harness caught Allow being unclickable) | `[hidden] { display: none !important; }` reset |
| Receiver switched to the video view on `ontrack`, which fires at SDP time — before any media flows — hiding all status/diagnostics behind a black video | view switch on `loadedmetadata` (real frames); persistent HUD overlay (state · path · hints) |
| Connected-but-black (sender capturing black: macOS Screen Recording permission, occluded window) was indistinguishable from failure | 8 s no-frames watchdog names the likely cause on screen |
| Canceled share picker left a half-configured RTCPeerConnection | capture before peer creation; cancel is clean |
| tap-to-play hid itself even when `play()` failed again | hides only on successful play |

---

## Appendix A — home AirPlay receiver track (separate, unstarted)

Real AirPlay at home via **UxPlay** (FDH2/UxPlay, v1.73.x, iOS 26-compatible): box with a
screen + Intel iGPU (VAAPI), build from source with GStreamer, systemd service
(`-n <name> -nh -p 7000` → single ZBF rule TCP+UDP 7000–7002 from client VLANs), UniFi mDNS
reflection between VLANs, optional web control panel (FastAPI, systemd-mediated
start/stop/PIN). Blocked on: choosing the box attached to a screen (N150-class mini PC
preferred; SR-IOV iGPU VFs cannot drive physical outputs, so VM103 does not qualify).
Optional add-on: shairport-sync for AirPlay-2 audio.

## Appendix B — Wide-Area Bonjour side quest (unstarted, uncertain)

Unicast DNS-SD to make the *home* receiver appear in the AirPlay picker from anywhere the
tailnet reaches: publish `b._dns-sd._udp` browse-domain + `_airplay._tcp`/`_raop._tcp`
PTR/SRV/TXT records in a zone served to tailnet clients (MagicDNS split-DNS), A record →
receiver's tailnet IP. Reliable for AirPrint historically; **finicky and version-sensitive
for AirPlay — treat as an afternoon experiment, nothing depends on it.** Audio over WAN is
buffered and fine; mirroring over DERP paths will be rough.
