"""Free, deterministic extraction from a stored answer: which tracked brands are
named, in what order, how often, and which sources were cited. No AI calls.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import delete, select

from ..models import Answer, Brand, Citation, Competitor, Mention


@dataclass
class Entity:
    type: str  # self|competitor
    name: str
    competitor_id: int | None
    domain: str
    pattern: re.Pattern


def _alt(name: str) -> str:
    """Single-word names match with their capitalisation ("Sanity", not "sanity check";
    "Interact", not "interact with"). Multi-word names match in any case."""
    if " " in name.strip() or "." in name:
        return f"(?i:{re.escape(name)})"
    return re.escape(name) + "|" + re.escape(name.upper()) if name[:1].isupper() else f"(?i:{re.escape(name)})"


def _pattern(names: list[str]) -> re.Pattern:
    alts = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    body = "|".join(_alt(a) for a in alts)
    return re.compile(rf"(?<![\w.-])(?:{body})(?![\w-])")


def entities_for(brand: Brand, competitors: list[Competitor]) -> list[Entity]:
    ents = [Entity("self", brand.name, None, (brand.domain or "").lower(),
                   _pattern([brand.name, *(brand.aliases or [])]))]
    for c in competitors:
        if c.active:
            ents.append(Entity("competitor", c.name, c.id, (c.domain or "").lower(),
                               _pattern([c.name, *(c.aliases or [])])))
    return ents


def find_mentions(text: str, ents: list[Entity]) -> list[tuple[Entity, int, int]]:
    """Returns (entity, first_offset, count) ordered by first appearance."""
    found = []
    for e in ents:
        hits = list(e.pattern.finditer(text or ""))
        if hits:
            found.append((e, hits[0].start(), len(hits)))
    found.sort(key=lambda x: x[1])
    return found


def norm_domain(url_or_domain: str) -> str:
    s = url_or_domain.strip().lower()
    if "//" in s:
        s = urlparse(s).netloc
    s = s.split("/")[0].split(":")[0]
    return s[4:] if s.startswith("www.") else s


def _owns(domain: str, owner: str) -> bool:
    owner = norm_domain(owner) if owner else ""
    return bool(owner) and (domain == owner or domain.endswith("." + owner))


def classify_url(url: str, ents: list[Entity]) -> tuple[str, str, str, str]:
    """-> (domain, path, owner_type, owner_name)"""
    try:
        p = urlparse(url)
        domain, path = norm_domain(p.netloc), p.path or "/"
    except ValueError:
        return "", "", "third_party", ""
    for e in ents:
        if _owns(domain, e.domain):
            return domain, path, ("own" if e.type == "self" else "competitor"), e.name
    return domain, path, "third_party", ""


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()


def analyse_answer(s, ans: Answer, ents: list[Entity]) -> None:
    """(Re)build mentions and citations for one answer. Idempotent."""
    # Rebuild tracked-brand mentions from scratch (AI-pass enrichment is redone on request)
    s.execute(delete(Mention).where(Mention.answer_id == ans.id, Mention.entity_type.in_(["self", "competitor"])))
    if ans.llm_analysed:
        ans.llm_analysed = False
    s.execute(delete(Citation).where(Citation.answer_id == ans.id))
    for pos, (e, off, cnt) in enumerate(find_mentions(ans.response_text, ents), start=1):
        s.add(Mention(answer_id=ans.id, entity_type=e.type, entity_name=e.name, competitor_id=e.competitor_id,
                      position=pos, char_offset=off, count=cnt))
    for i, c in enumerate(_answer_citations(ans), start=1):
        dom, path, otype, oname = classify_url(c["url"], ents)
        if not dom:
            continue
        s.add(Citation(answer_id=ans.id, url=c["url"][:2000], domain=dom[:200], path=path[:1000],
                       title=(c.get("title") or "")[:500], position=i, owner_type=otype, owner_name=oname))
    ans.analysed = True


def _answer_citations(ans: Answer) -> list[dict]:
    return [c for c in (ans.citations or []) if isinstance(c, dict) and c.get("url")]


def analyse_run(s, run_id: int) -> int:
    answers = list(s.scalars(select(Answer).where(Answer.run_id == run_id, Answer.status == "ok")))
    if not answers:
        return 0
    brand = s.get(Brand, answers[0].brand_id)
    ents = entities_for(brand, list(brand.competitors))
    for a in answers:
        analyse_answer(s, a, ents)
    return len(answers)
