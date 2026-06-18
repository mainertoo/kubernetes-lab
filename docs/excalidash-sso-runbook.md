# ExcaliDash — Authentik SSO runbook

ExcaliDash ([ZimengXiong/ExcaliDash](https://github.com/ZimengXiong/ExcaliDash)) is a
self-hosted Excalidraw dashboard with **native OIDC**. It authenticates directly
against Authentik — there is **no Traefik forward-auth middleware** on its
IngressRoute, and no Dex broker. `AUTH_MODE=hybrid` keeps a local email/password
login alongside SSO so you can't be locked out while tuning the OIDC config.

- Internal URL: `https://excalidash.lab.mainertoo.com`
- Manifests: `apps/base/excalidash/`
- Architecture: one pod, two containers — `app` (nginx SPA, :80, proxies `/api/`
  + `/socket.io/` to the backend over `127.0.0.1:8000`) and `backend`
  (Node/Prisma API, :8000, owns the SQLite DB on the `excalidash-config` PVC at
  `/app/prisma`, backed up daily via Kopia).

## 1. Create the Authentik OAuth2/OpenID provider

In Authentik admin → **Applications → Providers → Create → OAuth2/OpenID Provider**:

| Field | Value |
|---|---|
| Name | `excalidash` |
| Authorization flow | `default-provider-authorization-explicit-consent` (or implicit) |
| Client type | **Confidential** |
| Client ID | *(auto — copy to the secret)* |
| Client Secret | *(auto — copy to the secret)* |
| Redirect URIs | `https://excalidash.lab.mainertoo.com/api/auth/oidc/callback` (Strict) |
| Signing Key | authentik default (RS256) |
| Scopes | `openid`, `email`, `profile` |

This yields the issuer `https://authentik.lab.mainertoo.com/application/o/excalidash/`
— matches `OIDC_ISSUER_URL` in `excalidash-release.yaml`.

Then **Applications → Create**: Name `ExcaliDash`, slug `excalidash`, Provider =
the provider above, Launch URL `https://excalidash.lab.mainertoo.com`. Bind the
groups/users you want to allow.

## 2. Fill and encrypt the secret

Edit `apps/base/excalidash/excalidash-secret.sops.yaml`:

```bash
openssl rand -base64 48   # -> JWT_SECRET
openssl rand -base64 48   # -> CSRF_SECRET
```

Set `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` from the Authentik provider, then:

```bash
sops --encrypt --in-place apps/base/excalidash/excalidash-secret.sops.yaml
grep -q '^\s*OIDC_CLIENT_SECRET: ENC\[' apps/base/excalidash/excalidash-secret.sops.yaml \
  && echo "ENCRYPTED OK" || echo "NOT ENCRYPTED — do not commit"
```

(VS Code auto-encrypts `*.sops.yaml` on save — but verify `ENC[` regardless; the
suffix alone does not guarantee encryption.)

## 3. Deploy + first login

Commit on a branch from `master`, open a PR (CI renders + diffs), merge. Flux
reconciles. First person to log in **via Authentik** becomes admin
(`OIDC_FIRST_USER_ADMIN=true`).

## Gotchas

- **In-cluster reachability of `authentik.lab`** — the backend fetches the OIDC
  discovery doc from `OIDC_ISSUER_URL` at startup. If the pod can't hairpin to
  `authentik.lab.mainertoo.com`, set `OIDC_DISCOVERY_URL` to the in-cluster
  Authentik service URL (keep `OIDC_ISSUER_URL` as the browser-facing host so the
  token `iss` still matches).
- **Redirect URI mismatch** is the usual first-login failure — it must be exactly
  `…/api/auth/oidc/callback` and registered Strict in Authentik.
- **TRUST_PROXY=true** is required so the backend builds `https` redirect/cookie
  URLs from Traefik's `X-Forwarded-*` headers instead of the internal `http` host.
- **Tighten to SSO-only** later by setting `AUTH_MODE=oidc_enforced` once login
  works and an admin exists.
