# UniFi network tooling

Pull the live network from the **UDM SE UniFi Network Integration API** and feed it
into Homebox inventory + the Wiki.js network page.

## Auth — no new secret

Reuses the existing in-cluster secret `apps/base/ui-toolkit/ui-toolkit-secret.sops.yaml`
(keys `UNIFI_API_KEY` + `UNIFI_CONTROLLER_URL`) via `sops -d`, or env `UNIFI_API_KEY` /
`UNIFI_CONTROLLER_URL`. The key is a UniFi Network API key (read access is enough);
mint/revoke under UniFi → Settings → Control Plane → Integrations.

## Usage

```bash
./netinfo.py devices                 # adopted UniFi gear (gateway/switches/APs)
./netinfo.py clients [--wired]       # connected clients
./netinfo.py map -o network-map.yaml # full structured map (devices + clients)

# Homebox inventory spec (consumed by ../homebox/inventory.py):
./netinfo.py homebox -o network-inventory.yaml                # gear + QNAP only
./netinfo.py homebox --all-clients -o network-inventory.yaml  # gear + every client
../homebox/inventory.py apply network-inventory.yaml          # dry-run
../homebox/inventory.py apply network-inventory.yaml --commit # write

# VLAN-migration verification (read-only; Integration API is read-mostly):
./netinfo.py networks                 # list VLANs/subnets
./netinfo.py wlans                    # list SSIDs and their networks
./netinfo.py firewall                 # list firewall policies
./netinfo.py verify                   # diff live VLANs against the target scheme
                                      #   (TARGET_VLANS in netinfo.py / docs/network-vlan-design.md)

# Wiki page (topology tree + device table + client/IP map):
./netinfo.py wiki -o /tmp/network-wiki.md
python3 ~/.claude/skills/wikijs-update/wiki.py upsert \
  --path infrastructure/networking/topology --title "Network — UniFi Topology & Inventory" \
  --file /tmp/network-wiki.md --tags network,unifi,topology,inventory
```

## Notes & conventions

- **Locations** for gear are derived from device names (e.g. `..._GARAGE`, `..._Loft`).
  Unmatched gear and the QNAP default to **Mason Closet**; review and re-home in Homebox.
- **`--all-clients`** drops every client into a **`Unsorted (auto-import)`** location with
  its MAC in the item serial-number field — sort rooms/details in the Homebox UI afterward.
- Item names are made unique (MAC appended on collisions) so re-apply stays idempotent.
  ⚠️ This is a **one-shot bulk seed**: after you rename/move items in the Homebox UI, the
  generated spec no longer matches by name — don't blindly re-apply it.
- `network-inventory.yaml` / `network-map.yaml` are **generated** (gitignored) — the tool
  is the source of truth, regenerate as the network changes.
- Wiki page: `infrastructure/networking/topology` (id 173), linked from the Networking index.
- `networks`/`wlans`/`firewall`/`verify` return None gracefully if this controller's
  Integration API doesn't expose config reads (it's read-mostly; write/config scope is
  rolling out through 2026) — verify in the UI in that case. These read commands need
  **LAN access** to the UDM (run on-site or over Tailscale; they fail from off-network).
