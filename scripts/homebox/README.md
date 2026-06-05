# Homebox inventory-as-code

Populate [Homebox](https://homebox.lab.mainertoo.com) from a declarative YAML spec
instead of clicking through the UI. Mirrors the `scripts/dispatcharr/` pattern:
a hand-edited source of truth + an idempotent `apply` (dry-run by default).

## The model

```
locations (nested tree)  ─┐
tags / labels (flat)      ─┼─▶  items  (name + location path + tags)
```

Idempotency comes from **name-matching against the live server** — there's no state
file. Re-applying only creates/updates what's missing or changed:

| Object    | Matched by                          |
|-----------|-------------------------------------|
| location  | full slash-path (`Garage/Bin 1`)    |
| tag       | name (case-insensitive)             |
| item      | name + location (case-insensitive)  |

Renames aren't tracked (a renamed node = a new object). `pull` first to see current names.

## Auth

Homebox has **no API keys** — its OpenAPI spec only defines `Bearer` auth, obtainable
solely via `POST /v1/users/login`. So the tool logs in with a **local** account
(`HBOX_OPTIONS_ALLOW_LOCAL_LOGIN=true`) and uses that run's JWT. The account must
belong to the **same Homebox group** as the inventory you're populating. OIDC/Authentik
logins can't be scripted.

Credentials come from `credentials.sops.yaml` (via `sops -d`) or env vars:

```bash
cp credentials.sops.yaml ...        # already created; edit the placeholders
sops --encrypt --in-place credentials.sops.yaml   # or save in VS Code to auto-encrypt
# verify it encrypted — the value lines must read ENC[...] before committing
```

`username`+`password` is the **durable** choice (re-logs in each run, never expires).
A pulled JWT works too but expires:

```bash
./inventory.py token        # prints `# expires:` + HOMEBOX_TOKEN=<jwt>
```

## Usage

```bash
./inventory.py whoami                       # verify auth, show your group + counts
./inventory.py pull -o current.yaml         # snapshot live state into a spec
./inventory.py barcode 0049000028911        # product lookup by EAN/UPC (auto-fill)

# edit inventory.yaml, then:
./inventory.py apply inventory.yaml          # PLAN — writes nothing
./inventory.py apply inventory.yaml --commit # apply the plan
./inventory.py apply inventory.yaml --commit --no-update   # create-only, never modify
```

`apply` always prints the plan first; `--commit` is required to write. Stdlib + PyYAML
only (`pip3 install pyyaml`).

## Spec format

See `inventory.yaml` for a worked example. Item fields beyond name/location/tags/
quantity/description (`manufacturer`, `modelNumber`, `serialNumber`, `notes`,
`insured`, `purchasePrice`, `purchaseFrom`, `purchaseTime`) are applied via a
follow-up update after create.
