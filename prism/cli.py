"""Command line: python -m prism.cli <command>

  init                      create schema/tables and default engines (safe to re-run)
  seed                      add the four portcos with starter competitors and prompts
  estimate <slug> [--samples N] [--engines a,b]   show cost before running (free)
  run <slug> [--samples N] [--engines a,b] [--yes] run now in this shell (asks first)
  set-key <provider>        store a provider key in the secret store (prompted, not echoed);
                            also `set-key entra` (Entra client secret) and `set-key teams` (alerts webhook)
  active-runs               ids of runs in progress (deploy/update.sh won't restart the worker under them)
  test-engine <key>         one real call to check key/model (costs one call)
  audit-site <slug>         AI readiness audit of the brand's site (free)
  worker                    start the scheduler loop (systemd uses this)
  token <name> [--write]    create an API/MCP token (printed once)
  selfcheck                 production safety checks: sign-in on, database, secrets file 600 and ours,
                            metadata endpoint blocked, other apps' settings unreadable (exit 1 on a FAIL)
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from .catalogue import ensure_defaults
from .db import init_db, session_scope
from .models import ApiToken, Brand, Engine, ProviderKey, now


def _brand(s, slug):
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    if not b:
        sys.exit(f"Unknown brand {slug}")
    return b


def main(argv=None):
    ap = argparse.ArgumentParser(prog="prism")
    ap.add_argument("cmd")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--samples", type=int)
    ap.add_argument("--engines")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    keys = a.engines.split(",") if a.engines else None

    if a.cmd == "init":
        init_db()
        with session_scope() as s:
            ensure_defaults(s)
        print("Database ready.")
    elif a.cmd == "seed":
        from .seed import seed_example_facts, seed_portcos
        with session_scope() as s:
            n = seed_portcos(s)
            seed_example_facts(s)
        print(f"Added {n} brand(s).")
    elif a.cmd in ("estimate", "run"):
        from .collector import execute_run, plan_run
        with session_scope() as s:
            b = _brand(s, a.arg)
            plan = plan_run(s, b.id, samples=a.samples, engine_keys=keys, triggered_by="cli", create=False)
            print(f"{b.name}: {plan.tasks} calls across {len(plan.engine_calls)} engine(s), "
                  f"estimated ${plan.estimate.total:,.2f}. Month to date ${plan.estimate.global_spent:,.2f}"
                  f" of ${plan.estimate.global_cap or 0:,.0f}.")
            for k, n in plan.engine_calls.items():
                print(f"  {k}: {n} calls")
            for p in plan.estimate.problems:
                print("  BLOCKED:", p)
            if a.cmd == "estimate" or not plan.estimate.ok or plan.tasks == 0:
                return
            if not a.yes and input("Run it? [y/N] ").strip().lower() != "y":
                return
            plan = plan_run(s, b.id, samples=a.samples, engine_keys=keys, triggered_by="cli")
            rid = plan.run.id
        r = execute_run(rid)
        print(f"Run {rid}: {r.status}, {r.done_calls} ok, {r.failed_calls} failed, ${r.cost_usd:,.4f}, "
              f"identical to previous: {r.identical_to_previous_pct}%")
    elif a.cmd == "set-key":
        from .config import get_settings
        from .secrets import set_secret
        from .common import audit
        st = get_settings()
        special = {"entra": (st.entra_client_secret_name, "Entra client secret"),
                   "teams": (st.teams_webhook_secret_name, "Teams webhook URL")}
        with session_scope() as s:
            if a.arg in special:
                name, label = special[a.arg]
                val = getpass.getpass(f"{label} (input hidden): ").strip()
                if not val:
                    sys.exit("Nothing typed; nothing stored.")
                set_secret(name, val)
                audit(s, "cli", "secret_set", name=name, last4=val[-4:])
            else:
                row = s.scalar(select(ProviderKey).where(ProviderKey.provider == a.arg))
                if not row:
                    sys.exit("Use one of: entra, teams, " + ", ".join(p.provider for p in s.scalars(select(ProviderKey))))
                val = getpass.getpass(f"{a.arg} key (input hidden): ").strip()
                if not val:
                    sys.exit("Nothing typed; nothing stored.")
                set_secret(row.secret_name, val)
                row.last4, row.updated_at, row.updated_by = val[-4:], now(), "cli"
                audit(s, "cli", "key_set", provider=a.arg, last4=val[-4:])
        print("Stored. Restart to pick it up at once: sudo systemctl restart prism-web prism-worker")
    elif a.cmd == "active-runs":
        from .models import Run
        with session_scope() as s:
            ids = [r.id for r in s.scalars(select(Run).where(Run.status == "running"))]
        print(" ".join(map(str, ids)))
    elif a.cmd == "test-engine":
        from .collector import test_engine
        with session_scope() as s:
            e = s.scalar(select(Engine).where(Engine.key == a.arg))
            ok, msg, cost = test_engine(s, e, "cli")
        print("OK" if ok else "FAILED", msg)
    elif a.cmd == "audit-site":
        from .analysis.site_audit import run_audit
        with session_scope() as s:
            res = run_audit(s, _brand(s, a.arg))
            print(f"Score {res.score}/100")
            for c in res.results["checks"]:
                print(f"  [{c['status']}] {c['name']}: {c.get('detail', '')}")
    elif a.cmd == "worker":
        from .scheduler import loop
        loop()
    elif a.cmd == "token":
        from .web.auth import hash_token, new_token
        tok = new_token()
        with session_scope() as s:
            s.add(ApiToken(name=a.arg or "cli", token_hash=hash_token(tok), scope="write" if a.write else "read",
                           created_by="cli"))
        print(tok)
    elif a.cmd == "selfcheck":
        sys.exit(selfcheck())
    else:
        print(__doc__)


# other apps' settings on the Jarvis VM: the prism user must not be able to read any of them
OTHER_APPS_SECRETS = ["/etc/orbit/orbit.env", "/var/www/farview/farview-team/.env", "/var/www/farview/farview-team/.env.local",
                      "/var/www/farview/portfolioiq/.env", "/var/www/conferences/conferencedash-azure/backend/.env",
                      "/home/azureuser/apps/flo/backend/.env", "/home/azureuser/myapp/official/.env",
                      "/home/azureuser/prism/.env"]


def selfcheck() -> int:
    """Run as the prism user inside the service sandbox (the `prism` wrapper does that). Prints no secrets."""
    import os
    import socket

    from .config import get_settings
    from .db import get_engine, ping
    from .secrets import get_secret
    fails = 0

    def line(level, msg):
        nonlocal fails
        fails += level == "FAIL"
        print(f"  {level:<4} {msg}")

    st = get_settings()
    line("OK", f"settings load: environment={st.environment}, sign-in={st.auth_mode}, public URL {st.public_url}")
    if os.geteuid() == 0:
        line("FAIL", "running as root: run through the `prism` wrapper (it runs as the prism user)")
    if st.auth_mode != "entra" and st.environment != "dev":
        line("FAIL", "sign-in is off outside dev")
    eng = get_engine()
    db_ok = ping()
    line("OK" if db_ok else "FAIL", f"database answers ({eng.dialect.name}{', schema ' + st.db_schema if eng.dialect.name == 'mssql' else ''})")
    if st.secret_backend == "file":
        path = st.secrets_file
        if os.path.exists(path):
            stt = os.stat(path)
            mode_ok = (stt.st_mode & 0o777) == 0o600 and stt.st_uid == os.geteuid()
            line("OK" if mode_ok else "FAIL", f"secrets file {path} mode {stt.st_mode & 0o777:o}, owned by uid {stt.st_uid}")
        else:
            line("WARN", f"no secrets file yet at {path} (created when the first key is saved)")
    else:
        line("WARN", f"secret backend is {st.secret_backend!r}: on a shared VM Key Vault via the VM identity isn't per-app")
    if st.auth_mode == "entra":
        has = bool(st.entra_client_secret or get_secret(st.entra_client_secret_name))
        line("OK" if has and st.entra_tenant_id and st.entra_client_id else "FAIL", "Entra tenant, client id and secret present")
    try:
        with socket.create_connection(("169.254.169.254", 80), timeout=2):
            line("FAIL", "Azure metadata endpoint 169.254.169.254 is reachable (IPAddressDeny not applied)")
    except OSError:
        line("OK", "Azure metadata endpoint blocked")
    readable = [f for f in OTHER_APPS_SECRETS if os.path.exists(f) and os.access(f, os.R_OK)]
    line("FAIL" if readable else "OK", "other apps' settings unreadable" + (f": CAN READ {', '.join(readable)}" if readable else ""))
    print("selfcheck:", "FAILED" if fails else "passed")
    return 1 if fails else 0


if __name__ == "__main__":
    main()
