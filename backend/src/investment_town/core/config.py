import os
from dataclasses import dataclass
from pathlib import Path


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
    database_path: str = os.getenv(
        "DATABASE_PATH",
        str(Path(__file__).resolve().parents[3] / "data" / "investment-town.db"),
    )
    control_api_token: str | None = os.getenv("CONTROL_API_TOKEN") or None


settings = Settings()
