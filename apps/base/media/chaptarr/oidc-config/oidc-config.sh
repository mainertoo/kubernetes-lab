#!/bin/sh
# Seed/refresh Chaptarr's native-OIDC settings in /config/config.xml from env.
# Client id/secret/allowed emails come from the SOPS secret `chaptarr-oidc`;
# authority/scopes are plain env on the initContainer. Chaptarr reads these
# keys only from config.xml (no env overrides), so this runs as an
# initContainer before the app starts. Idempotent.
#
# Lives in a ConfigMap with Flux postBuild substitution DISABLED, so the shell
# ${...} below survive. While the secret values are empty it changes nothing,
# so merging before the Authentik provider exists is safe.
set -eu
CFG="${CFG:-/config/config.xml}"

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
sed_escape() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

# upsert KEY VALUE: replace <KEY>...</KEY> (or <KEY />), else add before </Config>
upsert() {
  v=$(sed_escape "$(xml_escape "$2")")
  if grep -q "<$1>.*</$1>\|<$1 */>" "$CFG"; then
    sed -i -e "s|<$1>.*</$1>|<$1>$v</$1>|" -e "s|<$1 */>|<$1>$v</$1>|" "$CFG"
  else
    sed -i -e "s|</Config>|  <$1>$v</$1>\n</Config>|" "$CFG"
  fi
}

# set_if_unset KEY VALUE: only when absent/empty/None (first boot), so a later
# change in the UI (e.g. falling back to Forms) sticks across restarts.
set_if_unset() {
  cur=$(sed -n "s|.*<$1>\(.*\)</$1>.*|\1|p" "$CFG" | head -n1)
  case "$cur" in ""|None) upsert "$1" "$2" ;; esac
}

for k in OIDC_AUTHORITY OIDC_CLIENT_ID OIDC_CLIENT_SECRET OIDC_ALLOWED_EMAILS; do
  eval "val=\${$k:-}"
  [ -n "$val" ] || { echo "oidc-config: $k is empty, leaving config.xml untouched"; exit 0; }
done

# First boot: Chaptarr fills every other key (ApiKey, Port, ...) with its
# defaults when it loads a config.xml that lacks them.
[ -f "$CFG" ] || printf '<Config>\n</Config>\n' > "$CFG"

upsert OidcAuthority     "$OIDC_AUTHORITY"
upsert OidcClientId      "$OIDC_CLIENT_ID"
upsert OidcClientSecret  "$OIDC_CLIENT_SECRET"
upsert OidcScopes        "${OIDC_SCOPES:-openid profile email}"
upsert OidcAllowedEmails "$OIDC_ALLOWED_EMAILS"
set_if_unset AuthenticationMethod   Oidc
set_if_unset AuthenticationRequired Enabled

chown 1000:1000 "$CFG" 2>/dev/null || true
echo "oidc-config: OIDC keys written to $CFG (secret values not printed)"
grep -o '<\(AuthenticationMethod\|AuthenticationRequired\|OidcAuthority\|OidcScopes\)>[^<]*' "$CFG" || true
