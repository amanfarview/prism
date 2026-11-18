"""Concrete adapters for each AI engine.

API shapes checked against provider docs in Nov 2026:
- OpenAI Responses API with the `web_search` tool ($10 per 1k calls + content tokens)
- Anthropic Messages API with the `web_search_*` server tool ($10 per 1k searches)
- Gemini generateContent with `google_search` grounding ($14 per 1k queries after free tier)
- Perplexity Sonar chat completions (citations + search_results) or the Agent API
- xAI Responses API with `web_search`; DeepSeek Responses API with `deepseek_web_search`
- Any OpenAI-compatible chat endpoint (e.g. Meta's Llama API) for memory-only checks
- DataForSEO for what consumer apps show (ChatGPT / Gemini scrapers, AI Overviews, AI Mode)

Every adapter is defensive when parsing: if a provider adds fields we keep going,
and the full raw JSON is stored with the answer for later re-parsing.
"""
from __future__ import annotations

import hashlib
import random
from urllib.parse import urlparse

import httpx

from .base import (Adapter, CallContext, CitationRef, EngineResult,
                   ProviderError, user_location)

COUNTRY_NAMES = {
    "GB": "United Kingdom", "US": "United States", "IE": "Ireland", "DE": "Germany",
    "FR": "France", "NL": "Netherlands", "ES": "Spain", "IT": "Italy", "SE": "Sweden",
    "DK": "Denmark", "NO": "Norway", "FI": "Finland", "AU": "Australia", "CA": "Canada",
    "SG": "Singapore", "IN": "India", "AE": "United Arab Emirates",
}


def _dedupe(cits: list[CitationRef]) -> list[CitationRef]:
    """One entry per URL; a URL counts as cited in the text if any occurrence is."""
    out: dict[str, CitationRef] = {}
    for c in cits:
        u = (c.url or "").strip()
        if not u:
            continue
        if u in out:
            out[u].cited_in_text = out[u].cited_in_text or c.cited_in_text
            out[u].title = out[u].title or c.title
        else:
            out[u] = CitationRef(u, c.title, c.cited_in_text)
    return list(out.values())


# ------------------------------------------------------------------ Responses API family
class ResponsesAdapter(Adapter):
    """OpenAI Responses-format endpoints: OpenAI, xAI, DeepSeek, Perplexity Agent API."""

    def __init__(self, provider: str, name: str, base_url: str, path: str, search_tool: str,
                 supports_location: bool = False):
        self.provider, self.name = provider, name
        self.base_url, self.path, self.search_tool = base_url, path, search_tool
        self.supports_location = supports_location

    def build_payload(self, ctx: CallContext) -> dict:
        payload: dict = {"model": ctx.model, "input": ctx.prompt}
        if ctx.mode == "search":
            tool: dict = {"type": ctx.settings.get("search_tool", self.search_tool)}
            if self.supports_location:
                tool["user_location"] = user_location(ctx)
            payload["tools"] = [tool]
        if ctx.settings.get("preset"):
            payload["preset"] = ctx.settings["preset"]
        if ctx.settings.get("reasoning_effort"):
            payload["reasoning"] = {"effort": ctx.settings["reasoning_effort"]}
        return payload

    def call(self, ctx: CallContext) -> EngineResult:
        base = ctx.settings.get("base_url") or self.base_url
        url = base.rstrip("/") + "/" + self.path.lstrip("/")
        data, ms = self.post(url, headers={"Authorization": f"Bearer {self.key()}",
                                            "Content-Type": "application/json"},
                             payload=self.build_payload(ctx))
        res = parse_responses(data)
        res.latency_ms, res.model = ms, data.get("model") or ctx.model
        if ctx.mode != "search":
            res.search_calls = 0
        return res


def parse_responses(data: dict) -> EngineResult:
    texts: list[str] = []
    cits: list[CitationRef] = []
    queries: list[str] = []
    search_calls = 0
    for item in data.get("output", []) or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    texts.append(c.get("text", ""))
                    for a in c.get("annotations", []) or []:
                        if a.get("url"):
                            cits.append(CitationRef(a["url"], a.get("title", "")))
                    for a in c.get("citations", []) or []:  # DeepSeek style
                        if a.get("url"):
                            cits.append(CitationRef(a["url"], a.get("title", "")))
        elif t in ("web_search_call", "deepseek_web_search_call", "search_call"):
            search_calls += 1
            act = item.get("action") or {}
            q = act.get("query") or item.get("query")
            if q:
                queries.append(q)
            for q in act.get("queries", []) or []:
                queries.append(q)
            for s in act.get("sources", []) or []:
                if isinstance(s, dict) and s.get("url"):
                    cits.append(CitationRef(s["url"], s.get("title", ""), cited_in_text=False))
        elif t == "search_results":  # Perplexity Agent API
            search_calls += 1
            for r in item.get("results", []) or []:
                if r.get("url"):
                    cits.append(CitationRef(r["url"], r.get("title", ""), cited_in_text=False))
            for q in item.get("queries", []) or []:
                queries.append(q)
    for u in data.get("citations", []) or []:  # xAI top-level list
        if isinstance(u, str):
            cits.append(CitationRef(u, ""))
        elif isinstance(u, dict) and u.get("url"):
            cits.append(CitationRef(u["url"], u.get("title", "")))
    text = "".join(texts).strip() or (data.get("output_text") or "")
    usage = data.get("usage") or {}
    stu = usage.get("server_tool_use") or {}
    search_calls = max(search_calls, int(stu.get("web_search_requests", 0) or 0))
    # Prefer citations that appear in the text first, then retrieved-only sources
    cits = sorted(_dedupe(cits), key=lambda c: (not c.cited_in_text))
    return EngineResult(text=text, citations=cits, search_queries=queries,
                        input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        search_calls=search_calls, raw=data)


# ------------------------------------------------------------------ Anthropic
class AnthropicAdapter(Adapter):
    provider, name = "anthropic", "anthropic_messages"

    def call(self, ctx: CallContext) -> EngineResult:
        payload: dict = {
            "model": ctx.model,
            "max_tokens": int(ctx.settings.get("max_tokens", 2048)),
            "messages": [{"role": "user", "content": ctx.prompt}],
        }
        if ctx.mode == "search":
            tool = {"type": ctx.settings.get("tool_version", "web_search_20250305"),
                    "name": "web_search", "max_uses": int(ctx.settings.get("max_uses", 5)),
                    "user_location": user_location(ctx)}
            payload["tools"] = [tool]
        headers = {"x-api-key": self.key(), "content-type": "application/json",
                   "anthropic-version": ctx.settings.get("api_version", "2023-06-01")}
        data, ms = self.post(ctx.settings.get("base_url", "https://api.anthropic.com") + "/v1/messages",
                             headers=headers, payload=payload)
        res = parse_anthropic(data)
        res.latency_ms, res.model = ms, data.get("model") or ctx.model
        return res


def parse_anthropic(data: dict) -> EngineResult:
    texts, cits, queries = [], [], []
    for b in data.get("content", []) or []:
        t = b.get("type")
        if t == "text":
            texts.append(b.get("text", ""))
            for c in b.get("citations", []) or []:
                if c.get("url"):
                    cits.append(CitationRef(c["url"], c.get("title", "")))
        elif t == "server_tool_use":
            q = (b.get("input") or {}).get("query")
            if q:
                queries.append(q)
        elif t == "web_search_tool_result":
            content = b.get("content")
            if isinstance(content, list):
                for r in content:
                    if isinstance(r, dict) and r.get("url"):
                        cits.append(CitationRef(r["url"], r.get("title", ""), cited_in_text=False))
    usage = data.get("usage") or {}
    stu = usage.get("server_tool_use") or {}
    cits = sorted(_dedupe(cits), key=lambda c: (not c.cited_in_text))
    return EngineResult(text="".join(texts).strip(), citations=cits, search_queries=queries,
                        input_tokens=int(usage.get("input_tokens", 0) or 0)
                        + int(usage.get("cache_read_input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        search_calls=int(stu.get("web_search_requests", len(queries)) or 0), raw=data)


# ------------------------------------------------------------------ Gemini
class GeminiAdapter(Adapter):
    provider, name = "gemini", "gemini_generate"

    def call(self, ctx: CallContext) -> EngineResult:
        base = ctx.settings.get("base_url", "https://generativelanguage.googleapis.com/v1beta")
        url = f"{base}/models/{ctx.model}:generateContent"
        payload: dict = {"contents": [{"role": "user", "parts": [{"text": ctx.prompt}]}]}
        if ctx.mode == "search":
            payload["tools"] = [{"google_search": {}}]
        data, ms = self.post(url, headers={"x-goog-api-key": self.key(), "Content-Type": "application/json"},
                             payload=payload)
        res = parse_gemini(data)
        res.latency_ms, res.model = ms, data.get("modelVersion") or ctx.model
        if ctx.mode != "search":
            res.search_calls = 0
        return res


def parse_gemini(data: dict) -> EngineResult:
    cands = data.get("candidates") or []
    if not cands:
        fb = data.get("promptFeedback") or {}
        raise ProviderError(f"Gemini returned no candidates {fb}")
    c0 = cands[0]
    parts = (c0.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    gm = c0.get("groundingMetadata") or {}
    cits = []
    for ch in gm.get("groundingChunks", []) or []:
        web = ch.get("web") or {}
        uri, title = web.get("uri", ""), web.get("title", "")
        if not uri:
            continue
        # Gemini returns redirect URIs; the title carries the real domain.
        if "vertexaisearch" in uri and title and "." in title and " " not in title:
            uri = f"https://{title}/"
        cits.append(CitationRef(uri, title))
    queries = gm.get("webSearchQueries") or []
    um = data.get("usageMetadata") or {}
    return EngineResult(text=text, citations=_dedupe(cits), search_queries=list(queries),
                        input_tokens=int(um.get("promptTokenCount", 0) or 0),
                        output_tokens=int(um.get("candidatesTokenCount", 0) or 0)
                        + int(um.get("thoughtsTokenCount", 0) or 0),
                        search_calls=len(queries), raw=data)


# ------------------------------------------------------------------ chat completions family
class ChatCompletionsAdapter(Adapter):
    """Perplexity Sonar (search built in) and any OpenAI-compatible chat endpoint."""

    def __init__(self, provider: str, name: str, base_url: str, search_builtin: bool):
        self.provider, self.name, self.base_url, self.search_builtin = provider, name, base_url, search_builtin

    def call(self, ctx: CallContext) -> EngineResult:
        base = ctx.settings.get("base_url") or self.base_url
        if not base:
            raise ProviderError("No base_url configured for this engine (Command Centre > Engines).")
        payload: dict = {"model": ctx.model, "messages": [{"role": "user", "content": ctx.prompt}]}
        if self.search_builtin:
            payload["web_search_options"] = {"user_location": {"country": ctx.country}}
            if ctx.settings.get("search_context_size"):
                payload["web_search_options"]["search_context_size"] = ctx.settings["search_context_size"]
        data, ms = self.post(base.rstrip("/") + "/chat/completions",
                             headers={"Authorization": f"Bearer {self.key()}", "Content-Type": "application/json"},
                             payload=payload)
        res = parse_chat(data, self.search_builtin)
        res.latency_ms, res.model = ms, data.get("model") or ctx.model
        return res


def parse_chat(data: dict, search_builtin: bool) -> EngineResult:
    ch = (data.get("choices") or [{}])[0]
    text = ((ch.get("message") or {}).get("content") or "").strip()
    cits: list[CitationRef] = []
    for r in data.get("search_results", []) or []:
        if r.get("url"):
            cits.append(CitationRef(r["url"], r.get("title", "")))
    for u in data.get("citations", []) or []:
        if isinstance(u, str):
            cits.append(CitationRef(u, ""))
    usage = data.get("usage") or {}
    cost = 0.0
    if isinstance(usage.get("cost"), dict):
        cost = float(usage["cost"].get("total_cost", 0) or 0)
    return EngineResult(text=text, citations=_dedupe(cits),
                        input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                        output_tokens=int(usage.get("completion_tokens", 0) or 0),
                        search_calls=1 if (search_builtin and cits) else 0,
                        raw=data, extra_cost_usd=cost)


# ------------------------------------------------------------------ DataForSEO (consumer view)
class DataForSEOAdapter(Adapter):
    """What a person actually sees: scraped ChatGPT/Gemini app answers, AI Overviews, AI Mode.

    The provider carries the scraping risk; we pay per result. Response shapes vary
    by endpoint, so parsing looks for markdown/text and any url-bearing references.
    """
    provider = "dataforseo"

    ENDPOINTS = {
        "dataforseo_chatgpt_app": "/v3/ai_optimization/chat_gpt/llm_scraper/live/advanced",
        "dataforseo_gemini_app": "/v3/ai_optimization/gemini/llm_scraper/live/advanced",
        "dataforseo_ai_overview": "/v3/serp/google/organic/live/advanced",
        "dataforseo_ai_mode": "/v3/serp/google/ai_mode/live/advanced",
    }

    def __init__(self, name: str):
        self.name = name

    def call(self, ctx: CallContext) -> EngineResult:
        cred = self.key()
        if ":" not in cred:
            raise ProviderError("DataForSEO credential must be 'login:password'")
        login, pw = cred.split(":", 1)
        task = {"keyword": ctx.prompt, "location_name": COUNTRY_NAMES.get(ctx.country, "United Kingdom"),
                "language_code": ctx.language}
        if self.name == "dataforseo_ai_overview":
            task["load_async_ai_overview"] = True
        base = ctx.settings.get("base_url", "https://api.dataforseo.com")
        data, ms = self.post(base + self.ENDPOINTS[self.name], headers={"Content-Type": "application/json"},
                             payload=[task], auth=httpx.BasicAuth(login, pw))
        res = parse_dataforseo(data, self.name)
        res.latency_ms, res.model = ms, self.name
        return res


def _walk(o, fn):
    if isinstance(o, dict):
        fn(o)
        for v in o.values():
            _walk(v, fn)
    elif isinstance(o, list):
        for v in o:
            _walk(v, fn)


def parse_dataforseo(data: dict, name: str) -> EngineResult:
    if data.get("status_code") not in (20000, None):
        raise ProviderError(f"DataForSEO {data.get('status_code')}: {data.get('status_message')}")
    tasks = data.get("tasks") or []
    if tasks and tasks[0].get("status_code") not in (20000, None):
        t = tasks[0]
        raise ProviderError(f"DataForSEO task {t.get('status_code')}: {t.get('status_message')}")
    result = ((tasks[0].get("result") or [{}])[0]) if tasks else {}
    scope = result
    if name in ("dataforseo_ai_overview",):
        aio = [i for i in (result.get("items") or []) if i.get("type") == "ai_overview"]
        if not aio:
            return EngineResult(text="", raw={"note": "no AI Overview shown for this query", "cost": data.get("cost")},
                                extra_cost_usd=float(data.get("cost") or 0))
        scope = aio[0]
    text = scope.get("markdown") or ""
    if not text:
        bits: list[str] = []
        _walk(scope, lambda d: bits.append(d["text"]) if isinstance(d.get("text"), str) else None)
        text = "\n".join(bits)
    cits: list[CitationRef] = []

    def grab(d):
        if isinstance(d.get("url"), str) and d["url"].startswith("http") and d.get("type") not in ("organic",):
            cits.append(CitationRef(d["url"], d.get("title") or d.get("domain") or ""))
    for key in ("references", "sources", "links"):
        _walk(scope.get(key), grab)
    if not cits:
        _walk(scope, grab)
    return EngineResult(text=text.strip(), citations=_dedupe(cits), search_calls=1, raw=data,
                        extra_cost_usd=float(data.get("cost") or 0))


# ------------------------------------------------------------------ Mock (demo + tests, free)
class MockAdapter(Adapter):
    """Writes plausible answers naming tracked brands, so the whole app can be
    exercised without spending money. Never used for real reporting."""
    provider, name = "mock", "mock"

    def key(self) -> str:
        return "mock"

    def call(self, ctx: CallContext) -> EngineResult:
        ents: list[dict] = ctx.settings.get("_entities", [])
        seed = int(hashlib.sha256(f"{ctx.prompt}|{ctx.settings.get('_salt','')}|{ctx.model}".encode()).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        bias = ctx.settings.get("_bias", {})
        picked = [e for e in ents if rnd.random() < bias.get(e["name"], 0.45)]
        rnd.shuffle(picked)
        picked = picked[:6]
        if not picked:
            text = ("There isn't one clear answer here. It depends on your budget, the size of your team "
                    "and the systems you already run. Shortlist a few vendors and ask for a demo.")
        else:
            lines = [f"Here are some options worth considering for this ({ctx.country} market):", ""]
            for i, e in enumerate(picked, 1):
                adj = rnd.choice(["is widely used", "is strong on integrations", "suits large teams",
                                  "is often praised for ease of use", "is a good fit for enterprise"])
                lines.append(f"{i}. **{e['name']}** {adj}.")
            lines += ["", "The right choice depends on your requirements."]
            text = "\n".join(lines)
        cits = []
        if ctx.mode == "search":
            pool = [e.get("domain") for e in ents if e.get("domain")] + [
                "g2.com", "gartner.com", "reddit.com", "youtube.com", "techradar.com", "capterra.com"]
            for d in rnd.sample(pool, k=min(len(pool), rnd.randint(2, 6))):
                cits.append(CitationRef(f"https://{d}/{rnd.choice(['', 'blog/guide', 'compare', 'pricing'])}", d))
        return EngineResult(text=text, citations=cits, search_queries=[ctx.prompt] if ctx.mode == "search" else [],
                            input_tokens=40, output_tokens=len(text) // 4,
                            search_calls=1 if ctx.mode == "search" else 0, raw={"mock": True}, latency_ms=5)


REGISTRY: dict[str, Adapter] = {
    "openai_responses": ResponsesAdapter("openai", "openai_responses", "https://api.openai.com/v1", "responses",
                                         "web_search", supports_location=True),
    "xai_responses": ResponsesAdapter("xai", "xai_responses", "https://api.x.ai/v1", "responses", "web_search"),
    "deepseek_responses": ResponsesAdapter("deepseek", "deepseek_responses", "https://api.deepseek.com", "responses",
                                           "deepseek_web_search"),
    "perplexity_agent": ResponsesAdapter("perplexity", "perplexity_agent", "https://api.perplexity.ai/v1", "agent",
                                         "web_search"),
    "anthropic_messages": AnthropicAdapter(),
    "gemini_generate": GeminiAdapter(),
    "perplexity_sonar": ChatCompletionsAdapter("perplexity", "perplexity_sonar", "https://api.perplexity.ai", True),
    "openai_compat_chat": ChatCompletionsAdapter("openai_compat", "openai_compat_chat", "", False),
    "dataforseo_chatgpt_app": DataForSEOAdapter("dataforseo_chatgpt_app"),
    "dataforseo_gemini_app": DataForSEOAdapter("dataforseo_gemini_app"),
    "dataforseo_ai_overview": DataForSEOAdapter("dataforseo_ai_overview"),
    "dataforseo_ai_mode": DataForSEOAdapter("dataforseo_ai_mode"),
    "mock": MockAdapter(),
}


def get_adapter(name: str) -> Adapter:
    if name not in REGISTRY:
        raise ProviderError(f"Unknown adapter {name}")
    return REGISTRY[name]


def domain_of(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h
