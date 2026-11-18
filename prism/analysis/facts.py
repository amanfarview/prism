"""Fact checking against the brand's fact sheet.

Free pass (always on): each fact can carry "wrong patterns", regexes that signal
a known false statement (an old price, a wrong HQ, "owned by X"). Any answer that
names the brand and matches one becomes a 'wrong' claim to fix.

The optional AI pass (llm_extract.py) checks every statement about the brand
against the whole fact sheet.
"""
from __future__ import annotations

import re

from sqlalchemy import delete, select

from ..models import Answer, Claim, Fact, Mention


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    r1, r2 = text.find(".", end), text.find("\n", end)
    cands = [x for x in (r1, r2) if x != -1]
    right = min(cands) + 1 if cands else len(text)
    return text[left:right].strip()[:600]


def pattern_check_answer(s, ans: Answer, facts: list[Fact], brand_name: str) -> int:
    s.execute(delete(Claim).where(Claim.answer_id == ans.id, Claim.method == "pattern"))
    named = s.scalar(select(Mention.id).where(Mention.answer_id == ans.id, Mention.entity_type == "self").limit(1))
    if not named:
        return 0
    n = 0
    for f in facts:
        for pat in f.wrong_patterns or []:
            try:
                rx = re.compile(pat, re.IGNORECASE)
            except re.error:
                continue
            m = rx.search(ans.response_text or "")
            if m:
                s.add(Claim(answer_id=ans.id, brand_id=ans.brand_id, subject=brand_name, attribute=f.attribute,
                            statement=_sentence_around(ans.response_text, m.start(), m.end()),
                            verdict="wrong", fact_id=f.id, method="pattern"))
                n += 1
                break
    return n


def pattern_check_run(s, run_id: int) -> int:
    answers = list(s.scalars(select(Answer).where(Answer.run_id == run_id, Answer.status == "ok")))
    if not answers:
        return 0
    from ..models import Brand
    brand = s.get(Brand, answers[0].brand_id)
    facts = list(s.scalars(select(Fact).where(Fact.brand_id == brand.id)))
    if not facts:
        return 0
    return sum(pattern_check_answer(s, a, facts, brand.name) for a in answers)
