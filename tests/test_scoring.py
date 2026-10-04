from datetime import date
from decimal import Decimal

import pytest

from zakupki_monitor.scoring import (
    FilterConfig,
    RuleSpec,
    TenderView,
    apply_filter,
    final_rating,
    score_rules,
)

TODAY = date(2026, 10, 4)


def view(**kw) -> TenderView:  # type: ignore[no-untyped-def]
    base = dict(
        object="Проектирование газопровода высокого давления",
        law=223,
        method="Конкурс в электронной форме",
        customer_name='АО "МОСОБЛГАЗ"',
        customer_inn="5032292612",
        initial_price=Decimal("3000000"),
        deadline_at=date(2026, 10, 14),
        okpd2=["71.12.19.100"],
    )
    base.update(kw)
    return TenderView(**base)  # type: ignore[arg-type]


def test_filter_passes() -> None:
    r = apply_filter(view(), FilterConfig(price_min=Decimal(1000), min_days_to_deadline=3), TODAY)
    assert r.passed and r.reasons == []


def test_filter_reasons() -> None:
    cfg = FilterConfig(
        price_min=Decimal("5000000"),
        stop_words=["газопровод"],
        min_days_to_deadline=30,
        laws=[44],
        msp_only=True,
    )
    r = apply_filter(view(), cfg, TODAY)
    assert not r.passed
    text = " | ".join(r.reasons)
    assert "закон 223" in text
    assert "ниже минимума" in text
    assert "10 дн." in text
    assert "стоп-слово «газопровод»" in text
    assert "МСП" in text
    assert len(r.reasons) == 5


def test_filter_price_max_and_msp_ok() -> None:
    cfg = FilterConfig(price_max=Decimal(1000), msp_only=True)
    r = apply_filter(
        view(method="Запрос котировок для субъектов малого и среднего предпринимательства"),
        cfg,
        TODAY,
    )
    assert r.reasons == ["цена 3000000 выше максимума 1000"]


def test_unknown_price_and_deadline_do_not_filter() -> None:
    cfg = FilterConfig(price_min=Decimal(1), min_days_to_deadline=5)
    assert apply_filter(view(initial_price=None, deadline_at=None), cfg, TODAY).passed


def test_rules_score_and_breakdown() -> None:
    rules = [
        RuleSpec(type="keyword", value="газопровод", weight=3),
        RuleSpec(type="keyword", value="лифт", weight=2),
        RuleSpec(type="okpd2", value="71.12", weight=3),
        RuleSpec(type="customer", value="5032292612", weight=1),
        RuleSpec(type="price_range", value="1000000-5000000", weight=1),
        RuleSpec(type="days_to_deadline", value="7", weight=1),
    ]
    res = score_rules(view(), rules, TODAY)
    assert res.score == pytest.approx(round(9 / 11 * 100, 2))
    matched = [b["matched"] for b in res.breakdown]
    assert matched == [True, False, True, True, True, True]
    assert res.breakdown[1]["points"] == 0


def test_rules_edge_cases() -> None:
    assert score_rules(view(), [], TODAY).score == 0
    neg = [
        RuleSpec(type="keyword", value="газопровод", weight=2),
        RuleSpec(type="customer", value="мособлгаз", weight=-5),
    ]
    assert score_rules(view(), neg, TODAY).score == 0  # штраф уводит в 0, не ниже
    assert (
        score_rules(view(okpd2=[]), [RuleSpec(type="okpd2", value="71", weight=1)], TODAY).score
        == 0
    )
    open_range = RuleSpec(type="price_range", value="2000000..", weight=1)
    assert score_rules(view(), [open_range], TODAY).score == 100


def test_final_rating() -> None:
    assert final_rating(80, None) == 80
    assert final_rating(80, 40) == 60
    assert final_rating(80, 40, ai_weight=0.25) == 70
    assert final_rating(80, 40, ai_weight=0) == 80
    assert final_rating(80, 40, ai_weight=1) == 40
