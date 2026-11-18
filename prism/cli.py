"""Command line: python -m prism.cli <command>

  init                      create schema/tables and default engines (safe to re-run)
  seed                      add the four portcos with starter competitors and prompts
  estimate <slug> [--samples N] [--engines a,b]   show cost before running (free)
  run <slug> [--samples N] [--engines a,b] [--yes] run now in this shell (asks first)
  set-key <provider>        store a provider key in Key Vault (prompted, not echoed)
  test-engine <key>         one real call to check key/model (costs one call)
  audit-site <slug>         AI readiness audit of the brand's site (free)
  worker                    start the scheduler loop (systemd uses this)
  token <name> [--write]    create an API/MCP token (printed once)
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
        from .secrets import set_secret
        with session_scope() as s:
            row = s.scalar(select(ProviderKey).where(ProviderKey.provider == a.arg))
            if not row:
                sys.exit("Providers: " + ", ".join(p.provider for p in s.scalars(select(ProviderKey))))
            val = getpass.getpass(f"{a.arg} key (input hidden): ").strip()
            set_secret(row.secret_name, val)
            row.last4, row.updated_at, row.updated_by = val[-4:], now(), "cli"
        print("Stored.")
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
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
