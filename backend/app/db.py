"""Database engine + session management (Postgres in compose, SQLite locally)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings

log = logging.getLogger("m2x.db")


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = settings.resolved_database_url
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        kwargs: dict = {"connect_args": connect_args}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        eng = create_engine(url, future=True, **kwargs)

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver glue
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

        return eng
    return create_engine(url, future=True, pool_pre_ping=True, pool_size=10, max_overflow=20)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


# Columns introduced after a database may already have been created. ``create_all``
# only creates whole tables, so a deployment that predates them would keep serving
# an older schema; these are all nullable-with-default additions, which both
# SQLite and Postgres accept as a plain ALTER.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "payments": {"asset_id": "INTEGER DEFAULT 0"},
    "refunds": {"asset_id": "INTEGER DEFAULT 0"},
    "providers": {"payment_asset_id": "INTEGER DEFAULT 0"},
    "bazaar_listings": {"asset_id": "INTEGER DEFAULT 0"},
}


def _add_missing_columns() -> None:
    from .config import ASSET_ID

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for column, ddl in columns.items():
                if column in present:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                # Backfill: rows written before the column existed were all paid
                # in the mandated asset, which is what 0 would otherwise hide.
                if table != "bazaar_listings":
                    conn.execute(text(f"UPDATE {table} SET {column} = :asset"),
                                 {"asset": ASSET_ID})
                log.info("schema: added %s.%s", table, column)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for background tasks / scripts."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
