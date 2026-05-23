# Wiki-js information architecture

Source of truth for the `wiki.lab.mainertoo.com` page tree. The wiki itself can be re-derived from this doc — keep them in sync.

> Built 2026-05-23 from a 14-page legacy state via the wiki-js GraphQL API. Initial population executed in three phases (A: moves + content relocations, B: section overviews, C: leaf stubs). All page IDs preserved on moves, so history and comments survive.

## Top-level tree

```
home                                              Mainertoo Homelab Wiki (landing + section index)
│
├── infrastructure/                               what exists, how it's wired
│   ├── hardware/
│   │   ├── proxmox-nodes                         mammoth, whistler, zermatt, mac, ugreen
│   │   ├── networking-physical                   switches, TB4 mesh, UDM, VLANs
│   │   └── nas-storage                           QNAP + UGREEN zbackup layout
│   ├── software/
│   │   ├── k3s                                   v1.35.5+k3s1, HA topology
│   │   ├── flux                                  reconciliation model, prune=true blast radius
│   │   ├── ceph                                  pools, storage classes, tuning
│   │   ├── traefik                               IngressRoute pattern, Authentik middleware
│   │   └── controllers-index                     one-liner per controller
│   ├── networking/
│   │   ├── dns-tailscale-split                   ★ split-DNS architecture (pre-filled)
│   │   ├── adguard-internal                      ★ 192.168.1.50, *.lab + *.staging zones (pre-filled)
│   │   ├── metallb-ipam                          address pools, L2 announcements
│   │   └── kube-vip                              control-plane VIP
│   └── external-access/
│       ├── pangolin                              ★ outside-services front door (pre-filled)
│       ├── cloudflare-tunnels                    ★ cloudflared + newt (pre-filled)
│       └── authentik-sso                         Traefik middleware pattern
│
├── apps/                                         catalogue + per-app nuances
│   ├── overview                                  app-template, label-driven backups
│   ├── media/
│   │   ├── plex
│   │   ├── jellyfin
│   │   ├── sonarr-radarr-prowlarr
│   │   └── nzbdav
│   ├── productivity/
│   │   ├── joplin
│   │   ├── wiki-js
│   │   ├── opencut
│   │   └── sparky-fitness
│   ├── infra-adjacent/
│   │   ├── authentik
│   │   ├── gatus
│   │   └── garage
│   ├── home-iot/
│   │   ├── home-assistant
│   │   ├── esphome
│   │   └── matter-server
│   └── nuances                                   cross-cutting deployment gotchas
│
├── smart-home/                                   Home Assistant–centric
│   ├── home-assistant-overview
│   ├── automations                               folder — one page per automation
│   ├── devices/
│   │   ├── matter
│   │   ├── zwave
│   │   ├── zigbee
│   │   ├── esphome-yaml
│   │   └── intel-gpu-frigate
│   ├── notifications/
│   │   ├── mobile
│   │   ├── notifiarr
│   │   └── ntfy
│   └── lessons-learned
│
├── ai/                                           local AI build-out
│   ├── overview
│   ├── inference/
│   │   ├── ollama
│   │   ├── vllm
│   │   └── local-llm-catalogue
│   ├── ui/
│   │   ├── open-webui
│   │   └── librechat
│   ├── agents/
│   │   ├── openclaw                              ★ openclaw.ai (pre-filled stub)
│   │   └── hermes-agent                          ★ hermes-agent.nousresearch.com (pre-filled stub)
│   ├── rag-and-memory
│   └── integrations/
│       ├── home-assistant-llm
│       └── claude-desktop-mcp
│
├── backup-and-recovery/                          10-layer system
│   ├── architecture                              80 KB Backup System Complete Reference (relocated from legacy)
│   ├── runbooks/
│   │   ├── single-pvc-restore
│   │   ├── cnpg-pitr
│   │   ├── cluster-nuke
│   │   └── total-ceph-loss
│   ├── operator-laptop-secrets                   ★ mirrors docs/backup-architecture.md §8b (pre-filled)
│   ├── kopia-internals
│   └── label-driven-backups                      ★ pre-filled
│
├── gitops/                                       cluster-as-code mechanics
│   ├── flux-flow
│   ├── apps-pattern
│   ├── sops-secrets
│   ├── renovate
│   └── ci-flux-local
│
├── operations/                                   day-2 runbooks
│   ├── k3s-upgrade
│   ├── node-drain-and-reset
│   ├── ceph-scrub-queue
│   ├── descheduler
│   └── common-incidents/
│       ├── 2025-12-27-pve-mammoth-root-disk-full (relocated)
│       └── multi-pvc-restore-pod-recipe          (relocated)
│
├── postgres-cnpg/                                eight CNPG clusters
│   ├── migration-history
│   ├── disaster-recovery
│   ├── dr-flip-script
│   └── per-app-quirks
│
├── docs/                                         placeholder for wiki-js Git Storage sync
│
├── reference/                                    lookup material
│   ├── completed-projects/
│   │   └── 2026-02-plex-volsync-v2-migration    (relocated)
│   ├── decisions-log
│   └── glossary
│
└── archive/                                      historical pages, no longer current
    ├── 2025-narrative-welcome                    (relocated, narrative paragraph lifted into home)
    ├── 2025-server-setup/
    │   ├── portainer-edge-agent                  (relocated)
    │   ├── qnap-docker-access                    (relocated)
    │   └── tsdproxy                              (relocated)
    └── 2026-backup-drafts/
        ├── backup-strategy-html-summary          (relocated)
        ├── hephaestus-volsync-restic-runbook     (relocated; Restic-era, superseded)
        ├── jellyfin-restic-restore-playbook-v1   (relocated)
        ├── jellyfin-restic-restore-playbook-v2   (relocated)
        └── sparky-fitness-restore-test           (relocated)
```

★ = pages pre-filled with substantive content rather than stubs. Everything else is a stub awaiting content.

## Old → new page mapping

The 14 legacy pages were either relocated or archived (no deletes). All page IDs preserved.

| Legacy path | New path | Disposition |
|---|---|---|
| `home` (id 6) | `home` (rewritten) | New landing w/ section grid + intent paragraph lifted from id 1 |
| `backup/proxmox_fix` (id 1) | `archive/2025-narrative-welcome` | Voice preserved on landing; original archived |
| `Server_Setup/QNAS_docker_access` (id 2) | `archive/2025-server-setup/qnap-docker-access` | Dead per user |
| `Server_Setup/QNAS_docker_access/tsdproxy` (id 3) | `archive/2025-server-setup/tsdproxy` | Dead per user |
| `Server_Setup/Portainer-edge-agent` (id 4) | `archive/2025-server-setup/portainer-edge-agent` | Same era as QNAS, archived |
| `backup/intro` (id 5) | `operations/common-incidents/2025-12-27-pve-mammoth-root-disk-full` | Real incident report — retitled |
| `backup/backup_strategy` (id 7) | `archive/2026-backup-drafts/backup-strategy-html-summary` | Superseded by id 14 |
| `Restoring_in_cluster` (id 8) | `archive/2026-backup-drafts/sparky-fitness-restore-test` | Early-volsync test note |
| `backup/jellyfin-restore` (id 9) | `archive/2026-backup-drafts/jellyfin-restic-restore-playbook-v1` | Restic-era, superseded |
| `backup/Plex-migration-Volsync-v2-backup_restore` (id 10) | `reference/completed-projects/2026-02-plex-volsync-v2-migration` | Historical project — retitled |
| `backup/Jellyfin-restore-AI` (id 11) | `archive/2026-backup-drafts/jellyfin-restic-restore-playbook-v2` | Restic-era, superseded |
| `backup/backup-restore-playbook` (id 12) | `operations/common-incidents/multi-pvc-restore-pod-recipe` | Useful recipe — retitled |
| `backup/claude-code-restore-runbook` (id 13) | `archive/2026-backup-drafts/hephaestus-volsync-restic-runbook` | Restic-era, superseded by docs/backup-recovery.md |
| `backup/Backup_Strategy_and_Layout_Claude` (id 14) | `backup-and-recovery/architecture` | The canonical 80 KB reference — retitled |

## Pre-filled pages (the ★ entries above)

These eight pages were created with real content rather than stubs because the user either explicitly asked for them or the content was already canonical elsewhere in the repo:

| Page | Source / why pre-filled |
|---|---|
| `infrastructure/networking/dns-tailscale-split` | User explicitly requested DNS section; captures Tailscale split-DNS architecture |
| `infrastructure/networking/adguard-internal` | User explicitly requested AdGuard section; documents 192.168.1.50 LXC + DNS rewrites |
| `infrastructure/external-access/pangolin` | User explicitly requested; outside-services front door + newt connector relationship |
| `infrastructure/external-access/cloudflare-tunnels` | User explicitly requested; cloudflared + newt distinction |
| `ai/agents/openclaw` | User identified openclaw.ai as candidate |
| `ai/agents/hermes-agent` | User identified hermes-agent.nousresearch.com as candidate |
| `backup-and-recovery/operator-laptop-secrets` | Mirrors `docs/backup-architecture.md` §8b (PR #583) |
| `backup-and-recovery/label-driven-backups` | Summarizes `docs/label-driven-backups.md` + Kyverno policy |

## Conventions

- **Path slugs**: lowercase, hyphen-separated. Legacy paths used mixed case and underscores; the reorg normalizes everything to lowercase-kebab.
- **Tags**: `overview` for section landings + folder pages, `stub` for unfilled leaves. Re-tag as content matures.
- **Stub template**: `# Title\n\n> **Status: stub — content to be filled in.**\n\n{description}\n\n---\n\n← Back to **[Parent](/parent/path)**\n`
- **Folder overview template**: `# Title\n\n{description}\n\n## Sub-pages\n\n- [Child](/path) — oneliner\n- ...\n`
- **Archive vs delete**: never delete a page — move to `archive/<year>-<context>/<slug>` instead. Preserves IDs, history, and inbound links.

## How this was built (for re-runs)

All page mutations executed via the wiki-js GraphQL API at `http://127.0.0.1:13000/graphql` (via `kubectl -n wiki-js port-forward svc/wiki-js 13000:3000`). The driver scripts lived in `/tmp/` during the build:

| Script | Phase | What it did |
|---|---|---|
| `/tmp/wiki-reorg-phase-a.py` | A | 12 moves + 4 retitles + 1 home rewrite |
| `/tmp/wiki-reorg-phase-b.py` | B | 11 section overview pages |
| `/tmp/wiki-reorg-phase-c.py` | C | 92 folder overviews + leaf stubs |
| `/tmp/wiki-home-rewrite.py` | (B+C) | Re-rewrote home with table-based section grid + emoji icons |

API token was read from a SOPS-decrypted `WIKI_API_KEY` stored in `apps/base/wiki-js/wiki-js-secret.sops.yaml` (PR #584).

## Refresh discipline

When the wiki tree changes meaningfully (new top-level section, large reorg), update this file in the same PR. Per-page additions don't need a doc update unless they introduce a new sub-tree.

## Open follow-ups

- **Wiki-js Git Storage backend** — enable so the repo's `/docs` folder syncs to the wiki's `docs/` section automatically. Eliminates the manual copy-paste loop and makes wiki content versioned + Renovate-bumpable.
- **PR #584** — lands `WIKI_API_KEY` on master so Flux reconciles it into the live cluster Secret.
- **Stub content** — 80+ leaves are currently stubs. Fill in as the homelab story matures.

## See also

- [`backup-architecture.md`](backup-architecture.md) §8b — operator-laptop secrets, mirrored at `/backup-and-recovery/operator-laptop-secrets` in the wiki.
- [`backup-system-wiki.md`](backup-system-wiki.md) — the 80 KB canonical reference, lives at `/backup-and-recovery/architecture` in the wiki.
- [`label-driven-backups.md`](label-driven-backups.md) — mirrored at `/backup-and-recovery/label-driven-backups`.
