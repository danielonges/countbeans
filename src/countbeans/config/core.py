from functools import cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="COUNTBEANS_",
    )

    api_id: int
    api_hash: str
    bot_token: str
    database_url: str
    log_level: str = "INFO"
    log_format: str = "text"


@cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
