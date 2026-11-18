"""Runs, budgets, freshness, stats, impact and the free analysers."""
from datetime import timedelta

from sqlalchemy import select, update

from prism.analysis import crawlers, demand, impact, site_audit
from prism.analysis.extract import entities_for, find_mentions
from prism.analysis.stats import direction, wilson
from prism.collector import execute_run, plan_run
from prism.models import Answer, Brand, Change, Engine, Run, SpendEntry, now


def _enable_mock(s, price_per_call=0.0):
    for e in s.scalars(select(Engine).where(Engine.provider == "mock")):
        e.enabled = True
        e.price_per_call = price_per_call


def _amp(s):
    return s.scalar(select(Brand).where(Brand.slug == "amplience"))


def test_mentions_order_aliases_and_false_positives(db):
    with db() as s:
        b = _amp(s)
        ents = entities_for(b, list(b.competitors))
        text = "Do a sanity check first. Contentful is common; AEM and Amplience Dynamic Content also fit. Sanity too."
        found = find_mentions(text, ents)
        names = [e.name for e, _, _ in found]
        assert names == ["Contentful", "Adobe Experience Manager", "Amplience", "Sanity"]


def test_run_end_to_end_and_freshness(db):
    with db() as s:
        _enable_mock(s)
        rid = plan_run(s, _amp(s).id, samples=2).run.id
    r = execute_run(rid, retry_sleep=0)
    assert r.status == "done" and r.done_calls == r.planned_calls > 0
    with db() as s:
        a = s.scalar(select(Answer).where(Answer.run_id == rid, Answer.engine_key == "demo_search"))
        assert a.response_text and a.text_hash and a.analysed
    # second run whose answers are forced identical to the first -> freshness flags 100%
    with db() as s:
        rid2 = plan_run(s, _amp(s).id, samples=2).run.id
    execute_run(rid2, retry_sleep=0)
    with db() as s:
        first = {(a.prompt_id, a.engine_key, a.sample_idx): a.text_hash
                 for a in s.scalars(select(Answer).where(Answer.run_id == rid))}
        for a in s.scalars(select(Answer).where(Answer.run_id == rid2)):
            a.text_hash = first[(a.prompt_id, a.engine_key, a.sample_idx)]
        from prism.analysis import freshness
        res = freshness.check_run(s, s.get(Run, rid2))
        assert res["identical_to_previous_pct"] == 100.0


def test_budget_refuses_and_stops(db):
    with db() as s:
        _enable_mock(s, price_per_call=1.0)  # $1 per call
        from prism.models import Budget
        s.scalar(select(Budget).where(Budget.scope == "global")).monthly_cap_usd = 5.0
        plan = plan_run(s, _amp(s).id, samples=1, create=False)
        assert not plan.estimate.ok and plan.estimate.problems  # 20 calls x $1 > $5
        rid = plan_run(s, _amp(s).id, samples=1).run.id  # force it through to test the in-run guard
    r = execute_run(rid, retry_sleep=0)
    assert r.status == "budget_stopped"
    with db() as s:
        from sqlalchemy import func
        spent = s.scalar(select(func.sum(SpendEntry.cost_usd)))
        assert spent <= 5.0 + 1e-9


def test_wilson_and_direction():
    lo, hi = wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert direction(10, 100, 30, 100)[0] == "up"
    assert direction(10, 100, 12, 100)[0] == "no clear change"
    assert direction(1, 3, 2, 3)[0] == "too little data"


def test_impact_difference_in_differences(db):
    with db() as s:
        _enable_mock(s)
        b = _amp(s)
        rid = plan_run(s, b.id, samples=4).run.id
    execute_run(rid, retry_sleep=0)
    with db() as s:
        b = _amp(s)
        t0 = now() - timedelta(days=10)
        target = b.prompts[0].id
        ans = list(s.scalars(select(Answer).where(Answer.run_id == rid)))
        # half the answers 'before', half 'after'; make the target prompt always named after
        from prism.models import Mention
        for a in ans:  # samples 0,1 before the change, 2,3 after: every prompt has both
            a.created_at = t0 - timedelta(days=3) if a.sample_idx < 2 else t0 + timedelta(days=3)
            if a.prompt_id == target and a.created_at > t0:
                if not any(m.entity_type == "self" for m in s.scalars(select(Mention).where(Mention.answer_id == a.id))):
                    s.add(Mention(answer_id=a.id, entity_type="self", entity_name=b.name, position=1))
        c = Change(brand_id=b.id, title="t", shipped_on=t0, window_days=7, target_prompt_ids=[target])
        s.add(c)
        s.flush()
        res = impact.evaluate(s, c, min_n=3)
        assert res["n"]["post_t"] > 0 and res["rates"]["post_t"] == 1.0
        assert res["did"] is not None and res["ci"][0] <= res["did"] <= res["ci"][1]


def test_demand_suggestions():
    csv_text = "Top queries,Clicks,Impressions\nbest headless cms for retail,10,900\namplience login,50,2000\ncms,1,50\nwhich dam is best for ecommerce?,3,300\n"
    out = demand.suggestions(csv_text, "Amplience")
    qs = [x["query"] for x in out]
    assert "best headless cms for retail" in qs and "which dam is best for ecommerce?" in qs
    assert "cms" not in qs and "amplience login" not in qs


def test_robots_rules():
    rules = site_audit._robots_rules("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\nDisallow: /private\n")
    assert not site_audit.bot_allowed(rules, "GPTBot")
    assert site_audit.bot_allowed(rules, "OAI-SearchBot")
    assert not site_audit.bot_allowed(rules, "OAI-SearchBot", "/private/x")


def test_crawler_ingest(db):
    with db() as s:
        b = _amp(s)
        body = ('1.1.1.1 - - [10/Nov/2026:10:00:00 +0000] "GET /a HTTP/1.1" 200 5 "-" "Mozilla/5.0; ChatGPT-User/1.0"\n'
                '1.1.1.1 - - [10/Nov/2026:10:00:00 +0000] "GET /b HTTP/1.1" 200 5 "-" "Mozilla/5.0 Chrome"\n'
                '{"user_agent": "ClaudeBot/1.0", "path": "/c", "status": 404, "timestamp": "2026-11-10T10:00:00Z"}')
        res = crawlers.ingest(s, b.id, body)
        assert res == {"lines": 3, "ai_hits": 2}


def test_reanalyse_after_alias_change(db):
    with db() as s:
        _enable_mock(s)
        rid = plan_run(s, _amp(s).id, samples=1).run.id
    execute_run(rid, retry_sleep=0)
    with db() as s:
        s.execute(update(Answer).where(Answer.run_id == rid).values(response_text="Try ACME Corp or Contentful."))
        b = _amp(s)
        b.aliases = ["ACME Corp"]
        from prism.analysis import extract
        extract.analyse_run(s, rid)
        s.flush()
        from prism.models import Mention
        selfs = s.scalars(select(Mention).join(Answer, Answer.id == Mention.answer_id)
                          .where(Answer.run_id == rid, Mention.entity_type == "self")).all()
        assert selfs and all(m.position == 1 for m in selfs)
