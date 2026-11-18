"""Default engines, budgets and settings, created on first start.

Prices are list prices noted in Nov 2026 and are only used for estimates and the
spend ledger. They are editable in Command Centre > Engines; check them against
each provider's pricing page before relying on the numbers.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import AppSetting, Budget, Engine, ProviderKey
from .providers.base import SECRET_NAMES

ENGINES = [
    # key, label, provider, adapter, mode, model, in$/M, out$/M, $/search, $/call, est_in, est_out, est_searches, settings
    ("chatgpt_search", "ChatGPT (live search)", "openai", "openai_responses", "search", "gpt-5.5",
     5.0, 30.0, 0.010, 0.0, 6000, 800, 1.5, {}),
    ("chatgpt_memory", "ChatGPT (memory)", "openai", "openai_responses", "memory", "gpt-5.5",
     5.0, 30.0, 0.0, 0.0, 60, 700, 0, {}),
    ("claude_search", "Claude (live search)", "anthropic", "anthropic_messages", "search", "claude-sonnet-4-6",
     3.0, 15.0, 0.010, 0.0, 8000, 800, 2, {"max_uses": 5, "tool_version": "web_search_20250305"}),
    ("claude_memory", "Claude (memory)", "anthropic", "anthropic_messages", "memory", "claude-sonnet-4-6",
     3.0, 15.0, 0.0, 0.0, 60, 700, 0, {}),
    ("gemini_search", "Gemini (Google Search grounding)", "gemini", "gemini_generate", "search", "gemini-3.7-flash",
     0.5, 3.0, 0.014, 0.0, 300, 900, 2, {}),
    ("gemini_memory", "Gemini (memory)", "gemini", "gemini_generate", "memory", "gemini-3.7-flash",
     0.5, 3.0, 0.0, 0.0, 60, 700, 0, {}),
    ("perplexity", "Perplexity (Sonar Pro)", "perplexity", "perplexity_sonar", "search", "sonar-pro",
     3.0, 15.0, 0.0, 0.010, 100, 800, 1, {"search_context_size": "medium"}),
    ("grok_search", "Grok (live search)", "xai", "xai_responses", "search", "grok-4.7",
     3.0, 15.0, 0.010, 0.0, 4000, 800, 1.5, {}),
    ("deepseek_search", "DeepSeek (live search)", "deepseek", "deepseek_responses", "search", "deepseek-flash",
     0.3, 1.2, 0.00075, 0.0, 4000, 800, 2, {}),
    ("meta_llama_memory", "Meta Llama (memory)", "openai_compat", "openai_compat_chat", "memory",
     "Llama-4-Maverick-17B-128E-Instruct-FP8", 0.2, 0.6, 0.0, 0.0, 60, 700, 0,
     {"base_url": "https://api.llama.com/compat/v1"}),
    # What people actually see in the consumer apps (paid data provider, carries the scraping risk)
    ("chatgpt_app", "ChatGPT app view (DataForSEO)", "dataforseo", "dataforseo_chatgpt_app", "app", "",
     0, 0, 0, 0.010, 0, 0, 0, {}),
    ("gemini_app", "Gemini app view (DataForSEO)", "dataforseo", "dataforseo_gemini_app", "app", "",
     0, 0, 0, 0.010, 0, 0, 0, {}),
    ("google_ai_overview", "Google AI Overviews (DataForSEO)", "dataforseo", "dataforseo_ai_overview", "app", "",
     0, 0, 0, 0.002, 0, 0, 0, {}),
    ("google_ai_mode", "Google AI Mode (DataForSEO)", "dataforseo", "dataforseo_ai_mode", "app", "",
     0, 0, 0, 0.004, 0, 0, 0, {}),
    # Free demo engines: exercise the whole app without spending anything
    ("demo_search", "Demo engine, search (no cost)", "mock", "mock", "search", "mock-1",
     0, 0, 0, 0, 0, 0, 0, {}),
    ("demo_memory", "Demo engine, memory (no cost)", "mock", "mock", "memory", "mock-1",
     0, 0, 0, 0, 0, 0, 0, {}),
]

DEFAULT_SETTINGS = {
    "llm_extraction": {"enabled": False, "engine_key": "", "provider": "anthropic",
                       "model": "claude-haiku-4-5", "price_in_per_m": 1.0, "price_out_per_m": 5.0},
    "defaults": {"samples": 3, "identical_alert_pct": 60},
    "stats": {"confidence": 0.95, "min_answers": 6},
}


def ensure_defaults(s) -> None:
    existing = {e.key for e in s.scalars(select(Engine))}
    for (key, label, provider, adapter, mode, model, pin, pout, psearch, pcall,
         ein, eout, esearch, settings) in ENGINES:
        if key in existing:
            continue
        s.add(Engine(key=key, label=label, provider=provider, adapter=adapter, mode=mode, model=model,
                     price_in_per_m=pin, price_out_per_m=pout, price_per_search=psearch, price_per_call=pcall,
                     est_in_tokens=ein, est_out_tokens=eout, est_searches=esearch, settings=settings,
                     enabled=False))
    have = {p.provider for p in s.scalars(select(ProviderKey))}
    for prov, secret in SECRET_NAMES.items():
        if prov not in have:
            s.add(ProviderKey(provider=prov, secret_name=secret))
    if not s.scalar(select(Budget).where(Budget.scope == "global")):
        s.add(Budget(scope="global", provider="", monthly_cap_usd=300.0))
    for k, v in DEFAULT_SETTINGS.items():
        if not s.get(AppSetting, k):
            s.add(AppSetting(key=k, value=v))
    s.flush()
