# Wallos subscription populator

`populate.py` seeds Wallos subscriptions from a curated CSV. Wallos has **no
write API** — its only create path is the session-authenticated
`endpoints/subscription/add.php` (which also requires a CSRF token), so the
script logs in, scrapes the CSRF token, resolves currency/category/payer IDs
from the rendered form, and POSTs each row. It is **idempotent** (skips
existing names when an API key is supplied) and **dry-run by default**.

## Input CSV

The output of the Rocket Money / Amex audit pipeline
(`wallos_detect.py` → `wallos_finalize.py`), columns:

```text
name, price, currency, billing_cycle, next_payment, category, payment_source, status, notes
```

Only rows with `status` of `confirmed` or `detected` are loaded.
`billing_cycle` must be one of: Daily, Weekly, Every 2 weeks, Monthly,
Every 2 months, Quarterly, Every 6 months, Yearly.

> **Do NOT commit the CSV.** It is derived from personal financial exports and
> this repo is public. Keep it under `~/Downloads` (or anywhere outside the
> repo) and pass its path as the argument.

## Auth (env only — no secrets in the repo)

| var | purpose |
| --- | --- |
| `WALLOS_URL` | default `https://wallos.lab.mainertoo.com` |
| `WALLOS_COOKIE` | `PHPSESSID=…` from a logged-in browser. **Use this for OIDC-only or TOTP-enabled logins** (skips the password flow). |
| `WALLOS_USERNAME` / `WALLOS_PASSWORD` | local login (used only if `WALLOS_COOKIE` is unset). |
| `WALLOS_API_KEY` | optional; Profile → API key. Enables dedup against existing subscriptions. |
| `WALLOS_CURRENCY_ID` / `WALLOS_PAYER_USER_ID` | optional overrides if auto-detection picks wrong. |

This instance uses Authentik OIDC. If local password login is disabled,
grab `PHPSESSID` from your browser dev tools after logging in and export it as
`WALLOS_COOKIE`.

## Run

```bash
export WALLOS_COOKIE='PHPSESSID=xxxxxxxx'        # or WALLOS_USERNAME/PASSWORD
export WALLOS_API_KEY='yyyy'                      # optional, for dedup

# dry-run: prints exactly what it would create, writes nothing
python3 scripts/wallos/populate.py ~/Downloads/wallos-final-subscriptions.csv

# apply for real
python3 scripts/wallos/populate.py ~/Downloads/wallos-final-subscriptions.csv --apply
```

Subscriptions are created with `auto_renew=on` and notifications off. Category
and payment-method default to real seeded IDs ("No category" / "Credit Card")
rather than `0` — a `0` id makes Wallos's dashboard throw PHP "undefined array
key" warnings (`stats_calculations.php` only pre-initializes real IDs). Re-map
to meaningful categories in the UI afterward (the CSV categories are raw
exporter strings).

Stdlib only; no `pip install` required.
