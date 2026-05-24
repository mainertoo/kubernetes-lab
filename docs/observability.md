# Observability

How metrics, logs, and alerts work in this cluster. The whole stack reconciles via Flux from `apps/base/{kube-prometheus-stack,grafana,loki,alloy}/`.

## Stack at a glance

| Component | What it does | Namespace |
|---|---|---|
| **Prometheus** (via `kube-prometheus-stack`) | Pulls metrics from every ServiceMonitor / PodMonitor / Probe in the cluster every ~30s. 14d retention, 18GB max on a 20Gi PVC. | `kube-prometheus-stack` |
| **Alertmanager** | Routes Prometheus alerts to Discord via the bundled `alertmanager-discord-bridge`. | `kube-prometheus-stack` |
| **Grafana** | Dashboards + ad-hoc query UI. Authentik-fronted OIDC. Sidecar auto-imports any ConfigMap labeled `grafana_dashboard=1`. | `grafana` |
| **Loki** | Single-binary log store, 14d retention, filesystem backend on `ceph-rbd`. No memcached caches, no canary (homelab-trim). | `loki` |
| **Alloy** | DaemonSet that tails every pod's logs cluster-wide and ships them to Loki with `{namespace, pod, container, node, app, job}` labels. | `alloy` |

UI: <https://grafana.lab.mainertoo.com> (Authentik-protected). Group membership: `Grafana Admins` / `Grafana Editors` → Admin / Editor; everyone else → Viewer.

## Scrape coverage

Prometheus is configured to match **every** ServiceMonitor / PodMonitor / Probe / PrometheusRule in the cluster — no `release:` label gating. This is intentional; the chart-default selector silently excluded most app metrics. See [memory `feedback_kube_prom_servicemonitor_selector_trap`](#) for the gotcha and fix history.

Currently scraping (post-2026-05-24 broadening, 24 active scrape pools):

- **Cluster control-plane** — kube-apiserver, kubelet, coredns, kube-state-metrics, node-exporter
- **Storage** — ceph-mgr (custom ServiceMonitor)
- **Apps** — Loki, Kyverno (4 controllers), VolSync (controller + monitor), Grafana
- **Custom-enabled** — cert-manager (controller + webhook + cainjector), CNPG operator, Flux (6 controllers via `flux-podmonitor.yaml`), Gatus
- **The stack itself** — Prometheus, Alertmanager, the Prometheus Operator

### Adding a new ServiceMonitor

If a chart you install exposes Prometheus metrics, **enable its ServiceMonitor via chart values** (no label-gating needed):

```yaml
# Whatever the chart calls its prometheus / monitoring section, e.g.:
prometheus:
  enabled: true
  servicemonitor:
    enabled: true
# OR for CNPG-style charts:
monitoring:
  podMonitorEnabled: true
```

Prometheus's open selectors will pick it up on next reload (≤30s after Flux reconciles). Verify via Grafana → Explore → datasource Prometheus → query `up{job="<chart-name>"}` — should return 1.

### CNPG per-cluster metrics (special case)

The CNPG chart's `monitoring.podMonitorEnabled: true` only enables a PodMonitor for the **operator** pod. For per-cluster postgres metrics (`cnpg_*`, replication lag, `pg_stat_*`), each `Cluster` CR needs:

```yaml
spec:
  monitoring:
    enablePodMonitor: true
```

This isn't wired up cluster-wide yet — currently deferred until the alert coverage proves insufficient.

## Custom alerts

Custom rules live in `apps/base/kube-prometheus-stack/`. Two files:

- `ceph-health-prometheusrule.yaml` — Ceph health (HEALTH_WARN, HEALTH_ERR)
- `homelab-prometheusrule.yaml` — homelab-specific rules grouped by domain

The chart's defaults provide ~29 more rule groups covering kube control-plane, apiserver SLOs, node-exporter, kube-state-metrics, etc. — those don't need to be touched.

### Current homelab-custom alerts

| Group | Alert | Expression (shortened) | Severity | `for:` |
|---|---|---|---|---|
| `storage.pvc` | `PVCNearFull` | `used/capacity ≥ 0.85 and < 0.95` | warning | 30m |
| `storage.pvc` | `PVCAlmostFull` | `used/capacity ≥ 0.95` | critical | 10m |
| `backups.volsync` | `VolSyncBackupStale` | `volsync_volume_out_of_sync{role="source"} == 1` | critical | 30h |
| `backups.volsync` | `VolSyncKopiaRepoDisconnected` | `volsync_kopia_repository_connectivity == 0` | critical | 30m |
| `certificates.cert-manager` | `CertExpiringSoon` | `(cert_expiration - now) < 14d` | warning | 1h |
| `certificates.cert-manager` | `CertExpiringCritical` | `(cert_expiration - now) < 3d` | critical | 1h |

All severities flow through the existing Alertmanager → Discord bridge with no per-alert routing.

### Adding a new alert

1. Edit `apps/base/kube-prometheus-stack/homelab-prometheusrule.yaml`, add a rule under the appropriate group (or a new group).
2. **Validate the expression against live Prometheus before committing.** Port-forward via `kubectl proxy --port=8001` and query the Prometheus API:

   ```bash
   BASE='http://localhost:8001/api/v1/namespaces/kube-prometheus-stack/services/kube-prometheus-stack-prometheus:9090/proxy'
   curl -s -G "$BASE/api/v1/query" --data-urlencode 'query=<your-expression>'
   ```

   The dry-run should return either zero matches (alert is silent today) or a known-list of matches (alert is catching real existing state, which is what you want before merging).

3. Verify post-merge: `kubectl -n kube-prometheus-stack get prometheusrule homelab-custom` should show your new rule, and `https://grafana.lab.mainertoo.com/alerting/list` should list it in state `Inactive`.

### Severity convention

- `warning` — act within a day or so
- `critical` — act now, page-worthy

Both go to the same Discord channel today. Per-severity routing is a future Alertmanager config change.

## Dashboards

Grafana auto-imports any ConfigMap labeled `grafana_dashboard=1` cluster-wide (sidecar config in `apps/base/grafana/grafana-release.yaml`).

Pre-loaded by the chart on 2026-05-24, 24 dashboards available out of the box including:

- **Kubernetes / Compute Resources / Cluster** — "is the cluster healthy?"
- **Kubernetes / Compute Resources / Node** — per-host CPU/memory/disk/network
- **Kubernetes / Compute Resources / Namespace (Pods)** — drill into a specific app's resource usage
- **Node Exporter / Nodes** — full per-host hardware view
- **Persistent Volumes** — PVC growth + storage class breakdown
- **Alertmanager Overview** — current firing/silenced alerts

Plus the CNPG bundled dashboard (auto-published since 2026-05-24) under the `CloudNative-PG` folder.

To pin dashboards to your homepage: open one, click the star icon next to its title. Starred dashboards appear at the top of Grafana's home view.

## Logs (Loki + Alloy)

Alloy ships every pod's stdout/stderr to Loki with structured labels. Query in Grafana → Explore → datasource **Loki**.

### Useful starter queries

| Goal | LogQL |
|---|---|
| All errors anywhere, last 1h | `{cluster="lab"} \|~ "(?i)(error\|err\|fatal\|panic)"` |
| Errors in a specific namespace | `{namespace="dawarich"} \|~ "(?i)error"` |
| Volsync mover output | `{namespace="volsync-system"} \| logfmt` |
| CNPG cluster lifecycle events | `{namespace="<app>"} \|~ "instance\|switchover\|failover"` |
| Kubelet evictions cluster-wide | `{job="kubelet"} \|~ "evict\|OOM"` |
| Single pod, all containers | `{namespace="<ns>", pod="<pod>"}` |

Save these via the Star icon in Grafana's Explore view — they appear under "Query history" → "Starred" thereafter.

Retention is 14 days (`limits_config.retention_period: 336h` in `apps/base/loki/loki-release.yaml`).

## Operational notes

### Querying Prometheus directly

The Prometheus pod is distroless — no shell tools inside. Use `kubectl proxy`:

```bash
kubectl proxy --port=8001 &
BASE='http://localhost:8001/api/v1/namespaces/kube-prometheus-stack/services/kube-prometheus-stack-prometheus:9090/proxy'

# Instant query
curl -s -G "$BASE/api/v1/query" --data-urlencode 'query=<expression>' | jq

# List all metric names
curl -s "$BASE/api/v1/label/__name__/values" | jq -r '.data[]' | grep <prefix>

# Current scrape targets
curl -s "$BASE/api/v1/targets?state=active" | jq -r \
  '.data.activeTargets | group_by(.scrapePool) | .[] | "\(.[0].scrapePool): \(length) targets"' | sort
```

### Resource budget

| Component | PVC | CPU/RAM (approx) |
|---|---|---|
| Prometheus | 20Gi (`ceph-rbd`) | ~50m / ~1.4Gi |
| Alertmanager | 2Gi | minimal |
| Grafana | existing claim | ~50m / 200Mi |
| Loki (singleBinary) | 20Gi | ~200m / 1–2Gi |
| Alloy | none (logs in flight) | DaemonSet, ~50m / 200Mi per node |

Total observability footprint: ~42Gi PVC + minimal CPU/RAM at homelab scale.

## See also

- [`backup-architecture.md`](backup-architecture.md) — VolSync backup architecture (the `volsync_*` metrics feed `VolSyncBackupStale` + `VolSyncKopiaRepoDisconnected` here)
- [`label-driven-backups.md`](label-driven-backups.md) — how PVC labels drive VolSync backup generation
- [`cnpg-disaster-recovery.md`](cnpg-disaster-recovery.md) — CNPG cluster-level recovery (separate from observability)
- `apps/base/kube-prometheus-stack/` — all custom monitoring resources (PrometheusRules, ServiceMonitors, PodMonitor for Flux)
