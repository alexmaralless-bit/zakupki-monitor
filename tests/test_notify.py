from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from zakupki_monitor.appsettings import RuntimeSettings
from zakupki_monitor.collector import evaluate
from zakupki_monitor.models import utcnow
from zakupki_monitor.notify import TelegramError, build_digest, send_digest
from zakupki_monitor.profiles import ProfileSpec, save_profile
from zakupki_monitor.scoring import RuleSpec
from zakupki_monitor.search_url import SearchParams
from zakupki_monitor.store import upsert_tender

from .conftest import make_tender_entry

TODAY = date(2026, 10, 4)


def seed(session):  # type: ignore[no-untyped-def]
    profile = save_profile(
        session,
        ProfileSpec(
            name="Газ",
            search=SearchParams(search_string="x"),
            rules=[RuleSpec(type="keyword", value="газопровод", weight=1)],
        ),
    )
    rows = {
        "high": make_tender_entry("1", deadline_at=TODAY + timedelta(days=10)),
        "low": make_tender_entry("2", object="Закупка канцтоваров", deadline_at=TODAY + timedelta(days=10)),
        "urgent": make_tender_entry(
            "3", object="Закупка канцтоваров", deadline_at=TODAY + timedelta(days=1),
            initial_price=Decimal("100"),
        ),
        "old": make_tender_entry("4", deadline_at=TODAY + timedelta(days=10)),
    }  # fmt: skip
    for key, entry in rows.items():
        tender = upsert_tender(session, entry).tender
        if key == "old":
            tender.first_seen_at = utcnow() - timedelta(days=5)
        evaluate(session, tender, profile, TODAY)
    session.flush()


def test_digest_has_new_good_and_urgent(session) -> None:  # type: ignore[no-untyped-def]
    seed(session)
    rt = RuntimeSettings(telegram_threshold=70)
    text = build_digest(session, rt, today=TODAY)
    assert text is not None
    assert "Срочно" in text and "канцтоваров" in text  # срочная, хоть рейтинг низкий
    assert "Новые с рейтингом от 70: 1" in text  # старая и слабая не попали
    assert text.count("Проектирование газопровода") == 1


def test_digest_empty(session) -> None:  # type: ignore[no-untyped-def]
    assert build_digest(session, RuntimeSettings(), today=TODAY) is None


def test_send_digest_posts_to_telegram(session) -> None:  # type: ignore[no-untyped-def]
    seed(session)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rt = RuntimeSettings(telegram_token="123:ABC", telegram_chat_id="42")
    assert send_digest(session, rt, client, today=TODAY) is True
    assert seen[0].url.path == "/bot123:ABC/sendMessage"
    body = json.loads(seen[0].content)
    assert body["chat_id"] == "42" and "Монитор госзакупок" in body["text"]


def test_telegram_errors(session) -> None:  # type: ignore[no-untyped-def]
    seed(session)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(TelegramError):
        send_digest(
            session, RuntimeSettings(telegram_token="t", telegram_chat_id="1"), client, TODAY
        )
    with pytest.raises(TelegramError):
        send_digest(session, RuntimeSettings(), client, TODAY)
