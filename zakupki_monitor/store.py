"""Сохранение закупок: дедупликация по реестровому номеру и журнал изменений."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Tender, TenderEvent, utcnow
from .parser import SearchEntry

TRACKED_FIELDS = ("initial_price", "deadline_at", "stage", "updated_at")
PLAIN_FIELDS = (
    "law",
    "method",
    "object",
    "customer_name",
    "currency",
    "published_at",
    "url",
)

Outcome = Literal["new", "updated", "unchanged"]


@dataclass
class UpsertResult:
    tender: Tender
    outcome: Outcome
    changed_fields: list[str]


def _fmt(value: Any) -> str | None:
    return None if value is None else str(value)


def upsert_tender(session: Session, entry: SearchEntry) -> UpsertResult:
    tender = session.scalar(select(Tender).where(Tender.reg_number == entry.reg_number))
    now = utcnow()
    if tender is None:
        tender = Tender(reg_number=entry.reg_number, first_seen_at=now, last_seen_at=now)
        for name in (*PLAIN_FIELDS, *TRACKED_FIELDS):
            setattr(tender, name, getattr(entry, name))
        tender.customer_inn = entry.customer_inn
        session.add(tender)
        session.flush()
        return UpsertResult(tender, "new", [])

    changed: list[str] = []
    for name in TRACKED_FIELDS:
        old, new = getattr(tender, name), getattr(entry, name)
        if old != new:
            changed.append(name)
            session.add(
                TenderEvent(tender=tender, field=name, old_value=_fmt(old), new_value=_fmt(new))
            )
            setattr(tender, name, new)
    for name in PLAIN_FIELDS:
        setattr(tender, name, getattr(entry, name))
    if entry.customer_inn and not tender.customer_inn:
        tender.customer_inn = entry.customer_inn
    tender.last_seen_at = now
    session.flush()
    return UpsertResult(tender, "updated" if changed else "unchanged", changed)
