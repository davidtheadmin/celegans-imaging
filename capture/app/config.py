from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CELEGANS_")

    TOKEN: str
    DATA_ROOT: str = "/home/pi/celegans-data"
    EXPERIMENTS_DIR: str = "experiments"   # on-disk folder name for session data
    PICTURES_DIR: str = "pictures"         # on-disk folder name for free stills
    VIDEOS_DIR: str = "videos"             # on-disk folder name for free videos
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    MAX_AUTO_SHUTTER_US: int = 500_000  # AE shutter cap — prefer darker image over multi-second freeze
    CAPTURE_MIN_FREE_GB: float = 2.0    # capture refuses (HTTP 507) below this after reclamation
    # Recycle-bin lifetime. retention.py reads CELEGANS_RETENTION_TRASH_MAX_AGE_DAYS
    # from env directly; carried here so it is validated and visible in one place.
    RETENTION_TRASH_MAX_AGE_DAYS: float = 7.0


settings = Settings()
