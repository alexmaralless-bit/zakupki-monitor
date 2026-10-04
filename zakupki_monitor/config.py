"""Настройки приложения: переменные окружения ZM_* и файл .env в корне проекта."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"
SITE_ROOT = "https://zakupki.gov.ru"
USER_AGENT = "zakupki-monitor/0.1 (+demo)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ZM_", env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    db_path: Path = Path("data/zakupki.db")
    ca_bundle: Path = Path("certs/ru-ca.pem")
    pause_min: float = Field(2.0, ge=0)
    pause_max: float = Field(4.0, ge=0)
    max_retries: int = Field(3, ge=1)
    backoff_base: float = Field(2.0, ge=0)
    timeout: float = 30.0
    default_max_pages: int = Field(5, ge=1)
    user_agent: str = USER_AGENT

    def resolved_db_path(self) -> Path:
        return self.db_path if self.db_path.is_absolute() else PROJECT_ROOT / self.db_path

    def resolved_ca_bundle(self) -> Path:
        return self.ca_bundle if self.ca_bundle.is_absolute() else PROJECT_ROOT / self.ca_bundle

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.resolved_db_path()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
