#!/usr/bin/env python3
"""
ratgdo-logger — persistent bus-level capture for a Security+ 1.0 ratgdo.

Why this exists: the right garage door (ratgdo32 fc5a30, secplus1) opens
spontaneously overnight with no Home Assistant / user command. The device
already logs at DEBUG (every secplus1 bus byte + state changes) over its
unencrypted ESPHome API, but that stream is live-only. This subscribes to it
continuously, filters the steady wall-panel keepalive noise, timestamps the
interesting lines, and on a door OPENING/OPEN transition dumps the preceding
raw-byte ring buffer so the next phantom event leaves a full bus trace showing
whether the ratgdo emitted the command itself (firmware/GPIO glitch) or genuinely
received one (external remote).

Output: timestamped lines to stdout (kubectl logs) and to $LOG_DIR/<name>.log
on a PVC (size-rotated), so an event days from now survives pod/log churn.
"""
import asyncio
import collections
import datetime
import os
import re

HOST = os.environ.get("RATGDO_HOST", "192.168.20.152")
PORT = int(os.environ.get("RATGDO_PORT", "6053"))
NAME = os.environ.get("RATGDO_NAME", "ratgdo")
LOG_DIR = os.environ.get("LOG_DIR", "/data")
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(25 * 1024 * 1024)))
RING = int(os.environ.get("RING_LINES", "600"))

LOG_PATH = os.path.join(LOG_DIR, f"{NAME}.log")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# secplus1 wall-panel emulation keepalive bytes — pure noise, ~4/sec
KEEPALIVE = ("[38]", "[39]", "[3a]")
# substrings that always matter
INTERESTING = (
    "door state", "light state", "lock state", "obstruction", "button",
    "motion", "received", "command", "toggle", "open", "clos",
    "boot", "reboot", "restart", "ota", "wifi", "disconnect", "warn", "error",
)

ring = collections.deque(maxlen=RING)
last_state = {}


def ts():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def write(line):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def emit(msg):
    write(f"[{ts()}] {msg}")


def is_interesting(low):
    if "sent byte" in low:                       # keep only non-keepalive sends
        return not any(b in low for b in KEEPALIVE)
    return any(kw in low for kw in INTERESTING)


def handle(raw_line):
    text = ANSI.sub("", raw_line).rstrip()
    if not text:
        return
    ring.append(f"[{ts()}] {text}")
    low = text.lower()
    if not is_interesting(low):
        return
    # collapse unchanged steady-state repeats (Door/Light/Lock state=...)
    for key in ("Door state=", "Light state=", "Lock state="):
        if key in text:
            val = text.split(key, 1)[1].strip()
            if last_state.get(key) == val:
                return
            last_state[key] = val
            emit(text)
            if key == "Door state=" and val.upper().startswith(("OPENING", "OPEN")):
                emit(f"===== EVENT CONTEXT: door -> {val} — dumping last {len(ring)} raw lines =====")
                for r in list(ring):
                    write(r)
                emit("===== END EVENT CONTEXT =====")
            return
    emit(text)


async def stream():
    from aioesphomeapi import APIClient
    try:
        from aioesphomeapi import LogLevel
        level = LogLevel.LOG_LEVEL_VERBOSE
    except Exception:
        level = 6  # LOG_LEVEL_VERBOSE

    cli = APIClient(HOST, PORT, None)
    await cli.connect(login=True)
    info = await cli.device_info()
    emit(f"# connected: {info.name} | esphome {info.esphome_version} | "
         f"project {getattr(info, 'project_name', '?')} {getattr(info, 'project_version', '')}")

    def on_log(resp):
        try:
            msg = resp.message.decode("utf-8", "replace")
        except Exception:
            msg = str(getattr(resp, "message", resp))
        for ln in msg.splitlines():
            handle(ln)

    try:
        cli.subscribe_logs(on_log, log_level=level)
    except TypeError:
        cli.subscribe_logs(on_log)

    # health-check loop: device_info() raises if the connection has died
    while True:
        await asyncio.sleep(30)
        await cli.device_info()


async def main():
    emit(f"# ratgdo-logger starting; target {NAME} {HOST}:{PORT}; log {LOG_PATH}")
    while True:
        try:
            await stream()
        except Exception as exc:  # noqa: BLE001 — log anything and retry
            emit(f"# connection lost: {type(exc).__name__}: {exc}; reconnecting in 10s")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
