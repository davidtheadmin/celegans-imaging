# C. elegans Imaging Station

Raspberry Pi 5–based automated imaging system for C. elegans assays (motility and survival scoring).

## Hardware

- **Camera**: Raspberry Pi Camera Module 3 (IMX708), attached via CSI ribbon cable
- **Illumination**: Custom LED transilluminator (bottom light), controlled via GPIO
- **Compute**: Raspberry Pi 5 (8 GB RAM)
- **Optics**: Fixed magnification macro lens for whole-plate imaging

## Dev environment

- **Laptop**: Windows 11, edits code locally. Dependencies like `picamera2` only exist on the Pi — import errors locally are expected.
- **Pi IP**: `192.168.50.2` (static, direct ethernet cable, no router)
- **Laptop IP**: `192.168.50.1`
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

deploy/
└── celegans-capture.service   # systemd unit

scripts/
└── deploy.sh             # push → pull → restart helper

capture.py                # standalone full-res capture script (do not modify)
```

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

| Phase | Scope |
|-------|-------|
| 1 | FastAPI skeleton: config, auth, session/plate CRUD, health/status, static file serving, systemd unit |
| 2 | Camera integration: `capture.py` imported by the service, single-frame and timelapse endpoints |
| 3 | Flat-field correction pipeline, exposure calibration endpoint |
| 4 | Analysis service (`analysis/`): motility scoring, survival scoring |
| 5 | Web UI for plate management, live preview, result visualisation |
