"""Database engine and session.

Models are declared without a schema. On SQL Server we translate the default schema to settings.db_schema
(e.g. "prism") so Prism lives in its own schema in the shared Farview Azure SQL database ("jarvis"), like
Orbit. SQLite (laptop/tests) has no schemas, so nothing is translated there.

Azure SQL lessons carried over from Orbit (deploy/azure_sql.py, Oct-Nov 2026):
- The jarvis database's default collation is a Latin-1 code page. Plain VARCHAR squashes text ("Łódź" became
  "Lódz"), and Prism stores AI answers word for word, so every String/Text column is NVARCHAR and text is
  sent as NVARCHAR.
- DATETIME2, not DATETIME (DATETIME rounds to 3 ms).
- SQL Server takes at most 2,100 parameters per statement; IN lists are rendered inline instead.
- Azure closes idle connections after ~4 minutes: recycle pooled connections before that.
- The database is S0 (10 DTUs) and shared with Orbit, Jarvis, Flo and PortfolioIQ: keep the pool small.
"""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import DateTime, LargeBinary, String, Text, Unicode, create_engine, event, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.sql.elements import BindParameter

from .config import get_settings


class Base(DeclarativeBase):
    pass


@compiles(String, "mssql")
def _mssql_string(type_, compiler, **kw):
    return f"NVARCHAR({type_.length})" if type_.length else "NVARCHAR(max)"


@compiles(Text, "mssql")
def _mssql_text(type_, compiler, **kw):
    return "NVARCHAR(max)"


@compiles(LargeBinary, "mssql")
def _mssql_binary(type_, compiler, **kw):
    return "VARBINARY(max)"  # never the deprecated IMAGE type


@compiles(DateTime, "mssql")
def _mssql_datetime(type_, compiler, **kw):
    return "DATETIMEOFFSET" if type_.timezone else "DATETIME2"


@compiles(BindParameter, "mssql")
def _mssql_in_lists(element, compiler, **kw):
    if element.expanding:
        kw["literal_execute"] = True
        if isinstance(element.type, String) and not isinstance(element.type, Unicode):
            element = element._with_binary_element_type(Unicode())  # N'...': the jarvis DB isn't UTF-8
    return compiler.visit_bindparam(element, **kw)


_engine = None
_Session = None


def get_engine(url: str | None = None):
    global _engine, _Session
    if _engine is not None and url is None:
        return _engine
    s = get_settings()
    url = url or s.database_url
    if url.startswith("sqlite"):
        eng = create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    else:
        # use_setinputsizes off: pyodbc sends text as NVARCHAR sized to the value (accents intact, indexes usable)
        eng = create_engine(url, pool_pre_ping=True, pool_recycle=180, pool_size=5, max_overflow=5,
                            pool_timeout=60, use_setinputsizes=False, deprecate_large_types=True)
        if eng.dialect.name == "mssql":
            eng.dialect.use_insertmanyvalues_wo_returning = True
            eng = eng.execution_options(schema_translate_map={None: s.db_schema})
    _engine = eng
    _Session = sessionmaker(bind=eng, expire_on_commit=False)
    return eng


def reset_engine(url: str):
    """Used by tests (and deploy/azure_sql.py) to point at a fresh database."""
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


def ping() -> bool:
    """True if the database answers a trivial query (used by /healthz/ready and the health check)."""
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def init_db():
    """Create the schema (SQL Server) and any missing tables. Safe to re-run.

    On Azure SQL the schema is created by deploy/prism_schema.sql (owned by the prismapp login, which has no
    right to create schemas); this only creates it when it's missing and the login is allowed to."""
    from . import models  # noqa: F401  (register tables)

    eng = get_engine()
    if eng.dialect.name == "mssql":
        schema = get_settings().db_schema
        if not schema.replace("_", "").isalnum():
            raise ValueError(f"bad schema name {schema!r}")
        with eng.begin() as c:
            c.execute(text(
                f"IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = '{schema}') "
                f"EXEC('CREATE SCHEMA [{schema}]')"
            ))
    Base.metadata.create_all(eng)
