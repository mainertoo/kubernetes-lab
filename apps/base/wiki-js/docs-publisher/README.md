# wiki-js-docs-publisher

CronJob that publishes every `.md` file under the repo's `/docs/` directory into wiki-js as a page under the `docs/` section.

## Why

Wiki-js is internal-only behind Authentik SSO and split-DNS — public GitHub Actions runners can't reach it directly. Rather than wire up Tailscale + GH Actions secrets, this Job runs **inside the cluster**, uses the existing in-cluster `wiki-js-secret`'s `WIKI_API_KEY`, and posts to the in-cluster service URL `http://wiki-js.wiki-js.svc.cluster.local:3000/graphql`.

## What it does each run

1. **Init container** clones `mainertoo/kubernetes-lab` `master` (depth 1) into an emptyDir. Authenticates to GitHub via a fine-grained PAT — see [GitHub PAT requirement](#github-pat-requirement) below for setup + rotation.
2. **Publish container** runs `publish.py` which:
   - Walks `/workspace/docs/*.md` (recursive).
   - For each file:
     - Wiki path = `docs/<lowercased-relative-path-without-.md>`.
     - Title = first H1 in file, fallback to filename title-cased.
     - Description = first non-empty paragraph after H1, truncated to 250 chars.
     - Content = file contents with relative `.md` links rewritten to `/docs/...` (no extension).
   - Queries existing wiki pages under `docs/*`, dispatches **create** (new) or **update** (changed) or **skip** (unchanged content).

## What it does NOT do

- **No deletes.** If a `.md` file is removed from the repo, the corresponding wiki page sticks around as an orphan. To clean up, remove the page from the wiki UI or extend `publish.py` with a `--prune` flag.
- **No bidirectional sync.** The repo is the source of truth for `docs/*`. Edits made in the wiki UI to those pages will be overwritten on the next 15-min run.
- **No frontmatter handling.** YAML frontmatter (none currently in `/docs`) would render literally.
- **No image rewriting.** Relative image refs (none currently in `/docs`) would break.

## GitHub PAT requirement

The repo is private (per the deferred `project_repo_going_public` audit). The init container clones over HTTPS using a fine-grained PAT injected as `GITHUB_TOKEN` from `wiki-js-secret`. The token is only set on the init container — the publish container never sees it.

### Generating the PAT

1. GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → Generate new token.
2. **Token name**: `wiki-js-docs-publisher`.
3. **Expiration**: 1 year recommended.
4. **Repository access** → **Only select repositories** → `mainertoo/kubernetes-lab`.
5. **Repository permissions** → **Contents** → **Read-only**. Everything else: No access.
6. Generate. Copy the `github_pat_…` value.

### Storing the PAT

```bash
sops apps/base/wiki-js/wiki-js-secret.sops.yaml
# Add under stringData:
#   GITHUB_TOKEN: github_pat_xxxxxxxxxxxxxxxxxxxxxx
# Save — VS Code SOPS extension re-encrypts on save.

# Verify encryption per feedback_sops_suffix_not_guarantee_encryption:
grep -c 'ENC\[' apps/base/wiki-js/wiki-js-secret.sops.yaml
# Should be one higher than before. If unchanged, the save did NOT re-encrypt
# and the token is in plaintext on disk.
```

Then commit + push + open a PR off `master` (not the merged docs-publisher branch — per [[feedback_followup_pr_branch]]).

### Rotation

When the PAT nears expiration:

1. Generate a new PAT with the same scope.
2. `sops apps/base/wiki-js/wiki-js-secret.sops.yaml` → replace `GITHUB_TOKEN` value → save.
3. Commit + push. Flux reconciles, next CronJob run picks up the new token.
4. Revoke the old PAT in GitHub.

### Why not just flip the repo public?

The audit work (`project_repo_going_public`) is complete but the visibility flip was deferred. If/when you flip it, this whole PAT mechanism becomes unnecessary — drop the `GITHUB_TOKEN` env block from the init container, drop the GITHUB_TOKEN key from the Secret, revoke the PAT in GitHub.

## Schedule

`*/15 * * * *` (every 15 minutes). `concurrencyPolicy: Forbid` prevents overlap. `successfulJobsHistoryLimit: 3`. Each run takes ~3-10s for the current 15 docs.

## Secret rotation

`WIKI_API_KEY` is stored in `apps/base/wiki-js/wiki-js-secret.sops.yaml`. To rotate:

1. Generate a new token in wiki-js (Admin → API Access → New API Key).
2. Edit `wiki-js-secret.sops.yaml` (VS Code SOPS extension auto-encrypts on save).
3. Commit + push. Flux reconciles, next CronJob run picks up the new env value.
4. Revoke the old token in wiki-js Admin.

## Manual run / debugging

```bash
# Trigger a one-off Job from the CronJob template
kubectl -n wiki-js create job --from=cronjob/wiki-js-docs-publisher manual-$(date +%s)

# Watch logs
kubectl -n wiki-js logs -l app.kubernetes.io/name=wiki-js-docs-publisher --tail=100

# Dry-run locally with port-forward (see PR #585 / wiki-information-architecture.md)
kubectl -n wiki-js port-forward svc/wiki-js 13000:3000 &
REPO_DOCS_PATH=$PWD/docs \
WIKI_API_URL=http://127.0.0.1:13000/graphql \
WIKI_API_KEY=$(sops -d apps/base/wiki-js/wiki-js-secret.sops.yaml | yq '.stringData.WIKI_API_KEY') \
DRY_RUN=1 \
python3 apps/base/wiki-js/docs-publisher/configmap.yaml  # well, extract the script first
```

## Renovate

Both images (`alpine/git`, `python`) are picked up by Renovate's `kubernetes` manager and will get version-bump PRs.

## Related

- [`docs/wiki-information-architecture.md`](../../../../docs/wiki-information-architecture.md) — wiki IA + manual-reorg history.
- PR #584 — added `WIKI_API_KEY` to `wiki-js-secret.sops.yaml`.
- PR #585 — wiki IA documentation.
