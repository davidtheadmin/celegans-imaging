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
    # Colony Survival detection: 0-10 dial on the automatic threshold (5 =
    # unchanged, higher finds fainter colonies), and the blur applied to the
    # detection map before thresholding so feathery colonies count as one
    # object instead of many fragments (0 = off). Both default to the
    # pre-slider behaviour, so an existing config.json counts as it always did.
    counting_sensitivity: float = 5.0
    counting_smooth_um: float = 0.0
    # Threshold mode. "otsu" derives the cut from each plate separately, which
    # makes every plate its own reference — fine for reading one image, wrong
    # for a dose-response. "fixed" applies counting_od_threshold to every plate
    # in the run, so counts are comparable across conditions. Default stays
    # "otsu" so existing configs are unchanged.
    counting_threshold_mode: str = "otsu"
    counting_od_threshold: float = 0.05
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
    # Per-class score rescoring ("Correct for uneven class confidence").
    # ON by default. The class heads are not on a common scale — the L2 head's
    # 99th-percentile score is 0.14 while L1/L3 reach 0.80 — so an arg-max over
    # raw scores compares numbers that do not compare, and L2 loses nearly every
    # contest it enters. The pass runs last, after every suppression step, and
    # RELABELS ONLY: box count and geometry are unchanged at any alpha.
    #
    # This flag is a switch, not a value. Ticked passes no alpha at all, so
    # launcher/vision/stage_conf.json's rescore.alpha applies (ships at 2.0);
    # unticked forces alpha 0, which is a bit-identical no-op. The number lives
    # in that file and nowhere else, so it stays tunable without a rebuild.
    survival_rescore: bool = True
    # Ignore previous results and send every image through the model again?
    # Off by default: a Development run reuses detections from earlier runs of
    # the same folder, image by image, which is what makes "analyse each
    # timepoint as it comes in, then combine them at the end" cheap. The cache
    # invalidates itself whenever anything that decides which BOXES exist
    # changes (per-class floors, size gate, tiling/merge, the model file), so
    # this switch is an escape hatch rather than a routine setting.
    survival_force_reanalyze: bool = False
    # Legacy, no longer read. soft_stage_scores.csv is written on every
    # Development run — the body-size figure is built from it, so it stopped
    # being optional. Kept only so an old config.json still loads cleanly.
    survival_soft_scores: bool = False
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
