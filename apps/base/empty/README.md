# apps/base/empty

This directory contains a single `kustomization.yaml` with an empty resources list:

```yaml
resources: []
```

## Why it exists

Some apps have multiple PVCs managed by separate Flux `Kustomization` objects. For example, `dawarich` has three:

- `apps/production/dawarich/app.yaml` → points at `apps/base/dawarich/` (the HelmRelease, IngressRoute, etc.)
- `apps/production/dawarich/db.yaml` → needs to create a PVC + volsync ReplicationSource for the postgres volume
- `apps/production/dawarich/media.yaml` → same, for the media volume

The db and media Kustomizations don't need any app manifests — they only need the volsync `Components` (which add the PVC and ReplicationSource on top). But a Flux `Kustomization` requires a valid `path` that contains *something*. Pointing them at `apps/base/empty` gives Flux a valid, renderable path that produces zero base resources, onto which the Components layer their PVC definitions cleanly.

Without this, you'd either have to duplicate empty kustomization files into every app directory, or restructure how volsync Components work.

## Is there a cleaner approach?

Yes — ideally each app's base kustomization would live alongside its volsync config so there's only one Flux Kustomization per app. That requires moving to `spec.components` directly on the app Kustomization rather than separate per-PVC Kustomizations. This is the direction the newer single-PVC apps (e.g. homepage, homebox) already use — they include `volsync-v2` as a component on the same Kustomization that points at `apps/base/<app>`.

The multi-Kustomization pattern (using `apps/base/empty`) predates that approach and is used by apps that were migrated before the single-Kustomization pattern was established. It works correctly but means more Flux objects per app.

**Do not delete this directory** — removing it will break all Flux Kustomizations that use `path: ./apps/base/empty` as their target.

Current users: calibre, authentik, sparky-fitness, dawarich, wiki-js, riven, tracearr, zilean, calibre-web-automated, notifiarr, shelfmark, jackett, cinesync (and others).
