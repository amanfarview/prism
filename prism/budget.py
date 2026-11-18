"""Spend control. Nothing runs without a cost estimate, and runs stop at the cap.

- Before a run: estimate the cost and refuse if it would break the global or a
  provider's monthly cap.
- During a run: each call reserves its estimated cost first and settles the real
  cost after, so parallel calls cannot run past the cap by more than one call each.
- Alerts go to Teams at 50/80/100% of a cap, once per month per threshold.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select

from .common import notify
from .models import Budget, Engine, SpendEntry, now


def month_key(d: datetime | None = None) -> str:
    return (d or now()).strftime("%Y-%m")


def month_spend(s, provider: str | None = None, month: str | None = None) -> float:
    q = select(func.coalesce(func.sum(SpendEntry.cost_usd), 0.0)).where(SpendEntry.month == (month or month_key()))
    if provider:
        q = q.where(SpendEntry.provider == provider)
    return float(s.scalar(q) or 0.0)


def caps(s) -> tuple[float | None, dict[str, float]]:
    g = None
    per: dict[str, float] = {}
    for b in s.scalars(select(Budget)):
        if b.scope == "global":
            g = b.monthly_cap_usd
        elif b.provider:
            per[b.provider] = b.monthly_cap_usd
    return g, per


def call_cost(engine: Engine, input_tokens: int, output_tokens: int, search_calls: int,
              reported: float = 0.0) -> float:
    if reported and reported > 0:
        return round(reported, 6)
    return round(input_tokens * engine.price_in_per_m / 1e6 + output_tokens * engine.price_out_per_m / 1e6
                 + search_calls * engine.price_per_search + engine.price_per_call, 6)


@dataclass
class Estimate:
    total: float
    by_provider: dict[str, float]
    calls: int
    ok: bool
    problems: list[str] = field(default_factory=list)
    global_cap: float | None = None
    global_spent: float = 0.0


def estimate(s, engine_calls: dict[str, int]) -> Estimate:
    """engine_calls: {engine_key: number of calls}"""
    engines = {e.key: e for e in s.scalars(select(Engine).where(Engine.key.in_(list(engine_calls))))}
    byp: dict[str, float] = {}
    total = 0.0
    for k, n in engine_calls.items():
        e = engines.get(k)
        if not e:
            continue
        c = e.est_cost() * n
        byp[e.provider] = byp.get(e.provider, 0.0) + c
        total += c
    gcap, pcaps = caps(s)
    spent = month_spend(s)
    problems = []
    if gcap is not None and spent + total > gcap:
        problems.append(f"Estimated ${total:,.2f} would take this month to ${spent + total:,.2f}, "
                        f"over the ${gcap:,.0f} cap (already spent ${spent:,.2f}).")
    for p, c in byp.items():
        if p in pcaps:
            ps = month_spend(s, p)
            if ps + c > pcaps[p]:
                problems.append(f"{p}: estimated ${c:,.2f} would exceed its ${pcaps[p]:,.0f} cap "
                                f"(spent ${ps:,.2f}).")
    return Estimate(total=round(total, 4), by_provider=byp, calls=sum(engine_calls.values()),
                    ok=not problems, problems=problems, global_cap=gcap, global_spent=spent)


class BudgetGuard:
    """Thread-safe running total for one run."""

    def __init__(self, s):
        self.lock = threading.Lock()
        self.gcap, self.pcaps = caps(s)
        self.spent = month_spend(s)
        self.pspent = {p: month_spend(s, p) for p in self.pcaps}
        self.reserved = 0.0
        self.preserved: dict[str, float] = {}
        self.stopped = False
        self.reason = ""

    def reserve(self, provider: str, est: float) -> bool:
        with self.lock:
            if self.stopped:
                return False
            if self.gcap is not None and self.spent + self.reserved + est > self.gcap:
                self.stopped, self.reason = True, f"global monthly cap ${self.gcap:,.0f} reached"
                return False
            if provider in self.pcaps:
                if self.pspent.get(provider, 0) + self.preserved.get(provider, 0) + est > self.pcaps[provider]:
                    self.stopped, self.reason = True, f"{provider} monthly cap ${self.pcaps[provider]:,.0f} reached"
                    return False
            self.reserved += est
            self.preserved[provider] = self.preserved.get(provider, 0) + est
            return True

    def settle(self, provider: str, est: float, actual: float) -> None:
        with self.lock:
            self.reserved -= est
            self.preserved[provider] = self.preserved.get(provider, 0) - est
            self.spent += actual
            if provider in self.pspent or provider in self.pcaps:
                self.pspent[provider] = self.pspent.get(provider, 0) + actual


def record_spend(s, *, provider: str, engine_key: str, cost: float, brand_id=None, run_id=None,
                 purpose: str = "collect") -> None:
    if cost <= 0:
        return
    s.add(SpendEntry(month=month_key(), provider=provider, engine_key=engine_key, brand_id=brand_id,
                     run_id=run_id, purpose=purpose, cost_usd=cost))


def check_alerts(s) -> None:
    m = month_key()
    for b in s.scalars(select(Budget)):
        spent = month_spend(s, b.provider or None)
        if not b.monthly_cap_usd:
            continue
        pct = spent / b.monthly_cap_usd * 100
        sent = dict(b.alerts_sent or {})
        done = set(sent.get(m, []))
        new = [t for t in (b.alert_pcts or []) if pct >= t and t not in done]
        if new:
            label = "Overall" if b.scope == "global" else b.provider
            notify("budget", f"{label} spend is ${spent:,.2f} this month, {pct:.0f}% of the "
                             f"${b.monthly_cap_usd:,.0f} cap.")
            sent[m] = sorted(done | set(new))
            b.alerts_sent = sent
