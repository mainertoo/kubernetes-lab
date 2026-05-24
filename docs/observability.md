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

### Bookmark query patterns

These aren't queries you run on a schedule or alerts you build — they're **bookmarked query templates** for incident response. The pattern is:

> Something feels off / Discord pings you → open Grafana → Explore → Loki → run one of these to grep all cluster logs at once instead of `kubectl logs` whack-a-mole.

Save them via Grafana Explore → run the query → click the star next to it in "Query history". Starred queries appear under the "Starred" tab thereafter, one-click recall.

| Pattern | When you'd use it | Query |
| --- | --- | --- |
| **Cluster-wide errors, last hour** | "Did anything blow up today?" — broad sweep before bed, or after a worker host hang | `{cluster="lab"} \|~ "(?i)(error\|fatal\|panic)"` |
| **One namespace's errors** | App is misbehaving, you know which one | `{namespace="dawarich"} \|~ "(?i)error"` ← swap `dawarich` for the affected app |
| **VolSync mover output** | A `VolSyncBackupStale` or `VolSyncKopiaRepoDisconnected` alert fired — why didn't the backup work? | `{namespace="volsync-system"} \|~ "kopia\|mover\|backup"` |
| **CNPG lifecycle events** | A `CNPGCollectorDown` / `CNPGBackupFailing` alert fired, or a postgres cluster is acting weird | `{namespace="dawarich"} \|~ "switchover\|failover\|recovery\|promot"` ← swap `dawarich` for the affected CNPG namespace |
| **Kubelet evictions cluster-wide** | Descheduler did something visible, memory pressure showed up, or a worker is misbehaving | `{job="kubelet"} \|~ "evict\|OOM\|memory pressure"` |
| **Single pod, all containers** | Drilling into one pod's logs (Alloy ships stdout for all containers including init containers + sidecars) | `{namespace="<ns>", pod="<pod>"}` |

Skip these entirely if you're not the kind of person who'd reach for log search — the dashboards + Discord alerts already cover proactive monitoring. Loki shines specifically for *"why did X happen at 03:42 this morning?"*

### Operational pointers

- **Retention** is 14 days (`limits_config.retention_period: 336h` in `apps/base/loki/loki-release.yaml`). Logs older than that are deleted by the compactor.
- **Time range** — Grafana Explore defaults to the last hour. For overnight incidents bump it to "Last 24 hours" before running the query.
- **Rate vs lines** — `|~ "error"` returns matching lines. Wrap with `rate(... [5m])` to get matches/sec for graphing (e.g., to see error spikes during a specific window). The query bar's `Logs` / `Metrics` toggle switches modes.

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
| --- | --- | --- |
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
