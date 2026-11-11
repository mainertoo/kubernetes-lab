# Wiki.js Git Storage → mainertoo/wiki

The homelab wiki (`wiki.lab.mainertoo.com`, Wiki.js **3.x**, namespace `wiki`)
mirrors its whole content tree to the **private** `mainertoo/wiki` repo with
Wiki.js's built-in Git storage module.

- **Branch `v3`, mode `push`, every 5 minutes.** The wiki is the only source of
  truth: Wiki.js force-pushes, so edits made directly on the branch are
  overwritten. Always edit through the wiki UI or the `wikijs-update` skill
  (`wiki3.py`).
- Every page is a Markdown file with YAML frontmatter, and the folder tree mirrors
  page paths (e.g. `infrastructure/networking/adguard-internal.md`). Uploaded files
  sit at the repo root (`screenshot-2026-06-11-at-4.33.35-pm.png`).
- ⚠️ **Wiki.js keeps its own history and force-pushes it.** Any commit made to `v3`
  outside the wiki, including files the wiki doesn't own, is deleted on the next
  push (verified 2026-11-11). Never store anything else on this branch.
- The MkDocs guides site (`apps/base/mkdocs`) git-syncs branch `v3` for its pages
  (`guides/docs/`). Its `mkdocs.yml` and asset hook live in this repo
  (`apps/base/mkdocs/site-config/`), not on the branch.
- **Branch `main` is the frozen Wiki.js 2.x archive** (bidirectional sync until the
  2026-11-11 cutover; v2 itself was retired the same day). Nothing writes to it.
- Wiki content is **private**: it lives only in `mainertoo/wiki`, never in this
  public repo. `kubernetes-lab/docs` is a separate, curated public subset.
- Other readers of branch `v3`: the Spark `~/scripts/wiki-vault-mirror.sh`
  (Obsidian/Hermes vault, read-only deploy key `spark-wiki-vault-mirror (ro)`).

## Where the config lives

The storage target (repo URL, branch, mode, schedule and the SSH key, entered
inline) is runtime state in the v3 Postgres DB (`wiki-cnpg-db`, CNPG-backed, so it
survives in DB backups). There is no manifest for it, and no PVC: v3 commits from
a scratch checkout on each sync.

The deploy key is `wiki-js-storage` (read-write on `mainertoo/wiki`; the name dates
from v2). Its private half is SOPS-encrypted as `WIKI_GIT_SSH_KEY` in
`apps/base/wiki/wiki-git-deploy-key.sops.yaml` (Secret `wiki-git-deploy-key`). Nothing
mounts it: it's the git copy for DR and key rotation, and the live copy is in the
storage config. It's kept out of `wiki-secret`, which the pod loads as env vars.

## Setup / DR runbook

After a wiki rebuild with an empty DB:

1. The target branch must already exist: v3 refuses to push to a missing branch.
   Branch `v3` exists today; for a new branch, create it with one commit first.
2. In the wiki, *Administration → Storage → Git*:
   - repository `git@github.com:mainertoo/wiki.git`, branch `v3`
   - auth: SSH, key pasted inline from `WIKI_GIT_SSH_KEY`
   - mode **push**, schedule 5 minutes
   - site setting `localePrefix` **off**, so paths match the existing branch
3. To export every page at once, run the target's *sync untracked* action.

Storage actions and saves need a **logged-in user session**: v3 refuses API keys
for them. Over the REST API, log in first
(`PUT /_api/sites/:site/auth/login`, as `wiki3.py` does), then call
`POST /_api/sites/:site/storage/targets/:id/actions/syncUntracked`. The v2-era
`scripts/wiki/configure-git-storage.py` (GraphQL) was retired at the cutover.

## Rotating the deploy key

1. `ssh-keygen -t ed25519 -f /tmp/k -N "" -C wiki-js-storage` (no passphrase).
2. Add `/tmp/k.pub` as a **read-write** deploy key on `mainertoo/wiki`
   (`gh repo deploy-key add /tmp/k.pub -R mainertoo/wiki -w -t wiki-js-storage`),
   and remove the old one.
3. Store the private key in the SOPS secret (no plaintext on disk):
   `sops set apps/base/wiki/wiki-git-deploy-key.sops.yaml '["stringData"]["WIKI_GIT_SSH_KEY"]' "$(jq -Rs . < /tmp/k)"`
4. Paste the new key into *Administration → Storage → Git*, save, then
   `shred -u /tmp/k*`.

## Gotchas

- Wiki.js **lowercases** page paths (`migrations/README.md` → `migrations/readme.md`).
- Push mode force-pushes its own history: never point the target at `main` or any
  branch with history you want to keep, and never commit to `v3` by hand.
- Imported upload names use `-` instead of `_` (`screenshot_x.png` →
  `screenshot-x.png`); pages reference the new names.
