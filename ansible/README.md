# ansible/

Node prep and K3s lifecycle (install / upgrade / uninstall), plus the
in-cluster prerequisites that ship before Flux (kube-vip for the HA
control-plane VIP, MetalLB for service LoadBalancers).

Everything is structured per-cluster, so production and staging are
managed by the same playbooks but against separate inventories with
separate `group_vars`.

## Layout

```
ansible/k3s-cluster/
├── inventory/
│   ├── production/
│   │   ├── dynamic.sh                   # reads terraform/environments/production/terraform.tfstate
│   │   └── group_vars/all.yml           # production-specific values
│   └── staging/
│       ├── dynamic.sh                   # reads terraform/environments/staging/terraform.tfstate
│       └── group_vars/all.yml           # staging-specific values
└── playbooks/
    ├── k3s_install.yml                  # fresh install: 1 master cluster-init + N joining masters + N agent workers
    ├── k3s_upgrade.yml                  # rolling upgrade: serial:1, cordon → drain → upgrade → wait → uncordon
    ├── k3s_uninstall.yml                # tear down via official k3s-uninstall.sh + k3s-agent-uninstall.sh
    ├── kubevip_install.yml              # control-plane VIP DaemonSet
    ├── metallb_install.yml              # MetalLB v0.14.9 + L2 IPAddressPool
    ├── k3s_server_config.yml            # (one-off / config drift helper)
    └── templates/
        ├── kubevip_daemonset.yml.j2
        └── metallb_config.yml.j2
```

The inventory `dynamic.sh` scripts shell out to `terraform output` against
their cluster's state file, then emit Ansible inventory JSON with `master`
and `worker` groups. Group names are kept cluster-agnostic in playbooks
(`groups['master']`) — the per-cluster scope comes from which inventory
directory you point at.

Staging's `dynamic.sh` emits an empty-but-valid inventory when its
state file doesn't exist yet (pre-Phase-4 of the two-cluster restoration),
so `ansible-inventory --list` doesn't blow up.

## Invocation

Always pass `-i` pointing at the cluster you intend to target. Forgetting
this defaults to local-only and can be confusing.

```bash
# Production
ansible-playbook -i ansible/k3s-cluster/inventory/production/dynamic.sh \
  ansible/k3s-cluster/playbooks/k3s_install.yml

# Staging
ansible-playbook -i ansible/k3s-cluster/inventory/staging/dynamic.sh \
  ansible/k3s-cluster/playbooks/k3s_install.yml

# Dry runs use --check (with --diff for change preview)
ansible-playbook -i .../production/dynamic.sh playbooks/k3s_upgrade.yml --check --diff

# Quick connectivity check
ansible all -i .../production/dynamic.sh -m ping
```

SSH: the dynamic inventory hardcodes `ansible_user = ubuntu` and
`ansible_ssh_private_key_file = $HOME/.ssh/id_ed25519_k3s`. Make sure
that key exists locally and its public half is in
`terraform/ssh_host_ed25519.pub` (terraform injects that file into
the VMs via cloud-init).

## Per-cluster group_vars

Every variable that differs between clusters lives in
`inventory/<cluster>/group_vars/all.yml` — playbooks don't hardcode
cluster-specific values. The full set:

| Variable | What it controls |
|---|---|
| `cluster_name` | Used by the MetalLB template to name the IPAddressPool / L2Advertisement. |
| `k3s_version` | Pinned version for both install and upgrade. **Bump this before running `k3s_upgrade.yml`.** |
| `k3s_disable` | Built-in K3s components to disable (currently `[traefik, servicelb]` for both clusters; we run Traefik via Helm + MetalLB). |
| `k3s_apiserver_extra_args` | Raw `--kube-apiserver-arg=...` flags. Production needs the MutatingAdmissionPolicy alpha gates. |
| `kube_vip_ip` | Control-plane VIP. Production `.160`, staging `.170`. |
| `kubevip_image`, `kubevip_interface` | DaemonSet image + interface name. |
| `metallb_pool_range` | The IP range MetalLB hands out. Production `.180-.199`, staging `.200-.219` (non-overlapping). |

If you need to override one of these for a single play (testing a new
version, etc.), pass `-e k3s_version=v1.34.6+k3s1` on the command line
— `-e` wins over `group_vars`.

## Playbook reference

### `k3s_install.yml`

Fresh install. Three plays:
1. **Common prep** on every node (`k3s_cluster` group): disable swap,
   install `ceph-common`, persist `ceph` kernel module.
2. **Servers** (`master` group): first master runs `--cluster-init`,
   additional masters join with `K3S_URL` + `K3S_TOKEN` read from the
   first master's `/var/lib/rancher/k3s/server/node-token`.
3. **Agents** (`worker` group): wait for the API on the first master,
   then install with `K3S_URL` + `K3S_TOKEN`.

All installs pass `INSTALL_K3S_VERSION={{ k3s_version }}` and an
`INSTALL_K3S_EXEC` rendered from `k3s_disable` + `k3s_apiserver_extra_args`.

### `k3s_upgrade.yml`

Rolling upgrade. Two plays (masters then workers), each with `serial: 1`
and `max_fail_percentage: 0` so a node failure aborts the rest.

For each node:
1. `kubectl cordon`
2. `kubectl drain --ignore-daemonsets --delete-emptydir-data --force --timeout=300s`
3. Re-run the K3s installer with `INSTALL_K3S_VERSION={{ k3s_version }}`.
   Master `INSTALL_K3S_EXEC` preserves `--tls-san {{ kube_vip_ip }}` so
   the apiserver cert SAN survives.
4. Wait for the systemd unit (`k3s` or `k3s-agent`) to be active.
5. Wait for the node to report `Ready=True` against the API.
6. `kubectl uncordon`.

Workers don't re-pass `K3S_URL`/`K3S_TOKEN` — they're already persisted
in `/etc/systemd/system/k3s-agent.service.env`.

### `k3s_uninstall.yml`

Tears down via the official scripts (`k3s-uninstall.sh` for masters,
`k3s-agent-uninstall.sh` for workers). Both clean up service units,
data directories, and CNI state — not just the binary.

### `kubevip_install.yml`

Applies the kube-vip RBAC manifest from upstream, then renders + applies
`templates/kubevip_daemonset.yml.j2` with `kube_vip_ip` / `kubevip_image`
/ `kubevip_interface` from group_vars. Idempotent.

### `metallb_install.yml`

Applies the MetalLB v0.14.9 native manifests, waits for the controller
Deployment to roll out, renders + applies `templates/metallb_config.yml.j2`
with `cluster_name` (used for the IPAddressPool/L2Advertisement names)
and `metallb_pool_range`. Also scales the controller to 2 replicas with
a topology spread + adds a PodDisruptionBudget for HA (matches the cluster-wide
HA architecture).

## Bootstrapping Flux on a new cluster (after `k3s_install.yml`)

Two manual prerequisites the playbooks don't handle (yet):

1. **SOPS age key** — the same age key that decrypts production's
   secrets must be in the new cluster's `flux-system` namespace as the
   `sops-age` Secret, or any encrypted manifest fails to apply.
   ```bash
   kubectl --kubeconfig ~/.kube/<cluster> create namespace flux-system
   kubectl --kubeconfig ~/.kube/<cluster> create secret generic sops-age \
     -n flux-system --from-file=age.agekey=$HOME/.config/sops/age/keys.txt
   ```

2. **GitHub deploy-key Secret** — the `flux-system` GitRepository
   references a Secret named `flux-system` containing an SSH deploy key
   authorized on the repo. For a homelab a single deploy key shared
   between clusters is fine; copy from production:
   ```bash
   kubectl get secret flux-system -n flux-system -o json \
     | jq 'del(.metadata.creationTimestamp,.metadata.resourceVersion,.metadata.uid,.metadata.managedFields,.metadata.annotations,.metadata.labels)' \
     | kubectl --kubeconfig ~/.kube/<cluster> apply -f -
   ```

3. **Bootstrap Flux** itself by applying the cluster's flux-system
   manifests with one caveat — the SOPS-encrypted `cluster-secrets.sops.yaml`
   trips kustomize's parser (it has no SOPS plugin), so skip it on
   the bootstrap apply and let Flux's kustomize-controller reconcile
   it after start-up:
   ```bash
   # Local edit (revert immediately after) to skip the SOPS file
   sed -i.bak '/cluster-secrets\.sops\.yaml/d' clusters/<cluster>/flux-system/kustomization.yaml
   kubectl --kubeconfig ~/.kube/<cluster> apply -k clusters/<cluster>/flux-system/
   mv clusters/<cluster>/flux-system/kustomization.yaml.bak clusters/<cluster>/flux-system/kustomization.yaml
   ```

   Flux comes up, pulls the repo, reconciles `clusters/<cluster>/`
   (which still includes the SOPS file in git), decrypts via the
   `sops-age` secret, and applies the encrypted manifests. The
   parent `flux-system` Kustomization gets self-managed thereafter.

## Pre-flight checklist before running `k3s_upgrade.yml`

Mandatory before bumping the live cluster — running the upgrade against
unhealthy state can deadlock the drain.

- [ ] `kubectl get nodes -o wide` — all nodes Ready, on the current version.
- [ ] `flux get kustomizations -A` — everything `Ready=True`.
- [ ] `flux get hr -A | awk '$5 != "True"'` — empty (no failing HelmReleases).
- [ ] `kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded` — empty.
- [ ] `kubectl get replicationsource -A` — recent successful sync on every PVC.
- [ ] `git log --oneline ansible/k3s-cluster/inventory/<cluster>/group_vars/all.yml | head -5` — confirm `k3s_version` actually bumped, and review the Rancher support matrix for the target.
- [ ] Validate first on staging when it exists. **Don't go straight to production.**

If a drain stalls (PDBs blocking eviction, stuck finalizers, etc.) the
upgrade play will hang on the drain step. Cancel the play, fix the
underlying issue, then re-run.

## Considerations for future updates

### Adding a new cluster

1. Add the cluster to terraform first (see `terraform/README.md`).
2. `mkdir -p ansible/k3s-cluster/inventory/<name>/group_vars`
3. Copy `production/dynamic.sh` and edit the `STATE_FILE` path.
4. Create `group_vars/all.yml` — pick a non-overlapping `kube_vip_ip`
   and `metallb_pool_range`.
5. Run `ansible-inventory -i .../<name>/dynamic.sh --graph` to confirm
   the master/worker groups populate.

### Adding a new playbook

- Use `groups['master']` / `groups['worker']` — playbooks are
  cluster-agnostic by design.
- Pull any cluster-specific values from `group_vars` (don't hardcode
  IPs, VIPs, pool names, version strings).
- `become: yes` is the convention in this tree (linters that prefer
  `true` will warn — that's cosmetic, the playbooks work either way).
- For new resource manifests, follow the template pattern: a `.j2`
  under `playbooks/templates/`, rendered to `/tmp/` on the master,
  then `kubectl apply`-ed. The MetalLB / kube-vip playbooks are good
  references.

### Migrating off the legacy script-based inventory

Ansible has marked script-based inventories deprecated; the warning
`Inventory scripts should always provide 'meta.hostvars'` fires on
every run. Removal slated for ansible-core 2.23. The migration is
straightforward when the time comes — switch the script to emit
`_meta.hostvars` or convert to a YAML inventory generated from
terraform outputs. Not urgent until 2.23 is on the horizon.

### Version bumps

- **K3s** — edit `k3s_version` in each cluster's `group_vars/all.yml`,
  commit, then run `k3s_upgrade.yml`. Always staging first.
- **kube-vip** — edit `kubevip_image` in each cluster's `group_vars/all.yml`.
  Re-run `kubevip_install.yml`; the DaemonSet rolls.
- **MetalLB** — the upstream URL is hardcoded in `metallb_install.yml`.
  Bump it there directly and re-run the playbook.

## Related

- VM IPs / placement come from `terraform/environments/<env>/terraform.tfstate`.
  See `terraform/README.md` for the provisioning side.
- The K3s control plane runs with Flux as the GitOps engine — once the
  cluster is up and Flux is bootstrapped against `clusters/<env>/`,
  most things are managed by manifest changes, not Ansible.
