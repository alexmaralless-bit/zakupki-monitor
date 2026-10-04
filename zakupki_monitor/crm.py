"""Отправка тендера в CRM через вебхук n8n (схема tender.v1, спецификация, раздел 6)."""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .appsettings import RuntimeSettings
from .models import CrmExport, Profile, Score, Tender

log = logging.getLogger(__name__)


class CustomerV1(BaseModel):
    name: str | None = None
    inn: str | None = None


class AIV1(BaseModel):
    score: float | None = None
    verdict: str = ""
    reasons: list[str] = []
    quotes: list[str] = []


class TenderV1(BaseModel):
    schema_version: Literal["tender.v1"] = "tender.v1"
    reg_number: str
    law: int | None
    method: str | None
    object: str
    customer: CustomerV1
    price: float | None
    currency: str | None
    published_at: str | None
    updated_at: str | None
    deadline_at: str | None  # YYYY-MM-DD или YYYY-MM-DDTHH:MM (время заказчика, МСК)
    url: str | None
    profile: str
    rating: float | None
    rating_breakdown: dict[str, Any] | None
    ai: AIV1
    user_note: str | None


def build_payload(tender: Tender, profile: Profile, score: Score | None) -> TenderV1:
    deadline = tender.deadline_at.isoformat() if tender.deadline_at else None
    if deadline and tender.deadline_time:
        deadline += f"T{tender.deadline_time.strftime('%H:%M')}"
    ai_raw = (score.breakdown or {}).get("ai") if score else None
    ai = AIV1.model_validate(
        {k: v for k, v in (ai_raw or {}).items() if k in AIV1.model_fields and v is not None}
    )
    return TenderV1(
        reg_number=tender.reg_number,
        law=tender.law,
        method=tender.method,
        object=tender.object,
        customer=CustomerV1(name=tender.customer_name, inn=tender.customer_inn),
        price=float(tender.initial_price) if tender.initial_price is not None else None,
        currency=tender.currency,
        published_at=tender.published_at.isoformat() if tender.published_at else None,
        updated_at=tender.updated_at.isoformat() if tender.updated_at else None,
        deadline_at=deadline,
        url=tender.url,
        profile=profile.name,
        rating=score.rating if score else None,
        rating_breakdown=score.breakdown if score else None,
        ai=ai,
        user_note=tender.note,
    )


class CrmError(Exception):
    pass


def send_to_crm(
    session: Session,
    tender: Tender,
    profile: Profile,
    rt: RuntimeSettings,
    client: httpx.Client | None = None,
) -> CrmExport:
    """POST на вебхук; запрос и ответ пишутся в crm_exports, при успехе статус -> sent_to_crm."""
    url = rt.webhook_url.strip()
    if not url.startswith(("http://", "https://")):
        raise CrmError("Адрес вебхука n8n не задан: укажите его в настройках")
    score = next((s for s in tender.scores if s.profile_id == profile.id), None)
    payload = build_payload(tender, profile, score).model_dump(mode="json")
    headers = {"Content-Type": "application/json"}
    if rt.webhook_secret:
        headers[rt.webhook_header or "X-Webhook-Secret"] = rt.webhook_secret

    export = CrmExport(tender=tender, url=url, request=payload)
    own = client is None
    http = client or httpx.Client(timeout=30.0, trust_env=False)
    try:
        resp = http.post(url, json=payload, headers=headers)
        export.http_status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:2000]}
        export.response = body if isinstance(body, dict) else {"data": body}
        export.ok = resp.is_success
        if not export.ok:
            export.error = f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        export.ok = False
        export.error = f"{type(exc).__name__}: {exc}"
        log.warning("отправка в CRM не удалась: %s", exc)
    finally:
        if own:
            http.close()
    if export.ok:
        tender.status = "sent_to_crm"
    session.add(export)
    session.flush()
    return export
