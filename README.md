# WormScan — C. elegans Imaging Station

Automated imaging and motility analysis system for *C. elegans* assays, built on a Raspberry Pi 5. Captures whole-plate timelapse and still images, syncs data to a Windows laptop, and runs the analysis pipelines locally: motility and crawling via Tierpsy
Tracker in a container, colony counting in pure Python, and developmental
staging with a YOLO model.

---

## Start here

**Using WormScan?** Two documents, in order:

1. **[Installing WormScan](launcher/INSTALL.md)** - download one file,
   double-click it, paste one connection link. No Python, no administrator
   rights, nothing to configure about the network.
2. **[User guide](docs/USER-GUIDE.md)** - running an experiment end to end, and
   **what the numbers mean and which of them to trust.** Read the
   [Reading the output](docs/USER-GUIDE.md#reading-the-output) section before
   putting any of these figures in a figure: motility distances are in pixels
   rather than microns, and the worm-staging counts are provisional in a
   specific, known way.

**Developing WormScan?** Read **[ARCHITECTURE.md](ARCHITECTURE.md)** — how the
system fits together, the boundaries between its parts, and the invariants that
must hold. Documents under `docs/history/` are archived snapshots and are **not**
current; `docs/history/CURRENT_STATE.md` in particular asks to be trusted over
everything else and is long out of date.

Everything below this point is for developing and maintaining the system, not
for using it.

---

## Hardware

| Component | Details |
|-----------|---------|
| Compute | Raspberry Pi 5 (8 GB RAM) |
| Camera | Sony IMX477 HQ Camera (12.3 MP, 4056×3040) via CSI ribbon |
| Illumination | Custom LED transilluminator (bottom light), GPIO-controlled |
| Optics | Fixed-magnification macro lens for whole-plate imaging |

---

## Repository layout

```
ARCHITECTURE.md              # How the system fits together — read this first
CLAUDE.md                    # Working notes: environment, deploy, rules that bite
BACKLOG.md                   # Open work

capture/                     # FastAPI service (runs on the Pi)
├── app/
│   ├── main.py              # App factory, route mounting, static files
│   ├── config.py            # pydantic-settings, reads from .env
│   ├── auth.py              # Bearer token dependency
│   ├── models.py            # Session, Plate, request/response models
│   ├── sessions.py          # Filesystem-backed session CRUD
│   ├── camera.py            # picamera2 camera manager (see the locking comment)
│   ├── capture_ops.py       # Still and video capture logic
│   ├── focus.py             # Laplacian focus scoring
│   ├── disk_guard.py        # Capture-time free-space guard (HTTP 507)
│   ├── routers/             # camera_ctrl, free_capture, manifest,
│   │                        #   plate_capture, preview, system, analyze
│   └── static/              # Web UI, no framework: index.html, app.js,
│                            #   app.css, themes.css, extras.js
├── capture.py               # Standalone full-res capture script (do not modify)
├── retention.py             # Data retention daemon (removes acked files)
├── requirements.txt
└── .env.example

launcher/                    # Windows desktop app (CustomTkinter)
├── main.py                  # Entry point; starts the agent threads
├── config.py                # Settings dataclass, APPDATA persistence
├── paths.py                 # Tunable/model/venv resolution (dev vs installed)
├── sync.py                  # Background sync thread (Pi → laptop mirror)
├── ui.py                    # Main window, analysis/settings/review dialogs
├── theme.py, widgets.py     # Design tokens + reusable widget layer (view-only)
├── analyze_worker.py        # Services the Pi's "analyse on laptop" relay
├── update_check.py          # One-shot GitHub release check
├── survival*.py             # Development pipeline: agent, cache, Excel,
│                            #   figures, size distributions, HTML explorer
├── analysis/                # Motility, crawling and counting pipelines
│   ├── motility.py          # MotilityAgent + MotilityStatus
│   ├── crawling*.py         # Crawling agent, metrics, linker, plots, renders
│   ├── counting*.py         # Colony counter + agent; crop_wells.py
│   ├── engine.py            # Container-engine abstraction (docker/podman/nerdctl)
│   ├── docker_utils.py      # Tierpsy invocation + preflight, over engine.py
│   ├── ffmpeg_utils.py      # Discovery, probe_fps(), convert_to_avi()
│   ├── concurrency.py       # Worker sizing from the engine's resource view
│   ├── analysis_csv.py      # Grouping, flicker filter, bend-rate engine
│   ├── render_video.py      # Overlay renders shared by both video pipelines
│   └── plots.py             # Per-video PNGs and overview figure
├── vision/                  # Staging inference — SEPARATE venv, never imported
│   ├── infer_stage.py       # CLI the launcher shells out to
│   ├── tiled_infer.py       # Tiling + merge library
│   └── stage_conf.json      # Shared defaults, with measurement provenance
├── viewers/                 # Standalone HTML grid-viewer generators
├── motility_params.json     # Tierpsy parameters — motility
├── crawling_params.json     # Tierpsy parameters — crawling (deliberately different)
├── requirements.txt         # Exact-pinned; see the policy comment in the file
├── setup.bat                # Developer convenience venv builder
└── INSTALL.md               # End-user installation guide

packaging/                   # Windows installer (Inno Setup) + build scripts
dev/                         # Dev-only scripts, not imported by the app
├── tools/                   # Diagnostics: param sweeps, spectra, previews
└── development_tests/       # Verification harness for the Development pipeline

deploy/
├── celegans-capture.service    # systemd unit for the FastAPI service
├── celegans-retention.service  # systemd unit for the retention daemon
└── celegans-retention.timer    # systemd timer (2 min after boot, then every 15 min)

scripts/
├── deploy_local.sh             # THE deploy: rsync/tar over SSH (Pi has no internet)
├── deploy.sh                   # Push → pull → restart (needs internet on the Pi)
├── sync-pi-clock.sh            # Set Pi clock from laptop over SSH
├── rename_data_folders.sh      # One-time data migration helper
├── move_videos_out_of_pictures.sh
└── wipe_data.sh

docs/
├── USER-GUIDE.md            # For the people running experiments
├── calibration/             # Bend-counting calibration script + notes
└── history/                 # Archived snapshots — NOT current, do not trust
```

---

## Dev environment

- **Laptop**: Windows 11. Edits code locally — `picamera2` import errors are expected.
- **Pi IP**: `192.168.50.2` (static, direct ethernet, no router)
- **The Pi hands out addresses.** `eth0` runs NetworkManager's `shared` mode, so
  the Pi is a DHCP server on the direct cable: plug any machine in and it gets
  `192.168.50.11`–`.254` automatically. No static IP to configure per machine,
  and no administrator rights needed on the client — which is the whole point,
  because setting a static address on Windows requires admin and a new user
  usually does not have it. See [Pi network](#pi-network) below.
- **SSH alias**: `ssh celegans` (user `pi`, key-based auth — use this, never `celegans.local`)
- **Pi repo**: `/home/pi/celegans-imaging/`
- **Pi venv**: `/home/pi/celegans-imaging/.venv/` (created with `--system-site-packages`)
- **Deploy channel**: `scripts/deploy_local.sh` — copies the tree over SSH and
  restarts the service. **The Pi has no internet**, so the `git pull`-on-the-Pi
  path in `scripts/deploy.sh` only works if it is given one. A new Python
  dependency cannot be installed by a deploy; put it on the Pi by hand.

---

## Data layout (outside the repo, on the Pi)

```
/home/pi/celegans-data/
├── experiments/<session_id>/
│   ├── session.json          # Manifest (atomic writes via .tmp → os.replace)
│   └── plates/<condition_id>_<name>_plate<NN>/
├── pictures/<date>/          # Free still captures
├── videos/<date>/            # Free video captures
├── flatfield/
└── .trash/                   # Soft-deleted files
```

All data is filesystem-as-database — no SQL. `session.json` manifests are the source of truth.

---

## Running the service (Pi)

```bash
# Install
sudo cp deploy/celegans-capture.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now celegans-capture

# Logs
sudo journalctl -u celegans-capture -f
```

The web UI is served at `http://192.168.50.2:8000`. Authenticate with the bearer token from `capture/.env`.

> Note the service binds `0.0.0.0`, so it also answers on `wlan0` when the Pi is
> on Wi-Fi. Anyone on that network who has the token can reach it. There is one
> shared token for everyone; treat it accordingly.

<a name="pi-network"></a>
### Pi network

`eth0` is configured by the NetworkManager profile **`direct-laptop`**:

```
ipv4.method     shared
ipv4.addresses  192.168.50.2/24
ipv4.gateway    --
```

`shared` keeps the manual address *and* starts a DHCP server on that interface
(using the `dnsmasq` binary from `dnsmasq-base`, already present — the `dnsmasq`
package itself is not installed and is not needed).

Extra DHCP options live in `/etc/NetworkManager/dnsmasq-shared.d/celegans.conf`:

```
dhcp-option=3    # advertise NO default gateway
dhcp-option=6    # advertise NO DNS server
dhcp-range=192.168.50.10,192.168.50.50,255.255.255.0,12h
```

The first two matter. Without them, `shared` mode advertises the Pi as the
client's default route, and a Windows machine would try to reach the internet
down a cable that goes nowhere — breaking its normal networking. NetworkManager
overrides the range with its own `.11`–`.254`, which is harmless.

To reproduce on a fresh Pi:

```bash
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo tee /etc/NetworkManager/dnsmasq-shared.d/celegans.conf > /dev/null <<'EOF'
dhcp-option=3
dhcp-option=6
dhcp-range=192.168.50.10,192.168.50.50,255.255.255.0,12h
EOF
sudo nmcli connection modify direct-laptop ipv4.addresses 192.168.50.2/24 ipv4.method shared
sudo nmcli connection up direct-laptop
```

Set the address and the method in one command: `shared` on its own picks
`10.42.0.1/24` and every hardcoded reference to `192.168.50.2` breaks.

The laptop keeps its hand-set static `192.168.50.1` and ignores DHCP. There is
also an inactive `netplan-eth0` profile; `direct-laptop` wins on boot, which is
worth re-checking after any network change.

---

## Running the launcher (Windows)

| Path | Command |
|------|---------|
| Dev (Git Bash) | `source launcher/.venv/Scripts/activate && python launcher/main.py` |
| End-user | Install `WormScanSetup-<version>.exe` — see [INSTALL.md](launcher/INSTALL.md) |

`setup.bat` is a **developer convenience**: it builds `launcher/.venv` from
source and drops a desktop shortcut. It does not build the vision environment,
so a `setup.bat` install cannot run Development mode. End users get the
installer, which bundles a private Python, ffmpeg and both environments.

---

## Motility analysis pipeline

The launcher's **Open Analysis → Motility** flow:

1. Pick a folder of `.mp4` files (flat or condition-subfolders layout).
2. Pre-flight checks: a container engine running, Tierpsy image present, ffmpeg available.
3. Per video: probe fps (ffprobe) → convert to MJPEG AVI (ffmpeg) → run Tierpsy
   headless in a container → read `_featuresN.hdf5`. Results are cached per video
   and the cache is keyed on the parameters and the pipeline that produced it.
4. Compute bends-per-minute from head-swing-angle peaks: the signed angle between skeleton points 0/5 (head) and 20/30 (body), detrended, with peak prominence 0.50 rad (validated against manual counts).
5. Write `motility_results.xlsx` (one sheet per condition plus `_summary`),
   `motility_summary.csv`, per-video PNGs and `overview.png` to
   `<folder>/_analysis_<timestamp>/`.

Requires a container engine — **Docker, Podman or nerdctl** — with the Tierpsy
image present, plus `ffmpeg`/`ffprobe`. The installer bundles ffmpeg and ships a
shortcut that sets the engine up. Crawling uses the same machinery with a
different parameter set, a different linker and a different quality gate; it is
not a motility variant.

---

## Authentication

Shared bearer token. Pass as `X-Auth-Token` header or `?token=` query param. Only `GET /health` is unauthenticated. Token is configured in `capture/.env` (gitignored; see `.env.example`).

---

## Phase roadmap

| Phase | Status | Scope |
|-------|--------|-------|
| 1 | Done | FastAPI skeleton: config, auth, session/plate CRUD, health/status, static file serving, systemd unit |
| 2 | Done | Camera integration: still and video capture, live MJPEG preview |
| 3 | Done | Flat-field correction, video thumbnails, soft delete, retention daemon |
| 5a | Done | SHA256 manifest, ack endpoints, clock sync, AE shutter cap |
| 6 | Done | Windows launcher (CustomTkinter): sync agent plus four analysis pipelines — motility, crawling, colony survival, and Development (YOLO staging) — and the grid-viewer review tool |
| 7 | Done | Spatial calibration on the Pi: field-of-view calibration, ImageJ µm/px tags in every TIFF, per-session stamp |
| 8 | Done | Packaging: Windows installer, container-engine abstraction, AGPL licensing, pinned dependencies, update check |
| Next | — | Feed the Pi's µm/px into the Tierpsy parameter files (both still set `microns_per_pixel = -1.0`), so motility and crawling outputs become physical rather than pixels |

---

## Licence

WormScan is licensed under the **GNU Affero General Public License v3.0** —
see [`LICENSE`](LICENSE).

That choice is not arbitrary. The staging pipeline links `ultralytics`, which
is AGPL-3.0, and the installer ships it, so the combined work has to be offered
on the same terms. [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) lists
everything redistributed here and under what licence — including the bundled
ffmpeg, which is a **GPL** build because `render_video.py` encodes with
libx264.

Copyright (C) 2026 Erasmus MC, University Medical Center Rotterdam.

WormScan was developed at Erasmus MC. Under Dutch copyright law (Auteurswet
art. 7) copyright in work created by an employee in the course of their
employment vests in the employer, so Erasmus MC holds it. That is a statement
about who owns the code; it says nothing about authorship of the research —
see [`CITATION.cff`](CITATION.cff) for how to cite the software and who wrote
it.

Released as free and open-source software. Non-commercial in intent, though
note the AGPL does not itself restrict commercial use — it requires that source
remains available, including to users who interact with it over a network.
