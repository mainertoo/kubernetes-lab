#!/usr/bin/env python3
"""Check the repo's manifest conventions. Exits non-zero on any error.

Run from the repo root:  python3 scripts/lint/check_conventions.py

Rules (see CLAUDE.md "Apps pattern" and the wiki page gitops/apps-pattern):
  HR01  app-template releases use chartRef -> OCIRepository/app-template
  HR02  no upgrade.force
  HR03  no top-level values.resources / values.ingress in app-template values
        (the chart ignores `resources` there; `ingress` is replaced by IngressRoute)
  HR04  chart.spec.version is pinned to an exact version
  HR05  image tags are pinned (no latest/main/dev/... unless digest-pinned)
  KS01  per-app Flux Kustomizations: interval 1h, retryInterval 2m, prune true,
        wait false, namespace flux-system, path under ./apps/base
  KS02  a directory holding db-cnpg.yaml also holds app.yaml and a plain
        kustomization.yaml listing both
  SOPS01 every *.sops.yaml is actually encrypted
  REF01 every YAML file in an apps/base directory is listed in its kustomization.yaml

Exceptions live in ALLOW below, each with a reason.
"""
import glob
import os
import re
import sys

import yaml

ROLLING = {"latest", "main", "master", "dev", "develop", "nightly", "edge", "stable", "rolling"}

# (rule, path-or-prefix) -> reason. Keep this short; fix the manifest when you can.
ALLOW = {
    ("HR05", "apps/base/lcshare/"): "upstream publishes no version tags (tracked in the audit page)",
    ("HR03", "apps/base/tandoor/"): "legacy ingress block, pending removal",
    ("HR03", "apps/base/kitchenowl/"): "legacy ingress block, pending removal",
    ("HR03", "apps/base/vaultwarden/"): "legacy ingress block, pending removal",
    ("HR03", "apps/base/mealie/"): "legacy ingress block, pending removal",
    ("REF01", "apps/base/rancher/rancher-adfsconfig-spkey.sops.yaml"): "manual-apply secret, see apps/base/rancher/README.md",
    ("REF01", "apps/base/rancher/rancher-backup-schedule.yaml"): "consumed by apps/base/rancher-backup-schedule",
    ("REF01", "apps/base/media/jellyfin/jellyfin-restore-rsync.yaml"): "manual restore Job, see JELLYFIN-RESTORE-PLAYBOOK.md",
    ("REF01", "apps/base/home-assistant/ratgdo-logger/ratgdo-logger-deployment.yaml"): "retired 2026-09-16, kept for re-enable",
}

errors = []


def allowed(rule, path):
    return any(r == rule and path.startswith(p) for (r, p) in ALLOW)


def err(rule, path, msg):
    if not allowed(rule, path):
        errors.append(f"{rule}  {path}: {msg}")


def docs(path):
    try:
        with open(path) as fh:
            return [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        err("YAML", path, f"does not parse: {str(exc)[:80]}")
        return []


def walk_images(node, path):
    if isinstance(node, dict):
        repo, tag = node.get("repository"), node.get("tag")
        if isinstance(repo, str) and "tag" in node:
            t = str(tag)
            if "@sha256:" not in t and not node.get("digest") and (t in ROLLING or tag is None):
                err("HR05", path, f"image {repo} uses rolling tag '{t}'")
        for v in node.values():
            walk_images(v, path)
    elif isinstance(node, list):
        for v in node:
            walk_images(v, path)


def check_helmreleases():
    files = glob.glob("apps/base/**/*.yaml", recursive=True) + glob.glob("infrastructure/**/*.yaml", recursive=True)
    for f in sorted(files):
        if "/chart/" in f or f.endswith("gotk-components.yaml"):
            continue
        for d in docs(f):
            if d.get("kind") != "HelmRelease":
                continue
            spec = d.get("spec") or {}
            values = spec.get("values") or {}
            chart = (spec.get("chart") or {}).get("spec") or {}
            ref = spec.get("chartRef") or {}
            is_app_template = ref.get("name") == "app-template" or chart.get("chart") == "app-template"
            if chart:
                ver = chart.get("version")
                local = str(chart.get("chart", "")).startswith("./")
                if not local and (ver is None or not re.fullmatch(r"v?\d+\.\d+\.\d+([-+].+)?", str(ver))):
                    err("HR04", f, f"chart.spec.version is not pinned (got {ver!r})")
            if is_app_template:
                if chart:
                    err("HR01", f, "use chartRef to OCIRepository/app-template, not chart.spec")
                elif ref != {"kind": "OCIRepository", "name": "app-template", "namespace": "flux-system"}:
                    err("HR01", f, f"unexpected chartRef {ref}")
                if (spec.get("upgrade") or {}).get("force"):
                    err("HR02", f, "upgrade.force must not be set")
                for key in ("resources", "ingress"):
                    if key in values:
                        err("HR03", f, f"top-level values.{key} is not used; move it onto the container / use an IngressRoute")
            walk_images(values, f)


def check_overlays():
    for root in ("apps/production", "apps/staging"):
        for f in sorted(glob.glob(f"{root}/**/*.yaml", recursive=True)):
            for d in docs(f):
                if d.get("kind") != "Kustomization" or "toolkit.fluxcd.io" not in d.get("apiVersion", ""):
                    continue
                s, m = d.get("spec") or {}, d.get("metadata") or {}
                want = {"interval": "1h", "retryInterval": "2m", "prune": True, "wait": False}
                for k, v in want.items():
                    if s.get(k) != v:
                        err("KS01", f, f"spec.{k} should be {v!r} (got {s.get(k)!r})")
                if m.get("namespace") != "flux-system":
                    err("KS01", f, "metadata.namespace should be flux-system")
                if not str(s.get("path", "")).startswith("./apps/base"):
                    err("KS01", f, f"spec.path should start with ./apps/base (got {s.get('path')!r})")
        for f in sorted(glob.glob(f"{root}/**/db-cnpg.yaml", recursive=True)):
            d = os.path.dirname(f)
            k = os.path.join(d, "kustomization.yaml")
            if not os.path.exists(os.path.join(d, "app.yaml")):
                err("KS02", f, "database overlay needs a sibling app.yaml")
            kd = docs(k)[0] if os.path.exists(k) and docs(k) else {}
            if "kustomize.config.k8s.io" not in kd.get("apiVersion", "") or set(kd.get("resources") or []) != {"db-cnpg.yaml", "app.yaml"}:
                err("KS02", k, "must be a plain kustomization listing db-cnpg.yaml and app.yaml")


def check_sops():
    for f in sorted(glob.glob("**/*.sops.yaml", recursive=True)):
        text = open(f).read()
        if "ENC[" not in text or not re.search(r"^sops:", text, re.M):
            err("SOPS01", f, "file has the .sops.yaml suffix but is not encrypted")


def check_references():
    for k in sorted(glob.glob("apps/base/**/kustomization.yaml", recursive=True)):
        d = os.path.dirname(k)
        if "/chart" in d:
            continue
        text = open(k).read()
        live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not (os.path.isfile(p) and name.endswith((".yaml", ".yml")) and name != "kustomization.yaml"):
                continue
            if name not in live:
                err("REF01", p, "not referenced by its kustomization.yaml (delete it or list it)")


def main():
    check_helmreleases()
    check_overlays()
    check_sops()
    check_references()
    if errors:
        print(f"{len(errors)} convention error(s):\n")
        print("\n".join(errors))
        print("\nRules and exceptions: scripts/lint/check_conventions.py")
        return 1
    print("conventions OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
