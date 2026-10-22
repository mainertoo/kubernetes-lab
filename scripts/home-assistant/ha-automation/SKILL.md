---
name: ha-automation
description: Design, ship, verify and debug automations (and helper entities like template fans/switches, scripts, input_booleans) on the LAB Home Assistant in the kubernetes-lab repo — as git-managed HA packages under apps/base/home-assistant/packages/, delivered by PR + Flux. Covers entity discovery and device quirks (Bond RF toggles, assumed_state, status lights, upstream power relays), collision audits against existing automations, the HA scripting traps that have bitten here (condition vs stop in nested blocks, `for:` triggers after restart, unavailable→state), post-merge verification, misfire forensics via logbook context + traces, HomeKit exposure and wiki docs. Use whenever the user asks for a new HA automation, "make X turn off/on when Y", a virtual fan/switch for a dumb device, changing or fixing an existing automation, or "why did this automation fire / do that". Not for the remote Bell HA (use bell-ha).
---

# ha-automation — lab Home Assistant automations, the repo way

Lab HA runs in-cluster (`home-assistant` namespace, Flux-managed). New automations are
**git-managed HA packages**, not UI-editor automations, unless the user asks for a UI one.

Source of truth: `scripts/home-assistant/ha-automation/` in the kubernetes-lab repo;
`~/.claude/skills/ha-automation` is a symlink to it, so edit the repo copy and PR it.

Helper: `~/.claude/skills/ha-automation/haauto.py` (talks to the **lab** HA via
`~/kubernetes-lab/scripts/home-assistant/hass.py` credentials; read-only except
`enable`/`disable`). Run it from the repo root.

```bash
H=~/.claude/skills/ha-automation/haauto.py
python3 $H find <term> [--domain light,fan] [--area master_bedroom]   # ids, state, area, platform, QUIRKS
python3 $H audit <entity_id>...        # automations already touching these entities/devices
python3 $H check <package.yaml>        # entity ids vs live HA + renders static templates; exit 1 on problems
python3 $H template '<jinja>'          # render in live HA
python3 $H logbook <entity_id>... --minutes 90   # state changes with "← caused by" automation
python3 $H trace <automation id|entity> --runs 3  # step-by-step: which branch, which result
python3 $H verify <config_id>... --wait          # after merge: RUNNING, check_config, loaded + on
python3 $H enable|disable <automation>           # runtime kill-switch (persists across restart!)
```

## Workflow

### 1. Pin down intent, then the entities
- Run `find` for every device the user named. Map their words to entity ids and **show
  the mapping back** when anything is ambiguous ("master bathroom light" = fan light *and*
  mirror lights? → state your choice, offer to drop one).
- Read the **QUIRKS** column — it changes the design:
  - `BOND` / `ASSUMED-STATE` → on/off is often **one RF toggle**; HA's state is a guess.
    Never send `turn_off` to something HA thinks is already off (it toggles ON). Filter
    targets with `select('is_state','on')`. Fix a wrong guess with
    `bond.set_light_power_tracked_state` (no RF sent) or the physical remote — not HA/Siri.
  - `switch_as_x` → act on the light/fan wrapper, not the hidden switch.
  - `template` → no device, so **no area** until set in `registry-overrides.yaml`.
  - `UNAVAILABLE`/`UNKNOWN` → `to: "on"` will also fire from `unavailable`.
- Things that are never "just a light" here: the **Third Reality night lights**
  (`light.0xb40e…`) are the automation-driven **house-status lights** (green/amber/red);
  **upstream power relays** (Mason ZEN32 `switch.main_light_and_fan_switch`, Kitchen Shelly
  `…output_1`, `switch.tsw02`) cut power to smart devices behind them; `switch.3d_printer_plug`,
  washer/dryer power, `switch.water_main_shut_off_valve`, `switch.slzb_mr4u_router_mason_closet`
  must never be switched by a broad automation.
- Check what the trigger sensor really reports (`find` → device_class; `logbook` for how
  often it flips). The Third Reality PIRs are `device_class: motion` via registry override.

### 2. Collision audit
`audit <every target and trigger entity>` — existing automations (UI and package) that
read or drive them. Watch for scene controllers (ZEN32/ZEN35 blueprints toggle Bond
lights and track light state) and the house-status light automations. Say what you found.

### 3. Write the package
New file `apps/base/home-assistant/packages/<feature>.yaml` + one line in the
`home-assistant-packages` configMapGenerator in `apps/base/home-assistant/kustomization.yaml`.
Packages merge with `automation: !include automations.yaml`, `script: !include scripts.yaml`,
inline `input_boolean:` and `template: !include templates.yaml` — ids/keys must be unique
(the `check`/`audit` run catches collisions with live entities).

Header comment, matching `printer_costs.yaml` / `master_suite_auto_off.yaml`:

```yaml
# <feature>.yaml — HA package: <one line>.
#
# GIT IS THE SOURCE OF TRUTH. Shipped in the hash-suffixed `home-assistant-packages`
# ConfigMap and mounted READ-ONLY as the /config/packages directory; configuration.yaml
# loads it via `homeassistant: packages: !include_dir_named packages` (asserted by the
# ensure-ha-config initContainer). A change here rolls the HA pod.
#
# <what it does, which entities, what is deliberately NOT touched and why, known limits>
# ─────────────────────────────────────────────────────────────────────────────
automation:
  - id: <snake_case_unique_id>          # stable; the entity_id comes from the alias
    alias: "<Area> — <what it does>"
    description: >-
      ...
    mode: single|restart|queued
    triggers: [...]
    conditions: [...]
    actions: [...]
```

### 4. Scripting rules (each one has bitten this house)
1. **A `condition` inside `if: then:` / `choose:` only ends that nested block** — the
   steps after the `if` still run. To abort the run, use `- stop: "<reason>"`.
2. **`for:` triggers only fire on a transition.** A room already empty when HA starts never
   fires. If it matters, add `trigger: homeassistant / event: start` (id `started`) and a
   `wait_for_trigger` with `timeout` + `continue_on_timeout: true`, then `stop` if the
   wait was satisfied. Don't use a `for:` *condition* at startup — `last_changed` resets
   on restart.
3. **Act only on what needs changing** — `select('is_state','on')` before `turn_off`
   (Bond toggles; also keeps logbooks/HomeKit quiet).
4. **`to:` matches from `unavailable`** — add `from:` or a `not_from: [unavailable, unknown]`
   when a dropout must not count.
5. **Watchdogs poll, they don't edge-trigger** (see wiki Lessons learned → automation
   patterns): `time_pattern` + state-based, idempotent branches; notify once via a flag.
6. **Helpers in YAML get no `initial:`** — HA restores their last state. Hide internal
   helpers (`hidden_by: user` in `registry-overrides.yaml`) so they don't look like controls.
7. **Template fan/switch/light for a dumb device:** state is *assumed* — derive it from
   something real (relay state + a flag set by detection), and make detection watch the
   physical relay so wall-switch use stays in sync (pattern: `upstairs_bathroom_fan.yaml`).
8. **Device triggers** ("occupied"/"not_occupied") keep working after a `device_class`
   override; prefer plain `state` triggers in new work.
9. `mode:` — `restart` when a newer trigger should supersede a waiting run; `queued` for
   short state-mirroring automations; `single` otherwise.

### 5. Validate before the PR
```bash
python3 -c "import yaml;yaml.safe_load(open('apps/base/home-assistant/packages/<f>.yaml'))"
kustomize build apps/base/home-assistant | grep -c '<f>.yaml:'     # 1 = in the ConfigMap
python3 $H check apps/base/home-assistant/packages/<f>.yaml        # exit 0
```
`check` renders every static `{{ }}`; templates using `trigger`/`wait`/`this`/`repeat` or
`variables:` names are runtime-only — reason about those by hand (or `template` a
substituted copy).

### 6. PR → merge → verify
- Fresh branch from up-to-date `master` (never reuse a merged branch). PR to `master`;
  commit/PR attribution per the session reminder. Never `kubectl apply`.
- After the user merges: `flux get kustomizations -A | grep home-assistant`. If the pod
  hasn't rolled (compare `kustomize build … | grep -o 'home-assistant-packages-[a-z0-9]*'`
  with the deployment's mounted ConfigMap), `flux reconcile helmrelease home-assistant -n
  home-assistant`, then `kubectl -n home-assistant rollout status deploy/home-assistant`.
- `python3 $H verify <config ids> --wait`. A runtime-`disable`d automation **stays off
  after the restart** — `enable` it explicitly.
- The pod roll restarts HA (~35 s) and every HomeKit bridge once. Afterwards confirm the
  29 HomeKit entries are running (`status==1`) — see wiki `smart-home/homekit-bridges`.

### 7. Areas, names, HomeKit
- Template entities: add `area_id` (and a good `name`) to
  `scripts/home-assistant/registry-overrides.yaml`; `./ha-registry.py export <ids>` first
  (aliases are a closed set), then `apply`, then `check` = in sync. ⚠️ Only `apply` from a
  branch that contains every unmerged registry PR — a stale manifest reverts live fixes.
- To expose a new entity in Apple Home: add it to that room's **HomeKit <Room>** bridge
  (include mode) — name it right *first* (Apple copies names at pairing). Locks, cameras
  and TVs need accessory-mode entries. One reload per bridge; check `status==1`.

### 8. Testing
Don't actuate physical devices to test (late night, people in rooms) without asking. Give
the user a concrete test list: each trigger path, each "should NOT happen" case, and which
knob to turn if it misbehaves.

### 9. Document
- Wiki `smart-home/automations` → table + notes under **"Git-managed package automations"**;
  bump the count line.
- New traps → wiki `smart-home/lessons-learned` (**"Automation traps"** section).
- HomeKit-visible changes → wiki `smart-home/homekit-bridges`.
Use the `wikijs-update` skill.

## When an automation misbehaves
1. `disable` it immediately if it can fire again before a fix merges (and say so).
2. `logbook <the affected entities> --minutes 90` — the `← automation.…` context shows
   who changed what. A device that changed physically but **isn't in the logbook** means
   HA never saw it: suspect a toggle/assumed-state device.
3. `trace <automation> --runs 3` — shows the branch taken, every condition result, wait
   outcome, and service params.
4. Fix in git → PR → merge → `verify` → `enable` → add the lesson to the wiki.

## Existing packages (reference implementations)
| File | Shows |
|---|---|
| `printer_costs.yaml` | input_number + rest + template sensors |
| `upstairs_bathroom_fan.yaml` | template fan over a dumb device, assumed state, relay-pattern detection, script with timed steps |
| `master_suite_auto_off.yaml` | vacancy timeout, restart-safe startup path with `stop`, only-act-on-`on` filtering |
