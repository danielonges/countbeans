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

    # Connection-pool resilience for the always-on long-polling process. Pooled
    # connections go stale across idle periods (PG restart/failover, NAT/firewall
    # idle timeouts, managed poolers dropping idle server-side connections); the
    # next query then fails unless we guard against it.
    #   pre_ping: lightweight liveness check on checkout, transparently replacing
    #     a dead connection instead of erroring the user's command.
    #   recycle_seconds: proactively retire connections older than this (-1 to
    #     disable). Belt-and-suspenders, useful behind connection poolers.
    db_pool_pre_ping: bool = True
    db_pool_recycle_seconds: int = 1800


@cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
