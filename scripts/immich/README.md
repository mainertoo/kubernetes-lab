# scripts/immich

Local tooling for Immich's Authentik OIDC/SSO integration. Config-as-code, same
shape as `scripts/dispatcharr` / `scripts/wiki`: credentials live in
`credentials.sops.yaml` (decrypted on the fly with `sops -d`), nothing here is
reconciled by Flux.

Full background + the Authentik-side setup is in
[`docs/immich-sso-runbook.md`](../../docs/immich-sso-runbook.md).

## Files

| File | Purpose |
| --- | --- |
| `configure-oauth.py` | Merge the `oauth` block into Immich's live system config via `PUT /api/system-config`. Dry-run by default. |
| `audit-users.py` | Read-only: report which `Immich Users` group members will auto-link (email match) vs auto-register on first SSO login. |
| `credentials.sops.yaml` | SOPS-encrypted: `IMMICH_URL`, `IMMICH_API_KEY`, `OAUTH_ISSUER_URL`, `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`. |

## Prerequisites

1. `sops` + the repo age key (already used for all `*.sops.yaml`).
2. An **admin** Immich API key (Account Settings → API Keys). Put it in
   `credentials.sops.yaml` replacing the placeholder:
   ```bash
   sops scripts/immich/credentials.sops.yaml   # edit IMMICH_API_KEY, save
   ```
3. Network path to `IMMICH_URL` (default `https://immich.lab.mainertoo.com`,
   reachable on LAN / Tailscale).

## Usage

```bash
# Preview the oauth changes (changes nothing)
./scripts/immich/configure-oauth.py

# Apply them
./scripts/immich/configure-oauth.py --commit

# Inspect the live oauth block (clientSecret redacted)
./scripts/immich/configure-oauth.py --show

# Who merges vs who gets a new account
./scripts/immich/audit-users.py
```

Env vars (`IMMICH_URL`, `IMMICH_API_KEY`, `OAUTH_ISSUER_URL`, `OAUTH_CLIENT_ID`,
`OAUTH_CLIENT_SECRET`) override the creds file if set.
