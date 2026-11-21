---
name: portainer
description: Stand up and manage Docker stacks on the homelab Portainer-EE for things deployed OUTSIDE the K3s cluster — the QNAP (local Docker) and the edge-agent hosts (VPS, DGX Spark, zwave-js). List environments/stacks, deploy a new git-repository stack from mainertoo/home_server (or a local compose file), redeploy/pull, and remove stacks. Use whenever the user wants to deploy or change a non-Kubernetes Docker workload via Portainer. Wraps scripts/portainer/portainer.py; handles API-token auth from a SOPS secret automatically.
---

# Portainer

A helper CLI (`portainer.py`, in `scripts/portainer/` of the kubernetes-lab repo)
for the homelab's central **Portainer-EE** (`https://192.168.1.252:9443`). Use it
to deploy/manage Docker stacks on environments that are **not** the K3s cluster:
the QNAP local Docker, and the edge-agent hosts (VPS `11`, DGX Spark `16`,
zwave-js `15`).

## Auth
Uses `PORTAINER_URL` / `PORTAINER_API_KEY` from the environment, else
`sops -d scripts/portainer/credentials.sops.yaml` (a personal access token,
header `X-API-Key`). Never print the token. If it's missing or still the
placeholder, point the user at `scripts/portainer/README.md` to mint + fill it.

## This homelab's pattern (important)
Stacks here are **standard repository stacks** (`type 2`, "standalone") pulled
from the private `mainertoo/home_server` repo and deployed straight to the target
endpoint — including edge endpoints, over the edge tunnel. **Edge Compute features
stay DISABLED**; do NOT enable them or use Edge Stacks/Groups. Stacks track
`refs/heads/main` with 5-minute git auto-update. Git credential id `8`
(`mainertoo-github`) authenticates the private clone.

## When to use
- "Deploy X on the Spark / QNAP / VPS" (anything not in K3s) → add the compose to
  `home_server` on `main`, then `create-git`.
- Inspect what's running outside the cluster: `endpoints`, `stacks`.
- Pull latest / re-pin a stack: `redeploy`. Remove one: `rm`.

## Commands
```
portainer.py endpoints        # list environments -> find --endpoint id
portainer.py stacks           # list stacks (id, endpoint, git source)
portainer.py git-creds        # list git credentials -> find --git-cred-id
portainer.py create-git    --name N --endpoint ID --compose-path PATH --git-cred-id 8 [--env KEY|KEY=VAL] [--no-autoupdate] --yes
portainer.py create-compose --name N --endpoint ID --file ./docker-compose.yml [--env ...] --yes
portainer.py redeploy --id ID --endpoint ID [--ref refs/heads/main] [--pull] --yes
portainer.py rm --id ID --endpoint ID --yes
```
`--env KEY` (no `=`) pulls the value from `$KEY` so secrets stay off the command line.

## Safety
**Writes (create / redeploy / rm) are DRY-RUN unless `--yes`.** Honor the repo
rule "never apply directly / ask before acting" — show the user the dry-run plan
and confirm before passing `--yes`. When deploying a git stack that supersedes an
interim host-local `docker compose` copy, `docker compose down` the host-local one
first (same `container_name` clashes).
