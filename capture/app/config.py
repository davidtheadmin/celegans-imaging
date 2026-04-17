from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CELEGANS_")

    TOKEN: str
    DATA_ROOT: str = "/home/pi/celegans-data"
    HOST: str = "0.0.0.0"
    PORT: int = 8000


settings = Settings()
