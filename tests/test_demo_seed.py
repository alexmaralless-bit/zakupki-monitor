from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from zakupki_monitor.ai import AIService, DemoProvider
from zakupki_monitor.collector import evaluate
from zakupki_monitor.db import make_engine
from zakupki_monitor.models import Profile, Score, Tender

SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"


def _load_seed():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("demo_seed", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_seed"] = module
    spec.loader.exec_module(module)
    return module


def test_shift_deadlines_rescores_before_showing(tmp_path: Path) -> None:
    db = _load_seed().build(tmp_path / "demo.db", shift_deadlines=True)
    engine = make_engine(f"sqlite:///{db}")
    today = date.today()
    with sessionmaker(engine, expire_on_commit=False)() as session:
        ai = AIService(session, DemoProvider())
        interesting = session.scalars(select(Tender).where(Tender.status == "interesting")).all()
        assert len(interesting) == 3
        for item in interesting:
            assert item.deadline_at is not None
            assert today + timedelta(days=1) <= item.deadline_at <= today + timedelta(days=2)
        # сохранённые рейтинги совпадают с пересчётом по сдвинутым срокам
        for score in session.scalars(select(Score)).all():
            stored = (score.filtered, score.rating)
            tender = session.get(Tender, score.tender_id)
            profile = session.get(Profile, score.profile_id)
            assert tender and profile
            fresh = evaluate(session, tender, profile, today, ai)
            assert (fresh.filtered, fresh.rating) == stored
    engine.dispose()
