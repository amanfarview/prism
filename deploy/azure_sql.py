"""Move Prism from SQLite (/var/lib/prism/prism.db) into its own schema in the shared Azure SQL database
(farview.database.windows.net / jarvis), the way Orbit did. Run on the server as root, in this order:

  sudo /opt/prism-venv/bin/python /var/www/prism/deploy/azure_sql.py login   # type the prismapp password (hidden)
  sudo /opt/prism-venv/bin/python /var/www/prism/deploy/azure_sql.py test    # full demo pipeline in prism_test, then cleaned
  sudo /opt/prism-venv/bin/python /var/www/prism/deploy/azure_sql.py move    # stop, copy every row, check counts, switch, start
  sudo /opt/prism-venv/bin/python /var/www/prism/deploy/azure_sql.py back    # point Prism at the SQLite file again

prismapp owns prism, prism_staging and prism_test and can't see any other schema (deploy/prism_schema.sql).
The password is typed hidden, stored only in /etc/prism/prism.env (root, 600) and never printed.
Nothing is deleted: the SQLite file stays where it is as the fallback for `back`.
"""
from __future__ import annotations

import getpass
import os
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import quote_plus

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, APP)
import envtool  # noqa: E402

ENVF = "/etc/prism/prism.env"
SERVER, DATABASE, LOGIN = "farview.database.windows.net", "jarvis", "prismapp"
SQLITE_URL = "sqlite:////var/lib/prism/prism.db"
UNITS = ["prism-web", "prism-worker", "prism-mcp"]


def safe(s: str) -> str:
    """Any connection string in an error message, without its password."""
    return re.sub(r"(mssql\+pyodbc://[^:/@\s]+):[^@\s]*@", r"\1:***@", re.sub(r"(?i)(PWD=)[^;\s]*", r"\1***", str(s)))


def say(level: str, msg: str) -> None:
    print(f"  {level:<4} {safe(msg)}")


def azure_url() -> str:
    url = envtool.parse(ENVF).get("PRISM_AZURE_SQL_URL", "")
    if not url:
        sys.exit("  STOPPED: run `azure_sql.py login` first")
    return url


def use_db(url: str, schema: str):
    """Point this process's Prism code at url/schema (nothing else changes)."""
    os.environ.update({"PRISM_DATABASE_URL": url, "PRISM_DB_SCHEMA": schema, "PRISM_SECRET_BACKEND": "env",
                       "PRISM_AUTH_MODE": "entra", "PRISM_SESSION_SECRET": secrets.token_urlsafe(48),
                       "PRISM_ENVIRONMENT": "dev", "PRISM_SECRETS_FILE": "/nonexistent/secrets.json"})
    from prism.config import get_settings
    get_settings.cache_clear()
    from prism.db import reset_engine
    return reset_engine(url)


def cmd_login() -> None:
    pw = getpass.getpass(f"{LOGIN} password (hidden): ")
    if not pw or any(c in pw for c in '"\\\n'):
        sys.exit("  STOPPED: empty, or contains a quote/backslash (pick letters and digits). Nothing written.")
    url = (f"mssql+pyodbc://{LOGIN}:{quote_plus(pw)}@{SERVER}:1433/{DATABASE}"
           "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no&LoginTimeout=30")
    from sqlalchemy import text
    eng = use_db(url, "prism")
    try:
        with eng.connect() as c:
            who = c.execute(text("SELECT USER_NAME(), DB_NAME()")).one()
            owned = [r[0] for r in c.execute(text(
                "SELECT s.name FROM sys.schemas s JOIN sys.database_principals p ON p.principal_id = s.principal_id "
                "WHERE p.name = USER_NAME() ORDER BY s.name"))]
            others = c.execute(text("SELECT HAS_PERMS_BY_NAME('dbo', 'SCHEMA', 'SELECT') + "
                                    "ISNULL(HAS_PERMS_BY_NAME('orbit', 'SCHEMA', 'SELECT'), 0)")).scalar()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"  STOPPED: couldn't sign in to {SERVER}/{DATABASE} as {LOGIN}: {safe(e)[:300]}")
    say("OK", f"signed in as {who[0]} on {who[1]}")
    missing = {"prism", "prism_staging", "prism_test"} - set(owned)
    say("OK" if not missing else "FAIL", f"owns schemas: {', '.join(owned) or 'none'}" + (f" (missing {', '.join(sorted(missing))}: run deploy/prism_schema.sql)" if missing else ""))
    say("OK" if not others else "FAIL", "can't read dbo or orbit" if not others else "CAN read dbo/orbit: fix the login's rights")
    if missing or others:
        sys.exit(1)
    envtool.cmd_set(ENVF, [f"PRISM_AZURE_SQL_URL={url}"])
    say("OK", f"stored in {ENVF} as PRISM_AZURE_SQL_URL (not in use until `move`)")


def cmd_test() -> None:
    from sqlalchemy import func, select, text
    eng = use_db(azure_url(), "prism_test")
    from prism.analysis import report
    from prism.analysis.stats import mentions_by_answer
    from prism.catalogue import ensure_defaults
    from prism.collector import execute_run, plan_run
    from prism.db import Base, init_db, session_scope
    from prism.models import Answer, Brand, Engine, Fact
    from prism.seed import seed_portcos
    t0 = time.time()
    Base.metadata.drop_all(eng)  # prism_test only: whatever a previous test left
    try:
        init_db()
        say("OK", f"created {len(Base.metadata.tables)} tables in prism_test")
        with session_scope() as s:
            ensure_defaults(s)
            seed_portcos(s)
            for e in s.scalars(select(Engine).where(Engine.provider == "mock")):
                e.enabled = True
            b = s.scalar(select(Brand).where(Brand.slug == "amplience"))
            weird = "Łódź “quotes” — 中文 ü ñ ✓"
            s.add(Fact(brand_id=b.id, attribute="unicode", value=weird, wrong_patterns=[weird]))
            bid = b.id
        with session_scope() as s:
            back = s.scalar(select(Fact.value).where(Fact.attribute == "unicode"))
        say("OK" if back == weird else "FAIL", f"unicode round trip ({back!r})")
        with session_scope() as s:
            rid = plan_run(s, bid, samples=1).run.id
        r = execute_run(rid, retry_sleep=0)
        say("OK" if r.status == "done" and r.done_calls else "FAIL", f"demo run {rid}: {r.status}, {r.done_calls} answers")
        with session_scope() as s:
            sm = report.brand_summary(s, s.get(Brand, bid), 30)
            n = s.scalar(select(func.count(Answer.id)))
            ids = list(range(1, 2600))  # more than SQL Server's 2,100-parameter limit
            mentions_by_answer(s, ids)
        say("OK", f"reports work ({sm['answers']} answers summarised; {n} stored; 2,599-id IN list fine)")
        with eng.connect() as c:
            kinds = {r[0] for r in c.execute(text(
                "SELECT DISTINCT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'prism_test'"))}
        say("OK" if "varchar" not in kinds and "datetime" not in kinds else "FAIL", f"column types: {', '.join(sorted(kinds))}")
    finally:
        Base.metadata.drop_all(eng)
        say("OK", f"prism_test cleaned up ({time.time() - t0:.0f} s)")


def _services(action: str) -> None:
    subprocess.run(["systemctl", action, *UNITS], check=False)


def _wait_ready() -> bool:
    import urllib.request
    for _ in range(45):
        time.sleep(2)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8040/prism/healthz/ready", timeout=5) as r:
                if b'"db":true' in r.read():
                    return True
        except Exception:  # noqa: BLE001
            pass
    return False


def cmd_move() -> None:
    from sqlalchemy import create_engine, func, select
    url = azure_url()
    env = envtool.parse(ENVF)
    if env.get("PRISM_DATABASE_URL", "").startswith("mssql"):
        sys.exit("  STOPPED: Prism already uses Azure SQL")
    src_url = env.get("PRISM_DATABASE_URL") or SQLITE_URL
    if not src_url.startswith("sqlite") or not os.path.exists(src_url.split("sqlite:///", 1)[1]):
        sys.exit(f"  STOPPED: SQLite file for {src_url} not found")
    eng = use_db(url, "prism")
    from prism.db import Base, init_db
    init_db()
    with eng.connect() as c:
        busy = [t.name for t in Base.metadata.sorted_tables if c.execute(select(func.count()).select_from(t)).scalar()]
    if busy:
        sys.exit(f"  STOPPED: schema prism already has rows in {', '.join(busy)}. Nothing copied or switched.")
    say("..", "stopping prism-web, prism-worker, prism-mcp for the copy")
    _services("stop")
    switched = False
    try:
        src = create_engine(src_url)
        total = 0
        for t in Base.metadata.sorted_tables:
            with src.connect() as sc:
                rows = [dict(r) for r in sc.execute(select(t)).mappings()]
            if rows:
                with eng.begin() as dc:
                    for i in range(0, len(rows), 500):
                        dc.execute(t.insert(), rows[i:i + 500])
            with eng.connect() as dc:
                n = dc.execute(select(func.count()).select_from(t)).scalar()
            if n != len(rows):
                raise RuntimeError(f"{t.name}: copied {n} of {len(rows)} rows")
            total += n
        say("OK", f"copied {total} rows across {len(Base.metadata.tables)} tables; every count matches")
        envtool.cmd_set(ENVF, [f"PRISM_DATABASE_URL={url}", f"PRISM_SQLITE_URL={src_url}"])
        switched = True
        say("OK", f"{ENVF}: PRISM_DATABASE_URL now Azure SQL (the SQLite URL kept as PRISM_SQLITE_URL for `back`)")
    except Exception as e:  # noqa: BLE001
        say("FAIL", f"{type(e).__name__}: {e}")
    finally:
        _services("start")
    if not switched:
        sys.exit("  STOPPED: nothing switched; Prism is back on SQLite. The partial copy in schema prism must be emptied "
                 "before another try (tell Claude).")
    ok = _wait_ready()
    say("OK" if ok else "FAIL", "Prism answers on Azure SQL" if ok else "Prism isn't healthy on Azure SQL: run `azure_sql.py back`")
    if ok:
        subprocess.run(["/usr/local/bin/prism", "selfcheck"], check=False)


def cmd_back() -> None:
    env = envtool.parse(ENVF)
    sq = env.get("PRISM_SQLITE_URL") or SQLITE_URL
    envtool.cmd_set(ENVF, [f"PRISM_DATABASE_URL={sq}"])
    _services("restart")
    say("OK" if _wait_ready() else "FAIL", f"Prism points at {sq} again. Anything written on Azure SQL since the move is still there, not copied back.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"login": cmd_login, "test": cmd_test, "move": cmd_move, "back": cmd_back}.get(cmd, lambda: sys.exit(__doc__))()
