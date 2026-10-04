"""Сборщик выдачи ЕИС: вежливые запросы, постраничный обход, отсев, карточки, рейтинг."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Profile, Run, Score, Tender, utcnow
from .parser import CardData, SearchEntry, parse_card, parse_lot_list, parse_search_page
from .profiles import profile_filter, profile_rules, profile_search
from .scoring import (
    AIScorer,
    NullAIScorer,
    TenderView,
    apply_filter,
    final_rating,
    score_rules,
)
from .store import upsert_tender

log = logging.getLogger(__name__)


class FetchError(Exception):
    pass


def make_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Клиент ЕИС: свои сертификаты, без прокси из окружения (сайт доступен напрямую)."""
    return httpx.Client(
        verify=str(settings.resolved_ca_bundle()) if transport is None else True,
        trust_env=False,
        headers={"User-Agent": settings.user_agent},
        timeout=settings.timeout,
        follow_redirects=True,
        transport=transport,
    )


class Fetcher:
    """Один запрос за раз, пауза 2-4 с с разбросом, 3 попытки с нарастающей задержкой."""

    def __init__(
        self,
        client: httpx.Client,
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.requests = 0

    def get(self, url: str) -> str:
        s = self.settings
        if self.requests > 0:
            self.sleep(self.rng.uniform(s.pause_min, s.pause_max))
        self.requests += 1
        last: Exception | None = None
        for attempt in range(1, s.max_retries + 1):
            try:
                resp = self.client.get(url)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise FetchError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.text
            except (httpx.TransportError, httpx.HTTPStatusError, FetchError) as exc:
                last = exc
                log.warning("запрос не удался (%s/%s): %s", attempt, s.max_retries, exc)
                if attempt < s.max_retries:
                    self.sleep(s.backoff_base * 2 ** (attempt - 1))
        raise FetchError(f"{url}: {last}")


@dataclass
class RunStats:
    pages: int = 0
    found: int = 0
    new: int = 0
    updated: int = 0
    filtered: int = 0
    scored: int = 0
    cards: int = 0
    errors: int = 0
    notes: list[str] = field(default_factory=list)


def card_url(url: str) -> str:
    """Ссылка 223-ФЗ из выдачи перенаправляется на notice223; идём сразу по конечному адресу."""
    return url.replace(
        "/223/purchase/public/purchase/info/common-info.html",
        "/epz/order/notice/notice223/common-info.html",
    )


def tender_text(tender: Tender) -> str:
    """Текст закупки для ИИ: только то, что реально известно, без домыслов."""
    parts = [f"Объект закупки: {tender.object}"]
    if tender.customer_name:
        parts.append(f"Заказчик: {tender.customer_name}")
    if tender.method:
        parts.append(f"Способ определения поставщика: {tender.method}")
    if tender.okpd2:
        parts.append(f"ОКПД2: {', '.join(tender.okpd2)}")
    if tender.initial_price is not None:
        parts.append(f"Начальная цена: {tender.initial_price} {tender.currency or ''}".strip())
    if tender.delivery_place:
        parts.append(f"Место поставки: {tender.delivery_place}")
    if tender.bid_security:
        parts.append(f"Обеспечение заявки: {tender.bid_security}")
    if tender.contract_security:
        parts.append(f"Обеспечение исполнения контракта: {tender.contract_security}")
    return "\n".join(parts)


def tender_view(tender: Tender) -> TenderView:
    return TenderView(
        reg_number=tender.reg_number,
        text=tender_text(tender),
        object=tender.object,
        law=tender.law,
        method=tender.method,
        customer_name=tender.customer_name,
        customer_inn=tender.customer_inn,
        initial_price=tender.initial_price,
        deadline_at=tender.deadline_at,
        okpd2=list(tender.okpd2 or []),
    )


def _get_score(session: Session, tender: Tender, profile: Profile) -> Score | None:
    return session.scalar(
        select(Score).where(Score.tender_id == tender.id, Score.profile_id == profile.id)
    )


def evaluate(
    session: Session,
    tender: Tender,
    profile: Profile,
    today: date,
    ai: AIScorer | None = None,
) -> Score:
    """Отсев + баллы по правилам + ИИ (если задан критерий и оценщик); сохраняет Score."""
    ai = ai or NullAIScorer()
    view = tender_view(tender)
    flt = apply_filter(view, profile_filter(profile), today)
    score = _get_score(session, tender, profile)
    if score is None:
        score = Score(tender=tender, profile_id=profile.id)
        session.add(score)
    score.scored_at = utcnow()
    if not flt.passed:
        score.filtered = True
        score.filter_reasons = flt.reasons
        score.rules_score = score.ai_score = score.rating = None
        score.breakdown = None
        return score
    rules = score_rules(view, profile_rules(profile), today)
    outcome = ai.score(view, profile.ai_criterion) if profile.ai_criterion else None
    ai_score = outcome.score if outcome else None
    score.filtered = False
    score.filter_reasons = []
    score.rules_score = rules.score
    score.ai_score = ai_score
    score.rating = final_rating(rules.score, ai_score, profile.ai_weight)
    score.breakdown = {"rules": rules.breakdown, "ai": outcome.as_dict() if outcome else None}
    return score


def apply_card(tender: Tender, card: CardData) -> None:
    tender.okpd2 = card.okpd2 or None
    tender.bid_security = card.bid_security
    tender.contract_security = card.contract_security
    tender.delivery_place = card.delivery_place
    if card.customer_inn:
        tender.customer_inn = card.customer_inn
    if card.deadline_time:
        tender.deadline_time = card.deadline_time
    tender.card_fetched_at = utcnow()


def _fetch_card(tender: Tender, fetcher: Fetcher) -> None:
    """Карточка; у 223-ФЗ коды ОКПД2 лежат на вкладке «Список лотов» (+1 запрос)."""
    card = parse_card(fetcher.get(card_url(tender.url or "")))
    if not card.okpd2 and card.lot_list_url:
        card.okpd2 = parse_lot_list(fetcher.get(card.lot_list_url))
    apply_card(tender, card)


def collect(
    session: Session,
    profile: Profile,
    fetcher: Fetcher,
    max_pages: int | None = None,
    today: date | None = None,
    ai: AIScorer | None = None,
) -> RunStats:
    today = today or date.today()
    limit = max_pages or profile.max_pages
    params = profile_search(profile)
    stats = RunStats()
    run = Run(profile_id=profile.id)
    session.add(run)
    session.flush()

    for page_no in range(1, limit + 1):
        try:
            html = fetcher.get(params.to_url(page_no))
        except FetchError as exc:
            stats.errors += 1
            stats.notes.append(str(exc))
            break
        page = parse_search_page(html)
        stats.pages += 1
        if not page.entries:
            stats.notes.append(f"страница {page_no}: закупок не найдено")
            break
        stats.found += len(page.entries)
        changed_on_page = 0
        for entry in page.entries:
            changed_on_page += _process_entry(session, entry, profile, fetcher, stats, today, ai)
        session.flush()
        if changed_on_page == 0:
            stats.notes.append(f"страница {page_no}: нет новых и обновлённых, остановка")
            break
        if not page.has_next:
            break

    run.finished_at = utcnow()
    run.pages, run.found = stats.pages, stats.found
    run.new, run.updated = stats.new, stats.updated
    run.filtered, run.cards, run.errors = stats.filtered, stats.cards, stats.errors
    run.note = "; ".join(stats.notes) or None
    session.flush()
    return stats


def _process_entry(
    session: Session,
    entry: SearchEntry,
    profile: Profile,
    fetcher: Fetcher,
    stats: RunStats,
    today: date,
    ai: AIScorer | None,
) -> int:
    """Возвращает 1, если закупка новая или изменилась."""
    res = upsert_tender(session, entry)
    tender = res.tender
    if res.outcome == "new":
        stats.new += 1
    elif res.outcome == "updated":
        stats.updated += 1
    existing = _get_score(session, tender, profile)
    if res.outcome == "unchanged" and existing is not None:
        return 0
    changed = 1  # новая, изменившаяся или ещё не оценённая по этому профилю

    score = evaluate(session, tender, profile, today)  # первичный отсев по данным выдачи, без ИИ
    if score.filtered:
        stats.filtered += 1
        return changed
    # карточка - только для прошедших отсев; перезапрос, если закупка изменилась
    if tender.card_fetched_at is None or res.outcome == "updated":
        try:
            _fetch_card(tender, fetcher)
            stats.cards += 1
        except FetchError as exc:
            stats.errors += 1
            stats.notes.append(f"карточка {tender.reg_number}: {exc}")
    score = evaluate(session, tender, profile, today, ai)  # итог: с карточкой и ИИ
    if score.filtered:
        stats.filtered += 1
    else:
        stats.scored += 1
    return changed


def rescore_profile(
    session: Session, profile: Profile, today: date, ai: AIScorer | None = None
) -> int:
    """Пересчёт рейтинга уже собранных закупок профиля (после правки правил)."""
    scores = list(session.scalars(select(Score).where(Score.profile_id == profile.id)))
    for score in scores:
        evaluate(session, score.tender, profile, today, ai)
    session.flush()
    return len(scores)
