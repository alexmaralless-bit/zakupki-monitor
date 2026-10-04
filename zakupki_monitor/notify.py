"""Telegram-сводка: новые закупки с рейтингом не ниже порога и срочные (до подачи < 3 дней)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .appsettings import RuntimeSettings
from .models import Profile, Score, Tender, utcnow

URGENT_DAYS = 3
ACTIVE_STATUSES = ("new", "interesting")


def _line(tender: Tender, score: Score, today: date) -> str:
    price = f"{tender.initial_price:,.0f}".replace(",", " ") + " ₽" if tender.initial_price else "-"
    left = ""
    if tender.deadline_at:
        days = (tender.deadline_at - today).days
        left = f", подача до {tender.deadline_at:%d.%m.%Y} (осталось {days} дн.)"
    return (
        f"{score.rating:.0f} | {tender.object[:90]}\n"
        f"   {tender.customer_name or '-'}, {price}{left}\n   {tender.url or tender.reg_number}"
    )


def build_digest(
    session: Session,
    rt: RuntimeSettings,
    today: date | None = None,
    now: datetime | None = None,
) -> str | None:
    """Текст сводки или None, если сообщать не о чем."""
    today = today or date.today()
    now = now or utcnow()
    since = now - timedelta(days=1)
    rows = session.execute(
        select(Tender, Score, Profile)
        .join(Score, Score.tender_id == Tender.id)
        .join(Profile, Profile.id == Score.profile_id)
        .where(Score.filtered.is_(False), Tender.status.in_(ACTIVE_STATUSES))
        .order_by(Score.rating.desc())
    ).all()
    fresh: list[str] = []
    urgent: list[str] = []
    for tender, score, profile in rows:
        rating = score.rating or 0.0
        days = (tender.deadline_at - today).days if tender.deadline_at else None
        is_urgent = days is not None and 0 <= days < URGENT_DAYS
        if is_urgent:
            urgent.append(f"[{profile.name}] " + _line(tender, score, today))
        elif tender.first_seen_at >= since and rating >= rt.telegram_threshold:
            fresh.append(f"[{profile.name}] " + _line(tender, score, today))
    if not fresh and not urgent:
        return None
    parts = [f"Монитор госзакупок, сводка на {today:%d.%m.%Y}"]
    if urgent:
        parts.append(
            f"Срочно (подача менее {URGENT_DAYS} дней): {len(urgent)}\n" + "\n".join(urgent)
        )
    if fresh:
        parts.append(
            f"Новые с рейтингом от {rt.telegram_threshold:.0f}: {len(fresh)}\n" + "\n".join(fresh)
        )
    return "\n\n".join(parts)[:4000]


class TelegramError(Exception):
    pass


def send_telegram(
    rt: RuntimeSettings,
    text: str,
    client: httpx.Client | None = None,
    proxy: str | None = None,
) -> None:
    if not (rt.telegram_token and rt.telegram_chat_id):
        raise TelegramError("Не заданы токен бота и chat_id")
    own = client is None
    http = client or httpx.Client(timeout=20.0, trust_env=False, proxy=proxy or None)
    try:
        resp = http.post(
            f"https://api.telegram.org/bot{rt.telegram_token}/sendMessage",
            json={"chat_id": rt.telegram_chat_id, "text": text, "disable_web_page_preview": True},
        )
        if not resp.is_success:
            raise TelegramError(f"Telegram ответил HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        raise TelegramError(
            f"{type(exc).__name__}: {exc}".replace(rt.telegram_token, "***")
        ) from exc
    finally:
        if own:
            http.close()


def send_digest(
    session: Session,
    rt: RuntimeSettings,
    client: httpx.Client | None = None,
    today: date | None = None,
    proxy: str | None = None,
) -> bool:
    """Отправляет сводку, если она не пуста; True, если сообщение ушло."""
    text = build_digest(session, rt, today=today)
    if text is None:
        return False
    send_telegram(rt, text, client, proxy)
    return True
