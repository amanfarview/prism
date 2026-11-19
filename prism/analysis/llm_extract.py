"""Optional AI pass (off by default, costs money, budget-guarded).

One cheap-model call per answer adds what regexes can't: whether each tracked
brand is actually recommended, the tone used, brands we don't track yet, and
every statement about the client brand checked against its fact sheet.
"""
from __future__ import annotations

import json
import re

import httpx
from sqlalchemy import delete, select

from ..budget import BudgetGuard, record_spend
from ..common import get_setting, log
from ..models import Answer, Brand, Claim, Fact, Mention
from ..secrets import get_secret
from ..providers.base import SECRET_NAMES

SYSTEM = ("You read an AI assistant's answer to a buyer's question and extract structured facts. "
          "Be literal: only report what the answer actually says. Return JSON only, no prose.")

TEMPLATE = """Question asked: {question}

Tracked brands: {tracked}
Client brand: {client}

Client brand fact sheet (source of truth):
{facts}

Answer to analyse:
<<<
{answer}
>>>

Return JSON exactly in this shape:
{{"entities": [{{"name": "<tracked brand name>", "named": true, "recommended": true, "sentiment": 0.0}}],
 "other_brands": ["<vendor names in the answer that are not tracked>"],
 "client_claims": [{{"attribute": "<topic>", "statement": "<what the answer says about the client brand, short>",
                     "verdict": "correct|wrong|unverifiable", "fact_attribute": "<matching fact sheet attribute or empty>"}}]}}
Rules: include every tracked brand in entities. sentiment is -1 (negative) to 1 (positive), 0 if neutral or not named.
recommended is true only if the answer recommends it for the question. verdict is "wrong" only when the
fact sheet contradicts the statement; "unverifiable" when the fact sheet does not cover it."""


def _call(cfg: dict, prompt: str) -> tuple[dict, int, int]:
    provider, model = cfg.get("provider", "anthropic"), cfg.get("model")
    from ..config import get_settings
    if get_settings().is_staging:
        raise RuntimeError("Staging never makes paid calls")
    key = get_secret(SECRET_NAMES[provider])
    if not key:
        raise RuntimeError(f"No {provider} key for AI extraction")
    if provider == "anthropic":
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=90, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 1200, "system": SYSTEM,
                  "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return _json(text), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
    r = httpx.post("https://api.openai.com/v1/responses", timeout=90,
                   headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                   json={"model": model, "instructions": SYSTEM, "input": prompt,
                         "text": {"format": {"type": "json_object"}}})
    r.raise_for_status()
    d = r.json()
    text = "".join(c.get("text", "") for o in d.get("output", []) if o.get("type") == "message"
                   for c in o.get("content", []))
    u = d.get("usage", {})
    return _json(text), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def extract_run(s, run_id: int, user: str = "system") -> dict:
    cfg = get_setting(s, "llm_extraction", {}) or {}
    if not cfg.get("enabled"):
        return {"skipped": "AI extraction is off (Command Centre > Settings)"}
    answers = list(s.scalars(select(Answer).where(Answer.run_id == run_id, Answer.status == "ok",
                                                  Answer.llm_analysed.is_(False))))
    if not answers:
        return {"done": 0}
    brand = s.get(Brand, answers[0].brand_id)
    tracked = [brand.name] + [c.name for c in brand.competitors if c.active]
    facts = list(s.scalars(select(Fact).where(Fact.brand_id == brand.id)))
    fact_txt = "\n".join(f"- {f.attribute}: {f.value}" for f in facts) or "(none provided)"
    by_attr = {f.attribute.lower(): f for f in facts}
    pin, pout = float(cfg.get("price_in_per_m", 1.0)), float(cfg.get("price_out_per_m", 5.0))
    est = 1500 * pin / 1e6 + 500 * pout / 1e6
    guard = BudgetGuard(s)
    done = spent = 0.0
    errors = 0
    for a in answers:
        if not (a.response_text or "").strip():
            a.llm_analysed = True
            continue
        if not guard.reserve(cfg.get("provider", "anthropic"), est):
            break
        try:
            data, tin, tout = _call(cfg, TEMPLATE.format(question=a.prompt_text, tracked=", ".join(tracked),
                                                         client=brand.name, facts=fact_txt,
                                                         answer=a.response_text[:12000]))
        except Exception as e:  # keep going, report at end
            guard.settle(cfg.get("provider", "anthropic"), est, 0)
            errors += 1
            log.warning("AI extraction failed for answer %s: %s", a.id, e)
            continue
        cost = tin * pin / 1e6 + tout * pout / 1e6
        guard.settle(cfg.get("provider", "anthropic"), est, cost)
        record_spend(s, provider=cfg.get("provider", "anthropic"), engine_key="extraction", cost=cost,
                     brand_id=brand.id, run_id=run_id, purpose="extract")
        spent += cost
        _apply(s, a, data, brand, by_attr)
        a.llm_analysed = True
        done += 1
    return {"done": int(done), "errors": errors, "cost_usd": round(spent, 4),
            "stopped": guard.reason if guard.stopped else ""}


def _apply(s, a: Answer, data: dict, brand: Brand, by_attr: dict) -> None:
    ms = {m.entity_name.lower(): m for m in s.scalars(select(Mention).where(Mention.answer_id == a.id))}
    for e in data.get("entities", []) or []:
        m = ms.get(str(e.get("name", "")).lower())
        if m:
            try:
                m.sentiment = max(-1.0, min(1.0, float(e.get("sentiment", 0))))
            except (TypeError, ValueError):
                pass
            if isinstance(e.get("recommended"), bool):
                m.recommended = e["recommended"]
    s.execute(delete(Mention).where(Mention.answer_id == a.id, Mention.entity_type == "other"))
    for i, name in enumerate((data.get("other_brands") or [])[:15], start=1):
        if isinstance(name, str) and name.strip():
            s.add(Mention(answer_id=a.id, entity_type="other", entity_name=name.strip()[:200], position=100 + i))
    s.execute(delete(Claim).where(Claim.answer_id == a.id, Claim.method == "llm"))
    for c in (data.get("client_claims") or [])[:20]:
        verdict = c.get("verdict") if c.get("verdict") in ("correct", "wrong", "unverifiable") else "unverifiable"
        f = by_attr.get(str(c.get("fact_attribute", "")).lower())
        s.add(Claim(answer_id=a.id, brand_id=brand.id, subject=brand.name,
                    attribute=str(c.get("attribute", ""))[:200], statement=str(c.get("statement", ""))[:1000],
                    verdict=verdict, fact_id=f.id if f else None, method="llm"))
