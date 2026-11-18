"""Adapters build the right requests (URL, auth, payload) and survive errors. No network."""
import json

import httpx
import pytest
import respx

from prism.providers import CallContext, ProviderError, get_adapter


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for name in ["OPENAI", "ANTHROPIC", "GEMINI", "PERPLEXITY", "XAI", "DEEPSEEK"]:
        monkeypatch.setenv(f"PRISM_SECRET_PRISM_{name}_API_KEY", f"test-{name.lower()}-key")
    monkeypatch.setenv("PRISM_SECRET_PRISM_DATAFORSEO_CREDENTIALS", "login:pw")
    from prism import secrets
    secrets._cache.clear()


@respx.mock
def test_openai_request_shape():
    route = respx.post("https://api.openai.com/v1/responses").mock(return_value=httpx.Response(200, json={
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi", "annotations": []}]}],
        "usage": {"input_tokens": 1, "output_tokens": 1}}))
    r = get_adapter("openai_responses").call(CallContext(prompt="Best CMS?", model="gpt-5.5", mode="search", country="GB"))
    body = json.loads(route.calls[0].request.content)
    assert body["input"] == "Best CMS?"  # the question goes in unchanged
    assert body["tools"][0]["type"] == "web_search" and body["tools"][0]["user_location"]["country"] == "GB"
    assert route.calls[0].request.headers["authorization"] == "Bearer test-openai-key"
    assert r.text == "hi"


@respx.mock
def test_memory_mode_sends_no_tools():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json={
        "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}}))
    get_adapter("anthropic_messages").call(CallContext(prompt="q", model="claude-sonnet-4-6", mode="memory"))
    body = json.loads(route.calls[0].request.content)
    assert "tools" not in body and route.calls[0].request.headers["x-api-key"] == "test-anthropic-key"


@respx.mock
def test_gemini_and_dataforseo_auth():
    g = respx.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "a"}]}}]}))
    get_adapter("gemini_generate").call(CallContext(prompt="q", model="gemini-x", mode="search"))
    assert json.loads(g.calls[0].request.content)["tools"] == [{"google_search": {}}]
    assert g.calls[0].request.headers["x-goog-api-key"] == "test-gemini-key"
    d = respx.post("https://api.dataforseo.com/v3/serp/google/organic/live/advanced").mock(
        return_value=httpx.Response(200, json={"status_code": 20000, "cost": 0.002,
                                               "tasks": [{"status_code": 20000, "result": [{"items": []}]}]}))
    r = get_adapter("dataforseo_ai_overview").call(CallContext(prompt="best cms", mode="app"))
    assert d.calls[0].request.headers["authorization"].startswith("Basic ")
    assert json.loads(d.calls[0].request.content)[0]["load_async_ai_overview"] is True
    assert r.text == ""


@respx.mock
def test_errors_are_classified():
    respx.post("https://api.x.ai/v1/responses").mock(return_value=httpx.Response(429, text="slow down"))
    with pytest.raises(ProviderError) as e:
        get_adapter("xai_responses").call(CallContext(prompt="q", model="grok", mode="search"))
    assert e.value.retryable
    respx.post("https://api.deepseek.com/responses").mock(return_value=httpx.Response(400, text="bad model"))
    with pytest.raises(ProviderError) as e:
        get_adapter("deepseek_responses").call(CallContext(prompt="q", model="x", mode="search"))
    assert not e.value.retryable


def test_missing_key_message(monkeypatch):
    monkeypatch.delenv("PRISM_SECRET_PRISM_OPENAI_API_KEY")
    from prism import secrets
    secrets._cache.clear()
    with pytest.raises(ProviderError, match="Command Centre"):
        get_adapter("openai_responses").call(CallContext(prompt="q", model="m"))


@respx.mock
def test_site_audit_flags_cloaking_and_blocks(db):
    from sqlalchemy import select

    from prism.analysis.site_audit import run_audit
    from prism.models import Brand

    def home(request):
        ua = request.headers.get("user-agent", "")
        if "GPTBot" in ua:
            return httpx.Response(200, text="<html><body>short bot page</body></html>")
        words = " ".join(["content"] * 300)
        return httpx.Response(200, text=f'<html><head><title>T</title><script type="application/ld+json">{{"@type":"Organization"}}</script></head><body><h1>Hi</h1><h2>What is it?</h2><p>{words}</p></body></html>')
    respx.get("https://amplience.com/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: OAI-SearchBot\nDisallow: /\n"))
    respx.get("https://amplience.com/llms.txt").mock(return_value=httpx.Response(404))
    respx.get("https://amplience.com/sitemap.xml").mock(return_value=httpx.Response(200, text="<urlset><url><loc>x</loc></url></urlset>"))
    respx.get("https://amplience.com/").mock(side_effect=home)
    with db() as s:
        a = run_audit(s, s.scalar(select(Brand).where(Brand.slug == "amplience")))
        checks = {c["name"]: c["status"] for c in a.results["checks"]}
        assert checks["AI search bots allowed in robots.txt"] == "fail"
        assert checks["AI crawlers get the same page as people"] == "fail"
        assert checks["Structured data (JSON-LD)"] == "pass"
        assert 0 < a.score < 100


def test_file_secret_backend(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("PRISM_SECRET_BACKEND", "file")
    monkeypatch.setenv("PRISM_SECRETS_FILE", str(tmp_path / "s" / "secrets.json"))
    from prism.config import get_settings
    get_settings.cache_clear()
    from prism import secrets
    secrets._cache.clear()
    secrets.set_secret("prism-openai-api-key", "sk-test-123456")
    secrets._cache.clear()
    assert secrets.get_secret("prism-openai-api-key") == "sk-test-123456"
    assert oct(os.stat(tmp_path / "s" / "secrets.json").st_mode)[-3:] == "600"
