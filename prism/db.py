"""Database engine and session.

Models are declared without a schema. On SQL Server we translate the default
schema to settings.db_schema (e.g. "prism") so Prism lives in its own schema in
the shared Farview Azure SQL database, like Orbit. SQLite (local/tests) has no
schemas, so nothing is translated there.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_Session = None


def get_engine(url: str | None = None):
    global _engine, _Session
    if _engine is not None and url is None:
        return _engine
    s = get_settings()
    url = url or s.database_url
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(url, **kwargs)
    if eng.dialect.name == "mssql":
        eng = eng.execution_options(schema_translate_map={None: s.db_schema})
    if eng.dialect.name == "sqlite":
        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    _engine = eng
    _Session = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    return eng


def reset_engine(url: str):
    """Used by tests to point at a fresh database."""
    global _engine, _Session
    _engine = None
    _Session = None
    return get_engine(url)


def SessionLocal():
    if _Session is None:
        get_engine()
    return _Session()


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db():
    """Create the schema (SQL Server) and any missing tables. Safe to re-run."""
    from . import models  # noqa: F401  (register tables)

    eng = get_engine()
    if eng.dialect.name == "mssql":
        schema = get_settings().db_schema
        with eng.begin() as c:
            c.execute(text(
                f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{schema}') "
                f"EXEC('CREATE SCHEMA [{schema}]')"
            ))
    Base.metadata.create_all(eng)
