# beam — self-hosted WebRTC screen beamer

> **STATUS (2026-07-06): scaffold committed on `feat/beam-scaffold`, not deployed.**
> Adversarial review: see §11 review log.
> **Resume here:** read this doc top to bottom, then continue at §6 "v0 deployment
> checklist" — unchecked boxes are the frontier. Code scaffold: [`docker/beam/`](../../docker/beam/).

## 1. Context and decision record

Goal: walk into any location (work, a friend's house), get my phone/laptop content onto a
TV-attached computer there, with **nothing pre-installed on that computer** — just a browser
opening `beam.mainertoo.com`.

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
| `signal` | both, relayed | `to?`, `payload` (opaque SDP offer/answer/ICE) | relayed **only** between the receiver and an **approved** sender. Sender omits `to` (implicit: receiver); receiver must set `to`. Server stamps `from`. Pending senders get `error`. |
| `bye` | client → server | — | graceful leave. Receiver leaving closes the whole room (all peers notified via `error room-closed`). |
| `ping` / `pong` | server → client / client → server | — | app-level keepalive every 25 s (Cloudflare idles quiet websockets at ~100 s). Two missed pongs → server closes the socket. |
| `error` | server → client | `code`, `message` | machine-readable `code`: `room-not-found`, `room-full`, `not-approved`, `denied`, `room-closed`, `bad-message`. |

REST surface:

- `POST /api/rooms` → `{code, receiver_token}`. Called by the receiver page. Rate-limited
  per IP (v0 checklist).
- `GET /api/turn-credentials?code={CODE}` → `{username, credential, ttl, uris}`. Only for
  live rooms. Ephemeral HMAC credentials (§4); never a static secret in JS.
- `GET /healthz` → 200. Probes + Gatus.

Room lifecycle: codes are 5 chars from the unambiguous alphabet `ABCDEFGHJKMNPQRSTUVWXYZ23456789`
(no 0/O/1/I/L; ≈28.6 M combinations, and join attempts are rate-limited + approval-gated, so
guessing buys a stranger an Allow/Deny prompt on my TV at worst). Rooms die when the receiver
disconnects, or after `BEAM_ROOM_TTL_SECONDS` (default 900 s) idle.

WebRTC negotiation: **perfect negotiation** (MDN pattern); the **sender is the polite peer**.
Client logic in [`docker/beam/src/beam/static/webrtc.js`](../../docker/beam/src/beam/static/webrtc.js).
Path indicator: a 2 s `getStats()` loop reads the nominated candidate-pair type and shows
**direct** (host/srflx) vs **relayed** (relay) in both UIs — this is the first thing to look
at when a venue misbehaves.

## 4. Connectivity: TURN design

coturn with `use-auth-secret`: beam mints time-limited credentials per request —
`username = "<unix-expiry>:beam"`, `credential = base64(HMAC-SHA1(TURN_SECRET, username))` —
implemented in [`turncreds.py`](../../docker/beam/src/beam/turncreds.py), TTL 2 h. The same
`TURN_SECRET` lives in exactly two places: the beam k8s Secret (SOPS,
`apps/base/beam/beam-secret.sops.yaml`) and the coturn stack env on the VPS. Rotating it is
a two-place update; rooms survive (creds are minted per session).

Staged coturn stack — **destination: `mainertoo/home_server` repo → Portainer git stack on
the VPS** (do not hand-edit the live host; per repo convention). Compose, complete:

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
      - --user-quota=8          # concurrent allocations per username
      - --total-quota=32
      - --max-bps=6250000       # ~50 Mbit/s cap per allocation
```

VPS firewall additions: `3478/tcp`, `3478/udp`, `49160–49200/udp` in. beam's client-side ICE
config (`BEAM_TURN_URIS`):
`stun:turn.mainertoo.com:3478,turn:turn.mainertoo.com:3478?transport=udp,turn:turn.mainertoo.com:3478?transport=tcp`.

Deliberately deferred: **TURNS on tcp/443** (the deep escape for venues that allow only
HTTPS egress). Traefik owns 443 on the VPS, so this needs a second IP or SNI muxing —
build it the first time a real venue forces it, not before (§10).

## 5. Security model

| Threat | Control |
|---|---|
| Room-code guessing / drive-by senders | 5-char code space + per-IP rate limits on `POST /api/rooms` and WS joins + **receiver approval tap required before any SDP is relayed** + room TTL |
| TURN bandwidth theft (open-relay abuse) | no static creds in JS — ephemeral HMAC creds, 2 h TTL, minted only for live rooms; `denied-peer-ip` blocks relaying into RFC1918/CGNAT/loopback; per-user and total quotas; 50 Mbit/s per-allocation cap |
| Signaling DoS | per-IP rate limits, `max_senders_per_room=4`, 10 s hello deadline, two-missed-pong reaping, room TTL sweep |
| XSS via peer/room names | names are length-capped, HTML-escaped at render (`textContent`, never `innerHTML`); CSP `default-src 'self'` (no CDNs — QR lib et al. get vendored) |
| Media interception | DTLS-SRTP end-to-end between browsers; TURN relays ciphertext; signaling only over wss |
| Admin surface abuse | v0 ships **no** admin routes. v1 `/admin` goes behind the existing `authentik-sso` forwardAuth Middleware (`traefik-system`) as a **separate IngressRoute route block** — public room paths must NOT sit behind forward-auth (the Immich lesson: forward-auth in front of API/WS surfaces breaks native clients) |
| Secrets hygiene (public repo) | only `TURN_SECRET` exists; SOPS-encrypted in-cluster + Portainer env on VPS. Nothing else secret by design. This plan file contains none. |

## 6. v0 deployment checklist (the frontier)

Build/test (local):
- [ ] `cd docker/beam && uv run --extra dev pytest` green
- [ ] `docker build docker/beam` succeeds; `/healthz` 200 in container

Publish:
- [ ] merge scaffold PR (workflow `build-beam.yml` pushes `ghcr.io/mainertoo/beam` on
      changes under `docker/beam/**`)

TURN (VPS):
- [ ] generate `TURN_SECRET` (`openssl rand -hex 32`); copy compose from §4 into
      `home_server` repo; deploy Portainer stack with env; open VPS firewall ports
- [ ] DNS: `turn.mainertoo.com` A → VPS IP, **DNS-only/grey cloud**
- [ ] verify relay allocation with ephemeral creds (Trickle-ICE test page or
      `turnutils_uclient -u <minted-user> -w <minted-pass> turn.mainertoo.com`)

Cluster app:
- [ ] `/new-app beam` — image `ghcr.io/mainertoo/beam`, port 8080, no PVC, probes `/healthz`,
      host `beam.mainertoo.com`. Apply the known scaffolding fixes from memory:
      service key must render Service name `beam` to match the IngressRoute; two
      kustomization.yaml files (base + production overlay); IngressRoute matches the
      **public** host with `entryPoints: [websecure, web]`, `tls: {}` (pocket-bridge is the
      exemplar); NO forward-auth on `/`
- [ ] `apps/base/beam/beam-secret.sops.yaml`: `BEAM_TURN_SECRET` (+ `BEAM_TURN_URIS`,
      `BEAM_PUBLIC_ORIGIN` as plain values)
- [ ] Gatus: `components/gatus/external` with `APP=beam`, `GATUS_DOMAIN=mainertoo.com`,
      `GATUS_PATH=/healthz`; confirm the Discord alert actually fires once (break it on purpose)
- [ ] Cloudflare tunnel dashboard: public hostname `beam.mainertoo.com` → Traefik (same
      target as existing public hostnames)
- [ ] PR → flux-local CI green → merge → `flux reconcile` → page loads over TLS

Acceptance (definition of v0-done):
- [ ] LAN test: two machines same network → connect, path indicator **direct**, screen+audio
      visible, glass-to-glass latency subjectively < ~400 ms
- [ ] Forced-relay test: `?relay=1` on both pages (`iceTransportPolicy: "relay"`) → still
      connects, indicator **relayed**, coturn logs show the allocation
- [ ] Hostile-venue rehearsal: phone on LTE hotspot + laptop on home LAN → connects (relayed)
- [ ] beam pod restart mid-session → both pages surface a clear "room closed / rejoin" state
      (no zombie UIs)

## 7. Phase ladder

| Phase | Scope | Acceptance |
|---|---|---|
| **v0** | rooms + QR-less join by code; laptop screen+system-audio → TV; TURN fallback; path indicator; fullscreen receiver | §6 checklist all green |
| **v1** | phone camera + photo casting; video-file casting (stream mode); sender approval UX polish; QR on receiver (vendored lib); screen wake-lock; per-IP rate limiting; reconnect/ICE-restart on network change | photo night + present-at-work both work end-to-end |
| **v2** | file-mode video playback (transfer + native `<video>`, decision in §10); URL casting; multi-receiver (≤3); `/admin` (rooms list, kick) behind `authentik-sso`; stats overlay | movie file plays at native quality; two screens simultaneously |
| **v3 (stretch)** | iOS ReplayKit broadcast-upload extension feeding the same rooms → true iPhone screen mirroring | phone OS screen visible on a venue TV |

## 8. Testing strategy

- **Unit (exists in scaffold):** room state machine (join/approve/deny/capacity/routing/TTL)
  and TURN credential minting (format, expiry, HMAC cross-check) — `docker/beam/tests/`.
- **E2E (v0, CI-able):** Playwright, two headless Chromium contexts + the app;
  `--use-fake-ui-for-media-stream --use-fake-device-for-media-stream
  --auto-select-desktop-capture-source=Entire screen` makes `getDisplayMedia` non-interactive.
  Assert: room join, approval gate blocks SDP before Allow, `connectionState == "connected"`,
  candidate-pair type as expected, receiver `<video>` has flowing frames
  (`getStats` framesReceived increasing).
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
| TURNS on tcp/443 (2nd VPS IP or SNI mux) | first real venue that blocks 3478/relay-range egress | 2nd IP; SNI muxing with Traefik is fragile |
| file-mode video transport: DataChannel chunks vs ranged HTTP upload via beam | v2 | DataChannel (keeps beam media-free; backpressure via `bufferedAmount`) |
| SFU (>3 receivers) | someone actually asks for it | LiveKit if ever; not before |
| HA / multi-replica signaling | never, probably | rooms are 30-second re-creatable |
| iOS broadcast extension | v3 | prove demand with v1 phone modes first |

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

- *(empty — round 1 pending)*

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
