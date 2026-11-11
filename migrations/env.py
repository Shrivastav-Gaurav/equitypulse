"""Alembic environment.

`render_as_batch=True` because SQLite can't ALTER most things in place; batch mode rebuilds
the table instead, and is a no-op difference on PostgreSQL.
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool

from equitypulse.db.models import Base
from equitypulse.db.session import make_engine
from equitypulse.db.types import UTCDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    x_url = context.get_x_argument(as_dictionary=True).get("db_url")
    if x_url:
        return x_url
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    from equitypulse.config import get_settings  # only needed when no URL was passed

    return get_settings().database_url.get_secret_value()


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    # Render our UTC wrapper as a plain portable type, so migrations don't import app code.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


COMMON_OPTS: dict[str, Any] = dict(
    target_metadata=target_metadata,
    render_as_batch=True,
    compare_type=True,
    render_item=_render_item,
)


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), literal_binds=True, **COMMON_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, **COMMON_OPTS)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
