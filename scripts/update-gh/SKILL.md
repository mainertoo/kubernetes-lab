---
name: update-gh
description: Triage and merge all open Renovate PRs in the kubernetes-lab (Hephaestus) GitHub repo — inventory every PR's version jump, auto-merge the low-risk ones, run per-app pre-checks on the risky ones (CSI/storage, monitoring CRDs, DB-migrating apps, bazarr/DUMB dev digests), get the user's OK, merge, reconcile Flux and verify the rollout. Use when the user says "update GH", "update github", "check renovate PRs", "merge the renovate PRs", or invokes /update-gh.
---

# Update GH — Renovate PR triage

Goal: get the open Renovate PRs merged without breaking prod. Merge the safe PRs
yourself. For the risky ones, do the homework first, then ask the user once, with
a recommendation for each.

Repo: `mainertoo/kubernetes-lab`, trunk `master`, merge style `--merge` (merge
commits, not squash). Helper: `~/.claude/skills/update-gh/renovate-inventory.sh`.

## 1. Inventory

```bash
~/.claude/skills/update-gh/renovate-inventory.sh          # all open Renovate PRs
~/.claude/skills/update-gh/renovate-inventory.sh 2390     # one PR
```
For each PR it prints: number, title, mergeable state, a CI summary, and every
changed `-`/`+` line per file. Also run `flux get all -A --status-selector ready=false`.
If the cluster is already unhealthy, fix that or tell the user before merging anything.

Read the memory index for **held** PRs (search "Renovate", "HOLD", "held").
A PR the user explicitly held stays open unless they lift the hold.

## 2. Classify

A PR is **low risk** only if CI is green AND it is one of:
- an app image patch bump, or a `-lsNNN` / build-number bump (Plex, linuxserver, home-operations *arr)
- a minor bump of a stateless or self-migrating app with a history of clean bumps
  (`git log --oneline -5 -- apps/base/<app>/`: earlier Renovate bumps of it merged without follow-up fixes)
- Home Assistant patch (`2026.11.x`), Traefik or cloudflared patch
- Meilisearch for karakeep (dumpless upgrade is configured in the release)
- Redis server minor (and the matching pocket-bridge `requirements.txt` client bump)

Everything else is **higher risk**. Do the matching check before asking:

| Kind | Pre-check |
|---|---|
| `infrastructure/controllers/**` storage (ceph-csi, nfs-csi sidecars, snapshot-controller) | Read the release notes or CHANGELOG (`gh api repos/kubernetes-csi/<repo>/contents/CHANGELOG/CHANGELOG-X.Y.md?ref=release-X.Y`, quote the path in zsh). CVE/Go-rebuild-only = recommend. Confirm Ceph HEALTH_OK (`ceph_health_status` == 0 via the Prometheus proxy below). node-driver-registrar rolls the nodeplugin DaemonSets: safe while the cephfs StorageClass has no `mounter: fuse`. snapshot-controller: **see memory `feedback_snapshot_controller_v5_installCRDs_trap`**, and keep the v1beta2 group-snapshot CRDs present. |
| kube-prometheus-stack | Read the chart release notes for every version jumped (`gh release view kube-prometheus-stack-<v> -R prometheus-community/helm-charts`). Check that no changed default collides with our values. |
| Apps with DB migrations (wiki, CNPG apps, Prisma apps such as excalidash) | Read the upstream notes and compare the migration files between the two tags (`gh api repos/<o>/<r>/compare/<old>...<new> --jq '.files[].filename'`). For CNPG, confirm a `Backup` from the last ~24h has `completed`. Multi-image apps (excalidash frontend+backend) merge **together**. |
| **bazarr** `:development` digest | Mandatory migration diff, see memory `project_bazarr_dev_digest_migration_crashloops`. Run `renovate-inventory.sh --bazarr <new-sha256>`. Identical file set + hashes = safe. A new revision file = warn the user and be ready to repair forward. |
| **DUMB** `:dev` digest | `crane config ghcr.io/i-am-puid-0/dumb@sha256:<d> \| jq -r '.config.Env[]\|select(test("VERSION"))'` for old and new, plus `gh api 'repos/I-am-PUID-0/DUMB/commits?sha=dev&per_page=20'`. Summarize the churn. The restart briefly drops Plex/Jellyfin media mounts, so flag it. Check the setgid-shim memory (`project_dumb_2_23_setgid_archive_workspace_bug`). |
| Major version bumps, chart majors, Flux/K3s/CRD sources | Read the breaking changes. Never merge without the user. Anything under `clusters/production/` or a Flux `GitRepository`/`Kustomization` source change needs explicit confirmation (CLAUDE.md). |

## 3. Merge low risk

```bash
for n in <list>; do gh pr merge $n --merge >/dev/null 2>&1; echo "#$n $(gh pr view $n --json state --jq .state)"; done
git pull -q && flux reconcile source git flux-system -n flux-system
```
Tell the user in one line what was merged.

## 4. Ask about the rest, once

Use AskUserQuestion with **multiSelect**, grouping related PRs (all CSI sidecars as
one option; excalidash frontend+backend as one). Each option's description says
what changes, what the pre-check found, and the blast radius. Mark recommended
groups "(Recommended)". The auto-mode classifier blocks merging infra or storage
PRs without this explicit OK, so do not try to work around that.

Merge the approved groups. For storage infra, merge one PR at a time and wait for
`kubectl rollout status` between merges, unless the user says to batch them.

## 5. Verify (do not skip)

```bash
flux reconcile ks infra-controllers -n flux-system   # if infra changed
flux reconcile ks apps-production -n flux-system
flux get hr -A --no-header | awk '$5!="True"'          # wait on any "Running 'upgrade'" with kubectl wait hr/<n> --for=condition=Ready --timeout=360s
kubectl get pods -A --no-headers | awk '($4!="Running" && $4!="Completed") || ($3 ~ /^0\// && $4=="Running")'
```
- CSI: `kubectl rollout status` on every csi deploy/ds, then check the sig-storage image tags.
- Ceph: `kubectl get --raw "/api/v1/namespaces/kube-prometheus-stack/services/http:prometheus-operated:9090/proxy/api/v1/query?query=ceph_health_status" | jq -r '.data.result[0].value[1]'` should print `0`.
- Migrating apps: grep the new pod's logs for `migrat|error`, and `curl -s -o /dev/null -w '%{http_code}' https://<app>.lab.mainertoo.com` should return 200.
- bazarr: 1/1, and `kubectl -n media logs deploy/bazarr | grep -iE 'alembic|already exists|locate revision'` is empty.
- DUMB: poll until Plex's rclone sidecar reaches `dumb-external.dumb.svc.cluster.local:8282`. "Connection refused" during preinstall is expected for about a minute.
- Use `kubectl wait` or `rollout status` (they block). Bare `sleep` chains are refused. `timeout` is not installed on macOS.

Finally, re-list open Renovate PRs. New ones often appear mid-run (Renovate
rebases after merges). Triage them the same way or report them.

## 5b. K3s / containerd AppArmor watch (every run, until resolved)

```bash
~/.claude/skills/update-gh/k3s-containerd-check.sh   # exit 0 = nothing yet, 10 = act
```
Background: Ubuntu kernels >= 7.0.0-41/42 force an `//&unconfined` AppArmor
stack on container processes, so containerd's default profile blocks
self-signals. gosu livelocks and mariadbd hangs (LP#2172640, LP#2172422). The
fix is containerd#14487, backported to 2.0/2.3/2.4 but **not 2.2**, and K3s
v1.35 bundles 2.2.x. So the fix most likely arrives via a **K3s minor upgrade**
(v1.36+, containerd 2.3.x). Two things are held until then: the prod kernel
(memory `project_ubuntu_kernel_7_0_0_42_gosu_hang_2026_10_29`) and
`appArmorProfile: Unconfined` on the staging openemr/openmrs MariaDB pods (PR #2422).

When the script exits 10, report it and propose this order. Every step needs the user's OK:
1. Upgrade **staging** K3s to that release (`k3s_version` in
   `ansible/k3s-cluster/inventory/staging/group_vars/all.yml`, then `k3s_upgrade.yml`).
   A minor bump: read the K3s/Kubernetes upgrade notes first.
2. On a staging node still on kernel -42/-44, run the gosu test **without** the override:
   `kubectl run gosutest --context staging --restart=Never --image=docker.io/library/mariadb:13.0.2 --overrides='{"spec":{"activeDeadlineSeconds":60}}' --command -- gosu mysql id`
   It must print `uid=999(mysql)`. `DeadlineExceeded` means not fixed.
3. Revert the PR #2422 lines and confirm openemr/openmrs MariaDB still start.
4. Upgrade **prod** K3s while the kernel is still held on -38.
5. Only then lift the hold (`k3s_os_upgrade.yml -e kernel_hold=unhold`). Patch one
   node, run the gosu test pinned to it, check firecrawl rabbitmq and nuq-postgres
   (they use gosu), then roll the rest.
Never lift the kernel hold before the K3s upgrade. Leave the
`apparmor_restrict_unprivileged_unconfined` sysctl at its default.

If the script reports `backport-merged-on-release/2.2`, a 2.2 backport now exists:
add its merge SHA to `fix_sha()` so a K3s v1.35 patch release can qualify.
Once the hold and workaround are both gone, delete this section and the script.

## 6. Report

One short summary: merged (low / approved), anything held and why, any
post-merge issues, and any PR still open with a recommendation. If a hold or
gotcha was learned, update the Renovate memory file.
