from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    app_name: str = "Bulk Certificate Generator"
    environment: str = "development"
    debug: bool = False
    database_url: str = "sqlite:///./bulk_certificates.db"
    sqlite_busy_timeout_seconds: float = 10.0
    storage_dir: str = "storage/certificates"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
