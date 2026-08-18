# C. elegans Imaging Station — working notes

Raspberry Pi 5 imaging rig for C. elegans assays, plus a Windows analysis
launcher.

**Read `ARCHITECTURE.md` first.** It describes the system and its invariants.
This file holds only what you cannot cheaply derive by reading the code: the
environment, the deploy channel, and the rules that bite.

Anything in `docs/history/` is archived and must not be trusted — in particular
`CURRENT_STATE.md`, which instructs the reader to trust it over everything else
and is 36 commits out of date.

## Hardware

- **Camera**: Sony IMX477 HQ Camera (12.3 MP, 4056×3040 full array), CSI ribbon
- **Illumination**: custom LED transilluminator (bottom light), GPIO-controlled
- **Compute**: Raspberry Pi 5 (8 GB)
- **Optics**: fixed-magnification macro lens for whole-plate imaging

Magnification is fixed but **not** calibrated into the analysis: the Tierpsy
parameter files set `microns_per_pixel = -1.0`, so motility and crawling outputs
are pixels and pixels/second. The Pi *does* have a spatial calibration and
stamps ImageJ-readable µm/px into every TIFF — it just does not reach the
analysis parameters yet. Do not describe those columns as physical units.

## Dev environment

- **Laptop**: Windows 11, edits code locally. `picamera2` exists only on the Pi;
  import errors for it locally are expected.
- **Pi IP** `192.168.50.2` (static, direct ethernet, no router) ·
  **Laptop IP** `192.168.50.1` (hand-set static, ignores DHCP)
- **Other machines get an address automatically.** `eth0` uses NetworkManager
  `ipv4.method shared`, so the Pi is a DHCP server on the direct cable
  (`192.168.50.11`–`.254`). Options in
  `/etc/NetworkManager/dnsmasq-shared.d/celegans.conf` suppress the gateway and
  DNS advertisements so a client does not try to route the internet down a cable
  that goes nowhere. **Do NOT set `ipv4.method shared` without also setting
  `ipv4.addresses 192.168.50.2/24` in the same command** — alone it picks
  `10.42.0.1/24` and breaks every hardcoded address. Details in `README.md`.
- **SSH alias**: `ssh celegans` (user `pi`, key auth). Always use the alias,
  never `celegans.local`.
- **Repo on Pi**: `/home/pi/celegans-imaging/` ·
  **venv**: `.venv/` there, created with `--system-site-packages` so it can see
  the system `picamera2`, `opencv`, `numpy` and `scipy`.
- **Data on Pi**: `/home/pi/celegans-data/` — never inside the repo.

## Deployment — the Pi has no internet

Use **`scripts/deploy_local.sh`**. It copies the tree over SSH and restarts the
service. `scripts/deploy.sh` (commit → push → `git pull` on the Pi) only works if
the Pi is given outbound internet, which it normally does not have. A new Python
dependency cannot be installed by a deploy; it has to be put on the Pi by hand.

Deploying the capture service is a separate act from updating the launcher. A
change under `capture/` does nothing until it is deployed.

## Rules that bite

These are the ones that have cost real debugging. `ARCHITECTURE.md` §11 has the
full list with reasons.

1. **The launcher never imports `ultralytics` or `torch`.** Staging inference
   runs in a separate environment under `launcher/vision/`, reached by
   subprocess. Adding a direct import breaks the build and the licence boundary.
2. **Worker threads write status; the UI thread reads it. No widget is ever
   touched off the main thread.** Every agent's docstring states this.
3. **Do not change the camera locking/threading** in `capture/app/camera.py`.
   The preview thread deliberately does not take the capture lock; the comment
   at the site explains the deadlock that causes.
4. **Anything that changes what an analysis produces must enter the cache key.**
   Both caches (Tierpsy per-video, detections per-image) are otherwise happy to
   serve results produced under different settings.
5. **Never report an unmeasured quantity as zero.** A zero is a claim about the
   plate; a blank is a claim about the run. This has bitten twice.
6. **Reach tunable files through `launcher/paths.py`.** Hardcoded relative paths
   work in a checkout and silently use the wrong file when installed.
7. **Shell out through the engine abstraction, not to `docker`.** Three
   container engines are supported.

## Files-as-contract

No database anywhere. Folder structure and `session.json` manifests are the
source of truth; manifests are written atomically (`.tmp` then `os.replace`).
Every data file carries a `.sha256` sidecar, and `.acked` marks it synced.
The live schema is `capture/app/models.py` — read it there rather than from a
copy in a document.

## API auth

One shared bearer token, `X-Auth-Token` header or `?token=` query parameter,
compared with `secrets.compare_digest`. Only `/health` is unauthenticated. The
query-parameter path exists because an `<img>` MJPEG stream cannot send headers.

Config comes from `capture/.env` (gitignored — see `capture/.env.example`).

## Before you change the Development pipeline

`dev/development_tests/` is a seven-script harness covering it, including a
LibreOffice recalculation that checks every computed workbook cell against
Python. It exists because a signature mismatch once made every run raise into a
`pythonw.exe` void with no visible symptom. Run it.

## Testing and deps

Launcher dependencies are **exact-pinned** (`==`) on purpose; bumping one is a
deliberate act with a commit attached, not a casual edit. There are three
separate requirements files for three separate environments — the numpy versions
differing between launcher and vision is intentional isolation, not a conflict.
