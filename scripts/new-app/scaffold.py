#!/usr/bin/env python3
"""
scaffold.py — generate a convention-correct app deployment for the kubernetes-lab
(Hephaestus) GitOps repo.

Encodes the repo's institutional patterns so a new app is right the first time:
  - the TWO-kustomization split (apps/base/<app> + apps/production/<app> Flux CR)
  - label-driven volsync backups (backup: daily|hourly + backup-engine: kopia)
  - Gatus internal + external endpoints via components/gatus/{internal,external}
  - SOPS decryption wiring on the Flux Kustomization
  - bjw-s app-template HelmRelease, IngressRoute, Homepage annotations

It writes the common single-controller / single-container case. For special cases
(multi-container pods, CNPG databases, GPU nodeSelectors, advancedMounts) generate
the skeleton then hand-tune the release — see SKILL.md for canonical examples.

Run from the repo root (or pass --repo). Nothing is committed; you review + PR.
"""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import Path
from string import Template

AGE_RECIPIENT = "age1znqy83dnxfsdg88ahwxgt2dmazcj5aandmyg5fxty0nheup0jums6grjvk"

# ---------------------------------------------------------------------------
# Templates ($-substitution; literal { } in YAML pass through untouched)
# ---------------------------------------------------------------------------
NAMESPACE = Template("""apiVersion: v1
kind: Namespace
metadata:
  name: ${app}
  annotations:
    # Never let a kustomization edit delete the namespace (and everything in it).
    kustomize.toolkit.fluxcd.io/prune: disabled
""")

PVC = Template("""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${pvc_name}
  namespace: ${app}
${labels}spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${pvc_class}
  resources:
    requests:
      storage: ${pvc_size}
""")

SECRET_STUB = Template("""# STUB — fill in real values then encrypt:
#   sops --encrypt --in-place apps/base/${rel}/${app}-secret.sops.yaml
# (or open in VS Code, which auto-encrypts *.sops.yaml on save)
# Verify it shows ENC[ before committing — a .sops.yaml suffix does NOT
# guarantee encryption.
apiVersion: v1
kind: Secret
metadata:
  name: ${app}-secret
  namespace: ${app}
type: Opaque
stringData:
  EXAMPLE_KEY: "changeme"
""")

RELEASE = Template("""apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: ${app}
  namespace: ${app}
spec:
  interval: 30m
  chartRef:
    kind: OCIRepository
    name: app-template
    namespace: flux-system

  maxHistory: 2

  install:
    createNamespace: false
    remediation:
      retries: 3

  upgrade:
    cleanupOnFail: true
    remediation:
      retries: 3

  uninstall:
    keepHistory: false

  values:
    controllers:
      ${app}:
        containers:
          app:
            image:
              repository: ${image}
              tag: ${tag}
            env:
              - name: TZ
                value: America/Los_Angeles
${envfrom}            # NOTE: health probes are disabled by default — the generic / path
            # may not return 200. Enable a real readiness/liveness check once
            # you know the app's health endpoint.
            probes:
              liveness: { enabled: false }
              readiness: { enabled: false }
              startup: { enabled: false }

    service:
      app:
        controller: ${app}
        ports:
          http:
            port: ${port}
${persistence}""")

PERSISTENCE = Template("""
    persistence:
      config:
        existingClaim: ${pvc_name}
        globalMounts:
          - path: ${mount_path}
""")

ENVFROM = """            envFrom:
              - secretRef:
                  name: ${app}-secret
"""

INGRESSROUTE = Template("""apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: ${app}-ingressroute
  namespace: ${app}
  annotations:
    gethomepage.dev/href: "https://${internal_host}"
    gethomepage.dev/enabled: "true"
    gethomepage.dev/name: "${title}"
    gethomepage.dev/description: "${title}"
    gethomepage.dev/app: ${app}
    gethomepage.dev/group: "${hp_group}"
    gethomepage.dev/icon: "${hp_icon}"
spec:
  entryPoints:
    - websecure
    - web
  routes:
    - match: Host(`${internal_host}`)
      kind: Rule
${middlewares}      services:
        - name: ${app}
          port: ${port}
          scheme: http
  tls: {}
""")

MIDDLEWARES = """      middlewares:
        - name: authentik-sso
          namespace: traefik-system
"""

BASE_KUSTOMIZATION = Template("""apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: ${app}
resources:
${resources}""")

WRAPPER = Template("""---
# Flux Kustomization for the ${app} app.
# The gatus/internal + gatus/external components establish both health-check
# endpoints declaratively (auto-discovered ConfigMaps labelled gatus.io/enabled).
#
# yaml-language-server: $$schema=https://kube-schemas.pages.dev/kustomize.toolkit.fluxcd.io/kustomization_v1.json
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: &app ${app}
  namespace: flux-system
spec:
  interval: 1h
  path: ./apps/base/${rel}
  targetNamespace: ${app}
  prune: true
  retryInterval: 2m
  timeout: 5m
  wait: false
  decryption:
    provider: sops
    secretRef:
      name: sops-age
  sourceRef:
    kind: GitRepository
    name: flux-system
    namespace: flux-system
  components:
    - ${comp_prefix}/components/gatus/internal
    - ${comp_prefix}/components/gatus/external
  postBuild:
    substitute:
      APP: *app
      SUBDOMAIN: ${subdomain}
      GATUS_DOMAIN: ${gatus_domain}
      GATUS_PORT: "${port}"
      GATUS_PATH: ${gatus_path}
      GATUS_STATUS_INTERNAL: "${gatus_status_internal}"
""")


def kebab(s: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", s.lower()).strip("-")


def titleize(app: str) -> str:
    return " ".join(w.capitalize() for w in app.split("-"))


def main() -> int:
    p = argparse.ArgumentParser(description="Scaffold a kubernetes-lab app deployment.")
    p.add_argument("--app", required=True, help="App name (kebab-case)")
    p.add_argument("--image", required=True, help="Container image repository")
    p.add_argument("--tag", required=True, help="Container image tag")
    p.add_argument("--port", required=True, type=int, help="Container/service port")
    p.add_argument("--base-path", default="", help="Sub-path under apps/base, e.g. 'media'")
    p.add_argument("--namespace", default=None, help="Namespace (default: app name)")
    p.add_argument("--internal-host", default=None,
                   help="Internal IngressRoute host (default <app>.lab.mainertoo.com)")
    sso = p.add_mutually_exclusive_group()
    sso.add_argument("--sso", dest="sso", action="store_true", help="Front with Authentik SSO (default)")
    sso.add_argument("--no-sso", dest="sso", action="store_false", help="No SSO middleware")
    p.set_defaults(sso=True)
    # PVC
    p.add_argument("--pvc", action="store_true", help="Create a config PVC")
    p.add_argument("--pvc-name", default=None, help="PVC name (default <app>-config)")
    p.add_argument("--pvc-size", default="1Gi")
    p.add_argument("--pvc-class", default="ceph-rbd")
    p.add_argument("--mount-path", default="/config")
    p.add_argument("--backup", choices=["none", "daily", "hourly"], default="none",
                   help="volsync backup tier (adds backup + backup-engine: kopia labels)")
    # Secret
    p.add_argument("--secret", action="store_true", help="Create a SOPS secret stub + wire envFrom")
    # Gatus / external
    p.add_argument("--subdomain", default=None, help="External Gatus subdomain (default app)")
    p.add_argument("--gatus-domain", default="lab.mainertoo.com",
                   help="External Gatus domain (use mainertoo.com for public/Pangolin apps)")
    p.add_argument("--gatus-path", default="/")
    p.add_argument("--gatus-status-internal", default="200")
    # Homepage
    p.add_argument("--hp-group", default="Apps")
    p.add_argument("--hp-icon", default=None, help="Homepage icon (default sh-<app>.png)")
    # plumbing
    p.add_argument("--repo", default=".", help="Repo root (default cwd)")
    p.add_argument("--force", action="store_true", help="Overwrite existing app dir")
    args = p.parse_args()

    app = kebab(args.app)
    ns = args.namespace or app
    rel = f"{kebab(args.base_path)}/{app}" if args.base_path else app
    internal_host = args.internal_host or f"{app}.lab.mainertoo.com"
    subdomain = args.subdomain or app
    pvc_name = args.pvc_name or f"{app}-config"
    hp_icon = args.hp_icon or f"sh-{app}.png"
    title = titleize(app)
    # components/ is at repo root; depth from apps/production/<rel> is 2 + path segs
    comp_prefix = "/".join([".."] * (2 + rel.count("/") + 1))

    repo = Path(args.repo).resolve()
    base_dir = repo / "apps" / "base" / rel
    prod_dir = repo / "apps" / "production" / rel
    prod_list = repo / "apps" / "production" / "kustomization.yaml"

    if base_dir.exists() and not args.force:
        print(f"ERROR: {base_dir} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    for d in (repo / "apps" / "base", repo / "components" / "gatus" / "internal"):
        if not d.exists():
            print(f"ERROR: {d} not found — run from the kubernetes-lab repo root or pass --repo",
                  file=sys.stderr)
            return 1

    base_dir.mkdir(parents=True, exist_ok=True)
    prod_dir.mkdir(parents=True, exist_ok=True)

    written = []

    # namespace
    (base_dir / f"{app}-namespace.yaml").write_text(NAMESPACE.substitute(app=app))
    written.append(f"{app}-namespace.yaml")

    resources = [f"  - {app}-namespace.yaml"]

    # secret stub
    if args.secret:
        (base_dir / f"{app}-secret.sops.yaml").write_text(
            SECRET_STUB.substitute(app=app, rel=rel))
        written.append(f"{app}-secret.sops.yaml  (PLAINTEXT STUB — encrypt before commit)")
        resources.append(f"  - {app}-secret.sops.yaml")

    # pvc
    if args.pvc:
        labels = ""
        if args.backup != "none":
            labels = (f"  labels:\n    backup: {args.backup}\n"
                      f"    backup-engine: kopia\n")
        (base_dir / f"{app}-pvc.yaml").write_text(PVC.substitute(
            app=app, pvc_name=pvc_name, pvc_class=args.pvc_class,
            pvc_size=args.pvc_size, labels=labels))
        tag = f"  (backup: {args.backup})" if args.backup != "none" else ""
        written.append(f"{app}-pvc.yaml{tag}")
        resources.append(f"  - {app}-pvc.yaml")

    # release
    envfrom = Template(ENVFROM).substitute(app=app) if args.secret else ""
    persistence = PERSISTENCE.substitute(pvc_name=pvc_name, mount_path=args.mount_path) if args.pvc else ""
    (base_dir / f"{app}-release.yaml").write_text(RELEASE.substitute(
        app=app, image=args.image, tag=args.tag, port=args.port,
        envfrom=envfrom, persistence=persistence))
    written.append(f"{app}-release.yaml")
    resources.append(f"  - {app}-release.yaml")

    # ingressroute
    middlewares = MIDDLEWARES if args.sso else ""
    (base_dir / f"{app}-ingressroute.yaml").write_text(INGRESSROUTE.substitute(
        app=app, internal_host=internal_host, title=title, hp_group=args.hp_group,
        hp_icon=hp_icon, port=args.port, middlewares=middlewares))
    written.append(f"{app}-ingressroute.yaml")
    resources.append(f"  - {app}-ingressroute.yaml")

    # base kustomization
    (base_dir / "kustomization.yaml").write_text(BASE_KUSTOMIZATION.substitute(
        app=app, resources="\n".join(resources) + "\n"))
    written.append("kustomization.yaml")

    # production Flux Kustomization wrapper
    (prod_dir / "kustomization.yaml").write_text(WRAPPER.substitute(
        app=app, rel=rel, comp_prefix=comp_prefix, subdomain=subdomain,
        gatus_domain=args.gatus_domain, port=args.port, gatus_path=args.gatus_path,
        gatus_status_internal=args.gatus_status_internal))

    # patch production aggregate list (insert at top of resources:)
    entry = f"  - {rel}/kustomization.yaml"
    txt = prod_list.read_text()
    if entry.strip() not in txt:
        txt = re.sub(r"(resources:\n)", rf"\1{entry}\n", txt, count=1)
        prod_list.write_text(txt)
        patched = "added"
    else:
        patched = "already present"

    # validate base render
    try:
        subprocess.run(["kubectl", "kustomize", str(base_dir)],
                       check=True, capture_output=True, text=True)
        validation = "PASS (kubectl kustomize)"
    except Exception as e:  # noqa: BLE001
        out = getattr(e, "stderr", str(e))
        validation = f"FAILED:\n{out}"

    print("=" * 64)
    print(f"Scaffolded app: {app}")
    print(f"  apps/base/{rel}/         -> {', '.join(written)}")
    print(f"  apps/production/{rel}/kustomization.yaml  (Flux CR + gatus internal+external)")
    print(f"  apps/production/kustomization.yaml         -> entry {patched}")
    print(f"  base render validation: {validation}")
    print("=" * 64)
    print("NEXT STEPS:")
    n = 1
    if args.secret:
        print(f"  {n}. Fill apps/base/{rel}/{app}-secret.sops.yaml then encrypt + verify ENC[.")
        n += 1
    if args.gatus_domain == "mainertoo.com":
        print(f"  {n}. Add the Pangolin resource in the VPS UI: {subdomain}.mainertoo.com")
        print(f"     -> target {app}.{ns}.svc.cluster.local:{args.port}")
        n += 1
    print(f"  {n}. Enable real health probes in {app}-release.yaml once you know the app's path.")
    print(f"  {n + 1}. Commit on a fresh branch from master and open a PR (kube-flux-diff validates).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
