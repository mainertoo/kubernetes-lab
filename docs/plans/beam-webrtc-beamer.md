# beam — self-hosted WebRTC screen beamer

> **STATUS (2026-07-06): merged to master (PR #1072); image published; coturn LIVE on the
> VPS (Portainer stack 102) with relay verified end-to-end. Next: `/new-app beam` cluster wiring.**
> Adversarial review round 1 (Codex) complete — findings and dispositions in §11 review log.
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
| `turn` | server → client | `username`, `credential`, `ttl`, `uris` | ephemeral ICE/TURN config (§4), **pushed**: to the receiver right after its first `room-state`, to a sender immediately after approval (before the approved `room-state`). Only authenticated room members can ever hold relay credentials; there is no REST endpoint to mint them (review round 1). |
| `signal` | both, relayed | `to?`, `payload` (opaque SDP offer/answer/ICE) | relayed **only** between the receiver and an **approved** sender. Sender omits `to` (implicit: receiver); receiver must set `to`. Server stamps `from`. Pending senders get `error`. |
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

Deliberately deferred: **TURNS on tcp/443** (the deep escape for venues that allow only
HTTPS egress). Traefik owns 443 on the VPS, so this needs a second IP or SNI muxing —
build it the first time a real venue forces it, not before (§10).

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
- [ ] `/new-app beam` — image `ghcr.io/mainertoo/beam`, port 8080, no PVC, probes `/healthz`,
      host `beam.mainertoo.com`. Apply the known scaffolding fixes from memory:
      service key must render Service name `beam` to match the IngressRoute; two
      kustomization.yaml files (base + production overlay); IngressRoute matches the
      **public** host with `entryPoints: [websecure, web]`, `tls: {}` (pocket-bridge is the
      exemplar); NO forward-auth on `/`
- [x] `apps/base/beam/beam-secret.sops.yaml` created (SOPS-encrypted; inert until the app
      kustomization references it). `BEAM_TURN_URIS`/`BEAM_PUBLIC_ORIGIN` are non-secret and
      land as plain HelmRelease env at new-app time
- [ ] Gatus: `components/gatus/external` with `APP=beam`, `GATUS_DOMAIN=mainertoo.com`,
      `GATUS_PATH=/healthz`; confirm the Discord alert actually fires once (break it on purpose)
- [ ] Cloudflare tunnel dashboard: public hostname `beam.mainertoo.com` → Traefik (same
      target as existing public hostnames)
- [ ] PR → flux-local CI green → merge → `flux reconcile` → page loads over TLS

Acceptance (definition of v0-done):
- [ ] security gates (review round 1): cross-origin WS rejected in prod (test with a forged
      `Origin`), CSP present on all pages, TURN creds unobtainable without an approved WS
      session, per-IP rate limit on `POST /api/rooms` active and exercised
- [ ] idle WS through the CF tunnel survives > 5 min (25 s app pings vs ~100 s CF idle)
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
