from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from zakupki_monitor.appsettings import RuntimeSettings, save_runtime
from zakupki_monitor.config import Settings
from zakupki_monitor.db import make_engine, upgrade_db
from zakupki_monitor.models import Profile, Run, Score, Tender
from zakupki_monitor.profiles import ProfileSpec, save_profile
from zakupki_monitor.scheduler import DIGEST_JOB, Runner, make_scheduler, run_now, sync_jobs
from zakupki_monitor.scoring import RuleSpec
from zakupki_monitor.search_url import SearchParams

from .conftest import FakeSite, fixture_text

N8N = Path(__file__).parent.parent / "n8n"


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = Settings(db_path=tmp_path / "s.db", pause_min=0, pause_max=0, backoff_base=0)
    upgrade_db(settings.db_url)
    engine = make_engine(settings.db_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield settings, factory
    engine.dispose()


def add_profile(
    session: Session, name: str, interval: int | None, ai: str | None = None
) -> Profile:
    p = save_profile(
        session,
        ProfileSpec(
            name=name,
            search=SearchParams(search_string="x"),
            rules=[RuleSpec(type="keyword", value="газопровод", weight=1)],
            ai={"criterion": ai, "weight": 0.5},
        ),
    )
    p.interval_minutes = interval
    session.commit()
    return p


def test_jobs_follow_profile_intervals(env) -> None:  # type: ignore[no-untyped-def]
    settings, factory = env
    runner = Runner(settings, factory)
    scheduler = make_scheduler()
    with factory() as s:
        a = add_profile(s, "a", 30)
        b = add_profile(s, "b", None)
    assert sync_jobs(scheduler, runner) == [f"collect-{a.id}"]
    job = scheduler.get_job(f"collect-{a.id}")
    assert job is not None and job.trigger.interval.total_seconds() == 30 * 60
    assert scheduler.get_job(f"collect-{b.id}") is None
    # профиль выключили, другому включили интервал, включили сводку
    with factory() as s:
        s.get(Profile, a.id).interval_minutes = None  # type: ignore[union-attr]
        s.get(Profile, b.id).interval_minutes = 5  # type: ignore[union-attr]
        save_runtime(s, RuntimeSettings(telegram_enabled=True, telegram_hour=7))
        s.commit()
    assert sync_jobs(scheduler, runner) == sorted([f"collect-{b.id}", DIGEST_JOB])
    digest = scheduler.get_job(DIGEST_JOB)
    assert digest is not None and "hour='7'" in str(digest.trigger)
    assert scheduler.get_job(f"collect-{a.id}") is None
    run_now(scheduler, runner, b.id)
    assert scheduler.get_job(f"now-{b.id}") is not None


def test_runner_collects_with_ai_and_single_lock(env) -> None:  # type: ignore[no-untyped-def]
    settings, factory = env
    site = FakeSite({1: fixture_text("search_223.html")})
    runner = Runner(
        settings, factory, eis_transport=httpx.MockTransport(site.handler), sleep=lambda _: None
    )
    with factory() as s:
        p = add_profile(s, "gaz", 60, ai="газопровод")
        save_runtime(s, RuntimeSettings(ai_provider="demo"))
        s.commit()
    stats = runner.collect_profile(p.id)
    assert stats is not None and stats.new == 50 and stats.scored > 0
    with factory() as s:
        assert len(s.scalars(select(Run)).all()) == 1
        ai_scores = [sc.ai_score for sc in s.scalars(select(Score)) if not sc.filtered]
        assert ai_scores and all(v is not None for v in ai_scores)  # провайдер demo отработал
        card = s.scalar(select(Tender).where(Tender.reg_number == "32616430006"))
        assert card is not None and card.okpd2 == ["71.12.19"]  # из «Списка лотов» 223-ФЗ
        assert card.deadline_time is not None
    # пока идёт сбор, второй не стартует
    assert runner.lock.acquire(blocking=False)
    try:
        assert runner.collect_profile(p.id) is None
    finally:
        runner.lock.release()
    assert runner.collect_profile(9999) is None  # профиля нет


def test_digest_job_sends_only_when_enabled(env) -> None:  # type: ignore[no-untyped-def]
    settings, factory = env
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    runner = Runner(
        settings,
        factory,
        telegram_client=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert runner.digest() is False  # сводка выключена
    with factory() as s:
        p = add_profile(s, "gaz", None)
        from zakupki_monitor.collector import evaluate
        from zakupki_monitor.store import upsert_tender

        from .conftest import make_tender_entry

        t = upsert_tender(s, make_tender_entry("32600000009")).tender
        evaluate(s, t, p, date.today())
        save_runtime(
            s, RuntimeSettings(telegram_enabled=True, telegram_token="1:T", telegram_chat_id="9")
        )
        s.commit()
    assert runner.digest() is True and len(sent) == 1
    assert "Проектирование газопровода" in json.loads(sent[0].content)["text"]


@pytest.mark.parametrize("name", ["amocrm", "bitrix24", "universal"])
def test_n8n_templates_are_consistent(name: str) -> None:
    wf = json.loads((N8N / f"{name}.json").read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in wf["nodes"]}
    assert len(nodes) == len(wf["nodes"])  # имена уникальны
    first, last = wf["nodes"][0], wf["nodes"][-1]
    assert first["type"] == "n8n-nodes-base.webhook"
    assert first["parameters"]["httpMethod"] == "POST"
    assert first["parameters"]["responseMode"] == "responseNode"
    assert last["type"] == "n8n-nodes-base.respondToWebhook"
    # цепочка без разрывов от вебхука до ответа
    cur, seen = first["name"], [first["name"]]
    while cur in wf["connections"]:
        cur = wf["connections"][cur]["main"][0][0]["node"]
        assert cur in nodes
        seen.append(cur)
    assert seen == [n["name"] for n in wf["nodes"]]
    body = last["parameters"]["responseBody"]
    assert all(key in body for key in ("crm", "external_id", "url"))
    # в шаблонах нет секретов: у учётных данных пустой id и только имя
    for node in wf["nodes"]:
        for cred in node.get("credentials", {}).values():
            assert cred["id"] == "" and cred["name"]
