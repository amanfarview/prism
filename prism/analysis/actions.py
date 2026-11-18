"""Actions: what to do next, ranked, each with the evidence behind it.

Rule-based and free. Every action links back to the answers that triggered it,
so nobody has to take a score on trust.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from ..models import Brand, Citation, Run, SiteAudit, now
from .report import by_engine, claims, memory_vs_search
from .stats import Filter, answers_for, entity_stats, mentions_by_answer


@dataclass
class Action:
    kind: str
    title: str
    why: str
    steps: list[str]
    priority: float
    evidence: dict = field(default_factory=dict)


def build(s, brand: Brand, days: int = 30) -> list[Action]:
    acts: list[Action] = []
    cur = answers_for(s, Filter(brand.id, since=now() - timedelta(days=days)))
    if not cur:
        return [Action("setup", "Run the first measurement",
                       "No analysed answers in this period yet.",
                       ["Enable at least one engine in Command Centre > Engines and add its key.",
                        "Use Run now on this brand, after checking the cost estimate."], 100)]
    mba = mentions_by_answer(s, [a.id for a in cur])
    tr = [(brand.name, "self")] + [(c.name, "competitor") for c in brand.competitors if c.active]
    prompts = {p.id: p for p in brand.prompts}

    # 1. Prompts a competitor owns and we are missing from
    byp = defaultdict(list)
    for a in cur:
        byp[a.prompt_id].append(a)
    ids = [a.id for a in cur]
    cits_by_answer = defaultdict(list)
    for i in range(0, len(ids), 900):
        for c in s.scalars(select(Citation).where(Citation.answer_id.in_(ids[i:i + 900]))):
            cits_by_answer[c.answer_id].append(c)
    for pid, ans in byp.items():
        if len(ans) < 6 or pid not in prompts:
            continue
        st = entity_stats(ans, mba, tr)
        me = st[brand.name]
        rival = max((e for e in st.values() if e.type == "competitor"), key=lambda e: e.rate, default=None)
        if rival and rival.rate >= 0.5 and me.rate <= 0.25:
            srcs = Counter(c.domain for a in ans for c in cits_by_answer[a.id] if c.owner_type == "third_party")
            p = prompts[pid]
            gap = rival.rate - me.rate
            acts.append(Action(
                "win_prompt", f"Win “{p.text}”",
                f"{rival.name} is named in {rival.rate:.0%} of answers, {brand.name} in {me.rate:.0%} "
                f"({len(ans)} answers).",
                ["Publish or update one page that answers this question directly, with a short quotable answer near the top.",
                 *([f"Get covered on the sources AI reads for it: {', '.join(d for d, _ in srcs.most_common(4))}."]
                   if srcs else []),
                 "Log the change in Impact so we can measure it against untouched prompts."],
                priority=gap * 100 * (1 + min(p.demand, 10000) / 10000),
                evidence={"prompt_id": pid, "rival": rival.name}))

    # 2. Third-party sources that shape answers we are absent from
    self_named = {a.id for a in cur if any(m.entity_type == "self" for m in mba.get(a.id, []))}
    absent = Counter()
    for a in cur:
        if a.id in self_named:
            continue
        for c in cits_by_answer[a.id]:
            if c.owner_type == "third_party":
                absent[c.domain] += 1
    for d, n in absent.most_common(5):
        if n >= 3:
            acts.append(Action("source", f"Get onto {d}",
                               f"Cited {n} times in answers that don't name {brand.name}.",
                               [f"Check what {d} says about the category and whether {brand.name} is listed.",
                                "Pitch an update, review, listing or comparison inclusion."],
                               priority=min(60, n * 4), evidence={"domain": d}))

    # 3. Own site not being cited
    own = sum(1 for a in cur for c in cits_by_answer[a.id] if c.owner_type == "own")
    comp = sum(1 for a in cur for c in cits_by_answer[a.id] if c.owner_type == "competitor")
    if own == 0 and comp >= 3:
        acts.append(Action("own_site", f"AI isn't citing {brand.domain or 'your site'}",
                           f"Competitor sites were cited {comp} times, yours never.",
                           ["Run the Site audit: check AI crawlers aren't blocked and pages render without JavaScript.",
                            "Add clear comparison, pricing-model and use-case pages that answer buyer questions."],
                           priority=70))

    # 4. Memory vs live search gap
    mvs = memory_vs_search(by_engine(s, brand, days))
    if mvs["memory"] is not None and mvs["search"] is not None and mvs["memory_n"] >= 10 and mvs["search_n"] >= 10:
        if mvs["search"] - mvs["memory"] >= 0.2:
            acts.append(Action("authority", "Models don't know you without searching",
                               f"Named in {mvs['search']:.0%} of live-search answers but {mvs['memory']:.0%} from memory.",
                               ["This is slow-moving authority: analyst coverage, Wikipedia/Wikidata, review sites, "
                                "partner directories and press that end up in training data.",
                                "Keep the search-side wins coming meanwhile; memory follows over months."], 40))
        elif mvs["memory"] - mvs["search"] >= 0.2:
            acts.append(Action("content", "Live search finds competitors' pages, not yours",
                               f"Named in {mvs['memory']:.0%} of memory answers but only {mvs['search']:.0%} with search.",
                               ["This is fixable quickly: crawlability, page structure and fresh comparison content.",
                                "Run the Site audit and check which sources live answers cite instead."], 65))

    # 5. Engine-specific weakness
    rows = [r for r in by_engine(s, brand, days) if r["n"] >= 10]
    if len(rows) >= 2:
        avg = sum(r["rate"] for r in rows) / len(rows)
        for r in rows:
            if avg - r["rate"] >= 0.25:
                acts.append(Action("engine", f"Weak on {r['label']}",
                                   f"Named in {r['rate']:.0%} there vs {avg:.0%} on average; {r['leader']} leads.",
                                   ["Check which sources this engine cites for your prompts (Sources, filtered by engine)."],
                                   priority=(avg - r["rate"]) * 80))

    # 6. Wrong facts
    wrong = claims(s, brand, days, "wrong")
    unresolved = [(c, a) for c, a in wrong if not c.resolved]
    if unresolved:
        top = Counter(c.attribute or "General" for c, _ in unresolved)
        acts.append(Action("facts", f"AI is getting {brand.name} facts wrong ({len(unresolved)})",
                           f"Most often about: {', '.join(k for k, _ in top.most_common(3))}.",
                           ["Fix the source pages AI cites for these statements, and state the correct fact plainly on your site.",
                            "Mark each claim resolved once fixed; Impact will show if it sticks."],
                           priority=85 + min(15, len(unresolved))))

    # 7. Stale answers
    last = s.scalar(select(Run).where(Run.brand_id == brand.id, Run.identical_to_previous_pct.is_not(None))
                    .order_by(Run.id.desc()).limit(1))
    if last and last.identical_to_previous_pct >= 60:
        acts.append(Action("stale", "Answers are repeating word for word",
                           f"{last.identical_to_previous_pct:.0f}% of run {last.id}'s answers match the previous run.",
                           ["Check engine settings for caching or a fixed seed; real AI answers vary between runs."], 90))

    # 8. Site audit failures
    audit = s.scalar(select(SiteAudit).where(SiteAudit.brand_id == brand.id).order_by(SiteAudit.id.desc()).limit(1))
    if audit:
        fails = [c for c in audit.results.get("checks", []) if c.get("status") == "fail"]
        if fails:
            acts.append(Action("technical", f"Fix {len(fails)} site issue(s) for AI crawlers",
                               "; ".join(c["name"] for c in fails[:4]),
                               [c.get("fix", "") for c in fails[:5] if c.get("fix")], priority=50 + 5 * len(fails)))

    return sorted(acts, key=lambda a: -a.priority)
