# Wiki.js 2.x → 3.x migration (side-by-side)

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
| API | GraphQL `/graphql` (`wikijs-update` skill, `scripts/wiki/configure-git-storage.py`) | **REST `/_api`** (OpenAPI 3.0). ⚠️ API keys are not tied to a user and **can't save pages**; saving needs a **personal access token** |
| Git mirror | `mainertoo/wiki` `main`, bidirectional, SSH deploy key | `git` storage module still exists (SSH inline key, `sync`/`push`/`pull`), but the on-disk layout and frontmatter may differ |
| Auth | (preflight: confirm which strategies are enabled) | Local, generic OIDC/OAuth2, LDAP, SAML, GitHub, Google, Entra, Discord; must be **activated per site** |
| Proxy | n/a | enable *Security → Trust X-Forwarded-\* headers*, or callbacks build `http://<internal>` URLs |
| Image | runs as root-ish (init chown for cephfs) | runs as user `wiki`, Node 26, ~850 MB image |

## Naming

New resources take their **permanent names** now (namespace `wiki`, HelmRelease `wiki`,
CNPG `wiki-cnpg-db`), so no rename is needed after cutover. Renaming a Flux
Kustomization prunes its children. The old `wiki-js` namespace is then decommissioned as
a whole.

## Phases

### 0. Preflight (after #2048 is merged and v2 is healthy)

1. **Inventory v2**, read-only, to set the validation baseline: counts of `pages`,
   `pageHistory`, `assets`, `users`, `groups`, `comments`; enabled rows in
   `authentication` and `storage`; distinct `localeCode`; DB size.
2. **Fresh backups:**
   - on-demand CNPG `Backup` of `wiki-js-cnpg-db`
   - a manual volsync trigger for `wiki-js-data`
   - confirm `mainertoo/wiki` `main` has a recent commit, which proves git sync is live
3. **Trial export:** in v2 *Admin → Utilities → Export for Wiki.js 3.x*, all
   checkboxes on, download the `.wkbackup`. It contains users and password hashes:
   keep it **off-repo** (kubernetes-lab is public), e.g. the Mac or QNAP.

### 1. Stand up v3 alongside v2 (PR A)

- `apps/production/wiki/db-cnpg.yaml`: Flux Kustomization `wiki-db-cnpg` using
  `components/cnpg-cluster/{base,initdb}`, with `APP: wiki-cnpg-db`,
  `CNPG_DB_NAME: wiki`, `CNPG_DB_OWNER: wiki`,
  `CNPG_IMAGE: ghcr.io/cloudnative-pg/postgresql:17.6-bookworm`, 5Gi `ceph-rbd`,
  1 instance, its own backup slot, and a fresh lineage (`v1`, no restore source).
- `apps/base/wiki/`: namespace, bjw-s `app-template` HelmRelease, IngressRoute on
  **`wiki-v3.lab.mainertoo.com`** (temporary host), and a SOPS secret with
  `ADMIN_EMAIL` / `ADMIN_PASS` for root-admin bootstrap.
  - `image: ghcr.io/requarks/wiki:3.0.2`, `WIKI_PORT: 3000`, `TZ`, `DB_HOST:
    wiki-cnpg-db-rw`, `DB_NAME`/`DB_USER`, `DB_PASS` from `wiki-cnpg-db-app`.
  - No PVC, no init container.
  - Start at 256Mi request / 1.5Gi limit and tune after import.
- `apps/production/wiki/app.yaml`: `dependsOn: wiki-db-cnpg`, Gatus internal and
  external with `SUBDOMAIN: wiki-v3`.
- Add `apps/production/wiki` to `apps/production/kustomization.yaml`.
- The Renovate `requarks/wiki` major hold doesn't affect this: the new release starts on
  3.x and gets minor/patch updates.

### 2. Import and configure (manual, in the v3 UI)

1. Log in as root admin and run **Import from Wiki.js 2.x** with the preflight
   `.wkbackup`. Watch for errors.
2. Re-apply what the import skips:
   - *Security*: trust proxy headers.
   - *Site*: hostname, then logo.
   - *Authentication*: re-create whatever v2 had (preflight 0.1) and activate it for the
     site. If that is Authentik OIDC, the provider needs the `wiki-v3` callback for now
     and the final callback at cutover.
   - *API Access*: enable it, then create an API key and a **personal access token** for
     tooling. Store both in the new SOPS secret.
   - *Storage → Git*: `mainertoo/wiki`, SSH inline key (same deploy key), but **branch
     `v3`, mode `push`**. The live v2 instance still bidirectionally syncs `main`, so two
     writers on one branch would fight. `v3` lets us diff the layout first.
3. **Validate** against the preflight baseline:
   - page, user and asset counts
   - about 10 spot-checked pages with images, tables and diagrams
   - page history present
   - tags and navigation
   - **heading anchors**: v2's slug rules (`h-` prefix only for non-letter starts,
     emoji leaves a variation selector) may differ in v3, which would break hand-written
     `#` links
4. Diff `mainertoo/wiki` `main` against `v3`: file paths, case, frontmatter keys. The
   Spark's `wiki-vault-mirror.sh` (Hermes second-brain) reads `main`, so layout changes
   need checking there.

### 3. Tooling (PR B, merge at cutover)

- `~/.claude/skills/wikijs-update/wiki.py`: port GraphQL to REST `/_api`. Use the PAT for
  writes and keep the upsert-by-path semantics. Re-check the description-clearing trap
  against the new API.
- `scripts/wiki/configure-git-storage.py`: port to REST, or retire it if v3 storage
  config is UI/REST-only. Update `docs/wiki-git-storage.md`.
- Point `WIKI_API_URL` defaults at `https://wiki.lab.mainertoo.com/_api`.

### 4. Cutover (PR C)

1. **Freeze edits** on v2. Take the final `.wkbackup` and import into v3 with **Overwrite
   on conflict** on: plain idempotent import skips pages edited since the trial.
2. v2: disable git storage, so it stops pushing to `main`.
3. v3: switch git storage to branch `main`, mode `sync`. First run `push` once if the
   layout differs, so `main` reflects v3 cleanly.
4. Repo (PR C):
   - move the `wiki.lab.mainertoo.com` host to the `wiki` IngressRoute and drop
     `wiki-v3`
   - set Gatus `SUBDOMAIN: wiki`
   - scale v2 to `replicas: 0`, keeping its DB and PVC
   - update the homepage annotations
5. Auth provider: switch the callback to the final hostname.
6. Verify:
   - Gatus is green
   - the `wikijs-update` skill round-trip (create, update, delete on a `sandbox/` page)
   - a git round-trip (wiki edit lands in `main`, and a `main` edit lands in the wiki)
   - the next 03:30 vault-mirror run is clean

### 5. Soak and decommission (PR D, ≥ 2 weeks after cutover)

- Keep a last-resort copy:
  - final CNPG backup of `wiki-js-cnpg-db`
  - last v2 `.wkbackup`
- Remove from the repo:
  - `apps/production/wiki-js/`
  - `apps/base/wiki-js/`
  - its `apps/production/kustomization.yaml` entry
- Clean up VolSync leftovers for `wiki-js-data`. Prune leaves the Kyverno-generated
  Secret, RS, RD and S3 repo behind (see the volsync decommission memory).
- Drop the Renovate `requarks/wiki` major hold: it is only relevant to 2.x, and future
  4.x majors should be reviewed normally.
- Decommission `wiki-js` while a rollback path still exists: that is the reason for the
  soak, not an afterthought.

## Rollback

Until PR D, rollback means restoring the IngressRoute host to the `wiki-js` route and
scaling v2 back to 1. The v2 DB and PVC are never touched. Edits made on v3 after
cutover are lost on rollback, because there is no v3 → v2 export. They survive in the git
history of `main`, though, and could be re-applied to v2 by hand.

## Open questions

1. Which auth strategies are enabled on v2 today: local only, or Authentik OIDC too?
   That decides whether step 2 needs an Authentik provider/outpost change (see the
   new-SSO-app memory).
2. Acceptable edit-freeze window for cutover? Content is mostly API-written (skills,
   Hermes), so a short window plus pausing those writers should be enough.
3. Keep `mainertoo/wiki` `main` as the mirror (recommended), or start v3 on a fresh repo
   and archive the v2 one?
