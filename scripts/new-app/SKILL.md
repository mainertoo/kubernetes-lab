---
name: new-app
description: Scaffold a convention-correct app deployment in the kubernetes-lab (Hephaestus) GitOps repo — the two-kustomization split (apps/base + apps/production Flux Kustomization), bjw-s app-template HelmRelease, Traefik IngressRoute, label-driven volsync backup PVCs, SOPS secret wiring, and Gatus internal+external health endpoints via components/gatus. Use whenever adding a NEW app to the cluster so the repo's patterns are applied correctly the first time instead of rediscovered per app.
---

# new-app — scaffold a kubernetes-lab app deployment

Generates the full file set for a new app from the repo's canonical patterns, so
nothing standard is missed. Backed by `scaffold.py` (deterministic generation) in
this skill's directory. **Run from the kubernetes-lab repo root.** Nothing is
committed — you review, then PR.

## When to use
- The user wants to deploy a new app/service to the cluster.
- Not for editing an existing app, infra controllers, or staging-only manifests
  (staging lives under `infrastructure/controllers-staging/`, a different shape).

## What it generates
For app `<app>` (optionally under a `--base-path` like `media`):

```
apps/base/<path>/<app>/
  <app>-namespace.yaml
  <app>-release.yaml        # bjw-s app-template HelmRelease (OCIRepository app-template)
  <app>-ingressroute.yaml   # Traefik IngressRoute (+ Homepage annotations, optional SSO)
  <app>-pvc.yaml            # optional; backup: daily|hourly + backup-engine: kopia labels
  <app>-secret.sops.yaml    # optional STUB (plaintext — you fill + encrypt)
  kustomization.yaml
apps/production/<path>/<app>/
  kustomization.yaml        # Flux Kustomization CR: sops decryption + gatus components + postBuild
apps/production/kustomization.yaml   # patched: new entry added
```

The Flux Kustomization wraps `components/gatus/internal` + `components/gatus/external`
with `postBuild.substitute`, which is how the repo **establishes a Gatus internal
AND external endpoint per app** (auto-discovered ConfigMaps labelled
`gatus.io/enabled: "true"`). It also carries `decryption: { provider: sops }` so any
`*.sops.yaml` decrypts at reconcile.

## How to run

Confirm the inputs with the user first, then:

```bash
SKILL=~/.claude/skills/new-app
# Internal app, SSO-fronted, with a backed-up config PVC:
python3 $SKILL/scaffold.py \
  --app paperless-ngx --image ghcr.io/paperless-ngx/paperless-ngx --tag 2.14 \
  --port 8000 --pvc --backup daily --secret --sso

# Public app exposed via Pangolin (external Gatus check hits the public domain):
python3 $SKILL/scaffold.py \
  --app myguide --image nginx --tag 1.27-alpine --port 80 \
  --no-sso --subdomain myguide --gatus-domain mainertoo.com

# App under a sub-path (apps/base/media/<app>):
python3 $SKILL/scaffold.py --app navidrome --base-path media \
  --image deluan/navidrome --tag 0.53 --port 4533 --pvc --backup daily
```

### Key flags
- `--app` `--image` `--tag` `--port` — required.
- `--base-path media` — nest under `apps/base/media/`.
- `--sso` (default) / `--no-sso` — Authentik forward-auth middleware on the IngressRoute.
- `--pvc` `--pvc-size 5Gi` `--mount-path /config` `--backup daily|hourly` — config PVC;
  `--backup` adds the label-driven volsync labels (see `docs/label-driven-backups.md`).
  Omit `--backup` (or `none`) for stateless/reproducible data.
- `--secret` — emit a SOPS secret stub and wire `envFrom` into the container.
- `--subdomain` `--gatus-domain` — external Gatus check target. Default domain is
  `lab.mainertoo.com` (internal-via-Traefik); use `mainertoo.com` for public/Pangolin apps.
- `--hp-group "Apps"` `--hp-icon sh-foo.png` — Homepage dashboard tile.

## After scaffolding (the script prints these)
1. If `--secret`: fill `<app>-secret.sops.yaml`, then `sops --encrypt --in-place` it
   (or save in VS Code) and **verify it shows `ENC[`** — the `.sops.yaml` suffix alone
   does NOT guarantee encryption.
2. If public (`--gatus-domain mainertoo.com`): add the resource in the **Pangolin UI**
   on the VPS → `<subdomain>.mainertoo.com` → target `<app>.<ns>.svc.cluster.local:<port>`.
3. Replace the disabled health probes in `<app>-release.yaml` with a real
   readiness/liveness check once you know the app's health path.
4. Commit on a **fresh branch from master** and open a PR — `kube-flux-diff` CI
   validates the render + posts the diff. PRs target `master`.

## Extending for non-standard apps (script covers the ~80% case)
The generator writes a single-controller / single-container app. For more, scaffold
first, then hand-edit `<app>-release.yaml` using a canonical example:
- **Multi-container in one pod** (sidecars): `apps/base/mkdocs/mkdocs-release.yaml`
  (git-sync + build + nginx, shared volumes via `advancedMounts`).
- **CNPG database**: add a `db-cnpg.yaml` and a `dependsOn` on the db Flux Kustomization
  (see `apps/base/immich/` + `apps/production/immich/`). DB creds come from the
  CNPG-generated `<cluster>-app` secret.
- **GPU / node pinning**: `nodeSelector` under `controllers.<name>.pod`, e.g.
  `intel.feature.node.kubernetes.io/gpu: "true"` (see immich).
- **Multiple services** (bjw-s names them `<release>-<key>`, e.g. `immich-app`): a
  single service is named just `<release>` — keep one service unless you need more,
  or the IngressRoute/Gatus targets must use the `<release>-<key>` form.

## Guardrails
- Never `kubectl apply`; all changes go through git + Flux reconciliation.
- The script refuses to overwrite an existing app dir unless `--force`.
- It inserts the production-aggregate entry at the top of `resources:` — ordering is
  cosmetic; move it next to siblings if you like.
