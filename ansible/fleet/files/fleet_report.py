#!/usr/bin/env python3
"""Turn fleet probe lines (TSV on stdin) into a Discord webhook payload.

stdout: the JSON payload to POST.  stderr: the same summary, for the terminal.
Exit 0 always — a report must not fail the playbook.
"""
import json
import sys
from datetime import date

FIELDS = ("pending", "sec", "kernel_pkgs", "reboot", "lists", "unattended", "last_upgrade", "up")


def parse(line):
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 4:
        return None
    host = {"host": parts[0], "os": parts[1], "kernel": parts[2]}
    for p in parts[3:]:
        if "=" in p:
            k, _, v = p.partition("=")
            if k in FIELDS:
                host[k] = v
    return host


def num(h, key):
    try:
        return int(h.get(key, 0))
    except ValueError:
        return 0


def needs_attention(h):
    reasons = []
    if num(h, "pending"):
        reasons.append(f"{num(h, 'pending')} pending" + (f" ({num(h, 'sec')} security)" if num(h, "sec") else ""))
    if h.get("reboot", "no") != "no":
        reasons.append("reboot: " + h["reboot"])
    try:
        if int(h.get("lists", "0d").rstrip("d")) > 14:
            reasons.append("package lists " + h["lists"] + " old")
    except ValueError:
        pass
    return reasons


def main():
    hosts = [h for h in (parse(l) for l in sys.stdin if l.strip()) if h]
    flagged = [(h, needs_attention(h)) for h in hosts]
    flagged = [(h, r) for h, r in flagged if r]
    total_pending = sum(num(h, "pending") for h in hosts)
    total_sec = sum(num(h, "sec") for h in hosts)

    head = f"**Fleet status — {date.today().isoformat()}**"
    if not hosts:
        body = "No hosts reported (probe failed?)."
    elif not flagged:
        body = f"✅ All {len(hosts)} hosts up to date — 0 pending packages, none awaiting a reboot."
    else:
        lines = [
            f"⚠️ {len(flagged)} of {len(hosts)} hosts need attention "
            f"({total_pending} pending packages, {total_sec} security):",
            "```",
        ]
        for h, reasons in sorted(flagged, key=lambda x: -num(x[0], "pending")):
            lines.append(f"{h['host']:<22} {'; '.join(reasons)}")
        lines.append("```")
        body = "\n".join(lines)

    msg = f"{head}\n{body}"
    if len(msg) > 1900:  # Discord hard-caps at 2000
        msg = msg[:1880] + "\n… truncated ```"
    print(json.dumps({"content": msg, "allowed_mentions": {"parse": []}}))
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    main()
