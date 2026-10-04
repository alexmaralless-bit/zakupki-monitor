"""Окружение Alembic: адрес БД берётся из конфигурации (sqlalchemy.url)."""

from alembic import context
from sqlalchemy import create_engine

from zakupki_monitor.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url")
    assert url
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
