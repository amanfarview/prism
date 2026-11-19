"""Command Centre (admins): engines, keys, budgets, schedules, brands, settings, tokens, audit."""
from __future__ import annotations

import json
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select

from ..budget import caps, month_key, month_spend
from ..collector import test_engine
from ..common import audit, get_setting, set_setting
from ..config import get_settings
from ..models import (ApiToken, AuditLog, Brand, Budget, Competitor, Engine, Market, ProviderKey, Run, Schedule,
                      SpendEntry, now)
from ..scheduler import RUNS_PER_MONTH, next_run_time
from ..secrets import forget, set_secret
from .app import flash, get_db, redirect, render, require_admin
from .auth import check_csrf, hash_token, new_token

router = APIRouter()


def projected(s, sch: Schedule) -> float:
    b = s.get(Brand, sch.brand_id)
    q = select(Engine).where(Engine.enabled == True)
    if sch.engine_keys:
        q = q.where(Engine.key.in_(sch.engine_keys))
    per_call = sum(e.est_cost() for e in s.scalars(q))
    n_prompts = sum(1 for p in b.prompts if p.active)
    n_m = max(1, sum(1 for m in b.markets if m.active))
    return per_call * n_prompts * n_m * sch.samples * RUNS_PER_MONTH.get(sch.cadence, 4.33)


# ---------------------------------------------------------------- overview
@router.get("")
@router.get("/")
def home(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    gcap, pcaps = caps(s)
    spent = month_spend(s)
    by_prov = dict(s.execute(select(SpendEntry.provider, func.sum(SpendEntry.cost_usd))
                             .where(SpendEntry.month == month_key()).group_by(SpendEntry.provider)).all())
    engines = list(s.scalars(select(Engine).order_by(Engine.provider, Engine.key)))
    keys = {k.provider: k for k in s.scalars(select(ProviderKey))}
    scheds = list(s.scalars(select(Schedule).where(Schedule.active == True)))
    proj = sum(projected(s, x) for x in scheds)
    runs = list(s.scalars(select(Run).order_by(Run.id.desc()).limit(10)))
    bmap = {b.id: b for b in s.scalars(select(Brand))}
    return render(request, "cc_home.html", s, user, gcap=gcap, pcaps=pcaps, spent=spent, by_prov=by_prov,
                  engines=engines, keys=keys, scheds=scheds, proj=proj, runs=runs, bmap=bmap,
                  backend=get_settings().secret_backend, cc="home")


# ---------------------------------------------------------------- engines
@router.get("/engines")
def engines(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(Engine).order_by(Engine.enabled.desc(), Engine.mode.desc(), Engine.label)))
    keys = {k.provider: k for k in s.scalars(select(ProviderKey))}
    return render(request, "cc_engines.html", s, user, rows=rows, keys=keys, cc="engines")


@router.post("/engines/{key}/save")
async def engine_save(request: Request, key: str, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    e = s.scalar(select(Engine).where(Engine.key == key))
    before = {"model": e.model, "enabled": e.enabled}
    e.enabled = f.get("enabled") == "on"
    e.model = str(f.get("model", e.model)).strip()
    for fld in ("price_in_per_m", "price_out_per_m", "price_per_search", "price_per_call", "est_searches"):
        try:
            setattr(e, fld, float(f.get(fld, getattr(e, fld))))
        except ValueError:
            pass
    for fld in ("est_in_tokens", "est_out_tokens", "concurrency"):
        try:
            setattr(e, fld, int(f.get(fld, getattr(e, fld))))
        except ValueError:
            pass
    raw = str(f.get("settings", "")).strip()
    if raw:
        try:
            e.settings = json.loads(raw)
        except ValueError:
            flash(request, "Settings must be valid JSON; other changes saved.", "err")
    audit(s, user.email, "engine_saved", engine=key, before=before, after={"model": e.model, "enabled": e.enabled})
    flash(request, f"{e.label} saved.")
    return redirect("/cc/engines")


@router.post("/engines/{key}/test")
def engine_test(request: Request, key: str, csrf: str = Form(""), s=Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf)
    e = s.scalar(select(Engine).where(Engine.key == key))
    ok, msg, cost = test_engine(s, e, user.email)
    flash(request, f"{e.label}: {msg}", "ok" if ok else "err")
    return redirect("/cc/engines")


# ---------------------------------------------------------------- keys
@router.get("/keys")
def keys(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(ProviderKey).order_by(ProviderKey.provider)))
    from ..config import get_settings
    return render(request, "cc_keys.html", s, user, rows=rows, backend=get_settings().secret_backend, cc="keys")


@router.post("/keys/{provider}")
def key_save(request: Request, provider: str, value: str = Form(...), csrf: str = Form(""), s=Depends(get_db),
             user=Depends(require_admin)):
    check_csrf(request, csrf)
    row = s.scalar(select(ProviderKey).where(ProviderKey.provider == provider))
    if not row:
        raise HTTPException(404)
    value = value.strip()
    if len(value) < 8:
        flash(request, "That doesn't look like a key.", "err")
        return redirect("/cc/keys")
    try:
        set_secret(row.secret_name, value)
    except Exception as e:
        flash(request, f"Couldn't store the key: {e}", "err")
        return redirect("/cc/keys")
    forget(row.secret_name)
    row.last4, row.updated_at, row.updated_by = value[-4:], now(), user.email
    audit(s, user.email, "key_set", provider=provider, last4=row.last4)  # never the key itself
    from ..config import get_settings
    where = {"keyvault": "Azure Key Vault", "file": "the server's private secrets file"}.get(
        get_settings().secret_backend, "this process only (dev mode)")
    flash(request, f"{provider} key stored in {where} (ends {row.last4}). Test it from Engines.")
    return redirect("/cc/keys")


# ---------------------------------------------------------------- budget
@router.get("/budget")
def budget(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(Budget).order_by(Budget.scope.desc(), Budget.provider)))
    m = month_key()
    ledger = s.execute(select(SpendEntry.provider, SpendEntry.engine_key, SpendEntry.purpose,
                              func.count(SpendEntry.id), func.sum(SpendEntry.cost_usd))
                       .where(SpendEntry.month == m)
                       .group_by(SpendEntry.provider, SpendEntry.engine_key, SpendEntry.purpose)
                       .order_by(func.sum(SpendEntry.cost_usd).desc())).all()
    by_brand = s.execute(select(SpendEntry.brand_id, func.sum(SpendEntry.cost_usd)).where(SpendEntry.month == m)
                         .group_by(SpendEntry.brand_id)).all()
    months = s.execute(select(SpendEntry.month, func.sum(SpendEntry.cost_usd)).group_by(SpendEntry.month)
                       .order_by(SpendEntry.month.desc()).limit(12)).all()
    bmap = {b.id: b.name for b in s.scalars(select(Brand))}
    providers = sorted({e.provider for e in s.scalars(select(Engine)) if e.provider != "mock"})
    return render(request, "cc_budget.html", s, user, rows=rows, ledger=ledger, by_brand=by_brand, months=months,
                  bmap=bmap, providers=providers, spent=month_spend(s), month=m, cc="budget")


@router.post("/budget/save")
async def budget_save(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    g = s.scalar(select(Budget).where(Budget.scope == "global"))
    g.monthly_cap_usd = float(f.get("global_cap") or g.monthly_cap_usd)
    g.alert_pcts = [int(x) for x in str(f.get("alerts", "50,80,100")).split(",") if x.strip().isdigit()]
    for k, v in f.multi_items():
        if k.startswith("cap_"):
            prov = k[4:]
            row = s.scalar(select(Budget).where(Budget.scope == "provider", Budget.provider == prov))
            if v.strip() == "":
                if row:
                    s.delete(row)
                continue
            if not row:
                row = Budget(scope="provider", provider=prov)
                s.add(row)
            row.monthly_cap_usd = float(v)
            row.alert_pcts = g.alert_pcts
    audit(s, user.email, "budget_saved", global_cap=g.monthly_cap_usd)
    flash(request, "Budgets saved. Runs that would break a cap are refused, and running ones stop at the cap.")
    return redirect("/cc/budget")


# ---------------------------------------------------------------- schedules
@router.get("/schedules")
def schedules(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(Schedule).order_by(Schedule.brand_id)))
    bmap = {b.id: b for b in s.scalars(select(Brand))}
    engines = list(s.scalars(select(Engine).where(Engine.enabled == True).order_by(Engine.label)))
    proj = {x.id: projected(s, x) for x in rows}
    gcap, _ = caps(s)
    return render(request, "cc_schedules.html", s, user, rows=rows, bmap=bmap, engines=engines, proj=proj,
                  total=sum(v for x, v in zip(rows, proj.values()) if x.active), gcap=gcap, cc="schedules")


@router.post("/schedules/{sid}/save")
async def schedule_save(request: Request, sid: int, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    x = s.get(Schedule, sid)
    x.active = f.get("active") == "on"
    x.cadence = str(f.get("cadence", x.cadence))
    x.weekday = int(f.get("weekday", x.weekday))
    x.hour_utc = int(f.get("hour_utc", x.hour_utc))
    x.samples = max(1, min(20, int(f.get("samples", x.samples))))
    x.engine_keys = [v for k, v in f.multi_items() if k == "engine"]
    x.next_run_at = next_run_time(x, now()) if x.active else None
    audit(s, user.email, "schedule_saved", schedule=sid, active=x.active, cadence=x.cadence, samples=x.samples)
    flash(request, "Schedule saved.")
    return redirect("/cc/schedules")


# ---------------------------------------------------------------- brands
@router.get("/brands")
def brands(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(Brand).order_by(Brand.name)))
    return render(request, "cc_brands.html", s, user, rows=rows, cc="brands")


@router.post("/brands/new")
def brand_new(request: Request, name: str = Form(...), domain: str = Form(""), csrf: str = Form(""),
              s=Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf)
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:90]
    if s.scalar(select(Brand).where(Brand.slug == slug)):
        flash(request, "A brand with that name exists.", "err")
        return redirect("/cc/brands")
    b = Brand(name=name.strip(), slug=slug, domain=domain.strip().lower())
    s.add(b)
    s.flush()
    s.add(Market(brand_id=b.id, country="GB", language="en"))
    s.add(Schedule(brand_id=b.id, active=False))
    audit(s, user.email, "brand_added", brand=slug)
    return redirect(f"/cc/brands/{slug}")


@router.get("/brands/{slug}")
def brand_edit(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    conn = get_setting(s, f"connector:{slug}", {}) or {}
    return render(request, "cc_brand.html", s, user, b=b, conn=conn, cc="brands")


@router.post("/brands/{slug}/save")
async def brand_save(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    b.name = str(f.get("name", b.name)).strip()
    b.domain = str(f.get("domain", b.domain)).strip().lower()
    b.category = str(f.get("category", b.category)).strip()
    b.aliases = [a.strip() for a in str(f.get("aliases", "")).split(",") if a.strip()]
    b.active = f.get("active") == "on"
    b.notes = str(f.get("notes", ""))
    audit(s, user.email, "brand_saved", brand=slug)
    flash(request, "Saved. Use Re-analyse on recent runs to apply new names to stored answers (free).")
    return redirect(f"/cc/brands/{slug}")


@router.post("/brands/{slug}/competitor")
async def competitor_save(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    cid = int(f.get("id") or 0)
    c = s.get(Competitor, cid) if cid else Competitor(brand_id=b.id)
    c.name = str(f.get("name", "")).strip()
    c.domain = str(f.get("domain", "")).strip().lower()
    c.aliases = [a.strip() for a in str(f.get("aliases", "")).split(",") if a.strip()]
    c.active = f.get("active", "on") == "on"
    if not cid:
        s.add(c)
    audit(s, user.email, "competitor_saved", brand=slug, competitor=c.name)
    return redirect(f"/cc/brands/{slug}")


@router.post("/brands/{slug}/market")
async def market_save(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    mid = int(f.get("id") or 0)
    if mid:
        m = s.get(Market, mid)
        m.active = not m.active
    else:
        s.add(Market(brand_id=b.id, country=str(f.get("country", "GB")).upper()[:2],
                     language=str(f.get("language", "en")).lower()[:10], city=str(f.get("city", ""))[:100]))
    audit(s, user.email, "market_saved", brand=slug)
    return redirect(f"/cc/brands/{slug}")


@router.post("/brands/{slug}/connector")
async def connector_save(request: Request, slug: str, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    hub = str(f.get("hub", "")).strip()
    cfg = {"type": "amplience", "hub": hub, "strip_prefix": str(f.get("strip_prefix", "")).strip(),
           "locale_prefix": f.get("locale_prefix") == "on"} if hub else {}
    set_setting(s, f"connector:{slug}", cfg)
    audit(s, user.email, "connector_saved", brand=slug, hub=hub)
    flash(request, "Content connector saved." if hub else "Content connector removed.")
    return redirect(f"/cc/brands/{slug}")


@router.post("/brands/{slug}/logos/fetch")
def logos_fetch(request: Request, slug: str, csrf: str = Form(""), s=Depends(get_db), user=Depends(require_admin)):
    """Fetch the brand's and its competitors' icons again from their own sites (free; uploads are kept)."""
    check_csrf(request, csrf)
    from ..analysis import logos
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    if not b:
        raise HTTPException(404, "unknown brand")
    doms = [b.domain] + [c.domain for c in b.competitors if c.active]
    res = logos.refresh(s, doms, force=True)
    audit(s, user.email, "logos_fetched", brand=slug, **res)
    flash(request, f"Logos: checked {res['checked']} site(s), found {res['found']}. Sites without a usable icon show initials.")
    return redirect(f"/cc/brands/{slug}")


@router.post("/brands/{slug}/logo")
async def logo_upload(request: Request, slug: str, file: UploadFile, csrf: str = Form(""), s=Depends(get_db),
                      user=Depends(require_admin)):
    """Upload the brand's own logo (PNG, JPEG, WebP, GIF or ICO, up to 512 KB). Replaces the fetched icon."""
    check_csrf(request, csrf)
    from ..analysis import logos
    b = s.scalar(select(Brand).where(Brand.slug == slug))
    if not b:
        raise HTTPException(404, "unknown brand")
    data = await file.read(logos.MAX_IMAGE + 1)
    err = logos.store_upload(s, b.domain, data, user.email)
    if err:
        flash(request, err, "err")
    else:
        audit(s, user.email, "logo_uploaded", brand=slug, bytes=len(data))
        flash(request, f"Logo for {b.name} saved.")
    return redirect(f"/cc/brands/{slug}")


# ---------------------------------------------------------------- settings
@router.get("/settings")
def settings_page(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    return render(request, "cc_settings.html", s, user, llm=get_setting(s, "llm_extraction", {}),
                  d=get_setting(s, "defaults", {}), cc="settings")


@router.post("/settings/save")
async def settings_save(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    f = await request.form()
    check_csrf(request, f.get("csrf"))
    llm = dict(get_setting(s, "llm_extraction", {}) or {})
    llm.update(enabled=f.get("llm_enabled") == "on", provider=str(f.get("llm_provider", "anthropic")),
               model=str(f.get("llm_model", llm.get("model", ""))).strip(),
               price_in_per_m=float(f.get("llm_pin") or llm.get("price_in_per_m", 1)),
               price_out_per_m=float(f.get("llm_pout") or llm.get("price_out_per_m", 5)))
    set_setting(s, "llm_extraction", llm)
    d = dict(get_setting(s, "defaults", {}) or {})
    d.update(samples=max(1, int(f.get("samples") or 3)), identical_alert_pct=int(f.get("identical_alert_pct") or 60))
    set_setting(s, "defaults", d)
    audit(s, user.email, "settings_saved", llm_enabled=llm["enabled"])
    flash(request, "Settings saved.")
    return redirect("/cc/settings")


# ---------------------------------------------------------------- API tokens
@router.get("/tokens")
def tokens(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(ApiToken).order_by(ApiToken.id.desc())))
    new = request.session.pop("new_token", None)
    return render(request, "cc_tokens.html", s, user, rows=rows, new=new, cc="tokens")


@router.post("/tokens/new")
def token_new(request: Request, name: str = Form(...), scope: str = Form("read"), csrf: str = Form(""),
              s=Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf)
    tok = new_token()
    s.add(ApiToken(name=name.strip()[:120], token_hash=hash_token(tok), scope="write" if scope == "write" else "read",
                   created_by=user.email))
    audit(s, user.email, "token_created", name=name, scope=scope)
    request.session["new_token"] = tok  # shown once
    return redirect("/cc/tokens")


@router.post("/tokens/{tid}/revoke")
def token_revoke(request: Request, tid: int, csrf: str = Form(""), s=Depends(get_db), user=Depends(require_admin)):
    check_csrf(request, csrf)
    t = s.get(ApiToken, tid)
    t.revoked = True
    audit(s, user.email, "token_revoked", name=t.name)
    return redirect("/cc/tokens")


# ---------------------------------------------------------------- audit log
@router.get("/audit")
def audit_log(request: Request, s=Depends(get_db), user=Depends(require_admin)):
    rows = list(s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)))
    return render(request, "cc_audit.html", s, user, rows=rows, cc="audit")


_ = defaultdict  # keep import for future use
