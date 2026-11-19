"""Statistics with honest uncertainty.

Metrics (per entity, over a set of answers):
- mention_rate: share of answers that name the entity, with a Wilson 95% range
- visibility: 100 x average of position weight (1/position, 0 if not named), with a 95% range
- avg_position: average position when named (1 = named first among tracked brands)
- share_of_voice: entity mentions / all tracked-entity mentions

A change between two periods is only called "up" or "down" when a two-proportion
z-test says it is unlikely to be noise. Otherwise it is "no clear change".
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from ..models import Answer, Mention, Prompt, now

Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def mean_ci(xs: list[float], z: float = Z95) -> tuple[float, float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0.0
    m = sum(xs) / n
    if n == 1:
        return m, m, m
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    h = z * math.sqrt(var / n)
    return m, max(0.0, m - h), min(1.0, m + h)


def two_prop_z(k1: int, n1: int, k2: int, n2: int) -> float:
    if n1 == 0 or n2 == 0:
        return 0.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return (k2 / n2 - k1 / n1) / se


@dataclass
class EntityStats:
    name: str
    type: str
    n: int = 0
    k: int = 0
    weights: list[float] = field(default_factory=list)
    positions: list[int] = field(default_factory=list)
    mentions: int = 0

    @property
    def rate(self) -> float:
        return self.k / self.n if self.n else 0.0

    @property
    def rate_ci(self) -> tuple[float, float]:
        return wilson(self.k, self.n)

    @property
    def visibility(self) -> tuple[float, float, float]:
        m, lo, hi = mean_ci(self.weights)
        return m * 100, lo * 100, hi * 100

    @property
    def avg_position(self) -> float | None:
        return sum(self.positions) / len(self.positions) if self.positions else None


@dataclass
class Filter:
    brand_id: int
    since: datetime | None = None
    until: datetime | None = None
    engine_keys: list[str] | None = None
    modes: list[str] | None = None
    prompt_ids: list[int] | None = None
    market_id: int | None = None
    tag: str | None = None


def answers_for(s, f: Filter) -> list[Answer]:
    q = select(Answer).where(Answer.brand_id == f.brand_id, Answer.status == "ok", Answer.analysed == True)
    if f.since:
        q = q.where(Answer.created_at >= f.since)
    if f.until:
        q = q.where(Answer.created_at < f.until)
    if f.engine_keys:
        q = q.where(Answer.engine_key.in_(f.engine_keys))
    if f.modes:
        q = q.where(Answer.mode.in_(f.modes))
    if f.prompt_ids:
        q = q.where(Answer.prompt_id.in_(f.prompt_ids))
    if f.market_id:
        q = q.where(Answer.market_id == f.market_id)
    rows = list(s.scalars(q))
    if f.tag:
        ok = {p.id for p in s.scalars(select(Prompt).where(Prompt.brand_id == f.brand_id)) if f.tag in (p.tags or [])}
        rows = [a for a in rows if a.prompt_id in ok]
    return rows


def mentions_by_answer(s, answer_ids: list[int]) -> dict[int, list[Mention]]:
    out: dict[int, list[Mention]] = defaultdict(list)
    for i in range(0, len(answer_ids), 900):  # SQL Server parameter limit friendly
        chunk = answer_ids[i:i + 900]
        for m in s.scalars(select(Mention).where(Mention.answer_id.in_(chunk))):
            out[m.answer_id].append(m)
    return out


def entity_stats(answers: list[Answer], mba: dict[int, list[Mention]], tracked: list[tuple[str, str]]) -> dict[str, EntityStats]:
    """tracked: [(name, type)] for self and competitors."""
    st = {n: EntityStats(n, t) for n, t in tracked}
    for a in answers:
        ms = [m for m in mba.get(a.id, []) if m.entity_type in ("self", "competitor")]
        by = {m.entity_name: m for m in ms}
        for name, es in st.items():
            es.n += 1
            m = by.get(name)
            if m:
                es.k += 1
                es.weights.append(1.0 / m.position)
                es.positions.append(m.position)
                es.mentions += 1
            else:
                es.weights.append(0.0)
    return st


def share_of_voice(st: dict[str, EntityStats]) -> dict[str, float]:
    tot = sum(e.k for e in st.values())
    return {n: (e.k / tot if tot else 0.0) for n, e in st.items()}


def period_bounds(days: int, end: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    end = end or now()
    return end - timedelta(days=2 * days), end - timedelta(days=days), end


def direction(k1: int, n1: int, k2: int, n2: int) -> tuple[str, float]:
    z = two_prop_z(k1, n1, k2, n2)
    if n1 < 6 or n2 < 6:
        return "too little data", z
    if z > Z95:
        return "up", z
    if z < -Z95:
        return "down", z
    return "no clear change", z
