# qidistudio

A browser-streamed [QIDIStudio](https://github.com/QIDITECH/QIDIStudio) desktop —
QIDI's own fork of OrcaSlicer — packaged the same way LinuxServer packages
mainline OrcaSlicer (Selkies base + AppImage extract + openbox autostart).

## Why this image exists

The `lscr.io/linuxserver/orcaslicer` image (deployed as the `orcaslicer` app)
runs **mainline** OrcaSlicer and ships **no network plugin**
(`Network plugin mode: modern (version: )`, no `plugins/` dir). Its device path
is therefore a dead end: the Device tab and **"Sync filaments"** from the QIDI
Box both fail with *"cannot connect to printer"*. Only the plugin-free Moonraker
HTTP upload (slice + send gcode) works there.

QIDIStudio bundles QIDI's network agent and native Max 4 / QIDI Box support, so
device connect-by-IP and Box filament sync work over the LAN. It ships x86-64
Linux AppImages (`..._Ubuntu24.AppImage`), which run on the cluster workers.

Both slicers are kept in parallel:

| App | URL | Use |
|-----|-----|-----|
| `orcaslicer` | `orcaslicer.lab.mainertoo.com` | mainline Orca — slice + send |
| `qidistudio` | `qidi.lab.mainertoo.com` | QIDI Box sync, device tab, drying/heater |

Note: mainline OrcaSlicer **2.3.2+** also added a native `QidiPrinterAgent` that
syncs the Box over Moonraker HTTP (no plugin). It may work in the `orcaslicer`
app by setting the printer agent to "Qidi"; QIDIStudio here is the guaranteed-
complete path (drying temps, slot UI) that mainline Orca does not fully expose.

## Build

Built and pushed to `ghcr.io/mainertoo/qidistudio` by
`.github/workflows/build-qidistudio.yml` on changes under `docker/qidistudio/**`.
Pin the QIDIStudio version via the `QIDISTUDIO_VERSION` build arg in the
`Dockerfile` (Renovate-tracked, `github-releases` datasource).

## Deploy

`apps/base/qidistudio/` (bjw-s app-template HelmRelease) + the Flux overlay at
`apps/production/qidistudio/`. GPU via the Intel device plugin
(`gpu.intel.com/i915`), `/config` on a Kopia-backed ceph-rbd PVC. Web port is
**3001** (Selkies base), unlike the older orcaslicer image's 3000 (KasmVNC).
