from dataclasses import replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from zakupki_monitor.models import Tender, TenderEvent
from zakupki_monitor.parser import parse_search_page
from zakupki_monitor.store import upsert_tender

from .conftest import fixture_text


def test_dedup_and_events(session) -> None:  # type: ignore[no-untyped-def]
    entry = parse_search_page(fixture_text("search_223.html")).entries[0]
    r1 = upsert_tender(session, entry)
    assert r1.outcome == "new"
    assert upsert_tender(session, entry).outcome == "unchanged"
    assert session.scalars(select(Tender)).all() == [r1.tender]
    assert session.scalars(select(TenderEvent)).all() == []

    changed = replace(
        entry,
        initial_price=Decimal("1.50"),
        deadline_at=date(2026, 10, 20),
        stage="Работа комиссии",
        updated_at=date(2026, 10, 3),
    )
    r2 = upsert_tender(session, changed)
    assert r2.outcome == "updated"
    assert r2.tender.id == r1.tender.id
    assert set(r2.changed_fields) == {"initial_price", "deadline_at", "stage", "updated_at"}
    events = {e.field: (e.old_value, e.new_value) for e in session.scalars(select(TenderEvent))}
    assert events["initial_price"] == ("4838451.11", "1.50")
    assert events["deadline_at"] == ("2026-10-09", "2026-10-20")
    assert events["stage"] == ("Подача заявок", "Работа комиссии")
    assert session.scalars(select(Tender)).one().initial_price == Decimal("1.50")


def test_page_dedups_by_reg_number(session) -> None:  # type: ignore[no-untyped-def]
    entries = parse_search_page(fixture_text("search_44.html")).entries
    for e in entries + entries:
        upsert_tender(session, e)
    assert len(session.scalars(select(Tender)).all()) == 50
