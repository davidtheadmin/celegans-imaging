import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DATA: Path = Path(os.environ.get("APPDATA", Path.home())) / "WormScan"
_CONFIG_FILE = APP_DATA / "config.json"
_LOG_FILE = APP_DATA / "launcher.log"
_DEFAULT_MIRROR = (
    Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "WormScan"
)


@dataclass
class Settings:
    pi_url: str = "http://192.168.50.2:8000"
    token: str = ""
    mirror_root: str = str(_DEFAULT_MIRROR)
    poll_interval_s: int = 120
    # Analysis
    tierpsy_image: str = "tierpsy/tierpsy-tracker"
    tierpsy_image_tag: str = "latest"
    docker_command: str = "docker"
    analysis_video_timeout_s: int = 600
    motility_long_threshold_s: float = 5.0
    crawling_min_track_s: int = 30
    counting_split_sensitivity: float = 3.0
    counting_min_colony_um: float = 200.0
    # Worm survival: per-class staging confidence, {stage_name: floor}. Empty
    # means "use launcher/vision/stage_conf.json", the same file infer_stage.py
    # falls back to, so an untouched install and the capture UI's "Analyze on
    # laptop" button run identical thresholds. The analysis dialog fills this in
    # from that file the first time it opens, and its "Reset to defaults" button
    # puts it back. survival_conf below is the pre-per-class single slider; it
    # is no longer read, kept only so an old config.json still loads cleanly.
    survival_class_conf: dict = field(default_factory=dict)
    # Count egg detections? Default False: a plate is almost never a question
    # about worms AND eggs at once, and eggs are already outside the survival
    # denominator, so leaving them off changes clutter and the egg column, never
    # the survival percentage. Tick it for an egg-survival assay.
    survival_count_eggs: bool = False
    survival_conf: float = 0.25   # legacy, unused
    # Review (grid viewer) — last-used content type and video loop length.
    review_type: str = "auto"
    review_loop_s: float = 3.0
    # Number of videos to analyse concurrently. "auto" derives it from
    # docker info (see analysis/concurrency.py); an int overrides.
    concurrent_videos: str = "auto"


_FIELD_NAMES = {f.name for f in fields(Settings)}


def load() -> Settings:
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return Settings(**{k: v for k, v in data.items() if k in _FIELD_NAMES})
        except Exception:
            pass
    return Settings()


def save(settings: Settings) -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(
        json.dumps(asdict(settings), indent=2), encoding="utf-8"
    )


def setup_logging() -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
