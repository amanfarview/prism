"""Dashboard pages (everyone signed in)."""
from __future__ import annotations

import csv
import html
import io
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from markupsafe import Markup
from sqlalchemy import func, select

from ..analysis import actions as actions_mod
from ..analysis import demand, impact, report, site_audit
from ..analysis.extract import entities_for
from ..budget import month_spend
from ..collector import execute_run, plan_run
from ..common import audit
from ..config import get_settings
from ..models import (Answer, Brand, Change, Citation, Claim, CrawlerHit, Engine, Fact, Prompt, Run, SiteAudit,
                      SiteLogo, now)
from .app import flash, get_db, redirect, render, require_admin, require_user
from .auth import check_csrf

router = APIRouter()


def _brand(s, slug: str) -> Brand:
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    if not b:
        raise HTTPException(404, "Unknown brand")
    return b


def highlight(text: str, brand: Brand) -> Markup:
    esc = html.escape(text or "")
    ents = entities_for(brand, list(brand.competitors))
    spans = []
    for e in ents:
        for m in e.pattern.finditer(text or ""):
            spans.append((m.start(), m.end(), "self" if e.type == "self" else "comp"))
    if not spans:
        return Markup(esc)
    spans.sort()
    out, pos = [], 0
    for a, b, cls in spans:
        if a < pos:
            continue
        out.append(html.escape(text[pos:a]))
        out.append(f'<mark class="{cls}">{html.escape(text[a:b])}</mark>')
        pos = b
    out.append(html.escape(text[pos:]))
    return Markup("".join(out))


# ---------------------------------------------------------------- logos
@router.get("/logo/{domain}")
def logo(domain: str, s=Depends(get_db), user=Depends(require_user)):
    """A company's icon, served by Prism (never hot-linked). Sandboxed even if opened on its own."""
    from ..analysis.logos import normalise, sniff
    row = s.get(SiteLogo, normalise(domain))
    if not row or not row.data or not sniff(row.data):
        raise HTTPException(404, "no logo")
    return Response(row.data, media_type=sniff(row.data), headers={
        "Cache-Control": "private, max-age=86400",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})


# ---------------------------------------------------------------- portfolio
@router.get("/")
def portfolio(request: Request, days: int = 30, s=Depends(get_db), user=Depends(require_user)):
    rows = report.portfolio(s, days)
    spend = month_spend(s)
    return render(request, "portfolio.html", s, user, rows=rows, days=days, spend=spend)


# ---------------------------------------------------------------- brand overview
@router.get("/b/{slug}")
def overview(request: Request, slug: str, days: int = 30, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    sm = report.brand_summary(s, b, days)
    eng = report.by_engine(s, b, days)
    mvs = report.memory_vs_search(eng)
    tr = report.trend(s, b, max(days * 4, 90))
    acts = actions_mod.build(s, b, days)[:5]
    runs = list(s.scalars(select(Run).where(Run.brand_id == b.id).order_by(Run.id.desc()).limit(5)))
    doms = {c.name: c.domain for c in b.competitors}
    doms[b.name] = b.domain
    return render(request, "brand.html", s, user, b=b, sm=sm, eng=eng, mvs=mvs, trend=tr, acts=acts, runs=runs,
                  days=days, doms=doms, tab="overview")


# ---------------------------------------------------------------- prompts
@router.get("/b/{slug}/prompts")
def prompts(request: Request, slug: str, days: int = 30, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    rows = report.prompt_table(s, b, days)
    engines = {e.key: e for e in s.scalars(select(Engine))}
    used = sorted({k for r in rows for k in r["per_engine"]})
    return render(request, "prompts.html", s, user, b=b, rows=rows, days=days, engines=engines, used=used,
                  tab="prompts")


@router.post("/b/{slug}/prompts/add")
def add_prompts(request: Request, slug: str, text: str = Form(...), tags: str = Form(""),
                csrf: str = Form(""), s=Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf)
    b = _brand(s, slug)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    n = 0
    for line in text.splitlines():
        line = line.strip()
        if line:
            s.add(Prompt(brand_id=b.id, text=line[:1000], tags=tag_list, source="manual"))
            n += 1
    audit(s, user.email, "prompts_added", brand=b.slug, n=n)
    flash(request, f"Added {n} prompt(s).")
    return redirect(f"/b/{slug}/prompts")


@router.post("/b/{slug}/prompts/{pid}/toggle")
def toggle_prompt(request: Request, slug: str, pid: int, csrf: str = Form(""), s=Depends(get_db),
                  user=Depends(require_admin)):
    check_csrf(request, csrf)
    p = s.get(Prompt, pid)
    p.active = not p.active
    audit(s, user.email, "prompt_toggled", prompt=pid, active=p.active)
    return redirect(f"/b/{slug}/prompts")


@router.post("/b/{slug}/prompts/import")
async def import_demand(request: Request, slug: str, file: UploadFile, csrf: str = Form(""), s=Depends(get_db),
                        user=Depends(require_admin)):
    check_csrf(request, csrf)
    b = _brand(s, slug)
    sugg = demand.suggestions(await file.read(), b.name)
    existing = {p.text.lower() for p in b.prompts}
    sugg = [x for x in sugg if x["prompt"].lower() not in existing]
    return render(request, "demand.html", s, user, b=b, sugg=sugg, tab="prompts")


@router.post("/b/{slug}/prompts/import/confirm")
async def import_confirm(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    b = _brand(s, slug)
    n = 0
    for k, v in form.multi_items():
        if k == "pick":
            prompt, _, vol = v.rpartition("||")
            s.add(Prompt(brand_id=b.id, text=prompt[:1000], tags=["From search data"], source="gsc",
                         demand=int(vol or 0)))
            n += 1
    audit(s, user.email, "prompts_imported", brand=b.slug, n=n)
    flash(request, f"Added {n} prompt(s) from search data.")
    return redirect(f"/b/{slug}/prompts")


@router.get("/b/{slug}/prompts/{pid}")
def prompt_detail(request: Request, slug: str, pid: int, days: int = 60, engine: str = "", s=Depends(get_db),
                  user=Depends(require_user)):
    b = _brand(s, slug)
    p = s.get(Prompt, pid)
    ans, mba = report.answers_for_prompt(s, b, pid, days, engine or None)
    cits = defaultdict(list)
    ids = [a.id for a in ans]
    for i in range(0, len(ids), 900):
        for c in s.scalars(select(Citation).where(Citation.answer_id.in_(ids[i:i + 900])).order_by(Citation.position)):
            cits[c.answer_id].append(c)
    engines = {e.key: e for e in s.scalars(select(Engine))}
    per_engine = defaultdict(lambda: [0, 0])
    for a in ans:
        per_engine[a.engine_key][1] += 1
        per_engine[a.engine_key][0] += any(m.entity_type == "self" for m in mba.get(a.id, []))
    rows = [{"a": a, "html": highlight(a.response_text, b), "mentions": sorted(mba.get(a.id, []), key=lambda m: m.position),
             "cits": cits[a.id]} for a in ans[:150]]
    return render(request, "prompt.html", s, user, b=b, p=p, rows=rows, engines=engines, per_engine=per_engine,
                  engine=engine, days=days, total=len(ans), tab="prompts")


# ---------------------------------------------------------------- sources
@router.get("/b/{slug}/sources")
def sources(request: Request, slug: str, days: int = 30, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    src = report.sources(s, b, days)
    refs = dict(s.execute(select(Citation.url, Citation.content_ref).join(Answer, Answer.id == Citation.answer_id)
                          .where(Answer.brand_id == b.id, Citation.owner_type == "own")).all())
    return render(request, "sources.html", s, user, b=b, src=src, refs=refs, days=days, tab="sources")


# ---------------------------------------------------------------- facts
@router.get("/b/{slug}/facts")
def facts_page(request: Request, slug: str, days: int = 60, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    facts = list(s.scalars(select(Fact).where(Fact.brand_id == b.id).order_by(Fact.attribute)))
    wrong = report.claims(s, b, days, "wrong")
    unver = report.claims(s, b, days, "unverifiable")[:60]
    others = report.other_brands(s, b, days)
    from ..common import get_setting
    ai_on = (get_setting(s, "llm_extraction", {}) or {}).get("enabled")
    return render(request, "facts.html", s, user, b=b, facts=facts, wrong=wrong, unver=unver, others=others,
                  ai_on=ai_on, days=days, tab="facts")


@router.post("/b/{slug}/facts/save")
def save_fact(request: Request, slug: str, attribute: str = Form(...), value: str = Form(...),
              wrong_patterns: str = Form(""), source: str = Form(""), fact_id: int = Form(0), csrf: str = Form(""),
              s=Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf)
    b = _brand(s, slug)
    pats = [p.strip() for p in wrong_patterns.splitlines() if p.strip()]
    for p in pats:
        try:
            re.compile(p)
        except re.error as e:
            flash(request, f"Pattern not valid: {p} ({e})", "err")
            return redirect(f"/b/{slug}/facts")
    f = s.get(Fact, fact_id) if fact_id else Fact(brand_id=b.id)
    f.attribute, f.value, f.wrong_patterns, f.source, f.updated_at = attribute, value, pats, source, now()
    if not fact_id:
        s.add(f)
    audit(s, user.email, "fact_saved", brand=b.slug, attribute=attribute)
    flash(request, "Fact saved. It applies to future runs; re-check past answers from the Runs tab.")
    return redirect(f"/b/{slug}/facts")


@router.post("/b/{slug}/facts/{fid}/delete")
def delete_fact(request: Request, slug: str, fid: int, csrf: str = Form(""), s=Depends(get_db),
                user=Depends(require_admin)):
    check_csrf(request, csrf)
    f = s.get(Fact, fid)
    if f:
        s.delete(f)
        audit(s, user.email, "fact_deleted", fact=fid)
    return redirect(f"/b/{slug}/facts")


@router.post("/b/{slug}/claims/{cid}/resolve")
def resolve_claim(request: Request, slug: str, cid: int, csrf: str = Form(""), s=Depends(get_db),
                  user=Depends(require_user)):
    check_csrf(request, csrf)
    c = s.get(Claim, cid)
    c.resolved = not c.resolved
    audit(s, user.email, "claim_resolved", claim=cid, resolved=c.resolved)
    return redirect(f"/b/{slug}/facts")


# ---------------------------------------------------------------- actions
@router.get("/b/{slug}/actions")
def actions_page(request: Request, slug: str, days: int = 30, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    return render(request, "actions.html", s, user, b=b, acts=actions_mod.build(s, b, days), days=days,
                  tab="actions")


# ---------------------------------------------------------------- impact
@router.get("/b/{slug}/impact")
def impact_page(request: Request, slug: str, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    changes = list(s.scalars(select(Change).where(Change.brand_id == b.id).order_by(Change.shipped_on.desc())))
    results = {c.id: impact.evaluate(s, c) for c in changes}
    prompts = [p for p in b.prompts if p.active]
    pmap = {p.id: p for p in b.prompts}
    return render(request, "impact.html", s, user, b=b, changes=changes, results=results, prompts=prompts,
                  pmap=pmap, today=now().date().isoformat(), tab="impact")


@router.post("/b/{slug}/impact/add")
async def add_change(request: Request, slug: str, s=Depends(get_db), user=Depends(require_user)):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    b = _brand(s, slug)
    targets = [int(v) for k, v in form.multi_items() if k == "target"]
    if not targets:
        flash(request, "Pick at least one prompt the change is meant to move.", "err")
        return redirect(f"/b/{slug}/impact")
    c = Change(brand_id=b.id, title=str(form.get("title", ""))[:300], url=str(form.get("url", "")),
               shipped_on=datetime.fromisoformat(str(form.get("shipped_on"))),
               window_days=int(form.get("window_days") or 28), target_prompt_ids=targets,
               notes=str(form.get("notes", "")), created_by=user.email)
    s.add(c)
    audit(s, user.email, "change_logged", brand=b.slug, title=c.title)
    flash(request, "Change logged. Results appear once both windows have enough answers.")
    return redirect(f"/b/{slug}/impact")


# ---------------------------------------------------------------- site audit
@router.get("/b/{slug}/audit")
def audit_page(request: Request, slug: str, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    a = s.scalar(select(SiteAudit).where(SiteAudit.brand_id == b.id).order_by(SiteAudit.id.desc()).limit(1))
    return render(request, "audit.html", s, user, b=b, a=a, tab="audit")


@router.post("/b/{slug}/audit/run")
def audit_run(request: Request, slug: str, csrf: str = Form(""), s=Depends(get_db), user=Depends(require_user)):
    check_csrf(request, csrf)
    b = _brand(s, slug)
    a = site_audit.run_audit(s, b)
    audit(s, user.email, "site_audit", brand=b.slug, score=a.score)
    flash(request, f"Site audit done: {a.score:.0f}/100 (free, no AI calls).")
    return redirect(f"/b/{slug}/audit")


# ---------------------------------------------------------------- crawlers
@router.get("/b/{slug}/crawlers")
def crawlers_page(request: Request, slug: str, days: int = 30, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    since = now() - timedelta(days=days)
    hits = list(s.scalars(select(CrawlerHit).where(CrawlerHit.brand_id == b.id, CrawlerHit.at >= since)))
    by_bot = Counter((h.bot, h.operator, h.purpose) for h in hits)
    by_path = Counter(h.path for h in hits if h.purpose in ("user", "search"))
    errors = Counter((h.bot, h.status) for h in hits if h.status >= 400)
    return render(request, "crawlers.html", s, user, b=b, total=len(hits), by_bot=by_bot.most_common(),
                  by_path=by_path.most_common(25), errors=errors.most_common(10), days=days,
                  public_url=get_settings().public_url, tab="crawlers")


# ---------------------------------------------------------------- runs
@router.get("/b/{slug}/runs")
def runs_page(request: Request, slug: str, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    runs = list(s.scalars(select(Run).where(Run.brand_id == b.id).order_by(Run.id.desc()).limit(60)))
    errs = {}
    for r in runs[:10]:
        errs[r.id] = s.execute(select(Answer.engine_key, Answer.error).where(Answer.run_id == r.id,
                                                                             Answer.status == "error").limit(3)).all()
    engines = list(s.scalars(select(Engine).where(Engine.enabled == True).order_by(Engine.label)))
    return render(request, "runs.html", s, user, b=b, runs=runs, errs=errs, engines=engines, tab="runs")


@router.post("/b/{slug}/run/plan")
async def run_plan(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    b = _brand(s, slug)
    keys = [v for k, v in form.multi_items() if k == "engine"] or None
    samples = int(form.get("samples") or 3)
    plan = plan_run(s, b.id, samples=samples, engine_keys=keys, triggered_by=user.email, create=False)
    return render(request, "run_confirm.html", s, user, b=b, plan=plan, keys=keys or [], samples=samples, tab="runs")


@router.post("/b/{slug}/run/confirm")
async def run_confirm(request: Request, slug: str, bg: BackgroundTasks, s=Depends(get_db),
                      user=Depends(require_admin)):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    b = _brand(s, slug)
    keys = [v for k, v in form.multi_items() if k == "engine"] or None
    plan = plan_run(s, b.id, samples=int(form.get("samples") or 3), engine_keys=keys, triggered_by=user.email)
    if not plan.estimate.ok:
        s.rollback()
        flash(request, " ".join(plan.estimate.problems), "err")
        return redirect(f"/b/{slug}/runs")
    plan.run.status = "queued"
    audit(s, user.email, "run_queued", brand=b.slug, run=plan.run.id, est=plan.estimate.total)
    s.commit()
    if get_settings().auth_mode == "dev":
        bg.add_task(execute_run, plan.run.id)  # local: run in-process; production: the worker picks it up
    flash(request, f"Run {plan.run.id} queued: {plan.tasks} calls, about ${plan.estimate.total:,.2f}.")
    return redirect(f"/b/{slug}/runs")


@router.post("/b/{slug}/runs/{rid}/reanalyse")
def reanalyse(request: Request, slug: str, rid: int, csrf: str = Form(""), s=Depends(get_db),
              user=Depends(require_admin)):
    """Free: re-apply brand/competitor names and fact patterns to stored answers."""
    check_csrf(request, csrf)
    from ..analysis import extract, facts
    n = extract.analyse_run(s, rid)
    facts.pattern_check_run(s, rid)
    audit(s, user.email, "run_reanalysed", run=rid)
    flash(request, f"Re-analysed {n} answers (free).")
    return redirect(f"/b/{slug}/runs")


# ---------------------------------------------------------------- export
@router.get("/b/{slug}/export.csv")
def export_csv(slug: str, days: int = 90, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    since = now() - timedelta(days=days)
    ans = list(s.scalars(select(Answer).where(Answer.brand_id == b.id, Answer.created_at >= since)
                         .order_by(Answer.created_at)))
    from ..analysis.stats import mentions_by_answer
    mba = mentions_by_answer(s, [a.id for a in ans])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["answer_id", "run_id", "date", "engine", "mode", "model", "sample", "prompt", "brand_named",
                "brand_position", "brands_named_in_order", "citations", "cost_usd", "status", "answer_text"])
    for a in ans:
        ms = sorted(mba.get(a.id, []), key=lambda m: m.position)
        me = next((m for m in ms if m.entity_type == "self"), None)
        w.writerow([a.id, a.run_id, a.created_at.isoformat(), a.engine_key, a.mode, a.model, a.sample_idx,
                    a.prompt_text, bool(me), me.position if me else "",
                    "; ".join(m.entity_name for m in ms if m.entity_type != "other"),
                    " ".join(c.get("url", "") for c in (a.citations or [])), a.cost_usd, a.status, a.response_text])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="prism_{slug}_answers.csv"'})


@router.get("/b/{slug}/runs/count")
def run_count(slug: str, s=Depends(get_db), user=Depends(require_user)):
    b = _brand(s, slug)
    return {"runs": s.scalar(select(func.count(Run.id)).where(Run.brand_id == b.id))}
