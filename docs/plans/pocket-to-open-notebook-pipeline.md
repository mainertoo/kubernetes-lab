# Pocket AI → Open Notebook Pipeline (planning doc)

> **Status:** v7 — **CONVERGED** at Codex pass 7 (2026-05-28). 0 Critical, 0 High remaining; 1 Medium (F7-001, addressed) + 1 Low (F7-002, addressed) folded in as final pre-implementation patches. Plan is implementation-ready.
>
> **v7 changes** (Codex pass-6: 1 High + 3 Medium + 1 Low). Trajectory: pass 1 (1C+9H+14M) → pass 2 (5H+4M) → pass 3 (1H+3M) → pass 4 (2H+4M) → pass 5 (3H+3M+1L) → pass 6 (1H+3M+1L). All 5 accepted.
>
> - **F6-001 High**: v6's resume path found existing source via marker lookup but never advanced state from `received` to `source_created` — subsequent `advance_state(allowed_prior=["source_created"])` at step 10c rejected on every retry after a crash. v7 makes state advance **state-aware**: after source claim (whether via marker lookup OR fresh POST), advance state IF current state == `received` (no-op if state is already `source_created` or later). Same pattern for notes → `notes_created` and final → `complete`. Bridge checks current state before each advance; Lua scripts stay strict one-step-forward to preserve monotonicity. (§7.1)
> - **F6-002 Medium**: v6's "true exactly-once across crashes" claim contradicted the 5-page lookup cap. v7 reframes as "bounded idempotency for recent recordings (last ~500 sources in target notebook)". Added admin replay option `unbounded_scan=true` for old-recording recovery. (D16, §7.1 step 9b, §7.3)
> - **F6-003 Medium**: v6's `GET /api/notes?notebook_id=<id>` had no scale ceiling. v7 adds `open_notebook_notes_per_notebook` gauge updated on each lookup and `PocketBridgeNotebookNoteCount` warning alert at 5000 notes per notebook. Operational visibility for the v1 known-limit. (§7.1, §8.1, §8.2)
> - **F6-004 Medium**: §8.1 `state_cas_rejected_total` description still said `non_monotonic` "only triggers via /admin/replay reset_state or logic bug" while §8.2 was correctly hardened to "any increment is a real bug". v7 reconciles §8.1 to match: legitimate resets go to `replay_reset_total`, `non_monotonic` is bug-only. (§8.1)
> - **F6-005 Low**: subsection headers in §7 still tagged "v4/v5". v7 drops version tags from subsection headers (preserves them in change-log header only). (§7.1, §7.3, §7.6)
>
> **v6 changes** (Codex pass-5: 3 High + 3 Medium + 1 Low). Trajectory: pass 1 (1C+9H+14M) → pass 2 (5H+4M) → pass 3 (1H+3M) → pass 4 (2H+4M) → pass 5 (3H+3M+1L). All 7 accepted.
>
> - **F5-001 High**: v5's Phase 1 Lua code block still showed the non-UUID form (`SET ... '1' ...`), contradicting D14/§7.1/§7.6 UUID-fenced semantics. Implementer following Phase 1 alone would recreate F4-001. v6 replaces the snippet with UUID-fenced form: takes `owner_uuid` arg, `SET lock_key owner_uuid NX EX ttl`, returns `{action, state, owner_uuid}`. (§5 Phase 1)
> - **F5-002 High**: v5's `advance_state.lua` allowed transition when "lock value == owner_uuid OR lock absent" — the "OR absent" branch lets a stale worker whose lease expired advance state without ownership. v6 removes the "absent" branch entirely: `advance_state.lua` requires strict UUID match for all transitions including `complete`. Lock is released only AFTER successful `complete` transition. (§7.6)
> - **F5-003 High**: D16's pre-create lookup endpoints were not pinned in §6, so an implementer could pick wrong paths or miss pagination. v6 pins the exact contract: `GET /api/sources?notebook_id=<id>&limit=100&offset=N&sort_by=updated&sort_order=desc` (paginated, max 100/page, scan first 5 pages) for sources; `GET /api/notes?notebook_id=<id>` (returns all in single response, no pagination params) for notes. Both responses include `title` and `id`; client-side filter on title contains marker. (§6, §7.1)
> - **F5-004 Medium**: §8.1 `replay_total` label list missed `lock_held` and `already_complete` that v5 §7.3 emits. v6 adds both to the label enumeration. (§8.1)
> - **F5-005 Medium**: §8.2 alert guidance for `PocketBridgeStateCASNonMonotonic` said reset paths were expected — contradicted v5 §7.3 step 6 which emits `replay_reset_total` specifically to avoid that alert. v6 updates the alert annotation: `non_monotonic` should NOT fire on legitimate replay resets (those go to `replay_reset_total`); any `non_monotonic` increment is a real bug. (§8.2)
> - **F5-006 Medium**: §10 manual-export script only mentioned `[pocket-id:<recording_id>]` marker but D16 specifies notes carry the longer `[pocket-id:<id> kind:<summary|action_items>]` form. Without the `kind:` suffix, manual notes won't be claimed by bridge's per-kind lookup and duplicates appear. v6 spells out both marker forms in §10. (§10)
> - **F5-007 Low**: editorial — v5 changes header said `acquire_and_dispatch.lua` "returns the UUID it generated" implying Lua-side generation; §7.1 step 7 says bridge generates UUID. v6 standardizes: bridge generates `owner_uuid`, Lua stores it as the lock value, returns it back as confirmation only. (D14, §7.1)
>
> **v5 changes** (Codex pass-4: 2 High + 4 Medium, no Critical). Trajectory: pass 1 (1C+9H+14M) → pass 2 (5H+4M) → pass 3 (1H+3M) → pass 4 (2H+4M). All accepted in v5.
>
> - **F4-001 High**: v4's lease lacked owner fencing — lock value was constant `"1"`, so any worker could refresh/release/check any other worker's lock. On Redis blip → lease expire → worker B claim → both A and B writing IDs and advancing state. v5 fences with UUID-as-lock-value. `acquire_and_dispatch.lua` returns the UUID it generated. `refresh_lock`, `release_lock`, and `advance_state` all take the UUID and only mutate if the current value matches. On ownership-loss detection, worker aborts immediately and emits `lease_ownership_lost_total`. (D14 hardened, §7.1, §7.6)
> - **F4-002 High**: v4's external side effects to Open Notebook were not idempotent — a crash between Open Notebook 200 and Redis write left the API-side object created but Redis-side state at `received`, causing the resume path to create a duplicate. v5 embeds `[pocket-id:<recording_id>]` in every source title and `[pocket-id:<recording_id> kind:<summary|action_items>]` in every note title. Before each POST, bridge does `GET /api/sources` (or per-notebook `GET /api/notebooks/{id}/context`) and client-side-filters for the marker. If found, claim the existing object; if not, create. Adds one GET per ingest step but provides true exactly-once. Marker visible in Open Notebook UI as a small bracketed suffix. (D16 NEW, §7.1, §10)
> - **F4-003 Medium**: v4's `/admin/replay reset_state=true` would DEL a lock held by an active webhook worker, forking two competing ingest paths. v5 replay reads `pocket:lock:<recording_id>` first; if present and not owned by an explicit override, returns 409. New `force_delete_lock=true` param required to override; logs prominently. (§7.3)
> - **F4-004 Medium**: v4's Phase 0b1 label-capture used `| head -1`, silently dropping multiple matching pods. v5 fails closed: count matching pods, abort Phase 0b2 if != 1. (§5 Phase 0b1)
> - **F4-005 Medium**: v4's `state_cas_rejected_total{reason="non_monotonic"}` fired on legitimate `/admin/replay reset_state=true` paths, flapping the corruption alert. v5 adds distinct `replay_reset_total` counter for legitimate reset paths; `non_monotonic` reason scoped strictly to true logic bugs. (§7.3, §8.1, §8.2)
> - **F4-006 Medium**: v4 defined `lease_held_seconds` and `lease_expired_resume_total` in §8.1 but §7.1 never specified where to emit them. v5 anchors each metric to a specific handler branch in §7.1 and §7.6. (§7.1, §7.6, §8.1)
>
> **v4 changes** (Codex pass-3: 1 High + 3 Medium, no Critical). Trajectory: pass 1 had 1C+9H+14M, pass 2 had 5H+4M, pass 3 has 1H+3M. All accepted in v4.
>
> - **F3-001 High**: v3's `{"deferred":"in-progress"}` response permanently wedged recordings whose bridge crashed mid-ingest. The atomic state-write also served as a liveness signal, but a crash leaves state at `received`/`source_created`/`notes_created` with no actually-running worker; Pocket retries hit and get "in-progress" forever. v4 separates **liveness** from **progress**: a TTL-bound lease (`pocket:lock:<recording_id>` 60s) tracks active execution, monotonic state (`pocket:state:<recording_id>`) tracks progress. Lua script returns one of `{dedup, in_progress, resume, start}`. Handler resumes partial states using existing IDs in `pocket:ids:<recording_id>`. Lease is refreshed during long source POSTs and released on terminal state. (D14 revised, §7.1, §7.6 new)
> - **F3-002 Medium**: v3's `/admin/replay` assumed "ID in Redis = object exists in Open Notebook". A user-deleted object via UI would be reused as a stale ID, race to `complete`, and never recreate. v4 replay HEADs each cached ID (`GET /api/sources/<id>`, `GET /api/notes/<id>`) before reusing; 404 → clear stale ID → create fresh. Replay against `complete` state is explicitly allowed (post-recovery scenario). (§7.3)
> - **F3-003 Medium**: v3 assumed the Tailscale operator labels its egress-proxy Pod `tailscale.com/parent-resource-name=mac-ollama` — unverified. v4 splits Phase 0b into Phase 0b1 (ExternalName Service deploys, operator creates Pod, label captured via `kubectl get pod -n tailscale -l ... -o yaml`) and Phase 0b2 (NetworkPolicy committed with verified selector). Acceptance gate between. (§5)
> - **F3-004 Medium**: v3 defined `state_cas_rejected_total` and a `PocketBridgeStateCASNonMonotonic` alert but never incremented the metric in §7.1 — alert could never fire. v4 ties each Lua-script return path to a specific metric increment in handler steps. `non_monotonic` reason becomes meaningful for `/admin/replay` reset paths only (normal ingest can't trigger it). (§7.1, §8.1)
>
> **v3 changes** (Codex pass-2: 5 High + 4 Medium; all 5 Highs and 3/4 Mediums accepted; 1 deferred). All 5 Highs were new issues introduced by v2's own fixes — the cnpg-overlay-refactor convergence pattern. Full disposition in Appendix A.
>
> - **F2-001 High**: v2's state machine had TOCTOU (step 7 read → step 8 write). v3 uses an atomic Lua script (`pocket-bridge-state-cas.lua`) that does the state-transition check + write in a single Redis round-trip. (D14, §7.1)
> - **F2-002 High**: v2's step 8 unconditionally wrote `received`, silently downgrading state on retry. v3 makes all state transitions monotonic: the Lua script rejects any transition that isn't strictly forward in the chain `none → received → source_created → notes_created → complete`. (D14, §7.1)
> - **F2-003 High**: v2 placed the egress NetworkPolicy in `open-notebook`/`open-webui` — wrong end (a policy in ns X restricts pods IN X, not access TO X). v3 places the policy in the `tailscale` namespace where the operator places the egress proxy Pod, selecting that Pod by label `tailscale.com/parent-resource-name=mac-ollama`, ingress allowlist `open-notebook` + `open-webui` only. (§9)
> - **F2-004 High**: v2's "admin port ingress same-ns only" silently blocked Prometheus scraping (Prometheus runs in `kube-prometheus-stack`, not in-namespace) — every alert in §8.2 would never have fired. v3 splits metrics to a third port :8082 with its own NetworkPolicy allowing ingress from the Prometheus namespace only. (D15, §3, §4, §8)
> - **F2-005 High**: v2's egress NetworkPolicy omitted DNS, so the bridge could not resolve `open-notebook.open-notebook.svc.cluster.local`. v3 adds explicit UDP/TCP 53 egress to `kube-system` DNS pods. (§9)
> - **F2-006 Medium**: v2's fixture-capture gate in Phase 1 was unreachable (Pocket can only deliver after Phase 4). v3 introduces Phase 3a — fixture captured via Pocket dashboard's "send test event" feature between public-ingress activation (Phase 3) and real-recording acceptance (Phase 4). Phase 1 ships transformation code with conservative defaults; Phase 3a confirms or hotfixes against the real fixture. (§5)
> - **F2-007 Medium**: v2 hardcoded `200` as success in §7.1. v3 pins success codes per-endpoint from the OpenAPI fixture: `POST /api/sources/json` → 200, `POST /api/notebooks` → 201, `POST /api/notes` → 200, `POST /api/credentials` → 201. Bridge accepts the pinned code per endpoint. (§6)
> - **F2-009 Medium**: v2 omitted `automountServiceAccountToken: false`. v3 adds it. The bridge never calls the Kubernetes API. (§9)
> - **1 Medium deferred** — F2-008 (silent async-embedding degradation when Open Notebook embed fails after we return success). v3 documents the gap in §11; v1.5 will add a source-status poller CronJob that walks recent sources and alerts on `embedded == false` after N minutes.
>
> **v2 changes** (Codex pass-1: 1 Critical + 9 High + 13 Medium accepted; 1 Medium deferred): F1 four-state ingest machine; F2 `Pocket Inbox` default; F3 fixture capture via `--capture-fixture`; F4 manual-ingest script; F5 `/healthz` cluster-only; F6 single OpenAI-compatible mode for Open WebUI; F7 verified Open Notebook unavailable-provider behavior; F8 startup checks + secret precedes public ingress; F9 SOPS secret before Pocket URL flip; F10 Tailscale operator egress for cluster→Mac; F11 (Critical) `/admin/replay` internal-only with dual ports + Pangolin path allowlist; F12 1 MB body limit before HMAC; F13 `hmac.compare_digest` + header format validation; F14 Tailscale ACL + cluster NetworkPolicy; F15 replay fetch precedes dedup transition; F16 `embed: true` on source POST; F17 `notebooks: ["notebook:<id>"]` wording; F18 `contracts/open-notebook-2026-05-28.json` fixture; F19 `/api/models/defaults` schema probed pre-Phase-0; F20 `open_notebook_up` gauge; F21 bridge-owned `redis_up`; F23 `timestamp_fail` in failure alert; F24 per-operation write counters. F22 deferred.

## 1. Goal and motivation

Build a real-time pipeline that ingests Pocket AI recordings into a self-hosted Open Notebook instance, backed by a local LLM on the Mac. The bridge turns Pocket into a capture-only frontend while Open Notebook owns the analysis, synthesis, and long-term storage of every recording.

Side benefit: self-hosted equivalents of every Pocket Pro *analysis* feature (unlimited Ask, transformations, multi-source synthesis, podcast generation, bulk export). Pocket Pro *capture-side* limits (recording length, server retention, processing priority) remain gated by Pocket itself.

## 2. Decisions locked

| # | Decision | Rationale |
|---|---|---|
| D1 | FastAPI bridge over n8n | GitOps-native, no Postgres dependency, ~350 lines of Python |
| D2 | Pangolin (RackNerd VPS) → Newt for public ingress | Existing pattern; avoids Tailscale-client conflicts on the bridge side |
| D3 | One Open Notebook notebook per Pocket tag, with `Pocket Inbox` default for zero-tag recordings (F2) | Bridge auto-creates missing notebooks; source attaches to all matching tag notebooks via `SourceCreate.notebooks: ["notebook:<id>", ...]` |
| D4 | Redis-backed idempotency, four-state ingest tracker (F1) | States: `received` → `source_created` → `notes_created` → `complete`. Only `complete` deduplicates. 30-day TTL. |
| D5 | Source code lives in this repo at `apps/base/pocket-bridge/src/` | Solo-homelab scale doesn't justify a second repo |
| D6 | Local LLM = Ollama on Mac (M-series 64GB+) | Already installed |
| D7 | Chat model = `qwen3.6:latest` (Qwen3.5 MoE 36B, Q4_K_M, 262K context) | Already pulled; Apache 2.0 |
| D8 | Embedding model = `mxbai-embed-large` | 1024-dim, ~335MB |
| D9 | Chat UI = Open WebUI, OpenAI-compatible mode to Mac-Ollama (F6) | Single env var (`OPENAI_API_BASE_URLS`) |
| D10 | No backup labels on Redis PVC | Dedup state ephemeral; 30d TTL upper-bounds loss |
| D11 | Tailscale operator **egress** for cluster → Mac-Ollama (F10) | New `apps/base/mac-ollama-egress/`. Operator places proxy Pod in `tailscale` ns; nodes stay LAN-only. |
| D12 | `/admin/replay` on internal-only Service, second port (F11) | Public IngressRoute strict-matches `/webhook/pocket`. Pangolin path-allowlist at edge. Kubectl port-forward only. |
| D13 | `embed: true` on source POST (F16) | Default `false`; vector search requires `true`. |
| **D14 (hardened v5)** | **UUID-fenced lease + monotonic state (F2-001, F2-002, F3-001, F4-001)** | Lock value = UUID generated per-acquire. `refresh_lock`/`release_lock`/`advance_state` Lua scripts ALL take owner UUID arg and verify match before mutating. On ownership loss → worker aborts + emits `lease_ownership_lost_total`. Eliminates split-brain on Redis blip. |
| **D15 (v3)** | **Three-port bridge: :8080 webhook, :8081 admin/healthz, :8082 metrics (F2-004)** | Each port has its own Service + NetworkPolicy. |
| **D16 (revised v7)** | **Title-embedded recording-ID marker for bounded idempotency (F4-002, F6-002)** | Source titles: `"<original> [pocket-id:<recording_id>]"`. Notes: `"<original> [pocket-id:<recording_id> kind:<summary\|action_items>]"`. Pre-create lookup via `GET /api/sources` (paginated, 5-page cap) + `GET /api/notes` (single-shot per notebook) + client-side filter. Found → claim existing; not found → create. **Bounded idempotency**: covers crash-recovery for the most recent ~500 sources in target notebook. For older recordings, `/admin/replay` accepts `unbounded_scan=true` for full paginated scan. |

## 3. Architecture

```
Mac (M-series 64GB, Tailscale, tag:mac-ollama)
  └─ Ollama :11434 (bound to Tailscale IP)
       ├─ qwen3.6:latest                (chat / transformations)
       └─ mxbai-embed-large             (embeddings)
       └─ launchd, OLLAMA_KEEP_ALIVE=24h, OLLAMA_MAX_LOADED_MODELS=2
       └─ pmset -c sleep 0 + caffeinate
       └─ Tailscale ACL: tag:k8s-egress → tag:mac-ollama:11434 only

Cluster (production)
  ├─ Namespace tailscale  (operator-managed)
  │   └─ ts-mac-ollama-<id>-0 Pod   (egress proxy, joined to tailnet, tag:k8s-egress)
  │       └─ NetworkPolicy (v3 F2-003): ingress only from open-notebook + open-webui ns
  │
  ├─ apps/base/mac-ollama-egress/    Namespace ollama-egress
  │   └─ ExternalName Service        mac-ollama → fronts the tailscale proxy
  │
  ├─ apps/base/open-webui/           Namespace open-webui
  │   └─ OPENAI_API_BASE_URLS → http://mac-ollama.ollama-egress.svc:11434/v1
  │
  ├─ apps/base/open-notebook/        (existing) — credentials wired via mac-ollama egress
  │
  └─ apps/base/pocket-bridge/        Namespace open-notebook (co-located with consumer)
      ├─ Pod (one replica)
      │   ├─ bridge container
      │   │   ├─ :8080 /webhook/pocket   (public surface)
      │   │   ├─ :8081 /admin/replay + /healthz   (kubectl port-forward only)
      │   │   └─ :8082 /metrics          (Prometheus scrape only)
      │   └─ redis container (sidecar, AOF on 1Gi ceph-rbd PVC, no backup labels)
      │
      ├─ Service pocket-bridge-public   :8080 → IngressRoute /webhook/pocket
      ├─ Service pocket-bridge-admin    :8081 → NO IngressRoute, in-namespace only
      ├─ Service pocket-bridge-metrics  :8082 → NO IngressRoute, ServiceMonitor target
      │
      ├─ NetworkPolicy ingress:
      │   :8080 ← traefik-proxy ns only
      │   :8081 ← in-namespace only (no cross-ns)
      │   :8082 ← kube-prometheus-stack ns only  (v3 F2-004)
      │
      └─ NetworkPolicy egress (v3 F2-005):
          DNS → kube-system :53 UDP+TCP
          Open Notebook → open-notebook ns :5055
          Mac-Ollama → ollama-egress ns :11434
          Pocket API → 0.0.0.0/0 :443 (external HTTPS for /admin/replay)
      ▲
      │ Traefik IngressRoute (path strict-match /webhook/pocket) → pocket-bridge-public :8080
      │ RackNerd Pangolin → Newt (path allowlist = /webhook/pocket only)
      ▲
   Pocket Cloud (summary.completed webhook, HMAC-signed)
```

## 4. File layout

### 4.1 In this repo

```
apps/base/pocket-bridge/
├── src/
│   ├── main.py                            # FastAPI app, three-port serving
│   ├── lua/
│   │   ├── acquire_and_dispatch.lua       # Atomic lease + state read; returns
│   │   │                                  # {action, current_state, owner_uuid}
│   │   │                                  # (D14, F3-001, F4-001)
│   │   ├── refresh_lock.lua               # EXPIRE iff lock value == owner UUID
│   │   ├── release_lock.lua               # DEL    iff lock value == owner UUID
│   │   └── advance_state.lua              # SET    iff lock value == owner UUID
│   │                                      #        AND current state in allowed_prior
│   ├── requirements.txt                   # fastapi, uvicorn, httpx, redis,
│   │                                      #   pydantic, prometheus-client
│   └── Dockerfile                         # python:3.13-slim, non-root,
│                                          # automountServiceAccountToken: false applied
│                                          # via Deployment spec
├── contracts/
│   └── open-notebook-2026-05-28.json      # pinned OpenAPI snapshot (F18)
├── kustomization.yaml
├── pocket-bridge-release.yaml             # bjw-s, 2 containers + 3 Services
├── pocket-bridge-pvc.yaml                 # 1Gi ceph-rbd, NO backup labels
├── pocket-bridge-ingressroute.yaml        # Traefik /webhook/pocket → public Service
├── pocket-bridge-networkpolicies.yaml     # Per-port ingress + egress (DNS,
│                                          # Open Notebook, Mac-Ollama, Pocket API)
│                                          # (F2-004, F2-005)
├── pocket-bridge-servicemonitor.yaml      # scrape on :8082
├── pocket-bridge-prometheusrule.yaml      # Discord-routed
└── pocket-bridge-secret.sops.yaml         # POCKET_WEBHOOK_SECRET, OPEN_NOTEBOOK_API_KEY,
                                           # POCKET_API_TOKEN, REPLAY_ADMIN_TOKEN

apps/base/mac-ollama-egress/                # NEW (D11, F10)
├── kustomization.yaml
├── mac-ollama-egress-namespace.yaml       # ollama-egress ns
├── mac-ollama-egress-service.yaml         # ExternalName, tailscale.com/tailnet-fqdn
└── tailscale-proxy-networkpolicy.yaml     # v3/v4 — placed in `tailscale` ns,
                                           # selects operator-created Pod by label
                                           # (label captured in Phase 0b1 step 9,
                                           # NOT hardcoded — F3-003)
                                           # ingress allowlist: open-notebook + open-webui only
                                           # (F2-003)
                                           # Added in Phase 0b2 (separate PR from Service)

apps/base/open-webui/                       # NEW
├── kustomization.yaml
├── open-webui-namespace.yaml
├── open-webui-release.yaml
├── open-webui-pvc.yaml                    # backup: daily, backup-engine: kopia
├── open-webui-ingressroute.yaml           # chat.lab.mainertoo.com
└── open-webui-secret.sops.yaml

.github/workflows/
└── pocket-bridge-image.yaml               # paths: apps/base/pocket-bridge/{src,contracts}/**

scripts/
└── pocket-manual-ingest.sh                # F4 — DR fallback per §10

docs/runbooks/
└── pocket-bridge-manual-export.md         # references scripts/pocket-manual-ingest.sh
```

### 4.2 On the Mac

- `~/Library/LaunchAgents/com.ollama.tailscale.plist` (launchd unit)
- System: `pmset -c sleep 0 disksleep 0`
- Tailscale ACL: allow `tag:k8s-egress → tag:mac-ollama:11434`, deny all others

## 5. Phased rollout

### Phase 0 — Local LLM foundation

Goal: Mac-Ollama serving Qwen3.6 + mxbai-embed-large, reachable from cluster pods via Tailscale egress.

**Mac steps:**
1. `ollama pull mxbai-embed-large` (~335MB)
2. Configure launchd:
   ```bash
   launchctl setenv OLLAMA_HOST "$(tailscale ip -4):11434"
   launchctl setenv OLLAMA_MAX_LOADED_MODELS "2"
   launchctl setenv OLLAMA_KEEP_ALIVE "24h"
   ```
3. Restart Ollama; from another tailnet device verify `curl http://<mac-ts-ip>:11434/api/tags`
4. `sudo pmset -c sleep 0 disksleep 0`
5. In Tailscale admin: tag the Mac node `tag:mac-ollama`. Add ACL: `tag:k8s-egress → tag:mac-ollama:11434`. Verify other tailnet devices can no longer reach :11434.

**Cluster preflight — Tailscale egress (F10, split for label verification v4 F3-003):**

**Phase 0b1 — egress Service + label observation (one PR):**
6. Create `apps/base/mac-ollama-egress/`:
   - Namespace `ollama-egress`
   - ExternalName Service `mac-ollama` annotated `tailscale.com/tailnet-fqdn: <mac>.<tailnet>.ts.net` and `tailscale.com/tags: tag:k8s-egress`
   - **DO NOT include NetworkPolicy yet** — added in Phase 0b2 after labels observed
7. Wire into `apps/production/kustomization.yaml`; reconcile
8. Verify operator created the proxy Pod: `kubectl get pods -n tailscale | grep ts-mac-ollama`
9. **Capture the proxy Pod's actual labels (v4 F3-003, v5 fail-closed F4-004)**:
   ```bash
   PODS=$(kubectl get pods -n tailscale -l 'tailscale.com/managed=true' --no-headers \
            | awk '{print $1}' | grep mac-ollama)
   COUNT=$(echo "$PODS" | grep -c .)
   if [ "$COUNT" -ne 1 ]; then
     echo "FAIL: expected exactly 1 mac-ollama proxy Pod, found $COUNT" >&2
     echo "$PODS" >&2
     exit 1
   fi
   POD=$PODS
   kubectl get -n tailscale pod/$POD -o jsonpath='{.metadata.labels}' | jq
   ```
   Record the unique selector label(s) (likely `tailscale.com/parent-resource-name=mac-ollama` per recent operator versions, but verify against actual output). If COUNT != 1 → halt; do not proceed to Phase 0b2 until resolved.
10. From a test pod in `open-notebook` ns: `curl http://mac-ollama.ollama-egress.svc.cluster.local:11434/api/tags` must succeed.

**Phase 0b2 — NetworkPolicy with verified selector (separate PR):**
11. Add `apps/base/mac-ollama-egress/tailscale-proxy-networkpolicy.yaml`:
    - `metadata.namespace: tailscale`
    - `spec.podSelector.matchLabels:` ← exact label captured in step 9
    - `spec.ingress[0].from:` namespaces `open-notebook` + `open-webui` only
12. Reconcile; verify:
    - From `open-notebook` test pod: `curl http://mac-ollama.ollama-egress.svc:11434/api/tags` succeeds
    - From `default` test pod: same curl fails (blocked by NetworkPolicy)
13. **Acceptance gate**: if step 12 fails either direction, do not proceed.

**Open Notebook schema preflight (F19):**
14. Port-forward Open Notebook; `curl /openapi.json | jq` and extract `POST /api/models/defaults` request schema
15. Record in `apps/base/pocket-bridge/contracts/models-defaults-2026-05-28.json`

**Open WebUI deploy (F6):**
16. Create `apps/base/open-webui/`. OpenAI-compatible mode only: `OPENAI_API_BASE_URLS=http://mac-ollama.ollama-egress.svc.cluster.local:11434/v1`, `OPENAI_API_KEYS=any-non-empty`
17. Reconcile; verify `chat.lab.mainertoo.com` loads, lists both models, test chat succeeds

**Open Notebook credentials:**
18. Run the 4-step credential wiring (curl payloads pinned with exact schemas from step 15):
    ```bash
    ON=http://open-notebook.open-notebook.svc.cluster.local:5055
    TS=mac-ollama.ollama-egress.svc.cluster.local

    CID=$(curl -s -X POST "$ON/api/credentials" \
      -H 'Content-Type: application/json' \
      -d "{\"name\":\"mac-ollama\",\"provider\":\"ollama\",
           \"modalities\":[\"language\",\"embedding\"],
           \"base_url\":\"http://$TS:11434\"}" | jq -r '.id')

    curl -s -X POST "$ON/api/credentials/$CID/discover"

    curl -s -X POST "$ON/api/credentials/$CID/register-models" \
      -H 'Content-Type: application/json' \
      -d '{"models":[
            {"name":"qwen3.6:latest","provider":"ollama","type":"language"},
            {"name":"mxbai-embed-large","provider":"ollama","type":"embedding"}
          ]}'

    curl -s -X POST "$ON/api/models/defaults" -H 'Content-Type: application/json' -d @defaults.json
    ```
19. **Verify Open Notebook queue/retry semantics on provider unavailability (F7)**: stop Ollama, ingest a test source, observe behavior, document in Phase 0 results.
20. UI smoke test: create throwaway notebook, add text source, confirm embedding + chat both work.

**Acceptance:** Open Notebook embeds + chats against Mac-Ollama; cross-ns network restrictions verified bidirectionally; F7 behavior documented.

### Phase 1 — Bridge image (with conservative payload mapping)

Goal: container image published to GHCR. No cluster wiring yet.

1. Write `apps/base/pocket-bridge/src/`:
   - **Three-port serving (D15)**: webhook on :8080, admin/healthz on :8081, metrics on :8082. Three separate FastAPI app mounts on the same process (different `uvicorn`/`asyncio` ports) OR three subprocess workers — implementation choice TBD at code time, both achieve port isolation.
   - **Startup checks**: refuse to start if `POCKET_WEBHOOK_SECRET` or `REPLAY_ADMIN_TOKEN` is empty (F8, F11)
   - **Webhook handler (`POST /webhook/pocket` on :8080)**: body-size limit 1 MB before read (F12); header format validation (F13); `hmac.compare_digest` (F13); timestamp window check; UUID-fenced Lua lease+state acquire (D14 hardened, F3-001, F4-001); resume-aware ingest with **pre-create lookup via title marker** (D16, F4-002); tag resolution with `Pocket Inbox` default (F2); POST source with `embed: true` (D13, F16); POST 2 notes; UUID-fenced lease refresh during long ops; UUID-fenced state advance; metrics tied to Lua return paths (F3-004, F4-006)
   - **`POST /admin/replay` on :8081**: bearer auth via `compare_digest`; Pocket fetch FIRST (F15); **read pocket:lock first — if lock present, return 409 unless `force_delete_lock=true`** (F4-003); HEAD/GET each cached source/note ID against Open Notebook before reusing — 404 → clear stale ID + create fresh (F3-002); explicit allowance for replay against `complete` state (post-recovery scenario)
   - **`GET /healthz` on :8081 (F5)**: NOT on public port. Redis ping + Open Notebook ping
   - **`GET /metrics` on :8082**: Prometheus scrape target per §8.1
   - **Conservative payload mapping (F2-006)**: Pocket field paths coded from prior-chat hints (`summarizations[id].v2.actionItems`, `summary`, `transcript`, `tags`) but each extraction wrapped in `_safe_extract(payload, "path.to.field", default=None)`. Missing fields don't crash; they log + increment `webhook_total{result="payload_field_missing"}`. The fixture-validated mapping comes in Phase 3a.
   - **Lua acquire-and-dispatch** (`src/lua/acquire_and_dispatch.lua`, D14 hardened, F3-001, F4-001, F5-001):
     ```lua
     -- KEYS[1] = pocket:state:<recording_id>
     -- KEYS[2] = pocket:lock:<recording_id>
     -- ARGV[1] = lease_ttl_seconds (e.g. 60)
     -- ARGV[2] = owner_uuid (generated by bridge via uuid4)
     -- Returns: {action, current_state, owner_uuid}
     --   "dedup"       — state is "complete"          (lock NOT taken)
     --   "in_progress" — lock currently held by other  (lock NOT taken)
     --   "resume"      — partial state, no live lock   (lock TAKEN with owner_uuid)
     --   "start"       — no state at all               (lock TAKEN with owner_uuid)
     local state = redis.call('GET', KEYS[1])
     if state == false then state = 'none' end
     if state == 'complete' then return {'dedup', state, ''} end
     local lock_ok = redis.call('SET', KEYS[2], ARGV[2], 'NX', 'EX', ARGV[1])
     if not lock_ok then return {'in_progress', state, ''} end
     if state == 'none' then return {'start', state, ARGV[2]} end
     return {'resume', state, ARGV[2]}
     ```
     Sibling scripts (all UUID-fenced — strict match required, no "lock absent" shortcut, F5-002):
     - `refresh_lock.lua(KEYS[1]=lock_key, ARGV[1]=owner_uuid, ARGV[2]=ttl)`: `if GET == ARGV[1] then EXPIRE; return 1 else return 0`
     - `release_lock.lua(KEYS[1]=lock_key, ARGV[1]=owner_uuid)`: `if GET == ARGV[1] then DEL; return 1 else return 0`
     - `advance_state.lua(KEYS[1]=state_key, KEYS[2]=lock_key, ARGV[1]=owner_uuid, ARGV[2]=new_state, ARGV[3]=ttl, ARGV[4..N]=allowed_prior)`: requires `GET KEYS[2] == ARGV[1]` (strict — fails if lock absent); requires current state ∈ allowed_prior; SET state new_state EX ttl
     - **Per F5-007**: bridge generates `owner_uuid = uuid4()` once per ingest; Lua stores and echoes it as confirmation. No Lua-side UUID generation.
   - **Status code handling (F2-007)**: pinned per-endpoint from `contracts/open-notebook-2026-05-28.json`. `POST /api/sources/json` → 200. `POST /api/notebooks` → 201. `POST /api/notes` → 200. `POST /api/credentials` → 201. Bridge accepts only the pinned code per endpoint; any other 2xx counts as fail (catches API drift).
2. **Contract test**: load `contracts/open-notebook-2026-05-28.json` as fixture; bridge's Open Notebook client validates request shapes + expected response codes against the fixture (F18, F19, F2-007)
3. `.github/workflows/pocket-bridge-image.yaml` — paths filter `apps/base/pocket-bridge/{src,contracts}/**`; build, push to `ghcr.io/mainertoo/pocket-bridge:{sha,latest}`
4. Manual local smoke: `docker run` against stubbed Open Notebook; `/healthz` returns 200; `/metrics` returns prometheus text

**Acceptance:** image builds; healthcheck passes locally; contract tests pass; conservative payload mapping handles missing-field cases gracefully.

### Phase 2 — Cluster manifests (real secret committed before public exposure)

Goal: pocket-bridge deployed with real `POCKET_WEBHOOK_SECRET`, ready for webhooks but not yet internet-reachable.

1. In Pocket dashboard: create webhook with placeholder target URL (`https://placeholder.invalid/webhook/pocket`), delivery disabled. Capture the generated secret.
2. Build `apps/base/pocket-bridge/` manifests per §4.1:
   - Three ClusterIP Services (`pocket-bridge-public` :8080, `pocket-bridge-admin` :8081, `pocket-bridge-metrics` :8082) (D15, F2-004)
   - IngressRoute attaches to `pocket-bridge-public` only; Traefik strict-match `/webhook/pocket` (F5, F11)
   - **NetworkPolicies (v3 F2-004, F2-005)**:
     - Ingress :8080 from `traefik-proxy` ns only
     - Ingress :8081 from in-namespace only
     - Ingress :8082 from `kube-prometheus-stack` ns only
     - Egress: UDP+TCP 53 to `kube-system` (CoreDNS); TCP 5055 to `open-notebook` ns; TCP 11434 to `ollama-egress` ns; TCP 443 to `0.0.0.0/0` (Pocket API for /replay)
   - Deployment `automountServiceAccountToken: false` (F2-009)
   - PrometheusRule with all alerts per §8.2
   - ServiceMonitor targets `pocket-bridge-metrics` :8082
3. SOPS-encrypt **real** values for `POCKET_WEBHOOK_SECRET`, `REPLAY_ADMIN_TOKEN` (generate fresh), `POCKET_API_TOKEN`, `OPEN_NOTEBOOK_API_KEY` (empty placeholder OK)
4. Wire into `apps/production/kustomization.yaml`; reconcile
5. Verify bridge Pod starts (would refuse on empty secrets — F8, F11)
6. Verify `/healthz` from admin port via port-forward: `kubectl port-forward svc/pocket-bridge-admin 8081 -n open-notebook & curl http://localhost:8081/healthz`
7. Verify `/admin/replay` returns 404 on public port: `kubectl run debug --rm -it --image=curlimages/curl -- curl http://pocket-bridge-public.open-notebook.svc:8080/admin/replay`
8. **Verify Prometheus scrape is working**: in Prometheus UI, `up{service="pocket-bridge-metrics"} == 1`. (Catches any NetworkPolicy mistake before Phase 3.)
9. Verify DNS resolution from inside bridge Pod: `kubectl exec -n open-notebook deploy/pocket-bridge -c bridge -- nslookup open-notebook.open-notebook.svc.cluster.local`

**Acceptance:** bridge running with real secrets; three-port surface enforced; Prometheus scraping; DNS working; no internet exposure.

### Phase 3 — Public ingress via Pangolin (path-locked)

Goal: bridge reachable at `https://pocket-bridge.mainertoo.com/webhook/pocket` only.

1. On RackNerd Pangolin: resource `pocket-bridge.mainertoo.com` → `http://pocket-bridge-public.open-notebook.svc.cluster.local:8080`. **Path allowlist: `/webhook/pocket` only.** Other paths rejected at VPS edge.
2. From an external network (phone hotspot):
   - `curl -X POST https://pocket-bridge.mainertoo.com/webhook/pocket -d '{}'` → 401
   - `curl https://pocket-bridge.mainertoo.com/healthz` → 404 (Pangolin path restriction)
   - `curl https://pocket-bridge.mainertoo.com/admin/replay` → 404
   - `curl https://pocket-bridge.mainertoo.com/metrics` → 404
   - `curl -X POST -H 'Content-Type: application/json' --data-raw "$(yes A | head -c 2000000)" https://pocket-bridge.mainertoo.com/webhook/pocket` → 413 before HMAC check (F12)
3. From inside cluster: `kubectl port-forward svc/pocket-bridge-admin 8081 & curl -H 'Authorization: Bearer wrong' http://localhost:8081/admin/replay` → 401

**Acceptance:** public URL reachable only at `/webhook/pocket`; all other paths 404 at edge; body size limit enforced; admin endpoint internal-only.

### Phase 3a — Pocket payload fixture capture (NEW v3, F2-006)

Goal: confirm the conservative payload mapping shipped in Phase 1 matches real Pocket payloads. Catch field-path drift before real recordings.

1. In Pocket dashboard: temporarily update webhook target URL from placeholder to `https://pocket-bridge.mainertoo.com/webhook/pocket`. **Do not enable real recording delivery yet.**
2. Deploy bridge with env `POCKET_CAPTURE_FIXTURE=true`. In this mode, on receipt of any HMAC-valid webhook, the bridge writes the raw body to a logged location and returns 200 without processing.
3. Trigger Pocket's "Send Test Event" feature in the dashboard. Bridge captures the fixture.
4. `kubectl logs -n open-notebook deploy/pocket-bridge -c bridge` → extract captured JSON payload.
5. Commit as `apps/base/pocket-bridge/contracts/pocket-summary-completed-<date>.json`.
6. Compare captured fixture against Phase 1 conservative mapping:
   - If all field paths match: no code change needed; remove `POCKET_CAPTURE_FIXTURE` env, redeploy, proceed to Phase 4.
   - If field paths differ: open a hotfix PR updating `src/main.py` extractions; new image built via GHA; redeploy; re-run test event; verify mapping succeeds; proceed to Phase 4.
7. Add a unit test that loads the fixture and validates each field-extraction function against it. This test runs in CI on every bridge-src change going forward.

**Acceptance:** fixture committed; bridge processes the test event without `payload_field_missing` metric incrementing; CI unit test passes.

### Phase 4 — End-to-end with real recordings

Goal: real Pocket recordings land in Open Notebook fully.

1. In Pocket dashboard: enable webhook delivery (was disabled in Phase 2 step 1). SOPS secret with real `POCKET_WEBHOOK_SECRET` was committed in Phase 2 (F9).
2. Record a 30-second test clip with one tag (e.g. `test`):
   - HMAC verified; timestamp within window
   - State machine: Lua CAS `none → received → source_created → notes_created → complete`
   - `notebook_ensure_total{result="created"}` increments for `test` tag (first time)
   - Source POST returns 200 (per pinned status code); `embed: true`
   - 2 notes POSTed, both 200; attached to `test` notebook
3. Verify in Open Notebook UI: `test` notebook exists; source visible; `embedded == true`; `embedded_chunks > 0`; 2 notes attached
4. **Replay test (Pocket retry simulation)**: in Pocket dashboard, manually re-trigger delivery for same recording → bridge returns 200, `webhook_total{result="duplicate"}` increments; no second ingest; state remains `complete`
5. **Untagged-recording test (F2)**: record 10s clip with no tags → bridge routes to `Pocket Inbox` notebook (auto-created if first time)
6. **Concurrency test (F2-001 verification)**: in Pocket dashboard, manually re-trigger delivery twice in rapid succession (< 1s apart) → exactly one ingest completes; Lua CAS rejects the second; state machine remains consistent; no duplicate source/notes
7. **Long-recording test**: record 20-min clip → single webhook delivery; full transcript ingested; embed completes async (may take 1-2 min)
8. **/admin/replay test**: pick a recording, simulate failure by deleting its notes in Open Notebook UI, then `kubectl port-forward svc/pocket-bridge-admin 8081 & curl -X POST -H "Authorization: Bearer $REPLAY_TOKEN" -d '{"recording_id":"..."}' http://localhost:8081/admin/replay` → notes recreated, source not duplicated (state map reused)

**Acceptance:** real recordings (tagged, untagged, long) land; idempotency + concurrency verified; replay verified.

## 6. Open Notebook API contract (pinned from 2026-05-28 OpenAPI dump)

Live snapshot at `apps/base/pocket-bridge/contracts/open-notebook-2026-05-28.json` (F18). Bridge tests validate every request against this fixture.

**Per-endpoint pinning (v3 F2-007):**

| Endpoint | Body shape | Expected success | Notes |
|---|---|---|---|
| `POST /api/sources/json` | `{type:"text", content, title, notebooks:["notebook:<id>",...], transformations:[], embed:true, async_processing:true}` | **200** | `embed:true` required (D13, F16). Default false; vector search requires true. |
| `POST /api/notebooks` | `{name, description}` | **201** | Returns `{id:"notebook:<surreal-id>",...}` |
| `POST /api/notes` | `{content, title, note_type:"human"\|"ai", notebook_id:"notebook:<id>"}` | **200** | Single notebook only — multi-notebook notes not supported in this API version |
| `POST /api/credentials` | see §5 Phase 0 step 15 | **201** | |
| `GET /api/notebooks` | — | **200** | No name-filter param; filter client-side + cache in Redis |
| `GET /api/sources` (NEW pin v6 F5-003) | query: `notebook_id=<id>&limit=100&offset=N&sort_by=updated&sort_order=desc` | **200** | Array of `SourceListResponse` containing `id`, `title`. Pagination via offset; **max limit per page = 100**. Bridge marker-lookup scans up to **5 pages** (most recent first by `updated desc`) before declaring "not found". |
| `GET /api/notes` (NEW pin v6 F5-003) | query: `notebook_id=<id>` | **200** | Array of `NoteResponse` containing `id`, `title`. **No pagination** — returns all notes in notebook in one response. Bridge scans full list client-side. |
| `GET /api/sources/{source_id}` | path: source_id | **200** / 404 | Used by `/admin/replay` step 7 to verify cached IDs (F3-002) |
| `GET /api/notes/{note_id}` | path: note_id | **200** / 404 | Used by `/admin/replay` step 7 |

**Auth:** currently disabled cluster instance. Bridge sends `Authorization: Bearer <key>` only if `OPEN_NOTEBOOK_API_KEY` env non-empty. Survives a future auth-flip without code change.

**Re-probe procedure** on Open Notebook upgrade: port-forward, `curl /openapi.json > new-snapshot.json`, diff against pinned, commit a new dated fixture, run contract tests.

## 7. Bridge logic detail

### 7.1 Webhook handler order

```
1. Request size check at FastAPI middleware (max 1 MB before read)  → else 413
2. Read raw body + headers (Pocket-Signature, Pocket-Timestamp)
3. Validate header format (timestamp parses, signature is hex)       → else 401
4. Verify timestamp within 5-min window                              → else 401
5. Verify hmac.compare_digest(HMAC(body, secret), signature)         → else 401
6. Parse JSON; filter to event == "summary.completed"                → else 200 {"skipped": "non-summary"}
7. Generate owner_uuid = uuid4(). Invoke acquire_and_dispatch.lua(state_key, lock_key, lease_ttl=60s, owner_uuid). Branch on returned action:
     - "dedup":       → 200 {"skipped":"duplicate"},  webhook_total{result="duplicate"},  state_cas_rejected_total{reason="already_complete"}++
     - "in_progress": → 200 {"deferred":"concurrent"}, webhook_total{result="in_progress"}, state_cas_rejected_total{reason="concurrent_in_progress"}++
     - "start":       lock acquired with owner_uuid, state=none → advance state to "received" via advance_state.lua(owner_uuid, allowed_prior=["none"]); proceed to step 8.
                      If acting state="received" → EMIT ingest_state_total{state="received"}++
     - "resume":      lock acquired with owner_uuid, state ∈ {"received","source_created","notes_created"} → EMIT lease_expired_resume_total++; proceed to step 8 WITHOUT state advance; resume from current state.
8. Resolve tags → notebook_ids (cache hit / list-and-create on miss / Pocket Inbox if no tags)
9. Source idempotency + POST + state-aware advance (D16; endpoints pinned F5-003; state-aware F6-001):
     a. Construct marker: M = "[pocket-id:<recording_id>]"
     b. **Pre-create lookup**: paginated scan of `GET /api/sources?notebook_id=<ids[0]>&limit=100&offset=N&sort_by=updated&sort_order=desc`, N ∈ {0,100,200,300,400}. Client-side filter `title contains M`. Early-exit on first match. Cap at 5 pages (500 sources) — beyond that, declare "not found" and create fresh.
        - **Bounded idempotency**: this covers crash-recovery for recently-ingested recordings. For recordings older than ~500 sources back, operator can invoke `/admin/replay` with `unbounded_scan=true` for full paginated scan.
     c. **Source establishment** — one of:
        - **Found existing via 9b**: claim its source_id; SET pocket:ids:<recording_id>.source_id with UUID guard. Skip to step 9e.
        - **Not found AND no cached source_id**: POST /api/sources/json with title=`<derived_title> [pocket-id:<recording_id>]`, notebooks=[ids], embed=true, async_processing=true; expect 200 per pinned contract.
            - On success: SET pocket:ids:<recording_id>.source_id with UUID guard; EMIT open_notebook_write_total{operation="source_post",result="success"}++. Continue to step 9e.
            - On 4xx/5xx: release_lock.lua(owner_uuid); EMIT open_notebook_write_total{operation="source_post",result="fail"}++; return 500 — Pocket retry resumes after lease expiry.
        - **Cached source_id present** (resume path with intact Redis ID): no API call needed; skip to step 9e.
     d. (reserved — kept for numbering compatibility with replay path)
     e. **State-aware advance to `source_created` (v7 F6-001; F7-002: after each successful advance, handler updates local `current_state` variable in-memory before the next state check below)**:
        - If current_state == "received": call advance_state.lua(owner_uuid, allowed_prior=["received"], new_state="source_created"); EMIT ingest_state_total{state="source_created"}++.
        - If current_state ∈ {"source_created", "notes_created"}: SKIP advance — already at or past this state. (Monotonic invariant preserved; Lua never sees a no-op transition.)
     f. Periodically (every 20s) call refresh_lock.lua(owner_uuid). On ownership-lost return: EMIT lease_ownership_lost_total++; ABORT — do not continue making API calls; return 500.
10. Notes idempotency + POST + state-aware advance (D16; endpoints pinned F5-003; state-aware F6-001):
     a. Single fetch: `GET /api/notes?notebook_id=<ids[0]>` (returns all notes for the notebook in one response — no pagination). EMIT open_notebook_notes_per_notebook.set(len(response)) for F6-003 instrumentation.
     b. For each kind ∈ {"summary","action_items"}:
        - Marker_k = "[pocket-id:<recording_id> kind:<kind>]"
        - **Pre-create lookup**: filter the step 10a response client-side for `title contains Marker_k`.
        - If found: claim note_id (no API call).
        - If not found AND no cached note_id for this kind: POST /api/notes with title=`<kind-display-name> [pocket-id:<recording_id> kind:<kind>]`, notebook_id=ids[0]; expect 200; on success store id with UUID guard; on fail release_lock + 500.
        - If cached note_id present: no API call needed.
     c. **State-aware advance to `notes_created` (v7 F6-001)**:
        - If current_state == "source_created": call advance_state.lua(owner_uuid, allowed_prior=["source_created"], new_state="notes_created"); EMIT ingest_state_total{state="notes_created"}++.
        - If current_state == "notes_created": SKIP advance — already at this state.
11. **State-aware advance to `complete` (v7 F6-001)**:
     - If current_state == "notes_created": call advance_state.lua(owner_uuid, allowed_prior=["notes_created"], new_state="complete", ttl=2592000); release_lock.lua(owner_uuid); EMIT ingest_state_total{state="complete"}++; lease_held_seconds.observe(now - start_time).
     - If current_state == "complete": SKIP. (Cannot occur in practice: step 7 returned "dedup" for complete state.) Defensive code only.
12. Return 200 {"recording_id", "source_id", "note_ids", "notebooks"}, webhook_total{result="success"}
```

**Why UUID fencing + title markers (D14 hardened + D16, F4-001 + F4-002)**:
- UUID lock value = each worker proves ownership before any mutation. Redis blip → lease expire → worker B claim is now safe because worker A's next operation will detect ownership-lost and abort.
- Title markers = Open Notebook becomes the source of truth for "did this side effect happen?" Bridge's Redis is just a cache for fast lookup; on cache miss + crash recovery, pre-create lookup against Open Notebook finds the existing object via its embedded marker.
- Together: zero duplicates across any single-node crash scenario.

**Metric anchors (F3-004 + F4-006)**:
- `state_cas_rejected_total{reason}` — step 7 (`already_complete`, `concurrent_in_progress`), advance_state.lua rejections (`non_monotonic`, logic-bug-only on normal path)
- `lease_expired_resume_total` — step 7 "resume" branch only
- `lease_ownership_lost_total` — step 9e abort path
- `lease_held_seconds` — step 11 histogram observe at terminal release
- `ingest_state_total{state}` — at each state transition

### 7.2 Why async_processing=true

Open Notebook's embed + transformation pipeline can take 10–30s. Pocket's webhook timeout is ~10s. Async decouples bridge response from Open Notebook's heavy lifting.

**Known limitation (F2-008, deferred to v1.5):** If async embedding fails after we return success, bridge marks `complete` but vector search degrades silently. v1.5 will add a CronJob that walks recent sources via `GET /api/sources/{id}/status`, alerts on `embedded == false` after N minutes.

### 7.3 /admin/replay

```
POST /admin/replay  (on admin port :8081 only)
Authorization: Bearer <REPLAY_ADMIN_TOKEN>  (compare_digest)
Body: { recording_id: "...",
        reset_state: <bool, default false>,
        force_delete_lock: <bool, default false>,
        unbounded_scan: <bool, default false>  # v7 F6-002 — full paginated source scan for old recording recovery
      }

1. Verify bearer token                                                  → else 401, replay_total{result="bearer_fail"}++
2. Fetch recording from Pocket API (uses POCKET_API_TOKEN)              → else 502 (no state change, F15), replay_total{result="pocket_fetch_fail"}++
3. Read pocket:state:<recording_id>, pocket:ids:<recording_id>, pocket:lock:<recording_id>
4. **Live-lock check (v5 F4-003)**:
     - If pocket:lock present AND force_delete_lock != true:
         → 409 {"error":"active worker holds lease; pass force_delete_lock=true to override"}, replay_total{result="lock_held"}++
     - If force_delete_lock == true: DEL pocket:lock; log prominently as "operator forced lock release for <recording_id>"
5. If state == "complete" AND reset_state == false:
     - 409 {"error":"already complete; pass reset_state=true to override"}, replay_total{result="already_complete"}++
6. If reset_state == true:
     - DEL pocket:state:<recording_id>
     - EMIT replay_reset_total++ (v5 F4-005 — distinct from corruption-signaling state_cas_rejected_total{reason="non_monotonic"})
7. For each cached ID in pocket:ids:<recording_id>:
     - source_id: GET /api/sources/<id>  → 200: keep; 404: clear from cache
     - note_ids:  GET /api/notes/<id>    → 200: keep; 404: clear from cache
     (F3-002)
8. Acquire lease via acquire_and_dispatch.lua with new owner_uuid (treats state as fresh after step 6 reset)
9. Synthesize webhook payload from Pocket API response
10. Run normal ingest steps 8-12 from §7.1. If body had `unbounded_scan=true`, override the §7.1 step 9b 5-page cap with full paginated scan (continue until response.length < 100); otherwise default 5-page bound applies. **Call refresh_lock.lua(owner_uuid) every 5 pages during unbounded scan AND once immediately before any POST that follows** (v7 F7-001 — prevents lease expiry mid-scan from leaving an active POST with stale ownership). State-aware advance (F6-001) handles the resume path automatically — claimed-existing-objects don't double-advance.
11. On full success: replay_total{result="success"}++; on ingest failure: replay_total{result="ingest_fail"}++
```

**Why live-lock check + distinct reset metric (F4-003, F4-005)**:
- Step 4 prevents replay from yanking a lock out from under an active webhook worker. `force_delete_lock` is the documented escape hatch for "the worker is truly dead but lease hasn't expired yet" scenarios.
- Step 6 emits `replay_reset_total` for legitimate state resets — these don't trigger `PocketBridgeStateCASNonMonotonic`, which is reserved for genuine logic bugs.

**Why verify (F3-002)**: A user-deleted source/note via UI leaves a stale ID in Redis. Step 7's GET-before-reuse, combined with §7.1 step 9b's title-marker pre-create lookup (D16, F4-002), makes replay tolerant of out-of-band state mutations.

### 7.4 Tag cache invalidation

Cache `pocket:tag:<tag> → <notebook_id>` has no TTL. On source POST 404 → evict + re-resolve + retry once. Second 404 → 500 + `notebook_ensure_total{result="stale_cache_unrecoverable"}`.

### 7.5 Untagged recordings (F2)

Empty/absent `tags` → resolve to single notebook `Pocket Inbox` (auto-created on first use, cached under `pocket:tag:__default__`).

### 7.6 Lease management

- **Lease key**: `pocket:lock:<recording_id>`, **value = owner_uuid** (uuid4 per acquire, v5 F4-001), TTL 60s
- **Acquired**: by `acquire_and_dispatch.lua(state_key, lock_key, ttl, owner_uuid)` on `start`/`resume` outcomes — owner_uuid is the bridge-generated UUID passed in
- **Refreshed**: every 20s by `refresh_lock.lua(lock_key, owner_uuid)` — script: `if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('EXPIRE', KEYS[1], 60); return 1 else return 0 end`. Return 0 → ownership lost.
- **Released**: on `complete` by `release_lock.lua(lock_key, owner_uuid)` — only DELs if value matches. Implicit release on TTL expiry if bridge crashes.
- **State advance**: `advance_state.lua(state_key, lock_key, owner_uuid, allowed_prior, new_state, ttl)` — **strict UUID match required** (v6 F5-002): only mutates state if `GET lock_key == owner_uuid` AND current state ∈ allowed_prior. The "OR lock absent" shortcut from v5 is removed — a stale worker whose lease expired CANNOT advance state. Lock release happens only AFTER the successful `complete` transition (terminal release).
- **Ownership-lost handling (v5 F4-001)**: any Lua call returning "ownership lost" → bridge EMITs `lease_ownership_lost_total++`, releases nothing (the lock isn't ours anymore), ABORTS the current request with HTTP 500. The webhook will be retried by Pocket; the new attempt acquires fresh ownership.
- **Sizing**: 60s TTL covers typical async source POST (returns in 1-2s) plus margin. Pocket's webhook retry cadence is typically 5min+ between attempts, so a 60s lease can never collide with the same recording's next legitimate retry.
- **Implication**: a crash mid-ingest leaves orphaned state at `received` or `source_created`; lease expires within 60s; Pocket's retry (typically 5+ min later) lands the `resume` path with a NEW owner_uuid; pre-create title-marker lookup (D16) finds any Open Notebook objects the prior worker created; ingest completes without duplicates.

`/admin/replay` is only needed for harder failures: Open Notebook 5xx during the active window that exhausted Pocket's retry budget, manual UI deletion of objects, or recovering from `complete`-state errors that need full reprocessing.

## 8. Observability

### 8.1 Metrics (Prometheus, scraped via ServiceMonitor on pocket-bridge-metrics :8082)

| Metric | Labels | Tracks |
|---|---|---|
| `webhook_total` | `event`, `result` | success / duplicate / in_progress (NEW v3) / hmac_fail / timestamp_fail / non_summary / body_too_large / open_notebook_error / payload_field_missing (NEW v3) |
| `ingest_seconds` | `phase` | parse, dedup, tag_resolve, source_post, notes_post |
| `tag_cache_hits_total` | `result` | hit / miss / stale_evicted |
| `notebook_ensure_total` | `result` | found_in_cache / found_via_list / created / stale_cache_unrecoverable |
| `replay_total` | `result` | success / bearer_fail / pocket_fetch_fail / ingest_fail / **lock_held** / **already_complete** (v6 F5-004 — both emitted in §7.3 steps 4+5) |
| `redis_up` | — | Gauge 1/0 (F21) |
| `open_notebook_up` | — | Gauge 1/0 (F20) |
| `open_notebook_ping_total` | `result` | success / fail |
| `open_notebook_write_total` | `operation`, `result` | source_post / summary_note_post / action_items_note_post (F24) |
| `ingest_state_total` | `state` | received / source_created / notes_created / complete |
| `state_cas_rejected_total` | `reason` | concurrent_in_progress / already_complete / non_monotonic (each reason tied to specific Lua return path; `non_monotonic` is **bug-only** — legitimate `/admin/replay` resets emit `replay_reset_total`, not this metric. v7 F6-004 reconciled with §8.2 alert guidance.) |
| `open_notebook_notes_per_notebook` | `notebook_id` | Gauge (NEW v7 F6-003) — set on each step 10a fetch; surfaces note-count growth that could degrade single-shot `GET /api/notes` performance |
| `lease_held_seconds` | — | Histogram of lock-held duration (v4 F3-001) — emitted in §7.1 step 11 (terminal release path) |
| `lease_expired_resume_total` | — | Counter: incremented in §7.1 step 7 `"resume"` branch (v4 F3-001) — indicates a prior worker crashed mid-ingest and was recovered via lease expiry |
| `lease_ownership_lost_total` | — | Counter (NEW v5 F4-001) — incremented in §7.1 step 9e ownership-lost abort path; indicates Redis blip or stalled worker losing its lease |
| `replay_reset_total` | — | Counter (NEW v5 F4-005) — legitimate `/admin/replay reset_state=true` paths; separates from `state_cas_rejected_total{reason="non_monotonic"}` corruption signal |

### 8.2 Alerts (Discord-routed)

| Alert | Condition | Severity |
|---|---|---|
| `PocketBridgeWebhookFailureRate` | `rate(webhook_total{result=~"hmac_fail|timestamp_fail|open_notebook_error|body_too_large|payload_field_missing"}[15m]) > 0.1` | warning |
| `PocketBridgeOpenNotebookDown` | `open_notebook_up == 0` for > 5m | critical |
| `PocketBridgeRedisDown` | `redis_up == 0` for > 5m | critical |
| `PocketBridgeStaleCacheUnrecoverable` | `increase(notebook_ensure_total{result="stale_cache_unrecoverable"}[15m]) > 0` | warning |
| `PocketBridgePartialIngest` | `increase(open_notebook_write_total{operation="source_post",result="success"}[15m]) > increase(open_notebook_write_total{operation=~".*note_post",result="success"}[15m]) / 2` | warning |
| `MacOllamaUnreachable` | Black-box probe from cluster to `mac-ollama.ollama-egress.svc:11434/api/tags` fails > 10m | warning |
| `PocketBridgeStateCASNonMonotonic` (v3, scoped v6 F5-005) | `increase(state_cas_rejected_total{reason="non_monotonic"}[15m]) > 0` — **any increment is a real bug.** Legitimate `/admin/replay` resets emit `replay_reset_total` (not this metric); the `non_monotonic` reason is reserved for true state-machine corruption or logic errors. No alert suppression needed. | warning |
| `PocketBridgeFrequentLeaseResume` (v4) | `increase(lease_expired_resume_total[1h]) > 3` — indicates a worker is repeatedly crashing mid-ingest | warning |
| `PocketBridgeLeaseOwnershipLost` (NEW v5) | `increase(lease_ownership_lost_total[15m]) > 0` — bridge or Redis instability; UUID fencing detected split-brain | warning |
| `PocketBridgeNotebookNoteCount` (NEW v7 F6-003) | `max(open_notebook_notes_per_notebook) > 5000` — single-shot GET /api/notes degrading; cap or paginate before 10K | warning |
| `PocketBridgeNoScrape` (NEW v3, F2-004 belt-and-braces) | `up{service="pocket-bridge-metrics"} == 0` for > 5m | critical |

All alert `description:` annotations include the bridge name + namespace per [[feedback_discord_bridge_renders_description_only]].

**Deferred** (F22): `PocketBridgeWebhookSilence` — needs Pocket usage baseline; reopens 2 weeks post-Phase 4.

## 9. Security posture

- **Inbound public surface**: only `/webhook/pocket` at all three layers (Pangolin path allowlist, Traefik strict-match, FastAPI router). All other paths 404 at edge.
- **Body size limit**: 1 MB at Pangolin / Traefik / FastAPI before body read (F12)
- **HMAC verification**: `hmac.compare_digest` over raw-body bytes; signature/timestamp header format validated before comparison (F13)
- **`/admin/replay`**: admin port :8081, no IngressRoute, NetworkPolicy in-namespace ingress only; kubectl port-forward required (D12, F11)
- **`/metrics`**: metrics port :8082, no IngressRoute, NetworkPolicy ingress from `kube-prometheus-stack` ns only (D15, F2-004)
- **Egress NetworkPolicy** (v3 F2-005): DNS to kube-system :53 (UDP+TCP); TCP 5055 to `open-notebook` ns; TCP 11434 to `ollama-egress` ns; TCP 443 to `0.0.0.0/0` for Pocket API
- **Outbound to Mac-Ollama**: via `mac-ollama.ollama-egress.svc` egress proxy. Tailscale ACL: `tag:k8s-egress → tag:mac-ollama:11434` only (F14). **NetworkPolicy in `tailscale` ns** (v3 F2-003) on the operator-created proxy Pod, ingress allowlist `open-notebook` + `open-webui` only.
- **ServiceAccount**: bridge Deployment `automountServiceAccountToken: false` (v3 F2-009); bridge never calls Kubernetes API
- **Secrets**: all 4 tokens in `pocket-bridge-secret.sops.yaml`, age-encrypted, verified `ENC[` before commit per [[feedback_sops_suffix_not_guarantee_encryption]]
- **Bridge container**: non-root, read-only root filesystem, no Linux capabilities, `seccompProfile: RuntimeDefault`
- **Startup checks**: refuse to start if `POCKET_WEBHOOK_SECRET` or `REPLAY_ADMIN_TOKEN` is empty (F8, F11)

## 10. Manual-export fallback runbook (F4)

`docs/runbooks/pocket-bridge-manual-export.md` references `scripts/pocket-manual-ingest.sh`. Both ship in the same PR as Phase 1.

```bash
./scripts/pocket-manual-ingest.sh \
    --pocket-export ~/Downloads/recording-export.json \
    --notebook-id notebook:abcdef \
    --open-notebook https://notebook.lab.mainertoo.com \
    --bearer "$OPEN_NOTEBOOK_KEY"
```

Script mirrors bridge's source + 2 notes POST. Uses `/api/sources/json` with `embed: true`. **Embeds the exact same D16 title markers as the bridge** (v6 F5-006):
- Source title: `<derived_title> [pocket-id:<recording_id>]`
- Summary note title: `Summary [pocket-id:<recording_id> kind:summary]`
- Action-items note title: `Action items [pocket-id:<recording_id> kind:action_items]`

Without the per-kind suffix on notes, a later bridge ingest would claim the source but fail to find these notes via per-kind marker lookup (§7.1 step 10b), creating duplicate notes.

## 11. Out of scope (deferred)

- **Multi-tag note attachment** — notes are single-notebook only.
- **Pocket audio download** — defer to v1.5.
- **Cross-bridge dedup** — single replica; if scaling to 2+, the Lua CAS pattern still works because Redis is shared.
- **Pocket webhook subscription via API** — configured manually.
- **Open Notebook auth enablement** — bridge supports either state transparently.
- **F22 webhook-silence alert** — deferred until 2 weeks of baseline.
- **F2-008 silent async-embedding degradation** — deferred to v1.5. Will add a CronJob walking sources from last 24h via `GET /api/sources/{id}/status`, alerting on `embedded == false` after N minutes.

## 12. Risks and unknowns (refreshed for v3)

| # | Risk | Mitigation |
|---|---|---|
| R1 | Pocket webhook delivery delay > 5min | Start at 5min; widen via env-var if `webhook_total{result="timestamp_fail"}` shows drops |
| R2 | Mac sleeps despite caffeinate + pmset | `MacOllamaUnreachable` alert + F7 behavior verified in Phase 0 |
| R3 | Open Notebook schema drifts | Pinned fixture; contract tests; re-probe on upgrade |
| R4 | Qwen 3.6 tag ambiguity if Alibaba ships a real "Qwen3.6" | Document `ollama show` output as authoritative pin |
| R5 | Tag explosion | Monitor; add normalization if pathological |
| R6 | Redis AOF corruption | Tolerated (30d window); no recovery |
| R7 | GHA over-triggers | Path filter `apps/base/pocket-bridge/{src,contracts}/**` |
| R8 | Pangolin route mis-configuration | Path allowlist verified in Phase 3 |
| R9 | Tailscale operator + Mac connectivity issue blocks Phase 0 | Phase 0 step 9 acceptance gate (both directions) |
| R10 | Pocket payload field-name change post-deployment | `--capture-fixture` available in main.py; CI unit test catches deviation |
| R11 | F7 — Open Notebook does not queue on provider unavailability | Documented in Phase 0; if fail-loud: replay is primary recovery |
| **R12 (v3)** | Lua script error or Redis CLUSTER mode disagreement | Single-Redis sidecar (not cluster); all 4 Lua scripts unit-tested in CI against ephemeral redis-test container |
| **R13 (v3, hardened v4)** | Tailscale operator places egress proxy Pod with unexpected label | Phase 0b1 captures actual labels before Phase 0b2 commits NetworkPolicy; explicit acceptance gate after Phase 0b2 |
| **R14 (NEW v4)** | Lease too short — long async source POST (>60s) loses lock mid-flight | `refresh_lock.lua` extends EXPIRE every 20s during source POST; if Open Notebook itself stalls >60s with no refresh thread alive, the lease expires and another retry takes over (correct behavior). Histograms via `lease_held_seconds` surface this trend. |
| **R15 (NEW v4)** | `/admin/replay` GET-before-reuse race vs concurrent UI deletion | If user deletes a note BETWEEN replay's GET and POST steps, replay would resume with a stale ID. Acceptable for an admin tool — not in v1 scope. v1.5 may add a `replay_round_check` re-verification after note POST. |

## 13. Implementation checklist

Each line is a discrete PR-sized chunk. Sequential within phase; phases are sequential.

- [ ] Phase 0a — Mac launchd / pmset / Tailscale ACL (no repo change)
- [ ] Phase 0b1 — `apps/base/mac-ollama-egress/` Service + Namespace PR (D11, F10) — no NetPol yet
- [ ] Phase 0b2 — Add `tailscale-proxy-networkpolicy.yaml` with VERIFIED selector label (v4 F3-003)
- [ ] Phase 0c — `apps/base/open-webui/` PR (F6)
- [ ] Phase 0d — Open Notebook credential + model registration (curls only)
- [ ] Phase 0e — F7 verification documented in this doc § Phase 0
- [ ] Phase 1a — `apps/base/pocket-bridge/contracts/open-notebook-2026-05-28.json` (F18) + GHA workflow PR
- [ ] Phase 1b — `apps/base/pocket-bridge/src/` (three-port, atomic Lua, conservative payload mapping) + `scripts/pocket-manual-ingest.sh` (F4) PR
- [ ] Phase 2 — `apps/base/pocket-bridge/` manifests PR with real SOPS secrets, three Services, full NetworkPolicies (F8, F11, F2-004, F2-005, F2-009)
- [ ] Phase 3 — Pangolin route with path allowlist (no repo change)
- [ ] **Phase 3a — Pocket fixture capture + mapping verification (NEW v3 F2-006)**
- [ ] Phase 4a — Pocket webhook URL flip, full delivery enabled
- [ ] Phase 4b — Tagged, untagged, concurrency, long-recording, replay acceptance tests
- [ ] Post-Phase-4 — 2 weeks of telemetry, then F22 evaluation + v1.5 scoping (F2-008 poller, audio ingestion)

---

## Appendix A — Codex review pass log

### Pass 7 (2026-05-28) — **CONVERGED**: 2 findings (1 Medium + 1 Low, no Critical or High)

| ID | Class | Dim | Disposition |
|---|---|---|---|
| F7-001 | Medium | Lease lifecycle | Folded into v7 — `/admin/replay unbounded_scan` flow calls `refresh_lock` every 5 pages AND immediately before any POST that follows; prevents lease expiry during long unbounded scans (§7.3 step 10) |
| F7-002 | Low | Implementation clarity | Folded into v7 — step 9e annotation explicitly notes that handler updates local `current_state` variable after each successful `advance_state.lua` call (§7.1 step 9e) |

**Codex implementation-readiness verdict** (Pass 7):
> "§7 provides enough detail for an engineer to implement the bridge state machine, locking, idempotency lookup, replay path, and metrics without needing to invent core behavior. The phased rollout is coherent and gated: Tailscale egress before dependent apps, secrets before public ingress, fixture capture before real recordings, and observability verified before external exposure."

### Pass 6 (2026-05-28) — 5 findings (1 High + 3 Medium + 1 Low, no Critical)

| ID | Class | Dim | v7 disposition |
|---|---|---|---|
| F6-001 | High | State machine | Accepted — state-aware advance: bridge checks current_state before each `advance_state` call; skip if at-or-past. Lua scripts stay strict one-step-forward. Handles claim-existing (no state change needed) and create-new (state advance needed) paths uniformly. (§7.1 steps 9e, 10c, 11) |
| F6-002 | Medium | Idempotency wording | Accepted — D16 reworded from "true exactly-once" to "bounded idempotency for recent recordings (~500 sources)". `/admin/replay unbounded_scan=true` for old-recording recovery. (D16, §7.1 step 9b, §7.3) |
| F6-003 | Medium | Scale instrumentation | Accepted — `open_notebook_notes_per_notebook` gauge; `PocketBridgeNotebookNoteCount` warning at 5000 notes (§7.1, §8.1, §8.2) |
| F6-004 | Medium | Doc consistency | Accepted — §8.1 row updated: `non_monotonic` is bug-only; resets emit `replay_reset_total` (§8.1) |
| F6-005 | Low | Editorial | Accepted — version tags removed from §7.1, §7.3, §7.6 subsection headers (preserved in change-log header) |

### Pass 5 (2026-05-28) — 7 findings (3 High + 3 Medium + 1 Low, no Critical)

| ID | Class | Dim | v6 disposition |
|---|---|---|---|
| F5-001 | High | Internal consistency | Accepted — Phase 1 Lua snippet replaced with UUID-fenced form matching D14/§7.6 (§5 Phase 1) |
| F5-002 | High | Lock fencing | Accepted — `advance_state.lua` "OR lock absent" branch removed; strict UUID match for all transitions (§7.6) |
| F5-003 | High | Schema contract | Accepted — `GET /api/sources` (paginated, 100/page, 5-page cap) and `GET /api/notes` (single-shot per notebook) pinned in §6 with full query semantics (§6, §7.1) |
| F5-004 | Medium | Observability | Accepted — `replay_total` label list adds `lock_held` and `already_complete` (§8.1) |
| F5-005 | Medium | Alert consistency | Accepted — `PocketBridgeStateCASNonMonotonic` annotation updated: any increment is a real bug; legitimate replay resets go to `replay_reset_total` (§8.2) |
| F5-006 | Medium | Idempotency | Accepted — §10 manual-ingest script must use both source marker AND per-kind note markers (§10) |
| F5-007 | Low | Editorial | Accepted — UUID generation pinned to bridge side; Lua stores and echoes (D14, §7.1 step 7, §5 Phase 1 script comments) |

### Pass 4 (2026-05-28) — 6 findings (2 High + 4 Medium, no Critical)

| ID | Class | Dim | v5 disposition |
|---|---|---|---|
| F4-001 | High | Distributed locks | Accepted — UUID-fenced lease; all 4 Lua scripts take owner_uuid arg and verify ownership before mutating; worker abort on ownership-lost (D14 hardened, §7.1 step 9e, §7.6) |
| F4-002 | High | Idempotency | Accepted — title-marker `[pocket-id:<id>]` embedded in sources and notes (with `kind:` suffix for notes); pre-create lookup via GET-and-filter before every POST in §7.1 steps 9b + 10a (D16, §10) |
| F4-003 | Medium | Replay safety | Accepted — replay reads pocket:lock first; 409 unless `force_delete_lock=true`; logs prominently (§7.3 step 4) |
| F4-004 | Medium | Phase 0 gate | Accepted — Phase 0b1 step 9 uses explicit count check + fail-closed when != 1 matching Pod (§5 Phase 0b1) |
| F4-005 | Medium | Observability | Accepted — distinct `replay_reset_total` metric for legitimate reset paths; `state_cas_rejected_total{reason="non_monotonic"}` scoped to true logic bugs (§7.3, §8.1, §8.2) |
| F4-006 | Medium | Observability | Accepted — explicit metric emission anchors at §7.1 steps 7/9e/11 and §7.6 ownership-lost path; new metrics fully traceable to code paths (§7.1, §7.6, §8.1) |

### Pass 3 (2026-05-28) — 4 findings (1 High + 3 Medium, no Critical)

| ID | Class | Dim | v4 disposition |
|---|---|---|---|
| F3-001 | High | Concurrency/recovery | Accepted — lease (60s TTL) separated from monotonic state; resume-aware handler; `acquire_and_dispatch.lua` returns `{dedup,in_progress,resume,start}`; lease refresh during long ops; release on complete (D14 revised, §7.1, §7.6 new) |
| F3-002 | Medium | Idempotency | Accepted — `/admin/replay` GETs each cached ID before reuse; 404 → clear stale ID + create fresh; explicit allowance for `reset_state=true` against `complete` state (§7.3) |
| F3-003 | Medium | Security | Accepted — Phase 0b split into 0b1 (Service + label capture) and 0b2 (NetPol with verified selector); explicit acceptance gate between (§5 Phase 0, §13) |
| F3-004 | Medium | Observability | Accepted — each Lua return path tied to specific `state_cas_rejected_total{reason}` increment; `non_monotonic` scoped to replay reset paths; `PocketBridgeStateCASNonMonotonic` alert clarified (§7.1, §8.1, §8.2) |

### Pass 2 (2026-05-28) — 9 findings (5 High + 4 Medium, no Critical)

| ID | Class | Dim | v3 disposition |
|---|---|---|---|
| F2-001 | High | Concurrency | Accepted — atomic Lua CAS replaces read-then-write (D14, §7.1 step 7, §4 src/lua/state_cas.lua) |
| F2-002 | High | Concurrency | Accepted — Lua script enforces monotonic transitions via `allowed_prior_states` arg list (D14, §7.1) |
| F2-003 | High | Security | Accepted — NetworkPolicy moved to `tailscale` ns selecting operator-created Pod by label; ingress allowlist `open-notebook` + `open-webui` only (§4.1, §9, R13) |
| F2-004 | High | Observability | Accepted — three-port architecture; metrics on :8082 with own NetworkPolicy carve-out for `kube-prometheus-stack` ns. `PocketBridgeNoScrape` alert added as belt-and-braces (D15, §3, §4, §8) |
| F2-005 | High | NetworkPolicy | Accepted — explicit DNS egress to kube-system :53 added; verified in Phase 2 step 9 (§5 Phase 2, §9) |
| F2-006 | Medium | Phase ordering | Accepted — Phase 3a added between Phase 3 and Phase 4; Phase 1 ships conservative defaults; Phase 3a captures fixture via Pocket dashboard test event (§5) |
| F2-007 | Medium | Schema | Accepted — per-endpoint success codes pinned in §6 (`/api/sources/json` 200, `/api/notebooks` 201, `/api/notes` 200, `/api/credentials` 201); bridge accepts only the pinned code per endpoint |
| F2-008 | Medium | Observability | **Deferred to v1.5** — silent async-embedding degradation documented in §7.2 + §11; poller CronJob planned for v1.5 |
| F2-009 | Medium | Security | Accepted — `automountServiceAccountToken: false` in §9 + §5 Phase 2 |

### Pass 1 (2026-05-28) — 24 findings (1 Critical + 9 High + 14 Medium)

| ID | Class | Dim | v2 disposition |
|---|---|---|---|
| F1 | High | Omission | Accepted in v2 — four-state ingest machine (§7.1); strengthened in v3 with atomic Lua CAS (D14) |
| F2 | Medium | Omission | Accepted — `Pocket Inbox` default (D3, §7.5) |
| F3 | Medium | Omission | Accepted — fixture capture (relocated to Phase 3a in v3) |
| F4 | Medium | Omission | Accepted — `scripts/pocket-manual-ingest.sh` |
| F5 | High | Oversight | Accepted — `/healthz` cluster-only on admin port |
| F6 | Medium | Oversight | Accepted — single OpenAI-compatible mode for Open WebUI |
| F7 | Medium | Oversight | Accepted — verified in Phase 0 step 16; R11 tracks outcome |
| F8 | High | Ordering | Accepted — real secret in Phase 2; startup refuses empty |
| F9 | Medium | Ordering | Accepted — placeholder URL in Phase 2; real URL flip in Phase 4 |
| F10 | High | Ordering | Accepted — `apps/base/mac-ollama-egress/`; Phase 0 acceptance gate |
| F11 | **Critical** | Security | Accepted — dual ports (now triple in v3); strict path; Pangolin allowlist; startup checks |
| F12 | High | Security | Accepted — 1 MB at Pangolin/Traefik/FastAPI |
| F13 | High | Security | Accepted — header validation + `hmac.compare_digest` |
| F14 | High | Security | Accepted — Tailscale ACL + cluster NetworkPolicy (v3 fixed NetPol placement) |
| F15 | Medium | Security | Accepted — replay fetches Pocket first |
| F16 | High | Schema | Accepted — `embed: true` (D13) |
| F17 | Medium | Schema | Accepted — `["notebook:<id>"]` wording |
| F18 | Medium | Schema | Accepted — `contracts/open-notebook-2026-05-28.json` |
| F19 | Medium | Schema | Accepted — `/api/models/defaults` schema probed in Phase 0 step 11-12 |
| F20 | High | Observability | Accepted — `open_notebook_up` gauge + `open_notebook_ping_total{result}` |
| F21 | Medium | Observability | Accepted — bridge-owned `redis_up` |
| F22 | Medium | Observability | Deferred — webhook-silence alert needs baseline |
| F23 | Medium | Observability | Accepted — `timestamp_fail` in failure alert |
| F24 | Medium | Observability | Accepted — per-operation write counters |
