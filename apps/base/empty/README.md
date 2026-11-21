# apps/base/empty

A `kustomization.yaml` with an empty resources list:

```yaml
resources: []
```

## Why it exists

A Flux `Kustomization` needs a `spec.path` that renders, even when everything
it creates comes from `spec.components`. The CNPG database Kustomizations that
have no manifests of their own point here and layer
`components/cnpg-cluster/{base,initdb|recovery}` on top.

Current users (`grep -rl 'apps/base/empty' apps/production`):

| App | Flux Kustomization |
|---|---|
| authentik | `authentik-db-cnpg` |
| dawarich | `dawarich-db-cnpg` |
| joplin | `joplin-db` |
| opencut | `opencut-db-cnpg` |
| zilean | `zilean-db` |

Newer database apps use `apps/base/<app>/db-cnpg/` instead, which also holds
the Namespace so the database can be created before the app.

**Do not delete this directory** while any Kustomization still points at it:
the Kustomization would fail to build.
