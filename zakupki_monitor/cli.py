"""Командная строка: zakupki-monitor init-db | profile ... | collect | tenders list."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .collector import Fetcher, collect, make_client
from .config import get_settings
from .db import session_scope, upgrade_db
from .models import Profile, Score, Tender
from .profiles import (
    ProfileExistsError,
    import_url,
    load_profile_file,
    save_profile,
)

app = typer.Typer(help="Монитор госзакупок: сбор выдачи zakupki.gov.ru и рейтинг по правилам.")
profile_app = typer.Typer(help="Профили поиска.")
tenders_app = typer.Typer(help="Реестр закупок.")
app.add_typer(profile_app, name="profile")
app.add_typer(tenders_app, name="tenders")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("init-db")
def init_db() -> None:
    """Создать базу и применить миграции."""
    settings = get_settings()
    upgrade_db(settings.db_url)
    typer.echo(f"База готова: {settings.resolved_db_path()}")


@profile_app.command("add")
def profile_add(
    file: Annotated[Path, typer.Option("--file", "-f", exists=True, dir_okay=False)],
    replace: Annotated[bool, typer.Option(help="Заменить профиль с таким именем")] = False,
) -> None:
    """Создать профиль из файла YAML или TOML."""
    spec = load_profile_file(file)
    with session_scope() as session:
        try:
            p = save_profile(session, spec, replace=replace)
        except ProfileExistsError:
            typer.echo(f"Профиль «{spec.name}» уже есть; добавьте --replace", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Профиль «{p.name}» сохранён (правил: {len(p.rules)})")


@profile_app.command("import-url")
def profile_import_url(
    url: Annotated[str, typer.Argument(help="Ссылка поиска с zakupki.gov.ru")],
    name: Annotated[str, typer.Option("--name", "-n")],
    max_pages: Annotated[int, typer.Option(min=1)] = 5,
) -> None:
    """Создать профиль из ссылки расширенного поиска."""
    with session_scope() as session:
        try:
            p = import_url(session, url, name, max_pages)
        except ProfileExistsError:
            typer.echo(f"Профиль «{name}» уже есть", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Профиль «{p.name}» создан: {p.search.get('search_string')!r}")


@profile_app.command("list")
def profile_list() -> None:
    """Показать профили."""
    with session_scope() as session:
        profiles = session.scalars(select(Profile).order_by(Profile.id)).all()
        if not profiles:
            typer.echo("Профилей нет")
        for p in profiles:
            laws = [
                n for n, on in (("44", p.search.get("fz44")), ("223", p.search.get("fz223"))) if on
            ]
            typer.echo(
                f"{p.id}\t{p.name}\t«{p.search.get('search_string')}»\t"
                f"ФЗ: {'+'.join(laws)}\tправил: {len(p.rules)}\tстраниц: {p.max_pages}"
            )


def _pick_profiles(session: Session, name: str | None) -> list[Profile]:
    stmt = select(Profile).order_by(Profile.id)
    if name:
        stmt = stmt.where(Profile.name == name)
    profiles = list(session.scalars(stmt))
    if not profiles:
        typer.echo(
            "Профиль не найден" if name else "Профилей нет: создайте командой profile", err=True
        )
        raise typer.Exit(1)
    return profiles


@app.command("collect")
def collect_cmd(
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    max_pages: Annotated[int | None, typer.Option("--max-pages", min=1)] = None,
) -> None:
    """Собрать выдачу по профилю (или по всем профилям)."""
    settings = get_settings()
    upgrade_db(settings.db_url)
    with session_scope(settings) as session, make_client(settings) as client:
        fetcher = Fetcher(client, settings)
        for p in _pick_profiles(session, profile):
            stats = collect(session, p, fetcher, max_pages=max_pages or p.max_pages)
            session.commit()
            typer.echo(
                f"[{p.name}] страниц: {stats.pages}, найдено: {stats.found}, "
                f"новых: {stats.new}, обновлённых: {stats.updated}, отсеяно: {stats.filtered}, "
                f"оценено: {stats.scored}, карточек: {stats.cards}, ошибок: {stats.errors}"
            )
            for note in stats.notes:
                typer.echo(f"  {note}")


@tenders_app.command("list")
def tenders_list(
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    top: Annotated[int, typer.Option("--top", min=1)] = 20,
    show_filtered: Annotated[bool, typer.Option("--filtered", help="Показать отсеянные")] = False,
) -> None:
    """Закупки по убыванию рейтинга."""
    with session_scope() as session:
        profiles = _pick_profiles(session, profile)
        for p in profiles:
            stmt = (
                select(Tender, Score)
                .join(Score, Score.tender_id == Tender.id)
                .where(Score.profile_id == p.id, Score.filtered == show_filtered)
            )
            if not show_filtered:
                stmt = stmt.order_by(Score.rating.desc(), Tender.updated_at.desc())
            else:
                stmt = stmt.order_by(Tender.updated_at.desc())
            rows = session.execute(stmt.limit(top)).all()
            typer.echo(f"== {p.name} ({'отсеяно' if show_filtered else 'по рейтингу'}) ==")
            for t, s in rows:
                head = "; ".join(s.filter_reasons) if show_filtered else f"{s.rating:5.1f}"
                price = f"{t.initial_price:,.0f}".replace(",", " ") if t.initial_price else "-"
                typer.echo(
                    f"{head}\t{t.reg_number}\t{t.law}-ФЗ\t{price} ₽\t"
                    f"до {t.deadline_at or '-'}\t{t.object[:80]}"
                )


if __name__ == "__main__":
    app()
