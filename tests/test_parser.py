from datetime import date
from decimal import Decimal

from zakupki_monitor.parser import parse_card, parse_date, parse_money, parse_search_page

from .conftest import fixture_text


def test_search_223_counts_and_first_entry() -> None:
    page = parse_search_page(fixture_text("search_223.html"))
    assert len(page.entries) == 50
    assert page.has_next
    assert len({e.reg_number for e in page.entries}) == 50
    assert all(e.law == 223 for e in page.entries)
    first = page.entries[0]
    assert first.reg_number == "32616430006"
    assert first.method and first.method.startswith("Конкурс в электронной форме")
    assert first.stage == "Подача заявок"
    assert first.object.startswith("Выполнение предпроектных, проектных и изыскательских работ")
    assert first.customer_name == 'АКЦИОНЕРНОЕ ОБЩЕСТВО "МОСОБЛГАЗ"'
    assert first.customer_inn == "5032292612"
    assert first.initial_price == Decimal("4838451.11")
    assert first.currency == "RUB"
    assert first.published_at == date(2026, 10, 1)
    assert first.updated_at == date(2026, 10, 1)
    assert first.deadline_at == date(2026, 10, 9)
    assert first.url == (
        "https://zakupki.gov.ru/223/purchase/public/purchase/info/common-info.html"
        "?regNumber=32616430006"
    )


def test_search_44_counts_and_first_entry() -> None:
    page = parse_search_page(fixture_text("search_44.html"))
    assert len(page.entries) == 50
    assert all(e.law == 44 for e in page.entries)
    first = page.entries[0]
    assert first.reg_number == "0373200627926000002"
    assert first.method == "Запрос котировок в электронной форме"
    assert first.object == "Оказание услуг по уборке внутренних помещений"  # без подсветки span
    assert first.customer_inn is None  # в выдаче 44-ФЗ ИНН нет
    assert first.initial_price == Decimal("6061223.68")
    assert first.url == (
        "https://zakupki.gov.ru/epz/order/notice/zk20/view/common-info.html"
        "?regNumber=0373200627926000002"
    )
    assert all(e.initial_price and e.deadline_at and e.updated_at for e in page.entries)


def test_empty_page() -> None:
    page = parse_search_page("<html><body>ничего</body></html>")
    assert page.entries == []
    assert not page.has_next


def test_card_44() -> None:
    card = parse_card(fixture_text("card_44.html"))
    assert card.bid_security == "30 306,12 Российский рубль"
    assert card.contract_security == "5 %"
    assert card.delivery_place is not None
    assert card.delivery_place.startswith("Российская Федерация, г. Москва")
    assert card.delivery_place.count("Беговая") == 1  # повторы схлопнуты
    assert card.customer_inn is None  # на карточке 44-ФЗ только ИНН платёжных реквизитов
    assert card.okpd2 == []  # на этой карточке кодов нет, не выдумываем


def test_card_223() -> None:
    card = parse_card(fixture_text("card_223.html"))
    assert card.customer_inn == "5032292612"
    assert card.bid_security is None
    assert card.contract_security is None


def test_card_okpd2_extraction() -> None:
    html = (
        '<section><span class="section__title">Код ОКПД2</span>'
        '<span class="section__info">71.12.19.100 Услуги; 71.12.11</span></section>'
        '<section><span class="section__title">Требуется обеспечение заявки</span>'
        '<span class="section__info">Нет</span></section>'
    )
    card = parse_card(html)
    assert card.okpd2 == ["71.12.19.100", "71.12.11"]
    assert card.bid_security == "не требуется"


def test_money_and_date_helpers() -> None:
    assert parse_money("1 234 567,80\xa0₽") == (Decimal("1234567.80"), "RUB")
    assert parse_money("100 $") == (Decimal("100"), "USD")
    assert parse_money("") == (None, None)
    assert parse_date("Размещено 05.03.2026") == date(2026, 3, 5)
    assert parse_date("32.13.2026") is None
