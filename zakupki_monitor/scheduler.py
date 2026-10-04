"""Планировщик внутри процесса веб-приложения: сбор по интервалам профилей и суточная сводка."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .ai import build_scorer
from .appsettings import load_runtime
from .collector import Fetcher, RunStats, collect, make_client
from .config import Settings
from .models import Profile
from .notify import TelegramError, send_digest

log = logging.getLogger(__name__)

COLLECT_PREFIX = "collect-"
DIGEST_JOB = "digest"


class Runner:
    """Выполняет задания. Один общий замок: за раз идёт один сбор (один поток запросов к ЕИС)."""

    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        eis_transport: httpx.BaseTransport | None = None,
        ai_transport: httpx.BaseTransport | None = None,
        telegram_client: Callable[[], httpx.Client] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.eis_transport = eis_transport
        self.ai_transport = ai_transport
        self.telegram_client = telegram_client
        self.sleep = sleep
        self.lock = threading.Lock()

    def collect_profile(self, profile_id: int) -> RunStats | None:
        if not self.lock.acquire(blocking=False):
            log.info("сбор уже идёт, пропускаю профиль %s", profile_id)
            return None
        try:
            with self.factory() as session:
                profile = session.get(Profile, profile_id)
                if profile is None:
                    return None
                rt = load_runtime(session, self.settings)
                ai = (
                    build_scorer(session, rt.ai_provider, self.settings, self.ai_transport)
                    if profile.ai_criterion
                    else None
                )
                with make_client(self.settings, self.eis_transport) as client:
                    fetcher = Fetcher(client, self.settings, sleep=self.sleep)
                    stats = collect(session, profile, fetcher, ai=ai)
                session.commit()
                return stats
        except Exception:
            log.exception("сбор по профилю %s завершился ошибкой", profile_id)
            return None
        finally:
            self.lock.release()

    def digest(self) -> bool:
        with self.factory() as session:
            rt = load_runtime(session, self.settings)
            if not rt.telegram_enabled:
                return False
            client = self.telegram_client() if self.telegram_client else None
            try:
                return send_digest(
                    session, rt, client, today=date.today(), proxy=self.settings.proxy
                )
            except TelegramError as exc:
                log.warning("сводка в Telegram не отправлена: %s", exc)
                return False


def make_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    )


def sync_jobs(scheduler: BackgroundScheduler, runner: Runner) -> list[str]:
    """Приводит задания в соответствие с БД: интервалы профилей и сводка. Возвращает id заданий."""
    with runner.factory() as session:
        profiles = list(session.scalars(select(Profile).order_by(Profile.id)))
        rt = load_runtime(session, runner.settings)
    wanted: dict[str, tuple[Callable[..., object], object, tuple[object, ...]]] = {}
    for p in profiles:
        if p.interval_minutes:
            wanted[f"{COLLECT_PREFIX}{p.id}"] = (
                runner.collect_profile,
                IntervalTrigger(minutes=p.interval_minutes),
                (p.id,),
            )
    if rt.telegram_enabled:
        wanted[DIGEST_JOB] = (runner.digest, CronTrigger(hour=rt.telegram_hour), ())

    for job in scheduler.get_jobs():
        if job.id not in wanted:
            scheduler.remove_job(job.id)
    for job_id, (func, trigger, args) in wanted.items():
        scheduler.add_job(func, trigger, args=args, id=job_id, replace_existing=True)
    return sorted(wanted)


def run_now(scheduler: BackgroundScheduler, runner: Runner, profile_id: int) -> None:
    """Разовый запуск сбора в фоне (кнопка «Собрать сейчас»)."""
    scheduler.add_job(
        runner.collect_profile, args=(profile_id,), id=f"now-{profile_id}", replace_existing=True
    )
