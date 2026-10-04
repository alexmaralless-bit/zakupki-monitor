"""Настройки, которые меняются из веб-интерфейса, хранятся в таблице settings.

Ключи API к ИИ сюда не попадают: они только в .env.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .config import Settings
from .models import AppSetting


class RuntimeSettings(BaseModel):
    ai_provider: str = "yandex"
    webhook_url: str = ""
    webhook_header: str = "X-Webhook-Secret"
    webhook_secret: str = ""
    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_threshold: float = Field(70, ge=0, le=100)
    telegram_hour: int = Field(9, ge=0, le=23)


def load_runtime(session: Session, settings: Settings) -> RuntimeSettings:
    rows = {r.key: r.value for r in session.query(AppSetting).all()}
    data: dict[str, object] = {"ai_provider": settings.ai_provider}
    for name, field in RuntimeSettings.model_fields.items():
        if name in rows:
            data[name] = rows[name] == "1" if field.annotation is bool else rows[name]
    return RuntimeSettings.model_validate(data)


def save_runtime(session: Session, values: RuntimeSettings) -> None:
    for name, value in values.model_dump().items():
        text = ("1" if value else "0") if isinstance(value, bool) else str(value)
        row = session.get(AppSetting, name)
        if row is None:
            session.add(AppSetting(key=name, value=text))
        else:
            row.value = text
    session.flush()
