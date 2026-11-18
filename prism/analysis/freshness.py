"""Freshness check: are we getting genuinely new answers?

AI answers vary from run to run. If a run's answers are word-for-word the same as
the previous run's, something is caching (us, a proxy or the provider) and trends
are measuring the cache. This is the check that exposed Trakkr's reused answers.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from ..models import Answer, Run


def check_run(s, run: Run) -> dict:
    cur = list(s.scalars(select(Answer).where(Answer.run_id == run.id, Answer.status == "ok")))
    prev_run = s.scalar(select(Run).where(Run.brand_id == run.brand_id, Run.id < run.id,
                                         Run.status.in_(["done", "partial", "budget_stopped"]))
                        .order_by(Run.id.desc()).limit(1))
    key = lambda a: (a.prompt_id, a.engine_key, a.market_id, a.sample_idx)  # noqa: E731
    same = comparable = 0
    if prev_run:
        prev = {key(a): a.text_hash for a in s.scalars(select(Answer).where(Answer.run_id == prev_run.id,
                                                                            Answer.status == "ok"))}
        for a in cur:
            h = prev.get(key(a))
            if h:
                comparable += 1
                same += int(h == a.text_hash)
    # within this run: do repeated samples of the same question differ at all?
    groups: dict[tuple, set] = defaultdict(set)
    sizes: dict[tuple, int] = defaultdict(int)
    for a in cur:
        g = (a.prompt_id, a.engine_key, a.market_id)
        groups[g].add(a.text_hash)
        sizes[g] += 1
    multi = [g for g, n in sizes.items() if n > 1]
    frozen = sum(1 for g in multi if len(groups[g]) == 1)
    pct_prev = round(100 * same / comparable, 1) if comparable else None
    run.identical_to_previous_pct = pct_prev
    by_engine: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for g in multi:
        by_engine[g[1]][0] += int(len(groups[g]) == 1)
        by_engine[g[1]][1] += 1
    return {
        "identical_to_previous_pct": pct_prev,
        "compared_with_run": prev_run.id if prev_run else None,
        "frozen_sample_groups_pct": round(100 * frozen / len(multi), 1) if multi else None,
        "frozen_by_engine": {k: round(100 * v[0] / v[1], 1) for k, v in by_engine.items() if v[1]},
    }
