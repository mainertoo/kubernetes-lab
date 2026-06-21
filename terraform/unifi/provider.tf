# Credentials are injected as UNIFI_* environment variables by ./tf.sh, which
# decrypts scripts/unifi/credentials.sops.yaml at runtime. The provider reads:
#   UNIFI_API       controller URL (no /api path)
#   UNIFI_USERNAME  local admin user
#   UNIFI_PASSWORD  local admin password
#   UNIFI_INSECURE  skip TLS verify (UDM SE self-signed cert)
#   UNIFI_SITE      site name (default)
# Intentionally empty — never hardcode secrets here.
provider "unifi" {}
