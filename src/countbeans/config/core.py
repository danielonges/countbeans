from functools import cache

from pydantic import PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="COUNTBEANS_",
        # Tolerate unknown COUNTBEANS_* vars so a leftover entry in an existing
        # .env or deploy environment (e.g. the removed API_ID/API_HASH) doesn't
        # crash startup.
        extra="ignore",
    )

    bot_token: SecretStr
    database_url: PostgresDsn
    log_level: str = "INFO"


@cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
