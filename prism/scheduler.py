"""Worker loop (systemd: prism-worker). Picks up queued runs and due schedules.

Runs execute here, not in the web process, so a long run survives a web restart
and the UI stays fast. A schedule whose estimate would break a budget is skipped
and an alert is sent, rather than running partially.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import select

from .catalogue import ensure_defaults
from .collector import execute_run, plan_run
from .common import audit, log, notify
from .db import init_db, session_scope
from .models import Brand, Run, Schedule, now

RUNS_PER_MONTH = {"daily": 30.4, "weekly": 4.35, "fortnightly": 2.17, "monthly": 1.0}


def next_run_time(sch: Schedule, after: datetime) -> datetime:
    base = after.replace(minute=0, second=0, microsecond=0)
    if sch.cadence == "daily":
        t = base.replace(hour=sch.hour_utc)
        return t if t > after else t + timedelta(days=1)
    if sch.cadence == "monthly":
        t = base.replace(day=1, hour=sch.hour_utc)
        while t <= after:
            t = (t.replace(day=28) + timedelta(days=4)).replace(day=1, hour=sch.hour_utc)
        return t
    step = 14 if sch.cadence == "fortnightly" else 7
    days_ahead = (sch.weekday - base.weekday()) % 7
    t = (base + timedelta(days=days_ahead)).replace(hour=sch.hour_utc)
    if t <= after:
        t += timedelta(days=7)
    if sch.cadence == "fortnightly" and sch.last_run_at and (t - sch.last_run_at).days < 13:
        t += timedelta(days=step - 7)
    return t


def tick() -> list[int]:
    """One pass: queue due schedules, then execute queued runs one at a time."""
    queued: list[int] = []
    with session_scope() as s:
        t = now()
        for sch in s.scalars(select(Schedule).where(Schedule.active.is_(True))):
            if sch.next_run_at is None:
                sch.next_run_at = next_run_time(sch, t)
                continue
            if sch.next_run_at > t:
                continue
            brand = s.get(Brand, sch.brand_id)
            plan = plan_run(s, sch.brand_id, samples=sch.samples, engine_keys=sch.engine_keys or None,
                            triggered_by="schedule", schedule_id=sch.id)
            if not plan.estimate.ok:
                plan.run.status = "blocked"
                plan.run.note = " ".join(plan.estimate.problems)
                notify("scheduled run blocked by budget", f"{brand.name}: {plan.run.note}")
            elif plan.tasks == 0:
                plan.run.status = "blocked"
                plan.run.note = "Nothing to run: no enabled engines or no active prompts."
            else:
                plan.run.status = "queued"
            audit(s, "scheduler", "schedule_fired", brand=brand.slug, run=plan.run.id, status=plan.run.status,
                  est=plan.estimate.total)
            sch.last_run_at = t
            sch.next_run_at = next_run_time(sch, t)
        queued = [r.id for r in s.scalars(select(Run).where(Run.status == "queued").order_by(Run.id))]
    done = []
    for rid in queued:
        try:
            execute_run(rid)
            done.append(rid)
        except Exception as e:
            log.exception("run %s failed: %s", rid, e)
            with session_scope() as s:
                r = s.get(Run, rid)
                r.status, r.note = "failed", f"{type(e).__name__}: {e}"[:2000]
            notify("run crashed", f"Run {rid}: {e}")
    return done


def loop(interval: int = 30) -> None:  # pragma: no cover - long running
    init_db()
    with session_scope() as s:
        ensure_defaults(s)
    log.info("Prism worker started")
    while True:
        try:
            tick()
        except Exception as e:
            log.exception("worker tick failed: %s", e)
        time.sleep(interval)
