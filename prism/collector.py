"""Runs: plan -> estimate -> execute -> analyse.

A run asks every active prompt, in every market, on every chosen engine,
`samples` times. Each call is budget-checked before it goes out. Answers are
stored word for word, then analysed (free), fact-checked by pattern (free),
checked for freshness, and optionally enriched by the AI pass (paid).
"""
from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from sqlalchemy import select

from .analysis import content, extract, facts, freshness, llm_extract
from .budget import BudgetGuard, Estimate, call_cost, check_alerts, estimate, record_spend
from .common import audit, get_setting, log, notify
from .config import get_settings
from .db import session_scope
from .models import Answer, Brand, Engine, Market, Prompt, Run, now
from .providers import CallContext, ProviderError, get_adapter


@dataclass
class EngineSnap:
    key: str
    provider: str
    adapter: str
    mode: str
    model: str
    settings: dict
    est: float
    concurrency: int
    pin: float
    pout: float
    psearch: float
    pcall: float

    def cost(self, tin, tout, searches, reported=0.0) -> float:
        e = Engine(price_in_per_m=self.pin, price_out_per_m=self.pout, price_per_search=self.psearch,
                   price_per_call=self.pcall)
        return call_cost(e, tin, tout, searches, reported)


@dataclass
class Task:
    prompt_id: int
    prompt_text: str
    market_id: int | None
    country: str
    language: str
    city: str
    engine: EngineSnap
    sample: int


@dataclass
class Plan:
    run: Run
    estimate: Estimate
    tasks: int
    engine_calls: dict = field(default_factory=dict)


def _markets(brand: Brand) -> list[Market | None]:
    ms = [m for m in brand.markets if m.active]
    return ms or [None]


def _engines(s, keys: list[str] | None) -> list[Engine]:
    q = select(Engine).where(Engine.enabled.is_(True))
    if keys:
        q = q.where(Engine.key.in_(keys))
    return list(s.scalars(q))


def plan_run(s, brand_id: int, samples: int | None = None, engine_keys: list[str] | None = None,
             triggered_by: str = "manual", schedule_id: int | None = None, prompt_ids: list[int] | None = None,
             create: bool = True) -> Plan:
    brand = s.get(Brand, brand_id)
    samples = samples or int((get_setting(s, "defaults", {}) or {}).get("samples", 3))
    prompts = [p for p in brand.prompts if p.active and (not prompt_ids or p.id in prompt_ids)]
    engines = _engines(s, engine_keys)
    n_m = len(_markets(brand))
    engine_calls = {e.key: len(prompts) * n_m * samples for e in engines}
    est = estimate(s, engine_calls)
    run = Run(brand_id=brand_id, schedule_id=schedule_id, triggered_by=triggered_by, samples=samples,
              engine_keys=[e.key for e in engines], planned_calls=sum(engine_calls.values()),
              est_cost_usd=est.total, status="planned",
              note=f"prompts={','.join(str(p.id) for p in prompts)}" if prompt_ids else "")
    if create:
        s.add(run)
        s.flush()
    return Plan(run=run, estimate=est, tasks=run.planned_calls, engine_calls=engine_calls)


def _entities_for_mock(brand: Brand) -> tuple[list[dict], dict]:
    ents = [{"name": brand.name, "domain": brand.domain}] + [
        {"name": c.name, "domain": c.domain} for c in brand.competitors if c.active]
    rnd = random.Random(brand.slug)
    bias = {e["name"]: round(rnd.uniform(0.2, 0.7), 2) for e in ents}
    bias[brand.name] = 0.5
    return ents, bias


def _build_tasks(s, run: Run) -> tuple[list[Task], list[EngineSnap]]:
    brand = s.get(Brand, run.brand_id)
    only = None
    if run.note.startswith("prompts="):
        only = {int(x) for x in run.note[8:].split(",") if x}
    prompts = [p for p in brand.prompts if p.active and (only is None or p.id in only)]
    engines = _engines(s, run.engine_keys)
    ents, bias = _entities_for_mock(brand)
    snaps = []
    for e in engines:
        st = dict(e.settings or {})
        if e.provider == "mock":
            st.update({"_entities": ents, "_bias": bias})
        snaps.append(EngineSnap(e.key, e.provider, e.adapter, e.mode, e.model, st, e.est_cost(),
                                max(1, e.concurrency), e.price_in_per_m, e.price_out_per_m,
                                e.price_per_search, e.price_per_call))
    tasks = []
    for p in prompts:
        for m in _markets(brand):
            for e in snaps:
                for i in range(run.samples):
                    tasks.append(Task(p.id, p.text, m.id if m else None, m.country if m else "GB",
                                      m.language if m else "en", m.city if m else "", e, i))
    random.Random(run.id).shuffle(tasks)  # spread load across engines
    return tasks, snaps


def execute_run(run_id: int, max_workers: int | None = None, retry_sleep: float = 2.0) -> Run:
    max_workers = max_workers or get_settings().max_workers
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run.status not in ("planned", "queued", "failed"):
            raise RuntimeError(f"Run {run_id} is {run.status}")
        tasks, snaps = _build_tasks(s, run)
        guard = BudgetGuard(s)
        run.status, run.started_at = "running", now()
        brand_id = run.brand_id
    sems = {e.key: threading.Semaphore(e.concurrency) for e in snaps}
    counters = defaultdict(int)
    lock = threading.Lock()

    def work(t: Task):
        if not guard.reserve(t.engine.provider, t.engine.est):
            return "skipped"
        adapter = get_adapter(t.engine.adapter)
        settings = dict(t.engine.settings)
        if t.engine.provider == "mock":
            settings["_salt"] = f"{run_id}-{t.sample}-{t.engine.key}"
        ctx = CallContext(prompt=t.prompt_text, country=t.country, language=t.language, city=t.city,
                          model=t.engine.model, mode=t.engine.mode, settings=settings)
        res, err = None, ""
        with sems[t.engine.key]:
            for attempt in range(3):
                try:
                    res = adapter.call(ctx)
                    break
                except ProviderError as e:
                    err = str(e)
                    if not e.retryable:
                        break
                    time.sleep(retry_sleep * (2 ** attempt))
                except Exception as e:  # never let one call kill the run
                    err = f"{type(e).__name__}: {e}"
                    break
        cost = t.engine.cost(res.input_tokens, res.output_tokens, res.search_calls, res.extra_cost_usd) if res else 0.0
        guard.settle(t.engine.provider, t.engine.est, cost)
        with session_scope() as s2:
            a = Answer(run_id=run_id, brand_id=brand_id, prompt_id=t.prompt_id, market_id=t.market_id,
                       engine_key=t.engine.key, mode=t.engine.mode, sample_idx=t.sample, model=t.engine.model,
                       request_settings={k: v for k, v in t.engine.settings.items() if not k.startswith("_")},
                       prompt_text=t.prompt_text, status="ok" if res else "error", error=err[:2000])
            if res:
                a.model = res.model or t.engine.model
                a.response_text = res.text
                a.raw = res.raw_text()
                a.citations = [{"url": c.url, "title": c.title, "in_text": c.cited_in_text} for c in res.citations]
                a.search_queries = res.search_queries[:20]
                a.input_tokens, a.output_tokens = res.input_tokens, res.output_tokens
                a.search_calls, a.latency_ms, a.cost_usd = res.search_calls, res.latency_ms, cost
                a.text_hash = extract.text_hash(res.text)
            s2.add(a)
            record_spend(s2, provider=t.engine.provider, engine_key=t.engine.key, cost=cost, brand_id=brand_id,
                         run_id=run_id)
        with lock:
            counters["ok" if res else "err"] += 1
            counters["cost"] += cost
        return "ok" if res else "error"

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(work, t) for t in tasks]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:  # pragma: no cover
                log.exception("task crashed: %s", e)
                counters["err"] += 1

    with session_scope() as s:
        run = s.get(Run, run_id)
        run.done_calls, run.failed_calls = counters["ok"], counters["err"]
        run.cost_usd = round(counters["cost"], 4)
        extract.analyse_run(s, run_id)
        facts.pattern_check_run(s, run_id)
        s.flush()
        try:
            content.map_brand_citations(s, s.get(Brand, run.brand_id))
        except Exception as e:  # mapping is best-effort
            log.warning("content mapping failed: %s", e)
        fr = freshness.check_run(s, run)
        if guard.stopped:
            run.status = "budget_stopped"
            run.note = (run.note + f" | stopped: {guard.reason}").strip(" |")
        elif counters["ok"] == 0:
            run.status = "failed"
        elif counters["err"]:
            run.status = "partial"
        else:
            run.status = "done"
        run.finished_at = now()
        ai = llm_extract.extract_run(s, run_id)
        run.cost_usd = round(run.cost_usd + float(ai.get("cost_usd", 0) or 0), 4)
        audit(s, run.triggered_by, "run_finished", run_id=run_id, status=run.status, cost=run.cost_usd,
              freshness=fr, ai=ai)
        check_alerts(s)
        brand = s.get(Brand, run.brand_id)
        thr = float((get_setting(s, "defaults", {}) or {}).get("identical_alert_pct", 60))
        if fr.get("identical_to_previous_pct") is not None and fr["identical_to_previous_pct"] >= thr:
            notify("stale answers", f"{brand.name} run {run_id}: {fr['identical_to_previous_pct']}% of answers "
                                    f"identical to the previous run. Check engine settings for caching.")
        if run.status in ("failed", "budget_stopped", "partial"):
            notify(f"run {run.status}", f"{brand.name} run {run_id}: {run.done_calls} ok, {run.failed_calls} failed, "
                                        f"${run.cost_usd:.2f}. {run.note}")
        s.flush()
        s.expunge(run)
    return run


def test_engine(s, engine: Engine, user: str = "system") -> tuple[bool, str, float]:
    """One cheap real call to check key, model and parsing. Costs one call."""
    adapter = get_adapter(engine.adapter)
    ctx = CallContext(prompt="Which project management tools are popular with small teams?",
                      model=engine.model, mode=engine.mode,
                      settings={**(engine.settings or {}), "_entities": [{"name": "Asana", "domain": "asana.com"}],
                                "_salt": str(time.time())})
    try:
        res = adapter.call(ctx)
    except Exception as e:
        engine.last_test_at, engine.last_test_ok, engine.last_test_msg = now(), False, str(e)[:1000]
        audit(s, user, "engine_test", engine=engine.key, ok=False)
        return False, str(e), 0.0
    cost = call_cost(engine, res.input_tokens, res.output_tokens, res.search_calls, res.extra_cost_usd)
    record_spend(s, provider=engine.provider, engine_key=engine.key, cost=cost, purpose="test")
    msg = (f"OK in {res.latency_ms} ms, {len(res.text)} chars, {len(res.citations)} citations, "
           f"{res.search_calls} searches, model {res.model}, cost ${cost:.4f}")
    engine.last_test_at, engine.last_test_ok, engine.last_test_msg = now(), True, msg
    audit(s, user, "engine_test", engine=engine.key, ok=True, cost=cost)
    return True, msg, cost
