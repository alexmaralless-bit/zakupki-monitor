"""Модели БД (SQLAlchemy 2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Money(TypeDecorator[Decimal]):
    """Сумма в рублях: хранится целым числом копеек (точно и сортируется)."""

    impl = BigInteger
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> int | None:
        if value is None:
            return None
        return int((value * 100).to_integral_value())

    def process_result_value(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return (Decimal(value) / 100).quantize(Decimal("0.01"))


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    search: Mapped[dict[str, Any]]  # SearchParams
    filter: Mapped[dict[str, Any]]  # FilterConfig
    max_pages: Mapped[int] = mapped_column(Integer, default=5)
    ai_criterion: Mapped[str | None] = mapped_column(Text, default=None)
    ai_weight: Mapped[float] = mapped_column(Float, default=0.5)
    interval_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    rules: Mapped[list[Rule]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", order_by="Rule.id"
    )


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(500))
    weight: Mapped[float] = mapped_column(Float)

    profile: Mapped[Profile] = relationship(back_populates="rules")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    found: Mapped[int] = mapped_column(Integer, default=0)
    new: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    filtered: Mapped[int] = mapped_column(Integer, default=0)
    cards: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, default=None)


class Tender(Base):
    __tablename__ = "tenders"

    id: Mapped[int] = mapped_column(primary_key=True)
    reg_number: Mapped[str] = mapped_column(String(32), unique=True)
    law: Mapped[int | None] = mapped_column(Integer)
    method: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(String(200))
    object: Mapped[str] = mapped_column(Text, default="")
    customer_name: Mapped[str | None] = mapped_column(Text)
    customer_inn: Mapped[str | None] = mapped_column(String(12))
    initial_price: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(8))
    published_at: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[date | None] = mapped_column(Date)
    deadline_at: Mapped[date | None] = mapped_column(Date)
    deadline_time: Mapped[time | None] = mapped_column(Time)
    url: Mapped[str | None] = mapped_column(Text)
    # из карточки
    okpd2: Mapped[list[str] | None]
    bid_security: Mapped[str | None] = mapped_column(String(200))
    contract_security: Mapped[str | None] = mapped_column(String(200))
    delivery_place: Mapped[str | None] = mapped_column(Text)
    card_fetched_at: Mapped[datetime | None] = mapped_column(DateTime)
    # служебное
    status: Mapped[str] = mapped_column(String(20), default="new")
    note: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    events: Mapped[list[TenderEvent]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", order_by="TenderEvent.id"
    )
    scores: Mapped[list[Score]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )
    exports: Mapped[list[CrmExport]] = relationship(
        back_populates="tender", cascade="all, delete-orphan", order_by="CrmExport.id.desc()"
    )


class TenderEvent(Base):
    __tablename__ = "tender_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"))
    field: Mapped[str] = mapped_column(String(50))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="events")


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (UniqueConstraint("tender_id", "profile_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"))
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    filtered: Mapped[bool] = mapped_column(Boolean, default=False)
    filter_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    rules_score: Mapped[float | None] = mapped_column(Float)
    ai_score: Mapped[float | None] = mapped_column(Float)
    rating: Mapped[float | None] = mapped_column(Float)
    breakdown: Mapped[dict[str, Any] | None]
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="scores")


class AICache(Base):
    """Ответы ИИ: ключ (номер закупки, хеш критерия, хеш текста)."""

    __tablename__ = "ai_cache"
    __table_args__ = (UniqueConstraint("reg_number", "criterion_hash", "text_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reg_number: Mapped[str] = mapped_column(String(32), index=True)
    criterion_hash: Mapped[str] = mapped_column(String(64))
    text_hash: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(50))
    response: Mapped[dict[str, Any]]  # проверенный JSON {score, verdict, reasons, quotes}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CrmExport(Base):
    __tablename__ = "crm_exports"

    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, Any]]
    response: Mapped[dict[str, Any] | None]
    http_status: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="exports")


class AppSetting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
