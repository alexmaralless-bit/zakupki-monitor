"""Профили поиска: загрузка из YAML/TOML, импорт ссылки, сохранение в БД."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Profile, Rule
from .scoring import FilterConfig, RuleSpec
from .search_url import SearchParams


class AISettings(BaseModel):
    criterion: str | None = None
    weight: float = Field(0.5, ge=0, le=1)


class ProfileSpec(BaseModel):
    name: str
    search: SearchParams | None = None
    url: str | None = None
    max_pages: int = Field(5, ge=1)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    rules: list[RuleSpec] = Field(default_factory=list)
    ai: AISettings = Field(default_factory=AISettings)

    @model_validator(mode="after")
    def _need_search(self) -> ProfileSpec:
        if self.search is None:
            if not self.url:
                raise ValueError("нужен раздел search или поле url со ссылкой поиска")
            self.search = SearchParams.from_url(self.url)
        return self


def load_profile_file(path: Path) -> ProfileSpec:
    raw = path.read_bytes()
    data: dict[str, Any]
    if path.suffix.lower() == ".toml":
        data = tomllib.loads(raw.decode("utf-8"))
    else:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
    return ProfileSpec.model_validate(data)


class ProfileExistsError(Exception):
    pass


def save_profile(session: Session, spec: ProfileSpec, replace: bool = False) -> Profile:
    assert spec.search is not None
    existing = session.scalar(select(Profile).where(Profile.name == spec.name))
    if existing is not None:
        if not replace:
            raise ProfileExistsError(spec.name)
        session.delete(existing)
        session.flush()
    profile = Profile(
        name=spec.name,
        search=spec.search.model_dump(mode="json"),
        filter=spec.filter.model_dump(mode="json"),
        max_pages=spec.max_pages,
        ai_criterion=spec.ai.criterion,
        ai_weight=spec.ai.weight,
        rules=[Rule(type=r.type, value=r.value, weight=r.weight) for r in spec.rules],
    )
    session.add(profile)
    session.flush()
    return profile


def import_url(session: Session, url: str, name: str, max_pages: int = 5) -> Profile:
    spec = ProfileSpec(name=name, url=url, max_pages=max_pages)
    return save_profile(session, spec)


def profile_search(profile: Profile) -> SearchParams:
    return SearchParams.model_validate(profile.search)


def profile_filter(profile: Profile) -> FilterConfig:
    return FilterConfig.model_validate(profile.filter)


def profile_rules(profile: Profile) -> list[RuleSpec]:
    return [RuleSpec(type=r.type, value=r.value, weight=r.weight) for r in profile.rules]
