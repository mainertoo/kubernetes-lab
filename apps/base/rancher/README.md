# Rancher DR Guide

Rancher stores all runtime configuration — users, projects, RBAC, and auth providers — as Kubernetes custom resources (CRs) in etcd. There is no database or PVC to restore. Two layers of protection are in place.

## What's backed up

| File | What it contains | How it's protected |
|---|---|---|
| `rancher-authconfig.yaml` | Authentik SAML config (AuthConfig CR) | In git, reconciled by Flux |
| `rancher-adfsconfig-spkey.sops.yaml` | Authentik SP RSA private key | SOPS-encrypted in git, **manual apply only** |
| `rancher-backup-operator.yaml` | rancher-backup Helm chart | Deployed by Flux |
| `rancher-backup-schedule.yaml` | Daily CRD snapshot to Garage S3 | `volsync` bucket, `rancher-backup/` folder |

---

## Option 1 — AuthConfig (Authentik SAML)

The `rancher-authconfig.yaml` is applied continuously by Flux. On a fresh Rancher deploy, once the CRDs exist, Flux will reconcile the ADFS AuthConfig automatically. No manual steps needed for the auth config itself.

### The SP private key

The `rancher-adfsconfig-spkey.sops.yaml` file contains the RSA private key that Rancher uses to sign SAML assertions sent to Authentik. This key lives in `cattle-global-data/adfsconfig-spkey` — a namespace Kustomize cannot target alongside `cattle-system`, so it is **not** reconciled by Flux. Apply it once manually after a fresh Rancher deployment:

```bash
# Decrypt and apply
sops --decrypt apps/base/rancher/rancher-adfsconfig-spkey.sops.yaml | kubectl apply -f -
```

### When Authentik rotates its signing certificate

The SAML metadata XML inside `rancher-authconfig.yaml` contains Authentik's public signing certificate (the `idpMetadataContent` block). When Authentik rotates its cert, update the file:

```bash
# Export fresh AuthConfig from the cluster
kubectl get authconfig adfs -o yaml \
  | grep -v 'resourceVersion\|uid\|creationTimestamp\|generation\|status' \
  > apps/base/rancher/rancher-authconfig.yaml
```

Review the diff, commit, and Flux will reconcile.

---

## Option 2 — Rancher Backup Operator

The `rancher-backup` Helm chart deploys an operator that takes scheduled snapshots of all Rancher management CRs (`management.cattle.io/v3`, `project.cattle.io/v3`) and stores them as `.tar.gz` in Garage S3.

- **Schedule**: daily at 02:00 UTC
- **Retention**: 10 backups
- **Location**: `garage.lab.mainertoo.com` → bucket `volsync` → folder `rancher-backup/`

### Restoring from a backup

1. Deploy a fresh Rancher (Flux handles this via `rancher-release.yaml`).
2. Wait for Rancher to be fully ready and all CRDs to exist.
3. Find the backup file to restore from Garage S3 (`volsync/rancher-backup/`).
4. Create a `Restore` CR pointing at the backup:

```yaml
apiVersion: resources.cattle.io/v1
kind: Restore
metadata:
  name: restore-from-backup
  namespace: cattle-system
spec:
  backupFilename: rancher-daily-backup-2026-05-04T02-00-00Z.tar.gz
  storageLocation:
    s3:
      bucketName: volsync
      folder: rancher-backup
      region: us-west-1
      endpoint: garage.lab.mainertoo.com
      credentialSecretName: rancher-s3-backup-creds
      credentialSecretNamespace: cattle-system
```

```bash
kubectl apply -f restore.yaml -n cattle-system
kubectl logs -n cattle-resources-system -l app.kubernetes.io/name=rancher-backup -f
```

5. After restore completes, apply the SP private key (see Option 1 above).

---

## Full DR checklist (cluster rebuild)

1. Bootstrap Flux and sops-age secret
2. Flux reconciles infrastructure + rancher HelmRelease → Rancher comes up
3. Flux reconciles `rancher-authconfig.yaml` → SAML AuthConfig restored automatically
4. `kubectl apply` the SP private key: `sops -d rancher-adfsconfig-spkey.sops.yaml | kubectl apply -f -`
5. (Optional) Apply a `Restore` CR if you need users/projects/RBAC from the S3 backup
6. Log in via Authentik SSO to verify
