# Immich ↔ Authentik SSO (native OIDC) runbook

Immich authenticates against Authentik via its **native OAuth/OIDC** (not
forward-auth — the Immich mobile app can't do an interactive proxy flow).
Password login stays enabled on every surface; SSO is an opt-in button.

## Why the public issuer (`authentik.mainertoo.com`)

The OAuth issuer is a single value baked into Immich, and the browser must reach
it during the redirect. Immich is outward-facing
(`immich.mainertoo.com` via Pangolin, `immich.tuxedo-halosaur.ts.net` via Funnel),
so it uses the **public** Authentik hostname
`https://authentik.mainertoo.com/application/o/immich/` — the same one the other
outward-facing apps (audiobookshelf, readmeabook) use. That host resolves in
**both** DNS views (split-horizon: internal → Traefik, external → Pangolin), so
SSO works from LAN, Tailscale, and the open internet, and the in-cluster Immich
server can reach the token/userinfo endpoints for the server-side code exchange.
Internal-only apps (mealie, grafana, …) use the `.lab` issuer precisely because
they are never reached from outside — do **not** copy that for Immich.

## Authentik objects (created via `ak shell`, no API token stored)

Driven through the worker pod, which runs fully authorized:

```bash
POD=$(kubectl get pods -n authentik -l app.kubernetes.io/component=worker \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n authentik "$POD" -- ak shell -c '<python>'
```

Created:

- **OAuth2/OpenID Provider** `Provider for immich` (pk **159**): confidential
  client, `sub_mode=hashed_user_id`, `include_claims_in_id_token=True`,
  signing key = `authentik Self-signed Certificate`, authorization flow
  `default-provider-authorization-implicit-consent`, invalidation flow
  `default-provider-invalidation-flow`, scopes `openid email profile`.
  - **`grant_types = ["authorization_code","refresh_token"]`** — must be set
    explicitly; an API/ORM-created provider defaults to `[]` and returns
    `invalid_request` on login (the ceph-dashboard gotcha).
  - **Redirect URIs** (STRICT):
    - `https://immich.mainertoo.com/auth/login` + `/user-settings`
    - `https://immich.tuxedo-halosaur.ts.net/auth/login` + `/user-settings`
    - `https://immich.lab.mainertoo.com/auth/login` + `/user-settings`
    - `app.immich:///oauth-callback`
- **Application** `immich` (slug `immich`, launch `https://immich.mainertoo.com`),
  bound to the provider.
- **Group** `Immich Users` + a **PolicyBinding** on the application → only group
  members can access. Add/remove users by editing this group.

Verify the public discovery doc:

```bash
curl -s https://authentik.mainertoo.com/application/o/immich/.well-known/openid-configuration | jq .issuer
# -> "https://authentik.mainertoo.com/application/o/immich/"
```

## Immich side (config-as-code)

Immich does NOT read OAuth from env vars — settings live in the DB. Configure
via `scripts/immich/configure-oauth.py`, which merges only the `oauth` block into
`PUT /api/system-config` (admin UI stays editable). Credentials in
`scripts/immich/credentials.sops.yaml`.

```bash
sops scripts/immich/credentials.sops.yaml      # set IMMICH_API_KEY (admin key)
./scripts/immich/configure-oauth.py            # dry-run
./scripts/immich/configure-oauth.py --commit   # apply
```

oauth block applied: `enabled`, `issuerUrl`, `clientId`, `clientSecret`,
`scope=openid email profile`, `buttonText=Login with Authentik`,
`autoRegister=true`, `autoLaunch=false` (password form stays default),
`storageLabelClaim=preferred_username`, `roleClaim=immich_role`.

## User merge (by email)

Immich links an OAuth login to an existing user when the IdP `email` matches; with
`autoRegister=true` a non-matching email creates a new user. So align emails
**before** rollout:

```bash
./scripts/immich/audit-users.py     # MATCH = auto-link, NEW = will be created
```

Reconcile any mismatched emails (change the Authentik user's email to match the
existing Immich account, or vice-versa) so people land on their existing library.

## Mobile fallback

Mobile uses `app.immich:///oauth-callback`. If the deep link fails on a device,
set in the oauth block `mobileOverrideEnabled=true` +
`mobileRedirectUri=https://immich.mainertoo.com/api/oauth/mobile-redirect` and
register that redirect URI on the provider.

## Teardown

```bash
# Immich: set oauth.enabled=false via the admin UI or configure-oauth.py.
# Authentik (ak shell): delete the application 'immich', provider pk 159,
#   group 'Immich Users' (delete the PolicyBinding with the app, or the group).
```
