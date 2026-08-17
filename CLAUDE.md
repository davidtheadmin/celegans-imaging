# C. elegans Imaging Station

Raspberry Pi 5–based automated imaging system for C. elegans assays (motility and survival scoring).

## Hardware

- **Camera**: Sony IMX477 HQ Camera (12.3 MP, 4056×3040 full array), attached via CSI ribbon cable
- **Illumination**: Custom LED transilluminator (bottom light), controlled via GPIO
- **Compute**: Raspberry Pi 5 (8 GB RAM)
- **Optics**: Fixed magnification macro lens for whole-plate imaging

## Dev environment

- **Laptop**: Windows 11, edits code locally. Dependencies like `picamera2` only exist on the Pi — import errors locally are expected.
- **Pi IP**: `192.168.50.2` (static, direct ethernet cable, no router)
- **Laptop IP**: `192.168.50.1` (hand-set static; ignores DHCP)
- **Other machines get an address automatically.** `eth0` uses NetworkManager
  `ipv4.method shared`, so the Pi is a DHCP server on the direct cable
  (`192.168.50.11`–`.254`). Options in
  `/etc/NetworkManager/dnsmasq-shared.d/celegans.conf` suppress the gateway and
  DNS advertisements, so a client does not try to route the internet down a
  cable that goes nowhere. Do NOT set `ipv4.method shared` without also setting
  `ipv4.addresses 192.168.50.2/24` in the same command — alone it picks
  `10.42.0.1/24` and breaks every hardcoded address. Details in `README.md`.
- **SSH alias**: `ssh celegans` connects as user `pi` with key-based auth. Always use this alias — never `celegans.local`.
- **Pi user/home**: `pi` / `/home/pi/`
- **Repo on Pi**: `/home/pi/celegans-imaging/`
- **Venv**: `/home/pi/celegans-imaging/.venv/` (created with `--system-site-packages` so it can see system `picamera2`)
- **Deployment channel**: All code goes through Git. Commit → push to GitHub → `ssh celegans "cd celegans-imaging && git pull"`. No rsync or scp.
- **Pi internet**: intermittent (phone hotspot). If `git pull` or `pip install` fails with DNS errors, enable the hotspot.

## Repository layout

```
capture/                  # FastAPI service (Phase 1+)
├── .env.example          # committed config template
├── requirements.txt      # pinned to Pi venv versions
└── app/
    ├── main.py           # app factory, route mounting, static files
    ├── config.py         # pydantic-settings, reads from .env
    ├── auth.py           # bearer token dependency
    ├── models.py         # Session, Plate, request models
    ├── sessions.py       # filesystem-backed CRUD
    └── static/
        └── index.html

launcher/                 # Windows sync/launcher app (Phase 6)
├── main.py               # entry point
├── config.py             # settings dataclass, APPDATA persistence
├── sync.py               # background sync thread
├── ui.py                 # Tkinter main window + settings dialog
├── requirements.txt      # requests + scientific stack (pandas, numpy, scipy, scikit-image, opencv, h5py, tables, matplotlib, openpyxl, imageio-ffmpeg, tifffile, imagecodecs, customtkinter)
├── setup.bat             # one-time installer for non-technical users
├── INSTALL.md            # end-user installation guide
└── assets/
    └── wormscan.ico      # placeholder app icon (replace later)

deploy/
└── celegans-capture.service   # systemd unit

scripts/
└── deploy.sh             # push → pull → restart helper

capture.py                # standalone full-res capture script (do not modify)
```

## Launcher — both launch paths are supported

| Path | Command |
|------|---------|
| Dev (Git Bash, manual venv) | `source launcher/.venv/Scripts/activate && python launcher/main.py` |
| End-user (desktop shortcut) | Double-click the WormScan icon created by `setup.bat` |

`setup.bat` is additive and idempotent. Admin rights are not required — the venv lives
inside the repo folder and the shortcut targets the current user's Desktop.

## Data layout (outside the repo)

All data lives at `/home/pi/celegans-data/` — never inside the repo.

```
/home/pi/celegans-data/
├── sessions/
│   └── <session_id>/
│       ├── session.json          # manifest (see schema below)
│       └── plates/
│           └── <condition_id>_<name>_plate<NN>/   # image frames go here
├── freecapture/
├── flatfield/
└── .trash/
```

## Files-as-contract philosophy

No database. Folder structure and `session.json` manifests are the source of truth. All experimental metadata is encoded in the filesystem tree. Manifests are written atomically (write to `.tmp`, then `os.replace`) so a crash mid-write cannot corrupt a file.

## session.json schema (schema_version: 1)

```json
{
  "schema_version": 1,
  "id": "YYYYMMDDTHHMMSS_<6charhash>",
  "name": "string",
  "assay_mode": "motility | survival",
  "assay_config": {},
  "created_at": "<ISO 8601>",
  "plates": [
    {
      "id": "string",
      "condition_id": "string",
      "name": "string",
      "plate_number": 1,
      "folder_name": "<condition_id>_<name>_plate<NN>",
      "created_at": "<ISO 8601>"
    }
  ]
}
```

`assay_config` is a permissive dict — contents vary by assay type and are not validated by the API.

## API authentication

Shared bearer token. Pass as `X-Auth-Token` header or `?token=` query parameter. Compare via `secrets.compare_digest`. Only `/health` is unauthenticated.

Config is loaded from `capture/.env` (gitignored). See `capture/.env.example` for the template.

## Systemd service

```
Unit file:  deploy/celegans-capture.service
Install:    sudo cp deploy/celegans-capture.service /etc/systemd/system/
            sudo systemctl daemon-reload
            sudo systemctl enable --now celegans-capture
Logs:       sudo journalctl -u celegans-capture -f
```

## Phase roadmap

Status reflects live code. Note the analysis pipelines run in the Windows
launcher (`launcher/analysis/`), not as a Pi-side `analysis/` service as
originally sketched.

| Phase | Status | Scope |
|-------|--------|-------|
| 1 | Done | FastAPI skeleton: config, auth, session/plate CRUD, health/status, static file serving, systemd unit |
| 2 | Done | Camera integration: `capture.py` imported by the service, single-frame and timelapse endpoints |
| 3 | Done | Flat-field correction pipeline, exposure calibration endpoint |
| 4 | Done | Windows launcher + background sync agent (manifest polling, download, ack, local mirror); Pi-side SHA256 sidecars, manifest/ack endpoints, retention daemon + systemd timer |
| 5 | Done | Web UIs: capture-service plate management / live preview; launcher desktop UI (CustomTkinter) |
| Analysis | Done (in launcher) | Motility, crawling, and counting (colony-survival) pipelines in `launcher/analysis/` — motility & crawling headless via Tierpsy/Docker, counting via classical CV |
