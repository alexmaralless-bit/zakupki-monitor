"""Демо-база для скриншотов: data/demo.db из сохранённых страниц ЕИС (tests/fixtures).

Реальные данные выдачи и двух карточек; вердикты ИИ даёт провайдер «demo» (заглушка, не модель),
в интерфейсе при этом в подвале стоит пометка. Запуск:

    uv run python scripts/demo_seed.py [--shift-deadlines]
    ZM_DB_PATH=data/demo.db uv run zakupki-monitor serve

--shift-deadlines переносит сроки подачи трёх закупок на завтра-послезавтра, чтобы на
скриншоте была видна подсветка срочных; без флага сроки остаются как в выдаче.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from zakupki_monitor.ai import AIService, DemoProvider
from zakupki_monitor.appsettings import RuntimeSettings, save_runtime
from zakupki_monitor.collector import apply_card, evaluate
from zakupki_monitor.config import PROJECT_ROOT, Settings
from zakupki_monitor.db import make_engine, upgrade_db
from zakupki_monitor.models import Profile, Run, Score, Tender, utcnow
from zakupki_monitor.parser import parse_card, parse_lot_list, parse_search_page
from zakupki_monitor.profiles import ProfileSpec, save_profile
from zakupki_monitor.scoring import FilterConfig, RuleSpec
from zakupki_monitor.search_url import SearchParams
from zakupki_monitor.store import upsert_tender

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
CARD_REGS = {"0373200627926000002": "card_44.html", "32616430006": "card_223.html"}

PROFILES = [
    (
        "search_223.html",
        ProfileSpec(
            name="Проектирование газопроводов",
            search=SearchParams(search_string="мособлгаз", fz44=False, okpd2_codes=["71.12.19"]),
            filter=FilterConfig(stop_words=["поставка", "аренда"], laws=[223]),
            rules=[
                RuleSpec(type="keyword", value="газопровод", weight=3),
                RuleSpec(type="keyword", value="проектн", weight=2),
                RuleSpec(type="okpd2", value="71.12", weight=3),
                RuleSpec(type="customer", value="мособлгаз", weight=1),
                RuleSpec(type="price_range", value="1000000-10000000", weight=1),
                RuleSpec(type="days_to_deadline", value="5", weight=1),
            ],
            ai={
                "criterion": "Подходят закупки на проектирование и изыскания для наружных "
                "газопроводов; не подходят поставки оборудования и строительство без проекта.",
                "weight": 0.5,
            },
        ),
    ),
    (
        "search_44.html",
        ProfileSpec(
            name="Уборка помещений",
            search=SearchParams(search_string="уборка помещений", fz223=False),
            filter=FilterConfig(price_min=1_000_000, stop_words=["поставка"], laws=[44]),
            rules=[
                RuleSpec(type="keyword", value="уборк", weight=3),
                RuleSpec(type="keyword", value="комплексн", weight=2),
                RuleSpec(type="okpd2", value="81.21", weight=2),
                RuleSpec(type="price_range", value="2000000-8000000", weight=2),
                RuleSpec(type="days_to_deadline", value="5", weight=1),
            ],
            ai={
                "criterion": "Подходит комплексная уборка помещений и территорий для "
                "организаций, разовая мойка окон и вывоз мусора не интересны.",
                "weight": 0.4,
            },
        ),
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shift-deadlines", action="store_true")
    parser.add_argument("--db", type=Path, default=Path("data/demo.db"))
    args = parser.parse_args()

    settings = Settings(db_path=args.db, ai_provider="demo")
    db_file = settings.resolved_db_path()
    if db_file.exists():
        db_file.unlink()  # только демо-база внутри проекта
    upgrade_db(settings.db_url)
    engine = make_engine(settings.db_url)
    today = date.today()
    lots = parse_lot_list((FIXTURES / "lots_223.html").read_text(encoding="utf-8"))

    with sessionmaker(engine, expire_on_commit=False)() as session:
        save_runtime(session, RuntimeSettings(ai_provider="demo"))
        ai = AIService(session, DemoProvider())
        for fixture, spec in PROFILES:
            profile = save_profile(session, spec)
            page = parse_search_page((FIXTURES / fixture).read_text(encoding="utf-8"))
            run = Run(profile_id=profile.id, pages=1, found=len(page.entries))
            session.add(run)
            for entry in page.entries:
                res = upsert_tender(session, entry)
                run.new += res.outcome == "new"
                tender = res.tender
                card_file = CARD_REGS.get(tender.reg_number)
                if card_file and tender.card_fetched_at is None:
                    card = parse_card((FIXTURES / card_file).read_text(encoding="utf-8"))
                    if card.lot_list_url:
                        card.okpd2 = lots
                    apply_card(tender, card)
                    run.cards += 1
                score = evaluate(session, tender, profile, today, ai)
                run.filtered += score.filtered
            run.finished_at = utcnow()
            run.note = "демо-загрузка из сохранённых страниц выдачи"
        session.flush()

        top = session.execute(
            select(Tender, Score)
            .join(Score, Score.tender_id == Tender.id)
            .where(Score.filtered.is_(False))
            .order_by(Score.rating.desc())
        ).all()
        for i, (tender, _) in enumerate(top[:3]):
            tender.status = "interesting"
            if args.shift_deadlines:
                tender.deadline_at = today + timedelta(days=1 + i % 2)
        for tender, _ in top[-2:]:
            tender.status = "rejected"
        session.commit()
        total = len(session.scalars(select(Tender)).all())
        shown = len(session.scalars(select(Score).where(Score.filtered.is_(False))).all())
        profiles = len(session.scalars(select(Profile)).all())
        print(f"{db_file}: профилей {profiles}, закупок {total}, в рейтинге {shown}")
    engine.dispose()


if __name__ == "__main__":
    main()
