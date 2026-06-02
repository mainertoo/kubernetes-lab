# Open Notebook tailnet ingress + Mac sleep hardening (planning doc)

> **Status:** v3 — **CONVERGED** (2026-06-02, owner sign-off). 2 Codex adversarial passes: pass 1 (3C/4H/6M/4L) → pass 2 (0C/2H/4M/3L) → both pass-2 Highs dispositioned (one rejected as verified-false with a dangerous fix, one folded as test remediation). 0 Critical, 0 open High. Remaining items are implementation-time verifications (netpol label, ACL posture, streaming/redirect/stdio tests), not design flaws. **Implementation-ready.**
>
> **v3 changes (Codex pass 2: 0 Critical, 2 High, 4 Medium, 3 Low):**
> - **P2-H-1 (High) — REJECTED (verified false + dangerous fix):** repo confirms `service.app:` key with `controller: open-notebook`, but per [[feedback_bjws_service_rename_on_controller_removal]] a **single**-service bjw-s release names the Service `<release>` = `open-notebook` deterministically (multi-service would be `<release>-<key>`). A fresh reconcile yields `open-notebook`, not `open-notebook-app`. Codex's proposed fix (rename the key) would risk RENAMING the live Service → silently breaking the existing IngressRoute, pocket-bridge (hardcoded `open-notebook…svc:5055`), Gatus, ServiceMonitors — the wiki-js outage mode. Plan keeps `service.name: open-notebook`. Folded only the genuine latent note (§5.1). (§5.1)
> - **P2-H-2 (High → Medium) — ACCEPTED as test remediation:** §10 step 4 already gates on `API_URL` leakage; added the remediation path for if matches appear. (§10)
> - **P2-M-1 (Med) — ACCEPTED:** the §5.3 NetPol must be **Egress** (proxy→backend:5055), not Ingress (mirroring the egress-proxy polarity was wrong). Also downgraded the NetPol to OPTIONAL — for an ingress proxy it's low-value defense-in-depth, and a wrong-direction policy risks silently blocking traffic. (§5.3)
> - **P2-M-2 (Med) — ACCEPTED:** ACL forward-maintenance note — future `tag:k8s` Ingresses inherit the rule. (§6, §8)
> - **P2-M-3 (Med) — ACCEPTED:** L4 fallback needs `tailscale.com/hostname` + a different URL/scheme. (§8a)
> - **P2-M-4 (Med) — ACCEPTED:** added `lsof` stdio-confirm to acceptance. (§10)
> - **P2-L-2 — ACCEPTED:** reworded §11 device-removal guarantee. **P2-L-3 — ACCEPTED:** added commented password reminder (§9). **P2-L-1 — no action** (T6 risk-accepted).
>
> **v2 changes (Codex pass 1: 3 Critical, 4 High, 6 Medium, 4 Low):**
>
> **v2 changes (Codex pass 1: 3 Critical, 4 High, 6 Medium, 4 Low):**
> - **C-1 (Crit) — DISSOLVED:** reuse the already-OAuth-authorized `tag:k8s` instead of a new `tag:on-api`. Removes the "new tag not in operator OAuth client → device silently unapproved" failure mode entirely. Trade-off: ACL scoping is coarser (shared tag). Accepted given owner's tailnet risk stance. (§4 T3, §6)
> - **C-2 (Crit) — REJECTED, verified false:** live `kubectl get svc -n open-notebook` shows the Service IS named `open-notebook` (ports 5055+8502). The bjw-s naming speculation was wrong. Plan's `service.name: open-notebook` is correct. (§5.1)
> - **C-3 (Crit) — RISK-ACCEPTED (downgraded):** owner decision 2026-06-02 — ship ACL-only, no `OPEN_NOTEBOOK_API_KEY`. T6 demoted from hard prerequisite to optional future hardening. Residual risk documented in §8. (§8 T6)
> - **H-1 (High):** `tailscale.com/tags` is apply-once; added verify-tag-after-recreate to §10/§11.
> - **H-2 (High → Medium):** reframed — Codex↔MCP-server is local **stdio**; only plain HTTPS REST crosses the tailnet, so the SSE-through-L7 risk is narrow. Added verify step + L4 fallback. (§8a, §12 R4)
> - **H-3 (High):** added concrete test that ON's API responses don't embed `*.lab.mainertoo.com` URLs the off-LAN MCP client can't resolve. (§10)
> - **H-4 (High):** documented the Authentik-bypass side-channel prominently. (§8)
> - **M-1:** added ingress-proxy NetworkPolicy mirroring `mac-ollama-egress`. (§5.3)
> - **M-2/L-1 — VERIFIED RESOLVED:** no `open-notebook` device on the tailnet; name is free, no suffix. Preflight kept. (§6)
> - **M-3:** clamshell-sleep honesty fix. (§7)
> - **M-4:** LaunchAgent plist now included verbatim. (§7)
> - **M-5:** API-only comment added to Ingress. (§5.1)
> - **M-6:** conditional note tied to deferred T6. (§8)
> - **L-3:** rollback now includes manual tailnet device cleanup. (§11)
> - **L-4 — VERIFIED:** `apps/production/kustomization.yaml` already includes `open-notebook`.

> **Goal:** Make Open Notebook's API reachable from the MacBook over Tailscale (not just LAN), so Codex's `open-notebook-mcp` server works while travelling/out of the house.

## 1. Goal and scope

Today Codex → Open Notebook only works on the home LAN via `kubectl port-forward` to `:5055`. This plan adds a **Tailscale ingress** so the API is reachable at a stable MagicDNS name from anywhere the laptop has Tailscale, plus **Mac sleep hardening** so the backend doesn't nod off mid-session.

**In scope:** Tailscale `Ingress` for Open Notebook `:5055` (tailnet-only), ingress-proxy NetworkPolicy, Tailnet ACL note, Codex MCP config update, Mac power config.
**Out of scope:** adding cloud models *inside* Open Notebook (there is no token-free MCP route — see the separate conclusion in the chat history); the Pocket pipeline (live); Hermes.

## 2. Current state (verified 2026-06-01/02)

- Open Notebook live: pod `open-notebook` (ns `open-notebook`) on worker-2. **Service is named `open-notebook`** (ClusterIP `10.43.83.164`, ports `5055/TCP` + `8502/TCP`, selector `app.kubernetes.io/controller=open-notebook`). UI ingress `open-notebook.lab.mainertoo.com` behind Authentik SSO. **`:5055` API auth DISABLED** (`open-notebook-secret.sops.yaml` has no `OPEN_NOTEBOOK_API_KEY` key at all).
- Existing Tailscale plumbing is one-directional cluster→Mac (`mac-ollama` egress, `tag:k8s-egress`). No Mac→cluster-service path.
- Proven **ingress** pattern: `apps/base/vaultwarden/vaultwarden-tailscale-ingress.yaml` — `ingressClassName: tailscale`, `tailscale.com/tags: "tag:k8s"`, `tls.hosts: [vaultwarden]`. (Uses `tailscale.com/funnel: "true"` for PUBLIC exposure — we will NOT.)
- Tailnet: `tuxedo-halosaur.ts.net`. Mac `darcys-macbookpro` (`100.105.173.8`), owner `mainertoo@`, tag `tag:mac-ollama`. **No existing `open-notebook` device — name is free (verified).**
- Tailscale HTTPS/MagicDNS certs already enabled (vaultwarden's `tls:` proves it).
- `apps/production/kustomization.yaml` already includes `open-notebook` (verified).

## 3. Full travel data path

```
Laptop (anywhere w/ internet)
 ├─ Codex  ⇄ (local stdio) ⇄  uvx open-notebook-mcp     (both LOCAL on laptop)
 │                                   │  https://open-notebook.tuxedo-halosaur.ts.net  [tailnet, HTTPS REST]  ← NEW
 │                                   ▼
 │                            Open Notebook API :5055    (home cluster)
 │                                   │  http://mac-ollama.ollama-egress.svc:11434     [tailnet]  ← existing egress
 │                                   ▼
 └─ Mac-Ollama :11434               (back on the SAME laptop)
```

Key correction from review (H-2): the **MCP protocol itself runs over local stdio** between Codex and the `open-notebook-mcp` process. Only plain HTTPS **REST** calls traverse the tailnet. Dependencies for travel — all required at once: (1) laptop awake + Tailscale up; (2) home internet + cluster up (etcd quorum); (3) existing egress healthy.

## 4. Design decisions

| # | Decision | Rationale |
|---|---|---|
| T1 | Tailscale `Ingress` (L7 HTTPS), not raw LoadBalancer | Mirrors vaultwarden; MagicDNS HTTPS cert + clean hostname; terminates TLS → proxies to HTTP `:5055`. Codex uses `https://…ts.net` (443). |
| T2 | Tailnet-only — NO `tailscale.com/funnel` | API must NOT be on the public internet. |
| T3 (v2) | **Reuse `tag:k8s`, NOT a new `tag:on-api`** | `tag:k8s` is already in the operator's OAuth client scope (vaultwarden uses it). A new tag risks silent device-unapproved provisioning (Codex C-1). Coarser ACL is acceptable per owner's tailnet stance. |
| T4 | ACL `src` = the Mac; `dst` = the ingress device on 443 (optional, see §6/§8) | Least privilege if a default-deny ACL is in force. |
| T5 | Separate Ingress object; do NOT mutate the main Service | Keep internal wiring (bridge→ON, UI) untouched. |
| T6 (v2) | **Open Notebook API-key auth DEFERRED** (owner decision 2026-06-02) | Ship ACL/tailnet-only. Documented residual risk in §8. Revisit if the tailnet ever holds untrusted devices. |

## 5. Manifests

### 5.1 New: `apps/base/open-notebook/open-notebook-tailscale-ingress.yaml`
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: open-notebook-tailscale
  namespace: open-notebook
  annotations:
    # tailnet-only — intentionally NO tailscale.com/funnel
    tailscale.com/tags: "tag:k8s"   # reuse already-authorized tag; apply-once (see H-1)
spec:
  ingressClassName: tailscale
  # API-ONLY: routes ALL paths to FastAPI :5055. The Open Notebook UI is NOT here —
  # it stays on the Traefik IngressRoute at open-notebook.lab.mainertoo.com (Authentik-gated).
  tls:
    - hosts:
        - open-notebook            # → https://open-notebook.tuxedo-halosaur.ts.net
  defaultBackend:
    service:
      name: open-notebook          # VERIFIED live name; deterministic for a SINGLE-service bjw-s release
      port:
        number: 5055
```
> **Latent coupling (P2-H-1):** the backend name `open-notebook` holds because `open-notebook-release.yaml` has exactly **one** service block (`service.app`). Per [[feedback_bjws_service_rename_on_controller_removal]], adding a **second** service block would rename this Service to `open-notebook-app` and silently break this Ingress (and pocket-bridge/Gatus). If a 2nd service is ever added, update this `name:` accordingly. Do NOT "pre-fix" by renaming the `app` key — that risks renaming the live Service now.

### 5.2 Edit: `apps/base/open-notebook/kustomization.yaml`
Add `- open-notebook-tailscale-ingress.yaml` (and the netpol from §5.3) to `resources:`.
> Flux `prune: true` — removing these files later cleanly deletes the Ingress/NetPol (operator tears down the tailnet device). No PVC/data risk.

### 5.3 OPTIONAL: `apps/base/open-notebook/open-notebook-tailscale-netpol.yaml` (M-1, corrected by P2-M-1)
> **Low value — consider skipping.** Unlike the egress proxy (where restricting who can *reach* it matters), an *ingress* proxy receives tailnet-originated traffic via the operator tunnel, not pod-to-pod. So an `Ingress`-from-namespaces policy (the polarity of `mac-ollama-egress/tailscale-proxy-networkpolicy.yaml`) does **nothing** here. If you include a NetPol at all, it must be **Egress** (proxy → backend), and a wrong/over-tight one risks silently blocking the API path.

If included, use Egress direction:
```yaml
spec:
  podSelector:
    matchLabels:
      tailscale.com/managed: "true"
      tailscale.com/parent-resource: open-notebook-tailscale
      tailscale.com/parent-resource-ns: open-notebook
      tailscale.com/parent-resource-type: ingress   # VERIFY live label before committing
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: open-notebook
      ports:
        - protocol: TCP
          port: 5055
```
> ⚠️ Verify the live label post-provision: `kubectl get pod -n tailscale -l tailscale.com/parent-resource=open-notebook-tailscale -o yaml`. Commit only after confirming, per the `mac-ollama-egress` Phase-0b1/0b2 gate.

## 6. Tailnet ACL (tailscale admin, NOT this repo)

Because we reuse `tag:k8s`, **no `tagOwners` or OAuth-client change is needed**.

**Preflight:** confirm the tailnet ACL posture.
- If your tailnet is **default allow-all** (the common starting grant), the new ingress is reachable on 443 by *every* device you own — acceptable per the owner's stance (only personal devices: this Mac, an AppleTV, a long-offline arch box). No ACL change required.
- If you run **default-deny** with explicit `acls`, add:
```jsonc
{ "action": "accept", "src": ["tag:mac-ollama"], "dst": ["tag:k8s:443"] }
```
(Note: with shared `tag:k8s`, this also permits the Mac→vaultwarden:443 — harmless, your own service.)

> **Forward-maintenance (P2-M-2):** any *future* `tag:k8s` Ingress automatically inherits this `dst: tag:k8s:443` rule. If you ever expose a sensitive internal API under `tag:k8s`, re-scope this ACL (or give that service its own tag). Review the rule whenever a new `tag:k8s` Ingress is added.

**Preflight 2:** `tailscale status | grep open-notebook` → confirm the device name is free (verified 2026-06-02; re-check before each provision).

## 7. Mac sleep hardening (honest scope)

**Realistic goal:** stop *idle* sleep so a session doesn't drop while the laptop is **open and plugged in**. This is NOT lid-closed operation.

**M-3 honesty note — what these settings do and don't do:**
- `lid OPEN + AC` → no sleep (covered by `pmset -c sleep 0` + `caffeinate -s`). ✅
- `lid CLOSED + AC + no external display` → **macOS sleeps anyway** (clamshell). `pmset -c sleep 0` and `caffeinate -s` do NOT override this. ❌
- The only override is `sudo pmset -c disablesleep 1`, which disables ALL sleep incl. thermal — **Apple discourages it; do not use for a bag-carried laptop.**

**Config:**
```bash
sudo pmset -c sleep 0          # no idle system sleep on AC
sudo pmset -c disksleep 0      # optional
```

**LaunchAgent (M-4, verbatim per [[feedback_document_host_scripts]]):**
Path: `~/Library/LaunchAgents/com.mainertoo.caffeinate.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.mainertoo.caffeinate</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```
Install/verify:
```bash
launchctl load -w ~/Library/LaunchAgents/com.mainertoo.caffeinate.plist
launchctl list | grep caffeinate
```
**Operating expectation:** keep the lid open during Codex sessions when away. Lid closed in a bag = asleep = pipeline down (acceptable; you're not using it then).

## 8. Security analysis (the crux)

- The `:5055` API is **unauthenticated**. This ingress is therefore a **permanent side-channel around Authentik SSO** (H-4) — the UI is SSO-gated, but the API via the tailnet is not. Anyone who can reach the ingress device on 443 has full RW to all notebooks/sources/notes.
- **Sole guard is the tailnet** (membership + optional ACL). Per owner decision (T6), no API key. Residual risk accepted: a compromised personal tailnet device = full Open Notebook RW.
- Mitigations in force: tailnet-only (no Funnel); optional ACL `src=tag:mac-ollama` if default-deny; reused `tag:k8s` keeps provisioning reliable.
- **M-6 conditional:** if T6 is ever adopted (add `OPEN_NOTEBOOK_API_KEY`), it MUST be a single atomic PR updating (1) `open-notebook-secret.sops.yaml`, (2) the pocket-bridge secret/env (`OPEN_NOTEBOOK_API_KEY`, currently `""`), and (3) Codex MCP `OPEN_NOTEBOOK_PASSWORD` — or pocket-bridge silently 401s and ingestion dies (looks identical to the known silent-embed-failure mode).

### 8a. MCP transport note (H-2)
Codex↔`open-notebook-mcp` = local stdio (no tailnet) — **confirm empirically per §10, don't assume** (P2-M-4). The tailnet carries the MCP server's **REST** calls to ON. Risk only if a tool hits a *streaming* ON endpoint through the L7 ingress. Verify (§10); if any tool hangs, fall back to a Tailscale **LoadBalancer Service (L4 TCP)** exposing `:5055` raw.
> **L4 fallback specifics (P2-M-3):** the LoadBalancer gets a *different* MagicDNS name and **no TLS cert**. Set `tailscale.com/hostname: open-notebook-api` on the Service, and change §9's `OPEN_NOTEBOOK_URL` to `http://open-notebook-api.tuxedo-halosaur.ts.net:5055` (plaintext HTTP — still WireGuard-encrypted by Tailscale).

## 9. Codex MCP config (after ingress live)

`~/.codex/config.toml`:
```toml
[mcp_servers.open-notebook]
command = "uvx"
args = ["open-notebook-mcp"]
startup_timeout_sec = 60

[mcp_servers.open-notebook.env]
OPEN_NOTEBOOK_URL = "https://open-notebook.tuxedo-halosaur.ts.net"
# no OPEN_NOTEBOOK_PASSWORD — T6 deferred.
# IF T6 is ever adopted (P2-L-3): add OPEN_NOTEBOOK_PASSWORD = "<key>" here AND do the
# §8 M-6 three-part atomic update (ON secret + pocket-bridge + this) in one PR, or ingestion 401s silently.
```
Prereq: `brew install uv`.

## 10. Acceptance tests

1. `kubectl get ingress -n open-notebook open-notebook-tailscale` → address assigned.
2. Tailscale admin: new device `open-notebook` present, **tagged `tag:k8s`** (H-1: re-verify after any recreate), HTTPS cert issued. **Confirm FQDN is exactly `open-notebook.tuxedo-halosaur.ts.net`** (no suffix); if suffixed, update §9 (L-1).
3. From the Mac: `curl -sf https://open-notebook.tuxedo-halosaur.ts.net/api/models/by-provider/ollama` → 200 with the models.
4. **H-3 / P2-H-2 test:** `curl -s https://open-notebook.tuxedo-halosaur.ts.net/api/sources?notebook_id=<id> | grep -i lab.mainertoo.com` → expect **no matches**. **If matches appear:** the FastAPI embeds `API_URL` (`open-notebook.lab.mainertoo.com`) in responses, unreachable off-LAN. Remediation — either (a) set `API_URL: https://open-notebook.tuxedo-halosaur.ts.net` in the HelmRelease *only after* confirming the Authentik UI on :8502 still works (it uses API_URL for SSR), or (b) leave as-is and treat the affected MCP tools as LAN-only.
   Also run `curl -v .../api/notebooks` → confirm **no redirect** to `http://` or `lab.mainertoo.com` (R2 proxy-header check).
5. (If default-deny ACL) from a non-`tag:mac-ollama` device: curl → blocked.
6. Codex `/mcp` → `open-notebook` connected; a **search AND a chat** tool each return data (chat exercises the streaming path of 8a). **P2-M-4 stdio confirm:** while a tool runs, `lsof -p $(pgrep -f open-notebook-mcp) | grep -i listen` → expect **empty** (stdio transport, no listening socket). If it's listening, the MCP transport is SSE/HTTP and 8a's L7 streaming concern is live.
7. Off-LAN test: hotspot, repeat #3 and #6 → still works.

## 11. Rollback

- Remove the ingress + netpol files from `kustomization.yaml` + commit → Flux prunes the K8s objects; the operator will *attempt* tailnet device removal (may take ~60s, or need manual removal per L-3). Codex falls back to LAN port-forward.
- **L-3:** manually delete the `open-notebook` device from Tailscale admin → Machines if it lingers offline (auto-expires in ~120 d otherwise). Revert any §6 ACL entry.
- Revert `pmset`/unload the caffeinate LaunchAgent.

## 12. Open questions / risks for adversarial pass 2

- R2: HTTPS→HTTP termination correctness for FastAPI behind the tailscale L7 proxy (host header / redirects). Partially covered by §10 #4; confirm no redirect loops.
- R4: §8a — empirically confirm `open-notebook-mcp` REST calls are non-streaming (or that L7 passes SSE). Test #6.
- R6: is the lid-open operating expectation (M-3) acceptable, or is an external-display clamshell rig wanted for a stationary "always-on" mode?
- R-new: NetworkPolicy selector for the ingress proxy pod (§5.3) — verify live label before committing.
- R-new: confirm tailnet ACL posture (allow-all vs default-deny) to know whether §6's ACL rule is required or redundant.
```
