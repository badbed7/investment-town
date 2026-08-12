import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    paper_trading_only: bool = _as_bool(os.getenv("PAPER_TRADING_ONLY"), True)
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://investment:investment@localhost:5432/investment_town",
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")


settings = Settings()
