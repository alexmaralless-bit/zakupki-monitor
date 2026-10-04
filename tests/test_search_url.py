from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from zakupki_monitor.profiles import ProfileSpec
from zakupki_monitor.search_url import SearchParams

TEST_URL = (
    "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=%D0%BC%D0%BE%D1%81%D0%BE%D0%B1"
    "%D0%BB%D0%B3%D0%B0%D0%B7&morphology=on&search-filter=%D0%94%D0%B0%D1%82%D0%B0+%D1%80%D0%B0%D0%B7%D0%BC"
    "%D0%B5%D1%89%D0%B5%D0%BD%D0%B8%D1%8F&pageNumber=1&sortDirection=false&recordsPerPage=_50&sortBy"
    "=UPDATE_DATE&showLotsInfoHidden=false&fz44=on&fz223=on&af=on&currencyIdGeneral=-1&okpd2Ids=8879189"
    "%2C73158%2C73159%2C8890849&okpd2IdsCodes=71.12.19%2C71.12.19.100%2C71.12.19.200%2C71.12.19.000"
)


def test_import_fields() -> None:
    p = SearchParams.from_url(TEST_URL)
    assert p.search_string == "мособлгаз"
    assert p.morphology and p.fz44 and p.fz223 and p.af
    assert p.okpd2_ids == ["8879189", "73158", "73159", "8890849"]
    assert p.okpd2_codes == ["71.12.19", "71.12.19.100", "71.12.19.200", "71.12.19.000"]
    assert p.extra["search-filter"] == "Дата размещения"


def test_round_trip_is_exact() -> None:
    p = SearchParams.from_url(TEST_URL)
    assert p.to_url(1) == TEST_URL
    assert SearchParams.from_url(p.to_url(1)) == p


def test_page_number_and_idempotent_json() -> None:
    p = SearchParams.from_url(TEST_URL)
    again = SearchParams.model_validate(p.model_dump(mode="json"))
    assert again.to_url(3) == p.to_url(3)
    assert parse_qs(urlsplit(p.to_url(3)).query)["pageNumber"] == ["3"]


def test_price_region_and_only_44() -> None:
    url = (
        "https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString=x&fz44=on"
        "&priceFromGeneral=100000&priceToGeneral=2500000.50&customerPlace=77&customerPlaceCodes=5277335"
    )
    p = SearchParams.from_url(url)
    assert p.fz44 and not p.fz223 and not p.af and not p.morphology
    assert p.price_from == Decimal("100000") and p.price_to == Decimal("2500000.50")
    assert p.region_names == ["77"] and p.region_codes == ["5277335"]
    q = parse_qs(urlsplit(p.to_url()).query)
    assert q["priceToGeneral"] == ["2500000.50"] and q["customerPlaceCodes"] == ["5277335"]
    assert "fz223" not in q
    assert SearchParams.from_url(p.to_url()) == p


def test_profile_spec_from_url() -> None:
    spec = ProfileSpec(name="t", url=TEST_URL)
    assert spec.search is not None and spec.search.search_string == "мособлгаз"
