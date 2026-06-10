#!/usr/bin/env python3
"""Populate Wallos subscriptions from a curated CSV.

Wallos has no write API — its only create path is the session-authenticated
endpoint `endpoints/subscription/add.php`, which also requires a CSRF token.
This script logs in, scrapes the CSRF token + resolves currency/category/
payment-method/payer IDs from the rendered form, then POSTs each subscription.

Idempotent (skips names that already exist, when an API key is available) and
DRY-RUN BY DEFAULT — pass --apply to actually write.

CSV columns (the wallos_finalize.py output):
    name, price, currency, billing_cycle, next_payment, category, payment_source, status, notes
Only rows with status in {confirmed, detected} are loaded.

Auth (env, no plaintext in repo):
    WALLOS_URL          default https://wallos.lab.mainertoo.com
    WALLOS_COOKIE       e.g. "PHPSESSID=abc..." — paste from a logged-in browser.
                        Use this for OIDC-only or TOTP-enabled instances (skips login).
    WALLOS_USERNAME / WALLOS_PASSWORD   local login (used if WALLOS_COOKIE unset).
    WALLOS_API_KEY      optional; enables dedup against existing subs (Profile -> API key).
    WALLOS_CURRENCY_ID / WALLOS_PAYER_USER_ID   optional overrides if auto-detect fails.

Usage:
    python3 populate.py ~/Downloads/wallos-final-subscriptions.csv          # dry-run
    python3 populate.py ~/Downloads/wallos-final-subscriptions.csv --apply  # create

Stdlib only (urllib) — no pip install needed.
"""
import csv, os, re, sys, json, html
import urllib.request, urllib.parse, http.cookiejar

URL = os.environ.get("WALLOS_URL", "https://wallos.lab.mainertoo.com").rstrip("/")
COOKIE = os.environ.get("WALLOS_COOKIE", "")
USER = os.environ.get("WALLOS_USERNAME", "")
PW = os.environ.get("WALLOS_PASSWORD", "")
API_KEY = os.environ.get("WALLOS_API_KEY", "")
APPLY = "--apply" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("-")]
CSV_PATH = args[0] if args else os.path.expanduser("~/Downloads/wallos-final-subscriptions.csv")

# billing_cycle -> (cycle, frequency). cycle: 1=Daily 2=Weekly 3=Monthly 4=Yearly.
CYCLE = {
    "Daily": (1, 1), "Weekly": (2, 1), "Every 2 weeks": (2, 2),
    "Monthly": (3, 1), "Every 2 months": (3, 2), "Quarterly": (3, 3),
    "Every 6 months": (3, 6), "Yearly": (4, 1),
}

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def req(path, data=None):
    url = path if path.startswith("http") else URL + path
    headers = {"User-Agent": "wallos-populate/1.0"}
    if COOKIE:
        headers["Cookie"] = COOKIE
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, headers=headers)
    with opener.open(r, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")

def scrape_csrf(page):
    m = re.search(r'csrfToken\s*=\s*"([0-9a-fA-F]+)"', page) or \
        re.search(r'name="csrf_token"[^>]*value="([0-9a-fA-F]+)"', page)
    return m.group(1) if m else ""

def parse_select(page, field):
    block = re.search(r'<select[^>]*name="%s".*?</select>' % re.escape(field), page, re.S | re.I)
    out = []
    if block:
        for vid, txt in re.findall(r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', block.group(0), re.S):
            out.append((int(vid), html.unescape(re.sub(r"<[^>]+>", "", txt)).strip()))
    return out

def real(options):
    # Drop placeholder <option value="0"> ("— select —") so we never assign id 0,
    # which makes Wallos's dashboard throw "undefined array key" warnings.
    return [(v, t) for v, t in options if v != 0]

def resolve(options, label, env, *needles):
    """Return an id: env override > first option whose text matches a needle >
    first real option. Exits clearly if nothing parsed (e.g. expired session)."""
    if os.environ.get(env):
        return int(os.environ[env])
    for vid, txt in options:
        if any(n and n.lower() in txt.lower() for n in needles):
            return vid
    if not options:
        sys.exit(f"could not resolve {label}: no <select> options parsed from the form "
                 f"(WALLOS_COOKIE may have expired — re-export it). Or set {env} to override.")
    return options[0][0]

def login():
    if COOKIE:
        print("auth: using WALLOS_COOKIE (no login)"); return
    if not (USER and PW):
        sys.exit("auth: set WALLOS_COOKIE, or WALLOS_USERNAME + WALLOS_PASSWORD")
    page = req("/login.php")
    req("/login.php", {"username": USER, "password": PW, "remember": "on",
                       "csrf_token": scrape_csrf(page)})
    home = req("/")
    if "csrfToken" not in home:
        sys.exit("auth: login failed (bad creds, or TOTP/OIDC-only — use WALLOS_COOKIE instead)")
    print(f"auth: logged in as {USER}")

def existing_names():
    if not API_KEY:
        print("dedup: WALLOS_API_KEY unset — not checking for duplicates"); return set()
    try:
        data = json.loads(req(f"/api/subscriptions/get_subscriptions.php?api_key={API_KEY}"))
        return {s.get("name", "").strip().lower() for s in data.get("subscriptions", [])}
    except Exception as e:
        print(f"dedup: read API failed ({e}) — not checking"); return set()

def main():
    if not os.path.exists(CSV_PATH):
        sys.exit(f"csv not found: {CSV_PATH}")
    # add.php has no built-in dedup — without an API key to list existing subs,
    # a second --apply silently DUPLICATES everything. Refuse unless dedup is
    # possible (WALLOS_API_KEY) or the user explicitly opts out with --force.
    if APPLY and not API_KEY and "--force" not in sys.argv:
        sys.exit("Refusing to --apply without WALLOS_API_KEY (no duplicate protection).\n"
                 "  Re-running this would create DUPLICATE subscriptions.\n"
                 "  Fix: export WALLOS_API_KEY=... (Wallos -> Profile -> API key) so existing\n"
                 "  names are skipped, or pass --force to insert unconditionally.")
    login()
    # The add-subscription form (with the currency/category/payment/payer
    # <select>s + CSRF token) lives on subscriptions.php, NOT the / dashboard.
    page = req("/subscriptions.php")
    csrf = scrape_csrf(page)
    if not csrf:
        sys.exit("could not obtain CSRF token from subscriptions.php (session may have expired)")
    currencies = real(parse_select(page, "currency_id"))
    payers = real(parse_select(page, "payer_user_id"))
    categories = real(parse_select(page, "category_id"))
    methods = real(parse_select(page, "payment_method_id"))

    currency_id = resolve(currencies, "currency", "WALLOS_CURRENCY_ID", "USD", "US Dollar", "$")
    payer_id = resolve(payers, "payer", "WALLOS_PAYER_USER_ID", USER) if (payers or os.environ.get("WALLOS_PAYER_USER_ID")) else 1
    print(f"resolved: currency_id={currency_id}  payer_user_id={payer_id}  "
          f"({len(categories)} categories, {len(methods)} payment methods on instance)")

    have = existing_names()
    rows = [r for r in csv.DictReader(open(CSV_PATH, newline=""))
            if r.get("status", "") in ("confirmed", "detected")]
    print(f"\n{'PLAN (dry-run)' if not APPLY else 'APPLYING'} — {len(rows)} candidate rows\n")

    created = skipped = failed = 0
    for r in rows:
        name = r["name"].strip()
        if name.lower() in have:
            print(f"  skip (exists): {name}"); skipped += 1; continue
        bc = r["billing_cycle"]
        if bc not in CYCLE:
            print(f"  !! unknown cycle '{bc}' for {name} — skipped"); failed += 1; continue
        cycle, freq = CYCLE[bc]
        # real()-filtered above, so these resolve to a valid id (≥1), never 0 —
        # a 0 id makes Wallos's dashboard throw "undefined array key" warnings.
        cat_id = resolve(categories, "category", "WALLOS_CATEGORY_ID",
                         r.get("category", ""), "No category") if categories else 0
        pm_id = resolve(methods, "payment", "WALLOS_PAYMENT_METHOD_ID",
                        r.get("payment_source", ""), "Credit Card", "Card") if methods else 0
        fields = {
            "name": name, "price": r["price"], "currency_id": currency_id,
            "cycle": cycle, "frequency": freq,
            "next_payment": r["next_payment"], "start_date": r["next_payment"],
            "payment_method_id": pm_id, "payer_user_id": payer_id, "category_id": cat_id,
            "notes": r.get("notes", ""), "url": "", "logo-url": "",
            "notify_days_before": "0", "auto_renew": "1",
            "replacement_subscription_id": "0", "csrf_token": csrf,
        }
        print(f"  {'create' if APPLY else 'would create'}: {name:28} ${r['price']:>8} "
              f"/{bc:14} cur={currency_id} cat={cat_id} pm={pm_id}")
        if APPLY:
            resp = req("/endpoints/subscription/add.php", fields)
            low = resp.lower()
            if any(k in low for k in ("invalid csrf", "session_expired", "fill_all_fields", '"success":false', "expired")):
                print(f"      FAILED: {resp.strip()[:120]}"); failed += 1
            else:
                created += 1

    print(f"\n{'created' if APPLY else 'would create'}: {created if APPLY else len(rows)-skipped-failed}"
          f"  skipped(existing): {skipped}  failed: {failed}")
    if not APPLY:
        print("\nDry-run only. Re-run with --apply to write to Wallos.")

if __name__ == "__main__":
    main()
