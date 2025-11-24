
# Ceph Storage Integration for K3s (Flux + Rook) ### AI GENERATED ###

## Overview
This document explains how to integrate CephFS and Ceph RBD with your GitOps-managed K3s cluster using Flux, Kustomize, and Rook-Ceph.

## CephFS StorageClass
```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: cephfs
provisioner: cephfs.csi.ceph.com
parameters:
  clusterID: rook-ceph
  fsName: ceph-filesystem
  pool: cephfs_data
  mounter: kernel
reclaimPolicy: Retain
allowVolumeExpansion: true
mountOptions:
  - noatime
```

## Ceph RBD StorageClass
```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ceph-rbd
provisioner: rbd.csi.ceph.com
parameters:
  clusterID: rook-ceph
  pool: kube
  imageFeatures: layering
reclaimPolicy: Retain
allowVolumeExpansion: true
mountOptions:
  - discard
```

## PVC Pattern for CephFS
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: paperless-ngx
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: cephfs
  resources:
    requests:
      storage: 20Gi
```

## HelmRelease Pattern for CephFS
```yaml
persistence:
  data:
    existingClaim: paperless-ngx
    globalMounts:
      - path: /paperless
```

## PVC Pattern for Ceph RBD
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ceph-rbd
  resources:
    requests:
      storage: 50Gi
```

## HelmRelease Pattern for Ceph RBD
```yaml
persistence:
  data:
    existingClaim: postgres-data
```

## Testing
### CephFS
```
kubectl -n <ns> exec -it deploy/<app> -- df -h /path
kubectl -n <ns> exec -it deploy/<app> -- touch /path/test
```

### RBD
```
kubectl -n <ns> exec -it deploy/<db> -- df -h /var/lib/<db>
```

## Best Practices
- Always define PVCs manually.
- Use CephFS only when RWX is required.
- Use RBD for databases.
- Keep StorageClasses immutable.
- Never manually kubectl apply resources — everything must be in Git.
