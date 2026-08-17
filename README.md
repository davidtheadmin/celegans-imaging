# WormScan — C. elegans Imaging Station

Automated imaging and motility analysis system for *C. elegans* assays, built on a Raspberry Pi 5. Captures whole-plate timelapse and still images, syncs data to a Windows laptop, and runs headless motility analysis via Tierpsy Tracker in Docker.

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
capture/                     # FastAPI service (runs on the Pi)
├── app/
│   ├── main.py              # App factory, route mounting, static files
│   ├── config.py            # pydantic-settings, reads from .env
│   ├── auth.py              # Bearer token dependency
│   ├── models.py            # Session, Plate, request/response models
│   ├── sessions.py          # Filesystem-backed session CRUD
│   ├── camera.py            # picamera2 camera manager
│   ├── capture_ops.py       # Still and video capture logic
│   ├── focus.py             # Laplacian focus scoring
│   ├── routers/             # Route modules: camera_ctrl, free_capture,
│   │                        #   manifest, plate_capture, preview, system
│   └── static/index.html    # Dark instrument-style web UI (no framework)
├── capture.py               # Standalone full-res capture script (do not modify)
├── retention.py             # Data retention daemon (removes acked files)
├── requirements.txt
└── .env.example

launcher/                    # Windows desktop app (Tkinter)
├── main.py                  # Entry point
├── config.py                # Settings dataclass, APPDATA persistence
├── sync.py                  # Background sync thread (Pi → laptop mirror)
├── ui.py                    # Tkinter main window + settings dialog
├── analysis/                # Motility analysis pipeline
│   ├── motility.py          # MotilityAgent + MotilityStatus
│   ├── ffmpeg_utils.py      # convert_to_avi(), probe_fps()
│   ├── docker_utils.py      # run_tierpsy(), Docker pre-flight checks
│   ├── analysis_csv.py      # bends_per_minute(), CSV builder
│   └── plots.py             # Per-video PNGs and overview box plot
├── motility_params.json     # Frozen Tierpsy parameter set
├── tools/                   # Dev/diagnostic utilities
│   ├── tierpsy_param_sweep.py
│   ├── inspect_skeleton_failures.py
│   └── cut_clip.py
├── requirements.txt         # requests + scientific stack (pandas, numpy, scipy, scikit-image, opencv, h5py, tables, matplotlib, openpyxl, imageio-ffmpeg, tifffile, imagecodecs, customtkinter)
├── setup.bat                # One-time installer for non-technical users
└── INSTALL.md               # End-user installation guide

deploy/
├── celegans-capture.service    # systemd unit for the FastAPI service
├── celegans-retention.service  # systemd unit for the retention daemon
└── celegans-retention.timer    # systemd timer (daily retention run)

scripts/
├── deploy.sh                   # Push → pull → restart helper
├── sync-pi-clock.sh            # Set Pi clock from laptop over SSH
├── rename_data_folders.sh      # One-time data migration helper
├── move_videos_out_of_pictures.sh
└── wipe_data.sh

docs/calibration/            # Archived bend-counting calibration receipts
```

---

## Dev environment

- **Laptop**: Windows 11. Edits code locally — `picamera2` import errors are expected.
- **Pi IP**: `192.168.50.2` (static, direct ethernet, no router)
- **SSH alias**: `ssh celegans` (user `pi`, key-based auth — use this, never `celegans.local`)
- **Pi repo**: `/home/pi/celegans-imaging/`
- **Pi venv**: `/home/pi/celegans-imaging/.venv/` (created with `--system-site-packages`)
- **Deploy channel**: commit → push to GitHub → `ssh celegans "cd celegans-imaging && git pull"`. No rsync or scp.

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

---

## Running the launcher (Windows)

| Path | Command |
|------|---------|
| Dev (Git Bash) | `source launcher/.venv/Scripts/activate && python launcher/main.py` |
| End-user | Double-click the WormScan desktop shortcut created by `setup.bat` |

`setup.bat` is additive and idempotent; no admin rights required.

---

## Motility analysis pipeline

The launcher's **Open Analysis → Motility** flow:

1. Pick a folder of `.mp4` files (flat or condition-subfolders layout).
2. Pre-flight checks: Docker running, Tierpsy image pulled, ffmpeg available.
3. Per video: probe fps (ffprobe) → convert to MJPEG AVI (ffmpeg) → run Tierpsy headless in Docker → read `_featuresN.hdf5`.
4. Compute bends-per-minute from head-swing-angle peaks: the signed angle between skeleton points 0/5 (head) and 20/30 (body), detrended, with peak prominence 0.50 rad (validated against manual counts).
5. Write `motility_results.csv`, `motility_summary.csv`, per-video PNGs, and `overview.png` to `<folder>/_analysis/<timestamp>/`.

Requires: Docker Desktop with `tierpsy/tierpsy-tracker` image pulled, and `ffmpeg`/`ffprobe` in PATH.

---

## Authentication

Shared bearer token. Pass as `X-Auth-Token` header or `?token=` query param. Only `GET /health` is unauthenticated. Token is configured in `capture/.env` (gitignored; see `.env.example`).

---

## Phase roadmap

| Phase | Status | Scope |
|-------|--------|-------|
| 1 | Done | FastAPI skeleton: config, auth, session/plate CRUD, health/status, static file serving, systemd unit |
| 2 | Done | Camera integration: still capture, timelapse, live MJPEG preview |
| 3 | Done | Flat-field correction, video thumbnails, soft delete, retention daemon |
| 5a | Done | SHA256 manifest, ack endpoints, clock sync, AE shutter cap |
| 6 | Done | Windows launcher (CustomTkinter): sync agent plus motility, crawling, and counting (colony-survival) analysis pipelines |
| Next | — | `microns_per_pixel` calibration to unlock real-unit speed/length (analysis outputs are currently in pixels) |

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

Copyright (C) 2026 David Haeckes.
> If this work is owned by your institution rather than by you personally,
> replace that line with the correct holder before distributing.
