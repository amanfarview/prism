"""REST API (read for dashboards/scripts, write for log ingestion).

Auth: a signed-in browser session, or `Authorization: Bearer prism_...` service
token from Command Centre > API tokens. No public docs endpoint is exposed.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select

from ..analysis import actions as actions_mod
from ..analysis import crawlers, report
from ..models import Answer, Brand
from .app import get_db
from .auth import current_user, token_user

router = APIRouter()


def caller(request: Request, s=Depends(get_db)):
    u = token_user(s, request.headers.get("authorization")) or current_user(request)
    if not u:
        raise HTTPException(401, "unauthorised")
    return u


def _brand(s, slug):
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    if not b:
        raise HTTPException(404, "unknown brand")
    return b


def _r(x, d=4):
    return None if x is None else round(x, d)


@router.get("/brands")
def brands(s=Depends(get_db), u=Depends(caller)):
    return [{"slug": b.slug, "name": b.name, "domain": b.domain, "category": b.category}
            for b in s.scalars(select(Brand).where(Brand.active == True).order_by(Brand.name))]


@router.get("/brands/{slug}/summary")
def summary(slug: str, days: int = 30, s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    sm = report.brand_summary(s, b, days)
    return {"brand": b.slug, "days": days, "answers": sm["answers"],
            "mention_rate": _r(sm["rate"]), "mention_rate_95": [_r(x) for x in sm["rate_ci"]],
            "visibility": _r(sm["visibility"], 1), "visibility_95": [_r(x, 1) for x in sm["vis_ci"]],
            "avg_position": _r(sm["avg_position"], 2), "share_of_voice": _r(sm["sov"]),
            "rank": sm["rank"], "of": sm["of"], "vs_previous_period": sm["direction"],
            "leaderboard": [{"name": e["name"], "type": e["type"], "mention_rate": _r(e["rate"]),
                             "mention_rate_95": [_r(x) for x in e["rate_ci"]],
                             "visibility": _r(e["visibility"][0], 1), "avg_position": _r(e["avg_position"], 2)}
                            for e in sm["board"]]}


@router.get("/brands/{slug}/engines")
def engines(slug: str, days: int = 30, s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    return [{**{k: v for k, v in r.items() if k != "rate_ci"}, "rate_95": list(r["rate_ci"])}
            for r in report.by_engine(s, b, days)]


@router.get("/brands/{slug}/prompts")
def prompts(slug: str, days: int = 30, s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    return [{"id": r["prompt"].id, "text": r["prompt"].text, "tags": r["prompt"].tags, "answers": r["n"],
             "mention_rate": _r(r["rate"]), "mention_rate_95": [_r(x) for x in r["rate_ci"]],
             "avg_position": _r(r["avg_position"], 2), "top_competitor": r["top_competitor"],
             "top_competitor_rate": _r(r["top_competitor_rate"])} for r in report.prompt_table(s, b, days)]


@router.get("/brands/{slug}/answers")
def answers(slug: str, prompt_id: int | None = None, engine: str | None = None, limit: int = 50,
            s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    q = select(Answer).where(Answer.brand_id == b.id, Answer.status == "ok")
    if prompt_id:
        q = q.where(Answer.prompt_id == prompt_id)
    if engine:
        q = q.where(Answer.engine_key == engine)
    rows = s.scalars(q.order_by(Answer.id.desc()).limit(min(limit, 500)))
    return [{"id": a.id, "run_id": a.run_id, "at": a.created_at.isoformat(), "engine": a.engine_key, "mode": a.mode,
             "model": a.model, "sample": a.sample_idx, "prompt": a.prompt_text, "answer": a.response_text,
             "citations": a.citations, "search_queries": a.search_queries, "cost_usd": a.cost_usd} for a in rows]


@router.get("/brands/{slug}/sources")
def sources(slug: str, days: int = 30, s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    src = report.sources(s, b, days)
    return {"total_citations": src["total"], "own_share": _r(src["own_share"]), "domains": src["domains"],
            "own_pages": [{"url": u_, "count": n} for u_, n in src["own_pages"]]}


@router.get("/brands/{slug}/actions")
def actions(slug: str, days: int = 30, s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    return [{"kind": a.kind, "title": a.title, "why": a.why, "steps": a.steps, "priority": round(a.priority, 1),
             "evidence": a.evidence} for a in actions_mod.build(s, b, days)]


@router.get("/brands/{slug}/claims")
def claims(slug: str, verdict: str = "wrong", days: int = 60, s=Depends(get_db), u=Depends(caller)):
    b = _brand(s, slug)
    return [{"id": c.id, "attribute": c.attribute, "statement": c.statement, "verdict": c.verdict,
             "method": c.method, "resolved": c.resolved, "engine": a.engine_key, "answer_id": a.id,
             "at": a.created_at.isoformat()} for c, a in report.claims(s, b, days, verdict)]


@router.post("/ingest/crawlers/{slug}")
def ingest_crawlers(slug: str, request: Request, body: str = Body(..., media_type="text/plain"),
                    s=Depends(get_db)):
    u = token_user(s, request.headers.get("authorization"), need_write=True)
    if not u:
        raise HTTPException(401, "write token required")
    b = _brand(s, slug)
    return crawlers.ingest(s, b.id, body)
