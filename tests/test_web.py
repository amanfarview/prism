"""Web app smoke tests: every page renders; API needs a token in production auth mode."""
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from prism.collector import execute_run, plan_run
from prism.models import Brand, Engine


def _client():
    from prism.web.app import create_app
    return TestClient(create_app())


def test_pages_render(db):
    with db() as s:
        for e in s.scalars(select(Engine).where(Engine.provider == "mock")):
            e.enabled = True
        rid = plan_run(s, s.scalar(select(Brand).where(Brand.slug == "amplience")).id, samples=1).run.id
    execute_run(rid, retry_sleep=0)
    with _client() as c:
        for p in ["/prism/", "/prism/b/amplience", "/prism/b/amplience/prompts", "/prism/b/amplience/sources",
                  "/prism/b/amplience/facts", "/prism/b/amplience/actions", "/prism/b/amplience/impact",
                  "/prism/b/amplience/audit", "/prism/b/amplience/crawlers", "/prism/b/amplience/runs",
                  "/prism/cc/", "/prism/cc/engines", "/prism/cc/keys", "/prism/cc/budget", "/prism/cc/schedules",
                  "/prism/cc/brands", "/prism/cc/brands/amplience", "/prism/cc/settings", "/prism/cc/tokens",
                  "/prism/cc/audit", "/prism/b/amplience/export.csv"]:
            r = c.get(p)
            assert r.status_code == 200, p
        html = c.get("/prism/b/amplience/prompts").text
        pid = re.search(r"/prism/b/amplience/prompts/(\d+)\"", html).group(1)
        assert c.get(f"/prism/b/amplience/prompts/{pid}").status_code == 200
        assert c.get("/prism/healthz").json() == {"ok": True}


def test_api_requires_token_in_entra_mode(db, monkeypatch):
    monkeypatch.setenv("PRISM_AUTH_MODE", "entra")
    from prism.config import get_settings
    get_settings.cache_clear()
    from prism.db import session_scope
    from prism.models import ApiToken
    from prism.web.auth import hash_token, new_token
    tok = new_token()
    with session_scope() as s:
        s.add(ApiToken(name="t", token_hash=hash_token(tok), scope="read"))
    with _client() as c:
        assert c.get("/prism/api/v1/brands").status_code == 401
        r = c.get("/prism/api/v1/brands", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200 and any(b["slug"] == "amplience" for b in r.json())
        assert c.get("/prism/cc/", follow_redirects=False).status_code == 303  # to login
        # ingest needs a write token
        r = c.post("/prism/api/v1/ingest/crawlers/amplience", content="x",
                   headers={"Authorization": f"Bearer {tok}", "Content-Type": "text/plain"})
        assert r.status_code == 401
