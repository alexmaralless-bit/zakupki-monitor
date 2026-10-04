from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from zakupki_monitor.collector import Fetcher
from zakupki_monitor.config import Settings
from zakupki_monitor.db import make_engine, upgrade_db

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    upgrade_db(url)  # заодно проверяем миграцию
    engine = make_engine(url)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(pause_min=2, pause_max=4, backoff_base=2, max_retries=3)


class FakeSite:
    """Подмена ЕИС: страницы выдачи по номеру и карточки; хранит журнал запросов."""

    def __init__(self, pages: dict[int, str], card_html: str | None = None) -> None:
        self.pages = pages
        self.card_html = card_html or fixture_text("card_223.html")
        self.search_requests: list[int] = []
        self.card_requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if "extendedsearch" in request.url.path:
            page = int(request.url.params["pageNumber"])
            self.search_requests.append(page)
            if page in self.pages:
                return httpx.Response(200, text=self.pages[page])
            return httpx.Response(200, text="<html><body></body></html>")
        self.card_requests.append(str(request.url))
        if "lot-list" in request.url.path:
            return httpx.Response(200, text=fixture_text("lots_223.html"))
        return httpx.Response(200, text=self.card_html)

    def fetcher(self, settings: Settings, sleeps: list[float] | None = None) -> Fetcher:
        client = httpx.Client(transport=httpx.MockTransport(self.handler), trust_env=False)
        log = sleeps if sleeps is not None else []
        return Fetcher(client, settings, sleep=log.append)


def make_tender_entry(reg: str = "32600000001", **kw):  # type: ignore[no-untyped-def]
    from datetime import date
    from decimal import Decimal

    from zakupki_monitor.parser import SearchEntry

    data: dict[str, Any] = {
        "reg_number": reg,
        "law": 223,
        "method": "Конкурс",
        "stage": "Подача заявок",
        "object": "Проектирование газопровода высокого давления",
        "customer_name": 'АО "МОСОБЛГАЗ"',
        "customer_inn": "5032292612",
        "initial_price": Decimal("2500000"),
        "currency": "RUB",
        "published_at": date(2026, 10, 1),
        "updated_at": date(2026, 10, 1),
        "deadline_at": date(2026, 10, 20),
        "url": f"https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html?regNumber={reg}",
    }
    data.update(kw)
    return SearchEntry(**data)
