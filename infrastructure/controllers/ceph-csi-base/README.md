# Ceph CSI Integration

This document covers how Ceph CSI is structured and deployed in this cluster. The drivers connect Kubernetes to the external Ceph storage cluster (`192.168.99.12-14`, FSID `2b142655-7791-4f14-8934-0ea16121b652`) and provide two StorageClasses that apps consume:

- **`ceph-rbd`** — block storage, `ReadWriteOnce`, default for stateful apps
- **`cephfs`** — file storage, `ReadWriteMany`, used for shared volumes

## Folder structure

```
infrastructure/controllers/
├── ceph-csi-base/        Shared resources used by both drivers
├── ceph-csi-rbd/         RBD driver (CSIDriver, DaemonSet, Deployment, RBAC)
├── ceph-csi-cephfs/      CephFS driver (CSIDriver, DaemonSet, Deployment, RBAC)
└── storage/
    ├── ceph-rbd/         StorageClass + auth Secret for ceph-rbd
    ├── cephfs/           StorageClass + auth Secret for cephfs
    └── nfs-qnap/         (unrelated — NFS provisioner for QNAP)
```

The split is deliberate: **drivers** (Kubernetes plumbing) live under `ceph-csi-*/`; **storage classes** (what apps reference) live under `storage/`. The two `ceph-csi-*` driver folders mirror each other in shape.

### `ceph-csi-base/`

Resources that both the rbd and cephfs drivers depend on:

| File | Purpose |
|---|---|
| `namespace.yaml` | The `ceph-csi` namespace |
| `ceph-csi-config.yaml` | `ConfigMap/ceph-csi-config` — cluster registry (FSID + monitor list) consumed by every driver pod |
| `ceph-csi-encryption-kms-config.yaml` | `ConfigMap/ceph-csi-encryption-kms-config` — KMS config (currently empty, KMS not in use) |

This folder MUST reconcile before the driver folders. The parent `infrastructure/controllers/kustomization.yaml` lists `ceph-csi-base` ahead of `ceph-csi-rbd` and `ceph-csi-cephfs`.

### `ceph-csi-rbd/` and `ceph-csi-cephfs/`

Each driver folder follows the same shape:

| File | Contents |
|---|---|
| `<name>-csidriver.yaml` | `CSIDriver` — registers the driver with Kubernetes |
| `<name>-driver.yaml` | DaemonSet (one node-plugin pod per worker) + node `ServiceAccount` and ClusterRole/Binding |
| `<name>-provisioner-rbac.yaml` | Provisioner `ServiceAccount` + ClusterRole/Binding + leader-election Role/Binding |
| `<name>-controller.yaml` | Deployment (3 replicas, `Recreate` strategy) running the driver controller + sidecars (csi-provisioner, csi-attacher, csi-resizer, csi-snapshotter, liveness-prometheus) |

**Why `strategy: Recreate`** on the controller Deployments: the pods use leader election, and hard pod anti-affinity (one per node) means a `RollingUpdate` with `maxSurge: 1` deadlocks on a 3-node cluster. Recreate sidesteps this.

### `storage/ceph-rbd/` and `storage/cephfs/`

Each storage folder is two files:

| File | Purpose |
|---|---|
| `secret-<name>.sops.yaml` | SOPS-encrypted Ceph user credentials (`adminID`/`adminKey`/`userID`/`userKey`) in the `ceph-csi` namespace |
| `storageclass-<name>.yaml` | The `StorageClass` referenced by app PVCs |

Apps reference these by name (`storageClassName: ceph-rbd` or `cephfs`). The SCs in turn point at the Secret names in the `ceph-csi` namespace.

## Reconciliation flow

A single Flux Kustomization (`infra-controllers` in `flux-system`) manages everything under `infrastructure/controllers/`. It depends on `infra-repositories` (for the Flux Helm registries that other controllers use, not Ceph itself).

```
GitRepository/flux-system
        │
        ▼
Kustomization/infra-repositories ──► HelmRepositories
        │
        ▼
Kustomization/infra-controllers ──► All of infrastructure/controllers/
        │                              ├── ceph-csi-base
        │                              ├── ceph-csi-rbd
        │                              ├── ceph-csi-cephfs
        │                              ├── storage/ceph-rbd
        │                              ├── storage/cephfs
        │                              └── (cert-manager, traefik, etc.)
        ▼
   Cluster
```

Because everything is one Flux Kustomization, resources can be moved between subfolders freely without prune events — Flux tracks resource identity by `(GVK, namespace, name)`, not by source file.

## Image versions

Driver image: `quay.io/cephcsi/cephcsi:v3.16.2` — a pinned stable release, Renovate-managed.

Sidecar versions (Renovate-managed):

| Sidecar | Version |
|---|---|
| csi-provisioner | v6.2.0 |
| csi-attacher | v4.11.0 |
| csi-resizer | v2.1.0 |
| csi-snapshotter | v8.5.0 |
| csi-node-driver-registrar | v2.16.0 |

Both drivers run identical sidecar versions.

When upgrading sidecars, watch for removed feature gates (`HonorPVReclaimPolicy` was removed in csi-provisioner v6 — graduated to GA in Kubernetes 1.23).

## Common operations

**Rotate Ceph credentials**: `sops infrastructure/controllers/storage/<name>/secret-<name>.sops.yaml` — VS Code auto-encrypts on save.

**Add a new StorageClass against the same Ceph cluster**: drop a new `storageclass-*.yaml` in the relevant `storage/<name>/` folder pointing at the existing Secret.

**Reconcile after a manifest change**: `flux reconcile kustomization infra-controllers --with-source`.

**Verify health**:
```
kubectl get pods -n ceph-csi
flux get kustomizations -n flux-system | grep -v True
kubectl get sc
```

All driver pods should be Running. The provisioner Deployments run 3 replicas with leader election; only one is active at a time, the other two stand by.
