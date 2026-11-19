"""MCP server so Claude (Desktop, Code, Cowork) can ask Prism questions.

Read-only. Two ways to run:
  python -m prism.mcp_server            # stdio, e.g. Claude Code on the VM
  python -m prism.mcp_server --http     # streamable HTTP on 127.0.0.1:8041 behind Caddy,
                                        # every request needs a Prism API token (Bearer)
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from .analysis import actions as actions_mod
from .analysis import report
from .db import session_scope
from .models import Brand

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

server = _Server("prism", instructions=(
    "Prism measures how AI assistants (ChatGPT, Claude, Gemini, Perplexity, Grok, DeepSeek, Google AI) "
    "talk about Farview portfolio companies. Rates come with 95% ranges; quote them, don't overstate small changes."))


def _brand(s, slug: str) -> Brand:
    b = s.scalar(select(Brand).where(Brand.slug == slug.lower()))
    if not b:
        names = ", ".join(x.slug for x in s.scalars(select(Brand)))
        raise ValueError(f"Unknown brand '{slug}'. Known: {names}")
    return b


@server.tool()
def list_brands() -> list[dict]:
    """List tracked brands (portfolio companies) with their slugs."""
    with session_scope() as s:
        return [{"slug": b.slug, "name": b.name, "category": b.category} for b in s.scalars(select(Brand))]


@server.tool()
def brand_summary(slug: str, days: int = 30) -> dict:
    """Visibility, mention rate (with 95% range), rank vs competitors and trend for one brand."""
    with session_scope() as s:
        sm = report.brand_summary(s, _brand(s, slug), days)
        return {"answers": sm["answers"], "mention_rate": round(sm["rate"], 3),
                "mention_rate_95": [round(x, 3) for x in sm["rate_ci"]], "visibility": round(sm["visibility"], 1),
                "rank": sm["rank"], "of": sm["of"], "vs_previous_period": sm["direction"],
                "leaderboard": [{"name": e["name"], "mention_rate": round(e["rate"], 3)} for e in sm["board"][:10]]}


@server.tool()
def engine_breakdown(slug: str, days: int = 30) -> list[dict]:
    """How often the brand is named on each AI engine, split by memory vs live search."""
    with session_scope() as s:
        return [{"engine": r["label"], "mode": r["mode"], "answers": r["n"], "mention_rate": round(r["rate"], 3),
                 "leader": r["leader"]} for r in report.by_engine(s, _brand(s, slug), days)]


@server.tool()
def weakest_prompts(slug: str, days: int = 30, limit: int = 10) -> list[dict]:
    """Buyer questions where the brand is least visible relative to the leading competitor."""
    with session_scope() as s:
        rows = report.prompt_table(s, _brand(s, slug), days)
        return [{"prompt": r["prompt"].text, "mention_rate": round(r["rate"], 3), "answers": r["n"],
                 "top_competitor": r["top_competitor"], "top_competitor_rate": round(r["top_competitor_rate"], 3)}
                for r in rows if r["n"]][:limit]


@server.tool()
def top_sources(slug: str, days: int = 30, limit: int = 15) -> list[dict]:
    """Domains AI cites most for this brand's buyer questions, and how often the brand is absent."""
    with session_scope() as s:
        return report.sources(s, _brand(s, slug), days, limit)["domains"]


@server.tool()
def recommended_actions(slug: str, days: int = 30) -> list[dict]:
    """Ranked next steps with the evidence behind each."""
    with session_scope() as s:
        return [{"title": a.title, "why": a.why, "steps": a.steps} for a in actions_mod.build(s, _brand(s, slug), days)]


def _transport_security():
    """The SDK's default for a 127.0.0.1 server accepts only Host: localhost, so every request arriving through
    Caddy (Host: jarvis.farviewequity.com) got 421. Allow exactly the public host as well."""
    from .config import get_settings
    host = get_settings().public_host
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:  # very old SDK: nothing to configure
        return None
    hosts = ["127.0.0.1:*", "localhost:*"] + ([host] if host else [])
    origins = ["http://127.0.0.1:*", "http://localhost:*"] + ([f"https://{host}"] if host else [])
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def build_http_app():
    """Streamable HTTP app behind a token check. Every request needs a Prism API token (Bearer)."""
    from .web.auth import token_user
    sec = _transport_security()
    try:  # mcp >= 2
        app = server.streamable_http_app(transport_security=sec, host="127.0.0.1")
    except TypeError:  # mcp 1.x: settings live on the server object
        if sec is not None and hasattr(server, "settings"):
            server.settings.transport_security = sec
        app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            if scope.get("path") == "/mcp/":  # Caddy strips /prism; accept /prism/mcp and /prism/mcp/
                scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
            hdr = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            with session_scope() as s:
                ok = token_user(s, hdr) is not None
            if not ok:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"token required"}'})
                return
        await app(scope, receive, send)

    return guarded


def _http():  # pragma: no cover
    import uvicorn

    from .config import get_settings
    uvicorn.run(build_http_app(), host="127.0.0.1", port=get_settings().mcp_port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":  # pragma: no cover
    if "--http" in sys.argv:
        _http()
    else:
        server.run()
