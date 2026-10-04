"""Разбор HTML: страница выдачи расширенного поиска и карточка закупки (selectolax)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .config import SITE_ROOT

_CURRENCY_SYMBOLS = {"₽": "RUB", "$": "USD", "€": "EUR", "¥": "CNY"}
_OKPD2_RE = re.compile(r"\b\d{2}(?:\.\d{1,2}){0,3}(?:\.\d{1,3})?\b")


@dataclass
class SearchEntry:
    reg_number: str
    law: int | None
    method: str | None
    stage: str | None
    object: str
    customer_name: str | None
    customer_inn: str | None
    initial_price: Decimal | None
    currency: str | None
    published_at: date | None
    updated_at: date | None
    deadline_at: date | None
    url: str | None


@dataclass
class SearchPage:
    entries: list[SearchEntry]
    has_next: bool
    total_text: str | None = None


@dataclass
class CardData:
    okpd2: list[str] = field(default_factory=list)
    bid_security: str | None = None
    contract_security: str | None = None
    delivery_place: str | None = None
    customer_inn: str | None = None
    deadline_time: time | None = None
    lot_list_url: str | None = None  # 223-ФЗ: ОКПД2 лежат на вкладке «Список лотов»


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _text(node: LexborNode | None) -> str:
    return clean(node.text(separator="", strip=False)) if node is not None else ""


def parse_date(value: str | None) -> date | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", value or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_money(value: str | None) -> tuple[Decimal | None, str | None]:
    """'4 838 451,11 ₽' -> (Decimal('4838451.11'), 'RUB')."""
    raw = clean(value)
    if not raw:
        return None, None
    currency = next((code for sym, code in _CURRENCY_SYMBOLS.items() if sym in raw), None)
    m = re.search(r"\d[\d ]*(?:,\d+)?", raw)
    if not m:
        return None, currency
    try:
        amount = Decimal(m.group(0).replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None, currency
    return amount, currency


def _data_block(entry: LexborNode, title: str) -> str | None:
    for t in entry.css(".data-block__title"):
        if _text(t).startswith(title):
            nxt = t.next
            while nxt is not None and nxt.tag == "-text":
                nxt = nxt.next
            return _text(nxt) or None
    return None


def _body_value(entry: LexborNode, title: str) -> str | None:
    for t in entry.css(".registry-entry__body-title"):
        if _text(t) == title:
            nxt = t.next
            while nxt is not None and nxt.tag == "-text":
                nxt = nxt.next
            return _text(nxt) or None
    return None


def _absolute(href: str | None) -> str | None:
    return urljoin(SITE_ROOT + "/", href) if href else None


def _parse_entry(entry: LexborNode) -> SearchEntry | None:
    num_link = entry.css_first(".registry-entry__header-mid__number a")
    reg = re.sub(r"\D", "", _text(num_link))
    if not reg:
        return None
    title = _text(entry.css_first(".registry-entry__header-top__title"))
    law_m = re.match(r"(\d+)\s*-\s*ФЗ\s*(.*)", title)
    law = int(law_m.group(1)) if law_m else None
    method = (law_m.group(2) if law_m else title) or None
    stage = _text(entry.css_first(".registry-entry__header-mid__title")) or None

    customer_link = entry.css_first(".registry-entry__body-href a")
    customer_name = _text(customer_link) or None
    inn = None
    if customer_link is not None:
        qs = parse_qs(urlsplit(customer_link.attributes.get("href") or "").query)
        inn = (qs.get("inn") or [""])[0] or None

    price, currency = parse_money(_text(entry.css_first(".price-block__value")))
    return SearchEntry(
        reg_number=reg,
        law=law,
        method=method,
        stage=stage,
        object=_body_value(entry, "Объект закупки") or "",
        customer_name=customer_name,
        customer_inn=inn,
        initial_price=price,
        currency=currency,
        published_at=parse_date(_data_block(entry, "Размещено")),
        updated_at=parse_date(_data_block(entry, "Обновлено")),
        deadline_at=parse_date(_data_block(entry, "Окончание подачи заявок")),
        url=_absolute(num_link.attributes.get("href") if num_link is not None else None),
    )


def parse_search_page(html: str) -> SearchPage:
    tree = LexborHTMLParser(html)
    entries = [
        e
        for node in tree.css("div.search-registry-entry-block")
        if (e := _parse_entry(node)) is not None
    ]
    total = tree.css_first(".search-results__total")
    return SearchPage(
        entries=entries,
        has_next=tree.css_first("a.paginator-button-next") is not None,
        total_text=_text(total) or None,
    )


def _pairs(tree: LexborHTMLParser) -> list[tuple[str, str]]:
    """Пары «подпись -> значение» из обеих вёрсток карточки (44-ФЗ и 223-ФЗ)."""
    pairs: list[tuple[str, str]] = []
    for sel_title in (".section__title", ".common-text__title", ".common-text__value--gray"):
        for t in tree.css(sel_title):
            nxt = t.next
            while nxt is not None and nxt.tag == "-text":
                nxt = nxt.next
            if nxt is None:
                continue
            pairs.append((_text(t), _text(nxt)))
    return pairs


def _delivery_place(tree: LexborHTMLParser) -> str | None:
    for t in tree.css(".section__title"):
        if "Место поставки" in _text(t) or "Место выполнения" in _text(t):
            nxt = t.next
            while nxt is not None and nxt.tag == "-text":
                nxt = nxt.next
            if nxt is None:
                return None
            html = nxt.html or ""
            parts = [clean(LexborHTMLParser(p).text()) for p in re.split(r"<br\s*/?>", html)]
            uniq = list(dict.fromkeys(p for p in parts if p))
            return "; ".join(uniq) or None
    return None


def parse_time(value: str | None) -> time | None:
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", value or "")
    return time(int(m.group(1)), int(m.group(2))) if m else None


_OKPD_HEADERS = ("Код позиции", "ОКПД2", "Код ОКПД")
_OKPD_START = re.compile(r"\d{2}\.\d{2}(?:\.\d{1,3}){0,2}")


def _children(node: LexborNode | None, tag: str) -> list[LexborNode]:
    return [c for c in node.iter(include_text=False) if c.tag == tag] if node else []


def _table_rows(table: LexborNode) -> tuple[list[str], list[list[LexborNode]]]:
    """Заголовки (включая пустые ячейки) и строки тела только этой таблицы, без вложенных."""
    head_rows = _children(_first_child(table, "thead"), "tr")
    heads = [_text(c) for c in head_rows[0].iter(include_text=False)] if head_rows else []
    body = _first_child(table, "tbody")
    rows = [_children(r, "td") for r in _children(body, "tr")]
    return heads, rows


def _first_child(node: LexborNode, tag: str) -> LexborNode | None:
    return next((c for c in node.iter(include_text=False) if c.tag == tag), None)


def _okpd_from_tables(tree: LexborHTMLParser) -> list[str]:
    """44-ФЗ: код позиции (ОКПД2 / ОКПД2 из КТРУ) в таблице «Информация об объекте закупки»."""
    found: list[str] = []
    for table in tree.css("table"):
        heads, rows = _table_rows(table)
        idx = next((i for i, h in enumerate(heads) if any(k in h for k in _OKPD_HEADERS)), None)
        if idx is None:
            continue
        for cells in rows:
            if len(cells) == len(heads):
                m = _OKPD_START.match(_text(cells[idx]))
                if m:
                    found.append(m.group(0))
    return found


def _lot_list_url(tree: LexborHTMLParser) -> str | None:
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        if "lot-list.html" in href:
            return _absolute(href.replace("&amp;", "&"))
    return None


def parse_lot_list(html: str) -> list[str]:
    """223-ФЗ, «Список лотов»: коды из колонки «Классификация по ОКПД2»."""
    tree = LexborHTMLParser(html)
    codes: list[str] = []
    for table in tree.css("table"):
        heads, rows = _table_rows(table)
        idx = next((i for i, h in enumerate(heads) if "ОКПД2" in h), None)
        if idx is None or not heads or not heads[0].startswith("Номер, наименование лота"):
            continue
        for cells in rows:
            if len(cells) == len(heads):
                codes.extend(_OKPD2_RE.findall(_text(cells[idx]).split(" ")[0]))
    return list(dict.fromkeys(codes))


def parse_card(html: str) -> CardData:
    """Доп. поля из common-info.html. Чего нет на странице — None/пусто (не выдумываем)."""
    tree = LexborHTMLParser(html)
    pairs = _pairs(tree)
    data = CardData(delivery_place=_delivery_place(tree), lot_list_url=_lot_list_url(tree))

    def value_of(label_part: str) -> str | None:
        for label, value in pairs:
            if label_part in label and value:
                return value
        return None

    def security(required_label: str, size_label: str) -> str | None:
        req = value_of(required_label)
        if req is None:
            return None
        if req.lower().startswith("нет"):
            return "не требуется"
        return value_of(size_label) or "требуется"

    data.bid_security = security("Требуется обеспечение заявки", "Размер обеспечения заявки")
    data.contract_security = security(
        "Требуется обеспечение исполнения контракта", "Размер обеспечения исполнения контракта"
    )
    for label, value in pairs:
        if "ОКПД2" in label:
            data.okpd2.extend(_OKPD2_RE.findall(value))
    data.okpd2 = list(dict.fromkeys([*data.okpd2, *_okpd_from_tables(tree)]))
    for label, value in pairs:
        if "окончания срока подачи заявок" in label or "окончания подачи заявок" in label:
            data.deadline_time = parse_time(value)
            break
    # ИНН заказчика есть в блоке «Сведения о заказчике» карточки 223-ФЗ; в 44-ФЗ там
    # лишь ИНН платёжных реквизитов, его не берём.
    if "Сведения о заказчике" in tree.text():
        for label, value in pairs:
            if label == "ИНН" and re.fullmatch(r"\d{10}|\d{12}", value):
                data.customer_inn = value
                break
    if data.customer_inn is None:
        # 44-ФЗ: ИНН заказчика зашит в ИКЗ (символы 4-13), сверено по карточке.
        ikz = value_of("Идентификационный код закупки")
        if ikz and re.fullmatch(r"\d{36}", ikz):
            data.customer_inn = ikz[3:13]
    return data
