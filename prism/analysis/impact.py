"""Did a change work? Difference-in-differences against untouched prompts.

Before/after on its own can't separate a real effect from model updates or
seasonality, which move every prompt. So we compare the change in the targeted
prompts with the change in control prompts over the same windows, and put a
bootstrap 95% range on the difference.
"""
from __future__ import annotations

import random
from datetime import timedelta

from ..models import Brand, Change
from .stats import Filter, answers_for, mentions_by_answer


def _rate(xs: list[int]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def evaluate(s, change: Change, n_boot: int = 1000, min_n: int = 12) -> dict:
    brand = s.get(Brand, change.brand_id)
    w = timedelta(days=change.window_days)
    t0 = change.shipped_on
    targets = set(change.target_prompt_ids or [])
    controls = set(change.control_prompt_ids or []) or {p.id for p in brand.prompts if p.active} - targets
    pre = answers_for(s, Filter(brand.id, since=t0 - w, until=t0))
    post = answers_for(s, Filter(brand.id, since=t0, until=t0 + w))
    mba = mentions_by_answer(s, [a.id for a in pre + post])

    def named(a):
        return int(any(m.entity_type == "self" for m in mba.get(a.id, [])))

    cells = {
        "pre_t": [named(a) for a in pre if a.prompt_id in targets],
        "post_t": [named(a) for a in post if a.prompt_id in targets],
        "pre_c": [named(a) for a in pre if a.prompt_id in controls],
        "post_c": [named(a) for a in post if a.prompt_id in controls],
    }
    n = {k: len(v) for k, v in cells.items()}
    rates = {k: _rate(v) for k, v in cells.items()}
    out = {"n": n, "rates": rates, "did": None, "ci": None, "verdict": "too early",
           "window_ends": (t0 + w).date().isoformat()}
    if min(n.values()) < min_n:
        missing = [k for k, v in n.items() if v < min_n]
        out["note"] = f"Need at least {min_n} answers in each group; short in: {', '.join(missing)}."
        return out
    did = (rates["post_t"] - rates["pre_t"]) - (rates["post_c"] - rates["pre_c"])
    rnd = random.Random(change.id)
    boots = []
    for _ in range(n_boot):
        r = {k: sum(rnd.choice(v) for _ in v) / len(v) for k, v in cells.items()}
        boots.append((r["post_t"] - r["pre_t"]) - (r["post_c"] - r["pre_c"]))
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]
    out.update(did=did, ci=(lo, hi))
    out["verdict"] = "worked" if lo > 0 else ("hurt" if hi < 0 else "no clear effect yet")
    return out
