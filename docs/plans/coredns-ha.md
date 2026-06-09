# CoreDNS HA (2 replicas) on K3s

## Problem

K3s deploys CoreDNS as a **single replica** (the bundled server AddOn manifest has no
`replicas:` field, so it defaults to 1). That single pod is a cluster-wide DNS single
point of failure: when it reschedules — node drain, K3s/kernel upgrade, eviction — DNS is
unavailable for ~30s cluster-wide.

This surfaced as recurring **authentik-server restarts** (6 in 5 days, clean `exit 0`):
authentik's `/-/health/live/` runs the django-tenants middleware, which does a DB lookup on
every request. During a DNS gap it can't resolve the `authentik-db-rw` CNPG service, the
liveness probe fails 3× (~30s), and kubelet restarts the pod. Restarting doesn't fix DNS,
so the restarts are pure noise. Other apps ride out the blip because their liveness isn't
DB-coupled.

## Fix

Run **2 CoreDNS replicas**. The bundled manifest already includes:

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule    # forces the 2 pods onto different nodes
    labelSelector: { matchLabels: { k8s-app: kube-dns } }
```

so the only change required is `replicas: 2` — the spread constraint guarantees they land
on separate nodes, so a single node/pod loss never drops DNS to zero.

## Why this lives in Ansible, not Flux

Flux depends on cluster DNS to function. Managing CoreDNS via Flux creates a circular
dependency — a DNS failure could block Flux from fixing DNS. CoreDNS belongs in the K3s
bootstrap layer, which is Ansible here (same layer that already does `--disable` of traefik
and servicelb).

## Mechanism

K3s **regenerates** its bundled `coredns.yaml` on every server start and overwrites manual
edits, and a `kubectl scale` is reverted by its AddOn controller — so durability requires
owning the manifest. K3s supports this via a `.skip` sentinel:

`tasks/coredns_ha.yml` (run on every server / master node):
1. Copies the **current** bundled `/var/lib/rancher/k3s/server/manifests/coredns.yaml` to
   `coredns-ha.yaml` (so it tracks whatever CoreDNS version/config K3s currently ships).
2. Injects `replicas: 2` into the Deployment spec (anchored on the unique
   `revisionHistoryLimit: 0` line; a guard fails loudly if K3s changes the layout).
3. Writes `coredns.yaml.skip` so K3s stops re-applying (and reverting) the 1-replica
   bundled manifest. `.skip` makes K3s leave the bundled file alone — it does **not** delete
   the already-applied objects. `coredns-ha.yaml` is the *full* manifest (SA, RBAC,
   ConfigMap, Service, Deployment), so it remains correct whether `.skip` orphans or prunes.

The K3s deploy controller watches the manifests dir and applies `coredns-ha.yaml` as soon
as it appears — **no K3s restart needed**, and no DNS-outage cutover (it updates the existing
Deployment in place, just adding a pod). The `NodeHosts` ConfigMap key stays K3s-managed
(its node-IP controller is independent of the AddOn deploy).

## Execute

```bash
# (recommended) test on staging first
ansible-playbook -i ansible/k3s-cluster/inventory/staging/dynamic.sh \
  ansible/k3s-cluster/playbooks/k3s_coredns_ha.yml

# production
ansible-playbook -i ansible/k3s-cluster/inventory/production/dynamic.sh \
  ansible/k3s-cluster/playbooks/k3s_coredns_ha.yml
```

The playbook applies the override on all servers, then waits for `coredns` to report 2 ready
replicas.

## Verify

```bash
kubectl -n kube-system get deploy coredns -o wide          # 2/2 READY
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide # 2 pods, DIFFERENT nodes
kubectl -n kube-system get svc kube-dns                     # ClusterIP still 10.43.0.10

# failover test — delete one CoreDNS pod, confirm DNS keeps resolving throughout:
kubectl -n kube-system delete pod -l k8s-app=kube-dns --field-selector ... # one pod
kubectl run dnstest --rm -it --image=busybox --restart=Never -- \
  nslookup kubernetes.default.svc.cluster.local
```

Also confirm the K3s objects survived the `.skip` (should still exist):
`kubectl -n kube-system get sa coredns; get cm coredns; get svc kube-dns`.

## Rollback

On each server node:

```bash
sudo rm /var/lib/rancher/k3s/server/manifests/coredns.yaml.skip \
        /var/lib/rancher/k3s/server/manifests/coredns-ha.yaml
sudo systemctl restart k3s     # K3s re-applies the bundled 1-replica CoreDNS
```

## Upgrades

K3s rewrites the bundled `coredns.yaml` (often with a newer image) on upgrade. Because it's
`.skip`'d, our `coredns-ha.yaml` stays in force — but it would pin the *pre-upgrade* image
until re-derived. `k3s_upgrade.yml` therefore imports `tasks/coredns_ha.yml` in a final play
(after master + worker upgrades) to regenerate `coredns-ha.yaml` from the fresh bundled
manifest. If you ever upgrade K3s outside that playbook, re-run `k3s_coredns_ha.yml`.

## Going further (not done)

If transient DNS gaps persist after this (e.g. during multi-node maintenance), add
**NodeLocal DNSCache** — a per-node DNS cache DaemonSet that masks even a full central-CoreDNS
outage for cached lookups. It's additive (Flux-deployable, no cutover). Treat as phase 2.
