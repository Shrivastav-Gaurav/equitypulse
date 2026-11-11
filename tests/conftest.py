"""Shared fixtures. Nothing here reads the real .env or touches the network."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from equitypulse.db.session import make_engine

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"


def load_json_fixture(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    """A session on a fresh temp SQLite DB migrated to head with Alembic."""
    url = f"sqlite:///{(tmp_path / 'ep.db').as_posix()}"
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    with Session(engine) as session:
        yield session
    engine.dispose()
