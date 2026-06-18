# Skylight Family Calendar — DIY Build Plan

> Status: **Planning** (2026-06-18). A self-hosted "Skylight clone" — a wall-mounted
> touchscreen family calendar driven by Home Assistant, integrating the apps already
> running in the homelab (Google Calendar, Grocy, Donetick, DumbDo, Immich).

## Goal

Replicate (and exceed) a commercial Skylight Calendar: a wall-mounted touchscreen in a
high-traffic spot (kitchen/mudroom) showing a shared family calendar, per-person chores,
shopping list, meals, weather, and a photo-frame screensaver — with no subscription and
no vendor lock-in.

## Decisions (locked)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Calendar backend | **Google Calendar** | Best two-way HA sync, per-member calendars work out of the box |
| Display hardware | **27–32" capacitive touch monitor + mini PC** | True big-Skylight feel, 10-point touch; reference build scaled up |
| Chore/task split | **Donetick = assigned chores · Grocy = pantry/shopping/maintenance · DumbDo = scratchpad** | One source of truth per type; avoids competing task lists on screen |
| Meal planning | **Mealie = recipes + meal plan** (already deployed, mostly unused) | This project is the reason to finally use it; Grocy keeps pantry stock + shopping list |

## Architecture

```
                 ┌─────────────────────────────────────┐
                 │  Wall touchscreen (27–32") + mini PC │
                 │  Chrome kiosk → HA dashboard URL      │
                 └───────────────┬─────────────────────┘
                                 │ (LAN / WiFi)
                 ┌───────────────▼─────────────────────┐
                 │   Home Assistant (VM/HAOS — hub)      │
                 │   Lovelace "Skylight" dashboard       │
                 └──┬────────┬────────┬────────┬────────┘
                    │        │        │        │
        Google Cal ─┘   │    │    │    └─ Immich (photo screensaver)
        (2-way sync)    │    │    └─ Donetick (assigned chores + complete buttons)
                 Grocy ─┘    └─ Mealie (recipes + meal plan)
            (pantry/shopping/
             maintenance chores)
```

HA stays on its current VM/HAOS deployment (Matter/Thread + macvlan). The touchscreen is a
dumb client pointed at the HA dashboard. New supporting apps go into the K8s cluster via the
**`new-app` skill** (two-Kustomization split + label-driven backups).

## Home Assistant dashboard

> A ready-to-adapt Lovelace template lives in
> [`docs/skylight-calendar-dashboard.yaml`](skylight-calendar-dashboard.yaml) — composed
> build, with a legend of entity IDs to replace.

Build the Lovelace view to look like a Skylight. Two assembly options:

1. **Purpose-built:** [Skylight Calendar Card](https://community.home-assistant.io/t/skylight-calendar-card-a-family-friendly-schedule-card/981221) (HACS) — closest out-of-the-box look.
2. **Composed (more control):** Week Planner Card + Bubble Cards + Config Template Card + Button Card, as in the [reference DIY build](https://community.home-assistant.io/t/diy-family-calendar-skylight/844830).

### HACS cards / components

- **Week Planner Card** — main calendar grid + weather (composed option)
- **Skylight Calendar Card** — alternative all-in-one family calendar card
- **Bubble Cards** — tappable per-person filter chips + pop-ups
- **Config Template Card** — dynamic per-member calendar filtering
- **Button Card** — oversized "add event" / "mark chore done" buttons
- **kiosk-mode** — hide HA header/sidebar for the appliance look
- **Browser Mod** — navigation + screen control from automations

### HA integrations to enable

- **Google Calendar** — two-way, one calendar per family member
- **Grocy** (`custom-components/grocy`) — pantry stock, shopping list, tasks; + FamousWolf tasks/chores card
- **Mealie** (HA core `mealie` integration) — today's/this week's meal plan as a calendar/todo entity
- **Donetick** (`djryner/donetick_integration`) — sensor per chore + complete buttons; drives the per-person chore strip
- **Fully Kiosk** (optional, only if a tablet is added later) — motion/camera for wake-on-approach

### Dashboard sections (layout sketch)

- **Top:** week/month calendar, per-person color coding, tap-to-add-event
- **Left rail:** today's agenda + weather
- **Right rail:** Donetick assigned chores (per person), Grocy shopping list + expiring items, Mealie "what's for dinner"
- **Footer:** markdown family note board (or DumbPad if deployed)
- **Idle:** Immich slideshow screensaver (wake on touch / motion)

## Apps: roles & gaps

| App | Role on the wall | Status |
|-----|------------------|--------|
| Google Calendar | Event source (2-way) | **Add** — create per-member calendars + HA integration |
| Mealie | Recipes + weekly meal plan (shown on the wall) | Already deployed (`apps/base/mealie/`), mostly unused — this project activates it |
| Grocy | Pantry stock, shopping list, household-maintenance chores | Already self-hosted |
| Donetick | Assigned recurring family chores (kid-facing strip, points) | Already self-hosted |
| DumbDo | Quick scratch to-dos (no native HA integration — iframe or skip on wall) | Already self-hosted |
| Immich | Photo-frame screensaver when idle | Already self-hosted — reuse slideshow share |
| DumbPad | *Optional* family message board / notes | **Not deployed** — `new-app` it, or just use a HA markdown card instead |

No critical app is missing — Google Calendar is the one genuinely new piece. Mealie is
already present (just needs using). DumbPad is the only optional new deploy, and a HA
markdown card covers the same "leave a note" need without it.

### Mealie ↔ Grocy overlap

Both can do recipes, meal plans, and shopping lists — keep one owner per function to avoid
drift on the wall display:

- **Mealie:** recipe library + weekly meal plan (the family-facing "what's for dinner" card).
- **Grocy:** pantry stock + the single shopping list (it has the mature HA integration and
  low-stock → shopping-list automation). Mealie's meal-plan ingredients can feed Grocy's list
  manually, or just keep them independent at first.

## Hardware

**Path chosen: 27–32" capacitive touch monitor + mini PC.**

- **Display:** 27" capacitive touch monitor (e.g. ViewSonic TD2760, Iiyama ProLite T2755) or
  a 32" panel. Capacitive / 10-point touch (avoid resistive). Landscape for week view.
- **Compute:** small mini PC or Raspberry Pi 5 running Chrome in kiosk mode. Mini PC preferred
  for snappier rendering on a large panel; a spare thin client works.
- **Mounting:** flush wall mount + recessed low-voltage box for power/HDMI/USB-touch cable
  management. Plan in-wall power and a cable chase before purchase.
- **Wake-on-approach:** mmWave or PIR sensor → HA automation → Browser Mod screen on/off (or
  monitor DPMS via the mini PC). Keeps the panel dark/idle until someone walks up.
- **Touch wiring:** USB cable from monitor → mini PC for the touch digitizer (separate from
  video). Confirm the chosen monitor exposes USB touch on Linux/Windows.

## Bills of materials (3 tiers)

All three are **27–32" capacitive touch monitor + small fanless mini PC** running Chromium
(or Chrome on Windows) in kiosk mode, pointed at the HA dashboard. Prices are ballpark USD,
mid-2026.

### Tier 1 — Value (recommended starting point) · ~$450–550

| Part | Pick | ~Price | Notes |
|------|------|--------|-------|
| Display | **ViewSonic TD2760** 27" 1080p, 10-pt PCAP | $300–400 | HDMI/DP/VGA, VESA, Win/**Linux**/Chrome/Android; USB touch |
| Compute | **Beelink S12 Pro** or **MeLE Quieter 4C** (Intel N100, 16GB/512GB) | $130–170 | MeLE is fanless/silent; both sip power for 24/7 |
| Mount | Low-profile VESA fixed wall mount + recessed power/cable box | $30–60 | Plan in-wall power before buying |

Best bang-for-buck and the closest match to the reference DIY build, just bigger. 1080p at
27" is sharp enough at wall-viewing distance.

### Tier 2 — Flush in-wall / 32" · ~$900–1,200

| Part | Pick | ~Price | Notes |
|------|------|--------|-------|
| Display | **Mimo M32080C-OF** 32" open-frame PCAP, VESA-200 | $700–900 | Open-frame = clean flush in-wall mount (no bezel); HDMI |
| Compute | **ASUS ExpertCenter PN42** (N100/N200, fanless) | $250–350 | Kiosk/signage-rated, tidy single box behind the wall |
| Mount | In-wall recessed enclosure + open-frame bracket | $80–150 | Open-frame needs a custom recess/trim |

Pick this if you want the big, built-into-the-wall "appliance" look rather than a monitor on a bracket.

### Tier 3 — Always-on / industrial (24/7 duty) · ~$1,300–1,900

| Part | Pick | ~Price | Notes |
|------|------|--------|-------|
| Display | **Elo 2702L** 27" or **faytech FT27TMCAPOB** (24/7-rated PCAP) | $600–1,100 | Industrial duty cycle, bundled Linux touch drivers, no burn-in worry |
| Compute | **MITXPC MES-N100DC** fanless industrial mini PC | $300–450 | Wide-temp, 24/7-rated, DC-powered |
| Mount | Industrial VESA / recessed enclosure | $100–200 | |

Overkill for most homes, but the right call if the panel is on 24/7 and you don't want to
manage screensaver/dimming carefully.

### Selection notes (apply to any tier)

- **Capacitive (PCAP), not resistive** — you want multi-touch swipe/pinch, glove-free.
- **Matte / anti-glare IPS** — kitchens and mudrooms have glare; gloss is unreadable.
- **Touch is a separate USB cable** from video (HDMI/DP) — confirm Linux USB-touch support
  if driving with a Pi/Linux mini PC (Tier 1/2 picks all list Linux drivers). A Windows mini
  PC sidesteps driver questions, as the reference build did.
- **Burn-in / longevity:** consumer monitors (Tier 1) aren't rated for 24/7 — use the Immich
  screensaver + DPMS dimming and wake-on-motion so the panel isn't showing a static UI all day.
- **Resolution:** 1080p is fine at 27–32" wall distance; only go 4K if it'll be viewed up close.
- **Compute:** Intel **N100/N150**, 8–16GB RAM is plenty for a Chromium kiosk. Run Debian +
  Chromium `--kiosk`, or Windows + Chrome kiosk like the reference. Fanless preferred (silent,
  no dust intake behind a wall).

## Open items / next steps

1. Pick the physical mount location and confirm in-wall power + cable routing.
2. Choose the specific 27–32" touch monitor (verify Linux USB-touch support if using a Pi/mini PC).
3. Create per-family-member Google Calendars and wire the HA Google Calendar integration.
4. Build the Lovelace view (start with Skylight Calendar Card; fall back to composed cards).
5. Add Donetick + Grocy + Mealie HA integrations and the chore/shopping/meal cards (Mealie is already running — just wire it in and start populating recipes).
6. Wire Immich slideshow as the idle screensaver + a motion sensor for wake-on-approach.
7. *(Optional)* deploy DumbPad via the `new-app` skill — or skip it and use a HA markdown note card.

## Appendix A — Mini-PC kiosk setup (Debian + Chromium)

Minimal Debian, autologin to a bare X session that launches Chromium full-screen at the HA
dashboard. No desktop environment.

### 1. Base packages

```bash
sudo apt update
sudo apt install --no-install-recommends \
  xserver-xorg xinit x11-xserver-utils openbox chromium unclutter
```

`x11-xserver-utils` provides `xset` (DPMS control). `unclutter` hides the mouse cursor.

### 2. Autologin on tty1

```bash
sudo systemctl edit getty@tty1
```
```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin kiosk --noclear %I $TERM
```

### 3. Start X on login — `~/.bash_profile`

```bash
# ~kiosk/.bash_profile
if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
  exec startx -- -nocursor
fi
```

### 4. X session — `~/.xinitrc`

```bash
#!/bin/sh
xset -dpms        # let HA control the screen, not X's idle timer...
xset s off        # ...so disable X's own blanking
xset s noblank
unclutter -idle 0 &
openbox-session &

# Chromium kiosk. First run only: launch without --kiosk once to log into HA,
# then the profile persists the session cookie.
exec chromium \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --check-for-update-interval=31536000 \
  --autoplay-policy=no-user-gesture-required \
  --app="https://homeassistant.lab.mainertoo.com/lovelace-family/calendar"
```

### 5. Auth

Chromium persists the HA login cookie in its profile, so a one-time interactive login is
enough. For a fully hands-off rebuild, instead use a long-lived access token appended as
`?access_token=...` — but treat that URL as a secret (don't commit it).

### 6. Touch calibration

If touch axes are swapped/inverted, calibrate with `xinput_calibrator` and drop the resulting
matrix into `/etc/X11/xorg.conf.d/`. Most PCAP USB panels work without this.

## Appendix B — Wake-on-motion (screen on approach, off when idle)

A motion sensor (Zigbee PIR or mmWave presence) wakes the panel; after a few idle minutes it
sleeps. Because the panel is driven by the Linux mini PC, **HA toggles the screen by sending a
command to the mini PC**, not to the monitor directly. Two control paths:

### Path A — MQTT (recommended: no HA→device SSH, survives reboots)

On the mini PC, a tiny service subscribes to an MQTT topic and runs `xset`:

```bash
sudo apt install --no-install-recommends mosquitto-clients
```
`/usr/local/bin/kiosk-screen.sh`:
```bash
#!/bin/sh
export DISPLAY=:0
mosquitto_sub -h <MQTT_BROKER> -u kiosk -P '<pw>' -t 'kiosk/screen' | while read -r cmd; do
  case "$cmd" in
    on)  xset dpms force on  ;;
    off) xset dpms force off ;;
  esac
done
```
Run it as a systemd service (`After=graphical.target`, `Restart=always`). HA publishes `on`/`off`
to `kiosk/screen` via the `mqtt.publish` action.

### Path B — SSH command_line (simpler, but HA needs a key + known_hosts)

In HA `configuration.yaml` (HAOS: add the key under `/config` and reference it):
```yaml
shell_command:
  kiosk_screen_on:  "ssh -i /config/.ssh/kiosk -o StrictHostKeyChecking=accept-new kiosk@<ip> 'DISPLAY=:0 xset dpms force on'"
  kiosk_screen_off: "ssh -i /config/.ssh/kiosk -o StrictHostKeyChecking=accept-new kiosk@<ip> 'DISPLAY=:0 xset dpms force off'"
```

### The automation (works with either path)

```yaml
# Wake the panel the instant motion/presence is detected
- alias: Kiosk - wake on approach
  triggers:
    - trigger: state
      entity_id: binary_sensor.kitchen_presence   # mmWave preferred (no re-trigger gaps)
      to: "on"
  actions:
    - action: mqtt.publish        # Path A
      data: { topic: "kiosk/screen", payload: "on" }
    # - action: shell_command.kiosk_screen_on   # Path B alternative

# Sleep the panel after 5 min with no presence
- alias: Kiosk - sleep when idle
  triggers:
    - trigger: state
      entity_id: binary_sensor.kitchen_presence
      to: "off"
      for: "00:05:00"
  actions:
    - action: mqtt.publish
      data: { topic: "kiosk/screen", payload: "off" }
```

### Optional — Immich photo-frame before sleep

Instead of going straight to black, switch the dashboard to a full-screen Immich slideshow as a
screensaver, then sleep later. Use `browser_mod.navigate` (or a second Chromium tab) to point
at an Immich shared-album slideshow URL on a longer idle timer, and DPMS-off only overnight.

> **mmWave vs PIR:** prefer an mmWave presence sensor for this — PIR drops to "off" when you
> stand still (cooking, eating), which would sleep the screen in your face. mmWave holds
> presence while you're stationary.

## Sources

- [I built a Skylight calendar clone thanks to Home Assistant — XDA](https://www.xda-developers.com/i-built-a-skylight-calendar-clone-thanks-to-home-assistant/)
- [DIY Family Calendar (Skylight) — HA Community](https://community.home-assistant.io/t/diy-family-calendar-skylight/844830)
- [Skylight Calendar Card — HA Community](https://community.home-assistant.io/t/skylight-calendar-card-a-family-friendly-schedule-card/981221)
- [Donetick HA integration — djryner/donetick_integration](https://github.com/djryner/donetick_integration)
- [Grocy custom integration — custom-components/grocy](https://github.com/custom-components/grocy)
- [Grocy tasks/chores card — FamousWolf/grocy-tasks-chores](https://github.com/FamousWolf/grocy-tasks-chores)
- [DumbWare suite (DumbDo/DumbPad) — Noted](https://noted.lol/dumbware-io/)
- [Fully Kiosk Browser integration — Home Assistant](https://www.home-assistant.io/integrations/fully_kiosk/)
- [Family Dashboard – Home Assistant vs Magic Mirror — Planet4](https://www.planet4.se/family-dashboard-home-assistant-vs-magic-mirror/)
- [27–31" touchscreen for in-wall build — HA Community](https://community.home-assistant.io/t/27-31-touchscreen-for-visualization-build-in-wall/575402)
