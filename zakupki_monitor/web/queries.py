"""Выборка реестра: фильтры, сортировка, поиск; общая для страницы и выгрузки в Excel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from ..models import Profile, Score, Tender
from ..notify import URGENT_DAYS

STATUSES: dict[str, str] = {
    "new": "Новая",
    "interesting": "Интересная",
    "rejected": "Отклонена",
    "sent_to_crm": "В CRM",
}
SORTS = ("rating", "price", "deadline", "updated", "object")
PAGE_SIZE = 50


@dataclass
class RegistryFilter:
    profile: int | None = None
    status: str = ""
    law: int | None = None
    q: str = ""
    min_rating: float | None = None
    urgent: bool = False
    show_filtered: bool = False
    sort: str = "rating"
    dir: str = "desc"
    page: int = 1

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> RegistryFilter:
        def num(key: str, cast: type[int] | type[float]) -> Any:
            try:
                return cast(str(params[key]).replace(",", "."))
            except (KeyError, ValueError):
                return None

        sort = str(params.get("sort") or "rating")
        status = str(params.get("status") or "")
        return cls(
            profile=num("profile", int),
            status=status if status in STATUSES else "",
            law=num("law", int),
            q=str(params.get("q") or "").strip(),
            min_rating=num("min_rating", float),
            urgent=bool(params.get("urgent")),
            show_filtered=bool(params.get("show_filtered")),
            sort=sort if sort in SORTS else "rating",
            dir="asc" if params.get("dir") == "asc" else "desc",
            page=max(1, num("page", int) or 1),
        )

    def query_string(self, **override: Any) -> str:
        from urllib.parse import urlencode

        data: dict[str, Any] = {
            "profile": self.profile,
            "status": self.status,
            "law": self.law,
            "q": self.q,
            "min_rating": self.min_rating,
            "urgent": "1" if self.urgent else "",
            "show_filtered": "1" if self.show_filtered else "",
            "sort": self.sort,
            "dir": self.dir,
            "page": self.page,
            **override,
        }
        return urlencode({k: v for k, v in data.items() if v not in (None, "")})


def registry_stmt(f: RegistryFilter, today: date) -> Select[Tender, Score, Profile]:
    stmt = (
        select(Tender, Score, Profile)
        .join(Score, Score.tender_id == Tender.id)
        .join(Profile, Profile.id == Score.profile_id)
    )
    if not f.show_filtered:
        stmt = stmt.where(Score.filtered.is_(False))
    if f.profile:
        stmt = stmt.where(Score.profile_id == f.profile)
    if f.status:
        stmt = stmt.where(Tender.status == f.status)
    if f.law:
        stmt = stmt.where(Tender.law == f.law)
    if f.min_rating is not None:
        stmt = stmt.where(Score.rating >= f.min_rating)
    if f.urgent:
        stmt = stmt.where(
            Tender.deadline_at >= today, Tender.deadline_at < today + timedelta(days=URGENT_DAYS)
        )
    if f.q:
        like = f"%{f.q}%"
        stmt = stmt.where(
            or_(
                Tender.object.ilike(like),
                Tender.customer_name.ilike(like),
                Tender.reg_number.ilike(like),
            )
        )
    column = {
        "rating": Score.rating,
        "price": Tender.initial_price,
        "deadline": Tender.deadline_at,
        "updated": Tender.updated_at,
        "object": Tender.object,
    }[f.sort]
    order = column.asc().nulls_last() if f.dir == "asc" else column.desc().nulls_last()
    return stmt.order_by(order, Tender.id.desc())


def registry_rows(
    session: Session, f: RegistryFilter, today: date, paged: bool = True
) -> tuple[list[tuple[Tender, Score, Profile]], int]:
    stmt = registry_stmt(f, today)
    total = len(session.execute(stmt.with_only_columns(Score.id).order_by(None)).all())
    if paged:
        stmt = stmt.limit(PAGE_SIZE).offset((f.page - 1) * PAGE_SIZE)
    return [(t, s, p) for t, s, p in session.execute(stmt).all()], total
