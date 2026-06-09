# Wiki.js Git Storage → mainertoo/wiki

The homelab Wiki.js (`wiki.lab.mainertoo.com`) mirrors its **entire** content tree
to the **private** `mainertoo/wiki` repo using Wiki.js's built-in **Git Storage**
module, in **bidirectional (`sync`)** mode. This replaced the old one-way
`docs-publisher` CronJob (retired 2026-06-08).

- Every page is a Markdown file with YAML frontmatter; the repo directory tree
  mirrors page paths (e.g. `infrastructure/networking/adguard-internal.md`).
- **Bidirectional**: editing in the wiki UI/API commits + pushes to the repo;
  pushing a `.md` change to the repo pulls into the wiki on the next sync (~5 min).
- Wiki content is **private** — it lives only in `mainertoo/wiki`, never in this
  public repo. `kubernetes-lab/docs` is a separate, curated public subset.

## Why not GitOps-managed?

The storage **config** (repo URL, mode, key) is runtime state in the Wiki.js
Postgres DB (CNPG-backed, so it survives in DB backups), set via the Wiki.js
GraphQL API — there is no manifest for it. The **deploy key** is the one piece
kept in git: SOPS-encrypted as `WIKI_GIT_SSH_KEY` in
`apps/base/wiki-js/wiki-js-secret.sops.yaml`. `scripts/wiki/configure-git-storage.py`
re-applies the config from these two sources, so the setup is reproducible.

## Auth

- An **ed25519 SSH deploy key** with read-write on `mainertoo/wiki` (no passphrase
  — Wiki.js requires that). Public half = a deploy key on the repo; private half =
  `WIKI_GIT_SSH_KEY` in the SOPS secret.
- Configured in **`contents` mode**: Wiki.js writes the key to
  `data/secure/git-ssh.pem` itself (correct perms) and runs
  `ssh -i <key> -o StrictHostKeyChecking=no`, so there's no in-pod known_hosts or
  fsGroup permission setup to do.

## Setup / DR runbook

After a wiki rebuild (or to reconfigure), from the repo root:

```bash
# 1. (rebuild only) ensure the repo has a `main` branch with at least one commit
#    so the first bidirectional pull has a ref. A fresh mainertoo/wiki needs:
#      git init && git commit --allow-empty -m init && git push -u origin main

# 2. point Wiki.js at the repo in bidirectional mode (reads key from SOPS,
#    token from env or the live k8s secret):
python3 scripts/wiki/configure-git-storage.py config sync
python3 scripts/wiki/configure-git-storage.py status      # expect status: operational

# 3a. SEED FROM WIKI (DB has the pages, repo is empty) — export everything:
python3 scripts/wiki/configure-git-storage.py action syncUntracked
python3 scripts/wiki/configure-git-storage.py action sync

# 3b. SEED FROM REPO (fresh wiki DB, repo has the content) — import everything:
python3 scripts/wiki/configure-git-storage.py action importAll
```

Actions: `syncUntracked` (export all DB pages → local repo), `sync` (force a
sync, respects direction), `importAll` (import all repo content → wiki), `purge`
(clear the local repo clone — use for unrelated-merge-history errors; does not
touch the remote).

## Rotating the deploy key

1. `ssh-keygen -t ed25519 -f /tmp/k -N "" -C wiki-js-storage` (no passphrase).
2. Add `/tmp/k.pub` as a **read-write** deploy key on `mainertoo/wiki`
   (`gh repo deploy-key add /tmp/k.pub -R mainertoo/wiki -w -t wiki-js-storage`);
   remove the old one.
3. Store the private key into the SOPS secret (MAC-safe, no plaintext on disk):
   `sops set apps/base/wiki-js/wiki-js-secret.sops.yaml '["stringData"]["WIKI_GIT_SSH_KEY"]' "$(jq -Rs . < /tmp/k)"`
4. `python3 scripts/wiki/configure-git-storage.py config sync` to push the new key
   into the live storage config; `shred -u /tmp/k*`.

## Gotchas

- Wiki.js **lowercases** page paths (`migrations/README.md` → `migrations/readme.md`).
- The GraphQL `storage.updateTargets` config encodes each value as `{"v": <value>}`;
  a value of `********` means "keep the existing stored value" (used for sensitive
  fields). `updateTargets` patches per-target, so sending only `git` leaves the
  `local` target untouched.
