"""Database plumbing: engine, sessions, and exact column types.

Money and quantities are stored as text-backed Decimals so SQLite never rounds
them through a float. Datetimes are stored as UTC and always come back aware.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from sqlalchemy import DateTime, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class DecimalText(TypeDecorator):
    """Exact Decimal stored as a string."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return format(Decimal(value), "f")

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime, persisted as naive UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given; use timezone-aware datetimes")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:///:memory:"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    from atlas import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
