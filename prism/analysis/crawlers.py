"""AI crawler analytics from server/CDN logs.

Accepts either combined-format access log lines (nginx/Apache) or JSON lines with
user_agent/path/status/timestamp fields (Vercel/Cloudflare exports). Only AI bot
hits are kept. User agents can be spoofed, so treat counts as indicative.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from ..models import CrawlerHit, now

BOTS = [  # (substring in UA, bot, operator, purpose)
    ("OAI-SearchBot", "OAI-SearchBot", "OpenAI", "search"), ("ChatGPT-User", "ChatGPT-User", "OpenAI", "user"),
    ("GPTBot", "GPTBot", "OpenAI", "training"),
    ("Claude-SearchBot", "Claude-SearchBot", "Anthropic", "search"), ("Claude-User", "Claude-User", "Anthropic", "user"),
    ("ClaudeBot", "ClaudeBot", "Anthropic", "training"),
    ("Perplexity-User", "Perplexity-User", "Perplexity", "user"), ("PerplexityBot", "PerplexityBot", "Perplexity", "search"),
    ("Google-CloudVertexBot", "Google-CloudVertexBot", "Google", "user"),
    ("Googlebot", "Googlebot", "Google", "search"), ("bingbot", "Bingbot", "Microsoft", "search"),
    ("meta-externalagent", "Meta-ExternalAgent", "Meta", "training"), ("meta-externalfetcher", "Meta-ExternalFetcher", "Meta", "user"),
    ("Applebot", "Applebot", "Apple", "search"), ("Amazonbot", "Amazonbot", "Amazon", "search"),
    ("Bytespider", "Bytespider", "ByteDance", "training"), ("CCBot", "CCBot", "Common Crawl", "training"),
    ("MistralAI-User", "MistralAI-User", "Mistral", "user"), ("DuckAssistBot", "DuckAssistBot", "DuckDuckGo", "user"),
]
COMBINED = re.compile(r'\S+ \S+ \S+ \[(?P<ts>[^\]]+)\] "(?:\S+) (?P<path>\S+)[^"]*" (?P<status>\d{3}) \S+ "[^"]*" "(?P<ua>[^"]*)"')


def classify(ua: str):
    for sub, bot, op, purpose in BOTS:
        if sub.lower() in (ua or "").lower():
            return bot, op, purpose
    return None


def _parse_ts(ts: str) -> datetime:
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(ts, fmt)
            return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone(tz=None).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(float(ts) / (1000 if float(ts) > 1e11 else 1))
    except (TypeError, ValueError):
        return now()


def ingest(s, brand_id: int, body: str) -> dict:
    kept = seen = 0
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        seen += 1
        ua = path = ts = None
        status = 200
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ua = d.get("user_agent") or d.get("userAgent") or d.get("ClientRequestUserAgent") or ""
            path = d.get("path") or d.get("ClientRequestPath") or d.get("url") or ""
            status = int(d.get("status") or d.get("EdgeResponseStatus") or 200)
            ts = str(d.get("timestamp") or d.get("EdgeStartTimestamp") or "")
        else:
            m = COMBINED.match(line)
            if not m:
                continue
            ua, path, status, ts = m["ua"], m["path"], int(m["status"]), m["ts"]
        c = classify(ua)
        if not c:
            continue
        bot, op, purpose = c
        s.add(CrawlerHit(brand_id=brand_id, at=_parse_ts(ts) if ts else now(), bot=bot, operator=op,
                         purpose=purpose, path=(path or "")[:1000], status=status))
        kept += 1
    return {"lines": seen, "ai_hits": kept}
