# Hermes — CLI access & config management

How to administer the in-cluster [Hermes agent](../apps/base/hermes/) (Nous
Research `hermes-agent`) from the Mac command line, and where its configuration
lives.

The pod does **not** run `sshd`, so "SSH into Hermes" really means
`kubectl exec` into the pod. The helpers below (in `~/.zshrc` on the Mac) wrap
that. Default kube context is `production`.

## Mac `~/.zshrc` helpers (verbatim — restore these after a Mac rebuild)

```zsh
# Hermes in-cluster CLI (defaults to interactive chat). Old Mac-local deploy retired 2026-06-04.
# Pass any subcommand to run it in the pod, e.g.:
#   hermes config show
#   hermes config set web.search_backend searxng   # persists on the hermes-data PVC (daily Kopia backup)
#   hermes tools list ; hermes doctor
# Config lives in /opt/data/config.yaml on the PVC; restart the gateway/session to apply changes.
hermes() {
  kubectl exec -it -n hermes deploy/hermes -- /opt/hermes/bin/hermes "${@:-chat}"
}

# Raw shell inside the Hermes pod (poke at /opt/data, etc.).
hermes-sh() {
  kubectl exec -it -n hermes deploy/hermes -- sh
}

# Edit the PVC config.yaml in your local $EDITOR (the pod has no vi/nano):
# pull it out, edit, write it back. For a single value prefer `hermes config set`
# (it's validated); use this for bigger edits. Restart the gateway/session to apply.
hermes-config-edit() {
  local tmp="$(mktemp -t hermes-config-XXXX).yaml"
  kubectl exec -n hermes deploy/hermes -- cat /opt/data/config.yaml > "$tmp" || { echo "pull failed"; rm -f "$tmp"; return 1; }
  "${EDITOR:-vi}" "$tmp" || { rm -f "$tmp"; return 1; }
  kubectl exec -i -n hermes deploy/hermes -- sh -c 'cat > /opt/data/config.yaml' < "$tmp" \
    && echo "✓ config.yaml updated — restart the gateway/session to apply (e.g. /restart in Discord/Telegram)" \
    || echo "✗ write-back failed"
  rm -f "$tmp"
}
```

`hermes` alone opens interactive chat; pass any other subcommand to run it in
the pod.

## Common commands

```bash
hermes config show                            # view the resolved config
hermes config set web.search_backend searxng  # change a single value (validated)
hermes tools list                             # which toolsets are enabled
hermes doctor                                  # health check (env + toolsets)
hermes-sh                                       # raw shell in the pod
hermes-config-edit                              # edit /opt/data/config.yaml locally
```

## Where config lives — and which path to use

Hermes config is split across three surfaces that behave differently:

| Surface | Set via | Survives restart | In git |
|---|---|---|---|
| **K8s container env** (`FIRECRAWL_API_URL`, bot tokens, URLs) | HelmRelease `env:` / `envFrom:` | Yes (re-injected) | ✅ yes |
| **PVC `.env`** (`hermes config env-path`) | `hermes config` / file edit | Yes (PVC) | ❌ no |
| **PVC `config.yaml`** (`web.search_backend`, toolsets, browser…) | `hermes config set` / `hermes-config-edit` | Yes (PVC) | ❌ no |

- **Env vars → HelmRelease (gitops).** A shell `export` only affects that one
  shell and is wiped on restart; the running gateway never sees it. K8s
  container env also **takes precedence** over the PVC `.env`, so don't set the
  same key in both places. Example in-repo: `FIRECRAWL_API_URL`
  (`apps/base/hermes/hermes-release.yaml`) wiring web_search/web_extract to the
  in-cluster Firecrawl.
- **`config.yaml` settings → CLI on the PVC.** These are **not** templated from
  the HelmRelease — they're seeded on the `hermes-data` PVC and edited with
  `hermes config set`. The PVC is `backup: daily` + Kopia, so these changes ride
  the backup/restore plan rather than Flux. This is the accepted model (see
  caveat below).

### Caveats

- **Restart to apply.** Hermes loads config at gateway/session start. After a
  `set` / `hermes-config-edit`, run `/restart` (or start a new session) in
  Discord/Telegram.
- **`hermes-config-edit` writes verbatim** — a YAML typo won't surface until the
  next restart. Prefer `hermes config set` for single values (it validates).
- **Off-git = not reconstructed by Flux.** A bare cluster rebuild from git
  brings back the deployment but not PVC-only config; that returns via a Kopia
  restore (which may be older than your latest change). Anything you want
  reproducible-from-git belongs in the HelmRelease.
- **No editor in the pod** (`vi`/`nano` absent) — hence `hermes-config-edit`
  pulls the file out to your local `$EDITOR`.
