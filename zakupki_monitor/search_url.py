"""Параметры поиска ЕИС: разбор ссылки сайта и детерминированная сборка обратно."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import BaseModel, Field

from .config import SEARCH_URL

# Параметры, которыми управляет сам сборщик (сортировка, страница) или которые разобраны в поля.
_MANAGED = {
    "searchString",
    "morphology",
    "pageNumber",
    "sortDirection",
    "recordsPerPage",
    "sortBy",
    "fz44",
    "fz223",
    "af",
    "priceFromGeneral",
    "priceToGeneral",
    "okpd2Ids",
    "okpd2IdsCodes",
    "customerPlace",
    "customerPlaceCodes",
}


def _split(value: str) -> list[str]:
    return [p for p in value.split(",") if p]


def _dec(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(" ", "").replace(",", "."))
    except ArithmeticError:
        return None


class SearchParams(BaseModel):
    search_string: str = ""
    morphology: bool = True
    fz44: bool = True
    fz223: bool = True
    af: bool = True  # только закупки на этапе подачи заявок
    okpd2_codes: list[str] = Field(default_factory=list)
    okpd2_ids: list[str] = Field(default_factory=list)
    price_from: Decimal | None = None
    price_to: Decimal | None = None
    region_names: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list)
    extra: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_url(cls, url: str) -> SearchParams:
        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
        q: dict[str, str] = {}
        for key, value in pairs:
            q[key] = value
        has_laws = "fz44" in q or "fz223" in q
        return cls(
            search_string=q.get("searchString", ""),
            morphology=q.get("morphology") == "on",
            fz44=q.get("fz44") == "on" if has_laws else True,
            fz223=q.get("fz223") == "on" if has_laws else True,
            af=q.get("af") == "on",
            okpd2_codes=_split(q.get("okpd2IdsCodes", "")),
            okpd2_ids=_split(q.get("okpd2Ids", "")),
            price_from=_dec(q.get("priceFromGeneral")),
            price_to=_dec(q.get("priceToGeneral")),
            region_names=_split(q.get("customerPlace", "")),
            region_codes=_split(q.get("customerPlaceCodes", "")),
            extra={k: v for k, v in q.items() if k not in _MANAGED},
        )

    def query_pairs(self, page: int = 1) -> list[tuple[str, str]]:
        extra = dict(self.extra)
        pairs: list[tuple[str, str]] = [("searchString", self.search_string)]
        if self.morphology:
            pairs.append(("morphology", "on"))
        if "search-filter" in extra:
            pairs.append(("search-filter", extra.pop("search-filter")))
        pairs += [
            ("pageNumber", str(page)),
            ("sortDirection", "false"),
            ("recordsPerPage", "_50"),
            ("sortBy", "UPDATE_DATE"),
        ]
        if "showLotsInfoHidden" in extra:
            pairs.append(("showLotsInfoHidden", extra.pop("showLotsInfoHidden")))
        if self.fz44:
            pairs.append(("fz44", "on"))
        if self.fz223:
            pairs.append(("fz223", "on"))
        if self.af:
            pairs.append(("af", "on"))
        if self.price_from is not None:
            pairs.append(("priceFromGeneral", format(self.price_from, "f")))
        if self.price_to is not None:
            pairs.append(("priceToGeneral", format(self.price_to, "f")))
        if "currencyIdGeneral" in extra:
            pairs.append(("currencyIdGeneral", extra.pop("currencyIdGeneral")))
        if self.okpd2_ids:
            pairs.append(("okpd2Ids", ",".join(self.okpd2_ids)))
        if self.okpd2_codes:
            pairs.append(("okpd2IdsCodes", ",".join(self.okpd2_codes)))
        if self.region_names:
            pairs.append(("customerPlace", ",".join(self.region_names)))
        if self.region_codes:
            pairs.append(("customerPlaceCodes", ",".join(self.region_codes)))
        pairs += sorted(extra.items())
        return pairs

    def to_url(self, page: int = 1) -> str:
        return f"{SEARCH_URL}?{urlencode(self.query_pairs(page))}"
