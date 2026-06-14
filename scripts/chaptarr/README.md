# scripts/chaptarr — Chaptarr (staging alpha) SSO config-as-code

Chaptarr is a Readarr fork (audiobooks + ebooks in one instance), deployed to the
**staging** cluster for evaluation (`apps/base/chaptarr/`, image
`robertlordhood/chaptarr:latest`). It has **native OIDC**, used here to replace
the default Plex login with **Authentik**.

## Why a script (not env / not a Flux Secret)

The OIDC settings are NOT reliably settable via env on this alpha — the Servarr
env binding only documents `<App>__Auth__Method`; the `Oidc*` keys are unverified.
They live in the host config (`config.xml` on the `/config` PVC) and are set via
the REST API. So, exactly like `scripts/immich/`, we configure the app side with a
small idempotent script and keep the secret SOPS-encrypted.

## Authentik side (created out-of-band via `ak shell`)

On the **production** Authentik (`kubectl -n authentik exec deploy/authentik-server -- ak shell`):

- **OAuth2/OIDC provider** `Provider for chaptarr` — **pk 160**, confidential,
  authz flow `default-provider-authorization-implicit-consent`, invalidation
  `default-provider-invalidation-flow`, signing key `authentik Self-signed
  Certificate`, scopes `openid email profile`, redirect URI (STRICT)
  `https://chaptarr.staging.mainertoo.com/signin-oidc`.
- **Application** slug `chaptarr` → issuer
  `https://authentik.mainertoo.com/application/o/chaptarr/` (public split-horizon
  issuer, like immich). No restrictive policy binding (any Authentik user).

⚠️ **Teardown**: if Chaptarr is removed, delete the orphaned Authentik provider
(pk 160) + application `chaptarr` via `ak shell`, or they linger.

## Chaptarr side

```bash
# dry-run (shows the OIDC diff, changes nothing)
python3 scripts/chaptarr/configure-oidc.py

# apply
python3 scripts/chaptarr/configure-oidc.py --commit
# then: kubectl --context staging -n chaptarr rollout restart deploy/chaptarr

# show current live OIDC fields
python3 scripts/chaptarr/configure-oidc.py --show
```

Credentials (URL, API key, client id/secret) live in `credentials.sops.yaml`
(SOPS-encrypted). The script reads them via `sops -d`, or you can override any of
`CHAPTARR_URL / CHAPTARR_API_KEY / OIDC_AUTHORITY / OIDC_CLIENT_ID /
OIDC_CLIENT_SECRET` via env.

`oidcAllowAnyVerifiedUser` is `true` (any Authentik-verified user). To restrict to
specific accounts, set `oidcAllowedEmailDomains` / `oidcAllowedEmails` in the
Chaptarr UI or extend `OIDC_STATIC`.
