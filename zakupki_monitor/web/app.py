"""Веб-интерфейс реестра: FastAPI + Jinja2 + HTMX. Слушает только 127.0.0.1."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import __version__
from ..ai import PROVIDER_NAMES, PROVIDER_TITLES, ProviderConfigError, build_scorer, make_provider
from ..appsettings import RuntimeSettings, load_runtime, save_runtime
from ..collector import evaluate, rescore_profile
from ..config import Settings, get_settings
from ..crm import CrmError, send_to_crm
from ..db import make_engine, upgrade_db
from ..models import Profile, Rule, Run, Score, Tender
from ..notify import URGENT_DAYS, TelegramError, build_digest, send_telegram
from ..profiles import (
    ProfileExistsError,
    ProfileSpec,
    import_url,
    profile_filter,
    profile_search,
)
from ..scheduler import Runner, make_scheduler, run_now, sync_jobs
from ..scoring import RULE_TYPES, FilterConfig
from ..search_url import SearchParams
from .excel import build_workbook
from .queries import PAGE_SIZE, STATUSES, RegistryFilter, registry_rows

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]", "testserver"]

RULE_TYPE_TITLES = {
    "keyword": "Слово или фраза в объекте",
    "okpd2": "ОКПД2 (префикс кода)",
    "customer": "Заказчик (часть названия или ИНН)",
    "price_range": "Цена в диапазоне (от-до)",
    "days_to_deadline": "Дней до окончания подачи не меньше",
}


def _fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def _fmt_date(value: date | datetime | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "-"


def _fmt_time(value: time | None) -> str:
    return value.strftime("%H:%M") if value else ""


def _fmt_dt(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "-"


def _rating_class(value: float | None) -> str:
    if value is None:
        return "r-none"
    return "r-high" if value >= 70 else "r-mid" if value >= 40 else "r-low"


def deadline_info(deadline: date | None, today: date) -> tuple[int | None, str]:
    """(дней до окончания подачи, css-класс): срочно < 3 дней, просрочено < 0."""
    if deadline is None:
        return None, ""
    days = (deadline - today).days
    if days < 0:
        return days, "dl-past"
    return days, "dl-urgent" if days < URGENT_DAYS else ""


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.factory
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


Db = Annotated[Session, Depends(get_session)]


def create_app(
    settings: Settings | None = None,
    *,
    start_scheduler: bool = True,
    runner: Runner | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    upgrade_db(settings.db_url)
    engine = make_engine(settings.db_url)
    factory = sessionmaker(engine, expire_on_commit=False)
    runner = runner or Runner(settings, factory)
    scheduler = make_scheduler()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        sync_jobs(scheduler, runner)
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            engine.dispose()

    app = FastAPI(title="Монитор госзакупок", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.state.settings = settings
    app.state.factory = factory
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    templates.env.filters.update(
        money=_fmt_money, d=_fmt_date, t=_fmt_time, dt=_fmt_dt, rclass=_rating_class
    )

    @app.middleware("http")
    async def same_origin(request: Request, call_next: Any) -> Response:
        """Запись данных только со страниц самого приложения (защита от чужих вкладок)."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
                "testserver",
            ):
                return Response("Запрещено: чужой источник запроса", status_code=403)
        response: Response = await call_next(request)
        return response

    def render(
        request: Request,
        session: Session,
        name: str,
        *,
        status_code: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        rt = load_runtime(session, settings)
        base = {
            "demo": rt.ai_provider == "demo",
            "version": __version__,
            "statuses": STATUSES,
            "today": date.today(),
            "path": request.url.path,
        }
        return templates.TemplateResponse(
            request, name, {**base, **context}, status_code=status_code
        )

    def tender_or_404(session: Session, tender_id: int) -> Tender:
        tender = session.get(Tender, tender_id)
        if tender is None:
            raise HTTPException(404, "Закупка не найдена")
        return tender

    def row_context(tender: Tender, score: Score | None, profile: Profile | None) -> dict[str, Any]:
        days, css = deadline_info(tender.deadline_at, date.today())
        return {"t": tender, "s": score, "p": profile, "days": days, "dl_class": css}

    # ------------------------------------------------------------------ реестр

    @app.get("/", response_class=HTMLResponse)
    def registry(request: Request, session: Db) -> HTMLResponse:
        f = RegistryFilter.from_params(dict(request.query_params))
        rows, total = registry_rows(session, f, date.today())
        pages = max(1, -(-total // PAGE_SIZE))
        ctx: dict[str, Any] = {
            "f": f,
            "rows": [row_context(t, s, p) for t, s, p in rows],
            "total": total,
            "pages": pages,
            "profiles": session.scalars(select(Profile).order_by(Profile.name)).all(),
            "urgent_days": URGENT_DAYS,
        }
        name = "_registry.html" if request.headers.get("HX-Request") == "true" else "registry.html"
        if request.headers.get("HX-Boosted") == "true":
            name = "registry.html"
        return render(request, session, name, **ctx)

    @app.get("/export.xlsx")
    def export_xlsx(request: Request, session: Db) -> Response:
        f = RegistryFilter.from_params(dict(request.query_params))
        rows, _ = registry_rows(session, f, date.today(), paged=False)
        name = f"zakupki-{date.today():%Y-%m-%d}.xlsx"
        return Response(
            build_workbook(rows),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        )

    # ------------------------------------------------------------------ карточка

    def card_context(session: Session, tender: Tender, profile_id: int | None) -> dict[str, Any]:
        scored = session.execute(
            select(Score, Profile)
            .join(Profile, Profile.id == Score.profile_id)
            .where(Score.tender_id == tender.id)
            .order_by(Score.rating.desc().nulls_last())
        ).all()
        chosen = next((x for x in scored if x[1].id == profile_id), scored[0] if scored else None)
        score, profile = (chosen[0], chosen[1]) if chosen else (None, None)
        days, css = deadline_info(tender.deadline_at, date.today())
        rt = load_runtime(session, settings)
        return {
            "t": tender,
            "score": score,
            "profile": profile,
            "all_scores": scored,
            "breakdown": (score.breakdown or {}) if score else {},
            "days": days,
            "dl_class": css,
            "webhook_set": bool(rt.webhook_url),
            "export_error": None,
            "rule_types": RULE_TYPE_TITLES,
        }

    @app.get("/tenders/{tender_id}", response_class=HTMLResponse)
    def tender_card(
        request: Request, tender_id: int, session: Db, profile: int | None = None
    ) -> HTMLResponse:
        tender = tender_or_404(session, tender_id)
        return render(request, session, "tender.html", **card_context(session, tender, profile))

    @app.post("/tenders/{tender_id}/status", response_class=HTMLResponse)
    def set_status(
        request: Request,
        tender_id: int,
        session: Db,
        status: Annotated[str, Form()],
        profile_id: Annotated[int | None, Form()] = None,
        view: Annotated[str, Form()] = "row",
    ) -> Response:
        tender = tender_or_404(session, tender_id)
        if status not in STATUSES:
            raise HTTPException(400, "Неизвестный статус")
        tender.status = status
        session.flush()
        if view == "card":
            return render(
                request, session, "_actions.html", **card_context(session, tender, profile_id)
            )
        score = session.scalar(
            select(Score).where(Score.tender_id == tender.id, Score.profile_id == profile_id)
        )
        profile = session.get(Profile, profile_id) if profile_id else None
        return render(request, session, "_row.html", **row_context(tender, score, profile))

    @app.post("/tenders/{tender_id}/note", response_class=HTMLResponse)
    def save_note(tender_id: int, session: Db, note: Annotated[str, Form()] = "") -> HTMLResponse:
        tender_or_404(session, tender_id).note = note.strip() or None
        return HTMLResponse('<span class="saved">Заметка сохранена</span>')

    @app.post("/tenders/{tender_id}/crm", response_class=HTMLResponse)
    def to_crm(
        request: Request,
        tender_id: int,
        session: Db,
        profile_id: Annotated[int, Form()],
    ) -> HTMLResponse:
        tender = tender_or_404(session, tender_id)
        profile = session.get(Profile, profile_id)
        if profile is None:
            raise HTTPException(404, "Профиль не найден")
        rt = load_runtime(session, settings)
        ctx = card_context(session, tender, profile_id)
        try:
            send_to_crm(session, tender, profile, rt, client=app.state.crm_client_factory())
        except CrmError as exc:
            ctx["export_error"] = str(exc)
        return render(request, session, "_actions.html", **ctx)

    app.state.crm_client_factory = lambda: None

    @app.post("/tenders/{tender_id}/rescore")
    def rescore_one(
        tender_id: int, session: Db, profile_id: Annotated[int, Form()]
    ) -> RedirectResponse:
        tender = tender_or_404(session, tender_id)
        profile = session.get(Profile, profile_id)
        if profile is None:
            raise HTTPException(404, "Профиль не найден")
        rt = load_runtime(session, settings)
        ai = (
            build_scorer(session, rt.ai_provider, settings, app.state.runner.ai_transport)
            if profile.ai_criterion
            else None
        )
        evaluate(session, tender, profile, date.today(), ai)
        return RedirectResponse(f"/tenders/{tender_id}?profile={profile_id}", status_code=303)

    # ------------------------------------------------------------------ профили

    def profile_form_context(profile: Profile | None) -> dict[str, Any]:
        search = profile_search(profile) if profile else SearchParams()
        flt = profile_filter(profile) if profile else FilterConfig()
        return {
            "profile": profile,
            "search": search,
            "flt": flt,
            "rule_types": RULE_TYPE_TITLES,
            "rules": profile.rules if profile else [],
            "error": None,
        }

    def parse_profile_form(form: Any, profile: Profile | None) -> dict[str, Any]:
        def text(key: str) -> str:
            return str(form.get(key) or "").strip()

        def dec(key: str) -> Decimal | None:
            raw = text(key).replace(" ", "").replace(",", ".")
            if not raw:
                return None
            try:
                return Decimal(raw)
            except InvalidOperation:
                raise ValueError(f"Поле «{key}»: ожидается число") from None

        def integer(key: str) -> int | None:
            raw = text(key)
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"Поле «{key}»: ожидается целое число") from None

        def words(key: str) -> list[str]:
            return [w.strip() for w in text(key).replace("\n", ",").split(",") if w.strip()]

        name = text("name")
        if not name:
            raise ValueError("Укажите название профиля")
        old_search = profile_search(profile) if profile else SearchParams()
        search = old_search.model_copy(
            update={
                "search_string": text("search_string"),
                "morphology": "morphology" in form,
                "fz44": "fz44" in form,
                "fz223": "fz223" in form,
                "af": "af" in form,
                "okpd2_codes": words("okpd2_codes"),
                "price_from": dec("price_from"),
                "price_to": dec("price_to"),
            }
        )
        laws = [n for n, key in ((44, "law44"), (223, "law223")) if key in form]
        flt = FilterConfig(
            price_min=dec("price_min"),
            price_max=dec("price_max"),
            min_days_to_deadline=integer("min_days"),
            stop_words=words("stop_words"),
            msp_only="msp_only" in form,
            laws=laws or [44, 223],
        )
        weight_pct = dec("ai_weight") if text("ai_weight") else Decimal(50)
        interval = integer("interval_minutes")
        return {
            "name": name,
            "search": search.model_dump(mode="json"),
            "filter": flt.model_dump(mode="json"),
            "max_pages": max(1, integer("max_pages") or 5),
            "ai_criterion": text("ai_criterion") or None,
            "ai_weight": float(max(Decimal(0), min(Decimal(100), weight_pct or Decimal(0)))) / 100,
            "interval_minutes": interval if interval and interval > 0 else None,
        }

    @app.get("/profiles", response_class=HTMLResponse)
    def profiles_page(request: Request, session: Db, msg: str = "") -> HTMLResponse:
        profiles = session.scalars(select(Profile).order_by(Profile.id)).all()
        counts = dict(
            session.execute(
                select(Score.profile_id, func.count())
                .where(Score.filtered.is_(False))
                .group_by(Score.profile_id)
            ).all()
        )
        return render(
            request, session, "profiles.html", profiles=profiles, counts=counts, msg=msg, error=None
        )

    @app.post("/profiles/import", response_class=HTMLResponse)
    def profiles_import(
        request: Request,
        session: Db,
        url: Annotated[str, Form()],
        name: Annotated[str, Form()],
    ) -> Response:
        try:
            profile = import_url(session, url.strip(), name.strip() or "Новый профиль")
        except ProfileExistsError:
            return render(
                request,
                session,
                "profiles.html",
                status_code=400,
                profiles=session.scalars(select(Profile).order_by(Profile.id)).all(),
                counts={},
                msg="",
                error=f"Профиль «{name}» уже есть",
            )
        except ValueError as exc:
            return render(
                request,
                session,
                "profiles.html",
                status_code=400,
                profiles=session.scalars(select(Profile).order_by(Profile.id)).all(),
                counts={},
                msg="",
                error=str(exc),
            )
        return RedirectResponse(f"/profiles/{profile.id}", status_code=303)

    @app.get("/profiles/new", response_class=HTMLResponse)
    def profile_new(request: Request, session: Db) -> HTMLResponse:
        return render(request, session, "profile_form.html", **profile_form_context(None))

    @app.post("/profiles/new", response_class=HTMLResponse)
    async def profile_create(request: Request, session: Db) -> Response:
        form = await request.form()
        try:
            data = parse_profile_form(form, None)
            ProfileSpec.model_validate(
                {"name": data["name"], "search": data["search"], "filter": data["filter"]}
            )
        except ValueError as exc:
            ctx = profile_form_context(None)
            ctx["error"] = str(exc)
            return render(request, session, "profile_form.html", status_code=400, **ctx)
        if session.scalar(select(Profile).where(Profile.name == data["name"])):
            ctx = profile_form_context(None)
            ctx["error"] = f"Профиль «{data['name']}» уже есть"
            return render(request, session, "profile_form.html", status_code=400, **ctx)
        profile = Profile(**data)
        session.add(profile)
        session.flush()
        sync_jobs(scheduler, runner)
        return RedirectResponse(f"/profiles/{profile.id}?saved=1", status_code=303)

    def profile_or_404(session: Session, profile_id: int) -> Profile:
        profile = session.get(Profile, profile_id)
        if profile is None:
            raise HTTPException(404, "Профиль не найден")
        return profile

    @app.get("/profiles/{profile_id}", response_class=HTMLResponse)
    def profile_edit(
        request: Request, profile_id: int, session: Db, saved: int = 0, msg: str = ""
    ) -> HTMLResponse:
        ctx = profile_form_context(profile_or_404(session, profile_id))
        ctx.update(saved=bool(saved), msg=msg)
        return render(request, session, "profile_form.html", **ctx)

    @app.post("/profiles/{profile_id}", response_class=HTMLResponse)
    async def profile_save(request: Request, profile_id: int, session: Db) -> Response:
        profile = profile_or_404(session, profile_id)
        form = await request.form()
        try:
            data = parse_profile_form(form, profile)
            clash = session.scalar(
                select(Profile).where(Profile.name == data["name"], Profile.id != profile.id)
            )
            if clash:
                raise ValueError(f"Профиль «{data['name']}» уже есть")
        except ValueError as exc:
            ctx = profile_form_context(profile)
            ctx["error"] = str(exc)
            return render(request, session, "profile_form.html", status_code=400, **ctx)
        for key, value in data.items():
            setattr(profile, key, value)
        session.flush()
        sync_jobs(scheduler, runner)
        return RedirectResponse(f"/profiles/{profile_id}?saved=1", status_code=303)

    @app.post("/profiles/{profile_id}/delete")
    def profile_delete(profile_id: int, session: Db) -> RedirectResponse:
        session.delete(profile_or_404(session, profile_id))
        session.flush()
        sync_jobs(scheduler, runner)
        return RedirectResponse("/profiles?msg=Профиль+удалён", status_code=303)

    @app.post("/profiles/{profile_id}/run")
    def profile_run(profile_id: int, session: Db) -> RedirectResponse:
        profile_or_404(session, profile_id)
        run_now(scheduler, runner, profile_id)
        if not scheduler.running:
            return RedirectResponse("/profiles?msg=Планировщик+не+запущен", status_code=303)
        return RedirectResponse(
            "/profiles?msg=Сбор+запущен,+результат+в+журнале+прогонов", status_code=303
        )

    @app.post("/profiles/{profile_id}/rescore")
    def profile_rescore(profile_id: int, session: Db) -> RedirectResponse:
        profile = profile_or_404(session, profile_id)
        rt = load_runtime(session, settings)
        ai = (
            build_scorer(session, rt.ai_provider, settings, runner.ai_transport)
            if profile.ai_criterion
            else None
        )
        n = rescore_profile(session, profile, date.today(), ai)
        return RedirectResponse(
            f"/profiles/{profile_id}?msg=Пересчитано+закупок:+{n}", status_code=303
        )

    def rules_partial(request: Request, session: Session, profile: Profile) -> HTMLResponse:
        return render(
            request,
            session,
            "_rules.html",
            profile=profile,
            rules=profile.rules,
            rule_types=RULE_TYPE_TITLES,
            rule_error=None,
        )

    @app.post("/profiles/{profile_id}/rules", response_class=HTMLResponse)
    def rule_add(
        request: Request,
        profile_id: int,
        session: Db,
        type: Annotated[str, Form()],
        value: Annotated[str, Form()],
        weight: Annotated[str, Form()],
    ) -> HTMLResponse:
        profile = profile_or_404(session, profile_id)
        error = None
        try:
            w = float(weight.replace(",", "."))
        except ValueError:
            w, error = 0.0, "Вес должен быть числом"
        if type not in RULE_TYPES:
            error = "Неизвестный тип правила"
        elif not value.strip():
            error = "Укажите значение правила"
        if error is None:
            profile.rules.append(Rule(type=type, value=value.strip(), weight=w))
            session.flush()
        return render(
            request,
            session,
            "_rules.html",
            profile=profile,
            rules=profile.rules,
            rule_types=RULE_TYPE_TITLES,
            rule_error=error,
        )

    @app.post("/profiles/{profile_id}/rules/{rule_id}/delete", response_class=HTMLResponse)
    def rule_delete(request: Request, profile_id: int, rule_id: int, session: Db) -> HTMLResponse:
        profile = profile_or_404(session, profile_id)
        rule = session.get(Rule, rule_id)
        if rule is not None and rule.profile_id == profile.id:
            profile.rules.remove(rule)
            session.flush()
        return rules_partial(request, session, profile)

    # ------------------------------------------------------------------ настройки

    def settings_context(session: Session, **extra: Any) -> dict[str, Any]:
        rt = load_runtime(session, settings)
        status: dict[str, str] = {}
        for name in PROVIDER_NAMES:
            try:
                make_provider(name, settings)
                status[name] = "готов"
            except ProviderConfigError as exc:
                status[name] = str(exc)
        return {
            "rt": rt,
            "providers": {n: PROVIDER_TITLES[n] for n in PROVIDER_NAMES},
            "provider_status": status,
            "msg": "",
            "error": None,
            **extra,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, session: Db, msg: str = "") -> HTMLResponse:
        return render(request, session, "settings.html", **settings_context(session, msg=msg))

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request, session: Db) -> Response:
        form = await request.form()
        old = load_runtime(session, settings)

        def text(key: str, keep: str = "") -> str:
            raw = str(form.get(key) or "").strip()
            return raw or keep

        provider = text("ai_provider", old.ai_provider)
        try:
            new = RuntimeSettings(
                ai_provider=provider if provider in PROVIDER_NAMES else old.ai_provider,
                webhook_url=text("webhook_url"),
                webhook_header=text("webhook_header", "X-Webhook-Secret"),
                # пустое поле секрета оставляет прежнее значение; снять секрет - флажок
                webhook_secret=""
                if "clear_webhook_secret" in form
                else text("webhook_secret", old.webhook_secret),
                telegram_enabled="telegram_enabled" in form,
                telegram_token=text("telegram_token", old.telegram_token),
                telegram_chat_id=text("telegram_chat_id"),
                telegram_threshold=float(text("telegram_threshold", "70").replace(",", ".")),
                telegram_hour=int(text("telegram_hour", "9")),
            )
            if new.webhook_url and not new.webhook_url.startswith(("http://", "https://")):
                raise ValueError("Адрес вебхука должен начинаться с http:// или https://")
        except ValueError as exc:
            return render(
                request,
                session,
                "settings.html",
                status_code=400,
                **settings_context(session, error=f"Не удалось сохранить: {exc}"),
            )
        save_runtime(session, new)
        session.commit()
        sync_jobs(scheduler, runner)
        return RedirectResponse("/settings?msg=Настройки+сохранены", status_code=303)

    @app.post("/settings/telegram-test", response_class=HTMLResponse)
    def telegram_test(request: Request, session: Db) -> Response:
        rt = load_runtime(session, settings)
        text = build_digest(session, rt) or "Монитор госзакупок: проверка связи. Новых закупок нет."
        try:
            send_telegram(rt, text, app.state.telegram_client_factory(), settings.proxy)
        except TelegramError as exc:
            return render(
                request,
                session,
                "settings.html",
                status_code=400,
                **settings_context(session, error=f"Telegram: {exc}"),
            )
        return RedirectResponse("/settings?msg=Сообщение+отправлено", status_code=303)

    app.state.telegram_client_factory = lambda: None

    # ------------------------------------------------------------------ журнал

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, session: Db) -> HTMLResponse:
        rows = session.execute(
            select(Run, Profile.name)
            .join(Profile, Profile.id == Run.profile_id)
            .order_by(Run.id.desc())
            .limit(100)
        ).all()
        return render(request, session, "runs.html", runs=rows, jobs=scheduler.get_jobs())

    return app
