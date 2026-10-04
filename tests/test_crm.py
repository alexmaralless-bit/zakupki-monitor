from __future__ import annotations

import json
from datetime import time
from decimal import Decimal

import httpx

from zakupki_monitor.appsettings import RuntimeSettings
from zakupki_monitor.collector import evaluate
from zakupki_monitor.crm import CrmError, TenderV1, build_payload, send_to_crm
from zakupki_monitor.profiles import ProfileSpec, save_profile
from zakupki_monitor.scoring import RuleSpec
from zakupki_monitor.search_url import SearchParams
from zakupki_monitor.store import upsert_tender

from .conftest import make_tender_entry
from .test_collector import TODAY


def setup(session):  # type: ignore[no-untyped-def]
    profile = save_profile(
        session,
        ProfileSpec(
            name="Газ",
            search=SearchParams(search_string="x"),
            rules=[RuleSpec(type="keyword", value="газопровод", weight=1)],
        ),
    )
    tender = upsert_tender(session, make_tender_entry(initial_price=Decimal("2500000.50"))).tender
    tender.deadline_time = time(12, 30)
    tender.note = "позвонить"
    evaluate(session, tender, profile, TODAY)
    session.refresh(tender)
    return tender, profile


def test_payload_matches_tender_v1(session) -> None:  # type: ignore[no-untyped-def]
    tender, profile = setup(session)
    payload = build_payload(tender, profile, tender.scores[0]).model_dump(mode="json")
    expected = {
        "schema_version", "reg_number", "law", "method", "object", "customer", "price",
        "currency", "published_at", "updated_at", "deadline_at", "url", "profile", "rating",
        "rating_breakdown", "ai", "user_note",
    }  # fmt: skip
    assert set(payload) == expected
    assert payload["schema_version"] == "tender.v1"
    assert payload["customer"] == {"name": 'АО "МОСОБЛГАЗ"', "inn": "5032292612"}
    assert payload["price"] == 2500000.5
    assert payload["deadline_at"] == "2026-10-20T12:30"
    assert payload["rating"] == 100 and payload["user_note"] == "позвонить"
    assert payload["ai"]["score"] is None
    TenderV1.model_validate(payload)


def mock_client(handler):  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_success_stores_exchange_and_sets_status(session) -> None:  # type: ignore[no-untyped-def]
    tender, profile = setup(session)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"crm": "amocrm", "external_id": "77", "url": "https://x/77"}
        )

    rt = RuntimeSettings(
        webhook_url="http://n8n.local/webhook/z", webhook_header="X-Secret", webhook_secret="s3"
    )
    export = send_to_crm(session, tender, profile, rt, mock_client(handler))
    assert export.ok and export.http_status == 200
    assert export.response == {"crm": "amocrm", "external_id": "77", "url": "https://x/77"}
    assert tender.status == "sent_to_crm"
    assert seen[0].headers["x-secret"] == "s3"
    assert json.loads(seen[0].content)["reg_number"] == tender.reg_number
    assert export.request["reg_number"] == tender.reg_number


def test_send_failure_keeps_status(session) -> None:  # type: ignore[no-untyped-def]
    tender, profile = setup(session)
    rt = RuntimeSettings(webhook_url="http://n8n.local/webhook/z")
    export = send_to_crm(
        session, tender, profile, rt, mock_client(lambda r: httpx.Response(500, text="boom"))
    )
    assert not export.ok and export.error == "HTTP 500" and tender.status == "new"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    export = send_to_crm(session, tender, profile, rt, mock_client(broken))
    assert not export.ok and "ConnectError" in (export.error or "") and tender.status == "new"
    assert len(tender.exports) == 2


def test_send_requires_webhook(session) -> None:  # type: ignore[no-untyped-def]
    tender, profile = setup(session)
    try:
        send_to_crm(session, tender, profile, RuntimeSettings())
    except CrmError as exc:
        assert "вебхука" in str(exc)
    else:
        raise AssertionError("ожидали CrmError")
