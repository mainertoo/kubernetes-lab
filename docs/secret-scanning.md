# Secret scanning (gitleaks)

This **public** repo scans for plaintext secrets with
[gitleaks](https://github.com/gitleaks/gitleaks) at two layers:

| Layer | What | Enforced |
|---|---|---|
| **CI** (`.github/workflows/gitleaks.yml`) | Scans the commits a PR **adds** | Always — hard gate on every PR to `master` |
| **Local pre-commit** (`.githooks/pre-commit`) | Scans **staged** changes before commit | Opt-in per clone (see below) |

Config: [`.gitleaks.toml`](../.gitleaks.toml). It extends the gitleaks default
ruleset and allowlists **SOPS ciphertext** (`ENC[AES256_GCM,…]`, matched at line
scope) so the 79 encrypted `*.sops.yaml` files don't create noise — while still
catching a `.sops.yaml` that silently **failed** to encrypt (the
"`.sops.yaml` suffix ≠ encrypted" footgun).

## Enable the local hook (recommended, one-time per clone)

```bash
git config core.hooksPath .githooks
brew install gitleaks            # or https://github.com/gitleaks/gitleaks
```

- No-ops with a warning if gitleaks isn't installed (CI is still the hard gate).
- Bypass a confirmed false positive with `git commit --no-verify`.

## Why CI scans the PR diff, not full history

The gate's job is to stop **new** secrets. Scanning only the PR's commit range
means it doesn't block PRs on pre-existing historical findings — those are a
separate remediation decision (see below).

## Handling a finding

- **Real secret in your change** → remove it. Secrets go **only** in
  `*.sops.yaml`, encrypted; verify the value shows `ENC[` before committing.
- **False positive** → bypass locally with `git commit --no-verify`; for a
  durable allowlist add a narrow rule/path to `.gitleaks.toml` (prefer matching
  the specific line/path over disabling a whole rule).

## Known historical findings (git history, pre-dating this scanner)

A full-history scan surfaced secret-typed material in **old, already-deleted**
commits (none in the current tree). Because the repo is public, anything ever
committed must be treated as exposed. Tracked separately from this tooling — see
the maintainer's notes; remediation = rotate the affected credentials, and
optionally rewrite history. The recurrence patterns (`*.asc`, `kubeconfig*`,
`.decrypted~*`, `*.tfvars*`) are now gitignored so they can't come back.
