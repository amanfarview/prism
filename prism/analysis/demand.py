"""Prompt ideas from real demand: Search Console exports and on-site search terms.

Free. Reads a CSV (GSC 'Queries' export, or any file with a query column and an
impressions/count column) and returns the long-tail, question-like or comparison
queries that are closest to how people ask AI. Nothing is added until you pick.
"""
from __future__ import annotations

import csv
import io
import re

QUERY_COLS = ("top queries", "query", "queries", "search term", "search_term", "term", "keyword")
VOL_COLS = ("impressions", "clicks", "searches", "count", "volume")
SIGNAL = re.compile(r"\b(best|top|vs|versus|alternative|alternatives|compare|comparison|which|what|how|why|"
                    r"should|recommend|review|reviews|for|cheapest|enterprise|pricing)\b", re.I)


def suggestions(raw: bytes | str, brand_name: str, limit: int = 60) -> list[dict]:
    text = raw.decode("utf-8-sig", "ignore") if isinstance(raw, bytes) else raw
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    qi = next((i for i, h in enumerate(head) if h in QUERY_COLS), 0)
    vi = next((i for i, h in enumerate(head) if h in VOL_COLS), None)
    out = []
    for r in rows[1:]:
        if len(r) <= qi:
            continue
        q = r[qi].strip()
        if not q:
            continue
        try:
            vol = int(float((r[vi] if vi is not None and len(r) > vi else "0").replace(",", "")))
        except ValueError:
            vol = 0
        words = q.split()
        branded = brand_name.lower() in q.lower()
        score = (2 if SIGNAL.search(q) else 0) + (1 if len(words) >= 4 else 0) + (1 if q.endswith("?") else 0)
        if score < 2:
            continue
        prompt = q if q.endswith("?") or q[0].isupper() else q[0].upper() + q[1:]
        out.append({"query": q, "prompt": prompt, "volume": vol, "branded": branded, "score": score})
    out.sort(key=lambda x: (-x["score"], -x["volume"]))
    return out[:limit]
