# Prism

Farview's AI visibility (GEO) platform for portfolio companies: how ChatGPT, Claude, Gemini,
Perplexity, Grok, DeepSeek and Google's AI surfaces talk about Amplience, Unily, Exclaimer and
Grantify, what to do about it, and whether it worked.

Built after the Trakkr technical DD (Nov 2026) to avoid the weaknesses found there.

## What it does differently

| Trakkr (what we saw) | Prism |
|---|---|
| 1 answer per engine per prompt per scan | 3+ samples per prompt per engine; every rate has a 95% range |
| Stored "answers" were 10-item brand lists | The buyer's question goes in unchanged; the full answer is stored word for word |
| Second daily scan identical to the first | Freshness check on every run; alert if answers repeat |
| 5 of 8 engines had no live search | Each engine is labelled memory, search or app, and reported separately |
| Before/after on one sample a day | Difference-in-differences vs untouched prompts, with a bootstrap range |
| AI Pages: different page for bots | Site audit checks bots get the same page as people (no cloaking) |
| URL-level citations only | Cited pages traced to the Amplience content item behind them |
| No spend control visible | Pre-run estimate, monthly caps (overall and per provider), hard stop, Teams alerts |
| Keys/API tied to a person | Keys in Azure Key Vault; service tokens for API and MCP |
| Public /health leaks internals | /healthz says `{"ok": true}` and nothing else; no public API docs |

## Pieces

- **Collector** (`prism/collector.py`, `prism/providers/`): OpenAI Responses + web_search, Anthropic +
  web_search, Gemini + Google Search grounding, Perplexity Sonar or Agent API, xAI, DeepSeek,
  any OpenAI-compatible endpoint (e.g. Meta Llama API), DataForSEO (ChatGPT/Gemini app view, AI
  Overviews, AI Mode), and free demo engines.
- **Analysis** (`prism/analysis/`): free brand/position/citation extraction, Wilson ranges and
  significance tests, freshness, fact checks by pattern, optional AI pass (recommended?, tone,
  untracked vendors, every claim vs the fact sheet), ranked actions, impact (DiD), site audit,
  AI crawler log analytics, prompt ideas from Search Console / site-search CSVs, Amplience
  content tracing.
- **Web** (`prism/web/`): portfolio view, brand pages, Command Centre (engines, keys, budget,
  schedules, brands, settings, API tokens, audit log), REST API.
- **Worker** (`prism.cli worker`): scheduled and queued runs, outside the web process.
- **MCP server** (`prism/mcp_server.py`): read-only tools for Claude, token protected.

## Run locally (free, demo engines only)

```bash
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
export PRISM_AUTH_MODE=dev PRISM_DATABASE_URL=sqlite:///./demo.db   # dev mode must be asked for; default is sign-in
venv/bin/python scripts/demo_data.py
venv/bin/uvicorn prism.web.main:app --port 8040
# open http://127.0.0.1:8040/prism/   (dev mode: no login, you're admin; refused unless the URL is localhost)
venv/bin/python -m pytest -q
```

Demo answers are synthetic. Use them to learn the app, never for reporting.

## Costs

Every real call is paid. Defaults (list prices noted Nov 2026, editable in Command Centre > Engines):
one weekly run of the four portcos at 3 samples on the nine API engines is about **$23**, so about
**$100/month**, under the default **$300** cap. Testing each engine once costs about $0.26.
The AI pass, if switched on, adds roughly $0.003 per answer with a Haiku-class model.
Check prices against each provider's pricing page before relying on the numbers.

Production: see `DEPLOY.md` (Jarvis VM, own service user, `deploy/install.sh`, then `deploy/update.sh` for every deploy).
