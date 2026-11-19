"""Security behaviour that must not regress (Nov 2026 production hardening)."""
import socket

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


def _settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(f"PRISM_{k}", v)
    from prism.config import get_settings
    get_settings.cache_clear()
    return get_settings


# ---------------------------------------------------------------- fail closed
def test_default_is_sign_in_mode(monkeypatch):
    monkeypatch.delenv("PRISM_AUTH_MODE", raising=False)
    get = _settings(monkeypatch)
    assert get().auth_mode == "entra"


def test_dev_mode_refused_off_localhost(monkeypatch):
    get = _settings(monkeypatch, AUTH_MODE="dev", PUBLIC_URL="https://jarvis.farviewequity.com/prism")
    with pytest.raises(Exception, match="only allowed on localhost"):
        get()


def test_entra_mode_needs_real_session_secret(monkeypatch):
    get = _settings(monkeypatch, AUTH_MODE="entra", SESSION_SECRET="change-me")
    with pytest.raises(Exception, match="SESSION_SECRET"):
        get()


# ---------------------------------------------------------------- headers, cookie, body limit
def test_security_headers_and_cookie_path(db, monkeypatch):
    _settings(monkeypatch, AUTH_MODE="entra", PUBLIC_URL="https://jarvis.farviewequity.com/prism")
    from prism.web.app import create_app
    with TestClient(create_app(), base_url="https://testserver") as c:
        r = c.get("/prism/login", follow_redirects=False)  # sets the session (MSAL flow) before redirecting
        r2 = c.get("/prism/healthz")
    h = r2.headers
    assert "script-src 'self';" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"].split("style-src")[0]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    cookie = r.headers.get("set-cookie", "")
    if cookie:  # only set when Entra is configured enough to start a flow
        assert "path=/prism" in cookie.lower() and "httponly" in cookie.lower() and "secure" in cookie.lower()


def test_session_cookie_scoped_to_base_path(db):
    from prism.web.app import create_app
    with TestClient(create_app()) as c:
        c.get("/prism/cc/")  # dev mode: renders and stores a CSRF token in the session
        cookies = [ck for ck in c.cookies.jar if ck.name == "prism_session"]
    assert cookies and all(ck.path == "/prism" for ck in cookies)


def test_body_limit(db, monkeypatch):
    _settings(monkeypatch, MAX_BODY_MB="1")
    from prism.web.app import create_app
    with TestClient(create_app()) as c:
        r = c.post("/prism/api/v1/ingest/crawlers/amplience", content=b"x" * (2 * 1024 * 1024),
                   headers={"Content-Type": "text/plain"})
    assert r.status_code == 413


def test_ready_reports_db(db):
    from prism.web.app import create_app
    with TestClient(create_app()) as c:
        assert c.get("/prism/healthz/ready").json() == {"ok": True, "db": True}


# ---------------------------------------------------------------- admin is live, not frozen in the cookie
def test_admin_removed_takes_effect_immediately(monkeypatch):
    _settings(monkeypatch, AUTH_MODE="entra", PUBLIC_URL="https://x.example/prism", ADMIN_EMAILS="a@farviewequity.com")
    from prism.web import auth

    class Req:
        session = {"user": {"email": "a@farviewequity.com", "name": "A", "admin": True}}
    assert auth.current_user(Req()).is_admin
    _settings(monkeypatch, ADMIN_EMAILS="")
    assert not auth.current_user(Req()).is_admin


# ---------------------------------------------------------------- stored XSS
def test_fact_patterns_are_escaped(db):
    from prism.models import Brand, Fact
    with db() as s:
        b = s.scalar(select(Brand).where(Brand.slug == "amplience"))
        s.add(Fact(brand_id=b.id, attribute="x", value="y", wrong_patterns=["<script>alert(1)</script>"]))
    from prism.web.app import create_app
    with TestClient(create_app()) as c:
        html = c.get("/prism/b/amplience/facts").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_no_inline_scripts_in_templates():
    import re
    from pathlib import Path
    for p in (Path(__file__).resolve().parents[1] / "prism" / "web" / "templates").glob("*.html"):
        t = p.read_text()
        for m in re.finditer(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", t):
            raise AssertionError(f"inline <script> in {p.name}: CSP is script-src 'self'")
        assert not re.search(r"\son(submit|click|load|change|input)=", t), f"inline handler in {p.name}"


# ---------------------------------------------------------------- SSRF guard
@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "168.63.129.16", "::1", "::ffff:127.0.0.1",
                                "192.168.1.1", "100.64.0.1"])
def test_guard_refuses_internal_addresses(monkeypatch, ip):
    _settings(monkeypatch, BLOCK_PRIVATE_FETCH="true")
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(fam, socket.SOCK_STREAM, 6, "", (ip, 443))])
    from prism.netguard import BlockedURL, check_url
    with pytest.raises(BlockedURL):
        check_url(httpx.URL("https://innocent.example/"))


def test_guard_allows_public_and_checks_scheme_and_port(monkeypatch):
    _settings(monkeypatch, BLOCK_PRIVATE_FETCH="true")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    from prism.netguard import BlockedURL, check_url
    check_url(httpx.URL("https://example.com/"))
    for bad in ["file:///etc/passwd", "https://example.com:8040/", "gopher://example.com/"]:
        with pytest.raises(BlockedURL):
            check_url(httpx.URL(bad))


def test_guard_applies_to_redirects(monkeypatch):
    import respx
    _settings(monkeypatch, BLOCK_PRIVATE_FETCH="true")

    def fake(host, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1" if host == "evil.internal" else "93.184.216.34", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    from prism.netguard import BlockedURL, safe_client
    with respx.mock:
        respx.get("https://example.com/").mock(return_value=httpx.Response(302, headers={"location": "https://evil.internal/x"}))
        respx.get("https://evil.internal/x").mock(return_value=httpx.Response(200, text="secret"))
        with safe_client() as c, pytest.raises(BlockedURL):
            c.get("https://example.com/")


# ---------------------------------------------------------------- staging never spends
def test_staging_refuses_paid_calls(monkeypatch):
    _settings(monkeypatch, ENVIRONMENT="staging")
    monkeypatch.setenv("PRISM_SECRET_PRISM_OPENAI_API_KEY", "sk-test")
    from prism.providers.base import Adapter, ProviderError
    a = Adapter()
    a.provider = "openai"
    with pytest.raises(ProviderError, match="Staging"):
        a.key()


# ---------------------------------------------------------------- worker restart
def test_interrupted_runs_are_closed(db):
    from prism.models import Brand, Run
    from prism.scheduler import recover_interrupted
    with db() as s:
        b = s.scalar(select(Brand).where(Brand.slug == "amplience"))
        r1 = Run(brand_id=b.id, status="running", done_calls=3)
        r2 = Run(brand_id=b.id, status="running", done_calls=0)
        s.add_all([r1, r2])
        s.flush()
        ids = (r1.id, r2.id)
    assert sorted(recover_interrupted()) == sorted(ids)
    with db() as s:
        assert [s.get(Run, i).status for i in ids] == ["partial", "failed"]


# ---------------------------------------------------------------- Teams payload
def test_alerts_are_adaptive_cards():
    from prism.common import teams_card
    m = teams_card("Prism: x", ["y"])
    assert m["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


# ---------------------------------------------------------------- MCP over Caddy
def test_mcp_needs_token_and_accepts_public_host(db, monkeypatch):
    _settings(monkeypatch, AUTH_MODE="entra", PUBLIC_URL="https://jarvis.farviewequity.com/prism")
    from prism.mcp_server import build_http_app
    from prism.models import ApiToken
    from prism.web.auth import hash_token, new_token
    tok = new_token()
    with db() as s:
        s.add(ApiToken(name="t", token_hash=hash_token(tok), scope="read"))
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    auth = {"Authorization": f"Bearer {tok}"}
    with TestClient(build_http_app(), base_url="https://jarvis.farviewequity.com") as c:
        assert c.post("/mcp/", json=init, headers=hdr).status_code == 401
        r = c.post("/mcp/", json=init, headers={**hdr, **auth})  # the Host Caddy forwards
        bad = c.post("/mcp", json=init, headers={**hdr, **auth, "Host": "attacker.example"})
    assert r.status_code == 200, r.text[:300]
    assert bad.status_code == 421


# ---------------------------------------------------------------- Azure SQL DDL
def test_mssql_ddl_is_unicode_and_datetime2():
    from sqlalchemy.dialects import mssql
    from sqlalchemy.schema import CreateTable

    from prism.models import Answer, AuditLog
    ddl = str(CreateTable(Answer.__table__).compile(dialect=mssql.dialect())) + \
        str(CreateTable(AuditLog.__table__).compile(dialect=mssql.dialect()))
    assert "NVARCHAR(max)" in ddl and "DATETIME2" in ddl
    assert " VARCHAR(" not in ddl and " DATETIME," not in ddl and " DATETIME " not in ddl
