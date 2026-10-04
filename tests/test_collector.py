from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from zakupki_monitor.collector import Fetcher, FetchError, collect
from zakupki_monitor.config import Settings
from zakupki_monitor.models import Score, Tender
from zakupki_monitor.profiles import ProfileSpec, save_profile
from zakupki_monitor.scoring import FilterConfig, RuleSpec
from zakupki_monitor.search_url import SearchParams

from .conftest import FakeSite, fixture_text

TODAY = date(2026, 10, 4)


def make_profile(session, **kw):  # type: ignore[no-untyped-def]
    spec = ProfileSpec(
        name=kw.pop("name", "p"),
        search=SearchParams(search_string="x"),
        max_pages=kw.pop("max_pages", 5),
        filter=kw.pop("filter", FilterConfig()),
        rules=kw.pop("rules", [RuleSpec(type="keyword", value="газопровод", weight=1)]),
    )
    return save_profile(session, spec)


def test_incremental_stop_and_cards_only_for_passed(session, settings) -> None:  # type: ignore[no-untyped-def]
    p223, p44 = fixture_text("search_223.html"), fixture_text("search_44.html")
    # страница 3 повторяет страницу 2: на ней нет нового -> остановка
    site = FakeSite({1: p223, 2: p44, 3: p44, 4: p223})
    # отсев по цене: пропускаем лишь малую часть закупок
    profile = make_profile(session, filter=FilterConfig(price_min=Decimal("20000000")))
    sleeps: list[float] = []
    stats = collect(session, profile, site.fetcher(settings, sleeps), today=TODAY)
    assert site.search_requests == [1, 2, 3]
    assert stats.pages == 3 and stats.found == 150
    assert stats.new == 100
    total = len(session.scalars(select(Tender)).all())
    assert total == 100
    passed = len(session.scalars(select(Score).where(Score.filtered.is_(False))).all())
    assert stats.filtered == 100 - passed
    assert stats.cards == passed == len(site.card_requests)
    assert passed < 20  # карточки запрошены не для всех
    # паузы между запросами 2-4 с
    assert len(sleeps) == len(site.search_requests) + len(site.card_requests) - 1
    assert all(2 <= s <= 4 for s in sleeps)


def test_page_limit(session, settings) -> None:  # type: ignore[no-untyped-def]
    site = FakeSite({1: fixture_text("search_223.html"), 2: fixture_text("search_44.html")})
    profile = make_profile(session, filter=FilterConfig(price_min=Decimal("10") ** 12))
    stats = collect(session, profile, site.fetcher(settings), max_pages=1, today=TODAY)
    assert site.search_requests == [1]
    assert stats.pages == 1 and stats.found == 50


def test_second_run_stops_on_first_page(session, settings) -> None:  # type: ignore[no-untyped-def]
    site = FakeSite({1: fixture_text("search_223.html"), 2: fixture_text("search_44.html")})
    profile = make_profile(session, filter=FilterConfig(price_min=Decimal("10") ** 12))
    collect(session, profile, site.fetcher(settings), max_pages=1, today=TODAY)
    site.search_requests.clear()
    stats = collect(session, profile, site.fetcher(settings), max_pages=5, today=TODAY)
    assert site.search_requests == [1]
    assert stats.new == 0 and stats.updated == 0


def test_scoring_saved_with_card_data(session, settings) -> None:  # type: ignore[no-untyped-def]
    site = FakeSite({1: fixture_text("search_223.html")})
    profile = make_profile(
        session,
        rules=[
            RuleSpec(type="keyword", value="газопровод", weight=2),
            RuleSpec(type="customer", value="5032292612", weight=1),
        ],
    )
    stats = collect(session, profile, site.fetcher(settings), max_pages=1, today=TODAY)
    assert stats.cards == 50  # фильтра нет
    tender = session.scalars(select(Tender).where(Tender.reg_number == "32616430006")).one()
    score = session.scalars(select(Score).where(Score.tender_id == tender.id)).one()
    assert tender.card_fetched_at is not None
    assert score.rating == score.rules_score and score.ai_score is None
    assert score.breakdown is not None and len(score.breakdown["rules"]) == 2
    assert score.rating == pytest.approx(100.0)


def test_retry_with_backoff(settings) -> None:  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, text="ok")

    sleeps: list[float] = []
    f = Fetcher(httpx.Client(transport=httpx.MockTransport(handler)), settings, sleep=sleeps.append)
    assert f.get("https://example.test/") == "ok"
    assert calls["n"] == 3 and sleeps == [2.0, 4.0]


def test_retries_exhausted(settings: Settings) -> None:
    f = Fetcher(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        settings,
        sleep=lambda s: None,
    )
    with pytest.raises(FetchError):
        f.get("https://example.test/")


def test_fetch_error_recorded_in_run(session, settings) -> None:  # type: ignore[no-untyped-def]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    profile = make_profile(session)
    stats = collect(session, profile, Fetcher(client, settings, sleep=lambda s: None), today=TODAY)
    assert stats.errors == 1 and stats.pages == 0


def test_card_url_skips_redirect() -> None:
    from zakupki_monitor.collector import card_url

    assert (
        card_url(
            "https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html?regNumber=1"
        )
        == "https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html?regNumber=1"
    )
    u44 = "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=2"
    assert card_url(u44) == u44
