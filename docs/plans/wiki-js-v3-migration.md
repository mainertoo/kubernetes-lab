# Wiki.js 2.x → 3.x migration (side-by-side)

> **Status 2026-11-11: cut over (PR C).** `wiki.lab.mainertoo.com` serves v3; `wiki-v3.lab`
> stays as an alias until v2 is retired. v2 is scaled to 0 and reachable at `wiki-v2.lab`
> if scaled back up for rollback. MkDocs git-syncs branch `v3` for its pages; its
> `mkdocs.yml` + asset hook moved into this repo (`apps/base/mkdocs/site-config/`), because
> Wiki.js force-pushes `v3` and deleted them when they were copied onto the branch (the
> runbook's "push mode keeps files it doesn't own" was wrong). `scripts/wiki/configure-git-storage.py`
> was retired; `docs/wiki-git-storage.md` describes v3. Next: retire v2 after ~2 weeks
> (step 6), first moving `WIKI_GIT_SSH_KEY` into `apps/base/wiki/wiki-secret.sops.yaml`.

## Problem

Renovate's bump to `ghcr.io/requarks/wiki:3.0.2` (PR #2020) crash-looped prod on
2026-11-03: `Your PostgreSQL database version (15) is too old … Requires >= 17`. Rolled
back to `2.5.317` in #2048; v3 majors are now held in `renovate.json`.

Upgrading `wiki-js-cnpg-db` to PG17 would **not** have fixed it. Per the upstream
[upgrade guide](https://docs.js.wiki/setup/upgrade), *"an in-place upgrade is not
possible"* from 2.x. v3 is a rewrite that needs:

- PostgreSQL **17 or 18**, with `ltree` + `pg_trgm` (both trusted contrib extensions,
  present in the `cloudnative-pg/postgresql` images), on an **empty** database.
- Content moved via a **`.wkbackup` package**: 2.x *Admin → Utilities → Export for
  Wiki.js 3.x* (exporter shipped in 2.5.316, so 2.5.317 has it), then v3 *Admin →
  Utilities → Import from Wiki.js 2.x*. The import runs in the browser against the v3
  REST API and is idempotent (existing items are skipped unless *Overwrite on conflict*).
- **Not imported:** module settings (storage/**git**, **authentication**, comments,
  analytics), security settings, site logo, site hostname. These are re-done by hand.
- No way back: v3 has no 2.x export.

So the plan is a parallel v3 install, then an import, then a hostname cutover. The v2
stack stays untouched until the soak period ends, which keeps rollback a one-line
IngressRoute revert.

## What changes for us in v3

| Area | 2.x today | 3.x |
|---|---|---|
| DB | `wiki-js-cnpg-db`, PG15, schema `public` | new PG17 cluster, schema `wiki` (`DB_SCHEMA`) |
| Data volume | `wiki-js-data` cephfs 20Gi (`/wiki/config`, `/wiki/data`: git checkout + `git-ssh.pem`) | **none needed**: git storage commits and pushes per sync with no persistent volume; assets live in the DB |
| API | GraphQL `/graphql` (`wikijs-update` skill, `scripts/wiki/configure-git-storage.py`) | **REST `/_api`** (OpenAPI 3.0). ⚠️ API keys act for a group and **can't save, move, delete or render pages, or run storage actions**: those need a user session or PAT |
| Git mirror | `mainertoo/wiki` `main`, bidirectional, SSH deploy key | same repo, branch **`v3`**, mode **`push`** (force-push), SSH inline key (same deploy key); layout matches `main` with `localePrefix` off, frontmatter is YAML-restyled |
| Auth | local + Authentik OIDC | Local, generic OIDC/OAuth2, LDAP, SAML, GitHub, Google, Entra, Discord; must be **activated per site** |
| Proxy | n/a | enable *Security → Trust X-Forwarded-\* headers*, or callbacks build `http://<internal>` URLs |
| Image | runs as root-ish (init chown for cephfs) | runs as user `wiki`, Node 26, ~850 MB image |

## Naming

New resources take their **permanent names** now (namespace `wiki`, HelmRelease `wiki`,
CNPG `wiki-cnpg-db`), so no rename is needed after cutover. Renaming a Flux
Kustomization prunes its children. The old `wiki-js` namespace is then decommissioned as
a whole.

## Status (2026-11-06)

Phases 0–3 are **done**. v3 is live side by side at `wiki-v3.lab.mainertoo.com`, and it
is now the **only place wiki writes go**: v2 is frozen, and the `wikijs-update` skill's
`wiki.py` refuses writes. Next comes the soak period, then cutover (phase 4).

Decisions taken during phases 2–3. They supersede the original plan text:

- **The git mirror stays on branch `v3`, in `push` mode, permanently.**
  - It does not move to `main`, and it does not become `sync`.
  - v3 *push* is a **force-push** from the wiki, so pointing it at `main` would rewrite
    v2's history there.
  - Nothing edits the `.md` files directly any more, so the wiki is the single source of
    truth.
  - `main` stays as the frozen v2 archive. At cutover, `v3` becomes the repo's default
    branch and every reader switches to it.
- **No final re-import at cutover.**
  - v2 hasn't been edited since the import (2026-11-05 ~14:50Z), and the tooling now
    blocks v2 writes.
  - A re-import with *Overwrite on conflict* would undo the post-import content fixes.
  - Instead, check right before cutover that v2 is still unchanged (query below).
- **No personal access token.** Writes log in as `wiki-admin` (`ADMIN_PASS` in SOPS)
  and use that session. The API key `CLAUDE_API_KEY` is used for reads.

## Phases

### 0. Preflight ✅

v2 baseline:
- 298 pages, 836 history entries, 13 assets
- 3 users, 2 groups, 0 comments
- 1 locale (`en`), DB 36 MB

v2 auth was local + Authentik OIDC (`wiki-js` provider). v2 git storage was `sync` on
`main`, with SSH deploy key `wiki-js-storage`. The trial `.wkbackup` is kept off-repo.

### 1. Stand up v3 alongside v2 (PR A #2080) ✅

The v3 deployment:
- image `ghcr.io/requarks/wiki:3.1.0`
- CNPG `wiki-cnpg-db` on PG 17.6
- namespace `wiki`, host `wiki-v3.lab.mainertoo.com`
- Gatus internal only

The API key secret came in PR #2087.

### 2. Import and configure ✅

- **Import:** the first attempt through Traefik failed with a 502 on the pages stream,
  likely Traefik's 60s `readTimeout`. Re-run through `kubectl port-forward svc/wiki`, it
  succeeded.
- **Validation:**
  - 298/298 pages and 13/13 assets came across. v3 renamed asset files from `_` to `-`
    and rewrote the page links to match.
  - Tags, history and renders all came across.
  - 7 pages from v2's HTML editor became `visual`. `portainer-edge-agent` came across with
    its markdown escaped and its newlines lost; it was restored from v2's source.
- **Not carried over by the import, fixed by hand or API:**
  - **Guests:** the default rule was DENY; it is now ALLOW `read:pages/assets/comments`,
    matching v2.
  - **Login page:** the Authentik strategy had to be added to the site's login page
    (`sites.config.authStrategies`).
  - **Authentik:** the "Provider for wiki-js" got a second redirect URI,
    `https://wiki-v3.lab.mainertoo.com/_api/auth/84f96d89-1231-4533-83f8-ae1d7356201d/callback`.
  - **User accounts:** the import doesn't bring over passwords or SSO links. Existing
    users link to Authentik by email on their first SSO login.
  - **User preferences:** imported users kept only v2's `locale`. A missing
    timezone/date format broke the admin Users page until full prefs were set.
- **Git storage:** pushes to `mainertoo/wiki` branch `v3` in push mode, using the same
  deploy key, every 5 min.
  - The `v3` branch is an orphan with a README root commit. v3 refuses to push to a
    branch that doesn't exist.
  - Site `localePrefix` is off, so the paths match `main`.
- **Branch diff, `v3` vs `main`:** the paths match except for:
  - 7 `.html` → `.md`
  - 11 renamed screenshots
  - `main`'s extra `guides/mkdocs.yml` and `guides/hooks/stage_assets.py`

  Frontmatter carries the same data in a different YAML style: tags as a list, quoted
  dates, folded descriptions.
- **Heading anchors:** v3 is lowercase, drops punctuation, `_` and emoji, collapses
  dashes, and has no `h-` prefix.
  - A full scan found 54 broken `#` links, including some already broken in v2. 52 were
    fixed.
  - Still broken: `house/electrical/panels` → `breaker-loads#mainp1112` and
    `#mainp1718`. Those sections never existed; it's a content gap.

### 3. Tooling ✅

`~/.claude/skills/wikijs-update/wiki3.py` (REST `/_api`) handles list, get, upsert, move,
delete and render.
- **Auth:** the API key for reads, the wiki-admin session for writes.
- **Render:** it always re-renders server-side after a save. v3 doesn't render markdown
  on save, because the browser editor normally sends the HTML.
- **Docs:** `SKILL.md` makes v3 the default.
- **v2 tool:** `wiki.py` is kept for reading v2 only. Writes need
  `WIKI_V2_ALLOW_WRITE=1`.

`scripts/wiki/configure-git-storage.py` (v2 GraphQL) was not ported: v3 storage was set
via `PUT /_api/sites/:site/storage`. Port it or retire it in PR C.

### 4. Cutover (PR C)

1. **Confirm v2 is untouched since the import.** This must return 0 rows:
   ```sql
   -- kubectl -n wiki-js exec wiki-js-cnpg-db-1 -c postgres -- psql -U postgres -d wiki
   select path, "updatedAt" from pages where "updatedAt" > '2026-11-05T14:00:00Z';
   ```
   If it returns rows, port those pages to v3 by hand with `wiki3.py`. **Do not
   re-import.**
2. **v2: disable git storage** so `main` freezes as the v2 archive.
3. **Repo (PR C):**
   - move `wiki.lab.mainertoo.com` to the `wiki` IngressRoute and drop `wiki-v3`
   - move the homepage annotations from `wiki-js` to `wiki`
   - Gatus `SUBDOMAIN: wiki`, and add `components/gatus/external`, which v2 has
   - scale v2 to `replicas: 0`, keeping its DB and PVC for rollback
   - MkDocs git-sync: `GITSYNC_REF: v3` (`apps/base/mkdocs/mkdocs-release.yaml`)
   - port or retire `scripts/wiki/configure-git-storage.py`, and rewrite
     `docs/wiki-git-storage.md` for v3: branch `v3`, push-only, no PVC
4. **`mainertoo/wiki` repo:**
   - copy `guides/mkdocs.yml` and `guides/hooks/stage_assets.py` from `main` to `v3`.
     Push mode builds on the remote branch, so files the wiki doesn't own survive.
   - check that `stage_assets.py` copes with the `-` asset names
   - make `v3` the default branch
5. **Authentik:** add redirect URI
   `https://wiki.lab.mainertoo.com/_api/auth/84f96d89-1231-4533-83f8-ae1d7356201d/callback`.
   Remove the v2 and `wiki-v3` URIs once SSO is verified.
6. **Readers and tools:**
   - `wiki3.py` default `BASE` becomes `https://wiki.lab.mainertoo.com`
7. **Verify:**
   - Gatus is green
   - SSO login works on `wiki.lab`
   - anonymous read works
   - a `wiki3.py` round-trip on a `sandbox/` page lands in `v3` within 5 min
   - the MkDocs guides site rebuilds
   - the next 03:30 vault-mirror run is clean

**Obsidian / Hermes vault mirror ✅ (done 2026-11-06, ahead of cutover).** The Spark's
`~/scripts/wiki-vault-mirror.sh` (cron 03:30) now reads branch **`v3`**:
- it uses `fetch` + `checkout -B` + `reset --hard origin/v3`, so a force-push can't wedge it
- it authenticates over SSH with a dedicated **read-only** deploy key,
  `spark-wiki-vault-mirror (ro)` (`~/.ssh/wiki_mirror_ro`)

It had been failing since 2026-10-31 on an expired `gh` token behind its HTTPS remote.
The first run brought the vault from 290 to 299 pages. The backup of the old script is
`~/scripts/wiki-vault-mirror.sh.bak-2026-11-06`. Nothing more to do at cutover.

### 5. Soak and decommission (PR D, ≥ 2 weeks after cutover)

- Keep a last-resort copy:
  - the final CNPG backup of `wiki-js-cnpg-db`
  - the last v2 `.wkbackup`
- Remove from the repo:
  - `apps/production/wiki-js/`
  - `apps/base/wiki-js/`
  - its `apps/production/kustomization.yaml` entry
- Clean up VolSync leftovers for `wiki-js-data`. Prune leaves the Kyverno-generated
  Secret, RS, RD and S3 repo behind (see the volsync decommission memory).
- Retire deploy key `wiki-js-storage` only if v3 has its own. Today v3 **reuses** it, so
  keep it.
- Drop the Renovate `requarks/wiki` major hold: it only matters for 2.x, and future 4.x
  majors should be reviewed normally.
- Optionally delete `wiki.py` (v2) from the skill.

## Rollback

Until PR D, rollback means restoring the IngressRoute host to the `wiki-js` route and
scaling v2 back to 1. The v2 DB and PVC are never touched.

Edits made on v3 since the import don't exist on v2, and there is no v3 → v2 export. That
covers everything written since 2026-11-06, because writes moved to v3 that day. Those
edits survive in the git history of branch `v3` and could be re-applied to v2 by hand.

## Open questions

All three original questions are resolved: v2 auth was local + Authentik, writers are
frozen with no freeze window needed, and the repo is kept with v3 on its own branch.
