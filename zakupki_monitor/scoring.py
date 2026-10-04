"""Рейтинг закупки по правилам профиля (спецификация, раздел 3).

Слои: отсев (с причинами) -> баллы по правилам (0-100 с разбивкой) -> ИИ (заглушка этапа 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

RuleType = Literal["keyword", "okpd2", "customer", "price_range", "days_to_deadline"]
RULE_TYPES: tuple[str, ...] = ("keyword", "okpd2", "customer", "price_range", "days_to_deadline")


class FilterConfig(BaseModel):
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    min_days_to_deadline: int | None = None
    stop_words: list[str] = Field(default_factory=list)
    msp_only: bool = False
    laws: list[int] = Field(default_factory=lambda: [44, 223])


class RuleSpec(BaseModel):
    type: RuleType
    value: str
    weight: float


@dataclass
class TenderView:
    """Поля закупки, нужные для оценки (не зависит от ORM)."""

    object: str
    law: int | None = None
    method: str | None = None
    customer_name: str | None = None
    customer_inn: str | None = None
    initial_price: Decimal | None = None
    deadline_at: date | None = None
    okpd2: list[str] = field(default_factory=list)


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str]


@dataclass
class RulesResult:
    score: float  # 0..100
    breakdown: list[dict[str, Any]]


def days_to_deadline(view: TenderView, today: date) -> int | None:
    return (view.deadline_at - today).days if view.deadline_at else None


def apply_filter(view: TenderView, cfg: FilterConfig, today: date) -> FilterResult:
    reasons: list[str] = []
    if view.law is not None and cfg.laws and view.law not in cfg.laws:
        reasons.append(f"закон {view.law}-ФЗ не в списке {cfg.laws}")
    price = view.initial_price
    if price is not None:
        if cfg.price_min is not None and price < cfg.price_min:
            reasons.append(f"цена {price} ниже минимума {cfg.price_min}")
        if cfg.price_max is not None and price > cfg.price_max:
            reasons.append(f"цена {price} выше максимума {cfg.price_max}")
    if cfg.min_days_to_deadline is not None:
        days = days_to_deadline(view, today)
        if days is not None and days < cfg.min_days_to_deadline:
            reasons.append(
                f"до окончания подачи {days} дн., нужно не меньше {cfg.min_days_to_deadline}"
            )
    obj = view.object.lower()
    for word in cfg.stop_words:
        if word.strip() and word.strip().lower() in obj:
            reasons.append(f"стоп-слово «{word}» в объекте закупки")
    if cfg.msp_only and "малого и среднего" not in (view.method or "").lower():
        reasons.append("закупка не только для МСП")
    return FilterResult(passed=not reasons, reasons=reasons)


def _parse_range(value: str) -> tuple[Decimal | None, Decimal | None]:
    """'100000-5000000', '100000..', '..5000000' -> (от, до)."""
    sep = ".." if ".." in value else "-"
    left, _, right = value.partition(sep)

    def num(s: str) -> Decimal | None:
        s = s.strip().replace(" ", "").replace(",", ".")
        if not s:
            return None
        try:
            return Decimal(s)
        except InvalidOperation:
            return None

    return num(left), num(right)


def rule_matches(rule: RuleSpec, view: TenderView, today: date) -> bool:
    value = rule.value.strip()
    if rule.type == "keyword":
        return value.lower() in view.object.lower()
    if rule.type == "okpd2":
        return any(code.startswith(value) for code in view.okpd2)
    if rule.type == "customer":
        if value.isdigit():
            return view.customer_inn == value
        return value.lower() in (view.customer_name or "").lower()
    if rule.type == "price_range":
        if view.initial_price is None:
            return False
        lo, hi = _parse_range(value)
        return (lo is None or view.initial_price >= lo) and (hi is None or view.initial_price <= hi)
    if rule.type == "days_to_deadline":
        days = days_to_deadline(view, today)
        try:
            return days is not None and days >= int(value)
        except ValueError:
            return False
    return False


def score_rules(view: TenderView, rules: list[RuleSpec], today: date) -> RulesResult:
    """Сумма весов сработавших правил, нормированная на сумму положительных весов (0..100)."""
    breakdown: list[dict[str, Any]] = []
    total = 0.0
    for rule in rules:
        matched = rule_matches(rule, view, today)
        points = rule.weight if matched else 0.0
        total += points
        breakdown.append(
            {
                "type": rule.type,
                "value": rule.value,
                "weight": rule.weight,
                "matched": matched,
                "points": points,
            }
        )
    max_points = sum(r.weight for r in rules if r.weight > 0)
    score = 0.0 if max_points <= 0 else max(0.0, min(100.0, total / max_points * 100))
    return RulesResult(score=round(score, 2), breakdown=breakdown)


class AIScorer(Protocol):
    def score(self, view: TenderView, criterion: str) -> float | None: ...


class NullAIScorer:
    """Этап 1: ИИ-слой выключен. Настоящие провайдеры появятся на этапе 2."""

    def score(self, view: TenderView, criterion: str) -> float | None:
        return None


def final_rating(rules_score: float, ai_score: float | None, ai_weight: float = 0.5) -> float:
    """(1 - w) * правила + w * ИИ; без оценки ИИ - только правила."""
    if ai_score is None:
        return round(rules_score, 2)
    w = max(0.0, min(1.0, ai_weight))
    return round((1 - w) * rules_score + w * ai_score, 2)
