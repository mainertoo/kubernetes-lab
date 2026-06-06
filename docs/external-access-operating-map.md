# External access operating map

This page captures the current Kubernetes edge, certificate, SSO, tunnel, tailnet, and uptime-monitoring model. It is intended as the first place to check when answering: "how is this app reachable, what protects it, and what should Hermes check before changing it?"

> Discovery baseline: 2026-06-06, read-only live checks from the `production` Kubernetes context plus GitOps manifests in `mainertoo/kubernetes-lab`.

## Executive summary

- **Internal `*.lab.mainertoo.com` access is served by Traefik** in `traefik-system`.
- **Traefik is HA-scaled to three replicas** and exposed via a MetalLB `LoadBalancer` service at `192.168.90.180`.
- **TLS is centralized through cert-manager** with a Cloudflare DNS-01 `ClusterIssuer` and a wildcard certificate for `*.lab.mainertoo.com`.
- **SSO is implemented as a reusable Traefik forward-auth middleware** named `traefik-system/authentik-sso` pointing at the Authentik outpost endpoint.
- **Tailscale Operator provides separate tailnet/Funnel-style exposure** for selected apps, currently observed for Immich, Open Notebook, and Vaultwarden.
- **Gatus monitors a small operational subset**: homepage external, homepage internal, Gatus internal, Prometheus internal, and Alertmanager internal.
- **Cloudflared exists as a singleton tunnel daemon** in `cloudflared`; its exact external hostname routing is not fully documented in-repo and remains a follow-up inventory item.

## Live components

| Layer | Namespace | Live object | Current state |
|---|---|---|---|
| Edge proxy | `traefik-system` | `deployment/traefik-proxy` | `3/3` available, image `docker.io/traefik:v3.6.7` |
| Edge VIP | `traefik-system` | `service/traefik-proxy` | `LoadBalancer`, external IP `192.168.90.180`, ports `80`/`443` |
| SSO | `authentik` | `deployment/authentik-server` | `1/1` available, image `ghcr.io/goauthentik/server:2026.5.2` |
| SSO worker | `authentik` | `deployment/authentik-worker` | `1/1` available |
| SSO database | `authentik` | `pod/authentik-db-1` | CNPG-backed, `2/2` running |
| Certificates | `cert-manager` | controller/cainjector/webhook | each `2/2` available, image `quay.io/jetstack/*:v1.20.2` |
| Tailnet ingress | `tailscale` | `deployment/operator` | `1/1` available, image `tailscale/k8s-operator:v1.98.4` |
| Public tunnel | `cloudflared` | `deployment/cloudflared` | singleton tunnel daemon; token supplied from SOPS secret |
| Uptime checks | `gatus` | `deployment/gatus` | `1/1` available, image `ghcr.io/twin/gatus:v5.36.0` |

## GitOps source-of-truth map

| Concern | GitOps path |
|---|---|
| Traefik HelmRelease, values, namespace, file provider | `infrastructure/controllers/traefik-proxy/` |
| Traefik Authentik middleware | `infrastructure/controllers/traefik-proxy/authentik-middleware.yaml` |
| Traefik default TLSStore | `infrastructure/controllers/traefik-proxy/tlsstore-default.yaml` |
| cert-manager controller | `infrastructure/controllers/cert-manager/` |
| production wildcard issuer/certificate | `infrastructure/configs/cert-manager/production/` |
| Cloudflare API token secret reference | `infrastructure/secrets-shared/cloudflare-api-token.sops.yaml` |
| Cloudflared tunnel daemon | `infrastructure/controllers/cloudflared/` |
| Tailscale operator | `infrastructure/controllers/tailscale-operator/` |
| Authentik app | `apps/base/authentik/`, `apps/production/authentik/` |
| Gatus app/config | `apps/base/gatus/`, `apps/production/gatus/` |
| App-specific Traefik routes | mostly `apps/base/*/*-ingressroute.yaml` |
| App-specific Tailscale ingress | e.g. `apps/base/immich/*-tailscale-ingress.yaml`, `apps/base/vaultwarden/*-tailscale-ingress.yaml`, `apps/base/open-notebook/*-tailscale-ingress.yaml` |

## Traefik operating model

Traefik is deployed through Flux/Helm and configured for high availability:

- image tag: `v3.6.7`
- replicas: `3`
- PDB enabled with `minAvailable: 1`
- topology spread by `kubernetes.io/hostname`
- persistence disabled; TLS comes from cert-manager-managed Kubernetes secrets, not Traefik ACME storage
- Kubernetes CRD provider allows cross-namespace references so app namespaces can reference shared middleware in `traefik-system`
- HTTP redirects to HTTPS via the `web` entrypoint redirecting to `websecure`
- file provider mounted at `/config`, currently used for the PVE file-provider config

Dashboard route:

```text
Host(`traefik.lab.mainertoo.com`) && PathPrefix(`/dashboard`)
entryPoint: websecure
secretName: wildcard-lab-mainertoo-com-tls
```

## TLS and certificates

The production certificate stack is:

```text
ClusterIssuer/letsencrypt-production
  ACME server: https://acme-v02.api.letsencrypt.org/directory
  solver: Cloudflare DNS-01
  secret ref: cloudflare-api-token / api-token

Certificate/wildcard-lab-mainertoo-com
  namespace: traefik-system
  dnsNames: *.lab.mainertoo.com
  secretName: wildcard-lab-mainertoo-com-tls
```

Live certificate status observed:

| Namespace | Certificate | Ready | Secret |
|---|---|---:|---|
| `traefik-system` | `wildcard-lab-mainertoo-com` | `True` | `wildcard-lab-mainertoo-com-tls` |
| `cnpg-system` | `barman-cloud-client` | `True` | `barman-cloud-client-tls` |
| `cnpg-system` | `barman-cloud-server` | `True` | `barman-cloud-server-tls` |

Traefik's default `TLSStore` points to the wildcard secret:

```text
TLSStore/default -> wildcard-lab-mainertoo-com-tls
```

## Authentik SSO pattern

Shared middleware:

```text
traefik-system/authentik-sso
```

Forward-auth target:

```text
http://authentik-server.authentik.svc.cluster.local/outpost.goauthentik.io/auth/traefik
```

The middleware forwards Authentik identity headers such as:

- `X-authentik-username`
- `X-authentik-groups`
- `X-authentik-email`
- `X-authentik-name`
- `X-authentik-uid`
- `Authorization`

### Routes currently using `authentik-sso`

Read-only live discovery found **25 Traefik routes** referencing `traefik-system/authentik-sso`, including:

- `hermes.lab.mainertoo.com`
- `homepage.lab.mainertoo.com`
- `loki.lab.mainertoo.com`
- `open-notebook.lab.mainertoo.com`
- `code-server.lab.mainertoo.com` / `vscode.lab.mainertoo.com`
- `node-red.lab.mainertoo.com`
- `zigbee2mqtt.lab.mainertoo.com`
- `esphome.lab.mainertoo.com`
- selected media/admin utilities such as `sonarr`, `radarr`, `prowlarr`, `bazarr`, `lidarr`, and related routes

### Routes without the shared Authentik middleware

Read-only live discovery found **65 Traefik routes** without the shared Authentik middleware. This is not automatically wrong: many apps have native authentication, are intentionally family-facing, have app-specific exposure requirements, or may be protected elsewhere.

High-signal examples that should be understood before changing auth posture:

| Route | Notes / likely reason to review |
|---|---|
| `authentik.lab.mainertoo.com` | identity provider itself; must remain reachable for login flows |
| `vaultwarden.lab.mainertoo.com` | security-sensitive app with native auth and Tailscale ingress also present |
| `immich.lab.mainertoo.com` | family-facing app with separate Tailscale/Funnel ingress |
| `home-assistant.lab.mainertoo.com`, `ha.lab.mainertoo.com` | HA has native auth and mobile-app constraints |
| `wiki.lab.mainertoo.com` | docs/source-of-truth app; currently no shared Authentik middleware observed |
| `gatus.lab.mainertoo.com` | status page; middleware commented out in repo |
| `grafana.lab.mainertoo.com`, `prometheus.lab.mainertoo.com`, `alerts.lab.mainertoo.com` | monitoring surfaces; confirm native/SSO posture before widening exposure |
| `plex.lab.mainertoo.com`, `jellyfin.lab.mainertoo.com` | media apps with app-specific auth/exposure patterns |
| `paperless.lab.mainertoo.com` | sensitive document app; current route had no shared middleware observed |

Treat missing `authentik-sso` as an **inventory/review signal**, not a break-glass finding.

## Tailscale ingress and Funnel-style exposure

Classic Kubernetes `Ingress` resources using `ingressClassName: tailscale` were observed for:

| Namespace | Ingress | Address |
|---|---|---|
| `immich` | `immich-tailscale` | `immich.tuxedo-halosaur.ts.net` |
| `open-notebook` | `open-notebook-tailscale` | `open-notebook.tuxedo-halosaur.ts.net` |
| `vaultwarden` | `vaultwarden-tailscale` | `vaultwarden.tuxedo-halosaur.ts.net` |

Repo patterns show `tailscale.com/funnel: "true"` on Immich and Vaultwarden, with `tailscale.com/tags: "tag:k8s"` on initial provisioning. Immich's manifest explicitly notes a MagicDNS cutover/collision dependency with the old QNAP tailscale sidecar.

## Cloudflare tunnels

`cloudflared` is deployed as a singleton:

```text
namespace: cloudflared
image: cloudflare/cloudflared:latest
args: tunnel --no-autoupdate run --token $(CLOUDFLARED_TOKEN)
secret: cloudflared-secret / CLOUDFLARED_TOKEN
```

The tunnel token is SOPS-managed and should never be printed. The current repo manifest does not enumerate hostname-to-service mappings; those likely live in Cloudflare's tunnel configuration. That external mapping remains a documentation gap.

## Gatus checks

Current GitOps Gatus config includes:

| Endpoint | Group | URL | Expected condition |
|---|---|---|---|
| `homepage-external` | external | `https://homepage.lab.mainertoo.com` | status `< 400` |
| `homepage-internal` | internal | `http://homepage.homepage.svc.cluster.local:3000` | status `200` |
| `gatus-internal` | internal | `http://gatus.gatus.svc.cluster.local/health` | status `200` |
| `prometheus-internal` | internal | Prometheus service DNS `/-/ready` | status `200` |
| `alertmanager-internal` | internal | Alertmanager service DNS `/-/ready` | status `200` |

Alerts go to Discord and Alertmanager with default thresholds:

- failure threshold: `5`
- success threshold: `2`
- Alertmanager severity: `warning`
- source label: `gatus`

## Operational checks for Hermes

Read-only edge/access checks Hermes can safely run:

```bash
kubectl --context production -n traefik-system get deploy,svc,pods,ingressroute,middleware,tlsstore,certificate
kubectl --context production -n cert-manager get deploy,pods
kubectl --context production get clusterissuer,certificate -A
kubectl --context production get ingressroute -A
kubectl --context production get ingress -A
kubectl --context production -n authentik get deploy,pods,svc
kubectl --context production -n tailscale get deploy,statefulset,pods,svc
kubectl --context production -n gatus get deploy,pods,svc
```

Useful morning-digest signals:

- Traefik deployment available replicas less than desired.
- Traefik LoadBalancer IP missing or changed from `192.168.90.180`.
- `ClusterIssuer/letsencrypt-production` not `Ready=True`.
- `Certificate/wildcard-lab-mainertoo-com` not `Ready=True` or near expiry.
- Authentik server/worker/database unavailable.
- Tailscale operator unavailable or Tailscale proxy StatefulSets not ready.
- Gatus unavailable, or Gatus reports failed endpoints if API/status data is accessible.
- Unexpected growth in `IngressRoute` objects without a documented auth/exposure classification.

## Change-control policy

Safe without asking:

- Read-only `kubectl get/describe/logs` checks.
- GitOps repo inspection.
- Wiki.js reads.
- Drafting docs-only branches/PRs.

Ask first:

- Changing Traefik Helm values, entrypoints, file providers, dashboard exposure, or middleware defaults.
- Adding/removing `authentik-sso` from an app route.
- Changing Cloudflare tunnel tokens or hostname mappings.
- Changing Tailscale Funnel exposure or MagicDNS hostnames.
- Rotating Cloudflare/cert-manager/Auth/Tailscale secrets.
- Restarting Traefik, Authentik, cloudflared, Tailscale operator, or Gatus.
- Any direct live cluster mutation outside GitOps except emergency mitigation explicitly approved by the user.

## Documentation gaps / next inventory targets

1. **Cloudflare tunnel hostname map** — identify exactly which public `*.mainertoo.com` hostnames map through the `cloudflared` tunnel and which apps they reach.
2. **Pangolin/newt map** — Wiki.js references Pangolin/newt, but this pass only verified the Kubernetes `cloudflared` singleton and did not inspect the RackNerd/Pangolin side.
3. **Auth posture classification** — classify each route as one of: Authentik middleware, native app auth, intentionally public, tailnet/Funnel, monitoring-only, or needs review.
4. **Gatus expansion** — current Gatus checks are a useful seed but do not yet cover the full edge/auth/cert stack.
5. **MetalLB/kube-vip tie-in** — Traefik's VIP depends on MetalLB; the control-plane VIP depends on kube-vip. Both still deserve their own operating-map pages.

## Related wiki pages

- [Traefik](/infrastructure/software/traefik)
- [External access](/infrastructure/external-access)
- [Cloudflare tunnels](/infrastructure/external-access/cloudflare-tunnels)
- [Authentik SSO](/infrastructure/external-access/authentik-sso)
- [DNS — Tailscale split](/infrastructure/networking/dns-tailscale-split)
- [MetalLB IPAM](/infrastructure/networking/metallb-ipam)
- [kube-vip](/infrastructure/networking/kube-vip)
- [Gatus](/apps/infra-adjacent/gatus)
