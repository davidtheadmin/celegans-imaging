# WormScan — C. elegans Imaging Station

Automated imaging and motility analysis system for *C. elegans* assays, built on a Raspberry Pi 5. Captures whole-plate timelapse and still images, syncs data to a Windows laptop, and runs headless motility analysis via Tierpsy Tracker in Docker.

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
- **The Pi hands out addresses.** `eth0` runs NetworkManager's `shared` mode, so
  the Pi is a DHCP server on the direct cable: plug any machine in and it gets
  `192.168.50.11`–`.254` automatically. No static IP to configure per machine,
  and no administrator rights needed on the client — which is the whole point,
  because setting a static address on Windows requires admin and a new user
  usually does not have it. See [Pi network](#pi-network) below.
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

Copyright (C) 2026 The WormScan authors.

> **The copyright holder is not yet settled, and it matters.** Under Dutch
> copyright law (Auteurswet art. 7) copyright in work created by an employee in
> the course of their employment vests in the **employer** by default, and PhD
> candidates at Dutch universities are normally employees. If the institution
> holds the copyright then only the institution can choose the licence - so the
> AGPL-3.0 release above needs their agreement, not just the maintainer's.
>
> "The WormScan authors" is used deliberately in the meantime: it asserts
> nothing false. Two questions to settle with whoever handles research software
> or IP at the institution:
>
> 1. May this be released under AGPL-3.0? (Note that `ultralytics`, which the
>    staging pipeline links, is AGPL-3.0, so a permissive licence is not
>    available while it ships - see
>    [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).)
> 2. Whose name belongs on the notice - and the exact legal entity name?
>
> Also worth confirming that every contributor agrees, since each holds rights
> in their own contribution.
