"""Read models used by the UI, REST API and MCP server."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from ..models import Answer, Brand, Citation, Claim, Engine, Mention, Prompt, Run, now
from .stats import (Filter, answers_for, direction, entity_stats, mentions_by_answer,
                    share_of_voice, wilson)


def tracked(brand: Brand) -> list[tuple[str, str]]:
    return [(brand.name, "self")] + [(c.name, "competitor") for c in brand.competitors if c.active]


def engine_labels(s) -> dict[str, Engine]:
    return {e.key: e for e in s.scalars(select(Engine))}


def brand_summary(s, brand: Brand, days: int = 30, engine_keys=None, modes=None, tag=None) -> dict:
    end = now()
    cur_f = Filter(brand.id, since=end - timedelta(days=days), engine_keys=engine_keys, modes=modes, tag=tag)
    prev_f = Filter(brand.id, since=end - timedelta(days=2 * days), until=end - timedelta(days=days),
                    engine_keys=engine_keys, modes=modes, tag=tag)
    cur = answers_for(s, cur_f)
    prev = answers_for(s, prev_f)
    mba = mentions_by_answer(s, [a.id for a in cur + prev])
    tr = tracked(brand)
    st = entity_stats(cur, mba, tr)
    pst = entity_stats(prev, mba, tr)
    sov = share_of_voice(st)
    me = st[brand.name]
    pme = pst[brand.name]
    vis, vlo, vhi = me.visibility
    board = sorted(st.values(), key=lambda e: e.visibility[0], reverse=True)
    rank = next((i for i, e in enumerate(board, 1) if e.name == brand.name), None)
    dir_, z = direction(pme.k, pme.n, me.k, me.n)
    last_run = s.scalar(select(Run).where(Run.brand_id == brand.id).order_by(Run.id.desc()).limit(1))
    return {
        "brand": brand, "days": days, "answers": len(cur),
        "rate": me.rate, "rate_ci": me.rate_ci, "visibility": vis, "vis_ci": (vlo, vhi),
        "avg_position": me.avg_position, "sov": sov.get(brand.name, 0.0), "rank": rank, "of": len(board),
        "prev_rate": pme.rate if pme.n else None, "direction": dir_, "z": z,
        "board": [{"name": e.name, "type": e.type, "rate": e.rate, "rate_ci": e.rate_ci,
                   "visibility": e.visibility, "avg_position": e.avg_position, "sov": sov.get(e.name, 0.0),
                   "n": e.n} for e in board],
        "last_run": last_run,
    }


def by_engine(s, brand: Brand, days: int = 30) -> list[dict]:
    cur = answers_for(s, Filter(brand.id, since=now() - timedelta(days=days)))
    mba = mentions_by_answer(s, [a.id for a in cur])
    labels = engine_labels(s)
    groups = defaultdict(list)
    for a in cur:
        groups[a.engine_key].append(a)
    out = []
    for k, ans in groups.items():
        st = entity_stats(ans, mba, tracked(brand))
        me = st[brand.name]
        leader = max(st.values(), key=lambda e: e.rate)
        cited = sum(1 for a in ans if a.citations)
        out.append({"key": k, "label": labels[k].label if k in labels else k,
                    "mode": ans[0].mode, "n": len(ans), "rate": me.rate, "rate_ci": me.rate_ci,
                    "visibility": me.visibility[0], "avg_position": me.avg_position,
                    "leader": leader.name, "leader_rate": leader.rate,
                    "with_citations_pct": cited / len(ans) if ans else 0})
    return sorted(out, key=lambda r: r["label"])


def memory_vs_search(rows: list[dict]) -> dict:
    def pooled(mode):
        r = [x for x in rows if x["mode"] == mode]
        n = sum(x["n"] for x in r)
        k = sum(round(x["rate"] * x["n"]) for x in r)
        return (k / n if n else None), n
    m, mn = pooled("memory")
    sr, sn = pooled("search")
    return {"memory": m, "memory_n": mn, "search": sr, "search_n": sn}


def trend(s, brand: Brand, days: int = 120) -> dict:
    """Per run: mention rate for self and the top 4 competitors."""
    runs = list(s.scalars(select(Run).where(Run.brand_id == brand.id, Run.created_at >= now() - timedelta(days=days),
                                           Run.status.in_(["done", "partial", "budget_stopped"]))
                          .order_by(Run.id)))
    series = defaultdict(list)
    labels = []
    tr = tracked(brand)
    overall = {}
    for r in runs:
        ans = list(s.scalars(select(Answer).where(Answer.run_id == r.id, Answer.status == "ok",
                                                  Answer.analysed == True)))
        if not ans:
            continue
        mba = mentions_by_answer(s, [a.id for a in ans])
        st = entity_stats(ans, mba, tr)
        labels.append(r.created_at.strftime("%d %b"))
        for name, e in st.items():
            lo, hi = e.rate_ci
            series[name].append({"rate": round(e.rate * 100, 1), "lo": round(lo * 100, 1), "hi": round(hi * 100, 1)})
            overall[name] = overall.get(name, 0) + e.k
    top = [brand.name] + [n for n, _ in sorted(((n, v) for n, v in overall.items() if n != brand.name),
                                               key=lambda x: -x[1])[:4]]
    return {"labels": labels, "series": {n: series[n] for n in top if n in series}}


def prompt_table(s, brand: Brand, days: int = 30) -> list[dict]:
    cur = answers_for(s, Filter(brand.id, since=now() - timedelta(days=days)))
    mba = mentions_by_answer(s, [a.id for a in cur])
    byp = defaultdict(list)
    for a in cur:
        byp[a.prompt_id].append(a)
    prompts = {p.id: p for p in brand.prompts}
    out = []
    for pid, ans in byp.items():
        st = entity_stats(ans, mba, tracked(brand))
        me = st[brand.name]
        comp = sorted((e for e in st.values() if e.type == "competitor"), key=lambda e: -e.rate)
        per_engine = {}
        for k in {a.engine_key for a in ans}:
            sub = [a for a in ans if a.engine_key == k]
            kk = sum(1 for a in sub if any(m.entity_type == "self" for m in mba.get(a.id, [])))
            per_engine[k] = (kk, len(sub))
        p = prompts.get(pid)
        out.append({"prompt": p, "n": len(ans), "rate": me.rate, "rate_ci": me.rate_ci,
                    "avg_position": me.avg_position, "visibility": me.visibility[0],
                    "top_competitor": comp[0].name if comp and comp[0].k else "",
                    "top_competitor_rate": comp[0].rate if comp else 0, "per_engine": per_engine})
    for p in brand.prompts:
        if p.active and p.id not in byp:
            out.append({"prompt": p, "n": 0, "rate": 0, "rate_ci": (0, 0), "avg_position": None, "visibility": 0,
                        "top_competitor": "", "top_competitor_rate": 0, "per_engine": {}})
    return sorted(out, key=lambda r: (r["n"] == 0, r["rate"] - r["top_competitor_rate"]))


def sources(s, brand: Brand, days: int = 30, limit: int = 40) -> dict:
    cur = answers_for(s, Filter(brand.id, since=now() - timedelta(days=days)))
    ids = [a.id for a in cur]
    mba = mentions_by_answer(s, ids)
    self_named = {a.id for a in cur if any(m.entity_type == "self" for m in mba.get(a.id, []))}
    dom = Counter()
    dom_without_self = Counter()
    own_pages = Counter()
    owner = {}
    for i in range(0, len(ids), 900):
        for c in s.scalars(select(Citation).where(Citation.answer_id.in_(ids[i:i + 900]))):
            dom[c.domain] += 1
            owner[c.domain] = (c.owner_type, c.owner_name)
            if c.answer_id not in self_named:
                dom_without_self[c.domain] += 1
            if c.owner_type == "own":
                own_pages[c.url.split("?")[0]] += 1
    total = sum(dom.values())
    rows = [{"domain": d, "count": n, "share": n / total if total else 0, "owner_type": owner[d][0],
             "owner_name": owner[d][1], "without_you": dom_without_self[d]} for d, n in dom.most_common(limit)]
    return {"total": total, "domains": rows, "own_pages": own_pages.most_common(30),
            "own_share": sum(n for d, n in dom.items() if owner[d][0] == "own") / total if total else 0}


def answers_for_prompt(s, brand: Brand, prompt_id: int, days: int = 60, engine_key: str | None = None):
    f = Filter(brand.id, since=now() - timedelta(days=days), prompt_ids=[prompt_id],
               engine_keys=[engine_key] if engine_key else None)
    ans = sorted(answers_for(s, f), key=lambda a: (a.created_at, a.engine_key, a.sample_idx), reverse=True)
    mba = mentions_by_answer(s, [a.id for a in ans])
    return ans, mba


def claims(s, brand: Brand, days: int = 60, verdict: str | None = "wrong") -> list[tuple[Claim, Answer]]:
    q = (select(Claim, Answer).join(Answer, Answer.id == Claim.answer_id)
         .where(Claim.brand_id == brand.id, Answer.created_at >= now() - timedelta(days=days)))
    if verdict:
        q = q.where(Claim.verdict == verdict)
    return list(s.execute(q.order_by(Answer.created_at.desc()).limit(300)).all())


def other_brands(s, brand: Brand, days: int = 60, limit: int = 15) -> list[tuple[str, int]]:
    """Untracked vendors AI keeps naming (from the AI pass): candidates to add as competitors."""
    q = (select(Mention.entity_name, func.count(Mention.id)).join(Answer, Answer.id == Mention.answer_id)
         .where(Answer.brand_id == brand.id, Mention.entity_type == "other",
                Answer.created_at >= now() - timedelta(days=days))
         .group_by(Mention.entity_name).order_by(func.count(Mention.id).desc()).limit(limit))
    return [(n, c) for n, c in s.execute(q).all()]


def portfolio(s, days: int = 30) -> list[dict]:
    out = []
    for b in s.scalars(select(Brand).where(Brand.active == True).order_by(Brand.name)):
        sm = brand_summary(s, b, days)
        out.append(sm)
    return out


def prompt_counts(s, brand: Brand) -> tuple[int, int]:
    total = s.scalar(select(func.count(Prompt.id)).where(Prompt.brand_id == brand.id)) or 0
    active = s.scalar(select(func.count(Prompt.id)).where(Prompt.brand_id == brand.id, Prompt.active == True)) or 0
    return active, total


__all__ = ["brand_summary", "by_engine", "memory_vs_search", "trend", "prompt_table", "sources",
           "answers_for_prompt", "claims", "other_brands", "portfolio", "wilson"]
