"""Build a demo database with the free demo engines (no API calls, no cost).

    PRISM_DATABASE_URL=sqlite:///./demo.db python scripts/demo_data.py

Creates the four portcos, runs 6 weekly runs per brand on the demo engines
(backdated), logs an example change, and ingests sample crawler log lines.
Demo answers are synthetic: use them to learn the app, never for reporting.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, update  # noqa: E402

from prism.analysis import crawlers  # noqa: E402
from prism.catalogue import ensure_defaults  # noqa: E402
from prism.collector import execute_run, plan_run  # noqa: E402
from prism.db import init_db, session_scope  # noqa: E402
from prism.models import Answer, Brand, Change, Engine, Run, now  # noqa: E402
from prism.seed import seed_example_facts, seed_portcos  # noqa: E402


def main(weeks: int = 6):
    init_db()
    with session_scope() as s:
        ensure_defaults(s)
        seed_portcos(s)
        seed_example_facts(s)
        for e in s.scalars(select(Engine).where(Engine.provider == "mock")):
            e.enabled = True
        brands = [b.id for b in s.scalars(select(Brand))]
    for bid in brands:
        for w in range(weeks, 0, -1):
            with session_scope() as s:
                rid = plan_run(s, bid, samples=3, triggered_by="demo").run.id
            execute_run(rid, retry_sleep=0)
            when = now() - timedelta(days=7 * w - 3)
            with session_scope() as s:
                s.execute(update(Run).where(Run.id == rid).values(created_at=when, started_at=when, finished_at=when))
                s.execute(update(Answer).where(Answer.run_id == rid).values(created_at=when))
    with session_scope() as s:
        b = s.scalar(select(Brand).where(Brand.slug == "amplience"))
        ps = [p.id for p in b.prompts][:2]
        s.add(Change(brand_id=b.id, title="Added a comparison table to the headless CMS page",
                     url="https://amplience.com/", shipped_on=now() - timedelta(days=21), window_days=21,
                     target_prompt_ids=ps, created_by="demo"))
        logs = "\n".join([
            '1.2.3.4 - - [10/Nov/2026:10:00:00 +0000] "GET /platform/dam HTTP/1.1" 200 512 "-" "Mozilla/5.0; compatible; ChatGPT-User/1.0"',
            '1.2.3.5 - - [10/Nov/2026:10:01:00 +0000] "GET /compare/contentful HTTP/1.1" 200 512 "-" "Mozilla/5.0 (compatible; OAI-SearchBot/1.0)"',
            '1.2.3.6 - - [10/Nov/2026:10:02:00 +0000] "GET /pricing HTTP/1.1" 404 128 "-" "Mozilla/5.0 (compatible; ClaudeBot/1.0)"',
            '1.2.3.7 - - [11/Nov/2026:09:00:00 +0000] "GET /platform/dam HTTP/1.1" 200 512 "-" "Mozilla/5.0 Perplexity-User/1.0"',
            '1.2.3.8 - - [11/Nov/2026:09:05:00 +0000] "GET / HTTP/1.1" 200 512 "-" "Mozilla/5.0 Chrome/131"',
        ])
        hits = crawlers.ingest(s, b.id, logs)
        from prism.models import CrawlerHit
        s.flush()
        s.execute(update(CrawlerHit).values(at=now() - timedelta(days=3)))
    print("demo ready", hits)


if __name__ == "__main__":
    main()
