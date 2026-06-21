# scripts/portainer

`portainer.py` — stand up and manage Docker stacks on the homelab **Portainer-EE**
(`https://192.168.1.252:9443`) for things deployed **outside the K3s cluster**:
the QNAP (local Docker), and the edge-agent hosts (VPS, **DGX Spark**, zwave-js).

Pure stdlib + `pyyaml` (already used by `scripts/proxmox/pvinfo.py`). No other deps.

## Auth

Reads, in order:
1. `PORTAINER_URL` / `PORTAINER_API_KEY` from the environment, else
2. `sops -d scripts/portainer/credentials.sops.yaml` (values nested under
   `stringData:`, encrypted by the `^scripts/.+\.sops\.ya?ml$` rule in `.sops.yaml`).

The credential is a **personal access token** (header `X-API-Key`). The token is
never printed.

### First-time setup (fill the token)

1. In Portainer: top-right avatar → **My account** → **Access tokens** →
   **Add access token** → copy the `ptr_…` value.
2. Drop it into the encrypted creds:
   ```bash
   sops scripts/portainer/credentials.sops.yaml      # replace REPLACE_WITH_ptr_TOKEN, save
   grep PORTAINER_API_KEY scripts/portainer/credentials.sops.yaml   # verify it shows ENC[...]
   ```
   …or just `export PORTAINER_API_KEY=ptr_…` for a one-off run.

## This homelab's stack pattern

Stacks are deployed as **standard repository stacks** (`type 2`, "standalone")
pulled from the private `mainertoo/home_server` repo, deployed straight to the
target environment — including edge endpoints, via the edge tunnel. **Edge
Compute features stay disabled**; you do *not* need Edge Stacks/Groups. Each
stack tracks `refs/heads/main` with a 5-minute git auto-update.

Known ids (re-check with the commands below): central Portainer
`192.168.1.252:9443` · endpoints: `2` local-qnas, `11` docker-vps,
`15` docker-zwave-js, `16` docker-spark · git credential `8` (`mainertoo-github`).

## Usage

```bash
P=scripts/portainer/portainer.py

python3 $P endpoints          # list environments (find the endpoint id)
python3 $P stacks             # list stacks (id, endpoint, git source)
python3 $P git-creds          # list your git credentials (find --git-cred-id)

# Deploy a NEW git-repo stack (e.g. a new host_server/docker-spark/<app>):
python3 $P create-git --name myapp --endpoint 16 \
    --compose-path docker-spark/myapp/docker-compose.yml \
    --git-cred-id 8 --env SOME_TOKEN --yes
#   --env KEY=VALUE  sets it literally; --env KEY  pulls it from $KEY (keeps
#   secrets off the command line / shell history).

# Deploy from a LOCAL compose file (no git):
python3 $P create-compose --name myapp --endpoint 2 --file ./docker-compose.yml --yes

# Pull latest for an existing git stack / re-pin its ref:
python3 $P redeploy --id 100 --endpoint 16 --ref refs/heads/main --yes

# Remove a stack:
python3 $P rm --id 100 --endpoint 16 --yes
```

**Writes are dry-run unless `--yes`.** Honor the repo rule "never apply directly
/ ask before acting" — confirm with the user before `--yes` on create/redeploy/rm.

## Workflow for a new app

1. Add `docker-<host>/<app>/docker-compose.yml` to `mainertoo/home_server`,
   commit to `main` (PR). Bind-mount data to absolute host paths; set
   `container_name`. Pass secrets as stack env vars, not committed files.
2. `create-git --endpoint <id> --compose-path docker-<host>/<app>/docker-compose.yml
   --git-cred-id 8 --yes`.
3. If an interim host-local `docker compose` copy exists, `docker compose down`
   it first (same `container_name` would clash).
