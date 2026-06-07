# Always-On Ops Node

The always-on homelab operations node provides reliable in-network shell access for Kubernetes, GitHub, Proxmox, and related administration when the Mac laptop is asleep, off-network, or unavailable.

## Current Primary Ops Node

| Field | Value |
| --- | --- |
| Role alias | `ops-01` |
| Existing service hostname | `z-wave-js` / `zwave-js` |
| Proxmox VMID | `701` |
| Proxmox host | `pve-s13` |
| IP address | `192.168.1.236` |
| Login user | `mainertoo` |
| OS | Debian 13 `trixie` |
| Secondary role | Docker host for Z-Wave JS UI |
| Backup | Included in Proxmox Backup Server backups |

The VM continues to run Z-Wave JS and Portainer agent, so it should remain a boring, low-load operations host rather than a general CI/build runner.

## Why This Exists

The Mac remains a useful admin workstation, but it is not always reachable because it may be asleep, closed, or off-network. `ops-01` is always on, inside the home network, and outside the k3s cluster, giving Hermes and humans a stable place to run:

- `ssh` to Proxmox, QNAP, PBS, VPS/Pangolin, and other infrastructure
- `kubectl` and `flux` against the k3s cluster
- `git` and `gh` against GitHub
- `sops`/`age` for GitOps secret workflows

## Access Model

Preferred order:

1. `ops-01` / VM 701 / `z-wave-js` for routine Hermes and homelab operations.
2. Mac laptop as fallback/admin workstation.
3. Future `ops-02` on another Proxmox host if higher availability is needed.

SSH aliases on the ops node include:

- `ops-01`
- `z-wave-js`
- `pve-s13`
- `pve-mammoth`
- `pve-whistler`
- `pve-zermatt`
- `pve-ugreen`
- `pve-mac`
- `qnas`
- `pbs`
- `vps` for the RackNerd/Pangolin host

## Installed Tooling

The following tools are installed and verified on `ops-01`:

- `git`
- `gh`
- `kubectl`
- `flux`
- `helm`
- `kustomize`
- `sops`
- `age`
- `jq`
- `yq`
- Docker, already present for Z-Wave JS

Important files under `/home/mainertoo`:

- `~/.ssh/config`
- `~/.ssh/id_ed25519`
- `~/.ssh/cluster_key`
- `~/.kube/config`
- `~/.config/gh/hosts.yml`
- `~/.config/sops/age/keys.txt`
- `/home/mainertoo/src/kubernetes-lab`
- `/home/mainertoo/src/home_server`

Private key/config files should remain mode `600`.

## Hermes Integration

Hermes is deployed in Kubernetes, but its terminal backend uses SSH to the ops node.

The GitOps-managed HelmRelease sets the terminal SSH target with non-secret environment variables:

```yaml
TERMINAL_SSH_HOST: "192.168.1.236"
TERMINAL_SSH_USER: "mainertoo"
TERMINAL_SSH_PORT: "22"
TERMINAL_SSH_KEY: "/opt/data/.ssh/id_ed25519"
```

The SSH client drop-in mounted into the Hermes pod also recognizes the role alias and persistent known_hosts path:

```sshconfig
Host ops-01 z-wave-js 192.168.1.236
    HostName 192.168.1.236
    User mainertoo
    IdentityFile /opt/data/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
    UserKnownHostsFile /opt/data/.ssh/known_hosts
```

The backend selection itself is stored in the Hermes PVC config as `terminal.backend: ssh`.

## Storage Baseline

During bootstrap, VM 701 was expanded from a 16 GiB virtual disk to a 32 GiB virtual disk.

Final expected state:

```text
/dev/sda2 ext4 mounted at /, about 31G total
/swapfile 1G active
```

A Proxmox snapshot was created before bootstrap:

```text
pre-ops-bootstrap-20260606-144226
```

## Smoke Test

Run this from `ops-01` after future changes:

```bash
hostname -f
whoami
df -h /
swapon --show
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
gh auth status
ssh -o BatchMode=yes -T git@github.com
kubectl get nodes -o wide
flux get kustomizations -A --no-header | head -30
for h in pve-s13 pve-mammoth pve-whistler pve-zermatt pve-ugreen pve-mac qnas pbs vps; do
  printf '%-12s ' "$h"
  ssh -o BatchMode=yes -o ConnectTimeout=4 "$h" 'hostname 2>/dev/null || uname -n' 2>&1 | head -1 || true
done
```

## Watchdog

A quiet Hermes cron watchdog checks the ops-node path every 30 minutes and only emits a Discord alert when something fails.

- Hermes cron job: `ops-node-watchdog`
- Live script path on VM 701: `/home/mainertoo/scripts/ops-node-watchdog.sh`
- Version-controlled source: `scripts/ops-node-watchdog.sh`

It checks:

- current host is reachable
- root disk is below the alert threshold
- Z-Wave JS and Portainer containers are running
- GitHub auth is valid
- Kubernetes API is reachable and nodes are Ready
- Flux can list kustomizations
- key SSH aliases are reachable

## Safety Notes

- Avoid heavy CI/build workloads on VM 701 because it also hosts Z-Wave JS.
- Ask before rebooting this VM, restarting Docker, changing VM networking, or editing Z-Wave JS configuration.
- Treat copied SSH, GitHub, kubeconfig, and SOPS/age credentials as high-value secrets.
- `ops-02` can be added later on a different Proxmox host to remove the pve-s13/VM701 single point of failure.
