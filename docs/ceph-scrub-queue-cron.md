# Ceph deep-scrub backlog auto-queue (cron)

A host-side maintenance script that runs on `pve-mammoth` once per day,
finds any PG that Ceph has flagged with `PG_NOT_DEEP_SCRUBBED`, and
re-queues it for an immediate deep-scrub via `ceph pg deep-scrub <pgid>`.

This document is the source of truth for the script — everything below
(script source, cron file, logrotate config, install steps) is intended
to be enough to rebuild the deployment from scratch on a freshly
re-imaged Proxmox host.

## Why this exists

Ceph's deep-scrub scheduler does not retroactively re-queue PGs that
have missed their deadline. Once a PG's `last_deep_scrub_stamp` falls
behind:

```
osd_deep_scrub_interval × (1 + mon_warn_pg_not_deep_scrubbed_ratio)
        = 14d × (1 + 1.5)
        = 35 days        (cluster setting, 2026-05-23)
```

…the cluster goes to `HEALTH_WARN` with `PG_NOT_DEEP_SCRUBBED`, and the
PG stays in that state indefinitely until someone manually issues
`ceph pg deep-scrub <pgid>`. Empirically observed multiple times on this
cluster (April–May 2026); confirmed not to self-clear over multi-day
windows.

**History note (2026-05-23):** the warn ratio was raised from the
upstream default `0.75` → `1.5` and `osd_deep_scrub_interval` was moved
from the `osd` section to `global` so the mons and OSDs actually agree
on the interval. Before that fix, mons evaluated the warn threshold
against the upstream default 7 d while OSDs scheduled against 14 d,
so warnings fired at ~12 d for PGs that the OSD scheduler did not
intend to touch for another 2 days. See
`docs/ceph-tuning-2026-05-07.md` § "2026-05-23 scrub-warn cleanup".

The underlying scheduler keeps rotating through its normal PG pool on
the natural interval — it just doesn't *catch up* on overdues. Under
steady client IO the mclock scheduler de-prioritises scrub vs client
work, so a handful of PGs drift behind every week. Without
intervention the backlog grows monotonically and the `CephHealthWarn`
Prometheus alert stays on.

This cron is a workaround for that scheduler limitation, not a fix.
Removing this cron is safe if/when upstream Ceph adds automatic
back-fill of overdue scrubs.

## What it does, concretely

1. Acquires a flock on `/run/ceph-queue-overdue-scrubs.lock` (a second
   concurrent invocation exits immediately).
2. Pings `ceph -s` to confirm mon quorum is reachable. Exits 1 if not.
3. Greps `ceph health detail` for `PG_NOT_DEEP_SCRUBBED` entries and
   extracts the PG IDs.
4. For each ID, issues `ceph pg deep-scrub <pgid>`. The OSD owning the
   PG accepts the request and queues the scrub at the next available
   slot; the work itself respects `osd_max_scrubs` (currently 2) and
   the mclock scheduler.
5. Appends a single timestamped line per PG to
   `/var/log/ceph-queue-overdue-scrubs.log`.
6. Exits 0 on the no-overdue happy path (does NOT page on healthy days).

Idempotent — re-queueing a PG that is currently scrubbing or already
queued is a no-op as far as the OSD is concerned.

## Script source

Install at `/usr/local/sbin/ceph-queue-overdue-scrubs.sh`, mode `0755`,
owner `root:root`.

```bash
#!/bin/bash
# /usr/local/sbin/ceph-queue-overdue-scrubs.sh
#
# Re-queue PGs that have tripped PG_NOT_DEEP_SCRUBBED. See
# docs/ceph-scrub-queue-cron.md in the kubernetes-lab repo for the why.
#
# Flags:
#   -n | --dry-run    show what would be queued, do not call ceph pg deep-scrub
#   -h | --help       print this header and exit

set -euo pipefail

LOG=/var/log/ceph-queue-overdue-scrubs.log
LOCK=/run/ceph-queue-overdue-scrubs.lock
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Single-instance lock. -n = non-blocking; if a previous run is still
# active we just exit so daily cron + manual invocation don't collide.
exec 9>"$LOCK"
flock -n 9 || { echo "another instance already running" >&2; exit 0; }

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

log "=== run start (dry_run=$DRY_RUN) ==="

# Cheap availability probe — if ceph -s fails the mons aren't reachable
# from this host and there's nothing useful to do.
if ! ceph -s >/dev/null 2>&1; then
    log "ceph cluster unreachable; aborting"
    exit 1
fi

# Pull the overdue PG list from health detail. The format is:
#   pg <pgid> not deep-scrubbed since <iso ts>
mapfile -t PGS < <(
    ceph health detail 2>/dev/null \
      | awk '/not deep-scrubbed since/ {print $2}'
)

if [[ ${#PGS[@]} -eq 0 ]]; then
    log "no overdue PGs; nothing to do"
    log "=== run end ==="
    exit 0
fi

log "found ${#PGS[@]} overdue PG(s): ${PGS[*]}"

queued=0
failed=0
for pg in "${PGS[@]}"; do
    if [[ $DRY_RUN -eq 1 ]]; then
        log "would queue: $pg"
        continue
    fi
    if out=$(ceph pg deep-scrub "$pg" 2>&1); then
        log "queued $pg: $out"
        queued=$((queued + 1))
    else
        log "FAILED $pg: $out"
        failed=$((failed + 1))
    fi
done

log "summary: queued=$queued failed=$failed total_seen=${#PGS[@]}"
log "=== run end ==="
exit 0
```

## Cron entry

Install at `/etc/cron.d/ceph-queue-overdue-scrubs`, mode `0644`,
owner `root:root`. Single line, no continuation — `cron.d` files use
the `<min> <hr> <dom> <mon> <dow> <user> <command>` six-field format.

```cron
# Re-queue overdue Ceph deep-scrubs daily at 04:15 local.
# 04:15 was chosen to land after the natural rolling scrub window
# typically goes quiet (~03:00-04:00) and well before the Kopia and
# CNPG backup windows (06:00-08:00 UTC = 23:00-01:00 local).
# Re-evaluate the time if either of those windows moves.
15 4 * * * root /usr/local/sbin/ceph-queue-overdue-scrubs.sh
```

## Logrotate

Install at `/etc/logrotate.d/ceph-queue-overdue-scrubs`, mode `0644`,
owner `root:root`.

```
/var/log/ceph-queue-overdue-scrubs.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0640 root adm
}
```

8 weeks of compressed history is plenty for "did this run, what did it
queue" — the script writes O(10) lines per day of work.

## Install steps (from a fresh pve-mammoth)

```bash
# 1. Script
ssh root@pve-mammoth 'install -m 0755 /dev/stdin /usr/local/sbin/ceph-queue-overdue-scrubs.sh' \
  < <(curl -fsSL https://raw.githubusercontent.com/mainertoo/kubernetes-lab/master/docs/ceph-scrub-queue-cron.md \
      | awk '/^```bash$/,/^```$/' | sed '1d;$d')
# (Or scp it from a local checkout; the above pulls the embedded
#  source block out of this very document.)

# 2. Cron + logrotate
ssh root@pve-mammoth 'install -m 0644 /dev/stdin /etc/cron.d/ceph-queue-overdue-scrubs' << 'EOF'
15 4 * * * root /usr/local/sbin/ceph-queue-overdue-scrubs.sh
EOF

ssh root@pve-mammoth 'install -m 0644 /dev/stdin /etc/logrotate.d/ceph-queue-overdue-scrubs' << 'EOF'
/var/log/ceph-queue-overdue-scrubs.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0640 root adm
}
EOF

# 3. Smoke test with --dry-run, then check the log
ssh root@pve-mammoth '/usr/local/sbin/ceph-queue-overdue-scrubs.sh --dry-run; \
                      tail -n 20 /var/log/ceph-queue-overdue-scrubs.log'
```

After deploy, the next live run is the cron firing at 04:15. To force
an immediate first real run instead of waiting:

```bash
ssh root@pve-mammoth '/usr/local/sbin/ceph-queue-overdue-scrubs.sh'
```

## Verifying it's working

The script is intentionally quiet — it only writes to its log and
doesn't email or alert. Two ways to check it's healthy:

```bash
# Last 5 cron runs
ssh root@pve-mammoth 'grep "run start" /var/log/ceph-queue-overdue-scrubs.log | tail -5'

# Cron actually fired (syslog records cron invocations)
ssh root@pve-mammoth 'grep ceph-queue-overdue /var/log/syslog | tail -5'

# Backlog actually drained
ssh root@pve-mammoth 'ceph health detail | grep -c "not deep-scrubbed"'
```

The third command should normally print `0`. A non-zero count for more
than ~6h after the most recent cron run is the signal to investigate
(usually means a PG is stuck scrubbing, a single PG is too large to
finish before the next deadline check, or `osd_max_scrubs` is too low
for current churn).

## Why pve-mammoth and not pve-zermatt or pve-whistler

All three Proxmox hosts in the cluster carry the Ceph admin keyring
(they're all mons), so any of them could run this. `pve-mammoth` is
chosen because:

- It's the most consistent host (no `isolcpus` workaround like
  `pve-whistler`, no SPCC OSD pending replacement like `pve-zermatt`).
- It hosts osd.0 and osd.3 — the two OSDs we've been tuning — so logs
  from this script live alongside the OSD logs that matter most.
- Keeping a single host owning the cron avoids two hosts queuing the
  same PG simultaneously (which Ceph handles gracefully, but creates
  noise in audit logs).

If `pve-mammoth` is ever decommissioned or rebuilt, move the cron file
+ script to another mon host. There is no state in the script that
needs migration.

## Tunable knobs

| Knob | Current | When to change |
|---|---|---|
| Run frequency | daily at 04:15 | If overdue backlog regularly exceeds 12-15 PGs/day, bump to twice daily (`15 4,16 * * *`). |
| Log retention | 8 weeks (logrotate) | Fine; entries are tiny. Drop to 4 weeks if /var/log pressure ever becomes a thing. |
| Dry-run | off in cron | Stays off. Use `--dry-run` only when investigating. |

The actual scrub throttling lives in Ceph config, not here:
- `osd_max_scrubs` (currently 2) — concurrent scrubs per OSD.
- `osd_deep_scrub_interval` (currently 14d, set in `global`) — how often Ceph wants each PG deep-scrubbed.
- `mon_warn_pg_not_deep_scrubbed_ratio` (currently 1.5, set in `global`) — extra fraction of `osd_deep_scrub_interval` before the warning fires (so warn at 14d × 2.5 = 35d for current settings).
  - Both of the above sit in `global`, not `osd` — mons evaluate the warn threshold from their own config view and the override has to be in a section they read.

Changing those is a Ceph-tuning decision, not a cron-tuning decision —
see `docs/ceph-tuning-2026-05-07.md`.

## Removing this cron

This is a workaround. Remove it if any of the following become true:

- Upstream Ceph fixes the auto-requeue behaviour (track upstream
  releases; this has been an open complaint for years).
- We move off mclock to a scheduler where scrubs aren't starved by
  client IO (unlikely on Squid — mclock is the path forward).
- The cluster's deep-scrub schedule consistently keeps up on its own
  for 30+ days. To test: temporarily disable the cron
  (`chmod -x /usr/local/sbin/ceph-queue-overdue-scrubs.sh`) and watch
  `ceph health` for a couple weeks. If it stays at `HEALTH_OK`, remove
  the cron. If `PG_NOT_DEEP_SCRUBBED` recurs, re-enable.

Removal:
```bash
ssh root@pve-mammoth '
  rm -f /etc/cron.d/ceph-queue-overdue-scrubs
  rm -f /etc/logrotate.d/ceph-queue-overdue-scrubs
  rm -f /usr/local/sbin/ceph-queue-overdue-scrubs.sh
  rm -f /var/log/ceph-queue-overdue-scrubs.log*
'
```
