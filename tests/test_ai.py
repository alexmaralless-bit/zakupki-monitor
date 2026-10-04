from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from zakupki_monitor.ai import (
    SYSTEM_PROMPT,
    AIError,
    AIService,
    DemoProvider,
    ProviderConfigError,
    make_provider,
    normalize_ws,
    parse_model_output,
    quotes_verified,
)
from zakupki_monitor.collector import evaluate
from zakupki_monitor.config import Settings
from zakupki_monitor.models import AICache
from zakupki_monitor.profiles import ProfileSpec, save_profile
from zakupki_monitor.scoring import RuleSpec, TenderView, final_rating
from zakupki_monitor.search_url import SearchParams
from zakupki_monitor.store import upsert_tender

from .conftest import make_tender_entry

TEXT = "Объект закупки: Проектирование газопровода\nвысокого  давления\nЗаказчик: АО МОСОБЛГАЗ"


class FakeProvider:
    """Заглушка модели: отдаёт заранее заданный ответ и считает вызовы."""

    name = "fake"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        assert "КРИТЕРИЙ" in user and "ТЕКСТ ЗАКУПКИ" in user
        return self.reply


def reply(score=80, quotes=None, **kw) -> str:  # type: ignore[no-untyped-def]
    return json.dumps(
        {
            "score": score,
            "verdict": kw.get("verdict", "Подходит"),
            "reasons": ["газопровод"],
            "quotes": quotes if quotes is not None else ["газопровода высокого давления"],
        },
        ensure_ascii=False,
    )


def view(text: str = TEXT, reg: str = "32600000001") -> TenderView:
    return TenderView(object="Проектирование газопровода", reg_number=reg, text=text)


def test_parse_strict_json_and_fences() -> None:
    v = parse_model_output(
        '```json\n{"score": 55, "verdict": "ок", "reasons": [], "quotes": ["а"]}\n```'
    )
    assert v.score == 55 and v.quotes == ["а"]
    for bad in (
        "просто текст",
        '{"score": 140, "verdict": "x"}',
        '{"verdict": "нет оценки"}',
        "{oops}",
    ):
        with pytest.raises(AIError):
            parse_model_output(bad)


def test_quote_must_be_verbatim_modulo_whitespace() -> None:
    assert quotes_verified(
        ["газопровода высокого давления"], TEXT
    )  # перенос строки и двойной пробел
    assert not quotes_verified(["газопровода низкого давления"], TEXT)
    assert not quotes_verified([], TEXT)  # без цитаты балла нет
    assert not quotes_verified(["газопровод", " "], TEXT)
    assert normalize_ws("а\xa0 б\n\nв") == "а б в"


def test_service_scores_and_caches(session) -> None:  # type: ignore[no-untyped-def]
    fake = FakeProvider(reply(80))
    service = AIService(session, fake)
    first = service.score(view(), "газопроводы")
    assert first and first.score == 80 and first.quotes_ok and not first.cached
    again = service.score(view(), "газопроводы")
    assert again and again.cached and again.score == 80
    assert fake.calls == 1  # второй раз из кеша
    assert len(session.scalars(select(AICache)).all()) == 1
    # другой критерий или другой текст - новый вызов
    service.score(view(), "уборка")
    service.score(view(TEXT + "\nещё строка"), "газопроводы")
    assert fake.calls == 3


def test_invented_quote_zeroes_score(session) -> None:  # type: ignore[no-untyped-def]
    service = AIService(session, FakeProvider(reply(95, quotes=["цитаты нет в тексте"])))
    out = service.score(view(), "газопроводы")
    assert out and out.score == 0 and not out.quotes_ok
    assert out.error and "цитата" in out.error


def test_provider_garbage_gives_no_score(session) -> None:  # type: ignore[no-untyped-def]
    out = AIService(session, FakeProvider("я не умею в JSON")).score(view(), "x")
    assert out and out.score is None and out.error
    assert not session.scalars(select(AICache)).all()  # мусор не кешируется


def test_final_rating_formula_with_ai(session) -> None:  # type: ignore[no-untyped-def]
    assert final_rating(60, 100, 0.5) == 80
    assert final_rating(60, 0, 0.5) == 30  # обнулённый ИИ тянет рейтинг вниз
    assert final_rating(60, None, 0.5) == 60
    spec = ProfileSpec(
        name="p",
        search=SearchParams(search_string="x"),
        rules=[RuleSpec(type="keyword", value="газопровод", weight=1)],
        ai={"criterion": "газопроводы", "weight": 0.25},
    )
    profile = save_profile(session, spec)
    tender = upsert_tender(session, make_tender_entry()).tender
    ai = AIService(session, FakeProvider(reply(40, quotes=["Проектирование газопровода"])))
    score = evaluate(session, tender, profile, date(2026, 10, 4), ai)
    assert score.rules_score == 100 and score.ai_score == 40
    assert score.rating == 85  # 0.75 * 100 + 0.25 * 40
    assert score.breakdown and score.breakdown["ai"]["quotes_ok"] is True


def test_provider_requests_use_documented_shapes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "yandex" in request.url.host:
            return httpx.Response(
                200, json={"result": {"alternatives": [{"message": {"text": "yandex-ok"}}]}}
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "openai-ok"}}]})

    transport = httpx.MockTransport(handler)
    s = Settings(
        yandex_api_key="K", yandex_folder_id="F", openai_api_key="O", local_base_url="http://x/v1"
    )
    ya = make_provider("yandex", s, transport)
    assert ya.complete(SYSTEM_PROMPT, "u") == "yandex-ok"
    req = seen[-1]
    assert req.headers["authorization"] == "Api-Key K" and req.headers["x-folder-id"] == "F"
    assert json.loads(req.content)["modelUri"] == "gpt://F/yandexgpt/latest"
    oa = make_provider("openai", s, transport)
    assert oa.complete(SYSTEM_PROMPT, "u") == "openai-ok"
    assert seen[-1].headers["authorization"] == "Bearer O"
    assert json.loads(seen[-1].content)["response_format"] == {"type": "json_object"}
    local = make_provider("local", s, transport)
    assert local.complete("s", "u") == "openai-ok"
    assert str(seen[-1].url) == "http://x/v1/chat/completions"
    assert "authorization" not in seen[-1].headers


def test_missing_keys_and_unknown_provider() -> None:
    empty = Settings(yandex_api_key="", openai_api_key="", gigachat_auth_key="")
    for name in ("yandex", "openai", "gigachat"):
        with pytest.raises(ProviderConfigError):
            make_provider(name, empty)
    with pytest.raises(ProviderConfigError):
        make_provider("nope", empty)


def test_demo_provider_quotes_are_verbatim(session) -> None:  # type: ignore[no-untyped-def]
    out = AIService(session, DemoProvider()).score(view(), "Проектирование газопроводов")
    assert out and out.quotes_ok and out.score is not None and out.score >= 60
    assert out.verdict.startswith("[демо]")
