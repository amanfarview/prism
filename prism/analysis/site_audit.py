"""AI readiness audit of a brand's own site. Free (plain HTTP requests).

Checks the things that decide whether AI can read and quote a site:
robots.txt rules per AI bot, llms.txt, sitemap, and for key pages: does an AI
crawler get the same page a browser gets (no blocks, no cloaking), is the text
there without JavaScript, structured data, canonical, headings, question-shaped
passages. Prism recommends serving the SAME content to people and bots.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select

from ..models import Answer, Brand, Citation, SiteAudit
from ..netguard import safe_client

AI_BOTS = {
    "GPTBot": "OpenAI (training)", "OAI-SearchBot": "OpenAI (ChatGPT search)", "ChatGPT-User": "OpenAI (user fetch)",
    "ClaudeBot": "Anthropic (training)", "Claude-SearchBot": "Anthropic (search)", "Claude-User": "Anthropic (user fetch)",
    "PerplexityBot": "Perplexity (index)", "Perplexity-User": "Perplexity (user fetch)",
    "Google-Extended": "Google (Gemini training control)", "Googlebot": "Google (Search + AI Overviews)",
    "Bingbot": "Microsoft (Bing + Copilot)", "Applebot-Extended": "Apple (AI training control)",
    "meta-externalagent": "Meta (AI)", "CCBot": "Common Crawl", "Bytespider": "ByteDance",
}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")
GPT_UA = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"


def _robots_rules(txt: str) -> dict[str, list[tuple[str, str]]]:
    rules: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    last_was_agent = False
    for line in txt.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_was_agent:
                agents = []
            agents.append(v.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if k in ("allow", "disallow"):
            for a in agents:
                rules.setdefault(a, []).append((k, v))
    return rules


def bot_allowed(rules: dict, bot: str, path: str = "/") -> bool:
    group = rules.get(bot.lower()) or rules.get("*") or []
    best = ("allow", "")
    for kind, pat in group:
        if pat == "" and kind == "disallow":
            continue
        if path.startswith(pat.rstrip("*")) and len(pat) >= len(best[1]):
            best = (kind, pat)
    return best[0] == "allow"


def _text_words(html: str) -> int:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?s)<[^>]+>", " ", html)
    return len(re.findall(r"[A-Za-z]{2,}", txt))


def _page_checks(client: httpx.Client, url: str) -> dict:
    out = {"url": url}
    try:
        b = client.get(url, headers={"User-Agent": BROWSER_UA})
        g = client.get(url, headers={"User-Agent": GPT_UA})
    except httpx.HTTPError as e:
        return {**out, "error": str(e)[:200]}
    html = b.text if b.status_code < 400 else ""
    out["status_browser"], out["status_ai_bot"] = b.status_code, g.status_code
    wb, wg = _text_words(html), _text_words(g.text if g.status_code < 400 else "")
    out["words_no_js"], out["words_ai_bot"] = wb, wg
    out["same_for_ai"] = (g.status_code == b.status_code) and (wb == 0 or abs(wb - wg) / max(wb, 1) < 0.3)
    out["jsonld_types"] = sorted(set(re.findall(r'"@type"\s*:\s*"([^"]+)"', html)))[:12]
    out["has_title"] = bool(re.search(r"(?is)<title>\s*\S", html))
    out["has_meta_description"] = bool(re.search(r'(?is)<meta[^>]+name=["\']description["\']', html))
    out["canonical"] = bool(re.search(r'(?is)<link[^>]+rel=["\']canonical["\']', html))
    out["h1_count"] = len(re.findall(r"(?is)<h1[\s>]", html))
    heads = re.findall(r"(?is)<h[23][^>]*>(.*?)</h[23]>", html)
    out["question_headings"] = sum(1 for h in heads if "?" in re.sub("<[^>]+>", "", h))
    return out


def run_audit(s, brand: Brand, max_pages: int = 8, timeout: int = 20) -> SiteAudit:
    base = f"https://{brand.domain.strip('/')}" if brand.domain else ""
    checks: list[dict] = []
    pages: list[dict] = []
    if not base:
        a = SiteAudit(brand_id=brand.id, score=0, results={"checks": [{"name": "No domain set", "status": "fail",
                                                                         "fix": "Add the brand's domain."}]})
        s.add(a)
        return a
    with safe_client(timeout=timeout) as client:  # SSRF-guarded: see prism/netguard.py
        try:
            rb = client.get(urljoin(base, "/robots.txt"), headers={"User-Agent": BROWSER_UA})
            robots = rb.text if rb.status_code == 200 else ""
        except httpx.HTTPError:
            robots = ""
        rules = _robots_rules(robots)
        blocked = [b for b in AI_BOTS if not bot_allowed(rules, b)]
        search_bots = ["OAI-SearchBot", "ChatGPT-User", "Claude-SearchBot", "Claude-User", "PerplexityBot",
                       "Perplexity-User", "Googlebot", "Bingbot"]
        bad = [b for b in blocked if b in search_bots]
        checks.append({"name": "AI search bots allowed in robots.txt", "status": "fail" if bad else "pass",
                       "detail": ("Blocked: " + ", ".join(bad)) if bad else "All answer-time bots allowed",
                       "fix": "Allow OAI-SearchBot, ChatGPT-User, Claude-SearchBot, PerplexityBot, Googlebot and Bingbot."})
        checks.append({"name": "Training bots", "status": "info",
                       "detail": ("Blocked: " + ", ".join(b for b in blocked if b not in search_bots)) or "None blocked",
                       "fix": ""})
        try:
            lr = client.get(urljoin(base, "/llms.txt"), headers={"User-Agent": BROWSER_UA})
            has_llms = lr.status_code == 200 and len(lr.text.strip()) > 20 and "<html" not in lr.text[:200].lower()
        except httpx.HTTPError:
            has_llms = False
        checks.append({"name": "llms.txt present", "status": "pass" if has_llms else "warn",
                       "detail": "Found" if has_llms else "Not found",
                       "fix": "Publish /llms.txt summarising what you do, products and key pages (low effort, unproven upside)."})
        try:
            sm = client.get(urljoin(base, "/sitemap.xml"), headers={"User-Agent": BROWSER_UA})
            n_urls = len(re.findall(r"<loc>", sm.text)) if sm.status_code == 200 else 0
        except httpx.HTTPError:
            n_urls = 0
        checks.append({"name": "Sitemap", "status": "pass" if n_urls else "warn",
                       "detail": f"{n_urls} entries in /sitemap.xml" if n_urls else "No /sitemap.xml",
                       "fix": "Publish an XML sitemap and reference it in robots.txt."})
        urls = [base + "/"]
        cited = s.execute(select(Citation.url).join(Answer, Answer.id == Citation.answer_id)
                          .where(Answer.brand_id == brand.id, Citation.owner_type == "own").limit(200)).all()
        for (u,) in cited:
            u = u.split("?")[0]
            if u not in urls and urlparse(u).netloc.endswith(brand.domain):
                urls.append(u)
            if len(urls) >= max_pages:
                break
        pages = [_page_checks(client, u) for u in urls[:max_pages]]
    ok_pages = [p for p in pages if "error" not in p]
    diff = [p["url"] for p in ok_pages if not p.get("same_for_ai")]
    checks.append({"name": "AI crawlers get the same page as people", "status": "fail" if diff else "pass",
                   "detail": ("Different for: " + ", ".join(diff[:3])) if diff else f"{len(ok_pages)} pages checked",
                   "fix": "Serve AI bots exactly what people see. Bot-only pages risk cloaking penalties."})
    thin = [p["url"] for p in ok_pages if p.get("words_no_js", 0) < 150]
    checks.append({"name": "Text readable without JavaScript", "status": "fail" if thin else "pass",
                   "detail": ("Thin without JS: " + ", ".join(thin[:3])) if thin else "Content is in the HTML",
                   "fix": "Server-render key content; many AI fetchers don't run JavaScript."})
    no_ld = [p["url"] for p in ok_pages if not p.get("jsonld_types")]
    checks.append({"name": "Structured data (JSON-LD)", "status": "warn" if no_ld else "pass",
                   "detail": ("Missing on: " + ", ".join(no_ld[:3])) if no_ld else "Present",
                   "fix": "Add Organization, Product/SoftwareApplication, FAQPage and Article markup where true."})
    no_q = [p["url"] for p in ok_pages if p.get("question_headings", 0) == 0]
    checks.append({"name": "Question-shaped sections", "status": "warn" if len(no_q) == len(ok_pages) and ok_pages else "pass",
                   "detail": f"{len(ok_pages) - len(no_q)} of {len(ok_pages)} pages have question headings",
                   "fix": "Add short, direct answers under question headings that mirror buyer prompts."})
    weights = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
    scored = [c for c in checks if c["status"] in weights]
    score = round(100 * sum(weights[c["status"]] for c in scored) / len(scored), 1) if scored else 0
    a = SiteAudit(brand_id=brand.id, score=score, results={"checks": checks, "pages": pages,
                                                           "robots_found": bool(robots)})
    s.add(a)
    return a
