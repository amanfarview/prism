"""Provider adapter interface.

An adapter turns (engine config, prompt, market) into an EngineResult holding the
full answer text, citations, search queries and usage. Adapters never reformat
the question (no "list 10 brands" instructions): the prompt goes in exactly as a
buyer would type it, so the answer is what a buyer would read.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import get_settings
from ..secrets import get_secret

RAW_CAP = 60_000  # characters of provider JSON kept per answer


@dataclass
class CitationRef:
    url: str
    title: str = ""
    cited_in_text: bool = True


@dataclass
class EngineResult:
    text: str
    citations: list[CitationRef] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    search_calls: int = 0
    model: str = ""
    raw: Any = None
    latency_ms: int = 0
    extra_cost_usd: float = 0.0  # provider-reported cost (e.g. DataForSEO)

    def raw_text(self) -> str:
        try:
            s = json.dumps(self.raw, ensure_ascii=False, default=str)
        except Exception:
            s = str(self.raw)
        return s[:RAW_CAP]


class ProviderError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


@dataclass
class CallContext:
    """What an adapter needs for one call."""
    prompt: str
    country: str = "GB"
    language: str = "en"
    city: str = ""
    model: str = ""
    mode: str = "search"
    settings: dict = field(default_factory=dict)


SECRET_NAMES = {
    "openai": "prism-openai-api-key",
    "anthropic": "prism-anthropic-api-key",
    "gemini": "prism-gemini-api-key",
    "perplexity": "prism-perplexity-api-key",
    "xai": "prism-xai-api-key",
    "deepseek": "prism-deepseek-api-key",
    "openai_compat": "prism-openai-compat-api-key",
    "dataforseo": "prism-dataforseo-credentials",  # "login:password"
}


class Adapter:
    provider: str = ""
    name: str = ""

    def key(self) -> str:
        k = get_secret(SECRET_NAMES[self.provider])
        if not k:
            raise ProviderError(f"No API key set for {self.provider}. Add it in Command Centre > Keys.")
        return k

    def call(self, ctx: CallContext) -> EngineResult:  # pragma: no cover - interface
        raise NotImplementedError

    # shared HTTP helper with sane retry semantics
    def post(self, url: str, *, headers: dict, payload: dict | list, auth=None) -> tuple[dict, int]:
        t0 = time.time()
        timeout = get_settings().request_timeout_s
        try:
            r = httpx.post(url, headers=headers, json=payload, timeout=timeout, auth=auth)
        except httpx.TimeoutException as e:
            raise ProviderError(f"timeout calling {self.provider}", retryable=True) from e
        except httpx.HTTPError as e:
            raise ProviderError(f"network error calling {self.provider}: {e}", retryable=True) from e
        ms = int((time.time() - t0) * 1000)
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderError(f"{self.provider} HTTP {r.status_code}: {r.text[:300]}", retryable=True)
        if r.status_code >= 400:
            raise ProviderError(f"{self.provider} HTTP {r.status_code}: {r.text[:500]}")
        try:
            return r.json(), ms
        except ValueError as e:
            raise ProviderError(f"{self.provider} returned non-JSON: {r.text[:200]}") from e


def user_location(ctx: CallContext) -> dict:
    loc = {"type": "approximate", "country": ctx.country}
    if ctx.city:
        loc["city"] = ctx.city
    return loc
