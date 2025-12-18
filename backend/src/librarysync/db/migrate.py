from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

from librarysync.db.session import get_async_database_url


def run_migrations(retries: int = 10, delay_seconds: float = 1.0) -> None:
    migrations_path = Path(__file__).resolve().parent / "migrations"
    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(migrations_path))
    alembic_cfg.set_main_option("sqlalchemy.url", get_async_database_url())

    attempt = 0
    while True:
        try:
            command.upgrade(alembic_cfg, "head")
            return
        except OperationalError:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(delay_seconds)
